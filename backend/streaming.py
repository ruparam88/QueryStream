"""
streaming.py — SSE event generator for the /chat/stream endpoint.

Observer / Pub-Sub pattern:
  graph.astream() is the observable (emits state after each node).
  stream_query_events() is the observer (maps node output → SSE events).

SSE wire format (text/event-stream):
  data: <json string>\n\n

Each JSON payload has an "event" field so the frontend can switch on it:

  { "event": "thinking",  "attempt": 1 }
  { "event": "query",     "query": "SELECT ...", "attempt": 1 }
  { "event": "executing" }
  { "event": "result",    "reply": "...", "data": [...], "query": "..." }
  { "event": "cache_hit", "query": "...", "reply": "Cache hit" }
  { "event": "error",     "error": "column ... does not exist", "attempt": 1 }
  { "event": "healing",   "attempt": 2 }
  { "event": "escalated", "reply": "..." }
  { "event": "done" }

On error (exception during streaming):
  { "event": "stream_error", "error": "<message>" }
  { "event": "done" }

The frontend reconnects or shows the error message on "stream_error".
"""

import json
import logging
from typing import AsyncGenerator, Any

from graph import execution_graph
from db.repository import BaseRepository
from cache.semantic_cache import SemanticCache

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SSE helper
# ---------------------------------------------------------------------------

def _sse(payload: dict) -> str:
    """Format a dict as a single SSE data line."""
    return f"data: {json.dumps(payload)}\n\n"


# ---------------------------------------------------------------------------
# Public async generator — consumed by StreamingResponse
# ---------------------------------------------------------------------------

async def stream_query_events(
    natural_query: str,
    db_type: str,
    genai_client: Any,
    model: str,
    repository: BaseRepository,
    cache: SemanticCache | None = None,
) -> AsyncGenerator[str, None]:
    """
    Yields SSE-formatted strings as the graph executes.
    Integrates the semantic cache proxy: HIT → skip graph entirely.

    Args:
        natural_query:  User's natural language question.
        db_type:        "PostgreSQL" | "MySQL" | "MongoDB"
        genai_client:   Initialised google.genai.Client.
        model:          Gemini model ID.
        repository:     Async DB accessor (SQLRepository or MongoRepository).
        cache:          Optional SemanticCache; None = caching disabled.
    """
    try:
        # ── Phase 1: Semantic cache lookup ───────────────────────────
        if cache:
            cached_parsed = await cache.lookup(natural_query)
            if cached_parsed is not None:
                query_str = getattr(cached_parsed, "query",
                                    str(getattr(cached_parsed, "filter", "")))
                yield _sse({"event": "cache_hit", "query": query_str})

                data = await repository.execute_query(cached_parsed)
                reply = ("Here are the results for your query (cached):"
                         if data else "Query returned no results (cached).")
                yield _sse({"event": "result", "reply": reply, "data": data, "query": query_str})
                yield _sse({"event": "done"})
                return

        # ── Phase 2: LangGraph execution — stream node-by-node ───────
        from config import settings

        initial = {
            "natural_query": natural_query,
            "db_type":       db_type,
            "repository":    repository,
            "genai_client":  genai_client,
            "model":         model,
            "attempt":       0,
            "last_query":    "",
            "last_error":    "",
            "result_data":   None,
            "final_reply":   "",
            "node":          "GENERATING",
            "_parsed":       None,
        }

        final_state = None

        async for chunk in execution_graph.astream(initial):
            node_name  = next(iter(chunk))          # e.g. "GENERATING"
            node_state = chunk[node_name]            # state dict after node
            final_state = node_state                 # track for cache store

            if node_name == "GENERATING":
                next_node = node_state.get("node")

                if next_node == "ESCALATED":
                    # Destructive-op guard fired inside GENERATING
                    yield _sse({
                        "event":   "escalated",
                        "reply":   node_state["final_reply"],
                        "query":   node_state.get("last_query"),
                        "attempt": node_state["attempt"] + 1,
                    })

                else:
                    # Normal generation — tell the client the query is ready
                    yield _sse({
                        "event":   "query",
                        "query":   node_state.get("last_query", ""),
                        "attempt": node_state["attempt"] + 1,
                    })
                    yield _sse({"event": "executing"})

            elif node_name == "EXECUTING":
                next_node = node_state.get("node")

                if next_node == "SUCCESS":
                    yield _sse({
                        "event":   "result",
                        "reply":   node_state["final_reply"],
                        "data":    node_state.get("result_data") or [],
                        "query":   node_state.get("last_query"),
                        "attempt": node_state["attempt"] + 1,
                    })

                else:
                    # HEALING — execution failed
                    yield _sse({
                        "event":   "error",
                        "error":   node_state.get("last_error", "Unknown DB error"),
                        "attempt": node_state["attempt"] + 1,
                    })

            elif node_name == "HEALING":
                next_node = node_state.get("node")

                if next_node == "ESCALATED":
                    yield _sse({
                        "event":   "escalated",
                        "reply":   node_state["final_reply"],
                        "attempt": node_state["attempt"] + 1,
                    })

                else:
                    yield _sse({
                        "event":   "healing",
                        "attempt": node_state["attempt"] + 1,
                    })

        # ── Phase 3: Store successful result in semantic cache ────────
        if (
            cache
            and final_state
            and final_state.get("_parsed") is not None
            and final_state.get("result_data") is not None
        ):
            await cache.store(natural_query, final_state["_parsed"])
            logger.info("Streaming: cached result for query='%.60s'", natural_query)

    except Exception as exc:
        logger.exception("stream_query_events error: %s", type(exc).__name__)
        yield _sse({"event": "stream_error", "error": str(exc)})

    finally:
        yield _sse({"event": "done"})
