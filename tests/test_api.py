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
    assert (await client.post("/api/controls/reset-breaker/bogus", auth=AUTH, headers=CSRF)).status_code == 404


async def test_shared_assets_served(client):
    css = await client.get("/assets/app.css")
    js = await client.get("/assets/app.js")
    assert css.status_code == 200 and "--nav-bg" in css.text
    assert js.status_code == 200 and "renderShell" in js.text
    page = (await client.get("/", auth=AUTH)).text
    assert '/assets/app.css' in page
