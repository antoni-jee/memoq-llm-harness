"""Remote edit/save slice: drafts, explicit Save, dirty Open/Reload, conflicts.

Most tests drive the real app (headless AppTest) against the **real**
workspace contract: the app's own ``WorkspaceClient`` talks through a tiny
``requests``-shaped adapter to the real workspace Starlette app, backed by the
real ``WorkspaceService`` over an in-memory repository with the PostgreSQL
repository's transaction contract. So the save payload, the revision check,
``capture_edits``, the 409 body and the client's error mapping are all the
production code - only the database and the network are substituted. The
document service stays a fake (its upload/segments contract is covered by the
read-only suite). A few navigation tests use the in-file fake workspace, whose
save mirror is kept faithful to the service (see test_remote_app.py).

Offline: no socket is opened and no paid or live call is made.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import threading
import unittest
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import urlsplit

import requests
from starlette.testclient import TestClient
from streamlit.testing.v1 import AppTest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import test_remote_app as base  # noqa: E402  (helpers only; its TestCase is not re-exported)
from harness_remote.client import WorkspaceClient  # noqa: E402
from memoq_harness import state as state_mod  # noqa: E402
from services.workspace_service.app import create_app  # noqa: E402
from services.workspace_service.repository import WorkspaceRow  # noqa: E402
from services.workspace_service.service import (  # noqa: E402
    WorkspaceService, parse_patch_request, parse_suggestions_request,
)

DOC_A = "a" * 64
DOC_B = "b" * 64
ORIG_A = "1" * 64
ORIG_B = "2" * 64


class _MemoryRepository:
    """In-memory repository with PostgresWorkspaceRepository's contract
    (same shape as tests/test_workspace_service.py's FakeRepository)."""

    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()

    def status(self, expected_version):
        return "ok"

    def get(self, document_id):
        return self.rows.get(document_id)

    def transact(self, document_id, decide):
        with self.lock:
            current = self.rows.get(document_id)
            new, payload = decide(current)
            if new is None:
                return current, payload, False
            now = datetime.now(timezone.utc)
            stored = WorkspaceRow(
                document_id=document_id,
                snapshot=json.loads(json.dumps(new.snapshot)),  # JSONB-like round trip
                state_version=new.state_version, revision=new.revision,
                tag_source=new.tag_source, bound_original_id=new.bound_original_id,
                created_at=current.created_at if current else now, updated_at=now)
            self.rows[document_id] = stored
            return stored, payload, True


class _ServiceSession:
    """``requests.Session`` stand-in routing the real client to the real app.

    ``fail`` (optional) is ``callable(method, path) -> None | "before" |
    "after"``: "before" raises a connection error without reaching the service
    (nothing stored); "after" forwards the request (the service stores it) and
    then raises a read timeout, so the client never sees the response - the
    ambiguous case.
    """

    def __init__(self, test_client):
        self.tc = test_client
        self.calls = []
        self.fail = None

    def request(self, method, url, *, timeout=None, json=None, files=None,
                params=None, headers=None, allow_redirects=True):
        path = urlsplit(url).path
        self.calls.append((method, path, copy.deepcopy(json)))
        mode = self.fail(method, path) if self.fail else None
        if mode == "before":
            raise requests.exceptions.ConnectionError("down")
        response = self.tc.request(method, path, json=json, params=params,
                                   headers=headers, follow_redirects=False)
        if mode == "after":
            raise requests.exceptions.ReadTimeout("lost")
        return response

    def close(self):
        pass

    def patches(self):
        return [c for c in self.calls if c[0] == "PATCH"]


def _segments_a():
    return [
        base.doc_segment("k1", "u1", 0, "The cat.", "Le chat."),
        base.doc_segment("k2", "u2", 1, "The dog.", "Le chien."),
        base.doc_segment("k3", "u3", 2, "The bird.", "L'oiseau."),
    ]


def _segments_b():
    return [base.doc_segment("k1", "u1", 0, "A house.", "Une maison.")]


class RemoteEditTestCase(unittest.TestCase):

    def setUp(self):
        self._env_saved = dict(os.environ)
        os.environ["HARNESS_BACKEND"] = "remote"
        os.environ["HARNESS_DOCUMENT_URL"] = "http://doc.test"
        os.environ["HARNESS_WORKSPACE_URL"] = "http://ws.test"
        os.environ["HARNESS_PROCESSING_URL"] = "http://proc.test"
        patcher = mock.patch.object(state_mod.ProjectState, "save",
                                    side_effect=AssertionError("save() must not be called"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = _MemoryRepository()
        self.service = WorkspaceService(self.repo, "0001", max_body_bytes=2 * 1024 * 1024)
        self.tc = TestClient(create_app(self.service), raise_server_exceptions=False)
        self.session = _ServiceSession(self.tc)

    def tearDown(self):
        self.tc.close()
        os.environ.clear()
        os.environ.update(self._env_saved)

    # ---------------------------------------------------------------- helpers

    def install(self, uploads=None, segments_by_original=None):
        uploads = uploads or [{"document_id": DOC_A, "original_id": ORIG_A}]
        by_orig = segments_by_original or {ORIG_A: _segments_a(), ORIG_B: _segments_b()}
        self.document = base._FakeDocument(
            list(uploads), lambda original_id, **kw: copy.deepcopy(by_orig[original_id]))
        workspace = WorkspaceClient("http://ws.test", http_timeout=(1.0, 1.0),
                                    session=self.session)
        self.fake = base._FakeClients(self.document, workspace, base._FakeProcessing())
        patch = mock.patch("harness_remote.build_clients",
                           side_effect=lambda config: self.fake)
        patch.start()
        self.addCleanup(patch.stop)

    def _run(self, app):
        app.run()
        self.assertEqual(app.exception, [], "app raised: %r" % (app.exception,))
        return app

    def start(self, filename="a.mqxliff", content=b"<a/>"):
        app = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=120)
        self._run(app)
        app.file_uploader[0].upload(filename, content, mime_type="application/xml")
        self._run(app)
        self._run(app)
        self.assertIsNotNone(app.session_state.remote_view)
        return app

    def upload(self, app, filename, content):
        app.file_uploader[0].upload(filename, content, mime_type="application/xml")
        self._run(app)
        self._run(app)

    def click(self, app, label):
        for btn in list(app.button) + list(app.sidebar.button):
            if btn.label == label:
                btn.click()
                self._run(app)  # AppTest follows the handler's st.rerun()
                return
        raise AssertionError("no button %r in %r" % (
            label, [b.label for b in list(app.button) + list(app.sidebar.button)]))

    def sidebar_button(self, app, label):
        return [b for b in app.sidebar.button if b.label == label][0]

    def save_enabled(self, app):
        return not self.sidebar_button(app, "Save").disabled

    def labels(self, app):
        return [b.label for b in list(app.button) + list(app.sidebar.button)]

    def messages(self, app):
        out = []
        for kind in ("success", "error", "warning", "info"):
            out.extend((kind, el.value) for el in getattr(app, kind))
        return out

    def edit_text(self, app, index, value):
        app.text_area[index].input(value)
        self._run(app)

    def edit_comment(self, app, index, value):
        app.text_input[index].input(value)
        self._run(app)

    def stored(self, key, document_id=DOC_A):
        return self.repo.rows[document_id].snapshot["segments"][key]

    def revision(self, document_id=DOC_A):
        return self.repo.rows[document_id].revision

    def other_writer(self, updates, document_id=DOC_A):
        """A concurrent save through the real service (another session)."""
        return self.service.save_segments(document_id, parse_patch_request(
            {"expected_revision": self.revision(document_id), "segments": updates}))

    def drafts(self, app, document_id=DOC_A):
        return app.session_state.remote_drafts.get(document_id, {})

    # ------------------------------------------------------------- the basics

    def test_edit_and_save_through_real_workspace_contract(self):
        self.install()
        app = self.start()
        rev = self.revision()
        # Seed a model suggestion through the real suggestions endpoint so the
        # four concepts - original target, suggestion, saved text, draft - are
        # all present and distinct.
        self.service.record_suggestions(DOC_A, parse_suggestions_request(
            {"expected_revision": rev, "suggestions": {"k1": {"text": "Le chat (LLM)."}}}))
        self.click(app, "Reload saved work")
        rev = self.revision()
        saved_before = self.stored("k1")["user_final"]

        self.edit_text(app, 0, "Le chat humain.")
        self.edit_comment(app, 0, "tone")
        # A draft only: nothing was written, the server is unchanged.
        self.assertEqual(self.session.patches(), [])
        self.assertEqual(self.drafts(app)["k1"]["text"], "Le chat humain.")
        self.assertTrue(self.save_enabled(app))
        record = app.session_state.remote_view.record("k1")
        self.assertEqual(record.user_final, saved_before)   # saved != draft
        self.assertEqual(record.mt_target, "Le chat.")       # original target
        self.assertEqual(record.llm_suggestion, "Le chat (LLM).")

        self.click(app, "Save")
        patches = self.session.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0][2], {"expected_revision": rev, "segments": {
            "k1": {"text": "Le chat humain.", "comment": "tone"}}})
        self.assertEqual(self.revision(), rev + 1)
        self.assertEqual(self.stored("k1")["user_final"], "Le chat humain.")
        self.assertEqual(self.stored("k1")["user_comment"], "tone")
        self.assertTrue(self.stored("k1")["edited_by_human"])
        self.assertEqual(self.stored("k1")["llm_suggestion"], "Le chat (LLM).")
        view = app.session_state.remote_view
        self.assertEqual(view.revision, rev + 1)
        self.assertEqual(view.record("k1").user_final, "Le chat humain.")
        self.assertEqual(view.record("k1").mt_target, "Le chat.")
        self.assertEqual(self.drafts(app), {})
        self.assertFalse(self.save_enabled(app))
        self.assertEqual(app.text_area[0].value, "Le chat humain.")
        self.assertTrue(any(k == "success" and "Saved 1 segment" in m
                            for k, m in self.messages(app)))

    def test_record_suggestions_real_contract_prefill_protection_no_op(self):
        # The real WorkspaceClient -> workspace app -> WorkspaceService ->
        # repository contract for a model write: a clean row is prefilled, a
        # human-protected row keeps its text and flag, the original target is
        # left untouched, a change advances the revision, and an identical write
        # is a no-op that keeps it.
        self.install()
        app = self.start()
        # A human already saved k2, so its row is protected (edited_by_human).
        self.other_writer({"k2": {"text": "Human text."}})
        rev = self.revision()

        result = self.fake.workspace.record_suggestions(
            DOC_A, rev,
            {"k1": {"text": "Le chat (LLM)."},
             "k2": {"text": "Le chien (LLM)", "tag_report": "gloss term"}})

        # k1 is clean: the suggestion prefills user_final and marks it
        # processed, with the original target left untouched.
        self.assertEqual(self.stored("k1")["llm_suggestion"], "Le chat (LLM).")
        self.assertEqual(self.stored("k1")["user_final"], "Le chat (LLM).")
        self.assertEqual(self.stored("k1")["status"], "processed")
        self.assertEqual(self.stored("k1")["tag_report"], "")
        self.assertEqual(self.stored("k1")["mt_target"], "Le chat.")
        # k2 is protected: the suggestion sets llm_suggestion and tag_report, but
        # the human's user_final and the edited_by_human flag are kept.
        self.assertEqual(self.stored("k2")["llm_suggestion"], "Le chien (LLM)")
        self.assertEqual(self.stored("k2")["user_final"], "Human text.")
        self.assertEqual(self.stored("k2")["tag_report"], "gloss term")
        self.assertTrue(self.stored("k2")["edited_by_human"])
        # The change advanced the revision and the response named the rows.
        self.assertTrue(result["changed"])
        self.assertEqual(result["revision"], rev + 1)
        self.assertEqual(result["recorded_keys"], ["k1", "k2"])
        self.assertEqual(result["prefilled_keys"], ["k1"])
        self.assertEqual(result["protected_keys"], ["k2"])
        self.assertEqual(self.revision(), rev + 1)
        # A reload shows the stored proposal on the clean row; the protected row
        # keeps the human's text.
        self.click(app, "Reload saved work")
        self.assertEqual(app.session_state.remote_view.record("k1").llm_suggestion,
                         "Le chat (LLM).")
        self.assertEqual(app.session_state.remote_view.record("k2").user_final,
                         "Human text.")

        # An identical write at the new revision changes nothing: a no-op keeps
        # the revision and the protected row's text.
        no_op = self.fake.workspace.record_suggestions(
            DOC_A, rev + 1,
            {"k1": {"text": "Le chat (LLM)."},
             "k2": {"text": "Le chien (LLM)", "tag_report": "gloss term"}})
        self.assertFalse(no_op["changed"])
        self.assertEqual(no_op["revision"], rev + 1)
        self.assertEqual(self.revision(), rev + 1)
        self.assertEqual(self.stored("k1")["user_final"], "Le chat (LLM).")
        self.assertEqual(self.stored("k2")["user_final"], "Human text.")

    def test_process_proposal_only_does_not_accept_human_text(self):
        # The remote Process path writes a proposal only (prefill off): on a
        # clean row it stores the model text as llm_suggestion without ever
        # populating user_final, so the proposal is not accepted as the user's
        # text. The proposal survives a Reload, and a later manual Save is the
        # only thing that may make it the user's text.
        self.install()
        app = self.start()
        rev = self.revision()

        result = self.fake.workspace.record_suggestions(
            DOC_A, rev, {"k1": {"text": "Le chat (LLM)."}}, prefill=False)

        # The proposal is stored, but user_final is untouched: no automatic
        # acceptance. The original target and source stay distinct and the row
        # is only marked processed.
        self.assertEqual(self.stored("k1")["llm_suggestion"], "Le chat (LLM).")
        self.assertEqual(self.stored("k1")["user_final"], "")
        self.assertEqual(self.stored("k1")["status"], "processed")
        self.assertEqual(self.stored("k1")["tag_report"], "")
        self.assertEqual(self.stored("k1")["mt_target"], "Le chat.")
        self.assertEqual(self.stored("k1")["source_text"], "The cat.")
        self.assertFalse(self.stored("k1")["edited_by_human"])
        # The write advanced the revision (the proposal is persisted), so it
        # survives a Reload; nothing was prefilled or protected.
        self.assertTrue(result["changed"])
        self.assertEqual(result["revision"], rev + 1)
        self.assertEqual(result["recorded_keys"], ["k1"])
        self.assertEqual(result["prefilled_keys"], [])
        self.assertEqual(result["protected_keys"], [])
        self.assertEqual(self.revision(), rev + 1)
        # Reload preserves the proposal without fabricating human text.
        self.click(app, "Reload saved work")
        view = app.session_state.remote_view
        self.assertEqual(view.record("k1").llm_suggestion, "Le chat (LLM).")
        self.assertEqual(view.record("k1").user_final, "")
        self.assertEqual(view.record("k1").mt_target, "Le chat.")

    def test_process_proposal_only_keeps_human_text_and_comment(self):
        # A proposal-only write over a row the human already saved leaves the
        # human's text, comment and edited_by_human flag untouched - only the
        # proposal (and its tag report) is recorded.
        self.install()
        app = self.start()
        # The human already saved k1 with a comment.
        self.other_writer({"k1": {"text": "Le chat humain.", "comment": "tone note"}})
        rev = self.revision()

        result = self.fake.workspace.record_suggestions(
            DOC_A, rev,
            {"k1": {"text": "Le chat (LLM).", "tag_report": "gloss term"}},
            prefill=False)

        # The proposal and its report are recorded; the human's saved text and
        # comment, the protected flag and the original target stay.
        self.assertEqual(self.stored("k1")["llm_suggestion"], "Le chat (LLM).")
        self.assertEqual(self.stored("k1")["user_final"], "Le chat humain.")
        self.assertEqual(self.stored("k1")["user_comment"], "tone note")
        self.assertTrue(self.stored("k1")["edited_by_human"])
        self.assertEqual(self.stored("k1")["tag_report"], "gloss term")
        self.assertEqual(self.stored("k1")["mt_target"], "Le chat.")
        self.assertEqual(result["prefilled_keys"], [])
        self.assertEqual(result["protected_keys"], ["k1"])
        self.assertTrue(result["changed"])
        self.assertEqual(self.revision(), rev + 1)
        # Reload preserves the proposal; the human text and comment stay.
        self.click(app, "Reload saved work")
        view = app.session_state.remote_view
        self.assertEqual(view.record("k1").llm_suggestion, "Le chat (LLM).")
        self.assertEqual(view.record("k1").user_final, "Le chat humain.")
        self.assertEqual(view.record("k1").user_comment, "tone note")

    def test_manual_save_after_proposal_only_persists_human_text(self):
        # Putting a proposal into "Your text" is an explicit edit that creates
        # an unsaved draft; only Save persists it. The model proposal
        # (llm_suggestion) is kept through the save, so the four data kinds stay
        # distinct.
        self.install()
        app = self.start()
        rev = self.revision()
        # Store the proposal only - user_final stays empty (no automatic accept).
        self.fake.workspace.record_suggestions(
            DOC_A, rev, {"k1": {"text": "Le chat (LLM)."}}, prefill=False)
        self.click(app, "Reload saved work")
        view = app.session_state.remote_view
        self.assertEqual(view.record("k1").llm_suggestion, "Le chat (LLM).")
        self.assertEqual(view.record("k1").user_final, "")

        # The user edits "Your text": an unsaved draft, not yet persisted.
        self.edit_text(app, 0, "Le chat humain.")
        self.assertEqual(self.drafts(app)["k1"]["text"], "Le chat humain.")
        self.assertTrue(self.save_enabled(app))
        self.assertEqual(app.session_state.remote_view.record("k1").user_final, "")
        self.assertEqual(self.session.patches(), [])

        # Save persists the human text; the proposal is kept.
        self.click(app, "Save")
        self.assertEqual(self.session.patches()[-1][2]["segments"],
                         {"k1": {"text": "Le chat humain."}})
        self.assertEqual(self.stored("k1")["user_final"], "Le chat humain.")
        self.assertTrue(self.stored("k1")["edited_by_human"])
        self.assertEqual(self.stored("k1")["llm_suggestion"], "Le chat (LLM).")
        self.assertEqual(self.drafts(app), {})
        self.assertFalse(self.save_enabled(app))

    def test_discard_before_save_keeps_proposal_restores_human(self):
        # A proposal stored on a row the human already saved does not move the
        # human text. A manual edit creates a draft; Discard (before Save)
        # restores the prior saved human text and keeps the model proposal.
        self.install()
        app = self.start()
        # The human already saved k1.
        self.other_writer({"k1": {"text": "Le chat humain."}})
        rev = self.revision()
        # Process stores a new proposal only - the human text is left untouched.
        self.fake.workspace.record_suggestions(
            DOC_A, rev, {"k1": {"text": "Le chat (LLM)."}}, prefill=False)
        self.click(app, "Reload saved work")
        view = app.session_state.remote_view
        self.assertEqual(view.record("k1").llm_suggestion, "Le chat (LLM).")
        self.assertEqual(view.record("k1").user_final, "Le chat humain.")
        # A manual edit creates a draft; the saved human text and the proposal
        # are both still on the server (the draft is session-only).
        self.edit_text(app, 0, "Le chat (LLM).")
        self.assertEqual(self.drafts(app)["k1"]["text"], "Le chat (LLM).")
        self.assertEqual(self.stored("k1")["user_final"], "Le chat humain.")
        self.assertEqual(self.stored("k1")["llm_suggestion"], "Le chat (LLM).")
        # Discard (before Save) drops the draft; no write happens, so the prior
        # saved human text and the proposal both remain.
        self.click(app, "Discard")
        self.assertEqual(self.session.patches(), [])
        self.assertEqual(self.drafts(app), {})
        self.assertEqual(self.stored("k1")["user_final"], "Le chat humain.")
        self.assertEqual(self.stored("k1")["llm_suggestion"], "Le chat (LLM).")

    def test_empty_string_edit_is_saved_as_empty(self):
        self.install()
        app = self.start()
        self.other_writer({"k1": {"text": "Human text."}})
        self.click(app, "Reload saved work")
        self.assertEqual(app.text_area[0].value, "Human text.")

        self.edit_text(app, 0, "")
        self.assertEqual(self.drafts(app)["k1"]["text"], "")
        self.click(app, "Save")
        self.assertEqual(self.session.patches()[-1][2]["segments"], {"k1": {"text": ""}})
        self.assertEqual(self.stored("k1")["user_final"], "")
        record = app.session_state.remote_view.record("k1")
        self.assertEqual(record.user_final, "")          # not the original target
        self.assertEqual(record.mt_target, "Le chat.")
        self.assertEqual(app.text_area[0].value, "")
        self.assertEqual(self.drafts(app), {})

    def test_comment_only_edit_sends_only_the_comment(self):
        self.install()
        app = self.start()
        rev = self.revision()
        self.edit_comment(app, 1, "check term")
        self.click(app, "Save")
        self.assertEqual(self.session.patches()[-1][2],
                         {"expected_revision": rev,
                          "segments": {"k2": {"comment": "check term"}}})
        self.assertEqual(self.stored("k2")["user_comment"], "check term")
        self.assertFalse(self.stored("k2")["edited_by_human"])

    def test_no_change_means_no_mutation(self):
        self.install()
        app = self.start()
        rev = self.revision()
        # Typing the saved value back is not an edit: no draft, no Save, no PATCH.
        self.edit_text(app, 0, "changed")
        self.edit_text(app, 0, app.session_state.remote_view.record("k1").user_final)
        self.assertEqual(self.drafts(app), {})
        self.assertFalse(self.save_enabled(app))
        # Calling Save with nothing to save makes no request either.
        import remote_workflow
        with mock.patch.object(remote_workflow.st, "session_state", app.session_state):
            self.assertTrue(remote_workflow.save_remote_drafts())
        self.assertEqual(self.session.patches(), [])
        self.assertEqual(self.revision(), rev)

    # --------------------------------------------------- navigation and reruns

    def test_drafts_survive_reruns_and_page_navigation(self):
        self.install()
        app = self.start()
        app.session_state.remote_page_size = 1  # one row per page
        self._run(app)
        self.assertEqual(len(app.text_area), 1)
        self.edit_text(app, 0, "draft one")
        # Move to page 2 and back; the row's widget state is gone meanwhile.
        app.radio[0].set_value(1)
        self._run(app)
        self.assertEqual(app.text_area[0].value,
                         app.session_state.remote_view.record("k2").user_final)
        self._run(app)
        self.assertEqual(self.drafts(app)["k1"]["text"], "draft one")
        # Save from page 2: the draft on the unrendered row is saved too.
        self.click(app, "Save")
        self.assertEqual(self.stored("k1")["user_final"], "draft one")
        app.radio[0].set_value(0)
        self._run(app)
        self.assertEqual(app.text_area[0].value, "draft one")

    def test_drafts_survive_batch_navigation(self):
        # The real service fixes the batch size at 100, so batch navigation uses
        # the in-file fake workspace (batch_size=2), whose save mirror follows
        # the service's revision/no-op/unknown-key rules.
        server = {DOC_A: base.snapshot(
            DOC_A, ORIG_A, "target", 1, order=["k1", "k2", "k3"],
            segments={k: base.ws_record(k, user_final="saved %s" % k)
                      for k in ("k1", "k2", "k3")})}
        self.document = base._FakeDocument(
            {"document_id": DOC_A, "original_id": ORIG_A}, _segments_a())
        workspace = base._FakeWorkspace({}, {}, server=server)
        self.fake = base._FakeClients(self.document, workspace, base._FakeProcessing())
        patch = mock.patch("harness_remote.build_clients",
                           side_effect=lambda config: self.fake)
        patch.start()
        self.addCleanup(patch.stop)
        app = self.start()
        self.edit_text(app, 0, "draft k1")
        self.click(app, "Next")
        self.assertEqual(app.session_state.remote_batch, 1)
        self.assertEqual(app.text_area[0].value, "saved k3")
        self.edit_text(app, 0, "draft k3")
        self.click(app, "Previous")
        self.assertEqual(app.text_area[0].value, "draft k1")
        self.click(app, "Save")
        self.assertEqual(len(workspace.save_calls), 1)
        self.assertEqual(workspace.save_calls[0]["expected_revision"], 1)
        self.assertEqual(workspace.save_calls[0]["updates"],
                         {"k1": {"text": "draft k1"}, "k3": {"text": "draft k3"}})
        self.assertEqual(server[DOC_A]["revision"], 2)
        self.assertEqual(self.drafts(app), {})

    # ---------------------------------------------- document isolation / Open

    def test_dirty_open_cancel_keeps_drafts_and_does_not_open(self):
        self.install(uploads=[{"document_id": DOC_A, "original_id": ORIG_A},
                              {"document_id": DOC_B, "original_id": ORIG_B}])
        app = self.start()
        self.edit_text(app, 0, "unsaved A")
        self.upload(app, "b.mqxliff", b"<b/>")
        self.assertEqual(len(self.document.upload_calls), 1)  # B not opened
        self.assertEqual(app.session_state.remote_pending["action"], "open")
        self.assertIn("Save and open", self.labels(app))
        self.click(app, "Cancel")
        self.assertIsNone(app.session_state.remote_pending)
        self._run(app)
        self._run(app)
        # Not re-offered on rerun, still on A with its draft.
        self.assertIsNone(app.session_state.remote_pending)
        self.assertEqual(len(self.document.upload_calls), 1)
        self.assertEqual(app.session_state.remote_view.document_id, DOC_A)
        self.assertEqual(self.drafts(app)["k1"]["text"], "unsaved A")
        self.assertEqual(app.text_area[0].value, "unsaved A")

    def test_dirty_open_discard_opens_b_without_leaking_drafts(self):
        self.install(uploads=[{"document_id": DOC_A, "original_id": ORIG_A},
                              {"document_id": DOC_B, "original_id": ORIG_B}])
        app = self.start()
        self.edit_text(app, 0, "unsaved A")
        self.upload(app, "b.mqxliff", b"<b/>")
        self.click(app, "Discard and open")
        view = app.session_state.remote_view
        self.assertEqual(view.document_id, DOC_B)
        self.assertEqual(app.session_state.remote_drafts, {})
        # B's k1 shares A's segment key; its cell shows B's saved text, not A's draft.
        self.assertEqual(app.text_area[0].value, view.record("k1").user_final)
        self.assertNotEqual(app.text_area[0].value, "unsaved A")
        self.assertEqual(self.session.patches(), [])
        self.assertFalse(self.save_enabled(app))

    def test_dirty_open_save_and_open(self):
        self.install(uploads=[{"document_id": DOC_A, "original_id": ORIG_A},
                              {"document_id": DOC_B, "original_id": ORIG_B}])
        app = self.start()
        self.edit_text(app, 0, "keep A")
        self.upload(app, "b.mqxliff", b"<b/>")
        self.click(app, "Save and open")
        self.assertEqual(self.stored("k1", DOC_A)["user_final"], "keep A")
        self.assertEqual(app.session_state.remote_view.document_id, DOC_B)
        self.assertEqual(app.session_state.remote_drafts, {})

    def test_failed_save_blocks_pending_open(self):
        self.install(uploads=[{"document_id": DOC_A, "original_id": ORIG_A},
                              {"document_id": DOC_B, "original_id": ORIG_B}])
        app = self.start()
        self.edit_text(app, 0, "mine")
        self.other_writer({"k1": {"text": "theirs"}})  # makes the save stale
        self.upload(app, "b.mqxliff", b"<b/>")
        self.click(app, "Save and open")
        # 409: nothing written by us, B not opened, question still open.
        self.assertEqual(self.stored("k1")["user_final"], "theirs")
        self.assertEqual(len(self.document.upload_calls), 1)
        self.assertEqual(app.session_state.remote_view.document_id, DOC_A)
        self.assertEqual(app.session_state.remote_pending["action"], "open")
        self.assertEqual(self.drafts(app)["k1"]["text"], "mine")

    # --------------------------------------------------------- dirty Reload

    def test_dirty_reload_asks_and_cancel_reads_nothing(self):
        self.install()
        app = self.start()
        self.edit_text(app, 0, "draft")
        gets = len([c for c in self.session.calls if c[0] == "GET"])
        self.click(app, "Reload saved work")
        self.assertEqual(app.session_state.remote_pending, {"action": "reload"})
        self.assertEqual(len([c for c in self.session.calls if c[0] == "GET"]), gets)
        self.click(app, "Cancel")
        self.assertIsNone(app.session_state.remote_pending)
        self.assertEqual(self.drafts(app)["k1"]["text"], "draft")

    def test_dirty_reload_keep_and_discard(self):
        self.install()
        app = self.start()
        self.edit_text(app, 0, "draft")
        self.other_writer({"k2": {"text": "elsewhere"}})
        self.click(app, "Reload saved work")
        self.click(app, "Reload, keep my changes")
        # The other row follows the server; the draft is kept and not in conflict.
        self.assertEqual(app.session_state.remote_view.record("k2").user_final, "elsewhere")
        self.assertEqual(app.text_area[1].value, "elsewhere")
        self.assertEqual(app.text_area[0].value, "draft")
        self.assertEqual(self.drafts(app)["k1"]["text"], "draft")
        self.click(app, "Reload saved work")
        self.click(app, "Discard and reload")
        self.assertEqual(self.drafts(app), {})
        self.assertEqual(app.text_area[0].value,
                         app.session_state.remote_view.record("k1").user_final)
        self.assertEqual(self.session.patches(), [])

    # ------------------------------------------------------ conflicts (409)

    def test_stale_revision_marks_conflict_and_never_replays(self):
        self.install()
        app = self.start()
        self.edit_text(app, 0, "mine")
        self.other_writer({"k1": {"text": "theirs"}})
        rev_after_other = self.revision()
        self.click(app, "Save")
        # One PATCH, rejected; the server keeps the other writer's text.
        self.assertEqual(len(self.session.patches()), 1)
        self.assertEqual(self.stored("k1")["user_final"], "theirs")
        self.assertEqual(self.revision(), rev_after_other)
        self.assertEqual(self.drafts(app)["k1"]["text"], "mine")
        # The view re-read (read only) and the row is an explicit conflict.
        self.assertEqual(app.session_state.remote_view.revision, rev_after_other)
        self.assertIn("Keep mine", self.labels(app))
        self.assertTrue(any(k == "error" and "Not saved" in m
                            for k, m in self.messages(app)))
        self.assertFalse(self.save_enabled(app))
        # Save stays refused while unresolved - no second request.
        import remote_workflow
        with mock.patch.object(remote_workflow.st, "session_state", app.session_state):
            self.assertFalse(remote_workflow.save_remote_drafts())
        self.assertEqual(len(self.session.patches()), 1)

        # Explicit Keep mine, then Save: now a deliberate overwrite at the
        # current revision.
        self.click(app, "Keep mine")
        self.click(app, "Save")
        self.assertEqual(self.session.patches()[-1][2]["expected_revision"],
                         rev_after_other)
        self.assertEqual(self.stored("k1")["user_final"], "mine")
        self.assertEqual(self.drafts(app), {})

    def test_conflict_use_server_drops_my_edit(self):
        self.install()
        app = self.start()
        self.edit_text(app, 0, "mine")
        self.other_writer({"k1": {"text": "theirs"}})
        self.click(app, "Save")
        self.click(app, "Use server")
        self.assertEqual(self.drafts(app), {})
        self.assertEqual(app.text_area[0].value, "theirs")
        self.assertEqual(len(self.session.patches()), 1)

    def test_stale_revision_on_other_row_keeps_concurrent_change(self):
        self.install()
        app = self.start()
        self.edit_text(app, 0, "mine")
        self.other_writer({"k2": {"text": "theirs", "comment": "c"}})
        self.click(app, "Save")      # 409, rebased: no conflict on k1
        self.assertNotIn("Keep mine", self.labels(app))
        self.click(app, "Save")      # explicit second Save
        self.assertEqual(self.stored("k1")["user_final"], "mine")
        self.assertEqual(self.stored("k2")["user_final"], "theirs")
        self.assertEqual(self.stored("k2")["user_comment"], "c")
        self.assertEqual(self.session.patches()[-1][2]["segments"],
                         {"k1": {"text": "mine"}})

    # ---------------------------------------------------- ambiguous failures

    def test_lost_response_after_stored_save_is_reconciled(self):
        self.install()
        app = self.start()
        rev = self.revision()
        self.edit_text(app, 0, "mine")
        self.session.fail = lambda m, p: "after" if m == "PATCH" else None
        self.click(app, "Save")
        self.session.fail = None
        self.assertEqual(self.revision(), rev + 1)  # it was stored
        self.assertEqual(self.drafts(app), {})      # confirmed by the re-read
        self.assertEqual(app.session_state.remote_view.revision, rev + 1)
        self.assertTrue(any(k == "success" and "re-read" in m
                            for k, m in self.messages(app)))
        self.assertEqual(len(self.session.patches()), 1)

    def test_failed_connection_keeps_drafts_and_claims_nothing(self):
        self.install()
        app = self.start()
        rev = self.revision()
        self.edit_text(app, 0, "mine")
        self.session.fail = lambda m, p: "before" if m == "PATCH" else None
        self.click(app, "Save")
        self.session.fail = None
        self.assertEqual(self.revision(), rev)
        self.assertEqual(self.drafts(app)["k1"]["text"], "mine")
        msgs = self.messages(app)
        self.assertFalse(any(k == "success" for k, _ in msgs))
        self.assertTrue(any(k == "error" and "could not be confirmed" in m
                            for k, m in msgs))
        # A retry after the outage saves normally.
        self.click(app, "Save")
        self.assertEqual(self.stored("k1")["user_final"], "mine")

    def test_unconfirmable_outcome_keeps_drafts_and_retry_cannot_overwrite(self):
        self.install()
        app = self.start()
        rev = self.revision()
        self.edit_text(app, 0, "mine")
        # The PATCH is stored but its response lost, and the re-read fails.
        self.session.fail = lambda m, p: "after" if m == "PATCH" else "before"
        self.click(app, "Save")
        self.assertEqual(self.revision(), rev + 1)
        self.assertEqual(self.drafts(app)["k1"]["text"], "mine")
        self.assertEqual(app.session_state.remote_view.revision, rev)
        msgs = self.messages(app)
        self.assertFalse(any(k == "success" for k, _ in msgs))
        self.assertTrue(any("could not be re-read" in m for _, m in msgs))
        # Back online: a repeated Save carries the old revision and is refused,
        # then the re-read shows the change is stored and the draft is clean.
        self.session.fail = None
        self.click(app, "Save")
        self.assertEqual(self.session.patches()[-1][2]["expected_revision"], rev)
        self.assertEqual(self.revision(), rev + 1)
        self.assertEqual(self.drafts(app), {})


    # ------------------------------------------- fix pass after browser testing

    def _conflict_on(self, app, index, key, mine="mine", theirs="theirs"):
        """Edit row ``index`` (segment ``key``), let another writer save the same
        row, then Save: a 409 that leaves ``key`` in conflict."""
        self.edit_text(app, index, mine)
        self.other_writer({key: {"text": theirs}})
        self.click(app, "Save")
        self.assertEqual(len(self.session.patches()), 1)
        self.assertIn("Keep mine", [b.label for b in app.button])

    def test_conflict_on_another_page_is_listed_and_reachable(self):
        self.install()
        app = self.start()
        app.session_state.remote_page_size = 1
        self._run(app)
        app.radio[0].set_value(2)  # page of k3
        self._run(app)
        self._conflict_on(app, 0, "k3")
        # Go back to page 1: the conflict panel still lists k3 with its values.
        app.session_state.remote_page = 0
        self._run(app)
        self.assertEqual(app.text_area[0].value,
                         app.session_state.remote_view.record("k1").user_final)
        panel = " ".join(c.value for c in app.caption)
        self.assertIn("your draft: mine", panel)
        self.assertIn("now saved: theirs", panel)
        self.assertIn("Keep mine", [b.label for b in app.button])
        self.click(app, "Show row")
        self.assertEqual(app.session_state.remote_page, 2)
        self.assertEqual(app.text_area[0].value, "mine")

    def test_keep_mine_does_not_write_then_save_overwrites_deliberately(self):
        self.install()
        app = self.start()
        self._conflict_on(app, 0, "k1")
        rev = self.revision()
        self.assertFalse(self.save_enabled(app))
        self.click(app, "Keep mine")
        # The choice alone writes nothing.
        self.assertEqual(len(self.session.patches()), 1)
        self.assertEqual(self.stored("k1")["user_final"], "theirs")
        self.assertEqual(self.revision(), rev)
        self.assertNotIn("Keep mine", [b.label for b in app.button])
        self.assertTrue(self.save_enabled(app))
        self.click(app, "Save")
        self.assertEqual(self.session.patches()[-1][2],
                         {"expected_revision": rev, "segments": {"k1": {"text": "mine"}}})
        self.assertEqual(self.stored("k1")["user_final"], "mine")
        self.assertEqual(self.drafts(app), {})

    def test_use_server_does_not_write_and_keeps_other_drafts(self):
        self.install()
        app = self.start()
        self.edit_text(app, 1, "other draft")
        self._conflict_on(app, 0, "k1")
        rev = self.revision()
        self.click(app, "Use server")
        self.assertEqual(len(self.session.patches()), 1)
        self.assertEqual(self.revision(), rev)
        self.assertNotIn("k1", self.drafts(app))
        self.assertEqual(self.drafts(app)["k2"]["text"], "other draft")
        self.assertEqual(app.text_area[0].value, "theirs")
        self.assertEqual(app.text_area[1].value, "other draft")

    def test_unresolved_conflict_embargoes_every_write_path(self):
        import remote_workflow
        self.install(uploads=[{"document_id": DOC_A, "original_id": ORIG_A},
                              {"document_id": DOC_B, "original_id": ORIG_B}])
        app = self.start()
        self.edit_text(app, 1, "pending k2")
        self._conflict_on(app, 0, "k1")
        patches = len(self.session.patches())
        self.assertFalse(self.save_enabled(app))

        # Dirty Reload: Save-and-reload is disabled; even forced, it neither
        # writes nor reloads, and the question stays open.
        self.click(app, "Reload saved work")
        self.assertTrue([b for b in app.button if b.label == "Save and reload"][0].disabled)
        with mock.patch.object(remote_workflow.st, "session_state", app.session_state):
            remote_workflow.resolve_pending("save")
            self.assertFalse(remote_workflow.save_remote_drafts())
        self.assertEqual(len(self.session.patches()), patches)
        self.assertEqual(app.session_state.remote_pending, {"action": "reload"})

        # Reload keeping changes keeps the conflict and both drafts visible.
        self._run(app)
        self.click(app, "Reload, keep my changes")
        self.assertEqual(remote_workflow_conflicts(app), ["k1"])
        self.assertEqual(self.drafts(app)["k2"]["text"], "pending k2")
        self.assertIn("Keep mine", [b.label for b in app.button])

        # Dirty Open: Save-and-open is disabled; forced, nothing is written or
        # uploaded and A stays open.
        self.upload(app, "b.mqxliff", b"<b/>")
        self.assertTrue([b for b in app.button if b.label == "Save and open"][0].disabled)
        with mock.patch.object(remote_workflow.st, "session_state", app.session_state):
            remote_workflow.resolve_pending("save")
        self.assertEqual(len(self.session.patches()), patches)
        self.assertEqual(len(self.document.upload_calls), 1)
        self.assertEqual(app.session_state.remote_view.document_id, DOC_A)

    def test_save_from_sidebar_includes_the_last_field_edit(self):
        self.install()
        app = self.start()
        self.edit_text(app, 0, "first")
        # Type into another field and click Save in the same interaction, with
        # no rerun in between (the browser sends both together).
        app.text_area[1].input("typed then saved")
        self.sidebar_button(app, "Save").click()
        self._run(app)
        self.assertEqual(self.session.patches()[-1][2]["segments"],
                         {"k1": {"text": "first"}, "k2": {"text": "typed then saved"}})
        self.assertEqual(self.drafts(app), {})

    def test_cancel_keeps_open_document_identity_and_clears_selection(self):
        self.install(uploads=[{"document_id": DOC_A, "original_id": ORIG_A},
                              {"document_id": DOC_B, "original_id": ORIG_B}])
        app = self.start("a.mqxliff", b"<a/>")
        self.edit_text(app, 0, "unsaved A")
        self.upload(app, "b.mqxliff", b"<b/>")
        sidebar_text = " ".join(c.value for c in app.sidebar.caption)
        self.assertIn("Selected, not open (waiting", sidebar_text)
        self.click(app, "Cancel")
        self._run(app)
        # The uploader is empty; the open document is still A, with its draft.
        self.assertEqual(len(app.file_uploader), 1)
        self.assertFalse(app.file_uploader[0].value)
        self.assertIn("a.mqxliff", " ".join(m.value for m in app.sidebar.markdown))
        self.assertNotIn("b.mqxliff", " ".join(
            [m.value for m in app.sidebar.markdown] + [c.value for c in app.sidebar.caption]))
        self.assertEqual(app.session_state.remote_open_file["name"], "a.mqxliff")
        self.assertEqual(app.session_state.remote_view.document_id, DOC_A)
        self.assertEqual(self.drafts(app)["k1"]["text"], "unsaved A")
        self.assertIsNone(app.session_state.remote_pending)

        # Reselecting the file version that is open (same name and bytes) is a
        # no-op: no upload, no bind, no switch prompt, draft untouched.
        binds = len([c for c in self.session.calls if c[0] == "POST"])
        self.upload(app, "a.mqxliff", b"<a/>")
        self.assertEqual(len(self.document.upload_calls), 1)
        self.assertEqual(len([c for c in self.session.calls if c[0] == "POST"]), binds)
        self.assertIsNone(app.session_state.remote_pending)
        self.assertEqual(self.drafts(app)["k1"]["text"], "unsaved A")
        # Same name, different bytes is a different file: it is asked about.
        self.upload(app, "a.mqxliff", b"<a version='2'/>")
        self.assertEqual(app.session_state.remote_pending["action"], "open")
        self.assertEqual(len(self.document.upload_calls), 1)

    def test_discard_and_reload_drops_only_the_draft(self):
        # The user saw "revision 1" once after Discard and reload. Reload is a
        # read; it must neither roll back nor bind/patch the workspace.
        self.install()
        app = self.start()
        self.edit_text(app, 0, "persisted")
        self.click(app, "Save")
        rev = self.revision()
        self.assertGreater(rev, 1)
        self.edit_text(app, 0, "throwaway")
        self.edit_comment(app, 1, "throwaway comment")
        calls = len(self.session.calls)
        self.click(app, "Reload saved work")
        self.click(app, "Discard and reload")
        new_calls = self.session.calls[calls:]
        self.assertTrue(new_calls)
        self.assertEqual({c[0] for c in new_calls}, {"GET"})
        self.assertEqual(self.revision(), rev)
        self.assertEqual(self.stored("k1")["user_final"], "persisted")
        self.assertEqual(self.drafts(app), {})
        view = app.session_state.remote_view
        self.assertEqual(view.revision, rev)
        self.assertEqual(app.text_area[0].value, "persisted")
        self.assertEqual(app.text_input[1].value,
                         view.record("k2").user_comment)
        self.assertTrue(any("revision %d" % rev in m for _, m in self.messages(app)))
        self.assertIn("Revision %d" % rev,
                      " ".join(c.value for c in app.sidebar.caption))


    # ------------------------- two-tab partial conflict (live draft-loss report)
    #
    # Tab A saves segment "10" (k3) at the shared revision; stale tab B edits
    # "9" (k2) and "10", then clicks the sidebar Save. AppTest's ``.value`` reads
    # backend session state, so these tests also check what the *browser* is
    # told to show: a cell whose widget id is unchanged keeps its own (typed)
    # value; a re-keyed cell shows proto.value only when set_value is set.

    def browser_shows(self, app, index, ids_before):
        ta = app.text_area[index]
        if ta.proto.id in ids_before:
            return ta.value
        return ta.proto.value if ta.proto.set_value else ta.proto.default

    def _two_tab_409(self, last_edited):
        self.install()
        app = self.start()
        rev = self.revision()
        self.other_writer({"k3": {"text": "A's 10"}})          # tab A
        first, last = (1, 2) if last_edited == "k3" else (2, 1)
        values = {1: "B's 9", 2: "B's 10"}
        self.edit_text(app, first, values[first])             # blur: one rerun
        # Boundary 1 - widget-to-draft sync: the blurred edit is a draft.
        self.assertEqual(self.drafts(app)[{1: "k2", 2: "k3"}[first]]["text"],
                         values[first])
        # The last field is typed and Save is clicked in the same interaction.
        app.text_area[last].input(values[last])
        ids = {ta.proto.id for ta in app.text_area}
        self.sidebar_button(app, "Save").click()
        self._run(app)
        # Boundary 2 - the rejected PATCH carried both edits; nothing written.
        self.assertEqual(self.session.patches()[-1][2],
                         {"expected_revision": rev,
                          "segments": {"k2": {"text": "B's 9"},
                                       "k3": {"text": "B's 10"}}})
        self.assertEqual(len(self.session.patches()), 1)
        self.assertEqual(self.revision(), rev + 1)             # only tab A's save
        self.assertEqual(self.stored("k2")["user_final"], "")
        self.assertEqual(self.stored("k3")["user_final"], "A's 10")
        # Boundary 3 - both drafts kept, only k3 conflicted, both still shown,
        # and neither kept cell was remounted.
        self.assertEqual({k: d["text"] for k, d in self.drafts(app).items()},
                         {"k2": "B's 9", "k3": "B's 10"})
        self.assertEqual(remote_workflow_conflicts(app), ["k3"])
        self.assertIn(app.text_area[1].proto.id, ids)
        self.assertIn(app.text_area[2].proto.id, ids)
        self.assertEqual(self.browser_shows(app, 1, ids), "B's 9")
        self.assertEqual(self.browser_shows(app, 2, ids), "B's 10")
        self.assertFalse(self.save_enabled(app))
        # A further interaction (the browser sends back what it shows) must not
        # turn the kept draft into a clean row.
        self._run(app)
        self.assertEqual(set(self.drafts(app)), {"k2", "k3"})
        return app, rev

    def test_two_tab_409_keeps_nonconflicting_draft_last_edit_10(self):
        self._two_tab_409("k3")

    def test_two_tab_409_keeps_nonconflicting_draft_last_edit_9(self):
        self._two_tab_409("k2")

    def test_two_tab_use_server_changes_only_10_then_save_persists_only_9(self):
        app, rev = self._two_tab_409("k3")
        ids = {ta.proto.id for ta in app.text_area}
        self.click(app, "Use server")
        self.assertEqual(len(self.session.patches()), 1)       # no write
        self.assertEqual({k: d["text"] for k, d in self.drafts(app).items()},
                         {"k2": "B's 9"})
        self.assertIn(app.text_area[1].proto.id, ids)          # 9 untouched
        self.assertEqual(self.browser_shows(app, 1, ids), "B's 9")
        self.assertNotIn(app.text_area[2].proto.id, ids)       # 10 re-seeded
        self.assertEqual(self.browser_shows(app, 2, ids), "A's 10")
        self.assertTrue(self.save_enabled(app))
        self._run(app)
        self.assertEqual(set(self.drafts(app)), {"k2"})
        self.click(app, "Save")
        self.assertEqual(self.session.patches()[-1][2],
                         {"expected_revision": rev + 1,
                          "segments": {"k2": {"text": "B's 9"}}})
        self.assertEqual(self.stored("k2")["user_final"], "B's 9")
        self.assertEqual(self.stored("k3")["user_final"], "A's 10")
        self.assertEqual(self.drafts(app), {})

    def test_two_tab_keep_mine_writes_nothing_then_save_persists_both(self):
        app, rev = self._two_tab_409("k3")
        ids = {ta.proto.id for ta in app.text_area}
        self.click(app, "Keep mine")
        self.assertEqual(len(self.session.patches()), 1)       # no write
        self.assertEqual({k: d["text"] for k, d in self.drafts(app).items()},
                         {"k2": "B's 9", "k3": "B's 10"})
        self.assertEqual(self.browser_shows(app, 1, ids), "B's 9")
        self.assertEqual(self.browser_shows(app, 2, ids), "B's 10")
        self.assertTrue(self.save_enabled(app))
        self.click(app, "Save")
        self.assertEqual(self.session.patches()[-1][2],
                         {"expected_revision": rev + 1,
                          "segments": {"k2": {"text": "B's 9"},
                                       "k3": {"text": "B's 10"}}})
        self.assertEqual(self.stored("k2")["user_final"], "B's 9")
        self.assertEqual(self.stored("k3")["user_final"], "B's 10")
        self.assertEqual(self.drafts(app), {})


def remote_workflow_conflicts(app):
    import remote_workflow
    with mock.patch.object(remote_workflow.st, "session_state", app.session_state):
        return remote_workflow.conflicted_keys()


if __name__ == "__main__":
    unittest.main(verbosity=2)
