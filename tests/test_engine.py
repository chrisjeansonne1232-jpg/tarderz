"""Paper engine: fills must be honest (post-latency book, displayed depth,
limit price, budget, fees) and settlement/statistics must add up."""

import asyncio
import math

import pytest

from polybot.book import OrderBook
from polybot.config import Config
from polybot.db import Database
from polybot.engine import PaperEngine, walk_asks
from polybot.fees import FeeModel
from polybot.markets import MarketWindow
from polybot.metrics import summarize

FEE = FeeModel(rate=0.07, exponent=1, source="test")


def test_walk_asks_respects_limit_depth_budget_and_hidden():
    asks = [(0.50, 10.0), (0.51, 5.0), (0.53, 100.0)]
    fills = walk_asks(asks, max_shares=100, limit_price=0.52, budget=1000, fee=FEE, fee_in_collateral=True)
    assert [(p, s) for p, s, _ in fills] == [(0.50, 10.0), (0.51, 5.0)]  # stops at the limit
    assert fills[0][2] == FEE.match_fee(10, 0.50)
    hidden = walk_asks(asks, max_shares=100, limit_price=0.52, budget=1000, fee=FEE, fee_in_collateral=True,
                       hidden={0.50: 8.0})
    assert [(p, s) for p, s, _ in hidden] == [(0.50, 2.0), (0.51, 5.0)]  # size we already took is gone
    # Budget includes the fee when fees are paid in collateral; sizes floor to 0.01.
    budget = walk_asks([(0.50, 1000.0)], max_shares=1000, limit_price=1, budget=5.0, fee=FEE, fee_in_collateral=True)
    p, s, f = budget[0]
    assert s == math.floor(5.0 / (0.5 + FEE.fee_per_share(0.5)) * 100) / 100
    assert s * p + f <= 5.0 + 1e-9


class StubTracker:
    def __init__(self, w):
        self.w = w

    def current(self, now):
        return self.w if self.w.start_ts <= now < self.w.end_ts else None


class StubFV:
    def __init__(self, p_up):
        self.p_up, self.spot, self.strike, self.sigma, self.spot_adj = p_up, 64000.0, 64000.0, 1e-4, 64000.0


class StubApp:
    """Just enough of App for the engine: books, a window, a fair value."""

    def __init__(self, tmp_path, now, p_up=0.60):
        self.cfg = Config()
        self.cfg.sim.latency_ms = 50
        self.db = Database(str(tmp_path / "e.sqlite"))
        self.w = MarketWindow(
            series="t", slug="btc-updown-t-1", event_id="e", market_id="m", condition_id="c", question="q",
            description="", resolution_source="", start_ts=now - 60, end_ts=now + 240, up_token="UP",
            down_token="DN", tick_size=0.01, min_order_size=5, fees_enabled=True, gamma_fee=(0.07, 1.0),
            rules_ok=True, fee=FEE,
        )
        self.db.upsert_market(self.w)
        self.trackers = [StubTracker(self.w)]
        self.channel = type("C", (), {"books": {"UP": OrderBook("UP"), "DN": OrderBook("DN")}})()
        self.p_up = p_up
        self.log: list[tuple[str, str]] = []
        self.events: list[dict] = []

    def book(self, token, asks, bids=()):
        self.channel.books[token].apply_snapshot(
            [{"price": p, "size": s} for p, s in bids], [{"price": p, "size": s} for p, s in asks], 0, 0)

    def fair_value(self, w, now):
        return StubFV(self.p_up), ""

    def exec_log(self, tag, msg, ref=None, echo=True):
        self.log.append((tag, msg))

    def publish(self, msg):
        self.events.append(msg)


async def _signal_and_wait(eng, app, now, wait=0.12):
    eng.evaluate(now)
    await asyncio.sleep(wait)


def test_signal_fills_against_post_latency_book(tmp_path):
    import time

    async def scenario():
        now = time.time()
        app = StubApp(tmp_path, now, p_up=0.60)
        app.book("UP", [(0.52, 8.0), (0.53, 100.0)])
        app.book("DN", [(0.47, 100.0)])  # fair Down 0.40 < ask: no signal
        eng = PaperEngine(app)
        eng.evaluate(now)
        assert [t for t, _ in app.log] == ["SIG"]
        # The book moves against us during the latency window.
        app.book("UP", [(0.53, 6.0), (0.54, 100.0)])
        await asyncio.sleep(0.12)
        return app, eng

    app, eng = asyncio.run(scenario())
    assert [t for t, _ in app.log] == ["SIG", "FILL"]
    (t,) = eng.trades
    # Filled against the post-delay book: 6 sh @0.53 then 0.54, never the vanished 0.52.
    assert t.levels[0][:2] == [0.53, 6.0] and t.levels[1][0] == 0.54
    assert t.avg_price > 0.52 and t.signal_ask == 0.52
    assert t.cost + t.fee <= app.cfg.sim.max_trade_usd + 1e-9
    assert t.fee == pytest.approx(sum(FEE.match_fee(s, p) for p, s, _ in t.levels))
    assert t.edge_entry == pytest.approx(0.60 - t.avg_price - t.fee / t.shares)
    row = app.db.conn.execute("SELECT decision, trade_id FROM signals").fetchone()
    assert row == ("filled", t.id)


