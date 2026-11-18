"""Headless AppTest over ``app.py`` in remote mode, with fake ``RemoteClients``.

These exercise the read-only UI path that was wired into the app this slice:
the config seam (no ``ProjectState`` in remote mode), a one-shot Open that never
re-binds on rerun, a Reload that verifies the saved workspace still matches the
open document before it merges, the guarantee that a failed Open never produces
a mixed view, and the guarantee that no finished mutation control (Process /
Apply / Rerun / Analyse / Export) or editable field is ever reachable in remote
mode.

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
    def __init__(self, bind_resp, get_resp, *, server=None, save_resp=None):
        # server, when set, is a {document_id: snapshot} dict the workspace owns
        # and mutates on save; get_workspace then returns the live snapshot, so a
        # save that advances the revision is visible to a following read - the
        # post-save refresh and the 409/conflict tests need exactly that.
        # save_resp, when a server is not set, is a single configured response
        # (dict / exception / callable(document_id, expected_revision, updates)).
        self._bind = bind_resp
        self._get = get_resp
        self._server = server
        self._save = save_resp
        self.bind_calls = []
        self.get_calls = []
        self.save_calls = []

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


class _FakeProcessing:
    def __init__(self):
        self.process_calls = []

    def process(self, **kwargs):
        self.process_calls.append(kwargs)
        return {}


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
                       bind_resp=None, get_resp=None, server=None, save_resp=None):
        document = _FakeDocument(upload_resp, segments_resp)
        workspace = _FakeWorkspace(bind_resp if bind_resp is not None else {},
                                   get_resp if get_resp is not None else {},
                                   server=server, save_resp=save_resp)
        processing = _FakeProcessing()
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
                                 "k2": ws_record("k2", llm_suggestion="Le chien.")})
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
        # Save.
        self.assertEqual(len(app.text_area), 2)
        self.assertEqual(len(app.text_input), 2)

        # The finished-mutation controls must be absent from every scope.
        labels = self._button_labels(app)
        for forbidden in ("Process Current Batch", "Apply Changes & Calculate Diffs",
                          "Rerun", "Analyse my corrections",
                          "Export Valid MQXLIFF", "Reset active document"):
            self.assertNotIn(forbidden, labels)

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
        # No rows, so neither editable cell renders.
        self.assertEqual(len(app.text_area), 0)
        self.assertEqual(len(app.text_input), 0)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
