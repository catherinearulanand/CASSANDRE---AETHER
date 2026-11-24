"""Runtime settings. No secrets in repo; everything overridable by env (AETHER_*)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AETHER_", env_file=".env", extra="ignore")

    # SQLite is the zero-install local default; PostgreSQL URL (postgresql+psycopg://...) is the
    # documented persistent-demo target (infra/compose.yaml).
    database_url: str = f"sqlite:///{(REPO_ROOT / 'var' / 'aether.db').as_posix()}"
    object_store_dir: str = str(REPO_ROOT / "var" / "objects")
    signing_key_dir: str = str(REPO_ROOT / "var" / "keys")
    environment: str = "local"  # local | demo | test
    data_mode: str = "SYNTHETIC"  # every record is synthetic in this build
    source_mode: str = "REPLAY"  # REPLAY | LIVE_MODEL | MANUAL (deterministic offline default)
    session_ttl_seconds: int = 8 * 3600
    cookie_secure: bool = False  # local-only exception; set true behind TLS
    login_max_failures: int = 5
    login_window_seconds: int = 900
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    max_upload_bytes: int = 10 * 1024 * 1024
    approval_ttl_seconds: int = 24 * 3600
    auto_create_schema: bool = False  # force create_all on non-SQLite (tests only); production uses Alembic
    live_model_enabled: bool = False  # hosted provider disabled unless explicitly configured


@lru_cache
def get_settings() -> Settings:
    return Settings()
