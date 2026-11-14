"""Core correctness tests: tag round-tripping, filtering, batching, feedback.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

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


# Synthetic units for the fingerprint tests: (id, source, target, unit attrs).
# Inline tags are written unprefixed and "MQ:" stands for the memoQ prefix;
# _fp_xml() rewrites both so prefix choice can be varied per test.
FP_UNITS = [
    ("1",
     'Click <bpt id="1" ctype="bold">&lt;b&gt;</bpt>Save'
     '<ept id="1">&lt;/b&gt;</ept> now.',
     'Klicken Sie <bpt id="1" ctype="bold">&lt;b&gt;</bpt>Speichern'
     '<ept id="1">&lt;/b&gt;</ept>.',
     'MQ:status="NotStarted" MQ:locked="false"'),
    ("2", "Plain second row.", "Zweite Zeile.", 'MQ:status="NotStarted"'),
    # Confirmed, so the default filter hides it.
    ("3", "Hidden confirmed row.", "Bestaetigte Zeile.",
     'MQ:status="Confirmed" MQ:confirmed="true"'),
]


def _fp_xml(units=FP_UNITS, src="en-US", tgt="de-DE", xliff_prefix="",
            mq_prefix="mq", reverse_file_attrs=False) -> bytes:
    import re as re_mod  # noqa: PLC0415

    def inline(xml: str) -> str:
        if not xliff_prefix:
            return xml
        return re_mod.sub(r"<(/?)(bpt|ept|ph|it|g)\b",
                          r"<\1%s:\2" % xliff_prefix, xml)

    p = (xliff_prefix + ":") if xliff_prefix else ""
    xmlns = (' xmlns:%s="%s"' % (xliff_prefix, mqxliff.XLIFF_NS)
             if xliff_prefix else ' xmlns="%s"' % mqxliff.XLIFF_NS)
    file_attrs = ['original="doc.docx"', 'source-language="%s"' % src,
                  'target-language="%s"' % tgt, 'datatype="x-docx"']
    if reverse_file_attrs:
        file_attrs.reverse()
    body = "\n".join(
        '<%strans-unit id="%s" %s><%ssource>%s</%ssource>'
        '<%starget>%s</%starget></%strans-unit>'
        % (p, uid, attrs.replace("MQ:", mq_prefix + ":"), p, inline(source), p,
           p, inline(target), p, p)
        for (uid, source, target, attrs) in units)
    xml = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<%sxliff version="1.2"%s xmlns:%s="%s">\n'
           '<%sfile %s><%sbody>\n%s\n</%sbody></%sfile></%sxliff>\n'
           % (p, xmlns, mq_prefix, mqxliff.MQ_NS, p, " ".join(file_attrs), p,
              body, p, p, p))
    return xml.encode("utf-8")


def _fp(raw: bytes, filename: str = "doc.mqxliff") -> str:
    return mqxliff.MqxliffDocument(raw, filename).source_fingerprint()


def _fp_units(index: int, **changes):
    """FP_UNITS with one unit's fields replaced."""
    units = list(FP_UNITS)
    uid, source, target, attrs = units[index]
    units[index] = (changes.get("uid", uid), changes.get("source", source),
                    changes.get("target", target), changes.get("attrs", attrs))
    return units


class TestSourceFingerprint(unittest.TestCase):
    """Source-content identity of a document; see source_fingerprint()."""

    def setUp(self):
        self.base = _fp(_fp_xml())

    def test_is_a_sha256_hex_digest(self):
        self.assertRegex(self.base, r"^[0-9a-f]{64}$")

    def test_same_document_same_fingerprint(self):
        raw = _fp_xml()
        self.assertEqual(_fp(raw), _fp(raw))
        # Filename is not part of the identity.
        self.assertEqual(_fp(raw, "a.mqxliff"), _fp(raw, "other/b.mqxliff"))
        # Neither is the path a file was loaded from.
        with tempfile.TemporaryDirectory() as tmp:
            first = os.path.join(tmp, "one", "doc.mqxliff")
            second = os.path.join(tmp, "two", "renamed.mqxliff")
            for path in (first, second):
                os.makedirs(os.path.dirname(path))
                with open(path, "wb") as handle:
                    handle.write(raw)
            self.assertEqual(
                mqxliff.MqxliffDocument.from_path(first).source_fingerprint(),
                mqxliff.MqxliffDocument.from_path(second).source_fingerprint())
        self.assertEqual(_fp(raw), self.base)

    def test_shipped_fixture_is_stable_across_loads(self):
        first = mqxliff.MqxliffDocument.from_path(FIXTURE).source_fingerprint()
        second = load_doc().source_fingerprint()
        self.assertEqual(first, second)

    def test_target_only_changes_keep_fingerprint(self):
        cases = {
            "text": _fp_units(1, target="Ganz andere Uebersetzung."),
            "tags": _fp_units(0, target='<bpt id="1" ctype="italic">&lt;i&gt;'
                                        '</bpt>Anders<ept id="1">&lt;/i&gt;</ept>'),
            "empty": _fp_units(2, target=""),
        }
        for name, units in cases.items():
            with self.subTest(change=name):
                self.assertEqual(_fp(_fp_xml(units)), self.base)

    def test_status_and_locking_changes_keep_fingerprint(self):
        cases = {
            "status": _fp_units(1, attrs='MQ:status="ManuallyConfirmed"'),
            "locked": _fp_units(0, attrs='MQ:status="NotStarted" MQ:locked="true"'),
            "unconfirmed": _fp_units(2, attrs='MQ:status="NotStarted"'),
            "no-attrs": _fp_units(1, attrs=""),
        }
        for name, units in cases.items():
            with self.subTest(change=name):
                self.assertEqual(_fp(_fp_xml(units)), self.base)

    def test_filter_changes_keep_fingerprint_and_cover_hidden_segments(self):
        doc = mqxliff.MqxliffDocument(_fp_xml(), "doc.mqxliff")
        unparsed = doc.source_fingerprint()
        doc.parse_segments()
        reasons = {s.unit_id: s.skip_reason for s in doc.segments}
        self.assertEqual(reasons["3"], "confirmed")  # really hidden
        default_filter = doc.source_fingerprint()
        doc.parse_segments(mqxliff.SegmentFilter(
            skip_confirmed=False, skip_locked=False,
            require_nonempty_source=False))
        relaxed = doc.source_fingerprint()
        self.assertEqual(unparsed, self.base)
        self.assertEqual(default_filter, self.base)
        self.assertEqual(relaxed, self.base)
        # A hidden segment still contributes its source content.
        changed = _fp(_fp_xml(_fp_units(2, source="Edited hidden row.")))
        self.assertNotEqual(changed, self.base)

    def test_tag_placeholder_mode_keeps_fingerprint(self):
        doc = mqxliff.MqxliffDocument(_fp_xml(), "doc.mqxliff")
        doc.parse_segments(tag_source=tags.TAG_SOURCE_TARGET)
        by_target = doc.source_fingerprint()
        doc.parse_segments(tag_source=tags.TAG_SOURCE_SOURCE)
        by_source = doc.source_fingerprint()
        self.assertEqual(by_target, self.base)
        self.assertEqual(by_source, self.base)

    def test_source_text_and_tag_structure_changes_change_fingerprint(self):
        cases = {
            "text": _fp_units(1, source="Plain second row!"),
            "whitespace": _fp_units(1, source="Plain  second row."),
            "tag-attr-value": _fp_units(0, source=FP_UNITS[0][1].replace(
                'ctype="bold"', 'ctype="italic"')),
            "tag-content": _fp_units(0, source=FP_UNITS[0][1].replace(
                "&lt;b&gt;", "&lt;strong&gt;")),
            "tag-moved": _fp_units(
                0, source='Click Save<bpt id="1" ctype="bold">&lt;b&gt;</bpt>'
                          '<ept id="1">&lt;/b&gt;</ept> now.'),
            "tag-removed": _fp_units(0, source="Click Save now."),
            "tag-added": _fp_units(1, source='Plain <ph id="9">&lt;br/&gt;</ph>'
                                             'second row.'),
            # Same visible text, but the text moves into the tag.
            "text-into-tag": _fp_units(
                0, source='Click <bpt id="1" ctype="bold">&lt;b&gt;Save</bpt>'
                          '<ept id="1">&lt;/b&gt;</ept> now.'),
        }
        seen = {self.base}
        for name, units in cases.items():
            with self.subTest(change=name):
                value = _fp(_fp_xml(units))
                self.assertNotIn(value, seen)
                seen.add(value)

    def test_segment_identity_and_order_changes_change_fingerprint(self):
        swapped = [FP_UNITS[1], FP_UNITS[0], FP_UNITS[2]]
        cases = {
            "id": _fp_units(1, uid="20"),
            "order": swapped,
            "removed": FP_UNITS[:2],
            "added": FP_UNITS + [("4", "Extra row.", "", "")],
            # Same ids and sources, but the ids now sit on different texts.
            "ids-swapped": [("2",) + FP_UNITS[0][1:], ("1",) + FP_UNITS[1][1:],
                            FP_UNITS[2]],
        }
        for name, units in cases.items():
            with self.subTest(change=name):
                self.assertNotEqual(_fp(_fp_xml(units)), self.base)

    def test_language_pair_changes_change_fingerprint(self):
        cases = {
            "source": _fp_xml(src="en-GB"),
            "target": _fp_xml(tgt="ru-RU"),
            "swapped": _fp_xml(src="de-DE", tgt="en-US"),
        }
        for name, raw in cases.items():
            with self.subTest(change=name):
                self.assertNotEqual(_fp(raw), self.base)

    def test_prefixes_and_attribute_order_do_not_change_fingerprint(self):
        reordered = [(uid, source.replace('id="1" ctype="bold"',
                                          'ctype="bold" id="1"'),
                      target, " ".join(reversed(attrs.split(" "))))
                     for (uid, source, target, attrs) in FP_UNITS]
        self.assertNotEqual(_fp_xml(reordered), _fp_xml())
        cases = {
            "xliff-prefix": _fp_xml(xliff_prefix="xl"),
            "mq-prefix": _fp_xml(mq_prefix="memoq"),
            "both-prefixes": _fp_xml(xliff_prefix="xl", mq_prefix="memoq"),
            "attribute-order": _fp_xml(reordered, reverse_file_attrs=True),
        }
        for name, raw in cases.items():
            with self.subTest(change=name):
                self.assertNotEqual(raw, _fp_xml())  # bytes really differ
                self.assertEqual(_fp(raw), self.base)

    def test_comments_and_processing_instructions_are_ignored(self):
        tagged = FP_UNITS[0][1]
        cases = {
            "comment-between-runs": _fp_units(
                1, source="Plain <!-- reviewer note -->second row."),
            "pi-between-runs": _fp_units(1, source="Plain <?harness x?>second row."),
            "comment-mid-word": _fp_units(1, source="Plain sec<!--x-->ond row."),
            "comment-and-pi": _fp_units(
                1, source="<!--a-->Plain <?p q?>second<!--b--> row.<?r s?>"),
            "comment-before-tag": _fp_units(
                0, source=tagged.replace("Click <bpt", "Click <!--c--><bpt")),
            "comment-inside-tag": _fp_units(
                0, source=tagged.replace("&lt;b&gt;", "&lt;b<!--c-->&gt;")),
            "pi-after-tag": _fp_units(
                0, source=tagged.replace("</bpt>Save", "</bpt><?p?>Save")),
        }
        for name, units in cases.items():
            with self.subTest(change=name):
                raw = _fp_xml(units)
                self.assertNotEqual(raw, _fp_xml())  # bytes really differ
                # Equal to the comment-free base, so no surrounding text was lost.
                self.assertEqual(_fp(raw), self.base)
        # Control: the text around the comment does matter.
        lost_tail = _fp(_fp_xml(_fp_units(1, source="Plain <!--x-->")))
        self.assertNotEqual(lost_tail, self.base)

    def test_multi_file_language_pairs_and_units_all_count(self):
        base = _fp(_fp_multi([("en-US", "de-DE", ["A1", "A2"]),
                              ("en-US", "fr-FR", ["B1"])]))
        cases = {
            "second-file-language": [("en-US", "de-DE", ["A1", "A2"]),
                                     ("en-US", "it-IT", ["B1"])],
            "second-file-unit-source": [("en-US", "de-DE", ["A1", "A2"]),
                                        ("en-US", "fr-FR", ["B1 changed"])],
            "files-swapped": [("en-US", "fr-FR", ["B1"]),
                              ("en-US", "de-DE", ["A1", "A2"])],
            "file-dropped": [("en-US", "de-DE", ["A1", "A2"])],
        }
        for name, files in cases.items():
            with self.subTest(change=name):
                self.assertNotEqual(_fp(_fp_multi(files)), base)

    def test_moving_a_unit_across_file_boundary_changes_fingerprint(self):
        # Global unit order, ids and sources stay identical; only the <file>
        # that owns unit "A2" changes.
        for pairs in ((("en-US", "de-DE"), ("en-US", "de-DE")),
                      (("en-US", "de-DE"), ("en-US", "fr-FR"))):
            (s1, t1), (s2, t2) = pairs
            with self.subTest(pairs=pairs):
                before = _fp_multi([(s1, t1, ["A1", "A2"]), (s2, t2, ["B1"])])
                after = _fp_multi([(s1, t1, ["A1"]), (s2, t2, ["A2", "B1"])])
                self.assertEqual(
                    [s.unit_id for s in _parsed(before)],
                    [s.unit_id for s in _parsed(after)])
                self.assertNotEqual(_fp(before), _fp(after))

    def test_units_outside_a_file_are_counted_not_dropped(self):
        inside = _fp_multi([("en-US", "de-DE", ["A1", "Loose"])])
        outside = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<xliff version="1.2" xmlns="%s">'
            '<file original="f0.docx" source-language="en-US" '
            'target-language="de-DE"><body><trans-unit id="1"><source>A1'
            '</source><target/></trans-unit></body></file>'
            '<trans-unit id="2"><source>%s</source><target/></trans-unit>'
            '</xliff>')
        loose = (outside % (mqxliff.XLIFF_NS, "Loose")).encode("utf-8")
        changed = (outside % (mqxliff.XLIFF_NS, "Loose edited")).encode("utf-8")
        # The parser accepts the stray unit, so the fingerprint must cover it.
        self.assertEqual(len(_parsed(loose)), 2)
        self.assertNotEqual(_fp(loose), _fp(inside))
        self.assertNotEqual(_fp(loose), _fp(changed))


