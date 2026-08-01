"""
Step 4 — Updated unit tests for async QueryStream architecture.

Changes from previous version:
- All graph tests are now async (pytest-asyncio).
- `engine` / `mongo_db` mocks replaced by `BaseRepository` mock.
- `run_query_graph_async` replaces `run_query_graph`.
- Schema / validation tests unchanged (sync, no DB involved).

Coverage:
  T1  Schema parsing — valid JSON round-trips without markdown stripping.
  T2  Schema validation — bad JSON / out-of-range fields raise ValidationError.
  T3  Happy path  — graph reaches SUCCESS on first attempt (SQL & Mongo).
  T4  Retry counter — repo raises on attempt 1 → HEALING increments counter
                       → regenerates → succeeds on attempt 2.
  T5  Circuit breaker — 3 consecutive errors → ESCALATED, clean reply.
  T6  Destructive guard — requires_destructive_operation=True → ESCALATED,
                           repo.execute_query never called.
  T7  RedisSessionManager — load/save/missing key round-trips.
  T8  ChatManager — to_dict / from-dict state reconstruction.
"""

import asyncio
import json
import sys
import os
import pytest
import pytest_asyncio
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from schemas import SQLQueryResponse, MongoQueryResponse
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# Canonical JSON payloads
# ---------------------------------------------------------------------------

VALID_SQL_JSON = json.dumps({
    "thought_process": "Select all rows from users table.",
    "query": "SELECT * FROM users LIMIT 10",
    "confidence_score": 0.95,
    "requires_destructive_operation": False,
})

DESTRUCTIVE_SQL_JSON = json.dumps({
    "thought_process": "Drop the users table.",
    "query": "DROP TABLE users",
    "confidence_score": 0.99,
    "requires_destructive_operation": True,
})

VALID_MONGO_JSON = json.dumps({
    "thought_process": "Find all active users.",
    "collection": "users",
    "filter": {"status": "active"},
    "confidence_score": 0.88,
    "requires_destructive_operation": False,
})

REPAIRED_SQL_JSON = json.dumps({
    "thought_process": "Fixed column name typo: usrs → users.",
    "query": "SELECT id, name FROM users WHERE active = 1",
    "confidence_score": 0.90,
    "requires_destructive_operation": False,
})


def _llm_response(json_text: str) -> MagicMock:
    r = MagicMock()
    r.text = json_text
    return r


def _mock_repo(rows=None, raises=None) -> MagicMock:
    """
    Build an AsyncMock BaseRepository.
    - rows: list of dicts returned by execute_query
    - raises: exception raised by execute_query
    """
    repo = MagicMock()
    if raises:
        repo.execute_query = AsyncMock(side_effect=raises)
    else:
        repo.execute_query = AsyncMock(return_value=rows or [{"id": 1, "name": "Alice"}])
    repo.ping = AsyncMock()
    repo.get_schema_context = AsyncMock(return_value="Table: users (columns: id [int], name [varchar])")
    return repo


def _base_initial(db_type="PostgreSQL", repo=None, client=None) -> dict:
    from graph import QueryState
    return {
        "natural_query": "show me all users",
        "db_type": db_type,
        "repository": repo or _mock_repo(),
        "genai_client": client or MagicMock(),
        "model": "gemini-2.5-flash",
        "schema_context": "Table: users (columns: id [int], name [varchar])",
        "attempt": 0,
        "last_query": "",
        "last_error": "",
        "result_data": None,
        "final_reply": "",
        "node": "GENERATING",
        "_parsed": None,
    }


# ===========================================================================
# T1 — Schema parsing
# ===========================================================================

