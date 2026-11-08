import csv
import heapq
import os
import time
from itertools import zip_longest
from multiprocessing import get_context

import duckdb

from src import config
from src.matching import Version, assign_user

FETCH_SIZE = 50_000
TOP_USERS = 5
_END = object()


def _stream(cursor):
    while True:
        rows = cursor.fetchmany(FETCH_SIZE)
        if not rows:
            return
        yield from rows


def _grouped(rows):
    current, group = None, []
    for row in rows:
        if row[0] != current:
            if group:
                yield current, group
            current, group = row[0], []
        group.append(row)
    if group:
        yield current, group


def _paired(versions_iter, snapshots_iter):
    for v, s in zip_longest(versions_iter, snapshots_iter, fillvalue=_END):
        if v is _END:
            raise RuntimeError(f"Stream mismatch: user {s[0]} has snapshots but no versions")
        if s is _END:
            raise RuntimeError(f"Stream mismatch: user {v[0]} has versions but no snapshots")
        if v[0] != s[0]:
            raise RuntimeError(f"Stream mismatch: versions for user {v[0]}, snapshots for user {s[0]}")
        yield v[0], v[1], s[1]


def _empty_summary():
    return {"users": 0, "versions": 0, "large_users": 0, "largest": [], "slowest": []}


def run_shard(db_path, shard, n_shards, out_path, threads, large_user_versions):
    con = duckdb.connect(db_path, read_only=True)
    con.execute(f"SET threads = {threads}")
    con.execute(f"SET memory_limit = '{config.WORKER_MEMORY_LIMIT}'")
    spill = os.path.join(os.path.dirname(out_path), f"spill_worker_{shard}")
    os.makedirs(spill, exist_ok=True)
    con.execute(f"SET temp_directory = '{spill}'")

    shard_filter = f"hash(user_id) % {n_shards} = {shard}"
    multi_users = f"user_id IN (SELECT user_id FROM users WHERE n_snaps > 1)"

    versions_sql = f"""
        SELECT user_id, vid, first_snap, first_ord, c_norm, u_norm, t_norm, start_day, end_day
        FROM versions
        WHERE {shard_filter} AND {multi_users}
        ORDER BY user_id, first_snap, first_ord, vid
    """
    snapshots_sql = f"""
        SELECT user_id, snap_idx, list(vid ORDER BY ord, vid) AS vids
        FROM occ
        WHERE {shard_filter} AND {multi_users}
        GROUP BY user_id, snap_idx
        ORDER BY user_id, snap_idx
    """

    versions_con = con.cursor()
    snapshots_con = con.cursor()
    versions_iter = _grouped(_stream(versions_con.execute(versions_sql)))
    snapshots_iter = _grouped(_stream(snapshots_con.execute(snapshots_sql)))

    summary = _empty_summary()
    largest, slowest = [], []
    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["user_id", "vid", "pos_n"])
        for user_id, vrows, srows in _paired(versions_iter, snapshots_iter):
            versions = [Version(*row[1:]) for row in vrows]
            snapshots = [(row[1], row[2]) for row in srows]
            started = time.perf_counter()
            assigned = assign_user(versions, snapshots)
            seconds = time.perf_counter() - started
            for vid, pos_n in assigned.items():
                writer.writerow((user_id, vid, pos_n))

            summary["users"] += 1
            summary["versions"] += len(versions)
            if len(versions) > large_user_versions:
                summary["large_users"] += 1
            heapq.heappush(largest, (len(versions), len(snapshots), round(seconds, 3), user_id))
            heapq.heappush(slowest, (round(seconds, 3), len(versions), len(snapshots), user_id))
            if len(largest) > TOP_USERS:
                heapq.heappop(largest)
            if len(slowest) > TOP_USERS:
                heapq.heappop(slowest)

    con.close()
    summary["largest"] = largest
    summary["slowest"] = slowest
    return summary


def _merge(summaries):
    total = _empty_summary()
    for s in summaries:
        for key in ("users", "versions", "large_users"):
            total[key] += s[key]
    total["largest"] = [
        {"user_id": u, "versions": v, "snapshots": n, "seconds": t}
        for v, n, t, u in heapq.nlargest(TOP_USERS, (x for s in summaries for x in s["largest"]))
    ]
    total["slowest"] = [
        {"user_id": u, "versions": v, "snapshots": n, "seconds": t}
        for t, v, n, u in heapq.nlargest(TOP_USERS, (x for s in summaries for x in s["slowest"]))
    ]
    return total


def run(db_path, work_dir, workers, large_user_versions=None):
    workers = max(1, workers)
    limit = config.LARGE_USER_VERSIONS if large_user_versions is None else large_user_versions
    outputs = [os.path.join(work_dir, f"positions_{i}.csv") for i in range(workers)]
    args = [(db_path, i, workers, outputs[i], 1, limit) for i in range(workers)]

    if workers == 1:
        summaries = [run_shard(*args[0])]
    else:
        with get_context("spawn").Pool(workers) as pool:
            summaries = pool.starmap(run_shard, args)

    return outputs, _merge(summaries)
