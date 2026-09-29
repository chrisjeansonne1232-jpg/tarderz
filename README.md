# polybot: paper-trading research bot for Polymarket BTC Up/Down

Tests whether a latency / fair-value strategy on Polymarket's short-term
"Bitcoin Up or Down" markets (15-minute and 5-minute) still has an edge after
fees. **Paper trading only.** The project has no order-placement code and
needs no private key, wallet or API secret. Every endpoint it touches is
public and read-only.

| Stage | What | Status |
|---|---|---|
| 1 | Live data (Gamma discovery, CLOB order books, Coinbase, Chainlink) + fair value next to the book | **done** (`watch`) |
| 2 | Signals: ask below fair value by more than fee + slippage + buffer | next |
| 3 | Simulated taker fills: latency, book walking, fees, zero-fee shadow P&L, settlement | |
| 4 | `report`: win rate, P&L, fees, edge at entry vs realized, drawdown, 95% CI | |

## Setup

Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # just aiohttp
```

All settings live in `config.toml`. There are no secrets to configure.

## Stage 1: check the data

**1. `discover`** looks up the current 15m and 5m markets once and prints
everything the bot relies on. Use it to confirm the docs assumptions on your
own connection:

```bash
python -m polybot discover
```

This prints each market's question, UTC window, resolution source and full
description, and whether the rules check passed. It shows the Up/Down token
IDs, tick size and minimum size. It shows the fee fields from both Gamma
(`feeSchedule`) and the CLOB (`/clob-markets` `fd`), the legacy `/fee-rate`
value (shown only; not used), and a REST snapshot of the Up book.

**2. `watch`** streams everything and prints fair value next to the market
every 2 s. Ctrl+C stops it cleanly.

```bash
python -m polybot watch
```

```
21:36:41Z  CB 64,105.12 (lag 40ms, age 38ms)  CL 64,097.66 (lag 820ms, age 457ms)  basis +3.02±2.11  σ 51.6%/yr (boot+live 12m)  PM up
  btc-15m  τ 08:18 | S0 64,004.25[CL] S* 64,102.10 | fair Up 0.738 Dn 0.262 | Up 0.71x74/0.72x27  Dn 0.28x25/0.29x200 | edge after fee: Up +0.4¢ Dn -4.2¢
  btc-5m   τ 03:18 | S0 64,065.57[CL] S* 64,102.10 | fair Up 0.702 Dn 0.298 | Up 0.68x81/0.69x146  Dn 0.31x15/0.32x194 | edge after fee: Up +0.1¢ Dn -3.7¢
