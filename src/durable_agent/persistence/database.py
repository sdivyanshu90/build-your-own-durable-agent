"""SQLAlchemy engine lifecycle and local SQLite safety settings."""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from durable_agent.persistence.orm import Base


class Database:
    """Own the async engine and session factory; schema changes belong to Alembic."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self._sync = url.startswith("sqlite://") and not url.startswith("sqlite+aiosqlite://")
        if self._sync:
            sync_engine = create_engine(url, echo=echo, pool_pre_ping=True)
            self.engine: AsyncEngine | Engine = sync_engine
            factory = sessionmaker(sync_engine, expire_on_commit=False, autoflush=False)
            self.sessions: Any = _SyncSessionFactory(factory)
            event.listen(sync_engine, "connect", self._configure_sqlite)
        else:
            async_engine = create_async_engine(url, echo=echo, pool_pre_ping=True)
            self.engine = async_engine
            self.sessions = async_sessionmaker(
                async_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
            )
            if url.startswith("sqlite"):
                event.listen(async_engine.sync_engine, "connect", self._configure_sqlite)

    async def dispose(self) -> None:
        """Close all connection pools."""
        if isinstance(self.engine, AsyncEngine):
            await self.engine.dispose()
        else:
            self.engine.dispose()

    async def create_schema_for_tests(self) -> None:
        """Create metadata only for isolated tests; applications must run Alembic."""
        if isinstance(self.engine, AsyncEngine):
            async with self.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        else:
            Base.metadata.create_all(self.engine)

    async def ping(self) -> bool:
        """Verify a connection and trivial query without mutating state."""
        from sqlalchemy import text

        try:
            if isinstance(self.engine, AsyncEngine):
                async with self.engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
            else:
                with self.engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    @staticmethod
    def _configure_sqlite(dbapi_connection: object, connection_record: object) -> None:
        del connection_record
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()


class _SyncSessionAdapter:
    """Async-shaped facade for deterministic SQLite tests and constrained hosts."""

    def __init__(self, factory: sessionmaker[Session], *, transactional: bool) -> None:
        self._factory = factory
        self._transactional = transactional
        self._session: Session | None = None
        self._transaction: Any = None

    async def __aenter__(self) -> _SyncSessionAdapter:
        self._session = self._factory()
        if self._transactional:
            self._transaction = self._session.begin()
            self._transaction.__enter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._session is None:
            return
        try:
            if self._transaction is not None:
                self._transaction.__exit__(exc_type, exc, traceback)
        finally:
            self._session.close()

    def add(self, instance: object) -> None:
        self._required().add(instance)

    async def get(self, entity: Any, identifier: Any) -> Any:
        return self._required().get(entity, identifier)

    async def execute(self, statement: Any) -> Any:
        return self._required().execute(statement)

    async def scalar(self, statement: Any) -> Any:
        return self._required().scalar(statement)

    async def scalars(self, statement: Any) -> Any:
        return self._required().scalars(statement)

    async def flush(self) -> None:
        self._required().flush()

    def _required(self) -> Session:
        if self._session is None:
            raise RuntimeError("session adapter is not active")
        return self._session


class _SyncSessionFactory:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def __call__(self) -> _SyncSessionAdapter:
        return _SyncSessionAdapter(self._factory, transactional=False)

    def begin(self) -> _SyncSessionAdapter:
        return _SyncSessionAdapter(self._factory, transactional=True)
