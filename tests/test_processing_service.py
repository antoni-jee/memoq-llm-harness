"""Unit tests for processing-service (offline; network blocked).

Real engines are exercised only through a patched ``LLMClient._chat``; no
test reaches a provider.

Run with:  python -m unittest -v tests.test_processing_service
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient  # noqa: E402

from memoq_harness import defaults, llm, mqxliff, prompts  # noqa: E402
from memoq_harness import state as state_mod  # noqa: E402
from services.document_service.app import create_app as create_document_app  # noqa: E402
from services.document_service.service import DocumentService  # noqa: E402
from services.document_service.storage import FileStorage  # noqa: E402
from services.processing_service import config, engines  # noqa: E402
from services.processing_service.app import create_app  # noqa: E402
from services.processing_service.service import (ModelConfig,  # noqa: E402
                                                 ProcessingService)
from tests.test_document_service import SAMPLE, FakeRepository  # noqa: E402

SECRET_TEXT = "TOPSECRET-PROCESSING-TEXT"
FAKE_KEY = "sk-test-processing-key-000000000000"


def config_block(preset="OpenAI API", **overrides):
    """A per-request config block as the Streamlit app sends it.

    The default is a key-required OpenAI preset pointed at a dead local port;
    the request, not the service environment, selects this backend/endpoint/model.
    """
    block = {
        "preset": preset,
        "backend": "openai",
        "base_url": "http://127.0.0.1:9/v1",
        "api_key": FAKE_KEY,
        "model": "test-model",
    }
    block.update(overrides)
    return block


def segments_from(raw=SAMPLE, tag_source="target"):
    """What a caller sends: the workspace records of the active rows."""
    doc = mqxliff.MqxliffDocument.from_bytes(raw, "sample.mqxliff")
    return [{"key": s.key, "source_text": s.source_text, "mt_target": s.target_text}
            for s in doc.parse_segments(tag_source=tag_source) if s.translatable]


def blocked(*_args, **_kwargs):
    raise AssertionError("network access attempted in an offline test")


class ProcessingTestCase(unittest.TestCase):
    env: dict = {}

    def setUp(self):
        # Block outbound connections the way HTTP clients make them (name
        # resolution and create_connection). socket.connect itself stays
        # usable because asyncio's internal socketpair needs it on Windows.
        for target in ("create_connection", "getaddrinfo"):
            patcher = mock.patch.object(socket, target, blocked)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.settings = config.load_settings(self.env)
        self.service = ProcessingService(self.settings)
        self.client = TestClient(create_app(self.service), raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def process(self, **body):
        body.setdefault("segments", segments_from())
        return self.client.post("/process", json=body)


class TestStub(ProcessingTestCase):
    def test_stub_is_the_default_and_deterministic(self):
        first = self.process()
        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()
        self.assertEqual((body["engine"], body["operation"], body["tag_source"]),
                         ("stub", "batch", "target"))
        self.assertEqual(body["errors"], [])
        self.assertEqual(self.process().json()["suggestions"], body["suggestions"])

    def test_stub_rule_per_tag_source_with_empty_target_fallback(self):
        segs = {s["key"]: s for s in segments_from()}
        target = self.process(tag_source="target").json()["suggestions"]
        source = self.process(tag_source="source").json()["suggestions"]
        self.assertEqual(target["1#0"]["text"], "MT " + segs["1#0"]["mt_target"])
        self.assertEqual(source["1#0"]["text"], "MT " + segs["1#0"]["source_text"])
        self.assertEqual(segs["9#8"]["mt_target"], "")            # empty target in fixture
        self.assertEqual(target["9#8"]["text"], "MT " + segs["9#8"]["source_text"])

    def test_suggestions_have_workspace_submission_shape(self):
        body = self.process().json()
        for item in body["suggestions"].values():
            self.assertEqual(set(item), {"text"})
            self.assertTrue(item["text"].strip())

    def test_segment_with_no_text_is_missing(self):
        body = self.process(segments=[
            {"key": "a", "source_text": "Hello", "mt_target": ""},
            {"key": "b", "source_text": "  ", "mt_target": ""}]).json()
        self.assertEqual(list(body["suggestions"]), ["a"])
        self.assertEqual(body["missing"], ["b"])

    def test_stub_output_passes_document_service_tag_reports_in_both_modes(self):
        """The stub rule checked against the real tag-report logic."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        doc_service = DocumentService(FakeRepository(), FileStorage(os.path.join(tmp, "s")),
                                      1024 * 1024, "0002")
        with TestClient(create_document_app(doc_service)) as documents:
            original = documents.post("/documents", files={
                "file": ("sample.mqxliff", SAMPLE, "application/xml")}).json()
            for tag_source in ("target", "source"):
                with self.subTest(tag_source=tag_source):
                    texts = {k: v["text"] for k, v in self.process(
                        tag_source=tag_source,
                        segments=segments_from(tag_source=tag_source)).json()
                        ["suggestions"].items()}
                    self.assertIn("9#8", texts)  # empty-target row included
                    reports = documents.post(
                        "/originals/%s/tag-reports" % original["original_id"],
                        json={"tag_source": tag_source, "texts": texts}).json()["reports"]
                    self.assertEqual(reports, {k: "" for k in texts}, reports)


