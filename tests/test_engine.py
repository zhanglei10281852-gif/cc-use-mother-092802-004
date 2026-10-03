"""核算引擎测试：周期切分、迟到/重复/跨班次样本、产线切换、停机。"""

import sys
import unittest
from datetime import timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from steel_audit.contracts import (
    AccountingRule,
    EmissionSample,
    EquipmentInterval,
    ProductionLine,
    ShutdownNote,
)
from steel_audit.engine import calculate
from steel_audit.errors import ConflictError
from steel_audit.store import Repository
from steel_audit.timeutils import period_bounds

UTC = timezone.utc


def make_repo() -> Repository:
    repo = Repository()
    repo.add_line(ProductionLine(
        plant_id="P1", line_id="L1", capacity_tonnes=Decimal("100"),
        commissioned_at=__import__("datetime").datetime(2026, 1, 1, tzinfo=UTC),
    ))
    repo.add_rule(AccountingRule(
        rule_id="R", version="1.0",
        effective_at=__import__("datetime").datetime(2026, 1, 1, tzinfo=UTC),
        limits={"dust": Decimal("10")},
        canonical_units={"dust": "mg/m3"},
        conversions={"mg/Nm3": Decimal("1")},
        min_samples=1, sampling_interval_hours=Decimal("1"),
    ))
    return repo


def instant(sample_id, pollutant, hour, value, received_hour=None,
            line_id="L1", unit="mg/m3", day="2026-03-01"):
    return EmissionSample(
        sample_id=sample_id, line_id=line_id, pollutant=pollutant,
        measured_at=__import__("datetime").datetime.fromisoformat(
            f"{day}T{hour:02d}:00:00+00:00"),
        value=Decimal(str(value)), unit=unit,
        received_at=__import__("datetime").datetime.fromisoformat(
            f"{day}T{(received_hour if received_hour is not None else hour + 1):02d}:00:00+00:00"),
    )


class PeriodTests(unittest.TestCase):
    def test_shift_bounds_are_8h_aligned(self):
        from datetime import datetime
        s, e = period_bounds(datetime(2026, 3, 1, 9, 30, tzinfo=UTC), "shift")
        self.assertEqual(s, datetime(2026, 3, 1, 8, tzinfo=UTC))
        self.assertEqual(e, datetime(2026, 3, 1, 16, tzinfo=UTC))

    def test_unknown_period_rejected(self):
        with self.assertRaises(ValueError):
            period_bounds(__import__("datetime").datetime(2026, 3, 1, tzinfo=UTC),
                          "quarter")


