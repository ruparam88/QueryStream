"""
QueryStream execution graph — async self-healing retry state machine.

State transitions:
  GENERATING → EXECUTING → SUCCESS          (happy path)
  GENERATING → EXECUTING → HEALING → GENERATING  (retry on DB error)
  HEALING → ESCALATED                        (after MAX_ATTEMPTS)

Changes from v3:
- All node functions are now `async def` (uses LangGraph ainvoke).
- `engine` / `mongo_db` replaced by `repository: BaseRepository`.
  Business logic is now fully decoupled from the async driver.
- Blocking LLM call wrapped in asyncio.to_thread (non-blocking).

Note on _parsed:
  LangGraph only preserves keys declared in the TypedDict state schema.
  The parsed Pydantic object must be declared here so it survives the
  GENERATING → EXECUTING state transition.
"""

import asyncio
import logging
from typing import Any, Optional
from typing_extensions import TypedDict

from langgraph.graph import StateGraph, END

from config import settings
from schemas import SQLQueryResponse, MongoQueryResponse
from db.repository import BaseRepository

# Alias for backward compatibility with tests that do `from graph import MAX_ATTEMPTS`
MAX_ATTEMPTS: int = settings.max_query_attempts

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------

class QueryState(TypedDict):
    # Inputs (set once, never mutated)
    natural_query: str
    db_type: str                     # "PostgreSQL" | "MySQL" | "MongoDB"
    repository: Any                  # BaseRepository — async DB accessor
    genai_client: Any                # google.genai.Client (sync SDK)
    model: str                       # e.g. "gemini-2.5-flash"

    # Mutable workflow state
    attempt: int
    last_query: str                  # last generated query string
    last_error: str                  # last DB error message
    result_data: Optional[list]      # fetched rows / documents
    final_reply: str                 # human-readable outcome
    node: str                        # current logical state label
    _parsed: Any                     # Pydantic object: GENERATING → EXECUTING


# ---------------------------------------------------------------------------
# Node: GENERATING (async)
# Calls Gemini with structured output schema. Blocking I/O on thread pool.
# ---------------------------------------------------------------------------

async def node_generate(state: QueryState) -> QueryState:
    from google.genai import types

    client = state["genai_client"]
    db_type = state["db_type"]
    attempt = state["attempt"]
    natural_query = state["natural_query"]
    last_error = state.get("last_error", "")
    last_query = state.get("last_query", "")

    if attempt == 0 or not last_error:
        prompt = (
            f"You are an expert {db_type} query writer.\n"
            f"Translate the following user request into a query.\n\n"
            f"User request: {natural_query}"
        )
    else:
        prompt = (
            f"You are an expert {db_type} query writer performing self-healing repair.\n\n"
            f"Original user intent:\n{natural_query}\n\n"
            f"Previously attempted query (attempt {attempt}):\n{last_query}\n\n"
            f"Database error returned:\n{last_error}\n\n"
            f"Analyse the error, correct the query, and return a fixed version."
        )

    logger.info("GENERATING: db=%s attempt=%d", db_type, attempt + 1)

    is_mongo = db_type == "MongoDB"
    schema = MongoQueryResponse if is_mongo else SQLQueryResponse

    # Wrap the synchronous Gemini SDK call so it doesn't block the event loop.
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=state["model"],
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
        ),
    )

    if is_mongo:
        parsed = MongoQueryResponse.model_validate_json(response.text)
        query_str = str(parsed.filter)
        destructive = parsed.requires_destructive_operation
        logger.info(
            "Mongo output: collection=%s confidence=%.2f destructive=%s",
            parsed.collection, parsed.confidence_score, destructive,
        )
    else:
        parsed = SQLQueryResponse.model_validate_json(response.text)
        query_str = parsed.query
        destructive = parsed.requires_destructive_operation
        logger.info(
            "SQL output: confidence=%.2f destructive=%s",
            parsed.confidence_score, destructive,
        )

    if destructive:
        logger.warning("Destructive operation detected — escalating immediately.")
        return {
            **state,
            "node": "ESCALATED",
            "last_query": query_str,
            "final_reply": (
                "⚠️ The generated query requires a destructive operation "
                "(INSERT/UPDATE/DELETE/DROP). QueryStream is read-only. "
                "Please rephrase your request."
            ),
        }

    return {
        **state,
        "node": "EXECUTING",
        "last_query": query_str,
        "_parsed": parsed,
    }


