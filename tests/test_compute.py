"""核算引擎：跨班分摊、数据截止、停机扣减、证据缺口、规则方法。"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from steel_audit.errors import NotFoundError, ValidationError  # noqa: E402
from support import add_sample, dt, make_line, make_measured_rule, make_service  # noqa: E402


class CrossShiftAllocationTests(unittest.TestCase):
    def test_sample_spanning_shift_boundary_allocated_by_time(self):
        """跨班次样本按时间重叠比例分摊：06:00-10:00 各落 2 小时到两个班次。"""
        svc, _ = make_service()
        make_line(svc)
        make_measured_rule(svc)
        add_sample(svc, "L1", "uid-1", "100",
                   "2026-06-01T06:00", "2026-06-01T10:00")

        first = svc.compute("L1", "shift", "2026-06-01T00:00")
        second = svc.compute("L1", "shift", "2026-06-01T08:00")
        self.assertEqual(first["totals"]["total_emission"], "50.000000")
        self.assertEqual(second["totals"]["total_emission"], "50.000000")

        # 日周期则完整计入
        day = svc.compute("L1", "day", "2026-06-01T00:00")
        self.assertEqual(day["totals"]["total_emission"], "100.000000")


class DataCutoffTests(unittest.TestCase):
    def test_late_sample_invisible_before_cutoff(self):
        """数据截止后送达的样本不参与本次核算，并作为迟到缺口标出。"""
        svc, clock = make_service(now=dt("2026-07-01T00:00"))
        make_line(svc)
        make_measured_rule(svc, late_grace_hours=24)
        add_sample(svc, "L1", "uid-1", "100",
                   "2026-06-10T00:00", "2026-06-10T12:00",
                   received_at="2026-06-10T13:00")
        add_sample(svc, "L1", "uid-2", "40",
                   "2026-06-20T00:00", "2026-06-20T12:00",
                   received_at="2026-07-02T09:00")  # 迟到

        first = svc.compute("L1", "month", "2026-06-01T00:00",
                            data_cutoff="2026-07-01T00:00")
        self.assertEqual(first["totals"]["total_emission"], "100.000000")
        late_gaps = [g for g in first["gaps"] if g["type"] == "late_samples_pending"]
        self.assertEqual(len(late_gaps), 1)
        self.assertEqual(late_gaps[0]["count"], 1)

        # 截止时点后移，迟到样本纳入重算
        second = svc.compute("L1", "month", "2026-06-01T00:00",
                             data_cutoff="2026-07-03T00:00")
        self.assertEqual(second["totals"]["total_emission"], "140.000000")
        self.assertEqual([g for g in second["gaps"]
                          if g["type"] == "late_samples_pending"], [])


class ShutdownAndGapTests(unittest.TestCase):
    def test_shutdown_reduces_operating_hours_and_unexplained_is_gap(self):
        svc, _ = make_service()
        make_line(svc)
        make_measured_rule(svc)
        svc.add_shutdown("L1", "2026-06-01T00:00", "2026-06-01T04:00")  # 无说明
        conclusion = svc.compute("L1", "day", "2026-06-01T00:00")
        self.assertEqual(conclusion["totals"]["operating_hours"], "20.000000")
        self.assertEqual(conclusion["totals"]["shutdown_hours"], "4.000000")
        self.assertIn("unexplained_shutdown", {g["type"] for g in conclusion["gaps"]})

    def test_missing_samples_gap_lists_uncovered_windows(self):
        svc, _ = make_service()
        make_line(svc)
        make_measured_rule(svc, min_coverage="0.9")
        add_sample(svc, "L1", "uid-1", "10",
                   "2026-06-01T00:00", "2026-06-01T06:00")
        conclusion = svc.compute("L1", "day", "2026-06-01T00:00")
        gaps = [g for g in conclusion["gaps"] if g["type"] == "missing_samples"]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["coverage_ratio"], "0.250000")
        self.assertEqual(gaps[0]["uncovered_windows"],
                         [{"start": "2026-06-01T06:00:00Z",
                           "end": "2026-06-02T00:00:00Z"}])

    def test_samples_during_shutdown_flagged(self):
        svc, _ = make_service()
        make_line(svc)
        make_measured_rule(svc)
        svc.add_shutdown("L1", "2026-06-01T00:00", "2026-06-01T08:00",
                         reason="计划检修", evidence_ref="WO-1")
        add_sample(svc, "L1", "uid-1", "10",
                   "2026-06-01T02:00", "2026-06-01T03:00")
        conclusion = svc.compute("L1", "day", "2026-06-01T00:00")
        self.assertIn("samples_during_shutdown", {g["type"] for g in conclusion["gaps"]})

    def test_samples_outside_equipment_interval_flagged(self):
        svc, _ = make_service()
        make_line(svc, equipment_from="2026-06-01T00:00")
        make_measured_rule(svc)
        add_sample(svc, "L1", "uid-1", "10",
                   "2026-05-20T00:00", "2026-05-20T04:00")  # 设备投运前
        conclusion = svc.compute("L1", "month", "2026-05-01T00:00")
        self.assertIn("samples_outside_equipment_interval",
                      {g["type"] for g in conclusion["gaps"]})
        self.assertIn("no_equipment_interval", {g["type"] for g in conclusion["gaps"]})


class RuleMethodTests(unittest.TestCase):
    def test_factor_method_and_output_denominator(self):
        svc, _ = make_service()
        make_line(svc)
        svc.create_rule_version(
            code="R-factor", effective_from="2026-01-01T00:00",
            params={"indicators": ["output"], "method": "factor",
                    "output_indicator": "output", "emission_factor": "2.0",
                    "intensity_denominator": "output", "min_coverage_ratio": "0"})
        add_sample(svc, "L1", "uid-1", "500", "2026-06-10T00:00", "2026-06-11T00:00",
                   indicator="output", unit="t")
        conclusion = svc.compute("L1", "month", "2026-06-01T00:00")
        self.assertEqual(conclusion["totals"]["total_output"], "500.000000")
        self.assertEqual(conclusion["totals"]["total_emission"], "1000.000000")
        self.assertEqual(conclusion["totals"]["intensity"], "2.000000")

    def test_intensity_limit_exceedance(self):
        svc, _ = make_service()
        make_line(svc)
        make_measured_rule(svc, intensity_limit="0.1")
        add_sample(svc, "L1", "uid-1", "100",
                   "2026-06-01T00:00", "2026-06-01T12:00")
        conclusion = svc.compute("L1", "day", "2026-06-01T00:00")
        # 100 kg / 24 h ≈ 4.17 > 0.1
        self.assertIs(conclusion["totals"]["exceedance"], True)

    def test_unaligned_period_rejected(self):
        svc, _ = make_service()
        make_line(svc)
        make_measured_rule(svc)
        with self.assertRaises(ValidationError):
            svc.compute("L1", "month", "2026-06-15T00:00")

    def test_no_effective_rule_rejected(self):
        svc, _ = make_service()
        make_line(svc)
        with self.assertRaises(ValidationError):
            svc.compute("L1", "day", "2026-06-01T00:00")

    def test_unknown_line_rejected(self):
        svc, _ = make_service()
        with self.assertRaises(NotFoundError):
            svc.compute("NOPE", "day", "2026-06-01T00:00")


class DeterminismTests(unittest.TestCase):
    def test_same_inputs_same_outputs(self):
        """同一批已存数据、同一截止时点，重算结果逐字节一致。"""
        svc, _ = make_service()
        make_line(svc)
        make_measured_rule(svc, min_coverage="0.5")
        svc.add_shutdown("L1", "2026-06-03T00:00", "2026-06-03T06:00",
                         reason="检修")
        add_sample(svc, "L1", "uid-1", "100",
                   "2026-06-01T06:00", "2026-06-01T10:00")
        add_sample(svc, "L1", "uid-2", "60",
                   "2026-06-10T00:00", "2026-06-11T00:00")

        a = svc.compute("L1", "month", "2026-06-01T00:00",
                        data_cutoff="2026-07-01T00:00")
        b = svc.compute("L1", "month", "2026-06-01T00:00",
                        data_cutoff="2026-07-01T00:00")
        for key in ("totals", "gaps"):
            self.assertEqual(json.dumps(a[key], sort_keys=True),
                             json.dumps(b[key], sort_keys=True))
        self.assertEqual(json.dumps(a["detail"], sort_keys=True),
                         json.dumps(b["detail"], sort_keys=True))


if __name__ == "__main__":
    unittest.main()
