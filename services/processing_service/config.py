"""Runtime configuration for processing-service, read from environment variables.

The engine is chosen on the server only. Clients can name ``stub`` or the
configured engine, never a URL or a key. ``repr`` and error messages never
contain the API key. This service needs no database, so it does not import
the shared database module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional

ENV_HOST = "PROCESSING_SERVICE_HOST"
ENV_PORT = "PROCESSING_SERVICE_PORT"
ENV_ENGINE = "PROCESSING_ENGINE"
ENV_BASE_URL = "PROCESSING_BASE_URL"
ENV_MODEL = "PROCESSING_MODEL"
ENV_API_KEY = "PROCESSING_API_KEY"
ENV_TIMEOUT = "PROCESSING_TIMEOUT_SECONDS"
ENV_MAX_RETRIES = "PROCESSING_MAX_RETRIES"
ENV_CHUNK_SIZE = "PROCESSING_CHUNK_SIZE"
ENV_TEMPERATURE = "PROCESSING_TEMPERATURE"
ENV_MAX_TOKENS = "PROCESSING_MAX_TOKENS"
ENV_MAX_SEGMENTS = "PROCESSING_MAX_SEGMENTS"
ENV_MAX_BODY = "PROCESSING_MAX_BODY_BYTES"
ENV_LOG_LEVEL = "PROCESSING_LOG_LEVEL"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8103
ENGINE_STUB = "stub"
ENGINE_OPENAI = "openai"
ENGINE_ANTHROPIC = "anthropic"
ENGINE_KINDS = (ENGINE_STUB, ENGINE_OPENAI, ENGINE_ANTHROPIC)
LOG_LEVELS = ("critical", "error", "warning", "info", "debug")


class ConfigError(ValueError):
    """Configuration is missing or invalid. The message is safe to print."""


@dataclass(frozen=True)
class EngineSettings:
    kind: str = ENGINE_STUB
    base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    timeout: float = 300.0
    max_retries: int = 3
    chunk_size: int = 20
    temperature: float = 0.2
    max_tokens: int = 4096


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    engine: EngineSettings
    max_segments: int
    max_body_bytes: int
    log_level: str


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


def load_engine_settings(env: Mapping[str, str]) -> EngineSettings:
    kind = (env.get(ENV_ENGINE) or ENGINE_STUB).strip().lower()
    if kind not in ENGINE_KINDS:
        raise ConfigError("%s must be one of: %s." % (ENV_ENGINE, ", ".join(ENGINE_KINDS)))
    model = (env.get(ENV_MODEL) or "").strip()
    if kind != ENGINE_STUB and not model:
        raise ConfigError("%s is required when %s=%s." % (ENV_MODEL, ENV_ENGINE, kind))
    return EngineSettings(
        kind=kind,
        base_url=(env.get(ENV_BASE_URL) or "").strip(),
        model=model,
        api_key=(env.get(ENV_API_KEY) or "").strip(),
        timeout=_number(env, ENV_TIMEOUT, 300.0, 1, 3600, float),
        max_retries=_number(env, ENV_MAX_RETRIES, 3, 1, 10, int),
        chunk_size=_number(env, ENV_CHUNK_SIZE, 20, 1, 200, int),
        temperature=_number(env, ENV_TEMPERATURE, 0.2, 0, 2, float),
        max_tokens=_number(env, ENV_MAX_TOKENS, 4096, 0, 200000, int),
    )


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    env = os.environ if env is None else env
    log_level = (env.get(ENV_LOG_LEVEL) or "info").strip().lower()
    if log_level not in LOG_LEVELS:
        raise ConfigError("%s must be one of: %s." % (ENV_LOG_LEVEL, ", ".join(LOG_LEVELS)))
    return Settings(
        host=(env.get(ENV_HOST) or DEFAULT_HOST).strip(),
        port=_number(env, ENV_PORT, DEFAULT_PORT, 1, 65535, int),
        engine=load_engine_settings(env),
        max_segments=_number(env, ENV_MAX_SEGMENTS, 200, 1, 5000, int),
        max_body_bytes=_number(env, ENV_MAX_BODY, 20 * 1024 * 1024, 1024, 1024 ** 3, int),
        log_level=log_level,
    )
