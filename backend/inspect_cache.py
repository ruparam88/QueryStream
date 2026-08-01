#!/usr/bin/env python3
"""
inspect_cache.py — QueryStream semantic cache inspector (CLI tool).

Lets you see exactly what SQL is stored in Redis for each natural-language
query, flag poisoned entries (system-table references), and optionally clear
a session's cache without restarting the backend.

Reads REDIS_URL from .env (falls back to redis://localhost:6379).

Usage
-----
  # List ALL qs:sem:* keys in Redis and their entry counts
  python inspect_cache.py

  # Show all cached entries for a specific session + db_type
  python inspect_cache.py <session_id> <db_type>

  # Clear (flush) the cache for a specific session + db_type
  python inspect_cache.py --clear <session_id> <db_type>

  # Clear ALL qs:sem:* keys in Redis (nuclear reset)
  python inspect_cache.py --clear-all

Finding your session_id
-----------------------
  Open browser DevTools → Application → Session Storage → qs_session
  The value is your session_id (e.g. "k3j8xz1q2").
"""

import asyncio
import json
import sys
import os

# Load .env if python-dotenv is installed
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379")

_SYSTEM_SCHEMAS: frozenset[str] = frozenset({
    "information_schema", "performance_schema", "sys", "pg_catalog",
})

# ANSI colours (disabled on Windows unless FORCE_COLOR is set)
_COLOUR = sys.platform != "win32" or os.environ.get("FORCE_COLOR")
_RED    = "\033[31m" if _COLOUR else ""
_GRN    = "\033[32m" if _COLOUR else ""
_YLW    = "\033[33m" if _COLOUR else ""
_CYN    = "\033[36m" if _COLOUR else ""
_RST    = "\033[0m"  if _COLOUR else ""
_BOLD   = "\033[1m"  if _COLOUR else ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_entry(raw: str) -> tuple[dict, dict, str, bool]:
    """
    Parse a raw Redis list value into (entry, parsed, query_text, is_poisoned).
    Returns empty dicts / empty string / False on parse failure.
    """
    try:
        entry = json.loads(raw)
    except Exception:
        return {}, {}, "<corrupt JSON>", False

    try:
        parsed = json.loads(entry.get("parsed_json", "{}"))
    except Exception:
        parsed = {}

    query_text = parsed.get("query") or str(parsed.get("filter", ""))
    is_poisoned = any(t in query_text.lower() for t in _SYSTEM_SCHEMAS)
    return entry, parsed, query_text, is_poisoned


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def cmd_list(redis) -> None:
    """List all qs:sem:* keys and their entry counts."""
    keys = sorted(await redis.keys("qs:sem:*"))
    if not keys:
        print("No semantic cache keys found in Redis.")
        return

    print(f"{_BOLD}Found {len(keys)} cache key(s):{_RST}\n")
    for key in keys:
        count = await redis.llen(key)
        raw_list = await redis.lrange(key, 0, -1)
        n_poisoned = sum(
            1 for raw in raw_list if _parse_entry(raw)[3]
        )
        poison_tag = f"  {_RED}⚠ {n_poisoned} poisoned{_RST}" if n_poisoned else ""
        print(f"  {_CYN}{key}{_RST}  ({count} entr{'y' if count == 1 else 'ies'}){poison_tag}")

    print(
        f"\n{_YLW}Tip:{_RST} run  python inspect_cache.py <session_id> <db_type>  to inspect a key.\n"
        f"     run  python inspect_cache.py --clear <session_id> <db_type>  to flush it."
    )


async def cmd_inspect(redis, session_id: str, db_type: str) -> None:
    """Show all cached entries for a session+db_type pair."""
    key = f"qs:sem:{session_id}:{db_type}"
    raw_list = await redis.lrange(key, 0, -1)

    if not raw_list:
        print(f"No entries found for key: {_CYN}{key}{_RST}")
        return

    print(f"{_BOLD}Cache key:{_RST} {_CYN}{key}{_RST}")
    print(f"{_BOLD}Entries  :{_RST} {len(raw_list)}\n")
    print("=" * 72)

    for i, raw in enumerate(raw_list, 1):
        entry, parsed, query_text, is_poisoned = _parse_entry(raw)

        if is_poisoned:
            status = f"{_RED}⚠  POISONED (system-table reference){_RST}"
        else:
            status = f"{_GRN}✅ OK{_RST}"

        confidence = parsed.get("confidence_score", "N/A")
        schema_type = entry.get("schema_type", "N/A")

        print(f"{_BOLD}Entry #{i}{_RST}  [{status}]")
        print(f"  {_BOLD}Natural query{_RST} : {entry.get('natural_query', 'N/A')}")
        print(f"  {_BOLD}Schema type  {_RST} : {schema_type}")
        print(f"  {_BOLD}Generated SQL{_RST} : {query_text}")
        print(f"  {_BOLD}Confidence   {_RST} : {confidence}")
        print()

    if any(_parse_entry(r)[3] for r in raw_list):
        print(
            f"{_YLW}To clear poisoned entries run:{_RST}\n"
            f"  python inspect_cache.py --clear {session_id} {db_type}"
        )


async def cmd_clear(redis, session_id: str, db_type: str) -> None:
    """Delete all entries for a session+db_type cache key."""
    key = f"qs:sem:{session_id}:{db_type}"
    count = await redis.llen(key)
    if count == 0:
        print(f"Key {_CYN}{key}{_RST} is already empty (or does not exist).")
        return
    await redis.delete(key)
    print(f"{_GRN}Cleared {count} entr{'y' if count == 1 else 'ies'} from:{_RST} {_CYN}{key}{_RST}")
    print("The next query will go through the LLM and a fresh correct entry will be stored.")


async def cmd_clear_all(redis) -> None:
    """Nuclear option — delete ALL qs:sem:* keys."""
    keys = await redis.keys("qs:sem:*")
    if not keys:
        print("No semantic cache keys to clear.")
        return
    total = 0
    for key in keys:
        total += await redis.llen(key)
        await redis.delete(key)
    print(f"{_GRN}Cleared {total} total entries across {len(keys)} key(s).{_RST}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    import redis.asyncio as aioredis

    args = sys.argv[1:]

    # Parse flags
    clear_mode    = "--clear"     in args
    clear_all     = "--clear-all" in args
    positional    = [a for a in args if not a.startswith("--")]

    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        await redis.ping()
        print(f"Connected to {_CYN}{REDIS_URL}{_RST}\n")

        if clear_all:
            await cmd_clear_all(redis)

        elif clear_mode and len(positional) == 2:
            await cmd_clear(redis, positional[0], positional[1])

        elif len(positional) == 2:
            await cmd_inspect(redis, positional[0], positional[1])

        elif len(positional) == 0 and not clear_all:
            await cmd_list(redis)

        else:
            print(__doc__)
            sys.exit(1)

    except Exception as exc:
        print(f"{_RED}Error:{_RST} {exc}")
        sys.exit(1)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