class TestSchemaParsing:

    def test_sql_schema_parses_valid_json(self):
        obj = SQLQueryResponse.model_validate_json(VALID_SQL_JSON)
        assert obj.query == "SELECT * FROM users LIMIT 10"
        assert obj.confidence_score == 0.95
        assert obj.requires_destructive_operation is False

    def test_mongo_schema_parses_valid_json(self):
        obj = MongoQueryResponse.model_validate_json(VALID_MONGO_JSON)
        assert obj.collection == "users"
        assert obj.filter == {"status": "active"}
        assert obj.requires_destructive_operation is False

    def test_no_markdown_stripping_needed(self):
        obj = SQLQueryResponse.model_validate_json(VALID_SQL_JSON)
        assert obj.query.startswith("SELECT")


# ===========================================================================
# T2 — Schema validation
# ===========================================================================

class TestSchemaValidation:

    def test_confidence_out_of_range_raises(self):
        bad = json.dumps({
            "thought_process": "x", "query": "SELECT 1",
            "confidence_score": 1.5,
            "requires_destructive_operation": False,
        })
        with pytest.raises(ValidationError):
            SQLQueryResponse.model_validate_json(bad)

    def test_missing_required_field_raises(self):
        bad = json.dumps({
            "thought_process": "x", "query": "SELECT 1",
            "requires_destructive_operation": False,
        })
        with pytest.raises(ValidationError):
            SQLQueryResponse.model_validate_json(bad)

    def test_malformed_json_raises(self):
        with pytest.raises(Exception):
            SQLQueryResponse.model_validate_json("{not valid json}")


# ===========================================================================
# T3 — Happy path (async)
# ===========================================================================

class TestHappyPath:

    async def test_sql_happy_path(self):
        from graph import run_query_graph_async

        client = MagicMock()
        repo = _mock_repo(rows=[{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}])

        with patch("asyncio.to_thread", new=AsyncMock(return_value=_llm_response(VALID_SQL_JSON))):
            result = await run_query_graph_async(
                natural_query="show me all users",
                db_type="PostgreSQL",
                genai_client=client,
                model="gemini-2.5-flash",
                repository=repo,
            )

        assert result["attempts"] == 1
        assert result["data"] is not None
        assert result["query"] == "SELECT * FROM users LIMIT 10"
        repo.execute_query.assert_awaited_once()

    async def test_mongo_happy_path(self):
        from graph import run_query_graph_async

        client = MagicMock()
        repo = _mock_repo(rows=[{"_id": "abc", "name": "Alice", "status": "active"}])

        with patch("asyncio.to_thread", new=AsyncMock(return_value=_llm_response(VALID_MONGO_JSON))):
            result = await run_query_graph_async(
                natural_query="find active users",
                db_type="MongoDB",
                genai_client=client,
                model="gemini-2.5-flash",
                repository=repo,
            )

        assert result["attempts"] == 1
        assert result["data"] is not None
        repo.execute_query.assert_awaited_once()


# ===========================================================================
# T4 — Retry counter (async)
# ===========================================================================

class TestRetryCounter:

    async def test_attempt_increments_on_repo_error_then_succeeds(self):
        from graph import run_query_graph_async

        client = MagicMock()
        repo = _mock_repo()
        repo.execute_query = AsyncMock(side_effect=[
            Exception("syntax error at or near 'usrs'"),
            [{"id": 1, "name": "Alice"}],
        ])

        llm_call_count = 0
        async def fake_to_thread(fn, **kwargs):
            nonlocal llm_call_count
            llm_call_count += 1
            return _llm_response(REPAIRED_SQL_JSON)

        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            result = await run_query_graph_async(
                natural_query="show me all users",
                db_type="PostgreSQL",
                genai_client=client,
                model="gemini-2.5-flash",
                repository=repo,
            )

        assert result["attempts"] == 2
        assert result["data"] is not None
        assert llm_call_count == 2   # initial + repair

    async def test_repair_prompt_contains_db_error(self):
        from graph import run_query_graph_async

        db_error = "column 'usrs.id' does not exist"
        client = MagicMock()
        repo = _mock_repo()
        repo.execute_query = AsyncMock(side_effect=[
            Exception(db_error),
            [{"id": 1}],
        ])

        captured_prompts = []
        async def capture_to_thread(fn, **kwargs):
            captured_prompts.append(kwargs.get("contents", ""))
            return _llm_response(REPAIRED_SQL_JSON)

        with patch("asyncio.to_thread", side_effect=capture_to_thread):
            await run_query_graph_async(
                natural_query="show me all users",
                db_type="PostgreSQL",
                genai_client=client,
                model="gemini-2.5-flash",
                repository=repo,
            )

        assert len(captured_prompts) == 2
        assert db_error in captured_prompts[1]


