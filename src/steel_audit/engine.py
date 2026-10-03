"""核算引擎：在给定 ``as_of`` 时刻对某产线某周期做确定性重算。

关键语义
--------
- ``as_of`` 截止：只采纳 ``received_at <= as_of`` 且当时未撤回的样本。
  同一条结论在"数据到齐前"和"补齐后"分别计算，差异会以
  ``late_data_pending`` / 新结论的方式显式呈现，而不是被最新总量掩盖。
- 半开区间：周期、设备区间、监测窗口一律 ``[start, end)``。
- 跨周期/跨班次窗口按与桶的重叠**秒数**加权切分；停机与设备未覆盖的秒数先扣除。
- 重复样本：在 as_of 可见集合内按内容指纹分组，只保留最早到的一份。
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Dict, List, Optional, Tuple

from .contracts import AccountingRule, EmissionSample, EquipmentInterval
from .store import Repository
from .timeutils import as_utc, overlap_seconds, period_bounds, subtract_intervals

_FAR_FUTURE = datetime(9999, 12, 31, tzinfo=timezone.utc)

SEVERITY_ORDER = {"info": 0, "warning": 1, "blocker": 2}


@dataclass(frozen=True)
class Gap:
    code: str
    severity: str  # info | warning | blocker
    message: str
    details: Dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SegmentUse:
    """一份样本在周期内实际计入的片段。"""

    sample_id: str
    pollutant: str
    start_at: datetime
    end_at: datetime
    active_seconds: float
    equipment_id: str
    phase: str
    raw_value: Decimal
    canonical_value: Decimal
    weight_seconds: float


@dataclass(frozen=True)
class ExcludedSample:
    sample_id: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class PollutantResult:
    pollutant: str
    value: Optional[Decimal]          # canonical 单位下的加权均值
    unit: Optional[str]
    limit: Optional[Decimal]
    compliant: Optional[bool]
    included_samples: Tuple[str, ...]
    weight_seconds: float
    expected_samples: int
    observed_samples: int


@dataclass(frozen=True)
class CalcResult:
    line_id: str
    plant_id: str
    period: str
    period_start: datetime
    period_end: datetime
    as_of: datetime
    rule_id: str
    rule_version: str
    rule_effective_at: datetime
    operating_seconds: float
    shutdown_seconds: float
    equipment_gap_seconds: float
    pollutants: Tuple[PollutantResult, ...]
    segments: Tuple[SegmentUse, ...]
    excluded: Tuple[ExcludedSample, ...]
    gaps: Tuple[Gap, ...]
    included_sample_ids: Tuple[str, ...]
    verdict: str  # compliant | non_compliant | inconclusive
    calc_hash: str

    def has_blocker(self) -> bool:
        return any(g.severity == "blocker" for g in self.gaps)


def _canonical_unit(rule: AccountingRule, pollutant: str, unit: str) -> str:
    return rule.canonical_units.get(pollutant, unit)


def _convert(rule: AccountingRule, pollutant: str, value: Decimal,
             unit: str) -> Decimal:
    target = rule.canonical_units.get(pollutant, unit)
    if unit == target:
        return value
    factor = rule.conversions.get(unit)
    if factor is None:
        raise KeyError(unit)
    return value * Decimal(str(factor))


def _equipment_at(intervals: List[EquipmentInterval],
                  at: datetime) -> Optional[EquipmentInterval]:
    for iv in intervals:
        if iv.valid_from <= at and (iv.valid_to is None or at < iv.valid_to):
            return iv
    return None


def _coerce_dt(value) -> datetime:
    if isinstance(value, datetime):
        return as_utc(value)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return as_utc(parsed)


def calculate(
    repo: Repository,
    *,
    line_id: str,
    period: str,
    anchor,
    rule_id: str,
    as_of=None,
    rule_version: Optional[str] = None,
) -> CalcResult:
    """执行一次核算。本函数无副作用，可对同一输入反复调用得到相同结果。

    ``anchor`` / ``as_of`` 可传带时区的 datetime 或 ISO-8601 字符串。
    """
    line = repo.get_line(line_id)
    as_of = _coerce_dt(as_of) if as_of else datetime.now(timezone.utc)
    p_start, p_end = period_bounds(_coerce_dt(anchor), period)
    rule = repo.get_rule(rule_id, version=rule_version, as_of=as_of)

    gaps: List[Gap] = []
    excluded: List[ExcludedSample] = []

    # ---- 设备覆盖 --------------------------------------------------------
    intervals = [
        iv for iv in repo.equipment_for_line(line_id)
        if iv.valid_from < p_end and (iv.valid_to or _FAR_FUTURE) > p_start
    ]
    equip_segments: List[Tuple[datetime, datetime, EquipmentInterval]] = []
    for iv in intervals:
        seg_start = max(iv.valid_from, p_start)
        seg_end = min(iv.valid_to or _FAR_FUTURE, p_end)
        if seg_end > seg_start:
            equip_segments.append((seg_start, seg_end, iv))
    equip_segments.sort(key=lambda x: (x[0], x[1]))

    period_seconds = (p_end - p_start).total_seconds()
    # 仓储保证同产线设备区间互不重叠（相邻允许），故覆盖秒数直接相加；
    # 注意不能把相邻区间并成一段，否则会丢失改造切换的相位边界。
    equipment_covered = sum(
        (seg_end - seg_start).total_seconds()
        for seg_start, seg_end, _ in equip_segments
    )
    equipment_gap_seconds = period_seconds - equipment_covered
    equip_spans = [(a, b) for a, b, _ in equip_segments]
    for gap_seg in subtract_intervals((p_start, p_end), equip_spans):
        gaps.append(Gap(
            "equipment_coverage_gap",
            "blocker",
            "周期内存在无投运设备覆盖的时段，该时段无法归因到工艺/设备",
            {"start_at": gap_seg[0].isoformat(), "end_at": gap_seg[1].isoformat(),
             "seconds": (gap_seg[1] - gap_seg[0]).total_seconds()},
        ))

    phases = sorted({iv.phase for _, _, iv in equip_segments})
    if len(phases) > 1:
        gaps.append(Gap(
            "phase_switch_in_period",
            "info",
            f"周期跨越改造切换点，包含工艺阶段: {', '.join(phases)}",
            {"phases": phases,
             "boundaries": [
                 {"equipment_id": iv.equipment_id, "phase": iv.phase,
                  "valid_from": iv.valid_from.isoformat(),
                  "valid_to": iv.valid_to.isoformat() if iv.valid_to else None}
                 for iv in intervals
             ]},
        ))

    # ---- 停机 ------------------------------------------------------------
    notes = [
        n for n in repo.shutdowns_for_line(line_id)
        if n.start_at < p_end and n.end_at > p_start
    ]
    shutdown_clipped = [
        (max(n.start_at, p_start), min(n.end_at, p_end)) for n in notes
    ]
    shutdown_covered = 0.0
    for a, b in shutdown_clipped:
        shutdown_covered += sum(overlap_seconds(a, b, c, d) for c, d in equip_spans)
    for n in notes:
        gaps.append(Gap(
            "shutdown_in_period",
            "info",
            f"异常停机: {n.reason}",
            {"note_id": n.note_id, "start_at": n.start_at.isoformat(),
             "end_at": n.end_at.isoformat(),
             "equipment_id": n.equipment_id},
        ))

    # ---- 规则适用 --------------------------------------------------------
    if rule.effective_at > p_start:
        uncovered = (min(rule.effective_at, p_end) - p_start).total_seconds()
        gaps.append(Gap(
            "rule_not_effective_for_full_period",
            "warning",
            f"规则 {rule.key} 于 {rule.effective_at.isoformat()} 才生效，"
            "周期前段无适用规则",
            {"rule_id": rule.rule_id, "version": rule.version,
             "uncovered_seconds": max(0.0, uncovered)},
        ))

    # ---- 样本筛选 --------------------------------------------------------
    all_samples = repo.samples_for_line(line_id)

    # 迟到样本：测量时刻在周期内，但 as_of 时还没到
    def _measured_in_period(s: EmissionSample) -> bool:
        if s.is_window():
            return s.covers_from < p_end and s.covers_to > p_start
        return p_start <= s.measured_at < p_end

    late = [
        s for s in all_samples
        if _measured_in_period(s) and s.received_at > as_of
    ]
    for s in sorted(late, key=lambda x: x.sample_id):
        gaps.append(Gap(
            "late_data_pending",
            "warning",
            f"样本 {s.sample_id} 在 as_of 之后才到齐，本次结论未采纳",
            {"sample_id": s.sample_id, "received_at": s.received_at.isoformat()},
        ))

    visible = [
        s for s in all_samples
        if s.received_at <= as_of
        and (s.withdrawn_at is None or s.withdrawn_at > as_of)
    ]

    # 撤回样本（as_of 之前撤回）
    for s in sorted(all_samples, key=lambda x: x.sample_id):
        if _measured_in_period(s) and s.withdrawn_at is not None \
                and s.withdrawn_at <= as_of and s.received_at <= as_of:
            excluded.append(ExcludedSample(
                s.sample_id, "withdrawn",
                s.withdraw_reason or "样本已撤回",
            ))
            gaps.append(Gap(
                "sample_withdrawn",
                "info",
                f"样本 {s.sample_id} 已撤回，不参与核算",
                {"sample_id": s.sample_id,
                 "withdrawn_at": s.withdrawn_at.isoformat(),
                 "reason": s.withdraw_reason or ""},
            ))

    # 内容指纹去重：周期内可见集同指纹只留最早到（received_at, sample_id）
    fp_groups: Dict[Tuple, List[EmissionSample]] = {}
    for s in visible:
        if _measured_in_period(s):
            fp_groups.setdefault(Repository.fingerprint(s), []).append(s)
    duplicate_ids = set()
    for fp, group in fp_groups.items():
        if len(group) < 2:
            continue
        group_sorted = sorted(group, key=lambda x: (x.received_at, x.sample_id))
        keep = group_sorted[0]
        for dup in group_sorted[1:]:
            duplicate_ids.add(dup.sample_id)
            excluded.append(ExcludedSample(
                dup.sample_id, "duplicate",
                f"与 {keep.sample_id} 内容指纹一致，保留最早到样本",
            ))
        gaps.append(Gap(
            "duplicate_samples_deduped",
            "info",
            "检测到重复样本，按到件时间保留最早一份",
            {"kept": keep.sample_id,
             "dropped": [d.sample_id for d in group_sorted[1:]]},
        ))

    visible = [s for s in visible if s.sample_id not in duplicate_ids]

    # ---- 切片与加权 ------------------------------------------------------
    segments: List[SegmentUse] = []
    instant_weight = float(rule.sampling_interval_hours) * 3600.0

    for s in sorted(visible, key=lambda x: (x.measured_at, x.sample_id)):
        if not _measured_in_period(s):
            continue
        try:
            canonical = _convert(rule, s.pollutant, s.value, s.unit)
        except KeyError:
            excluded.append(ExcludedSample(
                s.sample_id, "unknown_unit",
                f"规则缺少单位 {s.unit} 的换算系数",
            ))
            gaps.append(Gap(
                "unknown_unit",
                "blocker",
                f"样本 {s.sample_id} 的单位 {s.unit} 无法换算到 canonical 单位",
                {"sample_id": s.sample_id, "unit": s.unit,
                 "pollutant": s.pollutant},
            ))
            continue

        if s.is_window():
            raw_pieces = subtract_intervals(
                (max(s.covers_from, p_start), min(s.covers_to, p_end)),
                shutdown_clipped,
            )
        else:
            t = s.measured_at
            in_shutdown = any(a <= t < b for a, b in shutdown_clipped)
            raw_pieces = [] if in_shutdown else [(t, t)]
            if in_shutdown:
                excluded.append(ExcludedSample(
                    s.sample_id, "during_shutdown", "测量时刻落在异常停机区间",
                ))

        for piece_start, piece_end in raw_pieces:
            if s.is_window():
                # 扣除无设备覆盖的部分，其余按当时所在设备归因
                covered_pieces = [
                    (max(piece_start, a), min(piece_end, b))
                    for a, b in equip_spans
                    if b > piece_start and a < piece_end
                ]
                uncovered_sec = (piece_end - piece_start).total_seconds() - sum(
                    (b - a).total_seconds() for a, b in covered_pieces
                )
                if uncovered_sec > 0:
                    gaps.append(Gap(
                        "sample_without_equipment",
                        "warning",
                        f"样本 {s.sample_id} 有 {uncovered_sec:.0f} 秒落在无设备"
                        "投运区间，已扣除",
                        {"sample_id": s.sample_id, "seconds": uncovered_sec},
                    ))
                pieces = [(a, b) for a, b in covered_pieces if b > a]
            else:
                iv = _equipment_at(intervals, piece_start)
                if iv is None:
                    excluded.append(ExcludedSample(
                        s.sample_id, "without_equipment",
                        "测量时刻无投运设备区间",
                    ))
                    continue
                pieces = [(piece_start, piece_end)]

            for seg_start, seg_end in pieces:
                iv = _equipment_at(intervals, seg_start)
                if iv is None:
                    continue
                active = (seg_end - seg_start).total_seconds()
                if not s.is_window():
                    weight = instant_weight
                    active_for_record = 0.0
                else:
                    weight = active
                    active_for_record = active
                segments.append(SegmentUse(
                    sample_id=s.sample_id,
                    pollutant=s.pollutant,
                    start_at=seg_start,
                    end_at=seg_end,
                    active_seconds=active_for_record,
                    equipment_id=iv.equipment_id,
                    phase=iv.phase,
                    raw_value=s.value,
                    canonical_value=canonical,
                    weight_seconds=weight,
                ))

    # ---- 按污染物汇总 ----------------------------------------------------
    pollutants = sorted(
        set(rule.limits) | {s.pollutant for s in visible if _measured_in_period(s)}
    )
    operating_seconds = max(0.0, equipment_covered - shutdown_covered)
    interval_secs = instant_weight
    expected_total = 0
    if interval_secs > 0:
        expected_total = -(-int(operating_seconds) // int(interval_secs))  # ceil

    results: List[PollutantResult] = []
    for pollutant in pollutants:
        segs = [seg for seg in segments if seg.pollutant == pollutant]
        weight_total = sum(seg.weight_seconds for seg in segs)
        if weight_total > 0:
            num = sum(
                seg.canonical_value * Decimal(str(seg.weight_seconds))
                for seg in segs
            )
            value = (num / Decimal(str(weight_total))).quantize(
                Decimal("0.000001")
            )
        else:
            value = None
        unit = rule.canonical_units.get(pollutant)
        limit = rule.limits.get(pollutant)
        sample_ids = tuple(sorted({seg.sample_id for seg in segs}))
        observed = len(sample_ids)
        # 样本当量：窗口样本按实际覆盖秒数折算，瞬时样本各代表一个采样间隔
        equivalents = int(round(weight_total / interval_secs)) if interval_secs else 0

        expected = expected_total
        compliant: Optional[bool]
        if observed < rule.min_samples:
            compliant = None
            gaps.append(Gap(
                "insufficient_samples",
                "blocker",
                f"污染物 {pollutant} 有效样本 {observed} 个，"
                f"少于规则要求的 {rule.min_samples} 个",
                {"pollutant": pollutant, "observed": observed,
                 "min_samples": rule.min_samples},
            ))
        elif expected > 0 and equivalents < expected:
            gaps.append(Gap(
                "sample_coverage_gap",
                "warning",
                f"污染物 {pollutant} 监测覆盖不足：应覆盖约 {expected} 个"
                f"采样间隔，实际 {equivalents} 个",
                {"pollutant": pollutant, "equivalents": equivalents,
                 "expected": expected},
            ))
        if limit is None:
            compliant = None
            if observed >= rule.min_samples:
                gaps.append(Gap(
                    "no_limit_defined",
                    "warning",
                    f"规则未定义污染物 {pollutant} 的限值，无法判定达标",
                    {"pollutant": pollutant},
                ))
        elif value is not None and observed >= rule.min_samples:
            compliant = value <= limit

        results.append(PollutantResult(
            pollutant=pollutant,
            value=value,
            unit=unit,
            limit=limit,
            compliant=compliant,
            included_samples=sample_ids,
            weight_seconds=weight_total,
            expected_samples=expected,
            observed_samples=equivalents,
        ))

    # ---- 结论判定 --------------------------------------------------------
    if any(g.severity == "blocker" for g in gaps):
        verdict = "inconclusive"
    elif any(r.compliant is False for r in results):
        verdict = "non_compliant"
    elif results and all(r.compliant is True for r in results):
        verdict = "compliant"
    else:
        verdict = "inconclusive"

    included_ids = tuple(sorted({seg.sample_id for seg in segments}))
    calc_hash = _hash_inputs(
        line_id=line_id, p_start=p_start, p_end=p_end, rule=rule,
        segments=segments,
        equipment=intervals, shutdowns=notes,
        included_ids=included_ids,
    )

    return CalcResult(
        line_id=line_id,
        plant_id=line.plant_id,
        period=period,
        period_start=p_start,
        period_end=p_end,
        as_of=as_of,
        rule_id=rule.rule_id,
        rule_version=rule.version,
        rule_effective_at=rule.effective_at,
        operating_seconds=operating_seconds,
        shutdown_seconds=shutdown_covered,
        equipment_gap_seconds=equipment_gap_seconds,
        pollutants=tuple(results),
        segments=tuple(segments),
        excluded=tuple(excluded),
        gaps=tuple(sorted(gaps, key=lambda g: (-SEVERITY_ORDER[g.severity], g.code))),
        included_sample_ids=included_ids,
        verdict=verdict,
        calc_hash=calc_hash,
    )


def _hash_inputs(*, line_id, p_start, p_end, rule: AccountingRule,
                 segments, equipment, shutdowns, included_ids) -> str:
    h = sha256()
    h.update(line_id.encode())
    h.update(p_start.isoformat().encode())
    h.update(p_end.isoformat().encode())
    # 刻意不纳入 as_of：证据集合与规则相同时，重算时刻不同不应产生"新结果"，
    # 否则"数据补齐"与"仅仅重新查询"将无法区分。
    h.update(rule.key.encode())
    h.update(rule.effective_at.isoformat().encode())
    h.update(rule.method.encode())
    for k in sorted(rule.limits):
        h.update(f"L|{k}|{rule.limits[k]}|{rule.canonical_units.get(k, '')}".encode())
    for k in sorted(rule.conversions):
        h.update(f"C|{k}|{rule.conversions[k]}".encode())
    for iv in sorted(equipment, key=lambda x: x.equipment_id):
        h.update(
            f"E|{iv.equipment_id}|{iv.valid_from.isoformat()}|"
            f"{iv.valid_to.isoformat() if iv.valid_to else ''}|{iv.phase}".encode()
        )
    for n in sorted(shutdowns, key=lambda x: x.note_id):
        h.update(f"S|{n.note_id}|{n.start_at.isoformat()}|{n.end_at.isoformat()}"
                 .encode())
    for seg in sorted(segments, key=lambda x: (x.start_at, x.sample_id)):
        h.update(
            f"X|{seg.sample_id}|{seg.start_at.isoformat()}|"
            f"{seg.end_at.isoformat()}|{seg.canonical_value}|"
            f"{seg.weight_seconds}".encode()
        )
    h.update(",".join(included_ids).encode())
    return h.hexdigest()
