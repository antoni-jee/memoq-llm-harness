"""PostgreSQL settings and connections shared by the backend services.

Moved unchanged from document-service (``config.py`` / ``repository.py``)
when workspace-service needed the same code. Driver: pg8000 (pure Python).

Error messages and ``repr`` never contain the database password or the full
database URL; :class:`RepositoryError` messages never contain SQL or server
error text, so they are safe to return to clients.
"""

from __future__ import annotations

import re
import ssl
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import parse_qs, unquote, urlsplit

import pg8000.native

DEFAULT_DB_TIMEOUT = 10.0
LOG_LEVELS = ("critical", "error", "warning", "info", "debug")
SSL_MODES = ("disable", "prefer", "require", "verify-ca", "verify-full")

SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class ConfigError(ValueError):
    """Configuration is missing or invalid. The message is safe to print."""


class RepositoryError(RuntimeError):
    """The database could not complete an operation. The message is safe to show."""


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


def load_database_settings_from(env: Mapping[str, str], url_var: str,
                                 timeout_var: str) -> DatabaseSettings:
    """Database settings from a service's own environment variable names."""
    url = (env.get(url_var) or "").strip()
    if not url:
        raise ConfigError("%s is required." % url_var)
    timeout = env_number(env, timeout_var, DEFAULT_DB_TIMEOUT, 1, 300, float)
    try:
        return parse_database_url(url, timeout)
    except ConfigError as exc:
        raise ConfigError("%s: %s" % (url_var, exc)) from None


def load_schema_from(env: Mapping[str, str], schema_var: str, default: str) -> str:
    schema = (env.get(schema_var) or default).strip()
    if not SCHEMA_RE.match(schema):
        raise ConfigError(
            "%s must be a lowercase identifier ([a-z_][a-z0-9_]*, max 63)." % schema_var)
    return schema


def env_number(env, name, default, low, high, kind):
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


def ssl_context_for(mode: str):
    """Map a libpq-style sslmode onto pg8000's ``ssl_context`` argument."""
    if mode == "disable":
        return False
    if mode == "prefer":
        return None  # pg8000: try TLS without verification, fall back to plain
    if mode == "require":
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    context = ssl.create_default_context()
    if mode == "verify-ca":
        context.check_hostname = False
    return context  # verify-full: certificate and host name are checked


def open_connection(settings: DatabaseSettings, application_name: str):
    """Open a pg8000 native connection. Failures raise a sanitised RepositoryError."""
    try:
        return pg8000.native.Connection(
            user=settings.user,
            password=settings.password or None,
            host=settings.host,
            port=settings.port,
            database=settings.database,
            timeout=settings.timeout,
            ssl_context=ssl_context_for(settings.sslmode),
            application_name=application_name,
        )
    except (pg8000.native.Error, OSError, ssl.SSLError):
        raise RepositoryError("Database connection failed.") from None


def quote_schema(schema: str) -> str:
    """Quote an already-validated schema identifier."""
    if not schema or not all(c.islower() or c.isdigit() or c == "_" for c in schema):
        raise ValueError("Invalid schema identifier.")
    return '"%s"' % schema
