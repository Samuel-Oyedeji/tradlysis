from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from app.api import history
from app.api.history import Bar, Chain, build_timeline, classify, hypothetical_outcome
from app.api.main import create_app
from app.db.models import (
    BrokerTransaction,
    Candle,
    Decision,
    DecisionRequest,
    Order,
    RiskCheck,
    Trade,
)
from tests.conftest import make_settings
from tests.helpers import T0

PLAN = {"direction": "BUY", "setup": "TREND_PULLBACK", "entry": 1.1026, "stop_loss": 1.1006, "take_profit": 1.1076,
        "risk_pips": 20.0, "reward_pips": 50.0, "risk_reward": 2.5, "stop_source": "PULLBACK_EXTREME",
        "target_source": "SWING_CLUSTER", "notes": []}
STRATEGY = {"setup": "TREND_PULLBACK", "direction": "BUY", "candidate": True, "failure_codes": [],
            "conditions": [{"name": "htf_trend", "passed": True, "detail": "H4 trend is bullish"},
                           {"name": "momentum", "passed": True, "detail": "bullish_candle=y"}]}
SNAPSHOT = {"price": {"bid": 1.1025, "ask": 1.1026, "spread_pips": 1.0},
            "timeframes": {"4h": {"trend": "bullish", "structure": "higher_highs_higher_lows", "rsi14": 60, "atr14_pips": 40}},
            "news": {"risk": "low", "blackout": False, "currency_bias": {"USD": {"bias": "NEUTRAL"}}},
            "market_regime": {"label": "STRONG_UPTREND"}, "volatility": {"regime": "NORMAL", "atr_h1_pips": 12}}


def bars(path: list[tuple[float, float]], start=T0 + timedelta(minutes=15)) -> list[Bar]:
    return [Bar(start + timedelta(minutes=15 * i), h, lo, (h + lo) / 2) for i, (h, lo) in enumerate(path)]


# ------------------------------------------------------------------ pure logic


def test_hypothetical_win_loss_ambiguous():
    start = T0 + timedelta(minutes=15)
    assert hypothetical_outcome(PLAN, start, bars([(1.1040, 1.1020), (1.1080, 1.1030)]))["result"] == "WOULD_WIN"
    lose = hypothetical_outcome(PLAN, start, bars([(1.1030, 1.1010), (1.1020, 1.1000)]))
    assert lose["result"] == "WOULD_LOSE" and lose["r"] == -1.0 and lose["bars"] == 2
    assert hypothetical_outcome(PLAN, start, bars([(1.1080, 1.1000)]))["result"] == "AMBIGUOUS"
    assert hypothetical_outcome(PLAN, start, bars([(1.1030, 1.1020)]))["result"] == "UNRESOLVED"
    # bars before the decision are ignored
    early = bars([(1.1080, 1.1030)], start=start - timedelta(hours=1))
    assert hypothetical_outcome(PLAN, start, early)["result"] == "UNRESOLVED"
    assert hypothetical_outcome(None, start, []) is None


def test_hypothetical_sell_side():
    plan = {**PLAN, "direction": "SELL", "stop_loss": 1.1046, "take_profit": 1.0976}
    assert hypothetical_outcome(plan, T0, bars([(1.1030, 1.0970)], start=T0))["result"] == "WOULD_WIN"
    assert hypothetical_outcome(plan, T0, bars([(1.1050, 1.1020)], start=T0))["result"] == "WOULD_LOSE"


def req(**kw) -> DecisionRequest:
    base = dict(id=1, experiment="x", instrument="EUR_USD", candle_time=T0, created_at=T0 + timedelta(minutes=15),
                snapshot=SNAPSHOT, trade_plan=PLAN, strategy_result=STRATEGY, llm_called=True,
                model="typesafe/jev-1.13", prompt_version="decision-v3")
    base.update(kw)
    return DecisionRequest(**base)


def dec(**kw) -> Decision:
    base = dict(id=1, request_id=1, source="LLM", decision="BUY", setup="TREND_PULLBACK", confidence=0.8,
                reason_codes=["HTF_BULLISH"], valid=True, created_at=T0 + timedelta(minutes=15, seconds=3),
                raw_response={"rationale": "clean pullback"}, latency_ms=900)
    base.update(kw)
    return Decision(**base)


def risk(approved: bool) -> RiskCheck:
    checks = [{"name": "SPREAD_LIMIT", "passed": True, "detail": "1.0 pips"},
              {"name": "NEWS_BLACKOUT", "passed": approved, "detail": "NFP"}]
    return RiskCheck(id=1, request_id=1, decision_id=1, approved=approved, checks=checks,
                     rejection_reasons=[] if approved else ["NEWS_BLACKOUT"], created_at=T0 + timedelta(minutes=15, seconds=4),
                     units=125000, risk_pct=0.25, risk_amount=Decimal("250"), entry_price=1.1026, stop_loss=1.1006,
                     take_profit=1.1076, risk_reward=2.5)


