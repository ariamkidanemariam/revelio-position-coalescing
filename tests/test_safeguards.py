import glob
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import duckdb

from src import build_output, config, main
from tests.test_matching import day, version
from src.matching import assign_user
from tests.test_pipeline import CASE_BASE, COLUMNS


def same(result, a, b):
    return result[a.vid] == result[b.vid]


class OutputChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.input = os.path.join(self.tmp, "input")
        self.output = os.path.join(self.tmp, "output")
        os.makedirs(self.input)
        self.saved = config.WORK_DIR
        config.WORK_DIR = os.path.join(self.tmp, "work")
        con = duckdb.connect(os.path.join(self.tmp, "make.duckdb"))
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
        config.WORK_DIR = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_failed_output_check_makes_the_command_fail(self):
        failing = build_output.check_queries(self.output) + [("forced failure", "SELECT 1", 0)]
        argv = ["main", "--input", self.input, "--output", self.output]
        with mock.patch.object(build_output, "check_queries", return_value=failing), \
                mock.patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as stop:
                main.main()
        self.assertNotEqual(stop.exception.code, 0)

    def test_other_parquet_files_in_the_output_folder_do_not_affect_the_checks(self):
        main.run(self.input, self.output)
        ours = sorted(glob.glob(os.path.join(self.output, "data_*.parquet")))[0]
        shutil.copy(ours, os.path.join(self.output, "copy_from_somewhere_else.parquet"))
        stats = main.run(self.input, self.output)
        self.assertEqual(stats["checks_failed"], 0)


class StartDateAnchor(unittest.TestCase):

    def test_one_correction_within_a_year_keeps_the_position(self):
        a = version(1, 1, "acme", None, "analyst", day(2020, 1))
        b = version(2, 2, "acme", None, "analyst", day(2020, 12))
        result = assign_user([a, b], [(1, [1]), (2, [2])])
        self.assertTrue(same(result, a, b))

    def test_chained_corrections_cannot_drift_the_start_date(self):
        a = version(1, 1, "acme", None, "analyst", day(2020, 1))
        b = version(2, 2, "acme", None, "analyst", day(2020, 12))
        c = version(3, 3, "acme", None, "analyst", day(2021, 11))
        d = version(4, 4, "acme", None, "analyst", day(2022, 10))
        result = assign_user([a, b, c, d], [(1, [1]), (2, [2]), (3, [3]), (4, [4])])
        self.assertTrue(same(result, a, b))
        self.assertFalse(same(result, a, c))
        self.assertFalse(same(result, a, d))


class TitleEdits(unittest.TestCase):

    def test_reworded_title_replacing_the_old_one_keeps_the_position(self):
        a = version(1, 1, "deloitte", None, "consulting analyst", day(2021, 1))
        b = version(2, 2, "deloitte", None, "analyst", day(2021, 1))
        result = assign_user([a, b], [(1, [1]), (2, [2])])
        self.assertTrue(same(result, a, b))

    def test_unrelated_title_with_same_company_and_start_is_a_new_position(self):
        a = version(1, 1, "google", None, "software engineer", day(2022, 1))
        b = version(2, 2, "google", None, "product manager", day(2022, 1))
        result = assign_user([a, b], [(1, [1]), (2, [2])])
        self.assertFalse(same(result, a, b))

    def test_real_example_without_a_shared_word_is_a_new_position(self):
        a = version(1, 1, "boeing", None, "system engineer", day(2019, 6))
        b = version(2, 2, "boeing", None, "engineering it", day(2019, 6))
        result = assign_user([a, b], [(1, [1]), (2, [2])])
        self.assertFalse(same(result, a, b))

    def test_title_edit_needs_the_old_entry_in_the_previous_snapshot(self):
        a = version(1, 1, "deloitte", None, "consulting analyst", day(2021, 1))
        other = version(2, 2, "nyu", None, "teaching assistant", day(2018, 9))
        b = version(3, 3, "deloitte", None, "analyst", day(2021, 1))
        result = assign_user([a, other, b], [(1, [1]), (2, [2]), (3, [3])])
        self.assertFalse(same(result, a, b))

    def test_title_edit_needs_an_identical_start_date(self):
        a = version(1, 1, "deloitte", None, "consulting analyst", day(2021, 1))
        b = version(2, 2, "deloitte", None, "analyst", day(2021, 3))
        result = assign_user([a, b], [(1, [1]), (2, [2])])
        self.assertFalse(same(result, a, b))

    def test_unrelated_title_cannot_use_a_seniority_change(self):
        a = version(1, 1, "google", None, "software engineer", day(2022, 1))
        b = version(2, 2, "google", None, "senior product manager", day(2022, 1))
        result = assign_user([a, b], [(1, [1]), (2, [2])])
        self.assertFalse(same(result, a, b))


def replaced_in_place(old, new):
    result = assign_user([old, new], [(1, [old.vid]), (2, [new.vid])])
    return result[old.vid] == result[new.vid]


