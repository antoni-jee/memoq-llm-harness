"""Remote-mode workflow: Open / Reload / Save orchestration.

This module owns the orchestration a remote session performs - opening a
document through the document service, projecting the active selection to the
workspace bind contract, re-reading the saved workspace on Reload, and the
session-scoped draft store with its explicit Save/Discard, conflict resolution
and dirty Open/Reload confirmation - plus the narrow validation of the
document-service segments envelope that the reads share. It is presentation-free: it builds a :class:`RemoteWorkspaceView` and
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

    The remote path reads through the cached remote clients; edits are held as
    session drafts and written only by an explicit Save through the workspace
    service. Processing, applying and exporting are out of scope, so none of the
    local mutation/session state is created.
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
    # Unsaved edits live here, not in the view and not in any widget key (see
    # the "unsaved drafts" section). Keyed by document id, so an edit on one
    # document never applies to another; it survives reruns, batch/page
    # navigation and a failed save, and is session-only (not a browser refresh).
    ss.remote_drafts = {}
    ss.remote_widget_gen = 0
    ss.remote_row_rev = {}  # per-row widget re-key counters (see draft section)
    # A dirty Open/Reload waits here for an explicit save/discard/cancel.
    ss.remote_pending = None
    # An upload the operator cancelled is not offered again until it changes.
    ss.remote_declined_marker = None
    # The file version actually open (set only by a successful Open), kept
    # apart from whatever the uploader currently holds: {"name", "marker"}.
    ss.remote_open_file = None
    # Bumped to give the uploader a fresh key, which empties it (after Cancel).
    ss.remote_uploader_gen = 0
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
    # A successful Open starts clean: no draft from any earlier document (or an
    # earlier version of this one) survives, and every cell re-seeds.
    ss.remote_drafts = {}
    ss.remote_pending = None
    ss.remote_open_file = {"name": filename, "marker": upload_marker(filename, raw)}
    _bump_widgets()
    counts = view.counts()
    notify("success", "Opened **%s** - %d segment(s), %d batch(es) of %d."
           % (filename, counts.get("active", 0), view.batch_count, view.batch_size))


def maybe_remote_open(raw: bytes, filename: str) -> bool:
    """Open only for a file version not yet opened.

    Streamlit re-exposes the same upload on every rerun; the upload marker
    keeps that from rebinding the document, so a rerender does not re-upload,
    re-bind or clear the view. Returns True when an open happened.

    Opening a new file while the live document has unsaved drafts does not
    open: it records a pending Open that the grid asks about (save and open,
    discard and open, or cancel - see ``resolve_pending``). A cancelled upload
    is not offered again until the uploader changes.

    Reselecting the file version that is already open (same name *and* bytes,
    i.e. the same upload marker) is a no-op: no upload, no bind, no prompt.
    """
    ss = st.session_state
    marker = upload_marker(filename, raw)
    if not upload_needs_load(marker, ss.get("remote_open_marker")):
        return False
    open_file = ss.get("remote_open_file")
    if ss.get("remote_view") is not None and open_file             and open_file.get("marker") == marker:
        ss.remote_open_marker = marker
        return False
    if marker == ss.get("remote_declined_marker"):
        return False
    view = ss.get("remote_view")
    if view is not None and _has_unsaved_drafts(view.document_id):
        pending = ss.get("remote_pending")
        if not (pending and pending.get("marker") == marker):
            ss.remote_pending = {"action": "open", "marker": marker,
                                 "filename": filename, "raw": raw}
        return False
    ss.remote_open_marker = marker
    remote_open(raw, filename)
    return True


def remote_reload() -> None:
    """Re-read the saved workspace and refresh the view, checking identity.

    A read only: no upload, no bind. If the reloaded workspace no longer names
    the same original or the same tag source, the view we have shown cannot be
    trusted and is cleared so the operator must open again explicitly, rather
    than a silent merge of two different documents (with unsaved drafts the
    view and drafts are kept instead, and nothing is merged). On success any
    kept drafts are rebased on the new read (see ``_rebase_drafts``); an edit
    whose saved value changed elsewhere becomes an explicit conflict.
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
        if _has_unsaved_drafts(view.document_id):
            # Clearing the view would strand the drafts; keep both, merge
            # nothing, and let the operator decide (a Save would be refused at
            # the old revision anyway).
            notify("error",
                   "The saved workspace no longer matches the open document "
                   "(original or tag source changed). Nothing was merged and "
                   "your unsaved changes were kept; discard them and re-open "
                   "the document to continue.")
            return
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
    before = view.revision
    view = RemoteWorkspaceView.from_remote(doc_segments, snapshot)
    note = _adopt_view(view)  # rebases any kept drafts; marks conflicts
    message = "Reloaded saved work - revision %d." % view.revision
    if unsaved_count():
        message += " " + _state_text(view, before)
    notify("warning" if conflicted_keys(view) else "success", message + note)


