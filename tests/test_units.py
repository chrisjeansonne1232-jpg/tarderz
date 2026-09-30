import json
import math
import random

import pytest

from polybot.book import OrderBook
from polybot.config import ConfigError, SeriesConfig, _build, Config
from polybot.fairvalue import (
    BasisEstimator,
    VolEstimator,
    annualize,
    deannualize,
    fair_up_probability,
)
from polybot.feeds import ChainlinkFeed, CoinbaseFeed
from polybot.config import ChainlinkConfig, SpotConfig, PolymarketWSConfig
from polybot.fees import FeeModel
from polybot.market_ws import MarketChannel
from polybot.markets import parse_event
from polybot.resolution import parse_resolution
from polybot.util import TimeSeries

# Shape of a live Gamma event for these markets (fields as captured from the API).
SAMPLE_EVENT = {
    "id": "473573",
    "slug": "btc-updown-5m-1778584200",
    "title": "Bitcoin Up or Down - May 12, 7:10AM-7:15AM ET",
    "description": "",
    "endDate": "2026-05-12T11:15:00Z",
    "markets": [
        {
            "id": "2229686",
            "question": "Bitcoin Up or Down - May 12, 7:10AM-7:15AM ET",
            "conditionId": "0xa5c6d0c4",
            "slug": "btc-updown-5m-1778584200",
            "resolutionSource": "https://data.chain.link/streams/btc-usd",
            "endDate": "2026-05-12T11:15:00Z",
            "outcomes": '["Up", "Down"]',
            "outcomePrices": '["0.515", "0.485"]',
            "closed": False,
            "orderPriceMinTickSize": 0.01,
            "orderMinSize": 5,
            "clobTokenIds": '["111", "222"]',
            "makerBaseFee": 1000,
            "takerBaseFee": 1000,
            "eventStartTime": "2026-05-12T11:10:00Z",
            "feesEnabled": True,
            "feeType": "crypto_fees_v2",
            "feeSchedule": {"exponent": 1, "rate": 0.07, "takerOnly": True, "rebateRate": 0.2},
            "description": (
                'This market will resolve to "Up" if the Bitcoin price at the end of the time range specified in '
                "the title is greater than or equal to the price at the beginning of that range. Otherwise, it will "
                'resolve to "Down".\nThe resolution source for this market is information from Chainlink, '
                "specifically the BTC/USD data stream available at https://data.chain.link/streams/btc-usd."
            ),
        }
    ],
}
SERIES_5M = SeriesConfig(name="btc-5m", slug_prefix="btc-updown-5m", interval_s=300)
TERMS = ["Chainlink", "greater than or equal"]


# --- fees -------------------------------------------------------------------------
def test_fee_matches_documented_crypto_curve():
    f = FeeModel(rate=0.07, exponent=1, source="t")
    assert f.match_fee(100, 0.50) == pytest.approx(1.75)  # docs: $1.75 per 100 shares at 50c
    assert f.match_fee(100, 0.25) == pytest.approx(1.3125)
    assert f.match_fee(100, 0.25) == f.match_fee(100, 0.75)  # symmetric
    assert f.fee_per_share(0.0) == 0.0 and f.fee_per_share(1.0) == 0.0


def test_fee_rounds_to_five_decimals():
    f = FeeModel(rate=0.07, exponent=1, source="t")
    assert f.match_fee(1, 0.37) == 0.01632  # 0.07*0.37*0.63 = 0.016317
    assert f.match_fee(0.001, 0.01) == 0.0  # below the smallest chargeable fee


def test_fee_exponent_is_honoured():
    # The pre-2026 crypto schedule used rate 0.25, exponent 2.
    f = FeeModel(rate=0.25, exponent=2, source="t")
    assert f.fee_per_share(0.5) == pytest.approx(0.25 * 0.0625)


# --- fair value -------------------------------------------------------------------
def test_fair_value_basics():
    s = deannualize(0.5)
    assert fair_up_probability(100.0, 100.0, s, 600) == pytest.approx(0.5)
    assert fair_up_probability(101.0, 100.0, s, 600) > 0.5 > fair_up_probability(99.0, 100.0, s, 600)
    # More time left -> closer to 0.5.
    assert fair_up_probability(100.1, 100.0, s, 60) > fair_up_probability(100.1, 100.0, s, 600)
    # Expired: ties resolve Up.
    assert fair_up_probability(100.0, 100.0, s, 0) == 1.0
    assert fair_up_probability(99.99, 100.0, s, 0) == 0.0