class TestOperations(ProcessingTestCase):
    def test_batch_uses_translate_batch_even_for_one_segment(self):
        one = segments_from()[:1]
        with mock.patch.object(engines.StubClient, "translate_batch",
                               wraps=engines.StubClient(tag_source="target").translate_batch) as tb, \
                mock.patch.object(engines.StubClient, "translate_one") as to:
            self.assertEqual(self.process(segments=one).status_code, 200)
        self.assertEqual(tb.call_count, 1)
        to.assert_not_called()

    def test_rerun_uses_translate_one_and_requires_exactly_one(self):
        one = segments_from()[:1]
        with mock.patch.object(engines.StubClient, "translate_batch") as tb:
            response = self.process(operation="rerun", segments=one)
        self.assertEqual(response.status_code, 200, response.text)
        tb.assert_not_called()
        self.assertEqual(response.json()["operation"], "rerun")
        self.assertEqual(list(response.json()["suggestions"]), [one[0]["key"]])
        self.assertEqual(self.process(operation="rerun", segments=segments_from()[:2])
                         .status_code, 400)


class TestPromptContext(ProcessingTestCase):
    def capture(self, **body):
        seen = {}

        def spy(stub, records, system_prompt, progress=None):
            seen["prompt"] = system_prompt
            return original(stub, records, system_prompt)

        original = engines.StubClient.translate_batch
        with mock.patch.object(engines.StubClient, "translate_batch", spy):
            response = self.process(**body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json(), seen["prompt"]

    def test_empty_context_uses_the_shipped_defaults(self):
        segs = segments_from()
        body, prompt = self.capture(segments=segs)
        expected = prompts.build_batch_prompt(
            hard_rules_text=defaults.DEFAULT_HARD_RULES, vibe=defaults.DEFAULT_VIBE,
            termbase=[], corrections_block="", source_lang="", target_lang="",
            directive="", template=state_mod.default_prompt_templates()["mt1"],
            batch_index=0, batch_count=1, segment_count=len(segs))
        self.assertEqual(prompt, expected)
        self.assertEqual(body["stage"], "mt1")
        self.assertEqual(body["system_prompt_chars"], len(expected))

    def test_context_is_applied_with_the_app_termbase_filter(self):
        segs = [{"key": "1#0", "source_text": "Open the dashboard now", "mt_target": ""}]
        body, prompt = self.capture(segments=segs, batch={"index": 1, "count": 3}, context={
            "hard_rules_text": "MY RULES", "vibe": "MY VIBE", "directive": "MY DIRECTIVE",
            "source_lang": "English", "target_lang": "German",
            "termbase": [{"source": "dashboard", "target": "Armaturenbrett"},
                         {"source": "printer", "target": "Drucker"}]})
        self.assertEqual(body["stage"], "mt2")
        for text in ("MY RULES", "MY VIBE", "MY DIRECTIVE", "Armaturenbrett"):
            self.assertIn(text, prompt)
        self.assertNotIn("Drucker", prompt)

    def test_stage_and_template_override(self):
        body, _ = self.capture(batch={"index": 2, "count": 3})
        self.assertEqual(body["stage"], "mt3")
        body, prompt = self.capture(context={"stage": "init",
                                             "template": "CUSTOM batch {{BATCH_NUMBER}}"})
        self.assertEqual(body["stage"], "init")
        self.assertIn("CUSTOM batch 1", prompt)


class TestValidation(ProcessingTestCase):
    def test_invalid_requests_are_rejected_whole(self):
        good = segments_from()[:1]
        cases = [
            [], {}, {"segments": []}, {"segments": "x"},
            {"segments": good, "operation": "translate"},
            {"segments": good, "tag_source": "both"},
            {"segments": good, "engine": ""},
            {"segments": good, "batch": {"index": 0}},
            {"segments": good, "batch": {"index": 3, "count": 3}},
            {"segments": good, "batch": {"index": -1, "count": 3}},
            {"segments": good, "context": []},
            {"segments": good, "context": {"vibe": 5}},
            {"segments": good, "context": {"stage": "mt9"}},
            {"segments": good, "context": {"template": 3}},
            {"segments": good, "context": {"termbase": [{"source": "a"}]}},
            {"segments": good, "context": {"extra": 1}},
            {"segments": [{"key": "a", "source_text": "x"}]},
            {"segments": [{"key": "", "source_text": "x", "mt_target": ""}]},
            {"segments": [dict(good[0], extra="x")]},
            {"segments": [good[0], good[0]]},
            {"segments": good, "base_url": "http://evil.invalid", "api_key": "x"},
        ]
        for body in cases:
            with self.subTest(body=repr(body)[:70]):
                response = self.client.post("/process", json=body)
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.client.post("/process", content=b"{}",
                                          headers={"content-type": "text/plain"}).status_code,
                         415)

    def test_limits(self):
        many = [{"key": "k%d" % i, "source_text": "x", "mt_target": ""} for i in range(201)]
        self.assertEqual(self.process(segments=many).status_code, 400)
        self.service.max_body_bytes = 2048
        big = [{"key": "a", "source_text": "x" * 5000, "mt_target": ""}]
        self.assertEqual(self.process(segments=big).status_code, 413)

    def test_unconfigured_engine_is_refused(self):
        response = self.process(engine="openai")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["details"], {"available": ["stub"]})


