"""
Time & Timezone Management Service — WB FBS Manager

Provides centralized server time inspection, robust timezone conversions,
seller local time formatting, and persistent daily digest trigger calculation.
"""
from datetime import datetime, date, time, timedelta, timezone
import logging
import time as pytime
from typing import Any, Dict, Optional
import zoneinfo

from sqlalchemy import select, and_
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Europe/Moscow"


def resolve_timezone(tz_name: Optional[str]) -> zoneinfo.ZoneInfo:
    """
    Safely resolve a timezone string into a ZoneInfo instance.
    Falls back gracefully to Europe/Moscow if the timezone name is invalid or empty.
    """
    if not tz_name or not str(tz_name).strip():
        return zoneinfo.ZoneInfo(DEFAULT_TIMEZONE)
    
    clean_tz = str(tz_name).strip()
    try:
        return zoneinfo.ZoneInfo(clean_tz)
    except Exception as exc:
        logger.warning(f"Invalid timezone '{clean_tz}', falling back to '{DEFAULT_TIMEZONE}': {exc}")
        return zoneinfo.ZoneInfo(DEFAULT_TIMEZONE)


def get_server_time_info() -> Dict[str, Any]:
    """
    Inspect the host server's local operating system time and UTC reference.
    Returns structured information about server clock, timezone name, and UTC offset.
    """
    utc_now = datetime.now(timezone.utc)
    local_now = datetime.now().astimezone()
    
    # Calculate offset in seconds and hours
    offset_seconds = local_now.utcoffset().total_seconds() if local_now.utcoffset() else 0.0
    offset_hours = offset_seconds / 3600.0
    offset_sign = "+" if offset_hours >= 0 else "-"
    offset_str = f"UTC{offset_sign}{abs(int(offset_hours)):02d}:{abs(int((offset_seconds % 3600) // 60)):02d}"

    # Detected system timezone name
    sys_tz_name = local_now.tzname() or pytime.tzname[0] or "UTC"

    return {
        "server_utc_now": utc_now.isoformat(),
        "server_local_now": local_now.strftime("%Y-%m-%d %H:%M:%S"),
        "server_timezone": sys_tz_name,
        "utc_offset_seconds": int(offset_seconds),
        "utc_offset_hours": round(offset_hours, 2),
        "utc_offset_str": offset_str,
    }


def get_now_in_timezone(tz_name: Optional[str]) -> datetime:
    """
    Return the current datetime converted from server UTC time into the target timezone.
    Always timezone-aware.
    """
    tz = resolve_timezone(tz_name)
    return datetime.now(timezone.utc).astimezone(tz)


def get_seller_local_time(seller: Any, now_utc: Optional[datetime] = None) -> datetime:
    """
    Get the seller's current local datetime based on their configured digest_timezone.
    """
    tz_str = getattr(seller, "digest_timezone", None) or DEFAULT_TIMEZONE
    tz = resolve_timezone(tz_str)
    
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    
    return now_utc.astimezone(tz)


def format_seller_digest_time(seller: Any, dt: Optional[datetime] = None) -> str:
    """
    Format time for display in messages with the correct local time and timezone label.
    Example: '09:44 (Asia/Krasnoyarsk)'
    """
    tz_str = getattr(seller, "digest_timezone", None) or DEFAULT_TIMEZONE
    local_dt = get_seller_local_time(seller, dt)
    return f"{local_dt.strftime('%H:%M')} ({tz_str})"


def is_seller_digest_due(
    seller: Any,
    now_utc: Optional[datetime] = None,
    db_session: Optional[Session] = None,
    in_memory_sent_tracker: Optional[dict] = None,
    grace_hours: float = 3.0,
) -> bool:
    """
    Determine whether a morning digest is due for the seller right now.

    Algorithm:
    1. Determine seller's current local time based on their configured timezone.
    2. Build the target datetime for today (e.g. today at 08:00 in seller's timezone).
    3. Check if seller_local_time >= target_datetime AND seller_local_time < target_datetime + grace_hours.
    4. Verify that the digest has NOT already been sent today (via in-memory cache and DB AuditLog).
    
    This ensures that when Celery Beat runs on a 30-minute schedule (or any schedule):
    - The digest fires on the first beat cycle at or after the target time.
    - No window is missed even if the beat cycle starts at an arbitrary offset.
    - The digest is sent exactly ONCE per calendar day in the seller's timezone.
    """
    if not getattr(seller, "is_active", True) or not getattr(seller, "digest_enabled", True):
        return False

    seller_id_str = str(getattr(seller, "id", ""))
    if not seller_id_str:
        return False

    tz_str = getattr(seller, "digest_timezone", None) or DEFAULT_TIMEZONE
    seller_tz = resolve_timezone(tz_str)

    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    seller_local_now = now_utc.astimezone(seller_tz)
    local_today_str = seller_local_now.strftime("%Y-%m-%d")

    # Check 1: In-memory tracker
    if in_memory_sent_tracker is not None:
        if in_memory_sent_tracker.get(seller_id_str) == local_today_str:
            return False

    # Check 2: Target time check
    target_hour = int(getattr(seller, "digest_hour", 8) or 8)
    target_minute = int(getattr(seller, "digest_minute", 0) or 0)

    target_dt = datetime(
        year=seller_local_now.year,
        month=seller_local_now.month,
        day=seller_local_now.day,
        hour=target_hour,
        minute=target_minute,
        tzinfo=seller_tz,
    )

    # Not yet reached target time today
    if seller_local_now < target_dt:
        return False

    # Past grace delivery window (e.g. more than 3 hours after target time)
    if seller_local_now >= target_dt + timedelta(hours=grace_hours):
        return False

    # Check 3: Database AuditLog check (survives process / container restarts)
    if db_session is not None:
        try:
            from app.models.audit import AuditLog
            
            # Beginning of local day in UTC
            start_of_day_local = datetime(
                seller_local_now.year, seller_local_now.month, seller_local_now.day,
                0, 0, 0, tzinfo=seller_tz
            )
            start_of_day_utc = start_of_day_local.astimezone(timezone.utc)

            already_sent = db_session.execute(
                select(AuditLog.id).where(
                    and_(
                        AuditLog.seller_id == seller_id_str,
                        AuditLog.action == "MORNING_DIGEST_SENT",
                        AuditLog.created_at >= start_of_day_utc,
                    )
                ).limit(1)
            ).scalar_one_or_none()

            if already_sent:
                if in_memory_sent_tracker is not None:
                    in_memory_sent_tracker[seller_id_str] = local_today_str
                return False
        except Exception as db_err:
            logger.debug(f"Audit log check skipped due to DB error: {db_err}")

    return True
