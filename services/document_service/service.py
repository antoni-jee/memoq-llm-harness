"""document-service business logic, independent of HTTP.

Upload order of operations, chosen so a failure never leaves a database row
pointing at a missing file:

1. validate size and parse the bytes as MQXLIFF (nothing stored on failure);
2. derive ``document_id`` (source fingerprint) and ``original_id`` (SHA-256);
3. if the original is already recorded, make sure its file is present and
   return it unchanged (idempotent);
4. write the file atomically, then insert the database rows in one
   transaction.

A failed insert does **not** delete the file. Files are content-addressed, so
an overlapping upload of the same bytes may already have committed a record
that points at exactly this file; no check made before deleting could rule
that out without a race. An unreferenced file is harmless: it holds exactly
the bytes its name promises, it is never served without a database record,
and the next successful upload of the same bytes reuses it.

Error messages never contain document text or credentials.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Protocol, Tuple

from memoq_harness import mqxliff, tags as tagmod

from . import SERVICE_NAME
from .repository import (
    DB_STATUS_OK, DocumentRecord, ExportRecord, OriginalRecord, RepositoryError,
)
from .storage import EXPORTS, ORIGINALS, FileStorage, StorageError

log = logging.getLogger(SERVICE_NAME)

_ID_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_FILENAME_CHARS = 200
TAG_SOURCES = (tagmod.TAG_SOURCE_TARGET, tagmod.TAG_SOURCE_SOURCE)
FILTER_FLAGS = ("skip_confirmed", "skip_locked", "use_status_attribute",
                "use_target_state", "require_nonempty_source")
EXPORT_REQUEST_FIELDS = ("translations", "tag_source", "filter", "mark_confirmed")
TAG_REPORT_FIELDS = ("tag_source", "texts")
MAX_TAG_REPORT_KEYS = 5000
MAX_LISTED_KEYS = 20


# --------------------------------------------------------------------- errors


class ServiceError(Exception):
    """Base class: ``status`` and ``code`` map onto the HTTP response."""

    status = 500
    code = "internal_error"

    def __init__(self, message: str, details: Optional[Dict[str, object]] = None):
        super().__init__(message)
        self.message = message
        self.details = details
        """Identifiers only (segment keys, field names) - never document text."""


class BadRequest(ServiceError):
    status, code = 400, "bad_request"


class InvalidDocument(ServiceError):
    status, code = 422, "invalid_mqxliff"


class TooLarge(ServiceError):
    status, code = 413, "too_large"


class NotFound(ServiceError):
    status, code = 404, "not_found"


class Unavailable(ServiceError):
    status, code = 503, "unavailable"


class Inconsistent(ServiceError):
    status, code = 500, "storage_inconsistent"


class UnknownSegment(ServiceError):
    status, code = 422, "unknown_segment"


class NonTranslatableSegment(ServiceError):
    status, code = 422, "non_translatable_segment"


class EmptyTranslation(ServiceError):
    status, code = 422, "empty_translation"


class ExportFailed(ServiceError):
    status, code = 500, "export_failed"


class ExportValidationFailed(ServiceError):
    status, code = 500, "export_validation_failed"


# ------------------------------------------------------------------ contracts


class DocumentRepository(Protocol):
    def status(self, expected_version: str) -> str: ...
    def add_original(self, record: OriginalRecord) -> Tuple[OriginalRecord, bool]: ...
    def get_original(self, original_id: str) -> Optional[OriginalRecord]: ...
    def get_document(self, document_id: str) -> Optional[DocumentRecord]: ...
    def add_export(self, record: ExportRecord) -> Tuple[ExportRecord, bool]: ...
    def get_export(self, export_id: str) -> Optional[ExportRecord]: ...


@dataclass(frozen=True)
class UploadResult:
    original: OriginalRecord
    created: bool


@dataclass(frozen=True)
class ExportRequest:
    translations: Dict[str, str]
    tag_source: str
    seg_filter: mqxliff.SegmentFilter
    mark_confirmed: bool


@dataclass(frozen=True)
class ExportResult:
    export: ExportRecord
    created: bool


# -------------------------------------------------------------------- helpers


def sanitize_filename(name: Optional[str]) -> str:
    """Display name only - never used in a storage path."""
    base = os.path.basename((name or "").replace("\\", "/"))
    cleaned = "".join(c for c in unicodedata.normalize("NFC", base)
                      if unicodedata.category(c)[0] != "C").strip()
    return cleaned[:MAX_FILENAME_CHARS] or "document.mqxliff"


def _listed(keys) -> List[str]:
    return sorted(keys)[:MAX_LISTED_KEYS]


def parse_export_request(body: object) -> ExportRequest:
    """Validate the JSON shape of an export request (not the segment keys)."""
    if not isinstance(body, dict):
        raise BadRequest("The request body must be a JSON object.")
    unknown = set(body) - set(EXPORT_REQUEST_FIELDS)
    if unknown:
        raise BadRequest("Unknown request field(s).", {"fields": _listed(unknown)})

    translations = body.get("translations")
    if not isinstance(translations, dict) or not translations:
        raise BadRequest("'translations' must be a non-empty object mapping segment "
                         "keys to placeholder text.")
    not_text = [k for k, v in translations.items() if not isinstance(v, str)]
    if not_text:
        raise BadRequest("Translation values must be strings.", {"keys": _listed(not_text)})

    tag_source = body.get("tag_source", tagmod.TAG_SOURCE_TARGET)
    if tag_source not in TAG_SOURCES:
        raise BadRequest("tag_source must be one of: %s." % ", ".join(TAG_SOURCES))

    flags = body.get("filter", {})
    if not isinstance(flags, dict):
        raise BadRequest("'filter' must be an object of boolean flags.")
    unknown_flags = set(flags) - set(FILTER_FLAGS)
    if unknown_flags:
        raise BadRequest("Unknown filter flag(s).", {"fields": _listed(unknown_flags)})
    if any(not isinstance(v, bool) for v in flags.values()):
        raise BadRequest("Filter flags must be true or false.")

    mark_confirmed = body.get("mark_confirmed", False)
    if not isinstance(mark_confirmed, bool):
        raise BadRequest("'mark_confirmed' must be true or false.")

    return ExportRequest(
        translations=dict(translations),
        tag_source=tag_source,
        seg_filter=mqxliff.SegmentFilter(**flags),
        mark_confirmed=mark_confirmed,
    )


def parse_tag_report_request(body: object) -> Tuple[str, Dict[str, str]]:
    """Validate a tag-report request completely: ``(tag_source, texts)``."""
    if not isinstance(body, dict):
        raise BadRequest("The request body must be a JSON object.")
    unknown = set(body) - set(TAG_REPORT_FIELDS)
    if unknown:
        raise BadRequest("Unknown request field(s).", {"fields": _listed(unknown)})
    tag_source = body.get("tag_source", tagmod.TAG_SOURCE_TARGET)
    if tag_source not in TAG_SOURCES:
        raise BadRequest("tag_source must be one of: %s." % ", ".join(TAG_SOURCES))
    texts = body.get("texts")
    if not isinstance(texts, dict) or not texts:
        raise BadRequest("'texts' must be a non-empty object mapping segment keys to "
                         "placeholder text.")
    if len(texts) > MAX_TAG_REPORT_KEYS:
        raise BadRequest("At most %d texts per request." % MAX_TAG_REPORT_KEYS)
    not_text = [k for k, v in texts.items() if not isinstance(v, str)]
    if not_text:
        raise BadRequest("Text values must be strings.", {"keys": _listed(not_text)})
    return tag_source, dict(texts)


def require_id(value: str, label: str) -> str:
    if not _ID_RE.match(value or ""):
        raise BadRequest("%s must be 64 lowercase hex characters." % label)
    return value


def serialize_segment(segment: mqxliff.Segment) -> Dict[str, object]:
    def tag_list(pairs):
        return [{"number": n, "xml": xml} for n, xml in pairs]

    return {
        "key": segment.key,
        "unit_id": segment.unit_id,
        "ordinal": segment.ordinal,
        "source_text": segment.source_text,
        "target_text": segment.target_text,
        "tag_source": segment.tag_source,
        "expected_numbers": list(segment.expected_numbers),
        "tags": tag_list(segment.raw_tags()),
        "source_tags": tag_list(segment.side_tags(tagmod.TAG_SOURCE_SOURCE)),
        "target_tags": tag_list(segment.side_tags(tagmod.TAG_SOURCE_TARGET)),
        "confirmed": segment.confirmed,
        "locked": segment.locked,
        "status": segment.status,
        "target_state": segment.target_state,
        "skip_reason": segment.skip_reason,
        "translatable": segment.translatable,
    }


def original_to_dict(record: OriginalRecord) -> Dict[str, object]:
    return {
        "original_id": record.original_id,
        "document_id": record.document_id,
        "filename": record.filename,
        "size_bytes": record.size_bytes,
        "segment_count": record.segment_count,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def export_to_dict(record: ExportRecord) -> Dict[str, object]:
    return {
        "export_id": record.export_id,
        "original_id": record.original_id,
        "document_id": record.document_id,
        "filename": record.filename,
        "size_bytes": record.size_bytes,
        "segments_written": record.segments_written,
        "tag_source": record.tag_source,
        "mark_confirmed": record.mark_confirmed,
        "warnings": list(record.warnings),
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def _parse(raw: bytes, filename: str) -> mqxliff.MqxliffDocument:
    try:
        return mqxliff.MqxliffDocument.from_bytes(raw, filename)
    except mqxliff.MqxliffError:
        # The parser's own message can quote document content; keep it out.
        raise InvalidDocument("The file is not a readable memoQ MQXLIFF document.") from None


# -------------------------------------------------------------------- service


class DocumentService:
    def __init__(self, repository: DocumentRepository, storage: FileStorage,
                 max_upload_bytes: int, schema_version: str):
        self.repository = repository
        self.storage = storage
        self.max_upload_bytes = max_upload_bytes
        self.schema_version = schema_version

    # ----------------------------------------------------------------- upload

    def upload(self, raw: bytes, filename: Optional[str]) -> UploadResult:
        if not raw:
            raise BadRequest("The uploaded file is empty.")
        if len(raw) > self.max_upload_bytes:
            raise TooLarge("The file exceeds the %d-byte upload limit." % self.max_upload_bytes)

        name = sanitize_filename(filename)
        document = _parse(raw, name)
        try:
            document_id = document.source_fingerprint()
            segment_count = len(document.parse_segments())
        except mqxliff.MqxliffError:
            raise InvalidDocument("The file is not a readable memoQ MQXLIFF document.") from None
        original_id = hashlib.sha256(raw).hexdigest()

        existing = self._repo(lambda: self.repository.get_original(original_id))
        if existing is not None:
            self._store(original_id, raw)  # repairs a missing file; no-op otherwise
            log.info("upload repeated: original %s (document %s)", original_id[:12], document_id[:12])
            return UploadResult(existing, created=False)

        self._store(original_id, raw)
        record = OriginalRecord(
            original_id=original_id, document_id=document_id, filename=name,
            size_bytes=len(raw), segment_count=segment_count,
        )
        try:
            stored, created = self.repository.add_original(record)
        except RepositoryError as exc:
            # Keep the file: a concurrent upload of the same bytes may own it.
            log.warning("upload failed at database insert; file %s kept for reuse",
                        original_id[:12])
            raise Unavailable(str(exc)) from None
        log.info("upload %s: original %s (document %s, %d segments)",
                 "stored" if created else "repeated", original_id[:12], document_id[:12],
                 segment_count)
        return UploadResult(stored, created)

    def _store(self, file_id: str, raw: bytes, kind: str = ORIGINALS) -> bool:
        try:
            return self.storage.write(file_id, raw, kind)
        except StorageError as exc:
            raise Unavailable(str(exc)) from None

    def _repo(self, call):
        try:
            return call()
        except RepositoryError as exc:
            raise Unavailable(str(exc)) from None

    # ------------------------------------------------------------------ reads

    def get_document(self, document_id: str) -> DocumentRecord:
        require_id(document_id, "document_id")
        record = self._repo(lambda: self.repository.get_document(document_id))
        if record is None:
            raise NotFound("Unknown document_id.")
        return record

    def get_original(self, original_id: str) -> OriginalRecord:
        require_id(original_id, "original_id")
        record = self._repo(lambda: self.repository.get_original(original_id))
        if record is None:
            raise NotFound("Unknown original_id.")
        return record

    def original_content(self, original_id: str) -> Tuple[OriginalRecord, bytes]:
        record = self.get_original(original_id)
        try:
            data = self.storage.read(original_id)
        except StorageError as exc:
            raise Unavailable(str(exc)) from None
        if data is None or hashlib.sha256(data).hexdigest() != original_id:
            log.error("stored file missing or altered for original %s", original_id[:12])
            raise Inconsistent("The stored original file is missing or damaged.")
        return record, data

    def segments(self, original_id: str, seg_filter: mqxliff.SegmentFilter,
                 tag_source: str) -> Tuple[OriginalRecord, List[Dict[str, object]]]:
        if tag_source not in TAG_SOURCES:
            raise BadRequest("tag_source must be one of: %s." % ", ".join(TAG_SOURCES))
        record, data = self.original_content(original_id)
        try:
            parsed = _parse(data, record.filename).parse_segments(seg_filter, tag_source)
        except mqxliff.MqxliffError:
            raise Inconsistent("The stored original can no longer be parsed.") from None
        return record, [serialize_segment(s) for s in parsed]

    # ------------------------------------------------------------ tag reports

    def tag_reports(self, original_id: str, tag_source: str,
                    texts: Dict[str, str]) -> Tuple[OriginalRecord, Dict[str, str]]:
        """Tag-integrity summary per text, against this original's tags.

        Read-only. Exactly the app's ``tag_report_for``: ``tags.restore`` with
        the segment's lookup and expected placeholder numbers; "" when clean,
        otherwise the report summary (e.g. "missing 2"). The ``tag_source``
        must match the one the texts' placeholders were numbered with.
        """
        if tag_source not in TAG_SOURCES:
            raise BadRequest("tag_source must be one of: %s." % ", ".join(TAG_SOURCES))
        record, data = self.original_content(original_id)
        try:
            segments = mqxliff.MqxliffDocument.from_bytes(data, record.filename).parse_segments(
                mqxliff.SegmentFilter(), tag_source)
        except mqxliff.MqxliffError:
            raise Inconsistent("The stored original can no longer be parsed.") from None
        by_key = {s.key: s for s in segments}
        unknown = [k for k in texts if k not in by_key]
        if unknown:
            raise UnknownSegment("Texts name segment keys this original does not have.",
                                 {"keys": _listed(unknown), "count": len(unknown)})
        reports: Dict[str, str] = {}
        for key, text in texts.items():
            segment = by_key[key]
            _, report = tagmod.restore(text, segment.lookup, expected=segment.expected_numbers)
            reports[key] = "" if report.ok else report.summary()
        return record, reports

    # ----------------------------------------------------------------- export

    def create_export(self, original_id: str, request: ExportRequest) -> ExportResult:
        """Apply explicit translations to one original and store the result.

        The caller supplies final placeholder text per segment key; this
        service does not choose between suggestions and human edits. Segments
        not named keep their stored target and metadata, exactly as memoQ
        wrote them. Writing reuses ``MqxliffDocument.apply_translation`` (the
        app's export path); tag damage is reported per segment as in the app.
        The stored original is never modified: it is parsed into a fresh tree.
        """
        record, data = self.original_content(original_id)
        try:
            document = mqxliff.MqxliffDocument.from_bytes(data, record.filename)
            segments = document.parse_segments(request.seg_filter, request.tag_source)
        except mqxliff.MqxliffError:
            raise Inconsistent("The stored original can no longer be parsed.") from None

        by_key = {s.key: s for s in segments}
        wanted = request.translations
        unknown = [k for k in wanted if k not in by_key]
        if unknown:
            raise UnknownSegment("Translations name segment keys this original does not have.",
                                 {"keys": _listed(unknown), "count": len(unknown)})
        skipped = {k: by_key[k].skip_reason for k in wanted if by_key[k].skip_reason}
        if skipped:
            listed = _listed(skipped)
            raise NonTranslatableSegment(
                "Translations target segments the filter excludes from writing.",
                {"keys": listed, "reasons": {k: skipped[k] for k in listed},
                 "count": len(skipped)})
        empty = [k for k, text in wanted.items() if not text.strip()]
        if empty:
            raise EmptyTranslation("Empty translations are not written; omit the segment instead.",
                                   {"keys": _listed(empty), "count": len(empty)})

        warnings: List[Dict[str, object]] = []
        try:
            for segment in segments:  # document order, independent of request order
                if segment.key not in wanted:
                    continue
                report = document.apply_translation(
                    segment, wanted[segment.key], mark_confirmed=request.mark_confirmed)
                if not report.ok:
                    warnings.append({"key": segment.key, "missing": list(report.missing),
                                     "unknown": list(report.unknown),
                                     "duplicated": list(report.duplicated)})
            output = document.to_bytes()
        except mqxliff.MqxliffError:
            raise ExportFailed("The export could not be generated.") from None

        # Validate before publishing: the result must parse and keep its source.
        try:
            check = mqxliff.MqxliffDocument.from_bytes(output, "verify")
            check.parse_segments()
            fingerprint = check.source_fingerprint()
        except mqxliff.MqxliffError:
            raise ExportValidationFailed("The generated export could not be re-parsed.") from None
        if fingerprint != record.document_id:
            raise ExportValidationFailed("The generated export changed the source document.")

        export_id = hashlib.sha256(output).hexdigest()
        self._store(export_id, output, EXPORTS)
        candidate = ExportRecord(
            export_id=export_id, original_id=record.original_id,
            filename=sanitize_filename(mqxliff.export_filename(record.filename)),
            size_bytes=len(output), segments_written=len(wanted),
            tag_source=request.tag_source, mark_confirmed=request.mark_confirmed,
            warnings=tuple(warnings),
        )
        try:
            stored, created = self.repository.add_export(candidate)
        except RepositoryError as exc:
            # Keep the file: an overlapping identical export may own it.
            log.warning("export failed at database insert; file %s kept for reuse",
                        export_id[:12])
            raise Unavailable(str(exc)) from None
        log.info("export %s: %s from original %s (%d segments, %d tag warnings)",
                 "stored" if created else "repeated", export_id[:12], original_id[:12],
                 len(wanted), len(warnings))
        return ExportResult(stored, created)

    def get_export(self, export_id: str) -> ExportRecord:
        require_id(export_id, "export_id")
        record = self._repo(lambda: self.repository.get_export(export_id))
        if record is None:
            raise NotFound("Unknown export_id.")
        return record

    def export_content(self, export_id: str) -> Tuple[ExportRecord, bytes]:
        record = self.get_export(export_id)
        try:
            data = self.storage.read(export_id, EXPORTS)
        except StorageError as exc:
            raise Unavailable(str(exc)) from None
        if data is None or hashlib.sha256(data).hexdigest() != export_id:
            log.error("stored file missing or altered for export %s", export_id[:12])
            raise Inconsistent("The stored export file is missing or damaged.")
        return record, data

    # -------------------------------------------------------------- readiness

    def readiness(self) -> Dict[str, object]:
        database = self.repository.status(self.schema_version)
        storage = "ok" if self.storage.check_writable() else "unavailable"
        ready = database == DB_STATUS_OK and storage == "ok"
        return {"status": "ready" if ready else "not_ready",
                "checks": {"database": database, "storage": storage}}