class EngineTests(unittest.TestCase):
    def _equip_full_day(self, repo, phase="baseline"):
        import datetime as dt
        repo.add_equipment(EquipmentInterval(
            equipment_id="E1", line_id="L1",
            valid_from=dt.datetime(2026, 2, 1, tzinfo=UTC),
            valid_to=None, phase=phase,
        ))

    def test_weighted_average_basic(self):
        repo = make_repo()
        self._equip_full_day(repo)
        repo.submit_sample(instant("s1", "dust", 2, 8))
        repo.submit_sample(instant("s2", "dust", 4, 6))
        r = calculate(repo, line_id="L1", period="day",
                      anchor="2026-03-01T12:00:00+00:00", rule_id="R",
                      as_of="2026-03-02T00:00:00+00:00")
        dust = next(p for p in r.pollutants if p.pollutant == "dust")
        self.assertEqual(dust.value, Decimal("7.000000"))
        self.assertIs(dust.compliant, True)
        self.assertEqual(r.verdict, "compliant")

    def test_non_compliant_verdict(self):
        repo = make_repo()
        self._equip_full_day(repo)
        repo.submit_sample(instant("s1", "dust", 2, 12))
        r = calculate(repo, line_id="L1", period="day",
                      anchor="2026-03-01T12:00:00+00:00", rule_id="R",
                      as_of="2026-03-02T00:00:00+00:00")
        self.assertEqual(r.verdict, "non_compliant")

    def test_regulated_pollutant_without_any_sample_is_blocker(self):
        repo = make_repo()
        self._equip_full_day(repo)
        # 规则限值含 so2，但整个周期没有任何 so2 样本
        repo.add_rule(AccountingRule(
            rule_id="R", version="1.1",
            effective_at=__import__("datetime").datetime(2026, 1, 1, tzinfo=UTC),
            limits={"dust": Decimal("10"), "so2": Decimal("50")},
            canonical_units={"dust": "mg/m3", "so2": "mg/m3"},
            min_samples=1, sampling_interval_hours=Decimal("1"),
        ))
        repo.submit_sample(instant("s1", "dust", 2, 8))
        r = calculate(repo, line_id="L1", period="day",
                      anchor="2026-03-01T12:00:00+00:00", rule_id="R",
                      rule_version="1.1",
                      as_of="2026-03-02T00:00:00+00:00")
        self.assertEqual(r.verdict, "inconclusive")
        gap = next(g for g in r.gaps
                   if g.code == "insufficient_samples"
                   and g.details["pollutant"] == "so2")
        self.assertEqual(gap.severity, "blocker")

    def test_late_sample_excluded_and_gap_flagged(self):
        import datetime as dt
        repo = make_repo()
        self._equip_full_day(repo)
        repo.submit_sample(instant("s1", "dust", 2, 8, received_hour=3))
        # s2 测量时刻在周期内，但 as_of 时尚未到齐
        repo.submit_sample(EmissionSample(
            sample_id="s2", line_id="L1", pollutant="dust",
            measured_at=dt.datetime(2026, 3, 1, 6, tzinfo=UTC),
            value=Decimal("2"), unit="mg/m3",
            received_at=dt.datetime(2026, 3, 5, tzinfo=UTC),
        ))
        r = calculate(repo, line_id="L1", period="day",
                      anchor="2026-03-01T12:00:00+00:00", rule_id="R",
                      as_of="2026-03-02T00:00:00+00:00")
        self.assertNotIn("s2", r.included_sample_ids)
        self.assertTrue(any(g.code == "late_data_pending" and
                            g.details["sample_id"] == "s2" for g in r.gaps))
        # 数据补齐后重算：s2 被采纳，缺口消失，结果改变
        r2 = calculate(repo, line_id="L1", period="day",
                       anchor="2026-03-01T12:00:00+00:00", rule_id="R",
                       as_of="2026-03-06T00:00:00+00:00")
        self.assertIn("s2", r2.included_sample_ids)
        self.assertFalse(any(g.code == "late_data_pending" for g in r2.gaps))
        self.assertNotEqual(r.calc_hash, r2.calc_hash)

    def test_duplicate_sample_ids_are_idempotent_but_conflicts_rejected(self):
        repo = make_repo()
        self._equip_full_day(repo)
        first = repo.submit_sample(instant("s1", "dust", 2, 8))
        again = repo.submit_sample(instant("s1", "dust", 2, 8))
        self.assertIs(first, again)
        with self.assertRaises(ConflictError):
            repo.submit_sample(instant("s1", "dust", 2, 9))

    def test_same_fingerprint_different_id_keeps_earliest_only(self):
        import datetime as dt
        repo = make_repo()
        self._equip_full_day(repo)
        a = instant("s-a", "dust", 2, 8)
        b = EmissionSample(
            sample_id="s-b", line_id="L1", pollutant="dust",
            measured_at=dt.datetime(2026, 3, 1, 2, tzinfo=UTC),
            value=Decimal("8"), unit="mg/m3",
            received_at=dt.datetime(2026, 3, 1, 10, tzinfo=UTC),
        )
        repo.submit_sample(a)
        stored_b = repo.submit_sample(b)
        self.assertEqual(stored_b.suspected_duplicate_of, "s-a")
        r = calculate(repo, line_id="L1", period="day",
                      anchor="2026-03-01T12:00:00+00:00", rule_id="R",
                      as_of="2026-03-02T00:00:00+00:00")
        self.assertIn("s-a", r.included_sample_ids)
        self.assertNotIn("s-b", r.included_sample_ids)
        self.assertTrue(any(g.code == "duplicate_samples_deduped" for g in r.gaps))
        self.assertTrue(any(x.reason == "duplicate" and x.sample_id == "s-b"
                            for x in r.excluded))

    def test_cross_shift_window_split_by_overlap_seconds(self):
        import datetime as dt
        repo = make_repo()
        self._equip_full_day(repo)
        repo.submit_sample(EmissionSample(
            sample_id="w1", line_id="L1", pollutant="dust",
            measured_at=dt.datetime(2026, 3, 1, 7, tzinfo=UTC),
            value=Decimal("8"), unit="mg/m3",
            received_at=dt.datetime(2026, 3, 1, 10, tzinfo=UTC),
            covers_from=dt.datetime(2026, 3, 1, 7, tzinfo=UTC),
            covers_to=dt.datetime(2026, 3, 1, 9, tzinfo=UTC),
        ))
        # 夜班桶 00:00-08:00：窗口只切出 07:00-08:00 一小时
        night = calculate(repo, line_id="L1", period="shift",
                          anchor="2026-03-01T03:00:00+00:00", rule_id="R",
                          as_of="2026-03-02T00:00:00+00:00")
        self.assertEqual(len(night.segments), 1)
        self.assertEqual(night.segments[0].weight_seconds, 3600.0)
        # 白班桶 08:00-16:00：窗口切出 08:00-09:00
        morning = calculate(repo, line_id="L1", period="shift",
                            anchor="2026-03-01T10:00:00+00:00", rule_id="R",
                            as_of="2026-03-02T00:00:00+00:00")
        self.assertEqual(morning.segments[0].weight_seconds, 3600.0)
        # 同一窗口在两个桶的加权均值应一致（值恒定）
        nv = next(p for p in night.pollutants if p.pollutant == "dust").value
        mv = next(p for p in morning.pollutants if p.pollutant == "dust").value
        self.assertEqual(nv, mv)

    def test_shutdown_excludes_samples_and_seconds(self):
        import datetime as dt
        repo = make_repo()
        self._equip_full_day(repo)
        repo.add_shutdown(ShutdownNote(
            note_id="d1", line_id="L1",
            start_at=dt.datetime(2026, 3, 1, 1, tzinfo=UTC),
            end_at=dt.datetime(2026, 3, 1, 5, tzinfo=UTC),
            reason="高炉休风",
        ))
        repo.submit_sample(instant("s1", "dust", 3, 20))   # 停机中
        repo.submit_sample(instant("s2", "dust", 6, 8))    # 运行中
        r = calculate(repo, line_id="L1", period="day",
                      anchor="2026-03-01T12:00:00+00:00", rule_id="R",
                      as_of="2026-03-02T00:00:00+00:00")
        self.assertNotIn("s1", r.included_sample_ids)
        self.assertIn("s2", r.included_sample_ids)
        self.assertEqual(r.shutdown_seconds, 4 * 3600)
        self.assertEqual(r.operating_seconds, 20 * 3600)
        self.assertTrue(any(x.reason == "during_shutdown" for x in r.excluded))


