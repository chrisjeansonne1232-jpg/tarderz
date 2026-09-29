"""Local stand-in for Polymarket (Gamma, CLOB REST, market websocket, RTDS)
and Coinbase (ticker websocket, candles), driven by one synthetic BTC path.

Payload shapes follow Polymarket's docs and official SDKs. Used by the
integration test and for offline demos:

    python -m tests.fake_exchange --port 8765
    python -m polybot --config tests/fake_config.toml watch
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import time
from datetime import datetime, timezone

from aiohttp import WSMsgType, web

DESCRIPTION = (
    'This market will resolve to "Up" if the Bitcoin price at the end of the time range specified in the '
    'title is greater than or equal to the price at the beginning of that range. Otherwise, it will resolve '
    'to "Down".\nThe resolution source for this market is information from Chainlink, specifically the '
    "BTC/USD data stream available at https://data.chain.link/streams/btc-usd.\nPlease note that this market "
    "is about the price according to Chainlink data stream BTC/USD, not according to other sources or spot markets."
)


def iso(ts: float, micros: bool = False) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ" if micros else "%Y-%m-%dT%H:%M:%SZ")


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


class FakeExchange:
    def __init__(
        self,
        series: dict[str, int],
        seed: int = 7,
        sigma_annual: float = 0.6,
        basis: float = -3.0,
        resolve_delay_s: float = 3.0,
        drop_every_s: float | None = None,
    ) -> None:
        self.series = series
        self.rng = random.Random(seed)
        self.price = 64000.0
        self.sigma_step = sigma_annual / math.sqrt(365 * 86400) * math.sqrt(0.1)
        self.basis = basis
        self.resolve_delay_s = resolve_delay_s
        self.drop_every_s = drop_every_s
        self.oracle: dict[int, float] = {}
        self.markets: dict[str, dict] = {}  # market id -> info
        self.by_cid: dict[str, dict] = {}
        self.by_token: dict[str, tuple[dict, str]] = {}
        self.cb_seq = 0
        self.stale_fair: dict[str, float] = {}  # lagged fair value per market -> creates "edges"
        self.connections = {"market": 0, "rtds": 0, "coinbase": 0}
        self.paused_until: dict[str, float] = {}  # feed -> time; simulates a silent stall
        self._tasks: list[asyncio.Task] = []

    # --- synthetic world ------------------------------------------------------
    async def _price_engine(self) -> None:
        while True:
            self.price *= math.exp(self.rng.gauss(0.0, self.sigma_step))
            sec = int(time.time())
            if sec not in self.oracle:
                self.oracle[sec] = round(self.price + self.basis + self.rng.gauss(0, 0.3), 2)
            for info in self.markets.values():
                p = self.fair_up(info)
                prev = self.stale_fair.get(info["id"], p)
                self.stale_fair[info["id"]] = prev + 0.15 * (p - prev)  # market reacts slowly
            await asyncio.sleep(0.1)

    def fair_up(self, info: dict) -> float:
        now = time.time()
        if now < info["start"]:
            return 0.5
        s0 = self.oracle.get(int(info["start"]), self.price)
        tau = max(info["end"] - now, 1e-3)
        sig = self.sigma_step / math.sqrt(0.1)
        return min(max(norm_cdf(math.log((self.price + self.basis) / s0) / (sig * math.sqrt(tau))), 0.01), 0.99)

    def register(self, slug: str) -> dict | None:
        prefix, _, ts = slug.rpartition("-")
        if prefix not in self.series or not ts.isdigit():
            return None
        h = hashlib.sha256(slug.encode()).hexdigest()
        mid = str(int(h[:8], 16))
        if mid in self.markets:
            return self.markets[mid]
        start = int(ts)
        info = {
            "id": mid, "slug": slug, "start": start, "end": start + self.series[prefix],
            "cid": "0x" + h, "up": str(int(h[:30], 16)), "down": str(int(h[30:60], 16)),
        }
        self.markets[mid] = info
        self.by_cid[info["cid"]] = info
        self.by_token[info["up"]] = (info, "Up")
        self.by_token[info["down"]] = (info, "Down")
        return info

    def outcome(self, info: dict) -> str | None:
        if time.time() < info["end"] + self.resolve_delay_s:
            return None
        s0, e = self.oracle.get(info["start"]), self.oracle.get(info["end"])
        if s0 is None or e is None:
            return None
        return "Up" if e >= s0 else "Down"

    def book_for(self, token: str) -> tuple[list[dict], list[dict]]:
        info, side = self.by_token[token]
        p_up = self.stale_fair.get(info["id"], 0.5)
        mid = p_up if side == "Up" else 1.0 - p_up
        best_bid = min(max(math.floor(mid * 100) / 100, 0.01), 0.98)
        rng = random.Random(f"{token}-{int(time.time() * 4)}")
        bids = [{"price": f"{best_bid - i / 100:.2f}", "size": f"{rng.randint(5, 200)}"} for i in range(8) if best_bid - i / 100 >= 0.01]
        asks = [{"price": f"{best_bid + (i + 1) / 100:.2f}", "size": f"{rng.randint(5, 200)}"} for i in range(8) if best_bid + (i + 1) / 100 <= 0.99]
        return bids, asks

    # --- Gamma / CLOB / Coinbase REST ---------------------------------------------
    def gamma_market(self, info: dict) -> dict:
        outcome = self.outcome(info)
        if outcome:
            prices = ["1", "0"] if outcome == "Up" else ["0", "1"]
        else:
            p = self.stale_fair.get(info["id"], 0.5)
            prices = [f"{p:.3f}", f"{1 - p:.3f}"]
        return {
            "id": info["id"], "question": f"Bitcoin Up or Down - fake {info['slug']}", "conditionId": info["cid"],
            "slug": info["slug"], "resolutionSource": "https://data.chain.link/streams/btc-usd",
            "endDate": iso(info["end"]), "startDate": iso(info["start"] - 86400),
            "outcomes": '["Up", "Down"]', "outcomePrices": json.dumps(prices),
            "active": True, "closed": outcome is not None, "enableOrderBook": True,
            "orderPriceMinTickSize": 0.01, "orderMinSize": 5,
            "clobTokenIds": json.dumps([info["up"], info["down"]]),
            "makerBaseFee": 1000, "takerBaseFee": 1000, "acceptingOrders": outcome is None,
            "eventStartTime": iso(info["start"]), "feesEnabled": True, "feeType": "crypto_fees_v2",
            "feeSchedule": {"exponent": 1, "rate": 0.07, "takerOnly": True, "rebateRate": 0.2},
            "description": DESCRIPTION,
        }

    def gamma_event(self, info: dict) -> dict:
        return {
            "id": "e" + info["id"], "slug": info["slug"], "title": f"Bitcoin Up or Down - fake {info['slug']}",
            "description": DESCRIPTION, "endDate": iso(info["end"]), "markets": [self.gamma_market(info)],
        }

    async def h_events(self, req: web.Request) -> web.Response:
        slug = req.query.get("slug")
        if slug:
            info = self.register(slug)
            return web.json_response([self.gamma_event(info)] if info else [])
        return web.json_response([])

    async def h_market(self, req: web.Request) -> web.Response:
        info = self.markets.get(req.match_info["id"])
        if info is None:
            raise web.HTTPNotFound()
        return web.json_response(self.gamma_market(info))

    async def h_markets(self, req: web.Request) -> web.Response:
        info = self.register(req.query.get("slug", ""))
        return web.json_response([self.gamma_market(info)] if info else [])

    async def h_clob_market(self, req: web.Request) -> web.Response:
        info = self.by_cid.get(req.match_info["cid"])
        if info is None:
            raise web.HTTPNotFound()
        return web.json_response({
            "t": [{"t": info["up"], "o": "Up"}, {"t": info["down"], "o": "Down"}],
            "mts": 0.01, "nr": False, "fd": {"r": 0.07, "e": 1, "to": True},
        })

    async def h_fee_rate(self, req: web.Request) -> web.Response:
        return web.json_response({"base_fee": 1000})

    async def h_book(self, req: web.Request) -> web.Response:
        tok = req.query.get("token_id", "")
        if tok not in self.by_token:
            raise web.HTTPNotFound()
        bids, asks = self.book_for(tok)
        return web.json_response({"asset_id": tok, "bids": bids, "asks": asks})

    async def h_candles(self, req: web.Request) -> web.Response:
        rng = random.Random(1)
        now = int(time.time()) // 60 * 60
        px, rows = self.price, []
        for i in range(300):
            t = now - 60 * i
            o = px * math.exp(rng.gauss(0, self.sigma_step * math.sqrt(600)))
            rows.append([t, min(o, px) - 5, max(o, px) + 5, o, px, 1.0])
            px = o
        return web.json_response(rows)

    def paused(self, feed: str) -> bool:
        return time.time() < self.paused_until.get(feed, 0.0)

    async def h_pause(self, req: web.Request) -> web.Response:
        """Test control: /admin/pause?feed=coinbase|rtds|market&seconds=N"""
        feed = req.query.get("feed", "")
        self.paused_until[feed] = time.time() + float(req.query.get("seconds", "10"))
        return web.json_response({"paused": feed, "until": self.paused_until[feed]})

    # --- websockets ---------------------------------------------------------------
    def _drop_deadline(self) -> float:
        if not self.drop_every_s:
            return math.inf
        return time.time() + self.drop_every_s * self.rng.uniform(0.7, 1.3)

    async def ws_market(self, req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        self.connections["market"] += 1
        subs: set[str] = set()
        last_books: dict[str, tuple[dict, dict]] = {}
        resolved_sent: set[str] = set()
        deadline = self._drop_deadline()

        async def send_books(tokens: list[str]) -> None:
            out = []
            for t in tokens:
                if t in self.by_token:
                    bids, asks = self.book_for(t)
                    last_books[t] = ({l["price"]: l["size"] for l in bids}, {l["price"]: l["size"] for l in asks})
                    info, _ = self.by_token[t]
                    out.append({"event_type": "book", "asset_id": t, "market": info["cid"], "bids": bids,
                                "asks": asks, "timestamp": str(int(time.time() * 1000)), "hash": "0x0"})
            if out:
                await ws.send_str(json.dumps(out))

        async def pump() -> None:
            while not ws.closed:
                await asyncio.sleep(0.25)
                if time.time() > deadline:
                    await ws.close()
                    return
                ts = str(int(time.time() * 1000))
                if self.paused("market"):
                    continue
                for t in list(subs):
                    if t not in last_books or t not in self.by_token:
                        continue
                    info, _ = self.by_token[t]
                    bids, asks = self.book_for(t)
                    nb = {l["price"]: l["size"] for l in bids}
                    na = {l["price"]: l["size"] for l in asks}
                    ob, oa = last_books[t]
                    changes = []
                    for side, old, new in (("BUY", ob, nb), ("SELL", oa, na)):
                        for p in set(old) | set(new):
                            if old.get(p) != new.get(p):
                                changes.append({"asset_id": t, "price": p, "size": new.get(p, "0"), "side": side})
                    last_books[t] = (nb, na)
                    bb = max(nb, key=float) if nb else "0"
                    ba = min(na, key=float) if na else "1"
                    for c in changes:
                        c.update({"hash": "0x0", "best_bid": bb, "best_ask": ba})
                    if changes:
                        await ws.send_str(json.dumps({"event_type": "price_change", "market": info["cid"],
                                                      "price_changes": changes, "timestamp": ts}))
                    out = self.outcome(info)
                    if out and info["id"] not in resolved_sent:
                        resolved_sent.add(info["id"])
                        win = info["up"] if out == "Up" else info["down"]
                        await ws.send_str(json.dumps({"event_type": "market_resolved", "market": info["cid"],
                                                      "winning_asset_id": win, "winning_outcome": out, "timestamp": ts}))

        pump_task = asyncio.create_task(pump())
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                if msg.data == "PING":
                    await ws.send_str("PONG")
                    continue
                m = json.loads(msg.data)
                ids = [str(a) for a in m.get("assets_ids", [])]
                if m.get("operation") == "unsubscribe":
                    subs.difference_update(ids)
                    for a in ids:
                        last_books.pop(a, None)
                else:
                    subs.update(ids)
                    await send_books(ids)
        finally:
            pump_task.cancel()
        return ws

    async def ws_rtds(self, req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        self.connections["rtds"] += 1
        deadline = self._drop_deadline()
        subscribed = asyncio.Event()

        async def pump() -> None:
            await subscribed.wait()
            sent = int(time.time()) - 1
            while not ws.closed:
                await asyncio.sleep(0.1)
                if time.time() > deadline:
                    await ws.close()
                    return
                if self.paused("rtds"):
                    sent = int(time.time()) - 1
                    continue
                # Each whole-second observation is relayed ~0.6 s after it is taken.
                while sent + 1 in self.oracle and time.time() >= sent + 1 + 0.6:
                    sent += 1
                    await ws.send_str(json.dumps({
                        "topic": "crypto_prices_chainlink", "type": "update", "timestamp": int(time.time() * 1000),
                        "payload": {"symbol": "btc/usd", "timestamp": sent * 1000, "value": self.oracle[sent]},
                    }))

        pump_task = asyncio.create_task(pump())
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                if msg.data == "PING":
                    await ws.send_str("PONG")
                    continue
                m = json.loads(msg.data)
                subs = m.get("subscriptions") or []
                if m.get("action") == "subscribe" and any(
                    s.get("topic") == "crypto_prices_chainlink" and isinstance(s.get("filters"), str) for s in subs
                ):
                    subscribed.set()
        finally:
            pump_task.cancel()
        return ws

    async def ws_coinbase(self, req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        self.connections["coinbase"] += 1
        deadline = self._drop_deadline()
        msg = await ws.receive(timeout=5)
        sub = json.loads(msg.data)
        assert sub["type"] == "subscribe" and "ticker" in sub["channels"]
        await ws.send_json({"type": "subscriptions", "channels": [{"name": "ticker", "product_ids": sub["product_ids"]}]})

        async def drain() -> None:  # process the client's close frame
            async for _ in ws:
                pass

        reader = asyncio.create_task(drain())
        while not ws.closed:
            await asyncio.sleep(0.1)
            if time.time() > deadline:
                await ws.close()
                break
            if self.paused("coinbase"):
                continue
            self.cb_seq += 1
            px = self.price
            await ws.send_json({
                "type": "ticker", "sequence": self.cb_seq, "product_id": "BTC-USD",
                "price": f"{px:.2f}", "best_bid": f"{px - 0.005:.2f}", "best_ask": f"{px + 0.005:.2f}",
                "side": "buy", "time": iso(time.time(), micros=True), "trade_id": self.cb_seq, "last_size": "0.001",
            })
        reader.cancel()
        return ws

    # --- server ---------------------------------------------------------------------
    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/gamma/events", self.h_events)
        app.router.add_get("/gamma/markets", self.h_markets)
        app.router.add_get("/gamma/markets/{id}", self.h_market)
        app.router.add_get("/clob/clob-markets/{cid}", self.h_clob_market)
        app.router.add_get("/clob/fee-rate", self.h_fee_rate)
        app.router.add_get("/clob/book", self.h_book)
        app.router.add_get("/coinbase-rest/products/{product}/candles", self.h_candles)
        app.router.add_get("/ws/market", self.ws_market)
        app.router.add_get("/rtds", self.ws_rtds)
        app.router.add_get("/coinbase", self.ws_coinbase)
        app.router.add_get("/admin/pause", self.h_pause)

        async def on_startup(_: web.Application) -> None:
            self._tasks.append(asyncio.create_task(self._price_engine()))

        async def on_cleanup(_: web.Application) -> None:
            for t in self._tasks:
                t.cancel()

        app.on_startup.append(on_startup)
        app.on_cleanup.append(on_cleanup)
        return app

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> tuple[web.AppRunner, int]:
        runner = web.AppRunner(self.app())
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        actual = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return runner, actual


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--drop-every", type=float, default=None, help="drop websocket connections every ~N seconds")
    args = ap.parse_args()
    fx = FakeExchange({"btc-updown-fake60s": 60, "btc-updown-fake30s": 30}, drop_every_s=args.drop_every)
    web.run_app(fx.app(), host="127.0.0.1", port=args.port, shutdown_timeout=0.5)


if __name__ == "__main__":
    main()
