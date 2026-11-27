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
import shutil
import sys
import tempfile
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
import remote_workflow  # noqa: E402
from harness_remote.client import DocumentClient, ProcessingClient, WorkspaceClient  # noqa: E402
from harness_remote.view import RemoteWorkspaceView  # noqa: E402
from memoq_harness import mqxliff  # noqa: E402
from memoq_harness import llm  # noqa: E402
from memoq_harness import state as state_mod  # noqa: E402
from services.document_service.app import create_app as create_doc_app  # noqa: E402
from services.document_service.service import DocumentService  # noqa: E402
from services.document_service.storage import FileStorage  # noqa: E402
from services.workspace_service.app import create_app  # noqa: E402
from services.workspace_service.repository import WorkspaceRow  # noqa: E402
from services.workspace_service.service import (  # noqa: E402
    WorkspaceService, parse_patch_request, parse_suggestions_request,
    parse_apply_request,
)
from services.processing_service import config as proc_config, engines  # noqa: E402
from services.processing_service.app import create_app as create_proc_app  # noqa: E402
from services.processing_service.service import ProcessingService  # noqa: E402
from test_document_service import FakeRepository  # noqa: E402

DOC_A = "a" * 64
DOC_B = "b" * 64
ORIG_A = "1" * 64
ORIG_B = "2" * 64

# A small, controlled MQXLIFF the real document service can parse, upload and
# export. Units 1 and 2 are plain translatable rows (fr-FR); unit 3 is
# confirmed, so the segment filter drops it and it is never in the active
# order. The keys therefore come out "1#0" and "2#1".
SMALL_XML = (
    b'<?xml version="1.0" encoding="utf-8"?>\n'
    b'<xliff version="1.2" '
    b'xmlns="urn:oasis:names:tc:xliff:document:1.2" '
    b'xmlns:mq="http://www.memoq.com/memoq/xliff">\n'
    b'  <file original="sample.docx" source-language="en-US" '
    b'target-language="fr-FR" datatype="x-docx">\n'
    b'    <body>\n'
    b'      <trans-unit id="1" mq:status="NotStarted" mq:locked="false">\n'
    b'        <source>The cat.</source>\n'
    b'        <target>Le chat.</target>\n'
    b'      </trans-unit>\n'
    b'      <trans-unit id="2" mq:status="NotStarted" mq:locked="false">\n'
    b'        <source>The dog.</source>\n'
    b'        <target>Le chien.</target>\n'
    b'      </trans-unit>\n'
    b'      <trans-unit id="3" mq:confirmed="true" mq:locked="false">\n'
    b'        <source>The bird.</source>\n'
    b'        <target>L\'oiseau.</target>\n'
    b'      </trans-unit>\n'
    b'    </body>\n'
    b'  </file>\n'
    b'</xliff>\n'
)


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
        response = self.tc.request(method, path, json=json, files=files,
                                   params=params, headers=headers,
                                   follow_redirects=False)
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


