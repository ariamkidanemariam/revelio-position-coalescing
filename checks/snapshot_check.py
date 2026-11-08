import os
import shutil
import sys

os.environ["KEEP_WORK"] = "1"
os.environ.setdefault("WORK_DIR", "/tmp/snapshot_check_work")
sys.path.insert(0, os.getcwd())

import duckdb

from src import config, main

SETUP = f"""
CREATE OR REPLACE TABLE rejected AS
SELECT user_id, observed_ts,
       coalesce(end_before_start, false) OR coalesce(too_far_future, false) OR no_content AS other_reason
FROM snap_check
WHERE coalesce(too_old, false);

CREATE OR REPLACE TABLE rej_rec AS
SELECT DISTINCT ON (o.user_id, o.observed_ts, c.dup_key)
       o.user_id, o.observed_ts, c.dup_key, coalesce(c.c_norm, c.u_norm) AS comp, c.t_norm,
       c.startdate, c.enddate, c.company_raw, c.title_raw,
       coalesce(least(c.startdate, c.enddate) < DATE '{config.EARLIEST_PLAUSIBLE_DATE}', false) AS is_old
FROM obs o JOIN cleaned c USING (rh) JOIN rejected USING (user_id, observed_ts)
WHERE c.usable;

CREATE OR REPLACE TABLE kept_rec AS
SELECT DISTINCT o.user_id, c.dup_key, coalesce(c.c_norm, c.u_norm) AS comp, c.t_norm, c.startdate
FROM obs o JOIN cleaned c USING (rh) JOIN snaps USING (user_id, observed_ts)
WHERE c.usable;

CREATE OR REPLACE TABLE rej_match AS
SELECT r.*,
       CASE
         WHEN EXISTS (SELECT 1 FROM kept_rec k WHERE k.user_id = r.user_id AND k.dup_key = r.dup_key)
           THEN '1 exact: same company, title and start'
         WHEN EXISTS (SELECT 1 FROM kept_rec k WHERE k.user_id = r.user_id AND k.comp = r.comp
                      AND k.startdate = r.startdate)
           THEN '2 same company and start, title differs'
         WHEN EXISTS (SELECT 1 FROM kept_rec k WHERE k.user_id = r.user_id AND k.comp = r.comp
                      AND k.t_norm = r.t_norm)
           THEN '3 same company and title, start differs'
         WHEN NOT EXISTS (SELECT 1 FROM snaps s WHERE s.user_id = r.user_id)
           THEN '5 user has no kept snapshot at all'
         ELSE '4 not found in any kept snapshot'
       END AS found
FROM rej_rec r;
"""


def main_cli():
    input_dir = sys.argv[1] if len(sys.argv) > 1 else "data/input"
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "/tmp/snapshot_check_output"
    main.run(input_dir, output_dir)
    con = duckdb.connect(os.path.join(config.WORK_DIR, "work.duckdb"))
    for statement in SETUP.split(";"):
        if statement.strip():
            con.execute(statement)

    print(f"\n=== SNAPSHOT CHECK: the {config.EARLIEST_PLAUSIBLE_DATE} rule ===")
    total, only_old = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE NOT other_reason) FROM rejected").fetchone()
    print(f"snapshots flagged by the rule: {total:,}; flagged by this rule only: {only_old:,}")

    print("\nQ1: snapshots flagged by this rule only, by number of records before the cut-off and number of other records")
    rows = con.execute("""
        SELECT n_old, CASE WHEN n_other = 0 THEN '0' WHEN n_other <= 3 THEN '1-3' WHEN n_other <= 9 THEN '4-9'
                           ELSE '10+' END AS others, count(*)
        FROM (SELECT r.user_id, r.observed_ts, count(*) FILTER (WHERE is_old) AS n_old,
                     count(*) FILTER (WHERE NOT is_old) AS n_other
              FROM rej_rec r JOIN rejected j USING (user_id, observed_ts)
              WHERE NOT j.other_reason GROUP BY 1, 2)
        GROUP BY 1, 2 ORDER BY 1, min(n_other)
    """).fetchall()
    for n_old, others, n in rows:
        print(f"  records before cut-off = {n_old:<3} other records = {others:<4} snapshots = {n:,}")
    exactly_one = con.execute("""
        SELECT count(*) FROM (SELECT r.user_id, r.observed_ts FROM rej_rec r JOIN rejected j USING (user_id, observed_ts)
                              WHERE NOT j.other_reason GROUP BY 1, 2
                              HAVING count(*) FILTER (WHERE is_old) = 1 AND count(*) FILTER (WHERE NOT is_old) >= 1)
    """).fetchone()[0]
    print(f"  => exactly one record before the cut-off, at least one other record: {exactly_one:,}")

    print("\nQ2/Q3: records in the flagged snapshots, by whether the same job appears in a kept snapshot of the same user")
    print(f"  {'':45}{'records before cut-off':>24}{'other records':>16}")
    for found, old, other in con.execute("""
        SELECT found, count(*) FILTER (WHERE is_old), count(*) FILTER (WHERE NOT is_old)
        FROM (SELECT DISTINCT user_id, dup_key, found, is_old FROM rej_match) GROUP BY 1 ORDER BY 1
    """).fetchall():
        print(f"  {found[2:]:45}{old:>24,}{other:>16,}")
    print("  (each distinct job of a user is counted once, however many flagged snapshots it appears in)")

    print("\nmost common dates before the cut-off:")
    for d, n in con.execute("""
        SELECT least(startdate, enddate), count(*) FROM rej_rec WHERE is_old GROUP BY 1 ORDER BY 2 DESC LIMIT 10
    """).fetchall():
        print(f"  {d}: {n:,}")

    print("\nexamples of other records not found in any kept snapshot "
          "(user | snapshot | company | title | start | end):")
    for row in con.execute("""
        SELECT user_id, observed_ts::DATE, company_raw, title_raw, startdate, enddate FROM rej_match
        WHERE NOT is_old AND found LIKE '4%' ORDER BY hash(user_id, dup_key) LIMIT 20
    """).fetchall():
        print("  " + " | ".join("—" if x is None else str(x) for x in row))
    con.close()
    shutil.rmtree(config.WORK_DIR, ignore_errors=True)
    shutil.rmtree(output_dir, ignore_errors=True)


if __name__ == "__main__":
    main_cli()
