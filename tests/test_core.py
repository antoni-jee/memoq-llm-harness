"""Core correctness tests: tag round-tripping, filtering, batching, feedback.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memoq_harness import (  # noqa: E402
    defaults, diffing, llm, mqxliff, prompt_templates, prompts, rtf, state, tags,
)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "sample.mqxliff")


def load_doc() -> mqxliff.MqxliffDocument:
    doc = mqxliff.MqxliffDocument.from_path(FIXTURE)
    doc.parse_segments()
    return doc


class TestTagIsolation(unittest.TestCase):
    def test_paired_tag_becomes_single_placeholder(self):
        inner = 'Click <bpt id="1">&lt;b&gt;</bpt>Save<ept id="1">&lt;/b&gt;</ept> now.'
        tagged = tags.isolate(inner)
        self.assertEqual(tagged.placeholder_text, "Click {1}Save{2} now.")
        self.assertEqual(len(tagged.tags), 2)

    def test_restore_is_exact_for_unmodified_text(self):
        inner = 'Click <bpt id="1">&lt;b&gt;</bpt>Save<ept id="1">&lt;/b&gt;</ept> now.'
        tagged = tags.isolate(inner)
        restored, report = tags.restore(tagged.placeholder_text, tagged)
        self.assertEqual(restored, inner)
        self.assertTrue(report.ok)

    def test_reordered_tags_survive(self):
        inner = '<bpt id="1">&lt;b&gt;</bpt>Save<ept id="1">&lt;/b&gt;</ept> the file'
        tagged = tags.isolate(inner)
        moved = "die Datei {1}speichern{2}"
        restored, report = tags.restore(moved, tagged)
        self.assertTrue(report.ok)
        self.assertEqual(
            restored, 'die Datei <bpt id="1">&lt;b&gt;</bpt>speichern<ept id="1">&lt;/b&gt;</ept>'
        )

    def test_literal_braces_in_text_switch_scheme(self):
        tagged = tags.isolate('Use {1} literally <ph id="2">X</ph>')
        self.assertNotEqual(tagged.scheme, "brace")
        restored, report = tags.restore(tagged.placeholder_text, tagged)
        self.assertTrue(report.ok)
        self.assertEqual(restored, 'Use {1} literally <ph id="2">X</ph>')

    def test_numeric_entity_is_not_double_escaped(self):
        tagged = tags.isolate("a&#160;b &amp; c")
        self.assertEqual(tagged.placeholder_text, "a\u00a0b & c")
        restored, _ = tags.restore(tagged.placeholder_text, tagged)
        self.assertEqual(restored, "a\u00a0b &amp; c")

    def test_double_escaped_entity_resolves_once(self):
        self.assertEqual(tags.unescape_text("&amp;lt;"), "&lt;")

    def test_dropped_tag_is_reported_and_reattached(self):
        tagged = tags.isolate('A <ph id="1">X</ph> B')
        restored, report = tags.restore("A B", tagged)
        self.assertFalse(report.ok)
        self.assertEqual(report.missing, [1])
        self.assertIn('<ph id="1">X</ph>', restored)

    def test_invented_tag_is_dropped_and_reported(self):
        tagged = tags.isolate("plain text")
        restored, report = tags.restore("plain {7} text", tagged)
        self.assertEqual(report.unknown, [7])
        self.assertEqual(restored, "plain  text")

    def test_model_output_is_escaped(self):
        tagged = tags.isolate("hello")
        restored, _ = tags.restore("Tom & Jerry <b>", tagged)
        self.assertEqual(restored, "Tom &amp; Jerry &lt;b&gt;")


class TestSharedNumbering(unittest.TestCase):
    def test_identical_tags_share_a_number(self):
        source, target, lookup = tags.isolate_pair(
            'A <ph id="1">X</ph> B', 'C <ph id="1">X</ph> D'
        )
        self.assertEqual(source.placeholder_text, "A {1} B")
        self.assertEqual(target.placeholder_text, "C {1} D")
        self.assertEqual(len(lookup.tags), 1)

    def test_target_only_tag_gets_a_fresh_number(self):
        source, target, lookup = tags.isolate_pair(
            'A <ph id="1">X</ph>', 'B <ph id="1">X</ph> <ph id="9">Z</ph>'
        )
        self.assertEqual(source.placeholder_text, "A {1}")
        self.assertEqual(target.placeholder_text, "B {1} {2}")
        self.assertEqual(lookup.tags[2], '<ph id="9">Z</ph>')

    def test_scheme_is_shared_across_both_sides(self):
        source, target, _ = tags.isolate_pair("literal {1} here", 'x <ph id="1">X</ph>')
        self.assertEqual(source.scheme, target.scheme)
        self.assertNotEqual(source.scheme, "brace")


class TestTagRetargeting(unittest.TestCase):
    """The target owns the tags, so memoQ's localised attributes survive."""

    SRC = ('Click <ph id="1">&lt;a href="/en/articles/123"&gt;</ph>here'
           '<ph id="2">&lt;/a&gt;</ph>.')
    TGT = ('Нажмите <ph id="1">&lt;a href="/ru/articles/123"&gt;</ph>здесь'
           '<ph id="2">&lt;/a&gt;</ph>.')

    def test_target_supplies_the_canonical_xml(self):
        source, target, lookup = tags.isolate_pair(self.SRC, self.TGT)
        self.assertIn("/ru/articles/123", lookup.tags[1])
        self.assertNotIn("/en/", lookup.tags[1])
        # Both sides still show the same placeholder numbers.
        self.assertEqual(source.placeholder_text, "Click {1}here{2}.")
        self.assertEqual(target.placeholder_text, "Нажмите {1}здесь{2}.")

    def test_localised_link_survives_a_round_trip(self):
        _, target, lookup = tags.isolate_pair(self.SRC, self.TGT)
        restored, report = tags.restore(
            target.placeholder_text, lookup, expected=sorted(target.tags))
        self.assertTrue(report.ok)
        self.assertIn("/ru/articles/123", restored)
        self.assertNotIn("/en/articles/123", restored)

    def test_source_canonical_mode_reverts_the_link(self):
        """Documents the old behaviour this change exists to fix."""
        source, target, lookup = tags.isolate_pair(
            self.SRC, self.TGT, prefer=tags.TAG_SOURCE_SOURCE)
        restored, _ = tags.restore(
            target.placeholder_text, lookup, expected=sorted(source.tags))
        self.assertIn("/en/articles/123", restored)

    def test_empty_target_falls_back_to_source(self):
        source, target, lookup = tags.isolate_pair(self.SRC, "")
        self.assertEqual(source.placeholder_text, "Click {1}here{2}.")
        self.assertEqual(len(lookup.tags), 2)
        self.assertIn("/en/", lookup.tags[1])
        self.assertEqual(target.placeholder_text, "")

    def test_target_without_tags_falls_back_to_source(self):
        source, _, lookup = tags.isolate_pair(self.SRC, "Просто текст.")
        self.assertEqual(source.placeholder_text, "Click {1}here{2}.")
        self.assertIn("/en/", lookup.tags[1])

    def test_identical_tags_still_share_numbers(self):
        source, target, lookup = tags.isolate_pair(
            'A <ph id="1">X</ph> B', 'C <ph id="1">X</ph> D')
        self.assertEqual(source.placeholder_text, "A {1} B")
        self.assertEqual(target.placeholder_text, "C {1} D")
        self.assertEqual(len(lookup.tags), 1)

    def test_source_only_extra_tag_gets_its_own_number(self):
        source, target, lookup = tags.isolate_pair(
            'A <ph id="1">X</ph> B <ph id="9">Z</ph>', 'C <ph id="1">X</ph> D')
        self.assertEqual(target.placeholder_text, "C {1} D")
        self.assertEqual(source.placeholder_text, "A {1} B {2}")
        self.assertEqual(lookup.tags[2], '<ph id="9">Z</ph>')

    def test_positional_mapping_when_attributes_differ(self):
        source, target, _ = tags.isolate_pair(
            '<ph id="1">&lt;a href="/en/a"&gt;</ph>one'
            '<ph id="2">&lt;a href="/en/b"&gt;</ph>two',
            '<ph id="1">&lt;a href="/ru/a"&gt;</ph>один'
            '<ph id="2">&lt;a href="/ru/b"&gt;</ph>два',
        )
        self.assertEqual(source.placeholder_text, "{1}one{2}two")
        self.assertEqual(target.placeholder_text, "{1}один{2}два")


