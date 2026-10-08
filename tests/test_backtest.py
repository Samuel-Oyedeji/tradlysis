"""Backtester (app/backtest.py): trade simulation, replay through the risk engine, history paging."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

import app.engine as engine_mod
from app import backtest as bt
from app.broker.types import InstrumentInfo
from app.config.store import ConfigError, single_experiment
from app.market_data.candles import Bar
from app.strategy.base import Condition, StrategyResult, TradePlan
from tests.conftest import make_settings
from tests.fake_broker import FakeBroker, make_client

INFO = InstrumentInfo("EUR_USD", -4, 5, 0, 100, 0.0333, "EURUSD")
T0 = datetime(2026, 7, 20, tzinfo=UTC)
P = 1.1000


def flat(minutes: int, n: int, start: datetime = T0, half_spread: float = 0.00005) -> list[Bar]:
    return [Bar(start + timedelta(minutes=minutes * i), P, P + 0.0002, P - 0.0002, P, 100, True,
                P - half_spread, P + half_spread) for i in range(n)]


def history(spike_at: datetime | None = None, half_spread: float = 0.00005) -> bt.History:
    m15 = flat(15, 96 * 52, half_spread=half_spread)
    if spike_at:
        i = next(k for k, b in enumerate(m15) if b.time == spike_at)
        m15[i].high = P + 0.0050
    return bt.History("EUR_USD", INFO, {"M15": m15, "H1": flat(60, 24 * 52), "H4": flat(240, 6 * 52),
                                         "D": [], "W": [], "M": []})


def fake_strategy(signals: dict[datetime, str]):
    """A strategy proposing a 20-pip-stop, 2R trade at the given decision times."""

    def evaluate(name, tech, bid, ask, settings):
        direction = signals.get(tech.as_of)
        if not direction:
            return StrategyResult("TREND_PULLBACK", None, False, [Condition("pullback_to_level", False)], None,
                                  ["NO_PULLBACK_TO_LEVEL"])
        sign = 1 if direction == "BUY" else -1
        entry = ask if sign > 0 else bid
        plan = TradePlan(direction, "TREND_PULLBACK", entry, entry - sign * 0.0020, entry + sign * 0.0040,
                         20, 40, 2.0, "TEST", "TEST")
        return StrategyResult("TREND_PULLBACK", direction, True, [Condition("ok", True)], plan, [])

    return evaluate


def run_for(settings=None) -> bt.Run:
    exp = single_experiment(settings or make_settings()).experiments[0]
    return bt.build_runs([exp], {}, {})[0]


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 8, hour, minute, tzinfo=UTC)  # a Tuesday


def trade(direction="BUY", entry=1.1, sl=1.098, tp=1.104) -> bt.SimTrade:
    return bt.SimTrade(direction, at(9), entry, sl, tp, 1000, 0.00005, 20)


def bar(o, h, lo, c=None) -> Bar:
    return Bar(at(10), o, h, lo, o if c is None else c)


def test_step_trade_exits():
    assert bt.step_trade(trade(), bar(1.1, 1.1042, 1.0999)) == ("TAKE_PROFIT", 1.104)
    assert bt.step_trade(trade(), bar(1.1, 1.10404, 1.0999)) is None, "the bid (mid - half spread) must reach it"
    assert bt.step_trade(trade(), bar(1.1, 1.1001, 1.0979)) == ("STOP_LOSS", 1.098)
    t = trade()
    assert bt.step_trade(t, bar(1.1, 1.105, 1.097)) == ("STOP_LOSS", 1.098) and t.ambiguous
    assert bt.step_trade(trade(), bar(1.097, 1.0975, 1.096)) == ("STOP_LOSS", pytest.approx(1.09695)), "gap"
    short = trade("SELL", 1.1, 1.102, 1.096)
    assert bt.step_trade(short, bar(1.1, 1.1001, 1.0959)) == ("TAKE_PROFIT", 1.096)
    assert bt.step_trade(trade("SELL", 1.1, 1.102, 1.096), bar(1.1, 1.1020, 1.0999)) == ("STOP_LOSS", 1.102)
    t = trade()
    bt.step_trade(t, bar(1.1, 1.1021, 1.0991))
    assert t.best_r == pytest.approx(1.025) and t.worst_r == pytest.approx(-0.475)  # at the bid


def test_replay_trades_through_the_risk_engine(monkeypatch):
    signals = {at(10, 30): "BUY", at(10, 45): "BUY", at(11, 15): "BUY", at(13, 30): "SELL"}
    monkeypatch.setattr(engine_mod, "evaluate_strategy", fake_strategy(signals))
    run = run_for()
    bt.replay(history(spike_at=at(11)), [run], at(10), at(14), account_currency="USD")

    assert run.cycles == 16 and run.setups == 4
    assert run.near_misses == {"pullback_to_level": 12}
    # 10:45 is blocked by the open position; 11:15 (just after the target) by the cooldown.
    assert run.risk_blocks == {"EXISTING_POSITION": 1, "MAX_TOTAL_RISK": 1, "SIGNAL_COOLDOWN": 2}
    first, second = run.trades
    assert (first.opened, first.outcome, first.closed) == (at(10, 30), "TAKE_PROFIT", at(11, 15))
    assert first.entry == pytest.approx(1.10005) and first.units == 12500  # 0.25% of 10000 / 20 pips
    assert first.r == pytest.approx(2.0) and first.pnl == pytest.approx(50.0)
    assert second.direction == "SELL" and second.outcome == "OPEN"
    assert run.balance == pytest.approx(10050)

    s = bt.summarize(run, 1)
    assert (s["trades"], s["closed"], s["wins"], s["total_r"], s["pnl"]) == (2, 1, 1, 2.0, 50.0)
    text = bt.render({"start": at(10).isoformat(), "end": at(14).isoformat(), "days": 1, "runs": [s]}, trades=True)
    assert "1 won / 0 lost (100%)" in text and "TAKE_PROFIT +2.00R" in text
    assert "setups the risk engine refused: SIGNAL_COOLDOWN 2, EXISTING_POSITION 1, MAX_TOTAL_RISK 1" in text


def test_replay_applies_the_experiment_limits(monkeypatch):
    monkeypatch.setattr(engine_mod, "evaluate_strategy", fake_strategy({at(10, 30): "BUY"}))
    wide = run_for()
    bt.replay(history(half_spread=0.0002), [wide], at(10), at(11), account_currency="USD")
    assert wide.trades == [] and wide.risk_blocks == {"SPREAD_LIMIT": 1}
    strict = run_for(make_settings(min_risk_reward=2.5))
    bt.replay(history(), [strict], at(10), at(11), account_currency="USD")
    assert strict.risk_blocks == {"MIN_RISK_REWARD": 1}


def test_daily_loss_breaker_halts_the_rest_of_the_day(monkeypatch):
    signals = {at(10, 30): "BUY", at(12): "SELL", at(13): "SELL"}
    monkeypatch.setattr(engine_mod, "evaluate_strategy", fake_strategy(signals))
    run = run_for(make_settings(max_daily_loss_pct=0.2))
    h = history()
    crash = next(b for b in h.bars["M15"] if b.time == at(11))
    crash.low = P - 0.0030  # stops the BUY: -0.25%
    bt.replay(h, [run], at(10), at(14), account_currency="USD")
    assert [t.outcome for t in run.trades] == ["STOP_LOSS"]
    assert run.breaker_trips == ["2026-09-08 12:00 daily loss limit"]
    assert run.risk_blocks["DAILY_LOSS_BREAKER"] == 1 and run.risk_blocks["MAX_DAILY_LOSS"] == 2


def test_assignments_and_variants():
    assert bt.parse_assignments(["min_risk_reward=1.5"], multi=False) == {"min_risk_reward": "1.5"}
    assert bt.parse_assignments(["breakout_min_touches=1, 2"], multi=True) == {"breakout_min_touches": ["1", "2"]}
    for bad in ["nope=1", "capital_api_key=x", "instrument=GBP_USD", "trading_mode=live", "min_risk_reward"]:
        with pytest.raises(ConfigError):
            bt.parse_assignments([bad], multi=False)
    assert len(bt.variants({"a": ["1", "2"], "b": ["x", "y", "z"]})) == 6
    with pytest.raises(ConfigError, match="at most"):
        bt.variants({"a": [str(i) for i in range(13)]})

    exp = single_experiment(make_settings(trading_enabled=False)).experiments[0]
    runs = bt.build_runs([exp], {"min_risk_reward": "1.5"}, {"max_spread_pips": ["1", "2"]})
    assert [r.label for r in runs] == [
        f"{exp.slug} [min_risk_reward=1.5, max_spread_pips=1]", f"{exp.slug} [min_risk_reward=1.5, max_spread_pips=2]"
    ]
    assert runs[1].settings.max_spread_pips == 2 and runs[0].settings.trading_enabled
    with pytest.raises(ConfigError, match="MIN_RISK_REWARD"):
        bt.build_runs([exp], {"min_risk_reward": "-1"}, {})
    with pytest.raises(ConfigError, match="unknown experiment"):
        bt.select_experiments(single_experiment(make_settings()), ["nope"])


async def test_candles_are_fetched_in_windows_the_api_accepts():
    broker = FakeBroker()
    broker.candles["M15"] = flat(15, 96 * 30)
    client = make_client(broker)
    got = await client.get_candles_between("M15", T0 + timedelta(days=2), T0 + timedelta(days=25))
    assert len(got) == 96 * 23 and got[0].time == T0 + timedelta(days=2)
    assert broker.price_requests == 3  # 1000 candles per request at most
    # windows without prices (before the history starts) are skipped
    early = await client.get_candles_between("M15", T0 - timedelta(days=12), T0 + timedelta(days=1))
    assert len(early) == 96
    await client.aclose()


async def test_backtest_end_to_end_against_the_broker(monkeypatch):
    monkeypatch.setattr(engine_mod, "evaluate_strategy", fake_strategy({at(10, 30): "BUY"}))
    h = history(spike_at=at(11))
    broker = FakeBroker()
    broker.candles = {g: h.bars[g] for g in ("M15", "H1", "H4")}
    client = make_client(broker)
    config = single_experiment(make_settings())
    notes: list[str] = []
    result = await bt.backtest(config, client, days=1, end=at(14, 5), vary={"max_spread_pips": ["0.5", "1.5"]},
                               progress=notes.append)
    await client.aclose()
    assert result["start"] == at(14).replace(day=7).isoformat() and result["account_currency"] == "USD"
    tight, normal = result["runs"]
    assert tight["trades"] == 0 and tight["risk_blocks"] == {"SPREAD_LIMIT": 1}
    assert normal["trades"] == 1 and normal["total_r"] == 2.0
    assert "EUR_USD: downloading candles" in notes
    text = bt.render(result)
    assert "Variants:" in text and "max_spread_pips=0.5" in text
    assert result["history"]["EUR_USD"]["H4"]["candles"] == 6 * 50 + 3  # complete by the end (14:00) only
    assert "EUR_USD candles: M15 " in text


async def test_data_api_backtest(db, monkeypatch):
    from app.api.main import create_app
    from app.config import store

    token = "t" * 32
    base = make_settings(database_url=db.engine.url.render_as_string(hide_password=False))
    await store.save_global(db, base, {"data_api_token": token}, "test")
    broker = FakeBroker()
    monkeypatch.setattr(bt, "make_client", lambda settings, experiments: make_client(broker))
    app = create_app(base, db=db)
    app.state.db = db
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/api/data/backtest?days=1")).status_code == 401
        assert (await c.get("/api/data/backtest?set.capital_api_key=x", headers=headers)).status_code == 400
        assert (await c.get("/api/data/backtest?days=999", headers=headers)).status_code == 400
        r = (await c.get("/api/data/backtest?days=1&wait=30&set.min_risk_reward=1.5", headers=headers)).json()
        assert r["status"] == "done", r
        assert r["params"]["set"] == {"min_risk_reward": "1.5"} and "Tradlysis backtest" in r["text"]
        again = (await c.get("/api/data/backtest?days=1&set.min_risk_reward=1.5", headers=headers)).json()
        assert again["started_at"] == r["started_at"], "a finished run is served again"
