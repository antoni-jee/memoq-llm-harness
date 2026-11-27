"""Unit tests for workspace-service (offline; fake repository).

The fake repository exists only for these tests; the runtime backend is
PostgresWorkspaceRepository, covered by tests/test_workspace_service_pg.py.

Run with:  python -m unittest -v tests.test_workspace_service
"""

from __future__ import annotations

import copy
import io
import json
import logging
import os
import sys
import threading
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient  # noqa: E402

from memoq_harness import mqxliff, state as state_mod  # noqa: E402
from services.workspace_service import config, migrate  # noqa: E402
from services.workspace_service.app import create_app  # noqa: E402
from services.workspace_service.repository import (  # noqa: E402
    DB_STATUS_NOT_MIGRATED, DB_STATUS_OK, DB_STATUS_UNAVAILABLE, RepositoryError,
    WorkspaceRow,
)
from services.workspace_service.service import (  # noqa: E402
    FIXED_BATCH_SIZE, WorkspaceService, _MemoryState,
)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "sample.mqxliff")
with open(FIXTURE, "rb") as _handle:
    SAMPLE = _handle.read()
VARIANT = SAMPLE.replace(b"<target>Das Dashboard zeigt", b"<target>Das Dashboard bietet", 1)

DOC_A = "a" * 64
DOC_B = "b" * 64
ORIG_1 = "1" * 64
ORIG_2 = "2" * 64
SECRET_TEXT = "TOPSECRET-WORKSPACE-TEXT"


def bind_body(raw=SAMPLE, original_id=ORIG_1, tag_source="target", seg_filter=None,
              source_file="sample.mqxliff", batch_size=None):
    """What a caller builds from document-service's /segments response."""
    doc = mqxliff.MqxliffDocument.from_bytes(raw, source_file)
    segments = doc.parse_segments(seg_filter, tag_source)
    body = {
        "original_id": original_id,
        "source_file": source_file,
        "tag_source": tag_source,
        "segments": [{"key": s.key, "unit_id": s.unit_id, "source_text": s.source_text,
                      "target_text": s.target_text}
                     for s in segments if s.translatable],
        "present_keys": [s.key for s in segments],
    }
    if batch_size is not None:
        body["batch_size"] = batch_size
    return body


class FakeRepository:
    """In-memory stand-in with PostgresWorkspaceRepository's contract."""

    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()
        self.status_value = DB_STATUS_OK
        self.fail = False
        self.writes = 0

    def _check(self):
        if self.fail:
            raise RepositoryError("Database operation failed.")

    def status(self, expected_version):
        return self.status_value

    def get(self, document_id):
        self._check()
        return self.rows.get(document_id)

    def transact(self, document_id, decide):
        self._check()
        with self.lock:
            current = self.rows.get(document_id)
            new, payload = decide(current)
            if new is None:
                return current, payload, False
            if current is not None:
                assert new.revision == current.revision + 1
            now = datetime.now(timezone.utc)
            stored = WorkspaceRow(
                document_id=document_id,
                snapshot=json.loads(json.dumps(new.snapshot)),  # JSONB-like round trip
                state_version=new.state_version, revision=new.revision,
                tag_source=new.tag_source, bound_original_id=new.bound_original_id,
                created_at=current.created_at if current else now, updated_at=now)
            self.rows[document_id] = stored
            self.writes += 1
            return stored, payload, True


