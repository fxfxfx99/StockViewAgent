"""A 股行情的日历时间统一按上海时区解释，与运行主机的 TZ 无关。"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def market_timestamp(value: datetime) -> int:
    """将无时区的行情时间解释为上海时间；已有时区的值保留其实际时刻。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=SHANGHAI_TZ)
    return int(value.timestamp())


def market_date(timestamp: float) -> date:
    return datetime.fromtimestamp(timestamp, SHANGHAI_TZ).date()
