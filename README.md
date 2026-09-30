# Tradlysis

An AI-assisted, systematic FX trading bot, implementing the *AI-Assisted Systematic FX
Trading Bot: Technical Architecture & V1 Product Specification*.

V1 is an **experiment, not a claim of profitability**. It trades **EUR/USD** with one setup
(**trend pullback**) on an **OANDA Practice (demo) account**. It combines deterministic
technical analysis with structured news context and uses an LLM (`typesafe/jev-1.13` via
OpenRouter) only to confirm setups. A deterministic risk engine has the final say. Every
decision opportunity is recorded, including WAITs and rejections.

> Retail FX trading carries substantial risk. Demo results are not proof of future
> profitability, and live execution differs from demo execution.

---

## What you need to provide

Nothing below is committed to the repository. Put it in `.env` (copy `.env.example`).

| What | Variables | Where to get it |
|---|---|---|
| OANDA practice account | `OANDA_PRACTICE_API_TOKEN`, `OANDA_PRACTICE_ACCOUNT_ID` | oanda.com → practice account → *Manage API Access* |
| PostgreSQL database | `DATABASE_URL` | any PostgreSQL 14+ (managed, or the bundled `db` service) |
| OpenRouter | `OPENROUTER_API_KEY` (model defaults to `typesafe/jev-1.13`) | openrouter.ai → Keys |
| Dashboard password | `DASHBOARD_PASSWORD` | choose one; the API stays locked until set |
| Telegram alerts (optional) | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | @BotFather; steps in `.env.example` |

Then check everything with the setup checker, which never trades:

```bash
python -m app.doctor              # or: docker compose run --rm engine python -m app.doctor
python -m app.doctor --telegram   # also sends a Telegram test message
```

## Database: Prisma schema + migrations (you run them)

`prisma/schema.prisma` is the single source of truth for the schema. The initial migration
is in `prisma/migrations/`. Apply migrations yourself:

```bash
npm ci                     # installs the Prisma CLI (only needed for migrations)
npm run migrate:deploy     # = prisma migrate deploy, uses DATABASE_URL from .env
# or with Docker:
docker compose run --rm migrate
```

The engine is Python, and Prisma's official client is TypeScript-only (the community Python
client is archived). So the Python code reads and writes through SQLAlchemy models
(`app/db/models.py`) that mirror the Prisma schema. `tests/test_schema_sync.py` fails if
they ever drift apart. It checks the models against both `schema.prisma` and a database
built from the migration SQL.

**Changing the schema:** edit `schema.prisma`, run `npx prisma migrate dev --name <change>`
against a development database, update `app/db/models.py`, then run the tests.

## Running it (Docker on a VPS)

```bash
cp .env.example .env          # fill in credentials
docker compose build
docker compose run --rm migrate
docker compose up -d engine api analyzer
docker compose logs -f engine
```

| Service | Command | Role |
|---|---|---|
| `engine` | `python -m app.engine` | market data, technicals, news, decisions, risk, execution, reconciliation |
| `api` | `uvicorn app.api.main:app` | dashboard + control plane (port 8000, bound to localhost) |
| `analyzer` | `python -m app.experiments.analyzer` | hourly experiment statistics; never trades |
| `migrate` | `prisma migrate deploy` | one-shot, run manually |
| `db` | PostgreSQL 16 | optional (`--profile local-db`) if you don't have a database |

The API is bound to `127.0.0.1:8000`. To use the dashboard from your phone, put a TLS reverse
proxy in front of it (for example Caddy: `your.domain { reverse_proxy 127.0.0.1:8000 }`), or
use an SSH tunnel.

### Local development (without Docker)

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m app.engine                            # engine
uvicorn app.api.main:app --reload --port 8000   # dashboard
python -m app.experiments.analyzer --once       # print a report
```

## Dashboard (desktop and mobile)

On desktop the pages share a sidebar with navigation and live status: mode, engine,
price stream, whether trading is allowed, and alerts. On phones this becomes a top bar
with Overview/History tabs.

Open `https://<your-host>/` and sign in with `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD`.

- **Last decision:** the path the latest 15-minute cycle took (candle → setup → model →
  risk → order) and where it stopped, linking to its timeline.
