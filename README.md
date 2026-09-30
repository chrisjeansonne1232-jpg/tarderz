# polybot: paper-trading research bot for Polymarket BTC Up/Down

Tests whether a latency / fair-value strategy on Polymarket's short-term
"Bitcoin Up or Down" markets (15-minute and 5-minute) still has an edge after
fees. **Paper trading only.** The project has no order-placement code and
needs no private key, wallet or API secret. Every endpoint it touches is
public and read-only.

| Stage | What | Status |
|---|---|---|
| 1 | Live data (Gamma discovery, CLOB order books, Coinbase, Chainlink) + fair value next to the book | **done** (`watch`) |
| 2 | Signals: ask below fair value by more than fee + slippage + buffer | **done** (`run`) |
| 3 | Simulated taker fills: latency, book walking, fees, zero-fee shadow P&L, settlement | **done** (`run`) |
| 4 | `report`: win rate, P&L, fees, edge at entry vs realized, drawdown, 95% CI | **done** (`report`) |

Monitoring dashboard (`--dashboard`):

| Stage | What | Status |
|---|---|---|
| 1 | Server + WebSocket, header, ticker strip, wallet card, execution log, stale handling | **done** |
| 2 | BTC candle chart, order book ladder | next |
| 3 | Signal map, equity curve, trades table, streak card | |
| 4 | Analytics histograms, footer polish, `dashboard --replay` | |

## Quick start

**Windows:** open PowerShell (Start menu → type "PowerShell" → Enter) and
paste this one line:

```powershell
irm https://raw.githubusercontent.com/chrisjeansonne1232-jpg/tarderz/HEAD/install.ps1 | iex
```

This one line:
1. Installs the bot into `C:\Users\<you>\polybot`. If you don't have Python
   3.11+, it offers to install it with winget.
2. Sets up the bot and checks the live markets.
3. Starts paper trading and opens the dashboard in your browser.
4. Adds **Polybot** and **Polybot (iPad)** shortcuts to your desktop for next
   time.

It also keeps the PC from sleeping while the bot runs. Keep the window open;
Ctrl+C stops the bot. Paste the same line again to update; your settings and
data are kept.

**Mac or Linux:** open Terminal and paste this one line:

```bash
curl -fsSL https://raw.githubusercontent.com/chrisjeansonne1232-jpg/tarderz/HEAD/install.sh | bash
```

This one line:
1. Downloads the bot into `~/polybot`.
2. Checks for Python 3.11+. On a Mac with Homebrew, it offers to install it.
3. Sets up a private environment and installs the dependencies.
4. Checks the live markets.
5. Starts paper trading with the dashboard and opens http://127.0.0.1:8787 in your browser.

On a Mac it also keeps the computer awake while the bot runs. Press Ctrl+C to stop.

- **Later:** `bash ~/polybot/start.sh`. In Finder you can also double-click
  `Start Polybot.command` in `~/polybot`.
- **Update:** paste the same one-liner again. Your `data/` and `config.toml`
  are kept.
- **On your iPad too:** `bash ~/polybot/start.sh --ipad`. It prints the
  address to open in Safari on the same wifi.

