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
existing schema it owns. The runner itself lives in
:mod:`services.common.migrations` and is shared with workspace-service.
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional, Sequence, Tuple

from services.common import migrations as runner
# Re-exported unchanged for existing callers and tests.
from services.common.migrations import applied_versions, render, split_statements  # noqa: F401

from .config import load_database_settings, load_schema

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
LOCK_NAMESPACE = "document"


def load_migrations(directory: str = MIGRATIONS_DIR) -> List[Tuple[str, str]]:
    """``(version, sql)`` pairs, in version order."""
    return runner.load_migrations(directory)


def latest_version(directory: str = MIGRATIONS_DIR) -> str:
    return runner.latest_version(directory)


def _lock_key(schema: str) -> int:
    return runner.lock_key(LOCK_NAMESPACE, schema)


def apply_migrations(conn, schema: str, directory: str = MIGRATIONS_DIR) -> List[str]:
    """Apply pending migrations on an open connection. Returns versions applied."""
    return runner.apply_migrations(conn, schema, directory, LOCK_NAMESPACE)


def main(argv: Optional[Sequence[str]] = None) -> int:
    return runner.run_cli(
        argv, service="document-service", module="services.document_service.migrate",
        directory=MIGRATIONS_DIR, namespace=LOCK_NAMESPACE,
        load_database_settings=load_database_settings, load_schema=load_schema)


if __name__ == "__main__":
    sys.exit(main())
