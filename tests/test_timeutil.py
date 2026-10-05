import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from steel_audit.timeutil import (  # noqa: E402
    overlap_seconds,
    period_end,
    subtract_intervals,
    union_intervals,
    validate_period,
)
from support import dt  # noqa: E402


class PeriodTests(unittest.TestCase):
    def test_shift_end_is_8_hours(self):
        self.assertEqual(period_end("shift", dt("2026-06-01T08:00")), dt("2026-06-01T16:00"))

    def test_month_end_rolls_year(self):
        self.assertEqual(period_end("month", dt("2026-12-01T00:00")), dt("2027-01-01T00:00"))

    def test_quarter_end(self):
        self.assertEqual(period_end("quarter", dt("2026-10-01T00:00")), dt("2027-01-01T00:00"))

    def test_unaligned_periods_rejected(self):
        for period_type, start in [
            ("shift", dt("2026-06-01T03:00")),
            ("day", dt("2026-06-01T06:00")),
            ("iso_week", dt("2026-06-02T00:00")),   # 周二
            ("month", dt("2026-06-15T00:00")),
            ("quarter", dt("2026-02-01T00:00")),
        ]:
            with self.assertRaises(ValueError, msg=f"{period_type} @ {start}"):
                validate_period(period_type, start)

    def test_unknown_period_type_rejected(self):
        with self.assertRaises(ValueError):
            validate_period("hour", dt("2026-06-01T00:00"))


class IntervalTests(unittest.TestCase):
    def test_overlap_seconds(self):
        a = (dt("2026-06-01T06:00"), dt("2026-06-01T10:00"))
        b = (dt("2026-06-01T08:00"), dt("2026-06-01T16:00"))
        self.assertEqual(overlap_seconds(a, b), 7200)
        self.assertEqual(overlap_seconds(a, a), 4 * 3600)

    def test_union_merges_touching(self):
        merged = union_intervals([
            (dt("2026-06-01T08:00"), dt("2026-06-01T10:00")),
            (dt("2026-06-01T06:00"), dt("2026-06-01T08:00")),
            (dt("2026-06-01T20:00"), dt("2026-06-01T21:00")),
        ])
        self.assertEqual(merged, [
            (dt("2026-06-01T06:00"), dt("2026-06-01T10:00")),
            (dt("2026-06-01T20:00"), dt("2026-06-01T21:00")),
        ])

    def test_subtract_intervals(self):
        base = [(dt("2026-06-01T00:00"), dt("2026-06-01T12:00"))]
        cuts = [(dt("2026-06-01T03:00"), dt("2026-06-01T05:00")),
                (dt("2026-06-01T10:00"), dt("2026-06-01T20:00"))]
        self.assertEqual(subtract_intervals(base, cuts), [
            (dt("2026-06-01T00:00"), dt("2026-06-01T03:00")),
            (dt("2026-06-01T05:00"), dt("2026-06-01T10:00")),
        ])


if __name__ == "__main__":
    unittest.main()
