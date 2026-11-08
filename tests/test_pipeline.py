import os
import shutil
import tempfile
import unittest

import duckdb

from src import config, main

COLUMNS = (
    "observed_ts, user_id, company_raw, company_linkedin_url, title_raw, "
    "location_raw, description, startdate, enddate, order_in_profile"
)

CASE_BASE = """
    ('2026-09-01', 12345, 'Revelio Labs', 'linkedin.com/company/revelio-labs', 'Machine Learning Engineer', 'New York, NY', 'Making workforce data more accurate', '2026-08-01', NULL, 0),
    ('2026-09-01', 12345, 'NYU', 'linkedin.com/company/nyu', 'Research Assistant', 'New York, NY', 'Research on various NLP models', '2025-09-01', '2026-08-01', 1),
    ('2026-07-19', 12345, 'NYU', 'linkedin.com/company/nyu', 'Research Assistant', 'New York, NY', 'Wrapping up NLP research', '2025-09-01', NULL, 0),
    ('2026-07-19', 12345, 'NYU', 'linkedin.com/company/nyu', 'TA', 'NYC', 'TA for data science 101', '2024-09-01', '2025-06-01', 1)
"""


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.input = os.path.join(self.tmp, "input")
        self.output = os.path.join(self.tmp, "output")
        os.makedirs(self.input)
        config.WORK_DIR = os.path.join(self.tmp, "work")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_pipeline(self, rows_sql):
        con = duckdb.connect(os.path.join(self.tmp, "make_input.duckdb"))
        con.execute(f"""
            COPY (
                SELECT observed_ts::DATE AS observed_ts, user_id::BIGINT AS user_id,
                       company_raw, company_linkedin_url, title_raw, location_raw, description,
                       startdate::DATE AS startdate, enddate::DATE AS enddate,
                       order_in_profile::INTEGER AS order_in_profile
                FROM (VALUES {rows_sql}) t({COLUMNS})
            ) TO '{self.input}/part.parquet' (FORMAT PARQUET)
        """)
        con.close()
        self.stats = main.run(self.input, self.output)
        con = duckdb.connect(os.path.join(self.tmp, "read_output.duckdb"))
        rows = con.execute(f"""
            SELECT asof_ts::VARCHAR, position_id, company_raw, company_linkedin_url, title_raw,
                   location_raw, description, startdate::VARCHAR, enddate::VARCHAR
            FROM read_parquet('{self.output}/*.parquet')
            ORDER BY asof_ts DESC, position_id
        """).fetchall()
        columns = [r[0] for r in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{self.output}/*.parquet')").fetchall()]
        con.close()
        return rows, columns

    def test_output_columns_match_the_brief(self):
        _, columns = self.run_pipeline(CASE_BASE)
        self.assertEqual(columns, [
            "asof_ts", "user_id", "company_raw", "company_linkedin_url", "title_raw",
            "location_raw", "description", "startdate", "enddate", "position_id",
        ])

    def test_brief_example_case_a(self):
        rows, _ = self.run_pipeline(CASE_BASE + """,
            ('2026-09-16', 12345, 'Revelio Labs', 'linkedin.com/company/revelio-labs', 'Machine Learning Engineer', 'New York, NY', 'Making workforce data more accurate', '2026-08-01', NULL, 0)
        """)
        ra, ta, ml = "12345-1", "12345-2", "12345-3"
        self.assertEqual([(r[0], r[1], r[4], r[6], r[8]) for r in rows], [
            ("2026-09-16", ra, "Research Assistant", "Research on various NLP models", "2026-08-01"),
            ("2026-09-16", ta, "TA", "TA for data science 101", "2025-06-01"),
            ("2026-09-16", ml, "Machine Learning Engineer", "Making workforce data more accurate", None),
            ("2026-09-01", ra, "Research Assistant", "Research on various NLP models", "2026-08-01"),
            ("2026-09-01", ta, "TA", "TA for data science 101", "2025-06-01"),
            ("2026-09-01", ml, "Machine Learning Engineer", "Making workforce data more accurate", None),
            ("2026-07-19", ra, "Research Assistant", "Wrapping up NLP research", None),
            ("2026-07-19", ta, "TA", "TA for data science 101", "2025-06-01"),
        ])

    def test_brief_example_case_b(self):
        rows, _ = self.run_pipeline(CASE_BASE + """,
            ('2026-09-16', 12345, 'RL', 'linkedin.com/company/revelio-labs', 'MLE', 'New York, NY', NULL, '2026-08-01', NULL, 0)
        """)
        latest = [r for r in rows if r[0] == "2026-09-16"]
        self.assertEqual(len(latest), 3)
        revelio = [r for r in latest if r[1] == "12345-3"][0]
        self.assertEqual(revelio[2], "RL")
        self.assertEqual(revelio[4], "MLE")
        self.assertEqual(revelio[6], "Making workforce data more accurate")
        self.assertEqual(len(rows), 8)
        self.assertEqual(self.stats["checks_failed"], 0)

    def test_bad_snapshots_are_discarded(self):
        rows, _ = self.run_pipeline("""
            ('2020-01-01', 1, 'Acme', NULL, 'Analyst', NULL, NULL, '2019-01-01', NULL, 0),
            ('2020-02-01', 1, 'Acme', NULL, 'Analyst', NULL, NULL, '2019-01-01', '2018-01-01', 0),
            ('2020-03-01', 1, 'Acme', NULL, 'Analyst', NULL, NULL, '2035-01-01', NULL, 0),
            ('2020-04-01', 1, 'Acme', NULL, 'Analyst', NULL, NULL, '1901-01-01', NULL, 0),
            ('2020-05-01', 1, '-', NULL, 'N/A', '???', NULL, NULL, NULL, 0),
            ('2020-06-01', 1, 'Acme', NULL, 'Analyst', NULL, NULL, '2019-01-01', NULL, 0)
        """)
        self.assertEqual(sorted({r[0] for r in rows}), ["2020-01-01", "2020-06-01"])
        self.assertEqual({r[1] for r in rows}, {"1-1"})

    def test_duplicates_in_a_snapshot_collapse_and_placeholders_become_null(self):
        rows, _ = self.run_pipeline("""
            ('2021-01-01', 2, 'Acme', 'https://www.linkedin.com/company/acme/', 'Analyst', 'none', 'Did things Show less', '2019-01-01', NULL, 0),
            ('2021-01-01', 2, 'Acme', 'https://www.linkedin.com/company/acme/', 'Analyst', 'none', 'Did things Show less', '2019-01-01', NULL, 1),
            ('2021-01-01', 2, 'ACME', NULL, 'analyst', NULL, NULL, '2019-01-01', NULL, 2)
        """)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row[3], "linkedin.com/company/acme")
        self.assertIsNone(row[5])
        self.assertEqual(row[6], "Did things")

    def test_conflicting_copies_keep_the_earliest_end_date(self):
        rows, _ = self.run_pipeline("""
            ('2021-01-01', 3, 'Acme', NULL, 'Analyst', 'Paris', 'Did things', '2008-01-01', '2010-12-01', 0),
            ('2021-01-01', 3, 'Acme', NULL, 'Analyst', NULL, NULL, '2008-01-01', '2010-01-01', 1)
        """)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][8], "2010-01-01")

    def test_an_ended_copy_beats_a_present_copy(self):
        rows, _ = self.run_pipeline("""
            ('2021-01-01', 4, 'Acme', NULL, 'Analyst', 'Paris', 'Did things', '2008-01-01', NULL, 0),
            ('2021-01-01', 4, 'Acme', NULL, 'Analyst', NULL, NULL, '2008-01-01', '2012-10-01', 1)
        """)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][8], "2012-10-01")

    def test_a_start_date_seen_once_does_not_replace_one_seen_more_often(self):
        rows, _ = self.run_pipeline("""
            ('2022-03-01', 5, 'KPMG', NULL, 'Senior Accountant', NULL, NULL, '2022-01-01', NULL, 0),
            ('2022-04-01', 5, 'KPMG', NULL, 'Senior Accountant', NULL, NULL, '2022-01-01', NULL, 0),
            ('2022-11-01', 5, 'KPMG', NULL, 'Senior Accountant', NULL, NULL, '2022-10-01', NULL, 0),
            ('2022-12-01', 5, 'KPMG', NULL, 'Senior Accountant', NULL, NULL, '2022-01-01', NULL, 0)
        """)
        starts = {r[0]: r[7] for r in rows}
        self.assertEqual(len({r[1] for r in rows}), 1)
        self.assertEqual(starts, {"2022-03-01": "2022-01-01", "2022-04-01": "2022-01-01",
                                  "2022-11-01": "2022-01-01", "2022-12-01": "2022-01-01"})

    def test_a_correction_seen_as_often_as_the_old_start_date_wins(self):
        rows, _ = self.run_pipeline("""
            ('2022-03-01', 5, 'KPMG', NULL, 'Senior Accountant', NULL, NULL, '2022-01-01', NULL, 0),
            ('2022-04-01', 5, 'KPMG', NULL, 'Senior Accountant', NULL, NULL, '2022-02-01', NULL, 0),
            ('2022-05-01', 5, 'KPMG', NULL, 'Senior Accountant', NULL, NULL, '2022-02-01', NULL, 0)
        """)
        starts = {r[0]: r[7] for r in rows}
        self.assertEqual(starts, {"2022-03-01": "2022-01-01", "2022-04-01": "2022-02-01",
                                  "2022-05-01": "2022-02-01"})

    def test_empty_and_whitespace_text_is_missing(self):
        rows, _ = self.run_pipeline("""
            ('2021-01-01', 4, 'Acme', '', 'Analyst', '   ', '', '2019-01-01', NULL, 0),
            ('2021-01-01', 4, '  ', NULL, ' ', 'Paris', 'x', '2015-01-01', '2016-01-01', 1)
        """)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0][3], rows[0][5], rows[0][6]), (None, None, None))

    def test_dates_given_as_text_with_blanks(self):
        con = duckdb.connect(os.path.join(self.tmp, "text_dates.duckdb"))
        con.execute(f"""
            COPY (SELECT * FROM (VALUES
                ('2021-01-01'::DATE, 5::BIGINT, 'Acme', NULL::VARCHAR, 'Analyst', NULL::VARCHAR, NULL::VARCHAR, '2019-01-01', '   ', 0),
                ('2021-06-01'::DATE, 5::BIGINT, 'Acme', NULL::VARCHAR, 'Analyst', NULL::VARCHAR, NULL::VARCHAR, '2019-01-01', '', 0)
            ) t(observed_ts, user_id, company_raw, company_linkedin_url, title_raw, location_raw,
                description, startdate, enddate, order_in_profile))
            TO '{self.input}/part.parquet' (FORMAT PARQUET)
        """)
        con.close()
        main.run(self.input, self.output)
        con = duckdb.connect(os.path.join(self.tmp, "read_text_dates.duckdb"))
        rows = con.execute(f"SELECT position_id, startdate::VARCHAR, enddate FROM read_parquet('{self.output}/*.parquet')").fetchall()
        con.close()
        self.assertEqual({r[0] for r in rows}, {"5-1"})
        self.assertTrue(all(r[1] == "2019-01-01" and r[2] is None for r in rows))

    def test_non_latin_company_endings_and_placeholders(self):
        rows, _ = self.run_pipeline("""
            ('2021-01-01', 6, '湖南长沙石墨烯研究院', NULL, '研究员', NULL, NULL, '2019-01-01', NULL, 0),
            ('2021-06-01', 6, '湖南长沙石墨烯研究院有限公司', NULL, '研究员', NULL, NULL, '2019-03-01', NULL, 0),
            ('2021-01-01', 7, 'شركة الافضل لوساطة التامين', NULL, 'محاسب', NULL, NULL, '2018-01-01', NULL, 0),
            ('2021-06-01', 7, 'الافضل لوساطة التأمين', NULL, 'محاسب', NULL, NULL, '2018-02-01', NULL, 0),
            ('2021-01-01', 8, '无', NULL, 'Analyst', NULL, NULL, '2019-01-01', NULL, 0),
            ('2021-01-01', 9, '無', NULL, 'Analyst', NULL, NULL, '2019-01-01', NULL, 0)
        """)
        self.assertEqual({r[1] for r in rows if r[1].startswith("6-")}, {"6-1"})
        self.assertEqual({r[1] for r in rows if r[1].startswith("7-")}, {"7-1"})
        self.assertEqual([r[2] for r in rows if r[1] == "8-1"], [None])
        self.assertEqual([r[2] for r in rows if r[1] == "9-1"], [None])

    def test_dates_scraped_into_location_are_recovered(self):
        rows, _ = self.run_pipeline("""
            ('2021-01-01', 3, 'Acme', NULL, 'Analyst', 'Paris', NULL, '2020-02-01', '2020-02-01', 0),
            ('2021-02-01', 3, 'Acme', NULL, 'Analyst', 'Feb 2020', NULL, NULL, NULL, 0)
        """)
        self.assertEqual({r[1] for r in rows}, {"3-1"})
        latest = rows[0]
        self.assertEqual(latest[5], "Paris")
        self.assertEqual(latest[7], "2020-02-01")

    def test_no_information_from_the_future(self):
        history = CASE_BASE + """,
            ('2026-09-16', 12345, 'RL', 'linkedin.com/company/revelio-labs', 'MLE', 'New York, NY', NULL, '2026-08-01', NULL, 0),
            ('2026-10-01', 12345, 'Revelio Labs', 'linkedin.com/company/revelio-labs', 'Staff MLE', 'Remote', 'New text', '2026-07-01', NULL, 0)
        """
        full, _ = self.run_pipeline(history)
        shutil.rmtree(self.output)
        os.remove(os.path.join(self.input, "part.parquet"))
        truncated, _ = self.run_pipeline(CASE_BASE)
        self.assertEqual([r for r in full if r[0] <= "2026-09-01"], truncated)


if __name__ == "__main__":
    unittest.main()
