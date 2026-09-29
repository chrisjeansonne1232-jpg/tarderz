"""Load and validate the single TOML config file.

Every tunable lives in config.toml. Unknown keys are rejected so a typo
fails loudly instead of silently falling back to a default.
"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_type_hints


class ConfigError(ValueError):
    pass


@dataclass
class GeneralConfig:
    db_path: str = "data/paperbot.sqlite"
    log_file: str = "data/paperbot.log"
    log_level: str = "INFO"
    user_agent: str = "polymarket-paperbot/0.1 (read-only research)"
    http_timeout_s: float = 10.0


@dataclass
class EndpointsConfig:
    gamma: str = "https://gamma-api.polymarket.com"
    clob: str = "https://clob.polymarket.com"
    market_ws: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    rtds_ws: str = "wss://ws-live-data.polymarket.com"
    coinbase_ws: str = "wss://ws-feed.exchange.coinbase.com"
    coinbase_rest: str = "https://api.exchange.coinbase.com"


@dataclass
class SeriesConfig:
    name: str = ""
    # Event slug is "{slug_prefix}-{window_start_unix_seconds}".
    slug_prefix: str = ""
    # Gamma series slug, used only as a fallback discovery path.
    series_slug: str = ""
    interval_s: int = 900
    enabled: bool = True


@dataclass
class MarketsConfig:
    discover_ahead_s: float = 120.0
    discover_retry_s: float = 5.0
    unsubscribe_after_end_s: float = 20.0
    # Light poll of the current market on Gamma; keeps the Gamma status honest
    # (it is a REST API, not a stream). 0 disables.
    gamma_heartbeat_s: float = 2.0
    # A window is flagged "rules not verified" (and never traded) unless its
    # description contains every one of these terms (case-insensitive).
    required_description_terms: list[str] = field(
        default_factory=lambda: ["Chainlink", "greater than or equal"]
    )
    series: list[SeriesConfig] = field(default_factory=list)


@dataclass
class ResolutionConfig:
    first_poll_after_end_s: float = 15.0
    poll_s: float = 15.0
    fast_window_s: float = 3600.0
    slow_poll_s: float = 300.0


@dataclass
class SpotConfig:
    product_id: str = "BTC-USD"
    price_source: str = "mid"  # "mid" of best bid/ask, or "last" trade
    stale_s: float = 30.0
    history_s: float = 3600.0


@dataclass
class ChainlinkConfig:
    enabled: bool = True
    topic: str = "crypto_prices_chainlink"
    symbol: str = "btc/usd"
    ping_interval_s: float = 5.0
    stale_s: float = 30.0
    # Accept the first Chainlink tick at or after a window boundary only if it
    # is at most this late; otherwise the boundary price is "missed".
    boundary_max_delay_s: float = 5.0


@dataclass
class PolymarketWSConfig:
    ping_interval_s: float = 10.0
    stale_s: float = 60.0
    # Resubscribe an asset if its local book disagrees with the server's
    # best bid/ask for this long.
    desync_resync_s: float = 5.0


@dataclass
class ModelConfig:
    vol_lookback_min: float = 30.0
    vol_sample_s: float = 1.0
    vol_min_live_s: float = 120.0
    vol_bootstrap_candles: bool = True
    vol_floor_annual: float = 0.10
    strike_source: str = "chainlink"  # "chainlink" | "coinbase"
    basis_correction: bool = True
    basis_halflife_s: float = 120.0
    basis_noise_bps: float = 0.0


@dataclass
class FeesConfig:
    source: str = "clob"  # "clob" | "gamma" | "fixed"
    fixed_rate: float = 0.07
    fixed_exponent: float = 1.0
    round_decimals: int = 5
    buy_fee_in: str = "collateral"  # "collateral" | "shares"


@dataclass
class StrategyConfig:
    # Signal when (fair - VWAP - fee/share - slippage_allowance) > safety_buffer,
    # i.e. the ask is below fair by more than fee + slippage + buffer.
    safety_buffer: float = 0.01  # $ per share
    slippage_allowance: float = 0.0  # extra $/share assumed on top of book-walk slippage
    min_seconds_remaining: float = 10.0  # no new entries this close to the window end
    skip_log_interval_s: float = 5.0  # log at most one skipped opportunity per window+side this often


@dataclass
class SimConfig:
    starting_bankroll: float = 100.0
    latency_ms: float = 300.0  # signal -> fill delay; fill uses the book as it is after the delay
    adverse_move: str = "take"  # book moved against us during latency: "take" the worse price or "skip"
    max_slippage: float = 0.02  # never pay more than signal ask + this per share (the order's limit)
    order_type: str = "FAK"  # FAK: partial fills allowed | FOK: all or nothing
    max_trade_usd: float = 10.0
    max_window_usd: float = 25.0
    # Our simulated fills don't remove liquidity from the real book, so the
    # size we took is hidden from later fills at that price for this long.
    liquidity_memory_s: float = 10.0
    status_interval_s: float = 60.0


@dataclass
class RecorderConfig:
    interval_s: float = 1.0  # 1-second spot + top-of-book snapshots (used by replay)
    depth: int = 5


@dataclass
class DashboardConfig:
    dashboard_host: str = "127.0.0.1"  # 0.0.0.0 to view from other devices on your LAN
    dashboard_port: int = 8787
    timezone: str = "America/Chicago"
    primary_series: str = "btc-15m"
    stale_after_s: float = 3.0
    tick_hz: float = 4.0
    log_lines: int = 500
    signal_window_min: float = 30.0


@dataclass
class WatchConfig:
    print_interval_s: float = 2.0


@dataclass
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    endpoints: EndpointsConfig = field(default_factory=EndpointsConfig)
    markets: MarketsConfig = field(default_factory=MarketsConfig)
    resolution: ResolutionConfig = field(default_factory=ResolutionConfig)
    spot: SpotConfig = field(default_factory=SpotConfig)
    chainlink: ChainlinkConfig = field(default_factory=ChainlinkConfig)
    polymarket_ws: PolymarketWSConfig = field(default_factory=PolymarketWSConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    fees: FeesConfig = field(default_factory=FeesConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    sim: SimConfig = field(default_factory=SimConfig)
    recorder: RecorderConfig = field(default_factory=RecorderConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)


_SCALARS = (bool, int, float, str)


def _build(cls: type, data: Any, path: str) -> Any:
    if not isinstance(data, dict):
        raise ConfigError(f"[{path}] must be a table")
    hints = get_type_hints(cls)
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(data) - names)
    if unknown:
        raise ConfigError(f"unknown key(s) in [{path}]: {', '.join(unknown)}")
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        typ = hints[f.name]
        where = f"{path}.{f.name}" if path else f.name
        if dataclasses.is_dataclass(typ):
            value = _build(typ, value, where)
        elif typ == list[SeriesConfig]:
            if not isinstance(value, list):
                raise ConfigError(f"{where} must be an array of tables")
            value = [_build(SeriesConfig, v, f"{where}[{i}]") for i, v in enumerate(value)]
        elif typ == list[str]:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError(f"{where} must be a list of strings")
        elif typ in _SCALARS:
            if typ is float and isinstance(value, int) and not isinstance(value, bool):
                value = float(value)
            if not isinstance(value, typ) or (typ is not bool and isinstance(value, bool)):
                raise ConfigError(f"{where} must be {typ.__name__}, got {value!r}")
        kwargs[f.name] = value
    return cls(**kwargs)


def _choice(value: str, allowed: tuple[str, ...], where: str) -> None:
    if value not in allowed:
        raise ConfigError(f"{where} must be one of {allowed}, got {value!r}")


def validate(cfg: Config) -> None:
    enabled = [s for s in cfg.markets.series if s.enabled]
    if not enabled:
        raise ConfigError("markets.series: at least one enabled series is required")
    names = [s.name for s in cfg.markets.series]
    if len(set(names)) != len(names) or not all(names):
        raise ConfigError("markets.series: every series needs a unique, non-empty name")
    for s in cfg.markets.series:
        if s.interval_s <= 0 or not s.slug_prefix:
            raise ConfigError(f"series {s.name}: interval_s > 0 and slug_prefix are required")
    _choice(cfg.spot.price_source, ("mid", "last"), "spot.price_source")
    _choice(cfg.model.strike_source, ("chainlink", "coinbase"), "model.strike_source")
    _choice(cfg.fees.source, ("clob", "gamma", "fixed"), "fees.source")
    _choice(cfg.fees.buy_fee_in, ("collateral", "shares"), "fees.buy_fee_in")
    if cfg.model.strike_source == "chainlink" and not cfg.chainlink.enabled:
        raise ConfigError("model.strike_source = 'chainlink' requires chainlink.enabled = true")
    if cfg.model.vol_sample_s <= 0 or cfg.model.vol_lookback_min <= 0:
        raise ConfigError("model.vol_sample_s and model.vol_lookback_min must be > 0")
    _choice(cfg.sim.adverse_move, ("take", "skip"), "sim.adverse_move")
    _choice(cfg.sim.order_type, ("FAK", "FOK"), "sim.order_type")
    if cfg.sim.starting_bankroll <= 0 or cfg.sim.max_trade_usd <= 0 or cfg.sim.max_window_usd <= 0:
        raise ConfigError("sim.starting_bankroll, max_trade_usd and max_window_usd must be > 0")
    if cfg.sim.latency_ms < 0 or cfg.sim.max_slippage < 0:
        raise ConfigError("sim.latency_ms and sim.max_slippage must be >= 0")
    if cfg.recorder.interval_s <= 0 or cfg.dashboard.tick_hz <= 0:
        raise ConfigError("recorder.interval_s and dashboard.tick_hz must be > 0")
    if not any(s.name == cfg.dashboard.primary_series for s in enabled):
        cfg.dashboard.primary_series = enabled[0].name
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(cfg.dashboard.timezone)
    except Exception as e:  # noqa: BLE001
        raise ConfigError(f"dashboard.timezone {cfg.dashboard.timezone!r} is not a valid IANA zone: {e}") from e


def load_config(path: str | Path) -> Config:
    p = Path(path)
    try:
        raw = tomllib.loads(p.read_text())
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {p}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{p}: {e}") from e
    cfg = _build(Config, raw, "")
    validate(cfg)
    return cfg
