"""End-to-end: run the real App against the local fake exchange with short
windows and forced websocket drops, then check what landed in SQLite."""

import asyncio
import csv
import json
import sqlite3
import time
from pathlib import Path

import aiohttp
import pytest

from polybot.app import App
from polybot.archive import run_archive
from polybot.config import Config, SeriesConfig, validate
from tests.fake_exchange import FakeExchange

RUN_SECONDS = 32


def make_config(port: int, db_path: str, log_path: str) -> Config:
    base = f"127.0.0.1:{port}"
    cfg = Config()
    cfg.general.db_path = db_path
    cfg.general.log_file = log_path
    cfg.endpoints.gamma = f"http://{base}/gamma"
    cfg.endpoints.clob = f"http://{base}/clob"
    cfg.endpoints.market_ws = f"ws://{base}/ws/market"
    cfg.endpoints.rtds_ws = f"ws://{base}/rtds"
    cfg.endpoints.coinbase_ws = f"ws://{base}/coinbase"
    cfg.endpoints.coinbase_rest = f"http://{base}/coinbase-rest"
    cfg.markets.series = [
        SeriesConfig(name="t6", slug_prefix="btc-updown-t6s", interval_s=6),
        SeriesConfig(name="t12", slug_prefix="btc-updown-t12s", interval_s=12),
    ]
    cfg.markets.discover_ahead_s = 3
    cfg.markets.unsubscribe_after_end_s = 2
    cfg.resolution.first_poll_after_end_s = 3.5
    cfg.resolution.poll_s = 1
    cfg.model.vol_min_live_s = 5
    cfg.watch.print_interval_s = 0.5
    cfg.strategy.min_seconds_remaining = 1.0
    cfg.sim.latency_ms = 100
    cfg.dashboard.dashboard_port = 0  # any free port
    cfg.markets.gamma_heartbeat_s = 1.0
    cfg.archive.dir = str(Path(db_path).parent / "archive")
    cfg.archive.interval_min = 0.1
    validate(cfg)
    return cfg


async def _dashboard_client(app: App, seen: dict) -> None:
    """Connect to the dashboard websocket like the browser does and record traffic."""
    while app.dashboard is None or app.dashboard.url.endswith(":0"):
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.5)
    url = app.dashboard.url.replace("http://", "ws://") + "/ws"
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(url) as ws:
            await ws.send_str('{"type":"order","side":"BUY"}')  # must be ignored: read-only
            t0 = time.time()
            async for msg in ws:
                m = json.loads(msg.data)
                seen.setdefault("order", []).append(m["type"])
                if m["type"] == "snapshot":
                    seen["snapshot"] = m
                if m["type"] == "tick" and t0 + 5 <= time.time() < t0 + 10:
                    seen["ticks_5_10s"] = seen.get("ticks_5_10s", 0) + 1
                if time.time() - t0 > 14:
                    break


