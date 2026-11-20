"""Headless AppTest over ``app.py`` in remote mode, with fake ``RemoteClients``.

These exercise the remote-mode UI path that was wired into the app: the config
seam (no ``ProjectState`` in remote mode), a one-shot Open that never re-binds on
rerun, a Reload that verifies the saved workspace still matches the open document
before it merges, the guarantee that a failed Open never produces a mixed view,
and the one gated mutation - "Process Current Batch", which sends the selected
batch to the model through the processing service and persists its proposals as
the model's ``llm_suggestion`` (never the human ``user_final``), blocked while a
document is dirty or conflicted or the batch is already worked. The other
finished-mutation controls (Apply / Rerun / Analyse / Export) stay unreachable.

The clients are faked at the one seam the app exposes: ``get_remote_clients``
resolves ``build_clients`` from the ``harness_remote`` module at call time, so
patching that module attribute injects the fake and the app's own
``ss._remote_clients`` cache holds the same object across reruns.

No local state is written: remote mode skips ``ProjectState`` entirely, so the
tests point ``AppTest`` straight at the repository ``app.py`` and need no copy.
Run with:  python tests/test_remote_app.py
"""

from __future__ import annotations

import copy
import os
import sys
import unittest
from unittest import mock

from streamlit.testing.v1 import AppTest