def test_fair_value_formula_exact():
    s, tau = 2e-5, 300.0
    x = math.log(64100 / 64000) / (s * math.sqrt(tau))
    expected = 0.5 * (1 + math.erf(x / math.sqrt(2)))
    assert fair_up_probability(64100, 64000, s, tau) == pytest.approx(expected, rel=1e-12)


def test_extra_uncertainty_pulls_towards_half():
    s = deannualize(0.5)
    base = fair_up_probability(100.02, 100.0, s, 5)
    wider = fair_up_probability(100.02, 100.0, s, 5, extra_log_std=5e-4)
    assert 0.5 < wider < base


def test_vol_estimator_recovers_sigma():
    rng = random.Random(3)
    true = deannualize(0.6)
    ve = VolEstimator(lookback_s=1800, sample_s=1.0, min_live_s=60, floor_annual=0.0)
    p, t = 60000.0, 0.0
    for _ in range(1800):
        t += 1.0
        p *= math.exp(rng.gauss(0, true))
        ve.add_sample(t, p)
    est = ve.estimate()
    assert est is not None
    assert annualize(est.sigma) == pytest.approx(0.6, rel=0.08)


def test_vol_estimator_blends_bootstrap_and_skips_gaps():
    ve = VolEstimator(lookback_s=600, sample_s=1.0, min_live_s=60, floor_annual=0.0)
    assert ve.estimate() is None
    closes = [100 * math.exp(0.001 * (i % 2)) for i in range(31)]
    assert ve.set_bootstrap_from_closes(closes, 60.0) == 30
    est = ve.estimate()
    assert est is not None and est.used_bootstrap and est.live_coverage_s == 0
    assert est.sigma == pytest.approx(math.sqrt(0.001 ** 2 / 60), rel=1e-9)
    ve.add_sample(0.0, 100.0)
    ve.add_sample(100.0, 200.0)  # 100 s gap: must not count as a return
    assert ve.estimate().live_coverage_s == 0


def test_vol_floor():
    ve = VolEstimator(lookback_s=600, sample_s=1.0, min_live_s=10, floor_annual=0.2)
    for i in range(30):
        ve.add_sample(float(i), 100.0)
    est = ve.estimate()
    assert est.floored and annualize(est.sigma) == pytest.approx(0.2)


def test_basis_ewma_converges():
    b = BasisEstimator(halflife_s=10)
    for i in range(200):
        b.update(float(i), 5.0 + (0.5 if i % 2 else -0.5))
    assert b.mean == pytest.approx(5.0, abs=0.1)
    assert 0.3 < b.std < 0.7


# --- order book -------------------------------------------------------------------
def test_book_snapshot_and_levels():
    b = OrderBook("a")
    b.apply_snapshot([{"price": ".48", "size": "30"}, {"price": "0.47", "size": "10"}],
                     [{"price": ".52", "size": "25"}], 1.0, 1.0)
    assert b.best_bid() == (0.48, 30.0) and b.best_ask() == (0.52, 25.0)
    b.apply_level("SELL", "0.51", "7", 2.0, 2.0)
    assert b.best_ask() == (0.51, 7.0)
    b.apply_level("BUY", "0.48", "0", 3.0, 3.0)  # size 0 removes the level
    assert b.best_bid() == (0.47, 10.0)
    v = b.view()
    assert v.asks == ((0.51, 7.0), (0.52, 25.0)) and v.bids == ((0.47, 10.0),)


def test_book_desync_tracking():
    b = OrderBook("a")
    b.apply_snapshot([{"price": "0.40", "size": "1"}], [{"price": "0.42", "size": "1"}], 0, 0)
    assert b.check_top("0.40", "0.42", 10.0) and b.mismatch_since is None
    assert not b.check_top("0.41", "0.42", 11.0) and b.mismatch_since == 11.0
    assert b.check_top("0.40", "0.42", 12.0) and b.mismatch_since is None
    empty = OrderBook("b")
    empty.apply_snapshot([], [], 0, 0)
    assert empty.check_top("0", "1", 1.0)  # server reports empty sides as 0 / 1


