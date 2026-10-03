"""领域对象到 JSON 可序列化结构（API 与持久化共用）。"""

from enum import Enum
from typing import Optional

from .conclusions import Conclusion
from .engine import CalcResult


def _dt(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _enum(value) -> Optional[str]:
    return value.value if isinstance(value, Enum) else value


def _decimal(value):
    return str(value) if value is not None else None


def line_to_dict(line) -> dict:
    return {
        "plant_id": line.plant_id,
        "line_id": line.line_id,
        "capacity_tonnes": str(line.capacity_tonnes),
        "commissioned_at": _dt(line.commissioned_at),
        "supersedes_line_id": line.supersedes_line_id,
    }


def equipment_to_dict(e) -> dict:
    return {
        "equipment_id": e.equipment_id,
        "line_id": e.line_id,
        "valid_from": _dt(e.valid_from),
        "valid_to": _dt(e.valid_to),
        "phase": e.phase,
        "name": e.name,
    }


def shutdown_to_dict(n) -> dict:
    return {
        "note_id": n.note_id,
        "line_id": n.line_id,
        "start_at": _dt(n.start_at),
        "end_at": _dt(n.end_at),
        "reason": n.reason,
        "equipment_id": n.equipment_id,
    }


def rule_to_dict(r) -> dict:
    return {
        "rule_id": r.rule_id,
        "version": r.version,
        "effective_at": _dt(r.effective_at),
        "limits": {k: str(v) for k, v in r.limits.items()},
        "canonical_units": dict(r.canonical_units),
        "conversions": {k: str(v) for k, v in r.conversions.items()},
        "min_samples": r.min_samples,
        "sampling_interval_hours": str(r.sampling_interval_hours),
        "method": r.method,
    }


def sample_to_dict(s) -> dict:
    return {
        "sample_id": s.sample_id,
        "line_id": s.line_id,
        "pollutant": s.pollutant,
        "measured_at": _dt(s.measured_at),
        "value": str(s.value),
        "unit": s.unit,
        "withdrawn": s.withdrawn,
        "received_at": _dt(s.received_at),
        "covers_from": _dt(s.covers_from),
        "covers_to": _dt(s.covers_to),
        "source": s.source,
        "withdrawn_at": _dt(s.withdrawn_at),
        "withdraw_reason": s.withdraw_reason,
        "suspected_duplicate_of": s.suspected_duplicate_of,
    }


def gap_to_dict(g) -> dict:
    return {
        "code": g.code,
        "severity": g.severity,
        "message": g.message,
        "details": g.details,
    }


def segment_to_dict(seg) -> dict:
    return {
        "sample_id": seg.sample_id,
        "pollutant": seg.pollutant,
        "start_at": _dt(seg.start_at),
        "end_at": _dt(seg.end_at),
        "active_seconds": seg.active_seconds,
        "equipment_id": seg.equipment_id,
        "phase": seg.phase,
        "raw_value": str(seg.raw_value),
        "canonical_value": str(seg.canonical_value),
        "weight_seconds": seg.weight_seconds,
    }


def excluded_to_dict(x) -> dict:
    return {"sample_id": x.sample_id, "reason": x.reason, "detail": x.detail}


def pollutant_to_dict(p) -> dict:
    return {
        "pollutant": p.pollutant,
        "value": _decimal(p.value),
        "unit": p.unit,
        "limit": _decimal(p.limit),
        "compliant": p.compliant,
        "included_samples": list(p.included_samples),
        "weight_seconds": p.weight_seconds,
        "expected_samples": p.expected_samples,
        "observed_samples": p.observed_samples,
    }


def result_to_dict(r: CalcResult) -> dict:
    return {
        "line_id": r.line_id,
        "plant_id": r.plant_id,
        "period": r.period,
        "period_start": _dt(r.period_start),
        "period_end": _dt(r.period_end),
        "as_of": _dt(r.as_of),
        "rule_id": r.rule_id,
        "rule_version": r.rule_version,
        "rule_effective_at": _dt(r.rule_effective_at),
        "operating_seconds": r.operating_seconds,
        "shutdown_seconds": r.shutdown_seconds,
        "equipment_gap_seconds": r.equipment_gap_seconds,
        "verdict": r.verdict,
        "calc_hash": r.calc_hash,
        "pollutants": [pollutant_to_dict(p) for p in r.pollutants],
        "segments": [segment_to_dict(s) for s in r.segments],
        "excluded": [excluded_to_dict(x) for x in r.excluded],
        "gaps": [gap_to_dict(g) for g in r.gaps],
        "included_sample_ids": list(r.included_sample_ids),
    }


def conclusion_to_dict(c: Conclusion, include_result: bool = True) -> dict:
    data = {
        "conclusion_id": c.conclusion_id,
        "line_id": c.line_id,
        "period": c.period,
        "period_start": _dt(c.period_start),
        "period_end": _dt(c.period_end),
        "as_of": _dt(c.as_of),
        "status": _enum(c.status),
        "verdict": c.verdict,
        "calc_hash": c.calc_hash,
        "rule_id": c.rule_id,
        "rule_version": c.rule_version,
        "issued_at": _dt(c.issued_at),
        "issued_by": c.issued_by,
        "superseding_ids": list(c.superseding_ids),
        "supersede_reason": c.supersede_reason,
        "evidence": {
            "sample_ids": list(c.evidence.sample_ids),
            "equipment_ids": list(c.evidence.equipment_ids),
            "shutdown_ids": list(c.evidence.shutdown_ids),
            "rule_id": c.evidence.rule_id,
            "rule_version": c.evidence.rule_version,
        },
    }
    if include_result:
        data["result"] = result_to_dict(c.result)
    return data
