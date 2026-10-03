"""结论生命周期测试：签发不可变、撤回触发更正、规则升级后复核。"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from steel_audit.conclusions import ConclusionStatus
from steel_audit.errors import ImmutableError, ValidationError
from steel_audit.service import AuditService

UTC = timezone.utc


def _iso(year, month, day, hour=0):
    return datetime(year, month, day, hour, tzinfo=UTC).isoformat()


def build_service() -> AuditService:
    svc = AuditService()
    svc.register_line(plant_id="P1", line_id="L1", capacity_tonnes="100",
                      commissioned_at=_iso(2026, 1, 1))
    # v1 规则：粉尘限值 10，每 4 小时一个标称采样间隔
    svc.add_rule(rule_id="R", version="1.0", effective_at=_iso(2026, 1, 1),
                 limits={"dust": Decimal("10")},
                 canonical_units={"dust": "mg/m3"},
                 min_samples=1, sampling_interval_hours="4")
    svc.add_equipment(equipment_id="E1", line_id="L1",
                      valid_from=_iso(2026, 2, 1), phase="baseline")
    # 改造当天全天每 4 小时一次样本（00/04/08/12/16/20 时），值 9
    for i, hour in enumerate(range(0, 24, 4)):
        svc.submit_sample(
            sample_id=f"s{i}", line_id="L1", pollutant="dust",
            measured_at=_iso(2026, 3, 1, hour), value="9", unit="mg/m3",
            received_at=_iso(2026, 3, 2, hour),
        )
    return svc


DAY_ANCHOR = _iso(2026, 3, 1, 12)
AS_OF_MARCH3 = _iso(2026, 3, 3)


class ConclusionImmutabilityTests(unittest.TestCase):
    def test_issued_conclusion_snapshot_unchanged_after_withdrawal(self):
        svc = build_service()
        c1 = svc.issue_conclusion(line_id="L1", period="day", anchor=DAY_ANCHOR,
                                  rule_id="R", issued_by="regulator-a",
                                  as_of=AS_OF_MARCH3, conclusion_id="C1")
        self.assertEqual(c1.status, ConclusionStatus.ISSUED)
        snapshot_value = c1.result.pollutants[0].value
        snapshot_hash = c1.calc_hash
        snapshot_samples = c1.evidence.sample_ids

        # 撤回一份样本并补一份新样本，重算结果必然改变
        svc.withdraw_sample("s2", reason="监测机构复核发现采样探头故障",
                            at=_iso(2026, 3, 4))
        svc.submit_sample(
            sample_id="s2-fix", line_id="L1", pollutant="dust",
            measured_at=_iso(2026, 3, 1, 8), value="5", unit="mg/m3",
            received_at=_iso(2026, 3, 4, 6),
        )

        old = svc.get_conclusion("C1")
        # 仅撤回样本、尚未签发更正版时，旧结论状态不变
        self.assertEqual(old.status, ConclusionStatus.ISSUED)
        # 旧结论的结果快照与证据集合一字未动
        self.assertEqual(old.calc_hash, snapshot_hash)
        self.assertEqual(old.result.pollutants[0].value, snapshot_value)
        self.assertEqual(old.evidence.sample_ids, snapshot_samples)

    def test_correction_chains_new_to_old_and_keeps_both(self):
        svc = build_service()
        c1 = svc.issue_conclusion(line_id="L1", period="day", anchor=DAY_ANCHOR,
                                  rule_id="R", issued_by="regulator-a",
                                  as_of=AS_OF_MARCH3, conclusion_id="C1")
        svc.withdraw_sample("s2", reason="探头故障", at=_iso(2026, 3, 4))
        svc.submit_sample(
            sample_id="s2-fix", line_id="L1", pollutant="dust",
            measured_at=_iso(2026, 3, 1, 8), value="5", unit="mg/m3",
            received_at=_iso(2026, 3, 4, 6),
        )
        c2 = svc.correct_conclusion(
            old_conclusion_id="C1", reason="样本 s2 撤回，以 s2-fix 替代",
            issued_by="regulator-b", as_of=_iso(2026, 3, 5),
            conclusion_id="C2",
        )
        self.assertEqual(c2.superseding_ids, ("C1",))
        self.assertEqual(c2.supersede_reason, "样本 s2 撤回，以 s2-fix 替代")
        self.assertEqual(c2.status, ConclusionStatus.ISSUED)
        self.assertNotEqual(c2.calc_hash, c1.calc_hash)

        chain = svc.lineage("C2")
        self.assertEqual([c.conclusion_id for c in chain], ["C2", "C1"])
        self.assertEqual([c.conclusion_id for c in svc.descendants("C1")],
                         ["C2"])
        # 旧结论已标记为被更正，但内容冻结
        self.assertEqual(svc.get_conclusion("C1").status,
                         ConclusionStatus.SUPERSEDED)

    def test_correction_rejected_when_nothing_changed(self):
        svc = build_service()
        svc.issue_conclusion(line_id="L1", period="day", anchor=DAY_ANCHOR,
                             rule_id="R", issued_by="a",
                             as_of=AS_OF_MARCH3, conclusion_id="C1")
        with self.assertRaises(ValidationError):
            svc.correct_conclusion(
                old_conclusion_id="C1", reason="无实质变化的重报",
                issued_by="a", as_of=AS_OF_MARCH3, conclusion_id="C2",
            )

    def test_draft_calculations_are_not_stored(self):
        svc = build_service()
        # recalculate 是纯试算，不产生结论
        svc.recalculate(line_id="L1", period="day", anchor=DAY_ANCHOR,
                        rule_id="R", as_of=AS_OF_MARCH3)
        self.assertEqual(svc.conclusions.list_for_line("L1"), [])

    def test_multi_hop_lineage(self):
        svc = build_service()
        svc.issue_conclusion(line_id="L1", period="day", anchor=DAY_ANCHOR,
                             rule_id="R", issued_by="a",
                             as_of=AS_OF_MARCH3, conclusion_id="C1")
        svc.withdraw_sample("s1", reason="r", at=_iso(2026, 3, 4))
        c2 = svc.correct_conclusion(old_conclusion_id="C1", reason="撤回s1",
                                    issued_by="a", as_of=_iso(2026, 3, 5),
                                    conclusion_id="C2")
        svc.withdraw_sample("s3", reason="r", at=_iso(2026, 3, 6))
        c3 = svc.correct_conclusion(old_conclusion_id="C2", reason="撤回s3",
                                    issued_by="a", as_of=_iso(2026, 3, 7),
                                    conclusion_id="C3")
        self.assertEqual([c.conclusion_id for c in svc.lineage("C3")],
                         ["C3", "C2", "C1"])
        self.assertEqual(c2.result.period_start, c3.result.period_start)


class SampleWithdrawalReviewTests(unittest.TestCase):
    def test_withdrawn_sample_excluded_but_visible_in_excluded_detail(self):
        svc = build_service()
        svc.withdraw_sample("s0", reason="采样时刻记录错误",
                            at=_iso(2026, 3, 2, 12))
        r = svc.recalculate(line_id="L1", period="day", anchor=DAY_ANCHOR,
                            rule_id="R", as_of=AS_OF_MARCH3)
        self.assertNotIn("s0", r.included_sample_ids)
        self.assertTrue(any(x.sample_id == "s0" and x.reason == "withdrawn"
                            for x in r.excluded))
        gap = next(g for g in r.gaps if g.code == "sample_withdrawn")
        self.assertEqual(gap.details["sample_id"], "s0")

    def test_withdrawal_after_as_of_does_not_change_historical_result(self):
        """以历史 as_of 重算时，之后才撤回的样本仍应被采纳（时间旅行语义）。"""
        svc = build_service()
        before = svc.recalculate(line_id="L1", period="day", anchor=DAY_ANCHOR,
                                 rule_id="R", as_of=AS_OF_MARCH3)
        svc.withdraw_sample("s0", reason="事后撤回", at=_iso(2026, 3, 10))
        replay = svc.recalculate(line_id="L1", period="day", anchor=DAY_ANCHOR,
                                 rule_id="R", as_of=AS_OF_MARCH3)
        self.assertEqual(before.calc_hash, replay.calc_hash)
        self.assertIn("s0", replay.included_sample_ids)


class RuleUpgradeReviewTests(unittest.TestCase):
    def test_upgrade_tightens_limit_and_recomputes_verdict(self):
        svc = build_service()
        c1 = svc.issue_conclusion(line_id="L1", period="day", anchor=DAY_ANCHOR,
                                  rule_id="R", rule_version="1.0",
                                  issued_by="a", as_of=AS_OF_MARCH3,
                                  conclusion_id="C1")
        self.assertEqual(c1.verdict, "compliant")  # 值 9 <= 限值 10

        # v2 规则 4 月生效，限值收紧到 8
        svc.add_rule(rule_id="R", version="2.0", effective_at=_iso(2026, 4, 1),
                     limits={"dust": Decimal("8")},
                     canonical_units={"dust": "mg/m3"},
                     min_samples=1, sampling_interval_hours="4")
        # 不指定版本时按 as_of 选最高生效版本
        trial = svc.recalculate(line_id="L1", period="day", anchor=DAY_ANCHOR,
                                rule_id="R", as_of=_iso(2026, 4, 2))
        self.assertEqual(trial.rule_version, "2.0")
        self.assertEqual(trial.verdict, "non_compliant")  # 9 > 8
        # 旧版规则复核结论不变
        replay_v1 = svc.recalculate(line_id="L1", period="day", anchor=DAY_ANCHOR,
                                    rule_id="R", rule_version="1.0",
                                    as_of=_iso(2026, 4, 2))
        self.assertEqual(replay_v1.calc_hash, c1.calc_hash)

        # 规则升级后的复核作为更正版签发，引用旧版
        c2 = svc.correct_conclusion(
            old_conclusion_id="C1", reason="核算规则升级至 2.0，限值收紧",
            issued_by="a", rule_version="2.0", as_of=_iso(2026, 4, 2),
            conclusion_id="C2",
        )
        self.assertEqual(c2.verdict, "non_compliant")
        self.assertEqual(c2.rule_version, "2.0")
        self.assertEqual(c2.superseding_ids, ("C1",))
        self.assertEqual(svc.get_conclusion("C1").verdict, "compliant")

    def test_explicit_old_version_still_resolvable(self):
        svc = build_service()
        svc.add_rule(rule_id="R", version="2.0", effective_at=_iso(2026, 4, 1),
                     limits={"dust": Decimal("8")},
                     canonical_units={"dust": "mg/m3"})
        r = svc.recalculate(line_id="L1", period="day", anchor=DAY_ANCHOR,
                            rule_id="R", rule_version="1.0",
                            as_of=_iso(2026, 4, 2))
        self.assertEqual(r.rule_version, "1.0")


class RecalculationPeriodTests(unittest.TestCase):
    def test_same_data_different_periods(self):
        svc = build_service()
        day = svc.recalculate(line_id="L1", period="day", anchor=DAY_ANCHOR,
                              rule_id="R", as_of=AS_OF_MARCH3)
        shift = svc.recalculate(line_id="L1", period="shift",
                                anchor=_iso(2026, 3, 1, 3), rule_id="R",
                                as_of=AS_OF_MARCH3)
        self.assertEqual(day.period, "day")
        self.assertEqual(len(day.segments), 6)
        self.assertEqual(len(shift.segments), 2)  # 夜班 00:00 与 04:00 两个样本
        self.assertNotEqual(day.calc_hash, shift.calc_hash)


if __name__ == "__main__":
    unittest.main()
