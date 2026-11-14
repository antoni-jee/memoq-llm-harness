"""Unit tests for document-service (offline; fake repository, temp storage).

The fake repository below exists only for these tests. The runtime backend is
:class:`services.document_service.repository.PostgresDocumentRepository`,
covered by tests/test_document_service_pg.py against a real database.

Run with:  python -m unittest -v tests.test_document_service
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient  # noqa: E402

from memoq_harness import mqxliff  # noqa: E402
from services.document_service import config, migrate  # noqa: E402
from services.document_service.app import create_app  # noqa: E402
from services.document_service.repository import (  # noqa: E402
    DB_STATUS_NOT_MIGRATED, DB_STATUS_OK, DB_STATUS_UNAVAILABLE, DocumentRecord,
    ExportRecord, OriginalRecord, RepositoryError,
)
from services.document_service.storage import EXPORTS  # noqa: E402
from services.document_service.service import DocumentService  # noqa: E402
from services.document_service.storage import FileStorage  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "sample.mqxliff")
with open(FIXTURE, "rb") as _handle:
    SAMPLE = _handle.read()
# Same fingerprinted source, different target text -> same document_id,
# different original_id.
VARIANT = SAMPLE.replace(b"<target>Das Dashboard zeigt", b"<target>Das Dashboard bietet", 1)
assert VARIANT != SAMPLE

SECRET_TEXT = "TOPSECRET-SEGMENT-TEXT"
# SHA-256 (LF line endings) of migration 0001 as committed in 11465dd.
MIGRATION_0001_SHA256 = "17ca69a13b348afb953a3351ce9c1ec36837d1b9f8eb828bc28bd9681cf0fe86"


class FakeRepository:
    """In-memory stand-in with the PostgresDocumentRepository interface."""

    def __init__(self):
        self.documents = {}
        self.originals = {}
        self.exports = {}
        self.status_value = DB_STATUS_OK
        self.fail = False

    def _check(self):
        if self.fail:
            raise RepositoryError("Database operation failed.")

    def status(self, expected_version):
        return self.status_value

    def add_original(self, record):
        self._check()
        now = datetime.now(timezone.utc)
        self.documents.setdefault(record.document_id, now)
        if record.original_id in self.originals:
            return self.originals[record.original_id], False
        stored = OriginalRecord(**{**record.__dict__, "created_at": now})
        self.originals[record.original_id] = stored
        return stored, True

    def get_original(self, original_id):
        self._check()
        return self.originals.get(original_id)

    def add_export(self, record):
        self._check()
        if record.export_id in self.exports:
            return self.get_export(record.export_id), False
        self.exports[record.export_id] = ExportRecord(
            **{**record.__dict__, "created_at": datetime.now(timezone.utc)})
        return self.get_export(record.export_id), True

    def get_export(self, export_id):
        self._check()
        stored = self.exports.get(export_id)
        if stored is None:
            return None
        document_id = self.originals[stored.original_id].document_id
        return ExportRecord(**{**stored.__dict__, "document_id": document_id})

    def get_document(self, document_id):
        self._check()
        if document_id not in self.documents:
            return None
        originals = tuple(sorted((o for o in self.originals.values()
                                  if o.document_id == document_id),
                                 key=lambda o: (o.created_at, o.original_id)))
        return DocumentRecord(document_id, self.documents[document_id], originals)


def stored_files(root):
    found = []
    for directory, _dirs, files in os.walk(root):
        found.extend(os.path.join(directory, f) for f in files)
    return found


class ServiceTestCase(unittest.TestCase):
    max_upload = 1024 * 1024

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = FakeRepository()
        self.storage = FileStorage(os.path.join(self.tmp, "store"))
        self.service = DocumentService(self.repo, self.storage, self.max_upload, "0001")
        self.client = TestClient(create_app(self.service), raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def upload(self, data=SAMPLE, name="sample.mqxliff"):
        return self.client.post("/documents", files={"file": (name, data, "application/xml")})

    def assert_nothing_stored(self):
        self.assertEqual(self.repo.originals, {})
        self.assertEqual(self.repo.documents, {})
        self.assertEqual(stored_files(self.storage.root), [])


class TestUploadIdentity(ServiceTestCase):
    def test_upload_returns_both_identifiers_matching_the_parser(self):
        response = self.upload()
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        parsed = mqxliff.MqxliffDocument.from_bytes(SAMPLE, "sample.mqxliff")
        self.assertEqual(body["document_id"], parsed.source_fingerprint())
        self.assertEqual(body["original_id"], hashlib.sha256(SAMPLE).hexdigest())
        self.assertEqual(body["segment_count"], len(parsed.parse_segments()))
        self.assertEqual(body["size_bytes"], len(SAMPLE))
        self.assertEqual(body["filename"], "sample.mqxliff")
        self.assertTrue(body["created"])

    def test_identical_reupload_is_idempotent(self):
        first = self.upload().json()
        second = self.upload(name="renamed.mqxliff")
        self.assertEqual(second.status_code, 200)
        body = second.json()
        self.assertFalse(body["created"])
        self.assertEqual(body["original_id"], first["original_id"])
        self.assertEqual(body["document_id"], first["document_id"])
        self.assertEqual(body["filename"], "sample.mqxliff")  # first upload's record
        self.assertEqual(len(self.repo.originals), 1)
        self.assertEqual(len(stored_files(self.storage.root)), 1)

    def test_same_source_different_bytes_are_separate_originals(self):
        a = self.upload().json()
        b = self.upload(VARIANT, "variant.mqxliff")
        self.assertEqual(b.status_code, 201)
        b = b.json()
        self.assertEqual(a["document_id"], b["document_id"])
        self.assertNotEqual(a["original_id"], b["original_id"])
        doc = self.client.get("/documents/%s" % a["document_id"]).json()
        self.assertEqual(sorted(o["original_id"] for o in doc["originals"]),
                         sorted([a["original_id"], b["original_id"]]))
        self.assertNotIn("latest", doc)
        for body in (a, b):
            meta = self.client.get("/originals/%s" % body["original_id"]).json()
            self.assertEqual(meta["original_id"], body["original_id"])
            self.assertEqual(meta["filename"], body["filename"])

    def test_original_content_is_byte_identical(self):
        body = self.upload(VARIANT).json()
        response = self.client.get("/originals/%s/content" % body["original_id"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, VARIANT)
        self.assertIn("attachment", response.headers["content-disposition"])

    def test_missing_file_is_repaired_by_identical_reupload(self):
        body = self.upload().json()
        os.remove(self.storage.path_for(body["original_id"]))
        self.assertEqual(self.client.get(
            "/originals/%s/content" % body["original_id"]).status_code, 500)
        self.assertEqual(self.upload().status_code, 200)
        self.assertEqual(self.client.get(
            "/originals/%s/content" % body["original_id"]).content, SAMPLE)

    def test_filename_is_sanitized_and_not_used_as_a_path(self):
        body = self.upload(name="..\\..\\evil\x07/../x.mqxliff").json()
        self.assertEqual(body["filename"], "x.mqxliff")
        files = stored_files(self.storage.root)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].endswith(body["original_id"] + ".mqxliff"))


class TestSegments(ServiceTestCase):
    def test_segments_belong_to_one_specific_original(self):
        a = self.upload().json()
        b = self.upload(VARIANT).json()
        seg_a = self.client.get("/originals/%s/segments" % a["original_id"]).json()
        seg_b = self.client.get("/originals/%s/segments" % b["original_id"]).json()
        self.assertEqual(seg_a["original_id"], a["original_id"])
        self.assertEqual(seg_b["original_id"], b["original_id"])
        keys_a = [s["key"] for s in seg_a["segments"]]
        self.assertEqual(keys_a, [s["key"] for s in seg_b["segments"]])
        targets_a = {s["key"]: s["target_text"] for s in seg_a["segments"]}
        targets_b = {s["key"]: s["target_text"] for s in seg_b["segments"]}
        changed = [k for k in keys_a if targets_a[k] != targets_b[k]]
        self.assertEqual(len(changed), 1)
        self.assertIn("bietet", targets_b[changed[0]])

    def test_segments_match_parser_and_expose_filter_results(self):
        body = self.upload().json()
        data = self.client.get("/originals/%s/segments" % body["original_id"]).json()
        parsed = mqxliff.MqxliffDocument.from_bytes(SAMPLE, "s").parse_segments()
        self.assertEqual([s["key"] for s in data["segments"]], [s.key for s in parsed])
        self.assertEqual([s["skip_reason"] for s in data["segments"]],
                         [s.skip_reason for s in parsed])
        first = data["segments"][0]
        for field in ("source_text", "target_text", "tags", "source_tags", "target_tags",
                      "expected_numbers", "confirmed", "locked", "translatable"):
            self.assertIn(field, first)
        self.assertEqual(data["tag_source"], "target")

    def test_filter_flags_and_tag_source_are_applied(self):
        body = self.upload().json()
        url = "/originals/%s/segments" % body["original_id"]
        default = self.client.get(url).json()["segments"]
        relaxed = self.client.get(url, params={"skip_confirmed": "false"}).json()["segments"]
        self.assertIn("confirmed", [s["skip_reason"] for s in default])
        self.assertNotIn("confirmed", [s["skip_reason"] for s in relaxed])
        source = self.client.get(url, params={"tag_source": "source"}).json()
        self.assertEqual(source["tag_source"], "source")
        self.assertEqual(self.client.get(url, params={"tag_source": "x"}).status_code, 400)
        self.assertEqual(self.client.get(url, params={"skip_locked": "maybe"}).status_code, 400)

    def test_unknown_and_malformed_ids(self):
        unknown = "0" * 64
        self.assertEqual(self.client.get("/originals/%s" % unknown).status_code, 404)
        self.assertEqual(self.client.get("/originals/%s/segments" % unknown).status_code, 404)
        self.assertEqual(self.client.get("/documents/%s" % unknown).status_code, 404)
        for bad in ("abc", "G" * 64, "A" * 64):
            self.assertEqual(self.client.get("/originals/%s" % bad).status_code, 400)
            self.assertEqual(self.client.get("/documents/%s" % bad).status_code, 400)


class TestRejectedUploads(ServiceTestCase):
    max_upload = 4096

    def test_invalid_uploads_store_nothing(self):
        cases = [
            (b"", 400),
            (b"this is not xml " + SECRET_TEXT.encode(), 422),
            (b"<html><body>%s</body></html>" % SECRET_TEXT.encode(), 422),
            (b'<xliff version="1.2"><file><body/></file></xliff>', 422),
            (b"<xliff><trans-unit id='1'><source>" + SECRET_TEXT.encode() + b"</source", 422),
            (b"x" * 5000, 413),
        ]
        for data, status in cases:
            with self.subTest(status=status, size=len(data)):
                response = self.upload(data)
                self.assertEqual(response.status_code, status, response.text)
                self.assertNotIn(SECRET_TEXT, response.text)
                self.assertIn("error", response.json())
                self.assert_nothing_stored()

    def test_request_shape_errors(self):
        self.assertEqual(self.client.post("/documents", content=SAMPLE[:100],
                                          headers={"content-type": "application/xml"}).status_code, 415)
        missing = self.client.post("/documents", files={"other": ("a", b"x")})
        self.assertEqual(missing.status_code, 400)
        self.assert_nothing_stored()

    def test_oversized_content_length_is_refused_before_reading(self):
        response = self.client.post(
            "/documents", content=b"--b\r\n",
            headers={"content-type": "multipart/form-data; boundary=b",
                     "content-length": str(10 ** 9)})
        self.assertEqual(response.status_code, 413)


class TestFailureAtomicity(ServiceTestCase):
    def test_failed_file_write_leaves_no_file_or_row(self):
        with mock.patch("services.document_service.storage.os.replace",
                        side_effect=OSError("disk full")):
            response = self.upload()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "unavailable")
        self.assert_nothing_stored()

    def test_failed_database_insert_leaves_an_unreferenced_file_that_retry_reuses(self):
        original_add = self.repo.add_original

        def failing_add(record):
            raise RepositoryError("Database operation failed.")

        self.repo.add_original = failing_add
        response = self.upload()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.repo.originals, {})
        files = stored_files(self.storage.root)
        self.assertEqual(len(files), 1)  # kept deliberately: may be owned by an overlapping upload
        original_id = hashlib.sha256(SAMPLE).hexdigest()
        # Never served without a database record.
        self.assertEqual(self.client.get("/originals/%s/content" % original_id).status_code, 404)

        self.repo.add_original = original_add
        retry = self.upload()
        self.assertEqual(retry.status_code, 201)
        self.assertEqual(stored_files(self.storage.root), files)
        self.assertEqual(self.client.get("/originals/%s/content" % original_id).content, SAMPLE)

    def test_failed_overlapping_upload_keeps_the_successful_original(self):
        """Two overlapping uploads of the same bytes, A succeeds, B fails.

        Interleaving reproduced deterministically: B checked the repository
        and the storage before A committed (so B saw no record and no file),
        both wrote the identical file, A's insert committed, then B's insert
        failed. B must not delete the file A's committed record points to.
        """
        a = self.upload()
        self.assertEqual(a.status_code, 201)
        original_id = a.json()["original_id"]

        def b_insert_fails(record):
            raise RepositoryError("Database operation failed.")

        with mock.patch.object(self.repo, "get_original", return_value=None), \
                mock.patch.object(self.storage, "has_valid", return_value=False), \
                mock.patch.object(self.repo, "add_original", side_effect=b_insert_fails):
            b = self.upload()
        self.assertEqual(b.status_code, 503)

        content = self.client.get("/originals/%s/content" % original_id)
        self.assertEqual(content.status_code, 200, content.text)
        self.assertEqual(content.content, SAMPLE)
        self.assertEqual(len(self.repo.originals), 1)

    def test_database_down_on_read_is_503(self):
        body = self.upload().json()
        self.repo.fail = True
        response = self.client.get("/originals/%s" % body["original_id"])
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["message"], "Database operation failed.")


class TestHealth(ServiceTestCase):
    def test_liveness_has_no_dependencies(self):
        self.repo.status_value = DB_STATUS_UNAVAILABLE
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_readiness_checks_database_and_storage(self):
        ok = self.client.get("/readyz")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json(), {"status": "ready",
                                     "checks": {"database": "ok", "storage": "ok"}})
        for value in (DB_STATUS_UNAVAILABLE, DB_STATUS_NOT_MIGRATED):
            self.repo.status_value = value
            response = self.client.get("/readyz")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["checks"]["database"], value)
        self.repo.status_value = DB_STATUS_OK

        blocker = os.path.join(self.tmp, "not-a-dir")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("x")
        self.service.storage = FileStorage(blocker)
        response = self.client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["checks"]["storage"], "unavailable")


class TestNoLeaks(ServiceTestCase):
    def test_logs_never_contain_document_text(self):
        doc = SAMPLE.replace(b"Das Dashboard zeigt", SECRET_TEXT.encode(), 1)
        with self.assertLogs("document-service", level="DEBUG") as logs:
            self.upload(doc)
            self.client.get("/originals/%s/segments" % hashlib.sha256(doc).hexdigest())
            self.repo.fail = True
            self.client.get("/originals/%s" % hashlib.sha256(doc).hexdigest())
            logging.getLogger("document-service").debug("end")
        self.assertNotIn(SECRET_TEXT, "\n".join(logs.output))

    def test_unexpected_error_is_generic(self):
        with mock.patch.object(self.service, "get_original",
                               side_effect=RuntimeError(SECRET_TEXT)):
            with self.assertLogs("document-service", level="ERROR") as logs:
                response = self.client.get("/originals/%s" % ("0" * 64))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"], "internal_error")
        self.assertNotIn(SECRET_TEXT, response.text)
        self.assertNotIn(SECRET_TEXT, "\n".join(logs.output))


def reference_export(raw, translations, tag_source="target", mark_confirmed=False):
    """What the app's own export path (apply_translation + to_bytes) produces."""
    doc = mqxliff.MqxliffDocument.from_bytes(raw, "ref")
    by_key = {s.key: s for s in doc.parse_segments(tag_source=tag_source)}
    for key, text in translations.items():
        doc.apply_translation(by_key[key], text, mark_confirmed=mark_confirmed)
    return doc.to_bytes()


