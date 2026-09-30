"""Builds dashboard messages from the running bot. Every value here is read
from the live objects (feeds, books, model, engine); nothing is synthesized."""

from __future__ import annotations

import math
import time
from collections import deque
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from .. import __version__
from ..fairvalue import annualize

if TYPE_CHECKING:
    from ..app import App
    from ..book import OrderBook
    from ..markets import MarketWindow

OFFICIAL_HOSTS = {"gamma-api.polymarket.com"}


def clean(obj: Any) -> Any:
    """Make a structure JSON-safe (no NaN/inf)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    return obj


def series_label(interval_s: int) -> str:
    return f"BTC {interval_s // 60}m Up/Down" if interval_s >= 60 else f"BTC {interval_s}s Up/Down"


def book_state(book: "OrderBook | None", depth: int) -> dict[str, Any] | None:
    if book is None:
        return None
    if not book.ready:
        return {"ready": False}
    v = book.view()
    return {
        "ready": True,
        "bids": [list(x) for x in v.bids[:depth]],
        "asks": [list(x) for x in v.asks[:depth]],
        "recv": book.recv_ts,
        "sync": book.mismatch_since is None,
    }


class _Rate:
    """Messages/second from a monotonically increasing counter."""

    def __init__(self) -> None:
        self.samples: deque[tuple[float, int]] = deque()

    def update(self, now: float, count: int) -> float | None:
        self.samples.append((now, count))
        while len(self.samples) > 2 and now - self.samples[1][0] >= 2.0:
            self.samples.popleft()
        t0, c0 = self.samples[0]
        return (count - c0) / (now - t0) if now - t0 >= 0.5 else None


class LiveSource:
    source = "realtime"

    def __init__(self, app: "App") -> None:
        self.app = app
        self._rates = {k: _Rate() for k in ("coinbase", "chainlink", "clob", "gamma")}
        self._db_size = (0.0, 0)

    # --- static-ish ------------------------------------------------------------------
    def meta(self) -> dict[str, Any]:
        cfg = self.app.cfg
        host = urlparse(cfg.endpoints.gamma).hostname or ""
        return {
            "source": self.source,
            "version": __version__,
            "paper": True,
            "trading": self.app.engine is not None,
            "started_at": self.app.started_at,
            "tz": cfg.dashboard.timezone,
            "stale_after_s": cfg.dashboard.stale_after_s,
            "signal_window_min": cfg.dashboard.signal_window_min,
            "latency_ms": cfg.sim.latency_ms,
            "safety_buffer": cfg.strategy.safety_buffer,
            "slippage_allowance": cfg.strategy.slippage_allowance,
            "min_seconds_remaining": cfg.strategy.min_seconds_remaining,
            "starting_bankroll": cfg.sim.starting_bankroll,
            "max_trade_usd": cfg.sim.max_trade_usd,
            "max_window_usd": cfg.sim.max_window_usd,
            "adverse_move": cfg.sim.adverse_move,
            "order_type": cfg.sim.order_type,
            "fee_buy_in": cfg.fees.buy_fee_in,
            "series": [
                {"name": s.name, "label": series_label(s.interval_s), "interval_s": s.interval_s}
                for s in cfg.markets.series if s.enabled
            ],
            "primary_series": cfg.dashboard.primary_series,
            "test_feed": host not in OFFICIAL_HOSTS,
            "endpoint_host": host,
            "feeds": ["coinbase", "chainlink", "clob", "gamma"] if self.app.chainlink else ["coinbase", "clob", "gamma"],
            # Normal gap between messages; a dot turns amber only beyond this.
            "feed_cadence_s": {"coinbase": 1.0, "chainlink": 1.0, "clob": 1.0,
                               "gamma": cfg.markets.gamma_heartbeat_s or cfg.dashboard.stale_after_s},
        }

    # --- per tick ---------------------------------------------------------------------------
    def _window(self, w: "MarketWindow", now: float) -> dict[str, Any]:
        app = self.app
        fv, why = app.fair_value(w, now)
        up = app.channel.books.get(w.up_token)
        dn = app.channel.books.get(w.down_token)
        strike, src = app.strike(w)
        out: dict[str, Any] = {
            "slug": w.slug, "series": w.series, "start": w.start_ts, "end": w.end_ts,
            "s0": strike, "s0_status": w.s0_status, "s0_src": src,
            # Both start prices, so the dashboard can show whether they agree.
            "ptb": w.ptb_polymarket, "s0_cl": w.s0_chainlink,
            "spot_adj": fv.spot_adj if fv else None,
            "fair_up": fv.p_up if fv else None, "fv_reason": why,
            "rules_ok": w.rules_ok,
            "fee": {"rate": w.fee.rate, "exp": w.fee.exponent, "src": w.fee.source} if w.fee else None,
            "min_order": w.min_order_size,
            "up": book_state(up, 5), "down": book_state(dn, 5),
            "edge_up": None, "edge_down": None,
        }
        if fv and w.fee:
            for key, book, p in (("edge_up", up, fv.p_up), ("edge_down", dn, 1.0 - fv.p_up)):
                ask = book.best_ask() if book is not None and book.ready else None
                if ask:
                    out[key] = p - ask[0] - w.fee.fee_per_share(ask[0])
        return out

    def tick(self) -> dict[str, Any]:
        app = self.app
        now = time.time()
        cb, cl, ch = app.coinbase, app.chainlink, app.channel
        feeds: dict[str, Any] = {
            "coinbase": {"last": cb.last_data_recv or None, "up": cb.connected,
                         "mps": self._rates["coinbase"].update(now, cb.messages)},
            "clob": {"last": ch.last_data_recv or None, "up": ch.connected,
                     "mps": self._rates["clob"].update(now, ch.messages)},
            "gamma": {"last": app.gamma.last_ok or None, "up": app.gamma.last_ok > (app.gamma.last_error or (0, ""))[0],
                      "mps": self._rates["gamma"].update(now, app.gamma.requests)},
        }
        if cl is not None:
            feeds["chainlink"] = {"last": cl.last_data_recv or None, "up": cl.connected,
                                  "mps": self._rates["chainlink"].update(now, cl.messages)}
        t = cb.last
        o = cl.last if cl else None
        est = app.vol.estimate()
        if now - self._db_size[0] > 5.0 and app.db is not None:
            self._db_size = (now, app.db.size_bytes())
        windows = {}
        for tr in app.trackers:
            w = tr.current(now)
            windows[tr.s.name] = self._window(w, now) if w else None
        eng = app.engine
        return {
            "type": "tick",
            "t": now,
            "feeds": feeds,
            "spot": {"price": t.price, "last": t.last, "bid": t.bid, "ask": t.ask, "exch_ts": t.exch_ts,
                     "recv_ts": t.recv_ts} if t else None,
            "oracle": {"value": o.value, "obs_ts": o.obs_ts, "recv_ts": o.recv_ts} if o else None,
            "basis": {"mean": app.basis.mean, "std": app.basis.std} if app.basis.mean is not None else None,
            "sigma": {"annual": annualize(est.sigma), "live_min": est.live_coverage_s / 60.0,
                      "boot": est.used_bootstrap, "floored": est.floored} if est else None,
            "windows": windows,
            "candle": app.candles.current,
            "loop": {"lag_ms": app.loop_lag_ms, "lag_max_ms": app.loop_lag_max_ms},
            "db_bytes": self._db_size[1],
            "uptime_s": now - app.started_at,
            "engine": {"in_flight": len(eng.pending), "cash": eng.cash()} if eng else None,
        }

    def snapshot(self) -> dict[str, Any]:
        app = self.app
        now = time.time()
        eng = app.engine
        window_s = app.cfg.dashboard.signal_window_min * 60.0
        signals = app.db.signals_between(now - window_s, now + 1.0) if app.db is not None else []
        for s in signals:
            s.pop("token", None)
        return clean({
            "type": "snapshot",
            "t": now,
            "meta": self.meta(),
            "tick": self.tick(),
            "stats": eng.stats(now) if eng else None,
            "log": [list(x) for x in app.log_ring],
            "log_counts": dict(app.log_counts),
            "trades": [t.public() for t in eng.trades[-300:]] if eng else [],
            "signals": signals,
            "candles": app.candles.all(),
        })
