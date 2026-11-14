"""SQL migration runner shared by the backend services.

Moved unchanged from document-service's ``migrate.py``; the only new
parameters are each service's migrations directory, advisory-lock namespace
and CLI names, so every service keeps its own schema, versions and lock.

Behaviour:

* creates the schema if it does not exist, plus ``<schema>.schema_migrations``;
* applies each pending ``NNNN_*.sql`` file in its own transaction and records
  its version;
* holds a PostgreSQL advisory lock (``memoq-harness:<namespace>:<schema>``)
  while running, so two concurrent runs cannot apply a migration twice;
* never drops anything.

Migration files must stay plain: the splitter cuts on ``;`` at the end of a
line, so no functions, DO blocks or dollar quoting.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import zlib
from typing import Callable, List, Optional, Sequence, Tuple

from .db import ConfigError, DatabaseSettings, RepositoryError, open_connection, quote_schema

_FILE_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def load_migrations(directory: str) -> List[Tuple[str, str]]:
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


def latest_version(directory: str) -> str:
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


def lock_key(namespace: str, schema: str) -> int:
    return zlib.crc32(("memoq-harness:%s:%s" % (namespace, schema)).encode("utf-8"))


def applied_versions(conn, schema: str) -> List[str]:
    exists = conn.run(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = :s AND table_name = 'schema_migrations'", s=schema)
    if not exists:
        return []
    return [r[0] for r in conn.run(
        "SELECT version FROM %s.schema_migrations ORDER BY version" % quote_schema(schema))]


def apply_migrations(conn, schema: str, directory: str, namespace: str) -> List[str]:
    """Apply pending migrations on an open connection. Returns versions applied."""
    q = quote_schema(schema)
    key = lock_key(namespace, schema)
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


def run_cli(argv: Optional[Sequence[str]], *, service: str, module: str, directory: str,
            namespace: str, load_database_settings: Callable[[], DatabaseSettings],
            load_schema: Callable[[], str]) -> int:
    """The ``python -m <module>`` entry point. Exit: 0 ok, 1 database, 2 config."""
    parser = argparse.ArgumentParser(prog="python -m %s" % module,
                                     description="Apply %s SQL migrations." % service)
    parser.add_argument("--status", action="store_true",
                        help="report applied and pending versions without changing anything")
    args = parser.parse_args(argv)

    try:
        settings = load_database_settings()
        schema = load_schema()
    except ConfigError as exc:
        print("%s migrate: configuration error: %s" % (service, exc), file=sys.stderr)
        return 2

    known = [v for v, _ in load_migrations(directory)]
    try:
        conn = open_connection(settings, application_name="%s-migrate" % service)
    except RepositoryError as exc:
        print("%s migrate: %s (%s)" % (service, exc, settings.redacted()), file=sys.stderr)
        return 1
    try:
        if args.status:
            done = applied_versions(conn, schema)
            pending = [v for v in known if v not in done]
            print("schema %s: applied %s; pending %s" % (
                schema, ", ".join(done) or "none", ", ".join(pending) or "none"))
            return 0
        applied = apply_migrations(conn, schema, directory, namespace)
    except Exception as exc:  # noqa: BLE001 - report the type only; server text may echo data
        print("%s migrate: failed (%s)." % (service, type(exc).__name__), file=sys.stderr)
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
