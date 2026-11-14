"""Runtime configuration for workspace-service, read from environment variables.

Error messages and ``repr`` never contain the database password or the full
database URL.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

from services.common.db import (
    LOG_LEVELS, ConfigError, DatabaseSettings, env_number, load_database_settings_from,
    load_schema_from,
)

ENV_HOST = "WORKSPACE_SERVICE_HOST"
ENV_PORT = "WORKSPACE_SERVICE_PORT"
ENV_DATABASE_URL = "WORKSPACE_DATABASE_URL"
ENV_DB_SCHEMA = "WORKSPACE_DB_SCHEMA"
ENV_DB_TIMEOUT = "WORKSPACE_DB_TIMEOUT_SECONDS"
ENV_MAX_BODY = "WORKSPACE_MAX_BODY_BYTES"
ENV_LOG_LEVEL = "WORKSPACE_LOG_LEVEL"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8102
DEFAULT_SCHEMA = "workspace"
DEFAULT_MAX_BODY = 20 * 1024 * 1024

__all__ = ["ConfigError", "Settings", "load_settings", "load_database_settings", "load_schema"]


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    database: DatabaseSettings
    db_schema: str
    max_body_bytes: int
    log_level: str


def load_database_settings(env: Optional[Mapping[str, str]] = None) -> DatabaseSettings:
    env = os.environ if env is None else env
    return load_database_settings_from(env, ENV_DATABASE_URL, ENV_DB_TIMEOUT)


def load_schema(env: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if env is None else env
    return load_schema_from(env, ENV_DB_SCHEMA, DEFAULT_SCHEMA)


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    env = os.environ if env is None else env
    host = (env.get(ENV_HOST) or DEFAULT_HOST).strip()
    port = env_number(env, ENV_PORT, DEFAULT_PORT, 1, 65535, int)
    max_body = env_number(env, ENV_MAX_BODY, DEFAULT_MAX_BODY, 1024, 1024 ** 3, int)
    log_level = (env.get(ENV_LOG_LEVEL) or "info").strip().lower()
    if log_level not in LOG_LEVELS:
        raise ConfigError("%s must be one of: %s." % (ENV_LOG_LEVEL, ", ".join(LOG_LEVELS)))
    return Settings(
        host=host,
        port=port,
        database=load_database_settings(env),
        db_schema=load_schema(env),
        max_body_bytes=max_body,
        log_level=log_level,
    )
