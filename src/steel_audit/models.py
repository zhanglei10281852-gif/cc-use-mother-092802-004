"""领域模型：产线、设备投运区间、监测样本、停机说明、规则版本与阶段结论。

与 contracts.py 的关系：contracts 是对外描述核证材料的最初契约（保持兼容），
本模块是后端内部的完整领域模型，时间一律为 UTC（见 timeutil）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from .timeutil import FAR_FUTURE, Interval

# ---- 样本状态 ----
SAMPLE_ACTIVE = "active"        # 有效，参与核算
SAMPLE_DUPLICATE = "duplicate"  # 自然键重复，被确定性排除（先到先采信）
SAMPLE_WITHDRAWN = "withdrawn"  # 已撤回，不再参与新的核算
SAMPLE_STATUSES = (SAMPLE_ACTIVE, SAMPLE_DUPLICATE, SAMPLE_WITHDRAWN)

# ---- 结论状态 ----
CONCLUSION_DRAFT = "draft"    # 草稿：可重算、可废弃，不进入更正链
CONCLUSION_ISSUED = "issued"  # 已签发：不可变，只能被新结论更正引用


@dataclass(frozen=True)
class EquipmentInterval:
    """设备投运区间：某台设备在产线上的投运时间段（改造前后为不同记录）。"""

    equipment_interval_id: str
    line_id: str
    equipment_code: str
    technology: str
    capacity_tph: Decimal
    commissioned_from: datetime
    decommissioned_to: Optional[datetime]  # None 表示仍在运
    created_at: datetime

    @property
    def window(self) -> Interval:
        return (self.commissioned_from, self.decommissioned_to or FAR_FUTURE)


@dataclass(frozen=True)
class Sample:
    """原始监测样本。interval 为采样时段，跨班次/跨周期时按时间比例分摊。"""

    sample_id: str
    agency_uid: str  # 监测机构原始编号（幂等键）
    line_id: str
    indicator: str
    value: Decimal
    unit: str
    interval_start: datetime
    interval_end: datetime
    received_at: datetime  # 送达时刻，迟到判定与数据截止的依据
    status: str = SAMPLE_ACTIVE
    duplicate_of: Optional[str] = None
    withdrawn_at: Optional[datetime] = None
    withdraw_reason: Optional[str] = None

    @property
    def interval(self) -> Interval:
        return (self.interval_start, self.interval_end)

    @property
    def duration_seconds(self) -> int:
        return int((self.interval_end - self.interval_start).total_seconds())


@dataclass(frozen=True)
class ShutdownEvent:
    """异常停机说明。reason 为空视为说明缺失，构成证据缺口。"""

    shutdown_id: str
    line_id: str
    start: datetime
    end: datetime
    reason: Optional[str]
    evidence_ref: Optional[str]
    created_at: datetime

    @property
    def window(self) -> Interval:
        return (self.start, self.end)


@dataclass(frozen=True)
class RuleVersion:
    """核算规则版本。params 为规则参数（JSON），见 rules.RuleParams。"""

    rule_version_id: str
    code: str
    effective_from: datetime
    params: dict
    created_at: datetime


@dataclass(frozen=True)
class Conclusion:
    """阶段结论。签发后不可变；更正通过新结论的 corrects_id 引用旧版。"""

    conclusion_id: str
    line_id: str
    period_type: str
    period_start: datetime
    period_end: datetime
    rule_version_id: str
    data_cutoff: datetime  # 数据截止时刻：仅纳入此前送达的样本
    status: str
    corrects_id: Optional[str]
    totals: dict   # 汇总结果（数值为字符串，保证精度与可复现）
    detail: dict   # 计算明细：分段、分摊、样本清单、被排除样本
    gaps: list     # 证据缺口清单
    explanation: Optional[dict]  # 签发时生成的相对上一版的差异说明
    created_at: datetime
    issued_at: Optional[datetime]
