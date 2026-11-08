import os
import shutil
import sys

os.environ["KEEP_WORK"] = "1"
os.environ.setdefault("WORK_DIR", "/tmp/end_date_check_work")
sys.path.insert(0, os.getcwd())

import duckdb

from src import config, main

RULES = {
    "old rule: most complete, then higher on profile": "row_number() OVER (PARTITION BY g ORDER BY filled DESC, ord)",
    "higher on profile": "row_number() OVER (PARTITION BY g ORDER BY ord)",
    "latest end date, 'present' counts as latest": "row_number() OVER (PARTITION BY g ORDER BY enddate DESC NULLS FIRST, ord)",
    "latest end date, 'present' counts as earliest": "row_number() OVER (PARTITION BY g ORDER BY enddate DESC NULLS LAST, ord)",
    "earliest end date (rule used by the pipeline)": "row_number() OVER (PARTITION BY g ORDER BY enddate ASC NULLS LAST, ord)",
}

SETUP = """
CREATE OR REPLACE TABLE r AS
SELECT o.user_id, o.observed_ts, o.ord, c.dup_key, c.enddate, c.filled,
       c.company_raw, c.title_raw, c.startdate,
       hash(o.user_id, o.observed_ts, c.dup_key) AS g
FROM obs o JOIN cleaned c USING (rh) JOIN snaps USING (user_id, observed_ts)
WHERE c.usable;

CREATE OR REPLACE TABLE conflict AS
SELECT g, any_value(user_id) AS user_id, any_value(observed_ts) AS observed_ts, any_value(dup_key) AS dup_key,
       CASE WHEN count(DISTINCT enddate) > 1 THEN 'two different end dates' ELSE 'end date vs present' END AS kind
FROM r GROUP BY g
HAVING count(*) > 1 AND (count(DISTINCT enddate) > 1 OR count(enddate) BETWEEN 1 AND count(*) - 1);

CREATE OR REPLACE TABLE single AS
SELECT user_id, observed_ts, dup_key, any_value(enddate) AS enddate
FROM r GROUP BY user_id, observed_ts, dup_key HAVING count(*) = 1;

CREATE OR REPLACE TABLE neighbour AS
SELECT c.g, c.kind,
       (SELECT s.enddate FROM single s WHERE s.user_id = c.user_id AND s.dup_key = c.dup_key
        AND s.observed_ts < c.observed_ts ORDER BY s.observed_ts DESC LIMIT 1) AS before_end,
       (SELECT count(*) FROM single s WHERE s.user_id = c.user_id AND s.dup_key = c.dup_key
        AND s.observed_ts < c.observed_ts) > 0 AS has_before,
       (SELECT s.enddate FROM single s WHERE s.user_id = c.user_id AND s.dup_key = c.dup_key
        AND s.observed_ts > c.observed_ts ORDER BY s.observed_ts LIMIT 1) AS after_end,
       (SELECT count(*) FROM single s WHERE s.user_id = c.user_id AND s.dup_key = c.dup_key
        AND s.observed_ts > c.observed_ts) > 0 AS has_after
FROM conflict c;
"""


def kept(rule_sql):
    return f"""
        SELECT g, enddate AS kept_end FROM (
            SELECT r.g, r.enddate, {rule_sql} AS rk FROM r JOIN conflict USING (g)) WHERE rk = 1
    """


def main_cli():
    input_dir = sys.argv[1] if len(sys.argv) > 1 else "data/input"
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "/tmp/end_date_check_output"
    main.run(input_dir, output_dir)
    con = duckdb.connect(os.path.join(config.WORK_DIR, "work.duckdb"))
    for statement in SETUP.split(";"):
        if statement.strip():
            con.execute(statement)

    print("\n=== END DATE CHECK ===")
    for kind, n in con.execute("SELECT kind, count(*) FROM conflict GROUP BY 1 ORDER BY 1").fetchall():
        print(f"conflicting duplicate groups, {kind}: {n:,}")
    n_before, n_after, n_any = con.execute(
        "SELECT count(*) FILTER (WHERE has_before), count(*) FILTER (WHERE has_after), "
        "count(*) FILTER (WHERE has_before OR has_after) FROM neighbour").fetchone()
    print(f"groups where the same job appears once in an earlier snapshot: {n_before:,}; "
          f"in a later snapshot: {n_after:,}; either: {n_any:,}")

    for kind in ("two different end dates", "end date vs present"):
        nb, na = con.execute(f"SELECT count(*) FILTER (WHERE has_before), count(*) FILTER (WHERE has_after) "
                             f"FROM neighbour WHERE kind = '{kind}'").fetchone()
        print(f"\n[{kind}] how often each rule keeps the end date the job shows in its neighbouring single-copy snapshot")
        print(f"  {'rule':50}{'matches earlier':>20}{'matches later':>20}")
        for name, rule_sql in RULES.items():
            before, after = con.execute(f"""
                SELECT count(*) FILTER (WHERE n.has_before AND k.kept_end IS NOT DISTINCT FROM n.before_end),
                       count(*) FILTER (WHERE n.has_after AND k.kept_end IS NOT DISTINCT FROM n.after_end)
                FROM neighbour n JOIN ({kept(rule_sql)}) k USING (g) WHERE n.kind = '{kind}'
            """).fetchone()
            print(f"  {name:50}{f'{before:,} / {nb:,}':>20}{f'{after:,} / {na:,}':>20}")
        if kind == "end date vs present":
            print("  neighbour says: " + ", ".join(f"{k} = {v:,}" for k, v in con.execute("""
                SELECT side || ' ' || CASE WHEN e IS NULL THEN 'present' ELSE 'ended' END, count(*) FROM (
                    SELECT 'earlier' AS side, before_end AS e FROM neighbour WHERE kind = 'end date vs present' AND has_before
                    UNION ALL
                    SELECT 'later', after_end FROM neighbour WHERE kind = 'end date vs present' AND has_after)
                GROUP BY 1 ORDER BY 1""").fetchall()))

    print("\nexamples (user | snapshot | company | title | start | end dates of the copies | "
          "earlier single copy's end | later single copy's end):")
    for kind in ("two different end dates", "end date vs present"):
        print(f"\n# {kind}")
        rows = con.execute(f"""
            SELECT c.user_id, c.observed_ts::DATE, any_value(r.company_raw), any_value(r.title_raw),
                   any_value(r.startdate), list(r.enddate ORDER BY r.ord),
                   CASE WHEN n.has_before THEN coalesce(n.before_end::VARCHAR, 'present') END,
                   CASE WHEN n.has_after THEN coalesce(n.after_end::VARCHAR, 'present') END
            FROM conflict c JOIN r USING (g) JOIN neighbour n USING (g)
            WHERE c.kind = '{kind}'
            GROUP BY c.g, c.user_id, c.observed_ts, n.has_before, n.before_end, n.has_after, n.after_end
            ORDER BY hash(c.g) LIMIT 20
        """).fetchall()
        for row in rows:
            print("  " + " | ".join("—" if x is None else str([str(d) if d else 'present' for d in x]) if isinstance(x, list) else str(x) for x in row))
    con.close()
    shutil.rmtree(config.WORK_DIR, ignore_errors=True)
    shutil.rmtree(output_dir, ignore_errors=True)


if __name__ == "__main__":
    main_cli()
