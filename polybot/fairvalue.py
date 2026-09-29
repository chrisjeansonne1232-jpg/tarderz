"""Fair probability that BTC finishes a window at or above its start price.

    p_up = Phi( ln(S / S0) / sqrt(sigma^2 * tau + eps^2) )

S0 = window start price, S = current spot, tau = seconds remaining,
sigma = realized volatility per sqrt(second) over the last N minutes.
eps (basis_noise_bps) is an optional extra log-price uncertainty for the gap
between Coinbase and the Chainlink stream the market resolves on; with
eps = 0 this is exactly Phi(ln(S/S0) / (sigma * sqrt(tau))).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

SECONDS_PER_YEAR = 365.0 * 86400.0


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def fair_up_probability(spot: float, strike: float, sigma_per_sqrt_s: float, tau_s: float, extra_log_std: float = 0.0) -> float:
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    x = math.log(spot / strike)
    var = sigma_per_sqrt_s ** 2 * max(tau_s, 0.0) + extra_log_std ** 2
    if var <= 0.0:
        return 1.0 if x >= 0 else 0.0  # ties resolve Up
    return norm_cdf(x / math.sqrt(var))


def annualize(sigma_per_sqrt_s: float) -> float:
    return sigma_per_sqrt_s * math.sqrt(SECONDS_PER_YEAR)


def deannualize(sigma_annual: float) -> float:
    return sigma_annual / math.sqrt(SECONDS_PER_YEAR)


@dataclass
class VolEstimate:
    sigma: float  # per sqrt(second), after floor
    live_coverage_s: float
    used_bootstrap: bool
    floored: bool


class VolEstimator:
    """Realized volatility from spot sampled every `sample_s` seconds.

    variance/sec = sum(r_i^2) / sum(dt_i) over the lookback. Until the live
    window covers the full lookback, the missing part is filled with the
    variance from 1-minute candles (if bootstrapped).
    """

    def __init__(self, lookback_s: float, sample_s: float, min_live_s: float, floor_annual: float) -> None:
        self.lookback_s = lookback_s
        self.sample_s = sample_s
        self.min_live_s = min_live_s
        self.floor = deannualize(floor_annual)
        self.max_gap_s = max(5.0 * sample_s, 5.0)
        self._returns: deque[tuple[float, float, float]] = deque()  # (t_end, r^2, dt)
        self._last: tuple[float, float] | None = None  # (t, log price)
        self.bootstrap_var: float | None = None  # per second
        self._dirty = True
        self._cached: VolEstimate | None = None

    def set_bootstrap_from_closes(self, closes: list[float], bar_seconds: float) -> int:
        """closes: oldest-first candle closes. Returns number of returns used."""
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
        if len(rets) < 5:
            return 0
        self.bootstrap_var = sum(r * r for r in rets) / (len(rets) * bar_seconds)
        self._dirty = True
        return len(rets)

    def add_sample(self, t: float, price: float) -> None:
        if price <= 0:
            return
        lp = math.log(price)
        if self._last is not None:
            dt = t - self._last[0]
            if dt <= 0:
                return
            if dt <= self.max_gap_s:
                r = lp - self._last[1]
                self._returns.append((t, r * r, dt))
        self._last = (t, lp)
        cutoff = t - self.lookback_s
        while self._returns and self._returns[0][0] <= cutoff:
            self._returns.popleft()
        self._dirty = True

    def estimate(self) -> VolEstimate | None:
        if not self._dirty:
            return self._cached
        self._dirty = False
        self._cached = self._compute()
        return self._cached

    def _compute(self) -> VolEstimate | None:
        sum_r2 = sum(x[1] for x in self._returns)
        live = sum(x[2] for x in self._returns)
        used_boot = False
        if self.bootstrap_var is not None and live < self.lookback_s:
            var = (sum_r2 + self.bootstrap_var * (self.lookback_s - live)) / self.lookback_s
            used_boot = True
        elif live >= self.min_live_s:
            var = sum_r2 / live
        else:
            return None
        sigma = math.sqrt(max(var, 0.0))
        floored = sigma < self.floor
        return VolEstimate(sigma=max(sigma, self.floor), live_coverage_s=live, used_bootstrap=used_boot, floored=floored)


class BasisEstimator:
    """EWMA of (Coinbase price - Chainlink price), aligned on Chainlink's
    observation timestamp so feed latency does not leak into the estimate."""

    def __init__(self, halflife_s: float) -> None:
        self.halflife_s = halflife_s
        self.mean: float | None = None
        self.var = 0.0
        self.n = 0
        self._last_t: float | None = None

    def update(self, t: float, diff: float) -> None:
        if self.mean is None or self._last_t is None:
            self.mean, self.var, self._last_t, self.n = diff, 0.0, t, 1
            return
        dt = max(t - self._last_t, 0.05)
        # Plain running mean until the EWMA's own weight takes over, so the
        # first (noisy) observation doesn't dominate for several half-lives.
        a = max(1.0 / (self.n + 1), 1.0 - 0.5 ** (dt / self.halflife_s))
        delta = diff - self.mean
        self.mean += a * delta
        self.var = (1.0 - a) * (self.var + a * delta * delta)
        self._last_t = max(t, self._last_t)
        self.n += 1

    @property
    def std(self) -> float:
        return math.sqrt(max(self.var, 0.0))
