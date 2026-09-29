"""Small shared helpers: time parsing, backoff, a bounded time series."""

from __future__ import annotations

import asyncio
import random
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone


def parse_iso(s: str) -> float:
    """Parse an ISO-8601 timestamp (e.g. '2026-05-12T11:15:00Z') to epoch seconds."""
    dt = datetime.fromisoformat(s.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def fmt_hms(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


def fmt_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def fmt_mmss(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    """Sleep up to `seconds`; return True if `stop` was set meanwhile."""
    if seconds <= 0:
        return stop.is_set()
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


class Backoff:
    """Exponential backoff with jitter for reconnect loops."""

    def __init__(self, base: float = 1.0, cap: float = 30.0) -> None:
        self.base = base
        self.cap = cap
        self._n = 0

    def next(self) -> float:
        delay = min(self.cap, self.base * (2 ** self._n))
        self._n += 1
        return delay * random.uniform(0.5, 1.0)

    def reset(self) -> None:
        self._n = 0


class TimeSeries:
    """Time-ordered (ts, value) samples, trimmed to a maximum age.

    Supports as-of lookups, which is how boundary prices (window start/end)
    and cross-feed alignment are computed.
    """

    def __init__(self, max_age_s: float) -> None:
        self.max_age_s = max_age_s
        self._ts: list[float] = []
        self._v: list[float] = []
        self._head = 0

    def __len__(self) -> int:
        return len(self._ts) - self._head

    def append(self, ts: float, value: float) -> None:
        if self._ts and ts < self._ts[-1]:
            i = bisect_right(self._ts, ts, lo=self._head)
            self._ts.insert(i, ts)
            self._v.insert(i, value)
        else:
            self._ts.append(ts)
            self._v.append(value)
        cutoff = self._ts[-1] - self.max_age_s
        self._head = bisect_left(self._ts, cutoff, lo=self._head)
        if self._head > 4096 and self._head * 2 > len(self._ts):
            del self._ts[: self._head]
            del self._v[: self._head]
            self._head = 0

    def first(self) -> tuple[float, float] | None:
        if len(self) == 0:
            return None
        return self._ts[self._head], self._v[self._head]

    def last(self) -> tuple[float, float] | None:
        if len(self) == 0:
            return None
        return self._ts[-1], self._v[-1]

    def asof(self, ts: float) -> tuple[float, float] | None:
        """Latest sample with sample_ts <= ts."""
        i = bisect_right(self._ts, ts, lo=self._head) - 1
        if i < self._head:
            return None
        return self._ts[i], self._v[i]

    def first_at_or_after(self, ts: float) -> tuple[float, float] | None:
        i = bisect_left(self._ts, ts, lo=self._head)
        if i >= len(self._ts):
            return None
        return self._ts[i], self._v[i]
