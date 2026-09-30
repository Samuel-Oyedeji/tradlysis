-- CreateTable
CREATE TABLE "market_prices" (
    "id" BIGSERIAL NOT NULL,
    "instrument" TEXT NOT NULL,
    "time" TIMESTAMPTZ(6) NOT NULL,
    "bid" DOUBLE PRECISION NOT NULL,
    "ask" DOUBLE PRECISION NOT NULL,
    "mid" DOUBLE PRECISION NOT NULL,
    "spread_pips" DOUBLE PRECISION NOT NULL,
    "tradeable" BOOLEAN NOT NULL DEFAULT true,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "market_prices_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "candles" (
    "id" BIGSERIAL NOT NULL,
    "instrument" TEXT NOT NULL,
    "granularity" TEXT NOT NULL,
    "time" TIMESTAMPTZ(6) NOT NULL,
    "open" DOUBLE PRECISION NOT NULL,
    "high" DOUBLE PRECISION NOT NULL,
    "low" DOUBLE PRECISION NOT NULL,
    "close" DOUBLE PRECISION NOT NULL,
    "bid_close" DOUBLE PRECISION,
    "ask_close" DOUBLE PRECISION,
    "volume" INTEGER NOT NULL DEFAULT 0,
    "complete" BOOLEAN NOT NULL DEFAULT false,
    "source" TEXT NOT NULL DEFAULT 'oanda',
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "candles_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "technical_snapshots" (
    "id" BIGSERIAL NOT NULL,
    "instrument" TEXT NOT NULL,
    "timeframe" TEXT NOT NULL,
    "candle_time" TIMESTAMPTZ(6) NOT NULL,
    "computed_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "close" DOUBLE PRECISION NOT NULL,
    "ema20" DOUBLE PRECISION,
    "ema50" DOUBLE PRECISION,
    "ema200" DOUBLE PRECISION,
    "rsi14" DOUBLE PRECISION,
    "atr14" DOUBLE PRECISION,
    "trend" TEXT NOT NULL,
    "structure" TEXT NOT NULL,
    "last_swing_high" DOUBLE PRECISION,
    "last_swing_low" DOUBLE PRECISION,
    "details" JSONB NOT NULL DEFAULT '{}',

    CONSTRAINT "technical_snapshots_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "support_resistance" (
    "id" BIGSERIAL NOT NULL,
    "instrument" TEXT NOT NULL,
    "computed_at" TIMESTAMPTZ(6) NOT NULL,
    "kind" TEXT NOT NULL,
    "source" TEXT NOT NULL,
    "timeframe" TEXT,
    "price_low" DOUBLE PRECISION NOT NULL,
    "price_high" DOUBLE PRECISION NOT NULL,
    "price" DOUBLE PRECISION NOT NULL,
    "touches" INTEGER NOT NULL DEFAULT 1,
    "strength" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "support_resistance_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "market_regimes" (
    "id" BIGSERIAL NOT NULL,
    "instrument" TEXT NOT NULL,
    "timeframe" TEXT NOT NULL,
    "computed_at" TIMESTAMPTZ(6) NOT NULL,
    "trend_regime" TEXT NOT NULL,
    "volatility_regime" TEXT NOT NULL,
    "atr" DOUBLE PRECISION,
    "atr_percentile" DOUBLE PRECISION,
    "details" JSONB NOT NULL DEFAULT '{}',

    CONSTRAINT "market_regimes_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "news_events" (
    "id" BIGSERIAL NOT NULL,
    "provider" TEXT NOT NULL,
    "external_id" TEXT NOT NULL,
    "title" TEXT NOT NULL,
    "currency" TEXT NOT NULL,
    "impact" TEXT NOT NULL,
    "event_time" TIMESTAMPTZ(6) NOT NULL,
    "forecast" TEXT,
    "previous" TEXT,
    "actual" TEXT,
    "forecast_value" DOUBLE PRECISION,
    "previous_value" DOUBLE PRECISION,
    "actual_value" DOUBLE PRECISION,
    "surprise" DOUBLE PRECISION,
    "surprise_direction" TEXT,
    "raw" JSONB NOT NULL DEFAULT '{}',
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "news_events_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "news_articles" (
    "id" BIGSERIAL NOT NULL,
    "provider" TEXT NOT NULL,
    "external_id" TEXT NOT NULL,
    "currency" TEXT,
    "title" TEXT NOT NULL,
    "summary" TEXT,
    "url" TEXT,
    "published_at" TIMESTAMPTZ(6),
    "interpreted_at" TIMESTAMPTZ(6),
    "raw" JSONB NOT NULL DEFAULT '{}',
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "news_articles_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "news_interpretations" (
    "id" BIGSERIAL NOT NULL,
    "article_id" BIGINT,
    "currency" TEXT NOT NULL,
    "tone" TEXT NOT NULL,
    "currency_bias" TEXT NOT NULL,
    "confidence" DOUBLE PRECISION NOT NULL,
    "reason_codes" TEXT[],
    "summary" TEXT,
    "model" TEXT NOT NULL,
    "prompt_version" TEXT NOT NULL,
    "valid" BOOLEAN NOT NULL DEFAULT true,
    "error" TEXT,
    "raw_response" JSONB,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "news_interpretations_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "decision_requests" (
    "id" BIGSERIAL NOT NULL,
    "experiment" TEXT NOT NULL,
    "instrument" TEXT NOT NULL,
    "candle_time" TIMESTAMPTZ(6) NOT NULL,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "snapshot" JSONB NOT NULL,
    "trade_plan" JSONB,
    "strategy_result" JSONB NOT NULL DEFAULT '{}',
    "llm_called" BOOLEAN NOT NULL DEFAULT false,
    "model" TEXT,
    "prompt_version" TEXT,
    "messages" JSONB,

    CONSTRAINT "decision_requests_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "decisions" (
    "id" BIGSERIAL NOT NULL,
    "request_id" BIGINT NOT NULL,
    "source" TEXT NOT NULL,
    "decision" TEXT NOT NULL,
    "setup" TEXT,
    "confidence" DOUBLE PRECISION,
    "reason_codes" TEXT[],
    "valid" BOOLEAN NOT NULL DEFAULT true,
    "validation_error" TEXT,
    "raw_response" JSONB,
    "latency_ms" INTEGER,
    "prompt_tokens" INTEGER,
    "completion_tokens" INTEGER,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "decisions_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "risk_checks" (
    "id" BIGSERIAL NOT NULL,
    "request_id" BIGINT NOT NULL,
    "decision_id" BIGINT NOT NULL,
    "approved" BOOLEAN NOT NULL,
    "checks" JSONB NOT NULL,
    "rejection_reasons" TEXT[],
    "direction" TEXT,
    "units" INTEGER,
    "risk_amount" DECIMAL(20,6),
    "risk_pct" DOUBLE PRECISION,
    "entry_price" DOUBLE PRECISION,
    "stop_loss" DOUBLE PRECISION,
    "take_profit" DOUBLE PRECISION,
    "risk_reward" DOUBLE PRECISION,
    "account_nav" DECIMAL(20,6),
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "risk_checks_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "orders" (
    "id" BIGSERIAL NOT NULL,
    "client_order_id" TEXT NOT NULL,
    "risk_check_id" BIGINT,
    "purpose" TEXT NOT NULL DEFAULT 'ENTRY',
    "instrument" TEXT NOT NULL,
    "direction" TEXT NOT NULL,
    "units" INTEGER NOT NULL,
    "order_type" TEXT NOT NULL DEFAULT 'MARKET',
    "requested_price" DOUBLE PRECISION,
    "price_bound" DOUBLE PRECISION,
    "stop_loss" DOUBLE PRECISION,
    "take_profit" DOUBLE PRECISION,
    "status" TEXT NOT NULL,
    "broker_order_id" TEXT,
    "fill_transaction_id" TEXT,
    "broker_trade_id" TEXT,
    "fill_price" DOUBLE PRECISION,
    "filled_units" INTEGER,
    "reject_reason" TEXT,
    "request_payload" JSONB NOT NULL,
    "response_payload" JSONB,
    "http_status" INTEGER,
    "attempts" INTEGER NOT NULL DEFAULT 0,
    "submitted_at" TIMESTAMPTZ(6),
    "filled_at" TIMESTAMPTZ(6),
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "orders_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "trades" (
    "id" BIGSERIAL NOT NULL,
    "broker_trade_id" TEXT NOT NULL,
    "order_id" BIGINT,
    "client_trade_id" TEXT,
    "experiment" TEXT,
    "instrument" TEXT NOT NULL,
    "direction" TEXT NOT NULL,
    "initial_units" INTEGER NOT NULL,
    "current_units" INTEGER NOT NULL,
    "open_price" DOUBLE PRECISION NOT NULL,
    "open_time" TIMESTAMPTZ(6) NOT NULL,
    "stop_loss" DOUBLE PRECISION,
    "take_profit" DOUBLE PRECISION,
    "initial_risk_price" DOUBLE PRECISION,
    "state" TEXT NOT NULL,
    "close_price" DOUBLE PRECISION,
    "close_time" TIMESTAMPTZ(6),
    "close_reason" TEXT,
    "realized_pl" DECIMAL(20,6),
    "unrealized_pl" DECIMAL(20,6),
    "financing" DECIMAL(20,6),
    "r_multiple" DOUBLE PRECISION,
    "unexpected" BOOLEAN NOT NULL DEFAULT false,
    "raw" JSONB NOT NULL DEFAULT '{}',
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "trades_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "broker_transactions" (
    "id" BIGSERIAL NOT NULL,
    "transaction_id" TEXT NOT NULL,
    "account_id" TEXT NOT NULL,
    "type" TEXT NOT NULL,
    "time" TIMESTAMPTZ(6) NOT NULL,
    "order_id" TEXT,
    "trade_id" TEXT,
    "client_order_id" TEXT,
    "reason" TEXT,
    "pl" DECIMAL(20,6),
    "raw" JSONB NOT NULL,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "broker_transactions_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "account_snapshots" (
    "id" BIGSERIAL NOT NULL,
    "taken_at" TIMESTAMPTZ(6) NOT NULL,
    "account_id" TEXT NOT NULL,
    "currency" TEXT NOT NULL,
    "balance" DECIMAL(20,6) NOT NULL,
    "nav" DECIMAL(20,6) NOT NULL,
    "unrealized_pl" DECIMAL(20,6) NOT NULL,
    "margin_used" DECIMAL(20,6) NOT NULL,
    "margin_available" DECIMAL(20,6) NOT NULL,
    "open_trade_count" INTEGER NOT NULL,
    "open_position_count" INTEGER NOT NULL,
    "pending_order_count" INTEGER NOT NULL,
    "last_transaction_id" TEXT,
    "raw" JSONB NOT NULL DEFAULT '{}',

    CONSTRAINT "account_snapshots_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "system_events" (
    "id" BIGSERIAL NOT NULL,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "level" TEXT NOT NULL,
    "component" TEXT NOT NULL,
    "event_type" TEXT NOT NULL,
    "message" TEXT NOT NULL,
    "details" JSONB NOT NULL DEFAULT '{}',

    CONSTRAINT "system_events_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "control_state" (
    "key" TEXT NOT NULL,
    "value" JSONB NOT NULL,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_by" TEXT,

    CONSTRAINT "control_state_pkey" PRIMARY KEY ("key")
);