def test_classify_each_stage():
    assert classify(Chain(req(), dec(source="PREFILTER", decision="WAIT", reason_codes=["NEWS_BLACKOUT"]))).code == "STOPPED_BY_NEWS"
    assert classify(Chain(req(), dec(decision="WAIT", reason_codes=["MOMENTUM_WEAK"]))).code == "STOPPED_BY_MODEL"
    assert classify(Chain(req(), dec(source="ERROR", decision="WAIT"))).code == "MODEL_ERROR"
    oc = classify(Chain(req(), dec(), risk(False)))
    assert oc.code == "STOPPED_BY_RISK" and oc.reasons == ["NEWS_BLACKOUT"] and oc.stopped_at == "risk"
    order = Order(status="REJECTED", reject_reason="INSUFFICIENT_MARGIN")
    assert classify(Chain(req(), dec(), risk(True), order)).code == "ORDER_FAILED"
    won = Trade(state="CLOSED", r_multiple=2.4, realized_pl=Decimal("600"))
    assert classify(Chain(req(), dec(), risk(True), Order(status="FILLED"), won)).label == "Won +2.40R"
    assert classify(Chain(trade=Trade(state="OPEN"))).code == "OPEN"


def test_timeline_for_stopped_setup_marks_stage_and_hypothetical():
    hypo = hypothetical_outcome(PLAN, T0 + timedelta(minutes=15), bars([(1.1080, 1.1030)]))
    nodes = build_timeline(Chain(req(), dec(), risk(False)), hypo)
    keys = [n["key"] for n in nodes]
    assert keys == ["candle", "setup", "context", "decision", "risk", "hypothetical"]
    risk_node = nodes[4]
    assert risk_node["status"] == "fail" and risk_node.get("stopped_here")
    assert risk_node["children"][0]["status"] == "fail"  # failed checks listed first
    assert nodes[-1]["status"] == "win"
    setup = nodes[1]
    assert [c["status"] for c in setup["children"]] == ["pass", "pass"]
    assert any(d["label"] == "Take-profit" for d in setup["details"])
    assert "rationale" in str(nodes[3]["details"]) or any(d["value"] == "clean pullback" for d in nodes[3]["details"])


def test_timeline_for_closed_trade():
    order = Order(id=1, client_order_id="tlys-1", direction="BUY", units=125000, instrument="EUR_USD", status="FILLED",
                  requested_price=1.1026, fill_price=1.1027, attempts=1, submitted_at=T0 + timedelta(minutes=16),
                  request_payload={}, created_at=T0)
    trade = Trade(id=1, broker_trade_id="501", direction="BUY", initial_units=125000, current_units=0, open_price=1.1027,
                  open_time=T0 + timedelta(minutes=16), state="CLOSED", close_price=1.1076, close_reason="TAKE_PROFIT",
                  close_time=T0 + timedelta(hours=5), realized_pl=Decimal("612.5"), r_multiple=2.45, unexpected=False)
    tx = BrokerTransaction(transaction_id="900", type="ORDER_FILL", time=T0 + timedelta(hours=5), reason="TAKE_PROFIT_ORDER",
                           pl=Decimal("612.5"), raw={})
    nodes = build_timeline(Chain(req(), dec(), risk(True), order, trade, [tx]))
    keys = [n["key"] for n in nodes]
    assert keys[-4:] == ["order", "opened", "tx-900", "closed"]
    assert nodes[-1]["status"] == "win" and "Won +2.45R" in nodes[-1]["summary"]
    slip = next(d["value"] for d in nodes[keys.index("order")]["details"] if d["label"] == "Slippage")
    assert slip == "+1.0 pips"
    assert not any(n.get("stopped_here") for n in nodes)


# ------------------------------------------------------------------ API (database)


@pytest.fixture
async def client(db):
    app = create_app(make_settings(database_url=db.engine.url.render_as_string(hide_password=False)), db=db)
    app.state.db = db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", auth=("admin", "secret")) as c:
        yield c


