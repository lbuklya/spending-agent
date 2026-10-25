import datetime as dt
import unittest

import spending_agent as sa

LOG = """# Учёт трат

| Дата | Сумма, ₽ | Категория | Что |
|------|----------|-----------|-----|
| 25.10 | 697 | продукты | арахисовая паста |
| 24.10 | -2 890 | прочее | возврат |
| 24.10 | 1 582 | продукты | доставка |
| 23.10 | 14 113 | стоматолог | лечение |
| 20.10 | 6 130 | Кир | врач (вне лимита недели) |
| 18.10 | 720 | прочее | прошлая неделя |
"""

SUNDAY = dt.date(2026, 10, 25)


class ParseOfflineTest(unittest.TestCase):
    def test_simple_phrase(self):
        self.assertEqual(sa.parse_offline("продукты 908"),
                         {"amount": 908, "category": "продукты", "description": "продукты", "days_ago": 0})

    def test_yesterday_and_spaces_in_amount(self):
        entry = sa.parse_offline("такси 1 180 вчера")
        self.assertEqual((entry["amount"], entry["days_ago"]), (1180, 1))

    def test_no_amount_is_an_error(self):
        with self.assertRaises(ValueError):
            sa.parse_offline("просто текст")


class LedgerTest(unittest.TestCase):
    def test_week_counts_only_limit_rows_of_this_week(self):
        # 697 - 2890 + 1582: dentist, «вне лимита» and last week's row are excluded
        self.assertEqual(sa.week_spent(LOG, SUNDAY), -611)

    def test_row_goes_first_in_table(self):
        text, row = sa.add_row(LOG, SUNDAY, {"amount": 908, "category": "продукты", "description": "магнит"})
        self.assertEqual(row, "| 25.10 | 908 | продукты | магнит |")
        self.assertEqual(text.split("\n")[4], row)
        self.assertEqual(sa.week_spent(text, SUNDAY), 297)


if __name__ == "__main__":
    unittest.main()
