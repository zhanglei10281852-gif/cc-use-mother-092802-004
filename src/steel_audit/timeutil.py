"""UTC 时间与统计周期工具。

约定（全系统统一，保证计算可复现）：
- 所有时间一律为 UTC，解析后精度截断到秒；
- 统计周期为左闭右开区间 [start, end)；
- 支持的周期类型：shift（8 小时班次，00/08/16 点起）、day、iso_week、month、quarter；
- 周期起点必须与类型对齐，否则拒绝计算（避免同一批数据被切出不同口径）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Tuple

UTC = timezone.utc

# 区间：左闭右开 [start, end)
Interval = Tuple[datetime, datetime]

SHIFT_START_HOURS = (0, 8, 16)
PERIOD_TYPES = ("shift", "day", "iso_week", "month", "quarter")

# 表示“无限远”的哨兵时间（设备未退役等场景）
FAR_FUTURE = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)


def parse_dt(value) -> datetime:
    """把 ISO 8601 字符串或 datetime 规范化为 UTC 秒级时间。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    else:
        raise ValueError(f"无法解析时间: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)


def fmt_dt(dt: datetime) -> str:
    """规范输出形式，如 2026-06-01T00:00:00Z。同一时刻的字符串表示唯一。"""
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate_period(period_type: str, start: datetime) -> None:
    """校验周期类型与起点对齐方式，不合法时抛出 ValueError。"""
    if period_type not in PERIOD_TYPES:
        raise ValueError(f"未知统计周期类型: {period_type!r}，支持 {PERIOD_TYPES}")
    if start.minute or start.second or start.microsecond:
        raise ValueError(f"周期起点必须整点: {fmt_dt(start)}")
    if period_type == "shift":
        if start.hour not in SHIFT_START_HOURS:
            raise ValueError(f"shift 周期须对齐 00/08/16 点: {fmt_dt(start)}")
    elif period_type == "day":
        if start.hour:
            raise ValueError(f"day 周期须对齐 00:00 UTC: {fmt_dt(start)}")
    elif period_type == "iso_week":
        if start.weekday() != 0 or start.hour:
            raise ValueError(f"iso_week 周期须对齐周一 00:00 UTC: {fmt_dt(start)}")
    elif period_type == "month":
        if start.day != 1 or start.hour:
            raise ValueError(f"month 周期须对齐每月 1 日 00:00 UTC: {fmt_dt(start)}")
    elif period_type == "quarter":
        if start.month not in (1, 4, 7, 10) or start.day != 1 or start.hour:
            raise ValueError(f"quarter 周期须对齐季首 00:00 UTC: {fmt_dt(start)}")


def period_end(period_type: str, start: datetime) -> datetime:
    """周期结束时刻（排他）。"""
    validate_period(period_type, start)
    if period_type == "shift":
        return start + timedelta(hours=8)
    if period_type == "day":
        return start + timedelta(days=1)
    if period_type == "iso_week":
        return start + timedelta(days=7)
    if period_type == "month":
        year, month = (start.year, start.month + 1) if start.month < 12 else (start.year + 1, 1)
        return datetime(year, month, 1, tzinfo=UTC)
    # quarter
    if start.month == 10:
        return datetime(start.year + 1, 1, 1, tzinfo=UTC)
    return datetime(start.year, start.month + 3, 1, tzinfo=UTC)


def duration_seconds(interval: Interval) -> int:
    return int((interval[1] - interval[0]).total_seconds())


def clip(a: Interval, window: Interval) -> Optional[Interval]:
    """a 与 window 的交集，无交集返回 None。"""
    lo, hi = max(a[0], window[0]), min(a[1], window[1])
    return (lo, hi) if lo < hi else None


def overlap_seconds(a: Interval, b: Interval) -> int:
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    return max(0, int((hi - lo).total_seconds()))


def union_intervals(intervals: Iterable[Interval]) -> List[Interval]:
    """合并重叠/相邻区间，返回按起点排序的不相交区间列表。"""
    items = sorted((s, e) for s, e in intervals if s < e)
    merged: List[Interval] = []
    for start, end in items:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def subtract_intervals(base: Iterable[Interval], cuts: Iterable[Interval]) -> List[Interval]:
    """从 base 中扣除 cuts，返回按起点排序的剩余区间。"""
    cutters = union_intervals(cuts)
    remaining: List[Interval] = []
    for lo, hi in sorted((s, e) for s, e in base if s < e):
        cursor = lo
        for c_lo, c_hi in cutters:
            if c_hi <= cursor:
                continue
            if c_lo >= hi:
                break
            if c_lo > cursor:
                remaining.append((cursor, min(c_lo, hi)))
            cursor = max(cursor, c_hi)
            if cursor >= hi:
                break
        if cursor < hi:
            remaining.append((cursor, hi))
    return remaining
