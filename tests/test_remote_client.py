"""Offline, mocked-HTTP tests for harness_remote (config + clients).

No real network: a ``FakeSession`` stands in for ``requests.Session`` and the
only real session is exercised without a call. The suite covers URL/config
validation, timeout selection, JSON and binary success, 409/422/503 handling,
connection and timeout errors, non-JSON/malformed error bodies, and that no
sensitive text leaks into a RemoteError.

Run with:  python -m unittest -v tests.test_remote_client
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness_remote import (  # noqa: E402
    ConfigError, DocumentClient, ProcessingClient, RemoteError,
    WorkspaceClient, build_clients, load_config,
)
from harness_remote.client import RemoteClients  # noqa: E402


SECRET = "TOPSECRET-DO-NOT-LEAK-4f8c2a"


# ------------------------------------------------------------------- fakes


class FakeResponse:
    """A response with an explicit status and one of JSON / text / bytes.

    ``headers`` is a plain mapping (e.g. a ``Location`` for a redirect); the
    clients under test must never read it, which the redirect tests assert.
    """

    def __init__(self, status_code, *, json_body=None, text=None, content=None,
                 headers=None):
        self.status_code = status_code
        self._json = json_body
        self._text = text
        self._content = content
        self._headers = headers

    @property
    def content(self):
        if self._content is not None:
            return self._content
        if self._text is not None:
            return self._text.encode("utf-8")
        return b""

    @property
    def headers(self):
        return dict(self._headers or {})

    def json(self):
        if self._json is not None:
            return self._json
        if self._text is not None:
            return json.loads(self._text)
        raise ValueError("no JSON body in fake response")


class FakeSession:
    """A stand-in for requests.Session: no network, records calls."""

    def __init__(self, responses=None, exc=None):
        self.responses = list(responses or [])
        self.exc = exc
        self.calls = []
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, dict(kwargs)))
        if self.exc is not None:
            raise self.exc
        if not self.responses:
            raise AssertionError("FakeSession ran out of responses for %s %s"
                                 % (method, url))
        return self.responses.pop(0)

    def close(self):
        self.closed = True

    @property
    def last(self):
        return self.calls[-1]


# ------------------------------------------------------------------- config


class TestConfig(unittest.TestCase):
    def test_defaults_are_local_and_neutral(self):
        cfg = load_config({})
        self.assertEqual(cfg.mode, "local")
        self.assertFalse(cfg.is_remote)
        self.assertEqual(cfg.http_timeout, (30.0, 30.0))
        self.assertEqual(cfg.process_timeout, (30.0, 600.0))
        self.assertEqual(cfg.document_url, "")

    def test_explicit_connect_and_read_tuples(self):
        cfg = load_config({"HARNESS_HTTP_TIMEOUT_SECONDS": "5",
                           "HARNESS_PROCESS_TIMEOUT_SECONDS": "120"})
        self.assertEqual(cfg.http_timeout, (5.0, 5.0))
        self.assertEqual(cfg.process_timeout, (5.0, 120.0))

    def test_invalid_backend_raises(self):
        with self.assertRaises(ConfigError):
            load_config({"HARNESS_BACKEND": "cloud"})

    def test_remote_requires_all_three_urls(self):
        with self.assertRaises(ConfigError):
            load_config({"HARNESS_BACKEND": "remote"})
        with self.assertRaises(ConfigError):
            load_config({"HARNESS_BACKEND": "remote",
                        "HARNESS_DOCUMENT_URL": "http://doc:8101"})

    def test_remote_never_falls_back_to_local(self):
        # A bad URL in remote mode must raise, not silently become local.
        bad = [
            {"HARNESS_BACKEND": "remote", "HARNESS_DOCUMENT_URL": "ftp://doc",
             "HARNESS_WORKSPACE_URL": "http://ws", "HARNESS_PROCESSING_URL": "http://p"},
            {"HARNESS_BACKEND": "remote", "HARNESS_DOCUMENT_URL": "http://doc",
             "HARNESS_WORKSPACE_URL": "http://user:pass@ws",
             "HARNESS_PROCESSING_URL": "http://p"},
        ]
        for env in bad:
            with self.subTest(env=env):
                with self.assertRaises(ConfigError):
                    load_config(env)

    def test_valid_remote_urls(self):
        cfg = load_config({
            "HARNESS_BACKEND": "remote",
            "HARNESS_DOCUMENT_URL": "http://doc:8101",
            "HARNESS_WORKSPACE_URL": "https://ws:8102/harness",
            "HARNESS_PROCESSING_URL": "http://proc:8103/",
        })
        self.assertTrue(cfg.is_remote)
        self.assertEqual(cfg.document_url, "http://doc:8101")
        self.assertEqual(cfg.workspace_url, "https://ws:8102/harness")
        self.assertEqual(cfg.processing_url, "http://proc:8103")  # trailing slash stripped

    def test_url_rejections(self):
        base = {"HARNESS_BACKEND": "remote", "HARNESS_DOCUMENT_URL": "http://doc",
                "HARNESS_WORKSPACE_URL": "http://ws", "HARNESS_PROCESSING_URL": "http://p"}
        for url in (
            "http://user:pass@doc", "http://@doc",
            "http://doc/?x=1", "http://doc/#frag",
            "ftp://doc", "http", "", "http:///nohost",
        ):
            env = dict(base)
            env["HARNESS_DOCUMENT_URL"] = url
            with self.subTest(url=url):
                with self.assertRaises(ConfigError):
                    load_config(env)

    def test_url_control_chars_and_ports_rejected(self):
        base = {"HARNESS_BACKEND": "remote", "HARNESS_DOCUMENT_URL": "http://doc",
                "HARNESS_WORKSPACE_URL": "http://ws", "HARNESS_PROCESSING_URL": "http://p"}
        bad = [
            "http://doc:8101/x y",    # internal space
            "http://doc:8101/x\ty",   # internal tab (a control character)
            "http://doc\x00:8101",    # embedded NUL control character
            "http://doc:0",           # port below the valid range
            "http://doc:65536",       # port above the valid range
            "http://doc:abc",         # nonnumeric port
            "http://[::1",            # malformed IPv6 (unbalanced bracket)
            "http://[::1:]:8080",     # malformed IPv6 literal
            "http://[12345]:8080",    # bracketed host that is not a valid IPv6
        ]
        for url in bad:
            env = dict(base)
            env["HARNESS_DOCUMENT_URL"] = url
            with self.subTest(url=url):
                with self.assertRaises(ConfigError):
                    load_config(env)

    def test_valid_special_urls_preserved(self):
        # localhost, valid IPv4/IPv6, path prefixes and surrounding spaces all
        # still parse.
        cases = [
            ("http://localhost", "http://localhost"),
            ("http://localhost:8080", "http://localhost:8080"),
            ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
            ("http://[::1]:8080", "http://[::1]:8080"),
            ("http://[2001:db8::1]:8080", "http://[2001:db8::1]:8080"),
            ("http://doc:8101/harness", "http://doc:8101/harness"),
            ("http://doc:8101/harness/", "http://doc:8101/harness"),
            ("  http://doc:8101  ", "http://doc:8101"),
        ]
        for url, expected in cases:
            env = {"HARNESS_BACKEND": "remote", "HARNESS_DOCUMENT_URL": url,
                   "HARNESS_WORKSPACE_URL": url, "HARNESS_PROCESSING_URL": url}
            with self.subTest(url=url):
                cfg = load_config(env)
                self.assertEqual(cfg.document_url, expected)
                self.assertEqual(cfg.workspace_url, expected)
                self.assertEqual(cfg.processing_url, expected)

    def test_url_error_does_not_disclose_value(self):
        # A bad URL may embed a secret; the ConfigError must name the variable
        # only, never echoing the value into its message or repr.
        for url in ("http://doc:abc/%s" % SECRET,
                    "http://[::%s]:8080" % SECRET,
                    "http://doc\x00/%s" % SECRET):
            env = {"HARNESS_BACKEND": "remote", "HARNESS_DOCUMENT_URL": url,
                   "HARNESS_WORKSPACE_URL": "http://ws",
                   "HARNESS_PROCESSING_URL": "http://p"}
            with self.subTest(url=url):
                with self.assertRaises(ConfigError) as ctx:
                    load_config(env)
                self.assertIn("HARNESS_DOCUMENT_URL", str(ctx.exception))
                self.assertNotIn(SECRET, str(ctx.exception))
                self.assertNotIn(SECRET, repr(ctx.exception))

    def test_invalid_timeouts_raise(self):
        with self.assertRaises(ConfigError):
            load_config({"HARNESS_HTTP_TIMEOUT_SECONDS": "abc"})
        with self.assertRaises(ConfigError):
            load_config({"HARNESS_HTTP_TIMEOUT_SECONDS": "0"})
        with self.assertRaises(ConfigError):
            load_config({"HARNESS_PROCESS_TIMEOUT_SECONDS": "100000"})


# ------------------------------------------------------------------- clients


class ClientTestCase(unittest.TestCase):
    @staticmethod
    def client_for(kind, responses=None, exc=None, http=(30.0, 30.0),
                   proc=(30.0, 600.0), url=None):
        session = FakeSession(responses=responses, exc=exc)
        if kind == "document":
            client = DocumentClient(url or "http://doc:8101", http_timeout=http,
                                   process_timeout=proc, session=session)
        elif kind == "workspace":
            client = WorkspaceClient(url or "http://ws:8102", http_timeout=http,
                                    process_timeout=proc, session=session)
        else:
            client = ProcessingClient(url or "http://proc:8103", http_timeout=http,
                                     process_timeout=proc, session=session)
        return client, session


class TestSuccess(ClientTestCase):
    def test_upload_sends_multipart_and_returns_json(self):
        client, session = self.client_for(
            "document", responses=[FakeResponse(201, json_body={
                "original_id": "abc", "created": True})])
        result = client.upload(b"<xml/>", filename="a.mqxliff",
                              content_type="application/xml")
        self.assertEqual(result, {"original_id": "abc", "created": True})
        method, url, kwargs = session.last
        self.assertEqual((method, url), ("POST", "http://doc:8101/documents"))
        self.assertEqual(kwargs["files"]["file"][0], "a.mqxliff")
        self.assertEqual(kwargs["files"]["file"][1], b"<xml/>")

    def test_segments_send_tag_source_and_filters(self):
        client, session = self.client_for(
            "document", responses=[FakeResponse(200, json_body={"segments": []})])
        client.segments("orig123", tag_source="source",
                        filters={"skip_confirmed": True})
        _, url, kwargs = session.last
        self.assertEqual(url, "http://doc:8101/originals/orig123/segments")
        self.assertEqual(kwargs["params"]["tag_source"], "source")
        self.assertIs(kwargs["params"]["skip_confirmed"], True)

    def test_export_sends_translation_body(self):
        client, session = self.client_for(
            "document", responses=[FakeResponse(201, json_body={"export_id": "x"})])
        client.export("orig", {"1#0": "Hello {1}"}, filters={"skip_locked": False})
        _, _, kwargs = session.last
        self.assertEqual(kwargs["json"]["translations"], {"1#0": "Hello {1}"})
        self.assertEqual(kwargs["json"]["tag_source"], "target")
        self.assertEqual(kwargs["json"]["filter"], {"skip_locked": False})
        self.assertIs(kwargs["json"]["mark_confirmed"], False)

    def test_tag_reports_body(self):
        client, session = self.client_for(
            "document", responses=[FakeResponse(200, json_body={
                "reports": {"1#0": ""}})])
        client.tag_reports("orig", {"1#0": "x"}, tag_source="source")
        _, _, kwargs = session.last
        self.assertEqual(kwargs["json"],
                         {"tag_source": "source", "texts": {"1#0": "x"}})

    def test_content_endpoints_return_bytes(self):
        payload = b"<?xml version='1.0'?>\n<xliff/>"
        client, session = self.client_for("document",
            responses=[FakeResponse(200, content=payload)])
        data = client.original_content("orig")
        self.assertEqual(data, payload)
        self.assertIsInstance(data, bytes)

    def test_export_content_endpoint(self):
        client, session = self.client_for(
            "document", responses=[FakeResponse(200, content=b"<xliff/>")])
        self.assertEqual(client.export_content("exp1"), b"<xliff/>")
        self.assertEqual(session.last[1], "http://doc:8101/exports/exp1/content")

    def test_workspace_bind_save_and_suggestions(self):
        client, session = self.client_for("workspace", responses=[
            FakeResponse(201, json_body={"created": True, "revision": 1}),
            FakeResponse(200, json_body={"revision": 2, "changed": True}),
            FakeResponse(200, json_body={"revision": 3, "recorded_keys": ["1#0"]})])
        out = client.bind("docid", "origid", source_file="r.mqxliff",
                          segments=[{"key": "1#0", "unit_id": "1",
                                     "source_text": "a", "target_text": "b"}],
                          present_keys=["1#0"])
        self.assertTrue(out["created"])
        client.save_segments("docid", 1, {"1#0": {"text": "hi"}})
        _, _, kw = session.calls[1]
        self.assertEqual(kw["json"]["expected_revision"], 1)
        out3 = client.record_suggestions("docid", 2, {"1#0": {"text": "yo"}})
        self.assertEqual(out3["recorded_keys"], ["1#0"])
        self.assertEqual(session.calls[1][0], "PATCH")   # save
        self.assertEqual(session.calls[2][0], "POST")    # suggestions

    def test_get_workspace_path(self):
        client, session = self.client_for(
            "workspace", responses=[FakeResponse(200, json_body={"revision": 1})])
        client.get_workspace("docid")
        self.assertEqual(session.last[1], "http://ws:8102/workspaces/docid")

    def test_processing_uses_process_timeout(self):
        client, session = self.client_for(
            "processing", responses=[FakeResponse(200, json_body={
                "suggestions": {"1#0": {"text": "Hi"}}})])
        out = client.process(segments=[{"key": "1#0", "source_text": "a",
                                        "mt_target": "b"}])
        self.assertEqual(out["suggestions"], {"1#0": {"text": "Hi"}})
        _, _, kwargs = session.last
        self.assertEqual(kwargs["timeout"], (30.0, 600.0))
        self.assertEqual(kwargs["json"]["operation"], "batch")

    def test_http_calls_use_http_timeout(self):
        client, session = self.client_for(
            "document", responses=[FakeResponse(200, json_body={"ok": True})])
        client.original("origid")
        self.assertEqual(session.last[2]["timeout"], (30.0, 30.0))

    def test_base_url_trailing_slash_stripped_in_join(self):
        client, session = self.client_for(
            "document", url="http://doc:8101/harness/",
            responses=[FakeResponse(200, json_body={})])
        client.original("o")
        self.assertEqual(session.last[1], "http://doc:8101/harness/originals/o")

    def test_default_session_is_a_requests_session(self):
        client = DocumentClient("http://doc", http_timeout=(30.0, 30.0))
        self.assertIsInstance(client._session, requests.Session)
        client.close()


class TestErrors(ClientTestCase):
    def test_409_preserves_current_revision(self):
        client, session = self.client_for("workspace", responses=[
            FakeResponse(409, json_body={
                "error": "stale_revision",
                "message": "The workspace changed since it was read; reload and retry.",
                "details": {"expected_revision": 3, "current_revision": 5}})])
        with self.assertRaises(RemoteError) as ctx:
            client.save_segments("docid", 3, {"1#0": {"text": "x"}})
        err = ctx.exception
        self.assertEqual(err.status, 409)
        self.assertEqual(err.code, "stale_revision")
        self.assertTrue(err.is_conflict)
        self.assertEqual(err.current_revision, 5)
        self.assertEqual(err.service, "workspace")
        self.assertEqual(err.details, {"expected_revision": 3, "current_revision": 5})

    def test_422_unknown_segment(self):
        client, session = self.client_for("document", responses=[
            FakeResponse(422, json_body={
                "error": "unknown_segment",
                "message": "Translations name segment keys this original does not have.",
                "details": {"keys": ["9#9"], "count": 1}})])
        with self.assertRaises(RemoteError) as ctx:
            client.export("orig", {"9#9": "x"})
        err = ctx.exception
        self.assertEqual(err.status, 422)
        self.assertEqual(err.code, "unknown_segment")
        self.assertEqual(err.details, {})  # no details retained outside a 409 conflict
        self.assertNotIn("9#9", str(err))  # identifiers are not surfaced

    def test_503_engine_unavailable(self):
        client, session = self.client_for("processing", responses=[
            FakeResponse(503, json_body={
                "error": "engine_unavailable", "message": "The SDK is not installed."})])
        with self.assertRaises(RemoteError) as ctx:
            client.ready()
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(ctx.exception.code, "engine_unavailable")

    def test_connection_error_does_not_leak(self):
        client, session = self.client_for(
            "document", exc=requests.exceptions.ConnectionError("could not reach 10.0.0.1:8101"))
        with self.assertRaises(RemoteError) as ctx:
            client.original("orig")
        err = ctx.exception
        self.assertIsNone(err.status)
        self.assertEqual(err.code, "connection_error")
        self.assertEqual(len(session.calls), 1)  # no automatic retry
        self.assertNotIn("10.0.0.1", str(err))

    def test_timeout_maps_to_remote_error(self):
        client, session = self.client_for("document", exc=requests.exceptions.Timeout("slow"))
        with self.assertRaises(RemoteError) as ctx:
            client.original("orig")
        self.assertIsNone(ctx.exception.status)
        self.assertEqual(ctx.exception.code, "timeout")
        self.assertEqual(len(session.calls), 1)

    def test_generic_request_exception_is_sanitized(self):
        client, session = self.client_for(
            "document", exc=requests.exceptions.RequestException(SECRET))
        with self.assertRaises(RemoteError) as ctx:
            client.original("orig")
        err = ctx.exception
        self.assertEqual(err.code, "http_error")
        self.assertNotIn(SECRET, str(err))
        self.assertNotIn(SECRET, err.message)

    def test_nonjson_error_body_does_not_leak(self):
        client, session = self.client_for(
            "document", responses=[FakeResponse(500, text="<html>%s</html>" % SECRET)])
        with self.assertRaises(RemoteError) as ctx:
            client.original("orig")
        err = ctx.exception
        self.assertEqual(err.status, 500)
        self.assertEqual(err.code, "http_500")
        self.assertEqual(err.details, {})
        self.assertNotIn(SECRET, str(err))
        self.assertNotIn(SECRET, err.message)
        self.assertNotIn(SECRET, repr(err))

    def test_malformed_error_body_loses_revision(self):
        client, session = self.client_for("workspace",
            responses=[FakeResponse(409, text="not json at all")])
        with self.assertRaises(RemoteError) as ctx:
            client.save_segments("docid", 1, {"1#0": {"text": "x"}})
        err = ctx.exception
        self.assertEqual(err.status, 409)
        self.assertEqual(err.code, "http_409")
        self.assertIsNone(err.current_revision)

    def test_success_but_nonjson_is_bad_response(self):
        client, session = self.client_for(
            "document", responses=[FakeResponse(200, text="plain text, not json")])
        with self.assertRaises(RemoteError) as ctx:
            client.original("orig")
        self.assertEqual(ctx.exception.code, "bad_response")

    def test_mutation_is_not_retried_on_conflict(self):
        client, session = self.client_for("workspace", responses=[
            FakeResponse(409, json_body={
                "error": "stale_revision", "details": {"current_revision": 2}})])
        with self.assertRaises(RemoteError):
            client.save_segments("docid", 1, {"1#0": {"text": "x"}})
        self.assertEqual(len(session.calls), 1)


class TestSafeErrors(ClientTestCase):
    """A non-2xx body is not trusted: only an allowlisted code and, for a 409
    conflict, two revision fields reach the caller. A secret placed anywhere in
    the body never survives into str/repr/code/message/retained details."""

    @staticmethod
    def views_of(err):
        return (str(err), repr(err), err.message, err.code,
                " ".join(str(v) for v in err.details.values()))

    def test_unlisted_error_code_and_body_dropped(self):
        # A hostile server can name any code and stuff any field with a secret;
        # none of it is trusted.
        client, session = self.client_for("document", responses=[
            FakeResponse(422, json_body={
                "error": SECRET, "message": "boom %s" % SECRET,
                "details": {"arbitrary_id": SECRET, "nested": {"token": SECRET}}})])
        with self.assertRaises(RemoteError) as ctx:
            client.export("orig", {"1#0": "x"})
        err = ctx.exception
        self.assertEqual(err.code, "http_422")
        self.assertEqual(err.details, {})
        for view in self.views_of(err):
            self.assertNotIn(SECRET, view)

    def test_allowed_code_ignores_server_message(self):
        # unknown_segment is allowlisted, but its client-owned message replaces
        # the server's; the server's message is never copied.
        client, session = self.client_for("document", responses=[
            FakeResponse(422, json_body={
                "error": "unknown_segment", "message": "leak: %s" % SECRET})])
        with self.assertRaises(RemoteError) as ctx:
            client.export("orig", {"9#9": "x"})
        err = ctx.exception
        self.assertEqual(err.code, "unknown_segment")
        self.assertNotEqual(err.message, "leak: %s" % SECRET)
        for view in self.views_of(err):
            self.assertNotIn(SECRET, view)

    def test_non_conflict_details_are_dropped(self):
        # A listed code on a non-409 still retains no details: nested data and
        # arbitrary identifiers are all dropped.
        client, session = self.client_for("document", responses=[
            FakeResponse(503, json_body={
                "error": "unavailable",
                "details": {"nested": {"token": SECRET}, "arbitrary_id": SECRET}})])
        with self.assertRaises(RemoteError) as ctx:
            client.ready()
        err = ctx.exception
        self.assertEqual(err.code, "unavailable")
        self.assertEqual(err.details, {})
        for view in self.views_of(err):
            self.assertNotIn(SECRET, view)

    def test_conflict_keeps_valid_revisions_and_drops_rest(self):
        client, session = self.client_for("workspace", responses=[
            FakeResponse(409, json_body={
                "error": "stale_revision", "message": "server text %s" % SECRET,
                "details": {"expected_revision": 3, "current_revision": 5,
                            "note": SECRET, "extra": [1, 2, 3]}})])
        with self.assertRaises(RemoteError) as ctx:
            client.save_segments("docid", 3, {"1#0": {"text": "x"}})
        err = ctx.exception
        self.assertEqual(err.code, "stale_revision")
        self.assertEqual(err.details, {"expected_revision": 3, "current_revision": 5})
        self.assertEqual(err.current_revision, 5)
        for view in self.views_of(err):
            self.assertNotIn(SECRET, view)

    def test_conflict_drops_malformed_revisions(self):
        # Negative int, string, and bool are all rejected as revisions.
        for value, label in ((-1, "negative"), ("5", "string"),
                              (True, "bool-true"), (False, "bool-false")):
            client, session = self.client_for("workspace", responses=[
                FakeResponse(409, json_body={
                    "error": "stale_revision",
                    "details": {"current_revision": value}})])
            with self.subTest(value=label):
                with self.assertRaises(RemoteError) as ctx:
                    client.save_segments("docid", 3, {"1#0": {"text": "x"}})
                err = ctx.exception
                self.assertEqual(err.details, {}, label)
                self.assertIsNone(err.current_revision, label)

    def test_conflict_keeps_valid_revision_alone(self):
        # A malformed expected_revision is dropped while a valid current_revision
        # survives, proving the two are validated independently.
        client, session = self.client_for("workspace", responses=[
            FakeResponse(409, json_body={
                "error": "stale_revision",
                "details": {"expected_revision": "3", "current_revision": 5}})])
        with self.assertRaises(RemoteError) as ctx:
            client.save_segments("docid", 3, {"1#0": {"text": "x"}})
        err = ctx.exception
        self.assertEqual(err.details, {"current_revision": 5})
        self.assertEqual(err.current_revision, 5)

    def test_199_is_a_failure_not_success(self):
        client, session = self.client_for(
            "document", responses=[FakeResponse(199, json_body={"ok": True})])
        with self.assertRaises(RemoteError) as ctx:
            client.original("orig")
        err = ctx.exception
        self.assertEqual(err.status, 199)
        self.assertEqual(err.code, "http_199")
        self.assertEqual(len(session.calls), 1)


class TestRedirects(ClientTestCase):
    """3xx is a failure: it is never followed, never dereferenced, and its
    Location target is never surfaced. ``allow_redirects=False`` is always
    passed to the transport."""

    @staticmethod
    def redirect_response(status, *, location=None, content=None, text=None):
        headers = {"Location": location} if location is not None else None
        return FakeResponse(status, content=content, text=text, headers=headers)

    def test_302_raises_safe_error_and_does_not_leak_location(self):
        # A classic cloud-metadata SSRF lure in the Location must not surface.
        target = "http://169.254.169.254/%s/" % SECRET
        client, session = self.client_for(
            "document", responses=[self.redirect_response(302, location=target)])
        with self.assertRaises(RemoteError) as ctx:
            client.original("orig")
        err = ctx.exception
        self.assertEqual(err.status, 302)
        self.assertEqual(err.code, "redirect_not_followed")
        self.assertEqual(err.service, "document")
        self.assertEqual(err.details, {})
        self.assertIsNone(err.current_revision)
        for view in (str(err), repr(err), err.message,
                     " ".join(err.details.values())):
            self.assertNotIn(SECRET, view)
            self.assertNotIn("169.254.169.254", view)

    def test_307_and_308_each_raise(self):
        for status in (307, 308):
            client, session = self.client_for(
                "document", responses=[self.redirect_response(
                    status, location="http://internal.corp/secret/%s" % SECRET)])
            with self.subTest(status=status):
                with self.assertRaises(RemoteError) as ctx:
                    client.document("docid")
                err = ctx.exception
                self.assertEqual(err.status, status)
                self.assertEqual(err.code, "redirect_not_followed")
                self.assertNotIn(SECRET, str(err))
                self.assertNotIn("internal.corp", str(err))

    def test_redirect_on_binary_download_does_not_return_bytes(self):
        # A 3xx on a binary endpoint must raise, not hand back the redirect body.
        client, session = self.client_for(
            "document", responses=[self.redirect_response(
                302, location="http://internal/x",
                content=b"<!doctype html> redirect body")])
        with self.assertRaises(RemoteError) as ctx:
            client.original_content("orig")
        self.assertEqual(ctx.exception.status, 302)
        self.assertEqual(ctx.exception.code, "redirect_not_followed")
        self.assertEqual(len(session.calls), 1)  # no follow-up request

    def test_redirect_on_mutation_does_not_leak_and_does_not_retry(self):
        client, session = self.client_for(
            "workspace", responses=[self.redirect_response(
                307, location="http://internal/%s" % SECRET)])
        with self.assertRaises(RemoteError) as ctx:
            client.save_segments("docid", 1, {"1#0": {"text": "x"}})
        self.assertEqual(ctx.exception.status, 307)
        self.assertEqual(ctx.exception.code, "redirect_not_followed")
        self.assertNotIn(SECRET, str(ctx.exception))
        self.assertEqual(len(session.calls), 1)  # no automatic retry/follow

    def test_allow_redirects_is_false_on_success(self):
        client, session = self.client_for(
            "document", responses=[FakeResponse(200, json_body={"ok": True})])
        client.original("orig")
        self.assertIs(session.last[2]["allow_redirects"], False)

    def test_allow_redirects_is_false_on_redirect(self):
        client, session = self.client_for(
            "document", responses=[self.redirect_response(
                302, location="http://internal/%s" % SECRET)])
        with self.assertRaises(RemoteError):
            client.original("orig")
        self.assertIs(session.last[2]["allow_redirects"], False)


class TestBuildClients(unittest.TestCase):
    def _remote_env(self):
        return {"HARNESS_BACKEND": "remote", "HARNESS_DOCUMENT_URL": "http://doc:8101",
                "HARNESS_WORKSPACE_URL": "http://ws:8102",
                "HARNESS_PROCESSING_URL": "http://proc:8103"}

    def test_builds_three_clients_sharing_a_session(self):
        cfg = load_config(self._remote_env())
        session = FakeSession()
        bundle = build_clients(cfg, session=session)
        self.assertIsInstance(bundle, RemoteClients)
        self.assertIsInstance(bundle.document, DocumentClient)
        self.assertIsInstance(bundle.workspace, WorkspaceClient)
        self.assertIsInstance(bundle.processing, ProcessingClient)
        self.assertIs(bundle.document._session, session)
        bundle.close()
        self.assertFalse(session.closed)  # injected session is not owned

    def test_local_mode_refuses_to_build(self):
        with self.assertRaises(ConfigError):
            build_clients(load_config({}))

    def test_owned_session_is_closed(self):
        cfg = load_config(self._remote_env())
        fake = FakeSession()
        with mock.patch("harness_remote.client.requests.Session", return_value=fake):
            bundle = build_clients(cfg)
        self.assertIs(bundle._session, fake)
        self.assertTrue(bundle._own_session)
        bundle.close()
        self.assertTrue(fake.closed)

    def test_falsey_injected_session_is_retained(self):
        # A session that is not None but is falsey must still be used as-is,
        # shared by all three clients, and never closed by the bundle. The old
        # ``session or requests.Session()`` would have silently replaced it.
        class FalseySession:
            closed = False

            def request(self, *a, **k):
                raise AssertionError("session should not be called")

            def close(self):
                self.closed = True

            def __bool__(self):
                return False

        session = FalseySession()
        cfg = load_config(self._remote_env())
        bundle = build_clients(cfg, session=session)
        self.assertIs(bundle._session, session)
        self.assertIs(bundle.document._session, session)
        self.assertIs(bundle.workspace._session, session)
        self.assertIs(bundle.processing._session, session)
        self.assertFalse(bundle._own_session)
        bundle.close()
        self.assertFalse(session.closed)  # injected session is not owned/closed


if __name__ == "__main__":
    unittest.main(verbosity=2)