def test_end_to_end_against_fake_exchange(tmp_path, capsys):
    seen: dict = {}

    async def scenario() -> tuple[App, FakeExchange]:
        fx = FakeExchange({"btc-updown-t6s": 6, "btc-updown-t12s": 12}, resolve_delay_s=1.0, drop_every_s=9)
        runner, port = await fx.start()
        cfg = make_config(port, str(tmp_path / "t.sqlite"), str(tmp_path / "t.log"))
        app = App(cfg, mode="run", dashboard=True)
        task = asyncio.create_task(app.run())
        client = asyncio.create_task(_dashboard_client(app, seen))
        await asyncio.sleep(RUN_SECONDS)
        app.stop.set()
        t0 = time.time()
        await asyncio.wait_for(task, timeout=15)
        seen["shutdown_s"] = time.time() - t0
        client.cancel()
        await runner.cleanup()
        return app, fx

    app, fx = asyncio.run(scenario())
    out = capsys.readouterr().out
    assert "dashboard: http://127.0.0.1:" in out
    assert seen["shutdown_s"] < 4.0  # clean, prompt Ctrl+C

    # Dashboard protocol: snapshot first, then ~4 Hz ticks plus immediate events.
    snap = seen["snapshot"]
    assert seen["order"][0] == "snapshot"
    assert snap["meta"]["paper"] is True and snap["meta"]["source"] == "realtime"
    assert snap["meta"]["test_feed"] is True  # not Polymarket's production host
    assert isinstance(snap["trade_stats"], list) and isinstance(snap["windows_recent"], list)
    assert snap["windows_recent"] and all({"slug", "start", "end", "s0", "outcome"} <= set(w) for w in snap["windows_recent"])
    assert 14 <= seen["ticks_5_10s"] <= 26
    assert "log" in seen["order"]

    # Every feed was dropped at least once and came back.
    assert app.coinbase.connects >= 2 and app.channel.connects >= 2 and app.chainlink.connects >= 2
    assert fx.connections["market"] >= 2

    db = sqlite3.connect(tmp_path / "t.sqlite")
    rows = db.execute(
        "SELECT slug, s0_status, chainlink_predicted, resolved_outcome, fee_rate, rules_ok FROM markets"
    ).fetchall()
    assert len(rows) >= 6
    assert all(r[4] == 0.07 and r[5] == 1 for r in rows)
    resolved = [r for r in rows if r[3] is not None]
    assert len(resolved) >= 3
    # Wherever we captured both boundary prices, our Chainlink-based outcome
    # must match the posted resolution.
    compared = [r for r in resolved if r[2] is not None]
    assert compared and all(r[2] == r[3] for r in compared)
    n, with_fv = db.execute("SELECT COUNT(*), COUNT(fair_up) FROM snapshots_1s").fetchone()
    assert n > 30 and with_fv > 10
    # Polymarket's published price to beat was picked up and agrees with our Chainlink start price.
    # (A report up to boundary_max_delay_s late may differ slightly; one stamped
    # exactly at the boundary must match.)
    ptb_rows = db.execute(
        "SELECT s0_chainlink, ptb_polymarket, s0_chainlink_ts - start_ts FROM markets "
        "WHERE ptb_polymarket IS NOT NULL AND s0_chainlink IS NOT NULL"
    ).fetchall()
    exact = [r for r in ptb_rows if abs(r[2]) < 1e-6]
    assert exact and all(abs(a - b) < 1e-6 for a, b, _ in exact)
    assert all(0 <= r[2] <= app.cfg.chainlink.boundary_max_delay_s for r in ptb_rows)
    assert db.execute("SELECT COUNT(*) FROM exec_log WHERE msg LIKE '%S0 check OK%'").fetchone()[0] >= 1
    tags = {r[0] for r in db.execute("SELECT DISTINCT tag FROM exec_log")}
    assert {"MKT", "RECONNECT", "SETTLE"} <= tags
    # Paper trades that settled carry consistent P&L.
    for status, pnl, pnl0, fee, payout, cost in db.execute(
        "SELECT status, pnl, pnl_zero_fee, fee, payout, cost FROM trades WHERE status IN ('WON','LOST')"
    ):
        assert pnl == pytest.approx(payout - cost - fee) and pnl0 - pnl == pytest.approx(fee)
    # Every evaluation was counted, and the archive was written while running.
    counts = dict(db.execute("SELECT reason, SUM(n) FROM opportunity_counts GROUP BY reason").fetchall())
    assert counts.get("checked", 0) > 50
    assert counts["checked"] >= sum(counts.get(k, 0) for k in ("no_edge", "in_flight", "below_fee", "below_buffer",
                                                                "too_late", "window_cap", "no_cash", "no_depth", "signal"))
    n_trades = db.execute("SELECT COUNT(*) FROM trades WHERE COALESCE(wallet, 'main') = 'main'").fetchone()[0]
    assert counts.get("filled", 0) == n_trades
    assert counts.get("signal", 0) >= counts.get("filled", 0) + counts.get("fill_skipped", 0)
    arch = tmp_path / "archive"
    days = [d for d in arch.iterdir() if d.is_dir()]
    assert days and all((d / f).exists() for d in days for f in ("summary.txt", "trades.csv", "signals.csv",
                                                                  "windows.csv", "log.csv"))
    run_archive(str(tmp_path / "t.sqlite"), arch, app.cfg.dashboard.timezone, time.time())
    with open(arch / "all_trades.csv", encoding="utf-8-sig") as f:
        assert sum(1 for _ in csv.reader(f)) - 1 == n_trades
    with open(arch / "whatif_trades.csv", encoding="utf-8-sig") as f:
        assert sum(1 for _ in csv.reader(f)) - 1 == db.execute(
            "SELECT COUNT(*) FROM trades WHERE wallet LIKE 'whatif-%'").fetchone()[0]
    rows = []
    for d in days:
        with open(d / "windows.csv", encoding="utf-8-sig") as f:
            rows += list(csv.DictReader(f))
    assert rows and any(int(r["checked"]) > 0 for r in rows)
    # Latency what-if wallets: separate books, each filling after its own delay.
    assert all(t.wallet == "main" for t in app.engine.trades)
    assert [e.wallet for e in app.whatif] == ["whatif-0ms", "whatif-50ms", "whatif-300ms", "whatif-nearcertain"]
    # near-certain wallet: only buys at 95-97c, only in the final 15% of a window, never above the signal price
    for avg, sig_ask, ts_sig, end, start in db.execute(
        "SELECT t.avg_price, t.signal_ask, t.ts_signal, m.end_ts, m.start_ts FROM trades t JOIN markets m ON m.slug = t.slug "
        "WHERE t.wallet = 'whatif-nearcertain'"
    ):
        assert 0.95 - 1e-9 <= sig_ask <= 0.97 + 1e-9 and avg <= sig_ask + 1e-9
        assert end - ts_sig <= 0.15 * (end - start) + 1e-6
    for eng in app.whatif[:3]:
        rows = db.execute("SELECT latency_ms, status FROM trades WHERE wallet = ?", (eng.wallet,)).fetchall()
        assert rows, eng.wallet
        assert all(lat < eng.latency_ms + 60 for lat, _ in rows) and all(lat >= eng.latency_ms - 1 for lat, _ in rows)
        assert eng.cash() > 0 and eng.starting == app.cfg.whatif.starting_bankroll
    assert db.execute("SELECT COUNT(*) FROM exec_log WHERE msg LIKE '%whatif%'").fetchone()[0] == 0  # quiet
    # The Coinbase-Chainlink basis converges to the fake's true offset (+3.00).
    assert abs(app.basis.mean - 3.0) < 1.5
