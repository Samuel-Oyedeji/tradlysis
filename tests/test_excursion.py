from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.experiments.excursion import (
    M5,
    M15,
    PathBar,
    close_category,
    compute_excursion,
    load_path,
    summarize,
)

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)


def path(points, step=M15, start=T0):
    """points: (high, low, close) per bar."""
    return [PathBar(start + step * i, step, h, lo, c) for i, (h, lo, c) in enumerate(points)]


def test_sell_like_the_oct_1_trade():
    # SELL 1.12922, stop 1.13146 (22.4 pips = 1R), target 1.12010 (~4.1R); dipped to 1.1218, closed 1.1240.
    bars = path([(1.1295, 1.1288, 1.1290), (1.1280, 1.1250, 1.1255), (1.1240, 1.1218, 1.1225), (1.1245, 1.1222, 1.1240)])
    ex = compute_excursion(direction="SELL", entry=1.12922, risk=0.00224, start=T0, end=T0 + M15 * 4,
                           path=bars, close_price=1.1240, take_profit=1.12010)
    assert ex.best_r == pytest.approx((1.12922 - 1.1218) / 0.00224, abs=1e-3)  # ~3.3R
    assert ex.result_r == pytest.approx((1.12922 - 1.1240) / 0.00224, abs=1e-3)  # ~2.3R
    assert ex.given_back_r == pytest.approx(ex.best_r - ex.result_r, abs=1e-3)
    assert ex.worst_r == pytest.approx((1.1295 - 1.12922) / 0.00224, abs=1e-3)
    assert ex.target_r == pytest.approx(4.071, abs=1e-3)
    assert ex.reached == {"1R": True, "2R": True, "3R": True}
    assert ex.best_at == T0 + M15 * 2 and not ex.open


def test_buy_stopped_out_and_window():
    bars = path([(1.2000, 1.1000, 1.1500)] + [(1.1010, 1.0995, 1.1000), (1.1012, 1.0980, 1.0980)], start=T0 - M15)
    # The first bar ends exactly at the open, so it is outside the trade window.
    ex = compute_excursion(direction="BUY", entry=1.1000, risk=0.0020, start=T0, end=T0 + M15 * 2,
                           path=bars, close_price=1.0980, take_profit=1.1050)
    assert ex.bars == 2
    assert ex.best_r == pytest.approx(0.6) and ex.worst_r == pytest.approx(1.0) and ex.result_r == pytest.approx(-1.0)
    assert ex.reached["1R"] is False and ex.given_back_r == pytest.approx(1.6)


def test_exit_level_caps_the_candle_overshoot():
    # The candle that hit the stop (1.0980) ran on to 1.0970 after the trade was closed.
    bars = path([(1.1010, 1.0995, 1.1000), (1.1001, 1.0970, 1.0975)])
    kw = dict(direction="BUY", entry=1.1000, risk=0.0020, start=T0, end=T0 + M15 * 2, path=bars, close_price=1.0980)
    assert compute_excursion(**kw).worst_r == pytest.approx(1.5)
    assert compute_excursion(**kw, exit_level="stop").worst_r == pytest.approx(1.0)
    up = path([(1.1065, 1.0995, 1.1060)])
    tp = dict(direction="BUY", entry=1.1000, risk=0.0020, start=T0, end=T0 + M15, path=up, close_price=1.1050)
    assert compute_excursion(**tp, exit_level="target").best_r == pytest.approx(2.5)


def test_open_and_unknown_close():
    bars = path([(1.1010, 1.0990, 1.1008)])
    live = compute_excursion(direction="BUY", entry=1.1000, risk=0.0010, start=T0, end=T0 + M15, path=bars, is_open=True)
    assert live.open and live.result_r == pytest.approx(0.8) and live.best_r == pytest.approx(1.0)
    unknown = compute_excursion(direction="BUY", entry=1.1000, risk=0.0010, start=T0, end=T0 + M15, path=bars)
    assert unknown.result_r is None and unknown.given_back_r is None and unknown.best_r == pytest.approx(1.0)
    assert compute_excursion(direction="BUY", entry=1.1, risk=None, start=T0, end=T0 + M15, path=bars) is None
    assert compute_excursion(direction="BUY", entry=1.1, risk=0.001, start=T0, end=T0 + M15, path=[]) is None


def trade(**kw):
    base = dict(state="CLOSED", close_reason="CLOSED", broker_trade_id="d1")
    base.update(kw)
    return SimpleNamespace(**base)


