import pytest
from pydantic import ValidationError

from app.config.settings import LIVE_CONFIRM_PHRASE, to_asyncpg_url
from tests.conftest import make_settings


def test_defaults_are_demo(settings):
    assert settings.is_demo
    assert settings.capital_base_url == "https://demo-api-capital.backend-capital.com"
    assert settings.capital_api_key == "test-api-key"
    assert settings.broker_epic == "EURUSD"
    assert settings.openrouter_model == "typesafe/jev-1.13"


def test_live_requires_confirmation():
    with pytest.raises(ValidationError):
        make_settings(trading_mode="live")
    s = make_settings(trading_mode="live", live_trading_confirm=LIVE_CONFIRM_PHRASE, capital_live_api_key="x")
    assert s.capital_base_url == "https://api-capital.backend-capital.com"
    assert s.capital_api_key == "x"  # live mode never uses the demo credentials
    assert s.capital_identifier == ""
    with pytest.raises(RuntimeError, match="CAPITAL_LIVE_IDENTIFIER"):
        s.require_broker_credentials()


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
    s = make_settings(capital_demo_api_key="")
    with pytest.raises(RuntimeError, match="CAPITAL_DEMO_API_KEY"):
        s.require_broker_credentials()