class TestTagVariants(unittest.TestCase):
    """Both tag ecosystems stay reachable when the MT localises a tag."""

    SRC = 'See <ph id="1">&lt;a href="/en/a"&gt;</ph>it.'
    TGT = 'См. <ph id="1">&lt;a href="/ru/a"&gt;</ph>это.'

    def test_source_variant_is_kept_under_its_own_number(self):
        source, target, lookup = tags.isolate_pair(self.SRC, self.TGT)
        # Canonical {1} is the target's localised XML...
        self.assertIn("/ru/a", lookup.tags[1])
        # ...and the source's own version survives as a second number.
        variant = lookup.variants[1]
        self.assertIn("/en/a", lookup.tags[variant])
        self.assertNotEqual(variant, 1)
        # The prompt text still uses one consistent number on both sides.
        self.assertEqual(source.placeholder_text, "See {1}it.")
        self.assertEqual(target.placeholder_text, "См. {1}это.")

    def test_identical_tags_record_no_variant(self):
        _, _, lookup = tags.isolate_pair(
            'A <ph id="1">X</ph>', 'Б <ph id="1">X</ph>')
        self.assertEqual(lookup.variants, {})
        self.assertEqual(len(lookup.tags), 1)

    def test_segment_side_tags_expose_both_ecosystems(self):
        doc = mqxliff.MqxliffDocument(
            SAMPLE_WITH_LOCALISED_LINK.encode("utf-8"), "x.mqxliff")
        doc.parse_segments()
        seg = doc.translatable_segments()[0]

        src = dict(seg.side_tags(tags.TAG_SOURCE_SOURCE))
        tgt = dict(seg.side_tags(tags.TAG_SOURCE_TARGET))
        self.assertTrue(any("/en/" in x for x in src.values()))
        self.assertTrue(any("/ru/" in x for x in tgt.values()))
        # Distinct XML means distinct numbers - injecting one cannot yield the
        # other by accident.
        self.assertFalse(set(src) & set(tgt))
        self.assertEqual(len(seg.injectable_tags()), len(src) + len(tgt))

    def test_shared_tag_appears_in_both_tabs_under_one_number(self):
        doc = load_doc()
        seg = next(s for s in doc.segments if s.unit_id == "1")
        src = dict(seg.side_tags(tags.TAG_SOURCE_SOURCE))
        tgt = dict(seg.side_tags(tags.TAG_SOURCE_TARGET))
        self.assertEqual(set(src), set(tgt))  # same tags both sides
        self.assertEqual(len(seg.injectable_tags()), len(tgt))


SAMPLE_WITH_LOCALISED_LINK = """<?xml version="1.0" encoding="utf-8"?>
<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2" \
xmlns:mq="http://www.memoq.com/memoq/xliff">
 <file original="a.docx" source-language="en-US" target-language="ru-RU" \
datatype="x-docx"><body>
  <trans-unit id="1" mq:status="NotStarted" mq:locked="false">
   <source>See <ph id="1">&lt;a href="/en/a"&gt;</ph>it.</source>
   <target>См. <ph id="1">&lt;a href="/ru/a"&gt;</ph>это.</target>
  </trans-unit>
 </body></file>
</xliff>
"""


class TestPrefillBypass(unittest.TestCase):
    """Newer Claude builds reject a prefilled assistant turn with a 400."""

    def test_model_detection(self):
        for model in ("claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5",
                      "claude-5-something", "anthropic/claude-4-6"):
            self.assertTrue(llm._rejects_prefill(model), model)
        for model in ("claude-3-5-sonnet-latest", "claude-3-opus", "gpt-4o"):
            self.assertFalse(llm._rejects_prefill(model), model)

    def _run(self, model):
        captured = {}

        class FakeMessages:
            def create(self, **kwargs):
                captured.update(kwargs)
                return type("R", (), {"content": [
                    type("B", (), {"type": "text", "text": '{"segments": []}'})()]})()

        client = llm.LLMClient.__new__(llm.LLMClient)
        client.config = llm.ClientConfig(
            backend=llm.BACKEND_ANTHROPIC, model=model)
        client._supports_json_mode = None
        client._client = type("C", (), {"messages": FakeMessages()})()
        out = client._chat([{"role": "system", "content": "SYS"},
                            {"role": "user", "content": "U"}], force_json=True)
        return captured, out

    def test_new_models_get_no_prefill_but_a_text_constraint(self):
        captured, out = self._run("claude-sonnet-5")
        roles = [m["role"] for m in captured["messages"]]
        self.assertNotIn("assistant", roles)
        self.assertIn("CRITICAL: Return ONLY a valid, raw JSON object",
                      captured["system"])
        self.assertIn("triple backticks", captured["system"])
        # Nothing is stitched on, so the reply comes back verbatim.
        self.assertEqual(out, '{"segments": []}')

    def test_older_models_keep_the_prefill(self):
        captured, out = self._run("claude-3-5-sonnet-latest")
        self.assertEqual(captured["messages"][-1],
                         {"role": "assistant", "content": "{"})
        self.assertNotIn("CRITICAL: Return ONLY", captured["system"])
        self.assertTrue(out.startswith("{"))

    def test_constraint_is_skipped_when_json_is_not_forced(self):
        captured, _ = self._run("claude-sonnet-5")
        self.assertIn("CRITICAL", captured["system"])

        client = llm.LLMClient.__new__(llm.LLMClient)
        client.config = llm.ClientConfig(
            backend=llm.BACKEND_ANTHROPIC, model="claude-sonnet-5")
        client._supports_json_mode = None
        seen = {}

        class FakeMessages:
            def create(self, **kwargs):
                seen.update(kwargs)
                return type("R", (), {"content": [
                    type("B", (), {"type": "text", "text": "ok"})()]})()

        client._client = type("C", (), {"messages": FakeMessages()})()
        client._chat([{"role": "system", "content": "SYS"},
                      {"role": "user", "content": "U"}], force_json=False)
        self.assertEqual(seen["system"], "SYS")


class TestProposalMerge(unittest.TestCase):
    def setUp(self):
        self.state = state.ProjectState(os.path.join(tempfile.mkdtemp(), "s.json"))

    def test_rules_land_under_a_marked_heading(self):
        applied = self.state.merge_proposals(["Не переводить имена функций"], "", 0)
        self.assertEqual(applied, ["Не переводить имена функций"])
        self.assertIn(state.ProjectState.AUTO_RULES_HEADING, self.state.hard_rules_text)
        self.assertIn("- Не переводить имена функций", self.state.hard_rules_text)

    def test_duplicates_are_ignored(self):
        self.state.merge_proposals(["Правило A"], "", 0)
        applied = self.state.merge_proposals(["Правило A"], "", 1)
        self.assertEqual(applied, [])
        self.assertEqual(self.state.hard_rules_text.count("Правило A"), 1)

    def test_existing_rule_text_is_not_re_added(self):
        first_line = defaults.DEFAULT_HARD_RULES.splitlines()[0]
        self.assertEqual(self.state.merge_proposals([first_line], "", 0), [])

    def test_vibe_note_is_appended_once(self):
        self.state.merge_proposals([], "Тон более технический", 0)
        self.state.merge_proposals([], "Тон более технический", 1)
        self.assertEqual(self.state.vibe.count("Тон более технический"), 1)

    def test_every_change_is_logged(self):
        self.state.merge_proposals(["Правило Б"], "Новый тон", 2)
        kinds = [e["kind"] for e in self.state.rule_log]
        self.assertEqual(sorted(kinds), ["rules", "vibe"])
        self.assertTrue(all(e["batch"] == "3" for e in self.state.rule_log))

    def test_revert_removes_only_the_auto_block(self):
        original = self.state.hard_rules_text
        self.state.merge_proposals(["Правило В"], "", 0)
        self.state.revert_auto_rules()
        self.assertNotIn("Правило В", self.state.hard_rules_text)
        self.assertIn(original.splitlines()[0], self.state.hard_rules_text)
        self.assertEqual(self.state.rule_log, [])

    def test_round_trips_through_state_json(self):
        self.state.merge_proposals(["Правило Г"], "", 0)
        self.state.global_directive = "Императив"
        self.state.save()
        reloaded = state.ProjectState.load(self.state.path)
        self.assertIn("Правило Г", reloaded.hard_rules_text)
        self.assertEqual(reloaded.global_directive, "Императив")
        self.assertEqual(len(reloaded.rule_log), 1)


class TestProposalParsing(unittest.TestCase):
    def test_rules_and_vibe_are_harvested(self):
        result = llm.BatchResult()
        llm._collect_proposals(json.dumps({
            "segments": [], "proposed_rules": ["A", "B"], "proposed_vibe": "V",
        }), result)
        self.assertEqual(result.proposed_rules, ["A", "B"])
        self.assertEqual(result.proposed_vibe, "V")

    def test_a_bare_string_is_accepted(self):
        result = llm.BatchResult()
        llm._collect_proposals('{"proposed_rules": "single"}', result)
        self.assertEqual(result.proposed_rules, ["single"])

    def test_absent_keys_are_fine(self):
        result = llm.BatchResult()
        llm._collect_proposals('{"segments": []}', result)
        self.assertEqual(result.proposed_rules, [])
        self.assertEqual(result.proposed_vibe, "")

    def test_garbage_does_not_raise(self):
        result = llm.BatchResult()
        llm._collect_proposals("not json at all", result)
        self.assertEqual(result.proposed_rules, [])

    def test_raw_text_joins_multiple_requests(self):
        result = llm.BatchResult(raw_responses=["one", "two"])
        joined = result.raw_text()
        self.assertIn("request 1 of 2", joined)
        self.assertIn("two", joined)
        self.assertEqual(llm.BatchResult(raw_responses=["solo"]).raw_text(), "solo")