async def seed(db):
    exp = make_settings().experiment_name
    async with db.session() as s:
        # 1) stopped by risk, price later reached TP -> would have won
        r1 = DecisionRequest(experiment=exp, instrument="EUR_USD", candle_time=T0, snapshot=SNAPSHOT, trade_plan=PLAN,
                             strategy_result=STRATEGY, llm_called=True)
        # 2) traded and closed at TP
        r2 = DecisionRequest(experiment=exp, instrument="EUR_USD", candle_time=T0 + timedelta(hours=6), snapshot=SNAPSHOT,
                             trade_plan=PLAN, strategy_result=STRATEGY, llm_called=True)
        # 3) no setup at all -> not in history
        r3 = DecisionRequest(experiment=exp, instrument="EUR_USD", candle_time=T0 + timedelta(hours=7), snapshot=SNAPSHOT,
                             strategy_result={"candidate": False, "failure_codes": ["NO_HTF_TREND"]}, llm_called=False)
        s.add_all([r1, r2, r3])
        await s.flush()
        d1 = Decision(request_id=r1.id, source="LLM", decision="BUY", confidence=0.8, reason_codes=["HTF_BULLISH"])
        d2 = Decision(request_id=r2.id, source="LLM", decision="BUY", confidence=0.9, reason_codes=["HTF_BULLISH"])
        d3 = Decision(request_id=r3.id, source="PREFILTER", decision="WAIT", reason_codes=["NO_HTF_TREND"])
        s.add_all([d1, d2, d3])
        await s.flush()
        rc1 = RiskCheck(request_id=r1.id, decision_id=d1.id, approved=False, checks=[{"name": "NEWS_BLACKOUT", "passed": False, "detail": "NFP"}],
                        rejection_reasons=["NEWS_BLACKOUT"])
        rc2 = RiskCheck(request_id=r2.id, decision_id=d2.id, approved=True, checks=[{"name": "SPREAD_LIMIT", "passed": True, "detail": ""}],
                        rejection_reasons=[], units=125000)
        s.add_all([rc1, rc2])
        await s.flush()
        o2 = Order(client_order_id="tlys-x", risk_check_id=rc2.id, instrument="EUR_USD", direction="BUY", units=125000,
                   status="FILLED", request_payload={}, requested_price=1.1026, fill_price=1.1026)
        s.add(o2)
        await s.flush()
        s.add(Trade(broker_trade_id="501", order_id=o2.id, experiment=exp, instrument="EUR_USD", direction="BUY",
                    initial_units=125000, current_units=0, open_price=1.1026, open_time=T0 + timedelta(hours=6, minutes=16),
                    stop_loss=1.1006, take_profit=1.1076, state="CLOSED", close_price=1.1076, close_reason="TAKE_PROFIT",
                    close_time=T0 + timedelta(hours=9), realized_pl=Decimal("625"), r_multiple=2.5))
        s.add(Trade(broker_trade_id="777", instrument="EUR_USD", direction="SELL", initial_units=-1000, current_units=-1000,
                    open_price=1.1, open_time=T0 + timedelta(hours=8), state="OPEN", unexpected=True))
        s.add(BrokerTransaction(transaction_id="900", account_id="a", type="ORDER_FILL", time=T0 + timedelta(hours=9),
                                trade_id="501", reason="TAKE_PROFIT_ORDER", pl=Decimal("625"), raw={}))
        for i in range(40):  # M15 candles; price climbs to the TP after the first decision
            t = T0 - timedelta(hours=2) + timedelta(minutes=15 * i)
            hi = 1.1030 + (0.0006 * max(0, i - 10))
            s.add(Candle(instrument="EUR_USD", granularity="M15", time=t, open=hi - 0.0005, high=hi, low=hi - 0.001,
                         close=hi - 0.0003, complete=True))
    return r1.id, r2.id


async def test_history_list_and_filters(db, client):
    r1, r2 = await seed(db)
    data = (await client.get("/api/history")).json()
    ids = [i["id"] for i in data["items"]]
    assert f"r-{r1}" in ids and f"r-{r2}" in ids and len(ids) == 3  # no-setup interval excluded; external trade included
    s = data["summary"]
    assert s["taken"] == 2 and s["won"] == 1 and s["stopped"] == 1 and s["stopped_would_win"] == 1
    stopped = next(i for i in data["items"] if i["id"] == f"r-{r1}")
    assert stopped["outcome"] == "STOPPED_BY_RISK" and stopped["hypothetical"]["result"] == "WOULD_WIN"
    ext = next(i for i in data["items"] if i["id"].startswith("t-"))
    assert ext["outcome"] == "EXTERNAL"
    traded = (await client.get("/api/history?filter=traded")).json()["items"]
    assert {i["id"] for i in traded} == {f"r-{r2}", ext["id"]}
    only_stopped = (await client.get("/api/history?filter=stopped")).json()["items"]
    assert [i["id"] for i in only_stopped] == [f"r-{r1}"]


async def test_history_item_timeline(db, client):
    r1, r2 = await seed(db)
    d = (await client.get(f"/api/history/r-{r2}")).json()
    keys = [n["key"] for n in d["timeline"]]
    assert keys[:5] == ["candle", "setup", "context", "decision", "risk"]
    assert keys[-3:] == ["opened", "tx-900", "closed"]
    assert d["levels"]["take_profit"] == 1.1076 and d["markers"]["close"]
    assert d["price_path"], "candles around the trade"
    d1 = (await client.get(f"/api/history/r-{r1}")).json()
    assert d1["timeline"][-1]["key"] == "hypothetical"
    assert any(n.get("stopped_here") for n in d1["timeline"])
    assert (await client.get("/api/history/r-999999")).status_code == 404
    assert (await client.get("/api/history/zzz")).status_code == 404


async def test_history_page_served(client):
    for path in ("/history", "/history/r-1"):
        r = await client.get(path)
        assert r.status_code == 200 and "<title>Trade History</title>" in r.text
    assert (await client.get("/api/history", auth=("admin", "nope"))).status_code == 401


def test_item_summary_direction_for_orphan_trade():
    s = history.item_summary(Chain(trade=Trade(id=5, state="OPEN", direction="SELL", open_time=T0, unexpected=True, open_price=1.1)), None)
    assert s["id"] == "t-5" and s["direction"] == "SELL" and s["outcome"] == "EXTERNAL"
