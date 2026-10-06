"""Settings stored in the database and edited on the dashboard's config page.

Layers, later ones win:

    code defaults  <  environment (.env)  <  app_config (global)  <  experiments.settings

Only bootstrap values have to stay in the environment (:data:`ENV_ONLY_KEYS`): the database
URL, the dashboard and config-page passwords and the trading mode (real money is only ever
switched on on the server). Everything else can be edited in-app. Every merged result is
validated by :class:`~app.config.settings.Settings`, so the hard risk ceilings apply to values
typed in the browser exactly as they do to the environment.

Each experiment has its own instrument, strategy, capital and parameters (risk limits, strategy
settings, model): :func:`experiment_settings` turns the global settings plus an experiment's
overrides into the ``Settings`` the engine's components already use.

Saving anything bumps the ``config_version`` control; the engine notices and restarts itself
with the new configuration (see ``app/engine.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.db.control import get_control, scoped, set_control
from app.db.enums import ControlKey
from app.db.models import AppConfig, ConfigChange, Experiment
from app.db.session import Database

GLOBAL_SCOPE = "global"
EXPERIMENT_SCOPE = "experiment"
SECRET_MASK = "(secret)"
STRATEGIES = {"trend_pullback": "Trend pullback"}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
INSTRUMENT_RE = re.compile(r"^[A-Z]{3}_[A-Z]{3}$")
IMPORT_LOCK_KEY = 7_214_990_332

# Read from the environment only; never stored in or read from the database.
ENV_ONLY_KEYS = (
    "database_url",
    "db_auto_create_tables",
    "dashboard_username",
    "dashboard_password",
    "config_password",
    "trading_mode",
    "live_trading_confirm",
    "api_host",
    "api_port",
    "log_level",
)


@dataclass(frozen=True)
class FieldInfo:
    key: str
    scope: str
    group: str
    label: str
    help: str = ""
    secret: bool = False
    kind: str = "text"  # text | number | bool | choice
    choices: tuple[str, ...] = ()


def _g(key: str, group: str, label: str, help: str = "", **kw: Any) -> FieldInfo:
    return FieldInfo(key, GLOBAL_SCOPE, group, label, help, **kw)


def _e(key: str, group: str, label: str, help: str = "", **kw: Any) -> FieldInfo:
    return FieldInfo(key, EXPERIMENT_SCOPE, group, label, help, **kw)


FIELDS: tuple[FieldInfo, ...] = (
    # --- global ----------------------------------------------------------------------------
    _g("trading_enabled", "Safety", "Trading enabled",
       "Master switch for new orders (all experiments). The kill switch on the overview works instantly.", kind="bool"),
    _g("stale_price_seconds", "Safety", "Stale price (s)", "A price older than this blocks new trades.", kind="number"),
    _g("max_account_state_age_seconds", "Safety", "Max account reading age (s)",
       "New trades pause when no valid account reading arrived for this long.", kind="number"),
    _g("capital_demo_api_key", "Capital.com demo", "API key", secret=True),
    _g("capital_demo_identifier", "Capital.com demo", "Login e-mail"),
    _g("capital_demo_api_password", "Capital.com demo", "API key password",
       "The custom password chosen when the API key was generated.", secret=True),
    _g("capital_demo_account_id", "Capital.com demo", "Account ID",
       "Empty = the account that is active after login. All experiments trade on this account."),
    _g("capital_live_api_key", "Capital.com live", "API key", "Only used when TRADING_MODE=live (set on the server).",
       secret=True),
    _g("capital_live_identifier", "Capital.com live", "Login e-mail"),
    _g("capital_live_api_password", "Capital.com live", "API key password", secret=True),
    _g("capital_live_account_id", "Capital.com live", "Account ID"),
    _g("capital_stream_url", "Capital.com advanced", "Streaming URL override", "Empty = the host returned at login."),
    _g("capital_request_timeout_seconds", "Capital.com advanced", "Request timeout (s)", kind="number"),
    _g("openrouter_api_key", "Decision model", "OpenRouter API key", secret=True),
    _g("openrouter_news_model", "Decision model", "News model",
       "Model that reads central-bank news. Empty = typesafe/jev-1.13 (the default decision model)."),
    _g("llm_timeout_seconds", "Decision model", "Timeout (s)", kind="number"),
    _g("llm_max_tokens", "Decision model", "Max tokens (chat models)", kind="number"),
    _g("llm_temperature", "Decision model", "Temperature (chat models)", kind="number"),
    _g("openrouter_base_url", "Decision model", "OpenRouter base URL"),
    _g("openrouter_app_url", "Decision model", "App URL sent to OpenRouter"),
    _g("openrouter_app_name", "Decision model", "App name sent to OpenRouter"),
    _g("news_calendar_url", "News", "Economic calendar URL", "ForexFactory weekly JSON feed."),
    _g("news_rss_feeds", "News", "Central-bank RSS feeds", 'Comma-separated "CURRENCY|url" pairs.'),
    _g("news_interpretation_enabled", "News", "Interpret news with the model", kind="bool"),
    _g("news_bias_lookback_hours", "News", "Bias lookback (h)", kind="number"),
    _g("news_calendar_poll_minutes", "News", "Calendar poll (min)", kind="number"),
    _g("news_rss_poll_minutes", "News", "RSS poll (min)", kind="number"),
    _g("telegram_bot_token", "Telegram alerts", "Bot token", "From @BotFather. Empty = alerts off.", secret=True),
    _g("telegram_chat_id", "Telegram alerts", "Chat ID"),
    _g("alert_dedup_seconds", "Telegram alerts", "Repeat-alert window (s)", kind="number"),
    _g("decision_candle_delay_seconds", "Scheduling", "Wait after candle close (s)", kind="number"),
    _g("reconcile_interval_seconds", "Scheduling", "Reconciliation interval (s)", kind="number"),
    _g("account_snapshot_interval_seconds", "Scheduling", "Account snapshot interval (s)", kind="number"),
    _g("price_persist_interval_seconds", "Scheduling", "Price sample interval (s)", kind="number"),
    _g("candle_history_count", "Scheduling", "Candles loaded at start-up", kind="number"),
    _g("analyzer_interval_minutes", "Scheduling", "Analyzer interval (min)", kind="number"),
    # --- per experiment -----------------------------------------------------------------------
    _e("risk_per_trade_pct", "Risk", "Risk per trade (%)", "Of the experiment's equity. Hard ceiling 1%.",
       kind="number"),
    _e("max_total_risk_pct", "Risk", "Max open risk (%)", "All of this experiment's open trades. Ceiling 2%.",
       kind="number"),
    _e("max_daily_loss_pct", "Risk", "Daily loss breaker (%)",
       "Of the experiment's equity at the start of the trading day (17:00 New York). Ceiling 5%.", kind="number"),
    _e("max_drawdown_pct", "Risk", "Drawdown breaker (%)", "From the experiment's peak equity. Ceiling 20%.",
       kind="number"),
    _e("max_open_trades", "Risk", "Max open trades", kind="number"),
    _e("max_margin_usage_pct", "Risk", "Max margin use (%)", "Of the account's available margin, per trade.",
       kind="number"),
    _e("min_risk_reward", "Trade filters", "Min risk:reward", "At least 1.0.", kind="number"),
    _e("min_decision_confidence", "Trade filters", "Min model confidence", "0 to 1.", kind="number"),
    _e("max_spread_pips", "Trade filters", "Max spread (pips)", kind="number"),
    _e("min_stop_pips", "Trade filters", "Min stop (pips)", kind="number"),
    _e("max_stop_pips", "Trade filters", "Max stop (pips)", kind="number"),
    _e("max_slippage_pips", "Trade filters", "Max slippage (pips)", "A worse fill is closed at once.", kind="number"),
    _e("signal_cooldown_minutes", "Trade filters", "Same-direction cooldown (min)", kind="number"),
    _e("news_blackout_before_minutes", "News rules", "Blackout before event (min)", kind="number"),
    _e("news_blackout_after_minutes", "News rules", "Blackout after event (min)", kind="number"),
    _e("news_blocking_impacts", "News rules", "Blocking impacts", "Comma-separated, e.g. HIGH or HIGH,MEDIUM."),
    _e("news_require_fresh_calendar", "News rules", "Require a fresh calendar", kind="bool"),
    _e("strategy_pullback_lookback_bars", "Strategy", "Pullback lookback (bars)", kind="number"),
    _e("strategy_level_tolerance_atr", "Strategy", "Level tolerance (ATR)", kind="number"),
    _e("strategy_stop_buffer_atr", "Strategy", "Stop buffer (ATR)", kind="number"),
    _e("strategy_fallback_target_r", "Strategy", "Fallback target (R)", kind="number"),
    _e("strategy_max_target_r", "Strategy", "Max target (R)", kind="number"),
    _e("openrouter_model", "Decision model", "Model", "Jev (typesafe/jev-1.13) or a chat model id."),
    _e("llm_call_policy", "Decision model", "When to call the model",
       "candidates_only: only when the rules find a setup. always: every 15 minutes.",
       kind="choice", choices=("candidates_only", "always")),
    _e("capital_epic", "Broker", "Capital.com market", "Empty = the instrument without '_' (EURUSD)."),
)
FIELD_BY_KEY = {f.key: f for f in FIELDS}
GLOBAL_KEYS = frozenset(f.key for f in FIELDS if f.scope == GLOBAL_SCOPE)
EXPERIMENT_KEYS = frozenset(f.key for f in FIELDS if f.scope == EXPERIMENT_SCOPE)
SECRET_KEYS = frozenset(f.key for f in FIELDS if f.secret)


class ConfigError(ValueError):
    """A configuration change was refused; the message says why."""


@dataclass
class ExperimentConfig:
    id: int
    slug: str
    name: str
    description: str | None
    instrument: str
    strategy: str
    enabled: bool
    capital: Decimal | None
    overrides: dict[str, str]
    settings: Settings

    def summary(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "instrument": self.instrument,
            "strategy": self.strategy,
            "enabled": self.enabled,
            "capital": None if self.capital is None else str(self.capital),
        }


@dataclass
class Configuration:
    settings: Settings  # global
    experiments: list[ExperimentConfig] = field(default_factory=list)
    stored: dict[str, str] = field(default_factory=dict)  # global app_config rows
    version: int = 0

    @property
    def enabled(self) -> list[ExperimentConfig]:
        return [e for e in self.experiments if e.enabled]

    def get(self, slug: str) -> ExperimentConfig | None:
        return next((e for e in self.experiments if e.slug == slug), None)

    def default_slug(self) -> str | None:
        """The experiment pages show when none is chosen: the first enabled one, else the first."""
        pick = self.enabled[0] if self.enabled else (self.experiments[0] if self.experiments else None)
        return pick.slug if pick else None


# ---------------------------------------------------------------------- merging


def _validate(values: dict[str, Any], scope: str) -> Settings:
    try:
        return Settings.model_validate(values)
    except ValidationError as exc:
        raise ConfigError(f"{scope}: {_error_text(exc)}") from None


def _error_text(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        loc = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{loc.upper()}: {msg}" if loc else msg)
    return "; ".join(parts)


def merge_global(base: Settings, stored: dict[str, str]) -> Settings:
    values = base.model_dump()
    values.update({k: v for k, v in stored.items() if k in GLOBAL_KEYS})
    return _validate(values, "global settings")


def experiment_settings(global_settings: Settings, slug: str, instrument: str, overrides: dict[str, Any]) -> Settings:
    """The ``Settings`` one experiment runs with: global values plus its own parameters."""
    values = global_settings.model_dump()
    values.update({k: v for k, v in overrides.items() if k in EXPERIMENT_KEYS})
    values["experiment_name"] = slug
    values["instrument"] = instrument
    return _validate(values, slug)


def display_value(settings: Settings, key: str) -> str:
    value = getattr(settings, key)
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    return str(value)


# ---------------------------------------------------------------------- loading


async def load_configuration(db: Database, base: Settings) -> Configuration:
    """Global settings and every experiment, merged and validated.

    A stored experiment that no longer validates (e.g. after a code change tightened a limit) is
    kept with its last valid parameters dropped to the global values, and marked disabled, so one
    bad row never stops the other experiments.
    """
    async with db.session() as s:
        stored = {r.key: r.value for r in (await s.scalars(select(AppConfig))).all()}
        rows = (await s.scalars(select(Experiment).order_by(Experiment.id))).all()
        version = await get_version(s)
    global_settings = merge_global(base, stored)
    experiments = []
    for r in rows:
        overrides = {k: str(v) for k, v in (r.settings or {}).items() if k in EXPERIMENT_KEYS}
        enabled = r.enabled
        try:
            settings = experiment_settings(global_settings, r.slug, r.instrument, overrides)
        except ConfigError:
            settings = experiment_settings(global_settings, r.slug, r.instrument, {})
            enabled = False
        experiments.append(
            ExperimentConfig(
                id=r.id, slug=r.slug, name=r.name, description=r.description, instrument=r.instrument,
                strategy=r.strategy, enabled=enabled, capital=r.capital, overrides=overrides, settings=settings,
            )
        )
    return Configuration(settings=global_settings, experiments=experiments, stored=stored, version=version)


async def get_version(session: AsyncSession) -> int:
    value = await get_control(session, ControlKey.CONFIG_VERSION)
    return int((value or {}).get("version") or 0)


async def _bump_version(session: AsyncSession, user: str) -> None:
    from app.market_data.timeutil import utcnow

    version = await get_version(session)
    await set_control(
        session, ControlKey.CONFIG_VERSION, {"version": version + 1, "at": utcnow().isoformat(), "by": user}, user
    )


# ---------------------------------------------------------------------- first start


async def import_env_once(db: Database, base: Settings) -> list[str]:
    """Copy settings given in the environment into the database, once.

    Runs on the first start of this version: global values set in ``.env`` become ``app_config``
    rows, and the experiment the environment described (EXPERIMENT_NAME, INSTRUMENT and the risk
    and strategy parameters) becomes the first experiment, enabled. Afterwards the environment
    can be reduced to :data:`ENV_ONLY_KEYS`. Returns the imported keys.
    """
    async with db.session() as s:
        # Serialise the engine, API and analyzer starting together.
        await s.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IMPORT_LOCK_KEY})
        if await get_control(s, ControlKey.CONFIG_IMPORTED) is not None:
            return []
        given = base.model_fields_set
        imported = []
        for key in sorted(GLOBAL_KEYS & given):
            stmt = insert(AppConfig).values(key=key, value=display_value(base, key), updated_by="env-import")
            await s.execute(stmt.on_conflict_do_nothing(index_elements=["key"]))
            imported.append(key)
        has_experiment = await s.scalar(select(Experiment.id).limit(1))
        if has_experiment is None:
            overrides = {k: display_value(base, k) for k in sorted(EXPERIMENT_KEYS & given)}
            stmt = insert(Experiment).values(
                slug=base.experiment_name,
                name="EUR/USD trend pullback" if base.experiment_name == "v1-trend-pullback-eur-usd" else base.experiment_name,
                instrument=base.instrument,
                strategy="trend_pullback",
                enabled=True,
                settings=overrides,
            )
            await s.execute(stmt.on_conflict_do_nothing(index_elements=["slug"]))
            imported += [f"{base.experiment_name}.{k}" for k in overrides]
            # Breakers and equity marks used to be account-wide; a tripped breaker stays tripped
            # for the experiment that tripped it.
            for key in (ControlKey.DAILY_LOSS_BREAKER, ControlKey.DRAWDOWN_BREAKER):
                value = await get_control(s, key)
                if value and value.get("tripped"):
                    await set_control(s, scoped(key, base.experiment_name), value, "env-import")
        await set_control(s, ControlKey.CONFIG_IMPORTED, {"keys": imported}, "env-import")
        for key in imported:
            scope, _, name = key.rpartition(".")
            s.add(ConfigChange(scope=scope or GLOBAL_SCOPE, key=name, old_value=None,
                               new_value=SECRET_MASK if name in SECRET_KEYS else "(from environment)",
                               changed_by="env-import"))
    return imported


# ---------------------------------------------------------------------- saving


def _audit_value(key: str, value: str | None) -> str | None:
    if value is None:
        return None
    return SECRET_MASK if key in SECRET_KEYS else value


async def save_global(db: Database, base: Settings, changes: dict[str, str | None], user: str) -> list[str]:
    """Apply global changes (``None`` = remove the stored value, falling back to ENV/default).

    Everything is validated together before anything is written. Returns the changed keys.
    """
    unknown = [k for k in changes if k not in GLOBAL_KEYS]
    if unknown:
        raise ConfigError(f"not editable here: {', '.join(sorted(unknown))}")
    async with db.session() as s:
        stored = {r.key: r.value for r in (await s.scalars(select(AppConfig).with_for_update())).all()}
        new = dict(stored)
        for k, v in changes.items():
            if v is None:
                new.pop(k, None)
            else:
                new[k] = str(v).strip()
        global_settings = merge_global(base, new)
        for e in (await s.scalars(select(Experiment))).all():
            experiment_settings(global_settings, e.slug, e.instrument, e.settings or {})
        changed = sorted(k for k in changes if stored.get(k) != new.get(k))
        for k in changed:
            if k in new:
                stmt = insert(AppConfig).values(key=k, value=new[k], updated_by=user)
                await s.execute(stmt.on_conflict_do_update(
                    index_elements=["key"], set_={"value": stmt.excluded.value, "updated_by": user,
                                                  "updated_at": stmt.excluded.updated_at},
                ))
            else:
                await s.execute(delete(AppConfig).where(AppConfig.key == k))
            s.add(ConfigChange(scope=GLOBAL_SCOPE, key=k, old_value=_audit_value(k, stored.get(k)),
                               new_value=_audit_value(k, new.get(k)), changed_by=user))
        if changed:
            await _bump_version(s, user)
    return changed


@dataclass
class ExperimentInput:
    name: str | None = None
    description: str | None = None
    instrument: str | None = None
    strategy: str | None = None
    enabled: bool | None = None
    capital: str | None = None  # "" clears it
    settings: dict[str, str | None] | None = None  # None value = back to the global/default value


def _parse_capital(raw: str) -> Decimal | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ConfigError(f"capital: not a number: {raw!r}") from None
    if not value.is_finite() or value <= 0:
        raise ConfigError("capital must be greater than 0")
    return value


async def save_experiment(
    db: Database, base: Settings, slug: str, data: ExperimentInput, user: str, *, create: bool = False
) -> list[str]:
    """Create or update an experiment. Returns the changed fields."""
    if data.settings:
        unknown = [k for k in data.settings if k not in EXPERIMENT_KEYS]
        if unknown:
            raise ConfigError(f"not an experiment setting: {', '.join(sorted(unknown))}")
    async with db.session() as s:
        row = await s.scalar(select(Experiment).where(Experiment.slug == slug).with_for_update())
        if create:
            if not SLUG_RE.match(slug):
                raise ConfigError("id: 2-63 characters, lower-case letters, digits and '-'")
            if row is not None:
                raise ConfigError(f"an experiment with id {slug!r} already exists")
            if not data.name or not data.instrument:
                raise ConfigError("name and instrument are required")
            row = Experiment(slug=slug, name="", instrument="", strategy="trend_pullback", enabled=False, settings={})
        elif row is None:
            raise ConfigError(f"no experiment {slug!r}")

        before = {
            "name": row.name, "description": row.description, "instrument": row.instrument,
            "strategy": row.strategy, "enabled": row.enabled, "capital": row.capital,
            "settings": dict(row.settings or {}),
        }
        after = {**before, "settings": dict(before["settings"])}
        if data.name is not None:
            name = data.name.strip()
            if not name or len(name) > 80:
                raise ConfigError("name: 1-80 characters")
            after["name"] = name
        if data.description is not None:
            after["description"] = data.description.strip()[:500] or None
        if data.instrument is not None:
            inst = data.instrument.strip().upper().replace("/", "_")
            if not INSTRUMENT_RE.match(inst):
                raise ConfigError("instrument: a currency pair like EUR_USD")
            if not create and inst != row.instrument and await _has_history(s, slug):
                raise ConfigError("instrument cannot change once the experiment has run; create a new experiment")
            after["instrument"] = inst
        if data.strategy is not None:
            if data.strategy not in STRATEGIES:
                raise ConfigError(f"strategy: one of {', '.join(STRATEGIES)}")
            after["strategy"] = data.strategy
        if data.enabled is not None:
            after["enabled"] = bool(data.enabled)
        if data.capital is not None:
            after["capital"] = _parse_capital(data.capital)
        for k, v in (data.settings or {}).items():
            if v is None:
                after["settings"].pop(k, None)
            else:
                after["settings"][k] = str(v).strip()

        stored = {r.key: r.value for r in (await s.scalars(select(AppConfig))).all()}
        experiment_settings(merge_global(base, stored), slug, after["instrument"], after["settings"])

        changed: list[tuple[str, Any, Any]] = []
        for k in ("name", "description", "instrument", "strategy", "enabled", "capital"):
            if before[k] != after[k] or create:
                changed.append((k, before[k], after[k]))
        for k in sorted(set(before["settings"]) | set(after["settings"])):
            if before["settings"].get(k) != after["settings"].get(k):
                changed.append((k, before["settings"].get(k), after["settings"].get(k)))
        if not changed:
            return []
        row.name, row.description, row.instrument = after["name"], after["description"], after["instrument"]
        row.strategy, row.enabled, row.capital = after["strategy"], after["enabled"], after["capital"]
        row.settings = after["settings"]
        if create:
            s.add(row)
        for k, old, new in changed:
            s.add(ConfigChange(scope=slug, key=k, old_value=None if old is None else str(old),
                               new_value=None if new is None else str(new), changed_by=user))
        await _bump_version(s, user)
    return [k for k, _, _ in changed]


async def _has_history(session: AsyncSession, slug: str) -> bool:
    from app.db.models import DecisionRequest

    return await session.scalar(select(DecisionRequest.id).where(DecisionRequest.experiment == slug).limit(1)) is not None


async def set_experiment_capital(db: Database, slug: str, capital: Decimal, user: str) -> None:
    """Record an experiment's starting capital (the engine does this once, from the account balance).

    Does not bump the config version: the engine that sets it is already running with it.
    """
    async with db.session() as s:
        row = await s.scalar(select(Experiment).where(Experiment.slug == slug).with_for_update())
        if row is None or row.capital is not None:
            return
        row.capital = capital
        s.add(ConfigChange(scope=slug, key="capital", old_value=None, new_value=str(capital), changed_by=user))


async def recent_changes(db: Database, limit: int = 50) -> list[dict[str, Any]]:
    async with db.session() as s:
        rows = (await s.scalars(select(ConfigChange).order_by(ConfigChange.id.desc()).limit(limit))).all()
    return [
        {
            "at": r.created_at.isoformat(), "scope": r.scope, "key": r.key,
            "old": r.old_value, "new": r.new_value, "by": r.changed_by,
        }
        for r in rows
    ]
