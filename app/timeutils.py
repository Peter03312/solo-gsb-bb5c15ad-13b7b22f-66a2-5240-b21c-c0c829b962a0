"""时间工具：统一使用时区感知时间并以 UTC 存储/比较。"""
from __future__ import annotations

from datetime import datetime, timezone

# 时长输出精度：微秒（秒后 6 位小数）。采样时刻可带微秒，半开区段的实际
# 时长因此可能出现亚毫秒差异（如 1.0004s）——该精度足以呈现影响结论的
# 微秒差异。注意：**舍入仅用于呈现**；是否严格超过阈值、是否存在正时长
# 未知区段等判定一律使用原始时长（datetime 差值），不得先舍入再比较。
DURATION_DIGITS = 6


def round_duration(seconds: float) -> float:
    """把时长（秒）舍入到微秒精度用于输出；不参与任何判定逻辑。"""
    return round(seconds, DURATION_DIGITS)


def to_utc(dt: datetime) -> datetime:
    """把时区感知的 datetime 转为 UTC；拒绝 naive datetime。"""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("时间戳必须带时区信息（例如 2026-10-01T00:00:00+00:00 或 Z）")
    return dt.astimezone(timezone.utc)


def to_iso(dt: datetime) -> str:
    """输出 ISO-8601 UTC 字符串。"""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_stored(value: str) -> datetime:
    """解析库里存的 ISO-8601 字符串。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
