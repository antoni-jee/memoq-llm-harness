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
* a save (PATCH) must name ``expected_revision``; a mismatch is rejected
  with nothing applied; an effective save increments the revision, and a
  save that changes nothing keeps it.

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
BIND_FIELDS = ("original_id", "source_file", "tag_source", "segments", "present_keys")
SEGMENT_FIELDS = ("key", "unit_id", "source_text", "target_text")
PATCH_FIELDS = ("expected_revision", "segments")
UPDATE_FIELDS = ("text", "comment")
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


def _new_state(document_id: str, tag_source: str, entry: dict) -> _MemoryState:
    # Context first: bind_segments reads batch_size, and placeholder text
    # depends on tag_source, so both are set before any state operation.
    state = _MemoryState(_NO_FILE)
    state.batch_size = FIXED_BATCH_SIZE
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

    return BindRequest(original_id=original_id, source_file=source_file,
                       tag_source=tag_source, segments=tuple(segments),
                       present_keys=tuple(present))


def parse_patch_request(body: object) -> PatchRequest:
    if not isinstance(body, dict):
        raise BadRequest("The request body must be a JSON object.")
    unknown = set(body) - set(PATCH_FIELDS)
    if unknown:
        raise BadRequest("Unknown request field(s).", {"fields": _listed(unknown)})
    expected = body.get("expected_revision")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
        raise BadRequest("'expected_revision' must be a positive integer.")
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
        "source_file": state.source_file,
        "current_batch": state.current_batch,
        "order": list(state.order),
        "segments": {key: asdict(record) for key, record in state.segments.items()},
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
                state = _new_state(document_id, request.tag_source, {})
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
