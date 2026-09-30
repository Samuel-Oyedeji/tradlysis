"""Async database engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import Settings


class Database:
    def __init__(self, settings: Settings) -> None:
        settings.require_database()
        self.engine: AsyncEngine = create_async_engine(
            settings.sqlalchemy_database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
        )
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Session that commits on success and rolls back on error."""
        async with self.sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        await self.engine.dispose()
