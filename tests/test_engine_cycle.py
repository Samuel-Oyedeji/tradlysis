"""End-to-end decision cycles through TradingEngine with a fake broker, fake LLM and real DB."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.alerts.telegram import TelegramSender
from app.db.models import Decision, DecisionRequest, Order, RiskCheck, SystemEvent, TechnicalSnapshot, Trade
from app.decision.openrouter import OpenRouterClient
from app.engine import TradingEngine
from app.market_data.state import PriceTick
from app.market_data.timeutil import floor_time, utcnow
from app.news.providers.base import CalendarEvent
from app.strategy import trend_pullback
from app.technicals.regime import MarketRegimeLabel
from tests.conftest import make_settings, trend_bars
from tests.fake_broker import FakeBroker, make_client
from tests.helpers import long_state

# Decisions API answers (Jev): a BUY choice at 80% plus yes/no checks.
BUY = {
    "decision": {"type": "choice", "choice": "BUY", "confidence": 0.8,
                 "probabilities": {"BUY": 0.8, "WAIT": 0.15, "SELL": 0.05}},
    "htf_trend_supports": {"type": "noul", "noul": 0.9},
    "pullback_to_level": {"type": "noul", "noul": 0.8},
    "momentum_confirms": {"type": "noul", "noul": 0.7},
    "room_to_target": {"type": "noul", "noul": 0.6},
    "news_risk_high": {"type": "noul", "noul": 0.1},
}


class FakeCalendar:
    name = "fake"

    def __init__(self, events=None):
        self.events = events or []

    async def fetch(self):
        return self.events


@pytest.fixture(autouse=True)
def market_always_open(monkeypatch):
    monkeypatch.setattr("app.engine.is_fx_market_open", lambda _now: True)
    monkeypatch.setattr("app.execution.executor.LOOKUP_DELAY_SECONDS", 0)
    monkeypatch.setattr("app.execution.executor.CONFIRM_DELAY_SECONDS", 0)


def llm_client(answers, calls):
    def handler(req):
        assert req.url.path == "/api/alpha/decisions"
        calls.append(json.loads(req.content))
        return httpx.Response(200, json={"answers": answers, "model": "typesafe/jev-1.13",
                                         "usage": {"input_tokens": 900, "output_tokens": 6}})

    return OpenRouterClient("key", "https://or.test/api/v1", transport=httpx.MockTransport(handler))


async def make_engine(db, broker, llm, calendar=None, **settings_kw) -> TradingEngine:
    settings = make_settings(database_url=db.engine.url.render_as_string(hide_password=False), **settings_kw)
    client = make_client(broker)
    engine = TradingEngine(settings, db=db, client=client, llm=llm, calendar=calendar or FakeCalendar(), feeds=[],
                           telegram=TelegramSender("", ""))
    await engine.setup()
    await engine.reconciler.reconcile_once()
    await engine.market.backfill()
    await engine.news.poll_calendar()
    engine.news.last_calendar_success = utcnow()
    engine.state.stream_connected = True
    engine.state.on_tick(PriceTick("EUR_USD", utcnow(), broker.bid, broker.ask), utcnow())
    return engine


def seed_candles(broker: FakeBroker, candle_time):
    broker.candles = {
        "M15": trend_bars(300, 1.08, 0.0002, minutes=15, start_time=candle_time - timedelta(minutes=15 * 299)),
        "H1": trend_bars(300, 1.05, 0.0004, minutes=60, start_time=candle_time - timedelta(hours=300)),
        "H4": trend_bars(300, 0.90, 0.0010, minutes=240, start_time=candle_time - timedelta(hours=4 * 300)),
        "D": trend_bars(5, 1.09, 0.001, minutes=1440, start_time=candle_time - timedelta(days=6)),
        "W": trend_bars(3, 1.08, 0.002, minutes=10080, start_time=candle_time - timedelta(days=28)),
    }


async def _decision(db):
    async with db.session() as s:
        return await s.scalar(select(Decision))


async def count(db, model):
    async with db.session() as s:
        return await s.scalar(select(func.count()).select_from(model))


async def test_no_setup_is_logged_as_prefilter_wait_and_deduplicated(db):
    broker = FakeBroker()
    candle_time = floor_time(utcnow(), "M15") - timedelta(minutes=15)
    seed_candles(broker, candle_time)
    calls = []
    engine = await make_engine(db, broker, llm_client(BUY, calls))
    summary = (await engine.run_cycle(candle_time))[engine.primary.slug]
    assert summary.request_id is not None, summary
    assert summary.decision == "WAIT" and not summary.llm_called
    assert calls == [], "no LLM call without a deterministic candidate"
    async with db.session() as s:
        req = await s.get(DecisionRequest, summary.request_id)
        dec = await s.scalar(select(Decision))
    assert req.snapshot["pair"] == "EUR_USD" and req.snapshot["price"]["spread_pips"] == 1.0
    assert set(req.snapshot["timeframes"]) == {"4h", "1h", "15m"}
    assert req.snapshot["market_regime"]["label"] in {r.value for r in MarketRegimeLabel}
    assert any(lv["source"] == "ROUND_NUMBER" for lv in req.snapshot["levels"]["support"] + req.snapshot["levels"]["resistance"])
    assert dec.source == "PREFILTER" and dec.reason_codes
    assert await count(db, TechnicalSnapshot) == 3
    # Same candle again (e.g. after a restart): not processed twice.
    again = (await engine.run_cycle(candle_time))[engine.primary.slug]
    assert again.note == "cycle already processed"
    assert await count(db, DecisionRequest) == 1
    assert await _setup_events(db) == [], "no setup, nothing for Telegram"


async def test_candidate_confirmed_approved_and_executed(db, monkeypatch):
    broker = FakeBroker()
    candle_time = floor_time(utcnow(), "M15") - timedelta(minutes=15)
    seed_candles(broker, candle_time)
    # Force the deterministic strategy to see the hand-crafted long setup.
    real = trend_pullback.evaluate_trend_pullback
    monkeypatch.setattr("app.engine.evaluate_trend_pullback", lambda tech, bid, ask, s: real(long_state(), bid, ask, s))
    calls = []
    engine = await make_engine(db, broker, llm_client(BUY, calls))
    summary = (await engine.run_cycle(candle_time))[engine.primary.slug]
    assert summary.candidate and summary.llm_called and summary.decision == "BUY", summary
    assert summary.approved, summary.rejections
    assert summary.order_status == "FILLED"
    assert len(calls) == 1
    sent = calls[0]
    assert sent["model"] == "typesafe/jev-1.13"
    assert sent["state"]["setup_check"]["candidate"] is True  # the snapshot is the state
    assert sent["questions"]["decision"]["type"] == "choice"
    assert set(sent["questions"]["decision"]["criteria"]) == {"BUY", "SELL", "WAIT"}
    async with db.session() as s:
        req = await s.scalar(select(DecisionRequest))
        rc = await s.scalar(select(RiskCheck))
        order = await s.scalar(select(Order))
        trade = await s.scalar(select(Trade))
    assert req.llm_called and req.model == "typesafe/jev-1.13" and req.prompt_version == "decision-v3"
    assert req.messages[0]["role"] == "questions"
    dec = await _decision(db)
    assert dec.confidence == pytest.approx(0.8) and dec.prompt_tokens == 900
    assert dec.reason_codes == ["HTF_BULLISH", "PULLBACK_TO_SUPPORT", "MOMENTUM_CONFIRMED", "ROOM_TO_TARGET"]
    assert rc.approved and rc.units > 0 and rc.risk_pct == pytest.approx(0.25, abs=0.01)
    assert order.status == "FILLED" and order.units == rc.units
    assert trade.state == "OPEN" and trade.order_id == order.id
    assert (await _setup_events(db)) == ["BUY EUR_USD"]


async def test_news_blackout_blocks_before_llm(db, monkeypatch):
    broker = FakeBroker()
    candle_time = floor_time(utcnow(), "M15") - timedelta(minutes=15)
    seed_candles(broker, candle_time)
    real = trend_pullback.evaluate_trend_pullback
    monkeypatch.setattr("app.engine.evaluate_trend_pullback", lambda tech, bid, ask, s: real(long_state(), bid, ask, s))
    nfp = CalendarEvent("fake", "nfp", "Non-Farm Payrolls", "USD", "HIGH", utcnow() + timedelta(minutes=10))
    calls = []
    engine = await make_engine(db, broker, llm_client(BUY, calls), calendar=FakeCalendar([nfp]))
    summary = (await engine.run_cycle(candle_time))[engine.primary.slug]
    assert summary.candidate and summary.decision == "WAIT" and not calls
    async with db.session() as s:
        dec = await s.scalar(select(Decision))
    assert dec.reason_codes == ["NEWS_BLACKOUT"]
    assert not broker.order_posts
    assert (await _setup_events(db)) == ["BUY EUR_USD"]


async def test_llm_buy_without_plan_is_rejected_by_risk(db):
    broker = FakeBroker()
    candle_time = floor_time(utcnow(), "M15") - timedelta(minutes=15)
    seed_candles(broker, candle_time)
    calls = []
    engine = await make_engine(db, broker, llm_client(BUY, calls), llm_call_policy="always")
    summary = (await engine.run_cycle(candle_time))[engine.primary.slug]
    assert summary.llm_called and summary.decision == "BUY"
    assert summary.approved is False and "TRADE_PLAN_MATCHES" in summary.rejections
    assert not broker.order_posts


async def _setup_events(db) -> list[str]:
    """First line ("<direction> <instrument>") of every SETUP event, i.e. each setup Telegram heard about."""
    async with db.session() as s:
        rows = (await s.scalars(select(SystemEvent).where(SystemEvent.event_type == "SETUP"))).all()
    return [" ".join(r.message.split("\n")[0].split(" ")[:2]) for r in rows]


async def make_multi_engine(db, broker, llm, experiments: dict[str, dict]) -> TradingEngine:
    """An engine running several experiments (slug -> experiment settings overrides) on one account."""
    from app.config.store import Configuration, ExperimentConfig, experiment_settings

    base = make_settings(database_url=db.engine.url.render_as_string(hide_password=False))
    config = Configuration(settings=base, experiments=[
        ExperimentConfig(id=i, slug=slug, name=slug.upper(), description=None, instrument="EUR_USD",
                         strategy="trend_pullback", enabled=True, capital=None, overrides=over,
                         settings=experiment_settings(base, slug, "EUR_USD", over))
        for i, (slug, over) in enumerate(experiments.items())
    ])
    engine = TradingEngine(base, configuration=config, db=db, client=make_client(broker), llm=llm,
                           calendar=FakeCalendar(), feeds=[], telegram=TelegramSender("", ""))
    await engine.setup()
    await engine.reconciler.reconcile_once()
    await engine.market.backfill()
    await engine.news.poll_calendar()
    engine.news.last_calendar_success = utcnow()
    engine.state.stream_connected = True
    engine.state.on_tick(PriceTick("EUR_USD", utcnow(), broker.bid, broker.ask), utcnow())
    return engine


@pytest.mark.parametrize("hedging", [False, True])
async def test_two_experiments_share_the_account(db, monkeypatch, hedging):
    broker = FakeBroker(hedging_mode=hedging)
    candle_time = floor_time(utcnow(), "M15") - timedelta(minutes=15)
    seed_candles(broker, candle_time)
    real = trend_pullback.evaluate_trend_pullback
    monkeypatch.setattr("app.engine.evaluate_trend_pullback", lambda tech, bid, ask, s: real(long_state(), bid, ask, s))
    calls = []
    engine = await make_multi_engine(db, broker, llm_client(BUY, calls), {
        "pullback-a": {},
        "pullback-b": {"risk_per_trade_pct": "0.5", "max_total_risk_pct": "1.0", "openrouter_model": "typesafe/jev-1.13"},
    })
    summaries = await engine.run_cycle(candle_time)
    assert set(summaries) == {"pullback-a", "pullback-b"}
    assert all(s.decision == "BUY" for s in summaries.values()), summaries
    async with db.session() as s:
        reqs = (await s.scalars(select(DecisionRequest).order_by(DecisionRequest.experiment))).all()
        trades = (await s.scalars(select(Trade).order_by(Trade.experiment))).all()
        checks = (await s.scalars(select(RiskCheck))).all()
    assert [r.experiment for r in reqs] == ["pullback-a", "pullback-b"]
    assert len(checks) == 2
    # Each experiment sizes on its own equity and risk setting (0.25% vs 0.5% of 100,000).
    sized = sorted(c.risk_pct for c in checks if c.units)
    assert sized[0] == pytest.approx(0.25, abs=0.01) and sized[-1] == pytest.approx(0.5, abs=0.02)
    if hedging:
        # Positions are kept apart: both experiments trade, each trade is attributed.
        assert len(broker.order_posts) == 2
        assert [t.experiment for t in trades] == ["pullback-a", "pullback-b"]
    else:
        # A netting account: the second order would reduce the first, so only one fills.
        assert len(broker.order_posts) == 1 and len(trades) == 1
        loser = next(s for s in summaries.values() if s.order_status != "FILLED")
        assert loser.order_status == "FAILED" or "EXISTING_POSITION" in (loser.rejections or [])
    status = engine.status()
    assert set(status["experiments"]) == {"pullback-a", "pullback-b"}
    assert status["experiments"]["pullback-b"]["risk_limits"]["risk_per_trade_pct"] == 0.5
    assert status["markets"]["EUR_USD"]["stream_connected"]


async def test_kill_switch_of_one_experiment_leaves_the_other_trading(db, monkeypatch):
    from app.db.control import scoped, set_control
    from app.db.enums import ControlKey

    broker = FakeBroker(hedging_mode=True)
    candle_time = floor_time(utcnow(), "M15") - timedelta(minutes=15)
    seed_candles(broker, candle_time)
    real = trend_pullback.evaluate_trend_pullback
    monkeypatch.setattr("app.engine.evaluate_trend_pullback", lambda tech, bid, ask, s: real(long_state(), bid, ask, s))
    engine = await make_multi_engine(db, broker, llm_client(BUY, []), {"pullback-a": {}, "pullback-b": {}})
    async with db.session() as s:
        await set_control(s, scoped(ControlKey.KILL_SWITCH, "pullback-a"), {"active": True, "reason": "test"}, "t")
    summaries = await engine.run_cycle(candle_time)
    assert "KILL_SWITCH" in summaries["pullback-a"].rejections
    assert summaries["pullback-b"].order_status == "FILLED"
