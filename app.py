"""memoQ LLM Harness - Streamlit front end.

Run with:  streamlit run app.py

This module is presentation only. Parsing, tag handling, state, prompting and
inference all live in the ``memoq_harness`` package so they stay testable
without a browser in the loop.

Workflow the UI is built around:

1. Load a ``.mqxliff`` exported from memoQ. Confirmed and locked rows are
   filtered out; only rows that still need work are loaded.
2. The remaining segments are sliced into batches (100 by default).
3. "Process Current Batch" sends the batch to the configured endpoint.
4. The operator edits the suggestions in the grid and adds notes.
5. "Apply Changes & Calculate Diffs" turns every human edit into a correction
   record, which is injected into the next batch's system prompt.
6. "Export Valid MQXLIFF" writes the file back for re-import into memoQ.
"""

from __future__ import annotations

import hashlib
import os
import traceback
from typing import Dict, List, Optional

import streamlit as st

from memoq_harness import (
    diffing,
    llm,
    mqxliff,
    prompt_templates,
    prompts,
    state as state_mod,
    xmlcompat,
)

APP_TITLE = "memoQ LLM Harness"
WORKDIR = os.path.dirname(os.path.abspath(__file__))
EXPORT_DIR = os.path.join(WORKDIR, "exports")
CONFIG_DIR = os.path.join(WORKDIR, "config")
STATE_PATH = os.path.join(WORKDIR, "state.json")
# Termbases tried at first start, in order. The operator's real memoQ export
# dropped in the project root wins over the shipped sample.
TERMBASE_CANDIDATES = (
    os.path.join(WORKDIR, "MQL term base.csv"),
    os.path.join(CONFIG_DIR, "termbase.csv"),
)

PAGE_SIZES = {"25 rows": 25, "50 rows": 50, "100 rows": 100, "Whole batch": 0}


# --------------------------------------------------------------- bootstrap


def bootstrap() -> None:
    """Create the session objects exactly once per browser session."""
    ss = st.session_state
    if ss.get("_ready"):
        return

    ss.project = state_mod.ProjectState.load(STATE_PATH)
    if not ss.project.termbase:
        for candidate in TERMBASE_CANDIDATES:
            if not os.path.exists(candidate):
                continue
            try:
                with open(candidate, "rb") as handle:
                    ss.project.load_termbase_csv(handle.read())
            except OSError:
                continue
            if ss.project.termbase:
                ss.termbase_source = os.path.basename(candidate)
                break

    preset = llm.PRESETS[llm.DEFAULT_PRESET]
    ss.config = llm.ClientConfig(
        preset=llm.DEFAULT_PRESET,
        base_url=preset.base_url,
        api_key="",
        model=preset.default_model,
    )
    ss.document = None
    ss.doc_bytes = None
    ss.doc_name = ""
    ss.doc_path = ""
    ss.upload_marker = None
    ss.confirm_reset = False
    ss.seg_filter = mqxliff.SegmentFilter()
    ss.source_lang = ""
    ss.target_lang = ""
    ss.rules_collapsed = False
    ss.show_diff = True
    ss.last_prompt = ""
    ss.last_stage = ""
    ss.page = 0
    ss.page_size = 25
    ss.notice = None
    ss.last_export = ""
    ss.setdefault("termbase_source", "")
    ss.model_list: List[str] = []
    ss._ready = True


def project() -> state_mod.ProjectState:
    return st.session_state.project


def document() -> Optional[mqxliff.MqxliffDocument]:
    return st.session_state.document


def notify(kind: str, message: str) -> None:
    """Queue a message that survives the ``st.rerun`` following an action."""
    st.session_state.notice = (kind, message)


def flush_notice() -> None:
    notice = st.session_state.pop("notice", None)
    if not notice:
        return
    kind, message = notice
    {"success": st.success, "warning": st.warning, "error": st.error}.get(kind, st.info)(message)


# ------------------------------------------------------------ document I/O