```

| Field | Meaning |
|---|---|
| `CB` | Coinbase BTC-USD (mid of best bid/ask by default). `lag` = local receive time minus Coinbase's timestamp. `age` = time since the last update |
| `CL` | Chainlink BTC/USD, the price these markets **resolve on**, relayed by Polymarket's RTDS websocket |
| `basis` | EWMA of Coinbase − Chainlink, aligned on Chainlink's observation time, ± its std |
| `σ` | realized vol, annualized. `boot+live` means 1-minute Coinbase candles are filling in until N minutes of live 1-second samples exist |
| `τ` | time left in the window |
| `S0[CL]` | window start price: the Chainlink tick at the window's start time |
| `S*` | Coinbase price minus basis, i.e. Coinbase moved onto Chainlink's scale |
| `fair Up` | Φ(ln(S*/S0) / (σ·√τ)) |
| `Up 0.71x74/0.72x27` | Up token best bid 0.71 (74 shares) / best ask 0.72 (27 shares). A trailing `≠` means the local book disagrees with the server's top of book (it resyncs automatically) |
| `edge after fee` | fair − best ask − taker fee per share at that ask. This is a display aid; the real signal logic (stage 2) also subtracts slippage and the safety buffer |

If the bot starts mid-window, that window shows `S0 missed`: it never saw the
start tick, so it can't price that window. The next window works normally.

**What to sanity-check in the first few minutes:**
- `discover` shows `rules verified: True`, resolution source `data.chain.link/streams/btc-usd`, and fee `rate=0.07 exponent=1`.
- `basis` settles at a few dollars, and CB and CL track each other.
- `fair Up` sits near the book's mid most of the time (big persistent gaps usually mean a model or data problem, not free money).
- After a few windows close, `data/paperbot.log` shows `RESOLVED …: Up (chainlink predicted Up)`. The one-minute summary line has an `oracle check agree/total`. If our Chainlink start/end prices ever predict the wrong outcome, you'll see a warning, and S0 must be fixed before any P&L means anything.

### Offline demo (no network)

A fake exchange serves the same payload shapes locally (short 30 s/60 s
windows, random websocket drops):

```bash
python -m tests.fake_exchange --port 8765 --drop-every 20 &
python -m polybot --config tests/fake_config.toml watch
```

## What was verified against the docs, and what changed from the original spec

The container this was built in could not reach `docs.polymarket.com`,
Polymarket's APIs or Coinbase (egress policy). Instead I verified against:
Polymarket's official V2 SDK source on GitHub (`py-clob-client-v2`,
`clob-client-v2`), Polymarket's `agent-skills` reference, the search-indexed
docs pages (Fees, Get fee rate, Market channel, RTDS, V2 migration), and a
captured live Gamma payload for these markets. `discover` re-checks all of
it live.

1. **Per-token fee endpoint → market-level fee parameters.** `GET
   /fee-rate?token_id=` still exists, but it returns the legacy V1 `base_fee`
   in bps (1000 on these markets), and that is **not** what's charged.
   Since CLOB V2 went live (Apr 28, 2026), taker fees are computed at match
   time from per-market parameters: `GET /clob-markets/{condition_id}` →
   `fd.r` (rate) and `fd.e` (exponent). This is what the official V2 SDKs
   read, and Gamma mirrors it as `feeSchedule`. The bot uses `fd`,
   cross-checks it against Gamma, and logs any mismatch.
2. **Fee formula:** `fee = shares × rate × (p·(1−p))^exponent`, in pUSD,
   rounded to 5 decimals. Crypto is currently `rate 0.07, exponent 1`, which
   is $1.75 per 100 shares at 50¢. Takers only; makers pay nothing.
3. **Buy-side fee currency is ambiguous.** Older docs text says buy fees are
   taken in shares. The V2 SDKs charge them in pUSD on top of notional. The
   default follows the SDKs (`fees.buy_fee_in = "collateral"`). It's
   configurable, and the P&L difference is second-order.
4. **S0 comes from Chainlink, not Coinbase.** Each market's description says
   it resolves on the **Chainlink BTC/USD data stream**, and that Up wins if
   the end price is **greater than or equal to** the start price (ties go
   Up). Coinbase and Chainlink differ by a few dollars, which is decisive
   near expiry. So S0 is the Chainlink price at the window start (via
   Polymarket's RTDS `crypto_prices_chainlink` topic), and Coinbase stays the
   fast feed, shifted by the measured basis. Set `model.strike_source =
   "coinbase"` and `basis_correction = false` to get your formula with
   Coinbase-only inputs.
5. **5-minute markets exist** (since Feb 2026). Slugs are
   `btc-updown-15m-<start>` and `btc-updown-5m-<start>`, where `<start>` is
   the window start in UTC epoch seconds. Both are tracked. The window
   timing is cross-checked against `eventStartTime`/`endDate`.
6. **Market websocket:** subscribing with `custom_feature_enabled: true` also
   enables `best_bid_ask` / `market_resolved` events. The bot uses the
   server's best bid/ask (which also comes on every `price_change`) to detect
   local-book drift and resubscribe. It sends `PING` every 10 s as
   documented.
7. **Collateral is pUSD** (1:1 USD-backed) since V2, not USDC.e. The paper
   bankroll is simply in dollars.
8. **Maker rebates:** 20% of taker fees (`rebateRate 0.2`) are paid to
   makers. This doesn't affect a taker-only simulation. It's noted because
   it's part of why taking liquidity is expensive here.

## Data

Everything goes to SQLite (`data/paperbot.sqlite`) and a rotating log
(`data/paperbot.log`):

- `markets`: every window seen, including full description, fee parameters
  and source, rules check, Chainlink/Coinbase start and end prices, our
  predicted outcome, and Polymarket's posted resolution.
- `fv_snapshots`: fair value, inputs and top of book every 5 s per live
  window (roughly 5 MB/day).
- `events`: warnings, discovery failures, fee/rules notes, and oracle
  mismatches.

```bash
sqlite3 data/paperbot.sqlite "SELECT slug, s0_chainlink, end_chainlink, chainlink_predicted, resolved_outcome FROM markets ORDER BY start_ts DESC LIMIT 20"
```

## Running it for several days

- **Keep the clock synced** (NTP; this is on by default on macOS and most
  Linux). Feed lags are computed from exchange timestamps, and window
  boundaries come from the wall clock.
- Run it in `tmux`/`screen`, or as a service so it restarts after crashes or
  reboots:

  ```ini
  # ~/.config/systemd/user/polybot.service
  [Unit]
  Description=polybot paper trading
  After=network-online.target

  [Service]
  WorkingDirectory=%h/tarderz
  ExecStart=%h/tarderz/.venv/bin/python -m polybot watch
  Restart=always
  RestartSec=10

  [Install]
  WantedBy=default.target
  ```

  Then run `systemctl --user enable --now polybot` and `loginctl
  enable-linger $USER`. (From stage 3 on, the command becomes `run`.)
- Restarts are safe. State lives in SQLite, and any window that closed while
  the bot was down is re-polled on startup until Polymarket posts its
  resolution. After a restart, the window in progress is skipped (its start
  tick was missed).
- Websockets auto-reconnect with backoff. A connection that goes quiet for
  too long is recycled even if it never closed.
- A laptop that sleeps will miss windows. Use a machine that stays awake.
- Outbound hosts needed: `gamma-api.polymarket.com`, `clob.polymarket.com`,
  `ws-subscriptions-clob.polymarket.com`, `ws-live-data.polymarket.com`,
  `ws-feed.exchange.coinbase.com`, `api.exchange.coinbase.com`.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Unit tests cover fees, fair value, the vol and basis estimators, book
maintenance, message parsing (in the documented payload shapes) and
resolution parsing. `tests/test_integration.py` runs the real app for about
30 s against the fake exchange, with 6 s/12 s windows and forced websocket
drops. It checks reconnects, rollover, boundary prices, resolutions and
snapshots end-to-end.

## Known limitations / open questions

- **Exact boundary tick.** Polymarket doesn't document exactly which
  Chainlink report counts as "the price at the beginning/end". The bot takes
  the report stamped exactly at the boundary, else the first one after it
  (up to 5 s late). The oracle check measures whether that matches real
  resolutions.
- **Your latency is not the fastest.** The simulated 300 ms delay is
  configurable. Real competitors are co-located and faster, so treat
  results as an upper bound.
- **Chainlink via RTDS** is a relay. Its delivery lag is shown as `CL lag`.