class TestAnalysisCritique(unittest.TestCase):
    def test_prompt_demands_a_validation_pass(self):
        text = prompts.ANALYSIS_SYSTEM
        self.assertIn("VALIDATE THE HUMAN'S EDITS", text)
        self.assertIn(prompts.OPERATOR_WARNING_HEADING, text)
        self.assertIn("dropped, duplicated, renumbered or invented", text)
        self.assertIn("never infer a style rule from what is plainly a slip", text)
        self.assertIn("ignore rows you flagged", text)

    def test_payload_carries_rules_and_expected_tags(self):
        correction = state.Correction("12", "src", "mt", "human")
        payload = json.loads(prompts.build_analysis_payload(
            [correction], "ПРАВИЛА", {"12": [1, 2]}))
        self.assertEqual(payload["editorial_rules_in_force"], "ПРАВИЛА")
        self.assertEqual(payload["edits"][0]["expected_tags"], [1, 2])

    def test_payload_omits_absent_extras(self):
        payload = json.loads(prompts.build_analysis_payload(
            [state.Correction("12", "s", "m", "h")]))
        self.assertNotIn("editorial_rules_in_force", payload)
        self.assertNotIn("expected_tags", payload["edits"][0])

    def test_split_analysis_separates_warnings_from_brief(self):
        text = ("%s\n- Row 12 dropped {2}.\n\nTRANSLATION BRIEF\n- Shorter."
                % prompts.OPERATOR_WARNING_HEADING)
        warnings, brief = prompts.split_analysis(text)
        self.assertIn("Row 12 dropped", warnings)
        self.assertNotIn("Shorter", warnings)
        self.assertIn("Shorter", brief)

    def test_split_analysis_with_no_warnings(self):
        warnings, brief = prompts.split_analysis("- Just a brief.")
        self.assertEqual(warnings, "")
        self.assertEqual(brief, "- Just a brief.")


class TestGlobalDirective(unittest.TestCase):
    def test_directive_is_framed_as_batch_wide(self):
        prompt = prompts.build_system_prompt(
            "", "", "", extra_instructions="Используйте императив.")
        self.assertIn("OPERATOR DIRECTIVE FOR THIS BATCH", prompt)
        self.assertIn("Используйте императив.", prompt)

    def test_absent_directive_adds_nothing(self):
        self.assertNotIn("OPERATOR DIRECTIVE",
                         prompts.build_system_prompt("", "", ""))


class TestSegmentFiltering(unittest.TestCase):
    def setUp(self):
        self.doc = load_doc()

    def test_total_units_parsed(self):
        self.assertEqual(len(self.doc.segments), 12)

    def test_skips_confirmed_locked_and_empty(self):
        translatable = {s.unit_id for s in self.doc.translatable_segments()}
        self.assertEqual(translatable, {"1", "2", "7", "8", "9", "11", "12"})

    def test_skip_reasons(self):
        reasons = {s.unit_id: s.skip_reason for s in self.doc.segments}
        self.assertEqual(reasons["3"], "confirmed")   # mq:confirmed attribute
        self.assertEqual(reasons["4"], "locked")      # mq:locked attribute
        self.assertEqual(reasons["5"], "confirmed")   # mq:status="Confirmed"
        self.assertEqual(reasons["6"], "confirmed")   # target state="final"
        self.assertEqual(reasons["10"], "empty source")

    def test_filter_toggles_are_respected(self):
        doc = mqxliff.MqxliffDocument.from_path(FIXTURE)
        doc.parse_segments(mqxliff.SegmentFilter(skip_confirmed=False, skip_locked=False))
        self.assertEqual(len(doc.translatable_segments()), 11)  # all but empty source

    def test_stats(self):
        stats = self.doc.stats()
        self.assertEqual(stats["total"], 12)
        self.assertEqual(stats["translatable"], 7)

    def test_tags_carry_no_inherited_namespace_noise(self):
        """lxml repeats inherited xmlns on every detached child; we strip it."""
        for segment in self.doc.segments:
            for _, xml in segment.raw_tags():
                self.assertNotIn("xmlns", xml, xml)

    def test_export_keeps_tags_free_of_namespace_noise(self):
        doc = load_doc()
        seg = next(s for s in doc.segments if s.unit_id == "1")
        doc.apply_translation(seg, seg.target_text)
        body = doc.to_bytes().decode("utf-8")
        self.assertNotIn('<bpt xmlns=', body)
        self.assertNotIn('<ept xmlns=', body)
        # The root still declares them exactly once.
        self.assertEqual(body.count("urn:oasis:names:tc:xliff:document:1.2"), 1)

    def test_source_text_is_placeholder_form(self):
        seg = next(s for s in self.doc.segments if s.unit_id == "1")
        self.assertEqual(seg.source_text, "Click {1}Save{2} to continue.")
        self.assertEqual(seg.target_text, "Klicken Sie {1}Speichern{2}, um fortzufahren.")

    def test_rejects_non_xliff(self):
        with self.assertRaises(mqxliff.MqxliffError):
            mqxliff.MqxliffDocument(b"<html><body>no</body></html>", "x.mqxliff")


class TestExport(unittest.TestCase):
    def test_write_target_and_reparse(self):
        doc = load_doc()
        seg = next(s for s in doc.segments if s.unit_id == "1")
        report = doc.apply_translation(seg, "Waehlen Sie {1}Sichern{2} aus.")
        self.assertTrue(report.ok)

        reparsed = mqxliff.MqxliffDocument(doc.to_bytes(), "out.mqxliff")
        reparsed.parse_segments()
        again = next(s for s in reparsed.segments if s.unit_id == "1")
        self.assertEqual(again.target_text, "Waehlen Sie {1}Sichern{2} aus.")

    def test_creates_missing_target_element(self):
        doc = load_doc()
        seg = next(s for s in doc.segments if s.unit_id == "9")
        doc.apply_translation(seg, "Dieses Segment hat jetzt ein Ziel.")

        reparsed = mqxliff.MqxliffDocument(doc.to_bytes(), "out.mqxliff")
        reparsed.parse_segments()
        again = next(s for s in reparsed.segments if s.unit_id == "9")
        self.assertEqual(again.target_text, "Dieses Segment hat jetzt ein Ziel.")

    def test_untouched_rows_are_preserved_verbatim(self):
        doc = load_doc()
        seg = next(s for s in doc.segments if s.unit_id == "1")
        doc.apply_translation(seg, "Neu {1}Text{2} hier.")

        reparsed = mqxliff.MqxliffDocument(doc.to_bytes(), "out.mqxliff")
        reparsed.parse_segments()
        confirmed = next(s for s in reparsed.segments if s.unit_id == "3")
        self.assertEqual(confirmed.target_text, "Diese Zeile ist bereits bestaetigt.")
        self.assertTrue(confirmed.confirmed)

    def test_nested_formatting_round_trips(self):
        doc = load_doc()
        seg = next(s for s in doc.segments if s.unit_id == "11")
        restored, report = tags.restore(
            seg.target_text, seg.lookup, expected=seg.source.expected_numbers
        )
        self.assertTrue(report.ok, report.summary())

    def test_mark_confirmed_sets_status(self):
        doc = load_doc()
        seg = next(s for s in doc.segments if s.unit_id == "12")
        doc.apply_translation(seg, "Bericht als PDF exportieren.", mark_confirmed=True)

        reparsed = mqxliff.MqxliffDocument(doc.to_bytes(), "out.mqxliff")
        reparsed.parse_segments()
        again = next(s for s in reparsed.segments if s.unit_id == "12")
        self.assertTrue(again.confirmed)

    def test_export_filename_shape(self):
        name = mqxliff.export_filename("report.mqxliff")
        self.assertTrue(name.startswith("report_harness_"))
        self.assertTrue(name.endswith(".mqxliff"))


