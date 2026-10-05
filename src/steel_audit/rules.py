"""核算规则版本参数解析与校验。

规则参数（JSON）字段：
- indicators            必填，该规则要求覆盖的监测指标列表（用于证据缺口判定）
- method                measured（实测加总，默认）| factor（产量 × 排放因子）
- emission_indicator    排放指标名，默认取 indicators[0]
- output_indicator      产量指标名，默认 "output"
- emission_factor       factor 法必填，排放因子
- intensity_denominator 强度分母：operating_hours（默认）| output
- min_coverage_ratio    证据充分性阈值，默认 0.9
- late_grace_hours      迟到宽限（小时），默认 24，用于标记迟到样本
- intensity_limit       可选，强度限值，超限在结论中标记 exceedance
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from .errors import ValidationError

METHOD_MEASURED = "measured"
METHOD_FACTOR = "factor"
METHODS = (METHOD_MEASURED, METHOD_FACTOR)

DENOM_OPERATING_HOURS = "operating_hours"
DENOM_OUTPUT = "output"
DENOMINATORS = (DENOM_OPERATING_HOURS, DENOM_OUTPUT)


def _to_decimal(value, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValidationError(f"规则参数 {field} 不是有效数值: {value!r}")


@dataclass(frozen=True)
class RuleParams:
    indicators: tuple
    method: str
    emission_indicator: str
    output_indicator: str
    emission_factor: Optional[Decimal]
    intensity_denominator: str
    min_coverage_ratio: Decimal
    late_grace_hours: int
    intensity_limit: Optional[Decimal]

    def to_dict(self) -> dict:
        return {
            "indicators": list(self.indicators),
            "method": self.method,
            "emission_indicator": self.emission_indicator,
            "output_indicator": self.output_indicator,
            "emission_factor": str(self.emission_factor) if self.emission_factor is not None else None,
            "intensity_denominator": self.intensity_denominator,
            "min_coverage_ratio": str(self.min_coverage_ratio),
            "late_grace_hours": self.late_grace_hours,
            "intensity_limit": str(self.intensity_limit) if self.intensity_limit is not None else None,
        }


def parse_params(raw) -> RuleParams:
    if not isinstance(raw, dict):
        raise ValidationError("规则参数须为 JSON 对象")
    indicators = raw.get("indicators")
    if not isinstance(indicators, list) or not indicators or not all(
        isinstance(i, str) and i.strip() for i in indicators
    ):
        raise ValidationError("规则参数 indicators 须为非空字符串数组")
    method = raw.get("method", METHOD_MEASURED)
    if method not in METHODS:
        raise ValidationError(f"未知核算方法 method={method!r}，支持 {METHODS}")
    emission_indicator = raw.get("emission_indicator") or indicators[0]
    output_indicator = raw.get("output_indicator", "output")
    factor_raw = raw.get("emission_factor")
    emission_factor = _to_decimal(factor_raw, "emission_factor") if factor_raw is not None else None
    if method == METHOD_FACTOR and emission_factor is None:
        raise ValidationError("factor 核算方法必须提供 emission_factor")
    denominator = raw.get("intensity_denominator", DENOM_OPERATING_HOURS)
    if denominator not in DENOMINATORS:
        raise ValidationError(f"未知强度分母 intensity_denominator={denominator!r}，支持 {DENOMINATORS}")
    min_coverage = _to_decimal(raw.get("min_coverage_ratio", "0.9"), "min_coverage_ratio")
    if not (Decimal("0") <= min_coverage <= Decimal("1")):
        raise ValidationError("min_coverage_ratio 须在 [0, 1] 区间")
    grace = raw.get("late_grace_hours", 24)
    if not isinstance(grace, int) or grace < 0:
        raise ValidationError("late_grace_hours 须为非负整数")
    limit_raw = raw.get("intensity_limit")
    intensity_limit = _to_decimal(limit_raw, "intensity_limit") if limit_raw is not None else None
    return RuleParams(
        indicators=tuple(indicators),
        method=method,
        emission_indicator=emission_indicator,
        output_indicator=output_indicator,
        emission_factor=emission_factor,
        intensity_denominator=denominator,
        min_coverage_ratio=min_coverage,
        late_grace_hours=grace,
        intensity_limit=intensity_limit,
    )
