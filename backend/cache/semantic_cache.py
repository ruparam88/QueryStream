"""
cache/semantic_cache.py — Proxy/Decorator pattern for semantic query caching.

All tunable parameters (threshold, TTL, max_entries, embed model) are read
from config.settings — no magic numbers in this file.

How it works:
  1. Embed the incoming natural-language query via Gemini text-embedding-004.
  2. Compare the embedding against all cached embeddings stored in Redis
     for this (session_id, db_type) using cosine similarity.
  3. HIT  (sim >= threshold): return the cached Pydantic parsed object.
     The caller re-executes it against the DB — zero LLM calls needed.
  4. MISS: caller runs the full LangGraph pipeline, then calls store().
  5. store(): embed + serialise the parsed result → append to Redis list.

Storage schema (Redis list key per session+db_type):
  Key:   qs:sem:{session_id}:{db_type}
  Value: JSON list of entries:
         {
           "natural_query":  str,
           "parsed_json":    str,   ← model_dump_json() of Pydantic object
           "schema_type":    "sql" | "mongo",
           "embedding":      [float, ...]
         }

No external dependencies beyond google-genai and redis.
Cosine similarity is computed in pure Python — no numpy required.
"""

import asyncio
import json
import logging

from config import settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "qs:sem"


# ---------------------------------------------------------------------------
# Pure-Python cosine similarity (no numpy)
# ---------------------------------------------------------------------------

def _cosine(a: list[float], b: list[float]) -> float:
    dot   = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    return dot / (mag_a * mag_b) if mag_a and mag_b else 0.0


# ---------------------------------------------------------------------------
# SemanticCache — the proxy layer
# ---------------------------------------------------------------------------

class SemanticCache:
    """
    Intercepts natural-language queries and serves cached parsed results
    when an incoming query is semantically equivalent (sim >= threshold)
    to a previously successful query within the same session + db_type.
    """

    def __init__(
        self,
        redis_client,
        genai_client,
        session_id: str,
        db_type: str,
        threshold: float | None = None,
    ) -> None:
        self._redis     = redis_client
        self._client    = genai_client
        self._key       = f"{_KEY_PREFIX}:{session_id}:{db_type}"
        self._db_type   = db_type
        self._threshold = threshold if threshold is not None else settings.sem_cache_threshold

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def lookup(self, natural_query: str):
        """
        Embed the query and compare against the cache.

        Returns:
            SQLQueryResponse | MongoQueryResponse  if cache HIT
            None                                   if cache MISS
        """
        embedding = await self._embed(natural_query)
        entries   = await self._load_entries()

        best_sim    = 0.0
        best_parsed = None

        for entry in entries:
            sim = _cosine(embedding, entry["embedding"])
            if sim > best_sim:
                best_sim    = sim
                best_parsed = entry

        if best_sim >= self._threshold and best_parsed:
            logger.info(
                "SEM CACHE HIT: sim=%.4f key=%s query='%.60s'",
                best_sim, self._key, natural_query,
            )
            return self._deserialise(best_parsed)

        logger.info(
            "SEM CACHE MISS: best_sim=%.4f key=%s query='%.60s'",
            best_sim, self._key, natural_query,
        )
        return None

    async def store(self, natural_query: str, parsed) -> None:
        """
        Embed and persist a successfully executed parsed query result.
        Only call this after a confirmed successful DB execution.
        """
        from schemas import SQLQueryResponse

        embedding   = await self._embed(natural_query)
        schema_type = "sql" if isinstance(parsed, SQLQueryResponse) else "mongo"

        entry = {
            "natural_query": natural_query,
            "parsed_json":   parsed.model_dump_json(),
            "schema_type":   schema_type,
            "embedding":     embedding,
        }

        pipe = self._redis.pipeline()
        pipe.rpush(self._key, json.dumps(entry))
        pipe.ltrim(self._key, -settings.cache_max_entries, -1)
        pipe.expire(self._key, settings.cache_ttl_seconds)
        await pipe.execute()

        logger.info(
            "SEM CACHE STORED: schema=%s key=%s query='%.60s'",
            schema_type, self._key, natural_query,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _embed(self, text: str) -> list[float]:
        """Call Gemini embedding API on a thread (sync SDK → non-blocking)."""
        response = await asyncio.to_thread(
            self._client.models.embed_content,
            model=settings.gemini_embed_model,
            contents=text,
        )
        return list(response.embeddings[0].values)

    async def _load_entries(self) -> list[dict]:
        raw_list = await self._redis.lrange(self._key, 0, -1)
        entries = []
        for raw in raw_list:
            try:
                entries.append(json.loads(raw))
            except (json.JSONDecodeError, KeyError):
                pass
        return entries

    @staticmethod
    def _deserialise(entry: dict):
        from schemas import SQLQueryResponse, MongoQueryResponse
        if entry["schema_type"] == "sql":
            return SQLQueryResponse.model_validate_json(entry["parsed_json"])
        return MongoQueryResponse.model_validate_json(entry["parsed_json"])
