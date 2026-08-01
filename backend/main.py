"""
QueryStream FastAPI application — stateless, async, horizontally scalable.

v5: uses central config (config.py) — no scattered os.environ.get calls.
    Removed stale imports: init_redis_pool, close_redis_pool.
    CORS origins now configurable via settings.cors_origins_list.
"""

import logging
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import settings
from agent import ChatManager
from session.redis_manager import RedisSessionManager, get_session_manager
from streaming import stream_query_events
from cache.semantic_cache import SemanticCache
from db.connections import make_async_sql_engine, make_motor_client
from db.repository import SQLRepository, MongoRepository

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan — manages the Redis connection pool
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis_pool = aioredis.ConnectionPool.from_url(
        settings.redis_url, decode_responses=True
    )
    logger.info("Redis pool created: %s", settings.redis_url.split("@")[-1])
    yield
    await app.state.redis_pool.disconnect()
    logger.info("Redis pool closed.")


app = FastAPI(
    title="QueryStream",
    description="AI-powered natural language database query engine.",
    version="5.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    session_id: str
    message: str
    is_option: Optional[bool] = False


class ChatResponse(BaseModel):
    reply: str
    options: Optional[List[str]] = None
    state: str
    db_type: Optional[str] = None
    data: Optional[List[Dict[str, Any]]] = None
    query: Optional[str] = None
    cache_hit: Optional[bool] = None


# ---------------------------------------------------------------------------
# Chat endpoint — stateless, DI-injected session manager (JSON response)
# ---------------------------------------------------------------------------

@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(
    request: ChatRequest,
    session_manager: RedisSessionManager = Depends(get_session_manager),
) -> ChatResponse:
    """
    Stateless chat endpoint.

    1. Load session state from Redis.
    2. Instantiate ChatManager from that state + Redis client.
    3. Process the user message (fully async, non-blocking).
    4. Persist updated state back to Redis.
    5. Return response.
    """
    session_data = await session_manager.load(request.session_id)
    manager = ChatManager(
        session_data,
        redis_client=session_manager._client,
        session_id=request.session_id,
    )

    try:
        response = await manager.process_message(request.message, is_option=bool(request.is_option))
        await session_manager.save(request.session_id, manager.to_dict())
        return ChatResponse(**response)
    except Exception as exc:
        logger.error("chat_endpoint error: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Streaming endpoint — SSE, real-time graph events
# ---------------------------------------------------------------------------

@app.post("/chat/stream")
async def chat_stream_endpoint(
    request: ChatRequest,
    session_manager: RedisSessionManager = Depends(get_session_manager),
) -> StreamingResponse:
    """
    Real-time SSE endpoint.

    For non-CONNECTED states (setup flow) wraps the response in an SSE
    envelope so the frontend only needs one code path.
    For CONNECTED state, streams graph node events as they happen:
      query → executing → result / error / healing → done
      cache_hit → result → done  (0 LLM calls on a cache hit)

    Wire format — each line:   data: <json>\\n\\n
    Final line always:          data: {"event": "done"}\\n\\n
    """
    import json

    async def _generate():
        session_data = await session_manager.load(request.session_id)
        manager = ChatManager(
            session_data,
            redis_client=session_manager._client,
            session_id=request.session_id,
        )

        msg_clean = request.message.strip().lower()
        _db_type_keywords = {"postgresql", "mysql", "mongodb", "postgres", "mongo"}
        # Route to setup flow if: explicit /reset, OR a db-type keyword typed while
        # already past AWAITING_DB_TYPE, OR is_option flag set by the frontend.
        is_reset_or_option = (
            msg_clean == "/reset"
            or request.is_option
            or (msg_clean in _db_type_keywords and manager.state != "AWAITING_DB_TYPE")
        )

        # Non-CONNECTED states or intelligent option/reset interventions
        if manager.state != "CONNECTED" or is_reset_or_option:
            response = await manager.process_message(request.message, is_option=bool(request.is_option))
            await session_manager.save(request.session_id, manager.to_dict())
            yield f"data: {json.dumps({'event': 'message', **response}, default=str)}\n\n"
            yield f"data: {json.dumps({'event': 'done'})}\n\n"
            return

        if not manager.genai_client:
            yield f"data: {json.dumps({'event': 'stream_error', 'error': 'No GEMINI_API_KEY configured.'})}\n\n"
            yield f"data: {json.dumps({'event': 'done'})}\n\n"
            return

        # Build repository from stored URI
        if manager.db_type in ("PostgreSQL", "MySQL"):
            engine     = make_async_sql_engine(manager.db_type, manager.uri)
            repository = SQLRepository(engine)
        else:
            mongo_cl   = make_motor_client(manager.uri)
            db_name    = manager.uri.split("/")[-1].split("?")[0] or "test"
            repository = MongoRepository(mongo_cl[db_name])

        cache = SemanticCache(
            redis_client=session_manager._client,
            genai_client=manager.genai_client,
            session_id=request.session_id,
            db_type=manager.db_type,
        ) if session_manager._client else None

        try:
            async for event_str in stream_query_events(
                natural_query=request.message,
                db_type=manager.db_type,
                genai_client=manager.genai_client,
                model=settings.gemini_model,
                repository=repository,
                cache=cache,
            ):
                yield event_str
        finally:
            if manager.db_type in ("PostgreSQL", "MySQL"):
                await engine.dispose()
            else:
                mongo_cl.close()

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",      # disable Nginx buffering
            "Connection":       "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Health check — verifies Redis connectivity and reports config summary
# ---------------------------------------------------------------------------

@app.get("/health")
async def health(
    session_manager: RedisSessionManager = Depends(get_session_manager),
):
    redis_ok = False
    try:
        await session_manager._client.ping()
        redis_ok = True
    except Exception:
        pass

    return {
        "status":       "ok" if redis_ok else "degraded",
        "redis":        "connected" if redis_ok else "unreachable",
        "model":        settings.gemini_model,
        "embed_model":  settings.gemini_embed_model,
        "cache_threshold": settings.sem_cache_threshold,
        "max_attempts": settings.max_query_attempts,
        "version":      "5.0.0",
    }


# ---------------------------------------------------------------------------
# Cache management endpoints
# ---------------------------------------------------------------------------

@app.delete("/cache/{session_id}")
async def clear_semantic_cache(
    session_id: str,
    db_type: str = "MySQL",
    session_manager: RedisSessionManager = Depends(get_session_manager),
) -> dict:
    """
    Flush the semantic query cache for a specific session + db_type pair.

    Use this to force LLM regeneration after a bad/stale result was cached.
    The next identical query will go through the LLM and a fresh entry will
    be stored.

    Query params:
        db_type: "PostgreSQL" | "MySQL" | "MongoDB"  (default: MySQL)
    """
    cache = SemanticCache(
        redis_client=session_manager._client,
        genai_client=None,      # not needed for clear() — only _embed() uses it
        session_id=session_id,
        db_type=db_type,
    )
    count = await cache.clear()
    logger.info(
        "Cache cleared via API: session=%s db_type=%s entries=%d",
        session_id, db_type, count,
    )
    return {
        "cleared":         True,
        "session_id":      session_id,
        "db_type":         db_type,
        "entries_removed": count,
    }


@app.get("/cache/{session_id}")
async def inspect_semantic_cache(
    session_id: str,
    db_type: str = "MySQL",
    session_manager: RedisSessionManager = Depends(get_session_manager),
) -> dict:
    """
    Inspect all entries in the semantic cache for a session + db_type pair.

    Returns the natural-language query, generated SQL, confidence score, and
    whether the entry is "poisoned" (references system catalog tables).

    Query params:
        db_type: "PostgreSQL" | "MySQL" | "MongoDB"  (default: MySQL)
    """
    cache = SemanticCache(
        redis_client=session_manager._client,
        genai_client=None,
        session_id=session_id,
        db_type=db_type,
    )
    entries = await cache.get_all_entries()
    return {
        "session_id": session_id,
        "db_type":    db_type,
        "count":      len(entries),
        "entries":    entries,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )
