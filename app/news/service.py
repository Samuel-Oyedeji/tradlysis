"""News/Fundamental Engine: collects calendar facts and news, interprets news with the LLM,
and exposes the current news state for the market snapshot."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.alerts.notifier import Notifier
from app.config.settings import Settings
from app.db.models import NewsArticle, NewsEvent, NewsInterpretation
from app.db.session import Database
from app.decision.openrouter import OpenRouterClient
from app.market_data.timeutil import utcnow
from app.news.interpreter import NEWS_PROMPT_VERSION, interpret_article
from app.news.providers.base import ArticleProvider, CalendarProvider
from app.news.state import EventView, InterpretationView, NewsState, build_news_state
from app.news.values import parse_value, surprise

log = logging.getLogger(__name__)
COMPONENT = "news"
CALENDAR_MAX_AGE = timedelta(hours=6)
MAX_INTERPRETATIONS_PER_POLL = 5


class NewsService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        notifier: Notifier,
        calendar: CalendarProvider,
        feeds: list[ArticleProvider],
        llm: OpenRouterClient | None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.notifier = notifier
        self.calendar = calendar
        self.feeds = feeds
        self.llm = llm
        self.last_calendar_success: datetime | None = None

    # ------------------------------------------------------------------ polling loops

    async def run_calendar_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.poll_calendar()
            await _sleep(stop, self.settings.news_calendar_poll_minutes * 60)

    async def run_articles_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.poll_articles()
            if self.settings.news_interpretation_enabled:
                await self.interpret_pending()
            await _sleep(stop, self.settings.news_rss_poll_minutes * 60)

    async def poll_calendar(self) -> int:
        try:
            events = await self.calendar.fetch()
        except Exception as exc:
            await self.notifier.warning(
                COMPONENT, "CALENDAR_FETCH_FAILED", f"{self.calendar.name}: {exc!r}",
                dedup_key="calendar_fetch_failed",
            )
            return 0
        if not events:
            return 0
        rows = []
        for e in events:
            fv, pv, av = parse_value(e.forecast), parse_value(e.previous), parse_value(e.actual)
            s, sd = surprise(av, fv)
            rows.append(
                {
                    "provider": e.provider,
                    "external_id": e.external_id,
                    "title": e.title,
                    "currency": e.currency,
                    "impact": e.impact,
                    "event_time": e.event_time,
                    "forecast": e.forecast,
                    "previous": e.previous,
                    "actual": e.actual,
                    "forecast_value": fv,
                    "previous_value": pv,
                    "actual_value": av,
                    "surprise": s,
                    "surprise_direction": sd,
                    "raw": e.raw,
                }
            )
        async with self.db.session() as s:
            stmt = insert(NewsEvent).values(rows)
            update_cols = (
                "title", "impact", "event_time", "forecast", "previous", "actual",
                "forecast_value", "previous_value", "actual_value", "surprise",
                "surprise_direction", "raw",
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["provider", "external_id"],
                set_={**{c: stmt.excluded[c] for c in update_cols}, "updated_at": func.now()},
            )
            await s.execute(stmt)
        self.last_calendar_success = utcnow()
        log.info("Calendar updated: %d events", len(rows))
        return len(rows)

    async def poll_articles(self) -> int:
        total = 0
        for feed in self.feeds:
            try:
                articles = await feed.fetch()
            except Exception as exc:
                await self.notifier.warning(
                    COMPONENT, "RSS_FETCH_FAILED", f"{feed.name}: {exc!r}", dedup_key=f"rss_{feed.name}"
                )
                continue
            if not articles:
                continue
            rows = [
                {
                    "provider": a.provider,
                    "external_id": a.external_id,
                    "currency": a.currency,
                    "title": a.title,
                    "summary": a.summary,
                    "url": a.url,
                    "published_at": a.published_at,
                    "raw": a.raw,
                }
                for a in articles
            ]
            async with self.db.session() as s:
                stmt = insert(NewsArticle).values(rows).on_conflict_do_nothing(
                    index_elements=["provider", "external_id"]
                )
                await s.execute(stmt)
            total += len(rows)
        return total

    async def interpret_pending(self) -> int:
        if self.llm is None or not self.llm.enabled:
            return 0
        horizon = utcnow() - timedelta(hours=self.settings.news_bias_lookback_hours)
        async with self.db.session() as s:
            pending = (
                await s.scalars(
                    select(NewsArticle)
                    .where(
                        NewsArticle.interpreted_at.is_(None),
                        NewsArticle.currency.is_not(None),
                        (NewsArticle.published_at.is_(None)) | (NewsArticle.published_at >= horizon),
                    )
                    .order_by(NewsArticle.published_at.desc().nulls_last())
                    .limit(MAX_INTERPRETATIONS_PER_POLL)
                )
            ).all()
        done = 0
        for art in pending:
            out, result, error = await interpret_article(
                self.llm,
                self.settings.news_model,
                art.currency or "",
                art.title,
                art.summary,
                art.published_at.isoformat() if art.published_at else None,
            )
            async with self.db.session() as s:
                s.add(
                    NewsInterpretation(
                        article_id=art.id,
                        currency=(out.currency if out else art.currency) or "",
                        tone=str(out.tone) if out else "NEUTRAL",
                        currency_bias=str(out.currency_bias) if out else "NEUTRAL",
                        confidence=out.confidence if out else 0.0,
                        reason_codes=[str(r) for r in out.reason_codes] if out else [],
                        summary=out.summary if out else None,
                        model=self.settings.news_model,
                        prompt_version=NEWS_PROMPT_VERSION,
                        valid=out is not None,
                        error=error,
                        raw_response=result.raw_response,
                    )
                )
                row = await s.get(NewsArticle, art.id)
                if row is not None:
                    row.interpreted_at = utcnow()
            done += 1
        return done

    # ------------------------------------------------------------------ state

    async def current_state(self, now: datetime) -> NewsState:
        base, quote = self.settings.instrument_currencies
        currencies = [base, quote]
        async with self.db.session() as s:
            events = (
                await s.scalars(
                    select(NewsEvent).where(
                        NewsEvent.event_time >= now - timedelta(hours=24),
                        NewsEvent.event_time <= now + timedelta(hours=24),
                    )
                )
            ).all()
            interps = (
                await s.scalars(
                    select(NewsInterpretation).where(
                        NewsInterpretation.valid.is_(True),
                        NewsInterpretation.created_at
                        >= now - timedelta(hours=self.settings.news_bias_lookback_hours),
                    )
                )
            ).all()
        fresh = self.last_calendar_success is not None and now - self.last_calendar_success <= CALENDAR_MAX_AGE
        return build_news_state(
            [
                EventView(
                    e.title, e.currency, e.impact, e.event_time, e.forecast, e.previous, e.actual,
                    e.surprise, e.surprise_direction,
                )
                for e in events
            ],
            [
                InterpretationView(
                    i.currency, i.currency_bias, i.tone, i.confidence, i.created_at, list(i.reason_codes or [])
                )
                for i in interps
            ],
            now,
            currencies,
            blocking_impacts=self.settings.news_blocking_impacts,
            blackout_before_minutes=self.settings.news_blackout_before_minutes,
            blackout_after_minutes=self.settings.news_blackout_after_minutes,
            bias_lookback_hours=self.settings.news_bias_lookback_hours,
            calendar_fresh=fresh,
        )


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass
