# Tradlysis – notes for AI assistants

- Spec: V1 of an AI-assisted FX bot (EUR/USD, trend pullback, OANDA practice account). See README.md.
- Python 3.11 engine; Prisma is used ONLY for schema + migrations (`prisma/`). Python uses SQLAlchemy models in
  `app/db/models.py` that must mirror `prisma/schema.prisma` (enforced by `tests/test_schema_sync.py`).
  Schema change = edit schema.prisma -> `npx prisma migrate dev --name x` -> update models.py -> tests.
  Never apply migrations to the user's database; they run `npm run migrate:deploy` themselves.
- Upserts must use `on_conflict_do_*(index_elements=[...])`: Prisma creates unique *indexes*, not named constraints.
- Safety invariants (do not weaken): the LLM never sets size/prices/limits; the risk engine (`app/risk/engine.py`)
  has final authority and records every check; only `app/execution/executor.py` submits orders; demo mode by
  default; never commit credentials.
- Tests: `pytest -q` (unit) and `TEST_DATABASE_URL=postgresql://... pytest -q` (integration against a disposable DB,
  rebuilt from the migration SQL). Lint: `ruff check app tests`.
- Bump `PROMPT_VERSION` in `app/decision/prompts.py` whenever the decision prompt changes.