def test_close_categories():
    assert close_category(trade(close_reason="TAKE_PROFIT"), {}) == "take_profit"
    assert close_category(trade(close_reason="STOP_LOSS"), {}) == "stop_loss"
    assert close_category(trade(close_reason="CLOSED"), {}) == "manual"  # closed in the Capital.com platform
    assert close_category(trade(close_reason="CLOSED"), {"d1": ["dashboard (admin)"]}) == "manual"  # "close all"
    assert close_category(trade(close_reason="CLOSED"), {"d1": ["slippage 3.0 pips exceeds ..."]}) == "bot_exit"
    assert close_category(trade(close_reason="MARGIN_CLOSEOUT"), {}) == "margin_closeout"
    assert close_category(trade(close_reason="CLOSED_UNKNOWN"), {}) == "unknown"
    assert close_category(trade(state="OPEN"), {}) is None


def test_summarize():
    ex = lambda best, res, tgt=4.0: {"best_r": best, "worst_r": 0.3, "result_r": res, "given_back_r": best - res,  # noqa: E731
                                     "target_r": tgt, "reached": {"1R": best >= 1, "2R": best >= 2, "3R": best >= 3}}
    rows = [
        {"outcome": "take_profit", "excursion": ex(4.0, 4.0)},
        {"outcome": "stop_loss", "excursion": ex(0.5, -1.0)},
        {"outcome": "stop_loss", "excursion": ex(2.2, -1.0)},
        {"outcome": "manual", "excursion": ex(3.3, 2.3)},
    ]
    s = summarize(rows)
    assert s["counts"] == {"take_profit": 1, "stop_loss": 2, "manual": 1}
    assert s["not_reaching_tp"]["trades"] == 3
    assert s["not_reaching_tp"]["reached"] == {"1R": 2, "2R": 2, "3R": 1}
    assert s["by_outcome"]["stop_loss"]["avg_best_r"] == pytest.approx(1.35)
    assert s["by_outcome"]["manual"]["avg_given_back_r"] == pytest.approx(1.0)
    assert s["avg_target_r"] == 4.0


# ---------------------------------------------------------------------- database


async def add_candles(db, granularity, step, points, start):
    from app.db.models import Candle

    async with db.session() as s:
        for i, (h, lo, c) in enumerate(points):
            s.add(Candle(instrument="EUR_USD", granularity=granularity, time=start + step * i, open=c, high=h, low=lo,
                         close=c, volume=1, complete=True))


async def test_load_path_prefers_five_minute_bars(db):
    # Two 15-minute slots; the first is fully covered by 5-minute bars, the second is not.
    await add_candles(db, "M15", M15, [(1.11, 1.09, 1.10), (1.12, 1.08, 1.10)], T0)
    await add_candles(db, "M5", M5, [(1.101, 1.099, 1.10), (1.105, 1.098, 1.10), (1.11, 1.09, 1.10), (1.12, 1.08, 1.10)], T0)
    async with db.session() as s:
        p = await load_path(s, "EUR_USD", T0, T0 + M15 * 2)
    assert [b.duration for b in p] == [M5, M5, M5, M15]
    assert p[-1].time == T0 + M15


async def test_trades_api_and_analysis_report_outcomes(db):
    import httpx

    from app.api.main import create_app
    from app.db.models import Trade
    from tests.conftest import make_settings

    settings = make_settings(database_url=db.engine.url.render_as_string(hide_password=False))
    await add_candles(db, "M15", M15, [(1.1295, 1.1288, 1.1290), (1.1280, 1.1218, 1.1225), (1.1245, 1.1222, 1.1240)], T0)
    async with db.session() as s:
        s.add(Trade(broker_trade_id="d1", experiment=settings.experiment_name, instrument="EUR_USD", direction="SELL",
                    initial_units=-1000, current_units=0, open_price=1.12922, open_time=T0, stop_loss=1.13146,
                    take_profit=1.12010, initial_risk_price=0.00224, state="CLOSED", close_price=1.1240,
                    close_time=T0 + M15 * 3, close_reason="CLOSED", realized_pl=Decimal("5.2"), r_multiple=2.33))
    app = create_app(settings, db=db)
    app.state.db = db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        [t] = (await c.get("/api/trades?state=CLOSED", auth=("admin", "secret"))).json()
        assert t["outcome"] == "manual"
        assert t["excursion"]["best_r"] == pytest.approx((1.12922 - 1.1218) / 0.00224, abs=1e-3)
        assert t["excursion"]["result_r"] == pytest.approx((1.12922 - 1.1240) / 0.00224, abs=1e-3)
        run = await c.post("/api/analysis/run", auth=("admin", "secret"), headers={"X-Requested-With": "tradlysis"})
        o = run.json()["report"]["metrics"]["trade_outcomes"]
    assert o["counts"]["manual"] == 1 and o["not_reaching_tp"]["reached"]["3R"] == 1
    [row] = o["trades"]
    assert row["outcome"] == "manual" and row["hours"] == 0.75 and row["excursion"]["given_back_r"] > 0.9
