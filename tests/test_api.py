from __future__ import annotations

import httpx
import pytest

from app.api.main import create_app
from app.db.control import get_control, scoped
from app.db.enums import ControlKey
from tests.conftest import make_settings

AUTH = ("admin", "secret")
CSRF = {"X-Requested-With": "tradlysis"}
EXP = make_settings().experiment_name


@pytest.fixture
async def client(db):
    app = create_app(make_settings(database_url=db.engine.url.render_as_string(hide_password=False)), db=db)
    app.state.db = db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_auth_required(client):
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/api/status")).status_code == 401
    assert (await client.get("/api/status", auth=("admin", "wrong"))).status_code == 401
    assert (await client.get("/api/status", auth=AUTH)).status_code == 200


async def test_locked_without_password(db):
    app = create_app(make_settings(dashboard_password=""), db=db)
    app.state.db = db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/api/status", auth=AUTH)).status_code == 503


async def test_dashboard_served(client):
    r = await client.get("/", auth=AUTH)
    assert r.status_code == 200 and "<title>Tradlysis</title>" in r.text


async def test_read_endpoints_empty_db(client):
    for path in ("/api/status", "/api/account", "/api/trades", "/api/decisions", "/api/events", "/api/news",
                 "/api/analysis/latest"):
        r = await client.get(path, auth=AUTH)
        assert r.status_code == 200, (path, r.text)
    s = (await client.get("/api/status", auth=AUTH)).json()
    assert s["mode"] == "demo" and s["engine_alive"] is False


async def test_kill_switch_requires_csrf_header(client, db):
    r = await client.post("/api/controls/kill-switch", json={"active": True}, auth=AUTH)
    assert r.status_code == 403
    r = await client.post("/api/controls/kill-switch", json={"active": True, "reason": "test"}, auth=AUTH, headers=CSRF)
    assert r.status_code == 200
    async with db.session() as s:
        assert (await get_control(s, ControlKey.KILL_SWITCH))["active"] is True
    status = (await client.get("/api/status", auth=AUTH)).json()
    assert status["controls"]["kill_switch"]["active"] is True


async def test_flatten_and_breaker_reset(client, db):
    assert (await client.post("/api/controls/flatten", json={}, auth=AUTH, headers=CSRF)).status_code == 200
    async with db.session() as s:
        assert (await get_control(s, ControlKey.FLATTEN_REQUEST))["requested"] is True
    assert (await client.post("/api/controls/reset-breaker/drawdown", auth=AUTH, headers=CSRF)).status_code == 200
    assert (await client.post("/api/controls/reset-breaker/daily", auth=AUTH, headers=CSRF)).status_code == 200
    async with db.session() as s:
        assert (await get_control(s, scoped(ControlKey.DAILY_LOSS_BREAKER, EXP)))["tripped"] is False
        assert (await get_control(s, scoped(ControlKey.DAY_START_NAV, EXP)))["trading_day"] is None
        assert (await get_control(s, scoped(ControlKey.PEAK_NAV, EXP)))["value"] == "0"
    assert (await client.post("/api/controls/reset-breaker/bogus", auth=AUTH, headers=CSRF)).status_code == 404
    r = await client.post("/api/controls/reset-breaker/daily?experiment=nope", auth=AUTH, headers=CSRF)
    assert r.status_code == 404


async def test_shared_assets_served(client):
    css = await client.get("/assets/app.css")
    js = await client.get("/assets/app.js")
    assert css.status_code == 200 and "--nav-bg" in css.text
    assert js.status_code == 200 and "renderShell" in js.text
    page = (await client.get("/", auth=AUTH)).text
    assert '/assets/app.css' in page


