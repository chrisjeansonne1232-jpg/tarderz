"""The near-certain-side replay: fills, limits and the train/test split, on a synthetic recording."""
import json
import zlib

from polybot.backtest import FavParams, Window, _windows, run_favorite_backtest, simulate
from polybot.db import Database
from polybot.fees import FeeModel

FEE = FeeModel(0.07, 1.0, "test")


def blob(ua, da, size=100.0):
    return zlib.compress(json.dumps({"ua": [[ua, size]] if ua else [], "da": [[da, size]] if da else []}).encode())


def window(outcome, asks, start=0.0, interval=300.0):
    """asks: list of (tau, up_ask, down_ask, size) one per second."""
    w = Window(f"w{start}", "btc-5m", start, start + interval, outcome, FEE, 5.0)
    for tau, ua, da, size in asks:
        w.rows.append((start + interval - tau, tau, ua, da, blob(ua, da, size)))
    return w


P = FavParams(0.95, 0.97, 0.15)


def test_buys_the_favorite_late_and_settles():
    w = window("Up", [(40, 0.96, 0.05, 100), (39, 0.96, 0.05, 100), (38, 0.96, 0.05, 100), (37, 0.96, 0.05, 100)])
    tr = simulate(w, P, 10.0, 25.0, 10.0, 10.0)
    assert tr and all(t["side"] == "Up" and t["won"] and abs(t["price"] - 0.96) < 1e-9 for t in tr)
    assert sum(t["cost"] + t["fee"] for t in tr) <= 25.0 + 1e-9          # per-market limit
    assert all(t["pnl"] > 0 and t["pnl0"] > t["pnl"] for t in tr)


def test_no_fill_above_signal_price_or_too_early():
    moved = window("Up", [(40, 0.96, 0.05, 100), (39, 0.98, 0.03, 100)])   # ask moved up before the order arrived
    assert simulate(moved, P, 10.0, 25.0, 10.0, 10.0) == []
    early = window("Up", [(200, 0.96, 0.05, 100), (199, 0.96, 0.05, 100)])  # not in the last 15%
    assert simulate(early, P, 10.0, 25.0, 10.0, 10.0) == []


def test_displayed_size_and_our_own_fills_limit_quantity():
    thin = window("Down", [(40, 0.96, 0.05, 6.0), (39, 0.96, 0.05, 6.0), (38, 0.96, 0.05, 6.0)])
    tr = simulate(thin, P, 10.0, 25.0, 10.0, 10.0)
    assert len(tr) == 1 and tr[0]["shares"] == 6.0 and not tr[0]["won"]      # second try: the 6 shares are ours already
    assert abs(tr[0]["pnl"] + tr[0]["cost"] + tr[0]["fee"]) < 1e-9


def test_full_replay_scores_only_on_unseen_markets(tmp_path):
    path = str(tmp_path / "r.sqlite")
    db = Database(path)
    for k in range(40):
        s = 1_000_000 + k * 300
        slug = f"btc-updown-5m-{s}"
        out = "Down" if k % 13 == 5 else "Up"
        db.conn.execute(
            "INSERT INTO markets(slug, series, start_ts, end_ts, resolved_outcome, fee_rate, fee_exponent, min_order_size) "
            "VALUES (?,?,?,?,?,?,?,?)", (slug, "btc-5m", s, s + 300, out, 0.07, 1.0, 5.0))
        for tau in range(60, 9, -1):
            db.conn.execute("INSERT INTO snapshots_1s(ts, series, slug, tau, up_ask, down_ask, blob) VALUES (?,?,?,?,?,?,?)",
                            (s + 300 - tau, "btc-5m", slug, tau, 0.96, 0.05, blob(0.96, 0.05)))
    db.close()
    assert len(list(_windows(path, 300))) == 40
    text = run_favorite_backtest(path, 10.0, 25.0, 10.0, 10.0, "UTC")
    assert "CHOSEN ON THE EARLIER DATA" in text and "VERDICT" in text and "later 40% (unseen)" in text
