"""Spot price feeds: Coinbase ticker (fast) and Chainlink via Polymarket RTDS
(the price these markets actually resolve on). Both are public, read-only."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

import aiohttp

from .config import ChainlinkConfig, SpotConfig
from .util import TimeSeries, parse_iso
from .ws import ReconnectingWS

log = logging.getLogger(__name__)


@dataclass
class SpotTick:
    price: float  # the price the model uses (mid or last per config)
    bid: float | None
    ask: float | None
    last: float
    exch_ts: float  # exchange timestamp, epoch seconds
    recv_ts: float  # local receive time, epoch seconds


class CoinbaseFeed(ReconnectingWS):
    """Coinbase Exchange public `ticker` channel (no auth)."""

    name = "coinbase"

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        cfg: SpotConfig,
        on_tick: Callable[[SpotTick], None] | None = None,
    ) -> None:
        super().__init__(session, url, stale_s=cfg.stale_s, protocol_heartbeat_s=20.0)
        self.cfg = cfg
        self.on_tick = on_tick
        self.last: SpotTick | None = None
        self.history = TimeSeries(cfg.history_s)  # keyed by exchange time
        self._last_seq = -1

    async def on_open(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._last_seq = -1
        await ws.send_json(
            {"type": "subscribe", "product_ids": [self.cfg.product_id], "channels": ["ticker"]}
        )

    def on_text(self, text: str, recv_ts: float) -> None:
        m = json.loads(text)
        typ = m.get("type")
        if typ == "error":
            log.error("coinbase: error message: %s", m)
            return
        if typ != "ticker" or m.get("product_id") != self.cfg.product_id:
            return
        seq = int(m.get("sequence") or 0)
        if seq and seq <= self._last_seq:
            return  # stale / duplicate
        self._last_seq = seq
        last = float(m["price"])
        bid = float(m["best_bid"]) if m.get("best_bid") else None
        ask = float(m["best_ask"]) if m.get("best_ask") else None
        exch_ts = parse_iso(m["time"]) if m.get("time") else recv_ts
        if self.cfg.price_source == "mid" and bid and ask and ask >= bid:
            px = (bid + ask) / 2.0
        else:
            px = last
        tick = SpotTick(price=px, bid=bid, ask=ask, last=last, exch_ts=exch_ts, recv_ts=recv_ts)
        self.last = tick
        self.history.append(exch_ts, px)
        if self.on_tick is not None:
            self.on_tick(tick)

    def price_at(self, ts: float) -> float | None:
        s = self.history.asof(ts)
        return s[1] if s else None


@dataclass
class OracleTick:
    value: float
    obs_ts: float  # Chainlink observation timestamp, epoch seconds
    recv_ts: float


class ChainlinkFeed(ReconnectingWS):
    """Chainlink BTC/USD stream relayed by Polymarket's RTDS websocket."""

    name = "chainlink"

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        cfg: ChainlinkConfig,
        history_s: float,
        on_tick: Callable[[OracleTick], None] | None = None,
    ) -> None:
        super().__init__(
            session,
            url,
            ping_text="PING",
            ping_interval_s=cfg.ping_interval_s,
            stale_s=cfg.stale_s,
        )
        self.cfg = cfg
        self.on_tick = on_tick
        self.last: OracleTick | None = None
        self.history = TimeSeries(history_s)  # keyed by observation time

    async def on_open(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        # RTDS expects `filters` as a JSON *string*, not an object.
        await ws.send_json(
            {
                "action": "subscribe",
                "subscriptions": [
                    {
                        "topic": self.cfg.topic,
                        "type": "*",
                        "filters": json.dumps({"symbol": self.cfg.symbol}, separators=(",", ":")),
                    }
                ],
            }
        )

    def on_text(self, text: str, recv_ts: float) -> None:
        m = json.loads(text)
        if not isinstance(m, dict) or m.get("topic") != self.cfg.topic:
            return
        payload = m.get("payload") or {}
        symbol = str(payload.get("symbol", "")).lower()
        if symbol and symbol != self.cfg.symbol:
            return
        # Either a single update or a batch (e.g. a backfill on subscribe).
        points = payload.get("data") if isinstance(payload.get("data"), list) else [payload]
        newest: OracleTick | None = None
        for p in points:
            if "value" not in p or "timestamp" not in p:
                continue
            ts = float(p["timestamp"])
            if ts > 1e11:  # milliseconds
                ts /= 1000.0
            tick = OracleTick(value=float(p["value"]), obs_ts=ts, recv_ts=recv_ts)
            self.history.append(tick.obs_ts, tick.value)
            if newest is None or tick.obs_ts >= newest.obs_ts:
                newest = tick
            if self.on_tick is not None:
                self.on_tick(tick)
        if newest is not None and (self.last is None or newest.obs_ts >= self.last.obs_ts):
            self.last = newest

    def boundary_price(self, boundary_ts: float) -> tuple[float, float] | None:
        """Price the market uses for a window boundary: the tick stamped exactly
        at the boundary if present, else the first tick after it (if not too
        late). Returns (obs_ts, value) or None if not (yet) available."""
        s = self.history.first_at_or_after(boundary_ts)
        if s is None:
            return None
        first = self.history.first()
        if first is not None and first[0] > boundary_ts:
            return None  # history starts after the boundary: we joined late
        if s[0] - boundary_ts > self.cfg.boundary_max_delay_s:
            return None
        return s