- Status: demo/live, engine heartbeat, kill switch and breaker state, NAV, today's P/L,
  price and spread.
- NAV chart for the last 7 days, with a table view.
- **Controls:** kill switch (stops new orders at once), *close all trades*, reset the
  daily or drawdown breaker.
- Open and closed trades (with R multiples), every 15-minute decision opportunity (tap one
  to see the exact snapshot sent to the model, its raw answer and every risk check), the
  experiment summary, the news calendar, central-bank reads and system events.

### History page (`/history`)

Linked from the navigation. On desktop it is a split view: the list stays on the left and the
selected item's timeline shows on the right. Move through items with ↑/↓. It lists every trade taken and every setup that was
stopped, grouped by day.
- **Filters:** All, Taken, Stopped.
- **Summary:** trades won and lost, plus how many stopped setups *would have* won or lost.

Tap an item to open its **timeline tree**. The tree follows the chain:
1. candle close
2. setup check, with each rule ✓/✗ and the trade plan
3. market context (timeframes, regime, news)
4. the model's decision and rationale
5. risk engine: failed checks first, then all of them
6. order: fill, slippage
7. trade opened
8. broker transactions
9. close, with R and P/L

A stopped chain is marked **■ Stopped here**. For stopped setups, a final dashed node shows
**what would have happened**: whether the planned take-profit or stop-loss was hit first in
the following 5 days. This is estimated from 15-minute mid-price candles, so it ignores spread
and slippage. Each timeline has a price chart with entry, stop-loss and take-profit lines and
markers for the decision, open and close. It also has a table view, and raw data can be expanded
at every step.

The API never talks to the broker. *Close all trades* sets a flag that the engine's
executor acts on within about 5 seconds, so the executor stays the only component that can
place orders.

## How a decision is made (every completed 15-minute candle)

1. Sync broker candles (M15, H1, H4, D, W, month). The broker's candles are authoritative.
2. **Technicals** (deterministic): EMA 20/50/200, RSI 14, ATR 14, swing structure
   (HH/HL/LH/LL), trend per timeframe, volatility regime and support/resistance:
   clustered H1/H4 swings, previous day/week/month high and low, and round-number
   (psychological) levels such as 1.1800. Stored in `technical_snapshots`,
   `support_resistance` and `market_regimes`.
3. **News**: ForexFactory calendar (event, currency, impact, forecast, actual, surprise).
   Python, not the LLM, turns a surprise into a currency and pair impact (for example, CPI
   below forecast means USD bearish, so EUR/USD up; unemployment is inverted). Fed/ECB press
   releases are interpreted by the LLM into a currency bias with confidence and reason codes.
   The result gives a news risk level and a blackout flag (±30 min around high-impact USD/EUR
   events).
   *Limitation:* the free ForexFactory weekly feed normally carries forecast and previous
   values but not the actual release, so surprise and impact only appear when an actual is
   present. The blackout (the safety-critical part) works regardless. A paid calendar can be
   added by implementing `CalendarProvider` in `app/news/providers/`.
   - **Overall market regime** (deterministic): strong uptrend, strong downtrend, breakout,
     compression, range, event risk or unclear. The trend-pullback setup suits the strong
     trends; the analyzer reports results per regime.