def _fp_unit_doc(unit_body: str) -> bytes:
    """One-unit document whose trans-unit body is given verbatim."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<xliff version="1.2" xmlns="%s" xmlns:mq="%s">'
        '<file original="d.docx" source-language="en-US" target-language="de-DE">'
        '<body><trans-unit id="1">%s<target>Ziel</target></trans-unit>'
        '</body></file></xliff>' % (mqxliff.XLIFF_NS, mqxliff.MQ_NS, unit_body)
    ).encode("utf-8")


def _fp_multi(files) -> bytes:
    """Several <file> elements: (source-lang, target-lang, [unit sources]).

    Unit ids are numbered globally so they stay unique across files.
    """
    parts, uid = [], 0
    for src, tgt, sources in files:
        units = []
        for source in sources:
            uid += 1
            units.append('<trans-unit id="%d"><source>%s</source>'
                         '<target/></trans-unit>' % (uid, source))
        parts.append('<file original="f%d.docx" source-language="%s" '
                     'target-language="%s"><body>%s</body></file>'
                     % (len(parts), src, tgt, "".join(units)))
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<xliff version="1.2" xmlns="%s">%s</xliff>'
            % (mqxliff.XLIFF_NS, "".join(parts))).encode("utf-8")


def _parsed(raw: bytes):
    """All segments of a document, every row kept, via the real parser."""
    doc = mqxliff.MqxliffDocument(raw, "doc.mqxliff")
    return doc.parse_segments(mqxliff.SegmentFilter(
        skip_confirmed=False, skip_locked=False, require_nonempty_source=False))


class TestSourceFingerprintSourceElements(unittest.TestCase):
    """How ``<source>`` and ``<seg-source>`` enter the fingerprint.

    Both elements count, separately: neither replaces the other. An absent
    element (null) differs from an existing empty one ([]). Segmentation
    markup is source XML content, so adding or changing it changes the
    fingerprint even when the visible source text is the same.
    """

    MRK = ('<seg-source><mrk mtype="seg" mid="1">Hello world.</mrk> '
           '<mrk mtype="seg" mid="2">Bye.</mrk></seg-source>')

    def assertAllDistinct(self, bodies):
        seen = {}
        for name, body in bodies.items():
            with self.subTest(case=name):
                value = _fp(_fp_unit_doc(body))
                self.assertNotIn(value, seen.values(),
                                 "collides with %s" % [k for k, v in seen.items()
                                                       if v == value])
                seen[name] = value

    def test_source_only(self):
        alpha = _fp(_fp_unit_doc("<source>Alpha</source>"))
        self.assertNotEqual(alpha, _fp(_fp_unit_doc("<source>Beta</source>")))
        self.assertEqual(alpha, _fp(_fp_unit_doc("<source>Alpha</source>")))

    def test_seg_source_with_segmentation_markup(self):
        self.assertAllDistinct({
            "base": "<source>Hello world. Bye.</source>" + self.MRK,
            "source-changed": "<source>Hello world. Goodbye.</source>" + self.MRK,
            "mrk-text": "<source>Hello world. Bye.</source>"
                        + self.MRK.replace("Bye.", "Goodbye."),
            "mrk-attribute": "<source>Hello world. Bye.</source>"
                             + self.MRK.replace('mid="2"', 'mid="3"'),
            # Same visible text, markup removed: a documented change.
            "markup-removed": "<source>Hello world. Bye.</source>"
                              "<seg-source>Hello world. Bye.</seg-source>",
            "seg-source-absent": "<source>Hello world. Bye.</source>",
        })

    def test_seg_source_with_plain_text_and_no_child_elements(self):
        self.assertAllDistinct({
            "base": "<source>Alpha</source><seg-source>Beta</seg-source>",
            "seg-source-changed": "<source>Alpha</source><seg-source>Gamma</seg-source>",
            "source-changed": "<source>Delta</source><seg-source>Beta</seg-source>",
            "source-only-alpha": "<source>Alpha</source>",
            "source-only-beta": "<source>Beta</source>",
        })

    def test_empty_seg_source_keeps_source_significant(self):
        for body in ("<seg-source/>", "<seg-source></seg-source>"):
            with self.subTest(form=body):
                empty = _fp(_fp_unit_doc("<source>Alpha</source>" + body))
                self.assertNotEqual(empty, _fp(_fp_unit_doc("<source>Beta</source>" + body)))
                self.assertNotEqual(empty, _fp(_fp_unit_doc("<source>Alpha</source>")))
        # Both spellings of an empty element are the same XML content.
        self.assertEqual(_fp(_fp_unit_doc("<source>Alpha</source><seg-source/>")),
                         _fp(_fp_unit_doc("<source>Alpha</source><seg-source></seg-source>")))

    def test_both_present_with_distinguishable_content(self):
        both = _fp(_fp_unit_doc(
            "<source>Source side</source><seg-source>Seg side</seg-source>"))
        with self.subTest(case="source-change-counts"):
            self.assertNotEqual(both, _fp(_fp_unit_doc(
                "<source>Source edit</source><seg-source>Seg side</seg-source>")))
        with self.subTest(case="seg-source-change-counts"):
            self.assertNotEqual(both, _fp(_fp_unit_doc(
                "<source>Source side</source><seg-source>Seg edit</seg-source>")))
        with self.subTest(case="roles-not-interchangeable"):
            self.assertNotEqual(both, _fp(_fp_unit_doc(
                "<source>Seg side</source><seg-source>Source side</seg-source>")))
        with self.subTest(case="element-order-irrelevant"):
            self.assertEqual(both, _fp(_fp_unit_doc(
                "<seg-source>Seg side</seg-source><source>Source side</source>")))

    def test_absent_and_empty_elements_are_distinguished(self):
        self.assertAllDistinct({
            "neither": "",
            "empty-source": "<source/>",
            "empty-seg-source": "<seg-source/>",
            "both-empty": "<source/><seg-source/>",
            "source-text": "<source>A</source>",
            "source-text-empty-seg": "<source>A</source><seg-source/>",
            "seg-text": "<seg-source>A</seg-source>",
            "empty-source-seg-text": "<source/><seg-source>A</seg-source>",
        })


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

    def test_filtering_hides_row_but_preserves_saved_work(self):
        """A row the filter hides keeps its saved text, comment and edited flag;
        it is excluded from active batches but not discarded from the store.

        Confirmed fixture row id 3 is editable only when "Skip confirmed rows"
        is off, which is what makes the hide/restore cycle valid.
        """
        all_keys = {s.key for s in self.doc.segments}  # full-document identity
        key3 = next(s.key for s in self.doc.segments if s.unit_id == "3")
        self.assertIn(key3, all_keys)

        # Reveal the confirmed row so it becomes an active, editable row.
        self.doc.parse_segments(mqxliff.SegmentFilter(skip_confirmed=False))
        self.state.bind_segments(
            self.doc.translatable_segments(), "sample.mqxliff", present_keys=all_keys)
        self.state.capture_edits({key3: "meine Fassung 3"}, {key3: "kommentar"})
        record = self.state.segments[key3]
        self.assertEqual(record.user_final, "meine Fassung 3")
        self.assertEqual(record.user_comment, "kommentar")
        self.assertTrue(record.edited_by_human)
        self.assertIn(key3, self.state.order)

        # Hide it: re-bind the default (translatable) set, which excludes the
        # confirmed row, while telling bind_segments the document still has it.
        self.doc.parse_segments()
        self.state.bind_segments(
            self.doc.translatable_segments(), "sample.mqxliff", present_keys=all_keys)

        active = [k for b in range(self.state.batch_count)
                  for k in self.state.batch_keys(b)]
        self.assertNotIn(key3, self.state.order)
        self.assertNotIn(key3, active)
        preserved = self.state.segments[key3]
        self.assertEqual(preserved.user_final, "meine Fassung 3")
        self.assertEqual(preserved.user_comment, "kommentar")
        self.assertTrue(preserved.edited_by_human)

        # Restoring the filter re-includes the row with its work intact.
        self.doc.parse_segments(mqxliff.SegmentFilter(skip_confirmed=False))
        self.state.bind_segments(
            self.doc.translatable_segments(), "sample.mqxliff", present_keys=all_keys)
        self.assertIn(key3, self.state.order)
        self.assertEqual(self.state.segments[key3].user_final, "meine Fassung 3")
        self.assertTrue(self.state.segments[key3].edited_by_human)

    def test_genuinely_absent_segment_is_pruned(self):
        """A key the full document no longer contains is pruned even though it
        held saved work; a merely filtered-out key is not.

        This pins the behaviour the filter fix depends on: pruning by the
        full-document identity, not by the active (filtered) order.
        """
        all_keys = {s.key for s in self.doc.segments}
        key3 = next(s.key for s in self.doc.segments if s.unit_id == "3")

        self.doc.parse_segments(mqxliff.SegmentFilter(skip_confirmed=False))
        self.state.bind_segments(
            self.doc.translatable_segments(), "sample.mqxliff", present_keys=all_keys)
        self.state.capture_edits({key3: "behalten"})
        self.assertIn(key3, self.state.segments)

        # id 3 is now genuinely absent: the full-document key set no longer
        # contains it, and neither does the active selection.
        still_present = all_keys - {key3}
        self.state.bind_segments(
            [s for s in self.doc.translatable_segments() if s.unit_id != "3"],
            "sample.mqxliff", present_keys=still_present)
        self.assertNotIn(key3, self.state.segments)
        self.assertNotIn(key3, self.state.order)

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


class TestMultiDocumentState(unittest.TestCase):
    """v5 state: several documents in one file, public attributes = active.

    Document IDs are opaque strings supplied by the caller; state.py does not
    compute them. A is the shipped fixture, B a synthetic document whose
    segment keys ("1#0", "2#1") overlap A's.
    """

    KEY = "1#0"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "state.json")
        self.doc_a = load_doc()
        self.doc_b = mqxliff.MqxliffDocument(_fp_xml(), "b.mqxliff")
        self.doc_b.parse_segments()
        self.state = state.ProjectState(self.path)
        self.state.batch_size = 3

    def tearDown(self):
        import shutil  # noqa: PLC0415
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ----------------------------------------------------------- helpers

    def bind(self, proj, doc, name, path=""):
        # bind_segments() assigns source_file AND source_path itself, so the
        # caller must pass the same metadata it gave switch_document().
        proj.bind_segments(doc.translatable_segments(), name, path,
                           present_keys={s.key for s in doc.segments})

    def open_a(self, proj=None):
        proj = proj or self.state
        proj.switch_document("doc-a", "a.mqxliff", r"C:\work\a.mqxliff")
        self.bind(proj, self.doc_a, "a.mqxliff", r"C:\work\a.mqxliff")
        return proj

    def open_b(self, proj=None):
        proj = proj or self.state
        proj.switch_document("doc-b", "b.mqxliff", r"C:\work\b.mqxliff")
        self.bind(proj, self.doc_b, "b.mqxliff", r"C:\work\b.mqxliff")
        return proj

    def work_on_a(self, proj):
        """Apply a human edit plus every other kind of document-scoped data."""
        proj.capture_edits({self.KEY: "A-MANUAL"}, {self.KEY: "A-comment"})
        proj.calculate_diffs(proj.segments[self.KEY].batch_index)
        proj.analysis["0"] = "A analysis"
        proj.global_directive = "A directive"
        proj.current_batch = 1

    def assert_a_work(self, proj):
        record = proj.segments[self.KEY]
        checks = {
            "user_final": (record.user_final, "A-MANUAL"),
            "user_comment": (record.user_comment, "A-comment"),
            "edited_by_human": (record.edited_by_human, True),
            "status": (record.status, "applied"),
            "corrections": ([c.human_final for c in proj.corrections], ["A-MANUAL"]),
            "analysis": (proj.analysis, {"0": "A analysis"}),
            "global_directive": (proj.global_directive, "A directive"),
            "current_batch": (proj.current_batch, 1),
            "source_text": (record.source_text,
                            self.doc_a.segments[0].source_text),
        }
        for field, (actual, expected) in checks.items():
            with self.subTest(field=field):
                self.assertEqual(actual, expected)

    def assert_b_isolated(self, proj):
        record = proj.segments[self.KEY]
        checks = {
            "source_text": (record.source_text, "Click {1}Save{2} now."),
            "mt_target": (record.mt_target, "Klicken Sie {1}Speichern{2}."),
            "user_final": (record.user_final, ""),
            "user_comment": (record.user_comment, ""),
            "edited_by_human": (record.edited_by_human, False),
            "status": (record.status, "pending"),
            "corrections": (proj.corrections, []),
            "analysis": (proj.analysis, {}),
            "global_directive": (proj.global_directive, ""),
            "current_batch": (proj.current_batch, 0),
        }
        for field, (actual, expected) in checks.items():
            with self.subTest(field=field):
                self.assertEqual(actual, expected)

    # ------------------------------------------------------------- tests

    def test_a_b_a_restores_document_scoped_data(self):
        self.open_a()
        self.work_on_a(self.state)
        self.assert_a_work(self.state)

        self.open_b()
        self.assertEqual(self.state.document_id, "doc-b")
        self.assertEqual(self.state.source_file, "b.mqxliff")
        self.assert_b_isolated(self.state)

        self.state.switch_document("doc-a", "a.mqxliff", r"C:\work\a.mqxliff")
        self.assertEqual(self.state.document_id, "doc-a")
        self.assert_a_work(self.state)
        # Re-binding the reopened document keeps the restored work.
        self.bind(self.state, self.doc_a, "a.mqxliff")
        self.assert_a_work(self.state)

    def test_new_document_starts_empty_despite_overlapping_keys(self):
        self.open_a()
        self.work_on_a(self.state)
        self.state.switch_document("doc-b", "b.mqxliff")
        self.assertEqual(self.state.segments, {})
        self.assertEqual(self.state.order, [])
        self.bind(self.state, self.doc_b, "b.mqxliff")
        self.assert_b_isolated(self.state)

    def test_save_load_keeps_both_documents_and_active_reference(self):
        self.open_a()
        self.work_on_a(self.state)
        self.open_b()
        self.state.capture_edits({self.KEY: "B-MANUAL"}, {self.KEY: "B-comment"})
        self.state.save()

        reloaded = state.ProjectState.load(self.path)
        self.assertEqual(reloaded.document_id, "doc-b")
        self.assertEqual(reloaded.document_ids, ["doc-a", "doc-b"])
        self.assertEqual(reloaded.source_file, "b.mqxliff")
        self.assertEqual(reloaded.source_path, r"C:\work\b.mqxliff")
        self.assertEqual(reloaded.segments[self.KEY].user_final, "B-MANUAL")
        self.assertEqual(reloaded.segments[self.KEY].user_comment, "B-comment")

        reloaded.switch_document("doc-a", "a.mqxliff")
        self.assert_a_work(reloaded)
        with open(self.path, encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertEqual(raw["version"], state.STATE_VERSION)
        self.assertEqual(raw["active_document"], "doc-b")
        self.assertEqual(sorted(raw["documents"]), ["doc-a", "doc-b"])

    def test_save_includes_changes_made_after_the_last_switch(self):
        self.open_a()
        self.state.save()
        # No switch between these mutations and save().
        self.work_on_a(self.state)
        self.state.vibe = "changed after switch"
        self.state.save()
        reloaded = state.ProjectState.load(self.path)
        self.assertEqual(reloaded.document_id, "doc-a")
        self.assert_a_work(reloaded)
        self.assertEqual(reloaded.vibe, "changed after switch")

    def test_documents_never_share_mutable_objects(self):
        self.open_a()
        self.work_on_a(self.state)
        a_record = self.state.segments[self.KEY]
        a_segments, a_order = self.state.segments, self.state.order
        a_corrections, a_analysis = self.state.corrections, self.state.analysis

        self.open_b()
        self.assertIsNot(self.state.segments, a_segments)
        self.assertIsNot(self.state.order, a_order)
        self.assertIsNot(self.state.corrections, a_corrections)
        self.assertIsNot(self.state.analysis, a_analysis)
        self.assertIsNot(self.state.segments[self.KEY], a_record)

        # Mutating stale references to A's old objects must not reach A's
        # stored snapshot, nor B.
        a_record.user_final = "STALE"
        a_order.append("stale-key")
        a_analysis["9"] = "stale"
        a_corrections.clear()
        self.assertEqual(self.state.segments[self.KEY].user_final, "")

        self.state.switch_document("doc-a", "a.mqxliff")
        self.assert_a_work(self.state)
        self.assertNotIn("stale-key", self.state.order)
        restored = self.state.segments[self.KEY]
        # Each restore builds fresh objects: mutate, leave, return.
        restored.user_comment = "edited in place"
        self.state.switch_document("doc-b", "b.mqxliff")
        self.state.switch_document("doc-a", "a.mqxliff")
        self.assertIsNot(self.state.segments[self.KEY], restored)
        self.assertEqual(self.state.segments[self.KEY].user_comment,
                         "edited in place")

    def test_same_id_switch_keeps_unsaved_in_memory_work(self):
        self.open_a()
        self.work_on_a(self.state)
        record = self.state.segments[self.KEY]
        self.state.switch_document("doc-a", "renamed.mqxliff", r"D:\moved\renamed.mqxliff")
        self.assertIs(self.state.segments[self.KEY], record)
        self.assert_a_work(self.state)
        self.assertEqual(self.state.source_file, "renamed.mqxliff")
        self.assertEqual(self.state.source_path, r"D:\moved\renamed.mqxliff")
        self.assertFalse(os.path.exists(self.path))  # switching never saves

    def test_global_settings_stay_global(self):
        self.open_a()
        self.state.vibe = "global vibe"
        self.state.hard_rules_text = "global rules"
        self.state.termbase = [state.TermEntry("entropy", "Entropie")]
        self.state.set_template("mt2", "global template")
        self.state.prompt_stage = "mt2"
        self.state.tag_source = tags.TAG_SOURCE_SOURCE
        self.state.auto_merge_proposals = False
        self.state.detailed_feedback = False
        self.state.rule_log.append({"kind": "rule", "text": "logged"})
        self.open_b()

        def check(proj):
            expected = {
                "vibe": "global vibe", "hard_rules_text": "global rules",
                "termbase": [("entropy", "Entropie")],
                "template": "global template", "prompt_stage": "mt2",
                "tag_source": tags.TAG_SOURCE_SOURCE,
                "auto_merge_proposals": False, "detailed_feedback": False,
                "rule_log": [{"kind": "rule", "text": "logged"}],
                "batch_size": 3,
            }
            actual = {
                "vibe": proj.vibe, "hard_rules_text": proj.hard_rules_text,
                "termbase": [(t.source, t.target) for t in proj.termbase],
                "template": proj.template_for("mt2"),
                "prompt_stage": proj.prompt_stage, "tag_source": proj.tag_source,
                "auto_merge_proposals": proj.auto_merge_proposals,
                "detailed_feedback": proj.detailed_feedback,
                "rule_log": proj.rule_log, "batch_size": proj.batch_size,
            }
            for field, value in expected.items():
                with self.subTest(field=field):
                    self.assertEqual(actual[field], value)

        check(self.state)
        # A change made while B is active is seen from A as well.
        self.state.vibe = "changed in B"
        self.state.switch_document("doc-a", "a.mqxliff")
        self.assertEqual(self.state.vibe, "changed in B")
        self.state.vibe = "global vibe"
        self.state.save()
        check(state.ProjectState.load(self.path))

    def test_hidden_saved_records_survive_switching_and_save_load(self):
        relaxed = mqxliff.SegmentFilter(skip_confirmed=False)
        self.state.switch_document("doc-a", "a.mqxliff")
        self.doc_a.parse_segments(relaxed)
        self.bind(self.state, self.doc_a, "a.mqxliff")
        hidden = next(s.key for s in self.doc_a.segments if s.unit_id == "3")
        self.state.capture_edits({hidden: "HIDDEN-MANUAL"}, {hidden: "hidden note"})
        # Back to the default filter: the confirmed row leaves the selection.
        self.doc_a.parse_segments()
        self.bind(self.state, self.doc_a, "a.mqxliff")
        self.assertNotIn(hidden, self.state.order)
        self.assertIn(hidden, self.state.segments)

        self.open_b()
        self.state.save()
        reloaded = state.ProjectState.load(self.path)
        reloaded.switch_document("doc-a", "a.mqxliff")
        self.assertNotIn(hidden, reloaded.order)
        record = reloaded.segments[hidden]
        self.assertEqual(record.user_final, "HIDDEN-MANUAL")
        self.assertEqual(record.user_comment, "hidden note")
        self.assertTrue(record.edited_by_human)

    # ------------------------------------------ explicitly empty active order

    def make_a_all_hidden(self):
        """A with saved human work, then an empty selection (all rows hidden)."""
        self.open_a()
        self.state.capture_edits({self.KEY: "A-MANUAL"}, {self.KEY: "A-comment"})
        self.state.bind_segments([], "a.mqxliff", r"C:\work\a.mqxliff",
                                 present_keys={s.key for s in self.doc_a.segments})
        self.assert_a_hidden(self.state, "after-bind")

    def assert_a_hidden(self, proj, phase):
        record = proj.segments.get(self.KEY)
        checks = {
            "order": (proj.order, []),
            "batch_count": (proj.batch_count, 0),
            "batch_keys": (proj.batch_keys(0), []),
            "record-kept": (record is not None, True),
        }
        if record is not None:
            checks.update({
                "user_final": (record.user_final, "A-MANUAL"),
                "user_comment": (record.user_comment, "A-comment"),
                "edited_by_human": (record.edited_by_human, True),
            })
        for field, (actual, expected) in checks.items():
            with self.subTest(phase=phase, field=field):
                self.assertEqual(actual, expected)

    def test_explicitly_empty_order_survives_switching(self):
        self.make_a_all_hidden()
        self.open_b()
        self.state.switch_document("doc-a", "a.mqxliff", r"C:\work\a.mqxliff")
        self.assert_a_hidden(self.state, "A-B-A")

    def test_explicitly_empty_order_survives_save_load_while_active(self):
        self.make_a_all_hidden()
        self.state.save()
        reloaded = state.ProjectState.load(self.path)
        self.assertEqual(reloaded.document_id, "doc-a")
        self.assert_a_hidden(reloaded, "A-active-reload")

    def test_explicitly_empty_order_survives_save_load_while_inactive(self):
        self.make_a_all_hidden()
        self.open_b()
        self.state.save()
        reloaded = state.ProjectState.load(self.path)
        self.assertEqual(reloaded.document_id, "doc-b")
        reloaded.switch_document("doc-a", "a.mqxliff", r"C:\work\a.mqxliff")
        self.assert_a_hidden(reloaded, "B-active-reload-then-A")

    def test_absent_order_is_still_reconstructed_for_old_entries(self):
        # An entry WITHOUT an "order" key (pre-order files) keeps the legacy
        # fallback: order is rebuilt from the records' ordinals.
        self.open_a()
        expected = list(self.state.order)
        self.state.save()
        with open(self.path, encoding="utf-8") as handle:
            raw = json.load(handle)
        del raw["documents"]["doc-a"]["order"]
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(raw, handle, ensure_ascii=False)
        reloaded = state.ProjectState.load(self.path)
        self.assertEqual(reloaded.order[:len(expected)], expected)
        self.assertEqual(set(reloaded.order), set(reloaded.segments))

    # ------------------------------------- unknown nested fields (v5 files)

    def write_future_fields(self):
        """Save A (with two corrections) and B, then add unknown nested fields
        on disk, as a newer version of the harness might have written them."""
        self.open_a()
        other = self.state.batch_keys(0)[1]
        self.state.capture_edits({self.KEY: "A-MANUAL", other: "A-OTHER"},
                                 {self.KEY: "A-comment"})
        self.state.calculate_diffs(0)
        self.assertEqual(len(self.state.corrections), 2)
        self.open_b()
        self.state.switch_document("doc-a", "a.mqxliff", r"C:\work\a.mqxliff")
        self.state.save()
        with open(self.path, encoding="utf-8") as handle:
            raw = json.load(handle)
        doc = raw["documents"]["doc-a"]
        doc["segments"][self.KEY]["future_seg"] = {"nested": [1, "x"]}
        doc["segments"][other]["future_seg"] = "other-seg"
        by_final = {c["human_final"]: c for c in doc["corrections"]}
        by_final["A-MANUAL"]["future_corr"] = "for-A-MANUAL"
        by_final["A-OTHER"]["future_corr"] = "for-A-OTHER"
        raw["documents"]["doc-b"]["segments"][self.KEY]["future_seg"] = "b-seg"
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(raw, handle, ensure_ascii=False)
        return other

    def saved_doc(self, doc_id="doc-a"):
        with open(self.path, encoding="utf-8") as handle:
            return json.load(handle)["documents"][doc_id]

    def assert_future_fields(self, other, phase):
        doc = self.saved_doc()
        corr = {c["human_final"]: c.get("future_corr") for c in doc["corrections"]}
        checks = {
            "seg-key": (doc["segments"][self.KEY].get("future_seg"),
                        {"nested": [1, "x"]}),
            "seg-other": (doc["segments"][other].get("future_seg"), "other-seg"),
            "corr-A-MANUAL": (corr.get("A-MANUAL"), "for-A-MANUAL"),
            "corr-A-OTHER": (corr.get("A-OTHER"), "for-A-OTHER"),
            "doc-b-seg": (self.saved_doc("doc-b")["segments"][self.KEY].get("future_seg"),
                          "b-seg"),
        }
        for field, (actual, expected) in checks.items():
            with self.subTest(phase=phase, field=field):
                self.assertEqual(actual, expected)

    def test_unknown_nested_fields_survive_load_save(self):
        other = self.write_future_fields()
        state.ProjectState.load(self.path).save()
        self.assert_future_fields(other, "load-save")

    def test_unknown_nested_fields_survive_switching(self):
        other = self.write_future_fields()
        proj = state.ProjectState.load(self.path)
        proj.switch_document("doc-b", "b.mqxliff")
        proj.switch_document("doc-a", "a.mqxliff")
        proj.save()
        self.assert_future_fields(other, "A-B-A")

    def test_known_field_edits_win_over_stale_copies(self):
        other = self.write_future_fields()
        proj = state.ProjectState.load(self.path)
        proj.segments[self.KEY].user_final = "EDITED-AFTER-LOAD"
        proj.corrections[0].comment = "edited correction comment"
        edited_final = proj.corrections[0].human_final
        proj.save()
        self.assert_future_fields(other, "after-edit")
        doc = self.saved_doc()
        self.assertEqual(doc["segments"][self.KEY]["user_final"], "EDITED-AFTER-LOAD")
        edited = next(c for c in doc["corrections"] if c["human_final"] == edited_final)
        self.assertEqual(edited["comment"], "edited correction comment")
        reloaded = state.ProjectState.load(self.path)
        self.assertEqual(reloaded.segments[self.KEY].user_final, "EDITED-AFTER-LOAD")

    def test_unknown_correction_fields_follow_their_correction_not_its_index(self):
        other = self.write_future_fields()
        proj = state.ProjectState.load(self.path)
        # Insert a new correction in front and remove the old first one: any
        # position-based reattachment would now mislabel the survivors.
        removed = proj.corrections.pop(0)
        proj.corrections.insert(0, state.Correction(
            segment_id="new", source="s", llm_suggestion="", human_final="NEW"))
        proj.save()
        doc = self.saved_doc()
        corr = {c["human_final"]: c for c in doc["corrections"]}
        self.assertNotIn(removed.human_final, corr)
        self.assertNotIn("future_corr", corr["NEW"])
        survivor = "A-OTHER" if removed.human_final == "A-MANUAL" else "A-MANUAL"
        self.assertEqual(corr[survivor]["future_corr"], "for-%s" % survivor)
        self.assertEqual(doc["segments"][other]["future_seg"], "other-seg")

    def test_unknown_segment_fields_do_not_move_to_a_replaced_record(self):
        self.write_future_fields()
        proj = state.ProjectState.load(self.path)
        # Rebinding after the source text changed replaces the record; the old
        # record's unknown data must not be attached to the new one.
        proj.segments[self.KEY].source_text = "stale source"
        self.bind(proj, self.doc_a, "a.mqxliff", r"C:\work\a.mqxliff")
        self.assertEqual(proj.segments[self.KEY].user_final, "")
        proj.save()
        self.assertNotIn("future_seg", self.saved_doc()["segments"][self.KEY])

    def test_reset_active_document_clears_only_that_document(self):
        # Unbound work first, so a retained unbound entry exists too.
        self.bind(self.state, self.doc_a, "a.mqxliff")
        self.state.capture_edits({self.KEY: "UNBOUND"})
        self.open_b()
        self.state.capture_edits({self.KEY: "B-MANUAL"}, {self.KEY: "B-comment"})
        self.open_a()
        self.work_on_a(self.state)
        self.state.vibe = "global vibe"

        self.state.reset_active_document()
        self.assertEqual(self.state.document_id, "doc-a")
        self.assertEqual(self.state.source_path, r"C:\work\a.mqxliff")
        for field, value in {"segments": {}, "order": [], "corrections": [],
                             "analysis": {}, "global_directive": "",
                             "current_batch": 0}.items():
            with self.subTest(field=field):
                self.assertEqual(getattr(self.state, field), value)
        self.state.save()

        reloaded = state.ProjectState.load(self.path)
        self.assertEqual(reloaded.document_id, "doc-a")
        self.assertEqual(reloaded.segments, {})
        self.assertEqual(reloaded.vibe, "global vibe")
        reloaded.switch_document("doc-b", "b.mqxliff")
        self.assertEqual(reloaded.segments[self.KEY].user_final, "B-MANUAL")
        self.assertEqual(reloaded.segments[self.KEY].user_comment, "B-comment")
        with open(self.path, encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertEqual(
            raw["unbound_document"]["segments"][self.KEY]["user_final"], "UNBOUND")

    def test_failed_reset_save_restores_the_active_work(self):
        self.open_a()
        self.work_on_a(self.state)
        original = state.ProjectState.save

        def failing_save(proj):
            raise OSError("simulated")

        state.ProjectState.save = failing_save
        try:
            with self.assertRaises(OSError):
                self.state.reset_active_document_and_save()
        finally:
            state.ProjectState.save = original
        self.assertEqual(self.state.document_id, "doc-a")
        self.assert_a_work(self.state)

    def test_reset_refuses_unbound_and_legacy_state(self):
        with self.assertRaises(ValueError):
            self.state.reset_active_document()
        self.state.switch_document(state.LEGACY_DOCUMENT_ID)
        with self.assertRaises(ValueError):
            self.state.reset_active_document()

    def test_unbound_work_round_trips_and_is_never_transferred(self):
        # Existing callers bind without switching; that work still persists.
        self.bind(self.state, self.doc_a, "a.mqxliff")
        self.state.capture_edits({self.KEY: "UNBOUND"})
        self.state.save()
        reloaded = state.ProjectState.load(self.path)
        self.assertIsNone(reloaded.document_id)
        self.assertEqual(reloaded.segments[self.KEY].user_final, "UNBOUND")

        reloaded.switch_document("doc-b", "b.mqxliff")
        self.assertEqual(reloaded.segments, {})
        reloaded.save()
        with open(self.path, encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertEqual(
            raw["unbound_document"]["segments"][self.KEY]["user_final"], "UNBOUND")

    def test_switch_rejects_an_empty_document_id(self):
        for bad in ("", None):
            with self.subTest(document_id=bad):
                with self.assertRaises(ValueError):
                    self.state.switch_document(bad)

    def test_save_is_atomic_when_serialisation_fails(self):
        self.open_a()
        self.work_on_a(self.state)
        self.state.save()
        with open(self.path, "rb") as handle:
            before = handle.read()
        self.state.analysis["bad"] = object()  # not JSON-serialisable
        with self.assertRaises(TypeError):
            self.state.save()
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), before)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_corrupt_v5_file_is_preserved_and_does_not_raise(self):
        self.open_a()
        self.state.save()
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write('{"version": 5, "documents": {')
        reloaded = state.ProjectState.load(self.path)
        self.assertIsNone(reloaded.document_id)
        self.assertEqual(reloaded.segments, {})
        with open(self.path + ".corrupt", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), '{"version": 5, "documents": {')


class TestLegacyStateMigration(unittest.TestCase):
    """Pre-v5 single-document files: kept whole, never auto-attached."""

    KEY = "1#0"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "state.json")
        doc = load_doc()
        old = state.ProjectState(os.path.join(self.tmp, "scratch.json"))
        old.batch_size = 3
        old.bind_segments(doc.translatable_segments(), "sample.mqxliff",
                          present_keys={s.key for s in doc.segments})
        old.capture_edits({self.KEY: "LEGACY-MANUAL"}, {self.KEY: "legacy note"})
        old.calculate_diffs(0)
        record = old.segments[self.KEY]
        segments = {k: asdict_record(v) for k, v in old.segments.items()}
        segments[self.KEY]["future_segment_field"] = "kept"
        self.legacy = {
            "version": 4,
            "source_file": "sample.mqxliff",
            "source_path": "",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-02T00:00:00",
            "batch_size": 3,
            "current_batch": 1,
            "vibe": "legacy vibe",
            "hard_rules_text": "legacy rules",
            "detailed_feedback": True,
            "analysis": {"0": "legacy analysis"},
            "prompt_templates": {},
            "prompt_stage": "auto",
            "tag_source": "target",
            "global_directive": "legacy directive",
            "auto_merge_proposals": True,
            "rule_log": [],
            "termbase": [{"source": "entropy", "target": "Entropie", "note": ""}],
            "corrections": [{"segment_id": record.segment_id, "source": record.source_text,
                             "llm_suggestion": "", "human_final": "LEGACY-MANUAL",
                             "comment": "legacy note", "batch_index": 0}],
            "order": list(old.order),
            "segments": segments,
            "future_top_level_field": {"nested": [1, 2]},
        }
        self.raw = json.dumps(self.legacy, ensure_ascii=False, indent=1).encode("utf-8")
        with open(self.path, "wb") as handle:
            handle.write(self.raw)
        self.backup = self.path + ".v4.bak"

    def tearDown(self):
        import shutil  # noqa: PLC0415
        shutil.rmtree(self.tmp, ignore_errors=True)

    def file_bytes(self, path=None):
        with open(path or self.path, "rb") as handle:
            return handle.read()

    def assert_legacy_view(self, proj):
        record = proj.segments[self.KEY]
        checks = {
            "document_id": (proj.document_id, state.LEGACY_DOCUMENT_ID),
            "source_file": (proj.source_file, "sample.mqxliff"),
            "user_final": (record.user_final, "LEGACY-MANUAL"),
            "user_comment": (record.user_comment, "legacy note"),
            "edited_by_human": (record.edited_by_human, True),
            "order": (proj.order, self.legacy["order"]),
            "corrections": ([c.human_final for c in proj.corrections], ["LEGACY-MANUAL"]),
            "analysis": (proj.analysis, {"0": "legacy analysis"}),
            "global_directive": (proj.global_directive, "legacy directive"),
            "current_batch": (proj.current_batch, 1),
            "created_at": (proj.created_at, "2026-01-01T00:00:00"),
        }
        for field, (actual, expected) in checks.items():
            with self.subTest(field=field):
                self.assertEqual(actual, expected)

    def test_loading_keeps_the_old_active_view_without_writing(self):
        before = os.stat(self.path).st_mtime_ns
        proj = state.ProjectState.load(self.path)
        self.assert_legacy_view(proj)
        self.assertEqual(proj.vibe, "legacy vibe")
        self.assertEqual(proj.hard_rules_text, "legacy rules")
        self.assertEqual(self.file_bytes(), self.raw)
        self.assertEqual(os.stat(self.path).st_mtime_ns, before)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["state.json"])

    def test_first_save_backs_up_the_legacy_bytes_exactly_once(self):
        proj = state.ProjectState.load(self.path)
        proj.save()
        self.assertEqual(self.file_bytes(self.backup), self.raw)
        with open(self.path, encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertEqual(raw["version"], 5)
        self.assertEqual(raw["active_document"], state.LEGACY_DOCUMENT_ID)
        # Every original field survives verbatim inside the legacy entry.
        entry = raw["documents"][state.LEGACY_DOCUMENT_ID]
        self.assertEqual(entry["legacy_original"], self.legacy)
        self.assertEqual(entry["legacy_original"]["future_top_level_field"],
                         {"nested": [1, 2]})
        # Later saves of the (now v5) file create no further backups.
        proj.save()
        backups = [n for n in os.listdir(self.tmp) if ".bak" in n]
        self.assertEqual(backups, ["state.json.v4.bak"])

    def test_legacy_work_survives_switching_and_save_load(self):
        proj = state.ProjectState.load(self.path)
        proj.switch_document("doc-a", "sample.mqxliff")
        # Same basename and same source text: still NOT attached automatically.
        doc = load_doc()
        proj.bind_segments(doc.translatable_segments(), "sample.mqxliff",
                           present_keys={s.key for s in doc.segments})
        record = proj.segments[self.KEY]
        self.assertEqual(record.user_final, "")
        self.assertFalse(record.edited_by_human)
        self.assertEqual(proj.corrections, [])
        proj.save()

        reloaded = state.ProjectState.load(self.path)
        self.assertEqual(reloaded.document_id, "doc-a")
        self.assertIn(state.LEGACY_DOCUMENT_ID, reloaded.document_ids)
        reloaded.switch_document(state.LEGACY_DOCUMENT_ID, "sample.mqxliff")
        self.assert_legacy_view(reloaded)
        reloaded.save()
        with open(self.path, encoding="utf-8") as handle:
            entry = json.load(handle)["documents"][state.LEGACY_DOCUMENT_ID]
        self.assertEqual(entry["legacy_original"], self.legacy)

    def test_identical_existing_backup_is_reused(self):
        with open(self.backup, "wb") as handle:
            handle.write(self.raw)
        state.ProjectState.load(self.path).save()
        self.assertEqual(self.file_bytes(self.backup), self.raw)
        self.assertFalse(os.path.exists(self.backup + ".1"))

    def test_different_existing_backup_is_kept_and_a_new_one_created(self):
        with open(self.backup, "wb") as handle:
            handle.write(b"an older, different backup")
        with open(self.backup + ".1", "wb") as handle:
            handle.write(b"another different backup")
        state.ProjectState.load(self.path).save()
        self.assertEqual(self.file_bytes(self.backup), b"an older, different backup")
        self.assertEqual(self.file_bytes(self.backup + ".1"), b"another different backup")
        self.assertEqual(self.file_bytes(self.backup + ".2"), self.raw)

    def test_backup_failure_leaves_the_legacy_file_untouched(self):
        proj = state.ProjectState.load(self.path)
        original = state._write_backup

        def failing(path, data):
            raise PermissionError("simulated: backup location not writable")

        state._write_backup = failing
        try:
            with self.assertRaises(state.StateBackupError):
                proj.save()
        finally:
            state._write_backup = original
        self.assertEqual(self.file_bytes(), self.raw)
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        self.assertFalse(os.path.exists(self.backup))

    def test_unverified_backup_blocks_the_overwrite(self):
        proj = state.ProjectState.load(self.path)
        original = state._write_backup
        state._write_backup = lambda path, data: False
        try:
            with self.assertRaises(state.StateBackupError):
                proj.save()
        finally:
            state._write_backup = original
        self.assertEqual(self.file_bytes(), self.raw)

    def test_fresh_state_saved_over_a_legacy_file_backs_it_up(self):
        # Today's "Reset project state" saves a brand-new ProjectState here.
        state.ProjectState(self.path).save()
        self.assertEqual(self.file_bytes(self.backup), self.raw)


def asdict_record(record) -> dict:
    from dataclasses import asdict  # noqa: PLC0415
    return asdict(record)


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


class TestDocumentSnapshotWrappers(unittest.TestCase):
    """Public per-document snapshot API used by workspace-service."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "state.json")

    def tearDown(self):
        import shutil  # noqa: PLC0415
        shutil.rmtree(self.tmp, ignore_errors=True)

    def worked_state(self):
        proj = state.ProjectState(self.path)
        proj.switch_document("doc-a", "a.mqxliff")
        doc = load_doc()
        proj.bind_segments(doc.translatable_segments(), "a.mqxliff",
                           present_keys={s.key for s in doc.segments})
        key = proj.order[0]
        proj.capture_edits({key: "Meine Fassung"}, {key: "Notiz"})
        return proj, key

    def test_snapshot_equals_the_private_snapshot(self):
        proj, _ = self.worked_state()
        self.assertEqual(proj.document_snapshot(), proj._document_snapshot())

    def test_round_trip_keeps_work_and_unknown_fields(self):
        proj, key = self.worked_state()
        entry = proj.document_snapshot()
        entry["future_document_field"] = {"x": 1}
        entry["segments"][key]["future_record_field"] = [1, 2]

        other = state.ProjectState(self.path)
        other.open_document_snapshot("doc-a", json.loads(json.dumps(entry)))
        self.assertEqual(other.document_id, "doc-a")
        record = other.segments[key]
        self.assertEqual((record.user_final, record.user_comment, record.edited_by_human),
                         ("Meine Fassung", "Notiz", True))
        self.assertEqual(other.order, proj.order)
        again = other.document_snapshot()
        self.assertEqual(again["future_document_field"], {"x": 1})
        self.assertEqual(again["segments"][key]["future_record_field"], [1, 2])
        self.assertEqual(again, entry)

    def test_matches_switch_document_view_and_writes_nothing(self):
        proj, key = self.worked_state()
        proj.save()
        loaded = state.ProjectState.load(self.path)
        loaded.switch_document("doc-a", "a.mqxliff")
        before = os.listdir(self.tmp)

        fresh = state.ProjectState(os.path.join(self.tmp, "never-written.json"))
        fresh.open_document_snapshot("doc-a", proj.document_snapshot())
        self.assertEqual({k: vars(r) for k, r in fresh.segments.items()},
                         {k: vars(r) for k, r in loaded.segments.items()})
        self.assertEqual(os.listdir(self.tmp), before)

    def test_open_rejects_bad_input(self):
        proj = state.ProjectState(self.path)
        for document_id, entry in (("", {}), (None, {}), ("doc", None), ("doc", [])):
            with self.assertRaises(ValueError):
                proj.open_document_snapshot(document_id, entry)


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


