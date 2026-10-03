"""时间与周期工具。

全部采用半开区间 ``[start, end)`` 语义；周期对齐到 epoch（1970-01-01，UTC），
保证任意时区的机器对同一周期请求算出相同的桶边界。
"""

from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator, Tuple

PERIODS = ("hour", "shift", "day", "week", "month")

#: 三班制：00:00-08:00 / 08:00-16:00 / 16:00-24:00（UTC 固定班次）
SHIFT_NAMES = ("night", "morning", "afternoon")
SHIFT_HOURS = 8

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"datetime 必须带时区: {value!r}")
    return value.astimezone(timezone.utc)


def period_bounds(at: datetime, period: str) -> Tuple[datetime, datetime]:
    """返回 ``at`` 所在统计周期的半开边界 ``[start, end)``。"""
    at = as_utc(at)
    if period == "hour":
        start = at.replace(minute=0, second=0, microsecond=0)
        return start, start + timedelta(hours=1)
    if period == "shift":
        idx = at.hour // SHIFT_HOURS
        start = at.replace(hour=idx * SHIFT_HOURS, minute=0, second=0, microsecond=0)
        return start, start + timedelta(hours=SHIFT_HOURS)
    if period == "day":
        start = at.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    if period == "week":
        day = at.replace(hour=0, minute=0, second=0, microsecond=0)
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=7)
    if period == "month":
        start = at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            next_month = start.replace(year=start.year + 1, month=1)
        else:
            next_month = start.replace(month=start.month + 1)
        return start, next_month
    raise ValueError(f"未知统计周期: {period!r}（支持 {PERIODS}）")


def shift_label(at: datetime) -> str:
    at = as_utc(at)
    return f"{at:%Y-%m-%d}/{SHIFT_NAMES[at.hour // SHIFT_HOURS]}"


def overlap_seconds(a_start: datetime, a_end: datetime,
                    b_start: datetime, b_end: datetime) -> float:
    """两个半开区间的重叠秒数（无重叠返回 0）。"""
    lo = max(a_start, b_start)
    hi = min(a_end, b_end)
    return max(0.0, (hi - lo).total_seconds())


def subtract_intervals(
    span: Tuple[datetime, datetime],
    blockers: Iterable[Tuple[datetime, datetime]],
) -> Iterator[Tuple[datetime, datetime]]:
    """从 ``span`` 中扣除若干停机/阻塞区间，返回剩余的半开子区间。"""
    cur_start, cur_end = span
    cuts = sorted(
        (max(b_start, cur_start), min(b_end, cur_end))
        for b_start, b_end in blockers
    )
    for b_start, b_end in cuts:
        if b_end <= cur_start or b_start >= cur_end:
            continue
        if b_start > cur_start:
            yield cur_start, b_start
        cur_start = max(cur_start, b_end)
        if cur_start >= cur_end:
            return
    if cur_start < cur_end:
        yield cur_start, cur_end
