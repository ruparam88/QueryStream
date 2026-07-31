"""
db/connections.py — Async engine and client factories.

Rules:
- One function per DB type. No singletons — callers own lifecycle.
- URI conversion happens here (sync dialect → async driver prefix).
- No business logic; pure infrastructure.
"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine
from motor.motor_asyncio import AsyncIOMotorClient


# ---------------------------------------------------------------------------
# URI helpers
# ---------------------------------------------------------------------------

def to_async_uri(db_type: str, uri: str) -> str:
    """
    Convert a user-supplied sync URI to an async-driver URI.
    PostgreSQL: postgresql:// → postgresql+asyncpg://
    MySQL:      mysql+pymysql:// or mysql:// → mysql+aiomysql://
    MongoDB:    unchanged (Motor accepts the same URI as PyMongo).
    """
    if db_type == "PostgreSQL":
        for prefix in ("postgresql+psycopg2://", "postgresql://"):
            if uri.startswith(prefix):
                return "postgresql+asyncpg://" + uri[len(prefix):]
    elif db_type == "MySQL":
        for prefix in ("mysql+pymysql://", "mysql://"):
            if uri.startswith(prefix):
                return "mysql+aiomysql://" + uri[len(prefix):]
    return uri


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def make_async_sql_engine(db_type: str, uri: str) -> AsyncEngine:
    """Return a SQLAlchemy async engine for PostgreSQL or MySQL."""
    async_uri = to_async_uri(db_type, uri)
    return create_async_engine(async_uri, echo=False, pool_pre_ping=True)


def make_motor_client(uri: str) -> AsyncIOMotorClient:
    """Return a Motor async MongoDB client."""
    return AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