class TestState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "state.json")
        self.doc = load_doc()
        self.state = state.ProjectState(self.path)
        self.state.batch_size = 3
        self.state.bind_segments(self.doc.translatable_segments(), "sample.mqxliff")

    def test_binding_and_batching(self):
        self.assertEqual(len(self.state.segments), 7)
        self.assertEqual(self.state.batch_count, 3)
        self.assertEqual(len(self.state.batch_keys(0)), 3)
        self.assertEqual(len(self.state.batch_keys(2)), 1)

    def test_rebatch(self):
        self.state.rebatch(100)
        self.assertEqual(self.state.batch_count, 1)

    def test_round_trip_persistence(self):
        self.state.vibe = "Formal, concise."
        self.state.save()
        reloaded = state.ProjectState.load(self.path)
        self.assertEqual(reloaded.vibe, "Formal, concise.")
        self.assertEqual(len(reloaded.segments), 7)
        self.assertEqual(reloaded.batch_size, 3)

    def test_corrupt_state_does_not_raise(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        reloaded = state.ProjectState.load(self.path)
        self.assertEqual(len(reloaded.segments), 0)
        self.assertTrue(os.path.exists(self.path + ".corrupt"))

    def test_suggestions_prefill_user_field(self):
        keys = self.state.batch_keys(0)
        self.state.record_suggestions({keys[0]: "Vorschlag"})
        record = self.state.segments[keys[0]]
        self.assertEqual(record.user_final, "Vorschlag")
        self.assertEqual(record.status, "processed")

    def test_rerun_does_not_clobber_human_edits(self):
        keys = self.state.batch_keys(0)
        self.state.record_suggestions({keys[0]: "erster Vorschlag"})
        self.state.capture_edits({keys[0]: "meine Fassung"})
        self.state.calculate_diffs(0)
        self.state.record_suggestions({keys[0]: "zweiter Vorschlag"})
        self.assertEqual(self.state.segments[keys[0]].user_final, "meine Fassung")

    def test_diff_only_captures_human_changes(self):
        keys = self.state.batch_keys(0)
        self.state.record_suggestions({k: "Maschine" for k in keys})
        self.state.capture_edits({keys[0]: "Maschine", keys[1]: "Mensch"})
        corrections = self.state.calculate_diffs(0)
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0].human_final, "Mensch")

    def test_comment_alone_produces_a_correction(self):
        keys = self.state.batch_keys(0)
        self.state.record_suggestions({keys[0]: "Maschine"})
        self.state.capture_edits({keys[0]: "Maschine"}, {keys[0]: "keep it shorter"})
        corrections = self.state.calculate_diffs(0)
        self.assertEqual(len(corrections), 1)
        self.assertFalse(self.state.segments[keys[0]].edited_by_human)
        self.assertEqual(corrections[0].comment, "keep it shorter")

    def test_reapplying_a_batch_replaces_its_corrections(self):
        keys = self.state.batch_keys(0)
        self.state.record_suggestions({k: "Maschine" for k in keys})
        self.state.capture_edits({keys[0]: "Erste"})
        self.state.calculate_diffs(0)
        self.state.capture_edits({keys[0]: "Zweite"})
        self.state.calculate_diffs(0)
        self.assertEqual(len(self.state.corrections), 1)
        self.assertEqual(self.state.corrections[0].human_final, "Zweite")

    def test_feedback_block_format(self):
        keys = self.state.batch_keys(0)
        self.state.record_suggestions({keys[0]: "Maschine"})
        self.state.capture_edits({keys[0]: "Mensch"})
        self.state.calculate_diffs(0)

        self.state.detailed_feedback = False
        block = self.state.feedback_block(upto_batch=1)
        segment_id = self.state.segments[keys[0]].segment_id
        self.assertEqual(block, '<%s: "Mensch">' % segment_id)

        self.state.detailed_feedback = True
        detailed = self.state.feedback_block(upto_batch=1)
        self.assertIn('<%s: "Mensch">' % segment_id, detailed)
        self.assertIn("rejected:", detailed)

    def test_feedback_excludes_current_and_later_batches(self):
        keys0, keys1 = self.state.batch_keys(0), self.state.batch_keys(1)
        self.state.record_suggestions({keys0[0]: "M", keys1[0]: "M"})
        self.state.capture_edits({keys0[0]: "A", keys1[0]: "B"})
        self.state.calculate_diffs(0)
        self.state.calculate_diffs(1)
        self.assertIn('"A"', self.state.feedback_block(upto_batch=1))
        self.assertNotIn('"B"', self.state.feedback_block(upto_batch=1))
        self.assertIn('"B"', self.state.feedback_block(upto_batch=2))

    def test_rebinding_preserves_human_work(self):
        keys = self.state.batch_keys(0)
        self.state.capture_edits({keys[0]: "behalten"})
        self.state.bind_segments(self.doc.translatable_segments(), "sample.mqxliff")
        self.assertEqual(self.state.segments[keys[0]].user_final, "behalten")

    def test_effective_text_precedence(self):
        record = state.SegmentRecord(
            key="k", segment_id="1", ordinal=0, batch_index=0, mt_target="mt"
        )
        self.assertEqual(record.effective_text(), "mt")
        record.llm_suggestion = "llm"
        self.assertEqual(record.effective_text(), "llm")
        record.user_final = "human"
        self.assertEqual(record.effective_text(), "human")

    def test_unapplied_manual_edit_survives_rerun(self):
        """Generate -> manual edit -> new suggestion, WITHOUT Apply.

        The captured human revision must survive in user_final; the fresh
        suggestion goes to llm_suggestion. This is the unapplied-edit path
        (no calculate_diffs in between).
        """
        keys = self.state.batch_keys(0)
        self.state.record_suggestions({keys[0]: "erster Vorschlag"})
        self.state.capture_edits({keys[0]: "meine Fassung"})
        self.state.record_suggestions({keys[0]: "zweiter Vorschlag"})
        record = self.state.segments[keys[0]]
        self.assertEqual(record.user_final, "meine Fassung")
        self.assertEqual(record.llm_suggestion, "zweiter Vorschlag")
        self.assertTrue(record.edited_by_human)

    def test_comment_does_not_freeze_target_text(self):
        """Same text plus a reviewer comment, then a new suggestion.

        A comment alone must not freeze user_final: it should take the new
        suggestion while the comment is preserved.
        """
        keys = self.state.batch_keys(0)
        self.state.record_suggestions({keys[0]: "erster Vorschlag"})
        self.state.capture_edits({keys[0]: "erster Vorschlag"},
                                 {keys[0]: "zu woertlich"})
        self.state.record_suggestions({keys[0]: "zweiter Vorschlag"})
        record = self.state.segments[keys[0]]
        self.assertEqual(record.user_final, "zweiter Vorschlag")
        self.assertEqual(record.user_comment, "zu woertlich")
        self.assertFalse(record.edited_by_human)


MEMOQ_HEADER = "English,English,English,English,English,Russian,Russian"


def memoq_csv(*rows: str) -> str:
    """Build a memoQ-shaped CSV: header row then positional data rows."""
    return "\n".join((MEMOQ_HEADER,) + rows) + "\n"