class WorkspaceTestCase(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepository()
        self.service = WorkspaceService(self.repo, "0001", max_body_bytes=2 * 1024 * 1024)
        self.client = TestClient(create_app(self.service), raise_server_exceptions=False)
        # The in-memory state must never touch files.
        patcher = mock.patch.object(state_mod.ProjectState, "save",
                                    side_effect=AssertionError("save() must not be called"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.client.close()

    def bind(self, document_id=DOC_A, **kwargs):
        body = kwargs.pop("body") if "body" in kwargs else bind_body(**kwargs)
        return self.client.post("/workspaces/%s/bind" % document_id, json=body)

    def get(self, document_id=DOC_A):
        return self.client.get("/workspaces/%s" % document_id)

    def patch(self, segments, expected_revision=None, document_id=DOC_A):
        if expected_revision is None:
            expected_revision = self.get(document_id).json()["revision"]
        return self.client.patch("/workspaces/%s/segments" % document_id,
                                 json={"expected_revision": expected_revision,
                                       "segments": segments})

    def record(self, key, document_id=DOC_A):
        return self.get(document_id).json()["segments"][key]

    def assert_unchanged(self, before, document_id=DOC_A):
        self.assertEqual(self.repo.rows.get(document_id), before)


class TestBind(WorkspaceTestCase):
    def test_new_bind_creates_revision_1_matching_the_segments(self):
        body = bind_body()
        response = self.bind()
        self.assertEqual(response.status_code, 201, response.text)
        summary = response.json()
        self.assertTrue(summary["created"])
        self.assertTrue(summary["changed"])
        self.assertEqual(summary["revision"], 1)
        self.assertEqual(summary["bound_original_id"], ORIG_1)
        self.assertEqual(summary["batch_size"], FIXED_BATCH_SIZE)
        self.assertEqual(summary["order"], [s["key"] for s in body["segments"]])

        workspace = self.get().json()
        self.assertEqual(workspace["tag_source"], "target")
        self.assertEqual(workspace["state_version"], state_mod.STATE_VERSION)
        for item in body["segments"]:
            record = workspace["segments"][item["key"]]
            self.assertEqual(record["source_text"], item["source_text"])
            self.assertEqual(record["mt_target"], item["target_text"])
            self.assertEqual(record["segment_id"], item["unit_id"])
            self.assertEqual(record["user_final"], "")
            self.assertFalse(record["edited_by_human"])

    def test_identical_rebind_is_a_no_op(self):
        self.bind()
        before = self.repo.rows[DOC_A]
        writes = self.repo.writes
        again = self.bind()
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.json()["created"])
        self.assertFalse(again.json()["changed"])
        self.assertEqual(again.json()["revision"], 1)
        self.assertEqual(self.repo.writes, writes)
        self.assertIs(self.repo.rows[DOC_A], before)

    def test_rebind_from_another_original_keeps_work_and_refreshes_mt(self):
        self.bind()
        self.assertEqual(self.patch({"2#1": {"text": "Meine Fassung", "comment": "Notiz"}})
                         .status_code, 200)
        response = self.bind(raw=VARIANT, original_id=ORIG_2)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["revision"], 3)
        self.assertEqual(response.json()["bound_original_id"], ORIG_2)
        record = self.record("2#1")
        self.assertEqual(record["user_final"], "Meine Fassung")
        self.assertEqual(record["user_comment"], "Notiz")
        self.assertTrue(record["edited_by_human"])
        self.assertIn("bietet", record["mt_target"])

    def test_changing_only_the_original_increments_revision(self):
        self.bind()
        response = self.bind(original_id=ORIG_2)
        self.assertEqual(response.json()["revision"], 2)
        self.assertTrue(response.json()["changed"])

    def test_new_bind_persists_the_chosen_batch_size(self):
        response = self.bind(body=bind_body(batch_size=10))
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["batch_size"], 10)
        # The chosen size is what the snapshot persists (the server is the
        # source of truth the view, Process and Apply read from).
        self.assertEqual(self.get().json()["batch_size"], 10)

    def test_repeat_bind_preserves_the_persisted_batch_size(self):
        # A re-open without an explicit size change must not reset the stored
        # 10 to the default 100 - checked separately from Reload, which is a
        # read-only re-read.
        self.assertEqual(self.bind(body=bind_body(batch_size=10)).status_code, 201)
        before = self.repo.rows[DOC_A]
        again = self.bind()  # default body: no batch_size sent
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.json()["changed"])
        self.assertEqual(again.json()["revision"], 1)
        self.assertEqual(self.repo.writes, 1)
        self.assertIs(self.repo.rows[DOC_A], before)
        self.assertEqual(self.get().json()["batch_size"], 10)

    def test_repeat_bind_ignores_the_requested_batch_size(self):
        # The re-bind recovers the persisted size, never the request's, so a
        # fresh request carrying a different size cannot silently reshape the
        # existing workspace (that is the separate set_batch_size operation).
        self.assertEqual(self.bind(body=bind_body(batch_size=10)).status_code, 201)
        before = self.repo.rows[DOC_A]
        again = self.bind(body=bind_body(batch_size=50))
        self.assertEqual(again.status_code, 200, again.text)
        self.assertFalse(again.json()["changed"])
        self.assertEqual(self.repo.writes, 1)
        self.assertIs(self.repo.rows[DOC_A], before)
        self.assertEqual(self.get().json()["batch_size"], 10)

    def test_batch_size_must_be_a_positive_integer(self):
        for bad in (0, -1, True, "10", 1.5, None):
            with self.subTest(batch_size=bad):
                body = bind_body()
                body["batch_size"] = bad
                response = self.bind(body=body)
                self.assertEqual(response.status_code, 400,
                                 "%r should be rejected" % (bad,))

    def test_narrower_selection_keeps_hidden_work(self):
        relaxed = bind_body(seg_filter=mqxliff.SegmentFilter(skip_confirmed=False))
        self.assertEqual(self.bind(body=relaxed).status_code, 201)
        self.patch({"3#2": {"text": "Bestaetigte Zeile neu"}})
        narrowed = self.bind()  # default filter hides confirmed row 3#2
        self.assertEqual(narrowed.status_code, 200)
        self.assertNotIn("3#2", narrowed.json()["order"])
        self.assertEqual(narrowed.json()["revision"], 3)  # selection changed
        self.assertEqual(self.record("3#2")["user_final"], "Bestaetigte Zeile neu")

    def test_comment_only_work_is_protected(self):
        self.bind()
        self.patch({"1#0": {"comment": "nur eine Notiz"}})
        self.assertFalse(self.record("1#0")["edited_by_human"])
        before = self.repo.rows[DOC_A]
        body = bind_body()
        body["present_keys"] = [k for k in body["present_keys"] if k != "1#0"]
        body["segments"] = [s for s in body["segments"] if s["key"] != "1#0"]
        response = self.bind(body=body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "would_discard_work")
        self.assertEqual(response.json()["details"]["keys"], ["1#0"])
        self.assert_unchanged(before)
        # Rebinding normally keeps the comment-only work.
        self.assertEqual(self.bind().status_code, 200)
        self.assertEqual(self.record("1#0")["user_comment"], "nur eine Notiz")

    def test_changed_source_text_with_work_is_refused(self):
        self.bind()
        self.patch({"1#0": {"text": "Eigene Fassung"}})
        before = self.repo.rows[DOC_A]
        body = bind_body()
        body["segments"][0]["source_text"] = "Different source {1}x{2}"
        response = self.bind(body=body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "would_discard_work")
        self.assert_unchanged(before)

    def test_tag_source_mismatch_is_refused(self):
        self.bind()
        before = self.repo.rows[DOC_A]
        response = self.bind(tag_source="source")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "tag_source_mismatch")
        self.assertEqual(response.json()["details"],
                         {"stored": "target", "requested": "source"})
        self.assert_unchanged(before)

    def test_source_tag_mode_round_trip(self):
        self.assertEqual(self.bind(DOC_B, tag_source="source").status_code, 201)
        self.assertEqual(self.get(DOC_B).json()["tag_source"], "source")
        self.assertFalse(self.bind(DOC_B, tag_source="source").json()["changed"])

    def test_invalid_bind_requests_change_nothing(self):
        self.bind()
        before = self.repo.rows[DOC_A]
        good = bind_body()
        first = dict(good["segments"][0])

        def variant(**changes):
            body = copy.deepcopy(good)
            body.update(changes)
            return body

        cases = {
            "not object": [],
            "unknown field": variant(extra=1),
            "missing original": {k: v for k, v in good.items() if k != "original_id"},
            "bad original": variant(original_id="xyz"),
            "bad tag_source": variant(tag_source="both"),
            "bad source_file": variant(source_file=5),
            "segments not list": variant(segments={}),
            "segment missing field": variant(segments=[{"key": "1#0"}]),
            "segment extra field": variant(segments=[dict(first, extra="x")]),
            "segment non-string": variant(segments=[dict(first, source_text=None)]),
            "empty key": variant(segments=[dict(first, key="")]),
            "duplicate segment keys": variant(segments=[first, first]),
            "present_keys not list": variant(present_keys="1#0"),
            "present_keys non-string": variant(present_keys=good["present_keys"] + [3]),
            "duplicate present_keys": variant(present_keys=good["present_keys"] + ["1#0"]),
            "segment not present": variant(present_keys=good["present_keys"][1:]),
        }
        for label, body in cases.items():
            with self.subTest(label):
                response = self.bind(body=body)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"], "bad_request")
                self.assert_unchanged(before)
        self.assertEqual(self.client.post("/workspaces/%s/bind" % ("A" * 64),
                                          json=good).status_code, 400)
        self.assertEqual(self.client.post("/workspaces/%s/bind" % DOC_A, content=b"x",
                                          headers={"content-type": "text/plain"}).status_code,
                         415)