def _sdk_error(cls, message):
    """An SDK exception instance without the SDK's HTTP-object constructor."""
    err = cls.__new__(cls)
    Exception.__init__(err, message)
    err.message = message
    return err


def _openai_reply(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _anthropic_reply(*texts):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=t) for t in texts])


class _FakeSDK:
    """Stands in for both SDK constructors; records init kwargs and requests."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.init_kwargs = []
        self.requests = []

    def _respond(self, **kwargs):
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0) if self.outcomes else _openai_reply("ok")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def factory(self, **kwargs):
        self.init_kwargs.append(kwargs)
        return SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=self._respond)),
            messages=SimpleNamespace(create=self._respond),
        )


class TestModelProbe(unittest.TestCase):
    """The sidebar Test button: one bounded request, no side effects."""

    def setUp(self):
        sleeper = mock.patch.object(llm.time, "sleep", side_effect=AssertionError(
            "the probe must never back off and retry"))
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def openai_config(self, **overrides):
        values = dict(
            preset="Local Engine (Ollama)", base_url="http://probe.invalid:11434/v1",
            api_key="sk-test-key", model="qwen-selected", temperature=0.3,
            max_tokens=4096, chunk_size=7, timeout=300.0, max_retries=3,
            backend=llm.BACKEND_OPENAI,
        )
        values.update(overrides)
        return llm.ClientConfig(**values)

    def anthropic_config(self, **overrides):
        values = dict(
            preset="Anthropic API", base_url="https://anthropic.invalid",
            api_key="sk-ant-test", model="claude-selected", max_tokens=4096,
            timeout=300.0, max_retries=3, backend=llm.BACKEND_ANTHROPIC,
        )
        values.update(overrides)
        return llm.ClientConfig(**values)

    def run_openai(self, config, *outcomes):
        sdk = _FakeSDK(*outcomes)
        with mock.patch.object(llm, "OpenAI", sdk.factory):
            try:
                return sdk, llm.probe_model(config), None
            except llm.LLMError as exc:
                return sdk, None, exc

    def run_anthropic(self, config, *outcomes):
        if llm.anthropic is None:
            self.skipTest("anthropic not installed")
        sdk = _FakeSDK(*outcomes)
        fake_module = SimpleNamespace(Anthropic=sdk.factory, NOT_GIVEN=object())
        with mock.patch.object(llm, "anthropic", fake_module):
            try:
                return sdk, llm.probe_model(config), None
            except llm.LLMError as exc:
                return sdk, None, exc

    # ---------------------------------------------------------- OpenAI path

    def test_openai_success_reports_a_short_preview(self):
        config = self.openai_config()
        sdk, result, error = self.run_openai(config, _openai_reply("  ok\n " + "x" * 200))
        self.assertIsNone(error)
        self.assertEqual(len(sdk.requests), 1)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, llm.PROBE_OK)
        self.assertTrue(result.preview.startswith("ok x"))
        text = result.message()
        self.assertNotIn("x" * (llm.PROBE_PREVIEW_CHARS + 1), text)
        self.assertIn("not checked", text)  # no quality/JSON claim
        self.assertIn("Requested model: `qwen-selected`", text)
        self.assertIn("Server-reported model: not reported", text)
        self.assertNotIn("HTTP", text)  # no status code is ever observed

    def test_server_reported_model_is_labelled_separately(self):
        reply = _openai_reply("ok")
        reply.model = "qwen3:8b-q4"
        _sdk, result, _error = self.run_openai(self.openai_config(), reply)
        self.assertEqual(result.requested_model, "qwen-selected")
        self.assertEqual(result.server_model, "qwen3:8b-q4")
        text = result.message()
        self.assertIn("Requested model: `qwen-selected`", text)
        self.assertIn("Server-reported model: `qwen3:8b-q4`", text)

    def test_reasoning_only_reply_is_inconclusive_with_metadata(self):
        thinking = "Let me think about how to answer the user carefully"
        reply = SimpleNamespace(
            model="qwen3:8b",
            choices=[SimpleNamespace(
                finish_reason="length",
                message=SimpleNamespace(content="", reasoning=thinking))],
            usage=SimpleNamespace(
                completion_tokens=16,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=16)),
        )
        config = self.openai_config()
        before = dataclasses.asdict(config)
        sdk, result, error = self.run_openai(config, reply, _openai_reply("ok"))
        self.assertIsNone(error)
        self.assertEqual(len(sdk.requests), 1)
        self.assertEqual(result.status, llm.PROBE_INCONCLUSIVE)
        self.assertFalse(result.ok)
        self.assertEqual(result.finish_reason, "length")
        self.assertEqual(result.output_tokens, 16)
        self.assertEqual(result.reasoning_tokens, 16)
        self.assertTrue(result.reasoning_present)
        self.assertEqual(result.reasoning_chars, len(thinking))
        text = result.message()
        self.assertNotIn(thinking, text)
        self.assertNotIn("think about", text)
        self.assertIn("finish reason `length`", text)
        self.assertIn("reasoning text present (%d chars, not shown)" % len(thinking), text)
        self.assertEqual(dataclasses.asdict(config), before)

    def test_odd_or_missing_metadata_never_breaks_the_probe(self):
        reply = SimpleNamespace(
            model=12345,
            choices=[SimpleNamespace(
                finish_reason=None,
                message=SimpleNamespace(content="ok", reasoning=["not", "text"]))],
            usage=SimpleNamespace(completion_tokens="many", completion_tokens_details=None),
        )
        _sdk, result, error = self.run_openai(self.openai_config(), reply)
        self.assertIsNone(error)
        self.assertTrue(result.ok)
        self.assertEqual(result.server_model, "")
        self.assertEqual(result.finish_reason, "")
        self.assertIsNone(result.output_tokens)
        self.assertIsNone(result.reasoning_tokens)
        self.assertFalse(result.reasoning_present)
        self.assertNotIn("Response metadata", result.message())

    def test_labels_are_sanitized_and_never_leak_secrets(self):
        config = self.openai_config(
            base_url="https://user:pa55word@Proxy.example:8443/v1?api_key=sk-query#frag",
            api_key="sk-sidebar-secret")
        sdk, result, error = self.run_openai(config, _openai_reply("ok"))
        self.assertIsNone(error)
        # The SDK still receives the endpoint exactly as configured.
        self.assertEqual(sdk.init_kwargs[0]["base_url"], config.base_url.rstrip("/"))
        for text in (result.message(), llm.connection_summary(config)):
            with self.subTest(text=text[:30]):
                self.assertIn("https://proxy.example:8443/v1", text)
                for secret in ("pa55word", "user:", "sk-query", "frag", "sk-sidebar-secret"):
                    self.assertNotIn(secret, text)
                self.assertIn("Credentials: sidebar key", text)

    def test_sanitize_url_edge_cases(self):
        self.assertEqual(llm.sanitize_url(""), "")
        self.assertEqual(llm.sanitize_url("http://localhost:11434/v1/"),
                         "http://localhost:11434/v1/")
        self.assertEqual(llm.sanitize_url("not a url"), "(unparseable URL)")
        self.assertEqual(llm.sanitize_url("http://host:notaport/v1"), "(unparseable URL)")

    def test_credential_source_labels(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(llm.credential_source(self.openai_config()), "sidebar key")
            self.assertIn("local placeholder",
                          llm.credential_source(self.openai_config(api_key="")))
            self.assertIn(llm.CREDENTIAL_UNDETERMINED,
                          llm.credential_source(self.anthropic_config(api_key="")))
            with mock.patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "tok-secret"}):
                label = llm.credential_source(self.anthropic_config(api_key=""))
                self.assertIn("ANTHROPIC_AUTH_TOKEN environment variable", label)
                self.assertNotIn("tok-secret", label)
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-env-secret",
                                              "ANTHROPIC_AUTH_TOKEN": "tok-secret"}):
                label = llm.credential_source(self.anthropic_config(api_key=""))
                self.assertIn("ANTHROPIC_API_KEY environment variable", label)
                self.assertNotIn("secret", label)
            # A typed key is what the SDK uses, whatever the environment holds.
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-env-secret"}):
                self.assertEqual(llm.credential_source(self.anthropic_config()), "sidebar key")

    def test_openai_uses_selected_model_endpoint_key_and_probe_bounds(self):
        config = self.openai_config()
        sdk, _message, error = self.run_openai(config, _openai_reply("ok"))
        self.assertIsNone(error)
        init = sdk.init_kwargs[0]
        self.assertEqual(init["base_url"], "http://probe.invalid:11434/v1")
        self.assertEqual(init["api_key"], "sk-test-key")
        self.assertEqual(init["max_retries"], 0)
        self.assertLessEqual(init["timeout"], 30)
        request = sdk.requests[0]
        self.assertEqual(request["model"], "qwen-selected")
        self.assertEqual(request["max_tokens"], llm.PROBE_MAX_TOKENS)
        self.assertLessEqual(llm.PROBE_MAX_TOKENS, 32)
        self.assertNotIn("response_format", request)
        self.assertEqual(request["messages"], [dict(m) for m in llm.PROBE_MESSAGES])

    def test_openai_keyless_local_endpoint_keeps_placeholder_key(self):
        sdk, _message, error = self.run_openai(self.openai_config(api_key=""), _openai_reply("ok"))
        self.assertIsNone(error)
        self.assertEqual(sdk.init_kwargs[0]["api_key"], "not-needed")

    def test_shorter_operator_timeout_is_kept(self):
        sdk, _message, _error = self.run_openai(self.openai_config(timeout=10.0), _openai_reply("ok"))
        self.assertEqual(sdk.init_kwargs[0]["timeout"], 10.0)

    def test_config_is_unchanged_after_success_and_every_failure(self):
        import openai
        outcomes = [
            _openai_reply("ok"),
            _openai_reply(""),
            "<html>not json</html>",
            _sdk_error(openai.BadRequestError, "Unsupported parameter: 'max_tokens'"),
            _sdk_error(openai.APITimeoutError, "Request timed out."),
        ]
        for outcome in outcomes:
            with self.subTest(outcome=repr(outcome)[:40]):
                config = self.openai_config()
                before = dataclasses.asdict(config)
                self.run_openai(config, outcome)
                self.assertEqual(dataclasses.asdict(config), before)

    def test_rejected_token_limit_fails_after_one_capped_request(self):
        import openai
        config = self.openai_config()
        sdk, message, error = self.run_openai(
            config,
            _sdk_error(openai.BadRequestError,
                       "Error code: 400 - Unsupported parameter: 'max_tokens'"),
            _openai_reply("would only be reached by an uncapped retry"),
        )
        self.assertIsNone(message)
        self.assertIsNotNone(error)
        self.assertEqual(len(sdk.requests), 1)
        self.assertEqual(sdk.requests[0]["max_tokens"], llm.PROBE_MAX_TOKENS)
        self.assertIn("max_tokens", str(error))
        self.assertIn("does not retry", str(error))
        self.assertEqual(config.max_tokens, 4096)

    def test_empty_output_is_inconclusive_after_one_request(self):
        for content in ("", "   \n", None):
            with self.subTest(content=content):
                config = self.openai_config()
                before = dataclasses.asdict(config)
                sdk, result, error = self.run_openai(
                    config, _openai_reply(content), _openai_reply("ok"))
                self.assertIsNone(error)
                self.assertEqual(len(sdk.requests), 1)
                self.assertEqual(result.status, llm.PROBE_INCONCLUSIVE)
                text = result.message()
                self.assertIn("no visible text", text)
                self.assertIn("%d-token probe" % llm.PROBE_MAX_TOKENS, text)
                self.assertIn("does not mean the model is unavailable", text)
                self.assertNotIn("HTTP", text)
                self.assertEqual(dataclasses.asdict(config), before)

    def test_transport_and_server_errors_are_not_retried(self):
        import openai
        cases = [
            (openai.APITimeoutError, "Request timed out.", "No reply within"),
            (openai.APIConnectionError, "Connection error.", "Could not connect"),
            (openai.RateLimitError, "Error code: 429", "rate-limited"),
            (openai.InternalServerError, "Error code: 503", "server error"),
            (openai.AuthenticationError, "Error code: 401", "credentials"),
            (openai.NotFoundError, "Error code: 404 model not found", "not found"),
        ]
        for cls, raw, lead in cases:
            with self.subTest(error=cls.__name__):
                sdk, message, error = self.run_openai(
                    self.openai_config(), _sdk_error(cls, raw), _openai_reply("ok"))
                self.assertIsNone(message)
                self.assertEqual(len(sdk.requests), 1)
                self.assertIn(lead, str(error))
                self.assertIn(raw, str(error))

    def test_malformed_openai_responses_fail_cleanly(self):
        import openai
        cases = [
            "<html>gateway page</html>",
            SimpleNamespace(),
            SimpleNamespace(choices=None),
            SimpleNamespace(choices=[]),
            SimpleNamespace(choices=[SimpleNamespace()]),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=["x"]))]),
            _sdk_error(openai.APIResponseValidationError, "Data returned by API invalid"),
        ]
        for outcome in cases:
            with self.subTest(outcome=repr(outcome)[:50]):
                sdk, message, error = self.run_openai(self.openai_config(), outcome)
                self.assertIsNone(message)
                self.assertIsInstance(error, llm.LLMError)
                self.assertIn("response format", str(error))
                self.assertEqual(len(sdk.requests), 1)

    def test_programming_errors_are_not_swallowed(self):
        sdk = _FakeSDK(ValueError("bug in caller"))
        with mock.patch.object(llm, "OpenAI", sdk.factory):
            with self.assertRaises(ValueError):
                llm.probe_model(self.openai_config())

    def test_ping_delegates_to_the_bounded_probe(self):
        config = self.openai_config()
        sdk = _FakeSDK(_openai_reply("ok"))
        with mock.patch.object(llm, "OpenAI", sdk.factory):
            client = llm.LLMClient(config)
            self.assertIn("ok", client.ping())
        self.assertEqual(sdk.requests[0]["max_tokens"], llm.PROBE_MAX_TOKENS)
        self.assertLessEqual(sdk.init_kwargs[-1]["timeout"], 30)
        self.assertEqual(config.timeout, 300.0)

    # ------------------------------------------------------- Anthropic path

    def test_anthropic_success_uses_selected_values_and_bounds(self):
        config = self.anthropic_config()
        before = dataclasses.asdict(config)
        sdk, result, error = self.run_anthropic(config, _anthropic_reply("o", "k"))
        self.assertIsNone(error)
        self.assertTrue(result.ok)
        self.assertEqual(result.preview, "ok")
        init = sdk.init_kwargs[0]
        self.assertEqual(init["base_url"], "https://anthropic.invalid")
        self.assertEqual(init["api_key"], "sk-ant-test")
        self.assertEqual(init["max_retries"], 0)
        self.assertLessEqual(init["timeout"], 30)
        self.assertEqual(len(sdk.requests), 1)
        request = sdk.requests[0]
        self.assertEqual(request["model"], "claude-selected")
        self.assertEqual(request["max_tokens"], llm.PROBE_MAX_TOKENS)
        self.assertNotIn("temperature", request)
        sent = [{"role": "system", "content": request["system"]}] + request["messages"]
        self.assertEqual(sent, [dict(m) for m in llm.PROBE_MESSAGES])
        self.assertEqual(dataclasses.asdict(config), before)

    def test_anthropic_empty_key_keeps_sdk_environment_fallback(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        with mock.patch.dict(os.environ, env, clear=True):
            sdk, result, error = self.run_anthropic(
                self.anthropic_config(api_key=""), _anthropic_reply("ok"))
        self.assertIsNone(error)
        self.assertIsNone(sdk.init_kwargs[0]["api_key"])
        # The fallback stays enabled, but is made visible rather than silent.
        self.assertIn(llm.CREDENTIAL_UNDETERMINED, result.credential_source)
        self.assertIn(llm.CREDENTIAL_UNDETERMINED, result.message())

    def test_anthropic_thinking_only_reply_is_inconclusive(self):
        reply = SimpleNamespace(
            model="claude-sonnet-5-20261001", stop_reason="max_tokens",
            usage=SimpleNamespace(output_tokens=16),
            content=[SimpleNamespace(type="thinking", thinking="private chain")],
        )
        sdk, result, error = self.run_anthropic(self.anthropic_config(), reply)
        self.assertIsNone(error)
        self.assertEqual(len(sdk.requests), 1)
        self.assertEqual(result.status, llm.PROBE_INCONCLUSIVE)
        self.assertEqual(result.finish_reason, "max_tokens")
        self.assertEqual(result.output_tokens, 16)
        self.assertEqual(result.server_model, "claude-sonnet-5-20261001")
        self.assertEqual(result.requested_model, "claude-selected")
        self.assertTrue(result.reasoning_present)
        self.assertNotIn("private chain", result.message())

    def test_anthropic_empty_reply_is_inconclusive(self):
        sdk, result, error = self.run_anthropic(
            self.anthropic_config(), _anthropic_reply(""), _anthropic_reply("ok"))
        self.assertIsNone(error)
        self.assertEqual(len(sdk.requests), 1)
        self.assertEqual(result.status, llm.PROBE_INCONCLUSIVE)

    def test_anthropic_failures_are_single_request(self):
        import anthropic
        cases = [
            SimpleNamespace(content=None),
            _sdk_error(anthropic.APITimeoutError, "Request timed out."),
            _sdk_error(anthropic.RateLimitError, "Error code: 429"),
            _sdk_error(anthropic.InternalServerError, "Error code: 529 overloaded"),
        ]
        for outcome in cases:
            with self.subTest(outcome=repr(outcome)[:50]):
                config = self.anthropic_config()
                before = dataclasses.asdict(config)
                sdk, message, error = self.run_anthropic(config, outcome, _anthropic_reply("ok"))
                self.assertIsNone(message)
                self.assertIsInstance(error, llm.LLMError)
                self.assertEqual(len(sdk.requests), 1)
                self.assertEqual(dataclasses.asdict(config), before)

    def test_anthropic_missing_credentials_is_reported(self):
        sdk, message, error = self.run_anthropic(
            self.anthropic_config(api_key=""),
            TypeError('"Could not resolve authentication method. Expected one of api_key"'))
        self.assertIsNone(message)
        self.assertIn("API key", str(error))

    def test_anthropic_unrelated_type_errors_propagate(self):
        if llm.anthropic is None:
            self.skipTest("anthropic not installed")
        sdk = _FakeSDK(TypeError("unexpected keyword argument 'foo'"))
        fake_module = SimpleNamespace(Anthropic=sdk.factory, NOT_GIVEN=object())
        with mock.patch.object(llm, "anthropic", fake_module):
            with self.assertRaises(TypeError):
                llm.probe_model(self.anthropic_config())


class TestTranslationRetryUnchanged(unittest.TestCase):
    """Characterises the normal request path so the probe work cannot drift it."""

    def make_openai_client(self, create, **config):
        client = llm.LLMClient.__new__(llm.LLMClient)
        client.config = llm.ClientConfig(backend=llm.BACKEND_OPENAI, **config)
        client._supports_json_mode = None
        client._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        return client

    def test_rejected_max_tokens_still_falls_back_to_server_default(self):
        calls = []

        def create(**kwargs):
            calls.append(dict(kwargs))
            if len(calls) == 1:
                raise RuntimeError("Unsupported parameter: 'max_tokens'")
            return _openai_reply("translated")

        client = self.make_openai_client(create, max_tokens=4096)
        self.assertEqual(client._chat([{"role": "user", "content": "x"}], False), "translated")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["max_tokens"], 4096)
        self.assertNotIn("max_tokens", calls[1])
        self.assertEqual(client.config.max_tokens, 0)

    def test_retryable_errors_are_still_retried(self):
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            if len(calls) < 3:
                raise RuntimeError("Error code: 503 temporarily unavailable")
            return _openai_reply("translated")

        client = self.make_openai_client(create, max_retries=3)
        with mock.patch.object(llm.time, "sleep") as sleep:
            out = client._chat([{"role": "user", "content": "x"}], False)
        self.assertEqual(out, "translated")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_empty_translation_reply_is_still_retried(self):
        replies = [_openai_reply(""), _openai_reply("translated")]
        client = self.make_openai_client(lambda **kw: replies.pop(0), max_retries=3)
        self.assertEqual(client._chat([{"role": "user", "content": "x"}], False), "translated")

    def test_anthropic_retryable_errors_are_still_retried(self):
        if llm.anthropic is None:
            self.skipTest("anthropic not installed")
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            if len(calls) < 2:
                raise RuntimeError("overloaded")
            return _anthropic_reply("translated")

        client = llm.LLMClient.__new__(llm.LLMClient)
        client.config = llm.ClientConfig(backend=llm.BACKEND_ANTHROPIC, max_retries=3)
        client._supports_json_mode = None
        client._client = SimpleNamespace(messages=SimpleNamespace(create=create))
        with mock.patch.object(llm.time, "sleep"):
            out = client._chat([{"role": "user", "content": "x"}], False)
        self.assertEqual(out, "translated")
        self.assertEqual(len(calls), 2)


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
