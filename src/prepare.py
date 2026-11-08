from src import config


def _sql_list(values):
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


def macro_statements():
    placeholders = _sql_list(config.PLACEHOLDER_VALUES)
    suffixes = "|".join(config.COMPANY_SUFFIXES)
    joined_suffixes = "|".join(config.COMPANY_SUFFIXES_NO_SPACES)
    prefixes = "|".join(config.COMPANY_PREFIXES)

    return [
        rf"""
        CREATE OR REPLACE MACRO clean_text(x) AS
        CASE
            WHEN x IS NULL THEN NULL
            WHEN NOT regexp_matches(x, '[\pL\pN]') THEN NULL
            WHEN length(x) <= 20 AND lower(trim(x)) IN ({placeholders}) THEN NULL
            ELSE trim(regexp_replace(x, '\s+', ' ', 'g'))
        END
        """,
        rf"""
        CREATE OR REPLACE MACRO clean_description(x) AS
        CASE
            WHEN x IS NULL THEN NULL
            WHEN NOT regexp_matches(x, '[\pL\pN]') THEN NULL
            WHEN length(x) <= 20 AND lower(trim(x)) IN ({placeholders}) THEN NULL
            WHEN regexp_matches(lower(right(x, 24)), '(show less|see more)\s*$')
                THEN nullif(trim(regexp_replace(
                    x, '\s*(…|\.\.\.)?\s*(show less|see more)\s*$', '', 'i')), '')
            ELSE trim(x)
        END
        """,
        r"""
        CREATE OR REPLACE MACRO clean_url(x) AS
        nullif(
            regexp_replace(
                regexp_replace(
                    url_decode(
                        regexp_replace(
                            regexp_replace(lower(trim(x)), '^[a-z]+://', ''),
                            '[?#].*$', '')),
                    '^[^/]*linkedin\.com', 'linkedin.com'),
                '/+$', ''),
            '')
        """,
        r"""
        CREATE OR REPLACE MACRO norm_text(x) AS
        nullif(trim(regexp_replace(
            strip_accents(lower(x)), '[^\pL\pN]+', ' ', 'g')), '')
        """,
        rf"""
        CREATE OR REPLACE MACRO strip_company_words(k) AS
        regexp_replace(
            regexp_replace(
                regexp_replace(
                    regexp_replace(k, '( ({suffixes}))+$', ''),
                    '^(.{{2,}}?)({joined_suffixes})$', '\1'),
                '^({joined_suffixes})(.{{2,}})$', '\2'),
            '^({prefixes}) (.+)$', '\2')
        """,
        rf"""
        CREATE OR REPLACE MACRO norm_company(x) AS
        nullif(trim(strip_company_words(norm_text(x))), '')
        """,
        r"""
        CREATE OR REPLACE MACRO location_as_date(x) AS
        CASE WHEN length(x) BETWEEN 8 AND 15 AND regexp_matches(
                lower(x),
                '^(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(19|20)\d{2}$')
             THEN coalesce(
                 try_strptime(regexp_replace(x, '(?i)^sept', 'Sep'), '%b %Y'),
                 try_strptime(x, '%B %Y'))::DATE
        END
        """,
    ]


def date_expression(column, type_name):
    if type_name and type_name.upper().split("(")[0] in ("DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE",
                                                         "TIMESTAMP_NS", "TIMESTAMP_MS", "TIMESTAMP_S"):
        return f"{column}::DATE"
    return f"TRY_CAST(nullif(trim({column}::VARCHAR), '') AS DATE)"


