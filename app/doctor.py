"""Setup checker: verifies configuration and every external dependency without trading.

    python -m app.doctor            # all checks
    python -m app.doctor --telegram # also send a Telegram test message

Safe to run at any time: it only reads from Capital.com (it never places orders), sends one tiny
prompt to OpenRouter per model and (optionally) one Telegram message. It checks the configuration
the engine would use: the settings saved on the config page, over the environment.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from contextlib import aclosing

from app.config.settings import Settings, get_settings
from app.config.store import Configuration, ExperimentConfig, single_experiment

OK = "✓"
FAIL = "✗"
SKIP = "–"


async def check_database(s: Settings) -> str:
    s.require_database()
    from sqlalchemy import text

    from app.db.bootstrap import check_tables
    from app.db.session import Database

    db = Database(s)
    try:
        async with db.session() as sess:
            await sess.execute(text("select 1"))
            prisma = (
                await sess.execute(
                    text(
                        "select count(*) from information_schema.tables "
                        "where table_schema = current_schema() and table_name = '_prisma_migrations'"
                    )
                )
            ).scalar()
        report = await check_tables(db)
    finally:
        await db.dispose()
    total = len(report.expected_tables)
    present = total - len(report.missing_tables)
    if report.missing_columns:
        cols = "; ".join(f"{t}: {', '.join(c)}" for t, c in report.missing_columns.items())
        raise RuntimeError(f"tables are missing columns, apply the Prisma migrations ({cols})")
    if report.missing_tables:
        hint = (
            "they are created automatically when the engine starts"
            if s.db_auto_create_tables
            else "run: python -m app.db.bootstrap"
        )
        raise RuntimeError(
            f"connected; {present}/{total} tables present, missing: {', '.join(report.missing_tables)} ({hint})"
        )
    note = "" if prisma else " (Prisma has no migration record; only needed if you use npm run migrate:deploy)"
    return f"connected; all {total} tables present{note}"


async def load_config(s: Settings) -> Configuration:
    """The stored configuration (read-only); the environment alone if the database has none yet."""
    from app.config.store import load_configuration
    from app.db.session import Database

    db = Database(s)
    try:
        config = await load_configuration(db, s)
    finally:
        await db.dispose()
    if not config.experiments:
        config.experiments = single_experiment(config.settings).experiments
    return config


async def check_capital(s: Settings, experiments: list[ExperimentConfig]) -> str:
    s.require_broker_credentials()
    from app.broker.capital import CapitalClient

    first = experiments[0] if experiments else None
    client = CapitalClient(
        s.capital_base_url,
        s.capital_api_key,
        s.capital_identifier,
        s.capital_api_password,
        account_id=s.capital_account_id,
        instrument=first.instrument if first else s.instrument,
        epic=first.settings.broker_epic if first else s.broker_epic,
        stream_url=s.capital_stream_url,
        max_get_retries=1,
    )
    for e in experiments:
        client.register_market(e.instrument, e.settings.broker_epic)
    markets = []
    warn = []
    try:
        acct = await client.get_account()
        prefs = await client.get_preferences()
        for instrument, epic in client.markets.items():
            inst = await client.get_instrument(instrument)
            status = await client.get_market_status(instrument)
            candles = await client.get_candles("M15", count=3, instrument=instrument)
            markets.append(
                f"{inst.epic or epic}: pip {inst.pip_size}, min size {inst.minimum_trade_size}, "
                f"margin {inst.margin_rate:.2%}, status {status}, {len(candles)} M15 candles"
            )
            if inst.lot_size != 1:
                warn.append(f"{epic} lot size {inst.lot_size} != 1: the engine will refuse to start")
        stream = await _first_quote(client)
    finally:
        await client.aclose()
    instruments = [e.instrument for e in experiments]
    shared = sorted({i for i in instruments if instruments.count(i) > 1})
    if shared and not prefs.get("hedgingMode"):
        warn.append(
            f"hedging mode is OFF and several experiments trade {', '.join(shared)}: only one of them can hold a "
            "position at a time (turn hedging on in Capital.com to let each keep its own)"
        )
    host = "demo" if "demo-api" in s.capital_base_url else "LIVE"
    return (
        f"{host} host, account {acct.account_id} {acct.currency} equity {acct.nav} (available {acct.margin_available}), "
        f"hedging {'on' if prefs.get('hedgingMode') else 'off'}; {'; '.join(markets)}; stream: {stream}"
        + (f" (warning: {'; '.join(warn)})" if warn else "")
    )


async def _first_quote(client, timeout: float = 15.0) -> str:
    async def first() -> str:
        async with aclosing(client.stream_quotes()) as quotes:
            async for quote in quotes:
                if quote is not None:
                    return f"live quote {quote.bid}/{quote.ask}"
        return "closed without a quote"

    try:
        return await asyncio.wait_for(first(), timeout)
    except TimeoutError:
        return f"connected, no quote within {timeout:.0f}s (market closed?)"


async def check_openrouter(s: Settings, models: list[str]) -> str:
    results = []
    for model in dict.fromkeys(models):
        results.append(await check_model(s.model_copy(update={"openrouter_model": model})))
    return "; ".join(results)


async def check_model(s: Settings) -> str:
    if not s.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    from app.decision.openrouter import OpenRouterClient, uses_decisions_api

    client = OpenRouterClient(s.openrouter_api_key, s.openrouter_base_url, s.openrouter_app_url, s.openrouter_app_name)
    if uses_decisions_api(s.openrouter_model):
        try:
            d = await client.decisions(
                model=s.openrouter_model,
                state={"check": "connectivity"},
                questions={"ok": {"type": "choice", "instructions": "Pick the option named yes.",
                                  "criteria": {"yes": "Always choose this.", "no": "Never choose this."}}},
            )
        finally:
            await client.aclose()
        if not d.ok:
            raise RuntimeError(f"model {s.openrouter_model} (Decisions API): {d.error}")
        answer = d.answers.get("ok") or {}
        return f"model {s.openrouter_model} answered '{answer.get('choice')}' via the Decisions API in {d.latency_ms} ms"
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["ok"],
        "properties": {"ok": {"type": "boolean"}},
    }
    try:
        r = await client.structured_completion(
            model=s.openrouter_model,
            messages=[{"role": "user", "content": 'Reply with the JSON object {"ok": true}.'}],
            schema_name="ping",
            schema=schema,
            max_tokens=20,
        )
    finally:
        await client.aclose()
    if not r.ok:
        raise RuntimeError(f"model {s.openrouter_model}: {r.error}")
    return f"model {s.openrouter_model} answered in {r.latency_ms} ms using structured-output mode '{r.mode}'"


async def check_calendar(s: Settings, experiments: list[ExperimentConfig]) -> str:
    from app.news.providers.forexfactory import ForexFactoryCalendar

    events = await ForexFactoryCalendar(s.news_calendar_url).fetch()
    currencies = sorted({c for e in experiments for c in e.settings.instrument_currencies})
    high = [e for e in events if e.impact == "HIGH" and e.currency in currencies]
    feeds = {cur for cur, _ in s.rss_feeds}
    missing = [c for c in currencies if c not in feeds]
    note = f" (no central-bank RSS feed for {', '.join(missing)})" if missing else ""
    return f"{len(events)} events this week, {len(high)} high-impact for {'/'.join(currencies)}{note}"


async def check_rss(s: Settings) -> str:
    from app.news.providers.rss import RssFeed

    parts = []
    for cur, url in s.rss_feeds:
        items = await RssFeed(cur, url).fetch()
        parts.append(f"{cur}: {len(items)} items")
    return ", ".join(parts) or "no feeds configured"


async def check_telegram(s: Settings, send: bool) -> str:
    from app.alerts.telegram import TelegramSender

    t = TelegramSender(s.telegram_bot_token, s.telegram_chat_id)
    try:
        if not t.enabled:
            raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set (alerts will only be logged)")
        if not send:
            return "configured (use --telegram to send a test message)"
        if not await t.send("✅ Tradlysis doctor: Telegram alerts are working."):
            raise RuntimeError("send failed; check the bot token and that you have messaged the bot")
        return "test message sent"
    finally:
        await t.aclose()


def check_dashboard(s: Settings) -> str:
    if not s.dashboard_password:
        raise RuntimeError("DASHBOARD_PASSWORD not set: the dashboard/API stays locked")
    if not s.config_password:
        raise RuntimeError(f"user '{s.dashboard_username}' set, but CONFIG_PASSWORD is not: the config page stays locked")
    return f"user '{s.dashboard_username}', dashboard and config passwords set"


async def main() -> int:
    parser = argparse.ArgumentParser(description="Check Tradlysis configuration and connectivity")
    parser.add_argument("--telegram", action="store_true", help="send a Telegram test message")
    args = parser.parse_args()
    try:
        s = get_settings()
    except Exception as exc:
        print(f"{FAIL} settings: {exc}")
        return 1
    failures = 0
    try:
        print(f"{OK} database: {await check_database(s)}")
    except Exception as exc:
        failures += 1
        print(f"{FAIL} database: {exc}")
    try:
        config = await load_config(s)
    except Exception as exc:
        print(f"{FAIL} configuration: could not read it from the database ({exc}); checking the environment only")
        config = single_experiment(s)
    g = config.settings
    print(f"Mode: {g.trading_mode.value} · {len(config.enabled)} of {len(config.experiments)} experiment(s) enabled")
    for e in config.experiments:
        x = e.settings
        print(
            f"  {'●' if e.enabled else '○'} {e.slug} ({e.instrument}, {e.strategy}, model {x.openrouter_model}): "
            f"{x.risk_per_trade_pct}%/trade, max open {x.max_total_risk_pct}%, daily loss {x.max_daily_loss_pct}%, "
            f"drawdown {x.max_drawdown_pct}%, min R:R {x.min_risk_reward}, capital {e.summary()['capital'] or 'from the account'}"
        )
    active = config.enabled or config.experiments

    checks: list[tuple[str, Callable[[], Awaitable[str]]]] = [
        ("capital.com", lambda: check_capital(g, active)),
        ("openrouter", lambda: check_openrouter(g, [e.settings.openrouter_model for e in active])),
        ("calendar", lambda: check_calendar(g, active)),
        ("rss", lambda: check_rss(g)),
        ("telegram", lambda: check_telegram(g, args.telegram)),
    ]
    for name, fn in checks:
        try:
            print(f"{OK} {name}: {await fn()}")
        except Exception as exc:
            failures += 1
            print(f"{FAIL} {name}: {exc}")
    try:
        print(f"{OK} dashboard: {check_dashboard(s)}")
    except Exception as exc:
        failures += 1
        print(f"{FAIL} dashboard: {exc}")
    print("All checks passed." if not failures else f"{failures} check(s) need attention.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