class TestSave(WorkspaceTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.bind().status_code, 201)

    def test_text_and_comment_are_saved_through_capture_edits(self):
        response = self.patch({"1#0": {"text": "Klicken Sie {1}Sichern{2}.",
                                       "comment": "Sichern statt Speichern"}}, 1)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"document_id": DOC_A, "revision": 2,
                                           "updated_keys": ["1#0"], "changed": True})
        record = self.record("1#0")
        self.assertEqual(record["user_final"], "Klicken Sie {1}Sichern{2}.")
        self.assertEqual(record["user_comment"], "Sichern statt Speichern")
        self.assertTrue(record["edited_by_human"])

    def test_text_equal_to_mt_is_not_a_human_edit(self):
        mt = self.record("12#11")["mt_target"]
        self.patch({"12#11": {"text": mt}}, 1)
        record = self.record("12#11")
        self.assertEqual(record["user_final"], mt)
        self.assertFalse(record["edited_by_human"])

    def test_comment_only_update_keeps_text_and_flag(self):
        self.patch({"1#0": {"text": "Eigene Fassung"}}, 1)
        response = self.patch({"1#0": {"comment": "nur Kommentar"}}, 2)
        self.assertEqual(response.json()["revision"], 3)
        record = self.record("1#0")
        self.assertEqual(record["user_final"], "Eigene Fassung")
        self.assertTrue(record["edited_by_human"])
        self.assertEqual(record["user_comment"], "nur Kommentar")

    def test_comment_only_on_untouched_row_does_not_mark_an_edit(self):
        self.patch({"2#1": {"comment": "Hinweis"}}, 1)
        record = self.record("2#1")
        self.assertEqual(record["user_final"], "")
        self.assertFalse(record["edited_by_human"])
        self.assertEqual(record["user_comment"], "Hinweis")

    def test_empty_comment_clears_and_omitted_comment_keeps(self):
        self.patch({"1#0": {"text": "Eins", "comment": "alt"}}, 1)
        self.patch({"1#0": {"text": "Zwei"}}, 2)
        self.assertEqual(self.record("1#0")["user_comment"], "alt")
        self.patch({"1#0": {"comment": ""}}, 3)
        record = self.record("1#0")
        self.assertEqual(record["user_comment"], "")
        self.assertEqual(record["user_final"], "Zwei")

    def test_identical_save_keeps_the_revision(self):
        self.patch({"1#0": {"text": "Eins", "comment": "c"}}, 1)
        writes = self.repo.writes
        response = self.patch({"1#0": {"text": "Eins", "comment": "c"}}, 2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"document_id": DOC_A, "revision": 2,
                                           "updated_keys": [], "changed": False})
        self.assertEqual(self.repo.writes, writes)

    def test_save_is_atomic_and_checks_the_revision(self):
        before = self.repo.rows[DOC_A]
        stale = self.patch({"1#0": {"text": "x"}}, 7)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"], "stale_revision")
        self.assertEqual(stale.json()["details"],
                         {"expected_revision": 7, "current_revision": 1})
        self.assert_unchanged(before)

        unknown = self.patch({"1#0": {"text": "x"}, "99#99": {"text": "y"}}, 1)
        self.assertEqual(unknown.status_code, 422)
        self.assertEqual(unknown.json()["error"], "unknown_segment")
        self.assertEqual(unknown.json()["details"]["keys"], ["99#99"])
        self.assert_unchanged(before)  # the valid key was not applied either

    def test_lost_update_is_rejected(self):
        """Two clients read revision 1; the second save must not overwrite the first."""
        first = self.patch({"1#0": {"text": "Client A"}}, 1)
        second = self.patch({"1#0": {"text": "Client B"}}, 1)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(self.record("1#0")["user_final"], "Client A")

    def test_bind_between_read_and_save_makes_the_save_stale_not_lost(self):
        self.patch({"1#0": {"text": "Eins"}}, 1)
        self.bind(original_id=ORIG_2)  # revision 3
        self.assertEqual(self.patch({"2#1": {"text": "x"}}, 2).status_code, 409)
        self.assertEqual(self.patch({"2#1": {"text": "x"}}, 3).status_code, 200)
        self.assertEqual(self.record("1#0")["user_final"], "Eins")

    def test_invalid_patch_requests(self):
        before = self.repo.rows[DOC_A]
        url = "/workspaces/%s/segments" % DOC_A
        cases = [
            [], {}, {"segments": {"1#0": {"text": "x"}}},
            {"expected_revision": 0, "segments": {"1#0": {"text": "x"}}},
            {"expected_revision": True, "segments": {"1#0": {"text": "x"}}},
            {"expected_revision": "1", "segments": {"1#0": {"text": "x"}}},
            {"expected_revision": 1, "segments": {}},
            {"expected_revision": 1, "segments": []},
            {"expected_revision": 1, "segments": {"1#0": {}}},
            {"expected_revision": 1, "segments": {"1#0": "x"}},
            {"expected_revision": 1, "segments": {"1#0": {"text": 5}}},
            {"expected_revision": 1, "segments": {"1#0": {"text": "x", "status": "applied"}}},
            {"expected_revision": 1, "segments": {"1#0": {"text": "x"}}, "extra": 1},
        ]
        for body in cases:
            with self.subTest(body=repr(body)[:60]):
                response = self.client.patch(url, json=body)
                self.assertEqual(response.status_code, 400, response.text)
                self.assert_unchanged(before)

    def test_unknown_workspace_and_oversized_body(self):
        self.assertEqual(self.patch({"1#0": {"text": "x"}}, 1, DOC_B).status_code, 404)
        self.assertEqual(self.get(DOC_B).status_code, 404)
        self.service.max_body_bytes = 100
        response = self.patch({"1#0": {"text": "x" * 500}}, 1)
        self.assertEqual(response.status_code, 413)