# ---------------------------------------------------------------------------
# Node: EXECUTING (async)
# Delegates to repository — zero driver coupling here.
# ---------------------------------------------------------------------------

async def node_execute(state: QueryState) -> QueryState:
    parsed = state.get("_parsed")
    repo: BaseRepository = state["repository"]
    logger.info("EXECUTING: db=%s attempt=%d", state["db_type"], state["attempt"] + 1)

    try:
        data = await repo.execute_query(parsed)
        logger.info("EXECUTING: success rows=%d", len(data))
        return {
            **state,
            "node": "SUCCESS",
            "result_data": data,
            "final_reply": (
                "Here are the results for your query:"
                if data else
                "Query ran successfully but returned no results."
            ),
        }

    except Exception as exc:
        logger.warning(
            "EXECUTING: failed attempt=%d error=%s",
            state["attempt"] + 1, type(exc).__name__,
        )
        return {
            **state,
            "node": "HEALING",
            "last_error": str(exc),
        }


# ---------------------------------------------------------------------------
# Node: HEALING (async)
# Increments attempt counter; decides retry vs escalate.
# ---------------------------------------------------------------------------

async def node_heal(state: QueryState) -> QueryState:
    next_attempt = state["attempt"] + 1
    logger.info("HEALING: next attempt=%d / max=%d", next_attempt, MAX_ATTEMPTS)

    if next_attempt >= MAX_ATTEMPTS:
        # Keep attempt at MAX_ATTEMPTS-1 so run_query_graph_async returns
        # attempt+1 == MAX_ATTEMPTS exactly.
        return {
            **state,
            "node": "ESCALATED",
            "final_reply": (
                f"Query failed after {MAX_ATTEMPTS} attempts. "
                f"Last error: {state['last_error']}"
            ),
        }

    return {
        **state,
        "attempt": next_attempt,
        "node": "GENERATING",
    }


# ---------------------------------------------------------------------------
# Routing helpers
# ---------------------------------------------------------------------------

def route_after_generate(state: QueryState) -> str:
    return state["node"]


def route_after_execute(state: QueryState) -> str:
    return state["node"]


def route_after_heal(state: QueryState) -> str:
    return state["node"]


# ---------------------------------------------------------------------------
# Graph compilation (once at import time)
# ---------------------------------------------------------------------------

def _build_graph() -> Any:
    g = StateGraph(QueryState)
    g.add_node("GENERATING", node_generate)
    g.add_node("EXECUTING", node_execute)
    g.add_node("HEALING", node_heal)
    g.set_entry_point("GENERATING")
    g.add_conditional_edges("GENERATING", route_after_generate, {
        "EXECUTING": "EXECUTING",
        "ESCALATED": END,
    })
    g.add_conditional_edges("EXECUTING", route_after_execute, {
        "SUCCESS": END,
        "HEALING": "HEALING",
    })
    g.add_conditional_edges("HEALING", route_after_heal, {
        "GENERATING": "GENERATING",
        "ESCALATED": END,
    })
    return g.compile()


execution_graph = _build_graph()


# ---------------------------------------------------------------------------
# Public entry point (async)
# ---------------------------------------------------------------------------

async def run_query_graph_async(
    natural_query: str,
    db_type: str,
    genai_client: Any,
    model: str,
    repository: BaseRepository,
) -> dict:
    """
    Async entry point. Called by agent.py.
    Returns a dict compatible with the existing ChatResponse contract.
    """
    initial: QueryState = {
        "natural_query": natural_query,
        "db_type": db_type,
        "repository": repository,
        "genai_client": genai_client,
        "model": model,
        "attempt": 0,
        "last_query": "",
        "last_error": "",
        "result_data": None,
        "final_reply": "",
        "node": "GENERATING",
        "_parsed": None,
    }

    final_state: QueryState = await execution_graph.ainvoke(initial)

    return {
        "reply":    final_state["final_reply"],
        "query":    final_state.get("last_query") or None,
        "data":     final_state.get("result_data"),          # None = escalated/failed
        "attempts": final_state["attempt"] + 1,
        "_parsed":  final_state.get("_parsed"),              # for SemanticCache.store()
    }