# harness_remote imports the memoq_harness package at module import time, so the
# repo root must be importable before we import harness_remote.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import remote_workflow  # noqa: E402
from harness_remote import RemoteError, RemoteWorkspaceView  # noqa: E402
from memoq_harness import llm, mqxliff, state as state_mod  # noqa: E402
# The real workspace request parsers: pure functions, offline and without a
# database. Feeding the app's actual bind payload to parse_bind_request proves
# the projection is server-acceptable; feeding the app's actual save payload to
# parse_patch_request proves the save request is server-acceptable too - neither
# is just internally consistent.
from services.workspace_service.service import (  # noqa: E402
    parse_bind_request, parse_patch_request,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# parse_bind_request enforces a 64-char lowercase-hex original id (the document's
# identity digest); a payload the app must build carries one, so the
# parser-verification test supplies a well-formed id.
ORIG_ID = "ab" * 32


def doc_segment(key, unit_id, ordinal, source_text, target_text, **kw):
    base = {
        "key": key,
        "unit_id": unit_id,
        "ordinal": ordinal,
        "source_text": source_text,
        "target_text": target_text,
        "tag_source": "target",
        "expected_numbers": [1],
        "tags": [{"number": 1, "xml": "<b/>"}],
        "confirmed": False,
        "locked": False,
        "status": "pending",
        "source_tags": [],
        "target_tags": [],
        "target_state": "",
        "skip_reason": "",
        "translatable": True,
    }
    base.update(kw)
    return base


def ws_record(key, **kw):
    base = {
        "key": key,
        "segment_id": key,
        "ordinal": 0,
        "batch_index": 0,
        "source_text": "",
        "mt_target": "",
        "llm_suggestion": "",
        "user_final": "",
        "user_comment": "",
        "status": "pending",
        "edited_by_human": False,
        "tag_report": "",
    }
    base.update(kw)
    return base


def snapshot(document_id, original_id, tag_source, revision, order, segments,
             batch_size=2, current_batch=0, source_file=""):
    return {
        "document_id": document_id,
        "revision": revision,
        "state_version": 5,
        "bound_original_id": original_id,
        "tag_source": tag_source,
        "batch_size": batch_size,
        "source_file": source_file,
        "current_batch": current_batch,
        "order": order,
        "segments": segments,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }


class _FakeDocument:
    def __init__(self, upload_resp, segments_resp):
        # A list is a queue of upload responses (one per Open, so a test can
        # drive document A then B through one fake); a single dict/exception is
        # the one-document case, returned every call.
        self._upload = upload_resp
        # A callable returns each document's own segments envelope, keyed by
        # original_id; a dict is a fixed envelope (a malformed one too), and a
        # list is a segment sequence wrapped into that envelope.
        self._segments = segments_resp
        self.upload_calls = []
        self.segments_calls = []

    def upload(self, raw, filename="document.mqxliff", content_type="application/xml"):
        self.upload_calls.append((raw, filename))
        if isinstance(self._upload, list):
            if not self._upload:
                raise IndexError("no more upload responses queued")
            response = self._upload.pop(0)
        else:
            response = self._upload
        if isinstance(response, BaseException):
            raise response
        return response

    def segments(self, original_id, *, tag_source="target", filters=None):
        self.segments_calls.append((original_id, tag_source))
        if callable(self._segments):
            response = self._segments(original_id, tag_source=tag_source,
                                     filters=filters)
        else:
            response = self._segments
        if isinstance(response, BaseException):
            raise response
        # Reproduce the document-service /segments 2xx body exactly: a 2xx
        # carries {original_id, document_id, tag_source, filter, segments}.
        # A dict is returned as-is so a test can inject a malformed envelope;
        # a list is the segment sequence and is wrapped into that envelope.
        # (The old fake returned the bare list, which is what hid the bug.)
        if isinstance(response, dict):
            return response
        return {
            "original_id": original_id,
            "document_id": "doc-1",
            "tag_source": tag_source,
            "filter": {},
            "segments": response,
        }


class _FakeWorkspace:
    def __init__(self, bind_resp, get_resp, *, server=None, save_resp=None,
                 record_resp=None):
        # server, when set, is a {document_id: snapshot} dict the workspace owns
        # and mutates on save; get_workspace then returns the live snapshot, so a
        # save that advances the revision is visible to a following read - the
        # post-save refresh and the 409/conflict tests need exactly that.
        # save_resp, when a server is not set, is a single configured response
        # (dict / exception / callable(document_id, expected_revision, updates)).
        # record_resp, when a server is not set, is a single configured response
        # for the suggestion write.
        self._bind = bind_resp
        self._get = get_resp
        self._server = server
        self._save = save_resp
        self._record = record_resp
        self.bind_calls = []
        self.get_calls = []
        self.save_calls = []
        self.record_calls = []
        # The non-mutating compatibility probe writes to a workspace nothing is
        # bound to, so a fresh service accepts the prefill field and 404s
        # "unknown workspace" while a stale one predates the contract and 400s
        # the unknown field. Default 404 mirrors a fresh service; set
        # _compat_probe_status to 400 to simulate a stale one. Probes are
        # recorded separately so they never count as a real write.
        self.compat_probe_calls = []
        self._compat_probe_status = 404

    def bind(self, document_id, original_id, *, source_file="", tag_source="target",
             segments=None, present_keys=None):
        self.bind_calls.append({
            "document_id": document_id,
            "original_id": original_id,
            "source_file": source_file,
            "tag_source": tag_source,
            "present_keys": list(present_keys or []),
            # Record the full items bind was handed (shallow-copied), not just
            # their keys, so a test can feed the app's actual payload back to the
            # real parse_bind_request and confirm the projection: the active
            # selection, four fields each, not the 16-field serialized item.
            "segments": ([dict(s) for s in segments]
                         if isinstance(segments, (list, tuple))
                         else ("NOT_A_LIST", type(segments).__name__)),
        })
        if isinstance(self._bind, BaseException):
            raise self._bind
        return self._bind

    def get_workspace(self, document_id):
        self.get_calls.append(document_id)
        if self._server is not None:
            # A copy, like a JSON body: later server writes must not mutate a
            # snapshot the app already read.
            return copy.deepcopy(self._server[document_id])
        response = self._get
        if callable(response):
            response = response(document_id)
        if isinstance(response, BaseException):
            raise response
        return response

    def save_segments(self, document_id, expected_revision, updates):
        """Mirror the workspace save contract closely enough to test against.

        With a live server it behaves like service.save_segments: a revision
        mismatch is a 409 stale_revision (drafts must be kept), a match applies
        the fields each update carries and advances the revision by one. The
        mirror honours two service details the save flow depends on: a text edit
        runs the capture_edits rule (user_final is the raw text, so an explicit
        "" stays ""; edited_by_human is set only when the stripped text is
        non-empty and differs from the baseline - the model suggestion, else the
        original target), while a comment-only update sets user_comment and never
        touches edited_by_human. Without a server, a single configured response
        stands in (a transport error, a fixed dict, or a callable).
        """
        self.save_calls.append({
            "document_id": document_id,
            "expected_revision": expected_revision,
            "updates": {key: dict(upd) for key, upd in updates.items()},
        })
        if self._server is not None:
            snap = self._server[document_id]
            if snap["revision"] != expected_revision:
                raise RemoteError(
                    service="workspace", status=409, code="stale_revision",
                    current_revision=snap["revision"],
                    message="The document changed since it was read; reload and "
                            "retry.")
            # The service rejects the whole request when it names a key the
            # workspace does not have (422 unknown_segment, nothing applied).
            if any(key not in snap["segments"] for key in updates):
                raise RemoteError(
                    service="workspace", status=422, code="unknown_segment",
                    message="The request named a segment key this original "
                            "does not have.")
            before = {key: (snap["segments"][key]["user_final"],
                            snap["segments"][key]["user_comment"],
                            snap["segments"][key]["edited_by_human"])
                      for key in updates}
            for key, upd in updates.items():
                record = snap["segments"][key]
                if "text" in upd:
                    text = upd["text"]
                    record["user_final"] = text
                    final = (text or "").strip()
                    baseline = ((record.get("llm_suggestion") or "").strip()
                               or (record.get("mt_target") or "").strip())
                    if final and final != baseline:
                        record["edited_by_human"] = True
                if "comment" in upd:
                    record["user_comment"] = upd["comment"]
            changed = sorted(
                key for key in updates
                if (snap["segments"][key]["user_final"],
                    snap["segments"][key]["user_comment"],
                    snap["segments"][key]["edited_by_human"]) != before[key])
            # Identical content at the current revision is a no-op that keeps
            # the revision, exactly as the service does.
            if changed:
                snap["revision"] += 1
                snap["updated_at"] = "2026-01-02T00:00:00"
            return {
                "document_id": document_id,
                "revision": snap["revision"],
                "updated_keys": changed,
                "changed": bool(changed),
            }
        if isinstance(self._save, BaseException):
            raise self._save
        response = self._save
        if callable(response):
            response = response(document_id, expected_revision, updates)
            if isinstance(response, BaseException):
                raise response
        return response if response is not None else {}

    def record_suggestions(self, document_id, expected_revision, suggestions,
                           prefill=True):
        """Mirror the workspace suggestion-write contract for the proposal
        persist.

        With a live server it behaves like service.record_suggestions (which
        delegates to ``ProjectState.record_suggestions``): a revision mismatch is
        a 409 stale_revision, a key the workspace lacks is a 422 unknown_segment,
        a key outside the active order is a 422 inactive_segment, and a match
        writes each suggestion's text into ``llm_suggestion``, sets
        ``tag_report`` and ``status = "processed"``, and copies the text into
        ``user_final`` *unless* the row is protected by ``edited_by_human`` (a
        human's saved text is never overwritten). With ``prefill`` false the
        proposal is stored only - ``user_final`` is left untouched, so a model
        suggestion is not accepted as the user's text. A request that changes
        nothing is a no-op that keeps the revision. The response names the
        recorded, prefilled and protected keys. Without a server a single
        configured response stands in.
        """
        # The compatibility probe targets a workspace nothing is bound to, so it
        # never reaches a real write. A fresh service accepts the prefill field
        # and 404s "unknown workspace"; a stale service predates the contract
        # and 400s the unknown field. The probe is recognised by its fixed
        # sentinel payload (a single "1#1" probe suggestion, revision 1,
        # prefill off) that no real write produces, so it is recorded
        # separately and answered with the configured status in both the server
        # and no-server paths.
        if (suggestions == {"1#1": {"text": "probe"}} and expected_revision == 1
                and prefill is False):
            self.compat_probe_calls.append(document_id)
            status = self._compat_probe_status
            raise RemoteError(
                service="workspace", status=status, code="http_%d" % status,
                message=("The request was rejected as malformed."
                         if status == 400 else
                         "Unknown workspace; bind the document first."))
        self.record_calls.append({
            "document_id": document_id,
            "expected_revision": expected_revision,
            "suggestions": {
                "keys": sorted(suggestions.keys()),
                "text": dict((k, s["text"]) for k, s in suggestions.items()),
                "tag_report": dict((k, s.get("tag_report", ""))
                                  for k, s in suggestions.items()),
            },
            "prefill": prefill,
        })
        if self._server is not None:
            snap = self._server[document_id]
            if snap["revision"] != expected_revision:
                raise RemoteError(
                    service="workspace", status=409, code="stale_revision",
                    current_revision=snap["revision"],
                    message="The document changed since it was read; reload and "
                            "retry.")
            if any(key not in snap["segments"] for key in suggestions):
                raise RemoteError(
                    service="workspace", status=422, code="unknown_segment",
                    message="The request named a segment key this original "
                            "does not have.")
            active = set(snap.get("order") or [])
            inactive = sorted(k for k in suggestions if k not in active)
            if inactive:
                raise RemoteError(
                    service="workspace", status=422, code="inactive_segment",
                    message="Suggestions name segments outside the active "
                            "selection.",
                    details={"keys": inactive, "count": len(inactive)})
            # Capture the fields record_suggestions mutates, so a request that
            # changes nothing is a no-op that keeps the revision.
            def _fields(key):
                rec = snap["segments"][key]
                return (rec.get("llm_suggestion", ""),
                        rec.get("user_final", ""),
                        rec.get("tag_report", ""),
                        rec.get("status", "pending"),
                        rec.get("edited_by_human", False))
            before = {key: _fields(key) for key in suggestions}
            for key, item in suggestions.items():
                record = snap["segments"][key]
                text = item.get("text", "")
                record["llm_suggestion"] = text
                record["tag_report"] = item.get("tag_report", "")
                record["status"] = "processed"
                if prefill and not record.get("edited_by_human"):
                    record["user_final"] = text
            changed = sorted(
                key for key in suggestions if _fields(key) != before[key])
            if changed:
                snap["revision"] += 1
                snap["updated_at"] = "2026-01-02T00:00:00"
            keys = sorted(suggestions)
            return {
                "document_id": document_id,
                "revision": snap["revision"],
                "updated_keys": changed,
                "changed": bool(changed),
                "recorded_keys": keys,
                "prefilled_keys": sorted(k for k in keys
                                        if prefill
                                        and not snap["segments"][k]
                                        .get("edited_by_human")),
                "protected_keys": sorted(k for k in keys
                                        if snap["segments"][k]
                                        .get("edited_by_human")),
            }
        if isinstance(self._record, BaseException):
            raise self._record
        response = self._record
        if callable(response):
            response = response(document_id, expected_revision, suggestions)
            if isinstance(response, BaseException):
                raise response
        return response if response is not None else {}


class _FakeProcessing:
    def __init__(self, *, process_resp=None, ready_resp=None,
                 probe_resp=None, list_resp=None):
        # process_resp, when set, is the configured /process 2xx body (a dict the
        # app reads for "suggestions"/"missing"), an exception (a 502/503/400 the
        # real client maps to a RemoteError) or a callable(segments=...,
        # engine=..., tag_source=..., batch=..., context=..., config=...). Without
        # it process returns an empty body, so no proposal is stored - the
        # default keeps the read-only tests, where Process is never clicked,
        # unchanged.
        # ready_resp is the /readyz body a connection test reads (default a ready
        # stub); it never calls a model.
        # probe_resp is the /probe body a "Test model" click reads (default a
        # ready probe); list_resp is the /models body a "List models" click reads
        # (default an empty list). Neither is called unless its button is clicked.
        self._process = process_resp
        self._ready = ready_resp if ready_resp is not None else {
            "status": "ready", "engine": "stub",
            "checks": {"engine": "ok"},
        }
        self._probe = probe_resp
        self._list = list_resp
        self.process_calls = []
        self.ready_calls = []
        self.probe_calls = []
        self.list_calls = []

    def ready(self):
        self.ready_calls.append(None)
        response = self._ready
        if callable(response):
            response = response()
        if isinstance(response, BaseException):
            raise response
        return response

    def probe(self, config):
        self.probe_calls.append(config)
        response = self._probe
        if response is None:
            return {"ok": True, "status": "ok", "message": "Probe finished."}
        if callable(response):
            response = response(config)
        if isinstance(response, BaseException):
            raise response
        return response

    def list_models(self, config):
        self.list_calls.append(config)
        response = self._list
        if response is None:
            return {"models": []}
        if callable(response):
            response = response(config)
        if isinstance(response, BaseException):
            raise response
        return response

    def process(self, **kwargs):
        self.process_calls.append(kwargs)
        response = self._process
        if response is None:
            return {}
        if callable(response):
            response = response(**kwargs)
        if isinstance(response, BaseException):
            raise response
        return response if response is not None else {}


class _FakeClients:
    """A stand-in for ``RemoteClients``; the app holds it in ``ss._remote_clients``."""

    def __init__(self, document, workspace, processing):
        self.document = document
        self.workspace = workspace
        self.processing = processing
        self._session = None
        self._own_session = False

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class RemoteAppTestCase(unittest.TestCase):
    """Remote-mode UI behaviour, driven through Streamlit's headless AppTest."""

    def setUp(self):
        # Remote mode is selected by environment; the three service URLs must be
        # present or load_config raises and the app shows the config error and
        # stops. These are fake endpoints: no request is ever made.
        self._env_saved = dict(os.environ)
        os.environ["HARNESS_BACKEND"] = "remote"
        os.environ["HARNESS_DOCUMENT_URL"] = "http://doc.test"
        os.environ["HARNESS_WORKSPACE_URL"] = "http://ws.test"
        os.environ["HARNESS_PROCESSING_URL"] = "http://proc.test"
        self.app_path = os.path.join(ROOT, "app.py")
        self._patches = []
        self.fake = None

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        os.environ.clear()
        os.environ.update(self._env_saved)

    # ---------------------------------------------------------------- helpers

    def _install_fakes(self, *, upload_resp, segments_resp,
                       bind_resp=None, get_resp=None, server=None, save_resp=None,
                       process_resp=None, ready_resp=None, record_resp=None,
                       probe_resp=None, list_resp=None):
        document = _FakeDocument(upload_resp, segments_resp)
        workspace = _FakeWorkspace(bind_resp if bind_resp is not None else {},
                                   get_resp if get_resp is not None else {},
                                   server=server, save_resp=save_resp,
                                   record_resp=record_resp)
        processing = _FakeProcessing(process_resp=process_resp,
                                     ready_resp=ready_resp,
                                     probe_resp=probe_resp,
                                     list_resp=list_resp)
        self.fake = _FakeClients(document, workspace, processing)

        # get_remote_clients does `from harness_remote import build_clients` at
        # call time, so patching the module attribute is the injection seam.
        def factory(config):
            return self.fake

        patch = mock.patch("harness_remote.build_clients", side_effect=factory)
        patch.start()
        self._patches.append(patch)
        return self.fake

    def _run(self, app):
        app.run()
        self.assertEqual(app.exception, [], "app raised: %r" % (app.exception,))
        return app

    def run_app(self):
        app = AppTest.from_file(self.app_path, default_timeout=120)
        app.run()
        self.assertEqual(app.exception, [], "initial run raised: %r" % (app.exception,))
        return app

    def _button(self, scope, label):
        for btn in scope.button:
            if btn.label == label:
                return btn
        raise AssertionError("no button labelled %r in %r"
                             % (label, [b.label for b in scope.button]))

    def _button_labels(self, app):
        labels = [b.label for b in app.button]
        labels.extend(b.label for b in app.sidebar.button)
        return labels

    def _open(self, app, fake, filename="sample.mqxliff", content=b"<x/>"):
        """Drive a first Open (upload + bind + read) and let the grid render."""
        app.file_uploader[0].upload(filename, content, mime_type="application/xml")
        self._run(app)  # Open runs and triggers a rerun
        self._run(app)  # the rerun re-renders the grid without re-opening

    # ------------------------------------------------------------------- 1. mode

    def test_remote_mode_has_no_project_state(self):
        self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=[],
            get_resp=snapshot("doc-1", "orig-1", "target", 1, order=[], segments={}),
        )
        app = self.run_app()
        # The mode seam took the remote branch: no ProjectState is ever built.
        self.assertTrue(app.session_state.remote_mode)
        self.assertIsNone(app.session_state.project)
        self.assertIsNone(app.session_state.remote_view)

    # ------------------------------------------------- 2. open binds once, no rebind

    def test_open_binds_once_and_rerender_does_not_rebind(self):
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        snapshot_obj = snapshot("doc-1", "orig-1", "target", 1,
                                order=["k1", "k2"],
                                segments={"k1": ws_record("k1"), "k2": ws_record("k2")})
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            get_resp=snapshot_obj,
        )
        app = self.run_app()
        self.assertIsNone(app.session_state.remote_view)

        self._open(app, fake)

        view = app.session_state.remote_view
        self.assertIsNotNone(view)
        self.assertIsInstance(view, RemoteWorkspaceView)
        self.assertEqual(view.document_id, "doc-1")
        self.assertEqual(view.original_id, "orig-1")
        self.assertEqual(view.batch_count, 1)
        self.assertEqual(len(fake.document.upload_calls), 1)
        self.assertEqual(len(fake.workspace.bind_calls), 1)
        self.assertEqual(fake.workspace.bind_calls[0]["original_id"], "orig-1")

        # A plain rerun that re-exposes the same upload must not re-upload or
        # re-bind: the upload marker short-circuits maybe_remote_open.
        self._run(app)
        self._run(app)
        self.assertEqual(len(fake.document.upload_calls), 1)
        self.assertEqual(len(fake.workspace.bind_calls), 1)

    # ---------------------------------------------------- 3. failed open: no mix

    def test_failed_open_keeps_existing_view(self):
        good = snapshot("doc-1", "orig-1", "target", 1,
                        order=["k1"],
                        segments={"k1": ws_record("k1", user_final="human final")})
        err = RemoteError(service="document", status=None, code="connection_error",
                          message="Could not connect to document.")
        self._install_fakes(upload_resp=err,
                            segments_resp=[doc_segment("k1", "u1", 0, "s", "t")],
                            get_resp=good)
        app = self.run_app()
        # Seed the live view, as a prior successful Open would have.
        app.session_state.remote_view = RemoteWorkspaceView.from_remote([], good)
        seeded = app.session_state.remote_view

        # A new upload whose upload fails must not replace the live view.
        app.file_uploader[0].upload("other.mqxliff", b"<y/>", mime_type="application/xml")
        self._run(app)

        self.assertIs(app.session_state.remote_view, seeded)
        self.assertEqual(app.session_state.remote_view.record("k1").user_final,
                         "human final")
        # No bind and no new upload of a partial view happened.
        self.assertEqual(len(self.fake.workspace.bind_calls), 0)

    # ----------------------------------------------- 4. reload identity mismatch

    def test_reload_identity_mismatch_clears_view(self):
        # The live view was bound to orig-A. The re-read workspace now names
        # orig-B: a silent mismatch that must clear, not merge.
        live = snapshot("doc-1", "orig-A", "target", 1,
                        order=["k1"], segments={"k1": ws_record("k1")})
        mismatched = snapshot("doc-1", "orig-B", "target", 2,
                              order=["k1"], segments={"k1": ws_record("k1")})
        self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-A"},
            segments_resp=[doc_segment("k1", "u1", 0, "s", "t")],
            get_resp=mismatched,
        )
        app = self.run_app()
        app.session_state.remote_view = RemoteWorkspaceView.from_remote([], live)
        self.assertEqual(app.session_state.remote_view.original_id, "orig-A")
        # Re-render so the sidebar shows the Reload control for the seeded view.
        self._run(app)

        btn = self._button(app.sidebar, "Reload saved work")
        btn.click()
        self._run(app)

        self.assertIsNone(app.session_state.remote_view)

    # ------------------------------------------------ 5. reload match: refresh

    def test_reload_identity_match_refreshes_view(self):
        live = snapshot("doc-1", "orig-A", "target", 1,
                        order=["k1"], segments={"k1": ws_record("k1")})
        refreshed = snapshot("doc-1", "orig-A", "target", 5,
                             order=["k1", "k2"],
                             segments={"k1": ws_record("k1", user_final="kept"),
                                      "k2": ws_record("k2", llm_suggestion="proposal")})
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-A"},
            segments_resp=doc_segments,
            get_resp=refreshed,
        )
        app = self.run_app()
        app.session_state.remote_view = RemoteWorkspaceView.from_remote([], live)
        self.assertEqual(app.session_state.remote_view.revision, 1)
        # Re-render so the sidebar shows the Reload control for the seeded view.
        self._run(app)

        btn = self._button(app.sidebar, "Reload saved work")
        btn.click()
        self._run(app)

        view = app.session_state.remote_view
        self.assertIsNotNone(view)
        self.assertEqual(view.revision, 5)
        self.assertEqual(view.original_id, "orig-A")
        self.assertEqual(view.batch_count, 1)
        # Reload re-read the document segments and merged the fresh workspace.
        self.assertTrue(any(s in ("orig-A",) for s in [c[0] for c in
                                fake.document.segments_calls]))
        self.assertIn("k2", [r.key for r in view.batch_records(0)])
        self.assertEqual(view.record("k1").user_final, "kept")

    # --------------------------------------------- 6. read-only: no mutation UI

    def test_read_only_grid_has_no_mutation_controls(self):
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        snap = snapshot("doc-1", "orig-1", "target", 1,
                        order=["k1", "k2"],
                        segments={"k1": ws_record("k1", user_final="Le chat.",
                                                  status="applied",
                                                  edited_by_human=True),
                                 "k2": ws_record("k2", llm_suggestion="Le chien.",
                                                 user_final="Le chien.",
                                                 status="applied",
                                                 edited_by_human=True)})
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            get_resp=snap,
        )
        app = self.run_app()
        self._open(app, fake)

        # The two editable cells exist now (human-text text_area + comment
        # text_input, one pair per visible row); they are not mutation controls
        # - they hold an unsaved draft in session state and write only through
        # Save. The model section adds two more text inputs (Base URL + the
        # session-only key) for the per-request config.
        self.assertEqual(len(app.text_area), 2)
        self.assertEqual(len(app.text_input), 4)

        # The finished-mutation controls must be absent from every scope.
        labels = self._button_labels(app)
        for forbidden in ("Apply Changes & Calculate Diffs",
                          "Rerun", "Analyse my corrections",
                          "Export Valid MQXLIFF", "Reset active document"):
            self.assertNotIn(forbidden, labels)

        # The one gated mutation is offered, but disabled: every translatable
        # row already has human text (k1 saved, k2 saved), so no row is eligible
        # and a full Rerun UX is deferred.
        proc = self._button(app, "Process Current Batch")
        self.assertTrue(proc.disabled)

        # The only control that remains is the read-only Reload.
        self.assertIn("Reload saved work", labels)

        # A freshly opened document is clean: Save and Discard are shown in the
        # sidebar but disabled, and no save call is made.
        for label in ("Save", "Discard"):
            self.assertTrue(self._button(app.sidebar, label).disabled)
        self.assertEqual(fake.workspace.save_calls, [])

    # ----------------------- 7. local mutation entry points are never reached

    def test_local_mutation_entry_points_not_invoked(self):
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        snap = snapshot("doc-1", "orig-1", "target", 1,
                        order=["k1", "k2"],
                        segments={"k1": ws_record("k1"), "k2": ws_record("k2")})
        self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            get_resp=snap,
        )

        # The real local-mutation entry points, patched to record any call.
        save_mock = mock.Mock()
        init_mock = mock.Mock()
        translate_mock = mock.Mock()
        analyze_mock = mock.Mock()
        with mock.patch.object(state_mod.ProjectState, "save", save_mock), \
                mock.patch.object(llm.LLMClient, "__init__", init_mock), \
                mock.patch.object(llm.LLMClient, "translate_batch", translate_mock), \
                mock.patch.object(llm.LLMClient, "analyze_corrections", analyze_mock):
            app = self.run_app()
            self._open(app, self.fake)
            # Exercise a read-only Reload too. Batch navigation is a single batch
            # here, so its buttons are disabled by design (presentation-only) and
            # are not clicked.
            if "Reload saved work" in self._button_labels(app):
                self._button(app.sidebar, "Reload saved work").click()
                self._run(app)
            # Exercise the gated Process too: it must route through the fake
            # processing service, never the local LLMClient. The fake returns no
            # suggestions, so nothing is stored.
            if "Process Current Batch" in self._button_labels(app):
                self._button(app, "Process Current Batch").click()
                self._run(app)

        self.assertFalse(save_mock.called, "ProjectState.save was reached in remote mode")
        self.assertFalse(init_mock.called, "LLMClient was constructed in remote mode")
        self.assertFalse(translate_mock.called,
                         "translate_batch was reached in remote mode")
        self.assertFalse(analyze_mock.called,
                         "analyze_corrections was reached in remote mode")

    # ------------------------------------------- 8. empty workspace renders clean

    def test_empty_workspace_renders_no_segments(self):
        self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=[],
            get_resp=snapshot("doc-1", "orig-1", "target", 1, order=[], segments={}),
        )
        app = self.run_app()
        self._open(app, self.fake)
        view = app.session_state.remote_view
        self.assertIsNotNone(view)
        self.assertTrue(view.is_empty)
        # No rows, so neither editable cell renders; the model section still
        # offers its per-request config inputs (Base URL + session-only key).
        self.assertEqual(len(app.text_area), 0)
        self.assertEqual(len(app.text_input), 2)
        self.assertNotIn("Process Current Batch", self._button_labels(app))

    # -------- 9. the Open/Reload envelope-unwrap regression (the acceptance bug)

    def test_open_unwraps_segments_envelope(self):
        # The document service's /segments 2xx is {.., "segments": [...]}, not a
        # bare list; the app must read that list, not iterate the envelope's
        # string keys (the AttributeError the acceptance upload hit at
        # remote_open). This fails on the pre-fix code and passes after the fix.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        snap = snapshot("doc-1", "orig-1", "target", 1,
                        order=["k1", "k2"],
                        segments={"k1": ws_record("k1"), "k2": ws_record("k2")})
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,  # the fake wraps this into the envelope
            get_resp=snap,
        )
        app = self.run_app()
        self._open(app, fake)

        view = app.session_state.remote_view
        self.assertIsNotNone(view)
        self.assertIsInstance(view, RemoteWorkspaceView)
        self.assertEqual([r.key for r in view.batch_records(0)], ["k1", "k2"])
        # bind received the unwrapped, projected segment items (four fields
        # each) and the matching key set, never the envelope dict.
        self.assertEqual(len(fake.workspace.bind_calls), 1)
        self.assertEqual(fake.workspace.bind_calls[0]["segments"], [
            {"key": "k1", "unit_id": "u1", "source_text": "The cat.",
             "target_text": "Le chat."},
            {"key": "k2", "unit_id": "u2", "source_text": "The dog.",
             "target_text": "Le chien."},
        ])
        self.assertEqual(fake.workspace.bind_calls[0]["present_keys"], ["k1", "k2"])

    def test_malformed_segments_response_is_a_safe_error(self):
        # A 2xx that is not the envelope (here: a dict that omits the "segments"
        # list) must surface a safe, explicit error and keep the live view -
        # never an AttributeError and never a silently empty workspace.
        good = snapshot("doc-1", "orig-1", "target", 1,
                        order=["k1"],
                        segments={"k1": ws_record("k1", user_final="human final")})
        malformed = {"original_id": "orig-1", "document_id": "doc-1",
                     "tag_source": "target", "filter": {}}  # no "segments" list
        self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=malformed,
            get_resp=good,
        )
        app = self.run_app()
        # Seed the live view, as a prior successful Open would have.
        app.session_state.remote_view = RemoteWorkspaceView.from_remote([], good)
        seeded = app.session_state.remote_view

        # A new upload whose segments response is malformed must not replace it.
        app.file_uploader[0].upload("sample.mqxliff", b"<x/>",
                                    mime_type="application/xml")
        self._run(app)

        # Safe: the RemoteError was caught in the sidebar, so nothing crashed.
        self.assertEqual(app.exception, [])
        # The live view is preserved, not cleared to empty.
        self.assertIs(app.session_state.remote_view, seeded)
        self.assertEqual(app.session_state.remote_view.record("k1").user_final,
                         "human final")
        # No bind happened for the malformed document.
        self.assertEqual(len(self.fake.workspace.bind_calls), 0)

    def test_reload_malformed_segments_is_a_safe_error(self):
        # The Reload path must honour the same guarantee as Open: a 2xx that is
        # not the envelope is a safe, explicit error that keeps the live view -
        # not an AttributeError and not a silent empty view. The re-read
        # workspace still matches identity, so the failure is reached only at
        # the document-segments unwrap, exactly where the Open guard lives.
        live = snapshot("doc-1", "orig-A", "target", 1,
                        order=["k1"],
                        segments={"k1": ws_record("k1", user_final="kept")})
        re_read = snapshot("doc-1", "orig-A", "target", 2,
                           order=["k1"],
                           segments={"k1": ws_record("k1", user_final="kept")})
        malformed = {"original_id": "orig-A", "document_id": "doc-1",
                     "tag_source": "target", "filter": {}}  # no "segments" list
        self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-A"},
            segments_resp=malformed,
            get_resp=re_read,
        )
        app = self.run_app()
        # Seed the live view, as a prior successful Open would have.
        app.session_state.remote_view = RemoteWorkspaceView.from_remote([], live)
        seeded = app.session_state.remote_view
        self.assertEqual(seeded.original_id, "orig-A")
        # Re-render so the sidebar shows the Reload control for the seeded view.
        self._run(app)

        self._button(app.sidebar, "Reload saved work").click()
        self._run(app)

        # Safe: the RemoteError was caught at the Reload caller, so nothing
        # crashed into the run.
        self.assertEqual(app.exception, [])
        # The live view is preserved (remote_reload only reassigns on success),
        # not cleared to empty.
        self.assertIs(app.session_state.remote_view, seeded)
        self.assertEqual(app.session_state.remote_view.record("k1").user_final,
                         "kept")
        # Reload is a read: it never binds.
        self.assertEqual(len(self.fake.workspace.bind_calls), 0)

    # -- 10. the active-selection / present_keys projection against the real parser

    def test_open_projects_active_selection_and_full_present_keys(self):
        # The document service returns every segment - the active/translatable
        # one and a skipped/non-translatable one - each in the full 16-field
        # serialized shape. The bind must carry only the active selection,
        # projected to the four fields the workspace parser accepts, while
        # present_keys keeps every document key so the skipped row is not
        # discarded. Verified by feeding the app's actual payload back to the
        # real parse_bind_request, offline and without a database.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat.",
                        translatable=True, skip_reason=""),
            doc_segment("k2", "u2", 1, "Skip me.", "Skip.",
                        translatable=False, skip_reason="locked"),
        ]
        snap = snapshot("doc-1", ORIG_ID, "target", 1,
                        order=["k1", "k2"],
                        segments={"k1": ws_record("k1"), "k2": ws_record("k2")})
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": ORIG_ID},
            segments_resp=doc_segments,
            get_resp=snap,
        )
        app = self.run_app()
        self._open(app, fake)

        # The view keeps the full canonical list: both rows render, and the
        # skipped one is marked non-translatable.
        view = app.session_state.remote_view
        self.assertIsNotNone(view)
        self.assertEqual([r.key for r in view.batch_records(0)], ["k1", "k2"])
        self.assertTrue(view.record("k1").translatable)
        self.assertFalse(view.record("k2").translatable)

        # bind received the projected active selection: only the translatable
        # row, four fields each, never the 16-field item or the skipped row.
        self.assertEqual(len(fake.workspace.bind_calls), 1)
        call = fake.workspace.bind_calls[0]
        self.assertEqual(call["segments"], [
            {"key": "k1", "unit_id": "u1", "source_text": "The cat.",
             "target_text": "Le chat."},
        ])
        self.assertEqual(call["present_keys"], ["k1", "k2"])

        # The app's actual payload is exactly what the real workspace parser
        # accepts: run it through parse_bind_request, offline, no database.
        body = {
            "original_id": call["original_id"],
            "source_file": call["source_file"],
            "tag_source": call["tag_source"],
            "segments": call["segments"],
            "present_keys": call["present_keys"],
        }
        request = parse_bind_request(body)
        # Active selection: only the translatable segment, projected to 4 fields.
        self.assertEqual([s.key for s in request.segments], ["k1"])
        self.assertEqual([s.unit_id for s in request.segments], ["u1"])
        self.assertEqual(request.segments[0].source_text, "The cat.")
        self.assertEqual(request.segments[0].target_text, "Le chat.")
        # present_keys is the complete document key set, in order, so the skipped
        # key survives even though it is not in the active selection.
        self.assertEqual(list(request.present_keys), ["k1", "k2"])
        self.assertIn("k2", request.present_keys)

    def test_malformed_segment_item_is_a_safe_error(self):
        # A segments list carrying a non-object item is a malformed success: the
        # active-selection projection would call .get on it and raise
        # AttributeError. It must instead be a safe, explicit error before
        # bind/view construction, and the live view is preserved.
        good = snapshot("doc-1", "orig-1", "target", 1,
                        order=["k1"],
                        segments={"k1": ws_record("k1", user_final="human final")})
        malformed = {
            "original_id": "orig-1", "document_id": "doc-1",
            "tag_source": "target", "filter": {},
            "segments": [doc_segment("k1", "u1", 0, "s", "t"), "not-a-dict"],
        }
        self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=malformed,
            get_resp=good,
        )
        app = self.run_app()
        # Seed the live view, as a prior successful Open would have.
        app.session_state.remote_view = RemoteWorkspaceView.from_remote([], good)
        seeded = app.session_state.remote_view

        app.file_uploader[0].upload("sample.mqxliff", b"<x/>",
                                    mime_type="application/xml")
        self._run(app)

        # Safe: the RemoteError was caught in the sidebar, so nothing crashed.
        self.assertEqual(app.exception, [])
        # The live view is preserved, not cleared to empty.
        self.assertIs(app.session_state.remote_view, seeded)
        self.assertEqual(app.session_state.remote_view.record("k1").user_final,
                         "human final")
        # No bind happened for the malformed document.
        self.assertEqual(len(self.fake.workspace.bind_calls), 0)

    def test_missing_required_segment_field_is_a_safe_error(self):
        # A segment item missing a required field (here: no unit_id) is a
        # malformed success: the active-selection projection would index a
        # missing field and raise KeyError. It must instead be a safe, explicit
        # error before bind/view construction, and the live view is preserved.
        good = snapshot("doc-1", "orig-1", "target", 1,
                        order=["k1"],
                        segments={"k1": ws_record("k1", user_final="human final")})
        broken = doc_segment("k1", "u1", 0, "s", "t")
        del broken["unit_id"]
        malformed = {
            "original_id": "orig-1", "document_id": "doc-1",
            "tag_source": "target", "filter": {},
            "segments": [broken],
        }
        self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=malformed,
            get_resp=good,
        )
        app = self.run_app()
        # Seed the live view, as a prior successful Open would have.
        app.session_state.remote_view = RemoteWorkspaceView.from_remote([], good)
        seeded = app.session_state.remote_view

        app.file_uploader[0].upload("sample.mqxliff", b"<x/>",
                                    mime_type="application/xml")
        self._run(app)

        # Safe: the RemoteError was caught in the sidebar, so nothing crashed.
        self.assertEqual(app.exception, [])
        # The live view is preserved, not cleared to empty.
        self.assertIs(app.session_state.remote_view, seeded)
        self.assertEqual(app.session_state.remote_view.record("k1").user_final,
                         "human final")
        # No bind happened for the malformed document.
        self.assertEqual(len(self.fake.workspace.bind_calls), 0)

    # -------------------------- 10. process the selected batch (new slice)

    def _clean_server(self, segments=None):
        segs = segments or {
            "k1": ws_record("k1", source_text="The cat.", mt_target="Le chat."),
            "k2": ws_record("k2", source_text="The dog.", mt_target="Le chien."),
        }
        return {"doc-1": snapshot(
            "doc-1", "orig-1", "target", 1, order=list(segs), segments=segs)}

    def _open_clean(self, fake):
        app = self.run_app()
        self._open(app, fake)
        return app

    def test_process_stores_proposals_and_reload_shows_them(self):
        # A process of a clean batch routes through the processing service and
        # persists the proposals through the workspace suggestion contract as a
        # proposal-only write (prefill off): the text becomes the model's
        # llm_suggestion and is NOT copied into user_final (a model suggestion is
        # not a human acceptance), while the original target (mt_target) and
        # source text stay untouched - four distinct fields. The proposals
        # survive a Reload because they are in the workspace.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        server = self._clean_server()
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            process_resp={"suggestions": {"k1": {"text": "Le chat (model)"},
                                         "k2": {"text": "Le chien (model)"}},
                          "missing": []},
        )
        app = self._open_clean(fake)

        proc = self._button(app, "Process Current Batch")
        self.assertFalse(proc.disabled)
        proc.click()
        self._run(app)

        # The model call went through the processing service, and the proposals
        # were recorded through the workspace suggestion contract as a
        # proposal-only write (prefill off): a model suggestion is not a human
        # acceptance, so user_final is left untouched.
        self.assertEqual(len(fake.processing.process_calls), 1)
        self.assertEqual(len(fake.workspace.record_calls), 1)
        call = fake.workspace.record_calls[0]
        self.assertEqual(call["prefill"], False)
        self.assertEqual(call["suggestions"]["text"]["k1"], "Le chat (model)")
        self.assertEqual(call["suggestions"]["text"]["k2"], "Le chien (model)")

        # The adopted view shows the stored proposals in the model column; with
        # prefill off they are NOT copied into user_final (the proposal is not
        # accepted as the user's text), while the original target and source text
        # stay untouched - four distinct fields.
        view = app.session_state.remote_view
        self.assertEqual(view.record("k1").llm_suggestion, "Le chat (model)")
        self.assertEqual(view.record("k2").llm_suggestion, "Le chien (model)")
        self.assertEqual(view.record("k1").user_final, "")
        self.assertEqual(view.record("k1").mt_target, "Le chat.")
        self.assertEqual(view.record("k1").source_text, "The cat.")

        # Reload re-reads the workspace, so the proposals survive a browser
        # refresh (the acceptance criterion); the human text stays absent.
        self._button(app.sidebar, "Reload saved work").click()
        self._run(app)
        self.assertEqual(app.session_state.remote_view.record("k1").llm_suggestion,
                         "Le chat (model)")
        self.assertEqual(app.session_state.remote_view.record("k2").llm_suggestion,
                         "Le chien (model)")
        self.assertEqual(app.session_state.remote_view.record("k1").user_final, "")

    def test_unsaved_draft_does_not_block_eligible_neighbour(self):
        # Parity item 4: a held unsaved draft on one row no longer blocks the
        # batch. The eligible neighbour is still sent, and the row that holds
        # the draft is neither re-sent nor auto-saved: its draft survives the
        # round trip (a proposal write is prefill-off, so it never touches a
        # human's saved text or clears their in-progress edit).
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        server = self._clean_server()
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            process_resp={"suggestions": {"k2": {"text": "Le chien (model)"}},
                          "missing": []},
        )
        app = self._open_clean(fake)
        # Type an unsaved edit on k1, creating a held draft; k2 stays blank.
        app.text_area[0].set_value("an operator edit that is not yet saved")
        self._run(app)

        # k2 is still eligible, so the batch is not blocked.
        proc = self._button(app, "Process Current Batch")
        self.assertFalse(proc.disabled)
        proc.click()
        self._run(app)

        # Exactly one request, carrying only the eligible row (k2), not k1.
        self.assertEqual(len(fake.processing.process_calls), 1)
        self.assertEqual(
            [s["key"] for s in fake.processing.process_calls[0]["segments"]],
            ["k2"])
        # The proposal was recorded as a proposal-only write (prefill off).
        self.assertEqual(len(fake.workspace.record_calls), 1)
        self.assertEqual(fake.workspace.record_calls[0]["suggestions"]["keys"],
                         ["k2"])
        self.assertFalse(fake.workspace.record_calls[0]["prefill"])
        # k1 was neither re-sent nor auto-saved: its saved text stays absent and
        # its in-progress draft is preserved by the rebase after the adopt.
        self.assertEqual(server["doc-1"]["segments"]["k1"]["user_final"], "")
        drafts = app.session_state.remote_drafts.get("doc-1", {})
        self.assertEqual(drafts["k1"]["text"],
                         "an operator edit that is not yet saved")

    def test_previous_proposal_does_not_block_eligible_rows(self):
        # Parity item 4: the blanket "already worked" block is gone. A row that
        # holds a prior model proposal but whose effective 'Your text' (a draft
        # when held, else the saved text) is blank stays eligible, so a prior
        # proposal never suppresses an otherwise-eligible row.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        # k1 carries a prior proposal (llm_suggestion) but no saved human text;
        # k2 is blank. Neither has effective 'Your text', so both are eligible.
        server = {"doc-1": snapshot(
            "doc-1", "orig-1", "target", 1, order=["k1", "k2"],
            segments={"k1": ws_record("k1", llm_suggestion="Le chat."),
                      "k2": ws_record("k2")})}
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            process_resp={"suggestions": {"k1": {"text": "Le chat (model)"},
                                         "k2": {"text": "Le chien (model)"}},
                          "missing": []},
        )
        app = self._open_clean(fake)
        proc = self._button(app, "Process Current Batch")
        self.assertFalse(proc.disabled)
        proc.click()
        self._run(app)

        # Both eligible rows are sent, not just the one without a proposal.
        self.assertEqual(len(fake.processing.process_calls), 1)
        self.assertEqual(
            [s["key"] for s in fake.processing.process_calls[0]["segments"]],
            ["k1", "k2"])
        self.assertEqual(fake.workspace.record_calls[0]["suggestions"]["keys"],
                         ["k1", "k2"])
        self.assertFalse(fake.workspace.record_calls[0]["prefill"])

    def test_mixed_batch_sends_only_blank_rows(self):
        # Parity item 4: a filled neighbour is skipped, the blank rows in the
        # same batch go. A row that already has saved human text is not eligible
        # and is never re-sent.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        # k1 already has saved human text; k2 is blank.
        server = {"doc-1": snapshot(
            "doc-1", "orig-1", "target", 1, order=["k1", "k2"],
            segments={"k1": ws_record("k1", user_final="Le chat.",
                                      edited_by_human=True),
                      "k2": ws_record("k2")})}
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            process_resp={"suggestions": {"k2": {"text": "Le chien (model)"}},
                          "missing": []},
        )
        app = self._open_clean(fake)
        proc = self._button(app, "Process Current Batch")
        self.assertFalse(proc.disabled)
        proc.click()
        self._run(app)

        self.assertEqual(len(fake.processing.process_calls), 1)
        self.assertEqual(
            [s["key"] for s in fake.processing.process_calls[0]["segments"]],
            ["k2"])
        self.assertEqual(fake.workspace.record_calls[0]["suggestions"]["keys"],
                         ["k2"])
        # The filled neighbour is untouched.
        self.assertEqual(server["doc-1"]["segments"]["k1"]["user_final"],
                         "Le chat.")

    def test_cleared_draft_is_eligible(self):
        # Parity item 4: an existing draft - including one the operator cleared
        # to empty - is used instead of the saved text when judging eligibility,
        # so a cleared row becomes eligible again even though a saved value sits
        # underneath.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        # k1 has saved human text but is cleared to "" in a held draft; k2 is
        # filled, so only the cleared k1 is eligible.
        server = {"doc-1": snapshot(
            "doc-1", "orig-1", "target", 1, order=["k1", "k2"],
            segments={"k1": ws_record("k1", user_final="old saved text"),
                      "k2": ws_record("k2", user_final="Le chien.",
                                      edited_by_human=True)})}
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            process_resp={"suggestions": {"k1": {"text": "Le chat (model)"}},
                          "missing": []},
        )
        app = self._open_clean(fake)
        # Clear k1's cell, holding a cleared draft over its saved text.
        app.text_area[0].set_value("")
        self._run(app)

        proc = self._button(app, "Process Current Batch")
        self.assertFalse(proc.disabled)
        proc.click()
        self._run(app)

        # The cleared row is eligible; the filled neighbour is not.
        self.assertEqual(len(fake.processing.process_calls), 1)
        self.assertEqual(
            [s["key"] for s in fake.processing.process_calls[0]["segments"]],
            ["k1"])
        self.assertEqual(fake.workspace.record_calls[0]["suggestions"]["keys"],
                         ["k1"])

    def test_no_eligible_rows_sends_no_request(self):
        # Parity item 4: when every translatable row already has text, no row is
        # eligible, the button is disabled and no request is sent.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        # Both rows already hold saved human text: neither is eligible.
        server = {"doc-1": snapshot(
            "doc-1", "orig-1", "target", 1, order=["k1", "k2"],
            segments={"k1": ws_record("k1", user_final="Le chat.",
                                      edited_by_human=True),
                      "k2": ws_record("k2", user_final="Le chien.",
                                      edited_by_human=True)})}
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
        )
        app = self._open_clean(fake)

        proc = self._button(app, "Process Current Batch")
        self.assertTrue(proc.disabled)
        # No eligible row: no model call and no write reach the services.
        self.assertEqual(fake.processing.process_calls, [])
        self.assertEqual(fake.workspace.record_calls, [])

    def test_connection_test_caches_engine_without_model_call(self):
        # The readiness check is a service/SDK probe (GET /readyz): it never
        # calls a model, and on success it caches the engine so the sidebar's
        # readiness caption lists it.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        server = self._clean_server()
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            ready_resp={"status": "ready", "engine": "gpt-4o",
                        "checks": {"engine": "ok"}},
        )
        app = self._open_clean(fake)
        self._button(app.sidebar, "Check service readiness").click()
        self._run(app)

        self.assertEqual(len(fake.processing.ready_calls), 1)
        # The probe never called a model, and never sent a batch.
        self.assertEqual(fake.processing.process_calls, [])
        self.assertEqual(getattr(fake.processing, "probe_calls", []), [])
        self.assertEqual(getattr(fake.processing, "list_calls", []), [])
        # The engine is cached in the session so the readiness caption lists it.
        self.assertEqual(app.session_state.remote_ready["engine"], "gpt-4o")
        # The old "Engine" selectbox is gone; the operator now picks a model
        # through the per-request "Provider preset" selector, which offers the
        # stub plus the real presets.
        engine_boxes = [sb for sb in app.selectbox if sb.label == "Engine"]
        self.assertEqual(engine_boxes, [])
        preset_boxes = [sb for sb in app.selectbox if sb.label == "Provider preset"]
        self.assertEqual(len(preset_boxes), 1)
        self.assertIn("Test backend (stub)", preset_boxes[0].options)
        self.assertIn("OpenAI API", preset_boxes[0].options)

    def test_process_real_preset_sends_per_request_config_not_engine(self):
        # With the default (real) preset selected, Process sends the operator's
        # per-request config block (the one that selects the real backend,
        # endpoint and model) and no engine, so the service runs that config.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        server = self._clean_server()
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            process_resp={"suggestions": {"k1": {"text": "Le chat (model)"},
                                         "k2": {"text": "Le chien (model)"}},
                          "missing": []},
        )
        app = self._open_clean(fake)

        proc = self._button(app, "Process Current Batch")
        proc.click()
        self._run(app)

        self.assertEqual(len(fake.processing.process_calls), 1)
        call = fake.processing.process_calls[0]
        # A real preset carries the config block and no engine (mutually
        # exclusive: the service selects the backend/endpoint/model from it).
        self.assertIsNone(call.get("engine"))
        config = call["config"]
        self.assertIsInstance(config, dict)
        self.assertEqual(config["preset"], "Local Engine (Ollama)")
        self.assertEqual(config["backend"], "openai")
        self.assertEqual(config["model"], "qwen2.5:32b-instruct")
        self.assertEqual(config["api_key"], "")
        # The session key is empty here; the service may use its own provider
        # key for a no-key preset (Ollama), which is the local default.
        # Only this batch's translatable segments were sent.
        sent = {seg["key"] for seg in call["segments"]}
        self.assertEqual(sent, {"k1", "k2"})
        self.assertEqual(call["batch"], {"index": 0, "count": 1})

    def test_process_stub_preset_sends_engine_stub_not_config(self):
        # Selecting the explicit stub preset makes Process send engine="stub"
        # and no config, so the service returns the stored machine translation.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        server = self._clean_server()
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            process_resp={"suggestions": {"k1": {"text": "Le chat"},
                                         "k2": {"text": "Le chien"}},
                          "missing": []},
        )
        app = self._open_clean(fake)

        preset = [sb for sb in app.selectbox if sb.label == "Provider preset"][0]
        preset.set_value("Test backend (stub)")
        self._run(app)

        proc = self._button(app, "Process Current Batch")
        proc.click()
        self._run(app)

        self.assertEqual(len(fake.processing.process_calls), 1)
        call = fake.processing.process_calls[0]
        self.assertIsNone(call.get("config"))
        self.assertEqual(call.get("engine"), "stub")
        sent = {seg["key"] for seg in call["segments"]}
        self.assertEqual(sent, {"k1", "k2"})

    def test_test_model_drives_one_probe_and_no_suggestions(self):
        # "Test model" runs exactly one bounded probe through the processing
        # service and never writes a suggestion or a batch.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
        ]
        server = self._clean_server()
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            probe_resp={"ok": True, "status": "ok", "message": "Probe finished."},
        )
        app = self._open_clean(fake)

        test_btn = self._button(app.sidebar, "Test model")
        test_btn.click()
        self._run(app)

        self.assertEqual(len(fake.processing.probe_calls), 1)
        self.assertEqual(fake.processing.process_calls, [])
        self.assertEqual(fake.processing.list_calls, [])
        self.assertEqual(fake.workspace.record_calls, [])

    def test_list_models_drives_one_list_and_no_generation(self):
        # "List models" runs exactly one model-list call through the processing
        # service and never generates or stores anything.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
        ]
        server = self._clean_server()
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            list_resp={"models": ["m-a", "m-b"]},
        )
        app = self._open_clean(fake)

        list_btn = self._button(app.sidebar, "List models")
        list_btn.click()
        self._run(app)

        self.assertEqual(len(fake.processing.list_calls), 1)
        self.assertEqual(fake.processing.process_calls, [])
        self.assertEqual(fake.processing.probe_calls, [])
        self.assertEqual(fake.workspace.record_calls, [])
        # The fetched list is cached for the selector and tied to the config.
        self.assertEqual(app.session_state.remote_model_list, ["m-a", "m-b"])

    def test_reruns_and_open_reload_make_no_model_calls(self):
        # A rerun, an Open and a Reload never call the model (no probe, no
        # generation, no suggestion write): only the explicit actions do.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
        ]
        server = self._clean_server()
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
        )
        app = self._open_clean(fake)

        # A bare rerun (no button clicked) and a Reload: still zero model calls.
        self._run(app)
        self._button(app.sidebar, "Reload saved work").click()
        self._run(app)
        self._run(app)

        self.assertEqual(fake.processing.process_calls, [])
        self.assertEqual(fake.processing.probe_calls, [])
        self.assertEqual(fake.processing.list_calls, [])
        self.assertEqual(fake.workspace.record_calls, [])

    def test_process_proposal_only_persistence_leaves_user_final(self):
        # A Process persists the model's text as llm_suggestion only (prefill
        # off): user_final, user_comment and the original target stay untouched,
        # so a model proposal is never presented as the user's accepted text.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
        ]
        server = self._clean_server()
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            process_resp={"suggestions": {"k1": {"text": "Le chat (model)"}},
                          "missing": []},
        )
        app = self._open_clean(fake)

        proc = self._button(app, "Process Current Batch")
        proc.click()
        self._run(app)

        self.assertEqual(len(fake.workspace.record_calls), 1)
        call = fake.workspace.record_calls[0]
        self.assertEqual(call["prefill"], False)
        view = app.session_state.remote_view
        self.assertEqual(view.record("k1").llm_suggestion, "Le chat (model)")
        self.assertEqual(view.record("k1").user_final, "")
        self.assertEqual(view.record("k1").mt_target, "Le chat.")
        self.assertEqual(view.record("k1").user_comment, "")

    def test_process_failure_claims_nothing(self):
        # A processing failure (a 502 from the service) is a safe error: no
        # proposal is claimed, nothing is stored, and the run does not crash.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        server = self._clean_server()
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            process_resp=RemoteError(
                service="processing", status=502, code="engine_failed",
                message="The upstream request failed."),
        )
        app = self._open_clean(fake)
        proc = self._button(app, "Process Current Batch")
        self.assertFalse(proc.disabled)
        proc.click()
        self._run(app)

        self.assertEqual(app.exception, [])
        # One model attempt, but no proposal was stored on failure.
        self.assertEqual(len(fake.processing.process_calls), 1)
        self.assertEqual(fake.workspace.record_calls, [])
        self.assertEqual(app.session_state.remote_view.record("k1").llm_suggestion,
                         "")
        # A model-call failure is a safe error, not an uncertain write: it does
        # not set the flag, so the next Process is not blocked.
        self.assertFalse(app.session_state.remote_process_uncertain)

    def test_process_stale_workspace_blocks_before_model_call(self):
        # A workspace that predates the prefill contract 400s the proposal
        # write - the "rejected as malformed" failure that wasted a paid model
        # call before. The non-mutating compatibility probe detects this before
        # any model call, so a Process blocks with a restart instruction and no
        # inference is sent to the incompatible workspace.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        server = self._clean_server()
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            process_resp={"suggestions": {"k1": {"text": "Le chat."},
                                         "k2": {"text": "Le chien."}},
                          "missing": []},
        )
        # The probe answers with a 400, the signature of a stale workspace that
        # rejects the prefill field.
        fake.workspace._compat_probe_status = 400
        app = self._open_clean(fake)

        proc = self._button(app, "Process Current Batch")
        self.assertFalse(proc.disabled)
        proc.click()
        self._run(app)

        self.assertEqual(app.exception, [])
        # The probe ran, but no model call was sent and nothing was written.
        self.assertEqual(len(fake.workspace.compat_probe_calls), 1)
        self.assertEqual(fake.processing.process_calls, [])
        self.assertEqual(fake.workspace.record_calls, [])
        # The cached verdict is "stale" and the proposals are untouched.
        self.assertEqual(app.session_state._remote_workspace_compat, "stale")
        self.assertEqual(app.session_state.remote_view.record("k1").llm_suggestion,
                         "")

        # A second Process stays blocked by the cached stale verdict, with no
        # second probe and no model call.
        proc = self._button(app, "Process Current Batch")
        self.assertTrue(proc.disabled)
        self.assertEqual(len(fake.workspace.compat_probe_calls), 1)
        self.assertEqual(fake.processing.process_calls, [])

    def test_process_unknown_compat_proceeds_without_bypass(self):
        # An uncertain compatibility probe (neither a clean 404 nor a 400, e.g.
        # a 500) is "unknown": the Process proceeds without removing prefill or
        # switching to the stub, so a model call is made and the proposal is
        # written with prefill off.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        server = self._clean_server()
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            process_resp={"suggestions": {"k1": {"text": "Le chat."},
                                         "k2": {"text": "Le chien."}},
                          "missing": []},
        )
        fake.workspace._compat_probe_status = 500
        app = self._open_clean(fake)

        proc = self._button(app, "Process Current Batch")
        self.assertFalse(proc.disabled)
        proc.click()
        self._run(app)

        self.assertEqual(app.exception, [])
        # The probe ran (and was "unknown") but the Process still went through
        # with prefill off.
        self.assertEqual(len(fake.workspace.compat_probe_calls), 1)
        self.assertEqual(app.session_state._remote_workspace_compat, "unknown")
        self.assertEqual(len(fake.processing.process_calls), 1)
        self.assertEqual(len(fake.workspace.record_calls), 1)
        self.assertEqual(fake.workspace.record_calls[0]["prefill"], False)

    def test_process_stale_revision_does_not_replay(self):
        # When the workspace moved since it was read, the write 409s. On purpose
        # the proposals are left un-persisted with no re-read, rebase, overwrite
        # or retry: a stale write is never replayed.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        server = self._clean_server()
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            process_resp={"suggestions": {"k1": {"text": "Le chat."},
                                         "k2": {"text": "Le chien."}},
                          "missing": []},
        )
        app = self._open_clean(fake)
        # Move the workspace ahead of the read the batch was built on.
        fake.workspace._server["doc-1"]["revision"] += 1
        proc = self._button(app, "Process Current Batch")
        self.assertFalse(proc.disabled)
        proc.click()
        self._run(app)

        self.assertEqual(app.exception, [])
        # One model call, one write attempt, and no retry or re-read: the
        # proposals are not stored and the live view is left as it was.
        self.assertEqual(len(fake.processing.process_calls), 1)
        self.assertEqual(len(fake.workspace.record_calls), 1)
        self.assertEqual(app.session_state.remote_view.record("k1").llm_suggestion,
                         "")
        # A stale write leaves the run uncertain: a clean read must precede the
        # next Process, so the flag blocks it until a Reload reconciles.
        self.assertTrue(app.session_state.remote_process_uncertain)

    def test_process_blocked_until_reload_after_uncertain(self):
        # An uncertain write (a 502 from record_suggestions) sets the
        # remote_process_uncertain flag, which blocks another Process until a
        # successful Reload reconciles the view. No retry or replay happens.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        get_resp = snapshot(
            "doc-1", "orig-1", "target", 1, order=["k1", "k2"],
            segments={"k1": ws_record("k1", source_text="The cat.",
                                      mt_target="Le chat."),
                      "k2": ws_record("k2", source_text="The dog.",
                                      mt_target="Le chien.")})
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            get_resp=get_resp,
            process_resp={"suggestions": {"k1": {"text": "Le chat."},
                                         "k2": {"text": "Le chien."}},
                          "missing": []},
            record_resp=RemoteError(
                service="workspace", status=502, code="http_502",
                message="The upstream request failed."),
        )
        app = self._open_clean(fake)

        proc = self._button(app, "Process Current Batch")
        self.assertFalse(proc.disabled)
        proc.click()
        self._run(app)

        # One model call, one write attempt, and no retry: the uncertain flag is
        # set and the proposals are not adopted.
        self.assertEqual(len(fake.processing.process_calls), 1)
        self.assertEqual(len(fake.workspace.record_calls), 1)
        self.assertTrue(app.session_state.remote_process_uncertain)
        self.assertEqual(app.session_state.remote_view.record("k1").llm_suggestion,
                         "")

        # The flag blocks another Process: the button is now disabled.
        proc = self._button(app, "Process Current Batch")
        self.assertTrue(proc.disabled)
        self.assertEqual(len(fake.processing.process_calls), 1)  # no second call

        # A successful Reload reconciles the view and clears the flag.
        self._button(app.sidebar, "Reload saved work").click()
        self._run(app)
        self.assertFalse(app.session_state.remote_process_uncertain)
        proc = self._button(app, "Process Current Batch")
        self.assertFalse(proc.disabled)

    def test_process_ignores_suggestions_for_other_batches(self):
        # A suggestion for a segment in ANOTHER batch of the same document (one
        # the service returned but the app did not send this request) must not
        # be persisted. Only the current batch's keys are accepted.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
            doc_segment("k3", "u3", 2, "The bird.", "L'oiseau."),
            doc_segment("k4", "u4", 3, "The fish.", "Le poisson."),
        ]
        segments = {
            "k1": ws_record("k1", source_text="The cat.", mt_target="Le chat."),
            "k2": ws_record("k2", source_text="The dog.", mt_target="Le chien."),
            "k3": ws_record("k3", source_text="The bird.", mt_target="L'oiseau."),
            "k4": ws_record("k4", source_text="The fish.", mt_target="Le poisson."),
        }
        server = {"doc-1": snapshot(
            "doc-1", "orig-1", "target", 1,
            order=["k1", "k2", "k3", "k4"], segments=segments, batch_size=2)}
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            process_resp={"suggestions": {"k1": {"text": "Le chat."},
                                         "k2": {"text": "Le chien."},
                                         "k3": {"text": "L'oiseau (model)"}},
                          "missing": []},
        )
        app = self._open_clean(fake)
        proc = self._button(app, "Process Current Batch")
        self.assertFalse(proc.disabled)
        proc.click()
        self._run(app)

        # The service returned a k3 proposal, but k3 is in batch 1, not batch 0,
        # so it is not in this request's sent_keys and must not be persisted.
        self.assertEqual(len(fake.processing.process_calls), 1)
        self.assertEqual(len(fake.workspace.record_calls), 1)
        sent = fake.workspace.record_calls[0]["suggestions"]
        self.assertEqual(set(sent["keys"]), {"k1", "k2"})
        self.assertNotIn("k3", sent["keys"])
        self.assertEqual(server["doc-1"]["segments"]["k1"]["llm_suggestion"],
                         "Le chat.")
        self.assertEqual(server["doc-1"]["segments"]["k2"]["llm_suggestion"],
                         "Le chien.")
        self.assertEqual(server["doc-1"]["segments"]["k3"]["llm_suggestion"],
                         "")

    def test_process_drops_malformed_suggestions(self):
        # A malformed suggestion (a non-dict item, empty or whitespace-only
        # text) is dropped before persistence, so only usable proposals are
        # stored. The four kinds stay distinct and nothing malformed reaches
        # the workspace.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
            doc_segment("k3", "u3", 2, "The bird.", "L'oiseau."),
            doc_segment("k4", "u4", 3, "The fish.", "Le poisson."),
        ]
        segments = {
            "k1": ws_record("k1", source_text="The cat.", mt_target="Le chat."),
            "k2": ws_record("k2", source_text="The dog.", mt_target="Le chien."),
            "k3": ws_record("k3", source_text="The bird.", mt_target="L'oiseau."),
            "k4": ws_record("k4", source_text="The fish.", mt_target="Le poisson."),
        }
        server = {"doc-1": snapshot(
            "doc-1", "orig-1", "target", 1,
            order=["k1", "k2", "k3", "k4"], segments=segments, batch_size=4)}
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            process_resp={"suggestions": {
                "k1": {"text": "Le chat."},         # well-formed
                "k2": "not-a-dict",                 # malformed: not a dict
                "k3": {"text": "   "},              # malformed: whitespace-only
                "k4": {"text": "Le poisson (model)"},  # well-formed
            }, "missing": []},
        )
        app = self._open_clean(fake)
        proc = self._button(app, "Process Current Batch")
        self.assertFalse(proc.disabled)
        proc.click()
        self._run(app)

        # Only the two well-formed proposals are persisted; the malformed ones
        # are dropped, and the server's k2/k3 are left untouched.
        self.assertEqual(len(fake.processing.process_calls), 1)
        self.assertEqual(len(fake.workspace.record_calls), 1)
        sent = fake.workspace.record_calls[0]["suggestions"]
        self.assertEqual(set(sent["keys"]), {"k1", "k4"})
        self.assertEqual(sent["text"]["k1"], "Le chat.")
        self.assertEqual(sent["text"]["k4"], "Le poisson (model)")
        self.assertEqual(server["doc-1"]["segments"]["k2"]["llm_suggestion"],
                         "")
        self.assertEqual(server["doc-1"]["segments"]["k3"]["llm_suggestion"],
                         "")

    def test_process_rejects_non_object_envelope(self):
        # A process response that is not an object (e.g. a JSON array the service
        # somehow returned) carries no usable proposal: nothing is stored, the run
        # does not crash, and - because the model call is stateless and persists
        # nothing - the run is not marked uncertain, so the operator may retry.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        server = self._clean_server()
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            process_resp=[{"suggestions": {"k1": {"text": "Le chat."}}}])
        app = self._open_clean(fake)

        proc = self._button(app, "Process Current Batch")
        self.assertFalse(proc.disabled)
        proc.click()
        self._run(app)

        self.assertEqual(len(fake.processing.process_calls), 1)
        self.assertEqual(fake.workspace.record_calls, [])
        self.assertFalse(app.session_state.remote_process_uncertain)
        self.assertEqual(server["doc-1"]["segments"]["k1"]["llm_suggestion"], "")

    def test_process_rejects_non_object_suggestions(self):
        # A response whose "suggestions" is not an object (a string or a list)
        # carries no usable proposal: nothing is stored and the run does not
        # crash. As with a bad envelope this is a read-side failure, so the run
        # is not marked uncertain.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        server = self._clean_server()
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            process_resp={"suggestions": "not-a-dict", "missing": []})
        app = self._open_clean(fake)

        proc = self._button(app, "Process Current Batch")
        self.assertFalse(proc.disabled)
        proc.click()
        self._run(app)

        self.assertEqual(len(fake.processing.process_calls), 1)
        self.assertEqual(fake.workspace.record_calls, [])
        self.assertFalse(app.session_state.remote_process_uncertain)

    def test_process_drops_non_string_text(self):
        # A non-string "text" (an int, a list, a dict) is dropped before
        # persistence, so only string proposals are stored and nothing malformed
        # reaches the workspace.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
            doc_segment("k3", "u3", 2, "The bird.", "L'oiseau."),
            doc_segment("k4", "u4", 3, "The fish.", "Le poisson."),
        ]
        segments = {
            "k1": ws_record("k1", source_text="The cat.", mt_target="Le chat."),
            "k2": ws_record("k2", source_text="The dog.", mt_target="Le chien."),
            "k3": ws_record("k3", source_text="The bird.", mt_target="L'oiseau."),
            "k4": ws_record("k4", source_text="The fish.", mt_target="Le poisson."),
        }
        server = {"doc-1": snapshot(
            "doc-1", "orig-1", "target", 1,
            order=["k1", "k2", "k3", "k4"], segments=segments, batch_size=4)}
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            process_resp={"suggestions": {
                "k1": {"text": 5},                  # malformed: not a string
                "k2": {"text": ["Le chat"]},         # malformed: a list
                "k3": {"text": "L'oiseau (model)"},  # well-formed
                "k4": {"text": {"x": 1}},            # malformed: a dict
            }, "missing": []})
        app = self._open_clean(fake)

        proc = self._button(app, "Process Current Batch")
        self.assertFalse(proc.disabled)
        proc.click()
        self._run(app)

        self.assertEqual(len(fake.processing.process_calls), 1)
        self.assertEqual(len(fake.workspace.record_calls), 1)
        sent = fake.workspace.record_calls[0]["suggestions"]
        self.assertEqual(set(sent["keys"]), {"k3"})
        self.assertEqual(sent["text"]["k3"], "L'oiseau (model)")
        for key in ("k1", "k2", "k4"):
            self.assertEqual(server["doc-1"]["segments"][key]["llm_suggestion"], "")

    def test_process_forwards_tag_report_through_write(self):
        # The actual Process path preserves a tag report the service supplies: the
        # builder forwards a valid string tag_report (not just text), the
        # workspace stores it on the row, and the confirming read shows it. This
        # is the end-to-end check the direct record_suggestions contract test in
        # test_remote_edit cannot give. A row the service did not carry a report
        # for keeps an empty tag report (the workspace clears an absent one).
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        server = self._clean_server()
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            process_resp={"suggestions": {
                "k1": {"text": "Le chat (model)", "tag_report": "gloss term"},
                "k2": {"text": "Le chien (model)"}},
                "missing": []})
        app = self._open_clean(fake)

        proc = self._button(app, "Process Current Batch")
        self.assertFalse(proc.disabled)
        proc.click()
        self._run(app)

        # The report is forwarded on k1 only; k2 carried none, so its entry is
        # text-only and its row's tag report is left empty.
        self.assertEqual(len(fake.workspace.record_calls), 1)
        sent = fake.workspace.record_calls[0]["suggestions"]
        self.assertEqual(sent["text"]["k1"], "Le chat (model)")
        self.assertEqual(sent["tag_report"]["k1"], "gloss term")
        self.assertEqual(sent["text"]["k2"], "Le chien (model)")
        # The stored value and the re-adopted view both carry the report.
        self.assertEqual(server["doc-1"]["segments"]["k1"]["tag_report"],
                         "gloss term")
        self.assertEqual(server["doc-1"]["segments"]["k2"]["tag_report"], "")
        view = app.session_state.remote_view
        self.assertEqual(view.record("k1").tag_report, "gloss term")
        self.assertEqual(view.record("k2").tag_report, "")

    def test_process_drops_overlong_tag_report(self):
        # A tag report longer than the workspace allows is dropped (the text is
        # still sent), so the write is not rejected as a 422 that would block the
        # whole batch as an uncertain write.
        doc_segments = [doc_segment("k1", "u1", 0, "The cat.", "Le chat.")]
        server = self._clean_server(segments={
            "k1": ws_record("k1", source_text="The cat.", mt_target="Le chat.")})
        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            server=server,
            process_resp={"suggestions": {
                "k1": {"text": "Le chat.", "tag_report": "r" * 1001}},
                "missing": []})
        app = self._open_clean(fake)

        proc = self._button(app, "Process Current Batch")
        self.assertFalse(proc.disabled)
        proc.click()
        self._run(app)

        self.assertEqual(len(fake.processing.process_calls), 1)
        self.assertEqual(len(fake.workspace.record_calls), 1)
        # The over-long report is omitted; the text is still recorded, the row's
        # tag report is cleared (an absent one), with no uncertain flag. The
        # write is proposal-only (prefill off), so user_final is not touched.
        sent = fake.workspace.record_calls[0]["suggestions"]
        self.assertEqual(sent["text"]["k1"], "Le chat.")
        self.assertEqual(sent["tag_report"]["k1"], "")
        self.assertEqual(fake.workspace.record_calls[0]["prefill"], False)
        self.assertEqual(server["doc-1"]["segments"]["k1"]["tag_report"], "")
        self.assertEqual(server["doc-1"]["segments"]["k1"]["user_final"], "")
        self.assertFalse(app.session_state.remote_process_uncertain)

    def test_process_confirmed_write_then_read_failure_sets_uncertain(self):
        # The write confirmed (a 2xx) but the confirming read failed: the saved
        # state is uncertain, so the flag blocks another Process until a Reload
        # reconciles it. No retry, no replay, and the run does not crash.
        doc_segments = [
            doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
            doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        ]
        snap = snapshot(
            "doc-1", "orig-1", "target", 1, order=["k1", "k2"],
            segments={"k1": ws_record("k1", source_text="The cat.",
                                      mt_target="Le chat."),
                      "k2": ws_record("k2", source_text="The dog.",
                                      mt_target="Le chien.")})
        gate = {"recorded": False}

        def get_factory(document_id):
            if gate["recorded"]:
                raise RemoteError(
                    service="workspace", status=502, code="http_502",
                    message="The upstream request failed.")
            return copy.deepcopy(snap)

        def record_factory(document_id, expected_revision, suggestions):
            gate["recorded"] = True
            return {"document_id": document_id,
                    "revision": expected_revision + 1,
                    "updated_keys": sorted(suggestions),
                    "changed": True,
                    "recorded_keys": sorted(suggestions)}

        fake = self._install_fakes(
            upload_resp={"document_id": "doc-1", "original_id": "orig-1"},
            segments_resp=doc_segments,
            get_resp=get_factory,
            process_resp={"suggestions": {"k1": {"text": "Le chat."},
                                         "k2": {"text": "Le chien."}},
                          "missing": []},
            record_resp=record_factory)
        app = self._open_clean(fake)

        proc = self._button(app, "Process Current Batch")
        self.assertFalse(proc.disabled)
        proc.click()
        self._run(app)

        # One model call and one write; the confirming read failed, so the view
        # is not re-adopted and the flag blocks another Process.
        self.assertEqual(len(fake.processing.process_calls), 1)
        self.assertEqual(len(fake.workspace.record_calls), 1)
        self.assertTrue(app.session_state.remote_process_uncertain)
        self.assertEqual(app.session_state.remote_view.record("k1")
                         .llm_suggestion, "")
        proc = self._button(app, "Process Current Batch")
        self.assertTrue(proc.disabled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