class _RemoteEditMixin:
    """Shared setup/teardown and helper methods for remote-edit tests.

    Mixed into every remote test class so the test methods are not duplicated
    by inheritance. Each concrete test class inherits from ``unittest.TestCase``
    and this mixin, not from another concrete test class.
    """

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

    def _real_document_client(self, raw, filename="sample.mqxliff"):
        """A real DocumentClient over a real document-service TestClient.

        Builds the production service (in-memory FakeRepository, temp file
        storage, schema 0001), uploads ``raw`` so its original sits in the
        store, and wraps the client in a fresh session that routes through the
        document app's TestClient. The caller swaps it in as
        ``self.fake.document`` so the app's segments/upload/export calls hit the
        real service contract instead of the fake.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        service = DocumentService(FakeRepository(),
                                  FileStorage(os.path.join(tmp, "store")),
                                  1024 * 1024, "0001")
        tc = TestClient(create_doc_app(service), raise_server_exceptions=False)
        self.addCleanup(tc.close)
        response = tc.post("/documents",
                           files={"file": (filename, raw, "application/xml")})
        self.assertEqual(response.status_code, 201, response.text)
        return DocumentClient("http://doc.test", http_timeout=(1.0, 1.0),
                              session=_ServiceSession(tc))

    def _real_processing_client(self):
        """A real ProcessingClient over the real processing-service TestClient.

        Builds the production service (in-memory settings; the stub engine needs
        no SDK or network) and wraps the client in a session that routes through
        the processing app's TestClient, so the app's Process call builds the
        prompt through the real service and calls the stub engine - no model and
        no socket. The caller swaps it in as ``self.fake.processing``.
        """
        settings = proc_config.load_settings({})
        service = ProcessingService(settings)
        tc = TestClient(create_proc_app(service), raise_server_exceptions=False)
        self.addCleanup(tc.close)
        return ProcessingClient("http://proc.test", http_timeout=(1.0, 1.0),
                                session=_ServiceSession(tc))

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

    def input_text_area(self, app, label, value):
        """Type into a text area found by its label, across main and sidebar."""
        areas = list(app.text_area) + list(app.sidebar.text_area)
        area = next(t for t in areas if t.label == label)
        area.input(value)
        self._run(app)
        return area

    def your_text_areas(self, app):
        """The per-row 'Your text' areas only.

        The main area also holds the sidebar's Editorial controls (Hard Rules /
        Vibe / Batch Directive), which this Streamlit version lists in
        ``app.text_area`` too; the per-row areas are the only ones keyed
        ``remote_edit_``, so count by key rather than the raw length.
        """
        return [ta for ta in app.text_area
                if ta.key and ta.key.startswith("remote_edit_")]

    def edit_row_text(self, app, row_index, value):
        """Edit the Nth visible 'Your text' cell, by per-row order.

        ``edit_text`` indexes the raw ``app.text_area`` list, which in this
        Streamlit version also carries the sidebar's editorial controls; the
        per-row cells are the ``remote_edit_``-keyed ones, so address them by
        order over the filtered list instead.
        """
        area = self.your_text_areas(app)[row_index]
        area.input(value)
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


class RemoteEditTestCase(_RemoteEditMixin, unittest.TestCase):

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

    def test_apply_proposal_creates_unsaved_draft_then_save_persists(self):
        # Apply is the explicit acceptance action for a proposal: it puts the
        # model's llm_suggestion into "Your text" as an unsaved draft. It never
        # writes to the saved workspace and never sets a saved human text (no
        # automatic accept); only a later explicit Save persists the proposal.
        self.install()
        app = self.start()
        rev = self.revision()
        # Store the proposal only - user_final stays empty (no auto-accept).
        self.fake.workspace.record_suggestions(
            DOC_A, rev, {"k1": {"text": "Le chat (LLM)."}}, prefill=False)
        self.click(app, "Reload saved work")
        view = app.session_state.remote_view
        self.assertEqual(view.record("k1").llm_suggestion, "Le chat (LLM).")
        self.assertEqual(view.record("k1").user_final, "")
        # Only the row with a proposal shows the button, enabled because the
        # proposal is not yet the cell's text; nothing has been written.
        self.assertEqual(self.labels(app).count("Apply proposal"), 1)
        self.assertFalse([b for b in app.button
                          if b.label == "Apply proposal"][0].disabled)
        self.assertEqual(self.session.patches(), [])

        self.click(app, "Apply proposal")
        # A draft only: nothing was written, the saved user_final is still empty
        # (the proposal is not auto-accepted) and the proposal itself is kept.
        self.assertEqual(self.session.patches(), [])
        self.assertEqual(self.drafts(app)["k1"]["text"], "Le chat (LLM).")
        self.assertEqual(self.drafts(app)["k1"]["comment"], "")
        self.assertEqual(app.session_state.remote_view.record("k1").user_final, "")
        self.assertEqual(app.session_state.remote_view.record("k1").llm_suggestion,
                         "Le chat (LLM).")
        self.assertEqual(app.text_area[0].value, "Le chat (LLM).")
        # The applied text now equals the proposal, so a repeat click is a
        # no-op: the button is disabled, but Save is offered.
        self.assertTrue([b for b in app.button
                         if b.label == "Apply proposal"][0].disabled)
        self.assertTrue(self.save_enabled(app))

        # Save persists the applied proposal as the human text; the proposal and
        # the four data kinds stay distinct.
        self.click(app, "Save")
        self.assertEqual(self.session.patches()[-1][2]["segments"],
                         {"k1": {"text": "Le chat (LLM)."}})
        # The applied text equals the proposal, so it is not counted as a human
        # edit (a verbatim accept is not the operator's own work) and stays
        # unmarked; the proposal itself is kept as a distinct field.
        self.assertEqual(self.stored("k1")["user_final"], "Le chat (LLM).")
        self.assertFalse(self.stored("k1")["edited_by_human"])
        self.assertEqual(self.stored("k1")["llm_suggestion"], "Le chat (LLM).")
        self.assertEqual(self.drafts(app), {})
        self.assertFalse(self.save_enabled(app))

    def test_apply_proposal_preserves_held_comment(self):
        # Applying a proposal keeps the comment the cell currently shows (a held
        # draft when one is), and a later Save persists both the applied text
        # and the held comment.
        self.install()
        app = self.start()
        rev = self.revision()
        self.fake.workspace.record_suggestions(
            DOC_A, rev, {"k1": {"text": "Le chat (LLM)."}}, prefill=False)
        self.click(app, "Reload saved work")
        # Hold a comment before applying; apply must keep it.
        self.edit_comment(app, 0, "tone")
        self.assertEqual(self.drafts(app)["k1"]["comment"], "tone")
        self.click(app, "Apply proposal")
        self.assertEqual(self.drafts(app)["k1"]["text"], "Le chat (LLM).")
        self.assertEqual(self.drafts(app)["k1"]["comment"], "tone")
        self.assertEqual(self.session.patches(), [])
        self.click(app, "Save")
        self.assertEqual(self.session.patches()[-1][2]["segments"],
                         {"k1": {"text": "Le chat (LLM).", "comment": "tone"}})
        self.assertEqual(self.stored("k1")["user_final"], "Le chat (LLM).")
        self.assertEqual(self.stored("k1")["user_comment"], "tone")
        self.assertEqual(self.stored("k1")["llm_suggestion"], "Le chat (LLM).")
        self.assertEqual(self.drafts(app), {})

    def test_apply_proposal_noop_when_already_shown(self):
        # When a proposal already equals the cell's text (the case after a
        # prefilled proposal), Apply is a no-op: the button is disabled, no
        # draft is created and nothing is written.
        self.install()
        app = self.start()
        rev = self.revision()
        # Prefill: the model proposal becomes user_final too, so the cell
        # already shows the proposal.
        self.fake.workspace.record_suggestions(
            DOC_A, rev, {"k1": {"text": "Le chat (LLM)."}})
        self.click(app, "Reload saved work")
        view = app.session_state.remote_view
        self.assertEqual(view.record("k1").llm_suggestion, "Le chat (LLM).")
        self.assertEqual(view.record("k1").user_final, "Le chat (LLM).")
        # The only "Apply proposal" button (k1) is disabled; k2 and k3 have no
        # proposal, so no button for them.
        self.assertEqual(self.labels(app).count("Apply proposal"), 1)
        self.assertTrue([b for b in app.button
                         if b.label == "Apply proposal"][0].disabled)
        self.assertEqual(self.drafts(app), {})
        self.assertEqual(self.session.patches(), [])

    def test_apply_proposal_button_absent_without_proposal(self):
        # A translatable row with no model proposal shows no "Apply proposal"
        # button at all.
        self.install()
        app = self.start()
        for label in self.labels(app):
            self.assertNotEqual(label, "Apply proposal")

    def test_apply_then_discard_restores_saved_and_keeps_proposal(self):
        # Applying a proposal to a row the human already saved, then Discarding:
        # the applied proposal is a session-only draft, so Discard restores the
        # saved human text and the model proposal stays on the server. Neither
        # Apply nor Discard writes (no PATCH, revision unchanged).
        self.install()
        app = self.start()
        self.other_writer({"k1": {"text": "Le chat humain."}})
        # Process stores a new proposal only - the human text is left untouched.
        self.fake.workspace.record_suggestions(
            DOC_A, self.revision(),
            {"k1": {"text": "Le chat (LLM)."}}, prefill=False)
        self.click(app, "Reload saved work")
        view = app.session_state.remote_view
        self.assertEqual(view.record("k1").user_final, "Le chat humain.")
        self.assertEqual(view.record("k1").llm_suggestion, "Le chat (LLM).")
        # Capture the revision after the (writing) record_suggestions and the
        # read-only reload; apply and discard must not advance it.
        rev = self.revision()
        # Apply: a draft only, no write; saved human text and proposal intact.
        self.click(app, "Apply proposal")
        self.assertEqual(self.drafts(app)["k1"]["text"], "Le chat (LLM).")
        self.assertEqual(self.session.patches(), [])
        self.assertEqual(self.revision(), rev)
        self.assertEqual(self.stored("k1")["user_final"], "Le chat humain.")
        self.assertEqual(self.stored("k1")["llm_suggestion"], "Le chat (LLM).")
        # Discard drops the applied draft; the saved human text and the proposal
        # both remain, and the cell shows the saved human text again.
        self.click(app, "Discard")
        self.assertEqual(self.session.patches(), [])
        self.assertEqual(self.revision(), rev)
        self.assertEqual(self.drafts(app), {})
        self.assertEqual(self.stored("k1")["user_final"], "Le chat humain.")
        self.assertEqual(self.stored("k1")["llm_suggestion"], "Le chat (LLM).")
        self.assertEqual(app.text_area[0].value, "Le chat humain.")

    def test_apply_proposal_keeps_neighbour_draft(self):
        # Applying a proposal to one row is a per-cell mutation: it must not
        # drop a neighbour's held draft. The apply re-keys the cells, and the
        # neighbour's draft is re-stored from its own re-keyed widget on the
        # next render (the same mechanism that keeps drafts across reruns).
        self.install()
        app = self.start()
        rev = self.revision()
        self.fake.workspace.record_suggestions(
            DOC_A, rev, {"k1": {"text": "Le chat (LLM)."}}, prefill=False)
        self.click(app, "Reload saved work")
        # Hold a draft on the neighbour k2 (a different row, a different kind).
        self.edit_text(app, 1, "Le chien humain.")
        self.assertEqual(self.drafts(app)["k2"]["text"], "Le chien humain.")
        self.assertNotIn("k1", self.drafts(app))
        # Apply the proposal to k1; k2's held draft must survive the re-key, and
        # nothing is written by the apply.
        self.click(app, "Apply proposal")
        self.assertEqual(self.drafts(app)["k1"]["text"], "Le chat (LLM).")
        self.assertEqual(self.drafts(app)["k2"]["text"], "Le chien humain.")
        self.assertEqual(self.session.patches(), [])

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

    def test_rerun_remote_row_survives_copy_save(self):
        # A per-row Rerun re-runs just one segment against the model and stores
        # its new proposal through record_suggestions with prefill off. The
        # proposal-only write never touches the human's saved text or comment, so
        # a Copy->Save that happened before the generation is not erased by it:
        # the new proposal replaces the old one in llm_suggestion while the saved
        # user_final and comment survive. The model call is one segment run as
        # operation="rerun", but it carries the current batch's index/count block
        # (like process_remote_row) so the re-run is staged with the current
        # batch's stage and framing.
        self.install()
        app = self.start()
        rev = self.revision()
        # Seed the first proposal as a proposal-only write (no auto-accept).
        self.fake.workspace.record_suggestions(
            DOC_A, rev, {"k1": {"text": "Le chat (v1)."}}, prefill=False)
        self.click(app, "Reload saved work")
        view = app.session_state.remote_view
        self.assertEqual(view.record("k1").llm_suggestion, "Le chat (v1).")
        self.assertEqual(view.record("k1").user_final, "")
        # Copy the proposal into the editable text and hold a comment, then Save:
        # the proposal becomes the human's saved text and the comment is saved.
        self.click(app, "Apply proposal")
        self.edit_comment(app, 0, "tone note")
        self.click(app, "Save")
        self.assertEqual(self.stored("k1")["user_final"], "Le chat (v1).")
        self.assertEqual(self.stored("k1")["user_comment"], "tone note")
        saved_user_final = self.stored("k1")["user_final"]
        saved_comment = self.stored("k1")["user_comment"]
        self.assertEqual(self.drafts(app), {})
        # The model now answers a rerun with a fresh proposal for the segment.
        self.fake.processing._process = {
            "suggestions": {"k1": {"text": "Le chat (v2.)"}}, "missing": []}
        # Rerun k1: the new generation must not erase the saved text or comment.
        self.click(app, "Rerun")
        # The model call went out as a one-segment rerun that carries the
        # current batch's index/count block - exactly like process_remote_row -
        # so the service stages the re-run with the current batch's stage and
        # framing. Here the doc is a single batch, so the block is index 0 of 1.
        self.assertEqual(len(self.fake.processing.process_calls), 1)
        call = self.fake.processing.process_calls[0]
        self.assertEqual(call["operation"], "rerun")
        self.assertEqual(len(call["segments"]), 1)
        self.assertEqual(call["segments"][0]["key"], "k1")
        self.assertEqual(call["batch"], {"index": 0, "count": 1})
        view = app.session_state.remote_view
        # The new proposal replaced the old one; the saved text and comment
        # survived the generation.
        self.assertEqual(view.record("k1").llm_suggestion, "Le chat (v2.)")
        self.assertEqual(view.record("k1").user_final, saved_user_final)
        self.assertEqual(view.record("k1").user_comment, saved_comment)
        # No draft was auto-created from the new proposal (no auto-accept).
        self.assertEqual(self.drafts(app), {})
        # The write was a proposal-only write: every suggestions POST used
        # prefill off, so nothing was prefilled into the saved text.
        suggestion_posts = [c for c in self.session.calls
                            if c[0] == "POST" and c[1].endswith("/suggestions")]
        self.assertTrue(suggestion_posts)
        for c in suggestion_posts:
            self.assertIs(c[2]["prefill"], False)
        # No uncertain outcome was flagged (the write confirmed and was re-read).
        self.assertFalse(app.session_state.get("remote_process_uncertain"))

    def test_rerun_remote_row_stale_write_is_not_retried(self):
        # A Rerun whose proposal write 409s (the workspace moved since it was
        # read) is not retried or rebased: the outcome is recorded as uncertain,
        # so another Rerun/Process is blocked until a successful Reload, and the
        # new proposal is not stored. This guards the "conflicts + uncertain
        # outcomes are not auto-retried" contract for the per-row path.
        self.install()
        app = self.start()
        rev = self.revision()
        self.fake.workspace.record_suggestions(
            DOC_A, rev, {"k1": {"text": "Le chat (v1)."}}, prefill=False)
        self.click(app, "Reload saved work")
        # Save a human text first, so the generation must not erase it.
        self.click(app, "Apply proposal")
        self.click(app, "Save")
        self.assertEqual(self.stored("k1")["user_final"], "Le chat (v1).")
        self.assertEqual(self.stored("k1")["llm_suggestion"], "Le chat (v1).")
        # The model answers a rerun with a fresh proposal.
        self.fake.processing._process = {
            "suggestions": {"k1": {"text": "Le chat (v2.)"}}, "missing": []}
        # Another session moves the workspace, so the view's revision is now
        # stale: the next write 409s.
        self.other_writer({"k1": {"text": "Le chat (concurrent)."}})
        self.click(app, "Rerun")
        # Exactly one model attempt; the stale write was not retried.
        self.assertEqual(len(self.fake.processing.process_calls), 1)
        self.assertTrue(app.session_state.get("remote_process_uncertain"))
        # The Rerun's new proposal was not stored (the write 409d).
        self.assertEqual(self.stored("k1")["llm_suggestion"], "Le chat (v1).")
        # The next Rerun stays blocked by the uncertain flag: the button is
        # disabled, so it spends no further model calls.
        rerun_btn = [b for b in app.button if b.label == "Rerun"][0]
        self.assertTrue(rerun_btn.disabled)

    def test_process_remote_row_sends_one_segment_and_keeps_drafts(self):
        # A per-row Process sends just this eligible row to the model as a
        # one-segment batch request (no operation="rerun", but a batch block,
        # like the batch path) and stores its proposal through
        # record_suggestions with prefill off. The proposal-only write never
        # touches the human's saved text, comment or the original target, so the
        # 4 data kinds stay distinct: the neighbours' saved text and comment
        # survive, and only the model's proposal (llm_suggestion) is updated.
        # The button is offered only for a translatable row with no effective
        # 'Your text', so a filled row has nothing to process and no button.
        self.install()
        app = self.start()
        # Seed saved human text + comment on the two neighbours so they are not
        # process-eligible (a filled row has nothing to send); only the blank
        # row k1 offers a Process button.
        self.other_writer({
            "k2": {"text": "Le chien (humain).", "comment": "term A"},
            "k3": {"text": "L'oiseau (humain).", "comment": "term B"},
        })
        self.click(app, "Reload saved work")
        view = app.session_state.remote_view
        # k2 and k3 have saved text, so only k1 is eligible: one Process button.
        process_btns = [b for b in app.button if b.label == "Process"]
        self.assertEqual(len(process_btns), 1)
        self.assertFalse(process_btns[0].disabled)
        self.assertEqual(view.record("k1").user_final, "")
        self.assertEqual(view.record("k2").user_final, "Le chien (humain).")
        self.assertEqual(view.record("k3").user_final, "L'oiseau (humain).")
        # A real unsaved draft on a neighbouring row (k2) must survive the
        # process: the draft is local to the client, so the model call and its
        # proposal-only write never touch it.
        self.edit_text(app, 1, "Le chien (draft).")
        self.assertEqual(self.drafts(app)["k2"]["text"], "Le chien (draft).")
        # The model answers a one-segment process with a proposal for k1.
        self.fake.processing._process = {
            "suggestions": {"k1": {"text": "Le chat (proc)."}}, "missing": []}
        self.click(app, "Process")
        # The model call went out as a one-segment batch Process, not a Rerun:
        # no operation kwarg, but a batch block, and exactly one segment.
        self.assertEqual(len(self.fake.processing.process_calls), 1)
        call = self.fake.processing.process_calls[0]
        self.assertIsNone(call.get("operation"))
        self.assertEqual(len(call["segments"]), 1)
        self.assertEqual(call["segments"][0]["key"], "k1")
        self.assertIsNotNone(call["batch"])
        # "Segments per request" (chunk_size) is carried in the per-request
        # config block, so the operator's "Segments per request" setting
        # reaches the processing service (Stage D: "Segments per request"
        # is sent).
        self.assertIsNotNone(call.get("config"))
        self.assertEqual(call["config"]["chunk_size"], 20)
        view = app.session_state.remote_view
        # The proposal is stored only as the model's suggestion, not
        # auto-accepted into the human's text.
        self.assertEqual(view.record("k1").llm_suggestion, "Le chat (proc).")
        self.assertEqual(view.record("k1").user_final, "")
        # The 4 data kinds stay distinct: the neighbours' saved text and
        # comment are untouched by the generation.
        self.assertEqual(view.record("k2").user_final, "Le chien (humain).")
        self.assertEqual(view.record("k2").user_comment, "term A")
        self.assertEqual(view.record("k3").user_final, "L'oiseau (humain).")
        self.assertEqual(view.record("k3").user_comment, "term B")
        # The new proposal did not auto-create a draft on the processed row
        # (k1), but the neighbouring row's unsaved draft is preserved.
        self.assertNotIn("k1", self.drafts(app))
        self.assertEqual(self.drafts(app)["k2"]["text"], "Le chien (draft).")
        # The write was a proposal-only write: every suggestions POST used
        # prefill off, so nothing was prefilled into the saved text.
        suggestion_posts = [c for c in self.session.calls
                            if c[0] == "POST" and c[1].endswith("/suggestions")]
        self.assertTrue(suggestion_posts)
        for c in suggestion_posts:
            self.assertIs(c[2]["prefill"], False)
        # No uncertain outcome was flagged (the write confirmed and was re-read).
        self.assertFalse(app.session_state.get("remote_process_uncertain"))

    def test_process_remote_row_stale_write_is_not_retried(self):
        # A per-row Process whose proposal write 409s (the workspace moved since
        # it was read) is not retried or rebased: the outcome is recorded as
        # uncertain, so another Process/Rerun is blocked until a successful
        # Reload, and the new proposal is not stored. This guards the "conflicts
        # + uncertain outcomes are not auto-retried" contract for the per-row
        # Process path, which shares the batch's uncertain handling. A blank row
        # (k1) is the eligible one a Process offers; a concurrent save on a
        # different row (k2) advances the workspace revision, so k1's stale write
        # 409s.
        self.install()
        app = self.start()
        # The view reads three blank, eligible rows.
        self.click(app, "Reload saved work")
        # The model answers a one-segment process with a proposal for k1.
        self.fake.processing._process = {
            "suggestions": {"k1": {"text": "Le chat (proc)."}}, "missing": []}
        # Another session moves the workspace, so the view's revision is now
        # stale: the next write 409s.
        self.other_writer({"k2": {"text": "Le chien (concurrent)."}})
        self.click(app, "Process")
        # Exactly one model attempt; the stale write was not retried.
        self.assertEqual(len(self.fake.processing.process_calls), 1)
        call = self.fake.processing.process_calls[0]
        self.assertEqual(call["segments"][0]["key"], "k1")
        self.assertTrue(app.session_state.get("remote_process_uncertain"))
        # The Process's new proposal was not stored (the write 409d): k1 has no
        # proposal and no saved text.
        self.assertEqual(self.stored("k1")["llm_suggestion"], "")
        self.assertEqual(self.stored("k1")["user_final"], "")
        # The next Process stays blocked by the uncertain flag: the button is
        # disabled, so it spends no further model calls.
        process_btn = [b for b in app.button if b.label == "Process"][0]
        self.assertTrue(process_btn.disabled)

    def test_export_carries_saved_human_text_and_preserves_rest(self):
        # Remote export carries the human's SAVED user_final only, through the
        # real document service, then re-parses the result. It checks the
        # requirements, not a match with the implementation:
        #   * a saved user_final is written into the export;
        #   * an unaccepted model proposal (llm_suggestion) is NOT written;
        #   * an unsaved draft is NOT written;
        #   * the source identity and un-touched / excluded rows are unchanged.
        # This matches the local contract: an empty user_final yields new_text()
        # == "" so local skips the row too and memoQ's own target survives.
        self.install()
        # Route the document side through the real document-service contract so
        # the export re-parses the stored original (the fake has no export path).
        self.fake.document = self._real_document_client(SMALL_XML, "sample.mqxliff")
        app = self.start("sample.mqxliff", SMALL_XML)
        view = app.session_state.remote_view
        doc_id = view.document_id
        # The confirmed unit 3 is filtered out; only the two plain rows are
        # active, shown with memoQ's original targets.
        self.assertEqual(list(view.order), ["1#0", "2#1"])
        self.assertEqual(view.record("1#0").mt_target, "Le chat.")
        self.assertEqual(view.record("2#1").mt_target, "Le chien.")
        # Store a proposal on row 1 without prefilling the human text, then save
        # a different human text on the same row: user_final and the proposal are
        # both present and distinct on the server.
        rev = self.revision(doc_id)
        self.fake.workspace.record_suggestions(
            doc_id, rev, {"1#0": {"text": "Le chat (LLM)."}}, prefill=False)
        self.click(app, "Reload saved work")
        view = app.session_state.remote_view
        self.assertEqual(view.record("1#0").llm_suggestion, "Le chat (LLM).")
        self.assertEqual(view.record("1#0").user_final, "")
        self.edit_text(app, 0, "Human translation.")
        self.click(app, "Save")
        self.assertEqual(self.stored("1#0", doc_id)["user_final"], "Human translation.")
        self.assertEqual(self.stored("1#0", doc_id)["llm_suggestion"], "Le chat (LLM).")
        # A second row holds an UNSAVED draft only: it must not leave the
        # workspace, so the export must not carry it.
        self.edit_text(app, 1, "Unsaved draft text")
        self.assertEqual(self.drafts(app, doc_id)["2#1"]["text"], "Unsaved draft text")
        self.assertEqual(self.stored("2#1", doc_id)["user_final"], "")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("remote_workflow.EXPORT_DIR", tmp):
                self.click(app, "Export Valid MQXLIFF")
            last_export = app.session_state.last_export
            self.assertTrue(last_export and os.path.exists(last_export), last_export)
            with open(last_export, "rb") as handle:
                exported_bytes = handle.read()
        # Re-parse what the export wrote, the way the service and the app do.
        exported = mqxliff.MqxliffDocument(exported_bytes, "verify").parse_segments()
        by_key = {s.key: s for s in exported}
        # The saved human text is written; the unaccepted proposal is not.
        self.assertEqual(by_key["1#0"].target_text, "Human translation.")
        self.assertNotIn("Le chat (LLM).", by_key["1#0"].target_text)
        # The unsaved draft is not written; the empty row keeps memoQ's target.
        self.assertNotIn("Unsaved draft text", by_key["2#1"].target_text)
        self.assertEqual(by_key["2#1"].target_text, "Le chien.")
        # An excluded (confirmed) row is left exactly as memoQ wrote it.
        self.assertEqual(by_key["3#2"].target_text, "L'oiseau.")
        # The source identity is unchanged - the export never rewrites source.
        self.assertEqual(
            mqxliff.MqxliffDocument(exported_bytes, "verify").source_fingerprint(),
            mqxliff.MqxliffDocument(SMALL_XML, "sample.mqxliff").source_fingerprint())
        self.assertTrue(any(k == "success" and "Exported" in m
                            for k, m in self.messages(app)))

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
        self.assertEqual(len(self.your_text_areas(app)), 1)
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

    def test_selecting_whole_batch_from_one_row_keeps_drafts_and_row_set(self):
        # A real picker move to "Whole batch" must (a) persist 0 as the page-size
        # setting, (b) show every row, (c) keep the held drafts, and (d) not
        # change the set of rows "Process Current Batch" would send (paging is
        # display-only). In a browser the operator moving the box fires the
        # on_change callback, which writes the setting; AppTest does not run that
        # callback from a scripted value, so we inject the same key the callback
        # writes - remote_page_size = 0 - which is the documented way to request
        # "Whole batch". The substantive guarantees (drafts survive, the batch's
        # row set is unchanged by paging) are asserted below. NOTE: AppTest does
        # actually fire the selectbox on_change from a scripted set_value (see
        # test_rows_shown_picker_drives_page_size_without_direct_write); the direct
        # write here is the explicit-setting path, kept as a stable control.
        self.install()
        app = self.start()
        app.session_state.remote_page_size = 1
        self._run(app)
        self.assertEqual(len(self.your_text_areas(app)), 1)
        # A held draft on the single rendered row.
        self.edit_text(app, 0, "kept")
        self.assertEqual(self.drafts(app)["k1"]["text"], "kept")
        batch_keys = sorted(
            r.key for r in app.session_state.remote_view.batch_records(0))
        # "Whole batch": the setting is 0, so every row is shown.
        app.session_state.remote_page_size = 0
        self._run(app)
        self.assertEqual(app.session_state.remote_page_size, 0)
        self.assertEqual(len(self.your_text_areas(app)), 3)
        # The draft on the now-rendered k1 row is still held, and no other row's
        # draft store is touched.
        self.assertEqual(self.drafts(app)["k1"]["text"], "kept")
        self.assertIsNone(self.drafts(app).get("k2"))
        # Paging never changes which rows the batch process would send.
        self.assertEqual(
            sorted(r.key for r in app.session_state.remote_view.batch_records(0)),
            batch_keys)
        self.click(app, "Save")
        self.assertEqual(self.stored("k1")["user_final"], "kept")

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
        # The XLIFF uploader is empty; the open document is still A, with its
        # draft. A second uploader (the Termbase CSV, E.2) also renders, so the
        # count is 2; the XLIFF control stays index 0.
        self.assertEqual(len(app.file_uploader), 2)
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


# ------------------------------------------------------------- Stage D tests
#
# These tests verify the three distinct Stage D knobs:
# 1. Batch size (working batch): server-fixed, view rebuild preserves drafts.
# 2. Segments per request (chunk_size): non-default value reaches multi-row batch.
# 3. Rows shown at once: >25-row pagination, visible count, draft survival.


def _segments_large():
    """A >25-row fixture for pagination tests."""
    return [
        base.doc_segment("k%d" % i, "u%d" % i, i, "Source %d." % i,
                         "Target %d." % i)
        for i in range(1, 30)  # 29 rows
    ]


def _segments_23():
    """A 23-row fixture: a chosen batch of 10 yields working batches 10/10/3."""
    return [
        base.doc_segment("k%d" % i, "u%d" % i, i, "Source %d." % i,
                         "Target %d." % i)
        for i in range(1, 24)  # 23 rows
    ]


class RemotePaginationTestCase(_RemoteEditMixin, unittest.TestCase):
    """Tests for the 'Rows shown at once' display-only pagination."""

    def test_large_fixture_pagination_visible_count_and_draft_survival(self):
        # A >25-row fixture with remote_page_size=25 should show exactly 25
        # rows on page 1, then the remaining 4 on page 2. A held draft on
        # page 1 survives the page switch and is saved when the operator
        # returns to page 1 and clicks Save.
        segments = _segments_large()
        self.install(segments_by_original={ORIG_A: segments})
        app = self.start()
        app.session_state.remote_page_size = 25
        app.session_state.remote_page = 0  # Ensure page 1
        self._run(app)
        # Page 1: exactly 25 rows visible.
        self.assertEqual(len(self.your_text_areas(app)), 25)
        # Hold a draft on the first row.
        self.edit_text(app, 0, "draft on k1")
        self.assertEqual(self.drafts(app)["k1"]["text"], "draft on k1")
        # Move to page 2: the remaining 4 rows are visible.
        app.radio[0].set_value(1)
        app.session_state.remote_page = 1
        self._run(app)
        self.assertEqual(len(self.your_text_areas(app)), 4)
        # The held draft on k1 (page 1, not rendered) is still in the store.
        self.assertEqual(self.drafts(app)["k1"]["text"], "draft on k1")
        # The batch's row set is unchanged by paging.
        batch_keys = sorted(
            r.key for r in app.session_state.remote_view.batch_records(0))
        self.assertEqual(len(batch_keys), 29)
        # Return to page 1 and save the held draft.
        app.radio[0].set_value(0)
        app.session_state.remote_page = 0
        self._run(app)
        self.assertEqual(len(self.your_text_areas(app)), 25)
        self.click(app, "Save")
        self.assertEqual(self.stored("k1")["user_final"], "draft on k1")
        # The batch's row set is still unchanged.
        self.assertEqual(
            sorted(r.key for r in app.session_state.remote_view.batch_records(0)),
            batch_keys)

    def test_rows_shown_picker_drives_page_size_without_direct_write(self):
        # D.4: drive the REAL "Rows shown at once" picker transition without a
        # direct remote_page/remote_page_size write standing in for the user
        # action. In a browser, moving the box fires on_change ->
        # _on_rows_per_page_change, which persists the setting and (with >25
        # rows) shows a page radio. AppTest fires that callback from a
        # scripted set_value, so the picker -> page-size path IS testable
        # headless: the move writes the setting, the page radio appears, and
        # page 1 of 25-per-page shows the first 25 rows. A held draft is
        # saved when the operator clicks Save, and the batch's row set is
        # unchanged by paging. The page-2 / return transitions are NOT driven
        # headlessly (the page radio re-keys on each page change, so AppTest
        # drops the scripted value); that browser navigation is recorded for G.
        segments = _segments_large()  # 29 rows
        self.install(segments_by_original={ORIG_A: segments})
        app = self.start()
        self._run(app)
        # The picker starts at "Whole batch" (0): all 29 rows, no page radio.
        self.assertEqual(len(self.your_text_areas(app)), 29)
        self.assertFalse(any(r.label == "Page" for r in app.radio),
                         "no page radio while the whole batch is shown")
        # Move the picker to 25 rows; the callback persists the setting and
        # shows the page radio (2 pages for 29 rows).
        picker = next(s for s in app.selectbox
                      if s.label == "Rows shown at once")
        picker.set_value(25)
        self._run(app)
        self.assertEqual(app.session_state.get("remote_page_size"), 25,
                         "the picker's on_change must persist the setting")
        self.assertEqual(len(self.your_text_areas(app)), 25,
                         "page 1 of 25-per-page shows the first 25 rows")
        self.assertTrue(any(r.label == "Page" for r in app.radio),
                        "the page radio should appear once paging is on")
        # Page-2 / return-to-page-1 transitions are NOT driven headlessly: the
        # "Page" radio re-keys on every page change (key "remote_page_radio_%d_%d"
        # % (batch_index, page)), so AppTest drops the scripted set_value on the
        # re-keyed control. In a browser the operator clicks the page control and
        # it navigates; that browser scenario is recorded for G (see
        # REMOTE_PARITY_STATUS / PLAN). The picker-driven page-size transition
        # (above) and the draft-store persistence are what IS reliably
        # headless-testable, and a held draft lives in the document-scoped
        # remote_drafts store independent of which page is rendered.
        self.edit_row_text(app, 0, "draft on k1")
        self.assertEqual(self.drafts(app)["k1"]["text"], "draft on k1")
        batch_keys = sorted(
            r.key for r in app.session_state.remote_view.batch_records(0))
        self.assertEqual(len(batch_keys), 29)
        # Save the held draft from page 1.
        self.click(app, "Save")
        self.assertEqual(self.stored("k1")["user_final"], "draft on k1")
        self.assertEqual(
            sorted(r.key for r in app.session_state.remote_view.batch_records(0)),
            batch_keys)

    def test_large_fixture_whole_batch_shows_all_rows(self):
        # remote_page_size=0 ("Whole batch") shows all 29 rows.
        segments = _segments_large()
        self.install(segments_by_original={ORIG_A: segments})
        app = self.start()
        app.session_state.remote_page_size = 0
        self._run(app)
        self.assertEqual(len(self.your_text_areas(app)), 29)
        # A held draft on the first row is preserved.
        self.edit_text(app, 0, "draft on k1")
        self.assertEqual(self.drafts(app)["k1"]["text"], "draft on k1")
        # The batch's row set is unchanged.
        batch_keys = sorted(
            r.key for r in app.session_state.remote_view.batch_records(0))
        self.assertEqual(len(batch_keys), 29)


class RemoteChunkSizeTestCase(_RemoteEditMixin, unittest.TestCase):
    """Tests for the 'Segments per request' (chunk_size) knob."""

    def test_non_default_chunk_size_reaches_multi_row_batch(self):
        # A non-default chunk_size (e.g. 10) is sent in the payload of a
        # multi-row batch Process. The processing service splits the records
        # into chunks of that size at the processing level. No real model
        # call is made; the stub engine is used, and the stub's
        # translate_batch spy captures the chunks.
        self.install()
        app = self.start()
        # Set a non-stub preset so the config block is sent.
        app.session_state.remote_model_preset = "Local Engine (Ollama)"
        # Set a non-default chunk_size via the widget's session state key.
        # The widget's key is "remote_chunk_size_0" on the first run.
        app.session_state["remote_chunk_size_0"] = 10
        self._run(app)
        # Process the batch (3 rows, all eligible).
        self.click(app, "Process Current Batch")
        # The processing call should have sent chunk_size=10 in the config.
        process_calls = self.fake.processing.process_calls
        self.assertTrue(process_calls, "Expected a /process call")
        config = process_calls[0].get("config")
        self.assertIsNotNone(config, "Expected a config block in the /process call")
        self.assertEqual(config["chunk_size"], 10,
                         "Expected chunk_size=10 in the /process payload")

    def test_real_split_groups_23_segments_into_10_10_3(self):
        # The REAL LLMClient.translate_batch splits a 23-segment batch at
        # chunk_size=10 into three model requests of 10/10/3 - not one
        # oversized request and not the stub. The model call is locked: the
        # OpenAI SDK client is built offline (no socket opens at
        # construction) and ``_chat`` is patched to return the chunk it was
        # given, so no real model or network is reached. The split is
        # observed by counting the segments in each call's user payload.
        records = [
            state_mod.SegmentRecord(
                key="k%d" % i, segment_id="u%d" % i, ordinal=i, batch_index=0,
                source_text="Source %d." % i, mt_target="")
            for i in range(1, 24)
        ]
        sizes = []

        def fake_chat(self, messages, force_json=True):
            payload = json.loads(messages[1]["content"])
            chunk = payload["segments"]
            sizes.append(len(chunk))
            return json.dumps({"segments": [
                {"id": s["id"], "target": "T-%s" % s["id"]}
                for s in chunk]})

        client = llm.LLMClient(llm.ClientConfig(
            preset="Local Engine (Ollama)", chunk_size=10,
            base_url="http://127.0.0.1:9", model="test-model",
            api_key="not-needed"))
        with mock.patch.object(llm.LLMClient, "_chat", fake_chat):
            result = client.translate_batch(records, "SYSTEM")
        self.assertEqual(result.requests, 3,
                         "expected one request per 10/10/3 chunk")
        self.assertEqual(sizes, [10, 10, 3],
                         "the 23 segments were not split 10/10/3")
        self.assertEqual(len(result.suggestions), 23,
                         "every segment of the batch must be covered")
        self.assertEqual(result.missing, [])
        self.assertEqual(result.errors, [])

    def test_non_default_temperature_and_max_tokens_reach_the_payload(self):
        # E.3 (a): a non-default temperature and max_tokens set in the
        # inference settings reach the /process config payload the remote
        # client sends - separately from chunk_size. No real model call is
        # made; the service is the stub engine. The values are read from the
        # widget session-state keys the first run uses.
        self.install()
        app = self.start()
        # A non-stub preset renders the inference settings and makes the
        # service send a config block.
        app.session_state.remote_model_preset = "Local Engine (Ollama)"
        # Set non-default values via the widgets' first-run session-state keys.
        app.session_state["remote_temperature_0"] = 0.35
        app.session_state["remote_max_tokens_0"] = 1234
        self._run(app)
        self.click(app, "Process Current Batch")
        process_calls = self.fake.processing.process_calls
        self.assertTrue(process_calls, "Expected a /process call")
        config = process_calls[0].get("config")
        self.assertIsNotNone(config, "Expected a config block in the /process call")
        self.assertEqual(config["temperature"], 0.35,
                         "Expected temperature=0.35 in the /process payload")
        self.assertEqual(config["max_tokens"], 1234,
                         "Expected max_tokens=1234 in the /process payload")


class RemoteBatchSizeTestCase(_RemoteEditMixin, unittest.TestCase):
    """Tests for the 'Batch size' (working batch) knob."""

    def test_batch_size_view_rebuild_preserves_drafts(self):
        # The batch size is the operator's chosen value, persisted in the
        # server's snapshot. When the view is rebuilt (e.g. after a Reload),
        # the batch size is read back from the server's snapshot, and drafts
        # are preserved by segment key. This test verifies that a held draft
        # survives a view rebuild at the same batch size.
        self.install()
        app = self.start()
        # Hold a draft on k1.
        self.edit_row_text(app, 0, "draft on k1")
        self.assertEqual(self.drafts(app)["k1"]["text"], "draft on k1")
        # Rebuild the view (simulate a Reload).
        view = app.session_state.remote_view
        new_view = type(view).from_remote(
            _segments_a(),
            self._server_snapshot(app.session_state),
        )
        app.session_state.remote_view = new_view
        self._run(app)
        # The batch size is read back from the snapshot (unchanged).
        self.assertEqual(app.session_state.remote_view.batch_size,
                         view.batch_size)
        # The held draft on k1 is still in the store.
        self.assertEqual(self.drafts(app)["k1"]["text"], "draft on k1")
        # The batch's row set is unchanged.
        self.assertEqual(
            sorted(r.key for r in app.session_state.remote_view.batch_records(0)),
            ["k1", "k2", "k3"])

    def open_with_batch(self, segments, batch_size, filename="a.mqxliff",
                        content=b"<a/>"):
        """Open a document, binding it at a chosen working batch size.

        The bootstrap seeds ``remote_batch_size`` at the default (100); setting
        it before the open's bind is the operator's "choose the size" action.
        The bind persists it in the server's snapshot, so a later Reload reads
        it back instead of the default.
        """
        self.install(segments_by_original={ORIG_A: segments})
        app = AppTest.from_file(os.path.join(ROOT, "app.py"),
                                default_timeout=120)
        self._run(app)  # bootstrap (seeds the default size)
        app.session_state.remote_batch_size = batch_size
        app.file_uploader[0].upload(filename, content,
                                    mime_type="application/xml")
        self._run(app)  # open (binds at the chosen size)
        self._run(app)  # render
        return app

    def test_chosen_batch_size_splits_into_working_batches_and_survives_reload(self):
        # A chosen working batch size of 10 splits a 23-row document into
        # batches of 10/10/3. The size is persisted in the server's snapshot, so
        # a Reload reads it back (not the default 100), and a held draft
        # survives by segment key.
        app = self.open_with_batch(_segments_23(), 10)
        view = app.session_state.remote_view
        self.assertEqual(view.batch_size, 10)
        self.assertEqual(view.batch_count, 3)
        self.assertEqual(len(view.batch_records(0)), 10)
        self.assertEqual(len(view.batch_records(1)), 10)
        self.assertEqual(len(view.batch_records(2)), 3)
        # Hold a draft on k1 (batch 0's leading row).
        self.edit_row_text(app, 0, "draft on k1")
        self.assertEqual(self.drafts(app)["k1"]["text"], "draft on k1")
        # A Reload reads the persisted size back, not the default 100.
        self.click(app, "Reload saved work")
        reloaded = app.session_state.remote_view
        self.assertEqual(reloaded.batch_size, 10)
        self.assertEqual(reloaded.batch_count, 3)
        self.assertEqual(len(reloaded.batch_records(2)), 3)
        # The held draft on k1 survived the Reload by segment key.
        self.assertEqual(self.drafts(app)["k1"]["text"], "draft on k1")

    def test_apply_processes_the_same_rows_the_ui_shows_for_batch(self):
        # Apply of a batch must turn exactly the UI's shown rows into
        # corrections. With a batch size of 10 on 23 rows, batch 0 shows the
        # first 10 rows; Apply of batch 0 must correct those same rows and must
        # not touch a row that belongs to a later batch. The UI's "Apply" is the
        # remote port of the local "Apply Changes & Calculate Diffs": it runs the
        # server's calculate_diffs on the batch the UI shows (the service's
        # apply_corrections, driven with the same batch_index the view uses), so
        # it processes the same slice the view renders.
        app = self.open_with_batch(_segments_23(), 10)
        view = app.session_state.remote_view
        batch0_keys = {r.key for r in view.batch_records(0)}
        batch1_keys = {r.key for r in view.batch_records(1)}
        self.assertTrue(batch0_keys.isdisjoint(batch1_keys))
        # Edit only the leading row of batch 0 (k1) and save it through the real
        # UI contract, so the server's state has that row's saved user_final.
        self.edit_row_text(app, 0, "human k1")
        self.click(app, "Save")
        # Apply batch 0: only batch 0's edit becomes a correction. The apply
        # runs the server's calculate_diffs on the batch the UI shows (batch 0).
        result = self.service.apply_corrections(
            DOC_A,
            parse_apply_request({
                "expected_revision": self.revision(),
                "batch_index": 0,
            }))
        self.assertTrue(result["changed"])
        # The Apply of batch 0 produced exactly the edit on batch 0's leading
        # row, recorded at batch_index 0 -- the same slice the UI shows for
        # batch 0.
        corrections = result["corrections"]
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["batch_index"], 0)
        self.assertEqual(corrections[0]["human_final"], "human k1")
        # No correction carries a later batch's index: the Apply respected the
        # batch boundary and did not process a batch-1 row.
        self.assertTrue(all(c["batch_index"] == 0 for c in corrections),
                        "a correction leaked a later batch's index: %r"
                        % corrections)

    def _server_snapshot(self, session_state):
        """Build a workspace snapshot from the current session state."""
        view = session_state.remote_view
        segments = {
            k: {"user_final": view.record(k).user_final,
                "user_comment": view.record(k).user_comment,
                "llm_suggestion": view.record(k).llm_suggestion,
                "edited_by_human": view.record(k).edited_by_human}
            for k in view.order
        }
        return {
            "document_id": view.document_id,
            "bound_original_id": view.original_id,
            "tag_source": view.tag_source,
            "revision": view.revision,
            "batch_size": view.batch_size,
            "order": list(view.order),
            "segments": segments,
            "source_file": view.source_file,
            "current_batch": view.current_batch,
            "counts": view.counts(),
        }


class RemoteContextTestCase(_RemoteEditMixin, unittest.TestCase):
    """Stage E: the operator's editorial rules reach the actually-built prompt.

    The remote UI's 'Editorial context' controls (Hard Rules / Vibe / Batch
    Directive) are forwarded with every Process call - batch, re-run and
    single-row - as the context block the processing service builds its system
    prompt from. These tests prove that a rule the operator types in the remote
    UI reaches the prompt the service actually assembles, with a fake/stub and
    no real model: the Process is routed through the *real* processing service
    (stub engine, no SDK, no socket) and ``engines.StubClient.translate_batch``
    is spied to capture the built prompt. A complementary test reads the
    forwarded context straight off the fake's recorded call to prove the
    UI-to-request wiring in isolation.
    """

    def _spy_built_prompt(self):
        """Patch the stub engine's batch translate to capture the built prompt.

        Mirrors ``TestPromptContext.capture`` in test_processing_service.py:
        the spy is patched on the class, so it receives the StubClient instance
        as its first argument and forwards to the original unbound method.
        """
        seen = {}
        original = engines.StubClient.translate_batch

        def spy(stub, records, system_prompt, progress=None):
            seen["prompt"] = system_prompt
            return original(stub, records, system_prompt)

        return seen, mock.patch.object(engines.StubClient, "translate_batch", spy)

    def test_user_hard_rule_reaches_the_built_prompt(self):
        # A hard rule typed into the remote UI's 'Hard Rules' control reaches
        # the system prompt the processing service actually builds - the full
        # UI-to-prompt path, with the stub engine and no real model.
        self.install()
        # Route the app's Process through the real processing service so the
        # prompt is built by the production code, not the fake.
        self.fake.processing = self._real_processing_client()
        # Spy on the stub engine to capture the prompt it is actually built with.
        seen, spy_patch = self._spy_built_prompt()
        spy_patch.start()
        self.addCleanup(spy_patch.stop)
        # Fail-closed: forbid construction of *any* real engine. Both real-client
        # paths (the config path and the legacy non-stub path) converge on
        # ``llm.LLMClient``; the stub path never builds one. A wrong preset
        # selection therefore raises here instead of opening a socket.
        guard = mock.Mock(side_effect=AssertionError(
            "a non-stub engine was constructed; the test must stay offline"))
        guard_patch = mock.patch.object(engines.llm, "LLMClient", guard)
        guard_patch.start()
        self.addCleanup(guard_patch.stop)
        app = self.start()
        # Drive the preset through the widget itself (a raw session-state write is
        # reverted by the UI on re-render), then let the widget write the preset
        # back. The default preset is a real engine; switch to the stub.
        preset_box = next(s for s in app.selectbox
                          if s.label == "Provider preset")
        preset_box.set_value("Test backend (stub)")
        self._run(app)
        # Verify the actually-selected preset and that the engine resolves to
        # stub *before* the processing service is called.
        self.assertEqual(app.session_state.remote_model_preset,
                         "Test backend (stub)")
        self.assertEqual(remote_workflow._process_target(app.session_state),
                         (None, "stub"))
        rule = "MARKER-RULE: never translate code names"
        self.input_text_area(app, "Hard Rules", rule)
        self.assertEqual(app.session_state.remote_hard_rules, rule)
        self.click(app, "Process Current Batch")
        # No real engine was constructed (offline, no socket); the stub built the
        # prompt the operator's rule reached.
        guard.assert_not_called()
        self.assertIn("prompt", seen, "the stub engine was not reached")
        self.assertIn(rule, seen["prompt"])

    def test_editorial_context_is_forwarded_in_the_process_call(self):
        # The UI-to-request wiring, in isolation: the typed rule is carried in
        # the /process call's context block. The fake records the call and
        # returns an empty body, so no proposal is stored and the app notifies
        # "no proposals" - the context is still recorded on the call.
        self.install()
        app = self.start()
        rule = "MARKER-RULE: keep the client name verbatim"
        self.input_text_area(app, "Hard Rules", rule)
        self.click(app, "Process Current Batch")
        self.assertTrue(self.fake.processing.process_calls,
                        "expected a /process call")
        call = self.fake.processing.process_calls[0]
        self.assertIn("context", call)
        self.assertEqual(call["context"]["hard_rules_text"], rule)
        # The other editorial fields the UI carries are forwarded too; a fresh
        # session's vibe/directive are the empty-string bootstrap defaults.
        self.assertEqual(call["context"]["vibe"], app.session_state.remote_vibe)
        self.assertEqual(
            call["context"]["directive"], app.session_state.remote_directive)

    def test_template_override_is_forwarded_in_the_process_call(self):
        # A stage-template override the operator types in the remote UI's
        # 'Stage template override' control is carried in the /process call's
        # context block, where the service uses it as the batch's stage
        # template. The fake records the call and returns an empty body, so the
        # context the app built is asserted straight off it.
        self.install()
        app = self.start()
        template = "MARKER-TEMPLATE: render {source} as a gloss"
        self.input_text_area(app, "Stage template override", template)
        self.assertEqual(app.session_state.remote_template, template)
        self.click(app, "Process Current Batch")
        self.assertTrue(self.fake.processing.process_calls,
                        "expected a /process call")
        call = self.fake.processing.process_calls[0]
        self.assertIn("context", call)
        self.assertEqual(call["context"]["template"], template)

    def test_termbase_is_forwarded_in_the_process_call(self):
        # The operator's termbase, held in this browser session only, is carried
        # in the /process call's context block as a list of {source, target,
        # note?} entries; the service re-filters it to the terms present in the
        # batch. A memoQ-CSV collection control is a later feature, so this
        # drives the field by direct session-state write (the source_lang/
        # target_lang precedent) and asserts the forwarding path the app built.
        self.install()
        app = self.start()
        app.session_state.remote_termbase = [
            {"source": "dashboard", "target": "panneau de bord"},
            {"source": "client", "target": "client", "note": "keep verbatim"},
        ]
        self.click(app, "Process Current Batch")
        self.assertTrue(self.fake.processing.process_calls,
                        "expected a /process call")
        call = self.fake.processing.process_calls[0]
        self.assertIn("context", call)
        self.assertEqual(
            call["context"]["termbase"],
            [{"source": "dashboard", "target": "panneau de bord"},
             {"source": "client", "target": "client", "note": "keep verbatim"}])

    def test_template_and_termbase_omitted_when_unset(self):
        # A fresh session has neither a template override nor a termbase: both
        # are left empty by the bootstrap, so neither key is forwarded and the
        # service falls back to its own default_prompt_templates() and no
        # termbase material - parity with the local default.
        self.install()
        app = self.start()
        self.click(app, "Process Current Batch")
        self.assertTrue(self.fake.processing.process_calls,
                        "expected a /process call")
        call = self.fake.processing.process_calls[0]
        self.assertIn("context", call)
        self.assertNotIn("template", call["context"])
        self.assertNotIn("termbase", call["context"])

    def test_template_override_applies_to_the_built_prompt(self):
        # E.1: a stage-template override typed into the remote UI's 'Stage
        # template override' control is not merely forwarded in the context
        # payload - it is *applied*: the real processing service substitutes it
        # for the built-in role when it assembles the system prompt the engine
        # actually receives. Proven offline through the real service and the
        # stub engine (no model, no socket): the prompt the stub is built with
        # carries the template's own text, not just the forwarded context key.
        self.install()
        self.fake.processing = self._real_processing_client()
        seen, spy_patch = self._spy_built_prompt()
        spy_patch.start()
        self.addCleanup(spy_patch.stop)
        # Fail-closed: forbid construction of any real engine, so a wrong preset
        # selection raises here instead of opening a socket.
        guard = mock.Mock(side_effect=AssertionError(
            "a non-stub engine was constructed; the test must stay offline"))
        guard_patch = mock.patch.object(engines.llm, "LLMClient", guard)
        guard_patch.start()
        self.addCleanup(guard_patch.stop)
        app = self.start()
        preset_box = next(s for s in app.selectbox
                          if s.label == "Provider preset")
        preset_box.set_value("Test backend (stub)")
        self._run(app)
        self.assertEqual(app.session_state.remote_model_preset,
                         "Test backend (stub)")
        self.assertEqual(remote_workflow._process_target(app.session_state),
                         (None, "stub"))
        template = "STAGE-TEMPLATE-MARKER-42: render the batch as a gloss"
        self.input_text_area(app, "Stage template override", template)
        self.assertEqual(app.session_state.remote_template, template)
        self.click(app, "Process Current Batch")
        guard.assert_not_called()
        self.assertIn("prompt", seen, "the stub engine was not reached")
        # The template's own text reaches the assembled prompt the engine gets.
        self.assertIn("STAGE-TEMPLATE-MARKER-42", seen["prompt"])

    def test_relevant_termbase_term_reaches_the_built_prompt_and_irrelevant_is_filtered(self):
        # E.1: the operator's termbase is not merely forwarded - a term that
        # occurs in the batch is *applied* (its rendered pair reaches the
        # assembled prompt the engine receives), while a term that does not
        # occur is filtered out and absent. Proven offline through the real
        # service and the stub engine: the service re-filters the termbase with
        # the same relevant_terms() the local workflow uses. The termbase has
        # no UI control yet, so it is driven by direct session-state write (the
        # source_lang/target_lang precedent); the assertion is on the built
        # prompt, not the forwarded payload.
        self.install()
        self.fake.processing = self._real_processing_client()
        seen, spy_patch = self._spy_built_prompt()
        spy_patch.start()
        self.addCleanup(spy_patch.stop)
        guard = mock.Mock(side_effect=AssertionError(
            "a non-stub engine was constructed; the test must stay offline"))
        guard_patch = mock.patch.object(engines.llm, "LLMClient", guard)
        guard_patch.start()
        self.addCleanup(guard_patch.stop)
        app = self.start()
        preset_box = next(s for s in app.selectbox
                          if s.label == "Provider preset")
        preset_box.set_value("Test backend (stub)")
        self._run(app)
        self.assertEqual(app.session_state.remote_model_preset,
                         "Test backend (stub)")
        # Batch 0 holds the three sample rows ("The cat.", "The dog.",
        # "The bird."); "cat" occurs, "dashboard" does not.
        app.session_state.remote_termbase = [
            {"source": "cat", "target": "Le chat"},
            {"source": "dashboard", "target": "panneau de bord"},
        ]
        self.click(app, "Process Current Batch")
        guard.assert_not_called()
        self.assertIn("prompt", seen, "the stub engine was not reached")
        prompt = seen["prompt"]
        # The relevant term's rendered pair is applied into the prompt.
        self.assertIn('"cat" -> "Le chat"', prompt)
        # The irrelevant term is filtered out - it never reaches the prompt.
        self.assertNotIn("dashboard", prompt)
        self.assertNotIn("panneau de bord", prompt)

    def test_termbase_csv_upload_reaches_the_built_prompt(self):
        # E.2: a memoQ termbase CSV the operator uploads through the new
        # 'Termbase CSV' control is parsed and loaded into the session, and a
        # term that occurs in the batch then reaches the assembled prompt the
        # engine receives - while a term that does not occur is filtered out.
        # Proven offline through the real processing service and the stub
        # engine (no model, no socket), the same way E.1 proved the termbase
        # path: the assertion is on the built prompt, not the forwarded payload.
        # Unlike E.1 (which wrote the termbase by direct session-state), the
        # CSV is driven through the UI's second file uploader.
        self.install()
        self.fake.processing = self._real_processing_client()
        seen, spy_patch = self._spy_built_prompt()
        spy_patch.start()
        self.addCleanup(spy_patch.stop)
        guard = mock.Mock(side_effect=AssertionError(
            "a non-stub engine was constructed; the test must stay offline"))
        guard_patch = mock.patch.object(engines.llm, "LLMClient", guard)
        guard_patch.start()
        self.addCleanup(guard_patch.stop)
        app = self.start()
        preset_box = next(s for s in app.selectbox
                          if s.label == "Provider preset")
        preset_box.set_value("Test backend (stub)")
        self._run(app)
        # The memoQ export has seven columns (five source synonyms, two target);
        # source is column 0, target column 5. "cat" occurs in batch 0
        # ("The cat."); "dashboard" does not.
        csv_bytes = (
            b"English,English,English,English,English,Russian,Russian\n"
            b"cat,cat,cat,cat,cat,Le chat,Le chat\n"
            b"dashboard,dashboard,dashboard,dashboard,dashboard,"
            b"panneau de bord,panneau de bord\n"
        )
        app.file_uploader[1].upload("termbase.csv", csv_bytes,
                                    mime_type="text/csv")
        self._run(app)
        # The upload parsed and populated the session termbase as a dict list.
        sources = [t["source"] for t in app.session_state.remote_termbase]
        self.assertIn("cat", sources)
        self.assertIn("dashboard", sources)
        self.click(app, "Process Current Batch")
        guard.assert_not_called()
        self.assertIn("prompt", seen, "the stub engine was not reached")
        prompt = seen["prompt"]
        # The relevant term's rendered pair is applied into the prompt.
        self.assertIn('"cat" -> "Le chat"', prompt)
        # The irrelevant term is filtered out - it never reaches the prompt.
        self.assertNotIn("dashboard", prompt)
        self.assertNotIn("panneau de bord", prompt)

    def test_malformed_termbase_csv_shows_a_clear_error(self):
        # E.2: a CSV that yields no loadable terms (a header with no data rows)
        # surfaces a clear, actionable error instead of silently doing nothing.
        self.install()
        app = self.start()
        # A header row only - nothing to load.
        csv_bytes = b"English,English,English,English,English,Russian,Russian\n"
        app.file_uploader[1].upload("empty.csv", csv_bytes,
                                    mime_type="text/csv")
        self._run(app)
        # Nothing was added to the session termbase.
        self.assertEqual(app.session_state.remote_termbase, [])
        errors = [v for (kind, v) in self.messages(app) if kind == "error"]
        self.assertTrue(any("No termbase terms loaded" in v for v in errors),
                        "expected a clear 'no terms loaded' error: %r"
                        % errors)

    def test_no_term_csv_does_not_clobber_the_active_termbase(self):
        # E.2 (preservation): a CSV that yields no usable terms must not wipe
        # the termbase the operator already loaded. The narrow E.2 check that
        # starts from an empty termbase cannot prove this; here a valid memoQ
        # export is loaded first (the active termbase), then a no-term CSV, and
        # the active termbase must survive alongside the clear "no terms loaded"
        # error the operator is shown.
        self.install()
        app = self.start()
        # 1. Load a valid memoQ export - the operator's active termbase.
        valid_bytes = (
            b"English,English,English,English,English,Russian,Russian\n"
            b"cat,cat,cat,cat,cat,Le chat,Le chat\n"
            b"dashboard,dashboard,dashboard,dashboard,dashboard,"
            b"panneau de bord,panneau de bord\n"
        )
        app.file_uploader[1].upload("termbase.csv", valid_bytes,
                                    mime_type="text/csv")
        self._run(app)
        self.assertEqual(
            [t["source"] for t in app.session_state.remote_termbase],
            ["cat", "dashboard"])
        # 2. Then load a CSV with no usable terms (a header, no data rows).
        app.file_uploader[1].upload("empty.csv",
                                    b"English,English,English,English,English,"
                                    b"Russian,Russian\n",
                                    mime_type="text/csv")
        self._run(app)
        # The clear, actionable error is shown ...
        errors = [v for (kind, v) in self.messages(app) if kind == "error"]
        self.assertTrue(any("No termbase terms loaded" in v for v in errors),
                        "expected a clear 'no terms loaded' error: %r"
                        % errors)
        # ... and the previously active termbase is preserved, not clobbered.
        self.assertEqual(
            [t["source"] for t in app.session_state.remote_termbase],
            ["cat", "dashboard"])


class RemoteCorrectionsTestCase(_RemoteEditMixin, unittest.TestCase):
    """Stage F, first scenario: a human edit becomes a correction, is stored in
    the workspace, survives a re-read, and reaches the next batch's feedback.

    The whole path runs offline against the *real* contract: the app's own
    ``WorkspaceClient`` talks through the real workspace Starlette app to the
    real ``WorkspaceService`` over the in-memory repository (the PostgreSQL
    transaction contract). No model, no socket. The model's proposal is seeded
    with the patch's ``llm_suggestion`` field (the same field a Process writes
    via ``record_suggestions``); the human's edit is the ``segments`` text,
    differing from the proposal, so it is a real correction rather than an
    implicit approval. The correction then lives in the document snapshot, so a
    re-read returns it, and the view renders it into the corrections block the
    next batch's prompt is built from.
    """

    def test_human_edit_becomes_correction_survives_reread_and_feeds_next_batch(self):
        self.install()
        app = self.start()
        view = app.session_state.remote_view
        self.assertFalse(view.is_empty)
        self.assertEqual(view.batch_count, 1)  # 3 rows, server batch size 100

        # 1. The model's proposal (what a Process records via the suggestions
        #    endpoint) and the human's differing edit (the patch endpoint), each
        #    through the real contract. The human's text differs from the
        #    proposal, so applying turns it into a real correction.
        rev = self.revision()
        self.service.record_suggestions(
            DOC_A, parse_suggestions_request(
                {"expected_revision": rev,
                 "suggestions": {"k1": {"text": "Le chat (LLM)."}}}))
        self.assertEqual(self.stored("k1")["llm_suggestion"], "Le chat (LLM).")

        rev = self.revision()
        self.fake.workspace.save_segments(
            DOC_A, rev, {"k1": {"text": "Le chat humain.", "comment": "tone"}})
        self.assertEqual(self.stored("k1")["user_final"], "Le chat humain.")
        self.assertEqual(self.stored("k1")["user_comment"], "tone")

        # 2. Apply: the human edit becomes a stored correction in batch 0.
        rev = self.revision()
        result = self.fake.workspace.apply_corrections(DOC_A, rev, 0)
        self.assertTrue(result["changed"])
        self.assertEqual(result["produced"], 1)
        correction = result["corrections"][0]
        self.assertEqual(correction["segment_id"], "u1")
        self.assertEqual(correction["source"], "The cat.")
        self.assertEqual(correction["llm_suggestion"], "Le chat (LLM).")
        self.assertEqual(correction["human_final"], "Le chat humain.")
        self.assertEqual(correction["comment"], "tone")
        self.assertEqual(correction["batch_index"], 0)
        # Apply is a state change: the row is marked applied and edited, and the
        # revision advanced.
        self.assertEqual(self.revision(), rev + 1)
        self.assertTrue(self.stored("k1")["edited_by_human"])

        # 3. Survives a re-read: a fresh get_workspace returns the stored
        #    correction - it lives in the document snapshot, not in memory.
        reread = self.fake.workspace.get_workspace(DOC_A)
        self.assertEqual(len(reread.get("corrections", [])), 1)
        self.assertEqual(reread["corrections"][0]["human_final"], "Le chat humain.")

        # 4. Reaches the next batch's feedback: a view rebuilt from the re-read
        #    snapshot renders the stored correction into the corrections block
        #    for a batch after batch 0, in the local Correction.render format.
        reread_view = RemoteWorkspaceView.from_remote(_segments_a(), reread)
        block = reread_view.corrections_block(upto_batch=1)
        self.assertIn('<u1: "Le chat humain.">', block)
        self.assertIn('source:   "The cat."', block)
        self.assertIn('rejected: "Le chat (LLM)."', block)
        self.assertIn('note:     "tone"', block)

        # A batch cannot correct itself: batch 0's correction does not feed
        # batch 0, so the block is empty for upto_batch=0.
        self.assertEqual(reread_view.corrections_block(upto_batch=0), "")

        # 5. The next batch's process is built from that block: the context the
        #    remote process call is built with carries the same rendered text.
        context = remote_workflow._remote_context_block(
            app.session_state, reread_view, 1)
        self.assertEqual(context["corrections_block"], block)

        # An empty correction set omits the key entirely (the local
        # feedback_block contract: no signal, no block). The pre-apply view and
        # a fresh snapshot have no corrections.
        empty_context = remote_workflow._remote_context_block(
            app.session_state, view, 1)
        self.assertNotIn("corrections_block", empty_context)
        self.assertEqual(view.corrections_block(upto_batch=1), "")

    def test_apply_button_persists_batch_correction_through_ui(self):
        # The operator's "Apply Changes & Calculate Diffs" button (distinct from
        # the per-row "Copy proposal") drives apply_remote_corrections: it saves
        # the held draft first, then runs the server's calculate_diffs on the
        # batch the UI shows, persisting a stored correction that survives a
        # re-read and feeds the next batch's feedback. The whole path is driven
        # through the real UI contract; no service method is called directly.
        self.install()
        app = self.start()
        view = app.session_state.remote_view
        self.assertFalse(view.is_empty)

        # Seed the model's proposal for k1 through the real suggestions endpoint
        # (what a Process records via record_suggestions), then hold the human's
        # differing edit as an unsaved draft in the 'Your text' cell.
        rev = self.revision()
        self.service.record_suggestions(
            DOC_A, parse_suggestions_request(
                {"expected_revision": rev,
                 "suggestions": {"k1": {"text": "Le chat (LLM)."}}}))
        self.click(app, "Reload saved work")
        self.edit_row_text(app, 0, "Le chat humain.")
        # The draft is held, not yet stored: no correction exists yet and the
        # saved text is still empty.
        self.assertEqual(self.revision(), view.revision + 1,
                         "the suggestion write advanced the revision once")
        # The draft is held, not yet stored: the snapshot always carries a
        # `corrections` key (initialized to []), and it is still empty.
        self.assertEqual(self.repo.rows[DOC_A].snapshot.get("corrections",
                                                             []), [])

        # Click the batch-level Apply button. It saves the held draft, then runs
        # the server's calculate_diffs on batch 0, persisting the correction.
        self.click(app, "Apply Changes & Calculate Diffs")

        # The correction is now stored in the workspace snapshot (a state
        # change: the row is marked applied/edited and the revision advanced).
        snap = self.repo.rows[DOC_A].snapshot
        self.assertEqual(len(snap["corrections"]), 1)
        correction = snap["corrections"][0]
        self.assertEqual(correction["segment_id"], "u1")
        self.assertEqual(correction["human_final"], "Le chat humain.")
        self.assertEqual(correction["llm_suggestion"], "Le chat (LLM).")
        self.assertEqual(correction["batch_index"], 0)
        self.assertTrue(self.stored("k1")["edited_by_human"])
        self.assertEqual(self.stored("k1")["user_final"], "Le chat humain.")

        # It survives a re-read and reaches the next batch's feedback block.
        reread = self.fake.workspace.get_workspace(DOC_A)
        self.assertEqual(len(reread.get("corrections", [])), 1)
        reread_view = RemoteWorkspaceView.from_remote(_segments_a(), reread)
        block = reread_view.corrections_block(upto_batch=1)
        self.assertIn('<u1: "Le chat humain.">', block)
        self.assertIn('rejected: "Le chat (LLM)."', block)

        # A no-op re-Apply does not duplicate the correction (the service
        # replaces rather than appends, and a no-change apply keeps the
        # revision).
        before = self.revision()
        self.click(app, "Apply Changes & Calculate Diffs")
        self.assertEqual(len(self.repo.rows[DOC_A].snapshot["corrections"]), 1)
        self.assertEqual(self.revision(), before,
                         "a no-op re-Apply keeps the revision")

    def test_apply_button_stale_revision_is_not_retried_or_rebased(self):
        # The PLAN's "repeated Apply + a stale revision" finish item, the
        # stale-write half: a concurrent change between the view's read and the
        # button-driven Apply makes the apply's expected revision stale, so the
        # server rejects it (409). The apply must NOT auto-retry (rebase and
        # re-apply) and must NOT rebase the held drafts; it records the run as
        # uncertain, leaving the next Process/Apply blocked until a Reload
        # reconciles. Driven through the real UI contract; only the apply is a
        # service call (the button's) - apply_corrections is never called
        # directly.
        self.install()
        app = self.start()
        view = app.session_state.remote_view
        self.assertFalse(view.is_empty)
        # Seed the model's proposal and a differing saved human edit, so an
        # Apply *would* store a correction were it not stale.
        rev = self.revision()
        self.service.record_suggestions(
            DOC_A, parse_suggestions_request(
                {"expected_revision": rev,
                 "suggestions": {"k1": {"text": "Le chat (LLM)."}}}))
        rev = self.revision()
        self.fake.workspace.save_segments(
            DOC_A, rev, {"k1": {"text": "Le chat humain.",
                                "comment": "tone"}})
        self.assertEqual(self.stored("k1")["user_final"], "Le chat humain.")
        # Re-read so the live view holds the current revision and no draft is
        # held; a no-op save keeps the apply's save step a no-op, so the apply
        # uses the stale view revision rather than a freshly read one.
        self.click(app, "Reload saved work")
        view = app.session_state.remote_view
        view_rev = view.revision
        base = self.revision()
        self.assertEqual(view_rev, base,
                         "the re-read view holds the current revision")
        # A concurrent change (another session) advances the revision, so the
        # view the Apply was built on is now stale - but the live view is not
        # re-read, so it still names the old revision.
        self.other_writer({"k2": {"text": "Le chien concurrent."}})
        advanced = self.revision()
        self.assertEqual(advanced, base + 1,
                         "the concurrent write advanced the revision once")
        self.assertEqual(view.revision, view_rev,
                         "the live view is unchanged; only the server moved")
        # Drive the batch-level Apply button. With no held draft the save step
        # is a no-op, so the apply uses the stale view revision and the server
        # 409s.
        self.click(app, "Apply Changes & Calculate Diffs")
        # No auto-retry and no rebase: the revision is still the concurrent
        # write's, no correction was stored, and the live view is left as-is
        # (the apply neither re-read nor rebased it).
        self.assertEqual(self.revision(), advanced,
                         "a stale Apply is not rebased or retried")
        self.assertEqual(
            self.repo.rows[DOC_A].snapshot.get("corrections", []), [],
            "a stale Apply stored nothing")
        self.assertEqual(app.session_state.remote_view.revision, view_rev,
                         "a stale Apply does not re-read or rebase the view")
        # The run is marked uncertain so another Process/Apply is blocked until
        # a Reload reconciles, and the operator is told the apply was refused.
        self.assertTrue(app.session_state.remote_process_uncertain,
                        "a stale Apply leaves the run uncertain")
        self.assertTrue(any(k == "error" and "not retried" in m
                            for k, m in self.messages(app)),
                        "a stale Apply reports that it was not retried")

    def _spy_built_prompt(self):
        """Capture the system prompt the processing service actually assembles
        for a batch, so the two-batch scenario can assert a stored correction
        reaches the next batch's prompt without a real call. Local copy of the
        RemoteContextTestCase helper (kept local so the corrections class stays
        independent of it)."""
        seen = {}
        original = engines.StubClient.translate_batch

        def spy(stub, records, system_prompt, progress=None):
            seen["prompt"] = system_prompt
            return original(stub, records, system_prompt)

        return seen, mock.patch.object(
            engines.StubClient, "translate_batch", spy)

    def comment_inputs(self, app):
        """The per-row 'Comment' inputs only.

        The main column also holds the sidebar's connection fields
        (base_url / api_key), which this Streamlit version lists in
        ``app.text_input`` too; the per-row comment cells are the only ones
        keyed ``remote_comment_``, so count by key rather than the raw length.
        """
        return [ti for ti in app.text_input
                if ti.key and ti.key.startswith("remote_comment_")]

    def edit_row_comment(self, app, row_index, value):
        inputs = self.comment_inputs(app)
        self.assertGreaterEqual(len(inputs), row_index + 1)
        inputs[row_index].input(value)
        self._run(app)

    def test_two_batch_scenario_correction_feeds_next_batch_prompt(self):
        # The PLAN's offline two-batch scenario, driven through the UI:
        # 3 rows at batch size 2 -> batch 0 = [k1, k2], batch 1 = [k3].
        # Edit + comment k1 via the UI, Apply (persisting a batch-0
        # correction), then Process batch 0 and batch 1 and confirm the
        # correction reaches batch 1's assembled prompt while batch 0's own
        # prompt never carries its own feedback.
        self.install(segments_by_original={ORIG_A: _segments_a()})
        app = AppTest.from_file(os.path.join(ROOT, "app.py"),
                                default_timeout=120)
        self._run(app)  # bootstrap (seeds the default size)
        app.session_state.remote_batch_size = 2
        app.file_uploader[0].upload("a.mqxliff", b"<a/>",
                                    mime_type="application/xml")
        self._run(app)  # open (binds at the chosen size)
        self._run(app)  # render
        self.assertEqual(app.session_state.remote_batch_size, 2,
                         "a held size must survive the open")
        self.assertEqual(app.session_state.remote_view.batch_count, 2,
                         "3 rows at size 2 must yield 2 batches")
        # Seed k1's model proposal so the Apply diff has something to reject.
        rev = self.revision()
        self.service.record_suggestions(
            DOC_A, parse_suggestions_request(
                {"expected_revision": rev,
                 "suggestions": {"k1": {"text": "Le chat (LLM)."}}}))
        self.click(app, "Reload saved work")
        # Drive the preset to the stub so the engine stays offline.
        preset_box = next(s for s in app.selectbox
                          if s.label == "Provider preset")
        preset_box.set_value("Test backend (stub)")
        self._run(app)
        self.assertEqual(app.session_state.remote_model_preset,
                         "Test backend (stub)")
        seen, spy_patch = self._spy_built_prompt()
        with spy_patch, mock.patch.object(
                engines.llm, "LLMClient",
                mock.Mock(side_effect=AssertionError(
                    "a non-stub engine was constructed; "
                    "the test must stay offline"))):
            self.fake.processing = self._real_processing_client()
            # 1. Edit + comment k1 (row 0 of batch 0) via the UI.
            self.edit_row_text(app, 0, "Le chat humain.")
            self.edit_row_comment(app, 0, "use the human register")
            # 2. Apply the batch's edits as a correction (via the UI button).
            self.click(app, "Apply Changes & Calculate Diffs")
            snap = self.repo.rows[DOC_A].snapshot
            self.assertTrue(
                any(c["segment_id"] == "u1"
                    and c["human_final"] == "Le chat humain."
                    and c["batch_index"] == 0
                    for c in snap.get("corrections", [])),
                "Apply must persist the batch-0 correction")
            # 3. Process batch 0 (its eligible row k2): its own prompt must
            #    NOT carry the batch-0 correction - a batch never feeds itself.
            self.click(app, "Process Current Batch")
            prompt0 = seen.get("prompt")
            self.assertIsNotNone(prompt0, "batch 0 must send a prompt")
            self.assertNotIn(
                "HUMAN CORRECTIONS FROM EARLIER BATCHES", prompt0,
                "a batch must not feed its own correction")
            self.assertNotIn('<u1: "Le chat humain.">', prompt0)
            # 4. Navigate to batch 1 and process it (its eligible row k3):
            #    its assembled prompt MUST carry the batch-0 correction.
            self.click(app, "Next")
            self.click(app, "Process Current Batch")
            prompt1 = seen.get("prompt")
            self.assertIsNotNone(prompt1, "batch 1 must send a prompt")
            self.assertIn("HUMAN CORRECTIONS FROM EARLIER BATCHES", prompt1)
            self.assertIn('<u1: "Le chat humain.">', prompt1)
            self.assertIn('rejected: "Le chat (LLM)."', prompt1)
            self.assertIn("use the human register", prompt1,
                          "the operator's comment must reach the correction")

    def _spy_rerun_prompt(self):
        """Capture the system prompt the processing service assembles for a
        per-row Rerun (operation="rerun"), so the scenario can assert a stored
        correction reaches the re-run's prompt without a real call. Mirrors
        _spy_built_prompt but on the single-segment translate_one path."""
        seen = {}
        original = engines.StubClient.translate_one

        def spy(stub, record, system_prompt):
            seen["prompt"] = system_prompt
            return original(stub, record, system_prompt)

        return seen, mock.patch.object(
            engines.StubClient, "translate_one", spy)

    def test_rerun_carry_corrections_block_from_earlier_batch(self):
        # The PLAN's "Rerun feedback use reconciled with local": the local
        # retry_segment shares build_prompt_for with the batch run, so a
        # per-row Rerun is judged against the same corrections_block as its
        # batch - the human corrections recorded in *earlier* batches. The
        # remote per-row Rerun must do the same. Drive a 3-row document at batch
        # size 2, store a batch-0 correction, navigate to batch 1, and confirm
        # the re-run of a batch-1 row carries that correction in the assembled
        # prompt - the reconciliation the batch Process already has (a batch
        # never feeds itself, but a later batch is guided by the earlier one).
        self.install(segments_by_original={ORIG_A: _segments_a()})
        app = AppTest.from_file(os.path.join(ROOT, "app.py"),
                                default_timeout=120)
        self._run(app)  # bootstrap (seeds the default size)
        app.session_state.remote_batch_size = 2
        app.file_uploader[0].upload("a.mqxliff", b"<a/>",
                                    mime_type="application/xml")
        self._run(app)  # open (binds at the chosen size)
        self._run(app)  # render
        self.assertEqual(app.session_state.remote_batch_size, 2,
                         "a held size must survive the open")
        self.assertEqual(app.session_state.remote_view.batch_count, 2,
                         "3 rows at size 2 must yield 2 batches")
        # Seed k1's model proposal so the Apply diff has something to reject.
        rev = self.revision()
        self.service.record_suggestions(
            DOC_A, parse_suggestions_request(
                {"expected_revision": rev,
                 "suggestions": {"k1": {"text": "Le chat (LLM)."}}}))
        self.click(app, "Reload saved work")
        # Drive the preset to the stub so the engine stays offline.
        preset_box = next(s for s in app.selectbox
                          if s.label == "Provider preset")
        preset_box.set_value("Test backend (stub)")
        self._run(app)
        self.assertEqual(app.session_state.remote_model_preset,
                         "Test backend (stub)")
        seen, spy_patch = self._spy_rerun_prompt()
        with spy_patch, mock.patch.object(
                engines.llm, "LLMClient",
                mock.Mock(side_effect=AssertionError(
                    "a non-stub engine was constructed; "
                    "the test must stay offline"))):
            self.fake.processing = self._real_processing_client()
            # 1. Edit + comment k1 (row 0 of batch 0) via the UI.
            self.edit_row_text(app, 0, "Le chat humain.")
            self.edit_row_comment(app, 0, "use the human register")
            # 2. Apply the batch's edits as a correction (via the UI button).
            self.click(app, "Apply Changes & Calculate Diffs")
            snap = self.repo.rows[DOC_A].snapshot
            self.assertTrue(
                any(c["segment_id"] == "u1"
                    and c["human_final"] == "Le chat humain."
                    and c["batch_index"] == 0
                    for c in snap.get("corrections", [])),
                "Apply must persist the batch-0 correction")
            # 3. Navigate to batch 1 and re-run its row k3: the re-run's
            #    assembled prompt MUST carry the batch-0 correction, because the
            #    per-row Rerun now carries the corrections_block like the batch
            #    Process does (a batch cannot correct itself, but a later batch
            #    is guided by the earlier one).
            self.click(app, "Next")
            self.click(app, "Rerun")
            prompt = seen.get("prompt")
            self.assertIsNotNone(prompt, "the re-run must send a prompt")
            self.assertIn("HUMAN CORRECTIONS FROM EARLIER BATCHES", prompt)
            self.assertIn('<u1: "Le chat humain.">', prompt)
            self.assertIn('rejected: "Le chat (LLM)."', prompt)
            self.assertIn("use the human register", prompt,
                          "the operator's comment must reach the correction")

    def test_rerun_uses_current_batch_stage_framing_in_nonfirst_batch(self):
        # The per-row Rerun must be staged with the *current* batch's stage and
        # framing, not the single-segment "mt1" / "1 of 1" default a batch-less
        # re-run would compute. Drive a 3-row document at batch size 2
        # (batch 0 = [k1, k2], batch 1 = [k3]), navigate to batch 1, and re-run
        # its row k3: the assembled prompt must carry batch 1's framing "2 of 2"
        # (batch_index=1, batch_count=2), the same values stage_for_batch(1, 2)
        # uses to pick the closing "mt3" stage - so the framing proves the
        # current batch's stage and framing reached the service. The default
        # stage templates carry no {{BATCH_NUMBER}}/{{BATCH_COUNT}} tokens, so
        # the framing is asserted through a template override that renders them.
        # The re-run stays a single-segment operation="rerun".
        self.install(segments_by_original={ORIG_A: _segments_a()})
        app = AppTest.from_file(os.path.join(ROOT, "app.py"),
                                default_timeout=120)
        self._run(app)  # bootstrap (seeds the default size)
        app.session_state.remote_batch_size = 2
        app.file_uploader[0].upload("a.mqxliff", b"<a/>",
                                    mime_type="application/xml")
        self._run(app)  # open (binds at the chosen size)
        self._run(app)  # render
        self.assertEqual(app.session_state.remote_batch_size, 2,
                         "a held size must survive the open")
        self.assertEqual(app.session_state.remote_view.batch_count, 2,
                         "3 rows at size 2 must yield 2 batches")
        # Drive the preset to the stub so the engine stays offline.
        preset_box = next(s for s in app.selectbox
                          if s.label == "Provider preset")
        preset_box.set_value("Test backend (stub)")
        self._run(app)
        self.assertEqual(app.session_state.remote_model_preset,
                         "Test backend (stub)")
        seen, spy_patch = self._spy_rerun_prompt()
        with spy_patch, mock.patch.object(
                engines.llm, "LLMClient",
                mock.Mock(side_effect=AssertionError(
                    "a non-stub engine was constructed; "
                    "the test must stay offline"))):
            self.fake.processing = self._real_processing_client()
            # Navigate to batch 1 (its row k3) and render a framing override that
            # surfaces the current batch's index/count, then re-run the row.
            self.click(app, "Next")
            self.input_text_area(
                app, "Stage template override",
                "OVERRIDE-FRAME batch={{BATCH_NUMBER}} of {{BATCH_COUNT}}")
            self.click(app, "Rerun")
            prompt = seen.get("prompt")
            self.assertIsNotNone(prompt, "the re-run must send a prompt")
            # The current batch is 1 of 2, so the framing must read "2 of 2"
            # (batch_index=1, batch_count=2) - not the single-segment "1 of 1"
            # a batch-less re-run would compute. That framing is exactly the
            # values stage_for_batch uses, so it also pins the closing "mt3"
            # stage (a batch-less re-run would compute stage "mt1").
            self.assertIn(
                "OVERRIDE-FRAME batch=2 of 2", prompt,
                "a batch-1 re-run must carry the current batch's framing "
                "and stage, not the single-segment default")
            self.assertNotIn(
                "OVERRIDE-FRAME batch=1 of 1", prompt,
                "a batch-1 re-run must not fall back to the single-segment "
                "'1 of 1' framing")

    def test_analyze_button_persists_critique_without_overwriting_translation(self):
        # The PLAN's "Analyze as a separate action": the "Analyse my
        # corrections" button (distinct from "Apply Changes & Calculate
        # Diffs" and "Copy proposal") reads the *stored* corrections a prior
        # apply made, asks the processing service for a critique, and persists
        # it in the workspace's separate analysis slot - never overwriting the
        # operator's translation, the model's proposal or the original target.
        # Driven through the real UI contract; only the apply and the analysis
        # are service calls, through the real client seam.
        self.install()
        app = self.start()
        view = app.session_state.remote_view
        self.assertFalse(view.is_empty)

        # Seed the model's proposal and a differing saved human edit, so an
        # Apply *can* store a correction the analysis then critiques.
        rev = self.revision()
        self.service.record_suggestions(
            DOC_A, parse_suggestions_request(
                {"expected_revision": rev,
                 "suggestions": {"k1": {"text": "Le chat (LLM)."}}}))
        self.click(app, "Reload saved work")
        self.edit_row_text(app, 0, "Le chat humain.")
        self.click(app, "Apply Changes & Calculate Diffs")
        snap = self.repo.rows[DOC_A].snapshot
        self.assertTrue(
            any(c["segment_id"] == "u1"
                and c["human_final"] == "Le chat humain."
                and c["batch_index"] == 0
                for c in snap.get("corrections", [])),
            "Apply must persist the batch-0 correction the analysis critiques")

        # Drive the preset to the stub so the engine stays offline.
        preset_box = next(s for s in app.selectbox
                          if s.label == "Provider preset")
        preset_box.set_value("Test backend (stub)")
        self._run(app)
        with mock.patch.object(
                engines.llm, "LLMClient",
                mock.Mock(side_effect=AssertionError(
                    "a non-stub engine was constructed; "
                    "the test must stay offline"))):
            self.fake.processing = self._real_processing_client()
            # The analysis reads the stored corrections (a prior apply), not
            # held drafts, so no new edit is needed.
            self.click(app, "Analyse my corrections")

        stored = self.repo.rows[DOC_A].snapshot
        # The critique lives in the separate analysis slot, keyed by batch.
        self.assertIn("0", stored.get("analysis", {}))
        critique = stored["analysis"]["0"]
        self.assertIn("TRANSLATION BRIEF", critique,
                      "the processing service must have computed a brief")
        # The operator's translation, the model's proposal and the original
        # target are untouched by the analysis.
        row = self.stored("k1")
        self.assertEqual(row["user_final"], "Le chat humain.")
        self.assertEqual(row["llm_suggestion"], "Le chat (LLM).")


class RemoteSetBatchSizeTestCase(_RemoteEditMixin, unittest.TestCase):
    """Explicit, revision-checked re-batch of a live workspace.

    Drives the operator's "Change batch size" button (``set_remote_batch_size``)
    end-to-end: a real size change re-slices the document and starts a new
    layout (``layout_version`` bumps), a same-size request is a no-op, and a
    stale revision is rejected with nothing mutated. Corrections keep their
    original ``batch_index`` and the ``layout_version`` they were produced
    under, so the new layout's feedback never picks up an earlier layout's
    correction even when the batch indices collide, and a later apply in the
    new layout cannot erase the old layout's history.
    """

    def open_with_batch(self, segments, batch_size, filename="a.mqxliff",
                        content=b"<a/>"):
        """Open a document, binding it at a chosen working batch size."""
        self.install(segments_by_original={ORIG_A: segments})
        app = AppTest.from_file(os.path.join(ROOT, "app.py"),
                                default_timeout=120)
        self._run(app)  # bootstrap (seeds the default size)
        app.session_state.remote_batch_size = batch_size
        app.file_uploader[0].upload(filename, content,
                                    mime_type="application/xml")
        self._run(app)  # open (binds at the chosen size)
        self._run(app)  # render
        return app

    def _open_23_at_100(self):
        """Open a 23-row document at the default batch size 100."""
        return self.open_with_batch(_segments_23(), 100)

    def _reload_view(self, app, segments=None):
        """Reload the view from the server after a rebatch.

        After a "Change batch size" click, ``set_remote_batch_size`` has already
        adopted the fresh view into ``st.session_state.remote_view``; the
        operator's drafts survive by key through ``_adopt_view``. We return that
        live view (the same one ``_adopt_view`` installed) rather than
        re-fetching, so the test reads what the UI would next render.
        """
        return app.session_state.remote_view

    def test_change_batch_size_100_to_10_starts_new_layout(self):
        # A real size change (100→10) re-slices the document into 3 batches
        # (10/10/3) and starts a new layout (layout_version bumps to 1).
        app = self._open_23_at_100()
        view = app.session_state.remote_view
        self.assertEqual(view.batch_size, 100)
        self.assertEqual(view.batch_count, 1)
        self.assertEqual(view.layout_version, 0)
        # Drive the "Change batch size" button: set the held value to 10,
        # then click. The button calls set_remote_batch_size(10).
        self._run(app)  # the number_input writes the held value
        # Find the batch size input and set it to 10
        batch_input = next(i for i in app.number_input if i.label == "Batch size")
        batch_input.set_value(10)
        self.click(app, "Change batch size")
        new_view = self._reload_view(app)
        self.assertEqual(new_view.batch_size, 10)
        self.assertEqual(new_view.batch_count, 3)
        self.assertEqual(new_view.layout_version, 1)
        self.assertEqual(len(new_view.batch_records(0)), 10)
        self.assertEqual(len(new_view.batch_records(1)), 10)
        self.assertEqual(len(new_view.batch_records(2)), 3)
        # The success message confirms the re-batch.
        self.assertTrue(
            any(k == "success" and "Re-batched" in m
                for k, m in self.messages(app)),
            "a real size change must report the re-batch")

    def test_correction_in_old_layout_excluded_from_new_layout_prompt(self):
        # 100→10 on 23 rows: a correction on row 23 (k23, the last row of
        # batch 0 at size 100) stays in history with batch_index 0 and
        # layout_version 0, but does NOT appear in the new layout's batch 2
        # prompt (batch 2 at size 10 is the last batch with 3 rows).
        app = self._open_23_at_100()
        # Seed the model's proposal for k23 so the Apply has a diff.
        rev = self.revision()
        self.service.record_suggestions(
            DOC_A, parse_suggestions_request(
                {"expected_revision": rev,
                 "suggestions": {"k23": {"text": "Le chat (LLM)."}}}))
        self.click(app, "Reload saved work")
        # Edit k23 (row 22, 0-indexed) and Apply batch 0.
        self.edit_row_text(app, 22, "Le chat humain.")
        self.click(app, "Apply Changes & Calculate Diffs")
        snap = self.repo.rows[DOC_A].snapshot
        corrections = snap.get("corrections", [])
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["batch_index"], 0)
        self.assertEqual(corrections[0]["layout_version"], 0)
        self.assertEqual(corrections[0]["human_final"], "Le chat humain.")
        # Now change the batch size to 10 (starts layout 1).
        self._run(app)
        batch_input = next(i for i in app.number_input if i.label == "Batch size")
        batch_input.set_value(10)
        self.click(app, "Change batch size")
        new_view = self._reload_view(app)
        self.assertEqual(new_view.layout_version, 1)
        self.assertEqual(new_view.batch_count, 3)
        # The old correction is still in history.
        reread = self.fake.workspace.get_workspace(DOC_A)
        self.assertEqual(len(reread.get("corrections", [])), 1)
        self.assertEqual(reread["corrections"][0]["layout_version"], 0)
        # The new layout's corrections_block for batch 2 must NOT include
        # the old correction.
        reread_view = RemoteWorkspaceView.from_remote(
            _segments_23(), reread)
        self.assertEqual(reread_view.layout_version, 1)
        self.assertEqual(reread_view.corrections_block(upto_batch=2), "",
                         "an old layout's correction must not leak into the "
                         "new layout's feedback")

    def test_return_to_original_size_does_not_reactivate_old_correction(self):
        # 10→100: a correction from layout 0 does NOT auto-activate in
        # layout 1. The new layout's feedback is empty until a new apply.
        app = self.open_with_batch(_segments_23(), 10)
        view = app.session_state.remote_view
        self.assertEqual(view.batch_size, 10)
        self.assertEqual(view.batch_count, 3)
        # Apply a correction on batch 2 (the last batch with 3 rows).
        # Navigate to batch 2 first.
        app.session_state.remote_batch = 2
        self._run(app)
        rev = self.revision()
        self.service.record_suggestions(
            DOC_A, parse_suggestions_request(
                {"expected_revision": rev,
                 "suggestions": {"k23": {"text": "Le chat (LLM)."}}}))
        self.click(app, "Reload saved work")
        # k23 is the 3rd row of batch 2 (index 2, 0-indexed).
        self.edit_row_text(app, 2, "Le chat humain.")
        self.click(app, "Apply Changes & Calculate Diffs")
        snap = self.repo.rows[DOC_A].snapshot
        corrections = snap.get("corrections", [])
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["batch_index"], 2)
        self.assertEqual(corrections[0]["layout_version"], 0)
        # Now change the batch size back to 100 (starts layout 1).
        self._run(app)
        batch_input = next(i for i in app.number_input if i.label == "Batch size")
        batch_input.set_value(100)
        self.click(app, "Change batch size")
        new_view = self._reload_view(app)
        self.assertEqual(new_view.batch_size, 100)
        self.assertEqual(new_view.batch_count, 1)
        self.assertEqual(new_view.layout_version, 1)
        # The old correction is still in history.
        reread = self.fake.workspace.get_workspace(DOC_A)
        self.assertEqual(len(reread.get("corrections", [])), 1)
        self.assertEqual(reread["corrections"][0]["layout_version"], 0)
        # But it does NOT appear in the new layout's feedback.
        reread_view = RemoteWorkspaceView.from_remote(
            _segments_23(), reread)
        self.assertEqual(reread_view.layout_version, 1)
        self.assertEqual(reread_view.corrections_block(upto_batch=1), "",
                         "a correction from an earlier layout must not "
                         "auto-activate in a new layout")

    def test_stale_revision_does_not_mutate_or_start_layout(self):
        # A concurrent write between the view's read and the "Change batch
        # size" click makes the revision stale. The set_batch_size must be
        # rejected (409) with nothing mutated and no layout started.
        app = self._open_23_at_100()
        view = app.session_state.remote_view
        view_rev = view.revision
        # A concurrent change advances the revision.
        self.other_writer({"k1": {"text": "Concurrent edit."}})
        advanced = self.revision()
        self.assertEqual(advanced, view_rev + 1)
        # Click "Change batch size" with the stale revision.
        self._run(app)
        batch_input = next(i for i in app.number_input if i.label == "Batch size")
        batch_input.set_value(10)
        self.click(app, "Change batch size")
        # The revision is still the concurrent write's; no layout was started.
        self.assertEqual(self.revision(), advanced)
        self.assertTrue(
            app.session_state.remote_process_uncertain,
            "a stale set_batch_size leaves the run uncertain")
        self.assertTrue(
            any(k == "error" and "Reload to reconcile" in m
                for k, m in self.messages(app)),
            "a stale set_batch_size must tell the operator to Reload")

    def test_drafts_survive_rebatch_by_key(self):
        # A held draft on k1 survives a 100→10 re-batch by segment key.
        app = self._open_23_at_100()
        # Hold a draft on k1.
        self.edit_row_text(app, 0, "draft on k1")
        self.assertEqual(self.drafts(app)["k1"]["text"], "draft on k1")
        # Change the batch size to 10.
        self._run(app)
        batch_input = next(i for i in app.number_input if i.label == "Batch size")
        batch_input.set_value(10)
        self.click(app, "Change batch size")
        new_view = self._reload_view(app)
        self.assertEqual(new_view.batch_size, 10)
        self.assertEqual(new_view.layout_version, 1)
        # The draft survived by key.
        self.assertEqual(self.drafts(app)["k1"]["text"], "draft on k1")

    def test_same_size_is_no_op_and_keeps_revision(self):
        # A same-size "Change batch size" is a no-op: no re-slice, no layout
        # bump, no revision change.
        app = self._open_23_at_100()
        rev = self.revision()
        # Set the held value to 100 (same as current) and click.
        self._run(app)
        batch_input = next(i for i in app.number_input if i.label == "Batch size")
        batch_input.set_value(100)
        self.click(app, "Change batch size")
        new_view = self._reload_view(app)
        self.assertEqual(new_view.batch_size, 100)
        self.assertEqual(new_view.layout_version, 0)
        self.assertEqual(new_view.batch_count, 1)
        self.assertEqual(self.revision(), rev,
                         "a same-size change must not advance the revision")
        self.assertTrue(
            any(k == "info" and "no new layout" in m
                for k, m in self.messages(app)),
            "a same-size change must report a no-op")

    def test_apply_in_new_layout_does_not_erase_old_history(self):
        # Apply in layout 0 (batch 0), re-batch to size 10 (layout 1), then
        # apply in layout 1 (batch 0). The old layout's correction must
        # survive; the new apply only replaces layout 1's batch 0 corrections.
        app = self._open_23_at_100()
        rev = self.revision()
        self.service.record_suggestions(
            DOC_A, parse_suggestions_request(
                {"expected_revision": rev,
                 "suggestions": {"k1": {"text": "Layout 0 proposal."}}}))
        self.click(app, "Reload saved work")
        self.edit_row_text(app, 0, "Layout 0 edit.")
        self.click(app, "Apply Changes & Calculate Diffs")
        snap = self.repo.rows[DOC_A].snapshot
        self.assertEqual(len(snap.get("corrections", [])), 1)
        self.assertEqual(snap["corrections"][0]["layout_version"], 0)
        self.assertEqual(snap["corrections"][0]["human_final"], "Layout 0 edit.")
        # Re-batch to size 10 (starts layout 1).
        self._run(app)
        batch_input = next(i for i in app.number_input if i.label == "Batch size")
        batch_input.set_value(10)
        self.click(app, "Change batch size")
        self.assertEqual(self._reload_view(app).layout_version, 1)
        # Apply in layout 1 (batch 0): a different edit on the same row.
        rev = self.revision()
        self.service.record_suggestions(
            DOC_A, parse_suggestions_request(
                {"expected_revision": rev,
                 "suggestions": {"k1": {"text": "Layout 1 proposal."}}}))
        self.click(app, "Reload saved work")
        self.edit_row_text(app, 0, "Layout 1 edit.")
        self.click(app, "Apply Changes & Calculate Diffs")
        snap = self.repo.rows[DOC_A].snapshot
        corrections = snap.get("corrections", [])
        # Both corrections survive.
        self.assertEqual(len(corrections), 2)
        layout_0 = [c for c in corrections if c["layout_version"] == 0]
        layout_1 = [c for c in corrections if c["layout_version"] == 1]
        self.assertEqual(len(layout_0), 1)
        self.assertEqual(len(layout_1), 1)
        self.assertEqual(layout_0[0]["human_final"], "Layout 0 edit.")
        self.assertEqual(layout_1[0]["human_final"], "Layout 1 edit.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