class TestRealEngine(ProcessingTestCase):
    env = {config.ENV_ENGINE: "openai", config.ENV_MODEL: "test-model",
           config.ENV_BASE_URL: "http://127.0.0.1:9/v1", config.ENV_API_KEY: FAKE_KEY,
           config.ENV_CHUNK_SIZE: "1"}

    def setUp(self):
        super().setUp()
        if llm.OpenAI is None and self._testMethodName != "test_missing_sdk_blocks_only_that_engine":
            self.skipTest("openai SDK not installed (see requirements-engines.txt)")

    def chat(self, *outcomes):
        patcher = mock.patch.object(llm.LLMClient, "_chat", side_effect=list(outcomes))
        fake = patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_configured_engine_is_default_and_stub_still_available(self):
        segs = [{"key": "1#0", "source_text": "Hello", "mt_target": ""}]
        self.chat('{"segments": [{"id": "1#0", "target": "Hallo"}]}')
        body = self.process(segments=segs).json()
        self.assertEqual((body["engine"], body["suggestions"]), ("openai", {"1#0": {"text": "Hallo"}}))
        self.assertEqual(self.process(segments=segs, engine="stub").json()["engine"], "stub")

    def test_partial_failure_returns_sanitized_errors(self):
        segs = [{"key": "a", "source_text": "One", "mt_target": ""},
                {"key": "b", "source_text": "Two", "mt_target": ""}]
        self.chat('{"segments": [{"id": "a", "target": "Eins"}]}',
                  llm.LLMError("provider said %s with key %s" % (SECRET_TEXT, FAKE_KEY)))
        response = self.process(segments=segs)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["suggestions"], {"a": {"text": "Eins"}})
        self.assertEqual(body["missing"], ["b"])
        self.assertEqual(body["errors"],
                         ["1 of 2 engine request(s) failed; their segments are listed as missing."])
        self.assertNotIn(SECRET_TEXT, response.text)
        self.assertNotIn(FAKE_KEY, response.text)

    def test_total_failure_is_502_without_provider_text(self):
        segs = [{"key": "a", "source_text": "One", "mt_target": ""}]
        self.chat(llm.LLMError(SECRET_TEXT))
        with self.assertLogs("processing-service", level="DEBUG") as logs:
            response = self.process(segments=segs)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "engine_failed")
        self.assertEqual(response.json()["details"], {"failed_requests": 1, "requests": 1})
        self.assertNotIn(SECRET_TEXT, response.text)
        self.assertNotIn(SECRET_TEXT, "\n".join(logs.output))

    def test_rerun_paths(self):
        one = [{"key": "a", "source_text": "One", "mt_target": ""}]
        self.chat('{"segments": [{"id": "a", "target": "Eins"}]}', "not json at all",
                  llm.LLMError(SECRET_TEXT))
        self.assertEqual(self.process(operation="rerun", segments=one).json()["suggestions"],
                         {"a": {"text": "Eins"}})
        self.assertEqual(self.process(operation="rerun", segments=one).json()["missing"], ["a"])
        failed = self.process(operation="rerun", segments=one)
        self.assertEqual(failed.status_code, 502)
        self.assertNotIn(SECRET_TEXT, failed.text)

    def test_missing_sdk_blocks_only_that_engine(self):
        with mock.patch.object(llm, "OpenAI", None):
            unavailable = self.process(segments=segments_from()[:1])
            self.assertEqual(unavailable.status_code, 503)
            self.assertEqual(unavailable.json()["error"], "engine_unavailable")
            self.assertEqual(self.process(segments=segments_from()[:1], engine="stub")
                             .status_code, 200)
            ready = self.client.get("/readyz")
            self.assertEqual(ready.status_code, 503)
            self.assertEqual(ready.json()["checks"], {"engine": "sdk_missing"})
            self.assertEqual(self.client.get("/healthz").status_code, 200)


