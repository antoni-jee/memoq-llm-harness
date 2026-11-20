"""Remote-mode UI: the sidebar, grid and row.

The sidebar shows the open document (apart from the uploader's selection) and
offers Save, Discard and Reload through the workflow module, plus the per-request
model configuration the remote operator chooses - a preset/provider, an editable
base URL, a session-only key, a model selection, a Test/List-models action and
the inference settings - which is sent to the processing service that calls the
model (the app never talks to the provider and never reads Qwen Code's own
config). The grid shows the merged segments with
presentation-only batch navigation and editable human-text and comment cells.
Those cells feed a session-scoped draft store (owned by ``remote_workflow``) and
reach the workspace only through the explicit Save action. A dirty Open/Reload is
held for an explicit save/discard/cancel answer, and rows changed elsewhere since
they were edited are listed in a Conflicts panel with Keep mine / Use server /
Show row. One gated mutation is offered - "Process Current Batch", which sends
the selected batch to the model through the processing service and persists the
proposals - blocked while the document is dirty or conflicted or the batch is
already worked. There is no Apply/Rerun/Analyse/Export and no tag injection.

It depends on ``ui_common`` (the shared UI helpers) and ``remote_workflow``
(the orchestration it calls), but never on ``app``. The
``remote_ui -> remote_workflow`` edge is one-way, so there is no import cycle.
"""

from __future__ import annotations

import streamlit as st

from harness_remote import RemoteError, RemoteViewRecord
from ui_common import APP_TITLE, notify, _display, upload_marker
from memoq_harness import llm

from remote_workflow import (
    maybe_remote_open, request_reload, _remote_error_text,
    save_remote_drafts, discard_remote_drafts, resolve_conflict, resolve_pending,
    sync_drafts_from_widgets, unsaved_count, conflicted_keys, conflict_fields,
    edited_fields, cell_seed, _drafts_for, row_location,
    remote_text_widget_key, remote_comment_widget_key,
    remote_test_connection,
    remote_model_test, remote_model_list, remote_invalidate_stale_model_list,
    STUB_PRESET_LABEL, process_remote_batch,
    process_block_reason, eligible_count,
)


