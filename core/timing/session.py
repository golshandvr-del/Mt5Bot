"""
Session / time-context detection (Phase 5, user-update-request).

Given a single bar timestamp (a POSIX epoch integer, as stored in OHLCV.time),
this module answers "what is the time context of this bar?":

  - which FX trading SESSIONS are active (Sydney / Tokyo / London / New York),
  - whether the bar falls inside a well-known OVERLAP (London-NewYork,
    Tokyo-London), which are historically the most active windows,
  - the DAY OF WEEK (0 = Monday .. 6 = Sunday),
  - the HOUR of day and a coarse HOUR BUCKET,
  - the MONTH, QUARTER and a simple SEASON label.

Design notes
------------
- Pure Python standard library only (datetime + timezone). No pandas, no pytz,
  no third-party tz database, so it runs on a minimal Windows 7 Python install.
- MT5 broker times are usually NOT UTC (many brokers use a server time near
  GMT+2/+3). Rather than guess, we let the config specify how to interpret the
  incoming bar timestamp:
      timing.timestamp_is_utc     (bool)  - True if OHLCV.time is already UTC.
      timing.utc_offset_hours     (float) - offset to ADD to the raw timestamp
                                            to convert it to UTC when the feed
                                            is in broker/local time.
  Session windows below are expressed in UTC, matching the common FX convention
  (approximate, DST is intentionally ignored to stay dependency-free and
  because the bot LEARNS the real edge empirically anyway).
- Session hour ranges are overridable from config (`timing.sessions`) so a user
  can tune them for their broker without touching code.

All text is standard ASCII English only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple


# Canonical session names (order matters for stable bucket keys).
SESSION_NAMES: List[str] = [
    "sydney",
    "tokyo",
    "london",
    "newyork",
    "london_newyork_overlap",
    "tokyo_london_overlap",
    "offhours",
]

# Human-friendly day names, index 0 = Monday (matches datetime.weekday()).
DAY_NAMES: List[str] = [
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
]

# Default UTC session windows as [start_hour, end_hour) in 24h UTC.
# These are the common approximate FX session hours. They can wrap past
# midnight (start > end) and that case is handled below.
_DEFAULT_SESSIONS: Dict[str, Tuple[int, int]] = {
    # Sydney ~ 21:00-06:00 UTC (wraps midnight).
    "sydney": (21, 6),
    # Tokyo ~ 00:00-09:00 UTC.
    "tokyo": (0, 9),
    # London ~ 07:00-16:00 UTC.
    "london": (7, 16),
    # New York ~ 12:00-21:00 UTC.
    "newyork": (12, 21),
}

# Season labels by month (northern-hemisphere meteorological seasons). This is
# only a convenient label; the bot still learns the real per-month edge itself.
_MONTH_TO_SEASON: Dict[int, str] = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "autumn", 10: "autumn", 11: "autumn",
}


def _hour_in_window(hour: int, start: int, end: int) -> bool:
    """
    True if `hour` (0..23) is inside [start, end) on a 24h clock, supporting
    windows that wrap past midnight (start > end), e.g. Sydney 21->6.
    """
    start = int(start) % 24
    end = int(end) % 24
    if start == end:
        # Degenerate window: treat as "always" only if a full-day was intended;
        # here we treat start==end as an empty window to avoid surprises.
        return False
    if start < end:
        return start <= hour < end
    # Wrapping window (e.g. 21..6): active if hour >= start OR hour < end.
    return hour >= start or hour < end


class TimeContext(object):
    """
    Immutable-ish description of the time context of a single bar.

    Attributes
    ----------
    dt_utc        : datetime in UTC (timezone-aware).
    sessions      : list of active base session names among
                    ["sydney","tokyo","london","newyork"] (may be empty).
    session_label : the single most descriptive session bucket, one of
                    SESSION_NAMES (prefers an overlap, else the "primary"
                    session, else "offhours").
    day_of_week   : int 0..6 (Monday..Sunday).
    day_name      : DAY_NAMES[day_of_week].
    hour          : int 0..23 (UTC).
    hour_bucket   : coarse bucket string, e.g. "h00_03".
    month         : int 1..12.
    quarter       : int 1..4.
    season        : "winter"/"spring"/"summer"/"autumn".
    """

    __slots__ = ("dt_utc", "sessions", "session_label", "day_of_week",
                 "day_name", "hour", "hour_bucket", "month", "quarter",
                 "season", "is_weekend")

    def __init__(self, dt_utc: datetime, sessions: List[str],
                 session_label: str, day_of_week: int, hour: int,
                 month: int, quarter: int, season: str):
        self.dt_utc = dt_utc
        self.sessions = sessions
        self.session_label = session_label
        self.day_of_week = int(day_of_week)
        self.day_name = DAY_NAMES[self.day_of_week % 7]
        self.hour = int(hour)
        self.hour_bucket = "h%02d_%02d" % (
            (self.hour // 4) * 4, ((self.hour // 4) * 4 + 3),
        )
        self.month = int(month)
        self.quarter = int(quarter)
        self.season = season
        self.is_weekend = self.day_of_week >= 5

    # Bucket accessors used by the learning/aggregation layer. Each returns the
    # (bucket_type, bucket_value) pairs that describe this bar. Keeping the type
    # names centralized here avoids typos across modules.
    def buckets(self) -> List[Tuple[str, str]]:
        """Return all (bucket_type, bucket_value) pairs for this context."""
        return [
            ("session", self.session_label),
            ("day", self.day_name),
            ("hour", self.hour_bucket),
            ("month", "m%02d" % self.month),
            ("quarter", "q%d" % self.quarter),
            ("season", self.season),
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "utc": self.dt_utc.strftime("%Y-%m-%d %H:%M:%S"),
            "sessions": list(self.sessions),
            "session_label": self.session_label,
            "day_of_week": self.day_of_week,
            "day_name": self.day_name,
            "hour": self.hour,
            "hour_bucket": self.hour_bucket,
            "month": self.month,
            "quarter": self.quarter,
            "season": self.season,
            "is_weekend": self.is_weekend,
        }


class SessionCalendar(object):
    """
    Converts bar timestamps into TimeContext objects using config-driven session
    windows and timestamp interpretation.

    Config (all optional, sensible defaults):
      timing.timestamp_is_utc  : bool  (default True)
      timing.utc_offset_hours  : float (default 0.0) added to raw ts -> UTC
      timing.sessions          : map name -> [start_hour, end_hour] in UTC
    """

    def __init__(self, cfg: Optional[Any] = None):
        self.cfg = cfg
        self.timestamp_is_utc = True
        self.utc_offset_hours = 0.0
        self.sessions: Dict[str, Tuple[int, int]] = dict(_DEFAULT_SESSIONS)

        if cfg is not None and hasattr(cfg, "get_path"):
            self.timestamp_is_utc = bool(
                cfg.get_path("timing.timestamp_is_utc", True)
            )
            self.utc_offset_hours = float(
                cfg.get_path("timing.utc_offset_hours", 0.0)
            )
            cfg_sessions = cfg.get_path("timing.sessions", None)
            if cfg_sessions and hasattr(cfg_sessions, "items"):
                for name, window in cfg_sessions.items():
                    try:
                        # window may be a list/tuple [start, end].
                        start = int(window[0])
                        end = int(window[1])
                        self.sessions[str(name)] = (start, end)
                    except Exception:
                        # Ignore malformed entries; keep default.
                        continue

    # ------------------------------------------------------------------ #
    def _to_utc_datetime(self, ts: Any) -> Optional[datetime]:
        """
        Convert a raw bar timestamp to a timezone-aware UTC datetime.

        Accepts an int/float POSIX epoch (seconds) or a datetime. Returns None
        on any failure so callers can degrade to neutral.
        """
        try:
            if isinstance(ts, datetime):
                dt = ts
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)
            else:
                # Interpret numeric epoch as UTC seconds.
                dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            if not self.timestamp_is_utc and self.utc_offset_hours:
                # The feed was in broker/local time; add the offset to reach UTC.
                dt = dt + timedelta(hours=self.utc_offset_hours)
            return dt
        except Exception:
            return None

    def _active_sessions(self, hour: int) -> List[str]:
        """Return base session names active at this UTC hour."""
        active: List[str] = []
        for name in ("sydney", "tokyo", "london", "newyork"):
            window = self.sessions.get(name)
            if not window:
                continue
            if _hour_in_window(hour, window[0], window[1]):
                active.append(name)
        return active

    @staticmethod
    def _session_label(active: List[str]) -> str:
        """
        Choose the single most descriptive session bucket from the active set.

        Preference order:
          - London + New York both active -> "london_newyork_overlap".
          - Tokyo + London both active     -> "tokyo_london_overlap".
          - else the single "primary" session by liquidity:
                london > newyork > tokyo > sydney.
          - else "offhours".
        """
        s = set(active)
        if "london" in s and "newyork" in s:
            return "london_newyork_overlap"
        if "tokyo" in s and "london" in s:
            return "tokyo_london_overlap"
        for primary in ("london", "newyork", "tokyo", "sydney"):
            if primary in s:
                return primary
        return "offhours"

    # ------------------------------------------------------------------ #
    def context(self, ts: Any) -> Optional[TimeContext]:
        """
        Build the TimeContext for a bar timestamp, or None if it cannot be
        parsed (caller should then treat time context as neutral).
        """
        dt = self._to_utc_datetime(ts)
        if dt is None:
            return None
        hour = dt.hour
        active = self._active_sessions(hour)
        label = self._session_label(active)
        month = dt.month
        quarter = (month - 1) // 3 + 1
        season = _MONTH_TO_SEASON.get(month, "unknown")
        return TimeContext(
            dt_utc=dt,
            sessions=active,
            session_label=label,
            day_of_week=dt.weekday(),
            hour=hour,
            month=month,
            quarter=quarter,
            season=season,
        )

    def context_from_ohlcv(self, ohlcv: Any,
                           index: int = -1) -> Optional[TimeContext]:
        """Convenience: build the TimeContext for one bar of an OHLCV series."""
        try:
            times = getattr(ohlcv, "time", None)
            if not times:
                return None
            return self.context(times[index])
        except Exception:
            return None