class TestSuggestions(WorkspaceTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.bind().status_code, 201)

    def suggest(self, suggestions, expected_revision=None, document_id=DOC_A):
        if expected_revision is None:
            expected_revision = self.get(document_id).json()["revision"]
        return self.client.post("/workspaces/%s/suggestions" % document_id,
                                json={"expected_revision": expected_revision,
                                      "suggestions": suggestions})

    def test_unprotected_rows_are_prefilled_protected_rows_keep_text(self):
        self.patch({"1#0": {"text": "Meine Fassung"}}, 1)            # edited -> protected
        self.patch({"2#1": {"comment": "nur Notiz"}}, 2)             # comment only
        response = self.suggest({
            "1#0": {"text": "Modell eins"},
            "2#1": {"text": "Modell zwei"},
            "12#11": {"text": "Modell drei", "tag_report": "missing 2"},
        }, 3)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "document_id": DOC_A, "revision": 4, "changed": True,
            "recorded_keys": ["1#0", "12#11", "2#1"],
            "prefilled_keys": ["12#11", "2#1"],
            "protected_keys": ["1#0"]})

        protected = self.record("1#0")
        self.assertEqual((protected["user_final"], protected["llm_suggestion"],
                          protected["edited_by_human"], protected["status"]),
                         ("Meine Fassung", "Modell eins", True, "processed"))
        comment_only = self.record("2#1")
        self.assertEqual((comment_only["user_final"], comment_only["user_comment"],
                          comment_only["edited_by_human"]),
                         ("Modell zwei", "nur Notiz", False))
        third = self.record("12#11")
        self.assertEqual((third["user_final"], third["tag_report"], third["status"]),
                         ("Modell drei", "missing 2", "processed"))
        self.assertEqual(self.record("7#6")["status"], "pending")  # not named: untouched

    def test_result_equals_local_project_state_record_suggestions(self):
        self.patch({"1#0": {"text": "Meine Fassung", "comment": "c"}}, 1)
        row = self.repo.rows[DOC_A]
        suggestions = {"1#0": "Modell eins", "2#1": "  Modell zwei  "}
        reports = {"1#0": "", "2#1": "unknown 9"}
        reference = state_mod.ProjectState(os.path.join(os.sep, "never", "written.json"))
        reference.batch_size = FIXED_BATCH_SIZE
        reference.open_document_snapshot(DOC_A, row.snapshot)
        reference.record_suggestions(suggestions, reports)

        self.suggest({k: {"text": v, "tag_report": reports[k]} for k, v in suggestions.items()},
                     2)
        stored = self.repo.rows[DOC_A].snapshot
        expected = reference.document_snapshot()
        self.assertEqual(stored["segments"], expected["segments"])
        self.assertEqual(stored["order"], expected["order"])

    def test_accepted_text_is_stored_exactly(self):
        text = "  Leerzeichen bleiben\nund Zeilenumbruch {1}x{2}  "
        self.suggest({"1#0": {"text": text}}, 1)
        record = self.record("1#0")
        self.assertEqual(record["llm_suggestion"], text)
        self.assertEqual(record["user_final"], text)

    def test_empty_and_whitespace_text_are_rejected_atomically(self):
        before = self.repo.rows[DOC_A]
        for bad in ("", "   ", "\n\t "):
            with self.subTest(text=repr(bad)):
                response = self.suggest({"1#0": {"text": "gut"}, "2#1": {"text": bad}}, 1)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["details"]["keys"], ["2#1"])
                self.assert_unchanged(before)

    def test_revision_ordering(self):
        body = {"1#0": {"text": "Modell"}}
        first = self.suggest(body, 1)
        self.assertEqual((first.status_code, first.json()["revision"]), (200, 2))
        # Replay with the old revision: stale even though the content is identical.
        replay = self.suggest(body, 1)
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(replay.json()["error"], "stale_revision")
        self.assertEqual(replay.json()["details"]["current_revision"], 2)
        # Identical content with the current revision: no-op, revision kept.
        writes = self.repo.writes
        same = self.suggest(body, 2)
        self.assertEqual(same.status_code, 200)
        self.assertFalse(same.json()["changed"])
        self.assertEqual(same.json()["revision"], 2)
        self.assertEqual(self.repo.writes, writes)
        # A different suggestion with the current revision increments it.
        changed = self.suggest({"1#0": {"text": "Modell neu"}}, 2)
        self.assertEqual((changed.json()["revision"], changed.json()["changed"]), (3, True))

    def test_stale_after_edit_or_rebind(self):
        read_revision = self.get().json()["revision"]
        self.patch({"1#0": {"text": "Mensch"}}, read_revision)
        before = self.repo.rows[DOC_A]
        stale = self.suggest({"2#1": {"text": "Modell"}}, read_revision)
        self.assertEqual(stale.status_code, 409)
        self.assert_unchanged(before)

        read_revision = self.get().json()["revision"]
        self.bind(original_id=ORIG_2)
        before = self.repo.rows[DOC_A]
        self.assertEqual(self.suggest({"2#1": {"text": "Modell"}}, read_revision).status_code,
                         409)
        self.assert_unchanged(before)

    def test_unknown_and_inactive_keys_reject_the_whole_request(self):
        before = self.repo.rows[DOC_A]
        unknown = self.suggest({"1#0": {"text": "ok"}, "99#99": {"text": "x"}}, 1)
        self.assertEqual(unknown.status_code, 422)
        self.assertEqual(unknown.json()["error"], "unknown_segment")
        self.assertEqual(unknown.json()["details"]["keys"], ["99#99"])
        self.assert_unchanged(before)

        # 3#2 is a confirmed row: present in the document, hidden by the filter.
        relaxed = bind_body(seg_filter=mqxliff.SegmentFilter(skip_confirmed=False))
        self.bind(body=relaxed)
        self.bind()  # narrow again: 3#2 kept but inactive
        self.assertIn("3#2", self.get().json()["segments"])
        before = self.repo.rows[DOC_A]
        inactive = self.suggest({"1#0": {"text": "ok"}, "3#2": {"text": "x"}})
        self.assertEqual(inactive.status_code, 422)
        self.assertEqual(inactive.json()["error"], "inactive_segment")
        self.assertEqual(inactive.json()["details"]["keys"], ["3#2"])
        self.assert_unchanged(before)

    def test_tag_report_is_set_and_cleared_as_record_suggestions_does(self):
        self.suggest({"1#0": {"text": "a", "tag_report": "missing 2"}}, 1)
        self.assertEqual(self.record("1#0")["tag_report"], "missing 2")
        self.suggest({"1#0": {"text": "a"}}, 2)
        self.assertEqual(self.record("1#0")["tag_report"], "")

    def test_invalid_suggestion_requests(self):
        before = self.repo.rows[DOC_A]
        url = "/workspaces/%s/suggestions" % DOC_A
        cases = [
            [], {}, {"suggestions": {"1#0": {"text": "x"}}},
            {"expected_revision": 0, "suggestions": {"1#0": {"text": "x"}}},
            {"expected_revision": False, "suggestions": {"1#0": {"text": "x"}}},
            {"expected_revision": 1, "suggestions": {}},
            {"expected_revision": 1, "suggestions": ["1#0"]},
            {"expected_revision": 1, "suggestions": {"1#0": "x"}},
            {"expected_revision": 1, "suggestions": {"1#0": {}}},
            {"expected_revision": 1, "suggestions": {"1#0": {"tag_report": "x"}}},
            {"expected_revision": 1, "suggestions": {"1#0": {"text": 5}}},
            {"expected_revision": 1, "suggestions": {"1#0": {"text": "x", "tag_report": 3}}},
            {"expected_revision": 1, "suggestions": {"1#0": {"text": "x", "status": "applied"}}},
            {"expected_revision": 1, "suggestions": {"1#0": {"text": "x",
                                                            "tag_report": "r" * 1001}}},
            {"expected_revision": 1, "suggestions": {"1#0": {"text": "x"}}, "extra": 1},
        ]
        for body in cases:
            with self.subTest(body=repr(body)[:60]):
                response = self.client.post(url, json=body)
                self.assertEqual(response.status_code, 400, response.text)
                self.assert_unchanged(before)
        self.assertEqual(self.client.post(url, content=b"x",
                                          headers={"content-type": "text/plain"}).status_code,
                         415)
        self.service.max_body_bytes = 100
        self.assertEqual(self.suggest({"1#0": {"text": "x" * 500}}, 1).status_code, 413)
        self.assert_unchanged(before)

    def test_unknown_workspace_isolation_and_unsupported_version(self):
        self.assertEqual(self.suggest({"1#0": {"text": "x"}}, 1, DOC_B).status_code, 404)
        self.bind(DOC_B, original_id=ORIG_2)
        self.suggest({"1#0": {"text": "Nur A"}}, 1, DOC_A)
        self.assertEqual(self.record("1#0", DOC_B)["llm_suggestion"], "")
        self.assertEqual(self.record("1#0", DOC_B)["user_final"], "")

        row = self.repo.rows[DOC_B]
        future = WorkspaceRow(**{**row.__dict__, "state_version": state_mod.STATE_VERSION + 1})
        self.repo.rows[DOC_B] = future
        response = self.suggest({"1#0": {"text": "x"}}, 1, DOC_B)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "unsupported_state_version")
        self.assertIs(self.repo.rows[DOC_B], future)

    def test_logs_and_errors_hold_no_suggestion_text(self):
        with self.assertLogs("workspace-service", level="DEBUG") as logs:
            ok = self.suggest({"1#0": {"text": SECRET_TEXT, "tag_report": SECRET_TEXT}}, 1)
            bad = self.suggest({"1#0": {"text": SECRET_TEXT}, "zz": {"text": SECRET_TEXT}}, 2)
            stale = self.suggest({"1#0": {"text": SECRET_TEXT}}, 1)
            logging.getLogger("workspace-service").debug("end")
        self.assertEqual(ok.status_code, 200)
        for response in (ok, bad, stale):
            self.assertNotIn(SECRET_TEXT, response.text)
        self.assertNotIn(SECRET_TEXT, "\n".join(logs.output))


