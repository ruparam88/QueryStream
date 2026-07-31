# QueryStream Backend — Refactoring & Configuration Reference

## Project Structure

```
backend/
├── config.py                  ← ★ Central config (Pydantic BaseSettings)
├── main.py                    ← FastAPI app, lifespan, endpoints
├── agent.py                   ← ChatManager: async, stateless, cache-aware
├── graph.py                   ← LangGraph self-healing state machine
├── schemas.py                 ← Pydantic output schemas (SQL + Mongo)
│
├── db/
│   ├── connections.py         ← Async engine/client factories
│   └── repository.py         ← BaseRepository + SQL/Mongo implementations
│
├── session/
│   └── redis_manager.py      ← RedisSessionManager + FastAPI Depends provider
│
├── cache/
│   └── semantic_cache.py     ← Cosine-similarity proxy (Gemini embeddings)
│
├── tests/
│   ├── conftest.py
│   └── test_querystream.py   ← 28 async tests (T1–T10)
│
├── .env                       ← Local dev values (gitignored)
├── .env.example               ← Template with full documentation
├── requirements.txt
└── pytest.ini
```

## Configuration Reference (`config.py`)

All values are loaded by `pydantic-settings` from environment variables **or** `.env`.
Environment variables always take precedence. Import anywhere with:

```python
from config import settings
settings.gemini_model   # "gemini-2.5-flash"
```

### Gemini / LLM

| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | `""` | Google AI Studio API key (**required** for queries) |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model for structured query generation |
| `GEMINI_EMBED_MODEL` | `text-embedding-004` | Model for semantic cache embeddings |

### Redis

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379` | Redis URI — supports `redis://` and `rediss://` (TLS) |
| `SESSION_TTL_SECONDS` | `3600` | Session key TTL (1 hour) |

### Semantic Cache

| Variable | Default | Description |
|---|---|---|
| `SEM_CACHE_THRESHOLD` | `0.95` | Cosine similarity cutoff for a cache hit |
| `CACHE_TTL_SECONDS` | `86400` | Cache entry TTL (24 hours) |
| `CACHE_MAX_ENTRIES` | `200` | Max entries per (session, db_type) list |

### Self-Healing Graph

| Variable | Default | Description |
|---|---|---|
| `MAX_QUERY_ATTEMPTS` | `3` | Max LLM+DB attempts before escalating |

### Application Server

| Variable | Default | Description |
|---|---|---|
| `APP_HOST` | `0.0.0.0` | Uvicorn bind host |
| `APP_PORT` | `8000` | Uvicorn bind port |
| `DEBUG` | `false` | Auto-reload + verbose output |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `CORS_ORIGINS` | `*` | Comma-separated allowed origins |

## Architecture Layers

```
POST /chat
   │
   ├─ FastAPI DI (Depends) ──► RedisSessionManager.load(session_id)
   │                            ▼
   ├─ ChatManager(session_data, redis_client, session_id)
   │    │
   │    ├─ AWAITING_DB_TYPE      → _handle_db_type()
   │    ├─ AWAITING_HOSTING_TYPE → _handle_hosting_type()
   │    ├─ AWAITING_URI          → _handle_uri() [async ping]
   │    └─ CONNECTED             → _handle_query()
   │         │
   │         ├─ 1. SemanticCache.lookup()      ← Redis list + cosine sim
   │         │       HIT  → repo.execute_query(cached_parsed)   [0 LLM]
   │         │       MISS ↓
   │         ├─ 2. run_query_graph_async()      ← LangGraph ainvoke
   │         │         GENERATING (Gemini structured output)
   │         │         EXECUTING  (BaseRepository.execute_query)
   │         │         HEALING    (retry / escalate)
   │         └─ 3. SemanticCache.store()        ← on success only
   │
   └─ RedisSessionManager.save(session_id, manager.to_dict())
```

## Config Removed From Each File

| File | Was | Now |
|---|---|---|
| `agent.py` | `os.environ.get("GEMINI_API_KEY")`, `load_dotenv()`, `_MODEL = "gemini-2.5-flash"` | `settings.gemini_api_key`, `settings.gemini_model` |
| `graph.py` | `MAX_ATTEMPTS = 3` (hardcoded) | `settings.max_query_attempts` |
| `cache/semantic_cache.py` | 4× `os.environ.get(...)` + 4 module-level constants | `settings.sem_cache_threshold`, `.cache_ttl_seconds`, `.cache_max_entries`, `.gemini_embed_model` |
| `session/redis_manager.py` | `os.environ.get("REDIS_URL")`, `os.environ.get("SESSION_TTL_SECONDS")` | `settings.redis_url`, `settings.session_ttl_seconds` |
| `main.py` | `os.environ.get("REDIS_URL")` in lifespan, hardcoded `allow_origins=["*"]` | `settings.redis_url`, `settings.cors_origins_list` |

> [!TIP]
> To override any setting in production, just set the environment variable — no `.env` file needed.
> Docker: `ENV GEMINI_API_KEY=...` | K8s: `envFrom: secretRef`

> [!NOTE]
> `settings` is an `lru_cache`d singleton. In tests, override values using `os.environ` or monkeypatch before import, or call `get_settings.cache_clear()` to reload.