# ------------------------------------------------------------- unsaved drafts
#
# The draft store is ``ss.remote_drafts``:
#
#     {document_id: {segment_key: {"text", "comment", "base_text", "base_comment"}}}
#
# ``text``/``comment`` are the operator's current cell values; ``base_*`` are the
# saved values the draft was started from (the record as last read). A field is
# *edited* when its value differs from its base, and only edited fields are ever
# sent. A field *conflicts* when it is edited and the saved record (as read now)
# holds neither its base nor the draft value - someone else saved that field
# after the draft was started. Save refuses while any conflict is unresolved, so
# a revision refresh can never turn into a silent overwrite.
#
# The store, not a widget key, is the source of truth: widget state disappears
# when a row is not rendered (another batch or page), the store does not.
#
# Widget keys are "<kind>_<gen>_<row rev>_<segment key>". Within one document a
# handler re-keys only the rows whose shown (text, comment) changes - a Use
# server, a Discard, a saved value that moved - so those cells re-seed from the
# store/record. A cell whose value stays the same (a draft kept across a 409 or
# a re-read, a Keep mine, a just-saved edit) keeps its widget: the browser does
# not remount it and never has to repopulate it, so a kept draft cannot be
# replaced on screen by the saved value and then synced back as a "clean" row.
# ``gen`` is bumped only when the document itself changes (Open).

_FIELDS = (("text", "base_text", "user_final"),
           ("comment", "base_comment", "user_comment"))


def _drafts_for(document_id: str) -> dict:
    return st.session_state.get("remote_drafts", {}).get(document_id, {})


def _has_unsaved_drafts(document_id: str) -> bool:
    """True when the document has at least one unsaved draft."""
    return bool(_drafts_for(document_id))


def unsaved_count() -> int:
    view = st.session_state.get("remote_view")
    return len(_drafts_for(view.document_id)) if view is not None else 0


def _bump_widgets() -> None:
    ss = st.session_state
    ss.remote_widget_gen = ss.get("remote_widget_gen", 0) + 1


def _widget_key(kind: str, key: str) -> str:
    ss = st.session_state
    return "remote_%s_%d_%d_%s" % (kind, ss.get("remote_widget_gen", 0),
                                   ss.get("remote_row_rev", {}).get(key, 0), key)


def remote_text_widget_key(key: str) -> str:
    return _widget_key("edit", key)


def remote_comment_widget_key(key: str) -> str:
    return _widget_key("comment", key)


def _shown(view) -> dict:
    """{segment key: (text, comment)} every cell of ``view`` shows right now."""
    drafts = _drafts_for(view.document_id)
    out = {}
    for key in view.order:
        draft = drafts.get(key)
        record = view.record(key)
        if draft:
            out[key] = (draft["text"], draft["comment"])
        elif record is not None:
            out[key] = (record.user_final, record.user_comment)
    return out


def _rekey_changed(before: dict, view) -> list:
    """Re-key the rows whose shown value differs from ``before``; return them."""
    ss = st.session_state
    revs = ss.setdefault("remote_row_rev", {})
    changed = [k for k, v in _shown(view).items() if before.get(k) != v]
    for key in changed:
        revs[key] = revs.get(key, 0) + 1
    return changed


def edited_fields(draft) -> list:
    return [f for f, base, _ in _FIELDS if draft[f] != draft[base]]


def conflict_fields(draft, record) -> list:
    """Edited fields whose saved value changed since the draft was started."""
    out = []
    for f, base, rec_attr in _FIELDS:
        server = getattr(record, rec_attr)
        if draft[f] != draft[base] and server != draft[base] and server != draft[f]:
            out.append(f)
    return out


def cell_seed(record) -> tuple:
    """(text, comment) an editing cell shows: the draft when held, else saved."""
    view = st.session_state.get("remote_view")
    draft = _drafts_for(view.document_id).get(record.key) if view else None
    if draft:
        return draft["text"], draft["comment"]
    return record.user_final, record.user_comment


def _store_cell(document_id: str, record, text: str, comment: str) -> None:
    """Record one row's cell values; keep a draft only while something is edited."""
    ss = st.session_state
    store = ss.setdefault("remote_drafts", {})
    bucket = store.setdefault(document_id, {})
    draft = bucket.get(record.key)
    if draft is None:
        draft = {"base_text": record.user_final,
                 "base_comment": record.user_comment}
    draft["text"] = text
    draft["comment"] = comment
    if edited_fields(draft):
        bucket[record.key] = draft
    else:
        bucket.pop(record.key, None)
    if not bucket:
        store.pop(document_id, None)


