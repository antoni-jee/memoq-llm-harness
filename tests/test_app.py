"""UI-level tests driven by Streamlit's AppTest harness.

These execute the real ``app.py`` script headlessly - no browser, no server -
so widget wiring, session-state handling and button callbacks are all covered.
The network call is stubbed, so nothing here needs a live endpoint.

Run with:  python tests/test_app.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

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
        app = self.load_doc(self.run_app())
        self.button(app.sidebar, "Reset project state").click().run()
        self.assertIsNone(app.session_state.document)
        self.assertEqual(len(app.session_state.project.segments), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
