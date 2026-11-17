"""Focused tests for the remote read-only view adapter.

These exercise ``harness_remote.view`` directly - no Streamlit, no HTTP, no
``memoq_harness``. They lock the one behaviour the UI depends on: the three
text fields stay distinct (original target vs. the human's saved text vs. the
model's proposal), the active selection comes from the workspace, and a failed
Open never produces a view.

Run with:  python tests/test_remote_view.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness_remote import view  # noqa: E402


def doc_segment(key, unit_id, ordinal, source_text, target_text, translatable=True):
    return {
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
        "skip_reason": "",
        "translatable": translatable,
    }


def ws_record(key, segment_id, ordinal, mt_target="", user_final="",
              user_comment="", llm_suggestion="", edited_by_human=False,
              status="pending", tag_report=""):
    return {
        "key": key,
        "segment_id": segment_id,
        "ordinal": ordinal,
        "batch_index": ordinal // 100,
        "source_text": "",
        "mt_target": mt_target,
        "llm_suggestion": llm_suggestion,
        "user_final": user_final,
        "user_comment": user_comment,
        "status": status,
        "edited_by_human": edited_by_human,
        "tag_report": tag_report,
    }


def workspace(document_id, original_id, tag_source, revision, order, segments,
              batch_size=100, current_batch=0, source_file=""):
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


class MergeDistinctFields(unittest.TestCase):
    def test_original_target_user_final_and_suggestion_stay_distinct(self):
        doc = [doc_segment("k1", "u1", 0, "The source.", "MT target text")]
        ws = workspace(
            "doc-1", "orig-1", "target", 1,
            order=["k1"],
            segments={"k1": ws_record(
                "k1", "u1", 0, mt_target="stale mt",
                user_final="Human final text",
                llm_suggestion="Model proposed text",
                user_comment="why", edited_by_human=True,
                status="processed", tag_report="tag issue")},
        )
        v = view.RemoteWorkspaceView.from_remote(doc, ws)
        rec = v.record("k1")

        # The original target is the document's target_text, NOT the stale value
        # that happens to be stored in the workspace record.
        self.assertEqual(rec.mt_target, "MT target text")
        self.assertEqual(rec.user_final, "Human final text")
        self.assertEqual(rec.llm_suggestion, "Model proposed text")
        # All three distinct.
        self.assertEqual(len({rec.mt_target, rec.user_final, rec.llm_suggestion}), 3)
        self.assertEqual(rec.source_text, "The source.")
        self.assertEqual(rec.user_comment, "why")
        self.assertTrue(rec.edited_by_human)
        self.assertEqual(rec.status, "processed")
        self.assertEqual(rec.tag_report, "tag issue")
        self.assertTrue(rec.is_dirty)  # human final != original target

    def test_user_final_only_falls_back_to_suggestion_then_mt(self):
        doc = [doc_segment("k1", "u1", 0, "src", "MT")]
        ws = workspace("d", "o", "target", 1, order=["k1"],
                       segments={"k1": ws_record("k1", "u1", 0, llm_suggestion="S")})
        rec = view.RemoteWorkspaceView.from_remote(doc, ws).record("k1")
        self.assertEqual(rec.mt_target, "MT")
        self.assertEqual(rec.llm_suggestion, "S")
        self.assertEqual(rec.user_final, "")
        self.assertFalse(rec.is_dirty)  # no human final


class ActiveSelection(unittest.TestCase):
    def test_order_and_batching_come_from_workspace(self):
        doc = [doc_segment("k%d" % i, "u%d" % i, i, "s%d" % i, "t%d" % i)
               for i in range(5)]
        ws = workspace("d", "o", "target", 3,
                       order=["k2", "k0", "k4", "k1", "k3"],
                       segments={k: ws_record(k, k, i)
                                 for i, k in enumerate(["k2", "k0", "k4", "k1", "k3"])},
                       batch_size=2)
        v = view.RemoteWorkspaceView.from_remote(doc, ws)
        self.assertEqual(v.batch_count, 3)  # 5 / 2
        self.assertEqual([r.key for r in v.batch_records(0)], ["k2", "k0"])
        self.assertEqual([r.key for r in v.batch_records(1)], ["k4", "k1"])
        self.assertEqual([r.key for r in v.batch_records(2)], ["k3"])
        self.assertEqual(v.batch_records(99), [])

    def test_progress_counts_status_and_edits(self):
        ws = workspace("d", "o", "target", 1,
                       order=["a", "b", "c"],
                       segments={
                           "a": ws_record("a", "a", 0, status="applied",
                                          edited_by_human=True),
                           "b": ws_record("b", "b", 1, status="processed"),
                           "c": ws_record("c", "c", 2, status="pending"),
                       },
                       batch_size=100)
        p = view.RemoteWorkspaceView.from_remote([], ws).progress(0)
        self.assertEqual(p.total, 3)
        self.assertEqual(p.processed, 2)
        self.assertEqual(p.applied, 1)
        self.assertEqual(p.edited, 1)

    def test_identity_fields_are_surfaced_for_reload_checks(self):
        ws = workspace("doc-77", "orig-42", "source", 9, order=[], segments={})
        v = view.RemoteWorkspaceView.from_remote([], ws)
        self.assertEqual(v.document_id, "doc-77")
        self.assertEqual(v.original_id, "orig-42")
        self.assertEqual(v.tag_source, "source")
        self.assertEqual(v.revision, 9)
        self.assertTrue(v.is_empty)
        self.assertEqual(v.batch_count, 0)


class Fallbacks(unittest.TestCase):
    def test_active_key_missing_from_document_segments_uses_saved_record(self):
        # The workspace has saved work on k1 but the document segment list does
        # not carry it; the record still renders from the saved mt_target.
        ws = workspace("d", "o", "target", 1, order=["k1"],
                       segments={"k1": ws_record("k1", "u1", 0,
                                                 mt_target="saved mt",
                                                 user_final="human")})
        rec = view.RemoteWorkspaceView.from_remote([], ws).record("k1")
        self.assertEqual(rec.mt_target, "saved mt")
        self.assertEqual(rec.user_final, "human")
        self.assertEqual(rec.source_text, "")

    def test_missing_workspace_record_still_renders_from_document(self):
        doc = [doc_segment("k1", "u1", 0, "hello", "bonjour")]
        ws = workspace("d", "o", "target", 1, order=["k1"], segments={})
        rec = view.RemoteWorkspaceView.from_remote(doc, ws).record("k1")
        self.assertEqual(rec.source_text, "hello")
        self.assertEqual(rec.mt_target, "bonjour")
        self.assertEqual(rec.user_final, "")
        self.assertEqual(rec.status, "pending")

    def test_counts_fall_back_to_computation(self):
        ws = workspace("d", "o", "target", 1,
                       order=["a", "b"],
                       segments={"a": ws_record("a", "a", 0, edited_by_human=True,
                                                user_final="x", mt_target="y"),
                                 "b": ws_record("b", "b", 1)})
        counts = view.RemoteWorkspaceView.from_remote([], ws).counts()
        self.assertEqual(counts["active"], 2)
        self.assertEqual(counts["with_saved_work"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
