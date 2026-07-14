import datetime as dt
import unittest

import engine


class ParseSinceExprTest(unittest.TestCase):
    def test_relative_days(self):
        fixed = dt.date(2026, 7, 15)
        self.assertEqual(engine.parse_since_expr("90d", today=fixed), "16-Apr-2026")

    def test_absolute_date(self):
        self.assertEqual(engine.parse_since_expr("2026-07-01"), "01-Jul-2026")

    def test_empty_fallback(self):
        self.assertEqual(engine.parse_since_expr(None), "01-Jan-2000")
        self.assertEqual(engine.parse_since_expr(""), "01-Jan-2000")

    def test_invalid_falls_back(self):
        self.assertEqual(engine.parse_since_expr("abc"), "01-Jan-2000")


if __name__ == "__main__":
    unittest.main()