def render_remote_sidebar() -> None:
    """Remote-mode sidebar: document identity, Save/Discard and Reload.

    It runs first in every remote rerun, so it starts by copying the editing
    cells into the draft store: every handler below (and in the grid) then
    decides on the operator's latest typing - including a field edited and
    left by clicking Save directly.
    """
    ss = st.session_state
    sync_drafts_from_widgets()
    with st.sidebar:
        st.title(APP_TITLE)
        st.caption("Remote mode - open, edit and save saved work")

        st.header("1. Connection")
        st.info("Backend: remote (document / workspace / processing services).")

        # Model configuration the remote operator chooses per request. It mirrors
        # the local sidebar: a preset/provider, an editable base URL, a password
        # key (held in this browser session only), a selectable or entered model,
        # a Test action, a List-models action and the inference settings. The
        # selection is sent to the processing service, which calls the model - the
        # app never talks to the provider and never falls back to a local
        # workflow. "Test backend (stub)" is the only offline option; it maps to
        # the service's stub engine and returns the stored machine translation.
        # This is independent of the open document, so it is offered before the
        # document section.
        st.subheader("Model")
        st.caption("Choose the provider and model a batch is sent to. The model "
                   "is called by the processing service for this one request; the "
                   "key is held in this browser session only and is never written "
                   "to disk or logged.")
        # Service readiness is a separate check from the model selection: it
        # confirms the service is up and its SDK is importable, but it does not
        # call the model.
        if st.button("Check service readiness", width='stretch'):
            remote_test_connection()
            st.rerun()
        ready = ss.get("remote_ready")
        if isinstance(ready, dict):
            state = "ready" if ready.get("status") == "ready" else "not ready"
            engine = ready.get("engine") or "(none configured)"
            st.caption("Service readiness: %s - configured engine %s (a "
                       "service/SDK check; it does not call the model)."
                       % (state, engine))
        else:
            st.caption("Service readiness: no check yet. Run one to confirm the "
                       "service is up (a service/SDK check, not a model call).")

        config = ss.remote_model_config
        rev = ss.get("remote_engine_rev", 0)
        preset_options = [STUB_PRESET_LABEL] + list(llm.PRESETS)
        current_preset = ss.get("remote_model_preset", llm.DEFAULT_PRESET)
        chosen = st.selectbox(
            "Provider preset", preset_options,
            index=preset_options.index(current_preset),
            key="remote_preset_select",
            help="The provider and model selection sent to the processing "
                 "service. 'Test backend (stub)' returns the machine "
                 "translation instead of calling a model.")
        # A preset switch resets the connection to the preset's defaults and
        # gives the following inputs a fresh key so the previous preset's stale
        # values cannot linger; the new preset's values take over on the rerun.
        if chosen != ss.remote_model_preset:
            ss.remote_model_preset = chosen
            if chosen != STUB_PRESET_LABEL:
                preset_def = llm.PRESETS[chosen]
                config.preset = chosen
                config.backend = preset_def.backend
                config.base_url = preset_def.base_url
                config.model = preset_def.default_model
                config.api_key = ""
            ss.remote_model_list = []
            ss.remote_engine_rev = rev + 1
            rev = rev + 1
            st.rerun()
        if ss.remote_model_preset == STUB_PRESET_LABEL:
            st.caption("The stub returns the stored machine translation; it does "
                       "not call a model. The service must still be reachable "
                       "for the request.")
        else:
            preset_def = llm.PRESETS[ss.remote_model_preset]
            config.base_url = st.text_input(
                "Base URL", value=config.base_url,
                key="remote_base_url_%d" % rev,
                help="The model endpoint. It must be reachable FROM the "
                     "processing service, which may run on a different host or "
                     "container than this app.")
            key_label = "API key"
            if preset_def.backend == llm.BACKEND_ANTHROPIC:
                key_label = "Anthropic API key"
            if not preset_def.requires_key:
                key_label += " (optional)"
            config.api_key = st.text_input(
                key_label, value=config.api_key, type="password",
                key="remote_api_key_%d" % rev,
                help="Held in this browser session only - never written to "
                     "state.json. Cleared when you switch the provider preset.")
            model_options = list(ss.remote_model_list
                                or preset_def.model_suggestions)
            if config.model not in model_options:
                model_options = [config.model] + model_options
            config.model = st.selectbox(
                "Model", model_options,
                index=model_options.index(config.model),
                accept_new_options=True,
                key="remote_model_%d" % rev,
                help="The model id sent to the endpoint. Run 'List models' to "
                     "fill the options from the endpoint, or type a model id.")
            col_a, col_b = st.columns(2)
            with col_a:
                if st.button("Test model", width='stretch'):
                    remote_model_test()
                    st.rerun()
            with col_b:
                if st.button("List models", width='stretch'):
                    remote_model_list()
                    st.rerun()
            st.caption(llm.connection_summary(config))
            with st.expander("Inference settings"):
                config.temperature = st.slider(
                    "Temperature", 0.0, 1.5, float(config.temperature), 0.05,
                    key="remote_temperature_%d" % rev)
                config.chunk_size = st.number_input(
                    "Context chunks", min_value=1, max_value=100,
                    value=int(config.chunk_size), step=1,
                    key="remote_chunk_size_%d" % rev)
                config.max_tokens = st.number_input(
                    "Max tokens", min_value=0, max_value=32000,
                    value=int(config.max_tokens), step=1,
                    key="remote_max_tokens_%d" % rev)
                config.timeout = st.number_input(
                    "Timeout (seconds)", min_value=10, max_value=3600,
                    value=int(config.timeout), step=1,
                    key="remote_timeout_%d" % rev)
            # A change to the connection settings (base URL, key, model, preset)
            # makes a previously fetched model list stale; clear it without
            # calling the provider to refresh it.
            remote_invalidate_stale_model_list(config)

        st.header("2. Document")
        upload = st.file_uploader("memoQ bilingual XLIFF",
                                  type=["mqxliff", "xliff", "xlf", "xml"],
                                  key="remote_uploader_%d"
                                      % ss.get("remote_uploader_gen", 0))
        selected_marker = None
        if upload is None:
            # Emptying the uploader re-arms it, mirroring the local sidebar, and
            # withdraws a pending or cancelled Open of the removed file. The
            # open document is unaffected.
            ss.remote_open_marker = None
            ss.remote_declined_marker = None
            pending = ss.get("remote_pending")
            if pending and pending.get("action") == "open":
                ss.remote_pending = None
        else:
            data = upload.getvalue()
            selected_marker = upload_marker(upload.name, data)
            try:
                if maybe_remote_open(data, upload.name):
                    st.rerun()
            except RemoteError as exc:
                # The marker is already recorded, so a bad file is not retried on
                # every rerun. A failed Open leaves the live view untouched
                # (remote_open only assigns on full success); just report it safely.
                notify("error", _remote_error_text(exc))

        view = ss.get("remote_view")
        open_file = ss.get("remote_open_file") or {}
        if view is not None:
            # The document actually open, shown apart from the uploader.
            st.markdown("**Open document:** %s"
                        % (_display(open_file.get("name") or view.source_file)
                           or "(unnamed)"))
            st.caption("Revision %d - document %s..."
                       % (view.revision, view.document_id[:12]))
        if upload is not None and selected_marker != open_file.get("marker"):
            pending = ss.get("remote_pending") or {}
            if pending.get("marker") == selected_marker:
                state = "waiting for your choice above the grid"
            elif selected_marker == ss.get("remote_declined_marker"):
                state = "cancelled"
            else:
                state = "could not be opened"
            st.caption(":orange[Selected, not open (%s):] %s"
                       % (state, _display(upload.name)))

        if view is None:
            st.info("Upload a `.mqxliff` file to open its saved work.")
            return

        counts = view.counts()
        col_a, col_b = st.columns(2)
        col_a.metric("Segments", counts.get("active", 0))
        col_b.metric("Batches", view.batch_count)

        st.header("3. Changes")
        st.caption("Edits are drafts until you click Save. A browser refresh "
                   "can lose unsaved drafts.")
        count = unsaved_count()
        conflicts = conflicted_keys(view)
        if conflicts:
            st.error("%d conflict(s): resolve them in the Conflicts panel above "
                     "the grid. Save is blocked until then." % len(conflicts))
        elif count:
            st.warning("%d unsaved change(s)." % count)
        can_save = bool(count) and not conflicts
        if st.button("Save", type="primary" if can_save else "secondary",
                     width='stretch', disabled=not can_save,
                     help="Write your unsaved changes to the saved workspace "
                          "at revision %d." % view.revision):
            save_remote_drafts()
            st.rerun()
        if st.button("Discard", width='stretch', disabled=not count,
                     help="Discard your unsaved changes and show the saved "
                          "text again."):
            discard_remote_drafts()
            st.rerun()
        if st.button("Reload saved work", width='stretch',
                     help="Re-read the saved workspace and refresh the "
                          "segments. Checks the document identity has not "
                          "changed. With unsaved changes you are asked first."):
            try:
                request_reload()
            except RemoteError as exc:
                # A malformed or unavailable read keeps the live view
                # (remote_reload reassigns it only on full success); report it
                # safely, mirroring the Open path, instead of raising into the
                # run.
                notify("error", _remote_error_text(exc))
            st.rerun()


