"""HTTP API for processing-service (Starlette).

Endpoints
---------
``GET  /healthz``  liveness; no dependency checks
``GET  /readyz``   readiness; the configured engine is usable (never calls a model)
``POST /process``  build the prompt and return suggestions for explicit segments
``POST /analyze``  a warning-aware critique of the operator's stored corrections
``POST /probe``    one bounded model probe for a per-request config (no suggestions)
``POST /models``   model ids the per-request config's endpoint advertises

Errors are JSON ``{"error", "message"}`` plus ``details`` with identifiers or
counts only. Nothing from a provider (error text, raw response) and no
prompt or credential is ever returned or logged.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, Optional

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import SERVICE_NAME
from .service import (BadRequest, ProcessingService, ServiceError, TooLarge,
                      parse_analyze_request, parse_config_request,
                      parse_process_request)

log = logging.getLogger(SERVICE_NAME)


class _UnsupportedMedia(Exception):
    pass


def _error(status: int, code: str, message: str,
           details: Optional[Dict[str, object]] = None) -> JSONResponse:
    body: Dict[str, object] = {"error": code, "message": message}
    if details:
        body["details"] = details
    return JSONResponse(body, status_code=status)


async def _read_json(request: Request, limit: int) -> object:
    if not request.headers.get("content-type", "").lower().startswith("application/json"):
        raise _UnsupportedMedia()
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > limit:
                raise TooLarge("The request body exceeds the %d-byte limit." % limit)
        except ValueError:
            raise BadRequest("Invalid Content-Length header.") from None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise TooLarge("The request body exceeds the %d-byte limit." % limit)
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BadRequest("The request body is not valid UTF-8 JSON.") from None


def create_app(service: ProcessingService) -> Starlette:
    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok", "service": SERVICE_NAME})

    async def readyz(request: Request) -> Response:
        body = service.readiness()
        return JSONResponse(body, status_code=200 if body["status"] == "ready" else 503)

    async def process(request: Request) -> Response:
        body = parse_process_request(await _read_json(request, service.max_body_bytes),
                                     service.max_segments)
        return JSONResponse(await run_in_threadpool(service.process, body))

    async def analyze(request: Request) -> Response:
        body = parse_analyze_request(await _read_json(request, service.max_body_bytes))
        return JSONResponse(await run_in_threadpool(service.analyze_corrections, body))

    async def probe(request: Request) -> Response:
        config = parse_config_request(await _read_json(request, service.max_body_bytes))
        return JSONResponse(await run_in_threadpool(service.probe, config))

    async def models(request: Request) -> Response:
        config = parse_config_request(await _read_json(request, service.max_body_bytes))
        return JSONResponse(await run_in_threadpool(service.list_models, config))

    async def service_error(request: Request, exc: ServiceError) -> Response:
        if exc.status >= 500:
            log.warning("%s %s -> %d %s", request.method, request.url.path, exc.status, exc.code)
        return _error(exc.status, exc.code, exc.message, exc.details)

    async def unsupported_media(request: Request, exc: _UnsupportedMedia) -> Response:
        return _error(415, "unsupported_media_type", "Send the request body as JSON.")

    async def http_error(request: Request, exc: HTTPException) -> Response:
        return _error(exc.status_code, "http_%d" % exc.status_code, str(exc.detail))

    async def unexpected_error(request: Request, exc: Exception) -> Response:
        # Type only: provider and parser messages can quote data or prompts.
        log.error("%s %s -> 500 unexpected %s", request.method, request.url.path,
                  type(exc).__name__)
        return _error(500, "internal_error", "Unexpected server error.")

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/readyz", readyz, methods=["GET"]),
        Route("/process", process, methods=["POST"]),
        Route("/analyze", analyze, methods=["POST"]),
        Route("/probe", probe, methods=["POST"]),
        Route("/models", models, methods=["POST"]),
    ]
    handlers: Dict[object, object] = {
        ServiceError: service_error,
        _UnsupportedMedia: unsupported_media,
        HTTPException: http_error,
        Exception: unexpected_error,
    }
    return Starlette(routes=routes, exception_handlers=handlers)
