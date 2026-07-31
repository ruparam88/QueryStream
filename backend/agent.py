"""
QueryStream agent — async, stateless per-request coordinator.

v5: uses central config (config.py) — no scattered os.environ.get calls.

Lifecycle per request:
  1. RedisSessionManager.load(session_id)       → session dict
  2. ChatManager(session_data, redis, sid)      → populate self.*
  3. await manager.process_message(msg)
       CONNECTED state:
         a. SemanticCache.lookup(msg)           → HIT → execute cached
         b. MISS → run_query_graph_async()
         c. SemanticCache.store(msg, _parsed)   → persist on success
  4. RedisSessionManager.save(session_id, ...)  → Redis
"""

import logging

from google import genai

from config import settings
from db.connections import make_async_sql_engine, make_motor_client
from db.repository import SQLRepository, MongoRepository
from graph import run_query_graph_async
from cache.semantic_cache import SemanticCache

logger = logging.getLogger(__name__)


class ChatManager:
    """
    Per-request coordinator. Holds session state loaded from Redis.
    Accepts an optional redis_client for semantic cache operations.
    Call to_dict() after process_message() to persist back to Redis.
    """

    def __init__(
        self,
        session_data: dict,
        redis_client=None,
        session_id: str = "",
    ) -> None:
        self.state        = session_data.get("conv_state", "AWAITING_DB_TYPE")
        self.db_type      = session_data.get("db_type")
        self.hosting_type = session_data.get("hosting_type")
        self.uri          = session_data.get("uri")

        self._redis      = redis_client
        self._session_id = session_id

        self.genai_client = (
            genai.Client(api_key=settings.gemini_api_key)
            if settings.gemini_api_key else None
        )

    def to_dict(self) -> dict:
        """Serialisable snapshot for Redis storage."""
        return {
            "conv_state":   self.state,
            "db_type":      self.db_type,
            "hosting_type": self.hosting_type,
            "uri":          self.uri,
        }

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def process_message(self, message: str) -> dict:
        dispatch = {
            "AWAITING_DB_TYPE":      self._handle_db_type,
            "AWAITING_HOSTING_TYPE": self._handle_hosting_type,
            "AWAITING_URI":          self._handle_uri,
            "CONNECTED":             self._handle_query,
        }
        handler = dispatch.get(self.state)
        if handler:
            return await handler(message)
        self.state = "AWAITING_DB_TYPE"
        return {
            "reply": "Unexpected state. Let's restart — what database type?",
            "options": ["PostgreSQL", "MySQL", "MongoDB"],
            "state": self.state,
        }

    # ------------------------------------------------------------------
    # Conversation state handlers
    # ------------------------------------------------------------------

    async def _handle_db_type(self, message: str) -> dict:
        msg = message.lower().strip()
        if "postgres" in msg:
            self.db_type = "PostgreSQL"
        elif "mysql" in msg or "sql" in msg:
            self.db_type = "MySQL"
        elif "mongo" in msg:
            self.db_type = "MongoDB"
        else:
            return {
                "reply": "I didn't recognise that. Please choose from PostgreSQL, MySQL, or MongoDB.",
                "options": ["PostgreSQL", "MySQL", "MongoDB"],
                "state": self.state,
            }
        self.state = "AWAITING_HOSTING_TYPE"
        return {
            "reply": f"Great! You selected {self.db_type}. Is this database hosted locally or in the cloud?",
            "options": ["Local", "Cloud"],
            "state": self.state,
        }

    async def _handle_hosting_type(self, message: str) -> dict:
        msg = message.lower().strip()
        if "local" in msg:
            self.hosting_type = "Local"
        elif "cloud" in msg:
            self.hosting_type = "Cloud"
        else:
            return {
                "reply": "Please select either Local or Cloud.",
                "options": ["Local", "Cloud"],
                "state": self.state,
            }

        examples = {
            ("PostgreSQL", "Local"):  "postgresql://user:password@localhost:5432/dbname",
            ("PostgreSQL", "Cloud"):  "postgresql://user:password@cloud-host:5432/dbname",
            ("MySQL", "Local"):       "mysql://user:password@localhost:3306/dbname",
            ("MySQL", "Cloud"):       "mysql://user:password@cloud-host:3306/dbname",
            ("MongoDB", "Local"):     "mongodb://localhost:27017/",
            ("MongoDB", "Cloud"):     "mongodb+srv://user:password@cluster.mongodb.net/",
        }
        self.state = "AWAITING_URI"
        return {
            "reply": (
                f"Understood. Please provide the connection URI for your "
                f"{self.hosting_type} {self.db_type} database.\n"
                f"Example: `{examples.get((self.db_type, self.hosting_type), '')}`"
            ),
            "state": self.state,
        }

    async def _handle_uri(self, message: str) -> dict:
        self.uri = message.strip()

        try:
            if self.db_type in ("PostgreSQL", "MySQL"):
                engine = make_async_sql_engine(self.db_type, self.uri)
                repo = SQLRepository(engine)
                await repo.ping()
                await engine.dispose()
            else:
                client = make_motor_client(self.uri)
                db_name = self.uri.split("/")[-1].split("?")[0] or "test"
                repo = MongoRepository(client[db_name])
                await repo.ping()
                client.close()

            self.state = "CONNECTED"
            logger.info("DB connected: type=%s hosting=%s", self.db_type, self.hosting_type)
            return {
                "reply": f"Successfully connected to the {self.db_type} database! What would you like to query?",
                "state": self.state,
                "db_type": self.db_type,
            }

        except Exception as exc:
            logger.warning("Connection failed: type=%s error=%s", self.db_type, type(exc).__name__)
            return {
                "reply": f"Failed to connect. Error: {exc}\nPlease check your URI and try again.",
                "state": self.state,
            }

    # ------------------------------------------------------------------
    # Query: Semantic Cache Proxy → LangGraph (on miss)
    # ------------------------------------------------------------------

    async def _handle_query(self, message: str) -> dict:
        if not self.genai_client:
            return {
                "reply": "No GEMINI_API_KEY configured. Please set it in your .env and restart.",
                "state": self.state,
                "db_type": self.db_type,
            }

        if self.db_type in ("PostgreSQL", "MySQL"):
            engine = make_async_sql_engine(self.db_type, self.uri)
            repository = SQLRepository(engine)
        else:
            mongo_client = make_motor_client(self.uri)
            db_name = self.uri.split("/")[-1].split("?")[0] or "test"
            repository = MongoRepository(mongo_client[db_name])

        cache: SemanticCache | None = None
        if self._redis and self.genai_client:
            cache = SemanticCache(
                redis_client=self._redis,
                genai_client=self.genai_client,
                session_id=self._session_id,
                db_type=self.db_type,
            )

        try:
            # ── Phase 1: Cache lookup ─────────────────────────────────
            if cache:
                cached_parsed = await cache.lookup(message)
                if cached_parsed is not None:
                    data = await repository.execute_query(cached_parsed)
                    return {
                        "reply":     "Here are the results for your query (cached):" if data
                                     else "Query returned no results (cached).",
                        "query":     getattr(cached_parsed, "query", str(getattr(cached_parsed, "filter", ""))),
                        "data":      data,
                        "state":     self.state,
                        "db_type":   self.db_type,
                        "cache_hit": True,
                    }

            # ── Phase 2: Full LangGraph execution (cache miss) ────────
            graph_result = await run_query_graph_async(
                natural_query=message,
                db_type=self.db_type,
                genai_client=self.genai_client,
                model=settings.gemini_model,
                repository=repository,
            )

            # ── Phase 3: Store on success ─────────────────────────────
            if cache and graph_result.get("_parsed") and graph_result.get("data") is not None:
                await cache.store(message, graph_result["_parsed"])

            return {
                "reply":   graph_result["reply"],
                "query":   graph_result.get("query"),
                "data":    graph_result.get("data"),
                "state":   self.state,
                "db_type": self.db_type,
            }

        except Exception as exc:
            logger.error("Query handler error: %s", type(exc).__name__)
            return {
                "reply": f"Unexpected error: {exc}",
                "state": self.state,
                "db_type": self.db_type,
            }
        finally:
            if self.db_type in ("PostgreSQL", "MySQL"):
                await engine.dispose()
            else:
                mongo_client.close()