class TestPerRequestConfig(ProcessingTestCase):
    """A per-request model config selects the backend/endpoint/model.

    The service's own environment is the offline stub here, so a request that
    carries a config and comes back as that config's backend proves the config
    won and the stub/env were not used as a fallback. Real calls are patched
    (``LLMClient._chat``, ``LLMClient.list_models``, ``llm.probe_model``), so no
    test reaches a provider.
    """

    def capture_config(self):
        """Capture the ClientConfig a real client was built from.

        Per-request config is not built through ``client_factory``; the service
        constructs ``llm.LLMClient`` directly, so we spy its ``__init__`` to see
        exactly which backend/endpoint/model/key the request resolved to.
        """
        box: dict = {}
        real_init = llm.LLMClient.__init__

        def spy(self, cfg):
            box["config"] = cfg
            return real_init(self, cfg)

        patcher = mock.patch.object(llm.LLMClient, "__init__", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return box

    def test_process_honors_explicit_config_over_service_env(self):
        # Service env is the default stub; the request's config must win.
        one = segments_from()[:1]
        box = self.capture_config()
        with mock.patch.object(llm.LLMClient, "_chat",
                               return_value='{"segments": [{"id": "1#0", "target": "Hallo"}]}'):
            response = self.client.post("/process", json={
                "segments": one, "config": config_block()})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["engine"], "openai")       # config backend, not "stub"
        self.assertEqual(body["suggestions"], {"1#0": {"text": "Hallo"}})
        self.assertEqual(body["missing"], [])
        cfg = box["config"]
        self.assertEqual(cfg.preset, "OpenAI API")
        self.assertEqual(cfg.model, "test-model")
        self.assertEqual(cfg.base_url, "http://127.0.0.1:9/v1")
        self.assertEqual(cfg.api_key, FAKE_KEY)
        self.assertEqual(cfg.backend, "openai")

    def test_omitted_config_uses_the_configured_engine(self):
        # A legacy caller with no config block keeps the service's engine.
        one = segments_from()[:1]
        response = self.process(segments=one)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["engine"], "stub")

    def test_missing_api_key_fails_safely_for_key_required_preset(self):
        one = segments_from()[:1]
        with mock.patch.object(llm.LLMClient, "__init__") as ctor:
            response = self.client.post("/process", json={
                "segments": one, "config": config_block(api_key="")})
        self.assertEqual(response.status_code, 400, response.text)
        body = response.json()
        self.assertEqual(body["error"], "missing_api_key")
        self.assertEqual(body["details"], {"preset": "OpenAI API"})
        self.assertNotIn(FAKE_KEY, response.text)
        ctor.assert_not_called()  # no client built, so no provider call

    def test_ollama_preset_needs_no_key(self):
        one = segments_from()[:1]
        with mock.patch.object(llm.LLMClient, "_chat",
                               return_value='{"segments": [{"id": "1#0", "target": "Hallo"}]}'):
            response = self.client.post("/process", json={
                "segments": one,
                "config": {"preset": "Local Engine (Ollama)", "api_key": ""}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["engine"], "openai")

    def test_config_and_engine_are_mutually_exclusive(self):
        one = segments_from()[:1]
        response = self.client.post("/process", json={
            "segments": one, "config": config_block(), "engine": "stub"})
        self.assertEqual(response.status_code, 400, response.text)

    def test_unknown_nested_config_field_is_rejected(self):
        one = segments_from()[:1]
        bad = config_block()
        bad["bogus_field"] = 1
        for path in ("/process", "/probe", "/models"):
            with self.subTest(path=path):
                body = {"segments": one, "config": bad} if path == "/process" \
                    else {"config": bad}
                self.assertEqual(self.client.post(path, json=body).status_code, 400)

    def test_probe_honors_config_and_writes_no_suggestion(self):
        seen: dict = {}

        def fake_probe(client_config):
            seen["config"] = client_config
            return llm.ProbeResult(status="ok", requested_model=client_config.model,
                                   endpoint=client_config.base_url,
                                   credential_source="sidebar key", elapsed_ms=12.0,
                                   preview="hello")

        with mock.patch.object(llm, "probe_model", fake_probe):
            response = self.client.post("/probe", json={"config": config_block()})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["ok"], body["status"]), (True, "ok"))
        self.assertIn("hello", body["message"])
        self.assertNotIn(FAKE_KEY, response.text)
        self.assertEqual(seen["config"].model, "test-model")
        self.assertEqual(seen["config"].api_key, FAKE_KEY)

    def test_list_models_honors_config(self):
        box = self.capture_config()
        with mock.patch.object(llm.LLMClient, "list_models", return_value=["m-a", "m-b"]):
            response = self.client.post("/models", json={"config": config_block()})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["models"], ["m-a", "m-b"])
        self.assertEqual(box["config"].model, "test-model")
        self.assertEqual(box["config"].api_key, FAKE_KEY)

    def test_probe_and_models_require_a_config(self):
        for path in ("/probe", "/models"):
            with self.subTest(path=path):
                # No config block, an explicit null, or an unknown top-level
                # field (anything other than "config") are all rejected.
                self.assertEqual(self.client.post(path, json={}).status_code, 400)
                self.assertEqual(self.client.post(path, json={"config": None}).status_code, 400)
                self.assertEqual(self.client.post(path, json={"segments": []}).status_code, 400)

    def test_explicit_config_with_missing_sdk_is_503(self):
        one = segments_from()[:1]
        with mock.patch.object(llm, "OpenAI", None):
            response = self.client.post("/process", json={
                "segments": one, "config": config_block()})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"], "engine_unavailable")

    def test_config_validation_bounds_and_types(self):
        one = segments_from()[:1]
        for override in ({"temperature": 5}, {"temperature": "x"}, {"temperature": True},
                         {"max_tokens": 200001}, {"max_tokens": 1.5},
                         {"chunk_size": 0}, {"chunk_size": 999}, {"timeout": 0},
                         {"timeout": 99999}, {"max_retries": 0}, {"max_retries": 99},
                         {"preset": "No such preset"}, {"backend": "magic"},
                         {"model": "   "}):
            with self.subTest(override=override):
                block = config_block(**override)
                self.assertEqual(self.client.post("/process", json={
                    "segments": one, "config": block}).status_code, 400)

    def test_key_never_leaks_in_repr_response_or_logs(self):
        # repr of the request config suppresses the key field entirely.
        cfg = ModelConfig(preset="OpenAI API", backend="openai",
                          base_url="http://127.0.0.1:9/v1", model="test-model",
                          api_key=FAKE_KEY)
        self.assertNotIn(FAKE_KEY, repr(cfg))
        self.assertNotIn(FAKE_KEY, str(cfg))

        one = segments_from()[:1]
        with mock.patch.object(llm.LLMClient, "_chat",
                               return_value='{"segments": [{"id": "1#0", "target": "Hallo"}]}'):
            with self.assertLogs("processing-service", level="DEBUG") as logs:
                response = self.client.post("/process", json={
                    "segments": one, "config": config_block()})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(FAKE_KEY, response.text)
        self.assertNotIn(FAKE_KEY, "\n".join(logs.output))


