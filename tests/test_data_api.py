"""Read-only data API (app/api/data.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.api.main import create_app
from app.config import store
from app.db.models import AppConfig, SystemEvent, Trade
from tests.conftest import make_settings

TOKEN = "t" * 32
BEARER = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def client(db):
    base = make_settings(database_url=db.engine.url.render_as_string(hide_password=False))
    await store.save_global(db, base, {"data_api_token": TOKEN, "openrouter_api_key": "sk-secret"}, "test")
    app = create_app(base, db=db)
    app.state.db = db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_token_is_required_and_separate_from_the_dashboard_login(client):
    assert (await client.get("/api/data/tables")).status_code == 401
    assert (await client.get("/api/data/tables", auth=("admin", "secret"))).status_code == 401
    assert (await client.get("/api/data/tables", headers={"Authorization": "Bearer nope"})).status_code == 401
    assert (await client.get("/api/data/tables", headers=BEARER)).status_code == 200


async def test_off_without_a_token(db):
    app = create_app(make_settings(database_url=db.engine.url.render_as_string(hide_password=False)), db=db)
    app.state.db = db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/api/data/tables", headers=BEARER)).status_code == 503


def test_short_tokens_are_refused():
    with pytest.raises(ValueError, match="DATA_API_TOKEN"):
        make_settings(data_api_token="short")


async def test_tables_rows_filters_and_secret_masking(client, db):
    t0 = datetime(2026, 10, 1, 10, tzinfo=UTC)
    async with db.session() as s:
        for i, exp in enumerate(["a", "b", "a"]):
            s.add(Trade(broker_trade_id=f"d{i}", experiment=exp, instrument="EUR_USD", direction="BUY",
                        initial_units=100, current_units=0, open_price=1.1, open_time=t0 + timedelta(hours=i),
                        state="CLOSED", raw={}))
        s.add(SystemEvent(level="INFO", component="x", event_type="E", message="m", details={}))

    tables = {t["table"]: t for t in (await client.get("/api/data/tables", headers=BEARER)).json()["tables"]}
    assert tables["trades"]["rows"] == 3 and tables["trades"]["time_column"] == "created_at"
    assert "experiment" in tables["trades"]["columns"]

    r = (await client.get("/api/data/tables/trades?f.experiment=a&order=open_time&columns=broker_trade_id,open_time",
                          headers=BEARER)).json()
    assert [x["broker_trade_id"] for x in r["rows"]] == ["d0", "d2"] and set(r["rows"][0]) == {"broker_trade_id", "open_time"}
    page = (await client.get("/api/data/tables/trades?limit=2&order=-open_time", headers=BEARER)).json()
    assert page["count"] == 2 and page["has_more"] and page["rows"][0]["broker_trade_id"] == "d2"
    assert (await client.get("/api/data/tables/trades?f.experiment=null", headers=BEARER)).json()["count"] == 0

    cfg = (await client.get("/api/data/tables/app_config?columns=value", headers=BEARER)).json()["rows"]
    values = {row["key"]: row["value"] for row in cfg}
    assert values["openrouter_api_key"] == store.SECRET_MASK and values["data_api_token"] == store.SECRET_MASK
    async with db.session() as s:
        assert (await s.scalar(select(AppConfig.value).where(AppConfig.key == "openrouter_api_key"))) == "sk-secret"


async def test_bad_requests(client):
    assert (await client.get("/api/data/tables/pg_user", headers=BEARER)).status_code == 404, "bot tables only"
    assert (await client.get("/api/data/tables/trades?columns=nope", headers=BEARER)).status_code == 400
    assert (await client.get("/api/data/tables/trades?f.nope=1", headers=BEARER)).status_code == 400
    assert (await client.get("/api/data/tables/trades?order=nope", headers=BEARER)).status_code == 400
    assert (await client.get("/api/data/tables/trades?limit=5000", headers=BEARER)).status_code == 422
    assert (await client.post("/api/data/tables/trades", headers=BEARER)).status_code == 405, "read-only"


async def test_diagnosis(client):
    r = await client.get("/api/data/diagnose?days=3", headers=BEARER)
    assert r.status_code == 200 and "Tradlysis diagnosis · last 3 day(s)" in r.json()["text"]
