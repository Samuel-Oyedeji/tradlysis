from __future__ import annotations

import httpx
import pytest

from app.api.main import create_app
from app.db.control import get_control
from app.db.enums import ControlKey
from tests.conftest import make_settings

AUTH = ("admin", "secret")
CSRF = {"X-Requested-With": "tradlysis"}


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
        assert (await get_control(s, ControlKey.DAILY_LOSS_BREAKER))["tripped"] is False
        assert (await get_control(s, ControlKey.DAY_START_NAV))["trading_day"] is None
        assert (await get_control(s, ControlKey.PEAK_NAV))["value"] == "0"
    assert (await client.post("/api/controls/reset-breaker/bogus", auth=AUTH, headers=CSRF)).status_code == 404


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
