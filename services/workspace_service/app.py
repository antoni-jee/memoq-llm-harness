"""HTTP API for workspace-service (Starlette).

Endpoints
---------
``GET   /healthz``                          liveness; no dependency checks
``GET   /readyz``                           readiness; database reachable and migrated
``POST  /workspaces/{document_id}/bind``    open/create and merge segment records
``GET   /workspaces/{document_id}``         the saved workspace
``PATCH /workspaces/{document_id}/segments`` save manual text and/or comments
``POST  /workspaces/{document_id}/suggestions`` record suggestions (prefills unprotected rows)

Errors are JSON ``{"error": <code>, "message": <safe text>}`` plus, where
useful, ``"details"`` with identifiers only.
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
from .service import (
    BadRequest, ServiceError, TooLarge, WorkspaceService, parse_bind_request,
    parse_patch_request, parse_suggestions_request,
)

log = logging.getLogger(SERVICE_NAME)


def _error(status: int, code: str, message: str,
           details: Optional[Dict[str, object]] = None) -> JSONResponse:
    body: Dict[str, object] = {"error": code, "message": message}
    if details:
        body["details"] = details
    return JSONResponse(body, status_code=status)


async def _read_json(request: Request, limit: int) -> object:
    """Read a JSON body of at most ``limit`` bytes."""
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


class _UnsupportedMedia(Exception):
    pass


def create_app(service: WorkspaceService) -> Starlette:
    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok", "service": SERVICE_NAME})

    async def readyz(request: Request) -> Response:
        body = await run_in_threadpool(service.readiness)
        return JSONResponse(body, status_code=200 if body["status"] == "ready" else 503)

    async def bind(request: Request) -> Response:
        body = parse_bind_request(await _read_json(request, service.max_body_bytes))
        result = await run_in_threadpool(
            service.bind, request.path_params["document_id"], body)
        return JSONResponse(result, status_code=201 if result["created"] else 200)

    async def get_workspace(request: Request) -> Response:
        result = await run_in_threadpool(service.get, request.path_params["document_id"])
        return JSONResponse(result)

    async def save_segments(request: Request) -> Response:
        body = parse_patch_request(await _read_json(request, service.max_body_bytes))
        result = await run_in_threadpool(
            service.save_segments, request.path_params["document_id"], body)
        return JSONResponse(result)

    async def record_suggestions(request: Request) -> Response:
        body = parse_suggestions_request(await _read_json(request, service.max_body_bytes))
        result = await run_in_threadpool(
            service.record_suggestions, request.path_params["document_id"], body)
        return JSONResponse(result)

    async def service_error(request: Request, exc: ServiceError) -> Response:
        if exc.status >= 500:
            log.warning("%s %s -> %d %s", request.method, request.url.path, exc.status, exc.code)
        return _error(exc.status, exc.code, exc.message, exc.details)

    async def unsupported_media(request: Request, exc: _UnsupportedMedia) -> Response:
        return _error(415, "unsupported_media_type", "Send the request body as JSON.")

    async def http_error(request: Request, exc: HTTPException) -> Response:
        return _error(exc.status_code, "http_%d" % exc.status_code, str(exc.detail))

    async def unexpected_error(request: Request, exc: Exception) -> Response:
        # Type only: messages from drivers or state code can quote data.
        log.error("%s %s -> 500 unexpected %s", request.method, request.url.path,
                  type(exc).__name__)
        return _error(500, "internal_error", "Unexpected server error.")

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/readyz", readyz, methods=["GET"]),
        Route("/workspaces/{document_id}/bind", bind, methods=["POST"]),
        Route("/workspaces/{document_id}", get_workspace, methods=["GET"]),
        Route("/workspaces/{document_id}/segments", save_segments, methods=["PATCH"]),
        Route("/workspaces/{document_id}/suggestions", record_suggestions, methods=["POST"]),
    ]
    handlers: Dict[object, object] = {
        ServiceError: service_error,
        _UnsupportedMedia: unsupported_media,
        HTTPException: http_error,
        Exception: unexpected_error,
    }
    return Starlette(routes=routes, exception_handlers=handlers)
