"""RSS/Atom feeds of central-bank communication (e.g. Fed and ECB press releases)."""

from __future__ import annotations

import calendar
import hashlib
from datetime import UTC, datetime

import feedparser
import httpx

from app.news.providers.base import Article


class RssFeed:
    def __init__(self, currency: str, url: str, client: httpx.AsyncClient | None = None) -> None:
        self.currency = currency
        self.url = url
        self.name = f"rss:{currency.lower()}"
        self._client = client or httpx.AsyncClient(
            timeout=20.0, headers={"User-Agent": "tradlysis/1.0 (+rss poller)"}, follow_redirects=True
        )

    async def fetch(self) -> list[Article]:
        resp = await self._client.get(self.url)
        resp.raise_for_status()
        return parse_feed(resp.content, self.name, self.currency)


def parse_feed(content: bytes, provider: str, currency: str) -> list[Article]:
    feed = feedparser.parse(content)
    out: list[Article] = []
    for entry in feed.entries:
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        link = entry.get("link")
        ident = entry.get("id") or link or title
        published = None
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if parsed:
            published = datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)
        summary = (entry.get("summary") or "").strip() or None
        out.append(
            Article(
                provider=provider,
                external_id=hashlib.sha1(str(ident).encode()).hexdigest(),
                title=title,
                currency=currency,
                summary=summary[:4000] if summary else None,
                url=link,
                published_at=published,
                raw={"id": ident, "link": link},
            )
        )
    return out