def prepare_statements(input_glob, url_column, start_type="DATE", end_type="DATE"):

    earliest = config.EARLIEST_PLAUSIBLE_DATE
    max_after = config.MAX_DAYS_AFTER_OBSERVATION

    start = date_expression("startdate", start_type)
    end = date_expression("enddate", end_type)
    raw_columns = (
        "company_raw::VARCHAR, ({u})::VARCHAR, title_raw::VARCHAR, location_raw::VARCHAR, "
        "description::VARCHAR, {start}, {end}"
    ).format(u=url_column, start=start, end=end)
    source = f"""
        read_parquet('{input_glob}', union_by_name = true)
        WHERE user_id IS NOT NULL AND observed_ts IS NOT NULL
    """

    return macro_statements() + [
        f"""
        CREATE OR REPLACE TABLE cleaned AS
        WITH distinct_rows AS (
            SELECT DISTINCT ON (rh) *
            FROM (
                SELECT
                    hash({raw_columns}) AS rh,
                    company_raw::VARCHAR  AS company_raw,
                    ({url_column})::VARCHAR AS url,
                    title_raw::VARCHAR    AS title_raw,
                    location_raw::VARCHAR AS location_raw,
                    description::VARCHAR  AS description,
                    {start}               AS startdate,
                    {end}                 AS enddate
                FROM {source}
            )
        ),
        step1 AS (
            SELECT
                rh,
                clean_text(company_raw)         AS company_raw,
                clean_url(url)                  AS company_linkedin_url,
                clean_text(title_raw)           AS title_raw,
                clean_text(location_raw)        AS location_raw,
                clean_description(description)  AS description,
                startdate,
                enddate
            FROM distinct_rows
        ),
        step2 AS (
            SELECT *, location_as_date(location_raw) AS location_date FROM step1
        ),
        step3 AS (
            SELECT
                rh,
                company_raw,
                company_linkedin_url,
                title_raw,
                CASE WHEN location_date IS NULL THEN location_raw END AS location_raw,
                description,
                CASE WHEN startdate IS NULL AND enddate IS NULL THEN location_date
                     ELSE startdate END                              AS startdate,
                CASE WHEN startdate IS NULL AND enddate IS NULL THEN location_date
                     ELSE enddate END                                AS enddate,
                norm_company(company_raw)                            AS c_norm,
                company_linkedin_url                                 AS u_norm,
                norm_text(title_raw)                                 AS t_norm
            FROM step2
        )
        SELECT
            *,
            hash(company_raw, company_linkedin_url, title_raw, location_raw,
                 description, startdate, enddate)                    AS content,
            hash(coalesce(c_norm, u_norm), t_norm, startdate,
                 CASE WHEN startdate IS NULL THEN enddate END)       AS dup_key,
            (company_raw IS NOT NULL)::INT + (company_linkedin_url IS NOT NULL)::INT
              + (title_raw IS NOT NULL)::INT + (location_raw IS NOT NULL)::INT
              + (description IS NOT NULL)::INT + (enddate IS NOT NULL)::INT AS filled,
            (c_norm IS NOT NULL OR u_norm IS NOT NULL OR t_norm IS NOT NULL) AS usable
        FROM step3
        """,
        f"""
        CREATE OR REPLACE TABLE obs AS
        SELECT user_id, observed_ts, order_in_profile AS ord, hash({raw_columns}) AS rh
        FROM {source}
        """,
        f"""
        CREATE OR REPLACE TABLE snap_check AS
        SELECT
            o.user_id,
            o.observed_ts,
            bool_or(c.enddate < c.startdate)                               AS end_before_start,
            bool_or(least(c.startdate, c.enddate) < DATE '{earliest}')     AS too_old,
            bool_or(greatest(c.startdate, c.enddate)
                    > o.observed_ts::DATE + INTERVAL {max_after} DAY)       AS too_far_future,
            NOT bool_or(c.usable)                                           AS no_content
        FROM obs o
        JOIN cleaned c USING (rh)
        GROUP BY o.user_id, o.observed_ts
        """,
        """
        CREATE OR REPLACE TABLE snaps AS
        SELECT
            user_id,
            observed_ts,
            dense_rank() OVER (PARTITION BY user_id ORDER BY observed_ts)::INTEGER AS snap_idx
        FROM snap_check
        WHERE NOT (coalesce(end_before_start, false) OR coalesce(too_old, false)
                   OR coalesce(too_far_future, false) OR no_content)
        """,
        """
        CREATE OR REPLACE TABLE occ AS
        SELECT
            o.user_id,
            o.observed_ts,
            s.snap_idx,
            o.ord,
            o.rh,
            hash(o.user_id, c.content) AS vid
        FROM obs o
        JOIN snaps s USING (user_id, observed_ts)
        JOIN cleaned c USING (rh)
        WHERE c.usable
        QUALIFY row_number() OVER (
            PARTITION BY o.user_id, o.observed_ts, c.dup_key
            ORDER BY c.enddate ASC NULLS LAST, c.filled DESC, o.ord
        ) = 1
        """,
        """
        CREATE OR REPLACE TABLE versions AS
        WITH v AS (
            SELECT
                user_id,
                vid,
                arg_min(snap_idx, (snap_idx, ord)) AS first_snap,
                arg_min(ord, (snap_idx, ord))      AS first_ord,
                any_value(rh)                      AS rh
            FROM occ
            GROUP BY user_id, vid
        )
        SELECT
            v.user_id, v.vid, v.first_snap, v.first_ord,
            c.c_norm, c.u_norm, c.t_norm,
            date_diff('day', DATE '1970-01-01', c.startdate) AS start_day,
            date_diff('day', DATE '1970-01-01', c.enddate)   AS end_day,
            c.company_raw, c.company_linkedin_url, c.title_raw, c.location_raw,
            c.description, c.startdate, c.enddate
        FROM v
        JOIN cleaned c USING (rh)
        """,
        """
        CREATE OR REPLACE TABLE users AS
        SELECT user_id, max(snap_idx) AS n_snaps
        FROM snaps
        GROUP BY user_id
        """,
    ]


def stats_query():
    return """
    SELECT
        (SELECT count(*) FROM obs)                                        AS rows_read,
        (SELECT count(*) FROM snap_check)                                 AS snapshots_in,
        (SELECT count(*) FROM snaps)                                      AS snapshots_kept,
        (SELECT count(*) FROM snap_check WHERE end_before_start)          AS bad_end_before_start,
        (SELECT count(*) FROM snap_check WHERE too_old)                   AS bad_too_old,
        (SELECT count(*) FROM snap_check WHERE too_far_future)            AS bad_future,
        (SELECT count(*) FROM snap_check WHERE no_content)                AS bad_no_content,
        (SELECT count(*) FROM occ)                                        AS records_after_dedup,
        (SELECT count(*) FROM versions)                                   AS distinct_versions,
        (SELECT count(*) FROM users)                                      AS users,
        (SELECT count(*) FROM users WHERE n_snaps > 1)                    AS users_multi_snapshot
    """
