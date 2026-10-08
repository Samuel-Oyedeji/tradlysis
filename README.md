# Tradlysis

An AI-assisted, systematic FX trading bot, implementing the *AI-Assisted Systematic FX
Trading Bot: Technical Architecture & V1 Product Specification*.

V1 is an **experiment, not a claim of profitability**. It runs one or more **experiments**
side by side (V1's is **EUR/USD** with one setup, **trend pullback**) on one **Capital.com demo
account**. Each experiment has its own pair, parameters, capital and risk limits (see
[Experiments](#experiments)), and everything is configured in the dashboard. It combines deterministic
technical analysis with structured news context and uses TypeSafe's Jev decision model
(`typesafe/jev-1.13`, through OpenRouter's Decisions API) only to confirm setups. A deterministic risk engine has the final say. Every
decision opportunity is recorded, including WAITs and rejections.

> Capital.com is a CFD broker, so this is a **CFD/FX trading experiment**: positions are
> leveraged EUR/USD contracts for difference, not spot interbank EUR/USD. CFDs are complex,
> leveraged products and most retail CFD accounts lose money. Demo results are not proof of
> future profitability, and demo execution and liquidity differ from live conditions.

---

## What you need to provide

Nothing below is committed to the repository. The server's `.env` (copy `.env.example`) holds
only what is needed before the database can be read, plus the trading mode:

| What | Variables | Where to get it |
|---|---|---|
| PostgreSQL database (Supabase) | `DATABASE_URL` | Supabase → *Connect* → **Session pooler** connection string (see below); any PostgreSQL 14+ also works |
| Dashboard login | `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD` | choose them; the API stays locked until set |
| Config page password | `CONFIG_PASSWORD` | choose one, different from the dashboard password; the Config page stays locked until set |
| Trading mode (optional) | `TRADING_MODE`, `LIVE_TRADING_CONFIRM` | `demo` by default; real money can only be switched on here, on the server |

Everything else is entered on the dashboard's **Config page** (`/config`, see
[Config page](#config-page-config)) and stored in the database:

| What | Settings | Where to get it |
|---|---|---|
| Capital.com demo account + API key | API key, login e-mail, API key password; optional account ID | capital.com → enable 2FA → *Settings → API integrations → Generate API key* |
| OpenRouter | API key (model defaults to `typesafe/jev-1.13`, per experiment) | openrouter.ai → Keys |
| Telegram alerts (optional) | bot token, chat ID | @BotFather → `/newbot`; message the bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` for the chat ID |
| Experiments | pair, capital, risk limits, strategy and model settings | your choice |

Upgrading from a version where everything was in `.env`: keep the old `.env` for the first start.
The engine (or API) copies those values into the database once, and turns `EXPERIMENT_NAME`,
`INSTRUMENT` and the risk settings into the first experiment, with its history. Afterwards you
can delete everything but the variables above from `.env`; the Config page lists any that are
still there but no longer used.

Then check everything with the setup checker, which never trades:

```bash
python -m app.doctor              # or: docker compose run --rm engine python -m app.doctor
python -m app.doctor --telegram   # also sends a Telegram test message
```

It checks the configuration the engine would use (the Config page's settings over `.env`) and
lists every experiment with its limits. The Capital.com check logs in on the demo host, then reads
the account, your hedging-mode preference and, for each enabled experiment's market, its pip size,
minimum size, margin, status and three M15 candles, and waits up to 15 s for a live quote on the
WebSocket stream.

## Broker: Capital.com

| What the bot needs | Capital.com API |
|---|---|
| Session | `POST /session` (API key + login + API-key password) → `CST` / `X-SECURITY-TOKEN`, renewed automatically on expiry (10 min idle) |
| Account balances | `GET /accounts` (equity = `balance`, cash = `deposit`, open P/L = `profitLoss`, `available`) |
| Instrument rules | `GET /markets/EURUSD` (decimals, minimum deal size, margin factor, market status) |
| Candles | `GET /prices/EURUSD` (M5 … W; monthly bars are built from daily bars) |
| Live prices | WebSocket `marketData.subscribe` on the streaming host from the login response, pinged every minute |
| Orders | `POST /positions` (market, with `stopLevel` / `profitLevel`), then `GET /confirms/{dealReference}` |
| Open positions / closes | `GET /positions`, `DELETE /positions/{dealId}` |
| Close details, audit trail | `GET /history/activity` (source SL / TP / USER …) and `GET /history/transactions` |

What the Capital.com API does not provide, and how the bot compensates:
- **No client order IDs.** Each order row gets our own ID and stores Capital.com's
  `dealReference`. If the order request times out before a reference comes back, the order is
  resolved from open positions and the activity history (matching instrument, direction, size,
  stop, target and time), and it is **never resubmitted**. A position that shows up later is
  linked to that order instead of being treated as unexpected.
- **No price bound on market orders.** A fill worse than `MAX_SLIPPAGE_PIPS` from the price the
  risk engine approved is closed straight away and alerted (`SLIPPAGE_EXCEEDED`).
- **No "open only" flag.** Right before sending an entry, the executor re-reads open positions
  and the account's hedging mode. With hedging off (Capital.com nets positions per market), it
  refuses if *any* position is open on the pair, whichever experiment owns it, so an order can
  never net against an open position. With hedging on, positions stay separate: it refuses only
  while this experiment, or a position no experiment owns, holds the pair. Two experiments on
  the same pair therefore need hedging mode on to trade at the same time.
- **P/L of closed trades** is computed from the open and close prices (exact for a USD account
  on EUR/USD). The broker's own cash movements, including overnight funding, are stored in
  `broker_transactions`.
- Sizing assumes one unit of deal size is one euro. The engine refuses to start if Capital.com
  reports a lot size other than 1.

Before letting the engine trade, test these steps once on the demo account:
1. log in
2. read the market
3. receive streamed prices (`python -m app.doctor` covers 1–3)
4. watch the first order fill in the dashboard with its stop-loss and take-profit attached
5. try *close all trades*

Check that the position size in Capital.com's platform matches the units shown on the dashboard.

## Database: Supabase

The bot uses your Supabase project as a plain PostgreSQL database. Only the connection string
is needed, not the Supabase API URL or keys. In the Supabase dashboard, click **Connect** and copy
the **Session pooler** URI into `.env`, replacing `[YOUR-PASSWORD]` with your database password:

```
DATABASE_URL=postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
```

- **Session pooler (port 5432) is the recommended choice.** It works for the engine and for
  Prisma, and it is reachable over IPv4, which most VPSs and Docker networks need.
- The **direct connection** (`db.<project-ref>.supabase.co:5432`) also works, but only over IPv6
  unless you have Supabase's IPv4 add-on.
- The **transaction pooler** (port 6543) works for the engine: the bot detects it and turns off
  prepared-statement caching. Prisma migrations cannot run through it, so use the session
  pooler URL if you ever run `npm run migrate:deploy`.
- If your password contains special characters (`@`, `:`, `/`, `#`, `?`), URL-encode them
  (for example `@` becomes `%40`).
- You don't need the bundled `db` service (`--profile local-db`) when using Supabase.

### Tables are created automatically

At start-up the engine, API and analyzer check for the bot's 21 tables and create any that are
missing, with the same indexes and foreign keys as the Prisma migration. They never alter, empty
or drop a table, and they never touch tables that aren't the bot's, so it is safe on a database
shared with other apps. Tables the bot creates get row-level security switched on (with no
policies), which keeps them out of Supabase's public REST API; the bot itself is unaffected.

```bash
python -m app.db.bootstrap --check   # report which tables are missing
python -m app.db.bootstrap           # create them now instead of waiting for the engine
```

`python -m app.doctor` reports the same. Set `DB_AUTO_CREATE_TABLES=false` to switch the automatic
creation off. Only *missing tables* are created: when a later version adds columns to an
existing table, that change still comes from a Prisma migration (the doctor lists missing columns).

## Database: Prisma schema + migrations (optional)

`prisma/schema.prisma` is the single source of truth for the schema. The initial migration
is in `prisma/migrations/`. You don't need Prisma to get started (see above). Use it when a
release ships a migration that changes existing tables:

```bash
npm ci                     # installs the Prisma CLI (only needed for migrations)
npm run migrate:deploy     # = prisma migrate deploy, uses DATABASE_URL from .env
# or with Docker:
docker compose run --rm migrate
```

`migrate deploy` only runs migration files that are not yet recorded in `_prisma_migrations`.
If the tables were created by the bot or by hand, record those migrations as applied first
(this runs no SQL): `npx prisma migrate resolve --applied 20260930084057_init`, and likewise
`20261006085014_config_and_experiments` once the bot has created the `app_config`,
`config_changes` and `experiments` tables. That migration only adds new tables, so the bot
creates them itself at start-up; no migration has to be run to deploy it.
**On a shared database never run `prisma migrate dev`, `prisma migrate reset` or `prisma db push`:**
they compare the whole database with this schema and can drop other apps' tables.

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
| `api` | `uvicorn app.api.main:app` | dashboard + control plane (host port 8710, bound to localhost) |
| `analyzer` | `python -m app.experiments.analyzer` | hourly experiment statistics; never trades |
| `migrate` | `prisma migrate deploy` | one-shot, run manually |
| `db` | PostgreSQL 16 | optional (`--profile local-db`) if you don't have a database |

The API is published on `127.0.0.1:8710` (container port 8000; the bundled `db`, if used, on
`127.0.0.1:5442`). Both host ports avoid ones commonly taken on a shared server and can be
changed with `API_HOST_PORT` / `DB_HOST_PORT` in `.env`; the list is at the top of
`docker-compose.yml`. To use the dashboard from your phone, put a TLS reverse proxy in front of
it (for example Caddy: `your.domain { reverse_proxy 127.0.0.1:8710 }`), or use an SSH tunnel.

### Automatic deployment (GitHub Actions)

`.github/workflows/deploy.yml` deploys every push to `main` (including merged pull requests)
once CI has passed on it. It SSHes into the server, goes to the checkout, fast-forwards it to
the commit CI tested, runs `docker compose build` and `docker compose up -d engine api
analyzer`, then waits for the API health check and checks that `engine` and `analyzer` are
running. After a healthy deploy it deletes the images the previous containers used, so old
builds don't fill the disk; images from other projects are not touched. A failure at any step
fails the job; the services already running are left as they are until `up -d` replaces them.
Migrations are not run (see above). The engine reconciles with the broker when it starts, so a
restart picks up open trades again. Work goes to `staging` first; merging `staging` into
`main` is what deploys.

One-time setup:

1. Make an SSH key just for deploys, on your own computer (not the server):
   `ssh-keygen -t ed25519 -f tradlysis_deploy -C github-actions-deploy -N ""`.
2. Append `tradlysis_deploy.pub` to `~/.ssh/authorized_keys` of the deploy user on the server.
   That user must be able to run `docker` without `sudo` and `git fetch` in the checkout, and
   the checkout must be on `main`.
3. In GitHub, Settings -> Secrets and variables -> Actions, add these repository secrets:

   | Secret | Value |
   |---|---|
   | `DEPLOY_HOST` | server IP or hostname |
   | `DEPLOY_USER` | the deploy user |
   | `DEPLOY_SSH_KEY` | the whole private key file `tradlysis_deploy` |
   | `DEPLOY_PATH` | absolute path of the checkout, e.g. `/home/ubuntu/tradlysis` |
   | `DEPLOY_HOST_FINGERPRINT` | output of `ssh-keygen -lf /etc/ssh/ssh_host_ecdsa_key.pub \| cut -d' ' -f2` run on the server (`SHA256:...`, nothing around it). The deploy action prefers the ECDSA host key over ED25519; if the server has no ECDSA key, use `ssh_host_rsa_key.pub` |
   | `DEPLOY_PORT` | only if SSH is not on port 22 |

4. Delete the local `tradlysis_deploy` private key once it is saved in GitHub.

To redeploy, re-run the failed Deploy job from the Actions tab. To roll back, revert the
commit on `main`; the revert deploys like any other push.

### Local development (without Docker)

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m app.engine                            # engine
uvicorn app.api.main:app --reload --port 8000   # dashboard
python -m app.experiments.analyzer --once       # print a report
```

## Dashboard (desktop and mobile)

On desktop the pages share a sidebar with the experiment picker, navigation and live status:
mode, engine, price stream, whether trading is allowed, and alerts. On phones this becomes a top
bar with the picker and tabs.

Open `https://<your-host>/` and sign in with `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD`.
Overview, History and Analysis show **one experiment**, picked in the sidebar. The browser
remembers the choice, and `?experiment=<id>` in a link selects one.

- **Last decision:** the path the experiment's latest 15-minute cycle took (candle → setup →
  model → risk → order) and where it stopped, linking to its timeline.
- Status: demo/live, engine heartbeat, kill switch and breaker state, the experiment's equity
  (its capital plus the P/L of its trades; the account's NAV is shown under it), today's P/L,
  price and spread.
- The experiment's equity for the last 7 days (its balance after each closed trade, then its
  live equity), with a table view.
- **Controls:** the experiment's kill switch (stops its new orders at once), *close this
  experiment's trades*, and reset its daily or drawdown breaker. A reset measures from the
  experiment's current equity: the daily loss until the next trading day, the drawdown from now
  on. Under *All experiments*: the account-wide kill switch and *close every trade on the
  account*.
- Open and closed trades (with R multiples), every 15-minute decision opportunity (tap one
  to see the exact snapshot sent to the model, its raw answer and every risk check), the
  experiment summary, the news calendar (first five items, the rest scroll), central-bank reads
  and system events.

### Experiments page (`/experiments`)

Every experiment side by side: running or switched off (and why it is halted), equity,
capital, realized and open P/L, trades, win rate, total R, risk per trade, model and last
decision, plus the shared account's NAV, open trades and margin. Each card opens that
experiment's overview or analysis.

### Config page (`/config`)

Replaces editing `.env` on the server. It needs the dashboard login **and** `CONFIG_PASSWORD`,
which unlocks the page in that browser tab for 15 minutes (extended while you use it; *Lock* ends
it early). After five wrong passwords it refuses further attempts for five minutes.

- **Experiments:** switch each one on or off, rename it, set its capital and any of its
  parameters (risk limits, trade filters, news rules, strategy settings, model and when to call
  it). *Reset* returns a parameter to the global default. The pair is fixed once an experiment
  has made a decision; create a new experiment for another pair.
- **New experiment:** id (permanent; stored with every decision and trade), name, pair,
  capital, and optionally the parameters of an existing experiment to start from. New
  experiments start switched off.
- **Global settings:** Capital.com demo and live credentials, OpenRouter, news sources,
  Telegram, safety and scheduling. Secrets are never sent back to the browser: a saved one shows
  as *saved*, and typing replaces it. *Reset* removes the saved value, so `.env` or the default
  applies again.
- **Recent changes:** who changed what and when. Secret values are never recorded.

Every value is validated with the same rules as before, including the hard risk ceilings,
before anything is saved. A saved change reaches the engine within about ten seconds: it lets
a running decision cycle finish, then restarts itself (in the same process) with the new
configuration. The trading mode stays in `.env`, so a stolen dashboard password can never
switch on real money.

### Read-only data API (`/api/data`)

For analysis tools and assistants that cannot reach the database itself (it only speaks HTTPS,
through the same server as the dashboard). Off until you set **Data API → Read-only API token**
(24+ characters) on the Config page; calls send it as `Authorization: Bearer <token>`. It is
separate from the dashboard login, so you can hand it out and change it on its own.

- Read-only: GET only, and every query runs in a read-only transaction.
- Only the bot's own tables, never other tables in the shared database; there is no raw-SQL
  endpoint. Secret settings (broker, OpenRouter, Telegram, this token) are masked.

```bash
curl -H "Authorization: Bearer $TOKEN" https://<your-host>/api/data/diagnose?days=7      # where cycles stop
curl -H "Authorization: Bearer $TOKEN" https://<your-host>/api/data/tables               # tables, columns, row counts
curl -H "Authorization: Bearer $TOKEN" "https://<your-host>/api/data/candles?instrument=EUR_USD&granularity=M15&days=30"  # broker history
curl -H "Authorization: Bearer $TOKEN" "https://<your-host>/api/data/tables/decision_requests?f.experiment=eurusd-breakout&since=2026-10-06T00:00:00Z&limit=200&columns=candle_time,strategy_result"
```

Rows: `limit` (max 1000) and `offset` (`has_more` says whether to page on), `order=column` or
`-column`, `columns=a,b`, `since`/`until` on the table's time column, and `f.<column>=<value>`
filters (`null` matches empty). `python -m app.diagnose` prints the same diagnosis locally.

`/api/data/candles` returns complete broker candles (M15, H1, H4, D or W; up to 500 days). Saved as
`<INSTRUMENT>_<GRANULARITY>.json` files, they feed `app.backtest.CandleFileClient`, which stands in for
the broker: a new strategy can then be backtested on real history on a machine that cannot reach
Capital.com, before it is deployed.

### Backtester (`/backtest` page, or `python -m app.backtest`)

Waiting weeks for live trades is a slow way to learn whether an experiment's rules work. The
backtester replays months of Capital.com candles through them instead: for every 15-minute
candle it builds the same technical state the engine builds, runs the experiment's strategy, and
sends each setup through the real risk engine (spread limit, stop distance, R:R, sizing,
cooldown, loss breakers) with the experiment's capital as a simulated account. Approved trades
fill at the candle's closing bid/ask and run to their stop or target.

**On the dashboard:** open **Backtest**, tick the experiments, pick a period (30 days to a year)
and press *Run backtest*. To test a change, add a setting under *Change settings*: one value tries
it, several (`1,2`) compare them side by side. Results show trades per week, win rate, total R,
profit factor, the cumulative-R curve, a plain-language reading, what blocked the rest of the
candles, near misses and every trade. The last ten runs stay listed until the dashboard restarts.

**From the server's shell:**

```bash
python -m app.backtest                                        # every enabled experiment, last 90 days
python -m app.backtest --experiment gbpusd-breakout --days 180 --trades
python -m app.backtest --experiment eurusd-breakout --vary breakout_min_touches=1,2 --vary min_risk_reward=1.2,1.5
python -m app.backtest --set max_spread_pips=2 --json
```

`--set key=value` changes a setting for the run; `--vary key=v1,v2` compares values (every
combination, up to 12). Nothing is saved: try a change here before you make it on `/config`.
The same runs over HTTPS: `GET /api/data/backtest?experiment=<slug>&days=90&vary.<key>=v1,v2&wait=50`
starts one (one at a time) and returns its progress; repeat the URL until `status` is `done`.

It reports trades per week, win rate, total and average R, profit factor, drawdown, what blocked
the rest of the candles and the near misses.

**Out-of-sample check.** Every backtest is split in two: the *tuning* period (the first two-thirds)
and the *check* period (the last third), plus results by quarter. Pick settings by their tuning
results only, then look at the check column: a setting that was best on the tuning period and is
still profitable on the check period (which it was not chosen on) has a real chance of holding up
live; one that falls apart there was luck. With `--vary`, the report says this for you ("holds
up", "mixed" or "does not hold up"). A result that is positive overall but negative in most
quarters is flagged as not steady. Read it as an upper bound on how often the
experiment trades: it treats every setup as if the model agreed, has no news blackout, and counts
a candle that touches both stop and target as a loss. It reads candles only; it never places
orders or writes to the database.

### Analysis page (`/analysis`)

Linked from the navigation and from the *Experiment analysis* button on the overview. It shows
what `python -m app.experiments.analyzer --once` prints, in plain terms:
- results so far: trades, win rate, average R, P/L and drawdown
- the funnel from 15-minute cycles to setups, model reviews, signals, risk approvals and fills
- why trades didn't happen: top wait reasons and risk-engine blocks
- execution and health figures
- results by condition: direction, market regime, model confidence, news risk and more
- **how trades ended**: reached take-profit, hit stop-loss, closed by you, or a bot safety exit; and for
  every trade its *best* and *worst* point (furthest in your favour / against you, in R), how much
  profit it gave back before closing, and whether it reached +1R, +2R and +3R first. This is the
  evidence for choosing stop-loss management rules (break-even, step-locks, trailing). It is measured
  from stored 5- and 15-minute candles (mid prices), so it covers past trades too and needs nothing
  recorded while a trade is open. The overview's trade tables and each trade's history timeline
  show the same figures.
- **setups held back only by risk:reward**: every candle where all the rules passed except
  `MIN_RISK_REWARD`, replayed on real candles at lower minimums (1.0, 1.2, 1.5, 1.8). For each it
  shows how many extra trades there would have been and how they would have ended (one trade at a
  time, mid prices, no model review), so you can see the effect of a lower `MIN_RISK_REWARD` before
  you change it. Results by condition also split closed trades by their planned risk:reward.
- a day-by-day timeline of what has run, over the last 7, 30 or 90 days

*Run analysis now* computes and stores a fresh report on demand, so the separate analyzer process
is optional.

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

1. Sync broker candles (M15, H1, H4, D, W, month). The broker's candles are authoritative
   (mid prices from Capital.com's bid/ask candles; a candle counts once its period has ended).
2. **Technicals** (deterministic): EMA 20/50/200, RSI 14, ATR 14, swing structure
   (HH/HL/LH/LL), trend per timeframe, volatility regime and support/resistance:
   clustered H1/H4 swings, previous day/week/month high and low, and round-number
   (psychological) levels such as 1.1800. Stored in `technical_snapshots`,
   `support_resistance` and `market_regimes`.
3. **News**: ForexFactory calendar (event, currency, impact, forecast, actual, surprise).
   Python, not the model, turns a surprise into a currency and pair impact (for example, CPI
   below forecast means USD bearish, so EUR/USD up; unemployment is inverted). Jev reads each
   Fed/ECB press release: the policy tone (hawkish / dovish / neutral, with its probability as
   the confidence) becomes a currency bias, and the main theme becomes the reason code.
   The result gives a news risk level and a blackout flag (±30 min around high-impact USD/EUR
   events).
   *Limitation:* the free ForexFactory weekly feed normally carries forecast and previous
   values but not the actual release, so surprise and impact only appear when an actual is
   present. The blackout (the safety-critical part) works regardless. A paid calendar can be
   added by implementing `CalendarProvider` in `app/news/providers/`.
   - **Overall market regime** (deterministic): strong uptrend, strong downtrend, breakout,
     compression, range, event risk or unclear. The trend-pullback setup suits the strong
     trends; the analyzer reports results per regime.
4. **Strategy check** (deterministic), with the experiment's own strategy and parameters.
   **Trend pullback** (`app/strategy/trend_pullback.py`):
   - H4 trend sets the direction.
   - H1 trend must not oppose it.
   - M15 price has pulled back into a support/resistance zone or the M15 EMA50, by at least
     1× ATR.
   - Momentum confirms: candle direction, close vs EMA20, and RSI turning.
   - Stop sits beyond the pullback plus 0.25 ATR; the target is the next opposing level
     (or 2R), capped at 4R.
   - The setup needs R:R of at least 1:2 (Experiment #1).

   **Range breakout** (`app/strategy/range_breakout.py`) trades what the pullback skips: a range
   that resolves into a breakout.
   - Range: the 20 completed H1 bars before the latest one, 1.5–6× ATR(H1) tall, with at least
     two separate visits to each edge.
   - Break: the latest M15 candle closes beyond the edge by 0.1× ATR(M15), with a body of at least
     0.5× ATR(M15), closing in the outer third of its range.
   - Fresh: none of the 4 M15 candles before it closed beyond the edge (no chasing).
   - The H4 trend does not point against the break.
   - Stop 30% of the range height back inside it; target the measured move (one range height
     beyond the edge), capped at 4R. Stop distance and the experiment's minimum R:R apply.
     These plans usually land around 1.2–2R, so run breakout experiments with a minimum R:R
     of about 1.5.

   **Trend following (4h)** (`app/strategy/trend_following.py`) is the slow, hold-for-days approach
   with the most published support for FX majors (time-series momentum).
   - Trend: 4h EMA50 above EMA200 (longs) or below (shorts).
   - Signal: the latest completed 4h candle closes beyond the high/low of the 20 4h candles before
     it, in the trend's direction, and it is the first such close; it is taken within 60 minutes of
     that 4h candle closing.
   - Stop 1.5× ATR(4h) from the entry; target 2.5R. 4h stops are wide (often 30–60 pips), so give
     these experiments a higher *Max stop (pips)*, e.g. 80.

   **London breakout** (`app/strategy/session_breakout.py`) trades the first break of the quiet
   overnight range when London opens. Hours are London time, so it follows British summer time.
   - Range: the high and low of today's 1h candles from 00:00 to 08:00, 1.5–6× ATR(1h) tall.
   - Entries 08:00–12:00 on weekdays: the latest 15m candle closes beyond the range by 0.1× ATR(15m),
     and it is the first close beyond it since 08:00.
   - Stop in the middle of the range (at least the minimum stop); target 2R.
   All of these are experiment settings on the Config page.
5. **Market snapshot** is built and stored in `decision_requests` (unique per candle, so a
   restart never processes a candle twice).
6. **Decision model**: Jev through OpenRouter's Decisions API (`POST /api/alpha/decisions`).
   The snapshot is sent as the *state* with typed questions: one choice of `BUY | SELL | WAIT`,
   whose probability is the confidence, and five yes/no checks (trend support, pullback to a
   level, momentum, room to target, news risk) that are recorded as reason codes. The rationale
   shown on the dashboard is the probability of each option. Jev can confirm or decline the plan
   but **cannot change entry, stop, target, size or any limit**. Invalid answers, errors and
   timeouts become WAIT. (Setting `OPENROUTER_MODEL` to a chat model uses chat completions with
   a JSON schema instead.)
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

   It sizes the position itself: 0.25% of the experiment's equity by default, converted to the
   account currency.
8. **Executor** (the only order submitter): writes the order row first, and re-checks the
   kill switch and that no position is open on the instrument. It then sends a market order
   with stop-loss and take-profit levels and confirms it by its deal reference. A fill beyond
   the slippage bound is closed at once. A timeout is never treated as filled or as not
   filled: the order is resolved from broker state and never resubmitted (see *Broker:
   Capital.com*).
9. **Reconciliation** (every 15 s): account balances, open positions, trade closes with R
   (from the activity history), the broker activity/transaction audit trail, stuck orders,
   unexpected positions (critical alert), each experiment's equity, peak and start-of-day equity,
   and its circuit breakers.

## Experiments

An experiment is one strategy on one pair, with its own parameters, model and risk limits.
Several run at the same time in one engine on **one broker account**, for example the EUR/USD
trend pullback next to a GBP/USD one, or two EUR/USD variants with different minimum
risk:reward. They are created and edited on the Config page.

- **Shared:** the Capital.com session and account, one price stream for all pairs, candles
  and technicals per pair, the news calendar and central-bank reads, reconciliation, and the
  executor (still the only component that places orders).
- **Per experiment:** decisions (`decision_requests.experiment` is its id), trades (attributed
  through their orders), Telegram alerts (named after it), system events, analysis reports,
  and its **own risk pool**:
  - *equity* = its capital + realized P/L of its closed trades + open P/L of its open trades.
    The capital is set on the Config page; if left empty, the engine sets it once from the
    account's cash balance (net of anything the experiment already made).
  - position size, total open risk and max open trades use its equity and its trades only;
  - its daily-loss and drawdown breakers measure its equity, and trip, reset and halt only it;
  - its own kill switch and *close its trades*.
  - Margin and account health are checked on the account, because they are physical limits of
    the one account.

Because each experiment has its own pool, losses add up across experiments: with three
experiments at a 5% drawdown limit, the account can lose more than 5% before all of them stop.
The account-wide kill switch stops all of them at once.

Two experiments on the same pair need the account's **hedging mode** on to hold positions at
the same time (see *Broker: Capital.com*); with it off, the first one to open a position blocks
the others until it closes. `python -m app.doctor` warns about this.

Two strategies exist: **trend pullback** and **range breakout** (see *How a decision is made*).
An experiment's strategy is chosen when it is created and is fixed once it has made a decision.
Each strategy has its own questions for the model (`app/decision/prompts.py`, versioned
separately). A new strategy is a module in `app/strategy/` returning a `StrategyResult`, a
`DecisionPrompt`, an entry in `app/strategy/registry.py` and a branch in
`app.engine.evaluate_strategy`.

Example line-up, all on one account with hedging mode on:

| Experiment | Pair | Strategy | Suggested changes from the defaults |
|---|---|---|---|
| `v1-trend-pullback-eur-usd` | EUR/USD | trend pullback | (V1) |
| `gbpusd-pullback` | GBP/USD | trend pullback | copy V1's parameters; max spread 2.0 pips |
| `eurusd-breakout` | EUR/USD | range breakout | min R:R 1.5 |
| `gbpusd-breakout` | GBP/USD | range breakout | min R:R 1.5; max spread 2.0 pips |

For GBP experiments the news settings need a Bank of England feed (`GBP|https://www.bankofengland.co.uk/rss/news`,
the default for new installations); `python -m app.doctor` names any traded currency without one.

**Going live** uses the same model: point the live credentials at the one live account and
enable the experiments that should trade it. Each keeps its own capital and limits, so set
their capitals to the share of the account each one may use.

## Safety controls

- Demo by default: demo mode always uses Capital.com's demo API host. Live mode requires
  `TRADING_MODE=live` and the exact `LIVE_TRADING_CONFIRM` phrase in the server's `.env`, and
  separate live credentials.
- Risk settings have hard ceilings: at most 1% per trade, 2% total risk, 5% daily loss
  and 20% drawdown, per experiment. Values typed on the Config page are checked against them.
- Kill switches (account-wide and per experiment), and daily-loss (auto-resets next trading day
  at 17:00 New York, or manually) and max-drawdown (manual reset) circuit breakers per experiment.
- Duplicate protection:
  - one decision per candle
  - one order per approved risk check
  - never resubmitting an entry whose outcome was unknown
  - refusing an entry while a position is open on the instrument (see *Broker: Capital.com* for
    how hedging mode changes this between experiments)
  - no new orders while any order is unresolved
- Telegram alerts, kept to what matters (everything else is logged and shown under system
  events on the dashboard):
  - setups the rules find, with the plan and the model's verdict
  - fills, risk/broker rejections, blocked orders, slippage closes and trade closes
  - reconciliation findings: unexpected positions, adopted/missing orders, and reconciliation
    failing for about a minute (single failed passes are not sent)
  - stale prices during market hours (individual stream disconnects/reconnects are not sent)
  - other errors, breaker trips, and engine start/stop

## Experiment analysis

`python -m app.experiments.analyzer --once` prints (and stores in `analysis_reports`) a report
for every experiment (`--experiment <id>` for one):
- opportunities, setup candidates, model calls, decisions, and rejection/WAIT reasons
- wins/losses, average winner and loser in R, expectancy, profit factor, max drawdown (in
  R and as a % of the experiment's equity)
- performance by setup condition, overall market regime, volatility and trend regime, news risk, model-confidence
  bucket, direction and planned risk:reward
- setups that failed exactly one rule, and a what-if replay of those blocked only by risk:reward
- slippage, spread and LLM latency

## Tests

```bash
pip install -e ".[dev]"
pytest -q                                      # unit tests (no database needed)
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/tradlysis_test pytest -q   # + integration
```

The integration tests rebuild the test database from the Prisma migration SQL. They run
the executor, reconciliation, full decision cycles and the API against a fake Capital.com
broker and a fake LLM. CI (`.github/workflows/ci.yml`) runs everything against PostgreSQL 16 and
also fails if `schema.prisma` has changes without a migration.

## Project layout

```
app/
  config/          settings (safety validation) and the database-backed configuration store
  broker/          Capital.com REST + WebSocket client, broker-neutral types
  market_data/     price stream, market state, candles, market hours
  technicals/      indicators, structure, levels, regimes, technical state
  news/            calendar/RSS providers, LLM interpretation, news state
  snapshot/        market snapshot builder
  strategy/        deterministic strategies (trend pullback, range breakout), trade plans, registry
  decision/        OpenRouter client (Decisions API + chat), prompts/questions per strategy (versioned), schema
  risk/            deterministic risk engine and currency conversion
  execution/       order executor (only component that submits orders)
  reconciliation/  broker ↔ database reconciliation and circuit breakers
  experiments/     analyzer (separate process)
  api/             FastAPI control plane + dashboard (static/index.html)
  alerts/          Telegram + system events
  db/              SQLAlchemy models mirroring Prisma, sessions, control flags, table bootstrap
  engine.py        orchestrator (python -m app.engine)
  doctor.py        setup checker (python -m app.doctor)
prisma/            schema.prisma + migrations (source of truth for the database)
docker/            Dockerfiles
tests/
```

## Out of scope for V1 (per the specification)

These are deliberately not built:
- high-frequency trading and real-money trading
- large indicator collections
- LLM-created strategies, LLM-controlled sizing or bypassing risk limits
- arbitrage
- automatic strategy optimisation before enough live-demo data exists

TradingView (optional in the spec) and Redis ("later if needed") are also not used.