def test_adverse_move_skip_policy(tmp_path):
    import time

    async def scenario():
        now = time.time()
        app = StubApp(tmp_path, now, p_up=0.60)
        app.cfg.sim.adverse_move = "skip"
        app.book("UP", [(0.52, 100.0)])
        app.book("DN", [(0.47, 100.0)])
        eng = PaperEngine(app)
        eng.evaluate(now)
        app.book("UP", [(0.53, 100.0)])
        await asyncio.sleep(0.12)
        return app, eng

    app, eng = asyncio.run(scenario())
    assert eng.trades == []
    assert app.log[-1][0] == "SKIP" and "moved 0.52→0.53" in app.log[-1][1]
    assert eng.cash() == pytest.approx(app.cfg.sim.starting_bankroll)  # reservation released
    # sent but not filled: recorded as "missed", distinct from opportunities never sent
    assert app.db.conn.execute("SELECT decision FROM signals ORDER BY id DESC").fetchone()[0] == "missed"


def test_skip_reasons_and_throttle(tmp_path):
    import time

    now = time.time()
    app = StubApp(tmp_path, now, p_up=0.51)
    app.book("UP", [(0.505, 100.0)])  # gross edge 0.5c < fee ~1.75c
    app.book("DN", [(0.60, 100.0)])
    eng = PaperEngine(app)
    eng.evaluate(now)
    eng.evaluate(now + 0.1)  # same opportunity again: throttled, not re-logged
    assert len(app.log) == 1 and app.log[0][0] == "SKIP" and "< fee" in app.log[0][1]
    # Near expiry: no new entries even with a big edge.
    app.p_up = 0.9
    app.w.end_ts = now + 5 + 10.0  # tau = 5 s < 10 s cutoff at now + 10
    eng.evaluate(now + 10)
    assert "cutoff" in app.log[-1][1]


def test_settlement_and_zero_fee_shadow(tmp_path):
    import time

    async def scenario():
        now = time.time()
        app = StubApp(tmp_path, now, p_up=0.70)
        app.book("UP", [(0.55, 100.0)])
        app.book("DN", [(0.60, 100.0)])
        eng = PaperEngine(app)
        await _signal_and_wait(eng, app, now)
        return app, eng

    app, eng = asyncio.run(scenario())
    (t,) = eng.trades
    eng.settle(app.w.slug, "Up")
    assert t.status == "WON" and t.payout == t.shares
    assert t.pnl == pytest.approx(t.shares - t.cost - t.fee)
    assert t.pnl_zero_fee == pytest.approx(t.shares - t.cost)
    assert app.log[-1][0] == "WIN"
    s = eng.stats(time.time())
    assert s["bankroll"] == pytest.approx(app.cfg.sim.starting_bankroll + t.pnl)
    assert s["zero_fee_pnl"] - s["net_pnl"] == pytest.approx(t.fee)
    # Settling again is a no-op.
    eng.settle(app.w.slug, "Down")
    assert t.status == "WON"


def test_window_cap(tmp_path):
    import time

    async def scenario():
        now = time.time()
        app = StubApp(tmp_path, now, p_up=0.80)
        app.cfg.sim.max_trade_usd = 10
        app.cfg.sim.max_window_usd = 15
        app.cfg.strategy.skip_log_interval_s = 0
        app.book("UP", [(0.50, 1000.0)])
        app.book("DN", [(0.60, 1000.0)])
        eng = PaperEngine(app)
        for i in range(4):
            await _signal_and_wait(eng, app, now + i * 0.2)
        return app, eng

    app, eng = asyncio.run(scenario())
    spent = sum(t.cash_out for t in eng.trades)
    assert spent <= 15 + 1e-9 and len(eng.trades) == 2
    assert any("window cap" in m for tag, m in app.log if tag == "SKIP")


def test_summarize_stats():
    def tr(i, pnl, status, fee=0.1, ts=1000.0):
        return {"status": status, "pnl": pnl, "pnl_zero_fee": pnl + fee, "fee": fee, "cost": 5.0, "fee_in": "collateral",
                "settled_ts": ts + i, "ts_fill": ts + i, "shares": 10.0, "edge_entry": 0.02}
    trades = [tr(0, 2.0, "WON"), tr(1, -3.0, "LOST"), tr(2, 1.0, "WON"), tr(3, 1.0, "WON")]
    s = summarize(trades, 100.0, "America/Chicago", 2000.0)
    assert s["win_rate"] == 0.75 and s["net_pnl"] == pytest.approx(1.0)
    assert s["profit_factor"] == pytest.approx(4.0 / 3.0)
    assert s["max_drawdown"] == pytest.approx(3.0)
    assert s["streak"] == 2 and s["best"] == 2.0 and s["worst"] == -3.0
    lo, hi = s["ci95"]
    assert lo < s["mean_pnl"] < hi
    assert s["zero_fee_pnl"] - s["net_pnl"] == pytest.approx(0.4)
