"""Polymarket CLOB market-channel websocket: keeps live local order books."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable

import aiohttp

from .book import OrderBook
from .config import PolymarketWSConfig
from .ws import ReconnectingWS

log = logging.getLogger(__name__)


def _ts(v: object, fallback: float) -> float:
    try:
        t = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return t / 1000.0 if t > 1e11 else t


@dataclass
class TradePrint:
    asset_id: str
    price: float
    size: float
    side: str
    fee_rate_bps: str | None
    exch_ts: float


class MarketChannel(ReconnectingWS):
    name = "polymarket"

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        cfg: PolymarketWSConfig,
        on_book: Callable[[str], None] | None = None,
        on_resolved: Callable[[dict], None] | None = None,
    ) -> None:
        super().__init__(
            session, url, ping_text="PING", ping_interval_s=cfg.ping_interval_s, stale_s=cfg.stale_s
        )
        self.cfg = cfg
        self.books: dict[str, OrderBook] = {}
        self.last_trade: dict[str, TradePrint] = {}
        self.on_book = on_book
        self.on_resolved = on_resolved
        self.resyncs = 0
        self._resync_pending: set[str] = set()

    # --- subscription management -----------------------------------------
    def wants_connection(self) -> bool:
        return bool(self.books)

    async def subscribe(self, asset_ids: list[str]) -> None:
        new = [a for a in asset_ids if a not in self.books]
        for a in new:
            self.books[a] = OrderBook(a)
        if new and self.connected:
            await self.send_json({"assets_ids": new, "operation": "subscribe", "custom_feature_enabled": True})

    async def unsubscribe(self, asset_ids: list[str]) -> None:
        gone = [a for a in asset_ids if a in self.books]
        for a in gone:
            del self.books[a]
            self.last_trade.pop(a, None)
        if gone and self.connected:
            await self.send_json({"assets_ids": gone, "operation": "unsubscribe"})

    async def on_open(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        for b in self.books.values():
            b.reset()
        self._resync_pending.clear()
        await ws.send_json(
            {"assets_ids": list(self.books), "type": "market", "custom_feature_enabled": True}
        )

    def on_disconnect(self) -> None:
        for b in self.books.values():
            b.reset()

    async def maintain(self) -> None:
        """Resubscribe any asset whose book has disagreed with the server's
        reported top of book for too long (fresh snapshot follows)."""
        now = time.time()
        for a, b in list(self.books.items()):
            if b.ready and b.mismatch_since is not None and now - b.mismatch_since > self.cfg.desync_resync_s:
                log.warning(
                    "polymarket: book desync on %s…%s (local %s/%s vs server %s/%s), resyncing",
                    a[:6], a[-4:], b.best_bid(), b.best_ask(), b.server_best_bid, b.server_best_ask,
                )
                b.reset()
                self.resyncs += 1
                if self.connected:
                    await self.send_json({"assets_ids": [a], "operation": "unsubscribe"})
                    await self.send_json(
                        {"assets_ids": [a], "operation": "subscribe", "custom_feature_enabled": True}
                    )

    # --- message handling ---------------------------------------------------
    def on_text(self, text: str, recv_ts: float) -> None:
        data = json.loads(text)
        msgs = data if isinstance(data, list) else [data]
        for m in msgs:
            if isinstance(m, dict):
                self._handle(m, recv_ts)

    def _handle(self, m: dict, recv_ts: float) -> None:
        et = m.get("event_type")
        if et == "book":
            b = self.books.get(m.get("asset_id", ""))
            if b is None:
                return
            b.apply_snapshot(
                m.get("bids") or m.get("buys") or [],
                m.get("asks") or m.get("sells") or [],
                _ts(m.get("timestamp"), recv_ts),
                recv_ts,
                m.get("hash"),
            )
            if self.on_book:
                self.on_book(b.asset_id)
        elif et == "price_change":
            exch_ts = _ts(m.get("timestamp"), recv_ts)
            changes = m.get("price_changes")
            if changes is None:  # older payload shape
                changes = [dict(c, asset_id=m.get("asset_id")) for c in m.get("changes") or []]
            touched: set[str] = set()
            for c in changes:
                b = self.books.get(c.get("asset_id", ""))
                if b is None or not b.ready:
                    continue
                b.apply_level(c["side"], c["price"], c["size"], exch_ts, recv_ts)
                touched.add(b.asset_id)
                if "best_bid" in c or "best_ask" in c:
                    b.check_top(c.get("best_bid"), c.get("best_ask"), recv_ts)
            if self.on_book:
                for a in touched:
                    self.on_book(a)
        elif et == "best_bid_ask":
            b = self.books.get(m.get("asset_id", ""))
            if b is not None and b.ready:
                b.check_top(m.get("best_bid"), m.get("best_ask"), recv_ts)
        elif et == "tick_size_change":
            b = self.books.get(m.get("asset_id", ""))
            if b is not None:
                b.tick_size = float(m["new_tick_size"])
                log.info("polymarket: tick size %s -> %s on %s", m.get("old_tick_size"), m["new_tick_size"], b.asset_id[:8])
        elif et == "last_trade_price":
            a = m.get("asset_id", "")
            if a in self.books:
                self.last_trade[a] = TradePrint(
                    asset_id=a,
                    price=float(m.get("price", 0)),
                    size=float(m.get("size", 0)),
                    side=str(m.get("side", "")),
                    fee_rate_bps=m.get("fee_rate_bps"),
                    exch_ts=_ts(m.get("timestamp"), recv_ts),
                )
        elif et == "market_resolved":
            if self.on_resolved:
                self.on_resolved(m)
