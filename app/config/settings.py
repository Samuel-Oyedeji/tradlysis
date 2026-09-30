"""Application configuration.

All settings come from environment variables (or a local ``.env`` file). Credentials are
never hard-coded; see ``.env.example`` for the full list.

Safety defaults:
  * ``TRADING_MODE`` defaults to ``demo`` and always talks to Capital.com's demo API host.
  * ``live`` mode additionally requires ``LIVE_TRADING_CONFIRM`` to equal
    :data:`LIVE_CONFIRM_PHRASE` and separate live credentials.
  * Risk parameters have hard ceilings that configuration cannot exceed.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_THIS_USES_REAL_MONEY"

# Hard ceilings. Configuration may be stricter, never looser.
HARD_MAX_RISK_PER_TRADE_PCT = 1.0
HARD_MAX_TOTAL_RISK_PCT = 2.0
HARD_MAX_DAILY_LOSS_PCT = 5.0
HARD_MAX_DRAWDOWN_PCT = 20.0

CAPITAL_HOSTS = {
    "demo": "https://demo-api-capital.backend-capital.com",
    "live": "https://api-capital.backend-capital.com",
}


class TradingMode(StrEnum):
    DEMO = "demo"
    LIVE = "live"


class LlmCallPolicy(StrEnum):
    # Only call the LLM when the deterministic strategy finds a candidate setup.
    CANDIDATES_ONLY = "candidates_only"
    # Call the LLM on every decision interval (more data, more cost).
    ALWAYS = "always"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Mode / safety -------------------------------------------------------------
    trading_mode: TradingMode = TradingMode.DEMO
    live_trading_confirm: str = ""
    # Global kill switch at boot. The runtime kill switch lives in the database
    # (control_state) and can be toggled from the dashboard.
    trading_enabled: bool = True

    # --- Broker: Capital.com ------------------------------------------------------------
    # API key, the login e-mail and the custom password set when the API key was generated.
    capital_demo_api_key: str = ""
    capital_demo_identifier: str = ""
    capital_demo_api_password: str = ""
    capital_demo_account_id: str = ""  # optional: defaults to the login's active account
    capital_live_api_key: str = ""
    capital_live_identifier: str = ""
    capital_live_api_password: str = ""
    capital_live_account_id: str = ""
    # Capital.com market identifier; defaults to INSTRUMENT without the underscore (EURUSD).
    capital_epic: str = ""
    # Optional override; by default the streaming host returned at login is used.
    capital_stream_url: str = ""
    capital_request_timeout_seconds: float = 15.0

    # --- Database --------------------------------------------------------------------
    database_url: str = ""

    # --- Experiment ------------------------------------------------------------------
    experiment_name: str = "v1-trend-pullback-eur-usd"
    instrument: str = "EUR_USD"

    # --- Decision layer (OpenRouter) ---------------------------------------------------
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "typesafe/jev-1.13"
    openrouter_news_model: str = ""  # defaults to openrouter_model
    openrouter_app_url: str = "https://github.com/samuel-oyedeji/tradlysis"
    openrouter_app_name: str = "Tradlysis"
    llm_timeout_seconds: float = 60.0
    llm_max_tokens: int = 600
    llm_temperature: float = 0.0
    llm_call_policy: LlmCallPolicy = LlmCallPolicy.CANDIDATES_ONLY

    # --- Scheduling ------------------------------------------------------------------
    decision_granularity: str = "M15"
    decision_candle_delay_seconds: int = 10
    reconcile_interval_seconds: int = 15
    account_snapshot_interval_seconds: int = 60
    price_persist_interval_seconds: float = 5.0
    news_calendar_poll_minutes: int = 30
    news_rss_poll_minutes: int = 15
    candle_history_count: int = 500

    # --- Risk (experiment parameters, deterministic) -----------------------------------
    risk_per_trade_pct: float = 0.25
    max_total_risk_pct: float = 0.5
    max_daily_loss_pct: float = 1.0
    max_drawdown_pct: float = 5.0
    max_open_trades: int = 1
    min_risk_reward: float = 2.0
    max_spread_pips: float = 1.5
    min_stop_pips: float = 5.0
    max_stop_pips: float = 40.0
    max_slippage_pips: float = 1.0
    min_decision_confidence: float = 0.6
    signal_cooldown_minutes: int = 60
    stale_price_seconds: float = 15.0
    max_account_state_age_seconds: float = 120.0
    max_margin_usage_pct: float = 50.0
    news_blackout_before_minutes: int = 30
    news_blackout_after_minutes: int = 30
    news_blocking_impacts: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["HIGH"])

    # --- Strategy (trend pullback) -------------------------------------------------------
    strategy_pullback_lookback_bars: int = 8
    strategy_level_tolerance_atr: float = 0.5
    strategy_stop_buffer_atr: float = 0.25
    strategy_fallback_target_r: float = 2.0
    strategy_max_target_r: float = 4.0

    # --- News ------------------------------------------------------------------------
    news_calendar_url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    # Comma-separated "CURRENCY|url" pairs of RSS feeds with central-bank communication.
    news_rss_feeds: str = (
        "USD|https://www.federalreserve.gov/feeds/press_monetary.xml,"
        "EUR|https://www.ecb.europa.eu/rss/press.html"
    )
    news_interpretation_enabled: bool = True
    # Block new trades when the calendar could not be refreshed recently (fail closed).
    news_require_fresh_calendar: bool = True
    news_bias_lookback_hours: int = 72

    # --- Alerts (Telegram) -------------------------------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    alert_dedup_seconds: int = 300

    # --- API / dashboard -----------------------------------------------------------------
    dashboard_username: str = "admin"
    dashboard_password: str = ""
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # --- Analyzer --------------------------------------------------------------------
    analyzer_interval_minutes: int = 60

    log_level: str = "INFO"

    # ------------------------------------------------------------------------------------

    @field_validator("news_blocking_impacts", mode="before")
    @classmethod
    def _split_impacts(cls, v: object) -> object:
        if isinstance(v, str):
            return [p.strip().upper() for p in v.split(",") if p.strip()]
        return v

    @model_validator(mode="after")
    def _validate_safety(self) -> Settings:
        if self.risk_per_trade_pct <= 0 or self.risk_per_trade_pct > HARD_MAX_RISK_PER_TRADE_PCT:
            raise ValueError(
                f"RISK_PER_TRADE_PCT must be in (0, {HARD_MAX_RISK_PER_TRADE_PCT}]"
            )
        if self.max_total_risk_pct <= 0 or self.max_total_risk_pct > HARD_MAX_TOTAL_RISK_PCT:
            raise ValueError(f"MAX_TOTAL_RISK_PCT must be in (0, {HARD_MAX_TOTAL_RISK_PCT}]")
        if self.max_total_risk_pct < self.risk_per_trade_pct:
            raise ValueError("MAX_TOTAL_RISK_PCT must be >= RISK_PER_TRADE_PCT")
        if self.max_daily_loss_pct <= 0 or self.max_daily_loss_pct > HARD_MAX_DAILY_LOSS_PCT:
            raise ValueError(f"MAX_DAILY_LOSS_PCT must be in (0, {HARD_MAX_DAILY_LOSS_PCT}]")
        if self.max_drawdown_pct <= 0 or self.max_drawdown_pct > HARD_MAX_DRAWDOWN_PCT:
            raise ValueError(f"MAX_DRAWDOWN_PCT must be in (0, {HARD_MAX_DRAWDOWN_PCT}]")
        if self.min_risk_reward < 1.0:
            raise ValueError("MIN_RISK_REWARD must be >= 1.0")
        if self.max_open_trades < 1:
            raise ValueError("MAX_OPEN_TRADES must be >= 1")
        if not 0.0 <= self.min_decision_confidence <= 1.0:
            raise ValueError("MIN_DECISION_CONFIDENCE must be between 0 and 1")
        if self.min_stop_pips <= 0 or self.max_stop_pips <= self.min_stop_pips:
            raise ValueError("Require 0 < MIN_STOP_PIPS < MAX_STOP_PIPS")
        if self.trading_mode == TradingMode.LIVE and self.live_trading_confirm != LIVE_CONFIRM_PHRASE:
            raise ValueError(
                "TRADING_MODE=live requires LIVE_TRADING_CONFIRM="
                f"{LIVE_CONFIRM_PHRASE}. V1 is designed for the Capital.com demo account."
            )
        return self

    # --- Derived values ----------------------------------------------------------------

    @property
    def is_demo(self) -> bool:
        return self.trading_mode == TradingMode.DEMO

    @property
    def capital_base_url(self) -> str:
        return CAPITAL_HOSTS[self.trading_mode.value]

    @property
    def capital_api_key(self) -> str:
        return self.capital_demo_api_key if self.is_demo else self.capital_live_api_key

    @property
    def capital_identifier(self) -> str:
        return self.capital_demo_identifier if self.is_demo else self.capital_live_identifier

    @property
    def capital_api_password(self) -> str:
        return self.capital_demo_api_password if self.is_demo else self.capital_live_api_password

    @property
    def capital_account_id(self) -> str:
        return self.capital_demo_account_id if self.is_demo else self.capital_live_account_id

    @property
    def broker_epic(self) -> str:
        return self.capital_epic or self.instrument.replace("_", "")

    @property
    def news_model(self) -> str:
        return self.openrouter_news_model or self.openrouter_model

    @property
    def rss_feeds(self) -> list[tuple[str, str]]:
        feeds: list[tuple[str, str]] = []
        for entry in self.news_rss_feeds.split(","):
            entry = entry.strip()
            if not entry:
                continue
            currency, _, url = entry.partition("|")
            if url:
                feeds.append((currency.strip().upper(), url.strip()))
        return feeds

    @property
    def instrument_currencies(self) -> tuple[str, str]:
        base, _, quote = self.instrument.partition("_")
        return base, quote

    @property
    def sqlalchemy_database_url(self) -> str:
        return to_asyncpg_url(self.database_url)

    def require_broker_credentials(self) -> None:
        prefix = "CAPITAL_DEMO_" if self.is_demo else "CAPITAL_LIVE_"
        missing = [
            prefix + name
            for name, value in (
                ("API_KEY", self.capital_api_key),
                ("IDENTIFIER", self.capital_identifier),
                ("API_PASSWORD", self.capital_api_password),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing broker configuration: {', '.join(missing)}")

    def require_database(self) -> None:
        if not self.database_url:
            raise RuntimeError("Missing DATABASE_URL")


# asyncpg rejects libpq/Prisma-only query parameters such as ?schema=public.
_ASYNCPG_ALLOWED_QUERY = {"ssl", "sslmode", "application_name", "timeout", "command_timeout"}


def to_asyncpg_url(url: str) -> str:
    """Convert a Prisma/libpq style ``postgresql://`` URL into a SQLAlchemy asyncpg URL."""
    if not url:
        return url
    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+asyncpg"
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key not in _ASYNCPG_ALLOWED_QUERY:
            continue
        if key == "sslmode":
            # asyncpg uses ``ssl`` rather than libpq's ``sslmode``.
            key = "ssl"
        query.append((key, value))
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