# ===========================================================================
# T5 — Circuit breaker (async)
# ===========================================================================

class TestCircuitBreaker:

    async def test_escalates_after_max_attempts(self):
        from graph import run_query_graph_async, MAX_ATTEMPTS

        client = MagicMock()
        repo = _mock_repo(raises=Exception("persistent DB error"))

        llm_call_count = 0
        async def fake_to_thread(fn, **kwargs):
            nonlocal llm_call_count
            llm_call_count += 1
            return _llm_response(VALID_SQL_JSON)

        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            result = await run_query_graph_async(
                natural_query="show me all users",
                db_type="PostgreSQL",
                genai_client=client,
                model="gemini-2.5-flash",
                repository=repo,
            )

        assert result["attempts"] == MAX_ATTEMPTS
        assert result["data"] is None
        assert "persistent DB error" in result["reply"]
        assert str(MAX_ATTEMPTS) in result["reply"]
        assert llm_call_count == MAX_ATTEMPTS

    async def test_independent_runs_do_not_share_state(self):
        from graph import run_query_graph_async, MAX_ATTEMPTS

        client = MagicMock()
        repo = _mock_repo(raises=Exception("DB down"))

        async def fake_to_thread(fn, **kwargs):
            return _llm_response(VALID_SQL_JSON)

        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            r1 = await run_query_graph_async("q1", "PostgreSQL", client, "model", repository=repo)
            r2 = await run_query_graph_async("q2", "PostgreSQL", client, "model", repository=repo)

        assert r1["attempts"] == MAX_ATTEMPTS
        assert r2["attempts"] == MAX_ATTEMPTS


# ===========================================================================
# T6 — Destructive guard (async)
# ===========================================================================

class TestDestructiveGuard:

    async def test_destructive_sql_blocked_before_repo(self):
        from graph import run_query_graph_async

        client = MagicMock()
        repo = _mock_repo()

        with patch("asyncio.to_thread", new=AsyncMock(return_value=_llm_response(DESTRUCTIVE_SQL_JSON))):
            result = await run_query_graph_async(
                natural_query="drop the users table",
                db_type="PostgreSQL",
                genai_client=client,
                model="gemini-2.5-flash",
                repository=repo,
            )

        assert result["data"] is None
        assert "destructive" in result["reply"].lower() or "read-only" in result["reply"].lower()
        repo.execute_query.assert_not_awaited()


# ===========================================================================
# T7 — RedisSessionManager (async, mocked Redis client)
# ===========================================================================

class TestRedisSessionManager:

    async def test_load_returns_empty_dict_for_missing_key(self):
        from session.redis_manager import RedisSessionManager
        client = MagicMock()
        client.get = AsyncMock(return_value=None)
        mgr = RedisSessionManager(client)
        result = await mgr.load("nonexistent")
        assert result == {}

    async def test_save_then_load_round_trip(self):
        from session.redis_manager import RedisSessionManager
        store = {}

        async def fake_get(key):
            return store.get(key)

        async def fake_setex(key, ttl, value):
            store[key] = value

        client = MagicMock()
        client.get = fake_get
        client.setex = fake_setex

        mgr = RedisSessionManager(client)
        payload = {"conv_state": "CONNECTED", "db_type": "PostgreSQL", "uri": "postgresql://x"}
        await mgr.save("sess-123", payload)
        loaded = await mgr.load("sess-123")
        assert loaded == payload

    async def test_corrupt_data_returns_empty_dict(self):
        from session.redis_manager import RedisSessionManager
        client = MagicMock()
        client.get = AsyncMock(return_value="{not valid json}")
        mgr = RedisSessionManager(client)
        result = await mgr.load("sess-corrupt")
        assert result == {}


