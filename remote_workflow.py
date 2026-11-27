"""Remote-mode workflow: Open / Reload / Save orchestration.

This module owns the orchestration a remote session performs - opening a
document through the document service, projecting the active selection to the
workspace bind contract, re-reading the saved workspace on Reload, the
session-scoped draft store with its explicit Save/Discard, conflict resolution
and dirty Open/Reload confirmation, and exporting the saved human text to a
valid MQXLIFF through the document service - plus the narrow validation of the
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

import os
import streamlit as st

from memoq_harness import defaults, llm, mqxliff, state
from harness_remote import RemoteError, RemoteWorkspaceView
from ui_common import notify, upload_marker, upload_needs_load

# Where exported MQXLIFFs land, mirroring app.py's local export dir. A test can
# redirect it by patching this module global.
EXPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports")

# The one explicit "use the offline stub" option, shown alongside the real
# presets. It maps to the processing service's "stub" engine, which returns
# the stored machine translation instead of calling a model.
STUB_PRESET_LABEL = "Test backend (stub)"

# A per-item "tag_report" the workspace suggestion write accepts is at most this
# many characters (services/workspace_service/service.py MAX_TAG_REPORT_CHARS).
# The real Process path mirrors it so a valid tag report is forwarded through the
# write rather than cleared, and an over-long one is dropped (text only) instead
# of triggering a 422 that would block the whole batch as an uncertain write.
_MAX_TAG_REPORT_CHARS = 1000


def _bootstrap_remote() -> None:
    """Session state for remote mode: no ProjectState, no local document.

    The remote path reads through the cached remote clients; edits are held as
    session drafts and written only by an explicit Save through the workspace
    service, and an export writes the saved human text to a fresh MQXLIFF
    through the document service. None of the local ProjectState is created.
    """
    ss = st.session_state
    ss.project = None
    ss.document = None
    ss.remote_view = None
    # The path of the last export this session produced (verified parseable); the
    # sidebar offers it for download until it is replaced or the session restarts.
    ss.last_export = ""
    ss.remote_open_marker = None
    ss.remote_batch = 0
    ss.remote_page = 0
    ss.remote_page_size = 0
    # The operator's working batch size, sent on Open; the server persists it and
    # is the source of truth thereafter (a re-read/Reload recovers it).
    ss.remote_batch_size = 100
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
    # Model configuration the remote UI exposes, mirroring the local sidebar:
    # one preset/provider + editable base URL + password key + model selection
    # and generation controls. The config is built into a per-request "config"
    # block and sent to the processing service, which selects the real backend,
    # endpoint and model. The key is held here in the browser session only; it
    # is never written to state.json, tracked files or logs.
    #
    # remote_model_config is the live ClientConfig the controls edit in place
    # (exactly like the local ss.config). It defaults to the local default
    # preset, NOT the stub.
    ss.remote_model_config = llm.ClientConfig()
    ss.remote_model_preset = llm.DEFAULT_PRESET
    # Models the last "List models" call returned for the current config; a stale
    # list is cleared whenever the connection settings change (never refreshed by
    # calling the provider).
    ss.remote_model_list = []
    # Widget re-key counter, bumped on a preset switch so the new preset's base
    # URL / key / model values take over the text inputs instead of the old
    # preset's stale widget values.
    ss.remote_engine_rev = 0
    # A connection-test result cache, kept distinct from the per-request config:
    # it reports the service's own configured engine / readiness, not the model
    # the operator has selected.
    ss.remote_ready = None
    # Set when a Process ends in an uncertain outcome (a stale write, a
    # transport/5xx/malformed write, a failed confirming read, or a bound-to-
    # a-different-original read). It blocks another Process until a successful
    # Reload or Open reconciles the view, so a clean read must precede the next
    # batch. Cleared by a successful Open or Reload.
    ss.remote_process_uncertain = False
    # Cached result of the non-mutating workspace compatibility probe
    # ("ok" / "stale" / "unknown"). Cleared on a successful Open or Reload so a
    # restarted workspace is re-checked before the next Process.
    ss._remote_workspace_compat = None
    # Editorial context sent to the processing service with every Process call.
    # Mirrors the local sidebar's rule/vibe/directive controls; the values are
    # held in this browser session only and are never written to state.json.
    # Defaults match the shipped constants so a fresh session behaves like the
    # local reference.
    ss.remote_hard_rules = defaults.DEFAULT_HARD_RULES
    ss.remote_vibe = defaults.DEFAULT_VIBE
    ss.remote_directive = ""
    ss.remote_source_lang = ""
    ss.remote_target_lang = ""
    # An optional per-batch stage-template override. When non-empty it is sent
    # as context.template and replaces the service's own stage template for the
    # active batch; left blank the service falls back to the same
    # default_prompt_templates() the local reference seeds, so a blank field is
    # parity with the local default. Held in this browser session only.
    ss.remote_template = ""
    # The operator's termbase, sent as context.termbase: the service re-filters
    # it to the terms present in the batch with the same relevant_terms() the
    # local app uses, so an empty list yields no termbase material. A memoQ CSV
    # collection control is a later, browser-accepted feature; the forwarding
    # path exists now. Held in this browser session only.
    ss.remote_termbase = []


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
        batch_size=ss.get("remote_batch_size", 100),
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
    # A fresh open reconciles any prior uncertain Process; it no longer blocks.
    ss.remote_process_uncertain = False
    # The workspace may have been restarted between opens; re-check compatibility.
    ss._remote_workspace_compat = None
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
    # A successful Reload is the reconciliation that unblocks a Process left in
    # an uncertain state: the view now reflects the saved work the operator must
    # trust before processing again.
    ss.remote_process_uncertain = False
    # The workspace may have been restarted between reloads; re-check
    # compatibility before the next Process.
    ss._remote_workspace_compat = None
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


def _effective_your_text(record) -> str:
    """The 'Your text' a row shows right now: the held draft when one is, else
    the saved human text. An explicitly cleared draft counts as blank, so a row
    the operator cleared (or a neighbour they filled) is judged by its current
    text, not by an older saved value.
    """
    return cell_seed(record)[0]


def _eligible_records(view, batch_index) -> list:
    """This batch's rows a Process should send: the translatable rows whose
    effective 'Your text' (draft when held, else saved) is blank after trimming.

    Eligibility is judged per row, so a previous model proposal, an edited
    status or a filled neighbour never blocks an otherwise-eligible row. Rows
    the operator cleared into an unsaved draft are eligible again, and unrelated
    rows are left untouched.
    """
    return [record for record in view.batch_records(batch_index)
            if record.translatable
            and not _effective_your_text(record).strip()]


def eligible_count(view, batch_index) -> int:
    """How many rows in this batch a Process would send to the model."""
    return len(_eligible_records(view, batch_index))


def row_is_process_eligible(view, record) -> bool:
    """True when a single-row Process would send ``record`` to the model.

    A Process sends only a batch's eligible rows - the translatable rows whose
    effective 'Your text' (a held draft, including a cleared one, otherwise the
    saved text) is blank after trimming. This is the per-row view of that rule,
    used to decide whether a row offers a Process button (a filled row does not).
    """
    if record is None or record.key not in view.order:
        return False
    if not record.translatable:
        return False
    batch_index = min(st.session_state.get("remote_batch", 0),
                      view.batch_count - 1)
    return record.key in {r.key for r in _eligible_records(view, batch_index)}


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


def export_remote_document(mark_confirmed: bool = False, only_changed: bool = True) -> None:
    """Export the saved human text to a valid MQXLIFF through the document service.

    Carries ``user_final`` only - never the model proposal: a proposal is not a
    human decision and must not leave the workspace as a committed target, so
    this is a deliberate divergence from the local export's ``new_text()`` (which
    also accepts a proposal as a fallback). Rows whose saved text is blank or,
    when ``only_changed`` is on, still equal to memoQ's target are omitted, so the
    service keeps memoQ's own target for them (a blank translation is rejected).
    Unsaved drafts are warned on but never dropped: they stay in the session until
    an explicit Save or Discard. The result is re-parsed before it is offered for
    download, mirroring the local export, so a file memoQ cannot open is caught
    here rather than on the operator's import.
    """
    ss = st.session_state
    view = ss.get("remote_view")
    if view is None:
        notify("error", "Open a document first.")
        return

    # Unsaved drafts are held, not exported; warn so the operator knows the
    # export reflects saved work only.
    if _has_unsaved_drafts(view.document_id):
        notify("warning", "You have %d unsaved change(s); only your saved "
               "text is exported." % unsaved_count())

    translations = {}
    for key in view.order:
        record = view.record(key)
        if record is None or not record.translatable:
            continue
        text = (record.user_final or "").strip()
        if not text:
            continue
        if only_changed and text == (record.mt_target or "").strip():
            continue
        translations[record.key] = record.user_final

    if not translations:
        notify("info", "Nothing to export - no saved human text to carry.")
        return

    clients = get_remote_clients()
    try:
        meta = clients.document.export(
            view.original_id, translations,
            tag_source=view.tag_source, mark_confirmed=mark_confirmed)
        export_id = str(meta.get("export_id") or "")
        if not export_id:
            raise RemoteError(
                service="document", status=None, code="bad_response",
                message="The export did not return an export id.")
        data = clients.document.export_content(export_id)
    except RemoteError as exc:
        notify("error", "Export failed: %s" % _remote_error_text(exc))
        return

    os.makedirs(EXPORT_DIR, exist_ok=True)
    out_path = os.path.join(
        EXPORT_DIR, mqxliff.export_filename(view.source_file or "document.mqxliff"))
    with open(out_path, "wb") as handle:
        handle.write(data)

    # Re-parse what we just wrote: an export memoQ cannot open is worse than no
    # export, and this catches it here rather than on the operator's import.
    try:
        mqxliff.MqxliffDocument(data, "verify").parse_segments()
    except Exception as exc:  # noqa: BLE001
        notify("error", "Export written but failed re-parse: %s" % exc)
        return

    ss.last_export = out_path
    warnings = meta.get("warnings") or []
    message = "Exported %d segment(s) to `%s` (verified parseable)." % (
        len(translations), out_path)
    if warnings:
        message += "\n\nTag warnings on %d row(s)." % len(warnings)
    notify("warning" if warnings else "success", message)


def apply_proposal(record) -> None:
    """Move one row's model proposal into its editable 'Your text' as a draft.

    This is the explicit acceptance action for a proposal: the operator applies
    the model's ``llm_suggestion`` to their text, reviews it in the 'Your text'
    cell, and then Saves. It is a draft mutation only - it never writes to the
    saved workspace and never sets a saved human text, so a model proposal is not
    auto-accepted as the user's text; only a later explicit Save persists it.

    It keeps the comment the cell currently shows (a held draft when one is, else
    the saved comment) and re-keys the cell so it then shows the proposal. It is
    a no-op when the row has no proposal, or the proposal already equals the text
    the cell shows (so a repeat click neither drafts nor re-keys).
    """
    ss = st.session_state
    view = ss.get("remote_view")
    if view is None:
        return
    proposal = (record.llm_suggestion or "").strip()
    if not proposal:
        return
    current_text, shown_comment = cell_seed(record)
    if current_text == record.llm_suggestion:
        return
    _store_cell(view.document_id, record, record.llm_suggestion, shown_comment)
    _bump_widgets()
    notify("info", "Applied the model's proposal to segment %s as an unsaved "
           "draft - review the 'Your text' cell and click Save to keep it."
           % record.segment_id)


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


# --------------------------------------------------------------- model / process
#
# The remote UI exposes a narrow slice of model configuration - the engine to
# send the selected batch to, plus a connection test - and one mutation, the
# "process the currently selected batch" action. Both the connection test and
# the processing go through the processing service's clients: the model endpoint
# lives in that service's environment, never in this app and never in Qwen
# Code's own config, so this code reads no ~/.qwen settings and no agent
# credentials, and there is no direct Streamlit-to-model call and no local
# workflow fallback. A processed batch's proposals are persisted through the
# workspace contract (``record_suggestions`` stores the model's last proposal as
# the row's ``llm_suggestion`` with a proposal-only write - prefill off - so it
# survives a Reload); the human's saved text (``user_final``), comments and the
# original target (``mt_target``) are never touched, keeping the four data kinds
# distinct. A model proposal is not a human acceptance.


def remote_test_connection() -> None:
    """Probe the processing service's readiness without calling a model.

    ``ready()`` is a GET ``/readyz``: it checks that the service is up and that
    the configured engine's SDK is importable - it never runs a model, sends no
    segment text and carries no credential. It is a *service/SDK readiness*
    check, not a real model probe: success proves the engine is available, not
    that a translation succeeded. On success the result is cached so the engine
    list and the UI can show it; on any failure the cache is cleared so a stale
    "ready" is never shown, and the safe message (no URL or secret) is queued.
    """
    ss = st.session_state
    clients = get_remote_clients()
    try:
        ready = clients.processing.ready()
    except RemoteError as exc:
        ss.remote_ready = None
        notify("error", "Service readiness check failed: %s"
               % _remote_error_text(exc))
        return
    ss.remote_ready = ready
    if isinstance(ready, dict) and ready.get("status") == "ready":
        engine = ready.get("engine")
        notify("success",
               "Processing service is ready: the %s engine's SDK is available. "
               "This checks service readiness; it does not call the model."
               % (engine if engine else "stub"))
    else:
        notify("warning", "Processing service is not ready yet (its engine SDK "
               "or configuration is unavailable). Try again when it is up.")


def _model_config_block(config) -> dict:
    """The per-request "config" block the processing service selects a model by.

    It carries the operator's session selection (preset, backend, base URL, the
    session-only key, model and the generation controls). The key is sent only to
    the processing service for this one request; the service never writes it to
    a log, response or tracked file, and the client never echoes it in an error.
    """
    return {
        "preset": config.preset,
        "backend": config.backend,
        "base_url": config.base_url,
        "api_key": config.api_key,
        "model": config.model,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "chunk_size": config.chunk_size,
        "timeout": config.timeout,
        "max_retries": config.max_retries,
    }


def _model_connection_signature(config) -> tuple:
    """The connection identity that decides which models an endpoint serves.

    A change to any of these makes a previously fetched model list stale, so the
    UI clears the cache rather than showing models that no longer apply.
    """
    return (config.preset, config.backend, config.base_url, config.model,
            config.api_key)


def remote_invalidate_stale_model_list(config) -> None:
    """Clear the cached model list when the connection settings changed.

    The provider is never called to refresh it: the list is simply emptied so the
    model selector falls back to the preset's suggestions until the operator runs
    "List models" again for the new settings.
    """
    ss = st.session_state
    signature = _model_connection_signature(config)
    if ss.get("_remote_model_sig") != signature:
        ss.remote_model_list = []
        ss._remote_model_sig = signature


def remote_model_test() -> None:
    """Run one bounded, user-initiated probe of the selected config.

    The probe runs in the processing service (it imports the model SDK and talks
    to the endpoint); this app only sends the config and shows the result. It is
    a readiness check, not a translation: it stores nothing and never becomes a
    model proposal or user text. A failure is a safe notice (no key or URL); it
    also clears a stale model list so the selector reflects the new settings.
    """
    ss = st.session_state
    config = ss.remote_model_config
    clients = get_remote_clients()
    try:
        result = clients.processing.probe(_model_config_block(config))
    except RemoteError as exc:
        ss.remote_model_list = []
        notify("error", "Model test failed: %s" % _remote_error_text(exc))
        return
    if isinstance(result, dict):
        message = result.get("message", "Probe finished.")
        notify("success" if result.get("ok") else "warning", message)
    else:
        notify("warning", "The model test returned an unexpected response.")


def remote_model_list() -> None:
    """Fetch the models the selected config's endpoint advertises.

    Only this explicit action calls the provider to list models; nothing else in
    the UI does. The result is cached for the selector and tied to the current
    connection settings, so a later settings change clears it.
    """
    ss = st.session_state
    config = ss.remote_model_config
    clients = get_remote_clients()
    try:
        result = clients.processing.list_models(_model_config_block(config))
    except RemoteError as exc:
        ss.remote_model_list = []
        notify("error", "Could not list models: %s" % _remote_error_text(exc))
        return
    models = result.get("models") if isinstance(result, dict) else None
    if isinstance(models, list):
        ss.remote_model_list = models
        ss._remote_model_sig = _model_connection_signature(config)
        notify("success", "Found %d model(s)." % len(models))
    else:
        ss.remote_model_list = []
        notify("warning", "Could not list models: unexpected response.")


def load_termbase_csv_into_session(ss: Dict[str, Any],
                                   data: "bytes | str",
                                   replace: bool = True):
    """Import a memoQ termbase CSV into the remote session's termbase (E.2).

    The remote session keeps its termbase in plain session state rather than a
    :class:`ProjectState`, so this reuses the state-free parser the local path
    shares (:func:`memoq_harness.state.parse_termbase_csv`) and stores the
    entries in the dict-list shape :func:`_remote_context_block` forwards. No
    server-side storage and no editor: the upload lands in the current session
    only. Returns the report so the caller can show what loaded / was skipped.
    """
    entries, report = state.parse_termbase_csv(data)
    parsed = [{"source": e.source, "target": e.target} for e in entries]
    # A CSV that yields no usable terms must not clobber the termbase the
    # operator already loaded: leaving the session's termbase in place keeps the
    # previously active terms, and the caller still surfaces report.summary().
    if not parsed:
        return report
    existing = ss.get("remote_termbase") or []
    if replace:
        ss.remote_termbase = parsed
    else:
        seen = {t.get("source", "") for t in existing}
        ss.remote_termbase = existing + [
            p for p in parsed if p["source"] not in seen]
    return report


def _remote_context_block(ss, view=None, batch_index: int = 0) -> dict:
    """Build the editorial context block sent to the processing service.

    Mirrors the local sidebar's rule/vibe/directive controls: it forwards each
    field the remote session carries (``hard_rules_text``, ``vibe``,
    ``directive``, ``source_lang``, ``target_lang``, ``template``,
    ``termbase``). The bootstrap seeds ``hard_rules``/``vibe`` with the shipped
    defaults and the rest with empty values, so a fresh session forwards the
    defaults and a changed field is sent as the user edits it. A field absent
    from session state (``is None``) is omitted, and the processing service's
    ``Context`` dataclass then falls back to its own default for that field.

    ``template`` is a per-batch stage-template override: when non-empty it
    replaces the service's own stage template for the active batch; left blank
    it is omitted, so the service falls back to the same
    ``default_prompt_templates()`` the local reference seeds (a blank field is
    parity). ``termbase`` is the operator's termbase; when non-empty it is
    forwarded and the service re-filters it to the terms present in the batch
    with the same ``relevant_terms()`` the local app uses, so an empty list
    omits the key and yields no termbase material.

    When a ``view`` is given, the block also carries ``corrections_block``: the
    feedback the local app assembles with ``feedback_block(upto_batch=
    batch_index)`` - the human corrections recorded in *earlier* batches, so a
    later batch's prompt is guided by the operator's edits to the earlier one.
    A batch cannot correct itself, so only corrections from batches before
    ``batch_index`` are included; an empty result omits the key (the service's
    ``Context`` default is ``""``).
    """
    block: dict = {}
    hard_rules = ss.get("remote_hard_rules")
    if hard_rules is not None:
        block["hard_rules_text"] = hard_rules
    vibe = ss.get("remote_vibe")
    if vibe is not None:
        block["vibe"] = vibe
    directive = ss.get("remote_directive")
    if directive is not None:
        block["directive"] = directive
    source_lang = ss.get("remote_source_lang")
    if source_lang is not None:
        block["source_lang"] = source_lang
    target_lang = ss.get("remote_target_lang")
    if target_lang is not None:
        block["target_lang"] = target_lang
    template = ss.get("remote_template")
    if template:
        block["template"] = template
    termbase = ss.get("remote_termbase")
    if termbase:
        block["termbase"] = list(termbase)
    if view is not None:
        corrections_block = view.corrections_block(upto_batch=batch_index)
        if corrections_block:
            block["corrections_block"] = corrections_block
    return block


def _process_target(ss) -> tuple:
    """What the Process call sends to the processing service.

    Returns ``(config, engine)``: a real preset yields the per-request config
    (``engine`` is None, so the service selects the backend/endpoint/model from
    it); the explicit stub sentinel yields ``(None, "stub")`` so the service
    returns the stored machine translation. The two are mutually exclusive, both
    on the client (one or the other, never both) and enforced again by the
    service.
    """
    if ss.get("remote_model_preset") == STUB_PRESET_LABEL:
        return None, "stub"
    return _model_config_block(ss.remote_model_config), None


def _probe_workspace_compat(clients) -> str:
    """Non-mutatingly check that the workspace accepts the prefill=false write.

    The Process path stores model proposals as the model's ``llm_suggestion``
    with ``prefill=False`` (a proposal-only write that never touches a human's
    saved text). The ``prefill`` field was added to the workspace's suggestion
    contract; a service that predates it rejects the field at parse time with a
    400, which the client maps to the generic "rejected as malformed" message
    that previously wasted a paid model call with no stored result. This probe
    detects that staleness *before* any model call.

    It writes to a fresh 64-hex ``document_id`` that nothing is bound to, so no
    DB write can happen: a stale service 400s at parse (the unknown ``prefill``
    field) and a fresh service accepts the field and 404s later ("unknown
    workspace; bind the document first"). Both paths leave the database
    untouched. A 400 means "stale"; a 404 means "ok"; any other outcome
    (transport error, a 5xx, or an unexpected success) is "unknown" and
    proceeds without bypassing prefill or switching to the stub.
    """
    probe_id = os.urandom(32).hex()
    try:
        clients.workspace.record_suggestions(
            probe_id, 1, {"1#1": {"text": "probe"}}, prefill=False)
    except RemoteError as exc:
        status = getattr(exc, "status", None)
        if status == 400:
            return "stale"
        if status == 404:
            return "ok"
        return "unknown"
    # A success or an unexpected non-RemoteError is not a clean discriminator;
    # treat it as "unknown" rather than assume the workspace is compatible.
    return "unknown"


def remote_check_workspace_compat(clients) -> str:
    """Return the workspace-compatibility verdict, probing once and caching it.

    The result ("ok"/"stale"/"unknown") is cached on
    ``ss._remote_workspace_compat`` and reused until the next successful Open or
    Reload clears it, so a Process never re-probes per rerender. A "stale"
    verdict blocks the model call with a restart instruction instead of paying
    for inference that cannot be stored; "ok" and "unknown" both proceed
    (prefill is never removed, retried without, or swapped for the stub).
    """
    ss = st.session_state
    cached = ss.get("_remote_workspace_compat")
    if cached is not None:
        return cached
    verdict = _probe_workspace_compat(clients)
    ss._remote_workspace_compat = verdict
    return verdict


def process_block_reason(view, batch_index) -> str:
    """The first reason processing is blocked, or "" when it is allowed.

    The guards, in order: an unresolved conflict (never over-written silently),
    an uncertain prior Process (a successful Reload or Open must reconcile it
    first, so no batch is processed over a view whose saved state is unverified),
    an incompatible workspace (a known-stale service that predates the prefill
    contract, which would 400 the proposal write - so a paid model call is
    blocked before it is spent), a batch with no translatable segment, and a
    batch whose every translatable row already has text (so there is nothing to
    send). A batch is no longer blocked by a blanket "already worked" check nor
    by unsaved drafts: a row is eligible on its own effective 'Your text' (a
    held draft, including a cleared one, otherwise the saved text), so a filled
    neighbour or a prior proposal never suppresses an eligible row. The same
    guards are re-checked inside ``process_remote_batch`` as a backstop, so a
    click can never bypass them.
    """
    if conflicted_keys(view):
        return "Resolve the conflicts in the Conflicts panel first."
    if st.session_state.get("remote_process_uncertain"):
        return ("The last process did not complete cleanly; Reload to reconcile "
                "the saved state before processing again.")
    if st.session_state.get("_remote_workspace_compat") == "stale":
        return ("The workspace service is incompatible with this client and "
                "cannot store proposals (a paid model call would be wasted). "
                "Restart the workspace service, then Reload and try again.")
    records = view.batch_records(batch_index)
    if not any(record.translatable for record in records):
        return "This batch has no translatable segments to process."
    if not _eligible_records(view, batch_index):
        return ("Every translatable row in this batch already has text; nothing "
                "to process.")
    return ""


def process_remote_batch() -> None:
    """Send the currently selected batch to the model and store its proposals.

    The model call goes through the processing service (no local fallback). The
    proposals are persisted through the workspace's ``record_suggestions`` as the
    model's ``llm_suggestion`` with a proposal-only write (prefill off): a
    human's saved text, comments and the original target are left untouched, so a
    model proposal is never presented as the user's accepted text. Only the
    eligible rows are sent - the translatable rows whose effective 'Your text'
    (a held draft, including a cleared one, otherwise the saved text) is blank
    after trimming - so a filled neighbour, a prior proposal or an edited status
    never blocks an eligible row, and no row is sent twice. Unrelated rows and
    any held drafts are left untouched (the proposal write is prefill-off, so it
    never touches a human's saved text), and a Process never auto-saves or
    discards a draft. Uncertain-outcome handling is deliberately conservative: a
    stale write (409), a transport/5xx/malformed write, a failed confirming read,
    or a read that no longer names the same original are recorded as
    ``remote_process_uncertain`` so another Process is blocked until a successful
    Reload or Open reconciles the view. None of these are rebased, overwritten,
    rerun or auto-retried, and the wordings never claim that nothing was stored
    or that the server refuses a repeat - a successful read/reconciliation must
    precede the next batch.
    """
    ss = st.session_state
    view = ss.get("remote_view")
    if view is None or view.is_empty:
        notify("info", "Open a document and select a batch to process.")
        return
    if conflicted_keys(view):
        notify("error", "Resolve the conflicts in the Conflicts panel before "
               "processing.")
        return
    if ss.get("remote_process_uncertain"):
        notify("warning", "The last process did not complete cleanly; Reload to "
               "reconcile the saved state before processing again.")
        return
    batch_index = min(ss.get("remote_batch", 0), view.batch_count - 1)
    records = view.batch_records(batch_index)
    if not any(record.translatable for record in records):
        notify("info", "This batch has no translatable segments to process.")
        return
    eligible = _eligible_records(view, batch_index)
    if not eligible:
        notify("info", "Every translatable row in this batch already has text; "
               "nothing to process.")
        return

    clients = get_remote_clients()

    # Before paying for a model call, verify the workspace can store the
    # proposal-only write (prefill=false). A stale workspace predates the
    # prefill contract and 400s the write - that is the "rejected as malformed"
    # failure that wasted a paid request before. The probe is non-mutating and
    # blocks on "stale" so no inference is sent to an incompatible workspace;
    # prefill is never removed, retried without, or swapped for the stub.
    if remote_check_workspace_compat(clients) == "stale":
        notify("error",
               "The workspace service is incompatible with this client and "
               "cannot store proposals. No model call was made. Restart the "
               "workspace service (it predates the prefill contract), then "
               "Reload and try again.")
        return

    # Only the eligible rows of this batch go to the model; each carries its key,
    # source text, original target and saved comment. Eligible rows are judged
    # per row by their effective 'Your text', so a row the operator cleared into
    # an unsaved draft is processed again rather than skipped. The operator's
    # editorial context (hard rules, vibe, batch directive, language) is
    # forwarded from the sidebar; a field the UI has not set falls back to the
    # service's shipped default, and the stage is derived from the batch index.
    segment_inputs = [
        {"key": record.key, "source_text": record.source_text,
         "mt_target": record.mt_target, "user_comment": record.user_comment}
        for record in eligible
    ]
    if not segment_inputs:
        notify("info", "This batch has no translatable segments to process.")
        return

    try:
        config, engine = _process_target(ss)
        result = clients.processing.process(
            segments=segment_inputs,
            engine=engine,
            tag_source=view.tag_source,
            batch={"index": batch_index, "count": view.batch_count},
            context=_remote_context_block(ss, view, batch_index),
            config=config,
        )
    except RemoteError as exc:
        # The model call failed or was uncertain; no proposal is claimed and
        # nothing is stored. A 502/503/400 from the service is mapped by the
        # client to a safe RemoteError; no raw body, URL or secret reaches here.
        notify("error", "Processing failed: %s" % _remote_error_text(exc))
        return

    # The model call is stateless (it persists nothing): a response that is not
    # the expected object, or whose "suggestions" is not an object, carries no
    # usable proposal, so nothing is stored and the operator may process again.
    # This is a read-side failure, not an uncertain write, so the flag is left
    # clear and the next Process is not blocked.
    if not isinstance(result, dict):
        notify("error", "The process response was not the expected object; no "
               "proposals were stored. Try processing this batch again.")
        return
    suggestions = result.get("suggestions")
    if not isinstance(suggestions, dict):
        notify("error", "The process response did not carry a valid suggestions "
               "object; no proposals were stored. Try processing this batch "
               "again.")
        return
    missing = result.get("missing")
    if not isinstance(missing, list):
        missing = []
    # Only this batch's keys may be accepted: a suggestion for a segment in
    # another batch of the same document (one the service returned but the app
    # never sent this request) must not be persisted. sent_keys is the exact set
    # the model call carried. Each accepted item keeps only a non-empty string
    # "text" (the workspace requires a string), and, when the service supplies
    # one, a string "tag_report" no longer than the workspace allows, so a valid
    # tag report is preserved through the real Process path rather than cleared.
    sent_keys = {seg["key"] for seg in segment_inputs}
    updates = {}
    for key, item in suggestions.items():
        if key not in sent_keys or not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        entry = {"text": text}
        tag_report = item.get("tag_report")
        if (isinstance(tag_report, str) and tag_report
                and len(tag_report) <= _MAX_TAG_REPORT_CHARS):
            entry["tag_report"] = tag_report
        updates[key] = entry
    if not updates:
        if missing:
            notify("warning", "No proposals were returned for this batch; %d "
                   "segment(s) had none. Nothing was stored." % len(missing))
        else:
            notify("warning", "No proposals were returned for this batch. "
                   "Nothing was stored.")
        return

    before = view.revision
    try:
        # Proposal-only write: the model's text is stored as the row's
        # llm_suggestion (so it survives a Reload), but prefill is off, so it is
        # never copied into user_final. A model proposal is not a human
        # acceptance; putting a proposal into "Your text" is an explicit,
        # separately-saved edit, and Discard before Save restores the prior saved
        # human text while keeping the proposal.
        clients.workspace.record_suggestions(
            view.document_id, view.revision, updates, prefill=False)
    except RemoteError as exc:
        if exc.is_conflict and exc.code == "stale_revision":
            # The workspace moved since it was read. Do not rebase, overwrite,
            # rerun or retry: leave the proposals un-persisted and let the
            # operator reload, so a stale write is never replayed. The flag
            # blocks another Process until the reload reconciles.
            ss.remote_process_uncertain = True
            notify("error", "The workspace was changed elsewhere since it was "
                   "read (current revision %d). The proposals may or may not "
                   "have been stored; Reload to reconcile, then process again."
                   % (exc.current_revision or before))
            return
        if _definitely_not_applied(exc):
            # A 3xx/4xx is a rejection before any write: nothing was stored. The
            # flag blocks another Process until a successful Reload.
            ss.remote_process_uncertain = True
            notify("error", "The proposals could not be stored (%s). Reload and "
                   "process again." % _remote_error_text(exc))
            return
        # Transport failure, 5xx or malformed 2xx: the outcome is unknown - the
        # write may have applied or not. Do not re-read or retry, and do not
        # claim nothing was stored or that the server refuses a repeat: a
        # successful read/reconciliation must precede another Process. The flag
        # blocks it; the wording tells the operator to Reload.
        ss.remote_process_uncertain = True
        notify("error", "The outcome of storing the proposals is uncertain (%s). "
               "Reload to see the current saved state, then process again."
               % exc.message)
        return

    # Confirmed by a 2xx: re-read and adopt so the view shows the stored
    # proposals in the "Model proposal" column. The write is prefill-off, so it
    # touched only llm_suggestion; any held draft is re-anchored (kept, since its
    # base and value are unchanged), so a Process never drops or auto-saves a
    # human's unsaved text.
    try:
        fresh = _read_view(view.document_id, view.original_id, view.tag_source)
    except RemoteError as exc:
        # The write confirmed (a 2xx) but the confirming read failed: the saved
        # state is uncertain. Do not claim nothing was stored or that a repeat
        # is refused; require a read/reconciliation before another Process.
        ss.remote_process_uncertain = True
        notify("warning", "Stored %d proposal(s), but the confirming read failed "
               "(%s). Reload to see the current saved state, then process again."
               % (len(updates), exc.message))
        return
    if fresh is None:
        ss.remote_process_uncertain = True
        notify("warning", "Stored %d proposal(s), but the workspace is now bound "
               "to a different original; re-open the document." % len(updates))
        return
    _adopt_view(fresh)
    missing_note = (" %d segment(s) had no proposal." % len(missing)
                    if missing else "")
    notify("success", "Stored %d proposal(s) in batch %d of %d - revision %d.%s"
           % (len(updates), batch_index + 1, view.batch_count, fresh.revision,
              missing_note))


def rerun_block_reason(view, record) -> str:
    """The first reason a single-row Rerun is blocked, or "" when it is offered.

    The same guards a batch honors: a conflict on any row, an unresolved
    uncertain outcome, or a stale workspace verdict each block the model call so
    a generation is never spent over an out-of-date view. A row that is not
    translatable, or that is no longer in the project, is not offered at all. The
    verdicts are cached, so offering a button never probes the workspace.
    """
    ss = st.session_state
    if view is None or view.is_empty:
        return "Open a document and select a batch to re-run."
    if record is None or record.key not in view.order:
        return "That segment is no longer in the project."
    if not record.translatable:
        return "This segment is not translatable."
    if conflicted_keys(view):
        return "Resolve the conflicts in the Conflicts panel first."
    if ss.get("remote_process_uncertain"):
        return ("The last process did not complete cleanly; Reload to reconcile "
                "the saved state before re-running.")
    if ss.get("_remote_workspace_compat") == "stale":
        return ("The workspace service is incompatible; restart it through the "
                "launcher. Open or Reload to try again.")
    return ""


def rerun_remote_row(record) -> None:
    """Re-run one segment against the model and store its new proposal.

    The model call goes through the processing service as a one-segment
    ``operation="rerun"`` (no local fallback, no auto-retry). The proposal is
    persisted through the workspace's ``record_suggestions`` as the model's
    ``llm_suggestion`` with a proposal-only write (prefill off): a human's saved
    text, comment and the original target are left untouched, so a Copy->Save is
    never erased by a later generation. The operation stays single-segment
    ("rerun", one segment), but the batch block carries the current batch's
    index/count - exactly like ``process_remote_row`` and ``process_remote_batch``
    - so the service stages the re-run with the current batch's stage and framing
    (``stage_for_batch`` of the current batch, not the single-segment "mt1" /
    "1 of 1" default), matching the local reference whose ``build_prompt_for``
    uses ``proj.current_batch``. The context block still carries the operator's
    editorial context and the corrections_block - the human corrections recorded
    in *earlier* batches - so a re-run is judged against the same feedback as its
    batch. The same uncertain-outcome
    guards as a batch apply: a stale write (409), a transport/5xx/malformed
    write, a failed confirming read, or a read that no longer names the same
    original are recorded as ``remote_process_uncertain`` so another Rerun/
    Process is blocked until a successful Reload or Open reconciles the view.
    None are rebased, overwritten, rerun or auto-retried.
    """
    ss = st.session_state
    view = ss.get("remote_view")
    if view is None or view.is_empty:
        notify("info", "Open a document first.")
        return
    if record is None or record.key not in view.order:
        notify("error", "That segment is no longer in the project.")
        return
    if not record.translatable:
        notify("info", "That segment is not translatable; nothing to re-run.")
        return
    if conflicted_keys(view):
        notify("error", "Resolve the conflicts in the Conflicts panel before "
               "re-running.")
        return
    if ss.get("remote_process_uncertain"):
        notify("warning", "The last process did not complete cleanly; Reload to "
               "reconcile the saved state before re-running.")
        return
    clients = get_remote_clients()
    if remote_check_workspace_compat(clients) == "stale":
        notify("error", "The workspace service is incompatible; restart it through "
               "the launcher. The re-run was not run.")
        return

    # A one-segment rerun (operation="rerun", one segment), but it carries the
    # current batch's index/count in the batch block - exactly like
    # process_remote_row and process_remote_batch - so the service stages the
    # re-run with the current batch's stage and framing (stage_for_batch of the
    # current batch, not the single-segment "mt1" / "1 of 1" default). This
    # matches the local reference, whose build_prompt_for is shared by the
    # whole-batch run and the single-row retry and uses proj.current_batch. The
    # context block still carries the operator's editorial context and the
    # corrections_block - the human corrections recorded in earlier batches - so
    # a re-run is judged against the same feedback as its batch. batch_index is
    # the current batch, like process_remote_row and process_block_reason_for_row.
    batch_index = min(ss.get("remote_batch", 0), view.batch_count - 1)
    segment_input = {
        "key": record.key,
        "source_text": record.source_text,
        "mt_target": record.mt_target,
        "user_comment": record.user_comment,
    }
    try:
        config, engine = _process_target(ss)
        result = clients.processing.process(
            segments=[segment_input],
            operation="rerun",
            engine=engine,
            tag_source=view.tag_source,
            batch={"index": batch_index, "count": view.batch_count},
            context=_remote_context_block(ss, view, batch_index),
            config=config,
        )
    except RemoteError as exc:
        notify("error", "Re-run failed: %s" % _remote_error_text(exc))
        return

    if not isinstance(result, dict):
        notify("error", "The re-run returned an unreadable result; it was not "
               "stored.")
        return
    suggestions = result.get("suggestions")
    if not isinstance(suggestions, dict):
        notify("error", "The re-run returned no suggestions; it was not stored.")
        return

    sent_keys = {record.key}
    updates: Dict[str, dict] = {}
    for key, item in suggestions.items():
        if key not in sent_keys or not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str) or text == "":
            continue
        entry: Dict[str, object] = {"text": text}
        report = item.get("tag_report")
        if isinstance(report, str) and report:
            entry["tag_report"] = report[:_MAX_TAG_REPORT_CHARS]
        updates[key] = entry

    if not updates:
        notify("warning", "The re-run returned no usable text for this segment; its "
               "previous text is kept.")
        return

    # The proposal-only write (prefill off) never touches a human's saved text,
    # comment or the original target, so a Copy->Save survives this generation.
    try:
        result = clients.workspace.record_suggestions(
            view.document_id, view.revision, updates, prefill=False)
    except RemoteError as exc:
        # The same no-replay contract as a batch: a stale or transport/5xx/
        # malformed write is recorded, never retried or overwritten.
        if exc.code == "stale_revision" or _definitely_not_applied(exc):
            ss.remote_process_uncertain = True
            notify("error", "The re-run result could not be saved because the "
                   "document changed. It was not retried or overwritten; Reload to "
                   "reconcile the saved state, then re-run.")
            return
        notify("error", "The re-run result could not be saved: %s"
               % _remote_error_text(exc))
        return

    missing = result.get("missing")
    if not isinstance(missing, (list, tuple)):
        missing = []
    missing = [k for k in missing if isinstance(k, str)]
    if missing:
        ss.remote_process_uncertain = True
        notify("error", "The workspace could not store the re-run result; it was "
               "not retried. Reload to reconcile the saved state.")
        return

    # Read back what the workspace now holds, and adopt only if it still names the
    # same original. The proposal lives in llm_suggestion, so the held draft (a
    # Copy, not accepted by this generation) is preserved.
    try:
        fresh = _read_view(view.document_id, view.original_id, view.tag_source)
    except RemoteError as exc:
        ss.remote_process_uncertain = True
        notify("error", "The re-run result was saved, but the saved state could "
               "not be read back: %s Reload to reconcile." % _remote_error_text(exc))
        return
    if fresh is None:
        ss.remote_process_uncertain = True
        notify("error", "The re-run result was saved, but the document now names a "
               "different original; it was not rebased. Open the document again.")
        return
    _adopt_view(fresh)
    notify("success", "Re-ran '%s' -> '%s' (stored as a proposal)."
           % (record.source_text[:80], updates[record.key]["text"][:80]))


def process_block_reason_for_row(view, record) -> str:
    """The first reason a single-row Process is blocked, or "" when offered.

    A per-row Process sends one segment, so the only batch-scoped guard it needs
    is "no eligible row in this batch": the row itself must be translatable and
    have no effective 'Your text' (a filled saved or drafted text is not sent).
    A row that is no longer in the project, or that is not translatable, is not
    offered. The view-level guards (a conflict, an unresolved uncertain outcome,
    a stale workspace) block the model call the same way they block a batch, so
    a generation is never spent over an out-of-date view.
    """
    ss = st.session_state
    if view is None or view.is_empty:
        return "Open a document and select a batch to process."
    if record is None or record.key not in view.order:
        return "That segment is no longer in the project."
    if not record.translatable:
        return "This segment is not translatable."
    batch_index = min(ss.get("remote_batch", 0), view.batch_count - 1)
    if record.key not in {r.key for r in _eligible_records(view, batch_index)}:
        return "This row already has text; nothing to process."
    if conflicted_keys(view):
        return "Resolve the conflicts in the Conflicts panel first."
    if ss.get("remote_process_uncertain"):
        return ("The last process did not complete cleanly; Reload to reconcile "
                "the saved state before processing again.")
    if ss.get("_remote_workspace_compat") == "stale":
        return ("The workspace service is incompatible; restart it through the "
                "launcher. Open or Reload to try again.")
    return ""


def process_remote_row(record) -> None:
    """Process one segment against the model and store its proposal.

    A one-segment batch process: the model call goes through the processing
    service as a single-segment request (no local fallback, no auto-retry). The
    proposal is persisted through the workspace's ``record_suggestions`` as the
    model's ``llm_suggestion`` with a proposal-only write (prefill off): a
    human's saved text, comment and the original target are left untouched, so a
    Copy->Save is never erased by a later generation. The same uncertain-outcome
    guards as a batch apply: a stale write (409), a transport/5xx/malformed
    write, a failed confirming read, or a read that no longer names the same
    original are recorded as ``remote_process_uncertain`` so another
    Process/Rerun is blocked until a successful Reload or Open reconciles the
    view. None are rebased, overwritten, rerun or auto-retried.
    """
    ss = st.session_state
    view = ss.get("remote_view")
    if view is None or view.is_empty:
        notify("info", "Open a document first.")
        return
    if record is None or record.key not in view.order:
        notify("error", "That segment is no longer in the project.")
        return
    if not record.translatable:
        notify("info", "This segment is not translatable; nothing to process.")
        return
    batch_index = min(ss.get("remote_batch", 0), view.batch_count - 1)
    if record.key not in {r.key for r in _eligible_records(view, batch_index)}:
        notify("info", "This row already has text; nothing to process.")
        return
    if conflicted_keys(view):
        notify("error", "Resolve the conflicts in the Conflicts panel before "
               "processing.")
        return
    if ss.get("remote_process_uncertain"):
        notify("warning", "The last process did not complete cleanly; Reload to "
               "reconcile the saved state before processing again.")
        return
    clients = get_remote_clients()
    if remote_check_workspace_compat(clients) == "stale":
        notify("error", "The workspace service is incompatible; restart it "
               "through the launcher. The process was not run.")
        return

    # A one-segment process: the row carries its key, source text, original
    # target and saved comment, with the batch block so the service stages it.
    segment_input = {
        "key": record.key,
        "source_text": record.source_text,
        "mt_target": record.mt_target,
        "user_comment": record.user_comment,
    }
    try:
        config, engine = _process_target(ss)
        result = clients.processing.process(
            segments=[segment_input],
            engine=engine,
            tag_source=view.tag_source,
            batch={"index": batch_index, "count": view.batch_count},
            context=_remote_context_block(ss, view, batch_index),
            config=config,
        )
    except RemoteError as exc:
        notify("error", "Processing failed: %s" % _remote_error_text(exc))
        return

    if not isinstance(result, dict):
        notify("error", "The process returned an unreadable result; it was not "
               "stored.")
        return
    suggestions = result.get("suggestions")
    if not isinstance(suggestions, dict):
        notify("error", "The process returned no suggestions; it was not "
               "stored.")
        return

    sent_keys = {record.key}
    updates: Dict[str, dict] = {}
    for key, item in suggestions.items():
        if key not in sent_keys or not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        entry: Dict[str, object] = {"text": text}
        report = item.get("tag_report")
        if isinstance(report, str) and report:
            entry["tag_report"] = report[:_MAX_TAG_REPORT_CHARS]
        updates[key] = entry

    if not updates:
        notify("warning", "No proposal was returned for this segment; its "
               "previous text is kept.")
        return

    # The proposal-only write (prefill off) never touches a human's saved text,
    # comment or the original target, so a Copy->Save survives this generation.
    before = view.revision
    try:
        clients.workspace.record_suggestions(
            view.document_id, view.revision, updates, prefill=False)
    except RemoteError as exc:
        if exc.is_conflict and exc.code == "stale_revision":
            ss.remote_process_uncertain = True
            notify("error", "The workspace was changed elsewhere since it was "
                   "read (current revision %d). The proposal may or may not "
                   "have been stored; Reload to reconcile, then process again."
                   % (exc.current_revision or before))
            return
        if _definitely_not_applied(exc):
            ss.remote_process_uncertain = True
            notify("error", "The proposal could not be stored (%s). Reload and "
                   "process again." % _remote_error_text(exc))
            return
        ss.remote_process_uncertain = True
        notify("error", "The outcome of storing the proposal is uncertain (%s). "
               "Reload to see the current saved state, then process again."
               % exc.message)
        return

    missing = result.get("missing")
    if not isinstance(missing, (list, tuple)):
        missing = []
    missing = [k for k in missing if isinstance(k, str)]
    if missing:
        ss.remote_process_uncertain = True
        notify("error", "The workspace could not store the process result; it "
               "was not retried. Reload to reconcile the saved state.")
        return

    # Read back what the workspace now holds, and adopt only if it still names
    # the same original. The proposal lives in llm_suggestion, so a held draft
    # (a Copy, not accepted by this generation) is preserved.
    try:
        fresh = _read_view(view.document_id, view.original_id, view.tag_source)
    except RemoteError as exc:
        ss.remote_process_uncertain = True
        notify("error", "The process result was saved, but the saved state "
               "could not be read back: %s Reload to reconcile."
               % _remote_error_text(exc))
        return
    if fresh is None:
        ss.remote_process_uncertain = True
        notify("error", "The process result was saved, but the document now "
               "names a different original; it was not rebased. Open the "
               "document again.")
        return
    _adopt_view(fresh)
    notify("success", "Stored a proposal for '%s' -> '%s'."
           % (record.source_text[:80], updates[record.key]["text"][:80]))


def apply_remote_corrections(batch_index=None) -> None:
    """Turn the current batch's human edits into stored corrections.

    The remote analog of the local ``apply_and_diff``: any held drafts are saved
    first so the workspace holds the human's text (the remote ``capture_grid``),
    then the workspace turns the saved edits - the human's ``user_final`` against
    the model's ``llm_suggestion`` or the original target - into correction
    records and stores them in its snapshot. Because the snapshot carries the
    corrections, they survive a re-read and, through the view's
    ``corrections_block``, guide the next batch's prompt. A batch whose edits
    match its baseline produces no correction (a no-op). The uncertain-outcome
    guards match the process paths: a stale write or a failed confirming read is
    recorded as ``remote_process_uncertain`` and is not rebased, overwritten or
    auto-retried.
    """
    ss = st.session_state
    view = ss.get("remote_view")
    if view is None or view.is_empty:
        notify("info", "Open a document first.")
        return
    if conflicted_keys(view):
        notify("error", "Resolve the conflicts in the Conflicts panel before "
               "applying.")
        return
    if ss.get("remote_process_uncertain"):
        notify("warning", "The last process did not complete cleanly; Reload to "
               "reconcile the saved state before applying again.")
        return
    # The corrections are computed from the *saved* human text, so any held
    # drafts must reach the workspace first. save_remote_drafts returns True when
    # nothing is unsaved (a no-op) and re-reads the workspace when it writes.
    if not save_remote_drafts():
        return
    view = ss.get("remote_view")
    if view is None:
        return
    batch_index = (batch_index if batch_index is not None
                   else min(ss.get("remote_batch", 0), view.batch_count - 1))
    clients = get_remote_clients()
    before = view.revision
    try:
        result = clients.workspace.apply_corrections(
            view.document_id, view.revision, batch_index)
    except RemoteError as exc:
        if exc.is_conflict and exc.code == "stale_revision":
            ss.remote_process_uncertain = True
            notify("error", "The workspace was changed elsewhere since it was "
                   "read (current revision %d). Apply was not retried; Reload to "
                   "reconcile, then apply again."
                   % (exc.current_revision or before))
            return
        if _definitely_not_applied(exc):
            notify("error", "Could not apply changes (%s). No correction was "
                   "stored." % _remote_error_text(exc))
            return
        ss.remote_process_uncertain = True
        notify("error", "Applying the changes may or may not have stored the "
               "corrections (%s). Reload to see the current saved state, then "
               "apply again." % exc.message)
        return

    # Re-read so the stored corrections feed the next batch's feedback block.
    try:
        fresh = _read_view(view.document_id, view.original_id, view.tag_source)
    except RemoteError as exc:
        ss.remote_process_uncertain = True
        notify("error", "The changes were applied, but the saved state could not "
               "be read back (%s). Reload to reconcile."
               % _remote_error_text(exc))
        return
    if fresh is None:
        ss.remote_process_uncertain = True
        notify("error", "The changes were applied, but the document now names a "
               "different original; it was not rebased. Open the document again.")
        return
    _adopt_view(fresh)
    produced = result.get("produced")
    notify("success", "Applied batch %d: %d correction(s) will guide the next "
           "batch." % (batch_index + 1, produced if produced is not None else 0))


def run_remote_analysis(batch_index=None) -> None:
    """Run a corrections critique as a separate, read-only action.

    The remote analog of the local ``run_analysis`` / ``analyze_corrections``:
    it reads the *stored* corrections from the view (the same records the local
    path reads from ``proj.corrections``), asks the processing service to
    compute a critique over them, and persists the result in a separate
    ``analysis`` slot through the workspace service. It never overwrites the
    operator's translation (``user_final`` / ``llm_suggestion`` / ``mt_target``
    / the held draft) - the critique is returned to the operator and stored only
    in ``analysis``, keyed by batch, exactly as the local ``proj.analysis[str(
    current_batch)]``.

    Unlike ``apply_remote_corrections``, it does NOT flush held drafts first:
    the critique runs on the *stored* corrections, matching the local ``run_
    analysis`` which reads ``proj.corrections`` (a prior apply), not unsaved
    drafts. A batch whose corrections were just applied is the input; nothing
    unsaved is required.

    The uncertain-outcome guards match the apply/process paths: an empty view,
    unresolved conflicts or a pending uncertain write short-circuit before any
    request. A stale revision or a failed confirming read is recorded as
    ``remote_process_uncertain`` and is not rebased, overwritten or auto-retried.
    """
    ss = st.session_state
    view = ss.get("remote_view")
    if view is None or view.is_empty:
        notify("info", "Open a document first.")
        return
    if conflicted_keys(view):
        notify("error", "Resolve the conflicts in the Conflicts panel before "
               "analysing.")
        return
    if ss.get("remote_process_uncertain"):
        notify("warning", "The last write did not complete cleanly; Reload to "
               "reconcile the saved state before analysing again.")
        return
    corrections = [dict(c) for c in (view.corrections or [])]
    if not corrections:
        notify("info", "No corrections recorded yet - apply a batch first.")
        return
    batch_index = (batch_index if batch_index is not None
                   else min(ss.get("remote_batch", 0), view.batch_count - 1))
    expected = view.expected_tags()
    hard_rules = ss.get("remote_hard_rules") or defaults.DEFAULT_HARD_RULES
    clients = get_remote_clients()
    # Compute the critique through the processing service (never the local
    # llm.LLMClient.analyze_corrections): the service selects the engine.
    try:
        result = clients.processing.analyze(
            corrections=corrections, hard_rules_text=hard_rules,
            expected_tags=expected)
    except RemoteError as exc:
        if _definitely_not_applied(exc):
            notify("error", "Could not run the analysis (%s). No critique was "
                   "stored." % _remote_error_text(exc))
            return
        ss.remote_process_uncertain = True
        notify("error", "The analysis may or may not have completed (%s). "
               "Reload to reconcile." % exc.message)
        return
    critique = result.get("critique") or ""
    # Persist the critique in a separate analysis slot through the workspace.
    before = view.revision
    try:
        result = clients.workspace.record_analysis(
            view.document_id, view.revision, batch_index, critique)
    except RemoteError as exc:
        if exc.is_conflict and exc.code == "stale_revision":
            ss.remote_process_uncertain = True
            notify("error", "The workspace was changed elsewhere since it was "
                   "read (current revision %d). The critique was not stored; "
                   "Reload to reconcile, then analyse again."
                   % (exc.current_revision or before))
            return
        if _definitely_not_applied(exc):
            notify("error", "Could not store the analysis (%s). No critique was "
                   "stored." % _remote_error_text(exc))
            return
        ss.remote_process_uncertain = True
        notify("error", "Storing the analysis may or may not have written "
               "(%s). Reload to see the current saved state, then analyse "
               "again." % exc.message)
        return
    # Re-read so the stored analysis shows in the panel and the revision is
    # fresh for the next action.
    try:
        fresh = _read_view(view.document_id, view.original_id, view.tag_source)
    except RemoteError as exc:
        ss.remote_process_uncertain = True
        notify("error", "The analysis was stored, but the saved state could not "
               "be read back (%s). Reload to reconcile."
               % _remote_error_text(exc))
        return
    if fresh is None:
        ss.remote_process_uncertain = True
        notify("error", "The analysis was stored, but the document now names a "
               "different original; it was not rebased. Open the document again.")
        return
    _adopt_view(fresh)
    notify("success", "Analysed %d correction(s) for batch %d."
           % (len(corrections), batch_index + 1))


def set_remote_batch_size(batch_size=None) -> None:
    """Re-batch the live document into a new working layout.

    The remote analog of the local batch-size change: it asks the workspace
    service to re-slice the document into working batches of the requested size
    and, when the size actually differs, start a *new layout*. A new layout
    leaves the stored corrections in history (they keep their original
    batch_index and the layout they were produced under), so the new layout's
    feedback never adopts an earlier layout's correction even when the batch
    indices collide, and a later apply in the new layout cannot erase the old
    layout's history. A same-size request is a no-op that starts no new layout.

    Unlike ``apply_remote_corrections`` it does NOT flush held drafts first: a
    re-batch is structural, so the operator's drafts survive by key through
    ``_adopt_view``. The same uncertain-outcome guards apply: an empty view,
    unresolved conflicts or a pending uncertain write short-circuit before any
    request; a stale revision or a failed confirming read is recorded as
    ``remote_process_uncertain`` and is not rebased, overwritten or auto-retried.
    """
    ss = st.session_state
    view = ss.get("remote_view")
    if view is None or view.is_empty:
        notify("info", "Open a document first.")
        return
    if conflicted_keys(view):
        notify("error", "Resolve the conflicts in the Conflicts panel before "
               "changing the batch size.")
        return
    if ss.get("remote_process_uncertain"):
        notify("warning", "The last write did not complete cleanly; Reload to "
               "reconcile the saved state before changing the batch size.")
        return
    size = int(batch_size) if batch_size is not None else int(ss.get("remote_batch_size", 100))
    before = view.revision
    clients = get_remote_clients()
    try:
        result = clients.workspace.set_batch_size(
            view.document_id, view.revision, size)
    except RemoteError as exc:
        if exc.is_conflict and exc.code == "stale_revision":
            ss.remote_process_uncertain = True
            notify("error", "The workspace was changed elsewhere since it was "
                   "read (current revision %d). The batch size was not changed; "
                   "Reload to reconcile, then change it again."
                   % (exc.current_revision or before))
            return
        if _definitely_not_applied(exc):
            notify("error", "Could not change the batch size (%s). No layout was "
                   "started." % _remote_error_text(exc))
            return
        ss.remote_process_uncertain = True
        notify("error", "Changing the batch size may or may not have started a "
               "new layout (%s). Reload to see the current saved state, then "
               "change it again." % exc.message)
        return
    # Re-read so the new layout (resliced batches, new version) becomes the live
    # view; the operator's drafts survive by key through the rebase.
    try:
        fresh = _read_view(view.document_id, view.original_id, view.tag_source)
    except RemoteError as exc:
        ss.remote_process_uncertain = True
        notify("error", "The batch size was changed, but the saved state could "
               "not be read back (%s). Reload to reconcile."
               % _remote_error_text(exc))
        return
    if fresh is None:
        ss.remote_process_uncertain = True
        notify("error", "The batch size was changed, but the document now names a "
               "different original; it was not rebased. Open the document again.")
        return
    _adopt_view(fresh)
    if result.get("changed"):
        notify("success", "Re-batched into batches of %d (layout %d). Earlier "
               "corrections stay in history and no longer guide these batches."
               % (fresh.batch_size, fresh.layout_version))
    else:
        notify("info", "Batches are already %d; no new layout was started."
               % fresh.batch_size)
