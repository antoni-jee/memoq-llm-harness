"""HTTP API for document-service (Starlette).

Endpoints
---------
``GET  /healthz``                         liveness; no dependency checks
``GET  /readyz``                          readiness; database + storage checks
``POST /documents``                       upload (multipart field ``file``)
``GET  /documents/{document_id}``         logical document and all its originals
``GET  /originals/{original_id}``         metadata of one exact upload
``GET  /originals/{original_id}/content`` the stored bytes, unchanged
``GET  /originals/{original_id}/segments`` parsed segments of that original
``POST /originals/{original_id}/exports``  export with explicit translations (JSON)
``GET  /exports/{export_id}``              metadata of one export
``GET  /exports/{export_id}/content``      the exported bytes

Errors are JSON ``{"error": <code>, "message": <safe text>}`` plus, where
useful, ``"details"`` holding identifiers only (segment keys, field names).
"""

from __future__ import annotations

import json
import logging
from typing import Dict, Optional

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from memoq_harness import mqxliff, tags as tagmod

from . import SERVICE_NAME
from .service import (
    FILTER_FLAGS, BadRequest, DocumentService, ServiceError, TooLarge, export_to_dict,
    original_to_dict, parse_export_request,
)

log = logging.getLogger(SERVICE_NAME)

# Multipart framing around the file itself; the file is still capped exactly.
MULTIPART_OVERHEAD = 64 * 1024
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _error(status: int, code: str, message: str,
           details: Optional[Dict[str, object]] = None) -> JSONResponse:
    body: Dict[str, object] = {"error": code, "message": message}
    if details:
        body["details"] = details
    return JSONResponse(body, status_code=status)


async def _read_capped(request: Request, limit: int) -> bytes:
    """Read the request body, refusing anything larger than ``limit`` bytes."""
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
    return b"".join(chunks)


def _flag(params, name: str, default: bool) -> bool:
    raw = params.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise BadRequest("%s must be true or false." % name)


def create_app(service: DocumentService) -> Starlette:
    max_body = service.max_upload_bytes + MULTIPART_OVERHEAD

    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok", "service": SERVICE_NAME})

    async def readyz(request: Request) -> Response:
        body = await run_in_threadpool(service.readiness)
        return JSONResponse(body, status_code=200 if body["status"] == "ready" else 503)

    async def upload(request: Request) -> Response:
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            return _error(415, "unsupported_media_type",
                          "Send the file as multipart/form-data in the field 'file'.")
        length = request.headers.get("content-length")
        if length is None:
            return _error(411, "length_required", "A Content-Length header is required.")
        try:
            if int(length) > max_body:
                raise TooLarge("The file exceeds the %d-byte upload limit." % service.max_upload_bytes)
        except ValueError:
            raise BadRequest("Invalid Content-Length header.") from None

        try:
            form = await request.form(max_files=1, max_fields=5)
        except (HTTPException, MultiPartException):
            return _error(400, "bad_request", "The multipart body could not be read.")
        try:
            item = form.get("file")
            if not isinstance(item, UploadFile):
                raise BadRequest("The multipart field 'file' is missing.")
            raw = await item.read(service.max_upload_bytes + 1)
            filename = item.filename
        finally:
            await form.close()

        result = await run_in_threadpool(service.upload, raw, filename)
        body = original_to_dict(result.original)
        body["created"] = result.created
        return JSONResponse(body, status_code=201 if result.created else 200)

    async def get_document(request: Request) -> Response:
        record = await run_in_threadpool(service.get_document, request.path_params["document_id"])
        return JSONResponse({
            "document_id": record.document_id,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "originals": [original_to_dict(o) for o in record.originals],
        })

    async def get_original(request: Request) -> Response:
        record = await run_in_threadpool(service.get_original, request.path_params["original_id"])
        return JSONResponse(original_to_dict(record))

    def attachment(data: bytes, filename: str) -> Response:
        safe_name = filename.replace('"', "'").encode("ascii", "replace").decode("ascii")
        return Response(data, media_type="application/xml", headers={
            "Content-Disposition": 'attachment; filename="%s"' % safe_name,
        })

    async def get_content(request: Request) -> Response:
        record, data = await run_in_threadpool(
            service.original_content, request.path_params["original_id"])
        return attachment(data, record.filename)

    async def create_export(request: Request) -> Response:
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith("application/json"):
            return _error(415, "unsupported_media_type", "Send the export request as JSON.")
        raw = await _read_capped(request, service.max_upload_bytes)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise BadRequest("The request body is not valid UTF-8 JSON.") from None
        export_request = parse_export_request(body)
        result = await run_in_threadpool(
            service.create_export, request.path_params["original_id"], export_request)
        payload = export_to_dict(result.export)
        payload["created"] = result.created
        return JSONResponse(payload, status_code=201 if result.created else 200)

    async def get_export(request: Request) -> Response:
        record = await run_in_threadpool(service.get_export, request.path_params["export_id"])
        return JSONResponse(export_to_dict(record))

    async def get_export_content(request: Request) -> Response:
        record, data = await run_in_threadpool(
            service.export_content, request.path_params["export_id"])
        return attachment(data, record.filename)

    async def get_segments(request: Request) -> Response:
        params = request.query_params
        seg_filter = mqxliff.SegmentFilter(**{
            name: _flag(params, name, getattr(mqxliff.SegmentFilter, name))
            for name in FILTER_FLAGS})
        tag_source = params.get("tag_source", tagmod.TAG_SOURCE_TARGET)
        record, segments = await run_in_threadpool(
            service.segments, request.path_params["original_id"], seg_filter, tag_source)
        return JSONResponse({
            "original_id": record.original_id,
            "document_id": record.document_id,
            "tag_source": tag_source,
            "filter": {name: getattr(seg_filter, name) for name in FILTER_FLAGS},
            "segments": segments,
        })

    async def service_error(request: Request, exc: ServiceError) -> Response:
        if exc.status >= 500:
            log.warning("%s %s -> %d %s", request.method, request.url.path, exc.status, exc.code)
        return _error(exc.status, exc.code, exc.message, exc.details)

    async def http_error(request: Request, exc: HTTPException) -> Response:
        return _error(exc.status_code, "http_%d" % exc.status_code, str(exc.detail))

    async def unexpected_error(request: Request, exc: Exception) -> Response:
        # Type only: messages from parsers or drivers can quote data.
        log.error("%s %s -> 500 unexpected %s", request.method, request.url.path,
                  type(exc).__name__)
        return _error(500, "internal_error", "Unexpected server error.")

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/readyz", readyz, methods=["GET"]),
        Route("/documents", upload, methods=["POST"]),
        Route("/documents/{document_id}", get_document, methods=["GET"]),
        Route("/originals/{original_id}", get_original, methods=["GET"]),
        Route("/originals/{original_id}/content", get_content, methods=["GET"]),
        Route("/originals/{original_id}/segments", get_segments, methods=["GET"]),
        Route("/originals/{original_id}/exports", create_export, methods=["POST"]),
        Route("/exports/{export_id}", get_export, methods=["GET"]),
        Route("/exports/{export_id}/content", get_export_content, methods=["GET"]),
    ]
    handlers: Dict[object, object] = {
        ServiceError: service_error,
        HTTPException: http_error,
        Exception: unexpected_error,
    }
    return Starlette(routes=routes, exception_handlers=handlers)
