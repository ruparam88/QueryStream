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


def _make_serializable(val):
    if val is None or isinstance(val, (int, float, bool, str)):
        return val
    if isinstance(val, dict):
        return {k: _make_serializable(v) for k, v in val.items()}
    if isinstance(val, (list, tuple, set)):
        return [_make_serializable(v) for v in val]
    return str(val)


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

    @abstractmethod
    async def get_schema_context(self) -> str:
        """Return a string summary of available database tables/collections and columns/fields."""


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
            return [{k: _make_serializable(v) for k, v in zip(result.keys(), row)} for row in rows]

    async def get_schema_context(self) -> str:
        """
        Return a richly formatted schema summary for the LLM prompt.

        Format emitted (DDL-style, one column per line):

            Table: orders
              - id          INTEGER
              - customer_id INTEGER
              - total       DECIMAL

            Table: customers
              - id          INTEGER
              - name        VARCHAR

            Foreign Keys:
              orders.customer_id  ->  customers.id
        """
        try:
            dialect_name = getattr(self._engine.dialect, "name", "postgresql")

            # ── 1. Column query (dialect-specific) ─────────────────────────
            if dialect_name == "postgresql":
                col_sql = """
                    SELECT table_name, column_name, data_type
                    FROM   information_schema.columns
                    WHERE  table_schema = 'public'
                    ORDER  BY table_name, ordinal_position
                """
                fk_sql = """
                    SELECT
                        tc.table_name            AS from_table,
                        kcu.column_name          AS from_col,
                        ccu.table_name           AS to_table,
                        ccu.column_name          AS to_col
                    FROM information_schema.table_constraints  tc
                    JOIN information_schema.key_column_usage   kcu
                         ON  tc.constraint_name = kcu.constraint_name
                         AND tc.table_schema    = kcu.table_schema
                    JOIN information_schema.constraint_column_usage ccu
                         ON  ccu.constraint_name = tc.constraint_name
                         AND ccu.table_schema    = tc.table_schema
                    WHERE tc.constraint_type = 'FOREIGN KEY'
                      AND tc.table_schema    = 'public'
                """
            else:  # MySQL (and fallback)
                col_sql = """
                    SELECT table_name, column_name, data_type
                    FROM   information_schema.columns
                    WHERE  table_schema = DATABASE()
                    ORDER  BY table_name, ordinal_position
                """
                fk_sql = """
                    SELECT
                        TABLE_NAME            AS from_table,
                        COLUMN_NAME           AS from_col,
                        REFERENCED_TABLE_NAME AS to_table,
                        REFERENCED_COLUMN_NAME AS to_col
                    FROM information_schema.KEY_COLUMN_USAGE
                    WHERE REFERENCED_TABLE_NAME IS NOT NULL
                      AND TABLE_SCHEMA = DATABASE()
                """

            async with self._engine.connect() as conn:
                col_result = await conn.execute(text(col_sql))
                col_rows   = col_result.fetchall()
                try:
                    fk_result = await conn.execute(text(fk_sql))
                    fk_rows   = fk_result.fetchall()
                except Exception:
                    fk_rows = []

            if not col_rows:
                return "No user tables found in schema."

            # ── 2. Build DDL-style column listing ──────────────────────────
            schema_map: dict[str, list[str]] = {}
            for t_name, c_name, d_type in col_rows:
                schema_map.setdefault(t_name, []).append(
                    f"  - {c_name:<28} {d_type.upper()}"
                )

            table_lines = []
            for table, cols in schema_map.items():
                table_lines.append(f"Table: {table}")
                table_lines.extend(cols)
                table_lines.append("")   # blank line between tables

            # ── 3. Append FK relationships ─────────────────────────────────
            if fk_rows:
                table_lines.append("Foreign Keys (use these for JOIN conditions):")
                for from_tbl, from_col, to_tbl, to_col in fk_rows:
                    table_lines.append(
                        f"  {from_tbl}.{from_col}  ->  {to_tbl}.{to_col}"
                    )

            return "\n".join(table_lines)

        except Exception as exc:
            logger.warning("Failed to introspect SQL schema: %s", exc)
            return "Schema introspection failed or unavailable."

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
            data.append({k: _make_serializable(v) for k, v in doc.items()})
        return data

    async def get_schema_context(self) -> str:
        """List MongoDB collections and sample document fields."""
        try:
            collections = await self._db.list_collection_names()
            lines = []
            for col in collections:
                if col.startswith("system.") or col.startswith("local."):
                    continue
                doc = await self._db[col].find_one()
                fields = list(doc.keys()) if doc and isinstance(doc, dict) else ["<empty collection>"]
                lines.append(f"Collection: {col} (fields: {', '.join(fields)})")
            return "\n".join(lines) or "No collections found."
        except Exception as exc:
            logger.warning("Failed to introspect Mongo schema: %s", exc)
            return "Schema introspection failed or unavailable."
