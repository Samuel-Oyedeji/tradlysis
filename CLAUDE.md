# Tradlysis – notes for AI assistants

- Spec: started as V1 of an AI-assisted FX/CFD bot (EUR/USD, trend pullback, Capital.com demo account); now runs
  several experiments (pairs x strategies) on one account. See README.md.
- Configuration: `.env` holds only bootstrap values (`app/config/store.py` `ENV_ONLY_KEYS`: DATABASE_URL, dashboard and
  config passwords, TRADING_MODE/LIVE_TRADING_CONFIRM). Everything else is stored in `app_config` (global) and
  `experiments.settings` (per experiment), edited on `/config`, and merged + validated by `Settings`. A new setting needs
  a `FieldInfo` in `store.FIELDS` (global or experiment scope) to be editable. Never move the trading mode into the DB.
- Experiments: several run in one engine on ONE broker account (`ExperimentRunner` per experiment in `app/engine.py`).
  Each has its own risk pool (equity = capital + P/L of its own trades), breakers, kill switch and close-all, stored
  as `control_state` keys `<key>:<slug>` (`app.db.control.scoped`). Orders belong to an experiment through
  risk_check -> decision_request; trades carry `experiment`.
- Broker access lives only in `app/broker/capital.py` (REST + WebSocket); services use the neutral types in
  `app/broker/types.py`. Tests run the real client against `tests/fake_broker.py` (httpx MockTransport).
- Python 3.11 engine; Prisma is used ONLY for schema + migrations (`prisma/`). Python uses SQLAlchemy models in
  `app/db/models.py` that must mirror `prisma/schema.prisma` (enforced by `tests/test_schema_sync.py`).
  Schema change = edit schema.prisma -> `npx prisma migrate dev --name x` -> update models.py -> tests.
  Never apply migrations to the user's database; they run `npm run migrate:deploy` themselves.
  The database is shared with other apps (Supabase): never suggest `prisma migrate dev/reset` or `db push` there.
- `app/db/bootstrap.py` creates *missing* tables from the models at start-up, so models.py must emit exactly the
  migration DDL (unique keys as unique indexes, Prisma names, `CURRENT_TIMESTAMP`); `tests/test_bootstrap.py` checks it.
- The decision model is TypeSafe Jev on OpenRouter's Decisions API (`OpenRouterClient.decisions`): typed questions
  (choice/noul) about a `state`, not chat. Questions live in `app/decision/prompts.py` and `app/news/interpreter.py`.
- Upserts must use `on_conflict_do_*(index_elements=[...])`: Prisma creates unique *indexes*, not named constraints.
- Safety invariants (do not weaken): the LLM never sets size/prices/limits; the risk engine (`app/risk/engine.py`)
  has final authority and records every check; only `app/execution/executor.py` submits orders; demo mode by
  default; never commit credentials.
- Tests: `pytest -q` (unit) and `TEST_DATABASE_URL=postgresql://... pytest -q` (integration against a disposable DB,
  rebuilt from the migration SQL). Lint: `ruff check app tests`.
- Strategies: `app/strategy/<name>.py` returns a `StrategyResult` (`app/strategy/base.py`); each has a
  `DecisionPrompt` and an entry in `app/strategy/registry.py`, dispatched by `app.engine.evaluate_strategy`.
  Strategy-only settings get `strategies=(...)` on their `FieldInfo`. Prompts never name a pair (the snapshot does).
- Bump a strategy's prompt version in `app/decision/prompts.py` (`PROMPT_VERSION` for trend pullback,
  `BREAKOUT_PROMPT_VERSION`) whenever its prompt or questions change
  (`NEWS_PROMPT_VERSION` in `app/news/interpreter.py` for the news questions).
