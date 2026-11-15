"""HTTP clients for the three backend services (remote mode).

Each client is a thin wrapper over one ``requests.Session`` for one service.
A small shared helper (:func:`_perform`, :class:`BaseClient`) maps transport and
HTTP failures onto :class:`RemoteError`, preserving the server's own sanitized
error shape (``error`` / ``message`` / ``details``) and, for 409 conflicts,
``current_revision`` so a caller can re-read and retry a mutation.

Safety rules, honoured throughout:

* No raw response body, request payload, credential or raw requests exception
  text ever reaches a :class:`RemoteError` message or representation.
* Redirects are never followed (``allow_redirects=False`` on every request); a
  3xx is a failure whose ``Location`` target is never read or surfaced.
* Mutations are not retried automatically.

The clients know nothing of ``memoq_harness``; they only move bytes and JSON
between the app and the services.
"""

from __future__ import annotations

from typing import Dict, Mapping, Optional, Tuple

import requests

from .config import ConfigError

# Generic, credential-free messages for HTTP failures that did not carry a
# usable JSON error body.
_DEFAULT_MESSAGES = {
    400: "The request was rejected as malformed.",
    401: "Authentication failed.",
    403: "The request was forbidden.",
    404: "The resource was not found.",
    405: "The method is not allowed for this resource.",
    409: "The request conflicts with the current state.",
    411: "A Content-Length is required.",
    413: "The request is too large.",
    415: "The request media type is not supported.",
    422: "The request was rejected as invalid.",
    500: "An unexpected error occurred on the service.",
    502: "The upstream request failed.",
    503: "The service is not ready or is unavailable.",
}

# Server error codes the client knows how to render. Each maps to a fixed,
# client-owned message: a listed code is accepted, but the server's own
# ``message`` is never copied, so a hostile server cannot put arbitrary text
# (a URL, a secret, HTML) into an error a caller surfaces.
_ALLOWED_ERROR_MESSAGES = {
    "stale_revision": "The document changed since it was read; reload and retry.",
    "unknown_segment": "The request named a segment key this original does not have.",
    "unavailable": "The service is not ready or is unavailable.",
    "engine_unavailable": "The translation engine is not available.",
}


class RemoteError(Exception):
    """A transport or HTTP failure in a safe, structured form.

    Carries the service name, the HTTP status (``None`` for a transport-level
    failure), a machine ``code``, a safe ``message`` and optional ``details``
    (identifiers only, as the services guarantee). ``current_revision`` is
    lifted from a 409's ``details`` so a mutation can be re-read and retried.
    Never carries a raw body, the request payload, credentials or exception
    text.
    """

    def __init__(self, *, service: str, status: Optional[int], code: str,
                 message: str, details: Optional[Mapping[str, object]] = None,
                 current_revision: Optional[int] = None):
        label = status if status is not None else "-"
        super().__init__("[%s] %s %s" % (service, label, code))
        self.service = service
        self.status = status
        self.code = code
        self.message = message
        self.details = dict(details) if details else {}
        self.current_revision = current_revision

    @property
    def is_conflict(self) -> bool:
        return self.status == 409

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return ("RemoteError(service=%r, status=%r, code=%r, current_revision=%r)"
                % (self.service, self.status, self.code, self.current_revision))


# --------------------------------------------------------------- shared helper


def _safe_json(response):
    """Parse a response body as JSON without ever exposing it; ``None`` if not JSON."""
    try:
        return response.json()
    except Exception:
        # A non-JSON body (an HTML proxy page, plain text, truncated data) is
        # not surfaced: the caller gets a generic message instead.
        return None


def _default_message(status: int) -> str:
    return _DEFAULT_MESSAGES.get(status, "The request failed with HTTP %d." % status)


def _error_from_response(service: str, response) -> RemoteError:
    """Map a non-2xx response onto RemoteError without trusting the body.

    The body is parsed only to read an allowlisted ``error`` code and, for a
    409 conflict, two revision fields. The server's ``message`` is never used
    (a known code selects a client-owned message; everything else falls back to
    ``http_<status>`` and the generic status message), and details are retained
    only for a 409 ``stale_revision`` conflict — just the two revision fields a
    caller needs to retry, each a nonnegative int that is not a bool — so nested
    data and arbitrary identifiers from the server are always dropped.
    """
    status = response.status_code
    code = "http_%d" % status
    message = _default_message(status)
    details: Dict[str, object] = {}
    current_revision: Optional[int] = None

    parsed = _safe_json(response)
    if isinstance(parsed, dict):
        c = parsed.get("error")
        if isinstance(c, str) and c in _ALLOWED_ERROR_MESSAGES:
            code = c
            message = _ALLOWED_ERROR_MESSAGES[c]

        if status == 409 and code == "stale_revision":
            d = parsed.get("details")
            if isinstance(d, dict):
                for name in ("expected_revision", "current_revision"):
                    v = d.get(name)
                    if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                        details[name] = v
            current_revision = details.get("current_revision")

    return RemoteError(service=service, status=status, code=code,
                       message=message, details=details or None,
                       current_revision=current_revision)