def segments_of(raw, tag_source="target"):
    return {s.key: s for s in mqxliff.MqxliffDocument.from_bytes(raw, "x").parse_segments(
        tag_source=tag_source)}


class TestExport(ServiceTestCase):
    TRANSLATIONS = {
        "1#0": "Click {1}Save{2} to continue.",
        "12#11": "Export the report to PDF.",
    }

    def setUp(self):
        super().setUp()
        self.a = self.upload().json()
        self.b = self.upload(VARIANT, "variant.mqxliff").json()

    def export(self, original_id=None, **body):
        body.setdefault("translations", self.TRANSLATIONS)
        return self.client.post("/originals/%s/exports" % (original_id or self.a["original_id"]),
                                json=body)

    def content(self, export_id):
        return self.client.get("/exports/%s/content" % export_id)

    # ------------------------------------------------------------ happy path

    def test_export_applies_translations_and_preserves_the_rest(self):
        response = self.export()
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertTrue(body["created"])
        self.assertEqual(body["original_id"], self.a["original_id"])
        self.assertEqual(body["document_id"], self.a["document_id"])
        self.assertEqual(body["segments_written"], 2)
        self.assertEqual(body["warnings"], [])
        self.assertTrue(body["filename"].startswith("sample_harness_"))

        data = self.content(body["export_id"]).content
        self.assertEqual(body["export_id"], hashlib.sha256(data).hexdigest())
        before, after = segments_of(SAMPLE), segments_of(data)
        self.assertEqual(list(before), list(after))
        for key in before:
            if key in self.TRANSLATIONS:
                self.assertEqual(after[key].target_text, self.TRANSLATIONS[key])
            else:
                self.assertEqual(after[key].target_text, before[key].target_text, key)
                self.assertEqual((after[key].confirmed, after[key].locked,
                                  after[key].status, after[key].target_state),
                                 (before[key].confirmed, before[key].locked,
                                  before[key].status, before[key].target_state), key)

    def test_export_bytes_equal_the_existing_app_export_path(self):
        body = self.export(mark_confirmed=True).json()
        expected = reference_export(SAMPLE, self.TRANSLATIONS, mark_confirmed=True)
        self.assertEqual(self.content(body["export_id"]).content, expected)
        self.assertTrue(body["mark_confirmed"])
        after = segments_of(expected)
        self.assertTrue(after["1#0"].confirmed)
        self.assertFalse(segments_of(SAMPLE)["1#0"].confirmed)

    def test_export_uses_the_requested_original(self):
        # Segment 2#1 is the one whose target differs between the two originals.
        only_other = {"1#0": "Click {1}Save{2} to continue."}
        from_a = self.export(self.a["original_id"], translations=only_other).json()
        from_b = self.export(self.b["original_id"], translations=only_other).json()
        self.assertEqual(from_a["original_id"], self.a["original_id"])
        self.assertEqual(from_b["original_id"], self.b["original_id"])
        self.assertNotEqual(from_a["export_id"], from_b["export_id"])
        self.assertIn("zeigt", segments_of(self.content(from_a["export_id"]).content)
                      ["2#1"].target_text)
        self.assertIn("bietet", segments_of(self.content(from_b["export_id"]).content)
                      ["2#1"].target_text)

    def test_original_bytes_and_fingerprint_are_unchanged(self):
        body = self.export().json()
        self.assertEqual(self.client.get(
            "/originals/%s/content" % self.a["original_id"]).content, SAMPLE)
        exported = self.content(body["export_id"]).content
        self.assertEqual(mqxliff.MqxliffDocument.from_bytes(exported, "e").source_fingerprint(),
                         self.a["document_id"])

    def test_inline_tags_are_restored_from_the_original_xml(self):
        nested = segments_of(SAMPLE)["11#10"]
        text = "{1}Nested {2}formatting{3} here{4} plus {5}."
        body = self.export(translations={"11#10": text, "1#0": "{1}Save{2} now."}).json()
        after = segments_of(self.content(body["export_id"]).content)
        self.assertEqual(after["11#10"].target_text, text)
        self.assertEqual(after["11#10"].lookup.tags, nested.lookup.tags)
        self.assertEqual(after["1#0"].target.tags, segments_of(SAMPLE)["1#0"].target.tags)

    def test_tag_damage_is_reported_but_exported_like_the_app(self):
        body = self.export(translations={"1#0": "Click {1}Save to continue."}).json()
        self.assertEqual(body["warnings"], [
            {"key": "1#0", "missing": [2], "unknown": [], "duplicated": []}])
        exported = self.content(body["export_id"]).content
        self.assertEqual(exported, reference_export(
            SAMPLE, {"1#0": "Click {1}Save to continue."}))
        self.assertIn("{2}", segments_of(exported)["1#0"].target_text)  # appended
        self.assertEqual(self.client.get("/exports/%s" % body["export_id"]).json()["warnings"],
                         body["warnings"])

    def test_source_tag_mode_and_empty_target_rows(self):
        body = self.export(translations={"9#8": "Neu geschrieben.", "1#0": "{1}X{2}"},
                           tag_source="source").json()
        self.assertEqual(body["tag_source"], "source")
        exported = self.content(body["export_id"]).content
        self.assertEqual(exported, reference_export(
            SAMPLE, {"9#8": "Neu geschrieben.", "1#0": "{1}X{2}"}, tag_source="source"))
        self.assertEqual(segments_of(exported)["9#8"].target_text, "Neu geschrieben.")

    def test_identical_export_is_idempotent(self):
        first = self.export().json()
        second = self.export(translations=dict(reversed(list(self.TRANSLATIONS.items()))))
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["created"])
        self.assertEqual(second.json()["export_id"], first["export_id"])
        self.assertEqual(len(self.repo.exports), 1)

    def test_metadata_and_download_round_trip(self):
        body = self.export().json()
        meta = self.client.get("/exports/%s" % body["export_id"]).json()
        for field in ("export_id", "original_id", "document_id", "filename", "size_bytes",
                      "segments_written", "tag_source", "mark_confirmed", "warnings"):
            self.assertEqual(meta[field], body[field], field)
        download = self.content(body["export_id"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(len(download.content), body["size_bytes"])
        self.assertEqual(download.content, self.storage.read(body["export_id"], EXPORTS))
        self.assertIn(body["filename"], download.headers["content-disposition"])

    # ------------------------------------------------------------ rejections

    def assert_rejected(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"], code)
        self.assertEqual(self.repo.exports, {})
        self.assertFalse(os.path.isdir(os.path.join(self.storage.root, "exports"))
                         and stored_files(os.path.join(self.storage.root, "exports")))

    def test_unknown_original_and_segment(self):
        self.assert_rejected(self.export("0" * 64), 404, "not_found")
        self.assert_rejected(self.export("XYZ"), 400, "bad_request")
        response = self.export(translations={"1#0": "ok", "99#99": "x", "1": "y"})
        self.assert_rejected(response, 422, "unknown_segment")
        self.assertEqual(response.json()["details"]["keys"], ["1", "99#99"])

    def test_segments_by_position_or_source_text_are_not_accepted(self):
        source_text = segments_of(SAMPLE)["1#0"].source_text
        for key in ("0", "1", source_text):
            with self.subTest(key=key[:20]):
                self.assert_rejected(self.export(translations={key: "x"}), 422, "unknown_segment")

    def test_non_translatable_and_empty_translations(self):
        response = self.export(translations={"3#2": "x", "4#3": "y"})
        self.assert_rejected(response, 422, "non_translatable_segment")
        self.assertEqual(response.json()["details"]["reasons"],
                         {"3#2": "confirmed", "4#3": "locked"})
        relaxed = self.export(translations={"3#2": "Neu."}, filter={"skip_confirmed": False})
        self.assertEqual(relaxed.status_code, 201)
        self.repo.exports.clear()
        shutil.rmtree(os.path.join(self.storage.root, "exports"))
        self.assert_rejected(self.export(translations={"1#0": "  "}), 422, "empty_translation")

    def test_invalid_request_shapes(self):
        url = "/originals/%s/exports" % self.a["original_id"]
        cases = [
            ([], 400), ("text", 400), ({}, 400),
            ({"translations": {}}, 400),
            ({"translations": ["1#0", "x"]}, 400),
            ({"translations": {"1#0": 5}}, 400),
            ({"translations": {"1#0": None}}, 400),
            ({"translations": {"1#0": "x"}, "extra": 1}, 400),
            ({"translations": {"1#0": "x"}, "tag_source": "both"}, 400),
            ({"translations": {"1#0": "x"}, "filter": {"skip_all": True}}, 400),
            ({"translations": {"1#0": "x"}, "filter": {"skip_locked": "no"}}, 400),
            ({"translations": {"1#0": "x"}, "filter": []}, 400),
            ({"translations": {"1#0": "x"}, "mark_confirmed": "yes"}, 400),
        ]
        for body, status in cases:
            with self.subTest(body=repr(body)[:60]):
                self.assert_rejected(self.client.post(url, json=body), status, "bad_request")
        self.assert_rejected(self.client.post(url, content=b"{not json",
                                              headers={"content-type": "application/json"}),
                             400, "bad_request")
        self.assert_rejected(self.client.post(url, content=b"\xff\xfe",
                                              headers={"content-type": "application/json"}),
                             400, "bad_request")
        self.assert_rejected(self.client.post(url, data={"translations": "x"}),
                             415, "unsupported_media_type")

    def test_oversized_request_body(self):
        self.service.max_upload_bytes = 200
        response = self.export(translations={"1#0": "x" * 500})
        self.assert_rejected(response, 413, "too_large")

    def test_unknown_export_ids(self):
        self.assertEqual(self.client.get("/exports/%s" % ("0" * 64)).status_code, 404)
        self.assertEqual(self.content("0" * 64).status_code, 404)
        self.assertEqual(self.client.get("/exports/nope").status_code, 400)

    # -------------------------------------------------------------- failures

    def test_generation_failure_is_not_success(self):
        with mock.patch.object(mqxliff.MqxliffDocument, "apply_translation",
                               side_effect=mqxliff.MqxliffError("boom " + SECRET_TEXT)):
            response = self.export()
        self.assert_rejected(response, 500, "export_failed")
        self.assertNotIn(SECRET_TEXT, response.text)

    def test_validation_failure_is_not_success(self):
        with mock.patch.object(mqxliff.MqxliffDocument, "to_bytes", return_value=b"<broken"):
            response = self.export()
        self.assert_rejected(response, 500, "export_validation_failed")

    def test_changed_source_fails_validation(self):
        changed = SAMPLE.replace(b"Export the report", b"Export a report", 1)
        self.assertNotEqual(changed, SAMPLE)
        with mock.patch.object(mqxliff.MqxliffDocument, "to_bytes", return_value=changed):
            response = self.export()
        self.assert_rejected(response, 500, "export_validation_failed")

    def test_storage_failure_is_not_success(self):
        with mock.patch("services.document_service.storage.os.replace",
                        side_effect=OSError("disk full")):
            response = self.export()
        self.assert_rejected(response, 503, "unavailable")

    def test_repository_failure_is_not_success_and_keeps_the_file(self):
        with mock.patch.object(self.repo, "add_export",
                               side_effect=RepositoryError("Database operation failed.")):
            response = self.export()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.repo.exports, {})
        export_id = hashlib.sha256(reference_export(SAMPLE, self.TRANSLATIONS)).hexdigest()
        self.assertTrue(self.storage.has_valid(export_id, EXPORTS))  # kept for reuse
        self.assertEqual(self.client.get("/exports/%s" % export_id).status_code, 404)
        retry = self.export()
        self.assertEqual(retry.status_code, 201)
        self.assertEqual(retry.json()["export_id"], export_id)

    def test_failed_overlapping_export_keeps_the_successful_file(self):
        first = self.export().json()
        with mock.patch.object(self.storage, "has_valid", return_value=False), \
                mock.patch.object(self.repo, "add_export",
                                  side_effect=RepositoryError("Database operation failed.")):
            self.assertEqual(self.export().status_code, 503)
        self.assertEqual(self.content(first["export_id"]).status_code, 200)

    def test_missing_export_file_is_reported(self):
        body = self.export().json()
        os.remove(self.storage.path_for(body["export_id"], EXPORTS))
        response = self.content(body["export_id"])
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"], "storage_inconsistent")

    def test_export_logs_and_errors_hold_no_text(self):
        secret = {"1#0": "{1}%s{2}" % SECRET_TEXT}
        with self.assertLogs("document-service", level="DEBUG") as logs:
            ok = self.export(translations=secret)
            bad = self.export(translations={"77#7": SECRET_TEXT})
            logging.getLogger("document-service").debug("end")
        self.assertEqual(ok.status_code, 201)
        self.assertNotIn(SECRET_TEXT, ok.text)
        self.assertNotIn(SECRET_TEXT, bad.text)
        self.assertNotIn(SECRET_TEXT, "\n".join(logs.output))


class TestConfig(unittest.TestCase):
    URL = "postgresql://harness:Pa55-secret@db.internal:6543/harness_db?sslmode=require"

    def env(self, **extra):
        base = {config.ENV_DATABASE_URL: self.URL,
                config.ENV_STORAGE_DIR: os.path.abspath(os.sep + "srv-docs")}
        base.update(extra)
        return base

    def test_full_settings_from_environment(self):
        s = config.load_settings(self.env(**{
            config.ENV_HOST: "0.0.0.0", config.ENV_PORT: "9001",
            config.ENV_DB_SCHEMA: "doc_svc", config.ENV_MAX_UPLOAD: "1000",
            config.ENV_LOG_LEVEL: "DEBUG", config.ENV_DB_TIMEOUT: "3"}))
        self.assertEqual((s.host, s.port, s.db_schema, s.max_upload_bytes, s.log_level),
                         ("0.0.0.0", 9001, "doc_svc", 1000, "debug"))
        db = s.database
        self.assertEqual((db.host, db.port, db.database, db.user, db.sslmode, db.timeout),
                         ("db.internal", 6543, "harness_db", "harness", "require", 3.0))
        self.assertEqual(db.password, "Pa55-secret")

    def test_defaults(self):
        s = config.load_settings(self.env(**{config.ENV_DATABASE_URL:
                                             "postgresql://u@h/d"}))
        self.assertEqual((s.host, s.port, s.db_schema), ("127.0.0.1", 8101, "document"))
        self.assertEqual(s.database.sslmode, "prefer")
        self.assertEqual(s.database.port, 5432)

    def test_password_never_appears_in_errors_repr_or_redaction(self):
        s = config.load_settings(self.env())
        self.assertNotIn("Pa55-secret", repr(s))
        self.assertNotIn("Pa55-secret", s.database.redacted())
        self.assertEqual(s.database.redacted(), "postgresql://harness@db.internal:6543/harness_db")
        bad = [
            "postgresql://harness:Pa55-secret@db:notaport/x",
            "mysql://harness:Pa55-secret@db/x",
            "postgresql://harness:Pa55-secret@db/x?sslmode=bogus",
            "postgresql://harness:Pa55-secret@db/x?password=Pa55-secret",
            "postgresql://:Pa55-secret@db/x",
            "postgresql://harness:Pa55-secret@db/",
        ]
        for url in bad:
            with self.subTest(url=url.split("@")[-1]):
                with self.assertRaises(config.ConfigError) as ctx:
                    config.load_settings(self.env(**{config.ENV_DATABASE_URL: url}))
                self.assertNotIn("Pa55-secret", str(ctx.exception))
                self.assertIn(config.ENV_DATABASE_URL, str(ctx.exception))

    def test_required_and_invalid_values(self):
        cases = [
            {config.ENV_DATABASE_URL: ""},
            {config.ENV_STORAGE_DIR: ""},
            {config.ENV_STORAGE_DIR: "relative/store"},
            {config.ENV_PORT: "70000"},
            {config.ENV_PORT: "eighty"},
            {config.ENV_DB_SCHEMA: "Bad-Schema"},
            {config.ENV_DB_SCHEMA: 'x"; drop'},
            {config.ENV_LOG_LEVEL: "loud"},
            {config.ENV_MAX_UPLOAD: "0"},
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                with self.assertRaises(config.ConfigError):
                    config.load_settings(self.env(**extra))


class TestMigrationFiles(unittest.TestCase):
    def test_versions_and_rendering(self):
        migrations = migrate.load_migrations()
        self.assertEqual([v for v, _ in migrations], ["0001", "0002"])
        self.assertEqual(migrate.latest_version(), "0002")
        statements = migrate.split_statements(migrate.render(migrations[0][1], "doc_x"))
        self.assertEqual(len(statements), 3)
        self.assertTrue(all('"doc_x".' in s for s in statements))
        self.assertFalse(any("{schema}" in s for s in statements))
        self.assertIn("'^[0-9a-f]{64}$'", statements[0])  # regex braces untouched
        exports = migrate.split_statements(migrate.render(migrations[1][1], "doc_x"))
        self.assertEqual(len(exports), 2)
        self.assertIn('REFERENCES "doc_x".originals', exports[0])
        self.assertIn("'[]'::jsonb", exports[0])

    def test_migration_0001_is_unchanged(self):
        """0001 is committed and may already be applied; it must never change."""
        sql = dict(migrate.load_migrations())["0001"]
        self.assertEqual(
            hashlib.sha256(sql.replace("\r\n", "\n").encode("utf-8")).hexdigest(),
            MIGRATION_0001_SHA256)

    def test_split_ignores_comments_and_blank_statements(self):
        sql = "-- c1\nCREATE TABLE a (x int);\n\n-- c2\nCREATE INDEX i ON a (x);\n;\n"
        self.assertEqual(migrate.split_statements(sql),
                         ["CREATE TABLE a (x int)", "CREATE INDEX i ON a (x)"])

    def test_quote_schema_rejects_unsafe_identifiers(self):
        from services.document_service.repository import quote_schema
        self.assertEqual(quote_schema("document"), '"document"')
        for bad in ("", 'a"b', "A", "a-b", "a b"):
            with self.assertRaises(ValueError):
                quote_schema(bad)

    def test_migrate_cli_reports_config_errors_without_secrets(self):
        env = {config.ENV_DATABASE_URL: "postgresql://u:Pa55-secret@h:bad/d"}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(migrate.main([]), 2)
        self.assertNotIn("Pa55-secret", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
