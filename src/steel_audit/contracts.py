"""产线、监测样本和核证版本的契约。

所有时间字段均为**带时区**的 ``datetime``；金额/测量值使用 ``Decimal``，
避免二进制浮点污染排放核算结果。
"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Mapping, Optional


@dataclass(frozen=True)
class ProductionLine:
    plant_id: str
    line_id: str
    capacity_tonnes: Decimal
    commissioned_at: datetime
    #: 改造后切换为新产线时，指向被替代的旧产线
    supersedes_line_id: Optional[str] = None


@dataclass(frozen=True)
class EmissionSample:
    sample_id: str
    line_id: str
    pollutant: str
    measured_at: datetime
    value: Decimal
    unit: str
    withdrawn: bool = False
    #: 样本进入系统的时刻（迟到样本判定依据）
    received_at: Optional[datetime] = None
    #: 跨班次/跨周期的连续监测窗口；为空表示瞬时样本
    covers_from: Optional[datetime] = None
    covers_to: Optional[datetime] = None
    #: 采样来源（监测机构/在线 CEMS 等），用于溯源
    source: Optional[str] = None
    withdrawn_at: Optional[datetime] = None
    withdraw_reason: Optional[str] = None
    #: 内容指纹相同但 sample_id 不同的疑似重复样本，指向先到样本
    suspected_duplicate_of: Optional[str] = None

    def is_window(self) -> bool:
        return self.covers_from is not None and self.covers_to is not None

    def mark_withdrawn(self, at: datetime, reason: str) -> "EmissionSample":
        return replace(self, withdrawn=True, withdrawn_at=at, withdraw_reason=reason)

    def mark_duplicate(self, original_id: str) -> "EmissionSample":
        return replace(self, suspected_duplicate_of=original_id)


@dataclass(frozen=True)
class EquipmentInterval:
    """设备投运区间，半开区间 ``[valid_from, valid_to)``。

    ``valid_to`` 为 None 表示仍在运行；同一产线的区间互不允许重叠
    （改造即新旧设备区间首尾相接）。
    """

    equipment_id: str
    line_id: str
    valid_from: datetime
    valid_to: Optional[datetime] = None
    phase: str = "baseline"
    name: str = ""


@dataclass(frozen=True)
class ShutdownNote:
    """异常停机说明；落在停机区间内的监测片段不参与运行统计。"""

    note_id: str
    line_id: str
    start_at: datetime
    end_at: datetime
    reason: str
    equipment_id: Optional[str] = None


@dataclass(frozen=True)
class AccountingRule:
    """核算规则版本。

    :param limits: 污染物 -> 标准限值（canonical 单位）
    :param canonical_units: 污染物 -> canonical 单位
    :param conversions: 原始单位 -> 换算到 canonical 单位的乘数
    :param min_samples: 每个统计桶该污染物的最少有效样本数
    :param sampling_interval_hours: 标称采样间隔（小时），用于推算应到样本数
    """

    rule_id: str
    version: str
    effective_at: datetime
    limits: Mapping[str, Decimal] = field(default_factory=dict)
    canonical_units: Mapping[str, str] = field(default_factory=dict)
    conversions: Mapping[str, Decimal] = field(default_factory=dict)
    min_samples: int = 1
    sampling_interval_hours: Decimal = Decimal("1")
    method: str = "weighted_average"

    @property
    def key(self) -> str:
        return f"{self.rule_id}@{self.version}"
