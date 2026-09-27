"""产线、监测样本和核证版本的契约。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class ProductionLine:
    plant_id: str
    line_id: str
    capacity_tonnes: Decimal
    commissioned_at: datetime


@dataclass(frozen=True)
class EmissionSample:
    sample_id: str
    line_id: str
    pollutant: str
    measured_at: datetime
    value: Decimal
    unit: str
    withdrawn: bool = False