# ===========================================================================
# T8 — ChatManager state round-trip
# ===========================================================================

class TestChatManager:

    def test_to_dict_from_empty_session(self):
        from agent import ChatManager
        mgr = ChatManager({})
        d = mgr.to_dict()
        assert d["conv_state"] == "AWAITING_DB_TYPE"
        assert d["db_type"] is None
        assert d["uri"] is None

    def test_to_dict_from_populated_session(self):
        from agent import ChatManager
        payload = {
            "conv_state": "CONNECTED",
            "db_type": "PostgreSQL",
            "hosting_type": "Local",
            "uri": "postgresql://user:pass@localhost/db",
        }
        mgr = ChatManager(payload)
        assert mgr.state == "CONNECTED"
        assert mgr.db_type == "PostgreSQL"
        assert mgr.to_dict() == payload

    async def test_handle_db_type_mutates_state(self):
        from agent import ChatManager
        mgr = ChatManager({})
        result = await mgr.process_message("postgresql")
        assert mgr.state == "AWAITING_HOSTING_TYPE"
        assert mgr.db_type == "PostgreSQL"
        assert result["state"] == "AWAITING_HOSTING_TYPE"

# ===========================================================================
# T9 — SemanticCache unit tests
# ===========================================================================

class TestSemanticCache:

    def _make_cache(self, redis_store: dict, embed_fn=None):
        from cache.semantic_cache import SemanticCache

        redis = MagicMock()
        store = redis_store

        async def fake_lrange(key, start, end):
            return store.get(key, [])

        def fake_pipeline():
            pipe = MagicMock()

            def rpush(key, val):
                store.setdefault(key, [])
                store[key].append(val)
                return pipe

            def ltrim(key, start, end): return pipe
            def expire(key, ttl): return pipe
            pipe.rpush = rpush
            pipe.ltrim = ltrim
            pipe.expire = expire
            pipe.execute = AsyncMock(return_value=None)
            return pipe

        redis.lrange = fake_lrange
        redis.pipeline = fake_pipeline

        default_embed = embed_fn or (lambda text: [1.0, 0.0])
        cache = SemanticCache(
            redis_client=redis,
            genai_client=MagicMock(),
            session_id="test-session",
            db_type="PostgreSQL",
        )

        async def fake_embed(text):
            return default_embed(text)
        cache._embed = fake_embed
        return cache

    async def test_miss_on_empty_cache(self):
        cache = self._make_cache({})
        result = await cache.lookup("show me all users")
        assert result is None

    async def test_store_then_hit_identical_query(self):
        from schemas import SQLQueryResponse
        store = {}
        cache = self._make_cache(store)
        parsed = SQLQueryResponse.model_validate_json(VALID_SQL_JSON)
        await cache.store("show me all users", parsed)
        hit = await cache.lookup("show me all users")
        assert hit is not None
        assert isinstance(hit, SQLQueryResponse)
        assert hit.query == parsed.query

    async def test_miss_on_orthogonal_embedding(self):
        from schemas import SQLQueryResponse
        embeddings = {
            "show me all users": [1.0, 0.0],
            "find top products":  [0.0, 1.0],
        }
        store = {}
        cache = self._make_cache(store, embed_fn=lambda t: embeddings.get(t, [1.0, 0.0]))
        parsed = SQLQueryResponse.model_validate_json(VALID_SQL_JSON)
        await cache.store("show me all users", parsed)
        result = await cache.lookup("find top products")
        assert result is None

    async def test_cosine_similarity_values(self):
        from cache.semantic_cache import _cosine
        assert abs(_cosine([1, 0], [1, 0]) - 1.0) < 1e-9
        assert abs(_cosine([1, 0], [0, 1]) - 0.0) < 1e-9
        assert abs(_cosine([1, 1], [1, 1]) - 1.0) < 1e-9
        assert _cosine([], []) == 0.0

    async def test_mongo_response_round_trips(self):
        from schemas import MongoQueryResponse
        store = {}
        cache = self._make_cache(store)
        cache._key = "qs:sem:test-session:MongoDB"
        cache._db_type = "MongoDB"
        parsed = MongoQueryResponse.model_validate_json(VALID_MONGO_JSON)
        await cache.store("find active users", parsed)
        hit = await cache.lookup("find active users")
        assert hit is not None
        assert isinstance(hit, MongoQueryResponse)
        assert hit.collection == "users"

    async def test_threshold_not_met(self):
        import math
        from schemas import SQLQueryResponse
        v_stored = [1.0, 0.0]
        angle = math.acos(0.94)   # sim = 0.94 < default threshold 0.95
        v_different = [math.cos(angle), math.sin(angle)]
        store = {}
        cache = self._make_cache(store, embed_fn=lambda t: v_stored)
        parsed = SQLQueryResponse.model_validate_json(VALID_SQL_JSON)
        await cache.store("q1", parsed)

        async def embed_different(text):
            return v_different
        cache._embed = embed_different

        result = await cache.lookup("q2 semantically different")
        assert result is None