## Manual setup

Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # aiohttp, fastapi, uvicorn, websockets
python -m polybot run --dashboard        # --host 0.0.0.0 to allow other devices
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
python -m polybot --config tests/fake_config.toml run --dashboard
```

The fake market reprices a little slowly, so the paper engine finds
"edges" there. Its P&L means nothing; it only exercises the pipeline. The
dashboard shows a **TEST** badge whenever the endpoints aren't Polymarket's
production hosts. `curl "localhost:8765/admin/pause?feed=coinbase&seconds=15"`
stalls one feed to check the STALE handling.

## Paper trading (`run`)

```bash
python -m polybot run               # add --dashboard for the web dashboard
python -m polybot report            # stats from the database, any time
```

`run` does everything `watch` does, plus the simulated strategy. It prints a
one-line status every 60 s (bankroll, open positions, trades today, net P&L
today with the zero-fee number next to it). **No order is ever sent
anywhere.** The engine only reads the local copy of the public order book.

On each Coinbase update, for each live window and each side (Up/Down):

1. **Fair value** `p = Φ(ln(S/S0) / (σ·√τ))` (see Stage 1).
2. **Signal** when `fair − VWAP − fee/share − slippage_allowance > safety_buffer`.
   VWAP is the average price of walking the asks for the intended size, so
   book-walk slippage counts against the edge. `safety_buffer` defaults to
   1¢. No new entries in the last `min_seconds_remaining` (10 s).
3. **Latency.** The order "arrives" `sim.latency_ms` (300 ms) later and fills
   against the book **as it looks then**. The order limit is the signal ask
   plus `max_slippage`. If the ask moved up, `adverse_move = "take"` pays the
   worse price (up to the limit); `"skip"` drops the trade.
4. **Honest fills.** The fill never takes more than the displayed depth.
   Size we already "took" at a price stays hidden from later paper fills for
   `liquidity_memory_s`, because our fills don't really remove liquidity.
   Fees are charged per matched level, rounded as documented. Order sizes
   respect `max_trade_usd`, `max_window_usd`, available paper cash, and the
   market's minimum order size.
5. **Settlement.** Positions are held to resolution. Each is settled from
   Polymarket's posted outcome (Gamma, polled after the window closes).
   Until that's posted, the position shows `PENDING`, and it's settled
   later, even across restarts. Every trade also carries a zero-fee shadow
   P&L (same trades, no fees) so you can see exactly what fees cost.

Every evaluated opportunity is logged to `signals`: signals taken, and
skips with their reason, throttled to one per window/side every 5 s. Every
fill and settlement goes to `trades`, and every log line to `exec_log`.

`report` prints trade count, win rate, gross P&L, total fees, net P&L,
zero-fee P&L, average edge at entry vs. realized (¢/share), max drawdown,
profit factor, and a 95% t-interval on average P&L per trade.

## Dashboard (`--dashboard`)

```bash
python -m polybot run --dashboard   # then open http://127.0.0.1:8787
```

A read-only web terminal served from inside the bot's own event loop
(FastAPI + uvicorn). It has no buttons that trade or change settings, and
messages sent to its WebSocket are ignored. The account is always labelled
**PAPER**.

- **iPad / other devices:** set `dashboard_host = "0.0.0.0"` under
  `[dashboard]` in `config.toml`, then open `http://<your computer's LAN
  IP>:8787`. Anyone on your wifi can view it (not change it). Your OS
  firewall may ask to allow incoming connections.
- **Offline:** the page, chart library (TradingView lightweight-charts,
  Apache-2.0) and font (JetBrains Mono, OFL) are all served locally from
  `polybot/dashboard/static/`.
- **Protocol:** one WebSocket at `/ws`. It sends a full snapshot on connect,
  then prices and books at `tick_hz` (4/s), plus trades, signals and log
  lines the moment they happen. `/api/snapshot` returns the same snapshot as
  JSON.
- **Every number comes from the bot.** There is no sample or animation data.
  If a feed is quiet for more than `stale_after_s` (3 s), everything that
  depends on it greys out and is labelled `STALE`. If the dashboard loses
  its connection to the bot, everything greys out and a banner says so.
  Feed dots show ms since each feed's last message. Gamma is a REST API, so
  it's polled every `markets.gamma_heartbeat_s` (2 s) to keep its dot
  meaningful.
- **Header:** net P&L today is the headline; the zero-fee number sits
  smaller next to it. "Today" means your `dashboard.timezone`
  (America/Chicago by default).
- Click the market name to switch between the 15m and 5m markets. Click a
  log tag to filter the log. Hover the log to pause auto-scroll (on an iPad,
  tap it).

Screenshots for layout checks: `python tools/screenshot.py http://127.0.0.1:8787 shots/`
(needs `pip install playwright`). It captures 1440×900 and 1180×820, and
reports anything that overflows or scrolls.

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
4. **S0 is Polymarket's own start price, not Coinbase's.** Each market's
   description says it resolves on the **Chainlink BTC/USD data stream**,
   and that Up wins if the end price is **greater than or equal to** the start
   price (ties go Up). Coinbase and Chainlink differ by a few dollars, which
   is decisive near expiry.
   - S0 is Polymarket's published "price to beat" (Gamma
     `eventMetadata.priceToBeat`), once it appears.
   - Until then, S0 is the Chainlink report stamped exactly at the window start
     (from Polymarket's RTDS `crypto_prices_chainlink` topic).
   - Every window the two are compared. A gap over $0.50 is logged as
     `S0 MISMATCH`, and `report` summarises the matches.
   - Coinbase stays the fast feed, shifted by the measured basis.
   - To get your formula with Coinbase-only inputs, set
     `model.strike_source = "coinbase"` and `basis_correction = false`.
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
- `signals`: every evaluated opportunity, including fair value, ask, VWAP,
  fee, edges, size, and decision (filled/skipped + reason).
- `trades`: every paper fill, including the levels it walked, fee, edge at
  entry, status (OPEN/PENDING/WON/LOST), and P&L with the zero-fee shadow.
- `exec_log`: every execution-log line shown on the dashboard.
- `snapshots_1s`: once a second per series: spot, Chainlink, basis, σ, S0,
  fair value, top of book, plus a compressed 5-level ladder and feed ages
  (this drives replay; roughly 25–50 MB/day).
- `candles_1m`: 1-minute BTC candles built from Coinbase ticks.
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
  ExecStart=%h/tarderz/.venv/bin/python -m polybot run --dashboard
  Restart=always
  RestartSec=10

  [Install]
  WantedBy=default.target
  ```

  Then run `systemctl --user enable --now polybot` and `loginctl
  enable-linger $USER`.
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
resolution parsing. `tests/test_engine.py` pins down the fill rules:
- the fill uses the post-latency book
- it respects depth, limit and budget
- liquidity we already took stays hidden
- it follows the adverse-move policy

It also checks the skip reasons, the window cap, settlement, the zero-fee
math and the statistics.

`tests/test_integration.py` runs the real app in `run` mode, with the
dashboard, for about 30 s against the fake exchange. It uses 6 s/12 s
windows and forced websocket drops. It checks reconnects, rollover, boundary
prices, resolutions, paper P&L consistency, the dashboard protocol (snapshot
first, then ~4 ticks/s), and that shutdown takes under 4 s.

## Known limitations / open questions

- **Exact boundary tick.** Polymarket doesn't document exactly which
  Chainlink report counts as "the price at the beginning/end". The bot only
  accepts a report stamped exactly at the boundary (`boundary_max_delay_s =
  0`). Two checks in `report` verify this against reality:
  - the S0 check (our start price vs Polymarket's published price to beat)
  - the outcome check (our Chainlink start/end prices vs Polymarket's
    posted result)
- **Your latency is not the fastest.** The simulated 300 ms delay is
  configurable. Real competitors are co-located and faster, so treat
  results as an upper bound.
- **Chainlink via RTDS** is a relay. Its delivery lag is shown as `CL lag`.
