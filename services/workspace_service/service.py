"""workspace-service business logic, independent of HTTP.

Each request rebuilds an in-memory :class:`memoq_harness.state.ProjectState`
for exactly one document from its stored v5 snapshot, runs the existing
logic (``bind_segments``, ``capture_edits``) and stores the new snapshot -
all inside one locked database transaction. ``ProjectState.save`` is never
used; the in-memory subclass refuses it.

Revision semantics:

* bind of a new document creates revision 1;
* a bind that changes nothing (same selection, same original, same stored
  snapshot) is a no-op and keeps the revision;
* any bind that changes the selection, the bound original or the stored
  snapshot increments the revision;
* a save (PATCH) or a suggestions submission must name
  ``expected_revision``; it is compared first, so a mismatch - including a
  replay of an identical earlier request - is rejected with nothing applied;
  an effective change increments the revision, and identical content sent
  with the current revision is a no-op that keeps it.

Error messages and logs contain identifiers only - never segment text or
credentials.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Dict, List, Optional, Protocol, Tuple

from memoq_harness import state as state_mod

from . import SERVICE_NAME
from .repository import DB_STATUS_OK, Decision, RepositoryError, WorkspaceRow

log = logging.getLogger(SERVICE_NAME)

SUPPORTED_STATE_VERSION = state_mod.STATE_VERSION
FIXED_BATCH_SIZE = state_mod.DEFAULT_BATCH_SIZE
"""Global setting in state.json; fixed until the global-settings slice."""

TAG_SOURCES = ("target", "source")
BIND_FIELDS = ("original_id", "source_file", "tag_source", "segments",
               "present_keys", "batch_size")
SEGMENT_FIELDS = ("key", "unit_id", "source_text", "target_text")
PATCH_FIELDS = ("expected_revision", "segments")
UPDATE_FIELDS = ("text", "comment")
SUGGESTION_FIELDS = ("expected_revision", "suggestions", "prefill")
SUGGESTION_ITEM_FIELDS = ("text", "tag_report")
APPLY_FIELDS = ("expected_revision", "batch_index")
# A corrections critique (the remote "Analyse my corrections") is a separate
# slot from the corrections it was computed over; it never overwrites the
# operator's saved text, the model's proposal or the original target.
ANALYSIS_FIELDS = ("expected_revision", "batch_index", "critique")
# An explicit re-batch (the remote "Change batch size") is a revision-checked
# mutation of a bound workspace's working batch size. It is distinct from the
# size a fresh bind carries: this one re-slices an existing document and starts
# a new layout (a layout_version bump) only when the size actually changes.
SET_BATCH_SIZE_FIELDS = ("expected_revision", "batch_size")
MAX_TAG_REPORT_CHARS = 1000
MAX_LISTED = 20
MAX_SOURCE_FILE_CHARS = 255

_ID_RE = re.compile(r"^[0-9a-f]{64}$")
# Never opened: the in-memory state refuses save(), and load() is not used.
_NO_FILE = os.path.join(os.path.abspath(os.sep), "workspace-service", "no-state-file.json")


# --------------------------------------------------------------------- errors


class ServiceError(Exception):
    status = 500
    code = "internal_error"

    def __init__(self, message: str, details: Optional[Dict[str, object]] = None):
        super().__init__(message)
        self.message = message
        self.details = details
        """Identifiers only (keys, field names, numbers) - never segment text."""


class BadRequest(ServiceError):
    status, code = 400, "bad_request"


class NotFound(ServiceError):
    status, code = 404, "not_found"


class TooLarge(ServiceError):
    status, code = 413, "too_large"


class UnknownSegment(ServiceError):
    status, code = 422, "unknown_segment"


class InactiveSegment(ServiceError):
    status, code = 422, "inactive_segment"


class TagSourceMismatch(ServiceError):
    status, code = 409, "tag_source_mismatch"


class WouldDiscardWork(ServiceError):
    status, code = 409, "would_discard_work"


class StaleRevision(ServiceError):
    status, code = 409, "stale_revision"


class UnsupportedStateVersion(ServiceError):
    status, code = 409, "unsupported_state_version"


class Unavailable(ServiceError):
    status, code = 503, "unavailable"


# ------------------------------------------------------------------ contracts


class WorkspaceRepository(Protocol):
    def status(self, expected_version: str) -> str: ...
    def get(self, document_id: str) -> Optional[WorkspaceRow]: ...
    def transact(self, document_id, decide) -> Tuple[Optional[WorkspaceRow], object, bool]: ...


@dataclass(frozen=True)
class BindSegment:
    """The four fields ``ProjectState.bind_segments`` reads from a segment."""

    key: str
    unit_id: str
    source_text: str
    target_text: str


@dataclass(frozen=True)
class BindRequest:
    original_id: str
    source_file: str
    tag_source: str
    segments: Tuple[BindSegment, ...]
    present_keys: Tuple[str, ...]
    batch_size: int = FIXED_BATCH_SIZE


@dataclass(frozen=True)
class SegmentUpdate:
    text: Optional[str] = None
    comment: Optional[str] = None


@dataclass(frozen=True)
class PatchRequest:
    expected_revision: int
    updates: Dict[str, SegmentUpdate]


# ------------------------------------------------------------- state helpers


class _MemoryState(state_mod.ProjectState):
    """ProjectState whose storage is the database row, never a file."""

    def save(self) -> str:  # pragma: no cover - guarded by tests
        raise RuntimeError("workspace-service stores workspaces in PostgreSQL only")

    def document_snapshot(self) -> dict:
        """The local snapshot plus ``batch_size`` and ``layout_version``.

        ``batch_size`` and ``layout_version`` are ``ProjectState`` globals, not
        document fields, so the local ``document_snapshot`` omits them. The
        workspace service is the source of truth for both, so they are injected
        into the persisted snapshot. On a re-read they are recovered from the
        snapshot (via ``_state_from_row``), so the operator's chosen size and the
        current layout version survive a Reload. ``layout_version`` defaults to 0
        on a read, which is the version of every snapshot written before layout
        versioning, so a pre-rebatch correction's feedback never disappears.
        """
        snap = super().document_snapshot()
        snap["batch_size"] = self.batch_size
        snap["layout_version"] = self.layout_version
        return snap


def _coerce_batch_size(value: object) -> int:
    """A positive int, or the fixed default for anything else.

    A corrupt row or a JSON boolean must not pin the size to 0 or 1, so non-int,
    bool and non-positive values fall back to ``FIXED_BATCH_SIZE``.
    """
    if isinstance(value, bool):
        return FIXED_BATCH_SIZE
    try:
        size = int(value)
    except (TypeError, ValueError):
        return FIXED_BATCH_SIZE
    return size if size > 0 else FIXED_BATCH_SIZE


def _new_state(document_id: str, tag_source: str, entry: dict,
               batch_size: Optional[int] = None) -> _MemoryState:
    # Context first: bind_segments reads batch_size, and placeholder text
    # depends on tag_source, so both are set before any state operation.
    state = _MemoryState(_NO_FILE)
    if "batch_size" in entry:
        # A re-read recovers the operator's persisted size from the snapshot.
        state.batch_size = _coerce_batch_size(entry["batch_size"])
    elif batch_size is not None:
        # A fresh bind takes the size the request chose.
        state.batch_size = batch_size
    else:
        state.batch_size = FIXED_BATCH_SIZE
    # The layout version is recovered from the persisted snapshot. A snapshot
    # written before layout versioning carries no such key; the default 0 is the
    # version of every pre-rebatch correction, so its feedback survives.
    state.layout_version = int(entry.get("layout_version", 0) or 0)
    state.tag_source = tag_source
    state.open_document_snapshot(document_id, entry)
    return state


def _state_from_row(row: WorkspaceRow) -> _MemoryState:
    if row.state_version != SUPPORTED_STATE_VERSION:
        raise UnsupportedStateVersion(
            "This workspace was stored in a format this service does not support.",
            {"state_version": row.state_version, "supported": SUPPORTED_STATE_VERSION})
    return _new_state(row.document_id, row.tag_source, row.snapshot)


def _comparable(snapshot: dict) -> dict:
    return {k: v for k, v in snapshot.items() if k != "updated_at"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def has_saved_work(record: state_mod.SegmentRecord) -> bool:
    """Human work a bind must never discard."""
    return bool(
        record.edited_by_human
        or record.user_comment.strip()
        or (record.user_final.strip() and record.user_final != record.mt_target)
    )


def _listed(items) -> List[str]:
    return sorted(items)[:MAX_LISTED]


def require_id(value: str, label: str = "document_id") -> str:
    if not _ID_RE.match(value or ""):
        raise BadRequest("%s must be 64 lowercase hex characters." % label)
    return value


# ---------------------------------------------------------- request parsing


def parse_bind_request(body: object) -> BindRequest:
    """Validate the whole bind request before anything is touched."""
    if not isinstance(body, dict):
        raise BadRequest("The request body must be a JSON object.")
    unknown = set(body) - set(BIND_FIELDS)
    if unknown:
        raise BadRequest("Unknown request field(s).", {"fields": _listed(unknown)})

    original_id = body.get("original_id")
    if not isinstance(original_id, str):
        raise BadRequest("'original_id' is required.")
    require_id(original_id, "original_id")

    source_file = body.get("source_file", "")
    if not isinstance(source_file, str) or len(source_file) > MAX_SOURCE_FILE_CHARS:
        raise BadRequest("'source_file' must be a string of at most %d characters."
                         % MAX_SOURCE_FILE_CHARS)

    tag_source = body.get("tag_source", "target")
    if tag_source not in TAG_SOURCES:
        raise BadRequest("tag_source must be one of: %s." % ", ".join(TAG_SOURCES))

    raw_segments = body.get("segments")
    if not isinstance(raw_segments, list):
        raise BadRequest("'segments' must be a list (the active selection, in order).")
    segments: List[BindSegment] = []
    bad_items = []
    for index, item in enumerate(raw_segments):
        if (not isinstance(item, dict) or set(item) != set(SEGMENT_FIELDS)
                or not all(isinstance(item[f], str) for f in SEGMENT_FIELDS)
                or not item["key"]):
            bad_items.append(index)
            continue
        segments.append(BindSegment(**{f: item[f] for f in SEGMENT_FIELDS}))
    if bad_items:
        raise BadRequest("Each segment needs exactly the string fields: %s (non-empty key)."
                         % ", ".join(SEGMENT_FIELDS), {"items": bad_items[:MAX_LISTED]})
    keys = [s.key for s in segments]
    duplicates = {k for k in keys if keys.count(k) > 1}
    if duplicates:
        raise BadRequest("Duplicate segment keys.", {"keys": _listed(duplicates)})

    present = body.get("present_keys")
    if (not isinstance(present, list)
            or not all(isinstance(k, str) and k for k in present)):
        raise BadRequest("'present_keys' must be a list of non-empty strings "
                         "(every segment key in the full document).")
    present_duplicates = {k for k in present if present.count(k) > 1}
    if present_duplicates:
        raise BadRequest("Duplicate present_keys.", {"keys": _listed(present_duplicates)})
    missing = set(keys) - set(present)
    if missing:
        raise BadRequest("Segment keys missing from present_keys.", {"keys": _listed(missing)})

    # batch_size is optional (defaults to the fixed size on a fresh bind); when
    # present it must be a positive int, never a JSON boolean.
    raw_batch_size = body.get("batch_size", FIXED_BATCH_SIZE)
    if isinstance(raw_batch_size, bool) or not isinstance(raw_batch_size, int):
        raise BadRequest("'batch_size' must be a positive integer.")
    if raw_batch_size <= 0:
        raise BadRequest("'batch_size' must be a positive integer.")

    return BindRequest(original_id=original_id, source_file=source_file,
                       tag_source=tag_source, segments=tuple(segments),
                       present_keys=tuple(present),
                       batch_size=raw_batch_size)


@dataclass(frozen=True)
class Suggestion:
    text: str
    tag_report: str = ""


@dataclass(frozen=True)
class SuggestionRequest:
    expected_revision: int
    suggestions: Dict[str, Suggestion]
    prefill: bool = True


@dataclass(frozen=True)
class ApplyRequest:
    expected_revision: int
    batch_index: int


@dataclass(frozen=True)
class AnalysisRequest:
    """A corrections critique to persist in the separate ``analysis`` slot.

    The remote port of the local ``run_analysis``: the critique is stored keyed
    by the batch string (``str(batch_index)``), exactly like
    ``proj.analysis[str(current_batch)]``, and survives a re-read. It is a
    separate slot from the corrections, so it never overwrites the operator's
    saved text.
    """
    expected_revision: int
    batch_index: int
    critique: str


@dataclass(frozen=True)
class SetBatchSizeRequest:
    """An explicit, revision-checked re-batch of a bound workspace.

    The remote port of the local batch-size change: it names the size the
    operator wants and the revision the view was read at. When the size differs
    from the persisted one the service re-slices the document and starts a new
    layout (a ``layout_version`` bump); a same-size or stale request is a no-op
    that keeps the revision and leaves every stored correction untouched.
    """
    expected_revision: int
    batch_size: int


def _expected_revision(body: dict) -> int:
    expected = body.get("expected_revision")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
        raise BadRequest("'expected_revision' must be a positive integer.")
    return expected


def parse_suggestions_request(body: object) -> SuggestionRequest:
    """Validate a suggestions submission completely before anything is touched.

    Text must be a string that is not empty or whitespace-only (the app keeps
    memoQ's target when the model returns nothing); accepted text is stored
    exactly as sent, without trimming.
    """
    if not isinstance(body, dict):
        raise BadRequest("The request body must be a JSON object.")
    unknown = set(body) - set(SUGGESTION_FIELDS)
    if unknown:
        raise BadRequest("Unknown request field(s).", {"fields": _listed(unknown)})
    expected = _expected_revision(body)
    raw = body.get("suggestions")
    if not isinstance(raw, dict) or not raw:
        raise BadRequest("'suggestions' must be a non-empty object mapping segment keys "
                         "to {\"text\", \"tag_report\"?}.")
    bad, empty = [], []
    for key, item in raw.items():
        if (not isinstance(item, dict) or "text" not in item
                or set(item) - set(SUGGESTION_ITEM_FIELDS)
                or not all(isinstance(v, str) for v in item.values())
                or len(item.get("tag_report", "")) > MAX_TAG_REPORT_CHARS):
            bad.append(key)
        elif not item["text"].strip():
            empty.append(key)
    if bad:
        raise BadRequest("Each suggestion must be an object with a string 'text' and an "
                         "optional string 'tag_report' (at most %d characters)."
                         % MAX_TAG_REPORT_CHARS, {"keys": _listed(bad)})
    if empty:
        raise BadRequest("Suggestion text must not be empty or whitespace-only; omit the "
                         "segment instead.", {"keys": _listed(empty)})
    prefill = body.get("prefill", True)
    if not isinstance(prefill, bool):
        raise BadRequest("'prefill' must be a boolean when supplied.")
    return SuggestionRequest(
        expected_revision=expected,
        suggestions={k: Suggestion(text=v["text"], tag_report=v.get("tag_report", ""))
                     for k, v in raw.items()},
        prefill=prefill,
    )


def parse_patch_request(body: object) -> PatchRequest:
    if not isinstance(body, dict):
        raise BadRequest("The request body must be a JSON object.")
    unknown = set(body) - set(PATCH_FIELDS)
    if unknown:
        raise BadRequest("Unknown request field(s).", {"fields": _listed(unknown)})
    expected = _expected_revision(body)
    raw = body.get("segments")
    if not isinstance(raw, dict) or not raw:
        raise BadRequest("'segments' must be a non-empty object mapping segment keys "
                         "to {\"text\"?, \"comment\"?}.")
    bad = [k for k, v in raw.items()
           if not isinstance(v, dict) or not v or set(v) - set(UPDATE_FIELDS)
           or not all(isinstance(x, str) for x in v.values())]
    if bad:
        raise BadRequest("Each update must be a non-empty object with string 'text' "
                         "and/or 'comment'.", {"keys": _listed(bad)})
    return PatchRequest(
        expected_revision=expected,
        updates={k: SegmentUpdate(text=v.get("text"), comment=v.get("comment"))
                 for k, v in raw.items()},
    )


def parse_apply_request(body: object) -> ApplyRequest:
    """Validate an apply-corrects request completely before anything is touched.

    ``batch_index`` names the batch whose human edits become corrections; it is a
    non-negative integer (a batch index, never a revision, so it may be 0).
    """
    if not isinstance(body, dict):
        raise BadRequest("The request body must be a JSON object.")
    unknown = set(body) - set(APPLY_FIELDS)
    if unknown:
        raise BadRequest("Unknown request field(s).", {"fields": _listed(unknown)})
    expected = _expected_revision(body)
    batch_index = body.get("batch_index")
    if not isinstance(batch_index, int) or isinstance(batch_index, bool) or batch_index < 0:
        raise BadRequest("'batch_index' must be a non-negative integer.")
    return ApplyRequest(expected_revision=expected, batch_index=batch_index)


def parse_analysis_request(body: object) -> AnalysisRequest:
    """Validate a record-analysis request completely before anything is touched.

    ``batch_index`` names the batch the critique was computed over (a non-negative
    integer, so it may be 0); it is stored as the key ``str(batch_index)``. The
    ``critique`` is stored exactly as sent (no trimming), like the other writes.
    """
    if not isinstance(body, dict):
        raise BadRequest("The request body must be a JSON object.")
    unknown = set(body) - set(ANALYSIS_FIELDS)
    if unknown:
        raise BadRequest("Unknown request field(s).", {"fields": _listed(unknown)})
    expected = _expected_revision(body)
    batch_index = body.get("batch_index")
    if not isinstance(batch_index, int) or isinstance(batch_index, bool) or batch_index < 0:
        raise BadRequest("'batch_index' must be a non-negative integer.")
    critique = body.get("critique")
    if not isinstance(critique, str):
        raise BadRequest("'critique' must be a string.")
    return AnalysisRequest(expected_revision=expected, batch_index=batch_index,
                           critique=critique)


def parse_set_batch_size_request(body: object) -> SetBatchSizeRequest:
    """Validate a re-batch request completely before anything is touched.

    ``batch_size`` is a positive integer (the same rule a bind applies), never a
    JSON boolean; ``expected_revision`` is checked like every other mutation.
    The request only names the size and the revision - the service re-slices and
    starts a new layout itself.
    """
    if not isinstance(body, dict):
        raise BadRequest("The request body must be a JSON object.")
    unknown = set(body) - set(SET_BATCH_SIZE_FIELDS)
    if unknown:
        raise BadRequest("Unknown request field(s).", {"fields": _listed(unknown)})
    expected = _expected_revision(body)
    raw_batch_size = body.get("batch_size")
    if isinstance(raw_batch_size, bool) or not isinstance(raw_batch_size, int):
        raise BadRequest("'batch_size' must be a positive integer.")
    if raw_batch_size <= 0:
        raise BadRequest("'batch_size' must be a positive integer.")
    return SetBatchSizeRequest(expected_revision=expected, batch_size=raw_batch_size)


# ------------------------------------------------------------------ output


def _iso(value) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else value


def workspace_to_dict(row: WorkspaceRow, state: state_mod.ProjectState) -> Dict[str, object]:
    return {
        "document_id": row.document_id,
        "revision": row.revision,
        "state_version": row.state_version,
        "bound_original_id": row.bound_original_id,
        "tag_source": row.tag_source,
        "batch_size": state.batch_size,
        "layout_version": state.layout_version,
        "source_file": state.source_file,
        "current_batch": state.current_batch,
        "order": list(state.order),
        "segments": {key: asdict(record) for key, record in state.segments.items()},
        "corrections": [asdict(c) for c in state.corrections],
        # The per-batch corrections critique, a separate slot that never
        # overwrites the operator's saved text. It mirrors the local
        # ``proj.analysis`` (keyed by the batch string), so the remote
        # "Analyse my corrections" action persists the same way.
        "analysis": dict(state.analysis),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def bind_summary(row: WorkspaceRow, state: state_mod.ProjectState) -> Dict[str, object]:
    return {
        "document_id": row.document_id,
        "revision": row.revision,
        "bound_original_id": row.bound_original_id,
        "tag_source": row.tag_source,
        "batch_size": state.batch_size,
        "order": list(state.order),
        "counts": {
            "segments": len(state.segments),
            "active": len(state.order),
            "with_saved_work": sum(1 for r in state.segments.values() if has_saved_work(r)),
        },
    }


# ------------------------------------------------------------------ service


class WorkspaceService:
    def __init__(self, repository: WorkspaceRepository, schema_version: str,
                 max_body_bytes: int):
        self.repository = repository
        self.schema_version = schema_version
        self.max_body_bytes = max_body_bytes

    def _transact(self, document_id: str, decide) -> Tuple[Optional[WorkspaceRow], object, bool]:
        try:
            return self.repository.transact(document_id, decide)
        except RepositoryError as exc:
            raise Unavailable(str(exc)) from None

    # ------------------------------------------------------------------ bind

    def bind(self, document_id: str, request: BindRequest) -> Dict[str, object]:
        require_id(document_id)

        def decide(current: Optional[WorkspaceRow]) -> Decision:
            if current is None:
                # A fresh bind takes the size the request chose; a re-read
                # (else branch) recovers the persisted size via _state_from_row.
                state = _new_state(document_id, request.tag_source, {},
                                   batch_size=request.batch_size)
                before = None
            else:
                if current.tag_source != request.tag_source:
                    raise TagSourceMismatch(
                        "This workspace uses a different tag_source; placeholder text "
                        "would not match its saved work.",
                        {"stored": current.tag_source, "requested": request.tag_source})
                state = _state_from_row(current)
                before = _comparable(state.document_snapshot())

            present = set(request.present_keys)
            supplied = {s.key: s for s in request.segments}
            endangered = [
                key for key, record in state.segments.items()
                if has_saved_work(record) and (
                    key not in present
                    or (key in supplied and supplied[key].source_text != record.source_text))
            ]
            if endangered:
                raise WouldDiscardWork(
                    "The bind would discard saved human work.",
                    {"keys": _listed(endangered), "count": len(endangered)})

            state.bind_segments(list(request.segments), request.source_file, "",
                                present_keys=present)
            after = _comparable(state.document_snapshot())
            if (current is not None and after == before
                    and current.bound_original_id == request.original_id):
                return None, False  # identical bind: no-op, revision kept
            state.updated_at = _now()
            row = WorkspaceRow(
                document_id=document_id, snapshot=state.document_snapshot(),
                state_version=SUPPORTED_STATE_VERSION,
                revision=1 if current is None else current.revision + 1,
                tag_source=request.tag_source, bound_original_id=request.original_id)
            return row, current is None

        stored, created, wrote = self._transact(document_id, decide)
        state = _state_from_row(stored)
        log.info("bind %s: document %s revision %d (%s)", "created" if created else "merged",
                 document_id[:12], stored.revision, "changed" if wrote else "no change")
        summary = bind_summary(stored, state)
        summary["created"] = bool(created)
        summary["changed"] = wrote
        return summary

    # ------------------------------------------------------------------ save

    def save_segments(self, document_id: str, request: PatchRequest) -> Dict[str, object]:
        require_id(document_id)

        def decide(current: Optional[WorkspaceRow]) -> Decision:
            if current is None:
                raise NotFound("Unknown workspace; bind the document first.")
            state = _state_from_row(current)
            if current.revision != request.expected_revision:
                raise StaleRevision(
                    "The workspace changed since it was read; reload and retry.",
                    {"expected_revision": request.expected_revision,
                     "current_revision": current.revision})
            unknown = [k for k in request.updates if k not in state.segments]
            if unknown:
                raise UnknownSegment("Updates name segment keys this workspace does not have.",
                                     {"keys": _listed(unknown), "count": len(unknown)})

            def snapshot_of(key):
                r = state.segments[key]
                return (r.user_final, r.user_comment, r.edited_by_human)

            before = {key: snapshot_of(key) for key in request.updates}
            texts = {k: u.text for k, u in request.updates.items() if u.text is not None}
            text_comments = {k: u.comment for k, u in request.updates.items()
                             if u.text is not None and u.comment is not None}
            # Text (with its optional comment) goes through the existing logic,
            # which also decides edited_by_human.
            state.capture_edits(texts, text_comments)
            # Comment-only updates touch the comment and nothing else.
            for key, update in request.updates.items():
                if update.text is None and update.comment is not None:
                    state.segments[key].user_comment = update.comment

            changed = sorted(k for k in request.updates if snapshot_of(k) != before[k])
            if not changed:
                return None, []  # identical save: no-op, revision kept
            state.updated_at = _now()
            row = WorkspaceRow(
                document_id=document_id, snapshot=state.document_snapshot(),
                state_version=SUPPORTED_STATE_VERSION, revision=current.revision + 1,
                tag_source=current.tag_source, bound_original_id=current.bound_original_id)
            return row, changed

        stored, changed, wrote = self._transact(document_id, decide)
        log.info("save: document %s revision %d (%d segment(s) changed)",
                 document_id[:12], stored.revision, len(changed))
        return {"document_id": document_id, "revision": stored.revision,
                "updated_keys": changed, "changed": wrote}

    # ----------------------------------------------------------- suggestions

    def record_suggestions(self, document_id: str,
                           request: SuggestionRequest) -> Dict[str, object]:
        """Store externally produced suggestions with the app's own rule.

        ``ProjectState.record_suggestions`` sets ``llm_suggestion``,
        ``tag_report`` and ``status="processed"``; when the request asks for it
        (``prefill``, the default) the suggestion is also copied into
        ``user_final`` for a row the human has not edited. A human's saved text
        is protected by ``edited_by_human`` and never overwritten. With
        ``prefill`` off the proposal is stored only, so a model suggestion is
        not presented as the user's accepted text. The response names which
        rows were prefilled and which were protected.
        """
        require_id(document_id)
        prefill = request.prefill

        def decide(current: Optional[WorkspaceRow]) -> Decision:
            if current is None:
                raise NotFound("Unknown workspace; bind the document first.")
            state = _state_from_row(current)
            # Revision first: a replay of an old request is stale even if its
            # content would change nothing.
            if current.revision != request.expected_revision:
                raise StaleRevision(
                    "The workspace changed since it was read; reload and retry.",
                    {"expected_revision": request.expected_revision,
                     "current_revision": current.revision})
            keys = list(request.suggestions)
            unknown = [k for k in keys if k not in state.segments]
            if unknown:
                raise UnknownSegment("Suggestions name segment keys this workspace does not "
                                     "have.", {"keys": _listed(unknown), "count": len(unknown)})
            active = set(state.order)
            inactive = [k for k in keys if k not in active]
            if inactive:
                raise InactiveSegment(
                    "Suggestions name segments outside the active selection.",
                    {"keys": _listed(inactive), "count": len(inactive)})

            protected = sorted(k for k in keys if state.segments[k].edited_by_human)
            prefilled = sorted(k for k in keys
                               if prefill and not state.segments[k].edited_by_human)
            before = _comparable(state.document_snapshot())
            state.record_suggestions(
                {k: s.text for k, s in request.suggestions.items()},
                {k: s.tag_report for k, s in request.suggestions.items()},
                prefill=prefill)
            payload = {"recorded_keys": sorted(keys), "prefilled_keys": prefilled,
                       "protected_keys": protected}
            if _comparable(state.document_snapshot()) == before:
                return None, payload  # identical content at the current revision: no-op
            state.updated_at = _now()
            row = WorkspaceRow(
                document_id=document_id, snapshot=state.document_snapshot(),
                state_version=SUPPORTED_STATE_VERSION, revision=current.revision + 1,
                tag_source=current.tag_source, bound_original_id=current.bound_original_id)
            return row, payload

        stored, payload, wrote = self._transact(document_id, decide)
        log.info("suggestions: document %s revision %d (%d recorded, %d prefilled, "
                 "%d protected, %s)", document_id[:12], stored.revision,
                 len(payload["recorded_keys"]), len(payload["prefilled_keys"]),
                 len(payload["protected_keys"]), "changed" if wrote else "no change")
        result = {"document_id": document_id, "revision": stored.revision, "changed": wrote}
        result.update(payload)
        return result

    # ----------------------------------------------------------- apply corrections

    def apply_corrections(self, document_id: str,
                          request: ApplyRequest) -> Dict[str, object]:
        """Turn one batch's human edits into stored corrections.

        The remote port of the local "Apply Changes & Calculate Diffs": it
        rebuilds the document's state and runs the existing ``calculate_diffs``
        logic, which reads each row's saved ``user_final`` against the model's
        ``llm_suggestion`` (or memoQ's MT when that is empty) and the saved
        comment. A row that differs, or carries a comment, becomes a
        :class:`~memoq_harness.state.Correction`; a row the human left untouched
        is implicit approval and carries no signal. The produced corrections are
        stored in the document's snapshot, so they survive a re-read and feed the
        next batch's feedback. Re-applying a batch replaces its earlier
        corrections rather than duplicating them.

        Revision semantics mirror the other mutations: the revision is checked
        first, so a stale or replayed apply is rejected with nothing stored; an
        effective change increments the revision, and an apply that changes
        nothing is a no-op that keeps it.
        """
        require_id(document_id)

        def decide(current: Optional[WorkspaceRow]) -> Decision:
            if current is None:
                raise NotFound("Unknown workspace; bind the document first.")
            state = _state_from_row(current)
            if current.revision != request.expected_revision:
                raise StaleRevision(
                    "The workspace changed since it was read; reload and retry.",
                    {"expected_revision": request.expected_revision,
                     "current_revision": current.revision})
            produced = state.calculate_diffs(request.batch_index)
            corrections = [asdict(c) for c in state.corrections]
            if _comparable(state.document_snapshot()) == _comparable(current.snapshot):
                return None, {"changed": False, "produced": len(produced),
                              "corrections": corrections}
            state.updated_at = _now()
            row = WorkspaceRow(
                document_id=document_id, snapshot=state.document_snapshot(),
                state_version=SUPPORTED_STATE_VERSION, revision=current.revision + 1,
                tag_source=current.tag_source, bound_original_id=current.bound_original_id)
            return row, {"changed": True, "produced": len(produced),
                         "corrections": corrections}

        stored, payload, wrote = self._transact(document_id, decide)
        log.info("apply: document %s revision %d (%d produced, %s)",
                 document_id[:12], stored.revision, payload["produced"],
                 "changed" if wrote else "no change")
        result = {"document_id": document_id, "revision": stored.revision}
        result.update(payload)
        return result

    def record_analysis(self, document_id: str,
                        request: AnalysisRequest) -> Dict[str, object]:
        """Persist a corrections critique in the separate ``analysis`` slot.

        The remote port of the local "Analyse my corrections": the critique the
        processing service computed over the operator's stored corrections is
        stored keyed by the batch string (``str(batch_index)``), exactly like the
        local ``proj.analysis[str(current_batch)]``, so it survives a re-read and
        shows in the read-only UI. It is a separate slot from the corrections,
        the saved text, the model's proposal and the original target, so it never
        overwrites any of them; re-analysing a batch replaces its earlier
        critique rather than duplicating it.

        Revision semantics mirror the other mutations: the revision is checked
        first, so a stale or replayed write is rejected with nothing stored; an
        effective change increments the revision, and a critique identical to the
        one already stored is a no-op that keeps it.
        """
        require_id(document_id)

        def decide(current: Optional[WorkspaceRow]) -> Decision:
            if current is None:
                raise NotFound("Unknown workspace; bind the document first.")
            state = _state_from_row(current)
            if current.revision != request.expected_revision:
                raise StaleRevision(
                    "The workspace changed since it was read; reload and retry.",
                    {"expected_revision": request.expected_revision,
                     "current_revision": current.revision})
            state.analysis[str(request.batch_index)] = request.critique
            if _comparable(state.document_snapshot()) == _comparable(current.snapshot):
                return None, {"changed": False,
                              "analysis": dict(state.analysis)}
            state.updated_at = _now()
            row = WorkspaceRow(
                document_id=document_id, snapshot=state.document_snapshot(),
                state_version=SUPPORTED_STATE_VERSION, revision=current.revision + 1,
                tag_source=current.tag_source, bound_original_id=current.bound_original_id)
            return row, {"changed": True, "analysis": dict(state.analysis)}

        stored, payload, wrote = self._transact(document_id, decide)
        log.info("analysis: document %s revision %d (%s)",
                 document_id[:12], stored.revision,
                 "changed" if wrote else "no change")
        result = {"document_id": document_id, "revision": stored.revision}
        result.update(payload)
        return result

    def set_batch_size(self, document_id: str,
                       request: "SetBatchSizeRequest") -> Dict[str, object]:
        """Re-batch a bound workspace into a new working layout.

        The remote port of the local batch-size change. When the requested size
        differs from the persisted one, the document is re-sliced and a *new
        layout* starts: ``layout_version`` is bumped by one, the segments get
        their new ``batch_index``, and ``current_batch`` is clamped into range.
        The stored corrections are left untouched - they keep the original
        ``batch_index`` and the ``layout_version`` they were produced under - so
        the new layout's feedback never picks up an earlier layout's correction
        even when the batch indices collide, and a later apply in the new layout
        cannot erase the old layout's history.

        A same-size request is a no-op: it neither re-slices nor bumps the
        version (so a reload, a repeat bind or a same-size change never starts a
        new layout), keeping the revision. A stale ``expected_revision`` is
        rejected with nothing mutated.
        """
        require_id(document_id)

        def decide(current: Optional[WorkspaceRow]) -> Decision:
            if current is None:
                raise NotFound("Unknown workspace; bind the document first.")
            if current.revision != request.expected_revision:
                raise StaleRevision(
                    "The workspace changed since it was read; reload and retry.",
                    {"expected_revision": request.expected_revision,
                     "current_revision": current.revision})
            # A same-size request reshapes nothing: no re-slice, no new layout,
            # no revision change, and every correction stays as it is.
            current_batch_size = int(current.snapshot.get("batch_size",
                                                          FIXED_BATCH_SIZE) or 0)
            if request.batch_size == current_batch_size:
                return None, {
                    "changed": False,
                    "layout_version": int(current.snapshot.get("layout_version", 0) or 0),
                    "batch_size": request.batch_size,
                }
            state = _state_from_row(current)
            before = _comparable(state.document_snapshot())
            state.rebatch(request.batch_size)
            # The size actually changed: start a new layout. The corrections keep
            # their original batch_index and the layout_version they were produced
            # under, so the new layout's feedback never adopts the old one's.
            state.layout_version += 1
            if _comparable(state.document_snapshot()) == before:
                return None, {"changed": False, "layout_version": state.layout_version,
                              "batch_size": request.batch_size}
            state.updated_at = _now()
            row = WorkspaceRow(
                document_id=document_id, snapshot=state.document_snapshot(),
                state_version=SUPPORTED_STATE_VERSION, revision=current.revision + 1,
                tag_source=current.tag_source, bound_original_id=current.bound_original_id)
            return row, {"changed": True, "layout_version": state.layout_version,
                         "batch_size": request.batch_size}

        stored, payload, wrote = self._transact(document_id, decide)
        log.info("set_batch_size: document %s revision %d (size %d, layout %d, %s)",
                 document_id[:12], stored.revision, payload["batch_size"],
                 payload["layout_version"], "changed" if wrote else "no change")
        result = {"document_id": document_id, "revision": stored.revision}
        result.update(payload)
        return result

    # ------------------------------------------------------------------ read

    def get(self, document_id: str) -> Dict[str, object]:
        require_id(document_id)
        try:
            row = self.repository.get(document_id)
        except RepositoryError as exc:
            raise Unavailable(str(exc)) from None
        if row is None:
            raise NotFound("Unknown workspace.")
        return workspace_to_dict(row, _state_from_row(row))

    # ------------------------------------------------------------- readiness

    def readiness(self) -> Dict[str, object]:
        database = self.repository.status(self.schema_version)
        ready = database == DB_STATUS_OK
        return {"status": "ready" if ready else "not_ready", "checks": {"database": database}}
