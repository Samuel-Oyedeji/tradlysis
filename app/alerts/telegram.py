"""Minimal Telegram Bot API sender."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class TelegramSender:
    def __init__(self, bot_token: str, chat_id: str, client: httpx.AsyncClient | None = None) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._client = client or httpx.AsyncClient(timeout=10.0)

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            resp = await self._client.post(
                url,
                json={"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True},
            )
            if resp.status_code != 200:
                log.warning("Telegram send failed: HTTP %s %s", resp.status_code, resp.text[:200])
                return False
            return True
        except httpx.HTTPError as exc:
            log.warning("Telegram send failed: %s", exc)
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
