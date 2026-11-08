import collections
import os
import shutil
import sys

os.environ["THREADS"] = "1"
os.environ["KEEP_WORK"] = "1"
os.environ.setdefault("WORK_DIR", "/tmp/acronym_check_work")
sys.path.insert(0, os.getcwd())

import duckdb

from src import config, main, match_stage, matching as M

URL_CASES = ("both URLs missing", "one side has a URL", "same URL", "different URLs")
PER_CASE_EXAMPLES = 100


def url_case(v, p):
    if v.u and p.urls:
        return "same URL" if v.u in p.urls else "different URLs"
    if v.u or p.urls:
        return "one side has a URL"
    return "both URLs missing"


def acronym_related(v, p):
    if not v.c or not p.companies or v.c in p.companies:
        return False
    if any(M.similarity(v.c, c) >= config.COMPANY_SIMILARITY or M.token_subset(v.c, c) for c in p.companies):
        return False
    cv, iv = M.compact(v.c), M.initials(v.c)
    return bool((cv and cv in p.company_initials) or (iv and any(M.compact(c) == iv for c in p.companies)))


class Recorder:
    def __init__(self):
        self.comparisons = 0
        self.users_seen = 0
        self.current_user = None
        self.events = collections.Counter()
        self.pairs = collections.defaultdict(set)
        self.final_users = collections.defaultdict(set)
        self.examples = collections.defaultdict(list)
        self.accepted = {}
        self.production_acronyms = 0
        self.production_acronyms_missed = 0
        self.detector_only = collections.Counter()

    def install(self):
        rec = self
        orig_compare, orig_assign, orig_paired = M.compare, M.assign_user, match_stage._paired

        def paired(versions_iter, snapshots_iter):
            for user_id, vrows, srows in orig_paired(versions_iter, snapshots_iter):
                rec.current_user = user_id
                rec.users_seen += 1
                yield user_id, vrows, srows

        def compare(v, p, snap):
            rec.comparisons += 1
            result = orig_compare(v, p, snap)
            company = M.compare_company(v, p)
            related = acronym_related(v, p)
            if company == M.ACRONYM:
                rec.production_acronyms += 1
                rec.production_acronyms_missed += not related
            if related:
                case = url_case(v, p)
                if v.u and v.u in p.urls:
                    identity = "URL"
                elif company == M.ACRONYM:
                    identity = "acronym rule"
                else:
                    identity = "other"
                conflict = bool(v.u and v.u in p.urls and any(u != v.u for u in p.urls))
                key = (rec.current_user, v.vid, p.n)
                rec.events[("candidate comparisons (events)", case)] += 1
                rec.pairs[("candidate pairs", case)].add(key)
                if company != M.ACRONYM:
                    rec.detector_only[company] += 1
                if result is None:
                    rec.accepted.pop((v.vid, p.n), None)
                else:
                    rec.events[("accepted comparisons (events)", case)] += 1
                    rec.pairs[("pairs accepted at least once", case)].add(key)
                    rec.accepted[(v.vid, p.n)] = (case, identity, rec.current_user, sorted(p.companies),
                                                  sorted(p.urls), sorted(p.titles), v.c, v.u, v.t, v.start,
                                                  conflict, sorted(p.starts))
            return result

        def assign_user(versions, snapshots):
            rec.accepted = {}
            result = orig_assign(versions, snapshots)
            for (vid, n), info in rec.accepted.items():
                if result[vid] == n:
                    case, identity = info[0], info[1]
                    rec.pairs[("final merges", case)].add((info[2], vid, n))
                    rec.pairs[("final merges by " + identity, case)].add((info[2], vid, n))
                    if info[10]:
                        rec.pairs[("final merges with a conflicting old URL", case)].add((info[2], vid, n))
                    rec.final_users[case].add(info[2])
                    if identity == "acronym rule" and len(rec.examples[case]) < PER_CASE_EXAMPLES:
                        rec.examples[case].append(info)
            return result

        match_stage._paired = paired
        M.compare = compare
        M.assign_user = assign_user
        match_stage.assign_user = assign_user


def url_kinds(con):
    return con.execute("""
        WITH v AS (
            SELECT nullif(trim(u_norm), '') AS u,
                   regexp_extract(lower(u_norm), 'linkedin[.]com/([^/?#]+)', 1) AS path
            FROM versions
        )
        SELECT CASE
                 WHEN u IS NULL THEN 'missing'
                 WHEN path = 'company' THEN 'linkedin company page'
                 WHEN path = 'school' THEN 'linkedin school page'
                 WHEN path = 'showcase' THEN 'linkedin showcase page'
                 WHEN path = 'in' THEN 'linkedin personal profile'
                 WHEN lower(u) LIKE '%linkedin.com/%' THEN 'other linkedin path'
                 ELSE 'not linkedin'
               END AS kind, count(*)
        FROM v GROUP BY 1 ORDER BY 2 DESC
    """).fetchall()