async def test_analysis_page_run_and_timeline(client, db):
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from app.db.models import Decision, DecisionRequest, RiskCheck, Trade

    exp = make_settings().experiment_name
    today = datetime.now(UTC).replace(hour=10, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    async with db.session() as s:
        for i, t in enumerate([yesterday, yesterday + timedelta(minutes=15), today]):
            candidate = i == 2
            r = DecisionRequest(experiment=exp, instrument="EUR_USD", candle_time=t, snapshot={},
                                strategy_result={"candidate": candidate}, llm_called=candidate)
            s.add(r)
            await s.flush()
            d = Decision(request_id=r.id, source="LLM" if candidate else "PREFILTER",
                         decision="BUY" if candidate else "WAIT", reason_codes=["NO_SETUP"], confidence=0.8)
            s.add(d)
            await s.flush()
            if candidate:
                s.add(RiskCheck(request_id=r.id, decision_id=d.id, approved=True, checks=[], rejection_reasons=[]))
        s.add(Trade(broker_trade_id="d1", experiment=exp, instrument="EUR_USD", direction="BUY", initial_units=1000,
                    current_units=0, open_price=1.1, open_time=today, state="CLOSED", close_time=today + timedelta(hours=1),
                    close_price=1.104, realized_pl=Decimal("4"), r_multiple=2.0, initial_risk_price=0.002))

    page = await client.get("/analysis", auth=AUTH)
    assert page.status_code == 200 and "Experiment analysis" in page.text
    assert (await client.get("/analysis")).status_code == 401

    tl = (await client.get("/api/analysis/timeline?days=7", auth=AUTH)).json()["days"]
    assert [d["day"] for d in tl] == [today.date().isoformat(), yesterday.date().isoformat()]
    t0, t1 = tl
    assert (t0["cycles"], t0["setups"], t0["model_calls"], t0["signals"], t0["approved"]) == (1, 1, 1, 1, 1)
    assert (t0["opened"], t0["closed"], t0["wins"], t0["total_r"], t0["realized_pl"]) == (1, 1, 1, 2.0, 4.0)
    assert (t1["cycles"], t1["setups"], t1["signals"], t1["closed"]) == (2, 0, 0, 0)

    assert (await client.post("/api/analysis/run", auth=AUTH)).status_code == 403  # CSRF header required
    run = await client.post("/api/analysis/run", auth=AUTH, headers=CSRF)
    assert run.status_code == 200
    m = run.json()["report"]["metrics"]
    assert m["opportunities"] == 3 and m["setup_candidates"] == 1 and m["closed_trades"]["trades"] == 1
    latest = (await client.get("/api/analysis/latest", auth=AUTH)).json()["report"]
    assert latest["metrics"]["opportunities"] == 3  # the run was stored, like the analyzer does


async def two_experiments(db):
    from app.config import store
    from app.config.store import ExperimentInput

    base = make_settings(database_url=db.engine.url.render_as_string(hide_password=False))
    for slug, inst, enabled in (("eur-a", "EUR_USD", True), ("gbp-b", "GBP_USD", False)):
        await store.save_experiment(db, base, slug, ExperimentInput(name=slug.upper(), instrument=inst, enabled=enabled,
                                                                    capital="1000"), "t", create=True)


async def test_pages_follow_the_selected_experiment(client, db):
    from datetime import UTC, datetime
    from decimal import Decimal

    from app.db.models import DecisionRequest, SystemEvent, Trade

    await two_experiments(db)
    t = datetime(2026, 9, 29, 10, tzinfo=UTC)
    async with db.session() as s:
        s.add(DecisionRequest(experiment="gbp-b", instrument="GBP_USD", candle_time=t, snapshot={}, strategy_result={}))
        s.add(Trade(broker_trade_id="x1", experiment="gbp-b", instrument="GBP_USD", direction="BUY", initial_units=1,
                    current_units=0, open_price=1.3, open_time=t, state="CLOSED", close_time=t, realized_pl=Decimal("25"),
                    r_multiple=1.0))
        s.add(SystemEvent(level="INFO", component="x", event_type="A", message="gbp", details={"experiment": "gbp-b"}))
        s.add(SystemEvent(level="INFO", component="x", event_type="B", message="all", details={}))
    status = (await client.get("/api/status", auth=AUTH)).json()
    assert status["experiment"] == "eur-a", "default: the first enabled experiment"
    assert [e["slug"] for e in status["experiments"]] == ["eur-a", "gbp-b"]
    assert (await client.get("/api/decisions", auth=AUTH)).json() == []
    assert len((await client.get("/api/decisions?experiment=gbp-b", auth=AUTH)).json()) == 1
    assert [e["message"] for e in (await client.get("/api/events", auth=AUTH)).json()] == ["all"]
    assert {e["message"] for e in (await client.get("/api/events?experiment=gbp-b", auth=AUTH)).json()} == {"gbp", "all"}
    acct = (await client.get("/api/account?experiment=gbp-b&hours=8760", auth=AUTH)).json()
    assert acct["experiment"]["balance"] == 1025.0 and acct["experiment"]["series"][-1]["balance"] == 1025.0
    assert (await client.get("/api/status?experiment=nope", auth=AUTH)).status_code == 404
    overview = {e["slug"]: e for e in (await client.get("/api/experiments", auth=AUTH)).json()["experiments"]}
    assert overview["gbp-b"]["closed_trades"] == 1 and overview["gbp-b"]["realized_pl"] == 25.0
    assert overview["eur-a"]["closed_trades"] == 0 and overview["eur-a"]["enabled"]
    assert (await client.get("/experiments", auth=AUTH)).status_code == 200

    r = await client.post("/api/controls/kill-switch", json={"active": True, "experiment": "gbp-b"}, auth=AUTH, headers=CSRF)
    assert r.status_code == 200
    assert (await client.get("/api/status?experiment=gbp-b", auth=AUTH)).json()["controls"]["kill_switch"]["active"]
    assert not (await client.get("/api/status", auth=AUTH)).json()["controls"]["kill_switch"].get("active")
    await client.post("/api/controls/flatten", json={"experiment": "gbp-b"}, auth=AUTH, headers=CSRF)
    async with db.session() as s:
        assert (await get_control(s, scoped(ControlKey.FLATTEN_REQUEST, "gbp-b")))["requested"] is True
        assert await get_control(s, ControlKey.FLATTEN_REQUEST) is None


async def test_config_page_needs_its_own_password(db):
    app = create_app(make_settings(database_url=db.engine.url.render_as_string(hide_password=False),
                                   config_password="cfg-pass"), db=db)
    app.state.db = db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/config")).status_code == 401
        assert (await c.get("/config", auth=AUTH)).status_code == 200
        assert (await c.get("/api/config", auth=AUTH)).status_code == 401, "dashboard login alone is not enough"
        bad = await c.post("/api/config/unlock", json={"password": "nope"}, auth=AUTH, headers=CSRF)
        assert bad.status_code == 401
        token = (await c.post("/api/config/unlock", json={"password": "cfg-pass"}, auth=AUTH, headers=CSRF)).json()["token"]
        h = {**CSRF, "X-Config-Token": token}
        cfg = (await c.get("/api/config", auth=AUTH, headers=h)).json()
        fields = {f["key"]: f for f in cfg["global"]}
        assert fields["openrouter_api_key"]["secret"] and fields["openrouter_api_key"]["value"] is None
        assert fields["openrouter_api_key"]["is_set"], "set in the test settings, but never sent back"
        assert "DATABASE_URL" in cfg["env_only"] and "CONFIG_PASSWORD" in cfg["env_only"]

        r = await c.put("/api/config", json={"values": {"telegram_chat_id": "5"}}, auth=AUTH, headers=CSRF)
        assert r.status_code == 401, "saving needs the unlock token too"
        r = await c.put("/api/config", json={"values": {"llm_timeout_seconds": "x"}}, auth=AUTH, headers=h)
        assert r.status_code == 400 and "LLM_TIMEOUT_SECONDS" in r.json()["detail"]
        r = await c.put("/api/config", json={"values": {"telegram_chat_id": "5"}}, auth=AUTH, headers=h)
        assert r.json()["changed"] == ["telegram_chat_id"]

        r = await c.post("/api/config/experiments", json={"slug": "gbp", "name": "GBP", "instrument": "GBP_USD",
                                                          "settings": {"risk_per_trade_pct": "3"}}, auth=AUTH, headers=h)
        assert r.status_code == 400 and "RISK_PER_TRADE_PCT" in r.json()["detail"]
        r = await c.post("/api/config/experiments", json={"slug": "gbp", "name": "GBP", "instrument": "GBP_USD",
                                                          "settings": {"risk_per_trade_pct": "0.3"}}, auth=AUTH, headers=h)
        assert r.status_code == 200
        r = await c.put("/api/config/experiments/gbp", json={"enabled": True}, auth=AUTH, headers=h)
        assert r.json()["changed"] == ["enabled"]
        r = await c.post("/api/config/experiments", json={"slug": "gbp-breakout", "name": "GBP breakout",
                                                          "instrument": "GBP_USD", "strategy": "range_breakout",
                                                          "settings": {"breakout_range_bars": "24"}},
                         auth=AUTH, headers=h)
        assert r.status_code == 200, r.text
        cfg = (await c.get("/api/config", auth=AUTH, headers=h)).json()
        [gbp] = [e for e in cfg["experiments"] if e["slug"] == "gbp"]
        risk = next(f for f in gbp["fields"] if f["key"] == "risk_per_trade_pct")
        assert gbp["enabled"] and risk["value"] == "0.3" and risk["source"] == "app"
        keys = {e["slug"]: {f["key"] for f in e["fields"]} for e in cfg["experiments"]}
        assert "breakout_range_bars" in keys["gbp-breakout"] and "strategy_pullback_lookback_bars" not in keys["gbp-breakout"]
        assert "strategy_pullback_lookback_bars" in keys["gbp"] and "breakout_range_bars" not in keys["gbp"]
        assert cfg["strategies"]["range_breakout"] == "Range breakout"
        assert cfg["version"] == 4 and cfg["changes"][0]["scope"] == "gbp-breakout"

        await c.post("/api/config/lock", auth=AUTH, headers=h)
        assert (await c.get("/api/config", auth=AUTH, headers=h)).status_code == 401


async def test_config_page_locked_without_config_password(client):
    r = await client.post("/api/config/unlock", json={"password": ""}, auth=AUTH, headers=CSRF)
    assert r.status_code == 503
