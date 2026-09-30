import pytest
from pydantic import ValidationError

from app.config.settings import LIVE_CONFIRM_PHRASE, to_asyncpg_url
from tests.conftest import make_settings


def test_defaults_are_demo(settings):
    assert settings.is_demo
    assert settings.oanda_rest_url == "https://api-fxpractice.oanda.com"
    assert settings.oanda_stream_url == "https://stream-fxpractice.oanda.com"
    assert settings.openrouter_model == "typesafe/jev-1.13"


def test_live_requires_confirmation():
    with pytest.raises(ValidationError):
        make_settings(trading_mode="live")
    s = make_settings(trading_mode="live", live_trading_confirm=LIVE_CONFIRM_PHRASE, oanda_live_api_token="x")
    assert s.oanda_rest_url == "https://api-fxtrade.oanda.com"
    assert s.oanda_api_token == "x"  # live mode never uses the practice token


@pytest.mark.parametrize(
    "field,value",
    [
        ("risk_per_trade_pct", 1.5),
        ("risk_per_trade_pct", 0),
        ("max_daily_loss_pct", 10),
        ("max_drawdown_pct", 50),
        ("min_risk_reward", 0.5),
    ],
)
def test_risk_ceilings(field, value):
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_total_risk_must_cover_single_trade():
    with pytest.raises(ValidationError):
        make_settings(risk_per_trade_pct=0.5, max_total_risk_pct=0.25)


def test_asyncpg_url_conversion():
    assert (
        to_asyncpg_url("postgresql://u:p@h:5432/db?schema=public&sslmode=require")
        == "postgresql+asyncpg://u:p@h:5432/db?ssl=require"
    )
    assert to_asyncpg_url("postgres://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"


def test_missing_credentials_detected():
    s = make_settings(oanda_practice_api_token="")
    with pytest.raises(RuntimeError, match="OANDA_PRACTICE_API_TOKEN"):
        s.require_broker_credentials()
