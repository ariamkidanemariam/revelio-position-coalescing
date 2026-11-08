import unittest
from datetime import date
from unittest import mock

from src import config
from src.matching import Version, assign_user, initials, seniority, title_key

EPOCH = date(1970, 1, 1)


def day(y, m, d=1):
    return (date(y, m, d) - EPOCH).days


def version(vid, snap, c=None, u=None, t=None, start=None, end=None, ord_=0):
    return Version(vid, snap, ord_, c, u, t, start, end)


def replaced(old, new):
    result = assign_user([old, new], [(1, [old.vid]), (2, [new.vid])])
    return result[old.vid] == result[new.vid]


class TextHelpers(unittest.TestCase):
    def test_title_key_expands_abbreviations_and_keeps_seniority(self):
        self.assertEqual(title_key("sr swe"), "senior software engineer")
        self.assertEqual(title_key("mle"), "machine learning engineer")

    def test_seniority_levels(self):
        self.assertEqual(seniority("software engineer ii"), seniority("software engineer 2"))
        self.assertEqual(seniority("senior software engineer"), {"senior"})
        self.assertEqual(seniority("software engineer"), frozenset())

    def test_initials(self):
        self.assertEqual(initials("revelio labs"), "rl")
        self.assertIsNone(initials("nyu"))


class AssignUser(unittest.TestCase):

    def test_brief_case_b_edited_company_and_title_keep_the_id(self):
        old = version(1, 1, "revelio labs", "linkedin.com/company/revelio-labs",
                      "machine learning engineer", day(2026, 8))
        new = version(2, 2, "rl", "linkedin.com/company/revelio-labs", "mle", day(2026, 8))
        result = assign_user([old, new], [(1, [1]), (2, [2])])
        self.assertEqual(result[1], result[2])

    def test_acronym_without_url_matches_on_title_and_identical_start(self):
        old = version(1, 1, "revelio labs", None, "machine learning engineer", day(2026, 8))
        new = version(2, 2, "rl", None, "mle", day(2026, 8))
        with mock.patch.object(config, "ALLOW_COMPANY_RENAME", False):
            self.assertTrue(replaced(old, new))

    def test_different_roles_at_same_company_stay_separate(self):
        a = version(1, 1, "google", None, "software engineer", day(2020, 1), day(2022, 1))
        b = version(2, 2, "google", None, "engineering manager", day(2022, 2))
        result = assign_user([a, b], [(1, [1]), (2, [1, 2])])
        self.assertNotEqual(result[1], result[2])

    def test_separate_stints_with_same_company_and_title_stay_separate(self):
        a = version(1, 1, "asbl pari", None, "animatrice", day(2019, 4), day(2019, 4))
        b = version(2, 2, "asbl pari", None, "animatrice", day(2019, 7), day(2019, 7))
        result = assign_user([a, b], [(1, [1]), (2, [2])])
        self.assertNotEqual(result[1], result[2])

    def test_corrected_start_date_keeps_the_id(self):
        a = version(1, 1, "acme", None, "analyst", day(2020, 1))
        b = version(2, 2, "acme", None, "analyst", day(2020, 3))
        result = assign_user([a, b], [(1, [1]), (2, [2])])
        self.assertEqual(result[1], result[2])

    def test_two_records_in_one_snapshot_are_two_positions(self):
        a = version(1, 1, "acme", None, "analyst", day(2020, 1), ord_=0)
        b = version(2, 1, "acme", None, "analyst", day(2018, 1), day(2019, 1), ord_=1)
        result = assign_user([a, b], [(1, [1, 2])])
        self.assertNotEqual(result[1], result[2])

    def test_ambiguous_match_joins_one_position_instead_of_creating_a_duplicate(self):
        a = version(1, 1, "acme", None, "analyst", day(2018, 1), day(2018, 6), ord_=0)
        b = version(2, 1, "acme", None, "analyst", day(2019, 1), day(2019, 6), ord_=1)
        c = version(3, 2, "acme", None, "analyst")
        result = assign_user([a, b, c], [(1, [1, 2]), (2, [3])])
        self.assertIn(result[3], {result[1], result[2]})
        self.assertEqual(len(set(result.values())), 2)

    def test_repeated_snapshots_do_not_multiply_positions(self):
        a = version(1, 1, "acme", None, "analyst", day(2020, 1))
        b = version(2, 2, "acme corp", None, "analyst", day(2020, 1))
        snapshots = [(1, [1])] + [(i, [1 if i % 2 else 2]) for i in range(2, 40)]
        result = assign_user([a, b], snapshots)
        self.assertEqual(len(set(result.values())), 1)

    def test_company_renamed_in_place_keeps_the_id(self):
        gfi = version(1, 1, "gfi informatique", "linkedin.com/company/gfiworld",
                      "chef de projet", day(2019, 2))
        inetum = version(2, 3, "inetum", "linkedin.com/company/inetum",
                         "chef de projet", day(2019, 2))
        result = assign_user([gfi, inetum], [(1, [1]), (2, [1]), (3, [2])])
        self.assertEqual(result[1], result[2])

    def test_same_title_and_start_at_unrelated_company_is_not_a_rename_if_not_replaced(self):
        a = version(1, 1, "alpha", None, "consultant", day(2019, 2))
        b = version(2, 3, "beta", None, "consultant", day(2019, 2))
        result = assign_user([a, b], [(1, [1]), (3, [2])])
        self.assertNotEqual(result[1], result[2])

    def test_ids_are_numbered_in_creation_order(self):
        a = version(10, 1, "acme", None, "analyst", day(2020, 1), ord_=0)
        b = version(20, 1, "beta", None, "engineer", day(2018, 1), ord_=1)
        c = version(30, 2, "gamma", None, "manager", day(2021, 1), ord_=0)
        result = assign_user([a, b, c], [(1, [10, 20]), (2, [30, 10, 20])])
        self.assertEqual((result[10], result[20], result[30]), (1, 2, 3))


