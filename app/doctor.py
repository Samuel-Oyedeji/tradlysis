"""Setup checker: verifies configuration and every external dependency without trading.

    python -m app.doctor            # all checks
    python -m app.doctor --telegram # also send a Telegram test message

Safe to run at any time: it only reads from OANDA, sends one tiny prompt to OpenRouter and
(optionally) one Telegram message.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable

from app.config.settings import Settings, get_settings

OK = "✓"
FAIL = "✗"
SKIP = "–"


async def check_database(s: Settings) -> str:
    s.require_database()
    from sqlalchemy import text

    from app.db.session import Database

    db = Database(s)
    try:
        async with db.session() as sess:
            await sess.execute(text("select 1"))
            rows = (
                await sess.execute(
                    text(
                        "select migration_name from _prisma_migrations "
                        "where finished_at is not null order by finished_at"
                    )
                )
            ).all()
    except Exception as exc:
        if "_prisma_migrations" in str(exc):
            raise RuntimeError("connected, but migrations have not been applied (run: npm run migrate:deploy)") from exc
        raise
    finally:
        await db.dispose()
    return f"connected; {len(rows)} migration(s) applied (latest: {rows[-1][0] if rows else 'none'})"


async def check_oanda(s: Settings) -> str:
    s.require_broker_credentials()
    from app.broker.oanda import OandaClient

    client = OandaClient(s.oanda_rest_url, s.oanda_stream_url, s.oanda_api_token, s.oanda_account_id, max_get_retries=1)
    try:
        acct = await client.get_account_summary()
        inst = await client.get_instrument(s.instrument)
        candles = await client.get_candles(s.instrument, "M15", count=3)
    finally:
        await client.aclose()
    warn = ""
    if s.is_demo and not s.oanda_account_id.startswith("101-"):
        warn = " (warning: practice account IDs normally start with 101-)"
    return (
        f"{s.trading_mode.value} account {acct.get('id')} {acct.get('currency')} NAV {acct.get('NAV')}; "
        f"{inst.name} pip {inst.pip_size}; {len(candles)} candles fetched{warn}"
    )


async def check_openrouter(s: Settings) -> str:
    if not s.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    from app.decision.openrouter import OpenRouterClient

    client = OpenRouterClient(s.openrouter_api_key, s.openrouter_base_url, s.openrouter_app_url, s.openrouter_app_name)
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


async def check_calendar(s: Settings) -> str:
    from app.news.providers.forexfactory import ForexFactoryCalendar

    events = await ForexFactoryCalendar(s.news_calendar_url).fetch()
    base, quote = s.instrument_currencies
    high = [e for e in events if e.impact == "HIGH" and e.currency in (base, quote)]
    return f"{len(events)} events this week, {len(high)} high-impact for {base}/{quote}"


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
    return f"user '{s.dashboard_username}', password set"


async def main() -> int:
    parser = argparse.ArgumentParser(description="Check Tradlysis configuration and connectivity")
    parser.add_argument("--telegram", action="store_true", help="send a Telegram test message")
    args = parser.parse_args()
    try:
        s = get_settings()
    except Exception as exc:
        print(f"{FAIL} settings: {exc}")
        return 1
    print(f"Mode: {s.trading_mode.value} · instrument {s.instrument} · experiment {s.experiment_name}")
    print(
        f"Risk: {s.risk_per_trade_pct}%/trade, max total {s.max_total_risk_pct}%, daily loss "
        f"{s.max_daily_loss_pct}%, drawdown {s.max_drawdown_pct}%, min R:R {s.min_risk_reward}"
    )

    checks: list[tuple[str, Callable[[], Awaitable[str]]]] = [
        ("database", lambda: check_database(s)),
        ("oanda", lambda: check_oanda(s)),
        ("openrouter", lambda: check_openrouter(s)),
        ("calendar", lambda: check_calendar(s)),
        ("rss", lambda: check_rss(s)),
        ("telegram", lambda: check_telegram(s, args.telegram)),
    ]
    failures = 0
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