class TestTermbaseCsv(unittest.TestCase):
    """memoQ's export is headless and positional: col 0 EN, col 5 RU."""

    def setUp(self):
        self.state = state.ProjectState(os.path.join(tempfile.mkdtemp(), "state.json"))

    def test_reads_columns_zero_and_five(self):
        report = self.state.load_termbase_csv(
            memoq_csv("entropy,,,,,энтропия,", "momentum,,,,,моментум,")
        )
        self.assertEqual(report.loaded, 2)
        self.assertEqual(self.state.termbase[0].source, "entropy")
        self.assertEqual(self.state.termbase[0].target, "энтропия")
        self.assertEqual(self.state.termbase[1].target, "моментум")

    def test_header_row_is_skipped(self):
        self.state.load_termbase_csv(memoq_csv("entropy,,,,,энтропия,"))
        self.assertNotIn("English", [t.source for t in self.state.termbase])
        self.assertEqual(len(self.state.termbase), 1)

    def test_intermediate_synonym_columns_are_ignored(self):
        self.state.load_termbase_csv(
            memoq_csv("Expert Advisor,EA,adviser,,,советник,эксперт")
        )
        entry = self.state.termbase[0]
        self.assertEqual(entry.source, "Expert Advisor")
        self.assertEqual(entry.target, "советник")

    def test_rows_missing_a_side_are_counted_not_crashed(self):
        report = self.state.load_termbase_csv(
            memoq_csv("entropy,,,,,энтропия,", "orphan,,,,,,", ",,,,,сирота,")
        )
        self.assertEqual(report.loaded, 1)
        self.assertEqual(report.incomplete_rows, 2)

    def test_short_rows_do_not_raise(self):
        report = self.state.load_termbase_csv(memoq_csv("too,few,columns"))
        self.assertEqual(report.loaded, 0)
        self.assertEqual(report.incomplete_rows, 1)

    def test_quoted_commas_survive(self):
        self.state.load_termbase_csv(
            memoq_csv('"fast, slow entropy",,,,,"быстрая, медленная энтропия",')
        )
        self.assertEqual(self.state.termbase[0].source, "fast, slow entropy")
        self.assertEqual(self.state.termbase[0].target, "быстрая, медленная энтропия")

    def test_embedded_newline_in_quoted_field_keeps_the_row(self):
        """The classic way naive line-splitting loses rows."""
        report = self.state.load_termbase_csv(
            memoq_csv('"multi\nline term",,,,,"многострочный\nтермин",', "after,,,,,после,")
        )
        self.assertEqual(report.loaded, 2)
        self.assertEqual(self.state.termbase[0].source, "multi line term")
        self.assertEqual(self.state.termbase[1].source, "after")

    def test_crlf_and_cr_line_breaks(self):
        for newline in ("\r\n", "\n"):
            with self.subTest(newline=repr(newline)):
                blob = newline.join([MEMOQ_HEADER, "a,,,,,б,", "c,,,,,д,"]) + newline
                fresh = state.ProjectState(os.path.join(tempfile.mkdtemp(), "s.json"))
                self.assertEqual(fresh.load_termbase_csv(blob).loaded, 2)

    def test_blank_rows_are_skipped(self):
        report = self.state.load_termbase_csv(
            memoq_csv("a,,,,,б,", "", "c,,,,,д,")
        )
        self.assertEqual(report.loaded, 2)
        self.assertEqual(report.blank_rows, 1)

    def test_whitespace_is_trimmed(self):
        self.state.load_termbase_csv(memoq_csv("  entropy  ,,,,,  энтропия  ,"))
        self.assertEqual(self.state.termbase[0].source, "entropy")
        self.assertEqual(self.state.termbase[0].target, "энтропия")

    def test_identical_sources_are_collapsed(self):
        report = self.state.load_termbase_csv(
            memoq_csv("entropy,,,,,энтропия,", "entropy,,,,,энтропия 2,")
        )
        self.assertEqual(report.loaded, 1)
        self.assertEqual(report.duplicate_rows, 1)
        self.assertEqual(self.state.termbase[0].target, "энтропия")  # first wins

    def test_case_distinct_sources_are_both_kept(self):
        """Real termbases use case to separate a UI heading from a verb phrase."""
        report = self.state.load_termbase_csv(memoq_csv(
            "Connect to an Account,,,,,Подключение к счету,",
            "connect to an account,,,,,подключиться к счету,",
        ))
        self.assertEqual(report.loaded, 2)
        self.assertEqual(report.duplicate_rows, 0)
        targets = [t.target for t in self.state.termbase]
        self.assertIn("Подключение к счету", targets)
        self.assertIn("подключиться к счету", targets)

    def test_case_distinct_entries_both_match_a_batch(self):
        self.state.load_termbase_csv(memoq_csv(
            "Connect to an Account,,,,,Подключение к счету,",
            "connect to an account,,,,,подключиться к счету,",
        ))
        hits = self.state.relevant_terms(["Click Connect to an Account here"])
        self.assertEqual(len(hits), 2)

    def test_utf8_bom_is_handled(self):
        blob = ("﻿" + memoq_csv("entropy,,,,,энтропия,")).encode("utf-8")
        report = self.state.load_termbase_csv(blob)
        self.assertEqual(report.loaded, 1)
        self.assertEqual(report.encoding, "utf-8-sig")

    def test_utf16_is_handled(self):
        blob = memoq_csv("entropy,,,,,энтропия,").encode("utf-16")
        report = self.state.load_termbase_csv(blob)
        self.assertEqual(report.loaded, 1)
        self.assertEqual(self.state.termbase[0].target, "энтропия")

    def test_semicolon_delimiter_is_sniffed(self):
        blob = "English;English;English;English;English;Russian;Russian\nentropy;;;;;энтропия;\n"
        report = self.state.load_termbase_csv(blob)
        self.assertEqual(report.delimiter, ";")
        self.assertEqual(report.loaded, 1)

    def test_thousand_row_file_loads_completely(self):
        rows = ["term%04d,,,,,термин%04d," % (i, i) for i in range(1000)]
        report = self.state.load_termbase_csv(memoq_csv(*rows))
        self.assertEqual(report.loaded, 1000)
        self.assertEqual(len(self.state.termbase), 1000)

    def test_custom_columns(self):
        self.state.load_termbase_csv("h,h\nen,ru\n", source_col=0, target_col=1)
        self.assertEqual(self.state.termbase[0].target, "ru")

    def test_export_round_trips_through_the_same_loader(self):
        self.state.load_termbase_csv(memoq_csv("entropy,,,,,энтропия,"))
        other = state.ProjectState(os.path.join(tempfile.mkdtemp(), "s.json"))
        other.load_termbase_csv(self.state.termbase_csv())
        self.assertEqual(other.termbase[0].source, "entropy")
        self.assertEqual(other.termbase[0].target, "энтропия")

    def test_append_mode_keeps_existing(self):
        self.state.load_termbase_csv(memoq_csv("a,,,,,б,"))
        self.state.load_termbase_csv(memoq_csv("c,,,,,д,"), replace=False)
        self.assertEqual([t.source for t in self.state.termbase], ["a", "c"])

    def test_real_memoq_export_parses_completely(self):
        """The operator's actual 1250-row export, when it is present."""
        real = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "MQL term base.csv")
        if not os.path.exists(real):
            self.skipTest("real termbase export not present")

        with open(real, "rb") as handle:
            report = self.state.load_termbase_csv(handle.read())

        self.assertEqual(report.encoding, "utf-8-sig")
        self.assertEqual(report.delimiter, ",")
        self.assertEqual(report.header_rows, 1)
        self.assertEqual(report.incomplete_rows, 0)
        self.assertGreater(report.loaded, 1200)
        # Every row accounted for: loaded + skipped == data rows seen.
        self.assertEqual(
            report.loaded + report.duplicate_rows + report.incomplete_rows
            + report.blank_rows,
            report.data_rows,
        )
        self.assertTrue(all(t.source and t.target for t in self.state.termbase))

        lookup = {t.source: t.target for t in self.state.termbase}
        self.assertEqual(lookup.get("equity"), "эквити")
        self.assertEqual(lookup.get("account index"), "индекс аккаунта")
        # Case-distinct pair must survive intact.
        self.assertEqual(lookup.get("Connect to an Account"), "Подключение к счету")
        self.assertEqual(lookup.get("connect to an account"), "подключиться к счету")

    def test_shipped_sample_termbase_parses(self):
        sample = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config", "termbase.csv")
        with open(sample, "rb") as handle:
            report = self.state.load_termbase_csv(handle.read())
        self.assertGreater(report.loaded, 20)
        self.assertEqual(report.incomplete_rows, 0)
        sources = [t.source for t in self.state.termbase]
        self.assertIn("entropy", sources)
        self.assertEqual(
            next(t.target for t in self.state.termbase if t.source == "entropy"),
            "энтропия",
        )


class TestRelevantTerms(unittest.TestCase):
    def setUp(self):
        self.state = state.ProjectState(os.path.join(tempfile.mkdtemp(), "state.json"))

    def test_filters_by_occurrence(self):
        self.state.load_termbase_csv(
            memoq_csv("entropy,,,,,энтропия,", "zebra,,,,,зебра,")
        )
        hits = self.state.relevant_terms(["The Entropy value rises"])
        self.assertEqual([t.source for t in hits], ["entropy"])

    def test_matching_is_whole_word(self):
        """A substring test would fire 'side' on 'consider'."""
        self.state.load_termbase_csv(memoq_csv("side,,,,,направление,"))
        self.assertEqual(self.state.relevant_terms(["Please consider this"]), [])
        self.assertEqual(len(self.state.relevant_terms(["the long side"])), 1)

    def test_multi_word_terms_match(self):
        self.state.load_termbase_csv(memoq_csv("position sizing,,,,,размер позиции,"))
        self.assertEqual(len(self.state.relevant_terms(["Configure position sizing now"])), 1)

    def test_non_alphanumeric_edges_still_match(self):
        self.state.load_termbase_csv(memoq_csv("C++,,,,,C++,"))
        self.assertEqual(len(self.state.relevant_terms(["written in C++ here"])), 1)

    def test_empty_batch_returns_nothing(self):
        self.state.load_termbase_csv(memoq_csv("a,,,,,б,"))
        self.assertEqual(self.state.relevant_terms([""]), [])

    def test_limit_is_respected(self):
        rows = ["term%03d,,,,,термин%03d," % (i, i) for i in range(200)]
        self.state.load_termbase_csv(memoq_csv(*rows))
        text = " ".join("term%03d" % i for i in range(200))
        self.assertEqual(len(self.state.relevant_terms([text], limit=25)), 25)

    def test_capitalised_ui_labels_do_not_fire_on_prose(self):
        """'To' -> 'Кому' is a UI label; it must not match the word 'to'."""
        self.state.load_termbase_csv(memoq_csv(
            "To,,,,,Кому,", "File,,,,,Файл,", "Type,,,,,Тип,",
        ))
        hits = self.state.relevant_terms(
            ["The advisor writes the curve to a log file of that type."])
        self.assertEqual(hits, [])

    def test_capitalised_terms_still_match_their_own_casing(self):
        self.state.load_termbase_csv(memoq_csv(
            "Expert Advisor,,,,,советник,", "To,,,,,Кому,",
        ))
        hits = self.state.relevant_terms(["Open the Expert Advisor and the To field"])
        self.assertEqual({t.source for t in hits}, {"Expert Advisor", "To"})

    def test_lowercase_terms_stay_case_insensitive(self):
        """So they still match when capitalised at the start of a sentence."""
        self.state.load_termbase_csv(memoq_csv("equity,,,,,эквити,"))
        self.assertEqual(len(self.state.relevant_terms(["Equity is rising."])), 1)

    def test_longer_term_wins_over_the_shorter_one_inside_it(self):
        self.state.load_termbase_csv(memoq_csv(
            "order filling type,,,,,тип исполнения ордера,",
            "filling type,,,,,тип заливки,",
        ))
        hits = self.state.relevant_terms(["Set the order filling type now."])
        self.assertEqual([t.source for t in hits], ["order filling type"])

    def test_subsumed_single_word_is_dropped(self):
        self.state.load_termbase_csv(memoq_csv(
            "magic number,,,,,магический номер,", "magic,,,,,магия,",
        ))
        hits = self.state.relevant_terms(["Set the magic number."])
        self.assertEqual([t.source for t in hits], ["magic number"])

    def test_unrelated_short_term_is_kept(self):
        self.state.load_termbase_csv(memoq_csv(
            "magic number,,,,,магический номер,", "equity,,,,,эквити,",
        ))
        hits = self.state.relevant_terms(["magic number and equity"])
        self.assertEqual(len(hits), 2)

    def test_real_export_filters_prose_sensibly(self):
        real = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "MQL term base.csv")
        if not os.path.exists(real):
            self.skipTest("real termbase export not present")
        with open(real, "rb") as handle:
            self.state.load_termbase_csv(handle.read())

        hits = self.state.relevant_terms([
            "The Expert Advisor writes the equity curve to a log file.",
            "Set the magic number and the order filling type before trading.",
        ])
        sources = {t.source for t in hits}
        self.assertIn("Expert Advisor", sources)
        self.assertIn("equity", sources)
        # UI labels must not leak in from ordinary prose words.
        for noise in ("To", "File", "Type", "Filling", "Magic"):
            self.assertNotIn(noise, sources)
        self.assertLess(len(hits), 8, sorted(sources))


