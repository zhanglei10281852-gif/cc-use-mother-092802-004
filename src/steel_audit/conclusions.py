"""阶段结论：签发即不可变；更正通过单向 supersede 链引用旧版。

结论保存的是**快照**（输入证据 ID 集合、规则版本、计算结果与输入哈希），
而不是引用可变实体——样本撤回、规则升级都不会改动旧结论的任何字段。
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple

from .engine import CalcResult
from .errors import ImmutableError, NotFoundError, ValidationError
from .timeutils import as_utc


class ConclusionStatus(str, Enum):
    DRAFT = "draft"          # 已计算未签发，可丢弃重算
    ISSUED = "issued"        # 已签发，冻结
    SUPERSEDED = "superseded"  # 被新版更正引用（仅状态标记变化，内容冻结）


@dataclass(frozen=True)
class EvidenceSnapshot:
    sample_ids: Tuple[str, ...]
    equipment_ids: Tuple[str, ...]
    shutdown_ids: Tuple[str, ...]
    rule_id: str
    rule_version: str


@dataclass(frozen=True)
class Conclusion:
    conclusion_id: str
    line_id: str
    period: str
    period_start: datetime
    period_end: datetime
    as_of: datetime
    status: ConclusionStatus
    verdict: str
    calc_hash: str
    rule_id: str
    rule_version: str
    issued_at: Optional[datetime]
    issued_by: Optional[str]
    supersede_reason: Optional[str]
    result: CalcResult
    evidence: EvidenceSnapshot
    superseding_ids: Tuple[str, ...] = ()


class ConclusionRegistry:
    def __init__(self) -> None:
        self._items: Dict[str, Conclusion] = {}

    @staticmethod
    def _snapshot(result: CalcResult) -> EvidenceSnapshot:
        return EvidenceSnapshot(
            sample_ids=result.included_sample_ids,
            equipment_ids=tuple(sorted({seg.equipment_id for seg in result.segments})),
            shutdown_ids=tuple(sorted({
                g.details["note_id"]
                for g in result.gaps
                if g.code == "shutdown_in_period"
            })),
            rule_id=result.rule_id,
            rule_version=result.rule_version,
        )

    def create_draft(self, conclusion_id: str, result: CalcResult,
                     superseding_ids: Tuple[str, ...] = (),
                     supersede_reason: Optional[str] = None) -> Conclusion:
        if conclusion_id in self._items:
            raise ValidationError(f"结论 ID 已存在: {conclusion_id}")
        for old_id in superseding_ids:
            if old_id not in self._items:
                raise NotFoundError(f"被更正的旧结论不存在: {old_id}")
        item = Conclusion(
            conclusion_id=conclusion_id,
            line_id=result.line_id,
            period=result.period,
            period_start=result.period_start,
            period_end=result.period_end,
            as_of=result.as_of,
            status=ConclusionStatus.DRAFT,
            verdict=result.verdict,
            calc_hash=result.calc_hash,
            rule_id=result.rule_id,
            rule_version=result.rule_version,
            issued_at=None,
            issued_by=None,
            supersede_reason=supersede_reason,
            result=result,
            evidence=self._snapshot(result),
            superseding_ids=tuple(superseding_ids),
        )
        self._items[conclusion_id] = item
        return item

    def issue(self, conclusion_id: str, issued_by: str,
              issued_at: Optional[datetime] = None) -> Conclusion:
        item = self._require(conclusion_id)
        if item.status == ConclusionStatus.ISSUED:
            return item
        if item.status == ConclusionStatus.SUPERSEDED:
            raise ImmutableError(
                f"结论 {conclusion_id} 已被更正引用并冻结，不能再签发"
            )
        issued = Conclusion(
            conclusion_id=item.conclusion_id,
            line_id=item.line_id,
            period=item.period,
            period_start=item.period_start,
            period_end=item.period_end,
            as_of=item.as_of,
            status=ConclusionStatus.ISSUED,
            verdict=item.verdict,
            calc_hash=item.calc_hash,
            rule_id=item.rule_id,
            rule_version=item.rule_version,
            issued_at=as_utc(issued_at) if issued_at else as_utc(item.as_of),
            issued_by=issued_by,
            supersede_reason=item.supersede_reason,
            result=item.result,
            evidence=item.evidence,
            superseding_ids=item.superseding_ids,
        )
        self._items[conclusion_id] = issued
        return issued

    def supersede(self, old_id: str, new_id: str, result: CalcResult,
                  reason: str, issued_by: str,
                  issued_at: Optional[datetime] = None) -> Conclusion:
        """签发新结论并把旧结论标记为 SUPERSEDED；旧结论快照内容不变。"""
        old = self._require(old_id)
        new = self.create_draft(
            new_id, result, superseding_ids=(old_id,), supersede_reason=reason
        )
        new = self.issue(new_id, issued_by=issued_by, issued_at=issued_at)
        # 旧对象只换枚举状态，result/evidence 等全部原样保留
        frozen_old = Conclusion(
            conclusion_id=old.conclusion_id,
            line_id=old.line_id,
            period=old.period,
            period_start=old.period_start,
            period_end=old.period_end,
            as_of=old.as_of,
            status=ConclusionStatus.SUPERSEDED,
            verdict=old.verdict,
            calc_hash=old.calc_hash,
            rule_id=old.rule_id,
            rule_version=old.rule_version,
            issued_at=old.issued_at,
            issued_by=old.issued_by,
            supersede_reason=old.supersede_reason,
            result=old.result,
            evidence=old.evidence,
            superseding_ids=old.superseding_ids,
        )
        self._items[old_id] = frozen_old
        return new

    def get(self, conclusion_id: str) -> Conclusion:
        return self._require(conclusion_id)

    def _require(self, conclusion_id: str) -> Conclusion:
        try:
            return self._items[conclusion_id]
        except KeyError:
            raise NotFoundError(f"结论不存在: {conclusion_id}")

    def list_for_line(self, line_id: str) -> List[Conclusion]:
        return [
            self._items[k] for k in sorted(self._items)
            if self._items[k].line_id == line_id
        ]

    def lineage(self, conclusion_id: str) -> List[Conclusion]:
        """沿 supersede 链回溯：当前结论 -> 它更正的旧版 -> 更旧版。"""
        chain = []
        cur = self._require(conclusion_id)
        seen = set()
        while cur is not None:
            if cur.conclusion_id in seen:
                raise ImmutableError(
                    f"更正关系出现环: {conclusion_id}"
                )
            seen.add(cur.conclusion_id)
            chain.append(cur)
            if cur.superseding_ids:
                cur = self._items[cur.superseding_ids[0]]
            else:
                cur = None
        return chain

    def descendants(self, conclusion_id: str) -> List[Conclusion]:
        """找出所有（直接或间接）更正了该结论的新版，按签发顺序。"""
        self._require(conclusion_id)
        out = []
        for item in self._items.values():
            chain_ids = {c.conclusion_id for c in self.lineage(item.conclusion_id)}
            if conclusion_id in chain_ids and item.conclusion_id != conclusion_id:
                out.append(item)
        out.sort(key=lambda c: (c.issued_at or c.as_of, c.conclusion_id))
        return out
