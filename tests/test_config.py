import os
import tempfile
import unittest
from unittest import mock

from src import config


class Validate(unittest.TestCase):
    def test_defaults_are_valid(self):
        self.assertEqual(config.validate(), [])

    def test_bad_threads(self):
        for bad in (0, -2, "abc"):
            with self.subTest(bad=bad), mock.patch.object(config, "THREADS", bad):
                self.assertTrue(any("THREADS" in p for p in config.validate()))

    def test_bad_memory_limit(self):
        with mock.patch.object(config, "MEMORY_LIMIT", "lots"):
            self.assertTrue(any("MEMORY_LIMIT" in p for p in config.validate()))

    def test_date_must_be_exactly_yyyy_mm_dd(self):
        for bad in ("19400101", "1940-1-1", "1940-02-30", "01/01/1940", "1940-01-01T00:00", 1940):
            with self.subTest(bad=bad), mock.patch.object(config, "EARLIEST_PLAUSIBLE_DATE", bad):
                self.assertTrue(any("EARLIEST_PLAUSIBLE_DATE" in p for p in config.validate()))

    def test_policy_switches_must_be_true_or_false(self):
        with mock.patch.object(config, "ALLOW_COMPANY_RENAME", "yes"):
            self.assertTrue(any("ALLOW_COMPANY_RENAME" in p for p in config.validate()))


class EnvParsing(unittest.TestCase):
    def test_unparseable_numbers_are_kept_for_validate(self):
        with mock.patch.dict(os.environ, {"N": "abc"}):
            self.assertEqual(config._env_number("N", 5, int), "abc")


class AvailableCpus(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def write(self, path, text):
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)

    def test_cgroup_v2_quota(self):
        self.write("cpu.max", "200000 100000")
        self.assertEqual(config.available_cpus(self.root), min(2, len(os.sched_getaffinity(0))))

    def test_cgroup_v1_quota(self):
        self.write("cpu/cpu.cfs_quota_us", "100000")
        self.write("cpu/cpu.cfs_period_us", "100000")
        self.assertEqual(config.available_cpus(self.root), 1)

    def test_no_limit(self):
        self.write("cpu.max", "max 100000")
        self.write("cpu/cpu.cfs_quota_us", "-1")
        self.write("cpu/cpu.cfs_period_us", "100000")
        self.assertEqual(config.available_cpus(self.root), len(os.sched_getaffinity(0)))


if __name__ == "__main__":
    unittest.main()


class MatchingWorkers(unittest.TestCase):

    def test_four_cpus_and_sixteen_gb_keep_four_workers(self):
        self.assertEqual(config.matching_workers(4, 16.0), 4)

    def test_many_cpus_and_little_memory_are_limited_by_memory(self):
        self.assertEqual(config.matching_workers(16, 8.0), 4)
        self.assertEqual(config.matching_workers(16, 16.0), 8)

    def test_at_least_one_worker(self):
        self.assertEqual(config.matching_workers(8, 1.0), 1)