class LevelChangesEditedInPlace(unittest.TestCase):

    def test_assistant_coach_to_associate_head_coach(self):
        old = version(1, 1, "new mexico highlands university", None, "assistant men basketball coach", day(2019, 7))
        new = version(2, 2, "new mexico highlands university", None, "associate head coach", day(2019, 7))
        self.assertTrue(replaced_in_place(old, new))

    def test_customer_service_agent_to_managing_senior_agent(self):
        old = version(1, 1, "transcom", None, "customer service agent", day(2019, 7))
        new = version(2, 2, "transcom", None, "managing senior agent", day(2019, 7))
        self.assertTrue(replaced_in_place(old, new))

    def test_engineer_to_senior_engineer(self):
        old = version(1, 1, "acme", None, "engineer", day(2020, 1))
        new = version(2, 2, "acme", None, "senior engineer", day(2020, 1))
        self.assertTrue(replaced_in_place(old, new))

    def test_senior_engineer_to_junior_engineer(self):
        old = version(1, 1, "acme", None, "senior engineer", day(2020, 1))
        new = version(2, 2, "acme", None, "junior engineer", day(2020, 1))
        self.assertTrue(replaced_in_place(old, new))

    def test_engineer_to_senior_engineer_with_a_different_start(self):
        old = version(1, 1, "acme", None, "engineer", day(2020, 1))
        new = version(2, 2, "acme", None, "senior engineer", day(2020, 4))
        self.assertFalse(replaced_in_place(old, new))

    def test_engineer_and_senior_engineer_in_one_snapshot(self):
        a = version(1, 1, "acme", None, "engineer", day(2020, 1), ord_=0)
        b = version(2, 2, "acme", None, "senior engineer", day(2020, 1), ord_=0)
        result = assign_user([a, b], [(1, [1]), (2, [1, 2])])
        self.assertFalse(same(result, a, b))

    def test_old_entry_gone_for_several_snapshots(self):
        a = version(1, 1, "acme", None, "engineer", day(2020, 1))
        other = version(2, 2, "nyu", None, "teaching assistant", day(2018, 9))
        b = version(3, 3, "acme", None, "senior engineer", day(2020, 1))
        result = assign_user([a, other, b], [(1, [1]), (2, [2]), (3, [3])])
        self.assertFalse(same(result, a, b))

    def test_different_company_with_same_title_and_start(self):
        old = version(1, 1, "google", None, "engineer", day(2020, 1))
        new = version(2, 2, "microsoft", None, "senior engineer", day(2020, 1))
        self.assertFalse(replaced_in_place(old, new))

    def test_company_known_only_by_acronym(self):
        old = version(1, 1, "revelio labs", None, "engineer", day(2020, 1))
        new = version(2, 2, "rl", None, "senior engineer", day(2020, 1))
        self.assertFalse(replaced_in_place(old, new))

    def test_two_new_records_compete_for_one_old_position(self):
        a = version(1, 1, "acme", None, "engineer", day(2020, 1), ord_=0)
        b = version(2, 2, "acme", None, "senior engineer", day(2020, 1), ord_=0)
        c = version(3, 2, "acme", None, "lead engineer", day(2020, 1), ord_=1)
        result = assign_user([a, b, c], [(1, [1]), (2, [2, 3])])
        self.assertNotEqual(result[2], result[3])
        self.assertEqual([result[2], result[3]].count(result[1]), 1)

    def test_the_rule_can_be_switched_off(self):
        old = version(1, 1, "acme", None, "engineer", day(2020, 1))
        new = version(2, 2, "acme", None, "senior engineer", day(2020, 1))
        with mock.patch.object(config, "ALLOW_LEVEL_EDIT", False, create=True):
            self.assertFalse(replaced_in_place(old, new))


class SeniorityVocabulary(unittest.TestCase):

    def check(self, title, expected):
        from src.matching import seniority
        self.assertEqual(seniority(title), frozenset(expected), title)

    def test_intern_anywhere(self):
        self.check("intern", {"intern"})
        self.check("marketing intern", {"intern"})

    def test_lead_in_a_multi_word_title(self):
        self.check("lead engineer", {"lead"})
        self.check("technical lead", {"lead"})
        self.check("lead", set())

    def test_principal_staff_associate_assistant_as_first_word(self):
        self.check("principal engineer", {"principal"})
        self.check("principal", set())
        self.check("staff engineer", {"staff"})
        self.check("kitchen staff", set())
        self.check("associate director", {"associate"})
        self.check("sales associate", set())
        self.check("associate", set())
        self.check("assistant manager", {"assistant"})
        self.check("administrative assistant", set())

    def test_head_of_and_chief_officer(self):
        self.check("head of sales", {"head"})
        self.check("head coach", set())
        self.check("chief executive officer", {"chief"})
        self.check("chief accountant", set())

    def test_single_letter_numerals_only_count_as_the_last_word(self):
        self.check("analyst i", {"1"})
        self.check("engineer v", {"5"})
        self.check("coordinadora de projectes transversals i organitzacio", set())
        self.check("specjalista ds sprzedazy i marketingu", set())
        self.check("v p marketing and sales", set())
        self.check("i c commisioning engineer", set())
        self.check("claims specialist iii at amtrust financial", {"3"})

    def test_catalan_and_is_not_a_level_change(self):
        a = version(1, 1, "ajuntament", None, "tecnica de comunicacio", day(2020, 1))
        b = version(2, 2, "ajuntament", None, "tecnica de comunicacio i premsa", day(2020, 3))
        result = assign_user([a, b], [(1, [1]), (2, [2])])
        self.assertTrue(same(result, a, b))

    def test_level_words_are_not_shared_meaningful_words(self):
        a = version(1, 1, "acme", None, "sales associate", day(2020, 1))
        b = version(2, 2, "acme", None, "associate director", day(2020, 1))
        self.assertFalse(replaced_in_place(a, b))


if __name__ == "__main__":
    unittest.main()
