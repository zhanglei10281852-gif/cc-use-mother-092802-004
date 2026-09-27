import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from steel_audit.contracts import EmissionSample, ProductionLine


class SteelContractTests(unittest.TestCase):
    def test_sample_identifies_line_and_measurement_time(self):
        time = datetime(2026, 3, 1, tzinfo=timezone.utc)
        value = EmissionSample("s-1", "line-2", "dust", time, Decimal("0.3"), "mg/m3")
        self.assertEqual(value.line_id, "line-2")
        self.assertEqual(value.measured_at, time)

    def test_line_capacity_uses_decimal(self):
        value = ProductionLine("p-1", "line-2", Decimal("1.25"), datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(value.capacity_tonnes, Decimal("1.25"))


if __name__ == "__main__":
    unittest.main()