def _render_pending() -> None:
    """The explicit question for a dirty Open/Reload, above the grid."""
    ss = st.session_state
    pending = ss.get("remote_pending")
    if not pending:
        return
    count = unsaved_count()
    if pending["action"] == "open":
        what = "Opening **%s**" % _display(pending.get("filename", ""))
        choices = [("save", "Save and open", "primary"),
                   ("discard", "Discard and open", "secondary"),
                   ("cancel", "Cancel", "secondary")]
    else:
        what = "Reloading"
        choices = [("save", "Save and reload", "primary"),
                   ("keep", "Reload, keep my changes", "secondary"),
                   ("discard", "Discard and reload", "secondary"),
                   ("cancel", "Cancel", "secondary")]
    blocked = bool(conflicted_keys())
    with st.container(border=True):
        st.warning("You have %d unsaved change(s). %s would replace the view "
                   "they belong to. What should happen to them?" % (count, what))
        if blocked:
            st.caption("Saving is blocked until the conflicts below are "
                       "resolved.")
        cols = st.columns(len(choices))
        for col, (choice, label, kind) in zip(cols, choices):
            if col.button(label, type=kind, width='stretch',
                          disabled=blocked and choice == "save",
                          key="remote_pending_%s" % choice):
                resolve_pending(choice)
                st.rerun()


