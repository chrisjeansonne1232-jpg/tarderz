"""CLI: python -m polybot {watch,discover} [--config config.toml]"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import aiohttp

from .app import App
from .config import Config, ConfigError, load_config
from .db import Database
from .metrics import format_report, summarize
from .markets import parse_event, resolve_fee_model
from .rest import ClobClient, GammaClient, HttpError
from .util import fmt_utc


def setup_logging(cfg: Config, console_level: str | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fmt.converter = time.gmtime
    Path(cfg.general.log_file).parent.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(cfg.general.log_file, maxBytes=20_000_000, backupCount=5, encoding="utf-8")
    fh.setLevel(cfg.general.log_level.upper())
    fh.setFormatter(fmt)
    root.addHandler(fh)
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel((console_level or cfg.general.log_level).upper())
    ch.setFormatter(fmt)
    root.addHandler(ch)


def keep_awake() -> None:
    """Stop Windows from sleeping while the bot runs (macOS: start.sh uses caffeinate).
    The request ends automatically when the process exits."""
    if sys.platform == "win32":
        import ctypes

        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


async def run_app(cfg: Config, mode: str, dashboard: bool = False, open_browser: bool = False) -> None:
    app = App(cfg, mode, dashboard=dashboard, open_browser=open_browser)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, app.stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: Ctrl+C raises KeyboardInterrupt instead
    await app.run()


async def discover(cfg: Config) -> None:
    """One-shot check of every documented assumption against the live API."""
    async with aiohttp.ClientSession(trust_env=True, headers={"User-Agent": cfg.general.user_agent}) as s:
        gamma = GammaClient(s, cfg.endpoints.gamma, cfg.general.http_timeout_s)
        clob = ClobClient(s, cfg.endpoints.clob, cfg.general.http_timeout_s)
        now = time.time()
        for series in [x for x in cfg.markets.series if x.enabled]:
            start = int(now // series.interval_s) * series.interval_s
            slug = f"{series.slug_prefix}-{start}"
            print(f"\n=== {series.name}: {slug}")
            try:
                event = await gamma.event_by_slug(slug)
            except HttpError as e:
                print(f"  Gamma request failed: {e}")
                continue
            if event is None:
                print("  NOT FOUND by slug.", end=" ")
                if series.series_slug:
                    evs = await gamma.events_by_series(series.series_slug, limit=5)
                    print(f"series '{series.series_slug}' lists: {[e.get('slug') for e in evs]}")
                else:
                    print()
                continue
            try:
                w = parse_event(event, series, cfg.markets.required_description_terms)
            except Exception as e:  # noqa: BLE001
                print(f"  could not parse event: {e}")
                print(json.dumps(event, indent=2)[:3000])
                continue
            fee, notes = await resolve_fee_model(cfg, clob, w)
            cm = await clob.clob_market(w.condition_id)
            try:
                legacy = await clob.legacy_fee_rate(w.up_token)
            except HttpError as e:
                legacy = f"error: {e}"
            book = await clob.book(w.up_token)
            m = next((x for x in event.get("markets", []) if x.get("slug") == slug), (event.get("markets") or [{}])[0])
            print(f"  question         : {w.question}")
            print(f"  window (UTC)     : {fmt_utc(w.start_ts)} -> {fmt_utc(w.end_ts)}")
            print(f"  resolutionSource : {w.resolution_source}")
            print(f"  description      : {w.description}")
            print(f"  rules verified   : {w.rules_ok} {w.rules_notes}")
            print(f"  tokens           : Up={w.up_token}\n                     Down={w.down_token}")
            print(f"  tick / min size  : {w.tick_size} / {w.min_order_size}")
            print(f"  Gamma fee fields : feesEnabled={m.get('feesEnabled')} feeType={m.get('feeType')} "
                  f"feeSchedule={m.get('feeSchedule')} takerBaseFee={m.get('takerBaseFee')}")
            print(f"  CLOB /clob-markets fd: {(cm or {}).get('fd')}  (mts={(cm or {}).get('mts')})")
            print(f"  CLOB /fee-rate (legacy, not used): {legacy}")
            print(f"  fee model used   : {fee.describe()}")
            for n in notes:
                print(f"  NOTE: {n}")
            if book:
                bids = sorted(book.get("bids", []), key=lambda l: -float(l["price"]))[:3]
                asks = sorted(book.get("asks", []), key=lambda l: float(l["price"]))[:3]
                print(f"  Up book (REST)   : bids {[(l['price'], l['size']) for l in bids]} asks {[(l['price'], l['size']) for l in asks]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="polybot", description="Read-only Polymarket BTC Up/Down paper-trading research bot")
    ap.add_argument("--config", default="config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="paper trade (simulated fills only; never places orders)")
    p_watch = sub.add_parser("watch", help="stream data and show live fair value vs. the book (no trading)")
    for p in (p_run, p_watch):
        p.add_argument("--dashboard", action="store_true", help="serve the monitoring dashboard")
        p.add_argument("--host", help="dashboard host (overrides dashboard.dashboard_host; 0.0.0.0 = whole LAN)")
        p.add_argument("--port", type=int, help="dashboard port (overrides dashboard.dashboard_port)")
        p.add_argument("--open", action="store_true", help="open the dashboard in your browser once it is up")
    sub.add_parser("discover", help="look up the current markets once and print everything we rely on")
    sub.add_parser("report", help="print paper-trading performance from the database")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if args.cmd == "discover":
        setup_logging(cfg, console_level="WARNING")
        asyncio.run(discover(cfg))
        return 0
    if args.cmd == "report":
        return report(cfg)
    if getattr(args, "host", None):
        cfg.dashboard.dashboard_host = args.host
    if getattr(args, "port", None):
        cfg.dashboard.dashboard_port = args.port
    setup_logging(cfg, console_level="WARNING")
    keep_awake()
    try:
        asyncio.run(run_app(cfg, args.cmd, dashboard=args.dashboard, open_browser=args.open and args.dashboard))
    except KeyboardInterrupt:
        pass
    return 0


def report(cfg: Config) -> int:
    path = Path(cfg.general.db_path)
    if not path.exists():
        print(f"no database at {path}; run `python -m polybot run` first", file=sys.stderr)
        return 1
    db = Database(str(path), read_only=True)
    try:
        trades = db.all_trades()
        agree, total = db.oracle_check()
        try:
            s0 = db.s0_check()
        except Exception:  # noqa: BLE001 - database from before the price-to-beat columns
            s0 = None
    finally:
        db.close()
    print(format_report(summarize(trades, cfg.sim.starting_bankroll, cfg.dashboard.timezone, time.time())))
    print()
    print("DATA CHECKS")
    print(f"  outcome check          {agree}/{total} windows: our Chainlink start/end prices predicted Polymarket's result")
    if s0 is None or s0["windows_with_ptb"] == 0:
        print("  S0 check               no Polymarket price-to-beat seen yet")
    else:
        print(f"  S0 check               {s0['matched']}/{s0['compared']} windows within $0.50 of Polymarket's price to beat"
              + (f" (median gap ${s0['median_abs_diff']:.2f}, max ${s0['max_abs_diff']:.2f})" if s0["compared"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
