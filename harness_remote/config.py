"""Runtime configuration for remote backend mode.

``HARNESS_BACKEND`` selects ``local`` (the default; this package is not used)
or ``remote``. In remote mode the three service URLs must be present and
valid. Remote misconfiguration raises :class:`ConfigError`; it never falls
back to local.

URLs are validated to be ``http``/``https`` service addresses with no embedded
credentials, query string or fragment, no spaces or control characters, a
numeric port within 1..65535, and (when present) a valid IPv6 host. A malformed
URL or port raises :class:`ConfigError` naming the variable, never the value.
Timeouts are stored as explicit
``(connect, read)`` tuples: the connect phase always uses
``HARNESS_HTTP_TIMEOUT_SECONDS``; the read phase uses
``HARNESS_PROCESS_TIMEOUT_SECONDS`` for processing calls and
``HARNESS_HTTP_TIMEOUT_SECONDS`` otherwise.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple
from urllib.parse import urlsplit

ENV_BACKEND = "HARNESS_BACKEND"
ENV_DOCUMENT_URL = "HARNESS_DOCUMENT_URL"
ENV_WORKSPACE_URL = "HARNESS_WORKSPACE_URL"
ENV_PROCESSING_URL = "HARNESS_PROCESSING_URL"
ENV_HTTP_TIMEOUT = "HARNESS_HTTP_TIMEOUT_SECONDS"
ENV_PROCESS_TIMEOUT = "HARNESS_PROCESS_TIMEOUT_SECONDS"

DEFAULT_BACKEND = "local"
DEFAULT_HTTP_TIMEOUT = 30.0
DEFAULT_PROCESS_TIMEOUT = 600.0
BACKENDS = ("local", "remote")

# Reasonable bounds; a value outside them is a misconfiguration, not a surprise.
HTTP_TIMEOUT_RANGE = (1.0, 3600.0)
PROCESS_TIMEOUT_RANGE = (1.0, 7200.0)


class ConfigError(ValueError):
    """Configuration is missing or invalid. The message is safe to print."""


@dataclass(frozen=True)
class RemoteConfig:
    mode: str
    http_timeout: Tuple[float, float]       # (connect, read)
    process_timeout: Tuple[float, float]    # (connect, read)
    document_url: str = ""
    workspace_url: str = ""
    processing_url: str = ""

    @property
    def is_remote(self) -> bool:
        return self.mode == "remote"


def _number(env: Mapping[str, str], name: str, default: float,
            low: float, high: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError("%s must be a number." % name) from None
    if not low <= value <= high:
        raise ConfigError("%s must be between %g and %g seconds." % (name, low, high))
    return value


def _validate_service_url(raw: Optional[str], label: str) -> str:
    """Return a bare ``http``/``https`` service address, or raise ConfigError.

    Validation uses only the standard-library URL parser and never echoes the
    value or a raw parser message: every error names the environment variable
    only. Leading and trailing spaces are tolerated (stripped), but internal
    whitespace and any control character are not; they are checked on the
    original input so ``strip`` cannot hide a boundary control character. A port
    must be numeric and within 1..65535, and an IPv6 literal is validated, so a
    malformed address or port is a ConfigError, not a later connection surprise.
    """
    text = (raw or "").strip()
    if not text:
        raise ConfigError("%s is required in remote mode." % label)

    # Reject a control character anywhere in the original input (checked before
    # strip so a boundary one cannot be hidden) and any whitespace that survives
    # the strip (i.e. an internal space/tab). Ordinary leading or trailing spaces
    # are allowed: they are not control characters and are removed by strip.
    original = raw or ""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in original) or any(ch.isspace() for ch in text):
        raise ConfigError("%s must not contain spaces or control characters." % label)

    # Parse with the standard library only. A malformed IPv6 literal, an
    # unbalanced bracket or a nonnumeric port raises here; convert it to a
    # ConfigError that names the variable, never the value or the parser text.
    try:
        parts = urlsplit(text)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        raise ConfigError("%s could not be parsed as a service URL." % label) from None

    if parts.scheme.lower() not in ("http", "https"):
        raise ConfigError("%s must be an http or https URL." % label)
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise ConfigError("%s must not contain credentials." % label)
    if parts.query:
        raise ConfigError("%s must not contain a query string." % label)
    if parts.fragment:
        raise ConfigError("%s must not contain a fragment." % label)
    if not hostname:
        raise ConfigError("%s must name a host." % label)
    # A bracket or a colon in the host means an IPv6 literal; validate it with
    # the standard library rather than trusting the split, so a malformed
    # literal is a ConfigError.
    if "[" in (parts.netloc or "") or ":" in (hostname or ""):
        try:
            ipaddress.IPv6Address(hostname)
        except ValueError:
            raise ConfigError("%s has a malformed IPv6 address." % label) from None
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError("%s has a port outside the 1..65535 range." % label)
    # A path is allowed (a service mounted under a prefix); just drop the slash.
    return text.rstrip("/")


def load_config(env: Optional[Mapping[str, str]] = None) -> RemoteConfig:
    env = os.environ if env is None else env

    mode = (env.get(ENV_BACKEND) or DEFAULT_BACKEND).strip().lower()
    if mode not in BACKENDS:
        raise ConfigError("%s must be one of: %s." % (ENV_BACKEND, ", ".join(BACKENDS)))

    http_seconds = _number(env, ENV_HTTP_TIMEOUT, DEFAULT_HTTP_TIMEOUT,
                           HTTP_TIMEOUT_RANGE[0], HTTP_TIMEOUT_RANGE[1])
    process_seconds = _number(env, ENV_PROCESS_TIMEOUT, DEFAULT_PROCESS_TIMEOUT,
                              PROCESS_TIMEOUT_RANGE[0], PROCESS_TIMEOUT_RANGE[1])

    http_timeout = (http_seconds, http_seconds)
    process_timeout = (http_seconds, process_seconds)

    if mode != "remote":
        return RemoteConfig(mode=mode, http_timeout=http_timeout,
                            process_timeout=process_timeout)

    document_url = _validate_service_url(env.get(ENV_DOCUMENT_URL), ENV_DOCUMENT_URL)
    workspace_url = _validate_service_url(env.get(ENV_WORKSPACE_URL), ENV_WORKSPACE_URL)
    processing_url = _validate_service_url(env.get(ENV_PROCESSING_URL), ENV_PROCESSING_URL)
    return RemoteConfig(
        mode=mode,
        http_timeout=http_timeout,
        process_timeout=process_timeout,
        document_url=document_url,
        workspace_url=workspace_url,
        processing_url=processing_url,
    )
