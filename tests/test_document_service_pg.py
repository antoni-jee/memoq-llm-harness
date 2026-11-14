"""PostgreSQL integration tests for document-service.

Skipped unless ``HARNESS_TEST_DATABASE_URL`` points at a disposable database
(``postgresql://user:password@host:port/dbname[?sslmode=...]``). The URL is
read from the environment only and is never printed.

What these tests create and remove, and nothing else:

* one schema per test run named ``doctest_<12 hex chars>`` (plus, for the
  readiness test, the name ``doctest_<hex>_absent`` is only *queried*, never
  created);
* the tables, index and migration record that migration 0001 creates inside
  that schema;
* temporary storage directories under the OS temp directory.

Teardown runs ``DROP SCHEMA IF EXISTS "doctest_<hex>" CASCADE`` for that
generated name only. No other schema or object is touched.

The database role needs CREATE on the database.

Run with:  python -m unittest -v tests.test_document_service_pg
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.document_service import config, migrate  # noqa: E402
from services.document_service.repository import (  # noqa: E402
    DB_STATUS_NOT_MIGRATED, DB_STATUS_OK, OriginalRecord, PostgresDocumentRepository,
    open_connection, quote_schema,
)

ENV_TEST_URL = "HARNESS_TEST_DATABASE_URL"
TEST_URL = os.environ.get(ENV_TEST_URL, "").strip()
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "sample.mqxliff")


@unittest.skipUnless(TEST_URL, "%s is not set; PostgreSQL integration not verified" % ENV_TEST_URL)
class PostgresTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = config.parse_database_url(TEST_URL, timeout=10)
        cls.schema = "doctest_" + uuid.uuid4().hex[:12]
        cls.tmp = tempfile.mkdtemp(prefix="doctest-")

    @classmethod
    def tearDownClass(cls):
        conn = open_connection(cls.db, application_name="document-service-tests")
        try:
            conn.run("DROP SCHEMA IF EXISTS %s CASCADE" % quote_schema(cls.schema))
        finally:
            conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def migrate(self):
        conn = open_connection(self.db)
        try:
            return migrate.apply_migrations(conn, self.schema)
        finally:
            conn.close()


class TestMigrationsAndRepository(PostgresTestCase):
    def test_1_migrations_create_schema_and_are_idempotent(self):
        repo = PostgresDocumentRepository(self.db, self.schema)
        self.assertEqual(repo.status("0001"), DB_STATUS_NOT_MIGRATED)
        self.assertEqual(self.migrate(), ["0001"])
        self.assertEqual(self.migrate(), [])
        conn = open_connection(self.db)
        try:
            tables = {r[0] for r in conn.run(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = :s",
                s=self.schema)}
        finally:
            conn.close()
        self.assertEqual(tables, {"documents", "originals", "schema_migrations"})
        self.assertEqual(repo.status("0001"), DB_STATUS_OK)

    def test_2_repository_round_trip_survives_new_instance(self):
        self.migrate()
        record = OriginalRecord(original_id="a" * 64, document_id="b" * 64,
                                filename="x.mqxliff", size_bytes=10, segment_count=3)
        stored, created = PostgresDocumentRepository(self.db, self.schema).add_original(record)
        self.assertTrue(created)
        again, created_again = PostgresDocumentRepository(self.db, self.schema).add_original(
            OriginalRecord(**{**record.__dict__, "filename": "other.mqxliff"}))
        self.assertFalse(created_again)
        self.assertEqual(again.filename, "x.mqxliff")

        fresh = PostgresDocumentRepository(self.db, self.schema)
        self.assertEqual(fresh.get_original("a" * 64).segment_count, 3)
        doc = fresh.get_document("b" * 64)
        self.assertEqual([o.original_id for o in doc.originals], ["a" * 64])
        self.assertIsNone(fresh.get_original("c" * 64))
        self.assertIsNone(fresh.get_document("c" * 64))

    def test_3_readiness_reports_an_unmigrated_schema(self):
        absent = PostgresDocumentRepository(self.db, self.schema + "_absent")
        self.assertEqual(absent.status("0001"), DB_STATUS_NOT_MIGRATED)


class TestServiceProcess(PostgresTestCase):
    """The real service as a separate process, started outside the repo root."""

    def start(self, storage_dir):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env = dict(os.environ)
        env.update({
            "PYTHONPATH": ROOT,
            config.ENV_DATABASE_URL: TEST_URL,
            config.ENV_DB_SCHEMA: self.schema,
            config.ENV_STORAGE_DIR: storage_dir,
            config.ENV_HOST: "127.0.0.1",
            config.ENV_PORT: str(port),
            config.ENV_LOG_LEVEL: "warning",
        })
        workdir = tempfile.mkdtemp(prefix="doctest-cwd-", dir=self.tmp)
        proc = subprocess.Popen(
            [sys.executable, "-m", "services.document_service"],
            cwd=workdir, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = "http://127.0.0.1:%d" % port
        import requests
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                self.fail("service exited early with code %s" % proc.returncode)
            try:
                if requests.get(base + "/readyz", timeout=2).status_code == 200:
                    return proc, base
            except requests.RequestException:
                pass
            time.sleep(0.3)
        proc.terminate()
        self.fail("service did not become ready within 30 s")

    @staticmethod
    def stop(proc):
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def test_upload_restart_and_retrieve(self):
        import requests
        self.migrate()
        storage = os.path.join(self.tmp, "storage")
        with open(FIXTURE, "rb") as handle:
            raw = handle.read()

        proc, base = self.start(storage)
        try:
            up = requests.post(base + "/documents", timeout=30,
                               files={"file": ("sample.mqxliff", raw, "application/xml")})
            self.assertEqual(up.status_code, 201, up.text)
            body = up.json()
            self.assertEqual(body["original_id"], hashlib.sha256(raw).hexdigest())
            segments = requests.get(base + "/originals/%s/segments" % body["original_id"],
                                    timeout=30).json()["segments"]
            self.assertEqual(len(segments), body["segment_count"])
        finally:
            self.stop(proc)

        proc, base = self.start(storage)
        try:
            doc = requests.get(base + "/documents/%s" % body["document_id"], timeout=30)
            self.assertEqual(doc.status_code, 200)
            self.assertEqual([o["original_id"] for o in doc.json()["originals"]],
                             [body["original_id"]])
            content = requests.get(base + "/originals/%s/content" % body["original_id"],
                                   timeout=30)
            self.assertEqual(content.content, raw)
            again = requests.post(base + "/documents", timeout=30,
                                  files={"file": ("sample.mqxliff", raw, "application/xml")})
            self.assertEqual(again.status_code, 200)
            self.assertFalse(again.json()["created"])
        finally:
            self.stop(proc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