class TestIsolationAndStorage(WorkspaceTestCase):
    def test_documents_with_identical_keys_stay_isolated(self):
        self.bind(DOC_A)
        self.bind(DOC_B, original_id=ORIG_2)
        self.patch({"1#0": {"text": "Nur A", "comment": "A"}}, 1, DOC_A)
        self.patch({"1#0": {"text": "Nur B"}}, 1, DOC_B)
        self.assertEqual(self.record("1#0", DOC_A)["user_final"], "Nur A")
        self.assertEqual(self.record("1#0", DOC_B)["user_final"], "Nur B")
        self.assertEqual(self.record("1#0", DOC_B)["user_comment"], "")
        self.assertEqual(self.get(DOC_A).json()["revision"], 2)
        self.assertEqual(self.get(DOC_B).json()["revision"], 2)

    def test_snapshot_is_the_project_state_document_entry(self):
        self.bind()
        self.patch({"1#0": {"text": "Eins", "comment": "c"}}, 1)
        row = self.repo.rows[DOC_A]
        reference = state_mod.ProjectState(os.path.join(os.sep, "never", "written.json"))
        reference.open_document_snapshot(DOC_A, row.snapshot)
        self.assertEqual(reference.document_snapshot(), row.snapshot)
        self.assertEqual(reference.segments["1#0"].user_final, "Eins")

    def test_unknown_snapshot_fields_survive_updates(self):
        self.bind()
        row = self.repo.rows[DOC_A]
        snapshot = copy.deepcopy(row.snapshot)
        snapshot["future_field"] = {"x": 1}
        snapshot["segments"]["1#0"]["future_record_field"] = True
        self.repo.rows[DOC_A] = WorkspaceRow(**{**row.__dict__, "snapshot": snapshot})
        self.patch({"1#0": {"text": "Eins"}}, 1)
        stored = self.repo.rows[DOC_A].snapshot
        self.assertEqual(stored["future_field"], {"x": 1})
        self.assertTrue(stored["segments"]["1#0"]["future_record_field"])

    def test_unsupported_state_version_is_refused_without_change(self):
        self.bind()
        row = self.repo.rows[DOC_A]
        future = WorkspaceRow(**{**row.__dict__, "state_version": state_mod.STATE_VERSION + 1})
        self.repo.rows[DOC_A] = future
        for response in (self.get(), self.bind(), self.patch({"1#0": {"text": "x"}}, 1)):
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["error"], "unsupported_state_version")
        self.assertIs(self.repo.rows[DOC_A], future)

    def test_memory_state_refuses_to_write_files(self):
        with mock.patch.object(state_mod.ProjectState, "save", lambda self: "x"):
            with self.assertRaises(RuntimeError):
                _MemoryState(os.path.join(os.sep, "x.json")).save()

    def test_database_failure_is_503_without_text(self):
        self.bind()
        self.repo.fail = True
        for response in (self.get(), self.bind(), self.patch({"1#0": {"text": "x"}}, 1)):
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["message"], "Database operation failed.")


