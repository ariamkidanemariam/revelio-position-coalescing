OUTPUT_COLUMNS = (
    "asof_ts", "user_id", "company_raw", "company_linkedin_url", "title_raw",
    "location_raw", "description", "startdate", "enddate", "position_id",
)

FIELDS = (
    "company_raw", "company_linkedin_url", "title_raw", "location_raw",
    "description", "startdate", "enddate",
)


def output_statements(position_csvs, output_dir):
    if position_csvs:
        csv_list = "[" + ", ".join("'" + p + "'" for p in position_csvs) + "]"
        matched = f"""
            SELECT user_id, vid, pos_n
            FROM read_csv({csv_list}, header = true,
                columns = {{'user_id': 'BIGINT', 'vid': 'UBIGINT', 'pos_n': 'INTEGER'}})
        """
    else:
        matched = "SELECT NULL::BIGINT AS user_id, NULL::UBIGINT AS vid, NULL::INTEGER AS pos_n WHERE false"

    fill = ",\n            ".join(
        [f"last_value(v.{f} IGNORE NULLS) OVER w AS {f}" for f in FIELDS if f != "startdate"]
        + ["c.start_pick.s AS startdate", "c.start_pick.e AS start_end"]
    )
    out_fields = ",\n                ".join(
        "CASE WHEN st.enddate < st.startdate THEN st.start_end ELSE st.enddate END AS enddate"
        if f == "enddate" else f"st.{f}"
        for f in FIELDS
    )

    return [
        f"""
        CREATE OR REPLACE TABLE posmap AS
        {matched}
        UNION ALL
        SELECT v.user_id, v.vid,
               row_number() OVER (PARTITION BY v.user_id ORDER BY v.first_ord, v.vid)::INTEGER
        FROM versions v
        JOIN users u USING (user_id)
        WHERE u.n_snaps = 1
        """,
        """
        CREATE OR REPLACE TABLE pos_obs AS
        SELECT o.user_id, m.pos_n, o.snap_idx, o.vid
        FROM occ o
        JOIN posmap m USING (user_id, vid)
        JOIN versions v USING (user_id, vid)
        QUALIFY row_number() OVER (
            PARTITION BY o.user_id, m.pos_n, o.snap_idx
            ORDER BY v.first_snap DESC, o.ord, o.vid) = 1
        """,
        """
        CREATE OR REPLACE TABLE pos_start AS
        SELECT user_id, pos_n, snap_idx, vid,
               arg_max(CASE WHEN startdate IS NOT NULL THEN struct_pack(s := startdate, e := enddate) END,
                       CASE WHEN startdate IS NOT NULL THEN struct_pack(c := seen, k := snap_idx) END)
                   OVER (PARTITION BY user_id, pos_n ORDER BY snap_idx
                         ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS start_pick
        FROM (
            SELECT o.user_id, o.pos_n, o.snap_idx, o.vid, v.startdate, v.enddate,
                   count(*) OVER (PARTITION BY o.user_id, o.pos_n, v.startdate ORDER BY o.snap_idx
                                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS seen
            FROM pos_obs o
            JOIN versions v USING (user_id, vid)
        )
        """,
        f"""
        CREATE OR REPLACE TABLE pos_state AS
        WITH changes AS (
            SELECT user_id, pos_n, snap_idx, vid, start_pick
            FROM pos_start
            QUALIFY vid IS DISTINCT FROM lag(vid) OVER (PARTITION BY user_id, pos_n ORDER BY snap_idx)
                 OR start_pick IS DISTINCT FROM lag(start_pick) OVER (PARTITION BY user_id, pos_n ORDER BY snap_idx)
        )
        SELECT c.user_id, c.pos_n, c.snap_idx,
            {fill}
        FROM changes c
        JOIN versions v USING (user_id, vid)
        WINDOW w AS (PARTITION BY c.user_id, c.pos_n ORDER BY c.snap_idx
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
        """,
        """
        CREATE OR REPLACE TABLE pos_first AS
        SELECT user_id, pos_n, min(snap_idx) AS first_snap
        FROM pos_obs
        GROUP BY user_id, pos_n
        """,
        f"""
        COPY (
            SELECT
                g.observed_ts AS asof_ts,
                g.user_id,
                {out_fields},
                g.user_id::VARCHAR || '-' || g.pos_n::VARCHAR AS position_id
            FROM (
                SELECT s.user_id, s.observed_ts, s.snap_idx, p.pos_n
                FROM snaps s
                JOIN pos_first p ON p.user_id = s.user_id AND s.snap_idx >= p.first_snap
            ) g
            ASOF JOIN pos_state st
                ON st.user_id = g.user_id
               AND st.pos_n = g.pos_n
               AND g.snap_idx >= st.snap_idx
        ) TO '{output_dir}' (FORMAT PARQUET, COMPRESSION SNAPPY,
                             PER_THREAD_OUTPUT true, OVERWRITE_OR_IGNORE true)
        """,
    ]


def check_queries(output_dir):
    out = f"read_parquet('{output_dir}/data_*.parquet')"
    return [
        ("output rows", f"SELECT count(*) FROM {out}", None),
        ("positions", f"SELECT count(DISTINCT position_id) FROM {out}", None),
        ("largest number of positions for one user",
         f"SELECT max(n) FROM (SELECT count(DISTINCT position_id) AS n FROM {out} GROUP BY user_id)", None),
        ("users with more than 50 positions",
         f"SELECT count(*) FROM (SELECT user_id FROM {out} GROUP BY user_id "
         f"HAVING count(DISTINCT position_id) > 50)", None),
        ("start date changes between consecutive snapshots of a position",
         f"""SELECT count(*) FROM (
                SELECT startdate, lag(startdate) OVER (PARTITION BY position_id ORDER BY asof_ts) AS before
                FROM {out}) WHERE before IS NOT NULL AND startdate IS DISTINCT FROM before""", None),
        ("duplicate (position_id, asof_ts)",
         f"SELECT count(*) - count(DISTINCT (position_id, asof_ts)) FROM {out}", 0),
        ("output snapshots not in the input",
         f"SELECT count(*) FROM (SELECT DISTINCT user_id, asof_ts FROM {out} "
         f"EXCEPT SELECT DISTINCT user_id, observed_ts FROM obs)", 0),
        ("rows with enddate before startdate", f"SELECT count(*) FROM {out} WHERE enddate < startdate", 0),
        ("positions missing from a later snapshot of their user",
         f"""SELECT count(*) FROM (
                SELECT position_id FROM {out}
                JOIN (SELECT user_id, asof_ts,
                             dense_rank() OVER (PARTITION BY user_id ORDER BY asof_ts) AS k
                      FROM (SELECT DISTINCT user_id, asof_ts FROM {out})) s USING (user_id, asof_ts)
                GROUP BY position_id HAVING max(k) - min(k) + 1 <> count(*))""", 0),
    ]
