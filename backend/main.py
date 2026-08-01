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
        response = await manager.process_message(request.message)
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

        # Non-CONNECTED states: single event — no graph streaming needed
        if manager.state != "CONNECTED":
            response = await manager.process_message(request.message)
            await session_manager.save(request.session_id, manager.to_dict())
            yield f"data: {json.dumps({'event': 'message', **response})}\n\n"
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )
