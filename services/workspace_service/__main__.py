"""Start workspace-service: ``python -m services.workspace_service``.

All configuration comes from environment variables (see README.md in this
directory). Run the migrations first: ``python -m services.workspace_service.migrate``.
"""

from __future__ import annotations

import logging
import sys

from . import SERVICE_NAME
from .config import ConfigError, Settings, load_settings
from .migrate import latest_version
from .repository import PostgresWorkspaceRepository
from .service import WorkspaceService


def build_service(settings: Settings) -> WorkspaceService:
    return WorkspaceService(
        repository=PostgresWorkspaceRepository(settings.database, settings.db_schema),
        schema_version=latest_version(),
        max_body_bytes=settings.max_body_bytes,
    )


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print("%s: configuration error: %s" % (SERVICE_NAME, exc), file=sys.stderr)
        return 2

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    import uvicorn

    from .app import create_app

    log = logging.getLogger(SERVICE_NAME)
    log.info("starting on %s:%d; database %s schema %s", settings.host, settings.port,
             settings.database.redacted(), settings.db_schema)
    uvicorn.run(
        create_app(build_service(settings)),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