def sync_drafts_from_widgets() -> None:
    """Copy this run's cell values into the draft store before any handler runs.

    Streamlit applies the browser's widget values to session state before the
    script starts, so reading them here - at the top of the remote render, ahead
    of every button handler - means a Save, Discard, Reload or Open decision
    always sees the operator's latest typing, wherever the clicked button lives.
    Rows not rendered last run have no widget state and keep their stored draft.
    """
    ss = st.session_state
    view = ss.get("remote_view")
    if view is None:
        return
    for key in view.order:
        text_key = remote_text_widget_key(key)
        comment_key = remote_comment_widget_key(key)
        if text_key not in ss and comment_key not in ss:
            continue
        record = view.record(key)
        if record is None:
            continue
        seed_text, seed_comment = cell_seed(record)
        text = ss[text_key] if text_key in ss else seed_text
        comment = ss[comment_key] if comment_key in ss else seed_comment
        _store_cell(view.document_id, record,
                    text if text is not None else "",
                    comment if comment is not None else "")


def row_location(view, key):
    """(batch_index, page_index) of a segment, for "Show row" navigation."""
    if key not in view.order:
        return None
    index = view.order.index(key)
    batch = index // view.batch_size
    page_size = st.session_state.get("remote_page_size", 0)
    page = (index - batch * view.batch_size) // page_size if page_size else 0
    return batch, page


def conflicted_keys(view=None) -> list:
    view = view if view is not None else st.session_state.get("remote_view")
    if view is None:
        return []
    out = []
    for key, draft in _drafts_for(view.document_id).items():
        record = view.record(key)
        if record is not None and conflict_fields(draft, record):
            out.append(key)
    return out


def _rebase_drafts(view) -> int:
    """Re-anchor the drafts on a freshly read view, without writing anything.

    * an unedited field follows the saved value;
    * an edited field the server already holds becomes clean;
    * an edited field whose saved value is still the base stays pending;
    * an edited field whose saved value moved elsewhere keeps its base, so
      ``conflict_fields`` reports it until the operator resolves it;
    * a draft for a segment the view no longer carries is dropped (the count
      is returned so the caller can report it).
    """
    ss = st.session_state
    store = ss.setdefault("remote_drafts", {})
    bucket = store.get(view.document_id, {})
    dropped = 0
    for key in list(bucket):
        draft = bucket[key]
        record = view.record(key)
        if record is None:
            bucket.pop(key)
            dropped += 1
            continue
        for f, base, rec_attr in _FIELDS:
            server = getattr(record, rec_attr)
            if draft[f] == draft[base]:
                # Not edited: follow the saved value.
                draft[f] = server
                draft[base] = server
            elif server == draft[f]:
                # The server already holds this edit: nothing left to send.
                draft[base] = server
        if not edited_fields(draft):
            bucket.pop(key)
    if not bucket:
        store.pop(view.document_id, None)
    return dropped


def _adopt_view(view) -> str:
    """Make ``view`` the live view, rebase the drafts on it and re-seed the
    cells whose shown value changed (all cells if the document changed).

    Returns a note about dropped drafts ("" when none were dropped).
    """
    old = st.session_state.get("remote_view")
    same_document = old is not None and old.document_id == view.document_id
    before = _shown(old) if same_document else None
    st.session_state.remote_view = view
    dropped = _rebase_drafts(view)
    if same_document:
        _rekey_changed(before, view)
    else:
        _bump_widgets()
    if dropped:
        return (" %d unsaved change(s) belonged to segments no longer in the "
                "workspace and were dropped." % dropped)
    return ""


def _read_view(document_id: str, original_id: str, tag_source: str):
    """Read the workspace and the document segments; build a view.

    Returns ``None`` when the workspace no longer names the same original or
    tag source (it must not be merged). Raises RemoteError on any failure.
    """
    clients = get_remote_clients()
    snapshot = clients.workspace.get_workspace(document_id)
    probe = RemoteWorkspaceView.from_remote([], snapshot)
    if probe.original_id != original_id or probe.tag_source != tag_source:
        return None
    doc_segments = _document_segments(
        clients.document.segments(original_id, tag_source=tag_source))
    return RemoteWorkspaceView.from_remote(doc_segments, snapshot)


