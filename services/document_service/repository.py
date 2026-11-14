"""PostgreSQL persistence for document-service (driver: pg8000, pure Python).

A short-lived connection is opened per operation, with the configured socket
timeout. That keeps the service correct across database restarts without a
pool, which is adequate for this application's request rate.

:class:`RepositoryError` messages are safe to return to clients: they never
contain the database URL, credentials, SQL or server error text.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterator, Optional, Tuple

import pg8000.native

from services.common import db as common_db
# Shared with workspace-service; re-exported so existing imports keep working.
from services.common.db import RepositoryError, quote_schema  # noqa: F401

from .config import DatabaseSettings

DB_STATUS_OK = "ok"
DB_STATUS_UNAVAILABLE = "unavailable"
DB_STATUS_NOT_MIGRATED = "not_migrated"


@dataclass(frozen=True)
class OriginalRecord:
    original_id: str
    document_id: str
    filename: str
    size_bytes: int
    segment_count: int
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class ExportRecord:
    export_id: str
    original_id: str
    filename: str
    size_bytes: int
    segments_written: int
    tag_source: str
    mark_confirmed: bool
    warnings: Tuple[Dict[str, object], ...] = ()
    document_id: str = ""
    """Filled from the original on reads; not stored in the exports table."""
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class DocumentRecord:
    document_id: str
    created_at: Optional[datetime]
    originals: Tuple[OriginalRecord, ...]


def open_connection(settings: DatabaseSettings, application_name: str = "document-service"):
    """Open a pg8000 native connection. Failures raise a sanitised RepositoryError."""
    return common_db.open_connection(settings, application_name)


_ORIGINAL_COLUMNS = "original_id, document_id, filename, size_bytes, segment_count, created_at"


def _original(row) -> OriginalRecord:
    return OriginalRecord(
        original_id=row[0], document_id=row[1], filename=row[2],
        size_bytes=int(row[3]), segment_count=int(row[4]), created_at=row[5],
    )


_EXPORT_COLUMNS = ("e.export_id, e.original_id, e.filename, e.size_bytes, e.segments_written, "
                   "e.tag_source, e.mark_confirmed, e.warnings, o.document_id, e.created_at")


def _export(row) -> ExportRecord:
    warnings = row[7]
    if isinstance(warnings, (str, bytes)):
        warnings = json.loads(warnings)
    return ExportRecord(
        export_id=row[0], original_id=row[1], filename=row[2], size_bytes=int(row[3]),
        segments_written=int(row[4]), tag_source=row[5], mark_confirmed=bool(row[6]),
        warnings=tuple(warnings or ()), document_id=row[8], created_at=row[9],
    )


class PostgresDocumentRepository:
    """Documents and originals in one schema owned by document-service."""

    def __init__(self, settings: DatabaseSettings, schema: str):
        self.settings = settings
        self.schema = schema
        self._q = quote_schema(schema)

    @contextmanager
    def _session(self) -> Iterator[object]:
        conn = open_connection(self.settings)
        try:
            yield conn
        except pg8000.native.Error:
            raise RepositoryError("Database operation failed.") from None
        except OSError:
            raise RepositoryError("Database connection was lost.") from None
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - closing a broken socket must not mask the error
                pass

    # ------------------------------------------------------------- readiness

    def status(self, expected_version: str) -> str:
        """``ok``, ``unavailable`` or ``not_migrated`` - for the readiness probe."""
        try:
            with self._session() as conn:
                conn.run("SELECT 1")
                exists = conn.run(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = :s AND table_name = 'schema_migrations'",
                    s=self.schema,
                )
                if not exists:
                    return DB_STATUS_NOT_MIGRATED
                rows = conn.run("SELECT max(version) FROM %s.schema_migrations" % self._q)
                current = rows[0][0] if rows else None
                return DB_STATUS_OK if current == expected_version else DB_STATUS_NOT_MIGRATED
        except RepositoryError:
            return DB_STATUS_UNAVAILABLE

    # ---------------------------------------------------------------- writes

    def add_original(self, record: OriginalRecord) -> Tuple[OriginalRecord, bool]:
        """Insert the document (if new) and the original (if new), atomically.

        Returns the stored record and whether this call created it. A repeated
        identical upload returns the first record unchanged.
        """
        with self._session() as conn:
            conn.run("START TRANSACTION")
            try:
                conn.run(
                    "INSERT INTO %s.documents (document_id) VALUES (:d) "
                    "ON CONFLICT (document_id) DO NOTHING" % self._q,
                    d=record.document_id,
                )
                inserted = conn.run(
                    "INSERT INTO %s.originals (original_id, document_id, filename, "
                    "size_bytes, segment_count) VALUES (:o, :d, :f, :n, :c) "
                    "ON CONFLICT (original_id) DO NOTHING RETURNING %s"
                    % (self._q, _ORIGINAL_COLUMNS),
                    o=record.original_id, d=record.document_id, f=record.filename,
                    n=record.size_bytes, c=record.segment_count,
                )
                if not inserted:
                    inserted = conn.run(
                        "SELECT %s FROM %s.originals WHERE original_id = :o"
                        % (_ORIGINAL_COLUMNS, self._q),
                        o=record.original_id,
                    )
                    created = False
                else:
                    created = True
                conn.run("COMMIT")
            except BaseException:
                try:
                    conn.run("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
                raise
        return _original(inserted[0]), created

    def add_export(self, record: ExportRecord) -> Tuple[ExportRecord, bool]:
        """Insert an export (if new). Identical bytes return the first record."""
        with self._session() as conn:
            inserted = conn.run(
                "INSERT INTO %s.exports (export_id, original_id, filename, size_bytes, "
                "segments_written, tag_source, mark_confirmed, warnings) "
                "VALUES (:e, :o, :f, :n, :w, :t, :m, CAST(:j AS jsonb)) "
                "ON CONFLICT (export_id) DO NOTHING RETURNING export_id" % self._q,
                e=record.export_id, o=record.original_id, f=record.filename,
                n=record.size_bytes, w=record.segments_written, t=record.tag_source,
                m=record.mark_confirmed, j=json.dumps(list(record.warnings)),
            )
        stored = self.get_export(record.export_id)
        if stored is None:  # pragma: no cover - the row was just written or already present
            raise RepositoryError("Database operation failed.")
        return stored, bool(inserted)

    # ----------------------------------------------------------------- reads

    def get_export(self, export_id: str) -> Optional[ExportRecord]:
        with self._session() as conn:
            rows = conn.run(
                "SELECT %s FROM %s.exports e JOIN %s.originals o USING (original_id) "
                "WHERE e.export_id = :e" % (_EXPORT_COLUMNS, self._q, self._q),
                e=export_id,
            )
        return _export(rows[0]) if rows else None

    def get_original(self, original_id: str) -> Optional[OriginalRecord]:
        with self._session() as conn:
            rows = conn.run(
                "SELECT %s FROM %s.originals WHERE original_id = :o" % (_ORIGINAL_COLUMNS, self._q),
                o=original_id,
            )
        return _original(rows[0]) if rows else None

    def get_document(self, document_id: str) -> Optional[DocumentRecord]:
        with self._session() as conn:
            docs = conn.run(
                "SELECT document_id, created_at FROM %s.documents WHERE document_id = :d" % self._q,
                d=document_id,
            )
            if not docs:
                return None
            rows = conn.run(
                "SELECT %s FROM %s.originals WHERE document_id = :d "
                "ORDER BY created_at, original_id" % (_ORIGINAL_COLUMNS, self._q),
                d=document_id,
            )
        return DocumentRecord(
            document_id=docs[0][0], created_at=docs[0][1],
            originals=tuple(_original(r) for r in rows),
        )
