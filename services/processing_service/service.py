"""processing-service business logic, independent of HTTP.

One request = one batch (``operation: "batch"``, one or more segments,
``translate_batch``) or one Rerun (``operation: "rerun"``, exactly one
segment, ``translate_one``). The system prompt is built with
``prompts.build_batch_prompt`` from caller-supplied context; every context
field falls back to the shipped defaults.

Responses carry suggestions in workspace-service's submission shape
(``{key: {"text": ...}}``) and only sanitized diagnostics: never raw provider
errors or responses, prompts, or credentials.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from memoq_harness import defaults, llm, prompt_templates, prompts
from memoq_harness import state as state_mod

from . import SERVICE_NAME
from .config import ENGINE_STUB, Settings
from .engines import (EngineUnavailable, StubClient, real_client,
                      sdk_available, sdk_available_for_config)

log = logging.getLogger(SERVICE_NAME)

OPERATIONS = ("batch", "rerun")
TAG_SOURCES = ("target", "source")
STAGES = ("auto",) + tuple(prompt_templates.STAGE_KEYS)
PROCESS_FIELDS = ("operation", "engine", "tag_source", "batch", "context", "segments", "config")
BATCH_FIELDS = ("index", "count")
CONFIG_FIELDS = ("preset", "backend", "base_url", "api_key", "model",
                 "temperature", "chunk_size", "max_tokens", "timeout", "max_retries")
CONTEXT_FIELDS = ("source_lang", "target_lang", "hard_rules_text", "vibe", "termbase",
                  "corrections_block", "directive", "stage", "template")
CONTEXT_TEXT_FIELDS = ("source_lang", "target_lang", "hard_rules_text", "vibe",
                       "corrections_block", "directive")
SEGMENT_REQUIRED = ("key", "source_text", "mt_target")
SEGMENT_OPTIONAL = ("user_comment",)
TERM_FIELDS = ("source", "target", "note")
MAX_TERMS = 20000
MAX_LISTED = 20
_NO_FILE = os.path.join(os.path.abspath(os.sep), "processing-service", "no-state-file.json")


# --------------------------------------------------------------------- errors


class ServiceError(Exception):
    status = 500
    code = "internal_error"

    def __init__(self, message: str, details: Optional[Dict[str, object]] = None):
        super().__init__(message)
        self.message = message
        self.details = details
        """Identifiers and counts only - never segment text, prompts or provider text."""


class BadRequest(ServiceError):
    status, code = 400, "bad_request"


class TooLarge(ServiceError):
    status, code = 413, "too_large"


class EngineNotReady(ServiceError):
    status, code = 503, "engine_unavailable"


class EngineFailed(ServiceError):
    status, code = 502, "engine_failed"


class MissingApiKey(ServiceError):
    """A key-required preset was given with no session key.

    A distinct, allowlisted code so the client can surface an actionable,
    sanitized message ("enter a key in the sidebar") instead of a generic 400.
    The provider's configured key is never offered as a fallback.
    """
    status, code = 400, "missing_api_key"


# ------------------------------------------------------------------ request


@dataclass(frozen=True)
class SegmentInput:
    key: str
    source_text: str
    mt_target: str
    user_comment: str = ""


@dataclass(frozen=True)
class Context:
    source_lang: str = ""
    target_lang: str = ""
    hard_rules_text: str = defaults.DEFAULT_HARD_RULES
    vibe: str = defaults.DEFAULT_VIBE
    termbase: Tuple[state_mod.TermEntry, ...] = ()
    corrections_block: str = ""
    directive: str = ""
    stage: str = "auto"
    template: Optional[str] = None


@dataclass(frozen=True)
class ModelConfig:
    """A per-request model configuration (a :class:`llm.ClientConfig` subset).

    Selected in the Streamlit sidebar and sent with the request so the user -
    not the service's environment - chooses the backend, endpoint and model for
    this one operation. The key is the operator's session key, sent only for this
    request and never the service's configured-engine key; it is repr-suppressed
    so it can appear in no request repr, log line or error.
    """
    preset: str
    backend: str
    base_url: str
    model: str
    temperature: float = 0.2
    max_tokens: int = 4096
    chunk_size: int = 20
    timeout: float = 300.0
    max_retries: int = 3
    api_key: str = field(default="", repr=False)


@dataclass(frozen=True)
class ProcessRequest:
    operation: str
    engine: Optional[str]
    tag_source: str
    batch_index: int
    batch_count: int
    context: Context = field(default_factory=Context)
    segments: Tuple[SegmentInput, ...] = ()
    config: Optional[ModelConfig] = None


# ------------------------------------------------------------------ analyze

# The analyze endpoint is a separate, read-only critique action (the local
# ``analyze_corrections``): it computes a critique over the operator's stored
# corrections and returns it in a ``critique`` field. It is NOT a new entry in
# ``OPERATIONS`` (which drives the segment-producing ``process`` path) - that
# keeps ``process`` / ``parse_process_request`` untouched.
CORRECTION_REQUIRED = ("segment_id", "source", "llm_suggestion", "human_final")
CORRECTION_OPTIONAL = ("comment", "batch_index", "layout_version")
ANALYZE_FIELDS = ("corrections", "hard_rules_text", "expected_tags",
                  "engine", "config")
MAX_ANALYZE_CORRECTIONS = 40
"""Mirrors the local ``analyze_corrections`` limit: only the most recent
corrections are sent to the model, so a corpus of corrections cannot blow the
prompt budget."""


@dataclass(frozen=True)
class AnalyzeRequest:
    corrections: Tuple[Dict[str, object], ...]
    hard_rules_text: str = ""
    expected_tags: Dict[str, Tuple[int, ...]] = field(default_factory=dict)
    engine: Optional[str] = None
    config: Optional[ModelConfig] = None


def _listed(items) -> List[str]:
    return sorted(items)[:MAX_LISTED]


def _int(value, label: str, low: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < low:
        raise BadRequest("'%s' must be an integer >= %d." % (label, low))
    return value


def _parse_context(raw: object) -> Context:
    if raw is None:
        return Context()
    if not isinstance(raw, dict):
        raise BadRequest("'context' must be an object.")
    unknown = set(raw) - set(CONTEXT_FIELDS)
    if unknown:
        raise BadRequest("Unknown context field(s).", {"fields": _listed(unknown)})
    bad = [f for f in CONTEXT_TEXT_FIELDS if f in raw and not isinstance(raw[f], str)]
    if bad:
        raise BadRequest("Context text fields must be strings.", {"fields": _listed(bad)})
    stage = raw.get("stage", "auto")
    if stage not in STAGES:
        raise BadRequest("'stage' must be one of: %s." % ", ".join(STAGES))
    template = raw.get("template")
    if template is not None and not isinstance(template, str):
        raise BadRequest("'template' must be a string or null.")
    terms_raw = raw.get("termbase", [])
    if not isinstance(terms_raw, list) or len(terms_raw) > MAX_TERMS:
        raise BadRequest("'termbase' must be a list of at most %d entries." % MAX_TERMS)
    terms, bad_terms = [], []
    for index, entry in enumerate(terms_raw):
        if (not isinstance(entry, dict) or not {"source", "target"} <= set(entry)
                or set(entry) - set(TERM_FIELDS)
                or not all(isinstance(v, str) for v in entry.values())):
            bad_terms.append(index)
            continue
        terms.append(state_mod.TermEntry(entry["source"], entry["target"], entry.get("note", "")))
    if bad_terms:
        raise BadRequest("Termbase entries need string 'source' and 'target' and an "
                         "optional 'note'.", {"items": bad_terms[:MAX_LISTED]})
    values = {f: raw[f] for f in CONTEXT_TEXT_FIELDS if f in raw}
    return Context(termbase=tuple(terms), stage=stage, template=template, **values)


def _config_number(raw: object, name: str, default, low, high, *, ints: bool):
    """One optional numeric config field, with type and bound checks.

    Absent -> the preset/service default. Present -> the right numeric type and
    a sane bound. Booleans are rejected as numbers (``True`` is an ``int``).
    """
    if name not in raw:
        return default
    value = raw[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadRequest("'config.%s' must be a number." % name)
    if ints and not isinstance(value, int):
        raise BadRequest("'config.%s' must be an integer." % name)
    if not (low <= value <= high):
        raise BadRequest("'config.%s' must be between %s and %s." % (name, low, high))
    return int(value) if ints else float(value)


def _parse_config(raw: object) -> Optional[ModelConfig]:
    """Validate an optional per-request model config block.

    Returns a :class:`ModelConfig`, or ``None`` when the caller sent no config (a
    legacy caller: the service's configured engine then applies). When a block is
    present it is a :class:`llm.ClientConfig` subset - which preset/backend, the
    endpoint, the model and the inference settings a real call needs. Unknown
    fields and out-of-bounds values are rejected, so an invalid explicit config
    fails safely: it never falls back to the service's environment or the stub.
    Strings fall back to the named preset's defaults when omitted.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BadRequest("'config' must be an object.")
    unknown = set(raw) - set(CONFIG_FIELDS)
    if unknown:
        raise BadRequest("Unknown config field(s).", {"fields": _listed(unknown)})

    preset = raw.get("preset", llm.DEFAULT_PRESET)
    if not isinstance(preset, str) or preset not in llm.PRESETS:
        raise BadRequest("'config.preset' must be a known preset name.",
                         {"presets": _listed(sorted(llm.PRESETS))})
    preset_def = llm.PRESETS[preset]
    backend = raw.get("backend", preset_def.backend)
    if not isinstance(backend, str) or backend not in (llm.BACKEND_OPENAI, llm.BACKEND_ANTHROPIC):
        raise BadRequest("'config.backend' must be 'openai' or 'anthropic'.",
                         {"backends": [llm.BACKEND_OPENAI, llm.BACKEND_ANTHROPIC]})

    base_url = raw.get("base_url", preset_def.base_url)
    api_key = raw.get("api_key", "")
    model = raw.get("model", preset_def.default_model)
    for name, value in (("base_url", base_url), ("api_key", api_key), ("model", model)):
        if not isinstance(value, str):
            raise BadRequest("'config.%s' must be a string." % name)
    if not model.strip():
        raise BadRequest("'config.model' must be a non-empty model name.")

    return ModelConfig(
        preset=preset, backend=backend, base_url=base_url, model=model,
        temperature=_config_number(raw, "temperature", 0.2, 0, 2, ints=False),
        max_tokens=_config_number(raw, "max_tokens", 4096, 0, 200000, ints=True),
        chunk_size=_config_number(raw, "chunk_size", 20, 1, 200, ints=True),
        timeout=_config_number(raw, "timeout", 300.0, 1, 3600, ints=False),
        max_retries=_config_number(raw, "max_retries", 3, 1, 10, ints=True),
        api_key=api_key,
    )


def parse_config_request(body: object) -> ModelConfig:
    """Validate the config block of a /probe or /models request (required)."""
    if not isinstance(body, dict):
        raise BadRequest("The request body must be a JSON object.")
    unknown = set(body) - {"config"}
    if unknown:
        raise BadRequest("Unknown request field(s).", {"fields": _listed(unknown)})
    config = _parse_config(body.get("config"))
    if config is None:
        raise BadRequest("'config' is required for this request.")
    return config


def parse_process_request(body: object, max_segments: int) -> ProcessRequest:
    """Validate the whole request before any work is done."""
    if not isinstance(body, dict):
        raise BadRequest("The request body must be a JSON object.")
    unknown = set(body) - set(PROCESS_FIELDS)
    if unknown:
        raise BadRequest("Unknown request field(s).", {"fields": _listed(unknown)})

    operation = body.get("operation", "batch")
    if operation not in OPERATIONS:
        raise BadRequest("'operation' must be one of: %s." % ", ".join(OPERATIONS))
    engine = body.get("engine")
    if engine is not None and (not isinstance(engine, str) or not engine):
        raise BadRequest("'engine' must be a non-empty string when given.")
    config = _parse_config(body.get("config"))
    if engine is not None and config is not None:
        raise BadRequest("Provide either a model config or an engine, not both.")
    tag_source = body.get("tag_source", "target")
    if tag_source not in TAG_SOURCES:
        raise BadRequest("'tag_source' must be one of: %s." % ", ".join(TAG_SOURCES))

    batch = body.get("batch", {"index": 0, "count": 1})
    if not isinstance(batch, dict) or set(batch) != set(BATCH_FIELDS):
        raise BadRequest("'batch' must be {\"index\": int, \"count\": int}.")
    index = _int(batch["index"], "batch.index", 0)
    count = _int(batch["count"], "batch.count", 1)
    if index >= count:
        raise BadRequest("'batch.index' must be smaller than 'batch.count'.")

    context = _parse_context(body.get("context"))

    raw_segments = body.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise BadRequest("'segments' must be a non-empty list.")
    if len(raw_segments) > max_segments:
        raise BadRequest("At most %d segments per request." % max_segments)
    segments, bad = [], []
    for position, item in enumerate(raw_segments):
        if (not isinstance(item, dict) or not set(SEGMENT_REQUIRED) <= set(item)
                or set(item) - set(SEGMENT_REQUIRED + SEGMENT_OPTIONAL)
                or not all(isinstance(v, str) for v in item.values())
                or not item["key"]):
            bad.append(position)
            continue
        segments.append(SegmentInput(item["key"], item["source_text"], item["mt_target"],
                                     item.get("user_comment", "")))
    if bad:
        raise BadRequest("Each segment needs string 'key' (non-empty), 'source_text' and "
                         "'mt_target', and an optional string 'user_comment'.",
                         {"items": bad[:MAX_LISTED]})
    keys = [s.key for s in segments]
    duplicates = {k for k in keys if keys.count(k) > 1}
    if duplicates:
        raise BadRequest("Duplicate segment keys.", {"keys": _listed(duplicates)})
    if operation == "rerun" and len(segments) != 1:
        raise BadRequest("'rerun' requires exactly one segment.")

    return ProcessRequest(operation=operation, engine=engine, tag_source=tag_source,
                          batch_index=index, batch_count=count, context=context,
                          segments=tuple(segments), config=config)


def parse_analyze_request(body: object, max_corrections: int = MAX_ANALYZE_CORRECTIONS
                          ) -> AnalyzeRequest:
    """Validate a whole analyze request before any work is done.

    The analyze endpoint is a separate, read-only critique action: it computes a
    critique over the operator's stored corrections and returns it in a
    ``critique`` field, never touching the operator's translation. Each
    correction is a JSON-shaped dict with the :class:`state_mod.Correction`
    fields - required ``segment_id`` / ``source`` / ``llm_suggestion`` /
    ``human_final`` and an optional string ``comment`` and integer
    ``batch_index``. An empty ``corrections`` list is accepted: the critique of
    nothing is the empty string, exactly the local ``analyze_corrections``
    contract. ``expected_tags`` maps a segment id to the inline-tag numbers that
    row is expected to contain; ``hard_rules_text`` is the editorial rules in
    force. A per-request model config or an engine selects the backend, exactly
    as the process path does (mutually exclusive), and an explicit config never
    falls back to the service's environment or the stub.
    """
    if not isinstance(body, dict):
        raise BadRequest("The request body must be a JSON object.")
    unknown = set(body) - set(ANALYZE_FIELDS)
    if unknown:
        raise BadRequest("Unknown request field(s).", {"fields": _listed(unknown)})

    engine = body.get("engine")
    if engine is not None and (not isinstance(engine, str) or not engine):
        raise BadRequest("'engine' must be a non-empty string when given.")
    config = _parse_config(body.get("config"))
    if engine is not None and config is not None:
        raise BadRequest("Provide either a model config or an engine, not both.")

    hard_rules_text = body.get("hard_rules_text", "")
    if not isinstance(hard_rules_text, str):
        raise BadRequest("'hard_rules_text' must be a string.")

    raw_tags = body.get("expected_tags", {})
    if not isinstance(raw_tags, dict):
        raise BadRequest("'expected_tags' must be an object mapping segment ids "
                         "to a list of integers.")
    expected_tags: Dict[str, Tuple[int, ...]] = {}
    for segment_id, numbers in raw_tags.items():
        if (not isinstance(segment_id, str) or not isinstance(numbers, list)
                or any(not isinstance(n, int) or isinstance(n, bool) for n in numbers)):
            raise BadRequest("Each 'expected_tags' value must be a list of integers.")
        expected_tags[segment_id] = tuple(numbers)

    raw_corrections = body.get("corrections", [])
    if not isinstance(raw_corrections, list):
        raise BadRequest("'corrections' must be a list.")
    if len(raw_corrections) > max_corrections:
        raise BadRequest("At most %d corrections per request." % max_corrections)
    corrections: List[Dict[str, object]] = []
    bad = []
    for position, item in enumerate(raw_corrections):
        if (not isinstance(item, dict) or not set(CORRECTION_REQUIRED) <= set(item)
                or set(item) - set(CORRECTION_REQUIRED + CORRECTION_OPTIONAL)):
            bad.append(position)
            continue
        # Required fields are strings; the optional comment is a string and the
        # optional batch_index a non-negative integer.
        for name in CORRECTION_REQUIRED:
            if not isinstance(item[name], str):
                bad.append(position)
                break
        else:
            comment = item.get("comment", "")
            batch_index = item.get("batch_index", 0)
            layout_version = item.get("layout_version", 0)
            if not isinstance(comment, str):
                bad.append(position)
            elif not isinstance(batch_index, int) or isinstance(batch_index, bool) \
                    or batch_index < 0:
                bad.append(position)
            elif not isinstance(layout_version, int) or isinstance(layout_version, bool) \
                    or layout_version < 0:
                bad.append(position)
            elif not item["segment_id"]:
                bad.append(position)
            else:
                corrections.append(item)
    if bad:
        raise BadRequest(
            "Each correction needs string 'segment_id' (non-empty), 'source', "
            "'llm_suggestion', 'human_final', and an optional string 'comment' "
            "and non-negative integer 'batch_index'.",
            {"items": _listed(sorted(bad))})

    return AnalyzeRequest(corrections=tuple(corrections),
                          hard_rules_text=hard_rules_text,
                          expected_tags=expected_tags, engine=engine,
                          config=config)


# ------------------------------------------------------------------ helpers


def relevant_terms(termbase, texts) -> List[state_mod.TermEntry]:
    """The app's termbase filter (``ProjectState.relevant_terms``), reused as is."""
    holder = state_mod.ProjectState(_NO_FILE)  # in memory only; never saved
    holder.termbase = list(termbase)
    return holder.relevant_terms(texts)


def build_prompt(request: ProcessRequest, records) -> Tuple[str, str]:
    """``(stage, system prompt)`` exactly as the app builds it for a batch."""
    context = request.context
    stage = (context.stage if context.stage != "auto"
             else prompts.stage_for_batch(request.batch_index, request.batch_count))
    template = (context.template if context.template is not None
                else state_mod.default_prompt_templates().get(stage, ""))
    terms = relevant_terms(context.termbase, [r.source_text for r in records])
    prompt = prompts.build_batch_prompt(
        hard_rules_text=context.hard_rules_text, vibe=context.vibe, termbase=terms,
        corrections_block=context.corrections_block, source_lang=context.source_lang,
        target_lang=context.target_lang, directive=context.directive, template=template,
        batch_index=request.batch_index, batch_count=request.batch_count,
        segment_count=len(records))
    return stage, prompt


# ------------------------------------------------------------------ service


class ProcessingService:
    def __init__(self, settings: Settings, client_factory=real_client):
        self.settings = settings
        self.client_factory = client_factory
        self.max_body_bytes = settings.max_body_bytes
        self.max_segments = settings.max_segments

    @property
    def configured_engine(self) -> str:
        return self.settings.engine.kind

    def available_engines(self) -> List[str]:
        return sorted({ENGINE_STUB, self.configured_engine})

    def _client(self, engine: str, tag_source: str):
        if engine == ENGINE_STUB:
            return StubClient(tag_source)
        try:
            return self.client_factory(self.settings.engine)
        except EngineUnavailable as exc:
            raise EngineNotReady(str(exc)) from None

    def _client_config_from_block(self, config: ModelConfig) -> llm.ClientConfig:
        """Build a real ClientConfig from a per-request config block.

        Uses only the operator's session key, never the service's configured-engine
        key. A key-required preset with an empty key fails fast with an actionable,
        sanitized error instead of silently borrowing a provider key or falling
        back to the stub. The Ollama preset needs no key (local placeholder).
        """
        preset = llm.PRESETS[config.preset]
        if preset.requires_key and not config.api_key.strip():
            raise MissingApiKey(
                "Enter an API key in the sidebar to use the %s provider." % config.preset,
                {"preset": config.preset})
        return llm.ClientConfig(
            preset=config.preset, base_url=config.base_url, api_key=config.api_key,
            model=config.model, temperature=config.temperature,
            max_tokens=config.max_tokens, chunk_size=config.chunk_size,
            timeout=config.timeout, max_retries=config.max_retries,
            backend=config.backend)

    def _real_client_from_config(self, config: ModelConfig):
        client_config = self._client_config_from_block(config)
        if not sdk_available_for_config(client_config):
            raise EngineNotReady("The SDK for the selected backend is not installed.")
        try:
            return llm.LLMClient(client_config)
        except llm.LLMError as exc:
            raise EngineNotReady(str(exc)) from None

    def _resolve_client(self, request: ProcessRequest, tag_source: str):
        """The client for this request, plus the engine string to report for it.

        An explicit model config always wins: it builds a real client from the
        block and never falls back to the service's environment or the stub. With
        no config the legacy path applies - a named engine, else the service's
        configured engine - which is what unconfigured callers rely on.
        """
        if request.config is not None:
            return self._real_client_from_config(request.config), request.config.backend
        engine = request.engine or self.configured_engine
        if engine not in self.available_engines():
            raise BadRequest("Unknown engine.", {"available": self.available_engines()})
        return self._client(engine, tag_source), engine

    def process(self, request: ProcessRequest) -> Dict[str, object]:
        records = [
            state_mod.SegmentRecord(
                key=s.key, segment_id=s.key.split("#", 1)[0], ordinal=position,
                batch_index=request.batch_index, source_text=s.source_text,
                mt_target=s.mt_target, user_comment=s.user_comment)
            for position, s in enumerate(request.segments)
        ]
        stage, system_prompt = build_prompt(request, records)
        client, engine = self._resolve_client(request, request.tag_source)

        start = time.time()
        failed = 0
        if request.operation == "rerun":
            try:
                text, _raw = client.translate_one(records[0], system_prompt)
            except llm.LLMError:
                log.warning("rerun failed at the engine (%s)", engine)
                raise EngineFailed("The engine request failed.") from None
            found = {records[0].key: text} if text is not None else {}
            requests = 1
        else:
            result = client.translate_batch(records, system_prompt)
            found = dict(result.suggestions)
            failed = len(result.errors)
            requests = result.requests

        suggestions = {k: {"text": v} for k, v in found.items()
                       if isinstance(v, str) and v.strip()}
        missing = [r.key for r in records if r.key not in suggestions]
        if failed and not suggestions:
            log.warning("batch failed at the engine (%s): %d failed request(s)", engine, failed)
            raise EngineFailed("Every engine request failed.",
                               {"failed_requests": failed, "requests": requests})
        errors = (["%d of %d engine request(s) failed; their segments are listed as missing."
                   % (failed, requests)] if failed else [])
        elapsed_ms = int((time.time() - start) * 1000)
        log.info("%s via %s: %d segment(s), %d suggested, %d missing, %d failed request(s)",
                 request.operation, engine, len(records), len(suggestions), len(missing), failed)
        return {
            "operation": request.operation,
            "engine": engine,
            "tag_source": request.tag_source,
            "stage": stage,
            "suggestions": suggestions,
            "missing": missing,
            "errors": errors,
            "requests": requests,
            "elapsed_ms": elapsed_ms,
            "system_prompt_chars": len(system_prompt),
        }

    def readiness(self) -> Dict[str, object]:
        ok = sdk_available(self.settings.engine)
        return {"status": "ready" if ok else "not_ready",
                "engine": self.configured_engine,
                "checks": {"engine": "ok" if ok else "sdk_missing"}}

    def probe(self, config: ModelConfig) -> Dict[str, object]:
        """One bounded, user-initiated generation probe for the selected model.

        Reuses the local :func:`llm.probe_model` exactly; the result is returned
        as a sanitized dict (its ``message()`` already strips credentials). No
        suggestion is written. Transport/API/shape failures become a 502.
        """
        client_config = self._client_config_from_block(config)
        if not sdk_available_for_config(client_config):
            raise EngineNotReady("The SDK for the selected backend is not installed.")
        try:
            result = llm.probe_model(client_config)
        except llm.LLMError:
            raise EngineFailed("The model probe failed.") from None
        log.info("model probe via %s: %s", client_config.model, result.status)
        return {"ok": result.ok, "status": result.status, "message": result.message()}

    def list_models(self, config: ModelConfig) -> Dict[str, object]:
        """Model ids the selected endpoint advertises, for the List-models action.

        Reuses :meth:`llm.LLMClient.list_models`; no suggestion is written and no
        generation happens. SDK-missing and connection failures become safe 503/502.
        """
        client_config = self._client_config_from_block(config)
        if not sdk_available_for_config(client_config):
            raise EngineNotReady("The SDK for the selected backend is not installed.")
        try:
            client = llm.LLMClient(client_config)
            models = client.list_models()
        except llm.LLMError:
            raise EngineFailed("Could not list models.") from None
        return {"models": list(models)}

    def _resolve_analyze_client(self, request: AnalyzeRequest):
        """Client + reported engine for an analysis request.

        Mirrors :meth:`_resolve_client` minus the tag source the analysis path
        does not use: an explicit model config always wins; otherwise a named
        engine, else the service's configured engine.
        """
        if request.config is not None:
            return self._real_client_from_config(request.config), request.config.backend
        engine = request.engine or self.configured_engine
        if engine not in self.available_engines():
            raise BadRequest("Unknown engine.", {"available": self.available_engines()})
        return self._client(engine, ""), engine

    def analyze_corrections(self, request: AnalyzeRequest) -> Dict[str, object]:
        """Build a warning-aware critique of the operator's stored corrections.

        A separate action from processing: it reads the corrections the client
        already applied and returns a brief the UI splits into warnings plus a
        translation brief, without touching any translation slot. Mirrors the
        real client's ``analyze_corrections`` contract so the stub and a
        configured model share the response shape.
        """
        start = time.time()
        client, engine = self._resolve_analyze_client(request)
        corrections = [
            state_mod.Correction(
                segment_id=c["segment_id"], source=c["source"],
                llm_suggestion=c["llm_suggestion"], human_final=c["human_final"],
                comment=c.get("comment", ""),
                batch_index=c.get("batch_index", 0),
                layout_version=c.get("layout_version", 0))
            for c in request.corrections
        ]
        try:
            critique = client.analyze_corrections(
                corrections, request.hard_rules_text, request.expected_tags)
        except llm.LLMError:
            raise EngineFailed("The engine request failed.") from None
        return {
            "operation": "analyze",
            "engine": engine,
            "corrections_analyzed": len(corrections),
            "critique": critique,
            "elapsed_ms": int((time.time() - start) * 1000),
        }