class LineSwitchTests(unittest.TestCase):
    """产线/设备改造切换：新旧设备区间首尾相接，跨切换点的样本分段归因。"""

    def test_equipment_overlap_rejected(self):
        import datetime as dt
        repo = make_repo()
        repo.add_equipment(EquipmentInterval(
            equipment_id="OLD", line_id="L1",
            valid_from=dt.datetime(2026, 2, 1, tzinfo=UTC),
            valid_to=dt.datetime(2026, 3, 1, 12, tzinfo=UTC),
            phase="baseline"))
        with self.assertRaises(ConflictError):
            repo.add_equipment(EquipmentInterval(
                equipment_id="NEW", line_id="L1",
                valid_from=dt.datetime(2026, 3, 1, 11, tzinfo=UTC),  # 重叠1h
                phase="retrofit"))

    def test_window_across_switch_split_by_phase(self):
        import datetime as dt
        repo = make_repo()
        repo.add_equipment(EquipmentInterval(
            equipment_id="OLD", line_id="L1",
            valid_from=dt.datetime(2026, 2, 1, tzinfo=UTC),
            valid_to=dt.datetime(2026, 3, 1, 12, tzinfo=UTC),
            phase="baseline"))
        repo.add_equipment(EquipmentInterval(
            equipment_id="NEW", line_id="L1",
            valid_from=dt.datetime(2026, 3, 1, 12, tzinfo=UTC),
            phase="retrofit"))
        # 窗口 10:00-14:00 跨越 12:00 切换点：旧段值高，新段值低
        repo.submit_sample(EmissionSample(
            sample_id="w-old", line_id="L1", pollutant="dust",
            measured_at=dt.datetime(2026, 3, 1, 10, tzinfo=UTC),
            value=Decimal("16"), unit="mg/m3",
            received_at=dt.datetime(2026, 3, 1, 15, tzinfo=UTC),
            covers_from=dt.datetime(2026, 3, 1, 10, tzinfo=UTC),
            covers_to=dt.datetime(2026, 3, 1, 12, tzinfo=UTC),
        ))
        repo.submit_sample(EmissionSample(
            sample_id="w-new", line_id="L1", pollutant="dust",
            measured_at=dt.datetime(2026, 3, 1, 12, tzinfo=UTC),
            value=Decimal("4"), unit="mg/m3",
            received_at=dt.datetime(2026, 3, 1, 15, tzinfo=UTC),
            covers_from=dt.datetime(2026, 3, 1, 12, tzinfo=UTC),
            covers_to=dt.datetime(2026, 3, 1, 14, tzinfo=UTC),
        ))
        r = calculate(repo, line_id="L1", period="day",
                      anchor="2026-03-01T12:00:00+00:00", rule_id="R",
                      as_of="2026-03-02T00:00:00+00:00")
        phases = {(s.phase, s.equipment_id) for s in r.segments}
        self.assertEqual(phases, {("baseline", "OLD"), ("retrofit", "NEW")})
        self.assertTrue(any(g.code == "phase_switch_in_period" for g in r.gaps))
        dust = next(p for p in r.pollutants if p.pollutant == "dust")
        self.assertEqual(dust.value, Decimal("10.000000"))  # (16*2+4*2)/4

    def test_equipment_gap_blocks_verdict(self):
        import datetime as dt
        repo = make_repo()
        repo.add_equipment(EquipmentInterval(
            equipment_id="E1", line_id="L1",
            valid_from=dt.datetime(2026, 3, 1, 12, tzinfo=UTC),  # 半天无设备
            phase="retrofit"))
        repo.submit_sample(instant("s1", "dust", 18, 4))
        r = calculate(repo, line_id="L1", period="day",
                      anchor="2026-03-01T12:00:00+00:00", rule_id="R",
                      as_of="2026-03-02T00:00:00+00:00")
        self.assertEqual(r.verdict, "inconclusive")
        self.assertTrue(any(g.code == "equipment_coverage_gap" and
                            g.severity == "blocker" for g in r.gaps))


class DeterminismTests(unittest.TestCase):
    def test_same_inputs_same_hash_independent_of_insertion_order(self):
        repo1 = make_repo()
        EngineTests()._equip_full_day(repo1)
        repo1.submit_sample(instant("s1", "dust", 2, 8))
        repo1.submit_sample(instant("s2", "dust", 4, 6))
        repo2 = make_repo()
        EngineTests()._equip_full_day(repo2)
        repo2.submit_sample(instant("s2", "dust", 4, 6))
        repo2.submit_sample(instant("s1", "dust", 2, 8))
        kw = dict(line_id="L1", period="day",
                  anchor="2026-03-01T12:00:00+00:00", rule_id="R",
                  as_of="2026-03-02T00:00:00+00:00")
        self.assertEqual(calculate(repo1, **kw).calc_hash,
                         calculate(repo2, **kw).calc_hash)


if __name__ == "__main__":
    unittest.main()
