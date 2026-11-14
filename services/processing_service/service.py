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
from .engines import EngineUnavailable, StubClient, real_client, sdk_available

log = logging.getLogger(SERVICE_NAME)

OPERATIONS = ("batch", "rerun")
TAG_SOURCES = ("target", "source")
STAGES = ("auto",) + tuple(prompt_templates.STAGE_KEYS)
PROCESS_FIELDS = ("operation", "engine", "tag_source", "batch", "context", "segments")
BATCH_FIELDS = ("index", "count")
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
class ProcessRequest:
    operation: str
    engine: Optional[str]
    tag_source: str
    batch_index: int
    batch_count: int
    context: Context = field(default_factory=Context)
    segments: Tuple[SegmentInput, ...] = ()


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
                          segments=tuple(segments))


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

    def process(self, request: ProcessRequest) -> Dict[str, object]:
        engine = request.engine or self.configured_engine
        if engine not in self.available_engines():
            raise BadRequest("Unknown engine.", {"available": self.available_engines()})

        records = [
            state_mod.SegmentRecord(
                key=s.key, segment_id=s.key.split("#", 1)[0], ordinal=position,
                batch_index=request.batch_index, source_text=s.source_text,
                mt_target=s.mt_target, user_comment=s.user_comment)
            for position, s in enumerate(request.segments)
        ]
        stage, system_prompt = build_prompt(request, records)
        client = self._client(engine, request.tag_source)

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
