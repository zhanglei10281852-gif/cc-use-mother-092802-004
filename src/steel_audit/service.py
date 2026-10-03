"""应用服务：编排仓储、核算引擎与结论签发。"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from .conclusions import Conclusion, ConclusionRegistry
from .contracts import (
    AccountingRule,
    EmissionSample,
    EquipmentInterval,
    ProductionLine,
    ShutdownNote,
)
from .engine import CalcResult, calculate
from .errors import ValidationError
from .store import Repository
from .timeutils import as_utc


class AuditService:
    def __init__(self, repo: Optional[Repository] = None,
                 registry: Optional[ConclusionRegistry] = None) -> None:
        self.repo = repo or Repository()
        self.conclusions = registry or ConclusionRegistry()

    # ---- 基础数据登记 ----------------------------------------------------

    def register_line(self, **fields) -> ProductionLine:
        return self.repo.add_line(_coerce_line(fields))

    def add_equipment(self, **fields) -> EquipmentInterval:
        return self.repo.add_equipment(_coerce_equipment(fields))

    def add_shutdown(self, **fields) -> ShutdownNote:
        return self.repo.add_shutdown(_coerce_shutdown(fields))

    def add_rule(self, **fields) -> AccountingRule:
        return self.repo.add_rule(_coerce_rule(fields))

    def submit_sample(self, **fields) -> EmissionSample:
        return self.repo.submit_sample(_coerce_sample(fields))

    def withdraw_sample(self, sample_id: str, reason: str,
                        at=None) -> EmissionSample:
        return self.repo.withdraw_sample(sample_id, reason, _opt_dt(at))

    # ---- 核算 ------------------------------------------------------------

    def recalculate(self, *, line_id: str, period: str, anchor,
                    rule_id: str, as_of=None,
                    rule_version: Optional[str] = None) -> CalcResult:
        return calculate(
            self.repo,
            line_id=line_id,
            period=period,
            anchor=_dt(anchor),
            rule_id=rule_id,
            as_of=_opt_dt(as_of),
            rule_version=rule_version,
        )

    # ---- 结论签发 --------------------------------------------------------

    def issue_conclusion(self, *, line_id: str, period: str, anchor,
                         rule_id: str, issued_by: str,
                         as_of=None, rule_version: Optional[str] = None,
                         conclusion_id: Optional[str] = None,
                         issued_at=None) -> Conclusion:
        result = self.recalculate(
            line_id=line_id, period=period, anchor=anchor,
            rule_id=rule_id, as_of=as_of, rule_version=rule_version,
        )
        cid = conclusion_id or _default_conclusion_id(line_id, period, result)
        draft = self.conclusions.create_draft(cid, result)
        return self.conclusions.issue(cid, issued_by=issued_by,
                                      issued_at=_opt_dt(issued_at))

    def correct_conclusion(self, *, old_conclusion_id: str, reason: str,
                           issued_by: str, rule_id: Optional[str] = None,
                           rule_version: Optional[str] = None,
                           as_of=None, conclusion_id: Optional[str] = None,
                           issued_at=None) -> Conclusion:
        """更正旧结论：以新输入重算同产线同周期，签发新版并引用旧版。"""
        old = self.conclusions.get(old_conclusion_id)
        result = self.recalculate(
            line_id=old.line_id,
            period=old.period,
            anchor=old.period_start,
            rule_id=rule_id or old.rule_id,
            as_of=as_of or _dt(datetime.now(old.period_start.tzinfo)),
            rule_version=rule_version,
        )
        if result.calc_hash == old.calc_hash:
            raise ValidationError(
                "重算结果与旧结论完全一致（calc_hash 相同），无需签发更正版"
            )
        cid = conclusion_id or _default_conclusion_id(
            old.line_id, old.period, result, suffix=old_conclusion_id
        )
        return self.conclusions.supersede(
            old_conclusion_id, cid, result, reason=reason,
            issued_by=issued_by, issued_at=_opt_dt(issued_at),
        )

    def get_conclusion(self, conclusion_id: str) -> Conclusion:
        return self.conclusions.get(conclusion_id)

    def lineage(self, conclusion_id: str):
        return self.conclusions.lineage(conclusion_id)

    def descendants(self, conclusion_id: str):
        return self.conclusions.descendants(conclusion_id)


# ---------------------------------------------------------------------------
# 入参 coercion：API/测试可直接传 dict（时间为 ISO 字符串、数值为字符串）
# ---------------------------------------------------------------------------

def _dt(value) -> datetime:
    if value is None:
        raise ValidationError("缺少时间字段")
    if isinstance(value, datetime):
        return as_utc(value)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValidationError(f"时间必须带时区偏移: {value!r}")
    return as_utc(parsed)


def _opt_dt(value):
    return _dt(value) if value is not None else None


def _decimal(value, name: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        raise ValidationError(f"{name} 必须是数值，收到 {value!r}")


def _coerce_line(d: dict) -> ProductionLine:
    return ProductionLine(
        plant_id=d["plant_id"],
        line_id=d["line_id"],
        capacity_tonnes=_decimal(d["capacity_tonnes"], "capacity_tonnes"),
        commissioned_at=_dt(d["commissioned_at"]),
        supersedes_line_id=d.get("supersedes_line_id"),
    )


def _coerce_equipment(d: dict) -> EquipmentInterval:
    return EquipmentInterval(
        equipment_id=d["equipment_id"],
        line_id=d["line_id"],
        valid_from=_dt(d["valid_from"]),
        valid_to=_opt_dt(d.get("valid_to")),
        phase=d.get("phase", "baseline"),
        name=d.get("name", ""),
    )


def _coerce_shutdown(d: dict) -> ShutdownNote:
    return ShutdownNote(
        note_id=d["note_id"],
        line_id=d["line_id"],
        start_at=_dt(d["start_at"]),
        end_at=_dt(d["end_at"]),
        reason=d["reason"],
        equipment_id=d.get("equipment_id"),
    )


def _coerce_rule(d: dict) -> AccountingRule:
    limits = {k: _decimal(v, f"limits.{k}") for k, v in
              (d.get("limits") or {}).items()}
    conversions = {k: _decimal(v, f"conversions.{k}") for k, v in
                   (d.get("conversions") or {}).items()}
    return AccountingRule(
        rule_id=d["rule_id"],
        version=str(d["version"]),
        effective_at=_dt(d["effective_at"]),
        limits=limits,
        canonical_units=dict(d.get("canonical_units") or {}),
        conversions=conversions,
        min_samples=int(d.get("min_samples", 1)),
        sampling_interval_hours=_decimal(
            d.get("sampling_interval_hours", "1"), "sampling_interval_hours"
        ),
        method=d.get("method", "weighted_average"),
    )


def _coerce_sample(d: dict) -> EmissionSample:
    return EmissionSample(
        sample_id=d["sample_id"],
        line_id=d["line_id"],
        pollutant=d["pollutant"],
        measured_at=_dt(d["measured_at"]),
        value=_decimal(d["value"], "value"),
        unit=d["unit"],
        withdrawn=bool(d.get("withdrawn", False)),
        received_at=_opt_dt(d.get("received_at")),
        covers_from=_opt_dt(d.get("covers_from")),
        covers_to=_opt_dt(d.get("covers_to")),
        source=d.get("source"),
        withdrawn_at=_opt_dt(d.get("withdrawn_at")),
        withdraw_reason=d.get("withdraw_reason"),
        suspected_duplicate_of=d.get("suspected_duplicate_of"),
    )


def _default_conclusion_id(line_id: str, period: str, result: CalcResult,
                           suffix: str = "") -> str:
    stamp = f"{result.period_start:%Y%m%d%H%M}"
    base = f"C-{line_id}-{period}-{stamp}-{result.calc_hash[:8]}"
    if suffix:
        base += f"-corr-{suffix}"
    return base
