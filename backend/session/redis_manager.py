"""
session/redis_manager.py — Distributed session state via Redis.

Replaces the in-process `sessions: Dict[str, ChatManager] = {}` dict.
One Redis key per session_id. TTL and URL read from config.settings.

Stored payload (JSON):
  {
    "conv_state":   "AWAITING_DB_TYPE" | ... | "CONNECTED",
    "db_type":      "PostgreSQL" | "MySQL" | "MongoDB" | null,
    "hosting_type": "Local" | "Cloud" | null,
    "uri":          "<connection URI>" | null
  }

Security note: the URI contains credentials. In production, encrypt the
payload with a KMS-managed key before storing.
"""

import json
import logging

import redis.asyncio as aioredis
from fastapi import Request

from config import settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "qs:session:"


class RedisSessionManager:
    """Thin async wrapper around a Redis client for session CRUD."""

    def __init__(self, client: aioredis.Redis) -> None:
        self._client = client

    async def load(self, session_id: str) -> dict:
        """Return session dict, or empty dict if session does not exist."""
        raw = await self._client.get(f"{_KEY_PREFIX}{session_id}")
        if raw is None:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Corrupt session data for %s — resetting.", session_id)
            return {}

    async def save(self, session_id: str, state: dict) -> None:
        """Persist session state and refresh TTL."""
        await self._client.setex(
            f"{_KEY_PREFIX}{session_id}",
            settings.session_ttl_seconds,
            json.dumps(state, default=str),
        )

    async def delete(self, session_id: str) -> None:
        """Explicitly evict a session (e.g., on logout or reset)."""
        await self._client.delete(f"{_KEY_PREFIX}{session_id}")


# ---------------------------------------------------------------------------
# FastAPI Dependency — yields a session manager backed by the pool
# ---------------------------------------------------------------------------

async def get_session_manager(request: Request) -> RedisSessionManager:
    """
    FastAPI Depends() provider.
    Yields a RedisSessionManager backed by the shared connection pool
    stored in app.state.redis_pool (initialised in main.py lifespan).
    """
    pool = request.app.state.redis_pool
    client = aioredis.Redis(connection_pool=pool)
    yield RedisSessionManager(client)
