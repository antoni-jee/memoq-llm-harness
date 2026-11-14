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
    DB_STATUS_OK, DocumentRecord, OriginalRecord, RepositoryError,
)
from .storage import FileStorage, StorageError

log = logging.getLogger(SERVICE_NAME)

_ID_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_FILENAME_CHARS = 200
TAG_SOURCES = (tagmod.TAG_SOURCE_TARGET, tagmod.TAG_SOURCE_SOURCE)


# --------------------------------------------------------------------- errors


class ServiceError(Exception):
    """Base class: ``status`` and ``code`` map onto the HTTP response."""

    status = 500
    code = "internal_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


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


# ------------------------------------------------------------------ contracts


class DocumentRepository(Protocol):
    def status(self, expected_version: str) -> str: ...
    def add_original(self, record: OriginalRecord) -> Tuple[OriginalRecord, bool]: ...
    def get_original(self, original_id: str) -> Optional[OriginalRecord]: ...
    def get_document(self, document_id: str) -> Optional[DocumentRecord]: ...


@dataclass(frozen=True)
class UploadResult:
    original: OriginalRecord
    created: bool


# -------------------------------------------------------------------- helpers


def sanitize_filename(name: Optional[str]) -> str:
    """Display name only - never used in a storage path."""
    base = os.path.basename((name or "").replace("\\", "/"))
    cleaned = "".join(c for c in unicodedata.normalize("NFC", base)
                      if unicodedata.category(c)[0] != "C").strip()
    return cleaned[:MAX_FILENAME_CHARS] or "document.mqxliff"


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

    def _store(self, original_id: str, raw: bytes) -> bool:
        try:
            return self.storage.write(original_id, raw)
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

    # -------------------------------------------------------------- readiness

    def readiness(self) -> Dict[str, object]:
        database = self.repository.status(self.schema_version)
        storage = "ok" if self.storage.check_writable() else "unavailable"
        ready = database == DB_STATUS_OK and storage == "ok"
        return {"status": "ready" if ready else "not_ready",
                "checks": {"database": database, "storage": storage}}
