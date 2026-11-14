"""SQL migrations for workspace-service.

Run explicitly before starting the service (and after every upgrade)::

    python -m services.workspace_service.migrate            # apply pending
    python -m services.workspace_service.migrate --status   # show, change nothing

Creates the configured schema (``WORKSPACE_DB_SCHEMA``, default
``workspace``) if needed, applies pending ``migrations/NNNN_*.sql`` files in
their own transactions under an advisory lock, records them in
``<schema>.schema_migrations`` and never drops anything. The runner is shared
with document-service (:mod:`services.common.migrations`).
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional, Sequence, Tuple

from services.common import migrations as runner

from .config import load_database_settings, load_schema

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
LOCK_NAMESPACE = "workspace"


def load_migrations(directory: str = MIGRATIONS_DIR) -> List[Tuple[str, str]]:
    return runner.load_migrations(directory)


def latest_version(directory: str = MIGRATIONS_DIR) -> str:
    return runner.latest_version(directory)


def apply_migrations(conn, schema: str, directory: str = MIGRATIONS_DIR) -> List[str]:
    return runner.apply_migrations(conn, schema, directory, LOCK_NAMESPACE)


def main(argv: Optional[Sequence[str]] = None) -> int:
    return runner.run_cli(
        argv, service="workspace-service", module="services.workspace_service.migrate",
        directory=MIGRATIONS_DIR, namespace=LOCK_NAMESPACE,
        load_database_settings=load_database_settings, load_schema=load_schema)


if __name__ == "__main__":
    sys.exit(main())