class TestDefaultRuleBlocks(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "state.json")
        self.state = state.ProjectState(self.path)

    def test_fresh_project_starts_with_the_defaults(self):
        self.assertEqual(self.state.hard_rules_text, defaults.DEFAULT_HARD_RULES)
        self.assertEqual(self.state.vibe, defaults.DEFAULT_VIBE)

    def test_defaults_carry_their_cyrillic_intact(self):
        self.assertIn("Правила редактуры", defaults.DEFAULT_HARD_RULES)
        self.assertIn("Главный принцип", defaults.DEFAULT_HARD_RULES)
        self.assertIn("индикатора рыночной энтропии", defaults.DEFAULT_VIBE)
        self.assertIn("Termbase", defaults.DEFAULT_VIBE)

    def test_edits_persist_and_are_not_overwritten_by_defaults(self):
        self.state.hard_rules_text = "Моя редакция."
        self.state.vibe = "Мой стиль."
        self.state.save()
        reloaded = state.ProjectState.load(self.path)
        self.assertEqual(reloaded.hard_rules_text, "Моя редакция.")
        self.assertEqual(reloaded.vibe, "Мой стиль.")

    def test_cleared_blocks_stay_cleared(self):
        """An empty box is a decision, not a missing value."""
        self.state.hard_rules_text = ""
        self.state.vibe = ""
        self.state.save()
        reloaded = state.ProjectState.load(self.path)
        self.assertEqual(reloaded.hard_rules_text, "")
        self.assertEqual(reloaded.vibe, "")

    def test_restore_buttons_reset_to_defaults(self):
        self.state.hard_rules_text = "x"
        self.state.vibe = "y"
        self.state.reset_hard_rules()
        self.state.reset_vibe()
        self.assertEqual(self.state.hard_rules_text, defaults.DEFAULT_HARD_RULES)
        self.assertEqual(self.state.vibe, defaults.DEFAULT_VIBE)

    def test_v2_state_migrates_hard_rules_table_into_the_termbase(self):
        legacy = {
            "version": 2,
            "vibe": "old vibe",
            "hard_rules": [{"source": "entropy", "target": "энтропия", "note": ""}],
        }
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(legacy, handle, ensure_ascii=False)

        migrated = state.ProjectState.load(self.path)
        self.assertEqual([t.source for t in migrated.termbase], ["entropy"])
        self.assertEqual(migrated.vibe, "old vibe")
        # No prose existed in v2, so the rulebook falls back to the default.
        self.assertEqual(migrated.hard_rules_text, defaults.DEFAULT_HARD_RULES)


BS = chr(92)  # a literal backslash, spelled out so no editor can mangle it


def _u(codepoint: int) -> str:
    """An RTF ``\\uN`` escape for one codepoint, built without escapes."""
    return "%su%d " % (BS, codepoint)