def _intended_updates(drafts_by_key, view) -> dict:
    """The per-segment update map: only the edited fields of each draft.

    An explicit empty-string edit is sent as ``""`` (never replaced by the
    original target or the model's suggestion). A draft for a segment the view
    does not carry is skipped, so no update names an unknown key.
    """
    updates = {}
    for key, draft in drafts_by_key.items():
        if view.record(key) is None:
            continue
        entry = {f: draft[f] for f in edited_fields(draft)}
        if entry:
            updates[key] = entry
    return updates


def _definitely_not_applied(exc: RemoteError) -> bool:
    """A 3xx/4xx is a rejection before anything was written (the service
    validates and compares the revision first). A transport failure, a 5xx or a
    malformed 2xx leaves the outcome unknown."""
    return exc.status is not None and 300 <= exc.status < 500


def _state_text(view, before_revision: int) -> str:
    conflicts = len(conflicted_keys(view))
    text = "Re-read revision %d" % view.revision
    if view.revision != before_revision:
        text += " (was %d)" % before_revision
    text += ": %d unsaved change(s) kept" % unsaved_count()
    if conflicts:
        text += (", %d in conflict - choose Keep mine or Use server on each "
                 "marked row, then Save" % conflicts)
    return text + "."


def save_remote_drafts() -> bool:
    """Send the live document's edited fields with ``expected_revision``.

    Returns True only when no unsaved change remains and every change sent is
    confirmed on the server (a 2xx followed by a re-read, or a re-read after an
    uncertain failure) - the one condition under which a pending Open/Reload
    may continue.

    * no edited field: no request at all;
    * an unresolved conflict: refused, nothing sent;
    * 2xx: the workspace is re-read and the drafts rebased, so they become clean
      only against confirmed server state;
    * 409 stale_revision: nothing was written. The workspace is re-read (a read
      only) and the drafts rebased, so concurrent changes to an edited field
      surface as per-row conflicts that block Save; nothing is replayed;
    * other 3xx/4xx: nothing was written; drafts are kept;
    * transport failure / 5xx / malformed 2xx: the outcome is unknown, so the
      workspace is re-read to reconcile; changes the server now holds become
      clean, the rest are kept. If that read fails too, nothing is marked saved.
    """
    ss = st.session_state
    view = ss.get("remote_view")
    if view is None:
        return True
    conflicts = conflicted_keys(view)
    if conflicts:
        notify("error",
               "Not saved: %d segment(s) were changed elsewhere since you edited "
               "them. Choose Keep mine or Use server on each marked row first."
               % len(conflicts))
        return False
    updates = _intended_updates(_drafts_for(view.document_id), view)
    if not updates:
        notify("info", "Nothing to save - no unsaved changes.")
        return True
    before = view.revision
    clients = get_remote_clients()
    try:
        clients.workspace.save_segments(view.document_id, view.revision, updates)
    except RemoteError as exc:
        stale = exc.is_conflict and exc.code == "stale_revision"
        if _definitely_not_applied(exc) and not stale:
            notify("error", "Not saved: %s Your changes were kept."
                   % _remote_error_text(exc))
            return False
        if stale:
            lead = ("Not saved: the workspace was changed elsewhere since it was "
                    "read. Nothing was written; your changes were kept.")
        else:
            lead = ("The save could not be confirmed (%s) - it may or may not "
                    "have been stored. Your changes were kept." % exc.message)
        try:
            fresh = _read_view(view.document_id, view.original_id, view.tag_source)
        except RemoteError as read_exc:
            notify("error", lead + " The workspace could not be re-read (%s); "
                   "use Reload when the connection is back. If the first save "
                   "was stored, a repeated Save is refused by the server, so "
                   "nothing is overwritten." % read_exc.message)
            return False
        if fresh is None:
            notify("error", lead + " The workspace is now bound to a different "
                   "original; re-open the document.")
            return False
        note = _adopt_view(fresh)
        if unsaved_count() == 0:
            # Verified by the read: the server holds every change (for a stale
            # revision, typically an earlier save whose response was lost).
            lead = ("The save response was lost, but" if not stale else
                    "Nothing was written now, but")
            notify("success", "%s the re-read workspace (revision %d) holds "
                   "all your changes.%s" % (lead, fresh.revision, note))
            return True
        notify("error", lead + " " + _state_text(fresh, before) + note)
        return False

    # Confirmed by a 2xx. Re-read so the view and drafts reflect stored state.
    try:
        fresh = _read_view(view.document_id, view.original_id, view.tag_source)
    except RemoteError as exc:
        notify("warning",
               "Saved %d segment(s), but the refresh failed (%s). The changes "
               "stay marked unsaved until Reload confirms them; a repeated Save "
               "is refused by the server, so nothing is overwritten."
               % (len(updates), exc.message))
        return False
    if fresh is None:
        notify("warning", "Saved, but the workspace is now bound to a different "
               "original. Your changes were kept; re-open the document.")
        return False
    note = _adopt_view(fresh)
    if unsaved_count():
        notify("warning", "Saved %d segment(s). " % len(updates)
               + _state_text(fresh, before) + note)
        return False
    notify("success", "Saved %d segment(s) - revision %d.%s"
           % (len(updates), fresh.revision, note))
    return True


