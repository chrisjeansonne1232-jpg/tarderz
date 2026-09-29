"""End-to-end: run the real App against the local fake exchange with short
windows and forced websocket drops, then check what landed in SQLite."""

import asyncio
import sqlite3

from polybot.app import App
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
    cfg.chainlink.boundary_max_delay_s = 2
    cfg.model.vol_min_live_s = 5
    cfg.watch.print_interval_s = 0.5
    cfg.watch.snapshot_interval_s = 0.5
    validate(cfg)
    return cfg


def test_end_to_end_against_fake_exchange(tmp_path, capsys):
    async def scenario() -> tuple[App, FakeExchange]:
        fx = FakeExchange({"btc-updown-t6s": 6, "btc-updown-t12s": 12}, resolve_delay_s=1.0, drop_every_s=9)
        runner, port = await fx.start()
        cfg = make_config(port, str(tmp_path / "t.sqlite"), str(tmp_path / "t.log"))
        app = App(cfg, mode="watch")
        task = asyncio.create_task(app.run())
        await asyncio.sleep(RUN_SECONDS)
        app.stop.set()
        await asyncio.wait_for(task, timeout=15)
        await runner.cleanup()
        return app, fx

    app, fx = asyncio.run(scenario())
    out = capsys.readouterr().out
    assert "fair Up" in out and "edge after fee" in out

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
    n, with_fv = db.execute("SELECT COUNT(*), COUNT(p_up) FROM fv_snapshots").fetchone()
    assert n > 20 and with_fv > 10
    # The Coinbase-Chainlink basis converges to the fake's true offset (+3.00).
    assert abs(app.basis.mean - 3.0) < 1.5
