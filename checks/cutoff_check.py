import os
import sys

sys.path.insert(0, os.getcwd())

import duckdb

from src import main, prepare

CUTOFFS = (1900, 1910, 1920, 1930, 1940, 1950, 1960, 1970)


def main_cli():
    input_dir = sys.argv[1] if len(sys.argv) > 1 else "data/input"
    glob_path = os.path.join(input_dir, "**", "*.parquet")
    con = duckdb.connect()
    types = main.column_types(con, glob_path)
    start = prepare.date_expression("startdate", types.get("startdate"))
    end = prepare.date_expression("enddate", types.get("enddate"))
    con.execute(f"""
        CREATE TABLE r AS
        SELECT user_id, observed_ts, least({start}, {end}) AS d
        FROM read_parquet('{glob_path}', union_by_name = true)
        WHERE user_id IS NOT NULL AND observed_ts IS NOT NULL
    """)
    con.execute("""
        CREATE TABLE s AS
        SELECT user_id, observed_ts, min(d) AS earliest FROM r GROUP BY 1, 2
    """)
    con.execute("""
        CREATE TABLE u AS
        SELECT user_id, max(earliest) AS best_snapshot_earliest, count(*) AS n FROM s GROUP BY 1
    """)
    rows, snaps, users = con.execute("SELECT (SELECT count(*) FROM r), (SELECT count(*) FROM s), "
                                     "(SELECT count(*) FROM u)").fetchone()

    print("\n=== CUT-OFF CHECK ===")
    print(f"rows: {rows:,}; snapshots: {snaps:,}; users: {users:,}")
    print("(this counts only the cut-off rule; the other checks are ignored)")
    print("\nwhat each cut-off would reject")
    print(f"  {'cut-off':>8}{'rows before it':>18}{'snapshots rejected':>22}{'users affected':>18}"
          f"{'users losing every snapshot':>30}")
    for year in CUTOFFS:
        r, s, ua, ul = con.execute(f"""
            SELECT (SELECT count(*) FROM r WHERE d < DATE '{year}-01-01'),
                   (SELECT count(*) FROM s WHERE earliest < DATE '{year}-01-01'),
                   (SELECT count(DISTINCT user_id) FROM s WHERE earliest < DATE '{year}-01-01'),
                   (SELECT count(*) FROM u WHERE best_snapshot_earliest < DATE '{year}-01-01')
        """).fetchone()
        print(f"  {year:>8}{r:>18,}{f'{s:,} ({100 * s / snaps:.3f}%)':>22}{ua:>18,}{ul:>30,}")

    print("\nrows by year of the earliest date, 1900-1969 (repeated rows included)")
    hist = dict(con.execute("""
        SELECT year(d), count(*) FROM r WHERE d >= DATE '1900-01-01' AND d < DATE '1970-01-01' GROUP BY 1
    """).fetchall())
    top = max(hist.values()) if hist else 1
    for y in range(1900, 1970):
        n = hist.get(y, 0)
        print(f"  {y}  {n:>8,}  {'#' * max(1 if n else 0, round(60 * n / top))}")

    print("\nmost common exact dates before 1950 (distinct users)")
    for d, n_users, n_rows in con.execute("""
        SELECT d, count(DISTINCT user_id), count(*) FROM r WHERE d < DATE '1950-01-01'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 15
    """).fetchall():
        print(f"  {d}: {n_users:,} users, {n_rows:,} rows")
    under, n_before = con.execute("""
        SELECT count(*) FILTER (WHERE month(d) = 1 AND day(d) = 1), count(*) FROM r WHERE d < DATE '1940-01-01'
    """).fetchone()
    print(f"\nrows before 1940 dated exactly 1 January: {under:,} of {n_before:,}")


if __name__ == "__main__":
    main_cli()