class TestHealthAndLeaks(WorkspaceTestCase):
    def test_liveness_and_readiness(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/readyz").json(),
                         {"status": "ready", "checks": {"database": "ok"}})
        for value in (DB_STATUS_UNAVAILABLE, DB_STATUS_NOT_MIGRATED):
            self.repo.status_value = value
            response = self.client.get("/readyz")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["checks"]["database"], value)
            self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_logs_and_errors_never_contain_segment_text(self):
        with self.assertLogs("workspace-service", level="DEBUG") as logs:
            body = bind_body()
            body["source_file"] = SECRET_TEXT
            self.bind(body=body)
            ok = self.patch({"1#0": {"text": SECRET_TEXT, "comment": SECRET_TEXT}}, 1)
            bad = self.patch({"1#0": {"text": SECRET_TEXT}, "zz": {"text": SECRET_TEXT}}, 2)
            stale = self.patch({"1#0": {"text": SECRET_TEXT}}, 99)
            logging.getLogger("workspace-service").debug("end")
        self.assertEqual(ok.status_code, 200)
        for response in (ok, bad, stale):
            self.assertNotIn(SECRET_TEXT, response.text)
        self.assertNotIn(SECRET_TEXT, "\n".join(logs.output))

    def test_unexpected_error_is_generic(self):
        with mock.patch.object(self.service, "get", side_effect=RuntimeError(SECRET_TEXT)):
            with self.assertLogs("workspace-service", level="ERROR") as logs:
                response = self.get()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(SECRET_TEXT, response.text)
        self.assertNotIn(SECRET_TEXT, "\n".join(logs.output))


class TestConfigAndMigrations(unittest.TestCase):
    URL = "postgresql://ws:Pa55-secret@db.internal:6543/harness?sslmode=require"

    def test_settings_from_environment_and_defaults(self):
        s = config.load_settings({config.ENV_DATABASE_URL: self.URL})
        self.assertEqual((s.host, s.port, s.db_schema, s.log_level),
                         ("127.0.0.1", 8102, "workspace", "info"))
        self.assertEqual(s.database.sslmode, "require")
        self.assertNotIn("Pa55-secret", repr(s))
        s = config.load_settings({
            config.ENV_DATABASE_URL: self.URL, config.ENV_HOST: "0.0.0.0",
            config.ENV_PORT: "9002", config.ENV_DB_SCHEMA: "ws_x",
            config.ENV_MAX_BODY: "4096", config.ENV_LOG_LEVEL: "DEBUG"})
        self.assertEqual((s.host, s.port, s.db_schema, s.max_body_bytes, s.log_level),
                         ("0.0.0.0", 9002, "ws_x", 4096, "debug"))

    def test_invalid_settings_hide_the_password(self):
        cases = [
            {},
            {config.ENV_DATABASE_URL: "postgresql://ws:Pa55-secret@db:bad/x"},
            {config.ENV_DATABASE_URL: self.URL, config.ENV_DB_SCHEMA: "Bad-Name"},
            {config.ENV_DATABASE_URL: self.URL, config.ENV_PORT: "0"},
            {config.ENV_DATABASE_URL: self.URL, config.ENV_LOG_LEVEL: "loud"},
        ]
        for env in cases:
            with self.subTest(env=sorted(env)):
                with self.assertRaises(config.ConfigError) as ctx:
                    config.load_settings(env)
                self.assertNotIn("Pa55-secret", str(ctx.exception))

    def test_migration_file_and_lock_namespace(self):
        from services.common import migrations as runner
        migrations = migrate.load_migrations()
        self.assertEqual([v for v, _ in migrations], ["0001"])
        statements = runner.split_statements(runner.render(migrations[0][1], "ws_x"))
        self.assertEqual(len(statements), 1)
        self.assertIn('CREATE TABLE "ws_x".workspaces', statements[0])
        self.assertNotEqual(runner.lock_key("workspace", "x"), runner.lock_key("document", "x"))

    def test_migrate_cli_reports_config_errors(self):
        with mock.patch.dict(os.environ, {config.ENV_DATABASE_URL: ""}, clear=False), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(migrate.main([]), 2)
        self.assertIn("workspace-service migrate: configuration error: "
                      "WORKSPACE_DATABASE_URL is required.", err.getvalue())


