"""核算引擎：按统计周期聚合样本、扣减停机、识别证据缺口。

确定性约定（同一输入必然得到同一输出）：
- 样本按 (interval_start, received_at, sample_id) 排序处理；
- 跨班次/跨周期样本按时间重叠比例分摊：allocated = value × 重叠秒数 / 采样总秒数；
- 仅纳入 status='active' 且 received_at <= data_cutoff 的样本；
- 数值一律使用 Decimal，输出统一量化为 6 位小数（ROUND_HALF_UP）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple

from . import models
from .errors import NotFoundError, ValidationError
from .rules import DENOM_OUTPUT, METHOD_FACTOR, RuleParams, parse_params
from .storage import Store
from .timeutil import (
    Interval,
    clip,
    duration_seconds,
    fmt_dt,
    overlap_seconds,
    period_end,
    subtract_intervals,
    union_intervals,
    validate_period,
)

Q6 = Decimal("0.000001")


def q6(value: Decimal) -> str:
    """统一量化 6 位小数字符串输出。"""
    return str(value.quantize(Q6, rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class ComputeRequest:
    line_id: str
    period_type: str
    period_start: datetime
    rule_version_id: Optional[str]  # None 表示按周期起点解析生效规则
    data_cutoff: datetime


def _sort_key(sample: models.Sample):
    return (sample.interval_start, sample.received_at, sample.sample_id)


def _window_json(w: Interval) -> dict:
    return {"start": fmt_dt(w[0]), "end": fmt_dt(w[1])}


def compute_conclusion(store: Store, req: ComputeRequest) -> dict:
    """计算一份结论内容（totals/detail/gaps），不写库。调用方负责持久化。"""
    line = store.get_line(req.line_id)
    if line is None:
        raise NotFoundError(f"产线不存在: {req.line_id}")
    try:
        validate_period(req.period_type, req.period_start)
    except ValueError as exc:
        raise ValidationError(str(exc))
    p_start = req.period_start
    p_end = period_end(req.period_type, p_start)
    period: Interval = (p_start, p_end)
    cutoff = req.data_cutoff

    if req.rule_version_id:
        rule = store.get_rule_version(req.rule_version_id)
        if rule is None:
            raise NotFoundError(f"核算规则版本不存在: {req.rule_version_id}")
    else:
        rule = store.find_effective_rule(p_start)
        if rule is None:
            raise ValidationError(
                f"周期起点 {fmt_dt(p_start)} 无已生效的核算规则版本，无法核算")
    params = parse_params(rule.params)

    equipment = store.list_equipment_intervals(req.line_id, p_start, p_end)
    shutdowns = store.list_shutdowns(req.line_id, p_start, p_end)
    samples = store.list_samples_overlapping(req.line_id, p_start, p_end)

    usable = sorted(
        (s for s in samples
         if s.status == models.SAMPLE_ACTIVE and s.received_at <= cutoff),
        key=_sort_key)
    late_pending = sorted(
        (s for s in samples
         if s.status == models.SAMPLE_ACTIVE and s.received_at > cutoff),
        key=_sort_key)
    withdrawn = sorted((s for s in samples if s.status == models.SAMPLE_WITHDRAWN),
                       key=_sort_key)
    duplicates = sorted((s for s in samples if s.status == models.SAMPLE_DUPLICATE),
                        key=_sort_key)

    # ---- 按设备投运边界把周期切成段：改造前后分段核算，效果可归因 ----
    bounds = {p_start, p_end}
    for e in equipment:
        bounds.add(max(e.commissioned_from, p_start))
        bounds.add(min(e.decommissioned_to, p_end) if e.decommissioned_to else p_end)
    points = sorted(bounds)
    segments: List[Tuple[datetime, datetime, Optional[models.EquipmentInterval]]] = []
    for a, b in zip(points, points[1:]):
        eq = next(
            (e for e in equipment
             if e.commissioned_from < b
             and (e.decommissioned_to is None or e.decommissioned_to > a)),
            None,
        )
        segments.append((a, b, eq))

    # 需要分摊的指标：规则要求覆盖的 + 核算方法用到的
    needed = set(params.indicators) | {params.emission_indicator}
    if params.method == METHOD_FACTOR or params.intensity_denominator == DENOM_OUTPUT:
        needed.add(params.output_indicator)
    needed_indicators = sorted(needed)

    seg_results = []
    for a, b, eq in segments:
        seg_window: Interval = (a, b)
        shutdown_windows = [w for w in (clip(sh.window, seg_window) for sh in shutdowns) if w]
        operating_windows = subtract_intervals([seg_window], shutdown_windows)
        op_seconds = sum(duration_seconds(w) for w in operating_windows)
        seg_seconds = duration_seconds(seg_window)

        per_indicator: Dict[str, dict] = {}
        for ind in needed_indicators:
            contrib = [s for s in usable
                       if s.indicator == ind and overlap_seconds(s.interval, seg_window) > 0]
            allocated = Decimal(0)
            for s in contrib:
                allocated += (
                    s.value
                    * Decimal(overlap_seconds(s.interval, seg_window))
                    / Decimal(s.duration_seconds)
                )
            sample_windows = [w for w in (clip(s.interval, seg_window) for s in contrib) if w]
            covered_pieces = [
                c for sw in sample_windows for ow in operating_windows
                for c in [clip(sw, ow)] if c
            ]
            cov_seconds = sum(duration_seconds(w) for w in union_intervals(covered_pieces))
            uncovered = subtract_intervals(operating_windows, sample_windows)
            ratio = (Decimal(cov_seconds) / Decimal(op_seconds)) if op_seconds > 0 else None
            per_indicator[ind] = {
                "allocated": allocated,
                "coverage_ratio": ratio,
                "coverage_seconds": cov_seconds,
                "sample_ids": [s.sample_id for s in contrib],
                "uncovered": uncovered,
            }

        seg_results.append({
            "start": a, "end": b, "equipment": eq,
            "operating_seconds": op_seconds,
            "shutdown_seconds": seg_seconds - op_seconds,
            "indicators": per_indicator,
        })

    # ---- 汇总 ----
    op_total = sum(s["operating_seconds"] for s in seg_results)
    shutdown_total = sum(s["shutdown_seconds"] for s in seg_results)

    def total_allocated(indicator: str) -> Decimal:
        return sum((s["indicators"][indicator]["allocated"] for s in seg_results), Decimal(0))

    total_output: Optional[Decimal] = None
    if params.method == METHOD_FACTOR:
        total_output = total_allocated(params.output_indicator)
        total_emission = total_output * params.emission_factor
    else:
        total_emission = total_allocated(params.emission_indicator)
        if params.output_indicator in needed_indicators:
            total_output = total_allocated(params.output_indicator)

    operating_hours = Decimal(op_total) / Decimal(3600)
    if params.intensity_denominator == DENOM_OUTPUT:
        denominator = total_output
    else:
        denominator = operating_hours
    intensity = (total_emission / denominator) if denominator and denominator != 0 else None

    coverage_indicator = (params.output_indicator if params.method == METHOD_FACTOR
                          else params.emission_indicator)
    cov_total = sum(s["indicators"][coverage_indicator]["coverage_seconds"] for s in seg_results)
    coverage_ratio = (Decimal(cov_total) / Decimal(op_total)) if op_total > 0 else None

    exceedance = None
    if params.intensity_limit is not None and intensity is not None:
        exceedance = intensity > params.intensity_limit

    totals = {
        "method": params.method,
        "total_emission": q6(total_emission),
        "total_output": q6(total_output) if total_output is not None else None,
        "operating_hours": q6(operating_hours),
        "shutdown_hours": q6(Decimal(shutdown_total) / Decimal(3600)),
        "intensity": q6(intensity) if intensity is not None else None,
        "intensity_denominator": params.intensity_denominator,
        "coverage_ratio": q6(coverage_ratio) if coverage_ratio is not None else None,
        "exceedance": exceedance,
    }

    # ---- 证据缺口 ----
    gaps: List[dict] = []
    for seg in seg_results:
        if seg["equipment"] is None:
            gaps.append({
                "type": "no_equipment_interval",
                "segment_start": fmt_dt(seg["start"]),
                "segment_end": fmt_dt(seg["end"]),
                "message": "该时段无设备投运记录，无法归属工艺状态",
            })
            continue
        for ind in sorted(params.indicators):
            data = seg["indicators"][ind]
            if seg["operating_seconds"] <= 0:
                continue
            ratio = data["coverage_ratio"]
            if ratio is None or ratio < params.min_coverage_ratio:
                gaps.append({
                    "type": "missing_samples",
                    "indicator": ind,
                    "equipment_code": seg["equipment"].equipment_code,
                    "segment_start": fmt_dt(seg["start"]),
                    "segment_end": fmt_dt(seg["end"]),
                    "coverage_ratio": q6(ratio) if ratio is not None else None,
                    "required_ratio": str(params.min_coverage_ratio),
                    "uncovered_windows": [_window_json(w) for w in data["uncovered"]],
                    "message": f"指标 {ind} 监测覆盖不足，存在未覆盖运行时段",
                })
    for sh in shutdowns:
        if not sh.reason:
            gaps.append({
                "type": "unexplained_shutdown",
                "shutdown_id": sh.shutdown_id,
                "start": fmt_dt(sh.start),
                "end": fmt_dt(sh.end),
                "message": "异常停机缺少说明",
            })
    during_shutdown = [
        s for s in usable
        if any(clip(s.interval, sh.window) for sh in shutdowns)
    ]
    if during_shutdown:
        gaps.append({
            "type": "samples_during_shutdown",
            "count": len(during_shutdown),
            "sample_ids": [s.sample_id for s in during_shutdown],
            "message": "停机时段内仍存在监测样本，需核实停机记录或样本归属",
        })
    if late_pending:
        gaps.append({
            "type": "late_samples_pending",
            "count": len(late_pending),
            "sample_ids": [s.sample_id for s in late_pending],
            "message": "数据截止后送达的样本未纳入本次核算，可通过重算并签发更正结论纳入",
        })
    if withdrawn:
        gaps.append({
            "type": "withdrawn_samples",
            "count": len(withdrawn),
            "sample_ids": [s.sample_id for s in withdrawn],
            "message": "该周期存在已撤回样本，已从核算中排除",
        })
    if duplicates:
        gaps.append({
            "type": "duplicate_samples_excluded",
            "count": len(duplicates),
            "items": [{"sample_id": s.sample_id, "duplicate_of": s.duplicate_of}
                      for s in duplicates],
            "message": "自然键重复的样本已按先到先采信规则排除",
        })
    outside_equipment = [
        s for s in usable
        if not any(e.commissioned_from < s.interval_end
                   and (e.decommissioned_to is None or e.decommissioned_to > s.interval_start)
                   for e in equipment)
    ]
    if outside_equipment:
        gaps.append({
            "type": "samples_outside_equipment_interval",
            "count": len(outside_equipment),
            "sample_ids": [s.sample_id for s in outside_equipment],
            "message": "样本落在任何设备投运区间之外，无法归属工艺状态",
        })

    # ---- 计算明细（供监管查询与版本间差异比对） ----
    late_after = p_end + timedelta(hours=params.late_grace_hours)
    detail = {
        "rule": {
            "rule_version_id": rule.rule_version_id,
            "code": rule.code,
            "effective_from": fmt_dt(rule.effective_from),
            "params": params.to_dict(),
        },
        "period": {"type": req.period_type, "start": fmt_dt(p_start), "end": fmt_dt(p_end)},
        "data_cutoff": fmt_dt(cutoff),
        "segments": [
            {
                "start": fmt_dt(seg["start"]),
                "end": fmt_dt(seg["end"]),
                "equipment": (
                    {
                        "equipment_interval_id": seg["equipment"].equipment_interval_id,
                        "equipment_code": seg["equipment"].equipment_code,
                        "technology": seg["equipment"].technology,
                    }
                    if seg["equipment"] else None
                ),
                "operating_seconds": seg["operating_seconds"],
                "shutdown_seconds": seg["shutdown_seconds"],
                "indicators": {
                    ind: {
                        "allocated": q6(data["allocated"]),
                        "coverage_ratio": (q6(data["coverage_ratio"])
                                           if data["coverage_ratio"] is not None else None),
                        "sample_ids": data["sample_ids"],
                    }
                    for ind, data in sorted(seg["indicators"].items())
                },
            }
            for seg in seg_results
        ],
        "samples": [
            {
                "sample_id": s.sample_id,
                "agency_uid": s.agency_uid,
                "indicator": s.indicator,
                "value": str(s.value),
                "unit": s.unit,
                "interval_start": fmt_dt(s.interval_start),
                "interval_end": fmt_dt(s.interval_end),
                "received_at": fmt_dt(s.received_at),
                "late": s.received_at > late_after,
                "overlap_seconds": overlap_seconds(s.interval, period),
                "allocated_value": q6(
                    s.value * Decimal(overlap_seconds(s.interval, period))
                    / Decimal(s.duration_seconds)
                ),
            }
            for s in usable
        ],
        "excluded": {
            "duplicates": [
                {"sample_id": s.sample_id, "duplicate_of": s.duplicate_of} for s in duplicates
            ],
            "withdrawn": [s.sample_id for s in withdrawn],
            "late_pending": [s.sample_id for s in late_pending],
        },
    }

    return {
        "line_id": req.line_id,
        "period_type": req.period_type,
        "period_start": p_start,
        "period_end": p_end,
        "rule_version_id": rule.rule_version_id,
        "data_cutoff": cutoff,
        "totals": totals,
        "detail": detail,
        "gaps": gaps,
    }
