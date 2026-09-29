"""1-minute OHLC candles built from Coinbase trade ticks."""

from __future__ import annotations

from collections import deque


class CandleBuilder:
    def __init__(self, keep: int = 360) -> None:
        self.closed: deque[list[float]] = deque(maxlen=keep)  # [t, o, h, l, c, ticks]
        self.current: list[float] | None = None

    def seed(self, rows: list[tuple[float, float, float, float, float]]) -> None:
        """Pre-fill history with exchange candles (e.g. from Coinbase REST)."""
        for t, o, h, l, c in rows:
            if self.current is not None and t >= self.current[0]:
                break
            if not self.closed or t > self.closed[-1][0]:
                self.closed.append([t, o, h, l, c, 0])

    def add(self, ts: float, price: float) -> list[float] | None:
        """Add a trade tick; returns the candle that just closed, if any."""
        m = float(int(ts // 60) * 60)
        cur = self.current
        if cur is None:
            while self.closed and self.closed[-1][0] >= m:
                self.closed.pop()  # a seeded (REST) bar for this minute is replaced by ticks
            self.current = [m, price, price, price, price, 1]
            return None
        if m == cur[0]:
            cur[2] = max(cur[2], price)
            cur[3] = min(cur[3], price)
            cur[4] = price
            cur[5] += 1
            return None
        if m < cur[0]:
            return None  # late tick for an already-closed minute
        self.closed.append(cur)
        self.current = [m, price, price, price, price, 1]
        return cur

    def all(self) -> list[list[float]]:
        out = list(self.closed)
        if self.current is not None:
            out.append(self.current)
        return out
