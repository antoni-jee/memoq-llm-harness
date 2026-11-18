"""Remote-mode UI: the sidebar, grid and row.

The sidebar shows the open document (apart from the uploader's selection) and
offers Save, Discard and Reload through the workflow module. The grid shows the
merged segments with presentation-only batch navigation and editable human-text
and comment cells. Those cells feed a session-scoped draft store (owned by
``remote_workflow``) and reach the workspace only through the explicit Save
action. A dirty Open/Reload is held for an explicit save/discard/cancel answer,
and rows changed elsewhere since they were edited are listed in a Conflicts
panel with Keep mine / Use server / Show row. There is no
Process/Apply/Rerun/Analyse/Export and no tag injection.

It depends on ``ui_common`` (the shared UI helpers) and ``remote_workflow``
(the orchestration it calls), but never on ``app``. The
``remote_ui -> remote_workflow`` edge is one-way, so there is no import cycle.
"""

from __future__ import annotations

import streamlit as st

from harness_remote import RemoteError, RemoteViewRecord
from ui_common import APP_TITLE, notify, _display, upload_marker
from remote_workflow import (
    maybe_remote_open, request_reload, _remote_error_text,
    save_remote_drafts, discard_remote_drafts, resolve_conflict, resolve_pending,
    sync_drafts_from_widgets, unsaved_count, conflicted_keys, conflict_fields,
    edited_fields, cell_seed, _drafts_for, row_location,
    remote_text_widget_key, remote_comment_widget_key,
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
    Discard live in the sidebar and act on every draft, rendered or not.
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
