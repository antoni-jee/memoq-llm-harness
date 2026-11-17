"""Remote-mode UI: the read-only sidebar, grid and row.

Presentation only. The sidebar offers Open/Reload through the workflow module;
the grid shows the merged segments with presentation-only batch navigation. No
editable field, no Process/Apply/Rerun/Analyse/Export, no tag injection.

It depends on ``ui_common`` (the shared UI helpers) and ``remote_workflow``
(the Open/Reload orchestration it calls), but never on ``app``. The
``remote_ui -> remote_workflow`` edge is one-way, so there is no import cycle.
"""

from __future__ import annotations

import streamlit as st

from harness_remote import RemoteError, RemoteViewRecord
from ui_common import APP_TITLE, notify, _display
from remote_workflow import maybe_remote_open, remote_reload, _remote_error_text


def render_remote_sidebar() -> None:
    """Remote-mode sidebar: read-only Open/Reload, no local controls.

    This is an *initial read-only integration*: it opens a document through the
    document service, binds the saved workspace and shows the segments. Editing,
    processing, applying, analysis and export are not wired in this slice, so
    no widget that writes to a segment exists here.
    """
    ss = st.session_state
    with st.sidebar:
        st.title(APP_TITLE)
        st.caption("Remote mode - initial read-only integration")

        st.header("1. Connection")
        st.info("Backend: remote (document / workspace / processing services).")

        st.header("2. Document")
        upload = st.file_uploader("memoQ bilingual XLIFF",
                                 type=["mqxliff", "xliff", "xlf", "xml"])
        if upload is None:
            # Emptying the uploader re-arms it, mirroring the local sidebar.
            ss.remote_open_marker = None
        else:
            data = upload.getvalue()
            try:
                if maybe_remote_open(data, upload.name):
                    st.rerun()
            except RemoteError as exc:
                # The marker is already recorded, so a bad file is not retried on
                # every rerun. A failed Open leaves the live view untouched
                # (remote_open only assigns on full success); just report it safely.
                notify("error", _remote_error_text(exc))

        view = ss.get("remote_view")
        if view is not None:
            counts = view.counts()
            col_a, col_b = st.columns(2)
            col_a.metric("Segments", counts.get("active", 0))
            col_b.metric("Batches", view.batch_count)
            if st.button("Reload saved work", width='stretch',
                        help="Re-read the saved workspace and refresh the "
                             "segments. Checks the document identity has not "
                             "changed."):
                try:
                    remote_reload()
                except RemoteError as exc:
                    # A malformed or unavailable read keeps the live view
                    # (remote_reload reassigns it only on full success); report
                    # it safely, mirroring the Open path, instead of raising into
                    # the run.
                    notify("error", _remote_error_text(exc))
                st.rerun()
        else:
            st.info("Upload a `.mqxliff` file to open its saved work "
                    "read-only.")


def render_remote_grid() -> None:
    """Read-only segment grid for remote mode.

    Presentation only: it shows the merged segments - source, the memoQ
    original target, the model's proposal and the human's saved text - with
    presentation-only batch navigation. No editable field, no
    Process/Apply/Rerun/Analyze/Export, no tag injection.
    """
    ss = st.session_state
    view = ss.get("remote_view")
    if view is None:
        st.info("Upload a `.mqxliff` file in the sidebar to open its saved "
                "work read-only.")
        return
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
    # a save, so nothing the operator sees can mutate a segment.
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
            format_func=lambda p: "%d-%d" % (p * page_size + 1,
                                             min((p + 1) * page_size, len(records))),
        )
    ss.remote_page = page
    visible = records[page * page_size: (page + 1) * page_size]

    st.divider()

    header = st.columns([1, 3, 3, 4, 3])
    for col, label in zip(header, ("ID", "Source", "memoQ target",
                                   "Model proposal", "Your saved text")):
        col.markdown("**%s**" % label)

    for record in visible:
        _render_remote_row(record)


def _render_remote_row(record: RemoteViewRecord) -> None:
    """One read-only segment row: four text columns, no editable field.

    The memoQ original target, the model's proposal and the human's saved text
    are kept in their own columns so the operator can see which side produced
    which text.
    """
    cols = st.columns([1, 3, 3, 4, 3])
    with cols[0]:
        st.markdown("`%s`" % record.segment_id)
        if record.edited_by_human:
            st.caption("edited")
        if record.status in ("processed", "applied"):
            st.caption(record.status)
        if record.tag_report:
            st.caption(":red[tags]")
    cols[1].markdown(_display(record.source_text) or ":grey[(empty)]")
    cols[2].markdown(_display(record.mt_target) or ":grey[(empty)]")
    cols[3].markdown(_display(record.llm_suggestion) or ":grey[(none)]")
    cols[4].markdown(_display(record.user_final) or ":grey[(none)]")
