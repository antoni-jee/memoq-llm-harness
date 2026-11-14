"""Runtime configuration for document-service, read from environment variables.

Nothing here falls back to the working directory or to a workstation path:
the storage directory must be given, and must be absolute. Error messages and
``repr`` never contain the database password or the full database URL.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping, Optional
from urllib.parse import parse_qs, unquote, urlsplit

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
DEFAULT_DB_TIMEOUT = 10.0
DEFAULT_MAX_UPLOAD = 50 * 1024 * 1024
LOG_LEVELS = ("critical", "error", "warning", "info", "debug")
SSL_MODES = ("disable", "prefer", "require", "verify-ca", "verify-full")

_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class ConfigError(ValueError):
    """Configuration is missing or invalid. The message is safe to print."""


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)
    sslmode: str = "prefer"
    timeout: float = DEFAULT_DB_TIMEOUT

    def redacted(self) -> str:
        """``postgresql://user@host:port/db`` - for logs; never the password."""
        return "postgresql://%s@%s:%d/%s" % (self.user, self.host, self.port, self.database)


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    database: DatabaseSettings
    db_schema: str
    storage_dir: str
    max_upload_bytes: int
    log_level: str


def parse_database_url(url: str, timeout: float = DEFAULT_DB_TIMEOUT) -> DatabaseSettings:
    """Parse ``postgresql://user:password@host:port/db?sslmode=...``.

    Supported query parameter: ``sslmode`` (disable, prefer, require,
    verify-ca, verify-full; default prefer, as in libpq). Anything else is
    rejected rather than silently ignored.
    """
    if not url or not url.strip():
        raise ConfigError("Database URL is empty.")
    parts = urlsplit(url.strip())
    if parts.scheme not in ("postgresql", "postgres"):
        raise ConfigError("Database URL must use the postgresql:// scheme.")
    try:
        port = parts.port or 5432
    except ValueError:
        raise ConfigError("Database URL has an invalid port.") from None
    if not parts.hostname:
        raise ConfigError("Database URL has no host.")
    user = unquote(parts.username or "")
    if not user:
        raise ConfigError("Database URL has no user name.")
    database = unquote(parts.path.lstrip("/"))
    if not database or "/" in database:
        raise ConfigError("Database URL must name exactly one database.")

    query = parse_qs(parts.query, keep_blank_values=True)
    unknown = sorted(set(query) - {"sslmode"})
    if unknown:
        raise ConfigError("Unsupported database URL parameter(s): %s." % ", ".join(unknown))
    sslmode = (query.get("sslmode") or ["prefer"])[-1]
    if sslmode not in SSL_MODES:
        raise ConfigError("sslmode must be one of: %s." % ", ".join(SSL_MODES))

    return DatabaseSettings(
        host=parts.hostname,
        port=port,
        database=database,
        user=user,
        password=unquote(parts.password or ""),
        sslmode=sslmode,
        timeout=timeout,
    )


def load_database_settings(env: Optional[Mapping[str, str]] = None) -> DatabaseSettings:
    env = os.environ if env is None else env
    url = (env.get(ENV_DATABASE_URL) or "").strip()
    if not url:
        raise ConfigError("%s is required." % ENV_DATABASE_URL)
    timeout = _number(env, ENV_DB_TIMEOUT, DEFAULT_DB_TIMEOUT, 1, 300, float)
    try:
        return parse_database_url(url, timeout)
    except ConfigError as exc:
        raise ConfigError("%s: %s" % (ENV_DATABASE_URL, exc)) from None


def load_schema(env: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if env is None else env
    schema = (env.get(ENV_DB_SCHEMA) or DEFAULT_SCHEMA).strip()
    if not _SCHEMA_RE.match(schema):
        raise ConfigError(
            "%s must be a lowercase identifier ([a-z_][a-z0-9_]*, max 63)." % ENV_DB_SCHEMA)
    return schema


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


def _number(env, name, default, low, high, kind):
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = kind(raw)
    except ValueError:
        raise ConfigError("%s must be a number." % name) from None
    if not low <= value <= high:
        raise ConfigError("%s must be between %s and %s." % (name, low, high))
    return value
