"""Remote-mode workflow: Open / Reload orchestration for the read-only path.

This module owns the orchestration a remote session performs - opening a
document through the document service, projecting the active selection to the
workspace bind contract, and re-reading the saved workspace on Reload - plus the
narrow validation of the document-service segments envelope that both calls
share. It is presentation-free: it builds a :class:`RemoteWorkspaceView` and
queues a notice, but renders nothing (see ``remote_ui``).

It depends on ``harness_remote`` (transport), ``memoq_harness`` (the tag
source) and ``ui_common`` (the shared UI helpers), but never on ``app`` - the
extracted remote modules must not reach back into the front end. The
``harness_remote.build_clients`` lookup is resolved at call time inside
``get_remote_clients`` so a test can substitute fake clients at that seam,
exactly as it did when this code lived in ``app.py``.
"""

from __future__ import annotations

import streamlit as st

from memoq_harness import mqxliff
from harness_remote import RemoteError, RemoteWorkspaceView
from ui_common import notify, upload_marker, upload_needs_load


def _bootstrap_remote() -> None:
    """Session state for remote mode: no ProjectState, no local document.

    The remote path reads through the cached remote clients and renders a
    read-only view. Editing, saving, processing and exporting are out of scope
    for this slice, so none of the local mutation/session state is created.
    """
    ss = st.session_state
    ss.project = None
    ss.document = None
    ss.remote_view = None
    ss.remote_open_marker = None
    ss.remote_batch = 0
    ss.remote_page = 0
    ss.remote_page_size = 0
    ss.remote_tag_source = mqxliff.tagmod.TAG_SOURCE_TARGET
    ss._remote_clients = None
    ss._ready = True


def get_remote_clients():
    """Lazily build and cache the remote clients for this session.

    Cached under an explicit owner key so a rerun reuses the same session
    objects instead of building a second set (and abandoning the first). The
    construction import is resolved at call time so a test can substitute fake
    clients at the ``harness_remote.build_clients`` seam.
    """
    ss = st.session_state
    cached = ss.get("_remote_clients")
    if cached is not None:
        return cached
    from harness_remote import build_clients as _build_clients
    ss._remote_clients = _build_clients(ss.remote_config)
    return ss._remote_clients


def _remote_error_text(exc: RemoteError) -> str:
    """Operator-safe text for a remote failure: the client's own message, plus
    a revision hint for a 409 conflict. No raw body or URL ever reaches this."""
    text = exc.message
    if exc.is_conflict and exc.current_revision is not None:
        text += " (current revision %d)" % exc.current_revision
    return text


# The fields the bind contract and the active-selection decision read. The other
# serialized fields are presentation-only; a response that omits these as
# strings, or that carries an item this workflow cannot read, is malformed.
_BIND_REQUIRED_FIELDS = ("key", "unit_id", "source_text", "target_text")


def _document_segments(response):
    """Unwrap and validate the document-service ``/segments`` 2xx body.

    That endpoint returns ``{"original_id", "document_id", "tag_source",
    "filter", "segments": [...]}``. A 2xx that is not that object, that omits a
    ``segments`` list, or that carries a segment this workflow cannot read - an
    item that is not an object, that omits one of the required fields as a
    string, or that has a non-boolean ``translatable`` - is a malformed success:
    reject it with an explicit :class:`RemoteError` before the caller builds the
    bind payload or the view, rather than let a caller iterate the envelope's
    string keys (a bare ``AttributeError``), read a missing list as an empty
    document, or index a field that is absent on one item.
    """
    if not isinstance(response, dict):
        raise RemoteError(
            service="document", status=None, code="bad_response",
            message="The segments response was not the expected object.")
    segments = response.get("segments")
    if not isinstance(segments, list):
        raise RemoteError(
            service="document", status=None, code="bad_response",
            message="The segments response did not carry a segments list.")
    for index, item in enumerate(segments):
        if not isinstance(item, dict):
            raise RemoteError(
                service="document", status=None, code="bad_response",
                message="Segment %d of the segments response is not an object."
                        % index)
        for field in _BIND_REQUIRED_FIELDS:
            if not isinstance(item.get(field), str):
                raise RemoteError(
                    service="document", status=None, code="bad_response",
                    message="Segment %d is missing its %r field."
                            % (index, field))
        if not isinstance(item.get("translatable"), bool):
            raise RemoteError(
                service="document", status=None, code="bad_response",
                message="Segment %d has an invalid 'translatable' field."
                        % index)
    return segments