class TestSetBatchSize(WorkspaceTestCase):
    """Explicit, revision-checked re-batch of a bound workspace.

    A real size change re-slices the document and starts a new layout
    (``layout_version`` bumps by one); a same-size request is a no-op that
    keeps the revision; a stale ``expected_revision`` is rejected with nothing
    mutated. Corrections keep their original ``batch_index`` and the
    ``layout_version`` they were produced under, so the new layout's feedback
    never picks up an earlier layout's correction even when the batch indices
    collide, and a later apply in the new layout cannot erase the old layout's
    history.
    """

    def setUp(self):
        super().setUp()
        self.assertEqual(self.bind(body=bind_body(batch_size=100)).status_code, 201)

    def _set_batch_size(self, expected_revision, batch_size, document_id=DOC_A):
        return self.client.post(
            "/workspaces/%s/set_batch_size" % document_id,
            json={"expected_revision": expected_revision,
                  "batch_size": batch_size})

    def _apply(self, batch_index, expected_revision=None, document_id=DOC_A):
        if expected_revision is None:
            expected_revision = self.get(document_id).json()["revision"]
        return self.client.post(
            "/workspaces/%s/apply" % document_id,
            json={"expected_revision": expected_revision,
                  "batch_index": batch_index})

    def test_stale_revision_is_rejected_without_mutation(self):
        before = self.repo.rows[DOC_A]
        response = self._set_batch_size(99, 10)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"], "stale_revision")
        self.assert_unchanged(before)

    def test_same_size_is_a_no_op_that_keeps_the_revision(self):
        rev = self.get().json()["revision"]
        before = self.repo.rows[DOC_A]
        response = self._set_batch_size(rev, 100)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["changed"])
        self.assertEqual(response.json()["layout_version"], 0)
        self.assertEqual(response.json()["batch_size"], 100)
        self.assertEqual(self.repo.writes, 1)
        self.assertIs(self.repo.rows[DOC_A], before)

    def test_real_change_bumps_layout_version_and_revision(self):
        rev = self.get().json()["revision"]
        response = self._set_batch_size(rev, 10)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["changed"])
        self.assertEqual(response.json()["layout_version"], 1)
        self.assertEqual(response.json()["batch_size"], 10)
        self.assertEqual(response.json()["revision"], rev + 1)
        # The re-sliced document has the new batch size.
        self.assertEqual(self.get().json()["batch_size"], 10)
        self.assertEqual(self.get().json()["layout_version"], 1)

    def test_corrections_from_earlier_layout_excluded_from_new_layout_feedback(self):
        # Apply a correction in layout 0 (batch 0), then re-batch to size 10
        # (layout 1). The new layout's corrections_block for batch 2 must NOT
        # include the old layout's correction, even though its batch_index is 0
        # and 0 < 2.
        rev = self.get().json()["revision"]
        self.patch({"1#0": {"text": "Eigene Fassung", "comment": "note"}}, rev)
        rev = self.get().json()["revision"]
        result = self._apply(0, rev)
        self.assertTrue(result.json()["changed"])
        self.assertEqual(len(result.json()["corrections"]), 1)
        # The correction is tagged with layout 0.
        self.assertEqual(result.json()["corrections"][0]["layout_version"], 0)
        # Re-batch to size 10: starts layout 1.
        rev = self.get().json()["revision"]
        set_resp = self._set_batch_size(rev, 10)
        self.assertTrue(set_resp.json()["changed"])
        self.assertEqual(set_resp.json()["layout_version"], 1)
        # The snapshot still has the old correction.
        snap = self.get().json()
        self.assertEqual(len(snap.get("corrections", [])), 1)
        self.assertEqual(snap["corrections"][0]["layout_version"], 0)
        # A new view at layout 1 must NOT include the old correction.
        from harness_remote.view import RemoteWorkspaceView
        reread = self.get().json()
        view = RemoteWorkspaceView.from_remote(
            [{"key": k, "unit_id": k, "ordinal": i, "source_text": "s",
              "target_text": "t"} for i, k in enumerate(snap["order"])],
            reread)
        self.assertEqual(view.layout_version, 1)
        # corrections_block for batch 2 must be empty (no layout-1 corrections).
        self.assertEqual(view.corrections_block(upto_batch=2), "")

    def test_re_applying_in_new_layout_does_not_erase_old_history(self):
        # Apply in layout 0 (batch 0), re-batch to size 10 (layout 1), then
        # apply again in layout 1 (batch 0). The old layout's correction must
        # survive; the new apply only replaces layout 1's batch 0 corrections.
        rev = self.get().json()["revision"]
        self.patch({"1#0": {"text": "Layout 0 edit"}}, rev)
        rev = self.get().json()["revision"]
        result = self._apply(0, rev)
        self.assertEqual(len(result.json()["corrections"]), 1)
        self.assertEqual(result.json()["corrections"][0]["layout_version"], 0)
        self.assertEqual(result.json()["corrections"][0]["human_final"],
                         "Layout 0 edit")
        # Re-batch to size 10: starts layout 1.
        rev = self.get().json()["revision"]
        set_resp = self._set_batch_size(rev, 10)
        self.assertTrue(set_resp.json()["changed"])
        # Apply in layout 1 (batch 0): a different edit on the same row.
        rev = self.get().json()["revision"]
        self.patch({"1#0": {"text": "Layout 1 edit"}}, rev)
        rev = self.get().json()["revision"]
        result2 = self._apply(0, rev)
        self.assertTrue(result2.json()["changed"])
        # Both corrections survive: old (layout 0) and new (layout 1).
        corrections = result2.json()["corrections"]
        self.assertEqual(len(corrections), 2)
        layout_0 = [c for c in corrections if c["layout_version"] == 0]
        layout_1 = [c for c in corrections if c["layout_version"] == 1]
        self.assertEqual(len(layout_0), 1)
        self.assertEqual(len(layout_1), 1)
        self.assertEqual(layout_0[0]["human_final"], "Layout 0 edit")
        self.assertEqual(layout_1[0]["human_final"], "Layout 1 edit")

    def test_return_to_original_size_does_not_reactivate_old_correction(self):
        # 100→10→100: the old correction from layout 0 does NOT auto-activate
        # in layout 2 (the re-batch back to 100). A new apply in layout 2
        # starts fresh.
        rev = self.get().json()["revision"]
        self.patch({"1#0": {"text": "Layout 0 edit"}}, rev)
        rev = self.get().json()["revision"]
        self._apply(0, rev)
        # 100→10 (layout 1).
        rev = self.get().json()["revision"]
        set_resp = self._set_batch_size(rev, 10)
        self.assertTrue(set_resp.json()["changed"])
        self.assertEqual(set_resp.json()["layout_version"], 1)
        # 10→100 (layout 2): the old correction is NOT auto-activated.
        rev = self.get().json()["revision"]
        set_resp2 = self._set_batch_size(rev, 100)
        self.assertTrue(set_resp2.json()["changed"])
        self.assertEqual(set_resp2.json()["layout_version"], 2)
        snap = self.get().json()
        # The old correction is still in history.
        self.assertEqual(len(snap.get("corrections", [])), 1)
        self.assertEqual(snap["corrections"][0]["layout_version"], 0)
        # But it does NOT appear in layout 2's feedback.
        from harness_remote.view import RemoteWorkspaceView
        view = RemoteWorkspaceView.from_remote(
            [{"key": k, "unit_id": k, "ordinal": i, "source_text": "s",
              "target_text": "t"} for i, k in enumerate(snap["order"])],
            snap)
        self.assertEqual(view.layout_version, 2)
        self.assertEqual(view.corrections_block(upto_batch=2), "")

    def test_stale_revision_does_not_mutate_drafts_or_corrections(self):
        rev = self.get().json()["revision"]
        self.patch({"1#0": {"text": "Draft text"}}, rev)
        rev = self.get().json()["revision"]
        before = self.repo.rows[DOC_A]
        stale = self._set_batch_size(99, 10)
        self.assertEqual(stale.status_code, 409)
        self.assert_unchanged(before)

    def test_drafts_survive_rebatch_by_key(self):
        rev = self.get().json()["revision"]
        self.patch({"1#0": {"text": "Held draft"}}, rev)
        rev = self.get().json()["revision"]
        set_resp = self._set_batch_size(rev, 10)
        self.assertTrue(set_resp.json()["changed"])
        # The draft survived: the re-slice re-anchors by key.
        record = self.record("1#0")
        self.assertEqual(record["user_final"], "Held draft")
        self.assertTrue(record["edited_by_human"])

    def test_unknown_workspace_is_404(self):
        rev = self.get().json()["revision"]
        response = self._set_batch_size(rev, 10, document_id="b" * 64)
        self.assertEqual(response.status_code, 404)

    def test_bad_request_rejects_invalid_batch_size(self):
        rev = self.get().json()["revision"]
        for bad in (0, -1, True, "10", 1.5, None):
            with self.subTest(batch_size=bad):
                before = self.repo.rows[DOC_A]
                response = self._set_batch_size(rev, bad)
                self.assertEqual(response.status_code, 400, response.text)
                self.assert_unchanged(before)

    def test_bad_request_rejects_unknown_fields(self):
        rev = self.get().json()["revision"]
        before = self.repo.rows[DOC_A]
        response = self.client.post(
            "/workspaces/%s/set_batch_size" % DOC_A,
            json={"expected_revision": rev, "batch_size": 10, "extra": 1})
        self.assertEqual(response.status_code, 400, response.text)
        self.assert_unchanged(before)

    def test_bad_request_rejects_bad_expected_revision(self):
        before = self.repo.rows[DOC_A]
        for bad in (0, -1, True, "1", None):
            with self.subTest(expected_revision=bad):
                response = self.client.post(
                    "/workspaces/%s/set_batch_size" % DOC_A,
                    json={"expected_revision": bad, "batch_size": 10})
                self.assertEqual(response.status_code, 400, response.text)
                self.assert_unchanged(before)

    def test_old_snapshot_defaults_to_layout_zero(self):
        # A snapshot written before layout versioning has no layout_version key.
        # The service must default to 0, and a pre-rebatch correction must
        # still feed feedback.
        rev = self.get().json()["revision"]
        self.patch({"1#0": {"text": "Pre-versioning edit"}}, rev)
        rev = self.get().json()["revision"]
        result = self._apply(0, rev)
        self.assertTrue(result.json()["changed"])
        # Simulate an old snapshot: remove layout_version from the stored row.
        row = self.repo.rows[DOC_A]
        old_snapshot = dict(row.snapshot)
        old_snapshot.pop("layout_version", None)
        old_snapshot["corrections"] = row.snapshot.get("corrections", [])
        new_row = WorkspaceRow(
            document_id=row.document_id, snapshot=old_snapshot,
            state_version=row.state_version, revision=row.revision,
            tag_source=row.tag_source, bound_original_id=row.bound_original_id)
        self.repo.rows[DOC_A] = new_row
        # A set_batch_size on the old snapshot should work (defaults to 0).
        rev2 = self.get().json()["revision"]
        set_resp = self._set_batch_size(rev2, 10)
        self.assertEqual(set_resp.status_code, 200, set_resp.text)
        self.assertTrue(set_resp.json()["changed"])
        self.assertEqual(set_resp.json()["layout_version"], 1)
        # The pre-versioning correction (layout 0) is still in history.
        snap = self.get().json()
        self.assertEqual(len(snap.get("corrections", [])), 1)
        self.assertEqual(snap["corrections"][0]["human_final"],
                         "Pre-versioning edit")


if __name__ == "__main__":
    unittest.main(verbosity=2)
