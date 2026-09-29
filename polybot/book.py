"""Local copy of a CLOB order book for one outcome token."""

from __future__ import annotations

from dataclasses import dataclass


def _px(p: object) -> float:
    return round(float(p), 6)


@dataclass(frozen=True)
class BookView:
    """Immutable snapshot of one side-sorted book, used for fill simulation."""

    asset_id: str
    bids: tuple[tuple[float, float], ...]  # best (highest) first
    asks: tuple[tuple[float, float], ...]  # best (lowest) first
    exch_ts: float
    recv_ts: float

    @property
    def best_bid(self) -> tuple[float, float] | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> tuple[float, float] | None:
        return self.asks[0] if self.asks else None


class OrderBook:
    def __init__(self, asset_id: str) -> None:
        self.asset_id = asset_id
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.ready = False  # True once a full snapshot has been applied
        self.exch_ts = 0.0
        self.recv_ts = 0.0
        self.hash: str | None = None
        self.tick_size: float | None = None
        # Server-reported top of book, used to detect local desync.
        self.server_best_bid: float | None = None
        self.server_best_ask: float | None = None
        self.mismatch_since: float | None = None
        self.snapshots = 0
        self.updates = 0

    def reset(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.ready = False
        self.mismatch_since = None

    def apply_snapshot(
        self, bids: list[dict], asks: list[dict], exch_ts: float, recv_ts: float, hash_: str | None = None
    ) -> None:
        self.bids = {_px(l["price"]): float(l["size"]) for l in bids if float(l["size"]) > 0}
        self.asks = {_px(l["price"]): float(l["size"]) for l in asks if float(l["size"]) > 0}
        self.ready = True
        self.exch_ts = exch_ts
        self.recv_ts = recv_ts
        self.hash = hash_
        self.mismatch_since = None
        self.snapshots += 1

    def apply_level(self, side: str, price: object, size: object, exch_ts: float, recv_ts: float) -> None:
        """Set the aggregate size at one price level. side BUY = bids, SELL = asks."""
        book = self.bids if side.upper() == "BUY" else self.asks
        p = _px(price)
        s = float(size)
        if s <= 0:
            book.pop(p, None)
        else:
            book[p] = s
        self.exch_ts = max(self.exch_ts, exch_ts)
        self.recv_ts = recv_ts
        self.updates += 1

    def best_bid(self) -> tuple[float, float] | None:
        if not self.bids:
            return None
        p = max(self.bids)
        return p, self.bids[p]

    def best_ask(self) -> tuple[float, float] | None:
        if not self.asks:
            return None
        p = min(self.asks)
        return p, self.asks[p]

    def check_top(self, server_bid: object, server_ask: object, now: float) -> bool:
        """Compare local top of book with the server's; track how long they disagree.
        Returns True if consistent."""
        sb = _px(server_bid) if server_bid not in (None, "") else None
        sa = _px(server_ask) if server_ask not in (None, "") else None
        self.server_best_bid, self.server_best_ask = sb, sa
        lb = self.best_bid()
        la = self.best_ask()
        lbp = lb[0] if lb else None
        lap = la[0] if la else None
        # Server reports an empty side as 0 (bid) or 1 (ask).
        ok = (lbp == sb or (lbp is None and sb in (None, 0.0))) and (
            lap == sa or (lap is None and sa in (None, 1.0))
        )
        if ok:
            self.mismatch_since = None
        elif self.mismatch_since is None:
            self.mismatch_since = now
        return ok

    def view(self) -> BookView:
        return BookView(
            asset_id=self.asset_id,
            bids=tuple(sorted(self.bids.items(), key=lambda kv: -kv[0])),
            asks=tuple(sorted(self.asks.items(), key=lambda kv: kv[0])),
            exch_ts=self.exch_ts,
            recv_ts=self.recv_ts,
        )