def report(rec, stats, raw_total, raw_missing):
    con = duckdb.connect(os.path.join(config.WORK_DIR, "work.duckdb"), read_only=True)
    kinds = url_kinds(con)
    versions = sum(n for _, n in kinds)
    con.close()
    print("\n=== ACRONYM CHECK ===")
    print(f"instrumentation: {rec.users_seen:,} users observed by the patched matcher, "
          f"{stats['users_multi_snapshot']:,} users matched by the pipeline, {rec.comparisons:,} comparisons")
    print(f"production acronym results: {rec.production_acronyms:,}; missed by this script's detector: "
          f"{rec.production_acronyms_missed:,}")
    print("acronym-related pairs this detector finds that production did not call an acronym "
          "(production's company result instead): "
          + (", ".join(f"{k} = {n:,}" for k, n in rec.detector_only.most_common()) or "none"))
    print(f"raw records: {raw_total:,}; without a company URL: {raw_missing:,} ({100 * raw_missing / raw_total if raw_total else 0:.1f}%)")
    print(f"distinct versions seen by the matcher: {versions:,}")
    for kind, n in kinds:
        print(f"  URL after cleaning = {kind}: {n:,} ({100 * n / versions if versions else 0:.1f}%)")
    print("\nacronym-related company names (counted whatever the URL says):")
    print(f"  {'':42}" + "".join(f"{c:>22}" for c in URL_CASES))
    rows = [("candidate comparisons (events)", rec.events), ("candidate pairs", rec.pairs), ("accepted comparisons (events)", rec.events),
            ("pairs accepted at least once", rec.pairs), ("final merges", rec.pairs), ("final merges by URL", rec.pairs),
            ("final merges by acronym rule", rec.pairs), ("final merges by other", rec.pairs),
            ("final merges with a conflicting old URL", rec.pairs)]
    for name, source in rows:
        values = [source[(name, c)] if source is rec.events else len(source[(name, c)]) for c in URL_CASES]
        print(f"  {name:42}" + "".join(f"{v:>22,}" for v in values))
    print(f"  {'distinct users in final merges':42}" + "".join(f"{len(rec.final_users[c]):>22,}" for c in URL_CASES))
    print("\n'final merges' is the authoritative count: pairs actually selected by the assignment.")
    print("'final merges by URL': the URLs are identical, so the URL identified the company, not the acronym.")
    print("'final merges by acronym rule': only the acronym rule identified the company.")
    print("'final merges by other': the URLs differ, so neither URL nor acronym identified the company; "
          "these come from the company-rename rule.")
    print("'with a conflicting old URL': the new URL matches one of the position's URLs but another differs.")
    print("\nALL acronym-rule final merges (identity | user | position companies | position URLs | titles "
          "| position start days <- new company | URL | title | start day):")
    for case in URL_CASES:
        print(f"\n# {case}")
        for e in rec.examples[case]:
            print("  " + " | ".join(str(x) for x in (e[1],) + e[2:6] + (e[11],)) + " <- "
                  + " | ".join(str(x) for x in e[6:10])
                  + (" | CONFLICTING OLD URL" if e[10] else ""))


def main_cli():
    input_dir = sys.argv[1] if len(sys.argv) > 1 else "data/input"
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "/tmp/acronym_check_output"
    if config.THREADS != 1:
        raise SystemExit("THREADS must be 1: match_stage.run then calls run_shard directly, without worker processes")
    glob_path = os.path.join(input_dir, "**", "*.parquet")
    con = duckdb.connect()
    url_col = main.url_column(main.column_types(con, glob_path))
    raw_total, raw_missing = con.execute(
        f"SELECT count(*), count(*) FILTER (WHERE nullif(trim(({url_col})::VARCHAR), '') IS NULL) "
        f"FROM read_parquet('{glob_path}', union_by_name = true)").fetchone()
    con.close()
    rec = Recorder()
    rec.install()
    stats = main.run(input_dir, output_dir)
    if rec.production_acronyms_missed:
        raise SystemExit(f"the detector missed {rec.production_acronyms_missed:,} production acronym results")
    if rec.users_seen != stats["users_multi_snapshot"] or rec.comparisons == 0:
        raise SystemExit(f"instrumentation saw {rec.users_seen:,} of {stats['users_multi_snapshot']:,} users: "
                         f"the report would be incomplete")
    report(rec, stats, raw_total, raw_missing)
    shutil.rmtree(config.WORK_DIR, ignore_errors=True)
    shutil.rmtree(output_dir, ignore_errors=True)


if __name__ == "__main__":
    main_cli()
