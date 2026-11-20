"""Timezone-aware timestamp helpers built on stdlib zoneinfo.

Every persisted timestamp is an absolute UTC instant rendered as an ISO-8601
string ending in 'Z'. Local wall-clock time is derived only at presentation /
comparison boundaries so cross-region and DST cases stay unambiguous.
"""
from __future__ import annotations

from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 string (with Z or offset) into an aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without timezone offset: {value!r}")
    return dt.astimezone(timezone.utc)


def to_iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def local_dt(dt_utc: datetime, tz_name: str) -> datetime:
    return dt_utc.astimezone(ZoneInfo(tz_name))


def local_iso(dt_utc: datetime, tz_name: str) -> str:
    return local_dt(dt_utc, tz_name).strftime("%Y-%m-%d %H:%M:%S %Z%z")


def local_date(dt_utc: datetime, tz_name: str) -> date:
    return local_dt(dt_utc, tz_name).date()


def utc_offset_minutes(dt_utc: datetime, tz_name: str) -> int:
    off = local_dt(dt_utc, tz_name).utcoffset()
    return int(off.total_seconds() // 60)


def format_offset(offset_min: int) -> str:
    sign = "+" if offset_min >= 0 else "-"
    h, m = divmod(abs(offset_min), 60)
    return f"UTC{sign}{h:02d}:{m:02d}"


def is_valid_tz(tz_name: str) -> bool:
    try:
        ZoneInfo(tz_name)
        return True
    except Exception:
        return False