-- CreateTable
CREATE TABLE "analysis_reports" (
    "id" BIGSERIAL NOT NULL,
    "experiment" TEXT NOT NULL,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "period_start" TIMESTAMPTZ(6),
    "period_end" TIMESTAMPTZ(6) NOT NULL,
    "metrics" JSONB NOT NULL,

    CONSTRAINT "analysis_reports_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "market_prices_instrument_time_idx" ON "market_prices"("instrument", "time");

-- CreateIndex
CREATE UNIQUE INDEX "candles_instrument_granularity_time_key" ON "candles"("instrument", "granularity", "time");

-- CreateIndex
CREATE UNIQUE INDEX "technical_snapshots_instrument_timeframe_candle_time_key" ON "technical_snapshots"("instrument", "timeframe", "candle_time");

-- CreateIndex
CREATE INDEX "support_resistance_instrument_computed_at_idx" ON "support_resistance"("instrument", "computed_at");

-- CreateIndex
CREATE INDEX "market_regimes_instrument_timeframe_computed_at_idx" ON "market_regimes"("instrument", "timeframe", "computed_at");

-- CreateIndex
CREATE INDEX "news_events_event_time_idx" ON "news_events"("event_time");

-- CreateIndex
CREATE INDEX "news_events_currency_event_time_idx" ON "news_events"("currency", "event_time");

-- CreateIndex
CREATE UNIQUE INDEX "news_events_provider_external_id_key" ON "news_events"("provider", "external_id");

-- CreateIndex
CREATE INDEX "news_articles_published_at_idx" ON "news_articles"("published_at");

-- CreateIndex
CREATE UNIQUE INDEX "news_articles_provider_external_id_key" ON "news_articles"("provider", "external_id");

-- CreateIndex
CREATE INDEX "news_interpretations_currency_created_at_idx" ON "news_interpretations"("currency", "created_at");

-- CreateIndex
CREATE INDEX "decision_requests_created_at_idx" ON "decision_requests"("created_at");

-- CreateIndex
CREATE UNIQUE INDEX "decision_requests_experiment_instrument_candle_time_key" ON "decision_requests"("experiment", "instrument", "candle_time");

-- CreateIndex
CREATE UNIQUE INDEX "decisions_request_id_key" ON "decisions"("request_id");

-- CreateIndex
CREATE INDEX "decisions_created_at_idx" ON "decisions"("created_at");

-- CreateIndex
CREATE UNIQUE INDEX "risk_checks_request_id_key" ON "risk_checks"("request_id");

-- CreateIndex
CREATE UNIQUE INDEX "risk_checks_decision_id_key" ON "risk_checks"("decision_id");

-- CreateIndex
CREATE INDEX "risk_checks_created_at_idx" ON "risk_checks"("created_at");

-- CreateIndex
CREATE UNIQUE INDEX "orders_client_order_id_key" ON "orders"("client_order_id");

-- CreateIndex
CREATE UNIQUE INDEX "orders_risk_check_id_key" ON "orders"("risk_check_id");

-- CreateIndex
CREATE INDEX "orders_status_idx" ON "orders"("status");

-- CreateIndex
CREATE INDEX "orders_created_at_idx" ON "orders"("created_at");

-- CreateIndex
CREATE UNIQUE INDEX "trades_broker_trade_id_key" ON "trades"("broker_trade_id");

-- CreateIndex
CREATE INDEX "trades_state_idx" ON "trades"("state");

-- CreateIndex
CREATE INDEX "trades_open_time_idx" ON "trades"("open_time");

-- CreateIndex
CREATE UNIQUE INDEX "broker_transactions_transaction_id_key" ON "broker_transactions"("transaction_id");

-- CreateIndex
CREATE INDEX "broker_transactions_time_idx" ON "broker_transactions"("time");

-- CreateIndex
CREATE INDEX "account_snapshots_taken_at_idx" ON "account_snapshots"("taken_at");

-- CreateIndex
CREATE INDEX "system_events_created_at_idx" ON "system_events"("created_at");

-- CreateIndex
CREATE INDEX "system_events_event_type_created_at_idx" ON "system_events"("event_type", "created_at");

-- CreateIndex
CREATE INDEX "analysis_reports_experiment_created_at_idx" ON "analysis_reports"("experiment", "created_at");

-- AddForeignKey
ALTER TABLE "news_interpretations" ADD CONSTRAINT "news_interpretations_article_id_fkey" FOREIGN KEY ("article_id") REFERENCES "news_articles"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "decisions" ADD CONSTRAINT "decisions_request_id_fkey" FOREIGN KEY ("request_id") REFERENCES "decision_requests"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "risk_checks" ADD CONSTRAINT "risk_checks_request_id_fkey" FOREIGN KEY ("request_id") REFERENCES "decision_requests"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "risk_checks" ADD CONSTRAINT "risk_checks_decision_id_fkey" FOREIGN KEY ("decision_id") REFERENCES "decisions"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "orders" ADD CONSTRAINT "orders_risk_check_id_fkey" FOREIGN KEY ("risk_check_id") REFERENCES "risk_checks"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "trades" ADD CONSTRAINT "trades_order_id_fkey" FOREIGN KEY ("order_id") REFERENCES "orders"("id") ON DELETE SET NULL ON UPDATE CASCADE;