def _channel() -> MarketChannel:
    return MarketChannel(None, "ws://x", PolymarketWSConfig())  # type: ignore[arg-type]


def test_market_channel_parses_book_list_and_price_changes():
    ch = _channel()
    for a in ("111", "222"):
        ch.books[a] = OrderBook(a)
    ch.on_text(json.dumps([
        {"event_type": "book", "asset_id": "111", "market": "0xc", "bids": [{"price": ".48", "size": "30"}],
         "asks": [{"price": ".52", "size": "25"}], "timestamp": "1700000000000", "hash": "0x"},
        {"event_type": "book", "asset_id": "222", "market": "0xc", "bids": [], "asks": [], "timestamp": "1700000000000"},
    ]), 1700000000.1)
    assert ch.books["111"].ready and ch.books["111"].exch_ts == pytest.approx(1700000000.0)
    ch.on_text(json.dumps({
        "event_type": "price_change", "market": "0xc", "timestamp": "1700000001000",
        "price_changes": [{"asset_id": "111", "price": "0.5", "size": "200", "side": "BUY", "hash": "h",
                           "best_bid": "0.5", "best_ask": "0.52"}],
    }), 1700000001.1)
    assert ch.books["111"].best_bid() == (0.5, 200.0)
    assert ch.books["111"].mismatch_since is None
    # Older payload shape.
    ch.on_text(json.dumps({"event_type": "price_change", "asset_id": "111", "timestamp": "1700000002000",
                           "changes": [{"price": "0.52", "side": "SELL", "size": "0"}]}), 1700000002.1)
    assert ch.books["111"].best_ask() is None
    ch.on_text(json.dumps({"event_type": "tick_size_change", "asset_id": "111", "old_tick_size": "0.01",
                           "new_tick_size": "0.001"}), 1.0)
    assert ch.books["111"].tick_size == 0.001


def test_price_change_before_snapshot_is_ignored():
    ch = _channel()
    ch.books["111"] = OrderBook("111")
    ch.on_text(json.dumps({"event_type": "price_change", "market": "0xc", "timestamp": "1",
                           "price_changes": [{"asset_id": "111", "price": "0.5", "size": "1", "side": "BUY"}]}), 1.0)
    assert not ch.books["111"].ready and ch.books["111"].bids == {}


# --- feeds ------------------------------------------------------------------------
def test_coinbase_ticker_parsing():
    feed = CoinbaseFeed(None, "ws://x", SpotConfig())  # type: ignore[arg-type]
    msg = {"type": "ticker", "sequence": 5, "product_id": "BTC-USD", "price": "64000.10",
           "best_bid": "64000.00", "best_ask": "64000.02", "time": "2026-05-12T11:10:00.123456Z"}
    feed.on_text(json.dumps(msg), 1778584200.2)
    assert feed.last.price == pytest.approx(64000.01)  # mid
    assert feed.last.exch_ts == pytest.approx(1778584200.123456)
    feed.on_text(json.dumps(dict(msg, sequence=4, price="1")), 1778584200.3)  # out of order: ignored
    assert feed.last.last == 64000.10


def test_chainlink_parsing_and_boundary_rule():
    feed = ChainlinkFeed(None, "ws://x", ChainlinkConfig(), history_s=3600)  # type: ignore[arg-type]
    t0 = 1778584200
    feed.on_text(json.dumps({"topic": "crypto_prices_chainlink", "type": "update", "timestamp": 0,
                             "payload": {"symbol": "btc/usd", "timestamp": (t0 - 1) * 1000, "value": 63999.5}}), t0)
    feed.on_text(json.dumps({"topic": "crypto_prices_chainlink", "type": "update", "timestamp": 0,
                             "payload": {"symbol": "eth/usd", "timestamp": t0 * 1000, "value": 3000}}), t0)
    assert feed.boundary_price(t0) is None  # nothing at/after the boundary yet
    feed.on_text(json.dumps({"topic": "crypto_prices_chainlink", "type": "subscribe",
                             "payload": {"symbol": "btc/usd", "data": [
                                 {"timestamp": t0 * 1000, "value": 64000.0},
                                 {"timestamp": (t0 + 1) * 1000, "value": 64001.0}]}}), t0 + 1.5)
    assert feed.boundary_price(t0) == (t0, 64000.0)
    assert feed.last.value == 64001.0
    late = ChainlinkFeed(None, "ws://x", ChainlinkConfig(), history_s=3600)  # type: ignore[arg-type]
    late.history.append(t0 + 2, 1.0)
    assert late.boundary_price(t0) is None  # joined after the boundary


