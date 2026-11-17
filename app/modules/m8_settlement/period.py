"""业务月份：按提货时刻在 Asia/Shanghai 的自然月归属。

月结口径必须与承运商账单一致：同一票只在提货月出现一次，跨时区写入的
UTC 值先换算到展示时区再取年月，避免月初/月末的票落错月份。
"""

from __future__ import annotations

import re
from datetime import datetime

from app import config
from app.modules.m2_rates.version import parse_utc

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def valid(month: str) -> bool:
    return bool(MONTH_RE.match((month or "").strip()))


def month_of(pickup_utc: str) -> str:
    """UTC ISO 文本 → 展示时区的 YYYY-MM；解析失败返回空串。"""
    stamp = parse_utc(pickup_utc)
    if stamp is None:
        return ""
    return stamp.astimezone(config.CN_TZ).strftime("%Y-%m")


def local_stamp(pickup_utc: str) -> str:
    stamp = parse_utc(pickup_utc)
    if stamp is None:
        return ""
    return stamp.astimezone(config.CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def from_datetime(stamp: datetime) -> str:
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=config.CN_TZ)
    return stamp.astimezone(config.CN_TZ).strftime("%Y-%m")
