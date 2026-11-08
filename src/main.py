import argparse
import glob
import os
import resource
import shutil
import time

import duckdb

from src import build_output, config, match_stage, prepare


def log(message, started):
    print(f"[{time.perf_counter() - started:7.1f}s] {message}", flush=True)


def connect(db_path):
    con = duckdb.connect(db_path)
    con.execute(f"SET threads = {config.THREADS}")
    con.execute(f"SET memory_limit = '{config.MEMORY_LIMIT}'")
    con.execute(f"SET temp_directory = '{os.path.join(config.WORK_DIR, 'spill')}'")
    con.execute("SET preserve_insertion_order = false")
    return con


def column_types(con, input_glob):
    rows = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{input_glob}', union_by_name = true)"
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def url_column(types):
    names = set(types)
    present = [c for c in ("company_linkedin_url", "company_website") if c in names]
    if not present:
        return "NULL::VARCHAR"
    if len(present) == 2:
        return "coalesce(company_linkedin_url, company_website)"
    return present[0]


def peak_memory_mb():
    own = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    workers = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return (own + workers) / 1024


def run(input_dir, output_dir):
    problems = config.validate()
    if problems:
        raise SystemExit("Invalid configuration:\n  - " + "\n  - ".join(problems))
    if not glob.glob(os.path.join(input_dir, "**", "*.parquet"), recursive=True):
        raise SystemExit(f"No .parquet files found under {input_dir}")
    started = time.perf_counter()
    input_glob = os.path.join(input_dir, "**", "*.parquet")

    shutil.rmtree(config.WORK_DIR, ignore_errors=True)
    os.makedirs(os.path.join(config.WORK_DIR, "spill"), exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    for old in glob.glob(os.path.join(output_dir, "data_*.parquet")):
        os.remove(old)
    db_path = os.path.join(config.WORK_DIR, "work.duckdb")

    con = connect(db_path)
    types = column_types(con, input_glob)
    statements = prepare.prepare_statements(
        input_glob, url_column(types), types.get("startdate"), types.get("enddate"))
    for statement in statements:
        con.execute(statement)
    names = [
        "rows_read", "snapshots_in", "snapshots_kept", "bad_end_before_start",
        "bad_too_old", "bad_future", "bad_no_content", "records_after_dedup",
        "distinct_versions", "users", "users_multi_snapshot",
    ]
    stats = dict(zip(names, con.execute(prepare.stats_query()).fetchone()))
    con.close()
    log(f"resources: threads={config.THREADS}, matching_workers={config.MATCH_WORKERS}, "
        f"memory_limit={config.MEMORY_LIMIT}, worker_memory_limit={config.WORKER_MEMORY_LIMIT}", started)
    log("prepared: " + ", ".join(f"{k}={v:,}" for k, v in stats.items()), started)

    match_started = time.perf_counter()
    csvs, matched = match_stage.run(db_path, config.WORK_DIR, config.MATCH_WORKERS)
    match_seconds = time.perf_counter() - match_started
    log(f"matched {matched['versions']:,} versions for {matched['users']:,} multi-snapshot users "
        f"[matching took {match_seconds:.1f}s]", started)
    largest = matched["largest"][0] if matched["largest"] else None
    slowest = matched["slowest"][0] if matched["slowest"] else None
    if largest:
        log(f"largest user history: user {largest['user_id']} with {largest['versions']:,} versions "
            f"in {largest['snapshots']:,} snapshots ({largest['seconds']:.3f}s); slowest user: "
            f"user {slowest['user_id']} ({slowest['seconds']:.3f}s, {slowest['versions']:,} versions)", started)
    if matched["large_users"]:
        log(f"WARNING: {matched['large_users']:,} users have more than {config.LARGE_USER_VERSIONS:,} "
            f"versions; their histories are held in memory one at a time and may slow matching", started)

    con = connect(db_path)
    for statement in build_output.output_statements(csvs, output_dir):
        con.execute(statement)
    positions = con.execute("SELECT count(*) FROM pos_first").fetchone()[0]
    log(f"wrote output for {positions:,} positions to {output_dir} "
        f"[peak memory ~{peak_memory_mb():,.0f} MB, approximate: main process plus the largest worker]", started)
    checks, failed = {}, 0
    for name, sql, expected in build_output.check_queries(output_dir):
        value = con.execute(sql).fetchone()[0] or 0
        checks[name] = value
        status = "" if expected is None else (" OK" if value == expected else " FAILED")
        failed += status == " FAILED"
        log(f"check: {name} = {value:,}{status}", started)
    con.close()
    total_seconds = time.perf_counter() - started
    log("all output checks passed" if not failed else f"WARNING: {failed} output check(s) failed", started)

    if os.environ.get("KEEP_WORK") != "1":
        shutil.rmtree(config.WORK_DIR, ignore_errors=True)

    return {
        **stats,
        "positions": positions,
        "checks": checks,
        "checks_failed": failed,
        "large_users": matched["large_users"],
        "largest_users": matched["largest"],
        "slowest_users": matched["slowest"],
        "match_seconds": round(match_seconds, 1),
        "total_seconds": round(total_seconds, 1),
        "peak_memory_mb": round(peak_memory_mb()),
    }


def main():
    parser = argparse.ArgumentParser(description="Revelio Labs position coalescing")
    parser.add_argument("--input", default="/input")
    parser.add_argument("--output", default="/output")
    args = parser.parse_args()
    stats = run(args.input, args.output)
    if stats["checks_failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
