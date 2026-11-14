"""Start processing-service: ``python -m services.processing_service``.

All configuration comes from environment variables (see README.md in this
directory). With no engine configured it runs the offline ``stub`` engine.
"""

from __future__ import annotations

import logging
import sys

from . import SERVICE_NAME
from .config import ConfigError, load_settings
from .service import ProcessingService


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

    from memoq_harness import llm

    from .app import create_app

    log = logging.getLogger(SERVICE_NAME)
    engine = settings.engine
    if engine.kind == "stub":
        log.info("starting on %s:%d; engine stub (offline)", settings.host, settings.port)
    else:
        log.info("starting on %s:%d; engine %s, model %s, endpoint %s, key %s",
                 settings.host, settings.port, engine.kind, engine.model,
                 llm.sanitize_url(engine.base_url) or "SDK default",
                 "set" if engine.api_key else "not set")
    uvicorn.run(
        create_app(ProcessingService(settings)),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