def test_time_series_asof():
    ts = TimeSeries(max_age_s=10)
    for i in range(20):
        ts.append(float(i), float(i) * 2)
    assert ts.first() == (9.0, 18.0)  # trimmed to max age
    assert ts.asof(12.5) == (12.0, 24.0)
    assert ts.first_at_or_after(12.5) == (13.0, 26.0)
    assert ts.asof(5.0) is None


# --- Gamma parsing / resolution -------------------------------------------------------
def test_parse_event_sample():
    w = parse_event(SAMPLE_EVENT, SERIES_5M, TERMS)
    assert w.rules_ok, w.rules_notes
    assert (w.up_token, w.down_token) == ("111", "222")
    assert w.start_ts == 1778584200 and w.end_ts == 1778584500
    assert w.gamma_fee == (0.07, 1.0)
    assert w.min_order_size == 5 and w.tick_size == 0.01


def test_parse_event_flags_unexpected_rules():
    ev = json.loads(json.dumps(SAMPLE_EVENT))
    ev["markets"][0]["description"] = "Resolves per Binance BTCUSDT close."
    w = parse_event(ev, SERIES_5M, TERMS)
    assert not w.rules_ok
    ev = json.loads(json.dumps(SAMPLE_EVENT))
    ev["markets"][0]["eventStartTime"] = "2026-05-12T11:05:00Z"  # 10-minute window != 5m series
    assert not parse_event(ev, SERIES_5M, TERMS).rules_ok


def test_parse_event_swapped_outcome_order():
    ev = json.loads(json.dumps(SAMPLE_EVENT))
    ev["markets"][0]["outcomes"] = '["Down", "Up"]'
    w = parse_event(ev, SERIES_5M, TERMS)
    assert (w.up_token, w.down_token) == ("222", "111")


def test_parse_resolution():
    m = dict(SAMPLE_EVENT["markets"][0])
    assert parse_resolution(m)[0] is None  # open market: prices are just quotes
    m.update(closed=True, outcomePrices='["0.995", "0.005"]')
    assert parse_resolution(m)[0] is None  # not a settled 1/0
    m.update(outcomePrices='["0", "1"]')
    assert parse_resolution(m)[0] == "Down"
    m.update(outcomePrices='["1", "0"]')
    assert parse_resolution(m)[0] == "Up"


# --- config -----------------------------------------------------------------------
def test_config_rejects_unknown_keys_and_bad_types():
    with pytest.raises(ConfigError, match="unknown key"):
        _build(Config, {"model": {"vol_lookbak_min": 5}}, "")
    with pytest.raises(ConfigError, match="must be float"):
        _build(Config, {"model": {"vol_lookback_min": "5"}}, "")
    cfg = _build(Config, {"model": {"vol_lookback_min": 5}}, "")
    assert cfg.model.vol_lookback_min == 5.0


# --- Polymarket price to beat / S0 ------------------------------------------------------
def test_extract_price_to_beat_shapes():
    from polybot.markets import extract_price_to_beat

    assert extract_price_to_beat({"eventMetadata": {"priceToBeat": 83498.21}}) == 83498.21
    assert extract_price_to_beat({"eventMetadata": '{"priceToBeat": "83498.21"}'}) == 83498.21
    assert extract_price_to_beat({"markets": [{"eventMetadata": {"priceToBeat": "84000"}}]}) == 84000.0
    assert extract_price_to_beat({"eventMetadata": {"priceToBeat": None}}) is None
    assert extract_price_to_beat({"eventMetadata": "not json"}) is None
    assert extract_price_to_beat(SAMPLE_EVENT) is None  # not published yet


