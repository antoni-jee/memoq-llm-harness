"""Runtime configuration for document-service, read from environment variables.

Nothing here falls back to the working directory or to a workstation path:
the storage directory must be given, and must be absolute. Error messages and
``repr`` never contain the database password or the full database URL.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

# Shared with workspace-service; re-exported so existing imports keep working.
from services.common.db import (  # noqa: F401
    DEFAULT_DB_TIMEOUT, LOG_LEVELS, SSL_MODES, ConfigError, DatabaseSettings,
    env_number as _number, load_database_settings_from, load_schema_from,
    parse_database_url,
)

ENV_HOST = "DOCUMENT_SERVICE_HOST"
ENV_PORT = "DOCUMENT_SERVICE_PORT"
ENV_DATABASE_URL = "DOCUMENT_DATABASE_URL"
ENV_DB_SCHEMA = "DOCUMENT_DB_SCHEMA"
ENV_DB_TIMEOUT = "DOCUMENT_DB_TIMEOUT_SECONDS"
ENV_STORAGE_DIR = "DOCUMENT_STORAGE_DIR"
ENV_MAX_UPLOAD = "DOCUMENT_MAX_UPLOAD_BYTES"
ENV_LOG_LEVEL = "DOCUMENT_LOG_LEVEL"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8101
DEFAULT_SCHEMA = "document"
DEFAULT_MAX_UPLOAD = 50 * 1024 * 1024


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    database: DatabaseSettings
    db_schema: str
    storage_dir: str
    max_upload_bytes: int
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
    port = _number(env, ENV_PORT, DEFAULT_PORT, 1, 65535, int)

    storage_dir = (env.get(ENV_STORAGE_DIR) or "").strip()
    if not storage_dir:
        raise ConfigError("%s is required." % ENV_STORAGE_DIR)
    if not os.path.isabs(storage_dir):
        raise ConfigError("%s must be an absolute path." % ENV_STORAGE_DIR)

    max_upload = _number(env, ENV_MAX_UPLOAD, DEFAULT_MAX_UPLOAD, 1, 1024 ** 3, int)
    log_level = (env.get(ENV_LOG_LEVEL) or "info").strip().lower()
    if log_level not in LOG_LEVELS:
        raise ConfigError("%s must be one of: %s." % (ENV_LOG_LEVEL, ", ".join(LOG_LEVELS)))

    return Settings(
        host=host,
        port=port,
        database=load_database_settings(env),
        db_schema=load_schema(env),
        storage_dir=os.path.normpath(storage_dir),
        max_upload_bytes=max_upload,
        log_level=log_level,
    )
