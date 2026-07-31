"""
db/repository.py — Repository Pattern: async data access abstraction.

Business logic (graph.py) depends only on BaseRepository.execute_query().
Concrete drivers (asyncpg, aiomysql, Motor) are isolated here.
"""

import logging
from abc import ABC, abstractmethod

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class BaseRepository(ABC):
    """Uniform async interface for SQL and MongoDB execution."""

    @abstractmethod
    async def execute_query(self, parsed) -> list[dict]:
        """
        Execute a generated query against the database.

        Args:
            parsed: SQLQueryResponse or MongoQueryResponse Pydantic object
                    from graph.py's GENERATING node.

        Returns:
            List of result rows as plain dicts (serialisable).
            Empty list if the query produces no rows.
        """

    @abstractmethod
    async def ping(self) -> None:
        """Verify connectivity. Raises on failure."""


# ---------------------------------------------------------------------------
# SQL implementation (PostgreSQL + MySQL via asyncpg / aiomysql)
# ---------------------------------------------------------------------------

class SQLRepository(BaseRepository):
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def ping(self) -> None:
        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def execute_query(self, parsed) -> list[dict]:
        async with self._engine.connect() as conn:
            result = await conn.execute(text(parsed.query))
            if not result.returns_rows:
                return []
            rows = result.fetchmany(10)
            return [dict(zip(result.keys(), row)) for row in rows]

    async def dispose(self) -> None:
        """Release the engine's connection pool. Call after use."""
        await self._engine.dispose()


# ---------------------------------------------------------------------------
# MongoDB implementation (Motor async driver)
# ---------------------------------------------------------------------------

class MongoRepository(BaseRepository):
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db

    async def ping(self) -> None:
        await self._db.client.admin.command("ping")

    async def execute_query(self, parsed) -> list[dict]:
        cursor = self._db[parsed.collection].find(parsed.filter).limit(10)
        data = []
        async for doc in cursor:
            doc["_id"] = str(doc["_id"])
            data.append(doc)
        return data