def _render_conflicts(view) -> None:
    """Every unresolved conflict, wherever its row is, with its resolution.

    Shows the operator's draft, the value now saved elsewhere and the value the
    draft started from, plus Keep mine / Use server (each changes only the
    local draft - Save is still the only write) and Show row, which moves the
    grid to the row's batch and page.
    """
    ss = st.session_state
    keys = conflicted_keys(view)
    if not keys:
        return
    labels = {"text": "Text", "comment": "Comment"}
    attrs = {"text": ("base_text", "user_final"),
             "comment": ("base_comment", "user_comment")}
    drafts = _drafts_for(view.document_id)
    with st.container(border=True):
        st.error("**Conflicts (%d).** These rows were saved elsewhere after you "
                 "started editing them. For each, keep your draft or take the "
                 "saved value. Choosing does not write; Save stays blocked "
                 "until every conflict is resolved." % len(keys))
        for key in keys:
            record, draft = view.record(key), drafts[key]
            where = row_location(view, key)
            st.markdown("Segment `%s` - batch %d"
                        % (record.segment_id, (where[0] + 1) if where else 0))
            for field in conflict_fields(draft, record):
                base, attr = attrs[field]
                st.caption("%s - your draft: %s" % (
                    labels[field], _display(draft[field]) or "(empty)"))
                st.caption("%s - now saved: %s" % (
                    labels[field], _display(getattr(record, attr)) or "(empty)"))
                st.caption("%s - when you started: %s" % (
                    labels[field], _display(draft[base]) or "(empty)"))
            keep_col, server_col, show_col = st.columns(3)
            if keep_col.button("Keep mine", key="remote_keep_%s" % key,
                               help="Your draft replaces the newer saved value "
                                    "on the next Save."):
                resolve_conflict(key, keep_mine=True)
                st.rerun()
            if server_col.button("Use server", key="remote_server_%s" % key,
                                 help="Drop your edit of the conflicting field "
                                      "and take the newer saved value."):
                resolve_conflict(key, keep_mine=False)
                st.rerun()
            if where and show_col.button("Show row", key="remote_show_%s" % key):
                ss.remote_batch, ss.remote_page = where
                st.rerun()