4. **Trend-pullback check** (deterministic, `app/strategy/trend_pullback.py`):
   - H4 trend sets the direction.
   - H1 trend must not oppose it.
   - M15 price has pulled back into a support/resistance zone or the M15 EMA50, by at least
     1× ATR.
   - Momentum confirms: candle direction, close vs EMA20, and RSI turning.
   - Stop sits beyond the pullback plus 0.25 ATR; the target is the next opposing level
     (or 2R), capped at 4R.
   - The setup needs R:R of at least 1:2 (Experiment #1).
5. **Market snapshot** is built and stored in `decision_requests` (unique per candle, so a
   restart never processes a candle twice).
6. **Decision model** via OpenRouter, schema-constrained to `BUY | SELL | WAIT` with setup,
   confidence and reason codes. It can confirm or decline the plan but **cannot change
   entry, stop, target, size or any limit**. Invalid output, errors and timeouts become WAIT.
   By default the model is only called when step 4 finds a candidate
   (`LLM_CALL_POLICY=candidates_only`); every other interval is still recorded as a
   prefilter WAIT with its reasons. Set `LLM_CALL_POLICY=always` to query it every interval.
7. **Risk engine** (`app/risk/engine.py`) evaluates and records every check:
   - kill switch; daily-loss and drawdown breakers
   - account health; confidence; plan/decision agreement
   - market open; price freshness; spread
   - news blackout and calendar freshness
   - existing positions; unresolved orders; signal cooldown
   - stop distance and R:R re-checked at the *current* price
   - total open risk; margin

   It sizes the position itself: 0.25% of equity by default, converted to the account
   currency.
8. **Executor** (the only order submitter): writes the order row first, then sends a
   `MARKET` / `FOK` / `OPEN_ONLY` order with stop-loss, take-profit, a slippage `priceBound`
   and a unique client ID. A timeout is never treated as filled or as not filled: the order
   is looked up by client ID, and resubmitted only if the broker confirms it doesn't exist.
9. **Reconciliation** (every 15 s): account summary, open trades, trade closes with R,
   broker transaction IDs, stuck orders, unexpected positions (critical alert), peak and
   start-of-day NAV, and the circuit breakers.

## Safety controls

- Demo by default. Live mode requires `TRADING_MODE=live`, the exact
  `LIVE_TRADING_CONFIRM` phrase and separate `OANDA_LIVE_*` credentials.
- Risk settings have hard ceilings: at most 1% per trade, 2% total risk, 5% daily loss
  and 20% drawdown.
- Global kill switch (dashboard), and daily-loss (auto-resets next trading day at 17:00 New
  York) and max-drawdown (manual reset) circuit breakers.
- Duplicate protection:
  - one decision per candle
  - one order per approved risk check
  - idempotent client order IDs
  - no new orders while any order is unresolved
- Telegram alerts for:
  - stream disconnects and stale prices
  - unexpected positions
  - broker/API errors
  - fills, closes and rejections
  - breaker trips
  - engine start/stop

## Experiment analysis

`python -m app.experiments.analyzer --once` prints (and stores in `analysis_reports`):
- opportunities, setup candidates, model calls, decisions, and rejection/WAIT reasons
- wins/losses, average winner and loser in R, expectancy, profit factor, max drawdown (in
  R and account %)
- performance by setup condition, overall market regime, volatility and trend regime, news risk, model-confidence
  bucket and direction
- slippage, spread and LLM latency

## Tests

```bash
pip install -e ".[dev]"
pytest -q                                      # unit tests (no database needed)
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/tradlysis_test pytest -q   # + integration
```

The integration tests rebuild the test database from the Prisma migration SQL. They run
the executor, reconciliation, full decision cycles and the API against a fake OANDA broker
and a fake LLM. CI (`.github/workflows/ci.yml`) runs everything against PostgreSQL 16 and
also fails if `schema.prisma` has changes without a migration.

## Project layout

```
app/
  config/          settings (env vars, safety validation)
  broker/          OANDA v20 REST + streaming client
  market_data/     price stream, market state, candles, market hours
  technicals/      indicators, structure, levels, regimes, technical state
  news/            calendar/RSS providers, LLM interpretation, news state
  snapshot/        market snapshot builder
  strategy/        deterministic trend-pullback rules and trade plan
  decision/        OpenRouter client, prompts (versioned), response schema
  risk/            deterministic risk engine and currency conversion
  execution/       order executor (only component that submits orders)
  reconciliation/  broker ↔ database reconciliation and circuit breakers
  experiments/     analyzer (separate process)
  api/             FastAPI control plane + dashboard (static/index.html)
  alerts/          Telegram + system events
  db/              SQLAlchemy models mirroring Prisma, sessions, control flags
  engine.py        orchestrator (python -m app.engine)
  doctor.py        setup checker (python -m app.doctor)
prisma/            schema.prisma + migrations (source of truth for the database)
docker/            Dockerfiles
tests/
```

## Out of scope for V1 (per the specification)

These are deliberately not built:
- multiple pairs, high-frequency trading and real-money trading
- large indicator collections
- LLM-created strategies, LLM-controlled sizing or bypassing risk limits
- arbitrage
- automatic strategy optimisation before enough live-demo data exists

TradingView (optional in the spec) and Redis ("later if needed") are also not used.
