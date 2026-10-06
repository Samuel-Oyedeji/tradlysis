"""Configuration stored in the database (app/config/store.py)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.config import store
from app.config.store import ConfigError, ExperimentInput
from app.db.control import get_control, scoped, set_control
from app.db.enums import ControlKey
from app.db.models import AppConfig, ConfigChange, DecisionRequest
from tests.conftest import make_settings
from tests.helpers import T0


def test_field_scopes_are_disjoint_and_exist_in_settings():
    fields = set(make_settings().model_dump())
    assert store.GLOBAL_KEYS.isdisjoint(store.EXPERIMENT_KEYS)
    assert (store.GLOBAL_KEYS | store.EXPERIMENT_KEYS) <= fields
    assert set(store.ENV_ONLY_KEYS) <= fields
    assert not (set(store.ENV_ONLY_KEYS) & (store.GLOBAL_KEYS | store.EXPERIMENT_KEYS))
    assert "dashboard_password" in store.ENV_ONLY_KEYS and "trading_mode" in store.ENV_ONLY_KEYS


def test_layers_and_validation():
    base = make_settings(openrouter_model="env/model", risk_per_trade_pct=0.25)
    merged = store.merge_global(base, {"telegram_chat_id": "42", "risk_per_trade_pct": "0.9"})
    assert merged.telegram_chat_id == "42"
    assert merged.risk_per_trade_pct == 0.25, "experiment keys are ignored at the global level"
    exp = store.experiment_settings(merged, "gbp", "GBP_USD", {"risk_per_trade_pct": "0.5", "max_total_risk_pct": "1"})
    assert exp.experiment_name == "gbp" and exp.instrument == "GBP_USD" and exp.risk_per_trade_pct == 0.5
    assert exp.openrouter_model == "env/model", "unset experiment keys fall back to the global value"
    with pytest.raises(ConfigError, match="RISK_PER_TRADE_PCT must be in"):
        store.experiment_settings(merged, "gbp", "GBP_USD", {"risk_per_trade_pct": "5"})
    with pytest.raises(ConfigError, match="LLM_TIMEOUT_SECONDS"):
        store.merge_global(base, {"llm_timeout_seconds": "soon"})


async def test_env_is_imported_once(db):
    base = make_settings(
        database_url=db.engine.url.render_as_string(hide_password=False),
        telegram_chat_id="99", min_risk_reward=1.5, news_blocking_impacts="HIGH,MEDIUM",
    )
    async with db.session() as s:
        await set_control(s, ControlKey.DRAWDOWN_BREAKER, {"tripped": True}, "old")
    imported = await store.import_env_once(db, base)
    assert "telegram_chat_id" in imported and "capital_demo_api_key" in imported
    assert "database_url" not in imported and "dashboard_password" not in imported
    config = await store.load_configuration(db, base)
    [exp] = config.experiments
    assert exp.slug == "v1-trend-pullback-eur-usd" and exp.enabled and exp.name == "EUR/USD trend pullback"
    assert exp.overrides == {"min_risk_reward": "1.5", "news_blocking_impacts": "HIGH,MEDIUM"}
    assert exp.settings.min_risk_reward == 1.5
    async with db.session() as s:
        assert (await get_control(s, scoped(ControlKey.DRAWDOWN_BREAKER, exp.slug)))["tripped"] is True
        audit = (await s.scalars(select(ConfigChange).where(ConfigChange.key == "capital_demo_api_key"))).one()
    assert audit.new_value == store.SECRET_MASK, "secrets never reach the audit trail"
    # A value removed in the app is not re-imported on the next start.
    await store.save_global(db, base, {"telegram_chat_id": None}, "admin")
    assert await store.import_env_once(db, base) == []
    async with db.session() as s:
        assert await s.get(AppConfig, "telegram_chat_id") is None


async def test_save_global_validates_audits_and_bumps_version(db):
    base = make_settings(database_url=db.engine.url.render_as_string(hide_password=False))
    with pytest.raises(ConfigError, match="not editable"):
        await store.save_global(db, base, {"dashboard_password": "x"}, "admin")
    with pytest.raises(ConfigError):
        await store.save_global(db, base, {"candle_history_count": "lots"}, "admin")
    changed = await store.save_global(db, base, {"openrouter_api_key": "sk-new", "telegram_chat_id": "7"}, "admin")
    assert changed == ["openrouter_api_key", "telegram_chat_id"]
    assert await store.save_global(db, base, {"telegram_chat_id": "7"}, "admin") == [], "no-op saves change nothing"
    config = await store.load_configuration(db, base)
    assert config.settings.openrouter_api_key == "sk-new" and config.version == 1
    rows = await store.recent_changes(db)
    assert {r["key"]: r["new"] for r in rows} == {"telegram_chat_id": "7", "openrouter_api_key": store.SECRET_MASK}


async def test_experiments_are_created_and_validated(db):
    base = make_settings(database_url=db.engine.url.render_as_string(hide_password=False))
    with pytest.raises(ConfigError, match="instrument"):
        await store.save_experiment(db, base, "gbp-pullback", ExperimentInput(name="GBP", instrument="GBPUSD1"), "a",
                                    create=True)
    with pytest.raises(ConfigError, match="RISK_PER_TRADE_PCT"):
        await store.save_experiment(db, base, "gbp-pullback", ExperimentInput(
            name="GBP", instrument="GBP/USD", settings={"risk_per_trade_pct": "2"}), "a", create=True)
    with pytest.raises(ConfigError, match="id"):
        await store.save_experiment(db, base, "Bad Slug", ExperimentInput(name="x", instrument="EUR_USD"), "a",
                                    create=True)
    await store.save_experiment(db, base, "gbp-pullback", ExperimentInput(
        name="GBP pullback", instrument="gbp/usd", capital="5000", settings={"min_risk_reward": "1.5"}), "a",
        create=True)
    with pytest.raises(ConfigError, match="already exists"):
        await store.save_experiment(db, base, "gbp-pullback", ExperimentInput(name="x", instrument="EUR_USD"), "a",
                                    create=True)
    config = await store.load_configuration(db, base)
    exp = config.get("gbp-pullback")
    assert exp.instrument == "GBP_USD" and exp.capital == Decimal("5000") and not exp.enabled
    assert exp.settings.min_risk_reward == 1.5 and config.default_slug() == "gbp-pullback"

    changed = await store.save_experiment(db, base, "gbp-pullback", ExperimentInput(
        enabled=True, settings={"min_risk_reward": None, "max_spread_pips": "2.5"}), "a")
    assert changed == ["enabled", "max_spread_pips", "min_risk_reward"]
    exp = (await store.load_configuration(db, base)).get("gbp-pullback")
    assert exp.enabled and exp.overrides == {"max_spread_pips": "2.5"} and exp.settings.min_risk_reward == 2.0

    async with db.session() as s:
        s.add(DecisionRequest(experiment="gbp-pullback", instrument="GBP_USD", candle_time=T0, snapshot={},
                              strategy_result={}))
    with pytest.raises(ConfigError, match="cannot change"):
        await store.save_experiment(db, base, "gbp-pullback", ExperimentInput(instrument="EUR_USD"), "a")
    with pytest.raises(ConfigError, match="strategy cannot change"):
        await store.save_experiment(db, base, "gbp-pullback", ExperimentInput(strategy="range_breakout"), "a")
    with pytest.raises(ConfigError, match="strategy: one of"):
        await store.save_experiment(db, base, "gbp-pullback", ExperimentInput(strategy="martingale"), "a")

    await store.set_experiment_capital(db, "gbp-pullback", Decimal("1"), "engine")
    assert (await store.load_configuration(db, base)).get("gbp-pullback").capital == Decimal("5000"), \
        "the engine only fills in a missing capital"


def test_strategy_settings_are_tagged():
    assert store.STRATEGIES == {"trend_pullback": "Trend pullback", "range_breakout": "Range breakout"}
    assert store.FIELD_BY_KEY["breakout_range_bars"].strategies == ("range_breakout",)
    assert store.FIELD_BY_KEY["strategy_pullback_lookback_bars"].strategies == ("trend_pullback",)
    assert store.FIELD_BY_KEY["min_risk_reward"].strategies == ()


async def test_changing_capital_rebases_the_breaker_marks(db):
    base = make_settings(database_url=db.engine.url.render_as_string(hide_password=False))
    await store.save_experiment(db, base, "eur", ExperimentInput(name="EUR", instrument="EUR_USD", capital="100000"),
                                "a", create=True)
    async with db.session() as s:
        await set_control(s, scoped(ControlKey.PEAK_NAV, "eur"), {"value": "100000"}, "reconciliation")
        await set_control(s, scoped(ControlKey.DAY_START_NAV, "eur"), {"trading_day": "2026-10-06", "value": "100000"}, "r")
    await store.save_experiment(db, base, "eur", ExperimentInput(name="EUR 2"), "a")
    async with db.session() as s:
        assert (await get_control(s, scoped(ControlKey.PEAK_NAV, "eur")))["value"] == "100000", "only capital re-bases"
    await store.save_experiment(db, base, "eur", ExperimentInput(capital="500"), "a")
    async with db.session() as s:
        assert (await get_control(s, scoped(ControlKey.PEAK_NAV, "eur")))["value"] == "0"
        assert (await get_control(s, scoped(ControlKey.DAY_START_NAV, "eur")))["trading_day"] is None
