"""SQLAlchemy models mirroring ``prisma/schema.prisma``.

Prisma owns the schema and migrations; these models only describe the existing tables so
the Python engine can read and write them. Never call ``metadata.create_all`` in
production. ``tests/test_schema_sync.py`` checks these models against the Prisma schema.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

TZ = DateTime(timezone=True)
MONEY = Numeric(20, 6)


def _now_col(**kw: Any) -> Mapped[datetime]:
    return mapped_column(TZ, nullable=False, server_default=func.now(), **kw)


def _json_col(name: str | None = None) -> Mapped[dict]:
    args = (name,) if name else ()
    return mapped_column(*args, JSONB, nullable=False, default=dict, server_default=text("'{}'"))


class Base(DeclarativeBase):
    pass


# --- Market data ----------------------------------------------------------------------


class MarketPrice(Base):
    __tablename__ = "market_prices"
    __table_args__ = (Index("market_prices_instrument_time_idx", "instrument", "time"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument: Mapped[str] = mapped_column(Text, nullable=False)
    time: Mapped[datetime] = mapped_column(TZ, nullable=False)
    bid: Mapped[float] = mapped_column(Float, nullable=False)
    ask: Mapped[float] = mapped_column(Float, nullable=False)
    mid: Mapped[float] = mapped_column(Float, nullable=False)
    spread_pips: Mapped[float] = mapped_column(Float, nullable=False)
    tradeable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = _now_col()


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint(
            "instrument", "granularity", "time", name="candles_instrument_granularity_time_key"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument: Mapped[str] = mapped_column(Text, nullable=False)
    granularity: Mapped[str] = mapped_column(Text, nullable=False)
    time: Mapped[datetime] = mapped_column(TZ, nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    bid_close: Mapped[float | None] = mapped_column(Float)
    ask_close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'oanda'"))
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col(onupdate=func.now())


class TechnicalSnapshot(Base):
    __tablename__ = "technical_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "instrument",
            "timeframe",
            "candle_time",
            name="technical_snapshots_instrument_timeframe_candle_time_key",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument: Mapped[str] = mapped_column(Text, nullable=False)
    timeframe: Mapped[str] = mapped_column(Text, nullable=False)
    candle_time: Mapped[datetime] = mapped_column(TZ, nullable=False)
    computed_at: Mapped[datetime] = _now_col()
    close: Mapped[float] = mapped_column(Float, nullable=False)
    ema20: Mapped[float | None] = mapped_column(Float)
    ema50: Mapped[float | None] = mapped_column(Float)
    ema200: Mapped[float | None] = mapped_column(Float)
    rsi14: Mapped[float | None] = mapped_column(Float)
    atr14: Mapped[float | None] = mapped_column(Float)
    trend: Mapped[str] = mapped_column(Text, nullable=False)
    structure: Mapped[str] = mapped_column(Text, nullable=False)
    last_swing_high: Mapped[float | None] = mapped_column(Float)
    last_swing_low: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict] = _json_col()


class SupportResistance(Base):
    __tablename__ = "support_resistance"
    __table_args__ = (
        Index("support_resistance_instrument_computed_at_idx", "instrument", "computed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument: Mapped[str] = mapped_column(Text, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    timeframe: Mapped[str | None] = mapped_column(Text)
    price_low: Mapped[float] = mapped_column(Float, nullable=False)
    price_high: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    touches: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    strength: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = _now_col()


class MarketRegime(Base):
    __tablename__ = "market_regimes"
    __table_args__ = (
        Index(
            "market_regimes_instrument_timeframe_computed_at_idx",
            "instrument",
            "timeframe",
            "computed_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument: Mapped[str] = mapped_column(Text, nullable=False)
    timeframe: Mapped[str] = mapped_column(Text, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    trend_regime: Mapped[str] = mapped_column(Text, nullable=False)
    volatility_regime: Mapped[str] = mapped_column(Text, nullable=False)
    atr: Mapped[float | None] = mapped_column(Float)
    atr_percentile: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict] = _json_col()


# --- News -------------------------------------------------------------------------------


class NewsEvent(Base):
    __tablename__ = "news_events"
    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="news_events_provider_external_id_key"),
        Index("news_events_event_time_idx", "event_time"),
        Index("news_events_currency_event_time_idx", "currency", "event_time"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    impact: Mapped[str] = mapped_column(Text, nullable=False)
    event_time: Mapped[datetime] = mapped_column(TZ, nullable=False)
    forecast: Mapped[str | None] = mapped_column(Text)
    previous: Mapped[str | None] = mapped_column(Text)
    actual: Mapped[str | None] = mapped_column(Text)
    forecast_value: Mapped[float | None] = mapped_column(Float)
    previous_value: Mapped[float | None] = mapped_column(Float)
    actual_value: Mapped[float | None] = mapped_column(Float)
    surprise: Mapped[float | None] = mapped_column(Float)
    surprise_direction: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict] = _json_col()
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col(onupdate=func.now())


class NewsArticle(Base):
    __tablename__ = "news_articles"
    __table_args__ = (
        UniqueConstraint(
            "provider", "external_id", name="news_articles_provider_external_id_key"
        ),
        Index("news_articles_published_at_idx", "published_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(TZ)
    interpreted_at: Mapped[datetime | None] = mapped_column(TZ)
    raw: Mapped[dict] = _json_col()
    created_at: Mapped[datetime] = _now_col()

    interpretations: Mapped[list[NewsInterpretation]] = relationship(back_populates="article")


class NewsInterpretation(Base):
    __tablename__ = "news_interpretations"
    __table_args__ = (
        Index("news_interpretations_currency_created_at_idx", "currency", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    article_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("news_articles.id", ondelete="SET NULL", onupdate="CASCADE")
    )
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    tone: Mapped[str] = mapped_column(Text, nullable=False)
    currency_bias: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reason_codes: Mapped[list[str] | None] = mapped_column(ARRAY(Text), default=list)
    summary: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    error: Mapped[str | None] = mapped_column(Text)
    raw_response: Mapped[Any | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now_col()

    article: Mapped[NewsArticle | None] = relationship(back_populates="interpretations")


# --- Decision pipeline --------------------------------------------------------------------


class DecisionRequest(Base):
    __tablename__ = "decision_requests"
    __table_args__ = (
        UniqueConstraint(
            "experiment",
            "instrument",
            "candle_time",
            name="decision_requests_experiment_instrument_candle_time_key",
        ),
        Index("decision_requests_created_at_idx", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    experiment: Mapped[str] = mapped_column(Text, nullable=False)
    instrument: Mapped[str] = mapped_column(Text, nullable=False)
    candle_time: Mapped[datetime] = mapped_column(TZ, nullable=False)
    created_at: Mapped[datetime] = _now_col()
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    trade_plan: Mapped[dict | None] = mapped_column(JSONB)
    strategy_result: Mapped[dict] = _json_col()
    llm_called: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    messages: Mapped[Any | None] = mapped_column(JSONB)

    decision: Mapped[Decision | None] = relationship(back_populates="request", uselist=False)
    risk_check: Mapped[RiskCheck | None] = relationship(back_populates="request", uselist=False)


class Decision(Base):
    __tablename__ = "decisions"
    __table_args__ = (Index("decisions_created_at_idx", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("decision_requests.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        unique=True,
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    setup: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    reason_codes: Mapped[list[str] | None] = mapped_column(ARRAY(Text), default=list)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    validation_error: Mapped[str | None] = mapped_column(Text)
    raw_response: Mapped[Any | None] = mapped_column(JSONB)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _now_col()

    request: Mapped[DecisionRequest] = relationship(back_populates="decision")
    risk_check: Mapped[RiskCheck | None] = relationship(back_populates="decision", uselist=False)


class RiskCheck(Base):
    __tablename__ = "risk_checks"
    __table_args__ = (Index("risk_checks_created_at_idx", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("decision_requests.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        unique=True,
    )
    decision_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("decisions.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        unique=True,
    )
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    checks: Mapped[Any] = mapped_column(JSONB, nullable=False)
    rejection_reasons: Mapped[list[str] | None] = mapped_column(ARRAY(Text), default=list)
    direction: Mapped[str | None] = mapped_column(Text)
    units: Mapped[int | None] = mapped_column(Integer)
    risk_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    risk_pct: Mapped[float | None] = mapped_column(Float)
    entry_price: Mapped[float | None] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float)
    take_profit: Mapped[float | None] = mapped_column(Float)
    risk_reward: Mapped[float | None] = mapped_column(Float)
    account_nav: Mapped[Decimal | None] = mapped_column(MONEY)
    created_at: Mapped[datetime] = _now_col()

    request: Mapped[DecisionRequest] = relationship(back_populates="risk_check")
    decision: Mapped[Decision] = relationship(back_populates="risk_check")
    order: Mapped[Order | None] = relationship(back_populates="risk_check", uselist=False)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("orders_status_idx", "status"),
        Index("orders_created_at_idx", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    risk_check_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("risk_checks.id", ondelete="SET NULL", onupdate="CASCADE"),
        unique=True,
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'ENTRY'"))
    instrument: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    order_type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'MARKET'"))
    requested_price: Mapped[float | None] = mapped_column(Float)
    price_bound: Mapped[float | None] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float)
    take_profit: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(Text)
    fill_transaction_id: Mapped[str | None] = mapped_column(Text)
    broker_trade_id: Mapped[str | None] = mapped_column(Text)
    fill_price: Mapped[float | None] = mapped_column(Float)
    filled_units: Mapped[int | None] = mapped_column(Integer)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    request_payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    response_payload: Mapped[Any | None] = mapped_column(JSONB)
    http_status: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    submitted_at: Mapped[datetime | None] = mapped_column(TZ)
    filled_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col(onupdate=func.now())

    risk_check: Mapped[RiskCheck | None] = relationship(back_populates="order")
    trades: Mapped[list[Trade]] = relationship(back_populates="order")


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        Index("trades_state_idx", "state"),
        Index("trades_open_time_idx", "open_time"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    broker_trade_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="SET NULL", onupdate="CASCADE")
    )
    client_trade_id: Mapped[str | None] = mapped_column(Text)
    experiment: Mapped[str | None] = mapped_column(Text)
    instrument: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    initial_units: Mapped[int] = mapped_column(Integer, nullable=False)
    current_units: Mapped[int] = mapped_column(Integer, nullable=False)
    open_price: Mapped[float] = mapped_column(Float, nullable=False)
    open_time: Mapped[datetime] = mapped_column(TZ, nullable=False)
    stop_loss: Mapped[float | None] = mapped_column(Float)
    take_profit: Mapped[float | None] = mapped_column(Float)
    initial_risk_price: Mapped[float | None] = mapped_column(Float)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    close_price: Mapped[float | None] = mapped_column(Float)
    close_time: Mapped[datetime | None] = mapped_column(TZ)
    close_reason: Mapped[str | None] = mapped_column(Text)
    realized_pl: Mapped[Decimal | None] = mapped_column(MONEY)
    unrealized_pl: Mapped[Decimal | None] = mapped_column(MONEY)
    financing: Mapped[Decimal | None] = mapped_column(MONEY)
    r_multiple: Mapped[float | None] = mapped_column(Float)
    unexpected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    raw: Mapped[dict] = _json_col()
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col(onupdate=func.now())

    order: Mapped[Order | None] = relationship(back_populates="trades")


class BrokerTransaction(Base):
    __tablename__ = "broker_transactions"
    __table_args__ = (Index("broker_transactions_time_idx", "time"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    transaction_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    time: Mapped[datetime] = mapped_column(TZ, nullable=False)
    order_id: Mapped[str | None] = mapped_column(Text)
    trade_id: Mapped[str | None] = mapped_column(Text)
    client_order_id: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    pl: Mapped[Decimal | None] = mapped_column(MONEY)
    raw: Mapped[Any] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _now_col()


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"
    __table_args__ = (Index("account_snapshots_taken_at_idx", "taken_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    taken_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    balance: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    nav: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    unrealized_pl: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    margin_used: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    margin_available: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    open_trade_count: Mapped[int] = mapped_column(Integer, nullable=False)
    open_position_count: Mapped[int] = mapped_column(Integer, nullable=False)
    pending_order_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_transaction_id: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict] = _json_col()


# --- Operations -------------------------------------------------------------------------


class SystemEvent(Base):
    __tablename__ = "system_events"
    __table_args__ = (
        Index("system_events_created_at_idx", "created_at"),
        Index("system_events_event_type_created_at_idx", "event_type", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = _now_col()
    level: Mapped[str] = mapped_column(Text, nullable=False)
    component: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict] = _json_col()


class ControlState(Base):
    __tablename__ = "control_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = _now_col(onupdate=func.now())
    updated_by: Mapped[str | None] = mapped_column(Text)


class AnalysisReport(Base):
    __tablename__ = "analysis_reports"
    __table_args__ = (
        Index("analysis_reports_experiment_created_at_idx", "experiment", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    experiment: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _now_col()
    period_start: Mapped[datetime | None] = mapped_column(TZ)
    period_end: Mapped[datetime] = mapped_column(TZ, nullable=False)
    metrics: Mapped[Any] = mapped_column(JSONB, nullable=False)