class Seniority(unittest.TestCase):

    def test_promotion_with_a_new_start_date_is_a_new_position(self):
        old = version(1, 1, "google", None, "software engineer", day(2020, 1))
        new = version(2, 2, "google", None, "senior software engineer", day(2020, 6))
        self.assertFalse(replaced(old, new))

    def test_level_change_with_a_new_start_date_is_a_new_position(self):
        old = version(1, 1, "acme", None, "analyst i", day(2020, 1))
        new = version(2, 2, "acme", None, "analyst ii", day(2020, 9))
        self.assertFalse(replaced(old, new))

    def test_same_level_written_differently_is_the_same_position(self):
        old = version(1, 1, "acme", None, "senior software engineer", day(2020, 1))
        new = version(2, 2, "acme", None, "sr software eng", day(2020, 1))
        self.assertTrue(replaced(old, new))
        old = version(1, 1, "acme", None, "software engineer ii", day(2020, 1))
        new = version(2, 2, "acme", None, "software engineer 2", day(2020, 1))
        self.assertTrue(replaced(old, new))

    def test_chinese_and_arabic_seniority_words(self):
        self.assertEqual(seniority("高级工程师"), {"senior"})
        self.assertEqual(seniority("محاسب اول"), {"senior"})
        old = version(1, 1, "华为技术", None, "工程师", day(2020, 1))
        self.assertFalse(replaced(old, version(2, 2, "华为技术", None, "高级工程师", day(2020, 6))))
        self.assertTrue(replaced(old, version(2, 2, "华为技术", None, "高级工程师", day(2020, 1))))

    def test_other_title_edit_at_same_level_keeps_the_id(self):
        old = version(1, 1, "deloitte", None, "consulting analyst", day(2021, 1))
        new = version(2, 2, "deloitte", None, "analyst", day(2021, 1))
        self.assertTrue(replaced(old, new))


class Acronyms(unittest.TestCase):

    def test_acronym_without_url_needs_the_same_start_date(self):
        old = version(1, 1, "revelio labs", None, "machine learning engineer", day(2026, 8))
        self.assertTrue(replaced(old, version(2, 2, "rl", None, "machine learning engineer ops", day(2026, 8))))
        self.assertFalse(replaced(old, version(2, 2, "rl", None, "mle", day(2026, 9))))

    def test_acronym_without_any_dates_is_not_enough(self):
        old = version(1, 1, "revelio labs", None, "machine learning engineer")
        new = version(2, 2, "rl", None, "machine learning engineer")
        self.assertFalse(replaced(old, new))

    def test_acronym_with_conflicting_urls_is_not_enough(self):
        old = version(1, 1, "revelio labs", "linkedin.com/company/revelio-labs",
                      "software developer", day(2026, 8))
        new = version(2, 2, "rl", "linkedin.com/company/rocket-lab",
                      "software developer backend", day(2026, 8))
        self.assertFalse(replaced(old, new))

    def test_same_url_links_an_acronym_without_dates(self):
        old = version(1, 1, "revelio labs", "linkedin.com/company/revelio-labs", "machine learning engineer")
        new = version(2, 2, "rl", "linkedin.com/company/revelio-labs", "mle")
        self.assertTrue(replaced(old, new))


class PolicySwitches(unittest.TestCase):

    def test_rename_rule_can_be_switched_off(self):
        gfi = version(1, 1, "gfi informatique", None, "chef de projet", day(2019, 2))
        inetum = version(2, 2, "inetum", None, "chef de projet", day(2019, 2))
        self.assertTrue(replaced(gfi, inetum))
        with mock.patch.object(config, "ALLOW_COMPANY_RENAME", False):
            self.assertFalse(replaced(gfi, inetum))

    def test_title_edit_rule_can_be_switched_off(self):
        old = version(1, 1, "deloitte", None, "consulting analyst", day(2021, 1))
        new = version(2, 2, "deloitte", None, "analyst", day(2021, 1))
        with mock.patch.object(config, "ALLOW_TITLE_EDIT", False):
            self.assertFalse(replaced(old, new))


class LargeHistories(unittest.TestCase):

    def test_similar_names_are_found_beyond_fifty_positions(self):
        others = [version(100 + i, 1, f"company {i}", None, f"role {i}", day(2000, 1) + i, ord_=i)
                  for i in range(60)]
        old = version(1, 1, "acme technologies", None, "data engineer", day(2020, 1), ord_=60)
        new = version(2, 2, "acme technology", None, "data engineers", day(2020, 3))
        snapshots = [(1, [o.vid for o in others] + [1]), (2, [o.vid for o in others] + [2])]
        result = assign_user(others + [old, new], snapshots)
        self.assertEqual(result[1], result[2])


class RecordsWithoutDates(unittest.TestCase):

    def test_record_without_dates_joins_the_same_company_and_title(self):
        a = version(1, 1, "acme", None, "analyst", day(2020, 1))
        b = version(2, 2, "acme", None, "analyst")
        self.assertTrue(replaced(a, b))


if __name__ == "__main__":
    unittest.main()