def test_boundary_exact_mode_and_tolerance():
    feed = ChainlinkFeed(None, "ws://x", ChainlinkConfig(boundary_max_delay_s=0), history_s=3600)  # type: ignore[arg-type]
    t0 = 1778584200
    feed.history.append(t0 - 1, 1.0)
    feed.history.append(t0 + 1, 2.0)  # first report is 1 s late: not the start price
    assert feed.boundary_price(t0) is None
    feed.history.append(t0, 3.0)
    assert feed.boundary_price(t0) == (t0, 3.0)
    lenient = ChainlinkFeed(None, "ws://x", ChainlinkConfig(boundary_max_delay_s=2), history_s=3600)  # type: ignore[arg-type]
    lenient.history.append(t0 - 1, 1.0)
    lenient.history.append(t0 + 1, 2.0)
    assert lenient.boundary_price(t0) == (t0 + 1, 2.0)
    too_late = ChainlinkFeed(None, "ws://x", ChainlinkConfig(), history_s=3600)  # type: ignore[arg-type]
    too_late.history.append(t0 - 1, 1.0)
    too_late.history.append(t0 + 2.5, 2.0)  # default tolerance is 2 s
    assert too_late.boundary_price(t0) is None


def test_database_migrates_old_markets_table(tmp_path):
    import sqlite3

    from polybot.db import Database

    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE markets (slug TEXT PRIMARY KEY, s0_chainlink REAL, resolved_outcome TEXT, end_ts REAL, market_id TEXT)")
    old.execute("INSERT INTO markets(slug, s0_chainlink) VALUES ('a', 100.0)")
    old.commit()
    old.close()
    db = Database(str(path))
    db.set_price_to_beat("a", 100.2, 1.0)
    chk = db.s0_check()
    assert chk["compared"] == 1 and chk["matched"] == 1 and abs(chk["median_abs_diff"] - 0.2) < 1e-9
    db.set_price_to_beat("a", 103.0, 1.0)
    assert db.s0_check()["matched"] == 0


def test_channel_group_merges_books_and_reports_stalest():
    import asyncio

    from polybot.market_ws import ChannelGroup

    g = ChannelGroup(None, "ws://x", PolymarketWSConfig(), ["a", "b"])  # type: ignore[arg-type]
    asyncio.run(g.channel_for("a").subscribe(["UP_A"]))
    asyncio.run(g.channel_for("b").subscribe(["UP_B"]))
    assert g.books.get("UP_A") is g.channel_for("a").books["UP_A"] and "UP_B" in g.books
    g.channel_for("a").last_data_recv, g.channel_for("b").last_data_recv = 100.0, 90.0
    assert g.last_data_recv == 90.0  # one quiet market makes the CLOB feed look stale
    asyncio.run(g.channel_for("b").unsubscribe(["UP_B"]))
    assert "UP_B" not in g.books and g.last_data_recv == 100.0  # idle connections don't count
    # Subscriptions only ask for the optional event types when configured.
    assert "custom_feature_enabled" not in g.channel_for("a")._sub({"assets_ids": []})


def test_boundary_miss_detail_names_nearest_reports():
    from types import SimpleNamespace

    from polybot.markets import SeriesTracker

    t0 = 1778584200
    feed = ChainlinkFeed(None, "ws://x", ChainlinkConfig(), history_s=3600)  # type: ignore[arg-type]
    fake = SimpleNamespace(chainlink=feed, cfg=SimpleNamespace(chainlink=ChainlinkConfig()))
    assert "no Chainlink reports" in SeriesTracker._miss_detail(fake, t0)
    feed.history.append(t0 - 0.4, 1.0)
    feed.history.append(t0 + 3.1, 2.0)
    msg = SeriesTracker._miss_detail(fake, t0)
    assert "-0.40s" in msg and "+3.10s" in msg and "allowed delay 2s" in msg
    feed2 = ChainlinkFeed(None, "ws://x", ChainlinkConfig(), history_s=3600)  # type: ignore[arg-type]
    feed2.history.append(t0 + 5, 1.0)
    assert "joined late" in SeriesTracker._miss_detail(SimpleNamespace(chainlink=feed2, cfg=fake.cfg), t0)
