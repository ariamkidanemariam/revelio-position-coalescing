import os
import shutil
import tempfile
import unittest

import duckdb

from src import config, main
from src.match_stage import _grouped, _merge, _paired
from tests.test_pipeline import CASE_BASE, COLUMNS


def groups(rows):
    return _grouped(iter(rows))


class PairedStreamsTest(unittest.TestCase):
    def test_matching_streams_are_paired_user_by_user(self):
        versions = groups([(1, "a"), (1, "b"), (2, "c")])
        snapshots = groups([(1, 0), (2, 0), (2, 1)])
        self.assertEqual([(u, len(v), len(s)) for u, v, s in _paired(versions, snapshots)],
                         [(1, 2, 1), (2, 1, 2)])

    def test_versions_stream_ending_early_is_an_error(self):
        versions = groups([(1, "a")])
        snapshots = groups([(1, 0), (2, 0)])
        with self.assertRaisesRegex(RuntimeError, "user 2 has snapshots but no versions"):
            list(_paired(versions, snapshots))

    def test_snapshots_stream_ending_early_is_an_error(self):
        versions = groups([(1, "a"), (2, "b")])
        snapshots = groups([(1, 0)])
        with self.assertRaisesRegex(RuntimeError, "user 2 has versions but no snapshots"):
            list(_paired(versions, snapshots))

    def test_different_users_at_the_same_point_is_an_error(self):
        versions = groups([(1, "a"), (3, "b")])
        snapshots = groups([(1, 0), (2, 0)])
        with self.assertRaisesRegex(RuntimeError, "versions for user 3, snapshots for user 2"):
            list(_paired(versions, snapshots))


class MergeTest(unittest.TestCase):
    def test_shard_summaries_are_combined(self):
        a = {"users": 2, "versions": 10, "large_users": 1,
             "largest": [(3, 2, 0.1, 11), (7, 4, 0.2, 12)], "slowest": [(0.1, 3, 2, 11), (0.2, 7, 4, 12)]}
        b = {"users": 1, "versions": 9, "large_users": 1,
             "largest": [(9, 5, 0.5, 21)], "slowest": [(0.5, 9, 5, 21)]}
        total = _merge([a, b])
        self.assertEqual((total["users"], total["versions"], total["large_users"]), (3, 19, 2))
        self.assertEqual([u["user_id"] for u in total["largest"]], [21, 12, 11])
        self.assertEqual(total["slowest"][0], {"user_id": 21, "versions": 9, "snapshots": 5, "seconds": 0.5})


class LargeUserCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.input = os.path.join(self.tmp, "input")
        os.makedirs(self.input)
        self.saved = (config.WORK_DIR, config.LARGE_USER_VERSIONS)
        config.WORK_DIR = os.path.join(self.tmp, "work")
        con = duckdb.connect(os.path.join(self.tmp, "make_input.duckdb"))
        con.execute(f"""
            COPY (
                SELECT observed_ts::DATE AS observed_ts, user_id::BIGINT AS user_id,
                       company_raw, company_linkedin_url, title_raw, location_raw, description,
                       startdate::DATE AS startdate, enddate::DATE AS enddate,
                       order_in_profile::INTEGER AS order_in_profile
                FROM (VALUES {CASE_BASE}) t({COLUMNS})
            ) TO '{self.input}/part.parquet' (FORMAT PARQUET)
        """)
        con.close()

    def tearDown(self):
        config.WORK_DIR, config.LARGE_USER_VERSIONS = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read_output(self, output):
        con = duckdb.connect(os.path.join(self.tmp, "read.duckdb"))
        rows = con.execute(f"SELECT * FROM read_parquet('{output}/*.parquet') ORDER BY ALL").fetchall()
        con.close()
        return rows

    def test_large_histories_are_reported_and_output_is_unchanged(self):
        normal = main.run(self.input, os.path.join(self.tmp, "out_normal"))
        config.LARGE_USER_VERSIONS = 2
        flagged = main.run(self.input, os.path.join(self.tmp, "out_flagged"))
        self.assertEqual(normal["large_users"], 0)
        self.assertEqual(flagged["large_users"], 1)
        self.assertEqual(flagged["largest_users"][0]["user_id"], 12345)
        self.assertEqual(flagged["largest_users"][0]["versions"], 4)
        self.assertEqual(self.read_output(os.path.join(self.tmp, "out_normal")),
                         self.read_output(os.path.join(self.tmp, "out_flagged")))


if __name__ == "__main__":
    unittest.main()
