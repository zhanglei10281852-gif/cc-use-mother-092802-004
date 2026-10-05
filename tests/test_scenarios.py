"""验收场景：产线切换（改造）、样本撤回、规则升级后的复核。

每个场景都验证：已签发结论不可变，新结论通过更正关系引用旧版，
且签发时生成的差异说明能区分“工艺变化”与“数据补齐”。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from support import add_sample, dt, make_line, make_measured_rule, make_service  # noqa: E402


class LineSwitchScenarioTests(unittest.TestCase):
    """产线月中完成改造：6 月 15 日旧设备退役、新设备投运。

    核算必须按设备投运边界分段，使改造效果可归因到具体工艺段；
    迟到样本触发的更正必须能说明差异来自数据补齐而非工艺变化。
    """

    def setUp(self):
        self.svc, self.clock = make_service(now=dt("2026-06-20T00:00"))
        self.svc.create_line(plant_id="P1", name="二号线", line_id="L1")
        self.svc.add_equipment_interval(
            "L1", equipment_code="BF-OLD", technology="传统烧结",
            commissioned_from="2026-01-01T00:00",
            decommissioned_to="2026-06-15T00:00")
        self.svc.add_equipment_interval(
            "L1", equipment_code="BF-NEW", technology="超低排放改造",
            commissioned_from="2026-06-15T00:00")
        make_measured_rule(self.svc, code="GB-2025", min_coverage="0",
                           late_grace_hours=48)
        add_sample(self.svc, "L1", "m-1", "120",
                   "2026-06-05T00:00", "2026-06-05T12:00",
                   received_at="2026-06-05T13:00")   # 旧设备段
        add_sample(self.svc, "L1", "m-2", "60",
                   "2026-06-20T00:00", "2026-06-20T12:00",
                   received_at="2026-06-20T13:00")   # 新设备段

    def test_retrofit_period_split_by_equipment_interval(self):
        self.clock.set(dt("2026-07-01T00:00"))
        issued = self.svc.issue(
            self.svc.compute("L1", "month", "2026-06-01T00:00")["conclusion_id"])

        segments = issued["detail"]["segments"]
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["equipment"]["equipment_code"], "BF-OLD")
        self.assertEqual(segments[0]["start"], "2026-06-01T00:00:00Z")
        self.assertEqual(segments[0]["end"], "2026-06-15T00:00:00Z")
        self.assertEqual(segments[0]["indicators"]["pm"]["allocated"], "120.000000")
        self.assertEqual(segments[1]["equipment"]["equipment_code"], "BF-NEW")
        self.assertEqual(segments[1]["indicators"]["pm"]["allocated"], "60.000000")
        self.assertEqual(issued["totals"]["total_emission"], "180.000000")

    def test_late_sample_correction_attributes_to_data_backfill(self):
        self.clock.set(dt("2026-07-01T00:00"))
        first = self.svc.issue(
            self.svc.compute("L1", "month", "2026-06-01T00:00")["conclusion_id"])

        # 迟到样本：6 月 28 日（新设备段）的监测值 7 月 3 日才送达
        self.clock.set(dt("2026-07-03T09:00"))
        late = add_sample(self.svc, "L1", "m-3", "30",
                          "2026-06-28T00:00", "2026-06-28T12:00")
        late_id = late["sample"]["sample_id"]

        self.clock.set(dt("2026-07-04T00:00"))
        second = self.svc.issue(
            self.svc.compute("L1", "month", "2026-06-01T00:00")["conclusion_id"])

        # 更正关系：新结论引用旧版，旧版不可变
        self.assertEqual(second["corrects_id"], first["conclusion_id"])
        self.assertEqual(second["totals"]["total_emission"], "210.000000")
        old = self.svc.get_conclusion(first["conclusion_id"])
        self.assertEqual(old["totals"]["total_emission"], "180.000000")
        self.assertFalse(old["is_current"])
        self.assertTrue(second["is_current"])

        # 差异说明：增量来自迟到样本（数据补齐），工艺分段未变
        explanation = second["explanation"]
        self.assertEqual(explanation["type"], "correction")
        self.assertEqual(explanation["samples_added"], [late_id])
        self.assertEqual(explanation["samples_removed"], [])
        self.assertIs(explanation["equipment_change"], False)
        self.assertIsNone(explanation["rule_change"])
        self.assertIn("迟到或补报样本纳入", explanation["cause_tags"])
        self.assertEqual(
            explanation["total_deltas"]["total_emission"],
            {"old": "180.000000", "new": "210.000000", "delta": "30.000000"})

        # 分段归因：旧设备段不变，增量全部落在新设备段
        segments = second["detail"]["segments"]
        self.assertEqual(segments[0]["indicators"]["pm"]["allocated"], "120.000000")
        self.assertEqual(segments[1]["indicators"]["pm"]["allocated"], "90.000000")

        # 变更脉络：从任一版本都能看到完整更正链
        lineage = self.svc.get_lineage(first["conclusion_id"])
        self.assertEqual([n["conclusion_id"] for n in lineage["chain"]],
                         [first["conclusion_id"], second["conclusion_id"]])
        self.assertEqual(lineage["current_head"], second["conclusion_id"])


class SampleWithdrawalScenarioTests(unittest.TestCase):
    """样本撤回：已签发结论不动，重算生成更正结论并引用旧版。"""

    def test_withdrawal_creates_correction_chain(self):
        svc, clock = make_service()
        make_line(svc)
        make_measured_rule(svc)
        add_sample(svc, "L1", "m-1", "100",
                   "2026-06-02T00:00", "2026-06-02T12:00")
        suspect = add_sample(svc, "L1", "m-2", "200",
                             "2026-06-03T00:00", "2026-06-03T12:00")
        first = svc.issue(
            svc.compute("L1", "month", "2026-06-01T00:00")["conclusion_id"])
        self.assertEqual(first["totals"]["total_emission"], "300.000000")

        # 监测机构撤回问题样本后重算
        svc.withdraw_sample(suspect["sample"]["sample_id"], reason="仪器校准异常")
        clock.set(dt("2026-07-02T00:00"))
        second = svc.issue(
            svc.compute("L1", "month", "2026-06-01T00:00")["conclusion_id"])

        self.assertEqual(second["corrects_id"], first["conclusion_id"])
        self.assertEqual(second["totals"]["total_emission"], "100.000000")
        self.assertEqual(second["explanation"]["samples_removed"],
                         [suspect["sample"]["sample_id"]])
        self.assertIn("样本撤回或数据截止调整", second["explanation"]["cause_tags"])
        # 撤回本身也作为证据缺口标出
        self.assertIn("withdrawn_samples", {g["type"] for g in second["gaps"]})

        # 旧版保持原值、可继续查询明细
        old = svc.get_conclusion(first["conclusion_id"])
        self.assertEqual(old["totals"]["total_emission"], "300.000000")
        self.assertEqual(len(old["detail"]["samples"]), 2)

        lineage = svc.get_lineage(second["conclusion_id"])
        self.assertEqual(len(lineage["chain"]), 2)
        self.assertEqual(lineage["chain"][0]["conclusion_id"],
                         first["conclusion_id"])


class RuleUpgradeScenarioTests(unittest.TestCase):
    """规则升级复核：新规则版本下重算历史周期，结论链保留完整脉络。"""

    def test_review_under_upgraded_rule_references_previous(self):
        svc, clock = make_service()
        make_line(svc)
        svc.create_rule_version(
            code="STEEL-2025", effective_from="2026-01-01T00:00",
            params={"indicators": ["output"], "method": "factor",
                    "output_indicator": "output", "emission_factor": "1.8",
                    "intensity_denominator": "output", "min_coverage_ratio": "0"})
        add_sample(svc, "L1", "o-1", "1000",
                   "2026-06-10T00:00", "2026-06-11T00:00",
                   indicator="output", unit="t")

        first = svc.issue(
            svc.compute("L1", "month", "2026-06-01T00:00")["conclusion_id"])
        self.assertEqual(first["rule_code"], "STEEL-2025")
        self.assertEqual(first["totals"]["total_emission"], "1800.000000")

        # 规则升级：2026-07-01 起排放因子调整为 2.2
        upgraded = svc.create_rule_version(
            code="STEEL-2026", effective_from="2026-07-01T00:00",
            params={"indicators": ["output"], "method": "factor",
                    "output_indicator": "output", "emission_factor": "2.2",
                    "intensity_denominator": "output", "min_coverage_ratio": "0"})

        # 复核 6 月：显式指定新版本规则重算
        clock.set(dt("2026-07-05T00:00"))
        second = svc.issue(svc.compute(
            "L1", "month", "2026-06-01T00:00",
            rule_version_id=upgraded["rule_version_id"])["conclusion_id"])

        self.assertEqual(second["corrects_id"], first["conclusion_id"])
        self.assertEqual(second["totals"]["total_emission"], "2200.000000")
        explanation = second["explanation"]
        self.assertEqual(explanation["rule_change"],
                         {"from_rule_version_id": first["rule_version_id"],
                          "to_rule_version_id": upgraded["rule_version_id"],
                          "from_code": "STEEL-2025", "to_code": "STEEL-2026"})
        self.assertIn("规则升级/变更", explanation["cause_tags"])
        # 样本集合未变：差异完全来自规则
        self.assertEqual(explanation["samples_added"], [])
        self.assertEqual(explanation["samples_removed"], [])

        # 7 月起默认解析到新规则
        july = svc.compute("L1", "month", "2026-07-01T00:00")
        self.assertEqual(july["rule_code"], "STEEL-2026")

        # 脉络：两版规则、两个结论一目了然
        lineage = svc.get_lineage(first["conclusion_id"])
        self.assertEqual([n["rule_code"] for n in lineage["chain"]],
                         ["STEEL-2025", "STEEL-2026"])


if __name__ == "__main__":
    unittest.main()
