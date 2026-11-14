"""PostgreSQL persistence for workspace-service (driver: pg8000).

Every change goes through :meth:`PostgresWorkspaceRepository.transact`: one
transaction that locks the workspace row (``SELECT ... FOR UPDATE``), lets the
caller decide on the new row from the *current* stored state, and writes it
with ``revision`` incremented. Two writers therefore serialise on the row and
never overwrite each other's changes blindly.

:class:`RepositoryError` messages are safe to show: no URL, credentials, SQL
or server error text.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, Iterator, Optional, Tuple

import pg8000.native

from services.common import db as common_db
from services.common.db import DatabaseSettings, RepositoryError, quote_schema

DB_STATUS_OK = "ok"
DB_STATUS_UNAVAILABLE = "unavailable"
DB_STATUS_NOT_MIGRATED = "not_migrated"

_COLUMNS = ("document_id, snapshot, state_version, revision, tag_source, bound_original_id, "
            "created_at, updated_at")


@dataclass(frozen=True)
class WorkspaceRow:
    document_id: str
    snapshot: Dict[str, object]
    state_version: int
    revision: int
    tag_source: str
    bound_original_id: Optional[str]
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


Decision = Tuple[Optional[WorkspaceRow], object]
"""``(row to write or None for "no change", payload returned to the caller)``."""


def _row(values) -> WorkspaceRow:
    snapshot = values[1]
    if isinstance(snapshot, (str, bytes)):
        snapshot = json.loads(snapshot)
    return WorkspaceRow(
        document_id=values[0], snapshot=snapshot, state_version=int(values[2]),
        revision=int(values[3]), tag_source=values[4], bound_original_id=values[5],
        created_at=values[6], updated_at=values[7],
    )


class PostgresWorkspaceRepository:
    def __init__(self, settings: DatabaseSettings, schema: str):
        self.settings = settings
        self.schema = schema
        self._q = quote_schema(schema)

    @contextmanager
    def _session(self) -> Iterator[object]:
        conn = common_db.open_connection(self.settings, "workspace-service")
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

    # ----------------------------------------------------------------- reads

    def get(self, document_id: str) -> Optional[WorkspaceRow]:
        with self._session() as conn:
            rows = conn.run("SELECT %s FROM %s.workspaces WHERE document_id = :d"
                            % (_COLUMNS, self._q), d=document_id)
        return _row(rows[0]) if rows else None

    # ---------------------------------------------------------------- writes

    def transact(self, document_id: str,
                 decide: Callable[[Optional[WorkspaceRow]], Decision]
                 ) -> Tuple[Optional[WorkspaceRow], object, bool]:
        """Lock, decide, write - atomically.

        ``decide`` receives the current row (or None) and must be free of
        side effects: it can run twice if a concurrent request creates the
        same workspace first. Exceptions it raises roll the transaction back
        and propagate unchanged. Returns ``(stored row, payload, wrote)``.
        """
        for _attempt in range(2):
            with self._session() as conn:
                conn.run("START TRANSACTION")
                try:
                    rows = conn.run("SELECT %s FROM %s.workspaces WHERE document_id = :d "
                                    "FOR UPDATE" % (_COLUMNS, self._q), d=document_id)
                    current = _row(rows[0]) if rows else None
                    new, payload = decide(current)
                    if new is None:
                        conn.run("ROLLBACK")
                        return current, payload, False
                    params = dict(d=document_id, s=json.dumps(new.snapshot, ensure_ascii=False),
                                  v=new.state_version, r=new.revision, t=new.tag_source,
                                  o=new.bound_original_id)
                    if current is None:
                        written = conn.run(
                            "INSERT INTO %s.workspaces (document_id, snapshot, state_version, "
                            "revision, tag_source, bound_original_id) VALUES "
                            "(:d, CAST(:s AS jsonb), :v, :r, :t, :o) "
                            "ON CONFLICT (document_id) DO NOTHING RETURNING %s"
                            % (self._q, _COLUMNS), **params)
                        if not written:
                            conn.run("ROLLBACK")
                            continue  # created concurrently: decide again under the lock
                    else:
                        written = conn.run(
                            "UPDATE %s.workspaces SET snapshot = CAST(:s AS jsonb), "
                            "state_version = :v, revision = :r, tag_source = :t, "
                            "bound_original_id = :o, updated_at = now() "
                            "WHERE document_id = :d AND revision = :old RETURNING %s"
                            % (self._q, _COLUMNS), old=current.revision, **params)
                        if not written:  # pragma: no cover - impossible while the row is locked
                            raise RepositoryError("Database operation failed.")
                    conn.run("COMMIT")
                    return _row(written[0]), payload, True
                except BaseException:
                    try:
                        conn.run("ROLLBACK")
                    except Exception:  # noqa: BLE001
                        pass
                    raise
        raise RepositoryError("Database operation failed.")
