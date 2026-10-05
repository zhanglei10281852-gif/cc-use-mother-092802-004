"""测试公共支撑：内存库 + 可控时钟 + 常用造数函数。"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from steel_audit.service import SteelAuditService  # noqa: E402
from steel_audit.storage import Store  # noqa: E402

UTC = timezone.utc


def dt(text: str) -> datetime:
    """快捷构造 UTC 时间，如 dt('2026-06-01T08:00')。"""
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


class Clock:
    """可推进的时钟，注入服务后测试可显式控制“现在”。"""

    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def set(self, now: datetime) -> None:
        self.now = now


def make_service(now: datetime = None):
    clock = Clock(now or dt("2026-07-01T00:00"))
    return SteelAuditService(Store(":memory:"), now_fn=clock), clock


def make_line(svc, line_id="L1", equipment_from="2026-01-01T00:00",
              equipment_code="BF-1", technology="baseline"):
    """建一条产线 + 一段自 equipment_from 起长期投运的设备区间。"""
    svc.create_line(plant_id="P1", name=f"产线{line_id}", line_id=line_id)
    svc.add_equipment_interval(
        line_id, equipment_code=equipment_code, technology=technology,
        commissioned_from=equipment_from)
    return line_id


def make_measured_rule(svc, code="R-2025", effective_from="2026-01-01T00:00",
                       indicators=("pm",), min_coverage="0", **extra):
    """实测法规则；min_coverage 默认 0，避免无关缺口干扰断言。"""
    params = {"indicators": list(indicators), "method": "measured",
              "min_coverage_ratio": min_coverage}
    params.update(extra)
    return svc.create_rule_version(code=code, effective_from=effective_from,
                                   params=params)


def add_sample(svc, line_id, uid, value, start, end, indicator="pm",
               unit="kg", received_at=None):
    return svc.submit_sample(
        line_id=line_id, agency_uid=uid, indicator=indicator, value=value,
        unit=unit, interval_start=start, interval_end=end,
        received_at=received_at)