def upload_marker(name: str, data: bytes) -> str:
    """Identity of one uploaded file *version*: its name plus its exact bytes.

    Deliberately not the source fingerprint: a target-only change keeps the
    fingerprint but is still a new file that must be loaded. Two different
    files sharing a basename get different markers.
    """
    digest = hashlib.sha256()
    digest.update(name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(data)
    return digest.hexdigest()


def upload_needs_load(marker: Optional[str], last_marker: Optional[str]) -> bool:
    """Load only when the uploader holds a file version not yet loaded.

    Streamlit keeps returning the same upload on every rerun, so reloading it
    each time would discard unsaved widget edits.
    """
    return marker is not None and marker != last_marker


def _clear_grid_widgets() -> None:
    """Drop grid widget state so rows cannot inherit another document's text."""
    for key in [k for k in st.session_state
                if k.startswith(("txt_", "note_", "txtrev_"))]:
        del st.session_state[key]


def _retained_work_label(proj: state_mod.ProjectState) -> str:
    """Name of active work that a switch would leave retained but detached."""
    if proj.document_id == state_mod.LEGACY_DOCUMENT_ID:
        return proj.source_file or "an earlier session"
    if proj.document_id is None and (proj.segments or proj.corrections
                                     or proj.analysis or proj.global_directive):
        return proj.source_file or "an earlier session"
    return ""


def load_document(raw: bytes, filename: str, source_path: str = "") -> None:
    """Parse a file and make it the active document of the project state.

    The document's identity is its source fingerprint, so the same document
    under another name or path reopens its own saved work, and a different
    document never inherits it. ``source_path`` is the disk path, or "" for a
    browser upload. Parsing and fingerprinting happen before any state change,
    so a bad file neither switches nor saves anything.
    """
    doc = mqxliff.MqxliffDocument.from_bytes(raw, filename)
    doc.parse_segments(st.session_state.seg_filter, tag_source=project().tag_source)
    document_id = doc.source_fingerprint()

    proj = project()
    retained = ""
    if proj.document_id != document_id:
        retained = _retained_work_label(proj)
    proj.switch_document(document_id, filename, source_path)
    # Pass the full-document key set separately from the filtered selection, so
    # a row hidden by the filter keeps its saved work instead of being pruned.
    # bind_segments() also sets source_file/source_path, so pass the same path.
    proj.bind_segments(
        doc.translatable_segments(),
        filename,
        source_path,
        present_keys={s.key for s in doc.segments},
    )
    save_error = ""
    try:
        proj.save()
    except OSError as exc:  # includes StateBackupError: nothing was written
        save_error = str(exc)

    _clear_grid_widgets()
    st.session_state.document = doc
    st.session_state.doc_bytes = raw
    st.session_state.doc_name = filename
    st.session_state.doc_path = source_path
    st.session_state.page = 0
    st.session_state.confirm_reset = False

    header = _find_file_header(doc)
    if header is not None:
        st.session_state.source_lang = header.get("source-language", "") or ""
        st.session_state.target_lang = header.get("target-language", "") or ""

    stats = doc.stats()
    message = (
        "Loaded **%s** - %d segments, %d to work on "
        "(%d confirmed, %d locked, %d empty skipped). %d batches of %d."
        % (
            filename,
            stats["total"],
            stats["translatable"],
            stats["confirmed"],
            stats["locked"],
            stats["empty_source"],
            proj.batch_count,
            proj.batch_size,
        )
    )
    kind = "success"
    if retained:
        kind = "warning"
        message += (
            "\n\nEarlier work for **%s** (not tied to a specific document) is "
            "retained in state.json but was NOT attached to this document. "
            "Reopening it needs a recovery flow that is not available yet."
            % retained)
    if save_error:
        kind = "error"
        message += "\n\nThe project state was NOT saved: %s" % save_error
    notify(kind, message)


def _find_file_header(doc: mqxliff.MqxliffDocument):
    for elem in doc.root.iter():
        if xmlcompat.localname(elem.tag) == "file":
            return elem
    return None


def reset_active_document() -> None:
    """Clear the active document's saved work and unload it from the editor.

    Other documents, retained earlier work and global settings are kept. The
    document is unloaded (not rebound) so the next rerun cannot reload it; the
    upload marker is left as is, so a file still sitting in the uploader is not
    reloaded either. Reopening is a deliberate Load or re-upload.
    """
    proj = project()
    name = st.session_state.doc_name or proj.source_file
    st.session_state.confirm_reset = False
    try:
        proj.reset_active_document_and_save()
    except OSError as exc:
        # Nothing was cleared: the state restored itself, the editor stays.
        notify("error", "Reset FAILED; nothing was cleared: %s" % exc)
        return
    notify("success", "Cleared the saved work for **%s**. Other documents "
                      "and global settings are unchanged." % name)
    _clear_grid_widgets()
    st.session_state.document = None
    st.session_state.doc_bytes = None
    st.session_state.doc_name = ""
    st.session_state.doc_path = ""
    st.session_state.page = 0
    st.session_state.confirm_reset = False


def render_reset_controls(proj: state_mod.ProjectState) -> None:
    """Two-step reset of the active document only."""
    if document() is None:
        st.session_state.confirm_reset = False
    if not st.session_state.get("confirm_reset"):
        if st.button("Reset active document", width='stretch',
                     disabled=document() is None,
                     help="Clear the saved work of the open document only. "
                          "Asks for confirmation."):
            st.session_state.confirm_reset = True
            st.rerun()
        return
    st.warning("Clear all saved work (translations, comments, corrections, "
               "analysis) for **%s**? Other documents and global settings are "
               "kept." % (st.session_state.doc_name or proj.source_file))
    confirm_col, cancel_col = st.columns(2)
    if confirm_col.button("Confirm reset", width='stretch', type="primary"):
        reset_active_document()
        st.rerun()
    if cancel_col.button("Cancel", width='stretch'):
        st.session_state.confirm_reset = False
        st.rerun()


def reapply_filter() -> None:
    if st.session_state.doc_bytes:
        load_document(st.session_state.doc_bytes, st.session_state.doc_name,
                      st.session_state.get("doc_path", ""))


def segments_by_key() -> Dict[str, mqxliff.Segment]:
    doc = document()
    if doc is None:
        return {}
    return {s.key: s for s in doc.translatable_segments()}


# ------------------------------------------------------------- grid actions


def text_key_for(record_key: str) -> str:
    """Widget key for a row's editing box, carrying a revision counter.

    Streamlit forbids assigning to a widget's ``session_state`` key once the
    widget has been created in the same run, so the supported way to change a
    box's contents from a button handler is to make it a *new* widget. Bumping
    the revision does that; the fresh widget seeds itself from the record.
    """
    rev = st.session_state.get("txtrev_%s" % record_key, 0)
    return "txt_%s_%d" % (record_key, rev)


def bump_text_widget(record_key: str) -> None:
    st.session_state["txtrev_%s" % record_key] = (
        st.session_state.get("txtrev_%s" % record_key, 0) + 1)


def capture_grid(batch_index: int) -> None:
    """Copy the grid's widget values into the project state."""
    proj = project()
    edits, comments = {}, {}
    for record in proj.batch_records(batch_index):
        text_key = text_key_for(record.key)
        note_key = "note_%s" % record.key
        if text_key in st.session_state:
            edits[record.key] = st.session_state[text_key]
        if note_key in st.session_state:
            comments[record.key] = st.session_state[note_key]
    proj.capture_edits(edits, comments)


def sync_widgets(batch_index: int) -> None:
    """Push state values back into the widgets after a programmatic change.

    Safe because it runs from a handler *before* this run's row widgets are
    created; anything later must go through :func:`bump_text_widget`.
    """
    for record in project().batch_records(batch_index):
        st.session_state[text_key_for(record.key)] = record.user_final
        st.session_state["note_%s" % record.key] = record.user_comment


def build_prompt_for(records, batch_index: int) -> str:
    """Assemble the system prompt for a set of records in the current batch.

    Shared by the whole-batch run and the single-row retry so a retried segment
    is judged against exactly the same rules, termbase slice, corrections and
    operator directive as its batch - only the segment list differs.
    """
    proj = project()
    # Only the terms that actually occur are worth the tokens.
    terms = proj.relevant_terms([r.source_text for r in records])
    corrections = proj.feedback_block(upto_batch=batch_index)
    stage_key = proj.active_stage(batch_index)
    system_prompt = prompts.build_system_prompt(
        hard_rules_text=proj.hard_rules_text,
        termbase=terms,
        vibe=proj.vibe,
        corrections_block=corrections,
        source_lang=st.session_state.source_lang,
        target_lang=st.session_state.target_lang,
        extra_instructions=proj.global_directive,
        template=proj.template_for(stage_key),
        template_values={
            "{{HARD_RULES}}": proj.hard_rules_text,
            "{{VIBE}}": proj.vibe,
            "{{TERMBASE}}": prompts.render_termbase(terms),
            "{{CORRECTIONS}}": corrections,
            "{{SOURCE_LANG}}": st.session_state.source_lang,
            "{{TARGET_LANG}}": st.session_state.target_lang,
            "{{BATCH_NUMBER}}": str(batch_index + 1),
            "{{BATCH_COUNT}}": str(proj.batch_count),
            "{{SEGMENT_COUNT}}": str(len(records)),
        },
    )
    st.session_state.last_prompt = system_prompt
    st.session_state.last_stage = stage_key
    return system_prompt


def tag_report_for(key: str, text: str) -> str:
    """Tag-integrity summary for one suggestion, or "" when it is clean."""
    segment = segments_by_key().get(key)
    if segment is None:
        return ""
    _, report = mqxliff.tagmod.restore(
        text, segment.lookup, expected=segment.expected_numbers
    )
    return "" if report.ok else report.summary()


def request_retry(key: str) -> None:
    """Retry button callback: queue the row; render_grid performs the call."""
    st.session_state.pending_retry = key


def retry_segment(key: str) -> None:
    """Re-run one row without touching the rest of the batch."""
    proj = project()
    batch_index = proj.current_batch
    capture_grid(batch_index)
    record = proj.segments.get(key)
    if record is None:
        notify("error", "That segment is no longer in the project.")
        return

    system_prompt = build_prompt_for([record], batch_index)
    try:
        client = llm.LLMClient(st.session_state.config)
        with st.spinner("Retrying segment %s..." % record.segment_id):
            text, raw = client.translate_one(record, system_prompt)
    except llm.LLMError as exc:
        notify("error", "Retry failed: %s" % exc)
        return
    except Exception as exc:  # noqa: BLE001 - one row must not kill the session
        notify("error", "Unexpected error on retry: %s" % exc)
        st.session_state.traceback = traceback.format_exc()
        return

    st.session_state.last_raw = raw
    if not text or not text.strip():
        notify("warning",
               "Segment %s came back empty again; its previous text is kept."
               % record.segment_id)
        return

    # Patch this row only. record_suggestions() would also be safe, but going
    # through it for one key makes the single-row intent explicit.
    proj.record_suggestions({key: text}, {key: tag_report_for(key, text)})
    proj.save()
    bump_text_widget(key)
    notify("success", "Segment %s re-translated." % record.segment_id)


def process_batch(batch_index: int) -> None:
    """Send the current batch to the model and store its suggestions."""
    proj = project()
    records = proj.batch_records(batch_index)
    if not records:
        notify("warning", "This batch is empty.")
        return

    system_prompt = build_prompt_for(records, batch_index)

    try:
        client = llm.LLMClient(st.session_state.config)
    except llm.LLMError as exc:
        notify("error", str(exc))
        return

    progress = st.progress(0.0, text="Contacting %s..." % st.session_state.config.model)

    def on_progress(done: int, total: int, label: str) -> None:
        progress.progress(min(1.0, done / max(1, total)), text=label)

    try:
        result = client.translate_batch(records, system_prompt, on_progress)
    except llm.LLMError as exc:
        progress.empty()
        notify("error", "Request failed: %s" % exc)
        return
    except Exception as exc:  # noqa: BLE001 - never lose the session to a stray error
        progress.empty()
        notify("error", "Unexpected error: %s" % exc)
        st.session_state.traceback = traceback.format_exc()
        return
    progress.empty()

    # Validate tag integrity before the text ever reaches the grid.
    tag_reports: Dict[str, str] = {}
    for key, text in result.suggestions.items():
        summary = tag_report_for(key, text)
        if summary:
            tag_reports[key] = summary

    count = proj.record_suggestions(result.suggestions, tag_reports)
    st.session_state.last_raw = result.raw_text()

    # Fold in any rule/vibe the model proposed, so the next batch inherits it.
    merged: List[str] = []
    if proj.auto_merge_proposals:
        merged = proj.merge_proposals(
            result.proposed_rules, result.proposed_vibe, batch_index)
        if merged:
            # The rules block is a plain text_area keyed by value, so it picks
            # the new text up on the next render without a key reset.
            st.session_state.rules_dirty = True

    proj.save()
    sync_widgets(batch_index)

    message = "Received %d of %d segments in %.1fs (%d requests)." % (
        count, len(records), result.elapsed, result.requests,
    )
    if tag_reports:
        message += " %d row(s) have tag warnings." % len(tag_reports)
    if result.missing:
        message += " %d segment(s) came back empty and kept their MT." % len(result.missing)
    if merged:
        message += " Merged %d model-proposed rule/vibe change(s)." % len(merged)
    if result.errors:
        message += "\n\nErrors: " + "; ".join(result.errors[:3])
    notify("warning" if (result.errors or result.missing) else "success", message)


def apply_and_diff(batch_index: int) -> int:
    """Capture edits, turn them into corrections, persist."""
    capture_grid(batch_index)
    proj = project()
    corrections = proj.calculate_diffs(batch_index)
    proj.save()
    return len(corrections)


def run_analysis() -> None:
    proj = project()
    if not proj.corrections:
        notify("warning", "No corrections recorded yet - apply a batch first.")
        return
    # Give the analyser the expected tag set per row so it can actually check
    # the human's edits rather than assume they are correct.
    by_id = {s.unit_id: s for s in (document().translatable_segments()
                                    if document() else [])}
    expected = {
        c.segment_id: by_id[c.segment_id].expected_numbers
        for c in proj.corrections if c.segment_id in by_id
    }

    try:
        client = llm.LLMClient(st.session_state.config)
        with st.spinner("Analysing your corrections..."):
            text = client.analyze_corrections(
                proj.corrections,
                hard_rules_text=proj.hard_rules_text,
                expected_tags=expected,
            )
    except llm.LLMError as exc:
        notify("error", "Analysis failed: %s" % exc)
        return
    proj.analysis[str(proj.current_batch)] = text
    proj.save()
    warnings, _ = prompts.split_analysis(text)
    notify("warning" if warnings else "success",
           "Analysis updated - the model flagged issues in your edits."
           if warnings else "Analysis updated.")


def export_document(mark_confirmed: bool, only_changed: bool) -> None:
    doc = document()
    proj = project()
    if doc is None:
        notify("error", "Load a file first.")
        return

    capture_grid(proj.current_batch)
    by_key = segments_by_key()
    written, warnings = 0, []

    for key in proj.order:
        record = proj.segments.get(key)
        segment = by_key.get(key)
        if record is None or segment is None:
            continue
        text = record.new_text() if only_changed else record.effective_text()
        if not text.strip():
            continue
        report = doc.apply_translation(segment, text, mark_confirmed=mark_confirmed)
        written += 1
        if not report.ok:
            warnings.append("%s: %s" % (record.segment_id, report.summary()))

    os.makedirs(EXPORT_DIR, exist_ok=True)
    out_path = os.path.join(EXPORT_DIR, mqxliff.export_filename(st.session_state.doc_name))
    doc.save(out_path)

    # Re-parse what we just wrote: an export memoQ cannot open is worse than no
    # export, and this catches it here rather than on the operator's import.
    try:
        with open(out_path, "rb") as handle:
            mqxliff.MqxliffDocument(handle.read(), "verify").parse_segments()
    except Exception as exc:  # noqa: BLE001
        notify("error", "Export written but failed re-parse: %s" % exc)
        return

    proj.save()
    st.session_state.last_export = out_path
    message = "Exported %d segments to `%s` (verified parseable)." % (written, out_path)
    if warnings:
        message += "\n\nTag warnings on %d row(s): %s" % (
            len(warnings), "; ".join(warnings[:5]),
        )
    notify("warning" if warnings else "success", message)


# ----------------------------------------------------------------- sidebar


def render_sidebar() -> None:
    proj = project()
    config = st.session_state.config

    with st.sidebar:
        st.title(APP_TITLE)
        st.caption("XML backend: %s" % xmlcompat.backend_name())

        # -- Connection ----------------------------------------------------
        st.header("1. Engine")
        names = list(llm.PRESETS)
        chosen = st.selectbox(
            "Environment preset",
            names,
            index=names.index(config.preset) if config.preset in names else 0,
            key="preset_select",
        )
        preset = llm.PRESETS[chosen]
        if chosen != config.preset:
            # Switching provider replaces the endpoint defaults and drops a
            # typed key, so one provider's key is never sent to another - even
            # between two OpenAI-compatible presets. Bumping the revision gives
            # the engine widgets fresh keys, so what they display matches the
            # new config instead of the previous preset's values.
            config.preset = chosen
            config.base_url = preset.base_url
            config.model = preset.default_model
            config.backend = preset.backend
            config.api_key = ""
            st.session_state.model_list = []
            st.session_state.engine_rev = st.session_state.get("engine_rev", 0) + 1
            st.rerun()
        config.backend = preset.backend
        engine_rev = st.session_state.get("engine_rev", 0)

        st.caption(preset.hint)
        if preset.backend == llm.BACKEND_ANTHROPIC and llm.anthropic is None:
            st.error("The `anthropic` package is missing. Run: `pip install anthropic`")

        config.base_url = st.text_input(
            "Base URL", value=config.base_url, key="base_url_%d" % engine_rev)
        key_label = (
            "Anthropic API key" if preset.backend == llm.BACKEND_ANTHROPIC
            else "API key" + ("" if preset.requires_key else " (optional)")
        )
        config.api_key = st.text_input(
            key_label,
            value=config.api_key,
            type="password",
            key="api_key_%d" % engine_rev,
            help="Held in this browser session only - never written to state.json. "
                 "Cleared when you switch provider preset.",
        )

        model_options = list(st.session_state.model_list or preset.model_suggestions)
        if config.model and config.model not in model_options:
            model_options.insert(0, config.model)
        config.model = st.selectbox(
            "Model",
            model_options,
            index=model_options.index(config.model) if config.model in model_options else 0,
            accept_new_options=True,
            key="model_%d" % engine_rev,
            help="Type any model name the endpoint serves.",
        )

        col_a, col_b = st.columns(2)
        if col_a.button("Test", width='stretch'):
            # One bounded request on a private copy of the config; see
            # llm.probe_model. Never touches document work or saved state.
            try:
                result = llm.probe_model(config)
                notify("success" if result.ok else "warning", result.message())
            except llm.LLMError as exc:
                notify("error", "Model test failed: %s" % exc)
            st.rerun()
        if col_b.button("List models", width='stretch'):
            try:
                st.session_state.model_list = llm.LLMClient(config).list_models()
                notify("success", "Found %d models." % len(st.session_state.model_list))
            except llm.LLMError as exc:
                notify("error", str(exc))
            st.rerun()
        # What Test and translation will actually use - sanitized, no secrets.
        st.caption(llm.connection_summary(config))

        with st.expander("Inference settings"):
            anthropic_backend = preset.backend == llm.BACKEND_ANTHROPIC
            config.temperature = st.slider(
                "Temperature", 0.0, 1.5, config.temperature, 0.05,
                help=(
                    "Not sent to the Anthropic Messages API, which takes no "
                    "temperature parameter - that endpoint uses its own default. "
                    "Applies to every other preset."
                    if anthropic_backend else
                    "Sampling temperature for this endpoint. Low values keep "
                    "terminology stable across a batch."
                ),
            )
            if anthropic_backend:
                st.caption(":grey[Temperature is ignored by the Anthropic preset.]")
            config.chunk_size = st.number_input(
                "Segments per request", 1, 100, config.chunk_size,
                help="A 100-segment batch is split into requests of this size. "
                     "Smaller is slower but far more reliable on local models.",
            )
            config.max_tokens = st.number_input(
                "Max output tokens", 0, 32000, config.max_tokens,
                help="0 lets the server decide (Anthropic always needs a "
                     "positive value, so a default is substituted there).",
            )
            config.timeout = st.number_input(
                "Timeout (seconds)", 10, 3600, int(config.timeout),
                help="Large local models on first load can take minutes.",
            )

        # -- Document ------------------------------------------------------
        st.header("2. Document")
        upload = st.file_uploader("memoQ bilingual XLIFF", type=["mqxliff", "xliff", "xlf", "xml"])
        if upload is None:
            # Emptying the uploader re-arms it, so the same file can be
            # uploaded again deliberately (e.g. after a reset).
            st.session_state.upload_marker = None
        else:
            data = upload.getvalue()
            marker = upload_marker(upload.name, data)
            if upload_needs_load(marker, st.session_state.get("upload_marker")):
                # Recorded before loading, so a failing file is not retried on
                # every rerun; re-uploading a fixed file loads it.
                st.session_state.upload_marker = marker
                try:
                    load_document(data, upload.name, "")
                    st.rerun()
                except (mqxliff.MqxliffError, OSError) as exc:
                    st.error(str(exc))

        # Typing a path beats the upload dialog when the file is already on
        # this machine, which for a local VM tool is the normal case.
        path_col, btn_col = st.columns([3, 1])
        disk_path = path_col.text_input(
            "...or load from a path", key="disk_path",
            placeholder=r"C:\projects\handbook.mqxliff", label_visibility="collapsed",
        )
        if btn_col.button("Load", width='stretch', disabled=not disk_path.strip()):
            candidate = os.path.expanduser(disk_path.strip().strip('"'))
            if not os.path.isfile(candidate):
                notify("error", "No file at `%s`." % candidate)
            else:
                try:
                    with open(candidate, "rb") as handle:
                        raw = handle.read()
                    load_document(raw, os.path.basename(candidate),
                                  os.path.abspath(candidate))
                except (OSError, mqxliff.MqxliffError) as exc:
                    notify("error", str(exc))
            st.rerun()

        if document() is not None:
            stats = document().stats()
            col_a, col_b = st.columns(2)
            col_a.metric("To work on", stats["translatable"])
            col_b.metric("Skipped", stats["total"] - stats["translatable"])

        with st.expander("Segment filter"):
            seg_filter = st.session_state.seg_filter
            changed = False
            for label, attr, help_text in (
                ("Skip confirmed rows", "skip_confirmed", 'confirmed="true" or a confirmed mq:status'),
                ("Skip locked rows", "skip_locked", 'locked="true"'),
                ("Trust mq:status", "use_status_attribute", "Treat mq:status=Confirmed as confirmed"),
                ('Trust target state', "use_target_state", 'Treat state="final"/"signed-off" as confirmed'),
                ("Skip empty source", "require_nonempty_source", "Rows with no source text"),
            ):
                value = st.checkbox(label, value=getattr(seg_filter, attr), help=help_text)
                if value != getattr(seg_filter, attr):
                    setattr(seg_filter, attr, value)
                    changed = True
            if changed and st.session_state.doc_bytes:
                reapply_filter()
                st.rerun()

        with st.expander("Inline tags"):
            options = [mqxliff.tagmod.TAG_SOURCE_TARGET, mqxliff.tagmod.TAG_SOURCE_SOURCE]
            labels = {
                mqxliff.tagmod.TAG_SOURCE_TARGET: "Current target (memoQ MT)",
                mqxliff.tagmod.TAG_SOURCE_SOURCE: "Source",
            }
            current = proj.tag_source if proj.tag_source in options else options[0]
            picked = st.radio(
                "Take tag XML from",
                options,
                index=options.index(current),
                format_func=lambda o: labels[o],
                help="memoQ's MT localises link targets and other tag attributes. "
                     "Taking tags from the target preserves them; taking them from "
                     "the source reverts them to the English original.",
            )
            if picked != proj.tag_source:
                proj.tag_source = picked
                proj.save()
                if st.session_state.doc_bytes:
                    reapply_filter()
                st.rerun()
            st.caption(
                "Rows with no target tags fall back to the source automatically."
            )

        # -- Batching ------------------------------------------------------
        st.header("3. Batching")
        new_size = st.number_input(
            "Batch size", 1, 1000, proj.batch_size,
            help="Segments per processing batch and per feedback cycle.",
        )
        if new_size != proj.batch_size:
            proj.rebatch(int(new_size))
            proj.save()
            st.rerun()

        label = st.selectbox("Rows shown at once", list(PAGE_SIZES), index=0,
                             help="Display paging only - the batch stays whole.")
        st.session_state.page_size = PAGE_SIZES[label]

        # -- Export --------------------------------------------------------
        st.header("4. Export")
        mark_confirmed = st.checkbox(
            "Mark exported rows as Confirmed", value=False,
            help="Sets mq:status=Confirmed and target state=translated on rows written.",
        )
        only_changed = st.checkbox(
            "Only rows the harness produced", value=True,
            help="Leaves untouched rows exactly as memoQ wrote them.",
        )
        if st.button("Export Valid MQXLIFF", type="primary", width='stretch',
                     disabled=document() is None):
            export_document(mark_confirmed, only_changed)
            st.rerun()

        if st.session_state.last_export and os.path.exists(st.session_state.last_export):
            with open(st.session_state.last_export, "rb") as handle:
                st.download_button(
                    "Download last export",
                    handle.read(),
                    file_name=os.path.basename(st.session_state.last_export),
                    mime="application/xml",
                    width='stretch',
                )

        with st.expander("Project"):
            st.caption("state.json: `%s`" % STATE_PATH)
            totals = proj.totals()
            st.write(
                {"segments": totals["segments"], "batches": totals["batches"],
                 "applied": totals["applied"], "corrections": totals["corrections"]}
            )
            if state_mod.LEGACY_DOCUMENT_ID in proj.document_ids and \
                    proj.document_id != state_mod.LEGACY_DOCUMENT_ID:
                st.caption(":orange[Earlier work saved before per-document "
                           "storage is retained in state.json; reopening it "
                           "needs a recovery flow that is not available yet.]")
            render_reset_controls(proj)


# -------------------------------------------------------------- left panel


def render_prompt_management(proj: state_mod.ProjectState) -> None:
    """Editable Init/MT1/MT2/MT3 stage templates."""
    st.subheader("Prompt Management")
    st.caption("The Init / MT1 / MT2 / MT3 workflow, editable and persisted.")

    stages = prompt_templates.STAGES
    keys = ["auto"] + [s.key for s in stages]
    labels = {"auto": "Auto (per batch)"}
    labels.update({s.key: "%s - %s" % (s.label, s.description) for s in stages})

    current = proj.prompt_stage if proj.prompt_stage in keys else "auto"
    picked = st.selectbox(
        "Stage for the next batch", keys, index=keys.index(current),
        format_func=lambda k: labels[k],
        help="Auto sends MT1 for the first batch, MT3 for the last and MT2 in "
             "between. Pin a stage to override.",
    )
    if picked != proj.prompt_stage:
        proj.prompt_stage = picked
        proj.save()
        st.rerun()

    if picked == "auto" and proj.batch_count:
        resolved = proj.active_stage(proj.current_batch)
        st.caption("Batch %d of %d resolves to **%s**."
                   % (proj.current_batch + 1, proj.batch_count, resolved.upper()))

    with st.expander("Edit templates"):
        st.caption(
            "Placeholders substituted at send time: %s. Anything else, including "
            "your own bracketed notes, is passed through untouched."
            % ", ".join("`%s`" % p for p in prompts.TEMPLATE_PLACEHOLDERS)
        )
        tabs = st.tabs([s.label for s in stages])
        for tab, stage in zip(tabs, stages):
            with tab:
                st.caption(stage.description)
                # The widget key carries a revision so Restore can reset the
                # box: Streamlit forbids assigning to a widget's session_state
                # key once it has been instantiated in the same run, so the
                # supported way to reset one is to make it a new widget.
                rev = st.session_state.get("tplrev_%s" % stage.key, 0)
                key = "tpl_%s_%d" % (stage.key, rev)
                st.session_state.setdefault(key, proj.template_for(stage.key))
                text = st.text_area(
                    stage.label, key=key, height=260, label_visibility="collapsed",
                )
                if text != proj.template_for(stage.key):
                    proj.set_template(stage.key, text)
                    proj.save()
                edited = text != stage.template
                cols = st.columns([2, 1])
                cols[0].caption(
                    "%s chars%s" % (f"{len(text):,}", " - edited" if edited else "")
                )
                if cols[1].button("Restore", key="rst_%s" % stage.key,
                                  width='stretch', disabled=not edited):
                    proj.reset_template(stage.key)
                    proj.save()
                    st.session_state["tplrev_%s" % stage.key] = rev + 1
                    st.rerun()

    if st.session_state.get("last_prompt"):
        with st.expander("Last prompt actually sent (%s)"
                         % (st.session_state.get("last_stage") or "?").upper()):
            st.code(st.session_state.last_prompt, language="text")


def render_left_panel() -> None:
    proj = project()

    # -- Section 1: the editorial rulebook, free prose, injected verbatim ----
    head, reset = st.columns([3, 1])
    head.subheader("Hard Rules")
    if reset.button("Restore", key="reset_rules", width='stretch',
                    help="Reset to the built-in default rulebook."):
        proj.reset_hard_rules()
        proj.save()
        st.rerun()
    st.caption("Editorial rulebook, sent verbatim. Overrides the vibe below.")

    rules = st.text_area(
        "Hard Rules", value=proj.hard_rules_text, height=320,
        label_visibility="collapsed",
        placeholder="Editorial rules the model must follow without exception.",
    )
    if rules != proj.hard_rules_text:
        proj.hard_rules_text = rules
        proj.save()
    st.caption("%s characters, ~%s tokens"
               % (f"{len(rules):,}", f"{prompts.estimate_tokens(rules):,}"))

    proj.auto_merge_proposals = st.checkbox(
        "Auto-merge rules the model proposes", value=proj.auto_merge_proposals,
        help="Appends model-proposed rules under a marked heading and updates "
             "the vibe, so the next batch inherits them. Every change is logged "
             "and can be reverted.",
    )
    if proj.rule_log:
        with st.expander("Automatic rule changes (%d)" % len(proj.rule_log)):
            for entry in reversed(proj.rule_log[-20:]):
                st.markdown("- `%s` batch %s **%s** - %s"
                            % (entry.get("at", ""), entry.get("batch", "?"),
                               entry.get("kind", ""), entry.get("detail", "")))
            if st.button("Revert automatic rules", key="revert_auto",
                         width='stretch'):
                proj.revert_auto_rules()
                proj.save()
                notify("success", "Automatically added rules removed.")
                st.rerun()

    st.divider()

    # -- Section 2: the article's stylistic direction ------------------------
    head, reset = st.columns([3, 1])
    head.subheader("Vibe / Soft Rules")
    if reset.button("Restore", key="reset_vibe", width='stretch',
                    help="Reset to the built-in default vibe."):
        proj.reset_vibe()
        proj.save()
        st.rerun()
    st.caption("Register, tone and per-article terminology.")

    vibe = st.text_area(
        "Vibe", value=proj.vibe, height=240, label_visibility="collapsed",
        placeholder="Stylistic direction and soft terminology for this article.",
    )
    if vibe != proj.vibe:
        proj.vibe = vibe
        proj.save()

    st.divider()

    render_prompt_management(proj)

    st.divider()

    # -- Section 3: the memoQ termbase, filtered per batch -------------------
    st.subheader("Termbase")
    st.caption("memoQ CSV export. Only the terms occurring in a batch are sent.")

    uploaded = st.file_uploader("Termbase CSV", type=["csv"], key="termbase_upload",
                                label_visibility="collapsed")
    if uploaded is not None and uploaded.name != st.session_state.get("_termbase_name"):
        report = proj.load_termbase_csv(uploaded.getvalue())
        st.session_state._termbase_name = uploaded.name
        st.session_state.termbase_source = uploaded.name
        proj.save()
        notify("warning" if report.incomplete_rows else "success",
               "Termbase: %s" % report.summary())
        st.rerun()

    if proj.termbase:
        origin = st.session_state.get("termbase_source") or ""
        st.caption("%s terms loaded%s."
                   % (f"{len(proj.termbase):,}", " from `%s`" % origin if origin else ""))
        with st.expander("Browse termbase"):
            needle = st.text_input("Filter", key="termbase_filter",
                                   placeholder="Type to filter terms")
            rows = [t for t in proj.termbase
                    if not needle.strip()
                    or needle.lower() in t.source.lower()
                    or needle.lower() in t.target.lower()]
            st.caption("%s of %s shown" % (f"{len(rows):,}", f"{len(proj.termbase):,}"))
            st.dataframe(
                [{"Source": t.source, "Target": t.target} for t in rows[:500]],
                width='stretch', hide_index=True, height=300,
            )
        st.download_button("Download termbase", proj.termbase_csv(),
                           file_name="termbase.csv", mime="text/csv",
                           width='stretch')
    else:
        st.info(
            "No termbase loaded. Upload the memoQ CSV export: row 0 is a header, "
            "column %d is the English term and column %d the Russian one."
            % (state_mod.MEMOQ_SOURCE_COL, state_mod.MEMOQ_TARGET_COL)
        )

    st.divider()

    st.subheader("AI Analysis of Your Corrections")
    if proj.corrections:
        st.caption("%d correction(s) recorded across %d batch(es)."
                   % (len(proj.corrections), len({c.batch_index for c in proj.corrections})))
    analysis = proj.analysis.get(str(proj.current_batch)) or (
        list(proj.analysis.values())[-1] if proj.analysis else ""
    )
    if analysis:
        warnings, brief = prompts.split_analysis(analysis)
        if warnings:
            # The analyser found something wrong with the *human's* edits.
            # Give it its own block so it cannot be skimmed past.
            st.error("**%s**\n\n%s" % (prompts.OPERATOR_WARNING_HEADING, warnings))
        st.markdown(brief or analysis)
    else:
        st.info("Apply a batch, then run the analysis to see what your edits imply.")

    if st.button("Analyse my corrections", width='stretch',
                 disabled=not proj.corrections):
        run_analysis()
        st.rerun()

    if proj.corrections:
        with st.expander("Feedback block sent to the model"):
            proj.detailed_feedback = st.checkbox(
                "Include source and rejected text", value=proj.detailed_feedback,
                help="Richer context teaches the pattern better; costs more tokens.",
            )
            block = proj.feedback_block(upto_batch=proj.current_batch)
            st.code(block or "(nothing yet - corrections start from the next batch)",
                    language="text")

    st.divider()

    # -- Everything the operator authored, as one plain-text record ---------
    if st.button("Export Rules to TXT", width='stretch'):
        try:
            path = proj.export_rules_txt(EXPORT_DIR)
        except OSError as exc:
            notify("error", "Could not write the rules export: %s" % exc)
        else:
            st.session_state.last_rules_export = path
            notify("success", "Rules written to `%s`." % path)
        st.rerun()

    last = st.session_state.get("last_rules_export")
    if last and os.path.exists(last):
        with open(last, "rb") as handle:
            st.download_button(
                "Download rules TXT", handle.read(),
                file_name=os.path.basename(last), mime="text/plain",
                width='stretch',
            )


# ------------------------------------------------------------- right panel


def render_grid() -> None:
    proj = project()
    # Consumed on every run, even one that returns early, so a queued Retry
    # can never fire later by surprise.
    pending_retry = st.session_state.pop("pending_retry", None)
    if document() is None:
        st.info("Upload a `.mqxliff` file in the sidebar to begin.")
        return
    if proj.batch_count == 0:
        st.warning("No translatable segments - every row is confirmed, locked or empty.")
        return

    batch_index = min(proj.current_batch, proj.batch_count - 1)
    proj.current_batch = batch_index
    records = proj.batch_records(batch_index)
    progress = proj.batch_progress(batch_index)

    head_left, head_right = st.columns([3, 2])
    with head_left:
        st.subheader("Batch %d of %d" % (batch_index + 1, proj.batch_count))
        st.caption(
            "%d segments - %d processed, %d applied, %d edited by you"
            % (progress["total"], progress["processed"], progress["applied"], progress["edited"])
        )
    with head_right:
        st.progress(progress["applied"] / max(1, progress["total"]))

    # -- Batch navigation. Leaving a batch runs its diff automatically, which
    # is the whole feedback loop: batch N's edits teach batch N+1.
    nav = st.columns([1, 1, 1, 2])
    target = batch_index

    if nav[0].button("Previous", width='stretch', disabled=batch_index == 0):
        target = batch_index - 1
    if nav[1].button("Next", width='stretch',
                     disabled=batch_index >= proj.batch_count - 1):
        target = batch_index + 1

    # The jump field's key encodes the batch, so moving batches creates a fresh
    # widget that defaults to the new number. Streamlit forbids assigning to a
    # widget's session_state key once it has been instantiated this run, so
    # re-keying is the supported way to reset it.
    jump = nav[2].number_input(
        "Go to", 1, proj.batch_count, value=batch_index + 1,
        key="batch_jump_%d" % batch_index, label_visibility="collapsed",
    )
    if target == batch_index and int(jump) - 1 != batch_index:
        target = int(jump) - 1

    if target != batch_index:
        count = apply_and_diff(batch_index)
        proj.current_batch = max(0, min(target, proj.batch_count - 1))
        proj.save()
        st.session_state.page = 0
        sync_widgets(proj.current_batch)
        notify("success", "Batch %d applied - %d correction(s) will guide the next batch."
               % (batch_index + 1, count))
        st.rerun()

    # -- Display paging within the batch -----------------------------------
    page_size = st.session_state.page_size or len(records)
    page_count = max(1, (len(records) + page_size - 1) // page_size)
    page = min(st.session_state.page, page_count - 1)
    if page_count > 1:
        page = st.radio(
            "Page", range(page_count), index=page, horizontal=True,
            format_func=lambda p: "%d-%d" % (p * page_size + 1,
                                             min((p + 1) * page_size, len(records))),
        )
    st.session_state.page = page
    visible = records[page * page_size : (page + 1) * page_size]

    # -- Batch-wide directive, fed into the next run -----------------------
    directive = st.text_area(
        "Global Batch Directive / Operator Comment",
        value=proj.global_directive,
        height="content",
        placeholder="Optional. Applies to every segment in this batch, e.g. "
                    "\"this section is a tutorial - use the imperative\". "
                    "Sent with the per-segment notes on the next run.",
    )
    if directive != proj.global_directive:
        proj.global_directive = directive
        proj.save()

    # -- Action buttons ----------------------------------------------------
    actions = st.columns([2, 2, 3])
    if actions[0].button("Process Current Batch", type="primary", width='stretch'):
        capture_grid(batch_index)
        process_batch(batch_index)
        st.rerun()
    if actions[1].button("Apply Changes & Calculate Diffs", width='stretch'):
        count = apply_and_diff(batch_index)
        notify("success", "Applied. %d correction(s) recorded for the next batch." % count)
        st.rerun()
    actions[2].caption("Processing sends this batch to `%s`. Applying converts your edits "
                       "into examples for the next batch." % st.session_state.config.model)

    st.divider()

    # A row's Retry button only queues its key (see request_retry). Run it here,
    # above the grid, so progress gets the full grid width. Already popped at
    # the top, so a failure or later rerun can never repeat the request.
    if pending_retry:
        retry_segment(pending_retry)
        st.rerun()

    header = st.columns([1, 4, 4, 5, 3])
    for col, label in zip(header, ("ID", "Source", "Current target (memoQ MT)",
                                   "Your text", "Note to model")):
        col.markdown("**%s**" % label)

    by_key = segments_by_key()
    for record in visible:
        _render_row(record, by_key.get(record.key))

    _render_raw_inspector()


def _render_raw_inspector() -> None:
    """Un-parsed model output for the current batch, for debugging."""
    st.divider()
    raw = st.session_state.get("last_raw") or ""
    with st.expander("Raw LLM Response Stream Inspector"):
        if not raw:
            st.caption("Nothing yet - process a batch or retry a segment.")
            return
        st.caption("%s characters, exactly as the model returned them."
                   % f"{len(raw):,}")
        st.code(raw, language="json")
        st.download_button(
            "Download raw response", raw,
            file_name="raw_response.txt", mime="text/plain", width='stretch',
        )


def _render_row(record: state_mod.SegmentRecord, segment: Optional[mqxliff.Segment]) -> None:
    cols = st.columns([1, 4, 4, 5, 3])

    with cols[0]:
        st.markdown("`%s`" % record.segment_id)
        if record.edited_by_human:
            st.caption("edited")
        if record.tag_report:
            st.caption(":red[tags]")
        if segment is not None and segment.lookup.tags:
            with st.popover("{ }", help="Show full raw XML tags"):
                _render_tag_tabs(segment)

    cols[1].markdown(_display(record.source_text))
    cols[2].markdown(_display(record.mt_target) or ":grey[(empty)]")

    with cols[3]:
        # Seed through session_state rather than ``value=``: passing both makes
        # Streamlit warn, and sync_widgets() needs to be able to overwrite the
        # field after the model returns.
        text_key = text_key_for(record.key)
        st.session_state.setdefault(text_key, record.user_final)
        st.text_area(
            "Your text", key=text_key,
            height="content", label_visibility="collapsed",
            placeholder="Process the batch to get a suggestion, or type your own.",
        )
        if segment is not None:
            _render_tag_buttons(record, segment, text_key)
        if record.tag_report:
            st.caption(":red[Tag issue: %s]" % record.tag_report)
        elif record.llm_suggestion and record.llm_suggestion != record.user_final:
            st.caption("Model proposed: %s" % _display(record.llm_suggestion[:120]))

    note_key = "note_%s" % record.key
    st.session_state.setdefault(note_key, record.user_comment)
    with cols[4]:
        st.text_area(
            "Note", key=note_key,
            height="content", label_visibility="collapsed",
            placeholder="Why you changed it - fed back to the model.",
        )
        # Row action lives here, not in the narrow ID column, so its label
        # fits. It only records the request: the model call and its spinner
        # run from render_grid at full width above the grid.
        st.button("Rerun", key="retry_%s" % record.key,
                  help="Re-run just this segment against the model",
                  width='content', on_click=request_retry, args=(record.key,))

    _render_diff(record)


def _render_tag_buttons(
    record: state_mod.SegmentRecord,
    segment: mqxliff.Segment,
    text_key: str,
) -> None:
    """Clickable placeholder buttons under the editing box.

    Streamlit exposes no caret position for a ``text_area`` - there is no API
    that reports where the operator's cursor is, and a component iframe cannot
    write back into another widget's state. So insertion happens server-side at
    a remembered point: the end of the text by default, which is where a
    reattached tag belongs anyway, and the operator drags it into place.
    """
    injectable = segment.injectable_tags()
    if not injectable:
        return

    row = st.columns(min(len(injectable), 8) + 1)
    for index, (number, _xml) in enumerate(injectable[:8]):
        label = segment.lookup.format_placeholder(number)
        if row[index].button(
            label, key="inj_%s_%d" % (record.key, number),
            help="Insert %s at the end of this row's text" % label,
        ):
            current = st.session_state.get(text_key, "") or ""
            separator = "" if (not current or current.endswith((" ", "\n"))) else " "
            updated = current + separator + label
            # Write through the record and re-key the widget: the box already
            # exists this run, so its state slot is locked.
            project().capture_edits({record.key: updated})
            project().save()
            bump_text_widget(record.key)
            st.rerun()

    if len(injectable) > 8:
        row[-1].caption("+%d more" % (len(injectable) - 8))


def _render_diff(record: state_mod.SegmentRecord) -> None:
    """Inline MT-vs-suggestion diff, shown before the operator starts editing."""
    if not st.session_state.get("show_diff", True):
        return
    if not record.llm_suggestion.strip():
        return

    change = diffing.stats(record.mt_target, record.llm_suggestion)
    if not change.changed:
        st.caption(":grey[Model returned the MT unchanged.]")
        return

    st.markdown(
        diffing.render_html(record.mt_target, record.llm_suggestion),
        unsafe_allow_html=True,
    )
    st.caption("MT to suggestion: %s" % change.summary())


def _render_tag_tabs(segment: mqxliff.Segment) -> None:
    """Both tag ecosystems, side by side, with their global numbers."""
    canonical = ("Current target" if segment.tag_source == mqxliff.tagmod.TAG_SOURCE_TARGET
                 else "Source")
    st.caption("Canonical side: **%s** - its XML is what gets written back." % canonical)

    source_tab, target_tab = st.tabs(["Tags from Source", "Tags from Current Target"])
    for tab, side in ((source_tab, mqxliff.tagmod.TAG_SOURCE_SOURCE),
                      (target_tab, mqxliff.tagmod.TAG_SOURCE_TARGET)):
        with tab:
            entries = segment.side_tags(side)
            if not entries:
                st.caption("_This side has no inline tags._")
                continue
            for number, xml in entries:
                st.markdown("`%s`" % segment.lookup.format_placeholder(number))
                st.code(xml, language="xml")

    shared = set(dict(segment.side_tags(mqxliff.tagmod.TAG_SOURCE_SOURCE))) & set(
        dict(segment.side_tags(mqxliff.tagmod.TAG_SOURCE_TARGET)))
    if shared:
        st.caption(
            "Numbers appearing in both tabs are one physical tag, not two - "
            "%s. Distinct numbers mean the sides genuinely differ and both "
            "variants are injectable."
            % ", ".join(segment.lookup.format_placeholder(n) for n in sorted(shared))
        )


def _display(text: str) -> str:
    """Escape markdown so segment text renders literally, tags included."""
    if not text:
        return ""
    for char in ("\\", "*", "_", "`", "[", "]", "#", "$"):
        text = text.replace(char, "\\" + char)
    return text.replace("\n", " ")


# ---------------------------------------------------------------- entrypoint


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide", initial_sidebar_state="expanded")
    bootstrap()
    render_sidebar()
    flush_notice()

    collapsed = st.session_state.get("rules_collapsed", False)

    # The toggle lives above the split so it is reachable in both states.
    bar = st.columns([2, 2, 8])
    if bar[0].button(
        "Show Rules Panel" if collapsed else "Collapse Rules Panel",
        width='stretch',
        help="Hide the rules/vibe column so the grid takes the full width.",
    ):
        st.session_state.rules_collapsed = not collapsed
        st.rerun()
    st.session_state.show_diff = bar[1].toggle(
        "Show diffs", value=st.session_state.get("show_diff", True),
        help="Inline MT vs. model-suggestion diff under each row.",
    )

    if collapsed:
        # Full width for focused editing.
        render_grid()
        return

    left, right = st.columns([1, 2], gap="large")
    with left:
        with st.container(height=900):
            render_left_panel()
    with right:
        render_grid()


if __name__ == "__main__":
    main()
