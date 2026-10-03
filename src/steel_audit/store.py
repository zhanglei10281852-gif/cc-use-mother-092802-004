"""内存仓储：核证数据的唯一事实来源。

设计要点：
- 写入默认幂等：同一 ID 以相同内容重复提交直接返回既有记录；
  内容冲突则抛 :class:`ConflictError`，杜绝"静默覆盖"。
- 结论（conclusion）只增不改：已签发结论没有任何更新入口，结构上保证不可变。
- 不依赖字典迭代顺序做业务判断；所有时序判定使用显式的 ``received_at``。
"""

from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from .contracts import (
    AccountingRule,
    EmissionSample,
    EquipmentInterval,
    ProductionLine,
    ShutdownNote,
)
from .errors import ConflictError, NotFoundError, ValidationError
from .timeutils import as_utc, overlap_seconds, utc_now


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


class Repository:
    def __init__(self) -> None:
        self._lines: Dict[str, ProductionLine] = {}
        self._equipment: Dict[str, EquipmentInterval] = {}
        self._shutdowns: Dict[str, ShutdownNote] = {}
        self._rules: Dict[Tuple[str, str], AccountingRule] = {}
        self._samples: Dict[str, EmissionSample] = {}

    # ----- 产线 -----------------------------------------------------------

    def add_line(self, line: ProductionLine) -> ProductionLine:
        _require(bool(line.line_id), "line_id 不能为空")
        _require(line.capacity_tonnes > 0, "产能必须为正数")
        line = ProductionLine(
            plant_id=line.plant_id,
            line_id=line.line_id,
            capacity_tonnes=line.capacity_tonnes,
            commissioned_at=as_utc(line.commissioned_at),
            supersedes_line_id=line.supersedes_line_id,
        )
        existing = self._lines.get(line.line_id)
        if existing is not None:
            if existing != line:
                raise ConflictError(f"产线 {line.line_id} 已存在且内容不一致")
            return existing
        if line.supersedes_line_id and line.supersedes_line_id not in self._lines:
            # 允许先登记新产线再补旧产线，但切换链查询时旧产线须存在
            pass
        self._lines[line.line_id] = line
        return line

    def get_line(self, line_id: str) -> ProductionLine:
        try:
            return self._lines[line_id]
        except KeyError:
            raise NotFoundError(f"产线不存在: {line_id}")

    def list_lines(self) -> List[ProductionLine]:
        return [self._lines[k] for k in sorted(self._lines)]

    # ----- 设备投运区间 ---------------------------------------------------

    def add_equipment(self, interval: EquipmentInterval) -> EquipmentInterval:
        _require(bool(interval.equipment_id), "equipment_id 不能为空")
        valid_from = as_utc(interval.valid_from)
        valid_to = as_utc(interval.valid_to) if interval.valid_to else None
        if valid_to is not None:
            _require(valid_to > valid_from, "设备区间 valid_to 必须晚于 valid_from")
        if interval.line_id not in self._lines:
            raise NotFoundError(f"设备引用了未登记的产线: {interval.line_id}")
        interval = EquipmentInterval(
            equipment_id=interval.equipment_id,
            line_id=interval.line_id,
            valid_from=valid_from,
            valid_to=valid_to,
            phase=interval.phase,
            name=interval.name,
        )
        existing = self._equipment.get(interval.equipment_id)
        if existing is not None:
            if existing != interval:
                raise ConflictError(f"设备 {interval.equipment_id} 已存在且内容不一致")
            return existing
        for other in self._equipment.values():
            if other.line_id != interval.line_id:
                continue
            if overlap_seconds(
                valid_from, valid_to or datetime.max.replace(tzinfo=valid_from.tzinfo),
                other.valid_from,
                other.valid_to or datetime.max.replace(tzinfo=valid_from.tzinfo),
            ) > 0:
                raise ConflictError(
                    f"设备区间重叠: {interval.equipment_id} 与 {other.equipment_id}"
                )
        self._equipment[interval.equipment_id] = interval
        return interval

    def equipment_for_line(self, line_id: str) -> List[EquipmentInterval]:
        return sorted(
            (e for e in self._equipment.values() if e.line_id == line_id),
            key=lambda e: e.valid_from,
        )

    def list_equipment(self) -> List[EquipmentInterval]:
        return sorted(
            self._equipment.values(), key=lambda e: (e.line_id, e.valid_from)
        )

    # ----- 异常停机 -------------------------------------------------------

    def add_shutdown(self, note: ShutdownNote) -> ShutdownNote:
        _require(bool(note.note_id), "note_id 不能为空")
        start_at = as_utc(note.start_at)
        end_at = as_utc(note.end_at)
        _require(end_at > start_at, "停机结束必须晚于开始")
        note = ShutdownNote(
            note_id=note.note_id,
            line_id=note.line_id,
            start_at=start_at,
            end_at=end_at,
            reason=note.reason,
            equipment_id=note.equipment_id,
        )
        existing = self._shutdowns.get(note.note_id)
        if existing is not None:
            if existing != note:
                raise ConflictError(f"停机说明 {note.note_id} 已存在且内容不一致")
            return existing
        self._shutdowns[note.note_id] = note
        return note

    def shutdowns_for_line(self, line_id: str) -> List[ShutdownNote]:
        return sorted(
            (n for n in self._shutdowns.values() if n.line_id == line_id),
            key=lambda n: (n.start_at, n.note_id),
        )

    # ----- 核算规则 -------------------------------------------------------

    def add_rule(self, rule: AccountingRule) -> AccountingRule:
        _require(bool(rule.rule_id), "rule_id 不能为空")
        _require(bool(rule.version), "version 不能为空")
        rule = AccountingRule(
            rule_id=rule.rule_id,
            version=rule.version,
            effective_at=as_utc(rule.effective_at),
            limits=dict(rule.limits),
            canonical_units=dict(rule.canonical_units),
            conversions=dict(rule.conversions),
            min_samples=rule.min_samples,
            sampling_interval_hours=Decimal(str(rule.sampling_interval_hours)),
            method=rule.method,
        )
        key = (rule.rule_id, rule.version)
        existing = self._rules.get(key)
        if existing is not None:
            if existing != rule:
                raise ConflictError(f"规则 {rule.key} 已存在且内容不一致")
            return existing
        self._rules[key] = rule
        return rule

    def get_rule(self, rule_id: str, version: Optional[str] = None,
                 as_of: Optional[datetime] = None) -> AccountingRule:
        """取规则；给 ``as_of`` 时返回该时刻已生效的最高版本（确定性选版）。"""
        candidates = [r for r in self._rules.values() if r.rule_id == rule_id]
        if not candidates:
            raise NotFoundError(f"核算规则不存在: {rule_id}")
        if version is not None:
            try:
                return self._rules[(rule_id, version)]
            except KeyError:
                raise NotFoundError(f"核算规则版本不存在: {rule_id}@{version}")
        if as_of is None:
            as_of = utc_now()
        effective = [r for r in candidates if r.effective_at <= as_of]
        if not effective:
            raise NotFoundError(
                f"截至 {as_of.isoformat()} 规则 {rule_id} 尚无生效版本"
            )
        return max(effective, key=lambda r: (version_key(r.version), r.effective_at))

    def list_rules(self) -> List[AccountingRule]:
        return [self._rules[k] for k in sorted(self._rules)]

    # ----- 监测样本 -------------------------------------------------------

    @staticmethod
    def fingerprint(sample: EmissionSample) -> Tuple:
        return (
            sample.line_id,
            sample.pollutant,
            sample.measured_at.isoformat(),
            sample.covers_from.isoformat() if sample.covers_from else None,
            sample.covers_to.isoformat() if sample.covers_to else None,
            str(sample.value),
            sample.unit,
        )

    def submit_sample(self, sample: EmissionSample) -> EmissionSample:
        """提交样本。

        确定性处理规则：
        - 同 ``sample_id`` 重复提交、内容一致 → 幂等返回（重复上报）；
        - 同 ``sample_id`` 但内容不一致 → :class:`ConflictError`；
        - ``sample_id`` 不同但内容指纹一致 → 标记 ``suspected_duplicate_of``，
          核算时只保留最早到的一份（见引擎）。
        """
        _require(bool(sample.sample_id), "sample_id 不能为空")
        _require(sample.value >= 0, "排放测量值不能为负")
        measured_at = as_utc(sample.measured_at)
        received_at = as_utc(sample.received_at) if sample.received_at else utc_now()
        covers_from = as_utc(sample.covers_from) if sample.covers_from else None
        covers_to = as_utc(sample.covers_to) if sample.covers_to else None
        if covers_from is not None and covers_to is not None:
            _require(covers_to > covers_from, "监测窗口 covers_to 必须晚于 covers_from")
        sample = EmissionSample(
            sample_id=sample.sample_id,
            line_id=sample.line_id,
            pollutant=sample.pollutant,
            measured_at=measured_at,
            value=sample.value,
            unit=sample.unit,
            withdrawn=sample.withdrawn,
            received_at=received_at,
            covers_from=covers_from,
            covers_to=covers_to,
            source=sample.source,
            withdrawn_at=as_utc(sample.withdrawn_at) if sample.withdrawn_at else None,
            withdraw_reason=sample.withdraw_reason,
            suspected_duplicate_of=sample.suspected_duplicate_of,
        )
        existing = self._samples.get(sample.sample_id)
        if existing is not None:
            if existing != sample:
                raise ConflictError(
                    f"样本 {sample.sample_id} 已存在且提交内容不一致，禁止覆盖；"
                    "如需纠正请先撤回再以新 ID 提交"
                )
            return existing

        # 提交时即标记撤回但未给撤回时刻：视为到件时即撤回（从未生效）
        if sample.withdrawn and sample.withdrawn_at is None:
            sample = EmissionSample(
                **{**sample.__dict__, "withdrawn_at": received_at}
            )

        # 内容指纹去重：同指纹保留 received_at 最早（再按 sample_id 兜底）
        same_fp = [
            s for s in self._samples.values()
            if self.fingerprint(s) == self.fingerprint(sample)
        ]
        if same_fp:
            original = min(same_fp, key=lambda s: (s.received_at, s.sample_id))
            sample = sample.mark_duplicate(original.sample_id)
        self._samples[sample.sample_id] = sample
        return sample

    def withdraw_sample(self, sample_id: str, reason: str,
                        at: Optional[datetime] = None) -> EmissionSample:
        try:
            sample = self._samples[sample_id]
        except KeyError:
            raise NotFoundError(f"样本不存在: {sample_id}")
        if sample.withdrawn:
            return sample
        withdrawn = sample.mark_withdrawn(as_utc(at) if at else utc_now(), reason)
        self._samples[sample_id] = withdrawn
        return withdrawn

    def get_sample(self, sample_id: str) -> EmissionSample:
        try:
            return self._samples[sample_id]
        except KeyError:
            raise NotFoundError(f"样本不存在: {sample_id}")

    def samples_for_line(self, line_id: str) -> List[EmissionSample]:
        return [
            self._samples[k]
            for k in sorted(self._samples)
            if self._samples[k].line_id == line_id
        ]

    def list_samples(self) -> List[EmissionSample]:
        return [self._samples[k] for k in sorted(self._samples)]


def version_key(version: str) -> Tuple:
    """ ``"1.10.2"`` -> ``(1, 10, 2)``；非数字段回退为 ``(-1, 原字符串)``。"""
    parts = []
    for chunk in version.split("."):
        try:
            parts.append((0, int(chunk), ""))
        except ValueError:
            parts.append((1, -1, chunk))
    return tuple(parts)
