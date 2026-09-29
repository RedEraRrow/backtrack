"""The one date/time parser: splitting a stamp, and reading what was typed."""
import datetime
import unittest

from src.utils.datetime_parse import parse_datetime, split_stamp


class SplitStampTest(unittest.TestCase):
    def test_halves(self):
        self.assertEqual(split_stamp('2008-07-02T18:30:00Z'), ('2008-07-02', '18:30:00'))
        self.assertEqual(split_stamp('2008-07-02 18:30'), ('2008-07-02', '18:30'))
        self.assertEqual(split_stamp('2008-07-02+01:00'), ('2008-07-02', ''))

    def test_a_space_inside_the_date_is_not_a_time(self):
        self.assertEqual(split_stamp('2008 07 02'), ('2008 07 02', ''))

    def test_nothing(self):
        self.assertEqual(split_stamp(None), ('', ''))


class ParseDatetimeTest(unittest.TestCase):
    def test_forms(self):
        d = datetime.date(2008, 7, 2)
        for raw in ('2008-07-02', '2008 07 02', '20080702', '2008-07-02T18:30Z'):
            self.assertEqual(parse_datetime(raw).date, d, raw)

    def test_ambiguous_day_month_needs_telling(self):
        self.assertIsNone(parse_datetime('02/07/2008').date)
        self.assertEqual(parse_datetime('02/07/2008', dayfirst=True).date, datetime.date(2008, 7, 2))

    def test_empty(self):
        self.assertIsNone(parse_datetime('').date)
        self.assertIsNone(parse_datetime(None).date)


if __name__ == "__main__":
    unittest.main()
