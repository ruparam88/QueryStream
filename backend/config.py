"""
config.py — Single source of truth for all QueryStream configuration.

Backed by Pydantic BaseSettings: values are read from environment variables
(and optionally from a .env file). Environment variables always take
precedence over the file, so production secrets injected via K8s Secrets /
Docker env override local dev values without code changes.

Usage (everywhere in the codebase):
    from config import settings
    settings.gemini_model        # "gemini-2.5-flash"
    settings.redis_url           # "redis://localhost:6379"

Adding a new config value:
    1. Add a field here with a sensible default.
    2. Document it in .env.example.
    3. Use settings.<field> — never os.environ.get() directly.
"""

from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    All runtime configuration for QueryStream.
    Field names map 1-to-1 to environment variable names (upper-cased).
    """

    # ------------------------------------------------------------------
    # Gemini / LLM
    # ------------------------------------------------------------------
    gemini_api_key: str = Field(
        default="",
        description="Google Gemini API key. Required in CONNECTED state.",
    )
    gemini_model: str = Field(
        default="gemini-2.5-flash",
        description="Gemini model ID used for structured query generation.",
    )
    gemini_embed_model: str = Field(
        default="text-embedding-004",
        description="Gemini model ID used for semantic cache embeddings.",
    )

    # ------------------------------------------------------------------
    # Redis
    # ------------------------------------------------------------------
    redis_url: str = Field(
        default="redis://localhost:6379",
        description="Redis connection URL. Supports redis:// and rediss:// (TLS).",
    )
    session_ttl_seconds: int = Field(
        default=3600,
        description="TTL for session keys in Redis. Default: 1 hour.",
    )

    # ------------------------------------------------------------------
    # Semantic cache
    # ------------------------------------------------------------------
    sem_cache_threshold: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description=(
            "Cosine similarity threshold above which a cached query is "
            "returned instead of running the LLM. Range: 0.0–1.0."
        ),
    )
    cache_ttl_seconds: int = Field(
        default=86400,
        description="TTL for semantic cache keys in Redis. Default: 24 hours.",
    )
    cache_max_entries: int = Field(
        default=200,
        description="Max entries kept per (session_id, db_type) cache list (LIFO trim).",
    )

    # ------------------------------------------------------------------
    # Self-healing graph
    # ------------------------------------------------------------------
    max_query_attempts: int = Field(
        default=3,
        ge=1,
        description=(
            "Maximum LLM + DB execution attempts before the graph "
            "escalates and returns an error to the user."
        ),
    )

    # ------------------------------------------------------------------
    # Application server
    # ------------------------------------------------------------------
    app_host: str = Field(
        default="0.0.0.0",
        description="Uvicorn bind host.",
    )
    app_port: int = Field(
        default=8000,
        description="Uvicorn bind port.",
    )
    debug: bool = Field(
        default=False,
        description="Enable debug mode (auto-reload, verbose logging).",
    )
    log_level: str = Field(
        default="INFO",
        description="Python logging level: DEBUG | INFO | WARNING | ERROR.",
    )
    cors_origins: str = Field(
        default="*",
        description=(
            "Comma-separated list of allowed CORS origins. "
            "Use '*' for development. Restrict in production."
        ),
    )

    # ------------------------------------------------------------------
    # Pydantic-Settings model config
    # ------------------------------------------------------------------
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",          # silently ignore unrecognised env vars
        case_sensitive=False,    # GEMINI_API_KEY == gemini_api_key
    )

    @property
    def cors_origins_list(self) -> list[str]:
        """Return CORS origins as a list (splits on comma)."""
        return [o.strip() for o in self.cors_origins.split(",")]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Cached settings singleton.
    Call get_settings() in FastAPI Depends() for testable injection.
    Module-level `settings` is the preferred shortcut everywhere else.
    """
    return Settings()


# Module-level singleton — import and use directly.
settings: Settings = get_settings()
