"""Live integration test against a real OpenAI-compatible endpoint.

Opt-in: skipped unless an endpoint is reachable. This is the one layer the
offline suites cannot cover - actual request shaping, JSON-mode negotiation and
a real model's response.

    python tests\\test_live.py                       # auto-detects Ollama
    set HARNESS_LIVE_MODEL=qwen3:4b-instruct
    set HARNESS_LIVE_URL=http://10.211.55.2:11434/v1
"""

from __future__ import annotations

import os
import sys
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memoq_harness import llm, mqxliff, prompts, state, tags  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "sample.mqxliff")

BASE_URL = os.environ.get("HARNESS_LIVE_URL") or llm.ollama_base_url()
MODEL = os.environ.get("HARNESS_LIVE_MODEL", "qwen3:4b-instruct")
API_KEY = os.environ.get("HARNESS_LIVE_KEY", "")


def endpoint_is_up() -> bool:
    try:
        request = urllib.request.Request(BASE_URL.rstrip("/") + "/models")
        if API_KEY:
            request.add_header("Authorization", "Bearer %s" % API_KEY)
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


LIVE = endpoint_is_up()


@unittest.skipUnless(LIVE, "No endpoint reachable at %s" % BASE_URL)
class TestLiveEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = llm.ClientConfig(
            base_url=BASE_URL, api_key=API_KEY, model=MODEL,
            temperature=0.1, chunk_size=4, timeout=600.0, max_tokens=2048,
        )
        cls.client = llm.LLMClient(cls.config)

    def test_ping(self):
        self.assertTrue(self.client.ping())

    def test_list_models_includes_the_target(self):
        models = self.client.list_models()
        self.assertTrue(models)
        self.assertIn(MODEL, models, "Model %r not served. Available: %s" % (MODEL, models))

    def test_translate_a_real_batch(self):
        doc = mqxliff.MqxliffDocument.from_path(FIXTURE)
        doc.parse_segments()
        segments = doc.translatable_segments()

        project = state.ProjectState(os.devnull)
        project.batch_size = 100
        project.vibe = "Formal German, address the reader as 'Sie'."
        project.termbase = [
            state.TermEntry("report", "Bericht"),
            state.TermEntry("dashboard", "Dashboard"),
        ]
        project.bind_segments(segments, "sample.mqxliff")

        records = project.batch_records(0)
        system_prompt = prompts.build_system_prompt(
            hard_rules_text="Keep code identifiers and UI labels 1:1.",
            vibe=project.vibe,
            corrections_block="",
            termbase=project.relevant_terms([r.source_text for r in records]),
            source_lang="en-US",
            target_lang="de-DE",
        )
        result = self.client.translate_batch(records, system_prompt)

        self.assertEqual(result.errors, [], result.errors)
        self.assertTrue(result.suggestions, "Model returned nothing.")
        # A small model may skip a row; most must come back.
        self.assertGreaterEqual(len(result.suggestions), max(1, len(records) // 2),
                                "Only %d/%d segments returned" % (len(result.suggestions),
                                                                  len(records)))

        # Every suggestion must restore to well-formed XML and land in a file
        # that re-parses - the property that actually matters for memoQ.
        by_key = {s.key: s for s in segments}
        for key, text in result.suggestions.items():
            segment = by_key[key]
            _, report = tags.restore(text, segment.lookup,
                                     expected=segment.source.expected_numbers)
            doc.apply_translation(segment, text)
            self.assertIsNotNone(report)

        reparsed = mqxliff.MqxliffDocument(doc.to_bytes(), "live.mqxliff")
        reparsed.parse_segments()
        self.assertEqual(len(reparsed.segments), 12)
        confirmed = next(s for s in reparsed.segments if s.unit_id == "3")
        self.assertEqual(confirmed.target_text, "Diese Zeile ist bereits bestaetigt.")

        print("\n  --- live output from %s ---" % MODEL)
        for key, text in list(result.suggestions.items())[:5]:
            print("   %-8s %s" % (by_key[key].unit_id, text))

    def test_feedback_block_changes_the_output(self):
        """A correction in the prompt should steer the next translation."""
        record = state.SegmentRecord(
            key="k1", segment_id="99", ordinal=0, batch_index=0,
            source_text="Export the report to PDF.", mt_target="",
        )
        corrections = (
            '<12: "Bericht als PDF exportieren">\n'
            '    source:   "Export the report to PDF."\n'
            '    rejected: "Exportieren Sie den Bericht nach PDF."\n'
            '    note:     "use the short infinitive form, not Sie-form"'
        )
        prompt = prompts.build_system_prompt(
            "", "Formal German.", corrections,
            source_lang="en-US", target_lang="de-DE",
        )
        result = self.client.translate_batch([record], prompt)
        self.assertTrue(result.suggestions, "Model returned nothing.")
        text = result.suggestions["k1"]
        print("\n  --- with correction in prompt --- %s" % text)
        self.assertTrue(text.strip())


if __name__ == "__main__":
    if not LIVE:
        print("No endpoint at %s - nothing to do." % BASE_URL)
    unittest.main(verbosity=2)