def _error_from_redirect(service: str, response) -> RemoteError:
    """Map a 3xx response onto a safe RemoteError without exposing its target.

    The client never follows redirects, so a 3xx is a failure. The ``Location``
    header (the redirect target) is never read or surfaced, so a hostile server
    cannot point an error message at an internal or private address, and the
    body is never parsed.
    """
    status = response.status_code
    return RemoteError(
        service=service, status=status, code="redirect_not_followed",
        message="The %s service redirected the request, which the client does "
                "not follow." % service,
    )


def _perform(session, service, method, url, *, timeout, expect_binary,
             json=None, files=None, params=None, headers=None):
    """One HTTP call with no retry: transport errors and HTTP failures become
    RemoteError; only a 2xx returns JSON (or raw bytes when ``expect_binary``).
    Redirects are never followed (``allow_redirects=False``), so a 3xx comes
    back raw and is mapped onto a safe RemoteError rather than dereferenced."""
    try:
        response = session.request(method, url, timeout=timeout, json=json,
                                   files=files, params=params, headers=headers,
                                   allow_redirects=False)
    except requests.exceptions.Timeout:
        # ConnectTimeout/ReadTimeout are subtypes, so this also catches them.
        raise RemoteError(service=service, status=None, code="timeout",
                          message="The request to %s timed out." % service) from None
    except requests.exceptions.ConnectionError:
        raise RemoteError(service=service, status=None, code="connection_error",
                          message="Could not connect to %s." % service) from None
    except requests.exceptions.RequestException:
        # No raw exception text: a message may quote a URL or a provider string.
        raise RemoteError(service=service, status=None, code="http_error",
                          message="The request to %s failed." % service) from None

    # Only a 2xx is a success; everything else is a safe RemoteError. A 3xx is
    # handled first so its Location target is never read; any other status
    # (1xx, 4xx, 5xx) is a failure mapped onto a safe RemoteError.
    if 300 <= response.status_code < 400:
        raise _error_from_redirect(service, response)
    if not (200 <= response.status_code < 300):
        raise _error_from_response(service, response)
    if expect_binary:
        return response.content
    try:
        return response.json()
    except Exception:
        raise RemoteError(service=service, status=response.status_code,
                          code="bad_response",
                          message="The %s service returned a non-JSON response."
                                  % service) from None


class BaseClient:
    """Shared request plumbing for one service; subclasses add endpoint methods."""

    service = "service"

    def __init__(self, base_url: str, *, http_timeout: Tuple[float, float],
                 process_timeout: Optional[Tuple[float, float]] = None,
                 session=None):
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.http_timeout = tuple(http_timeout)
        self.process_timeout = tuple(process_timeout) if process_timeout else self.http_timeout
        self._session = session if session is not None else requests.Session()
        self._owns_session = session is None

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _url(self, path: str) -> str:
        return "%s/%s" % (self.base_url, path.lstrip("/"))

    def _request(self, method, path, *, json=None, files=None, params=None,
                 binary=False, timeout=None, headers=None):
        effective = timeout if timeout is not None else self.http_timeout
        return _perform(self._session, self.service, method, self._url(path),
                        timeout=effective, expect_binary=binary, json=json,
                        files=files, params=params, headers=headers)

    def get(self, path, *, params=None, binary=False):
        return self._request("GET", path, params=params, binary=binary)

    def post_json(self, path, body, *, timeout=None):
        return self._request("POST", path, json=body, timeout=timeout)

    def patch_json(self, path, body, *, timeout=None):
        return self._request("PATCH", path, json=body, timeout=timeout)


