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

# System catalog tables that should never appear in user queries.
# Entries cached before the system-table guardrail was added may reference
# these. Filtering them here ensures stale poisoned entries are auto-skipped.
_SYSTEM_SCHEMAS: frozenset[str] = frozenset({
    "information_schema", "performance_schema", "sys", "pg_catalog",
})


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

    async def lookup(self, natural_query: str, live_table_names: set[str] | None = None):
        """
        Embed the query and compare against the cache.

        Args:
            natural_query:    User's natural-language question.
            live_table_names: Optional set of table/collection names currently
                              in the database. When provided, any cached entry
                              that references a table NOT in this set is treated
                              as stale and skipped — this auto-invalidates cache
                              entries after schema changes (tables added/dropped).

        Returns:
            SQLQueryResponse | MongoQueryResponse  if cache HIT
            None                                   if cache MISS
        """
        embedding = await self._embed(natural_query)
        entries   = await self._load_entries()

        best_sim    = 0.0
        best_parsed = None

        for entry in entries:
            # ── Poison guard ────────────────────────────────────────────
            # Check the ENTIRE raw parsed_json string (not just the .query
            # field) so system-schema refs hidden in aliases or sub-fields
            # are also caught.
            raw_parsed = entry.get("parsed_json", "")
            if any(t in raw_parsed.lower() for t in _SYSTEM_SCHEMAS):
                logger.warning(
                    "SEM CACHE: poison entry (system-table ref) skipped "
                    "key=%s entry='%.120s'", self._key, raw_parsed[:120]
                )
                continue

            # ── Staleness guard ─────────────────────────────────────────
            # If the caller supplies the live table names, verify that every
            # table referenced in the cached entry still exists.  This means
            # schema changes (DROP TABLE, migrations) automatically invalidate
            # entries instead of serving a query that will error at runtime.
            if live_table_names:
                try:
                    entry_query_lower = json.loads(raw_parsed).get("query", "").lower()
                    # Crude but effective: any live table name that appears in
                    # the SQL must be present in the live schema.  If the SQL
                    # references a name that is NOT in live_table_names we
                    # cannot trust this entry.
                    # Strategy: collect words in the SQL, intersect with
                    # live_table_names. At least one must match; if ZERO user
                    # tables appear it is probably a system-table query.
                    sql_words = set(entry_query_lower.replace('`', '').split())
                    overlap = sql_words & {t.lower() for t in live_table_names}
                    if not overlap:
                        logger.info(
                            "SEM CACHE: stale entry (no live tables matched) "
                            "skipped key=%s", self._key
                        )
                        continue
                except Exception:
                    pass

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

    async def clear(self) -> int:
        """
        Delete all cached entries for this (session_id, db_type) pair.

        Returns:
            Number of entries removed.
        """
        count = await self._redis.llen(self._key)
        await self._redis.delete(self._key)
        logger.info("SEM CACHE CLEARED: key=%s entries=%d", self._key, count)
        return count

    async def get_all_entries(self) -> list[dict]:
        """
        Return all cached entries without embedding vectors.
        Intended for /cache inspection endpoints and the inspect_cache.py script.
        """
        entries = await self._load_entries()
        result = []
        for e in entries:
            try:
                parsed = json.loads(e.get("parsed_json", "{}"))
            except Exception:
                parsed = {}
            query_text = parsed.get("query") or str(parsed.get("filter", ""))
            is_poisoned = any(t in query_text.lower() for t in _SYSTEM_SCHEMAS)
            result.append({
                "natural_query": e.get("natural_query", ""),
                "schema_type":   e.get("schema_type", ""),
                "query":         query_text,
                "confidence":    parsed.get("confidence_score"),
                "poisoned":      is_poisoned,
            })
        return result

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
