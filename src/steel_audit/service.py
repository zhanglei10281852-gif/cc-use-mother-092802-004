"""应用服务：命令（数据接入、重算、签发）与查询（明细、更正链、审计）。

关键语义：
- 重算总是产生新的草稿结论，签发时自动引用该 (产线, 周期) 的当前链头，
  形成“新结论 --corrects--> 旧结论”的更正关系；旧版保持不可变；
- 签发时生成 explanation：对比上一版的规则、样本集合、设备分段与总量，
  标注差异来源（规则升级 / 迟到补报 / 样本撤回 / 设备投运变化），
  使“改造效果来自工艺变化还是数据补齐”可直接判定。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from . import models
from .compute import ComputeRequest, compute_conclusion, q6
from .errors import ConflictError, NotFoundError, ValidationError
from .models import (
    CONCLUSION_DRAFT,
    CONCLUSION_ISSUED,
    SAMPLE_ACTIVE,
    SAMPLE_DUPLICATE,
    SAMPLE_WITHDRAWN,
)
from .rules import parse_params
from .storage import Store
from .timeutil import UTC, fmt_dt, parse_dt, validate_period


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _parse_decimal(value, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValidationError(f"{field} 不是有效数值: {value!r}")


def _sample_json(s: models.Sample) -> dict:
    return {
        "sample_id": s.sample_id,
        "agency_uid": s.agency_uid,
        "line_id": s.line_id,
        "indicator": s.indicator,
        "value": str(s.value),
        "unit": s.unit,
        "interval_start": fmt_dt(s.interval_start),
        "interval_end": fmt_dt(s.interval_end),
        "received_at": fmt_dt(s.received_at),
        "status": s.status,
        "duplicate_of": s.duplicate_of,
        "withdrawn_at": fmt_dt(s.withdrawn_at) if s.withdrawn_at else None,
        "withdraw_reason": s.withdraw_reason,
    }


def _equipment_json(e: models.EquipmentInterval) -> dict:
    return {
        "equipment_interval_id": e.equipment_interval_id,
        "line_id": e.line_id,
        "equipment_code": e.equipment_code,
        "technology": e.technology,
        "capacity_tph": str(e.capacity_tph),
        "commissioned_from": fmt_dt(e.commissioned_from),
        "decommissioned_to": fmt_dt(e.decommissioned_to) if e.decommissioned_to else None,
        "created_at": fmt_dt(e.created_at),
    }


def _rule_json(rv: models.RuleVersion) -> dict:
    return {
        "rule_version_id": rv.rule_version_id,
        "code": rv.code,
        "effective_from": fmt_dt(rv.effective_from),
        "params": rv.params,
        "created_at": fmt_dt(rv.created_at),
    }


def _segments_signature(detail: dict):
    """设备分段签名：用于判断两版结论之间设备投运区间是否变化。"""
    return [
        (seg["start"], seg["end"],
         seg["equipment"]["equipment_code"] if seg["equipment"] else None)
        for seg in detail["segments"]
    ]


class SteelAuditService:
    """核证后端服务入口。store 缺省为内存库；now_fn 可注入以便测试。"""

    def __init__(self, store: Optional[Store] = None, now_fn=None):
        self.store = store or Store()
        self._now_fn = now_fn or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        return self._now_fn()

    def _audit(self, actor: str, action: str, entity_type: str, entity_id: str,
               line_id: Optional[str], payload: dict) -> None:
        self.store.insert_audit(self._now(), actor, action, entity_type, entity_id,
                                line_id, payload)

    # ================= 命令 =================

    def create_line(self, plant_id: str, name: str = "", capacity_tonnes="0",
                    line_id: Optional[str] = None, actor: str = "system") -> dict:
        line_id = line_id or _new_id("line")
        if self.store.get_line(line_id):
            raise ConflictError(f"产线已存在: {line_id}")
        capacity = _parse_decimal(capacity_tonnes, "capacity_tonnes")
        self.store.insert_line(line_id, plant_id, name, capacity, self._now())
        self._audit(actor, "line_created", "line", line_id, line_id,
                    {"plant_id": plant_id, "name": name})
        return self.get_line(line_id)

    def add_equipment_interval(self, line_id: str, equipment_code: str,
                               commissioned_from, decommissioned_to=None,
                               technology: str = "", capacity_tph="0",
                               actor: str = "system") -> dict:
        self._require_line(line_id)
        start = parse_dt(commissioned_from)
        end = parse_dt(decommissioned_to) if decommissioned_to else None
        if end is not None and end <= start:
            raise ValidationError("设备退役时刻必须晚于投运时刻")
        capacity = _parse_decimal(capacity_tph, "capacity_tph")
        # 同一产线的设备区间不得重叠（同一时刻至多一台设备在运）
        from .timeutil import FAR_FUTURE
        new_end = end or FAR_FUTURE
        for e in self.store.list_equipment_intervals(line_id):
            e_end = e.decommissioned_to or FAR_FUTURE
            if start < e_end and new_end > e.commissioned_from:
                raise ConflictError(
                    f"设备投运区间与既有区间重叠: {e.equipment_code} "
                    f"[{fmt_dt(e.commissioned_from)}, "
                    f"{fmt_dt(e.decommissioned_to) if e.decommissioned_to else '在运'})")
        record = models.EquipmentInterval(
            equipment_interval_id=_new_id("eq"),
            line_id=line_id,
            equipment_code=equipment_code,
            technology=technology,
            capacity_tph=capacity,
            commissioned_from=start,
            decommissioned_to=end,
            created_at=self._now(),
        )
        self.store.insert_equipment_interval(record)
        self._audit(actor, "equipment_interval_added", "equipment_interval",
                    record.equipment_interval_id, line_id,
                    {"equipment_code": equipment_code,
                     "commissioned_from": fmt_dt(start),
                     "decommissioned_to": fmt_dt(end) if end else None})
        return _equipment_json(record)

    def submit_sample(self, line_id: str, agency_uid: str, indicator: str, value,
                      unit: str, interval_start, interval_end, received_at=None,
                      actor: str = "system") -> dict:
        """样本报送。确定处理规则：

        - 同一 agency_uid 内容完全一致：幂等重放，返回原样本；
        - 同一 agency_uid 内容不一致：冲突，须撤回原样本后以新编号重报；
        - 自然键（产线+指标+采样时段）与有效样本重复：标记 duplicate 并排除，
          先到先采信；不自动提升被排除样本。
        """
        self._require_line(line_id)
        if not indicator or not indicator.strip():
            raise ValidationError("indicator 不能为空")
        parsed_value = _parse_decimal(value, "value")
        start = parse_dt(interval_start)
        end = parse_dt(interval_end)
        if end <= start:
            raise ValidationError("采样时段结束必须晚于开始")
        received = parse_dt(received_at) if received_at else self._now()

        existing = self.store.get_sample_by_uid(agency_uid)
        if existing is not None:
            same = (
                existing.line_id == line_id
                and existing.indicator == indicator
                and existing.value == parsed_value
                and existing.unit == unit
                and existing.interval_start == start
                and existing.interval_end == end
            )
            if same:
                return {"outcome": "idempotent_replay", "sample": _sample_json(existing)}
            raise ConflictError(
                f"监测编号 {agency_uid} 已存在且内容不一致；"
                "请撤回原样本后以新编号重新报送")

        duplicates = self.store.find_active_natural_duplicates(line_id, indicator, start, end)
        status = SAMPLE_DUPLICATE if duplicates else SAMPLE_ACTIVE
        duplicate_of = duplicates[0].sample_id if duplicates else None
        sample = models.Sample(
            sample_id=_new_id("smp"),
            agency_uid=agency_uid,
            line_id=line_id,
            indicator=indicator,
            value=parsed_value,
            unit=unit,
            interval_start=start,
            interval_end=end,
            received_at=received,
            status=status,
            duplicate_of=duplicate_of,
        )
        self.store.insert_sample(sample)
        outcome = "duplicate_flagged" if duplicates else "accepted"
        self._audit(actor, "sample_submitted", "sample", sample.sample_id, line_id,
                    {"agency_uid": agency_uid, "outcome": outcome,
                     "duplicate_of": duplicate_of})
        return {"outcome": outcome, "sample": _sample_json(sample)}

    def withdraw_sample(self, sample_id: str, reason: str = "", actor: str = "system") -> dict:
        """撤回样本：仅影响撤回之后的重算；已签发结论保持原样（不可变）。

        注意：被撤样本的重复件不会自动提升为有效，需以新编号重新报送。
        """
        sample = self.store.get_sample(sample_id)
        if sample is None:
            raise NotFoundError(f"样本不存在: {sample_id}")
        if sample.status == SAMPLE_WITHDRAWN:
            return {"outcome": "already_withdrawn", "sample": _sample_json(sample)}
        if sample.status == SAMPLE_DUPLICATE:
            raise ConflictError("重复样本本就被排除，无需撤回")
        self.store.mark_sample_withdrawn(sample_id, self._now(), reason)
        self._audit(actor, "sample_withdrawn", "sample", sample_id, sample.line_id,
                    {"agency_uid": sample.agency_uid, "reason": reason})
        return {"outcome": "withdrawn",
                "sample": _sample_json(self.store.get_sample(sample_id))}

    def add_shutdown(self, line_id: str, start, end, reason: Optional[str] = None,
                     evidence_ref: Optional[str] = None, actor: str = "system") -> dict:
        self._require_line(line_id)
        start_dt, end_dt = parse_dt(start), parse_dt(end)
        if end_dt <= start_dt:
            raise ValidationError("停机结束时刻必须晚于开始时刻")
        record = models.ShutdownEvent(
            shutdown_id=_new_id("shd"),
            line_id=line_id,
            start=start_dt,
            end=end_dt,
            reason=reason or None,
            evidence_ref=evidence_ref or None,
            created_at=self._now(),
        )
        self.store.insert_shutdown(record)
        self._audit(actor, "shutdown_recorded", "shutdown", record.shutdown_id, line_id,
                    {"start": fmt_dt(start_dt), "end": fmt_dt(end_dt),
                     "has_reason": bool(record.reason)})
        return {
            "shutdown_id": record.shutdown_id,
            "line_id": line_id,
            "start": fmt_dt(start_dt),
            "end": fmt_dt(end_dt),
            "reason": record.reason,
            "evidence_ref": record.evidence_ref,
        }

    def create_rule_version(self, code: str, effective_from, params: dict,
                            actor: str = "system") -> dict:
        if not code or not code.strip():
            raise ValidationError("规则编码不能为空")
        effective = parse_dt(effective_from)
        canonical = parse_params(params).to_dict()  # 校验并规范化
        record = models.RuleVersion(
            rule_version_id=_new_id("rule"),
            code=code,
            effective_from=effective,
            params=canonical,
            created_at=self._now(),
        )
        self.store.insert_rule_version(record)
        self._audit(actor, "rule_version_created", "rule_version",
                    record.rule_version_id, None,
                    {"code": code, "effective_from": fmt_dt(effective)})
        return _rule_json(record)

    def compute(self, line_id: str, period_type: str, period_start,
                rule_version_id: Optional[str] = None, data_cutoff=None,
                actor: str = "system") -> dict:
        """按统计周期重算，生成新的草稿结论（不写更正链，签发时才入链）。"""
        p_start = parse_dt(period_start)
        try:
            validate_period(period_type, p_start)
        except ValueError as exc:
            raise ValidationError(str(exc))
        cutoff = parse_dt(data_cutoff) if data_cutoff else self._now()
        content = compute_conclusion(
            self.store,
            ComputeRequest(line_id=line_id, period_type=period_type,
                           period_start=p_start, rule_version_id=rule_version_id,
                           data_cutoff=cutoff),
        )
        record = models.Conclusion(
            conclusion_id=_new_id("con"),
            line_id=line_id,
            period_type=period_type,
            period_start=content["period_start"],
            period_end=content["period_end"],
            rule_version_id=content["rule_version_id"],
            data_cutoff=content["data_cutoff"],
            status=CONCLUSION_DRAFT,
            corrects_id=None,
            totals=content["totals"],
            detail=content["detail"],
            gaps=content["gaps"],
            explanation=None,
            created_at=self._now(),
            issued_at=None,
        )
        self.store.insert_conclusion(record)
        self._audit(actor, "conclusion_computed", "conclusion", record.conclusion_id,
                    line_id, {"period_type": period_type,
                              "period_start": fmt_dt(record.period_start),
                              "rule_version_id": record.rule_version_id,
                              "data_cutoff": fmt_dt(record.data_cutoff)})
        return self.get_conclusion(record.conclusion_id)

    def issue(self, conclusion_id: str, actor: str = "system") -> dict:
        """签发草稿：结论从此不可变，并以更正关系引用该周期上一版签发结论。"""
        record = self.store.get_conclusion(conclusion_id)
        if record is None:
            raise NotFoundError(f"结论不存在: {conclusion_id}")
        if record.status == CONCLUSION_ISSUED:
            raise ConflictError(f"结论已签发，不可重复签发: {conclusion_id}")
        with self.store.locked():
            head = self.store.find_issued_head(record.line_id, record.period_type,
                                               record.period_start)
            explanation = self._build_explanation(head, record)
            corrects_id = head.conclusion_id if head else None
            ok = self.store.publish_conclusion(conclusion_id, self._now(),
                                               corrects_id, explanation)
            if not ok:
                raise ConflictError(f"结论状态已变化，无法签发: {conclusion_id}")
        self._audit(actor, "conclusion_issued", "conclusion", conclusion_id,
                    record.line_id, {"corrects_id": corrects_id,
                                     "cause_tags": explanation.get("cause_tags", [])})
        return self.get_conclusion(conclusion_id)

    # ================= 查询 =================

    def get_line(self, line_id: str) -> dict:
        line = self.store.get_line(line_id)
        if line is None:
            raise NotFoundError(f"产线不存在: {line_id}")
        return line

    def list_equipment(self, line_id: str) -> List[dict]:
        self._require_line(line_id)
        return [_equipment_json(e) for e in self.store.list_equipment_intervals(line_id)]

    def list_samples(self, line_id: str) -> List[dict]:
        self._require_line(line_id)
        return [_sample_json(s) for s in self.store.list_samples(line_id)]

    def list_rule_versions(self) -> List[dict]:
        return [_rule_json(rv) for rv in self.store.list_rule_versions()]

    def get_conclusion(self, conclusion_id: str) -> dict:
        """结论完整视图：汇总、计算明细、证据缺口、差异说明。"""
        record = self.store.get_conclusion(conclusion_id)
        if record is None:
            raise NotFoundError(f"结论不存在: {conclusion_id}")
        return self._conclusion_json(record)

    def list_conclusions(self, line_id: str, period_type: Optional[str] = None,
                         period_start=None) -> List[dict]:
        self._require_line(line_id)
        p_start = parse_dt(period_start) if period_start else None
        records = self.store.list_conclusions(line_id, period_type, p_start)
        superseded = self.store.list_superseded_ids()
        return [self._conclusion_summary(c, superseded) for c in records]

    def get_lineage(self, conclusion_id: str) -> dict:
        """变更脉络：从最初版本到当前链头的完整更正链。"""
        focus = self.store.get_conclusion(conclusion_id)
        if focus is None:
            raise NotFoundError(f"结论不存在: {conclusion_id}")
        ancestors: List[models.Conclusion] = []
        node = focus
        while node.corrects_id:
            parent = self.store.get_conclusion(node.corrects_id)
            if parent is None:
                break
            ancestors.append(parent)
            node = parent
        descendants: List[models.Conclusion] = []
        node = focus
        while True:
            successor = self.store.find_issued_successor(node.conclusion_id)
            if successor is None:
                break
            descendants.append(successor)
            node = successor
        chain = list(reversed(ancestors)) + [focus] + descendants
        superseded = self.store.list_superseded_ids()
        return {
            "focus": conclusion_id,
            "current_head": chain[-1].conclusion_id,
            "chain": [self._conclusion_summary(c, superseded, with_explanation=True)
                      for c in chain],
        }

    def audit_trail(self, entity_type: Optional[str] = None,
                    entity_id: Optional[str] = None,
                    line_id: Optional[str] = None) -> List[dict]:
        return self.store.list_audit(entity_type, entity_id, line_id)

    # ================= 内部 =================

    def _require_line(self, line_id: str) -> None:
        if self.store.get_line(line_id) is None:
            raise NotFoundError(f"产线不存在: {line_id}")

    def _conclusion_json(self, c: models.Conclusion) -> dict:
        rule = self.store.get_rule_version(c.rule_version_id)
        superseded = self.store.list_superseded_ids()
        return {
            "conclusion_id": c.conclusion_id,
            "line_id": c.line_id,
            "period_type": c.period_type,
            "period_start": fmt_dt(c.period_start),
            "period_end": fmt_dt(c.period_end),
            "rule_version_id": c.rule_version_id,
            "rule_code": rule.code if rule else None,
            "data_cutoff": fmt_dt(c.data_cutoff),
            "status": c.status,
            "corrects_id": c.corrects_id,
            "is_current": c.status == CONCLUSION_ISSUED
                          and c.conclusion_id not in superseded,
            "totals": c.totals,
            "detail": c.detail,
            "gaps": c.gaps,
            "explanation": c.explanation,
            "created_at": fmt_dt(c.created_at),
            "issued_at": fmt_dt(c.issued_at) if c.issued_at else None,
        }

    def _conclusion_summary(self, c: models.Conclusion, superseded: set,
                            with_explanation: bool = False) -> dict:
        rule = self.store.get_rule_version(c.rule_version_id)
        summary = {
            "conclusion_id": c.conclusion_id,
            "line_id": c.line_id,
            "period_type": c.period_type,
            "period_start": fmt_dt(c.period_start),
            "period_end": fmt_dt(c.period_end),
            "rule_version_id": c.rule_version_id,
            "rule_code": rule.code if rule else None,
            "status": c.status,
            "corrects_id": c.corrects_id,
            "is_current": c.status == CONCLUSION_ISSUED
                          and c.conclusion_id not in superseded,
            "totals": c.totals,
            "gap_count": len(c.gaps),
            "created_at": fmt_dt(c.created_at),
            "issued_at": fmt_dt(c.issued_at) if c.issued_at else None,
        }
        if with_explanation:
            summary["explanation"] = c.explanation
        return summary

    @staticmethod
    def _build_explanation(old: Optional[models.Conclusion],
                           new: models.Conclusion) -> dict:
        """签发时生成差异说明：这版结论相对上一版为什么变了。"""
        if old is None:
            return {"type": "initial", "corrects": None, "cause_tags": ["首次签发"]}

        old_samples = {s["sample_id"] for s in old.detail.get("samples", [])}
        new_samples = {s["sample_id"] for s in new.detail.get("samples", [])}
        added = sorted(new_samples - old_samples)
        removed = sorted(old_samples - new_samples)

        rule_change = None
        if old.rule_version_id != new.rule_version_id:
            rule_change = {
                "from_rule_version_id": old.rule_version_id,
                "to_rule_version_id": new.rule_version_id,
                "from_code": old.detail["rule"]["code"],
                "to_code": new.detail["rule"]["code"],
            }

        equipment_change = (_segments_signature(old.detail)
                            != _segments_signature(new.detail))

        total_deltas = {}
        for key in sorted(set(old.totals) & set(new.totals)):
            ov, nv = old.totals[key], new.totals[key]
            if isinstance(ov, str) and isinstance(nv, str):
                try:
                    delta = Decimal(nv) - Decimal(ov)
                except InvalidOperation:
                    continue
                if delta != 0:
                    total_deltas[key] = {"old": ov, "new": nv, "delta": q6(delta)}
            elif ov != nv:
                total_deltas[key] = {"old": ov, "new": nv}

        cause_tags = []
        if rule_change:
            cause_tags.append("规则升级/变更")
        if added:
            cause_tags.append("迟到或补报样本纳入")
        if removed:
            cause_tags.append("样本撤回或数据截止调整")
        if equipment_change:
            cause_tags.append("设备投运区间变化（工艺/改造）")
        if not cause_tags:
            cause_tags.append("数据截止时点或口径微调")

        return {
            "type": "correction",
            "corrects": old.conclusion_id,
            "rule_change": rule_change,
            "samples_added": added,
            "samples_removed": removed,
            "equipment_change": equipment_change,
            "total_deltas": total_deltas,
            "cause_tags": cause_tags,
        }
