"""Polymarket taker fee model (CLOB V2).

Per the Fees docs and the official V2 SDKs (py-clob-client-v2 `fees.py`,
clob-client-v2 `fees/index.ts`), the platform taker fee on a match is

    fee = shares * rate * (price * (1 - price)) ** exponent      (in pUSD)

where `rate` / `exponent` are per-market parameters returned by
GET /clob-markets/{condition_id} as `fd.r` / `fd.e` (Gamma mirrors them as
`feeSchedule.rate` / `feeSchedule.exponent`). Crypto markets currently use
rate 0.07, exponent 1: 1.75 pUSD per 100 shares at p = 0.50. Fees are rounded
to 5 decimals (smallest charged fee 0.00001). Makers pay nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class FeeModel:
    rate: float
    exponent: float
    source: str  # where the parameters came from, for the audit trail
    decimals: int = 5
    taker_only: bool = True

    def fee_per_share(self, price: float) -> float:
        """Unrounded fee per share at `price` (used for edge estimates)."""
        if self.rate <= 0 or not 0.0 < price < 1.0:
            return 0.0
        return self.rate * (price * (1.0 - price)) ** self.exponent

    def match_fee(self, shares: float, price: float) -> float:
        """Fee charged on one match of `shares` at `price`, rounded as documented."""
        raw = shares * self.fee_per_share(price)
        q = Decimal(1).scaleb(-self.decimals)
        return float(Decimal(repr(raw)).quantize(q, rounding=ROUND_HALF_UP))

    def describe(self) -> str:
        peak = 100 * self.fee_per_share(0.5)
        return f"rate={self.rate:g} exponent={self.exponent:g} ({peak:.3f} per 100 sh @0.50) [{self.source}]"


ZERO_FEE = FeeModel(rate=0.0, exponent=1.0, source="zero-fee shadow")