class TestRtfImport(unittest.TestCase):
    """The RTF reader that feeds defaults.py and prompt_templates.py."""

    def test_plain_text_passes_through(self):
        self.assertEqual(rtf.rtf_to_text("just text"), "just text")

    def test_control_words_are_dropped(self):
        # The space after a control word is its delimiter and gets consumed, so
        # a literal space needs a second one. That is RTF, not a bug.
        self.assertEqual(
            rtf.rtf_to_text(r"{\rtf1\ansi\b Bold\b0  text}").strip(), "Bold text")

    def test_backslash_newline_is_a_paragraph_break(self):
        """macOS TextEdit writes paragraphs this way instead of \\par."""
        self.assertEqual(
            rtf.rtf_to_text("{\\rtf1\\ansi one\\\ntwo}").strip(), "one\ntwo")

    def test_par_is_a_paragraph_break(self):
        self.assertEqual(
            rtf.rtf_to_text(r"{\rtf1\ansi one\par two}").strip(), "one\ntwo")

    def test_unicode_escapes_decode(self):
        # Built from codepoints so no \\uXXXX escape can be mangled by an
        # editor on the way into this file.
        # _u already emits the delimiting space; joining with another would
        # leave a literal one in the output.
        body = "".join(_u(c) for c in (1042, 1072, 1081, 1073))
        source = "{" + BS + "rtf1" + BS + "ansi" + BS + "uc0 " + body + "}"
        self.assertEqual(rtf.rtf_to_text(source).strip(), "Вайб")

    def test_uc_skip_count_is_honoured(self):
        # \uc1 means one substitution character follows each \uN and is eaten.
        source = ("{" + BS + "rtf1" + BS + "ansi" + BS + "uc1 "
                  + _u(1042) + "?" + _u(1072) + "?}")
        self.assertEqual(rtf.rtf_to_text(source).strip(), "Ва")

    def test_hex_escapes_use_the_declared_codepage(self):
        self.assertEqual(
            rtf.rtf_to_text(r"{\rtf1\ansi\ansicpg1251 \'c2\'e0\'e9\'e1}").strip(),
            "Вайб")

    def test_metadata_destinations_are_skipped(self):
        source = (r"{\rtf1\ansi{\fonttbl\f0\froman Times;}"
                  r"{\colortbl;\red255\green0\blue0;}body}")
        self.assertEqual(rtf.rtf_to_text(source).strip(), "body")

    def test_star_destination_is_skipped_not_emitted(self):
        """A leaked \\* used to prefix every document with '*;;;'."""
        source = r"{\rtf1\ansi{\*\expandedcolortbl;;\cssrgb\c0\c0\c0;}body}"
        self.assertEqual(rtf.rtf_to_text(source).strip(), "body")

    def test_escaped_literals(self):
        self.assertEqual(
            rtf.rtf_to_text(r"{\rtf1\ansi a\{b\}c\\d}").strip(), "a{b}c\\d")

    def test_looks_like_rtf(self):
        self.assertTrue(rtf.looks_like_rtf(b"{\\rtf1\\ansi"))
        self.assertFalse(rtf.looks_like_rtf(b"plain text"))

    def test_real_sources_produce_the_shipped_constants(self):
        """defaults.py must still match the RTF it was generated from."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pairs = [
            (("rules",), defaults.DEFAULT_HARD_RULES),
            (("vibe",), defaults.DEFAULT_VIBE),
        ]
        for fragments, constant in pairs:
            path = rtf.find_rtf(root, *fragments)
            if not path:
                self.skipTest("RTF sources not present")
            with self.subTest(source=os.path.basename(path)):
                self.assertEqual(rtf.read_rtf_file(path), constant)

    def test_generated_constants_carry_their_cyrillic(self):
        self.assertIn("Правила редактуры", defaults.DEFAULT_HARD_RULES)
        self.assertIn("Главный принцип", defaults.DEFAULT_HARD_RULES)
        self.assertIn("Вайб", defaults.DEFAULT_VIBE)
        self.assertIn("Termbase", defaults.DEFAULT_VIBE)
        for text in (defaults.DEFAULT_HARD_RULES, defaults.DEFAULT_VIBE):
            self.assertNotIn("\\u", text)      # no leaked escapes
            self.assertNotIn("\\par", text)    # no leaked control words


class TestPromptTemplates(unittest.TestCase):
    def test_four_stages_are_present(self):
        self.assertEqual([s.key for s in prompt_templates.STAGES],
                         ["init", "mt1", "mt2", "mt3"])
        for stage in prompt_templates.STAGES:
            self.assertTrue(stage.template.strip(), stage.key)
            self.assertTrue(stage.label and stage.description, stage.key)

    def test_templates_match_their_rtf_sources(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for fragments, key in ((("mt", "0"), "init"), (("mt", "1"), "mt1"),
                               (("mt", "2"), "mt2"), (("mt", "3"), "mt3")):
            path = rtf.find_rtf(root, *fragments)
            if not path:
                self.skipTest("RTF sources not present")
            with self.subTest(stage=key):
                self.assertEqual(rtf.read_rtf_file(path),
                                 prompt_templates.STAGE_BY_KEY[key].template)

    def test_stage_for_batch(self):
        self.assertEqual(prompts.stage_for_batch(0, 5), "mt1")
        self.assertEqual(prompts.stage_for_batch(1, 5), "mt2")
        self.assertEqual(prompts.stage_for_batch(3, 5), "mt2")
        self.assertEqual(prompts.stage_for_batch(4, 5), "mt3")
        # A single-batch run is an opening, not a closing.
        self.assertEqual(prompts.stage_for_batch(0, 1), "mt1")

    def test_render_template_substitutes_known_tokens(self):
        text, used = prompts.render_template(
            "Rules: {{HARD_RULES}} / Batch {{BATCH_NUMBER}}",
            {"{{HARD_RULES}}": "R", "{{BATCH_NUMBER}}": "3"},
        )
        self.assertEqual(text, "Rules: R / Batch 3")
        self.assertEqual(used, {"{{HARD_RULES}}", "{{BATCH_NUMBER}}"})

    def test_unknown_placeholders_are_left_alone(self):
        text, used = prompts.render_template("Статья [НОМЕР СТАТЬИ] тут", {})
        self.assertEqual(text, "Статья [НОМЕР СТАТЬИ] тут")
        self.assertEqual(used, set())

    def test_template_replaces_the_builtin_role(self):
        prompt = prompts.build_system_prompt(
            "", "", "", template="МОЙ ШАБЛОН")
        self.assertTrue(prompt.startswith("МОЙ ШАБЛОН"))
        self.assertNotIn("You are a professional translator", prompt)

    def test_token_use_suppresses_the_duplicate_section(self):
        prompt = prompts.build_system_prompt(
            "RULEBODY", "VIBEBODY", "",
            template="Rules follow: {{HARD_RULES}}",
            template_values={"{{HARD_RULES}}": "RULEBODY"},
        )
        self.assertEqual(prompt.count("RULEBODY"), 1)
        self.assertNotIn("HARD EDITORIAL RULES", prompt)
        # The vibe had no token, so it is still emitted normally.
        self.assertIn("VIBEBODY", prompt)

    def test_tag_rules_and_contract_always_survive_a_template(self):
        prompt = prompts.build_system_prompt("", "", "", template="X")
        self.assertIn("INLINE TAG RULES", prompt)
        self.assertIn("OUTPUT FORMAT", prompt)


class TestRulesExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = state.ProjectState(os.path.join(self.tmp, "state.json"))

    def test_export_contains_every_authored_section(self):
        self.state.load_termbase_csv(memoq_csv("entropy,,,,,энтропия,"))
        text = self.state.rules_export_text()
        for heading in ("HARD RULES", "VIBE / SOFT RULES", "TERMBASE",
                        "PROMPT TEMPLATES"):
            self.assertIn(heading, text)
        self.assertIn("Правила редактуры", text)
        self.assertIn("entropy -> энтропия", text)
        self.assertIn("[init] Init", text)

    def test_edited_templates_are_flagged(self):
        self.state.set_template("mt2", "переписано")
        text = self.state.rules_export_text()
        self.assertIn("[mt2] MT2  (edited)", text)
        self.assertIn("переписано", text)

    def test_corrections_included_when_present(self):
        self.state.corrections = [state.Correction("12", "src", "mt", "human")]
        self.assertIn("HUMAN CORRECTIONS (1)", self.state.rules_export_text())

    def test_writes_a_timestamped_utf8_file(self):
        exports = os.path.join(self.tmp, "exports")
        path = self.state.export_rules_txt(exports)
        self.assertTrue(os.path.isfile(path))
        name = os.path.basename(path)
        self.assertTrue(name.startswith("rules_export_"), name)
        self.assertTrue(name.endswith(".txt"), name)
        self.assertEqual(os.path.dirname(path), exports)
        with open(path, encoding="utf-8") as handle:
            self.assertIn("Правила редактуры", handle.read())


class TestDiffing(unittest.TestCase):
    def test_identical_text_reports_no_change(self):
        self.assertFalse(diffing.stats("одно и то же", "одно и то же").changed)

    def test_word_replacement_is_one_delete_plus_one_insert(self):
        change = diffing.stats("быстрая энтропия", "медленная энтропия")
        self.assertEqual((change.added, change.removed), (1, 1))
        self.assertEqual(change.unchanged, 1)

    def test_pure_insertion(self):
        change = diffing.stats("энтропия", "быстрая энтропия")
        self.assertEqual((change.added, change.removed), (1, 0))

    def test_placeholders_are_single_tokens(self):
        runs = diffing.word_diff("{1}текст", "{1}текст")
        self.assertEqual(runs, [(diffing.EQUAL, "{1}текст")])
        self.assertIn("{1}", diffing.tokenize("a {1} b"))

    def test_moved_placeholder_is_detected(self):
        change = diffing.stats("{1}Save{2} now", "now {1}Save{2}")
        self.assertTrue(change.changed)

    def test_html_marks_both_sides(self):
        out = diffing.render_html("старый текст", "новый текст")
        self.assertIn("line-through", out)      # deletion
        self.assertIn("border-bottom", out)     # insertion
        self.assertIn("текст", out)

    def test_html_escapes_markup(self):
        out = diffing.render_html("a", "<script>x</script>")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_html_is_length_capped(self):
        """Both the text budget and the run count are bounded."""
        big = diffing.render_html("a " * 300, "b " * 300)
        small = diffing.render_html("a " * 300, "b " * 300, max_chars=120, max_runs=8)
        self.assertLess(len(small), len(big))
        self.assertLess(len(small), 2000)
        self.assertIn("...", small)

    def test_pathological_input_falls_back_instead_of_stalling(self):
        """difflib is quadratic; a frozen UI is worse than a coarse diff."""
        import time
        old, new = "a " * 4000, "b " * 4000
        start = time.time()
        runs = diffing.word_diff(old, new)
        self.assertLess(time.time() - start, 2.0)
        self.assertEqual([op for op, _ in runs], [diffing.DELETE, diffing.INSERT])
        self.assertTrue(diffing.stats(old, new).changed)

    def test_markdown_fallback(self):
        out = diffing.render_markdown("старый", "новый")
        self.assertIn("~~старый~~", out)
        self.assertIn("**новый**", out)

    def test_ratio_and_summary(self):
        change = diffing.stats("a b c d", "a b c e")
        self.assertAlmostEqual(change.ratio, 2 / 5)
        self.assertIn("%", change.summary())
        self.assertEqual(diffing.stats("x", "x").summary(), "no change")

    def test_empty_inputs_are_safe(self):
        self.assertFalse(diffing.stats("", "").changed)
        self.assertTrue(diffing.stats("", "новое").changed)
        self.assertIsInstance(diffing.render_html("", ""), str)


class TestAnthropicBackend(unittest.TestCase):
    def test_preset_exists_and_uses_the_anthropic_sdk(self):
        preset = llm.PRESETS["Anthropic API"]
        self.assertEqual(preset.backend, llm.BACKEND_ANTHROPIC)
        self.assertEqual(preset.default_model, "claude-3-5-sonnet-latest")
        self.assertTrue(preset.requires_key)
        self.assertIn("api.anthropic.com", preset.base_url)

    def test_newer_models_are_offered(self):
        suggestions = llm.PRESETS["Anthropic API"].model_suggestions
        self.assertIn("claude-sonnet-5", suggestions)
        self.assertIn("claude-opus-5", suggestions)

    def test_other_presets_stay_on_the_openai_sdk(self):
        for name in ("OpenAI API", "OpenRouter", "DeepSeek", "Local Engine (Ollama)"):
            self.assertEqual(llm.PRESETS[name].backend, llm.BACKEND_OPENAI, name)

    def test_config_flags_the_backend(self):
        self.assertTrue(llm.ClientConfig(backend=llm.BACKEND_ANTHROPIC).is_anthropic)
        self.assertFalse(llm.ClientConfig().is_anthropic)

    def test_system_prompt_is_hoisted_out_of_messages(self):
        """Anthropic takes `system` as a top-level field, not a message."""
        captured = {}

        class FakeMessages:
            def create(self, **kwargs):
                captured.update(kwargs)
                return type("R", (), {"content": [
                    type("B", (), {"type": "text", "text": '"a": 1}'})()]})()

        client = llm.LLMClient.__new__(llm.LLMClient)
        client.config = llm.ClientConfig(
            backend=llm.BACKEND_ANTHROPIC, model="claude-3-5-sonnet-latest")
        client._supports_json_mode = None
        client._client = type("C", (), {"messages": FakeMessages()})()

        out = client._chat(
            [{"role": "system", "content": "SYS"},
             {"role": "user", "content": "USER"}],
            force_json=True,
        )
        self.assertEqual(captured["system"], "SYS")
        self.assertEqual(captured["messages"][0], {"role": "user", "content": "USER"})
        # JSON is coaxed with an assistant prefill, which is stitched back on.
        self.assertEqual(captured["messages"][-1], {"role": "assistant", "content": "{"})
        self.assertEqual(out, '{"a": 1}')
        self.assertGreater(captured["max_tokens"], 0)

    def test_temperature_is_never_sent_to_anthropic(self):
        """messages.create() has no temperature parameter; passing it TypeErrors."""
        captured = {}

        class FakeMessages:
            def create(self, **kwargs):
                captured.update(kwargs)
                return type("R", (), {"content": [
                    type("B", (), {"type": "text", "text": "ok"})()]})()

        client = llm.LLMClient.__new__(llm.LLMClient)
        client.config = llm.ClientConfig(
            backend=llm.BACKEND_ANTHROPIC, temperature=0.9)
        client._supports_json_mode = None
        client._client = type("C", (), {"messages": FakeMessages()})()

        client._chat([{"role": "user", "content": "hi"}], force_json=False)
        self.assertNotIn("temperature", captured)

    def test_anthropic_kwargs_match_the_installed_sdk(self):
        """Guards against SDK parameter drift - how the crash got in."""
        try:
            import inspect

            from anthropic.resources.messages import Messages
        except ImportError:
            self.skipTest("anthropic not installed")

        accepted = set(inspect.signature(Messages.create).parameters)
        captured = {}

        class FakeMessages:
            def create(self, **kwargs):
                captured.update(kwargs)
                return type("R", (), {"content": [
                    type("B", (), {"type": "text", "text": "ok"})()]})()

        client = llm.LLMClient.__new__(llm.LLMClient)
        client.config = llm.ClientConfig(backend=llm.BACKEND_ANTHROPIC)
        client._supports_json_mode = None
        client._client = type("C", (), {"messages": FakeMessages()})()

        client._chat([{"role": "system", "content": "S"},
                      {"role": "user", "content": "U"}], force_json=True)

        unsupported = set(captured) - accepted
        self.assertEqual(unsupported, set(),
                         "these kwargs are not in the installed SDK: %s" % unsupported)

    def test_temperature_still_reaches_openai_backends(self):
        """The slider must keep working everywhere it is supported."""
        captured = {}

        class FakeCompletions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return type("R", (), {"choices": [type("C", (), {
                    "message": type("M", (), {"content": "ok"})()})()]})()

        client = llm.LLMClient.__new__(llm.LLMClient)
        client.config = llm.ClientConfig(
            backend=llm.BACKEND_OPENAI, temperature=0.37)
        client._supports_json_mode = None
        client._client = type("C", (), {
            "chat": type("Ch", (), {"completions": FakeCompletions()})()})()

        client._chat([{"role": "user", "content": "hi"}], force_json=False)
        self.assertEqual(captured["temperature"], 0.37)

    def test_max_tokens_is_always_positive(self):
        """Anthropic rejects the request without it; there is no server default."""
        captured = {}

        class FakeMessages:
            def create(self, **kwargs):
                captured.update(kwargs)
                return type("R", (), {"content": [
                    type("B", (), {"type": "text", "text": "ok"})()]})()

        client = llm.LLMClient.__new__(llm.LLMClient)
        client.config = llm.ClientConfig(backend=llm.BACKEND_ANTHROPIC, max_tokens=0)
        client._supports_json_mode = None
        client._client = type("C", (), {"messages": FakeMessages()})()

        client._chat([{"role": "user", "content": "hi"}], force_json=False)
        self.assertGreater(captured["max_tokens"], 0)


class TestPrompts(unittest.TestCase):
    def test_layers_present_and_ordered(self):
        prompt = prompts.build_system_prompt(
            hard_rules_text="Всегда 'е', не 'ё'.",
            vibe="Concise and formal.",
            corrections_block='<12: "Bericht exportieren">',
            termbase=[state.TermEntry("entropy", "энтропия")],
            source_lang="en-US",
            target_lang="ru-RU",
        )
        self.assertIn("en-US -> ru-RU", prompt)
        self.assertIn("Concise and formal.", prompt)
        self.assertIn("Всегда 'е', не 'ё'.", prompt)
        self.assertIn('"entropy" -> "энтропия"', prompt)
        self.assertIn('<12: "Bericht exportieren">', prompt)
        self.assertIn("JSON", prompt)

        # Authority increases down the prompt: vibe < hard rules < termbase
        # < human corrections.
        order = [prompt.index(x) for x in (
            "Concise and formal.", "Всегда 'е'", "entropy", "Bericht exportieren")]
        self.assertEqual(order, sorted(order))

    def test_hard_rules_prose_is_injected_verbatim(self):
        block = defaults.DEFAULT_HARD_RULES
        prompt = prompts.build_system_prompt(block, "", "")
        self.assertIn(block.strip(), prompt)

    def test_optional_sections_omitted(self):
        prompt = prompts.build_system_prompt("", "", "")
        self.assertNotIn("HARD EDITORIAL RULES", prompt)
        self.assertNotIn("TERMBASE ENTRIES", prompt)
        self.assertNotIn("HUMAN CORRECTIONS", prompt)
        self.assertIn("INLINE TAG RULES", prompt)

    def test_termbase_omitted_when_no_terms_match(self):
        prompt = prompts.build_system_prompt("rules", "vibe", "", termbase=[])
        self.assertNotIn("TERMBASE ENTRIES", prompt)

    def test_article_rules_are_declared_to_outrank_the_termbase(self):
        """Guards the false-friend case: 'filling type' -> 'тип заливки'."""
        prompt = prompts.build_system_prompt(
            "rules", "order filling type - тип исполнения ордера", "",
            termbase=[state.TermEntry("filling type", "тип заливки")],
        )
        section = prompt.split("TERMBASE ENTRIES", 1)[1]
        self.assertIn("not article law", section)
        self.assertIn("false friend", section)

    def test_user_payload_is_json_with_ids(self):
        record = state.SegmentRecord(
            key="1#0", segment_id="1", ordinal=0, batch_index=0,
            source_text="Hello {1}", mt_target="Hallo {1}",
        )
        payload = json.loads(prompts.build_user_payload([record]))
        self.assertEqual(payload["segments"][0]["id"], "1#0")
        self.assertNotIn("reviewer_note", payload["segments"][0])

    def test_reviewer_note_included_when_present(self):
        record = state.SegmentRecord(
            key="1#0", segment_id="1", ordinal=0, batch_index=0, user_comment="shorter"
        )
        payload = json.loads(prompts.build_user_payload([record]))
        self.assertEqual(payload["segments"][0]["reviewer_note"], "shorter")


class TestJsonExtraction(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(llm.extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_json(self):
        self.assertEqual(llm.extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_json_with_leading_prose(self):
        parsed = llm.extract_json('Sure! Here you go:\n{"segments": []}')
        self.assertEqual(parsed, {"segments": []})

    def test_truncated_array_is_salvaged(self):
        raw = '{"segments": [{"id": "1", "target": "eins"}, {"id": "2", "target": "zwei"}'
        parsed = llm.extract_json(raw)
        self.assertEqual(len(list(llm._iter_segments(parsed))), 2)

    def test_garbage_returns_none(self):
        self.assertIsNone(llm.extract_json("I cannot help with that."))

    def test_iter_segments_handles_bare_mapping(self):
        items = list(llm._iter_segments({"1#0": "eins", "2#0": "zwei"}))
        self.assertEqual(items[0], {"id": "1#0", "target": "eins"})

    def test_iter_segments_handles_alternate_key(self):
        items = list(llm._iter_segments({"translations": [{"id": "a", "target": "b"}]}))
        self.assertEqual(items[0]["target"], "b")

    def test_presets_are_well_formed(self):
        for name, preset in llm.PRESETS.items():
            self.assertTrue(preset.base_url.startswith("http"), name)
            self.assertTrue(preset.default_model, name)
            if preset.backend == llm.BACKEND_OPENAI:
                # Chat-completions endpoints are versioned; Anthropic's SDK
                # appends its own path, so /v1 would be wrong there.
                self.assertTrue(preset.base_url.endswith("/v1"), name)
        self.assertFalse(llm.PRESETS["Local Engine (Ollama)"].requires_key)
        self.assertIn("11434", llm.PRESETS["Local Engine (Ollama)"].base_url)


class TestOllamaHostResolution(unittest.TestCase):
    """OLLAMA_HOST decides where a tunnelled Ollama actually lives."""

    def setUp(self):
        self._saved = os.environ.get("OLLAMA_HOST")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = self._saved

    def test_unset_falls_back_to_localhost(self):
        os.environ.pop("OLLAMA_HOST", None)
        self.assertEqual(llm.ollama_base_url(), "http://localhost:11434/v1")

    def test_bare_host_port_gets_scheme_and_suffix(self):
        os.environ["OLLAMA_HOST"] = "10.211.55.2:11434"
        self.assertEqual(llm.ollama_base_url(), "http://10.211.55.2:11434/v1")

    def test_full_url_is_respected(self):
        os.environ["OLLAMA_HOST"] = "http://192.168.1.9:11434/"
        self.assertEqual(llm.ollama_base_url(), "http://192.168.1.9:11434/v1")

    def test_blank_is_treated_as_unset(self):
        os.environ["OLLAMA_HOST"] = "   "
        self.assertEqual(llm.ollama_base_url(), "http://localhost:11434/v1")


class TestEndToEnd(unittest.TestCase):
    """Load -> batch -> simulate model -> human edit -> diff -> export."""

    def test_full_cycle(self):
        tmp = tempfile.mkdtemp()
        doc = load_doc()
        project = state.ProjectState(os.path.join(tmp, "state.json"))
        project.batch_size = 4
        project.bind_segments(doc.translatable_segments(), "sample.mqxliff")

        # Batch 0: model responds, human edits one row.
        keys = project.batch_keys(0)
        project.record_suggestions({k: project.segments[k].mt_target for k in keys})
        project.capture_edits({keys[0]: "Waehlen Sie {1}Sichern{2} aus."})
        corrections = project.calculate_diffs(0)
        self.assertEqual(len(corrections), 1)

        # That correction must reach the next batch's prompt.
        prompt = prompts.build_system_prompt(
            project.hard_rules_text, project.vibe, project.feedback_block(upto_batch=1)
        )
        self.assertIn("Waehlen Sie {1}Sichern{2} aus.", prompt)

        # Export writes every row with text and stays parseable.
        by_key = {s.key: s for s in doc.translatable_segments()}
        written = 0
        for key in project.order:
            record = project.segments[key]
            text = record.effective_text()
            if text.strip():
                doc.apply_translation(by_key[key], text)
                written += 1
        self.assertEqual(written, 6)  # id 9 has no target text yet

        out = os.path.join(tmp, "out.mqxliff")
        doc.save(out)
        reparsed = mqxliff.MqxliffDocument.from_path(out)
        reparsed.parse_segments()
        edited = next(s for s in reparsed.segments if s.unit_id == "1")
        self.assertEqual(edited.target_text, "Waehlen Sie {1}Sichern{2} aus.")
        self.assertEqual(len(reparsed.segments), 12)

    def test_export_preserves_namespaces(self):
        doc = load_doc()
        raw = doc.to_bytes().decode("utf-8")
        self.assertIn("urn:oasis:names:tc:xliff:document:1.2", raw)
        self.assertIn("memoq", raw)
        self.assertTrue(raw.startswith("<?xml"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