def render_remote_grid() -> None:
    """Segment grid for remote mode.

    Shows the merged segments - source, the memoQ original target, the model's
    proposal and the editable human text (with the saved value shown beside an
    unsaved edit) - with presentation-only batch navigation. The pending
    Open/Reload question and the Conflicts panel sit above the rows; Save and
    Discard live in the sidebar and act on every draft, rendered or not. A
    gated "Process Current Batch" button sends the batch's eligible rows to the
    model and persists their proposals; it is disabled while a conflict is open,
    a prior Process must be reconciled, the workspace is incompatible, or no row
    is eligible (a row is eligible on its own effective 'Your text', so a filled
    neighbour or a prior proposal never blocks an eligible row).
    """
    ss = st.session_state
    view = ss.get("remote_view")
    if view is None:
        st.info("Upload a `.mqxliff` file in the sidebar to open its saved "
                "work.")
        return
    _render_pending()
    _render_conflicts(view)
    if view.is_empty:
        st.warning("No segments are saved for this workspace yet.")
        return

    batch_index = min(ss.get("remote_batch", 0), view.batch_count - 1)
    ss.remote_batch = batch_index
    records = view.batch_records(batch_index)
    progress = view.progress(batch_index)

    head_left, head_right = st.columns([3, 2])
    with head_left:
        st.subheader("Batch %d of %d" % (batch_index + 1, view.batch_count))
        st.caption("%d segments - %d processed, %d applied, %d edited"
                   % (progress.total, progress.processed, progress.applied,
                      progress.edited))
    with head_right:
        st.progress(progress.applied / max(1, progress.total))

    # Presentation-only navigation: it moves the batch but never runs a diff or
    # a save. Drafts live in the store, so they survive the move.
    nav = st.columns([1, 1, 2])
    target = batch_index
    if nav[0].button("Previous", width='stretch', disabled=batch_index == 0):
        target = batch_index - 1
    if nav[1].button("Next", width='stretch',
                     disabled=batch_index >= view.batch_count - 1):
        target = batch_index + 1
    jump = nav[2].number_input(
        "Go to", 1, view.batch_count, value=batch_index + 1,
        key="remote_batch_jump_%d" % batch_index,
        label_visibility="collapsed",
    )
    if target == batch_index and int(jump) - 1 != batch_index:
        target = int(jump) - 1
    if target != batch_index:
        ss.remote_batch = max(0, min(target, view.batch_count - 1))
        ss.remote_page = 0
        st.rerun()

    # One gated mutation: send this batch's eligible rows to the model through
    # the processing service and persist their proposals. The same guards that
    # disable the button are re-checked inside process_remote_batch, so a click
    # can never bypass them. A row is eligible on its own effective 'Your text'
    # (a held draft, including a cleared one, otherwise the saved text), so a
    # filled neighbour or a prior proposal never suppresses an eligible row; the
    # button is disabled only when the batch is conflicted, an uncertain Process
    # must be reconciled, the workspace is incompatible, or no row is eligible.
    eligible = eligible_count(view, batch_index)
    block = process_block_reason(view, batch_index)
    st.caption("Process sends this batch's %d eligible segment(s) - the "
               "translatable rows still without text - to the model and stores "
               "their proposals under 'Model proposal'. If the store outcome is "
               "uncertain, Reload to reconcile before processing again."
               % eligible)
    if st.button("Process Current Batch", type="primary",
                 width='stretch', disabled=bool(block)):
        process_remote_batch()
        st.rerun()
    if block:
        st.caption(":orange[%s]" % block)

    page_size = ss.get("remote_page_size", 0) or len(records)
    page_count = max(1, (len(records) + page_size - 1) // page_size)
    page = min(ss.get("remote_page", 0), page_count - 1)
    if page_count > 1:
        page = st.radio(
            "Page", range(page_count), index=page, horizontal=True,
            key="remote_page_radio_%d_%d" % (batch_index, page),
            format_func=lambda p: "%d-%d" % (p * page_size + 1,
                                             min((p + 1) * page_size, len(records))),
        )
    ss.remote_page = page
    visible = records[page * page_size: (page + 1) * page_size]

    st.divider()

    header = st.columns([1, 3, 3, 4, 3])
    for col, label in zip(header, ("ID", "Source", "memoQ target",
                                   "Model proposal", "Your text")):
        col.markdown("**%s**" % label)

    for record in visible:
        _render_remote_row(record)


def _render_remote_row(record: RemoteViewRecord) -> None:
    """One segment row: read-only source, memoQ target and model proposal, plus
    an editable human-text cell and a per-segment comment cell.

    The cells seed from the draft when one is held, else from the saved record.
    When the row holds an unsaved edit, the saved value is shown under the cell
    so the original target, the model proposal, the saved text and the draft
    stay four distinct things on screen. A conflicting row is marked and points
    to the Conflicts panel, where it is resolved.
    """
    ss = st.session_state
    view = ss.get("remote_view")
    draft = _drafts_for(view.document_id).get(record.key)
    edited = edited_fields(draft) if draft else []
    conflict = conflict_fields(draft, record) if draft else []

    seed_text, seed_comment = cell_seed(record)
    text_key = remote_text_widget_key(record.key)
    comment_key = remote_comment_widget_key(record.key)
    ss.setdefault(text_key, seed_text)
    ss.setdefault(comment_key, seed_comment)

    cols = st.columns([1, 3, 3, 4, 3])
    with cols[0]:
        st.markdown("`%s`" % record.segment_id)
        if conflict:
            st.caption(":red[conflict]")
        elif edited:
            st.caption(":orange[unsaved]")
        if record.edited_by_human:
            st.caption("edited")
        if record.status in ("processed", "applied"):
            st.caption(record.status)
        if record.tag_report:
            st.caption(":red[tags]")
    cols[1].markdown(_display(record.source_text) or ":grey[(empty)]")
    cols[2].markdown(_display(record.mt_target) or ":grey[(empty)]")
    cols[3].markdown(_display(record.llm_suggestion) or ":grey[(none)]")
    with cols[4]:
        st.text_area("Your text", key=text_key, height=120,
                     label_visibility="collapsed",
                     help="Edit the human text. It stays a draft until Save; "
                          "Discard restores the saved value. An empty box is "
                          "saved as empty text.")
        st.text_input("Comment", key=comment_key, label_visibility="collapsed",
                      placeholder="comment",
                      help="Per-segment comment, saved with the text.")
        if "text" in edited:
            st.caption("Saved text: %s" % (_display(draft["base_text"])
                                           or "(empty)"))
        if "comment" in edited:
            st.caption("Saved comment: %s" % (_display(draft["base_comment"])
                                              or "(empty)"))
        if conflict:
            st.caption(":red[Changed elsewhere - resolve in the Conflicts "
                       "panel above.]")