class DocumentClient(BaseClient):
    """document-service: upload, segments, tag reports, export and downloads."""

    service = "document"

    def health(self):
        return self.get("/healthz")

    def ready(self):
        return self.get("/readyz")

    def upload(self, raw: bytes, filename: str = "document.mqxliff",
               content_type: str = "application/xml"):
        files = {"file": (filename, raw, content_type)}
        return self._request("POST", "/documents", files=files)

    def document(self, document_id: str):
        return self.get("/documents/%s" % document_id)

    def original(self, original_id: str):
        return self.get("/originals/%s" % original_id)

    def original_content(self, original_id: str) -> bytes:
        return self.get("/originals/%s/content" % original_id, binary=True)

    def segments(self, original_id: str, *, tag_source: str = "target",
                 filters: Optional[Mapping[str, bool]] = None):
        params: Dict[str, object] = {"tag_source": tag_source}
        if filters:
            params.update({k: v for k, v in filters.items()})
        return self.get("/originals/%s/segments" % original_id, params=params)

    def export(self, original_id: str, translations: Mapping[str, str], *,
               tag_source: str = "target", filters: Optional[Mapping[str, bool]] = None,
               mark_confirmed: bool = False):
        body: Dict[str, object] = {
            "translations": dict(translations),
            "tag_source": tag_source,
            "mark_confirmed": mark_confirmed,
        }
        if filters:
            body["filter"] = dict(filters)
        return self.post_json("/originals/%s/exports" % original_id, body)

    def tag_reports(self, original_id: str, texts: Mapping[str, str], *,
                    tag_source: str = "target"):
        body = {"tag_source": tag_source, "texts": dict(texts)}
        return self.post_json("/originals/%s/tag-reports" % original_id, body)

    def export_metadata(self, export_id: str):
        return self.get("/exports/%s" % export_id)

    def export_content(self, export_id: str) -> bytes:
        return self.get("/exports/%s/content" % export_id, binary=True)


class WorkspaceClient(BaseClient):
    """workspace-service: bind, read, save edits/comments, record suggestions."""

    service = "workspace"

    def health(self):
        return self.get("/healthz")

    def ready(self):
        return self.get("/readyz")

    def bind(self, document_id: str, original_id: str, *,
             source_file: str = "", tag_source: str = "target",
             segments=None, present_keys=None):
        body = {
            "original_id": original_id,
            "source_file": source_file,
            "tag_source": tag_source,
            "segments": list(segments or []),
            "present_keys": list(present_keys or []),
        }
        return self.post_json("/workspaces/%s/bind" % document_id, body)

    def get_workspace(self, document_id: str):
        return self.get("/workspaces/%s" % document_id)

    def save_segments(self, document_id: str, expected_revision: int,
                      updates: Mapping[str, Mapping[str, str]]):
        body = {"expected_revision": expected_revision, "segments": dict(updates)}
        return self.patch_json("/workspaces/%s/segments" % document_id, body)

    def record_suggestions(self, document_id: str, expected_revision: int,
                           suggestions: Mapping[str, Mapping[str, str]]):
        body = {"expected_revision": expected_revision, "suggestions": dict(suggestions)}
        return self.post_json("/workspaces/%s/suggestions" % document_id, body)


class ProcessingClient(BaseClient):
    """processing-service: one stateless batch or rerun, plus readiness."""

    service = "processing"

    def health(self):
        return self.get("/healthz")

    def ready(self):
        return self.get("/readyz")

    def process(self, *, segments, operation: str = "batch", engine: Optional[str] = None,
                tag_source: str = "target", batch=None, context=None):
        body: Dict[str, object] = {
            "operation": operation, "tag_source": tag_source, "segments": list(segments),
        }
        if engine is not None:
            body["engine"] = engine
        if batch is not None:
            body["batch"] = batch
        if context is not None:
            body["context"] = context
        # Processing is slow: a long read, but the connect phase stays short.
        return self.post_json("/process", body, timeout=self.process_timeout)


class RemoteClients:
    """The three clients built from one config; share one session when created together."""

    def __init__(self, document=None, workspace=None, processing=None,
                 _session=None, _own_session=False):
        self.document = document
        self.workspace = workspace
        self.processing = processing
        self._session = _session
        self._own_session = _own_session

    def close(self) -> None:
        for client in (self.document, self.workspace, self.processing):
            if client is not None:
                client.close()
        if self._own_session and self._session is not None:
            self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def build_clients(config, *, session=None) -> RemoteClients:
    """Construct the three clients from a remote :class:`RemoteConfig`.

    Raises :class:`ConfigError` in local mode, where the remote clients are not
    used. This is wiring only: it makes no request and sequences nothing.
    """
    if not config.is_remote:
        raise ConfigError("build_clients requires HARNESS_BACKEND=remote")
    own = session is None
    if session is None:
        session = requests.Session()
    common = dict(http_timeout=config.http_timeout,
                  process_timeout=config.process_timeout, session=session)
    document = DocumentClient(config.document_url, **common)
    workspace = WorkspaceClient(config.workspace_url, **common)
    processing = ProcessingClient(config.processing_url, **common)
    return RemoteClients(document=document, workspace=workspace, processing=processing,
                         _session=session, _own_session=own)