def discard_remote_drafts() -> None:
    """Drop the live document's drafts; the cells show the saved text again."""
    ss = st.session_state
    view = ss.get("remote_view")
    if view is None:
        return
    before = _shown(view)
    dropped = ss.get("remote_drafts", {}).pop(view.document_id, None)
    _rekey_changed(before, view)
    if dropped:
        notify("success", "Discarded %d unsaved change(s); the saved text is "
               "shown again." % len(dropped))
    else:
        notify("info", "No unsaved changes to discard.")


def resolve_conflict(key: str, keep_mine: bool) -> None:
    """Resolve one row's conflict explicitly.

    Keep mine re-anchors the conflicting fields on the current saved value, so
    the next Save deliberately replaces it. Use server takes the saved value for
    the conflicting fields (other edited fields of the row stay pending).
    """
    ss = st.session_state
    view = ss.get("remote_view")
    if view is None:
        return
    bucket = ss.get("remote_drafts", {}).get(view.document_id, {})
    draft, record = bucket.get(key), view.record(key)
    if draft is None or record is None:
        return
    before = _shown(view)
    attrs = {f: (base, rec_attr) for f, base, rec_attr in _FIELDS}
    for f in conflict_fields(draft, record):
        base, rec_attr = attrs[f]
        draft[base] = getattr(record, rec_attr)
        if not keep_mine:
            draft[f] = draft[base]
    if not edited_fields(draft):
        bucket.pop(key)
        if not bucket:
            ss.remote_drafts.pop(view.document_id, None)
    _rekey_changed(before, view)


# --------------------------------------------- dirty Open/Reload confirmation


def request_reload() -> None:
    """Reload now when clean; with unsaved drafts, ask first."""
    ss = st.session_state
    view = ss.get("remote_view")
    if view is not None and _has_unsaved_drafts(view.document_id):
        ss.remote_pending = {"action": "reload"}
        return
    remote_reload()


def resolve_pending(choice: str) -> None:
    """Act on the operator's answer to a pending dirty Open/Reload.

    ``choice`` is one of ``save`` (save first; continue only if every change is
    confirmed saved), ``discard`` (drop the drafts and continue), ``keep``
    (Reload only: re-read and rebase the drafts, marking conflicts) or
    ``cancel`` (nothing happens; a cancelled upload is not offered again until
    the uploader changes).
    """
    ss = st.session_state
    pending = ss.get("remote_pending")
    if not pending:
        return
    action = pending["action"]
    if choice == "keep" and action != "reload":
        return  # keeping drafts across an Open would apply them to another document
    if choice == "cancel":
        ss.remote_pending = None
        if action == "open":
            ss.remote_declined_marker = pending["marker"]
            # Empty the uploader (a fresh widget key is Streamlit's supported
            # reset) so the cancelled file no longer looks selected or open.
            ss.remote_uploader_gen = ss.get("remote_uploader_gen", 0) + 1
            open_file = ss.get("remote_open_file") or {}
            notify("info", "Cancelled: %s was not opened. %s is still open with "
                   "your unsaved changes." % (pending.get("filename", "the file"),
                                              open_file.get("name", "The document")))
        else:
            notify("info", "Cancelled. Your unsaved changes are still here.")
        return
    saved_notice = None
    if choice == "save":
        if not save_remote_drafts():
            return  # drafts kept, the question stays open, nothing destructive
        saved_notice = ss.get("notice")
    ss.remote_pending = None
    try:
        if action == "open":
            ss.remote_open_marker = pending["marker"]
            # Open first: a failed Open keeps the live view and its drafts.
            remote_open(pending["raw"], pending["filename"])
        else:
            if choice == "discard":
                view = ss.get("remote_view")
                if view is not None:
                    before = _shown(view)
                    ss.get("remote_drafts", {}).pop(view.document_id, None)
                    _rekey_changed(before, view)
            remote_reload()
    except RemoteError as exc:
        notify("error", _remote_error_text(exc))
        return
    if saved_notice and saved_notice[0] == "success":
        kind, message = ss.get("notice") or ("success", "")
        notify(kind, saved_notice[1] + " " + message)