# ===========================================================================
# T10 — Cache proxy integration in ChatManager._handle_query
# ===========================================================================

def _mock_async_engine():
    """Return a MagicMock that can be used as an async SQLAlchemy engine."""
    engine = MagicMock()
    engine.dispose = AsyncMock()
    return engine


class TestCacheProxyIntegration:

    async def test_cache_hit_bypasses_llm(self):
        from agent import ChatManager
        from schemas import SQLQueryResponse
        session_data = {
            "conv_state": "CONNECTED",
            "db_type": "PostgreSQL",
            "uri": "postgresql://localhost/db",
        }
        cached_parsed = SQLQueryResponse.model_validate_json(VALID_SQL_JSON)
        mgr = ChatManager(session_data, redis_client=MagicMock(), session_id="s1")
        mgr.genai_client = MagicMock()
        repo = _mock_repo(rows=[{"id": 1}])
        async_engine = _mock_async_engine()

        with patch("agent.make_async_sql_engine", return_value=async_engine), \
             patch("agent.SQLRepository", return_value=repo), \
             patch("agent.SemanticCache") as MockCache:
            mock_cache_instance = AsyncMock()
            mock_cache_instance.lookup = AsyncMock(return_value=cached_parsed)
            mock_cache_instance.store  = AsyncMock()
            MockCache.return_value = mock_cache_instance
            with patch("agent.run_query_graph_async") as mock_graph:
                result = await mgr._handle_query("show me all users")
            mock_graph.assert_not_called()
            mock_cache_instance.store.assert_not_awaited()

        assert result["cache_hit"] is True
        assert result["data"] == [{"id": 1}]

    async def test_cache_miss_runs_graph_and_stores(self):
        from agent import ChatManager
        from schemas import SQLQueryResponse
        session_data = {
            "conv_state": "CONNECTED",
            "db_type": "PostgreSQL",
            "uri": "postgresql://localhost/db",
        }
        parsed = SQLQueryResponse.model_validate_json(VALID_SQL_JSON)
        graph_return = {
            "reply": "Results:", "query": "SELECT * FROM users",
            "data": [{"id": 1}], "attempts": 1, "_parsed": parsed,
        }
        mgr = ChatManager(session_data, redis_client=MagicMock(), session_id="s1")
        mgr.genai_client = MagicMock()
        repo = _mock_repo(rows=[{"id": 1}])
        async_engine = _mock_async_engine()

        with patch("agent.make_async_sql_engine", return_value=async_engine), \
             patch("agent.SQLRepository", return_value=repo), \
             patch("agent.SemanticCache") as MockCache:
            mock_cache_instance = AsyncMock()
            mock_cache_instance.lookup = AsyncMock(return_value=None)
            mock_cache_instance.store  = AsyncMock()
            MockCache.return_value = mock_cache_instance
            with patch("agent.run_query_graph_async", new=AsyncMock(return_value=graph_return)):
                result = await mgr._handle_query("show me all users")
            mock_cache_instance.store.assert_awaited_once_with("show me all users", parsed)

        assert result["data"] == [{"id": 1}]

    async def test_no_cache_when_redis_unavailable(self):
        from agent import ChatManager
        from schemas import SQLQueryResponse
        session_data = {
            "conv_state": "CONNECTED",
            "db_type": "PostgreSQL",
            "uri": "postgresql://localhost/db",
        }
        parsed = SQLQueryResponse.model_validate_json(VALID_SQL_JSON)
        graph_return = {
            "reply": "Results:", "query": "SELECT * FROM users",
            "data": [{"id": 1}], "attempts": 1, "_parsed": parsed,
        }
        mgr = ChatManager(session_data, redis_client=None, session_id="s1")
        mgr.genai_client = MagicMock()
        repo = _mock_repo(rows=[{"id": 1}])
        async_engine = _mock_async_engine()

        with patch("agent.make_async_sql_engine", return_value=async_engine), \
             patch("agent.SQLRepository", return_value=repo), \
             patch("agent.SemanticCache") as MockCache, \
             patch("agent.run_query_graph_async", new=AsyncMock(return_value=graph_return)):
            result = await mgr._handle_query("show me all users")
            MockCache.assert_not_called()

        assert result["data"] == [{"id": 1}]


