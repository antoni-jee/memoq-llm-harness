"""UI-level tests driven by Streamlit's AppTest harness.

These execute the real ``app.py`` script headlessly - no browser, no server -
so widget wiring, session-state handling and button callbacks are all covered.
The network call is stubbed, so nothing here needs a live endpoint.

Run with:  python tests/test_app.py
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streamlit.proto.TextInput_pb2 import TextInput as TextInputProto  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from memoq_harness import (  # noqa: E402
    defaults, llm, prompt_templates, state as state_mod,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "sample.mqxliff")

# Captures the system prompt of the most recent stubbed call so tests can
# assert that the feedback loop actually reaches the model.
LAST_PROMPT: list = [""]


def fake_translate_batch(self, records, system_prompt, progress=None):
    """Stand-in for a model: echoes the source, preserving its tags."""
    LAST_PROMPT[0] = system_prompt
    result = llm.BatchResult()
    for record in records:
        result.suggestions[record.key] = "MT %s" % record.source_text
    result.requests = 1
    result.elapsed = 0.01
    result.raw_responses = [json.dumps(
        {"segments": [{"id": r.key, "target": result.suggestions[r.key]}
                      for r in records]},
        ensure_ascii=False)]
    return result


def fake_analyze(self, corrections, limit=40, hard_rules_text="", expected_tags=None):
    LAST_ANALYSIS_ARGS.clear()
    LAST_ANALYSIS_ARGS.update(
        {"rules": hard_rules_text, "expected": expected_tags or {}})
    return "- The reviewer prefers shorter sentences."


def fake_analyze_with_warning(self, corrections, limit=40, hard_rules_text="",
                              expected_tags=None):
    from memoq_harness import prompts
    return (
        "%s\n- Row 12: the edit dropped tag {2}.\n\n"
        "TRANSLATION BRIEF\n- The reviewer prefers shorter sentences."
        % prompts.OPERATOR_WARNING_HEADING
    )


LAST_ANALYSIS_ARGS: dict = {}


class AppTestCase(unittest.TestCase):
    """Runs a sandboxed copy of the project so state.json stays disposable."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app_dir = os.path.join(self.tmp, "proj")
        shutil.copytree(
            ROOT, self.app_dir,
            ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "exports", "*.log", "state.json*", ".venv"
            ),
        )
        self.app_path = os.path.join(self.app_dir, "app.py")
        self.state_path = os.path.join(self.app_dir, "state.json")

        self._patches = [
            (llm.LLMClient, "translate_batch", llm.LLMClient.translate_batch),
            (llm.LLMClient, "analyze_corrections", llm.LLMClient.analyze_corrections),
            (llm.LLMClient, "__init__", llm.LLMClient.__init__),
        ]
        llm.LLMClient.translate_batch = fake_translate_batch
        llm.LLMClient.analyze_corrections = fake_analyze
        llm.LLMClient.__init__ = lambda self, config: setattr(self, "config", config)
        LAST_PROMPT[0] = ""

    def tearDown(self):
        for owner, name, original in self._patches:
            setattr(owner, name, original)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ----------------------------------------------------------- helpers

    def run_app(self) -> AppTest:
        app = AppTest.from_file(self.app_path, default_timeout=120)
        app.run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])
        return app

    def load_doc(self, app: AppTest, batch_size: int = 4) -> AppTest:
        """Load the fixture through the sidebar's path loader."""
        app.session_state.project.batch_size = batch_size
        app.sidebar.text_input(key="disk_path").set_value(FIXTURE).run()
        self.button(app.sidebar, "Load").click().run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])
        self.assertIsNotNone(app.session_state.document)
        return app

    @staticmethod
    def tkey(app: AppTest, record_key: str) -> str:
        """Current widget key for a row's editing box.

        The key carries a revision counter so button handlers can reset the
        box, so tests must resolve it rather than hard-code ``txt_<key>``.
        """
        rev = app.session_state.get("txtrev_%s" % record_key, 0)
        return "txt_%s_%d" % (record_key, rev)

    def row_text(self, app: AppTest, record_key: str):
        return app.text_area(key=self.tkey(app, record_key))

    @staticmethod
    def button(scope, label: str):
        for widget in scope.button:
            if widget.label == label:
                return widget
        raise AssertionError("No button labelled %r in %s" % (
            label, [w.label for w in scope.button]))

    @staticmethod
    def text_area(scope, label: str):
        for widget in scope.text_area:
            if widget.label == label:
                return widget
        raise AssertionError("No text_area labelled %r" % label)

    # ------------------------------------------------------- basic render

    def test_app_starts_clean(self):
        self.run_app()

    def test_prompts_for_a_file_when_empty(self):
        app = self.run_app()
        self.assertTrue(any("mqxliff" in el.value for el in app.info))

    def test_all_four_presets_offered(self):
        app = self.run_app()
        options = app.sidebar.selectbox[0].options
        for name in ("OpenAI API", "OpenRouter", "DeepSeek", "Local Engine (Ollama)"):
            self.assertIn(name, options)

    def test_ollama_is_the_default_preset(self):
        app = self.run_app()
        self.assertEqual(app.sidebar.selectbox[0].value, "Local Engine (Ollama)")
        self.assertEqual(app.session_state.config.base_url, llm.ollama_base_url())
        self.assertTrue(app.session_state.config.base_url.endswith("/v1"))

    def test_switching_preset_swaps_base_url_and_model(self):
        app = self.run_app()
        app.sidebar.selectbox[0].set_value("OpenAI API").run()
        self.assertEqual(app.session_state.config.base_url, "https://api.openai.com/v1")
        self.assertEqual(app.session_state.config.model, "gpt-4o")

    def test_api_key_is_masked_and_never_persisted(self):
        app = self.run_app()
        field = next(t for t in app.sidebar.text_input if "API key" in t.label)
        # AppTest hardcodes .type to "text_input"; masking lives on the proto.
        self.assertEqual(field.proto.type, TextInputProto.PASSWORD)
        field.set_value("sk-secret-value").run()
        app.session_state.project.save()
        with open(self.state_path, "r", encoding="utf-8") as handle:
            self.assertNotIn("sk-secret-value", handle.read())

    def test_termbase_preloads_at_startup(self):
        app = self.run_app()
        terms = app.session_state.project.termbase
        self.assertTrue(terms)
        self.assertTrue(all(t.source and t.target for t in terms))
        self.assertIn(app.session_state.termbase_source,
                      ("MQL term base.csv", "termbase.csv"))

    def test_real_export_takes_precedence_over_the_sample(self):
        """A memoQ export in the project root wins over config/termbase.csv."""
        real = os.path.join(self.app_dir, "MQL term base.csv")
        if not os.path.exists(real):
            self.skipTest("real termbase export not present")
        app = self.run_app()
        self.assertEqual(app.session_state.termbase_source, "MQL term base.csv")
        self.assertGreater(len(app.session_state.project.termbase), 1000)

    def test_sample_termbase_is_used_when_no_export_present(self):
        real = os.path.join(self.app_dir, "MQL term base.csv")
        if os.path.exists(real):
            os.remove(real)
        app = self.run_app()
        self.assertEqual(app.session_state.termbase_source, "termbase.csv")
        terms = app.session_state.project.termbase
        self.assertEqual(
            next(t.target for t in terms if t.source == "entropy"), "энтропия"
        )

    def test_left_panel_shows_both_default_blocks(self):
        app = self.run_app()
        proj = app.session_state.project
        self.assertEqual(proj.hard_rules_text, defaults.DEFAULT_HARD_RULES)
        self.assertEqual(proj.vibe, defaults.DEFAULT_VIBE)
        self.assertEqual(self.text_area(app, "Hard Rules").value,
                         defaults.DEFAULT_HARD_RULES)
        self.assertEqual(self.text_area(app, "Vibe").value, defaults.DEFAULT_VIBE)

    def test_hard_rules_are_editable_and_persist(self):
        app = self.run_app()
        self.text_area(app, "Hard Rules").set_value("Только моя редакция.").run()
        self.assertEqual(
            state_mod.ProjectState.load(self.state_path).hard_rules_text,
            "Только моя редакция.",
        )

    def test_vibe_persists_to_disk(self):
        app = self.run_app()
        self.text_area(app, "Vibe").set_value("Formal, use 'Sie'.").run()
        self.assertEqual(
            state_mod.ProjectState.load(self.state_path).vibe, "Formal, use 'Sie'."
        )

    def test_restore_buttons_bring_the_defaults_back(self):
        app = self.run_app()
        self.text_area(app, "Hard Rules").set_value("wiped").run()
        self.text_area(app, "Vibe").set_value("wiped").run()
        # Several buttons share the label "Restore", so select them by key.
        app.button(key="reset_rules").click().run()
        app.button(key="reset_vibe").click().run()

        proj = app.session_state.project
        self.assertEqual(proj.hard_rules_text, defaults.DEFAULT_HARD_RULES)
        self.assertEqual(proj.vibe, defaults.DEFAULT_VIBE)

    def test_template_restore_resets_the_box(self):
        app = self.run_app()
        self.text_area(app, "MT2").set_value("переписано").run()
        self.assertEqual(app.session_state.project.template_for("mt2"), "переписано")

        app.button(key="rst_mt2").click().run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])
        proj = app.session_state.project
        self.assertEqual(proj.template_for("mt2"),
                         prompt_templates.STAGE_BY_KEY["mt2"].template)
        # The visible box must show the restored text, not the edit.
        self.assertEqual(self.text_area(app, "MT2").value, proj.template_for("mt2"))

    # ------------------------------------------------------- document load

    def test_bad_path_reports_an_error(self):
        app = self.run_app()
        app.sidebar.text_input(key="disk_path").set_value("C:\\nope\\missing.mqxliff").run()
        self.button(app.sidebar, "Load").click().run()
        self.assertTrue(any("No file at" in el.value for el in app.error))

    def test_loading_filters_confirmed_and_locked(self):
        app = self.load_doc(self.run_app())
        proj = app.session_state.project
        # 7 of the fixture's 12 rows are workable.
        self.assertEqual(len(proj.segments), 7)
        self.assertEqual(proj.batch_count, 2)  # batch_size 4
        self.assertTrue(any("7 to work on" in el.value for el in app.success))

    def test_grid_shows_one_row_per_segment(self):
        app = self.load_doc(self.run_app())
        keys = {k for k in app.session_state if k.startswith("txt_")}
        self.assertEqual(len(keys), 4)  # first batch only

    def test_relaxing_the_filter_loads_more_rows(self):
        app = self.load_doc(self.run_app())
        checkbox = next(c for c in app.sidebar.checkbox if c.label == "Skip confirmed rows")
        checkbox.set_value(False).run()
        self.assertEqual(len(app.session_state.project.segments), 10)

    def test_filter_toggle_preserves_applied_human_edit(self):
        """Hiding a row through the filter, then restoring it, must not discard
        the human's saved text, comment, or edited flag.

        The confirmed fixture row id 3 is grid-editable only while
        "Skip confirmed rows" is off, which is what makes the scenario valid.
        """
        final = "Meine eigene Fassung 3"
        comment = "manuell geaendert"
        app = self.load_doc(self.run_app())

        def skip_confirmed_checkbox():
            return next(c for c in app.sidebar.checkbox
                        if c.label == "Skip confirmed rows")

        # 2. Reveal the confirmed row so it is grid-editable.
        skip_confirmed_checkbox().set_value(False).run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])

        # 3. Locate the confirmed segment by identity, not by batch position.
        proj = app.session_state.project
        target = next(
            r for b in range(proj.batch_count)
            for r in proj.batch_records(b)
            if str(r.segment_id) == "3")
        key = target.key

        # 4. Enter a distinct manual translation and a non-empty comment.
        self.row_text(app, key).set_value(final).run()
        app.text_area(key="note_%s" % key).set_value(comment).run()

        # 5. Apply and assert the work is saved.
        self.button(app, "Apply Changes & Calculate Diffs").click().run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])
        saved = app.session_state.project.segments[key]
        with self.subTest(phase="after-apply", field="user_final"):
            self.assertEqual(saved.user_final, final)
        with self.subTest(phase="after-apply", field="user_comment"):
            self.assertEqual(saved.user_comment, comment)
        with self.subTest(phase="after-apply", field="edited_by_human"):
            self.assertTrue(saved.edited_by_human)

        # 6. Hide the row via the filter.
        skip_confirmed_checkbox().set_value(True).run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])
        # Filtering excludes the row from processing (the active batches); it
        # must not require deletion from the persistent segment store.
        hidden = app.session_state.project
        active_keys = {
            r.key for b in range(hidden.batch_count)
            for r in hidden.batch_records(b)}
        self.assertNotIn(key, active_keys)

        # 7. Restore the filter so the row is included again.
        skip_confirmed_checkbox().set_value(False).run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])

        # 8. The saved human work must survive the hide/restore round trip.
        restored = app.session_state.project.segments.get(key)
        self.assertIsNotNone(restored)
        with self.subTest(phase="after-restore", field="user_final"):
            self.assertEqual(restored.user_final, final)
        with self.subTest(phase="after-restore", field="user_comment"):
            self.assertEqual(restored.user_comment, comment)
        with self.subTest(phase="after-restore", field="edited_by_human"):
            self.assertTrue(restored.edited_by_human)
        with self.subTest(phase="after-restore", field="editor-text"):
            self.assertEqual(self.row_text(app, key).value, final)

    # ----------------------------------------------------- the batch cycle

    def test_process_batch_fills_the_grid(self):
        app = self.load_doc(self.run_app())
        self.button(app, "Process Current Batch").click().run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])

        proj = app.session_state.project
        first = proj.batch_records(0)[0]
        self.assertTrue(first.llm_suggestion.startswith("MT "))
        self.assertEqual(first.user_final, first.llm_suggestion)
        self.assertEqual(first.status, "processed")
        self.assertEqual(app.session_state[self.tkey(app, first.key)], first.llm_suggestion)

    def test_apply_turns_edits_into_corrections(self):
        app = self.load_doc(self.run_app())
        self.button(app, "Process Current Batch").click().run()

        proj = app.session_state.project
        first = proj.batch_records(0)[0]
        self.row_text(app, first.key).set_value("Meine eigene Fassung").run()
        app.text_area(key="note_%s" % first.key).set_value("zu woertlich").run()
        self.button(app, "Apply Changes & Calculate Diffs").click().run()

        proj = app.session_state.project
        self.assertEqual(len(proj.corrections), 1)
        correction = proj.corrections[0]
        self.assertEqual(correction.human_final, "Meine eigene Fassung")
        self.assertEqual(correction.comment, "zu woertlich")
        self.assertTrue(proj.segments[first.key].edited_by_human)

    def test_unedited_rows_produce_no_corrections(self):
        app = self.load_doc(self.run_app())
        self.button(app, "Process Current Batch").click().run()
        self.button(app, "Apply Changes & Calculate Diffs").click().run()
        self.assertEqual(app.session_state.project.corrections, [])

    def test_next_batch_auto_applies_diffs(self):
        app = self.load_doc(self.run_app())
        self.button(app, "Process Current Batch").click().run()
        first = app.session_state.project.batch_records(0)[0]
        self.row_text(app, first.key).set_value("Handkorrektur").run()

        self.button(app, "Next").click().run()
        proj = app.session_state.project
        self.assertEqual(proj.current_batch, 1)
        self.assertEqual(len(proj.corrections), 1)
        self.assertEqual(proj.corrections[0].human_final, "Handkorrektur")

    def test_correction_reaches_the_next_batchs_prompt(self):
        app = self.load_doc(self.run_app())
        self.button(app, "Process Current Batch").click().run()
        first = app.session_state.project.batch_records(0)[0]
        self.row_text(app, first.key).set_value("Handkorrektur").run()
        self.button(app, "Next").click().run()

        self.button(app, "Process Current Batch").click().run()
        self.assertIn("HUMAN CORRECTIONS", LAST_PROMPT[0])
        self.assertIn("Handkorrektur", LAST_PROMPT[0])

    def test_rules_vibe_and_termbase_reach_the_prompt(self):
        app = self.load_doc(self.run_app())
        self.text_area(app, "Vibe").set_value("Sehr formell.").run()
        self.text_area(app, "Hard Rules").set_value("Всегда 'е', не 'ё'.").run()
        self.button(app, "Process Current Batch").click().run()

        self.assertIn("Sehr formell.", LAST_PROMPT[0])
        self.assertIn("Всегда 'е', не 'ё'.", LAST_PROMPT[0])
        self.assertIn("HARD EDITORIAL RULES", LAST_PROMPT[0])

    def test_only_matching_termbase_entries_are_sent(self):
        """Terms absent from the batch must not be spent on tokens."""
        app = self.load_doc(self.run_app())
        app.session_state.project.termbase = [
            state_mod.TermEntry("dashboard", "Dashboard"),   # in the fixture
            state_mod.TermEntry("entropy", "энтропия"),      # not in the fixture
        ]
        self.button(app, "Process Current Batch").click().run()

        # Scope the check to the termbase block: "энтропия" also occurs in the
        # default vibe text, where it legitimately belongs.
        prompt = LAST_PROMPT[0]
        self.assertIn("TERMBASE ENTRIES", prompt)
        section = prompt.split("TERMBASE ENTRIES", 1)[1].split("\n\n", 1)[0]
        self.assertIn('"dashboard" -> "Dashboard"', section)
        self.assertNotIn("энтропия", section)

    def test_navigating_back_does_not_bounce(self):
        app = self.load_doc(self.run_app())
        self.button(app, "Next").click().run()
        self.assertEqual(app.session_state.project.current_batch, 1)
        self.button(app, "Previous").click().run()
        self.assertEqual(app.session_state.project.current_batch, 0)
        app.run()  # a plain rerun must not move the batch again
        self.assertEqual(app.session_state.project.current_batch, 0)

    def test_reprocessing_preserves_human_edits(self):
        app = self.load_doc(self.run_app())
        self.button(app, "Process Current Batch").click().run()
        first = app.session_state.project.batch_records(0)[0]
        self.row_text(app, first.key).set_value("Meins").run()
        self.button(app, "Apply Changes & Calculate Diffs").click().run()
        self.button(app, "Process Current Batch").click().run()
        self.assertEqual(app.session_state.project.segments[first.key].user_final, "Meins")

    def test_analysis_button_populates_the_left_panel(self):
        app = self.load_doc(self.run_app())
        self.button(app, "Process Current Batch").click().run()
        first = app.session_state.project.batch_records(0)[0]
        self.row_text(app, first.key).set_value("Anders").run()
        self.button(app, "Apply Changes & Calculate Diffs").click().run()
        self.button(app, "Analyse my corrections").click().run()
        self.assertTrue(app.session_state.project.analysis)
        self.assertTrue(any("shorter sentences" in m.value for m in app.markdown))

    # ------------------------------------------------------------- export

    def test_export_writes_a_verified_file(self):
        app = self.load_doc(self.run_app())
        self.button(app, "Process Current Batch").click().run()
        self.button(app.sidebar, "Export Valid MQXLIFF").click().run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])

        exported = app.session_state.last_export
        self.assertTrue(os.path.isfile(exported))
        self.assertEqual(os.path.dirname(exported),
                         os.path.join(self.app_dir, "exports"))
        self.assertTrue(any("verified parseable" in m.value for m in app.success))

        from memoq_harness import mqxliff  # noqa: PLC0415
        doc = mqxliff.MqxliffDocument.from_path(exported)
        doc.parse_segments()
        self.assertEqual(len(doc.segments), 12)
        first = next(s for s in doc.segments if s.unit_id == "1")
        self.assertTrue(first.target_text.startswith("MT "))
        # A confirmed row must come through untouched.
        confirmed = next(s for s in doc.segments if s.unit_id == "3")
        self.assertEqual(confirmed.target_text, "Diese Zeile ist bereits bestaetigt.")

    def test_export_without_a_document_is_blocked(self):
        app = self.run_app()
        self.assertTrue(self.button(app.sidebar, "Export Valid MQXLIFF").disabled)

    def test_applied_edit_survives_restart_reload_and_export(self):
        """Captured edit -> restart -> reload the same file -> export.

        A second AppTest over the same sandbox app_dir is a fresh Streamlit
        session (new session_state, bootstrap runs again) that shares only the
        persisted state.json - the closest offline stand-in for a restart.
        """
        app = self.load_doc(self.run_app())
        self.button(app, "Process Current Batch").click().run()
        target = app.session_state.project.batch_records(0)[0]
        suggestion = target.llm_suggestion
        self.assertTrue(suggestion.startswith("MT "))
        # Keep the suggestion's tag placeholders; change only the prose.
        manual = "MANUELL " + suggestion[len("MT "):]

        self.row_text(app, target.key).set_value(manual).run()
        self.button(app, "Apply Changes & Calculate Diffs").click().run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])

        # 2. The revision reached the sandboxed state file on disk.
        # v5 layout: the record lives under the active document's entry.
        with open(self.state_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        saved = raw["documents"][raw["active_document"]]["segments"][target.key]
        self.assertEqual(saved["user_final"], manual)
        self.assertTrue(saved["edited_by_human"])

        # 3-5. Restart: new session, same state file; no document in memory.
        restarted = self.run_app()
        self.assertIsNone(restarted.session_state.document)
        self.assertFalse(restarted.session_state.doc_bytes)
        record = restarted.session_state.project.segments[target.key]
        self.assertEqual(record.user_final, manual)
        self.assertTrue(
            self.button(restarted.sidebar, "Export Valid MQXLIFF").disabled)

        # 6-7. Reload the identical fixture under the same filename.
        restarted = self.load_doc(restarted)
        record = restarted.session_state.project.segments[target.key]
        self.assertEqual(record.user_final, manual)
        self.assertTrue(record.edited_by_human)
        self.assertEqual(self.row_text(restarted, target.key).value, manual)

        # 8. Export and check the corresponding segment in the written file.
        self.button(restarted.sidebar, "Export Valid MQXLIFF").click().run()
        self.assertEqual(restarted.exception, [],
                         [str(e) for e in restarted.exception])
        self.assertTrue(
            any("verified parseable" in m.value for m in restarted.success))
        exported = restarted.session_state.last_export
        self.assertEqual(os.path.dirname(exported),
                         os.path.join(self.app_dir, "exports"))

        from memoq_harness import mqxliff  # noqa: PLC0415
        doc = mqxliff.MqxliffDocument.from_path(exported)
        doc.parse_segments()
        segment = next(s for s in doc.segments if s.key == target.key)
        self.assertEqual(segment.unit_id, target.segment_id)
        self.assertTrue(segment.target_text.startswith("MANUELL "),
                        segment.target_text)
        self.assertIn("Save", segment.target_text)

    # ------------------------------------------------- new in this revision

    def test_anthropic_preset_is_offered_and_switches_backend(self):
        app = self.run_app()
        self.assertIn("Anthropic API", app.sidebar.selectbox[0].options)
        app.sidebar.selectbox[0].set_value("Anthropic API").run()
        config = app.session_state.config
        self.assertEqual(config.backend, llm.BACKEND_ANTHROPIC)
        self.assertTrue(config.is_anthropic)
        self.assertEqual(config.model, "claude-3-5-sonnet-latest")
        self.assertIn("api.anthropic.com", config.base_url)

    def test_anthropic_key_field_is_labelled_and_masked(self):
        app = self.run_app()
        app.sidebar.selectbox[0].set_value("Anthropic API").run()
        field = next(t for t in app.sidebar.text_input
                     if "Anthropic API key" in t.label)
        self.assertEqual(field.proto.type, TextInputProto.PASSWORD)

    # ------------------------------------------------- model Test button

    def _fake_openai_sdk(self, reply):
        """Real LLMClient constructor and probe; only the SDK is stubbed."""
        llm.LLMClient.__init__ = self._patches[2][2]
        requests = []

        def factory(**_kwargs):
            def create(**request):
                requests.append(request)
                return reply
            return SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        patcher = mock.patch.object(llm, "OpenAI", factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        return requests

    def _work_with_manual_edit(self):
        app = self.load_doc(self.run_app())
        self.button(app, "Process Current Batch").click().run()
        key = app.session_state.project.batch_records(0)[0].key
        self.row_text(app, key).set_value("Manuelle Fassung").run()
        app.text_area(key="note_%s" % key).set_value("probe must keep this").run()
        self.button(app, "Apply Changes & Calculate Diffs").click().run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])
        return app

    def _snapshot(self, app):
        segments = {
            k: (s.user_final, s.user_comment, s.edited_by_human)
            for k, s in app.session_state.project.segments.items()}
        with open(self.state_path, "rb") as handle:
            disk = handle.read()
        return dataclasses.asdict(app.session_state.config), segments, disk

    def _click_test(self, app):
        self.button(app.sidebar, "Test").click().run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])

    def test_model_test_success_preserves_work_and_config(self):
        app = self._work_with_manual_edit()
        requests = self._fake_openai_sdk(SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="ok"))]))
        before = self._snapshot(app)

        self._click_test(app)

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["model"], app.session_state.config.model)
        self.assertEqual(requests[0]["messages"], [dict(m) for m in llm.PROBE_MESSAGES])
        self.assertEqual(requests[0]["max_tokens"], llm.PROBE_MAX_TOKENS)
        shown = " ".join(s.value for s in app.success)
        self.assertIn("returned visible text", shown)
        self.assertIn("not checked", shown)
        self.assertEqual(self._snapshot(app), before)

    def test_model_test_without_visible_text_is_a_warning(self):
        app = self._work_with_manual_edit()
        requests = self._fake_openai_sdk(SimpleNamespace(
            model="qwen3:8b",
            choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(
                content="", reasoning="hidden chain of thought"))]))
        before = self._snapshot(app)

        self._click_test(app)

        self.assertEqual(len(requests), 1)
        warned = " ".join(w.value for w in app.warning)
        self.assertIn("inconclusive", warned)
        self.assertIn("Server-reported model: `qwen3:8b`", warned)
        self.assertNotIn("hidden chain", warned)
        self.assertFalse(any("Model test" in e.value for e in app.error))
        self.assertEqual(self._snapshot(app), before)

    # ------------------------------------------- engine widgets and presets

    @staticmethod
    def _engine(app):
        base = next(t for t in app.sidebar.text_input if t.label == "Base URL")
        key = next(t for t in app.sidebar.text_input if "API key" in t.label)
        model = next(s for s in app.sidebar.selectbox if s.label == "Model")
        return base, key, model

    def _assert_widgets_match_config(self, app):
        base, key, model = self._engine(app)
        config = app.session_state.config
        self.assertEqual(base.value, config.base_url)
        self.assertEqual(key.value, config.api_key)
        self.assertEqual(model.value, config.model)

    def test_provider_switch_clears_typed_key_and_resets_widgets(self):
        app = self.run_app()
        app.sidebar.selectbox[0].set_value("DeepSeek").run()
        self._engine(app)[1].set_value("sk-deepseek-typed").run()
        self.assertEqual(app.session_state.config.api_key, "sk-deepseek-typed")

        # Same OpenAI-compatible protocol, different provider: key must go.
        app.sidebar.selectbox[0].set_value("OpenRouter").run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])
        config = app.session_state.config
        self.assertEqual(config.api_key, "")
        self.assertEqual(config.base_url, llm.PRESETS["OpenRouter"].base_url)
        self.assertEqual(config.model, llm.PRESETS["OpenRouter"].default_model)
        self._assert_widgets_match_config(app)

        app.sidebar.selectbox[0].set_value("Anthropic API").run()
        self.assertEqual(app.session_state.config.base_url,
                         llm.PRESETS["Anthropic API"].base_url)
        self._assert_widgets_match_config(app)

    def test_reruns_and_model_changes_keep_the_key(self):
        app = self.run_app()
        app.sidebar.selectbox[0].set_value("OpenAI API").run()
        self._engine(app)[1].set_value("sk-openai-typed").run()
        app.run()
        self.assertEqual(app.session_state.config.api_key, "sk-openai-typed")
        self._engine(app)[2].set_value("gpt-4o-mini").run()
        self.assertEqual(app.session_state.config.model, "gpt-4o-mini")
        self.assertEqual(app.session_state.config.api_key, "sk-openai-typed")
        self._assert_widgets_match_config(app)

    def test_connection_summary_is_shown_without_secrets(self):
        app = self.run_app()
        app.sidebar.selectbox[0].set_value("OpenAI API").run()
        self._engine(app)[1].set_value("sk-summary-secret").run()
        captions = " ".join(c.value for c in app.sidebar.caption)
        self.assertIn("Endpoint: `https://api.openai.com/v1`", captions)
        self.assertIn("Requested model: `gpt-4o`", captions)
        self.assertIn("Credentials: sidebar key", captions)
        self.assertNotIn("sk-summary-secret", captions)

    # ---------------------------------------------------------------- retry

    def _fake_translate_one(self, outcome):
        calls = []

        def translate_one(client, record, system_prompt):
            calls.append(record.key)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome, '{"raw": true}'

        patcher = mock.patch.object(llm.LLMClient, "translate_one", translate_one)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_retry_runs_once_and_keeps_other_row_edits(self):
        app = self.load_doc(self.run_app())
        self.button(app, "Process Current Batch").click().run()
        first, second = [r.key for r in app.session_state.project.batch_records(0)[:2]]
        self.row_text(app, second).set_value("Unapplied edit on row two").run()
        calls = self._fake_translate_one("Neu uebersetzt")

        app.button(key="retry_%s" % first).click().run()

        self.assertEqual(app.exception, [], [str(e) for e in app.exception])
        self.assertEqual(calls, [first])
        self.assertNotIn("pending_retry", app.session_state)
        self.assertIn("re-translated", " ".join(s.value for s in app.success))
        self.assertEqual(self.row_text(app, first).value, "Neu uebersetzt")
        self.assertEqual(self.row_text(app, second).value, "Unapplied edit on row two")
        self.assertEqual(app.session_state.project.segments[second].user_final,
                         "Unapplied edit on row two")
        app.run()
        self.assertEqual(calls, [first])

    def test_failed_retry_is_not_repeated_on_rerun(self):
        app = self.load_doc(self.run_app())
        key = app.session_state.project.batch_records(0)[0].key
        calls = self._fake_translate_one(llm.LLMError("simulated failure"))

        app.button(key="retry_%s" % key).click().run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])
        self.assertEqual(calls, [key])
        self.assertIn("Retry failed", " ".join(e.value for e in app.error))
        self.assertNotIn("pending_retry", app.session_state)

        app.run()
        app.run()
        self.assertEqual(calls, [key])

    def test_model_test_failure_is_reported_without_side_effects(self):
        app = self._work_with_manual_edit()
        requests = self._fake_openai_sdk("<html>gateway page</html>")
        before = self._snapshot(app)

        self._click_test(app)

        self.assertEqual(len(requests), 1)
        shown = " ".join(e.value for e in app.error)
        self.assertIn("Model test failed", shown)
        self.assertIn("response format", shown)
        self.assertEqual(self._snapshot(app), before)

    def test_collapse_button_hides_the_rules_panel(self):
        app = self.load_doc(self.run_app())
        self.assertIn("Hard Rules", [t.label for t in app.text_area])

        self.button(app, "Collapse Rules Panel").click().run()
        self.assertTrue(app.session_state.rules_collapsed)
        self.assertNotIn("Hard Rules", [t.label for t in app.text_area])
        # The grid is still there, now full width.
        self.assertTrue(any(b.label == "Process Current Batch" for b in app.button))

        self.button(app, "Show Rules Panel").click().run()
        self.assertFalse(app.session_state.rules_collapsed)
        self.assertIn("Hard Rules", [t.label for t in app.text_area])

    def test_extra_instructions_field_is_gone(self):
        app = self.run_app()
        self.assertNotIn("Extra", [t.label for t in app.text_area])

    def test_export_rules_to_txt_writes_a_file(self):
        app = self.load_doc(self.run_app())
        self.button(app, "Export Rules to TXT").click().run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])

        path = app.session_state.last_rules_export
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(os.path.dirname(path),
                         os.path.join(self.app_dir, "exports"))
        self.assertTrue(os.path.basename(path).startswith("rules_export_"))
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("HARD RULES", body)
        self.assertIn("Правила редактуры", body)
        self.assertIn("PROMPT TEMPLATES", body)

    def test_prompt_templates_are_exposed_and_editable(self):
        app = self.run_app()
        labels = [t.label for t in app.text_area]
        for stage in prompt_templates.STAGES:
            self.assertIn(stage.label, labels)

        self.text_area(app, "MT2").set_value("мой шаблон MT2").run()
        self.assertEqual(
            state_mod.ProjectState.load(self.state_path).template_for("mt2"),
            "мой шаблон MT2",
        )

    def test_pinned_stage_overrides_auto_selection(self):
        app = self.load_doc(self.run_app())
        proj = app.session_state.project
        self.assertEqual(proj.active_stage(0), "mt1")
        proj.prompt_stage = "mt3"
        self.assertEqual(proj.active_stage(0), "mt3")

    def test_stage_template_reaches_the_prompt(self):
        app = self.load_doc(self.run_app())
        # Edit through the widget, as an operator would; setting the model
        # object directly would be overwritten by the panel on the next run.
        self.text_area(app, "MT1").set_value("ШАБЛОН ПЕРВОЙ ПОРЦИИ").run()
        self.button(app, "Process Current Batch").click().run()
        self.assertTrue(LAST_PROMPT[0].startswith("ШАБЛОН ПЕРВОЙ ПОРЦИИ"))
        self.assertEqual(app.session_state.last_stage, "mt1")

    def test_template_token_is_substituted(self):
        app = self.load_doc(self.run_app())
        self.text_area(app, "Hard Rules").set_value("ПРАВИЛА-МАРКЕР").run()
        self.text_area(app, "MT1").set_value("Вот правила: {{HARD_RULES}} конец").run()
        self.button(app, "Process Current Batch").click().run()

        prompt = LAST_PROMPT[0]
        self.assertIn("Вот правила: ПРАВИЛА-МАРКЕР конец", prompt)
        self.assertEqual(prompt.count("ПРАВИЛА-МАРКЕР"), 1)  # not duplicated
        self.assertNotIn("HARD EDITORIAL RULES", prompt)

    def test_tag_source_defaults_to_target_and_is_switchable(self):
        app = self.load_doc(self.run_app())
        proj = app.session_state.project
        self.assertEqual(proj.tag_source, "target")
        segments = app.session_state.document.translatable_segments()
        self.assertTrue(all(s.tag_source == "target" for s in segments))

        proj.tag_source = "source"
        proj.save()
        app.run()

    def test_diff_toggle_exists_and_persists(self):
        app = self.run_app()
        self.assertTrue(app.session_state.show_diff)
        app.toggle[0].set_value(False).run()
        self.assertFalse(app.session_state.show_diff)

    def test_diff_renders_after_processing(self):
        app = self.load_doc(self.run_app())
        self.button(app, "Process Current Batch").click().run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])
        # The stub returns "MT " + source, so every row differs from its MT.
        captions = [c.value for c in app.caption]
        self.assertTrue(any("MT to suggestion" in c for c in captions), captions[:8])

    # ------------------------------------------- feature pass: items 1-7

    def test_grid_text_areas_auto_size_to_content(self):
        """No fixed pixel heights in the grid - no nested scrollbars.

        Height is applied client-side and is absent from the TextArea proto,
        so AppTest cannot observe it; assert on the row renderer's source.
        """
        import inspect
        import re

        import app as app_module
        body = inspect.getsource(app_module._render_row)
        body += inspect.getsource(app_module._render_tag_buttons)

        self.assertIn('height="content"', body)
        fixed = re.findall(r"height\s*=\s*\d+", body)
        self.assertEqual(fixed, [], "fixed pixel heights left in the grid: %s" % fixed)

    def test_tag_injection_button_appends_placeholder(self):
        app = self.load_doc(self.run_app())
        # Segment id 1 in the fixture carries {1} and {2}.
        record = next(r for r in app.session_state.project.batch_records(0)
                      if r.segment_id == "1")
        self.row_text(app, record.key).set_value("Готовый текст").run()

        app.button(key="inj_%s_1" % record.key).click().run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])
        # The box is deliberately re-keyed on insert, so resolve it afresh.
        self.assertEqual(app.session_state[self.tkey(app, record.key)],
                         "Готовый текст {1}")
        self.assertEqual(self.row_text(app, record.key).value, "Готовый текст {1}")
        # And it is captured into the project, not just the widget.
        self.assertEqual(
            app.session_state.project.segments[record.key].user_final,
            "Готовый текст {1}")

    def test_injection_button_absent_for_untagged_rows(self):
        app = self.load_doc(self.run_app())
        # Segment 8 ("Tom & Jerry ...") is in batch 0 and carries no tags.
        record = next(r for r in app.session_state.project.batch_records(0)
                      if r.segment_id == "8")
        keys = [b.key for b in app.button if b.key]
        self.assertFalse([k for k in keys if k.startswith("inj_%s" % record.key)])

    def test_retry_button_exists_for_every_row(self):
        app = self.load_doc(self.run_app())
        for record in app.session_state.project.batch_records(0):
            self.assertIsNotNone(app.button(key="retry_%s" % record.key))

    def test_retry_patches_only_its_own_row(self):
        app = self.load_doc(self.run_app())
        self.button(app, "Process Current Batch").click().run()
        proj = app.session_state.project
        records = proj.batch_records(0)
        target, neighbour = records[0], records[1]
        before = neighbour.llm_suggestion
        self.assertTrue(before)

        original = llm.LLMClient.translate_one
        llm.LLMClient.translate_one = lambda self, record, prompt: (
            "ПЕРЕВОД ПОСЛЕ ПОВТОРА", '{"segments":[{"id":"x","target":"..."}]}')
        try:
            app.button(key="retry_%s" % target.key).click().run()
        finally:
            llm.LLMClient.translate_one = original
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])

        proj = app.session_state.project
        self.assertEqual(proj.segments[target.key].llm_suggestion,
                         "ПЕРЕВОД ПОСЛЕ ПОВТОРА")
        self.assertEqual(proj.segments[neighbour.key].llm_suggestion, before)

    def test_retry_reports_an_empty_response_without_clobbering(self):
        app = self.load_doc(self.run_app())
        self.button(app, "Process Current Batch").click().run()
        record = app.session_state.project.batch_records(0)[0]
        before = record.llm_suggestion

        original = llm.LLMClient.translate_one
        llm.LLMClient.translate_one = lambda self, r, p: (None, "not json")
        try:
            app.button(key="retry_%s" % record.key).click().run()
        finally:
            llm.LLMClient.translate_one = original

        self.assertEqual(
            app.session_state.project.segments[record.key].llm_suggestion, before)
        self.assertTrue(any("empty again" in w.value for w in app.warning))

    def test_retry_preserves_manual_edit(self):
        """Process -> manual edit -> Retry, WITHOUT Apply.

        The manual revision must survive in state and the visible editor; the
        fresh suggestion goes to llm_suggestion; a neighbouring row is untouched.
        """
        app = self.load_doc(self.run_app())
        self.button(app, "Process Current Batch").click().run()

        proj = app.session_state.project
        target, neighbour = proj.batch_records(0)[0], proj.batch_records(0)[1]
        before_neighbour = (proj.segments[neighbour.key].llm_suggestion,
                            proj.segments[neighbour.key].user_final)

        self.row_text(app, target.key).set_value("meine Fassung").run()

        original = llm.LLMClient.translate_one
        llm.LLMClient.translate_one = (
            lambda self, record, prompt: ("ПОВТОРНЫЙ ВАРЬЯНТ",
            '{"segments":[{"id":"x","target":"..."}]}'))
        try:
            app.button(key="retry_%s" % target.key).click().run()
        finally:
            llm.LLMClient.translate_one = original
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])

        proj = app.session_state.project
        self.assertEqual(proj.segments[target.key].user_final, "meine Fassung")
        self.assertEqual(app.session_state[self.tkey(app, target.key)],
                         "meine Fassung")
        self.assertEqual(proj.segments[target.key].llm_suggestion,
                         "ПОВТОРНЫЙ ВАРЬЯНТ")
        after_neighbour = (proj.segments[neighbour.key].llm_suggestion,
                           proj.segments[neighbour.key].user_final)
        self.assertEqual(after_neighbour, before_neighbour)

    def test_reprocess_preserves_manual_edit(self):
        """Process -> manual edit -> Process again, WITHOUT Apply.

        The edited row keeps its manual text in state and the editor and its
        llm_suggestion refreshes; an untouched row receives a refreshed
        suggestion. First and second inferences use clearly distinct mocks.
        """
        app = self.load_doc(self.run_app())

        calls = {"n": 0}

        def counting_batch(self, records, prompt, progress=None):
            calls["n"] += 1
            prefix = "MT1" if calls["n"] == 1 else "MT2"
            result = llm.BatchResult()
            for record in records:
                result.suggestions[record.key] = "%s %s" % (
                    prefix, record.source_text)
            result.requests = 1
            result.elapsed = 0.01
            result.raw_responses = [json.dumps(
                {"segments": [{"id": r.key, "target": result.suggestions[r.key]}
                              for r in records]}, ensure_ascii=False)]
            return result

        original = llm.LLMClient.translate_batch
        llm.LLMClient.translate_batch = counting_batch
        try:
            self.button(app, "Process Current Batch").click().run()
            proj = app.session_state.project
            target, neighbour = proj.batch_records(0)[0], proj.batch_records(0)[1]
            self.assertTrue(
                proj.segments[target.key].llm_suggestion.startswith("MT1 "))

            self.row_text(app, target.key).set_value("meine Fassung").run()
            self.button(app, "Process Current Batch").click().run()
        finally:
            llm.LLMClient.translate_batch = original
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])

        proj = app.session_state.project
        self.assertEqual(proj.segments[target.key].user_final, "meine Fassung")
        self.assertEqual(app.session_state[self.tkey(app, target.key)],
                         "meine Fassung")
        self.assertTrue(
            proj.segments[target.key].llm_suggestion.startswith("MT2 "))
        self.assertTrue(proj.segments[target.key].edited_by_human)
        self.assertTrue(
            proj.segments[neighbour.key].llm_suggestion.startswith("MT2 "))
        self.assertTrue(
            proj.segments[neighbour.key].user_final.startswith("MT2 "))
        self.assertFalse(proj.segments[neighbour.key].edited_by_human)

    def test_global_directive_persists_and_reaches_the_prompt(self):
        app = self.load_doc(self.run_app())
        box = next(t for t in app.text_area
                   if t.label.startswith("Global Batch Directive"))
        box.set_value("Это руководство - используйте императив.").run()
        self.assertEqual(
            state_mod.ProjectState.load(self.state_path).global_directive,
            "Это руководство - используйте императив.")

        self.button(app, "Process Current Batch").click().run()
        self.assertIn("OPERATOR DIRECTIVE", LAST_PROMPT[0])
        self.assertIn("Это руководство", LAST_PROMPT[0])

    def test_raw_inspector_shows_the_model_output(self):
        app = self.load_doc(self.run_app())
        self.button(app, "Process Current Batch").click().run()
        raw = app.session_state.last_raw
        self.assertTrue(raw)
        self.assertTrue(any("Raw LLM Response Stream Inspector" in e.label
                            for e in app.expander))

    def test_proposed_rules_are_merged_and_logged(self):
        app = self.load_doc(self.run_app())
        proj = app.session_state.project
        before = proj.hard_rules_text

        applied = proj.merge_proposals(
            ["Не переводить имена функций"], "Тон стал более техническим", 0)
        self.assertEqual(len(applied), 2)
        self.assertIn("Не переводить имена функций", proj.hard_rules_text)
        self.assertIn("Тон стал более техническим", proj.vibe)
        self.assertNotEqual(proj.hard_rules_text, before)
        self.assertEqual(len(proj.rule_log), 2)

        proj.revert_auto_rules()
        self.assertNotIn("Не переводить имена функций", proj.hard_rules_text)
        self.assertEqual(proj.rule_log, [])

    def test_analysis_receives_rules_and_expected_tags(self):
        app = self.load_doc(self.run_app())
        self.button(app, "Process Current Batch").click().run()
        record = app.session_state.project.batch_records(0)[0]
        self.row_text(app, record.key).set_value("Правка").run()
        self.button(app, "Apply Changes & Calculate Diffs").click().run()
        self.button(app, "Analyse my corrections").click().run()

        self.assertTrue(LAST_ANALYSIS_ARGS["rules"])
        self.assertIn(record.segment_id, LAST_ANALYSIS_ARGS["expected"])

    def test_operator_warnings_get_their_own_block(self):
        app = self.load_doc(self.run_app())
        self.button(app, "Process Current Batch").click().run()
        record = app.session_state.project.batch_records(0)[0]
        self.row_text(app, record.key).set_value("Правка").run()
        self.button(app, "Apply Changes & Calculate Diffs").click().run()

        original = llm.LLMClient.analyze_corrections
        llm.LLMClient.analyze_corrections = fake_analyze_with_warning
        try:
            self.button(app, "Analyse my corrections").click().run()
        finally:
            llm.LLMClient.analyze_corrections = original

        errors = [e.value for e in app.error]
        self.assertTrue(any("OPERATOR CORRECTION WARNINGS" in e for e in errors),
                        errors)
        self.assertTrue(any("dropped tag" in e for e in errors))
        # The brief still renders, separately from the warning.
        self.assertTrue(any("shorter sentences" in m.value for m in app.markdown))

    def test_reset_clears_the_project(self):
        # Reset is now scoped to the active document and needs confirmation.
        app = self.load_doc(self.run_app())
        self.button(app.sidebar, "Reset active document").click().run()
        self.button(app.sidebar, "Confirm reset").click().run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])
        self.assertIsNone(app.session_state.document)
        self.assertEqual(len(app.session_state.project.segments), 0)

    # -------------------------------------------------- document switching

    @staticmethod
    def _write_mqxliff(path: str, original: str, rows) -> str:
        """Write a minimal synthetic MQXLIFF for document-switching tests.

        Rows are (unit_id, source, target) tuples; every row is NotStarted and
        unlocked, so it is translatable under the default filter. Text and
        attribute values are XML-escaped. The file is written UTF-8 under the
        test's own temporary storage.
        """
        from xml.sax.saxutils import escape  # noqa: PLC0415

        def attr(value: str) -> str:
            return escape(value, {'"': "&quot;"})

        units = "\n".join(
            '      <trans-unit id="%s" mq:status="NotStarted" '
            'mq:locked="false">\n'
            '        <source>%s</source>\n'
            '        <target>%s</target>\n'
            '      </trans-unit>' % (attr(unit_id), escape(source), escape(target))
            for (unit_id, source, target) in rows)
        xml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2" '
            'xmlns:mq="http://www.memoq.com/memoq/xliff">\n'
            '  <file original="%s" source-language="en-US" '
            'target-language="de-DE" datatype="x-docx">\n'
            "    <header>\n      <mq:hiddentexts />\n    </header>\n"
            "    <body>\n%s\n    </body>\n  </file>\n</xliff>\n"
            % (attr(original), units))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(xml)
        return path

    def load_path(self, app: AppTest, path: str) -> AppTest:
        """Load a document from an arbitrary path via the sidebar loader.

        Unlike :meth:`load_doc` it does not touch project state first, so a
        document switch is driven purely by the real UI path.
        """
        app.sidebar.text_input(key="disk_path").set_value(path).run()
        self.button(app.sidebar, "Load").click().run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])
        self.assertIsNotNone(app.session_state.document)
        return app

    def _apply_edit(self, app: AppTest, key: str, text: str, comment: str) -> None:
        """Type a translation and a comment into a row, then Apply."""
        self.row_text(app, key).set_value(text).run()
        app.text_area(key="note_%s" % key).set_value(comment).run()
        self.button(app, "Apply Changes & Calculate Diffs").click().run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])

    def _check_row(self, app: AppTest, phase: str, key: str, user_final: str,
                   user_comment: str, edited: bool) -> None:
        """Assert a row's saved record and both of its widgets, field by field.

        Each field is its own subTest so one failure does not hide the rest.
        The editor and note widgets seed from user_final / user_comment.
        """
        record = app.session_state.project.segments.get(key)
        with self.subTest(phase=phase, field="record-present"):
            self.assertIsNotNone(record)
        if record is not None:
            with self.subTest(phase=phase, field="user_final"):
                self.assertEqual(record.user_final, user_final)
            with self.subTest(phase=phase, field="user_comment"):
                self.assertEqual(record.user_comment, user_comment)
            with self.subTest(phase=phase, field="edited_by_human"):
                self.assertEqual(record.edited_by_human, edited)
        with self.subTest(phase=phase, field="editor"):
            self.assertEqual(self.row_text(app, key).value, user_final)
        with self.subTest(phase=phase, field="note-widget"):
            self.assertEqual(app.text_area(key="note_%s" % key).value,
                             user_comment)

    def _check_loaded(self, app: AppTest, phase: str, source_file: str,
                      expected: dict) -> None:
        """Prove which document is loaded, independently of the edited row.

        ``expected`` maps segment key -> (source_text, mt_target).
        """
        proj = app.session_state.project
        with self.subTest(phase=phase, field="source_file"):
            self.assertEqual(proj.source_file, source_file)
        for seg_key, (source_text, mt_target) in expected.items():
            record = proj.segments.get(seg_key)
            with self.subTest(phase=phase, field="source_text", key=seg_key):
                self.assertIsNotNone(record)
                self.assertEqual(record.source_text, source_text)
            with self.subTest(phase=phase, field="mt_target", key=seg_key):
                self.assertIsNotNone(record)
                self.assertEqual(record.mt_target, mt_target)

    def test_document_switch_does_not_leak_and_reopen(self):
        """Switching A -> B -> A must isolate B from A and restore A's work.

        A is the sandbox copy of the shipped fixture; B is a distinct document
        under a different basename whose keys deliberately overlap A's. This is
        a document-switch UI test, not a separate-process restart.
        """
        a_manual = "A-MANUAL-1"
        a_comment = "A-Kommentar-1"
        a_path = os.path.join(self.app_dir, "tests", "fixtures", "sample.mqxliff")
        self.assertTrue(os.path.isfile(a_path), a_path)
        b_rows = [("1", "Other document first row.", "Other target one."),
                  ("2", "Other document second row.", "Other target two.")]
        b_path = self._write_mqxliff(
            os.path.join(self.tmp, "other.mqxliff"), "other.docx", b_rows)
        b_expected = {"1#0": (b_rows[0][1], b_rows[0][2]),
                      "2#1": (b_rows[1][1], b_rows[1][2])}
        key = "1#0"

        # 1-3. Load A, apply a distinctive edit, confirm it is saved.
        app = self.run_app()
        app.session_state.project.batch_size = 4
        self.load_path(app, a_path)
        a_source = app.session_state.project.segments[key].source_text
        a_mt = app.session_state.project.segments[key].mt_target
        self._apply_edit(app, key, a_manual, a_comment)
        self._check_row(app, "A-saved", key, a_manual, a_comment, True)

        # 4-5. Load a distinct B; prove it is B, and that it inherits nothing:
        # an untouched row's editor and note widgets are empty.
        self.load_path(app, b_path)
        self._check_loaded(app, "B-loaded", "other.mqxliff", b_expected)
        self._check_row(app, "B-isolation", key, "", "", False)

        # 6-7. Reload A; its saved work must return in state and both widgets.
        self.load_path(app, a_path)
        self._check_loaded(app, "A-reloaded", "sample.mqxliff",
                           {key: (a_source, a_mt)})
        self._check_row(app, "A-restored", key, a_manual, a_comment, True)

    def test_same_basename_documents_do_not_share_saved_edits(self):
        """Two distinct documents that share a basename must not share edits.

        The shared segment keeps the same key AND source text across A and B so
        the source-change reset cannot mask the filename-identity hazard; the
        target text and a second segment differ to keep the documents distinct.
        Returning to A must then restore A's own work.
        """
        a_manual = "A-MANUAL-2"
        a_comment = "A-Kommentar-2"
        shared_source = "Shared source row."
        a_path = os.path.join(self.tmp, "dir_a", "document.mqxliff")
        b_path = os.path.join(self.tmp, "dir_b", "document.mqxliff")
        self._write_mqxliff(a_path, "a.docx",
                            [("1", shared_source, "A-shared-target."),
                             ("2", "A-only source row.", "A-only-target.")])
        self._write_mqxliff(b_path, "b.docx",
                            [("1", shared_source, "B-shared-target."),
                             ("2", "B-only source row.", "B-only-target.")])
        a_expected = {"1#0": (shared_source, "A-shared-target."),
                      "2#1": ("A-only source row.", "A-only-target.")}
        b_expected = {"1#0": (shared_source, "B-shared-target."),
                      "2#1": ("B-only source row.", "B-only-target.")}
        key = "1#0"

        # 1-3. Load A, apply a distinctive edit, confirm it is saved.
        app = self.run_app()
        app.session_state.project.batch_size = 4
        self.load_path(app, a_path)
        self._check_loaded(app, "A-loaded", "document.mqxliff", a_expected)
        self._apply_edit(app, key, a_manual, a_comment)
        self._check_row(app, "A-saved", key, a_manual, a_comment, True)

        # 4-5. Load B from a different full path but the SAME basename; prove
        # it is B independently of the shared key's saved work.
        self.load_path(app, b_path)
        self._check_loaded(app, "B-loaded", "document.mqxliff", b_expected)

        # 6-7. B must not inherit A's edits in state or in either widget.
        self._check_row(app, "B-isolation", key, "", "", False)

        # 8-9. Return to A; A's own work must be restored.
        self.load_path(app, a_path)
        self._check_loaded(app, "A-reloaded", "document.mqxliff", a_expected)
        self._check_row(app, "A-restored", key, a_manual, a_comment, True)

    # --------------------------------------- multi-document integration

    KEY = "1#0"

    def _sandbox_a(self) -> str:
        path = os.path.join(self.app_dir, "tests", "fixtures", "sample.mqxliff")
        self.assertTrue(os.path.isfile(path), path)
        return path

    def _other_b(self) -> str:
        return self._write_mqxliff(
            os.path.join(self.tmp, "other.mqxliff"), "other.docx",
            [("1", "Other document first row.", "Other target one."),
             ("2", "Other document second row.", "Other target two.")])

    @staticmethod
    def _fingerprint(path: str) -> str:
        from memoq_harness import mqxliff  # noqa: PLC0415
        return mqxliff.MqxliffDocument.from_path(path).source_fingerprint()

    def _raw_state(self) -> dict:
        with open(self.state_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def _a_and_b_with_work(self):
        """A and B both edited and applied; B is left active."""
        a_path, b_path = self._sandbox_a(), self._other_b()
        app = self.run_app()
        app.session_state.project.batch_size = 4
        self.load_path(app, a_path)
        self._apply_edit(app, self.KEY, "A-MANUAL", "A-note")
        self.load_path(app, b_path)
        self._apply_edit(app, self.KEY, "B-MANUAL", "B-note")
        self._check_row(app, "B-saved", self.KEY, "B-MANUAL", "B-note", True)
        return app, a_path, b_path

    def test_new_session_keeps_b_active_and_reload_restores_a(self):
        """A -> B -> fresh AppTest session (not a real process restart)."""
        _, a_path, b_path = self._a_and_b_with_work()
        fresh = self.run_app()
        proj = fresh.session_state.project
        self.assertIsNone(fresh.session_state.document)
        with self.subTest(field="active-document"):
            self.assertEqual(proj.document_id, self._fingerprint(b_path))
        with self.subTest(field="active-source-file"):
            self.assertEqual(proj.source_file, "other.mqxliff")
        with self.subTest(field="active-b-work"):
            self.assertEqual(proj.segments[self.KEY].user_final, "B-MANUAL")

        self.load_path(fresh, a_path)
        self._check_row(fresh, "A-after-new-session", self.KEY,
                        "A-MANUAL", "A-note", True)
        with self.subTest(field="disk-path-kept"):
            self.assertEqual(fresh.session_state.project.source_path,
                             os.path.abspath(a_path))
        self.load_path(fresh, b_path)
        self._check_row(fresh, "B-after-new-session", self.KEY,
                        "B-MANUAL", "B-note", True)

    def test_renamed_document_restores_its_state(self):
        a_path = self._sandbox_a()
        app = self.run_app()
        app.session_state.project.batch_size = 4
        self.load_path(app, a_path)
        self._apply_edit(app, self.KEY, "A-MANUAL", "A-note")
        renamed = os.path.join(self.tmp, "moved", "renamed.mqxliff")
        os.makedirs(os.path.dirname(renamed))
        shutil.copyfile(a_path, renamed)

        self.load_path(app, renamed)
        proj = app.session_state.project
        self.assertEqual(proj.source_file, "renamed.mqxliff")
        self.assertEqual(proj.source_path, os.path.abspath(renamed))
        self.assertEqual(proj.document_id, self._fingerprint(a_path))
        self.assertEqual(proj.document_ids, [proj.document_id])
        self._check_row(app, "renamed", self.KEY, "A-MANUAL", "A-note", True)

    def test_reset_active_document_keeps_other_documents_and_globals(self):
        app, a_path, b_path = self._a_and_b_with_work()
        app.session_state.project.vibe = "GLOBAL-VIBE"
        self.load_path(app, a_path)
        self._check_row(app, "A-before-reset", self.KEY, "A-MANUAL", "A-note", True)

        self.button(app.sidebar, "Reset active document").click().run()
        self.button(app.sidebar, "Confirm reset").click().run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])
        self.assertIsNone(app.session_state.document)
        self.assertEqual(app.session_state.project.segments, {})

        raw = self._raw_state()
        fp_a, fp_b = self._fingerprint(a_path), self._fingerprint(b_path)
        with self.subTest(field="a-cleared"):
            self.assertEqual(raw["documents"][fp_a]["segments"], {})
            self.assertEqual(raw["documents"][fp_a]["corrections"], [])
        with self.subTest(field="b-kept"):
            self.assertEqual(
                raw["documents"][fp_b]["segments"][self.KEY]["user_final"], "B-MANUAL")
        with self.subTest(field="global-kept"):
            self.assertEqual(raw["vibe"], "GLOBAL-VIBE")

        fresh = self.run_app()
        self.load_path(fresh, b_path)
        self._check_row(fresh, "B-after-reset", self.KEY, "B-MANUAL", "B-note", True)

    def test_cancel_reset_changes_nothing(self):
        app = self.run_app()
        app.session_state.project.batch_size = 4
        self.load_path(app, self._sandbox_a())
        self._apply_edit(app, self.KEY, "A-MANUAL", "A-note")
        with open(self.state_path, "rb") as handle:
            before = handle.read()

        self.button(app.sidebar, "Reset active document").click().run()
        self.button(app.sidebar, "Cancel").click().run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])
        self.assertIsNotNone(app.session_state.document)
        self._check_row(app, "after-cancel", self.KEY, "A-MANUAL", "A-note", True)
        with open(self.state_path, "rb") as handle:
            self.assertEqual(handle.read(), before)
        self.assertFalse(self.button(app.sidebar, "Reset active document").disabled)

    def test_confirmed_reset_does_not_reload_and_allows_reopening(self):
        app = self.run_app()
        app.session_state.project.batch_size = 4
        self.load_path(app, self._sandbox_a())
        self._apply_edit(app, self.KEY, "A-MANUAL", "A-note")
        self.button(app.sidebar, "Reset active document").click().run()
        self.button(app.sidebar, "Confirm reset").click().run()
        app.run()
        app.run()
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])
        self.assertIsNone(app.session_state.document)
        self.assertEqual(app.session_state.project.segments, {})
        self.assertTrue(self.button(app.sidebar, "Reset active document").disabled)

        # Deliberately reopening the same file works and starts clean.
        self.button(app.sidebar, "Load").click().run()
        self.assertIsNotNone(app.session_state.document)
        self._check_row(app, "reopened", self.KEY, "", "", False)

    def test_failed_reset_save_keeps_active_work_and_editor(self):
        app = self.run_app()
        app.session_state.project.batch_size = 4
        self.load_path(app, self._sandbox_a())
        self._apply_edit(app, self.KEY, "A-MANUAL", "A-note")
        with open(self.state_path, "rb") as handle:
            before = handle.read()
        self.button(app.sidebar, "Reset active document").click().run()

        original = state_mod.ProjectState.save

        def failing_save(proj):
            raise OSError("simulated: disk not writable")

        state_mod.ProjectState.save = failing_save
        try:
            self.button(app.sidebar, "Confirm reset").click().run()
        finally:
            state_mod.ProjectState.save = original
        self.assertEqual(app.exception, [], [str(e) for e in app.exception])
        self.assertIsNotNone(app.session_state.document)
        self._check_row(app, "after-failed-reset", self.KEY, "A-MANUAL", "A-note", True)
        self.assertTrue(any("simulated: disk not writable" in e.value for e in app.error),
                        [e.value for e in app.error])
        self.assertFalse(any("Cleared" in s.value for s in app.success))
        with open(self.state_path, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_legacy_work_is_retained_not_attached_and_warned(self):
        legacy = {
            "version": 4, "source_file": "sample.mqxliff", "order": ["1#0"],
            "segments": {"1#0": {
                "key": "1#0", "segment_id": "1", "ordinal": 0, "batch_index": 0,
                "source_text": "x", "user_final": "LEGACY-WORK",
                "edited_by_human": True}},
        }
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump(legacy, handle)
        app = self.run_app()
        app.session_state.project.batch_size = 4
        self.load_path(app, self._sandbox_a())
        self._check_row(app, "not-attached", self.KEY, "", "", False)
        self.assertTrue(any("NOT attached" in w.value for w in app.warning),
                        [w.value for w in app.warning])
        raw = self._raw_state()
        self.assertEqual(
            raw["documents"]["legacy"]["segments"]["1#0"]["user_final"], "LEGACY-WORK")
        self.assertTrue(os.path.exists(self.state_path + ".v4.bak"))

    def test_upload_change_detection_helper(self):
        """Unit test of the uploader's change detection, NOT end-to-end
        uploader coverage: AppTest cannot drive st.file_uploader."""
        import importlib.util  # noqa: PLC0415
        spec = importlib.util.spec_from_file_location("harness_app_ut", self.app_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        marker, needs = module.upload_marker, module.upload_needs_load

        one = self._write_mqxliff(os.path.join(self.tmp, "u1", "doc.mqxliff"),
                                  "d.docx", [("1", "Same source.", "Target one.")])
        two = self._write_mqxliff(os.path.join(self.tmp, "u2", "doc.mqxliff"),
                                  "d.docx", [("1", "Same source.", "Target two.")])
        # Target-only change: same fingerprint, still a new upload to load.
        self.assertEqual(self._fingerprint(one), self._fingerprint(two))
        with open(one, "rb") as h1, open(two, "rb") as h2:
            data_one, data_two = h1.read(), h2.read()
        m1 = marker("doc.mqxliff", data_one)
        m2 = marker("doc.mqxliff", data_two)
        self.assertNotEqual(m1, m2)  # same basename, different files
        self.assertEqual(m1, marker("doc.mqxliff", data_one))
        self.assertNotEqual(marker("a.mqxliff", b"x"), marker("b.mqxliff", b"x"))
        self.assertTrue(needs(m1, None))
        self.assertFalse(needs(m1, m1))      # a plain rerun does not reload
        self.assertTrue(needs(m2, m1))
        self.assertFalse(needs(None, m1))    # empty uploader loads nothing


if __name__ == "__main__":
    unittest.main(verbosity=2)