class TestHealthLogsConfig(ProcessingTestCase):
    def test_network_is_blocked_in_these_tests(self):
        with self.assertRaises(AssertionError):
            socket.create_connection(("example.invalid", 80), timeout=1)
        with self.assertRaises(AssertionError):
            socket.getaddrinfo("example.invalid", 443)

    def test_health(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/readyz").json(),
                         {"status": "ready", "engine": "stub", "checks": {"engine": "ok"}})

    def test_stub_works_without_provider_sdks(self):
        with mock.patch.object(llm, "OpenAI", None), mock.patch.object(llm, "anthropic", None):
            self.assertEqual(self.client.get("/readyz").status_code, 200)
            self.assertEqual(self.process().status_code, 200)

    def test_logs_hold_no_segment_text(self):
        segs = [{"key": "a", "source_text": SECRET_TEXT, "mt_target": SECRET_TEXT,
                 "user_comment": SECRET_TEXT}]
        with self.assertLogs("processing-service", level="DEBUG") as logs:
            self.assertEqual(self.process(segments=segs).status_code, 200)
            bad = self.client.post("/process", json={"segments": [{"key": SECRET_TEXT}]})
            logging.getLogger("processing-service").debug("end")
        self.assertNotIn(SECRET_TEXT, "\n".join(logs.output))
        self.assertNotIn(SECRET_TEXT, bad.text)

    def test_unexpected_error_is_generic(self):
        with mock.patch.object(self.service, "process", side_effect=RuntimeError(SECRET_TEXT)):
            with self.assertLogs("processing-service", level="ERROR") as logs:
                response = self.process()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(SECRET_TEXT, response.text)
        self.assertNotIn(SECRET_TEXT, "\n".join(logs.output))

    def test_configuration(self):
        s = config.load_settings({})
        self.assertEqual((s.host, s.port, s.engine.kind), ("127.0.0.1", 8103, "stub"))
        real = config.load_settings({config.ENV_ENGINE: "Anthropic", config.ENV_MODEL: "m",
                                     config.ENV_API_KEY: FAKE_KEY})
        self.assertEqual(real.engine.kind, "anthropic")
        self.assertNotIn(FAKE_KEY, repr(real))
        for env in ({config.ENV_ENGINE: "openai"},              # model required
                    {config.ENV_ENGINE: "magic"},
                    {config.ENV_PORT: "0"},
                    {config.ENV_TEMPERATURE: "5"},
                    {config.ENV_LOG_LEVEL: "loud"}):
            with self.subTest(env=env):
                with self.assertRaises(config.ConfigError) as ctx:
                    config.load_settings(dict(env, **{config.ENV_API_KEY: FAKE_KEY}))
                self.assertNotIn(FAKE_KEY, str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
