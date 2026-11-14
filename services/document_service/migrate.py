"""SQL migrations for document-service.

Run explicitly before starting the service (and after every upgrade)::

    python -m services.document_service.migrate            # apply pending
    python -m services.document_service.migrate --status   # show, change nothing

Behaviour:

* creates the configured schema (``DOCUMENT_DB_SCHEMA``, default ``document``)
  if it does not exist, plus ``<schema>.schema_migrations``;
* applies each pending ``migrations/NNNN_*.sql`` file in its own transaction
  and records its version;
* holds a PostgreSQL advisory lock for the schema while running, so two
  concurrent runs cannot apply the same migration twice;
* never drops anything.

The database role needs CREATE on the database (to create the schema) or an
existing schema it owns.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import zlib
from typing import List, Optional, Sequence, Tuple

from .config import ConfigError, load_database_settings, load_schema
from .repository import RepositoryError, open_connection, quote_schema

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
_FILE_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def load_migrations(directory: str = MIGRATIONS_DIR) -> List[Tuple[str, str]]:
    """``(version, sql)`` pairs, in version order."""
    found = []
    for name in sorted(os.listdir(directory)):
        match = _FILE_RE.match(name)
        if match:
            with open(os.path.join(directory, name), "r", encoding="utf-8") as handle:
                found.append((match.group(1), handle.read()))
    versions = [v for v, _ in found]
    if len(versions) != len(set(versions)):
        raise RuntimeError("Duplicate migration version.")
    return found


def latest_version(directory: str = MIGRATIONS_DIR) -> str:
    migrations = load_migrations(directory)
    return migrations[-1][0] if migrations else ""


def render(sql: str, schema: str) -> str:
    return sql.replace("{schema}", quote_schema(schema))


def split_statements(sql: str) -> List[str]:
    """Split on ``;`` at end of line, dropping ``--`` comment lines.

    Deliberately simple: migration files must not contain functions, DO
    blocks or dollar-quoted strings.
    """
    statements, current = [], []
    for line in sql.splitlines():
        if line.strip().startswith("--"):
            continue
        current.append(line)
        if line.rstrip().endswith(";"):
            statement = "\n".join(current).strip().rstrip(";").strip()
            if statement:
                statements.append(statement)
            current = []
    tail = "\n".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def _lock_key(schema: str) -> int:
    return zlib.crc32(("memoq-harness:document:" + schema).encode("utf-8"))


def applied_versions(conn, schema: str) -> List[str]:
    exists = conn.run(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = :s AND table_name = 'schema_migrations'", s=schema)
    if not exists:
        return []
    return [r[0] for r in conn.run(
        "SELECT version FROM %s.schema_migrations ORDER BY version" % quote_schema(schema))]


def apply_migrations(conn, schema: str, directory: str = MIGRATIONS_DIR) -> List[str]:
    """Apply pending migrations on an open connection. Returns versions applied."""
    q = quote_schema(schema)
    key = _lock_key(schema)
    conn.run("SELECT pg_advisory_lock(:k)", k=key)
    try:
        conn.run("CREATE SCHEMA IF NOT EXISTS %s" % q)
        conn.run(
            "CREATE TABLE IF NOT EXISTS %s.schema_migrations ("
            "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())" % q)
        done = set(applied_versions(conn, schema))
        applied = []
        for version, sql in load_migrations(directory):
            if version in done:
                continue
            conn.run("START TRANSACTION")
            try:
                for statement in split_statements(render(sql, schema)):
                    conn.run(statement)
                conn.run("INSERT INTO %s.schema_migrations (version) VALUES (:v)" % q, v=version)
                conn.run("COMMIT")
            except BaseException:
                try:
                    conn.run("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
                raise
            applied.append(version)
        return applied
    finally:
        try:
            conn.run("SELECT pg_advisory_unlock(:k)", k=key)
        except Exception:  # noqa: BLE001 - the lock is released with the session anyway
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m services.document_service.migrate",
                                     description="Apply document-service SQL migrations.")
    parser.add_argument("--status", action="store_true",
                        help="report applied and pending versions without changing anything")
    args = parser.parse_args(argv)

    try:
        settings = load_database_settings()
        schema = load_schema()
    except ConfigError as exc:
        print("document-service migrate: configuration error: %s" % exc, file=sys.stderr)
        return 2

    known = [v for v, _ in load_migrations()]
    try:
        conn = open_connection(settings, application_name="document-service-migrate")
    except RepositoryError as exc:
        print("document-service migrate: %s (%s)" % (exc, settings.redacted()), file=sys.stderr)
        return 1
    try:
        if args.status:
            done = applied_versions(conn, schema)
            pending = [v for v in known if v not in done]
            print("schema %s: applied %s; pending %s" % (
                schema, ", ".join(done) or "none", ", ".join(pending) or "none"))
            return 0
        applied = apply_migrations(conn, schema)
    except Exception as exc:  # noqa: BLE001 - report the type only; server text may echo data
        print("document-service migrate: failed (%s)." % type(exc).__name__, file=sys.stderr)
        return 1
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    if applied:
        print("schema %s: applied %s" % (schema, ", ".join(applied)))
    else:
        print("schema %s: up to date (%s)" % (schema, known[-1] if known else "no migrations"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