# ===========================================================================
# T11 — stream_query_events SSE generator
# ===========================================================================

class TestStreamQueryEvents:
    """
    Tests for streaming.stream_query_events().
    Strategy: patch asyncio.to_thread (LLM) and mock BaseRepository,
    collect all yielded SSE strings, parse them back to dicts, and
    assert the event sequence is correct.
    """

    async def _collect(self, **kwargs):
        """Run the generator and return a list of parsed event dicts."""
        from streaming import stream_query_events
        events = []
        async for raw in stream_query_events(**kwargs):
            raw = raw.strip()
            if raw.startswith("data: "):
                import json
                events.append(json.loads(raw[6:]))
        return events

    def _base_kwargs(self, repo, llm_response=None):
        from unittest.mock import MagicMock, AsyncMock, patch
        client = MagicMock()
        return dict(
            natural_query="show all users",
            db_type="PostgreSQL",
            genai_client=client,
            model="gemini-2.5-flash",
            repository=repo,
            cache=None,
        )

    # ── Happy path ────────────────────────────────────────────────────

    async def test_happy_path_event_sequence(self):
        repo = _mock_repo(rows=[{"id": 1, "name": "Alice"}])
        kwargs = self._base_kwargs(repo)

        with patch("asyncio.to_thread", new=AsyncMock(return_value=_llm_response(VALID_SQL_JSON))):
            events = await self._collect(**kwargs)

        event_names = [e["event"] for e in events]
        assert "thinking"  in event_names          # NEW: LLM generation started signal
        assert "query"     in event_names
        assert "executing" in event_names
        assert "result"    in event_names
        assert events[-1]["event"] == "done"
        # thinking must appear before query
        assert event_names.index("thinking") < event_names.index("query")

    async def test_result_event_contains_data(self):
        rows = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
        repo = _mock_repo(rows=rows)
        kwargs = self._base_kwargs(repo)

        with patch("asyncio.to_thread", new=AsyncMock(return_value=_llm_response(VALID_SQL_JSON))):
            events = await self._collect(**kwargs)

        result_evt = next(e for e in events if e["event"] == "result")
        assert result_evt["data"] == rows
        assert result_evt["query"] == "SELECT * FROM users LIMIT 10"

    # ── Retry path ────────────────────────────────────────────────────

    async def test_retry_emits_error_then_healing_then_result(self):
        repo = _mock_repo()
        repo.execute_query = AsyncMock(side_effect=[
            Exception("column 'usrs' does not exist"),
            [{"id": 1}],
        ])
        kwargs = self._base_kwargs(repo)

        with patch("asyncio.to_thread", new=AsyncMock(return_value=_llm_response(REPAIRED_SQL_JSON))):
            events = await self._collect(**kwargs)

        event_names = [e["event"] for e in events]
        assert "error"   in event_names
        assert "healing" in event_names
        assert "result"  in event_names
        # error must come before result
        assert event_names.index("error") < event_names.index("result")

    async def test_escalated_after_max_attempts(self):
        from graph import MAX_ATTEMPTS
        repo = _mock_repo(raises=Exception("DB always fails"))
        kwargs = self._base_kwargs(repo)

        with patch("asyncio.to_thread", new=AsyncMock(return_value=_llm_response(VALID_SQL_JSON))):
            events = await self._collect(**kwargs)

        event_names = [e["event"] for e in events]
        assert "escalated" in event_names
        assert events[-1]["event"] == "done"
        esc = next(e for e in events if e["event"] == "escalated")
        assert str(MAX_ATTEMPTS) in esc["reply"]

    # ── Destructive guard ─────────────────────────────────────────────

    async def test_destructive_op_emits_escalated_no_execute(self):
        repo = _mock_repo()
        kwargs = self._base_kwargs(repo)

        with patch("asyncio.to_thread", new=AsyncMock(return_value=_llm_response(DESTRUCTIVE_SQL_JSON))):
            events = await self._collect(**kwargs)

        event_names = [e["event"] for e in events]
        assert "escalated" in event_names
        assert "result"    not in event_names
        repo.execute_query.assert_not_awaited()

    # ── Cache hit ─────────────────────────────────────────────────────

    async def test_cache_hit_skips_graph(self):
        from schemas import SQLQueryResponse
        from unittest.mock import MagicMock, AsyncMock

        repo  = _mock_repo(rows=[{"id": 1}])
        cache = MagicMock()
        cached_parsed = SQLQueryResponse.model_validate_json(VALID_SQL_JSON)
        cache.lookup = AsyncMock(return_value=cached_parsed)
        cache.store  = AsyncMock()

        kwargs = self._base_kwargs(repo)
        kwargs["cache"] = cache

        with patch("asyncio.to_thread") as mock_thread:
            events = await self._collect(**kwargs)

        event_names = [e["event"] for e in events]
        assert "cache_hit" in event_names
        assert "result"    in event_names
        assert "query"     not in event_names   # graph never ran
        mock_thread.assert_not_called()          # LLM never called
        cache.store.assert_not_awaited()         # no re-store on HIT

    async def test_cache_miss_stores_on_success(self):
        from unittest.mock import MagicMock, AsyncMock

        repo  = _mock_repo(rows=[{"id": 1}])
        cache = MagicMock()
        cache.lookup = AsyncMock(return_value=None)   # MISS
        cache.store  = AsyncMock()

        kwargs = self._base_kwargs(repo)
        kwargs["cache"] = cache

        with patch("asyncio.to_thread", new=AsyncMock(return_value=_llm_response(VALID_SQL_JSON))):
            events = await self._collect(**kwargs)

        event_names = [e["event"] for e in events]
        assert "result" in event_names
        cache.store.assert_awaited_once()   # result stored in cache

    # ── done always last ──────────────────────────────────────────────

    async def test_done_is_always_last_event(self):
        """Even on exception, 'done' must be the final event."""
        repo = _mock_repo(raises=Exception("fatal"))
        kwargs = self._base_kwargs(repo)

        with patch("asyncio.to_thread", new=AsyncMock(return_value=_llm_response(VALID_SQL_JSON))):
            events = await self._collect(**kwargs)

        assert events[-1]["event"] == "done"