def _bind_payload(doc_segments):
    """Split a validated segment list into the bind's two parts.

    ``active`` is the translatable selection, each item projected to exactly the
    four fields the workspace parser accepts (the full 16-field serialized
    segment is not a valid bind item - its extra fields are rejected).
    ``present_keys`` is every document key in order, so a row the filter
    currently hides is not discarded from the saved workspace.
    """
    active = [{field: segment[field] for field in _BIND_REQUIRED_FIELDS}
              for segment in doc_segments if segment["translatable"]]
    present_keys = [segment["key"] for segment in doc_segments]
    return active, present_keys


def remote_open(raw: bytes, filename: str) -> None:
    """Upload a document, read its segments and bind the saved workspace.

    Builds a fresh read-only view and assigns it only on full success, so a
    failed Open never leaves a half-merged view behind. No ProjectState is
    touched. Any failure raises :class:`RemoteError`; the caller keeps the
    live view and shows the safe message.
    """
    ss = st.session_state
    clients = get_remote_clients()
    tag_source = ss.get("remote_tag_source", "target")

    upload_result = clients.document.upload(raw, filename=filename)
    document_id = str(upload_result.get("document_id") or "")
    original_id = str(upload_result.get("original_id") or "")
    if not document_id or not original_id:
        raise RemoteError(
            service="document", status=None, code="bad_response",
            message="The upload did not return a document and original id.",
        )

    doc_segments = _document_segments(
        clients.document.segments(original_id, tag_source=tag_source))
    active_segments, present_keys = _bind_payload(doc_segments)
    # bind carries only the active (translatable) selection, projected to the four
    # fields the workspace accepts; present_keys keeps every document key so the
    # workspace prunes nothing it cannot see (a row the filter hides is not
    # discarded).
    clients.workspace.bind(
        document_id, original_id, source_file=filename, tag_source=tag_source,
        segments=active_segments, present_keys=present_keys,
    )
    snapshot = clients.workspace.get_workspace(document_id)
    # The full canonical list drives the read-only view; the projected active
    # selection above is a separate view of the same document.
    view = RemoteWorkspaceView.from_remote(doc_segments, snapshot)

    ss.remote_view = view
    ss.remote_batch = 0
    ss.remote_page = 0
    counts = view.counts()
    notify("success", "Opened **%s** - %d segment(s), %d batch(es) of %d."
           % (filename, counts.get("active", 0), view.batch_count, view.batch_size))


def maybe_remote_open(raw: bytes, filename: str) -> bool:
    """Open only for a file version not yet opened.

    Streamlit re-exposes the same upload on every rerun; the upload marker
    keeps that from rebinding the document, so a rerender does not re-upload,
    re-bind or clear the view. Returns True when an open happened.
    """
    ss = st.session_state
    marker = upload_marker(filename, raw)
    if not upload_needs_load(marker, ss.get("remote_open_marker")):
        return False
    ss.remote_open_marker = marker
    remote_open(raw, filename)
    return True


def remote_reload() -> None:
    """Re-read the saved workspace and refresh the view, checking identity.

    A read only: no upload, no bind. If the reloaded workspace no longer names
    the same original or the same tag source, the view we have shown cannot be
    trusted and is cleared so the operator must open again explicitly, rather
    than a silent merge of two different documents.
    """
    ss = st.session_state
    view = ss.get("remote_view")
    if view is None:
        return
    clients = get_remote_clients()
    snapshot = clients.workspace.get_workspace(view.document_id)
    reloaded = RemoteWorkspaceView.from_remote([], snapshot)
    if (reloaded.original_id != view.original_id
            or reloaded.tag_source != view.tag_source):
        ss.remote_view = None
        ss.remote_open_marker = None
        notify("error",
               "The saved workspace no longer matches the open document "
               "(original or tag source changed). Re-open it to continue; no "
               "changes were merged.")
        return
    # Refresh the document segments so source text and the original target are
    # current, then rebuild the view over the refreshed read.
    doc_segments = _document_segments(
        clients.document.segments(view.original_id, tag_source=view.tag_source))
    view = RemoteWorkspaceView.from_remote(doc_segments, snapshot)
    ss.remote_view = view
    notify("success", "Reloaded saved work - revision %d." % view.revision)
