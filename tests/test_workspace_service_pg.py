"""PostgreSQL integration tests for workspace-service.

Skipped unless ``HARNESS_TEST_DATABASE_URL`` points at a disposable database.
The URL is read from the environment only and is never printed.

What these tests create and remove, and nothing else:

* one schema per test class named ``wstest_<12 hex chars>``, with the
  ``workspaces`` table and migration record that migration 0001 creates;
* temporary directories under the OS temp directory (working directory of
  the service process).

Each class's teardown runs ``DROP SCHEMA IF EXISTS "wstest_<hex>" CASCADE``
for its own generated name only. No other schema or object is touched.

Run with:  python -m unittest -v tests.test_workspace_service_pg
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.common.db import open_connection, parse_database_url, quote_schema  # noqa: E402
from services.workspace_service import config, migrate  # noqa: E402
from services.workspace_service.repository import (  # noqa: E402
    DB_STATUS_NOT_MIGRATED, DB_STATUS_OK, PostgresWorkspaceRepository,
)
from services.workspace_service.service import (  # noqa: E402
    StaleRevision, UnsupportedStateVersion, WorkspaceService, parse_bind_request,
    parse_patch_request,
)
from tests.test_workspace_service import DOC_A, DOC_B, ORIG_1, ORIG_2, bind_body  # noqa: E402

ENV_TEST_URL = "HARNESS_TEST_DATABASE_URL"
TEST_URL = os.environ.get(ENV_TEST_URL, "").strip()
LATEST = migrate.latest_version()


@unittest.skipUnless(TEST_URL, "%s is not set; PostgreSQL integration not verified" % ENV_TEST_URL)
class PostgresTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = parse_database_url(TEST_URL, timeout=10)
        cls.schema = "wstest_" + uuid.uuid4().hex[:12]
        cls.tmp = tempfile.mkdtemp(prefix="wstest-")

    @classmethod
    def tearDownClass(cls):
        conn = open_connection(cls.db, "workspace-service-tests")
        try:
            conn.run("DROP SCHEMA IF EXISTS %s CASCADE" % quote_schema(cls.schema))
        finally:
            conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def sql(self, statement, **params):
        conn = open_connection(self.db, "workspace-service-tests")
        try:
            return conn.run(statement, **params)
        finally:
            conn.close()

    def migrate(self):
        conn = open_connection(self.db, "workspace-service-tests")
        try:
            return migrate.apply_migrations(conn, self.schema)
        finally:
            conn.close()

    def service(self):
        return WorkspaceService(PostgresWorkspaceRepository(self.db, self.schema), LATEST,
                                max_body_bytes=2 * 1024 * 1024)


class TestMigrationsAndConcurrency(PostgresTestCase):
    def test_1_migrations_are_fresh_and_repeatable(self):
        repo = PostgresWorkspaceRepository(self.db, self.schema)
        self.assertEqual(repo.status(LATEST), DB_STATUS_NOT_MIGRATED)
        self.assertEqual(self.migrate(), ["0001"])
        self.assertEqual(self.migrate(), [])
        tables = {r[0] for r in self.sql(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = :s",
            s=self.schema)}
        self.assertEqual(tables, {"workspaces", "schema_migrations"})
        self.assertEqual(repo.status(LATEST), DB_STATUS_OK)

    def test_2_bind_save_and_revisions(self):
        self.migrate()
        service = self.service()
        created = service.bind(DOC_A, parse_bind_request(bind_body()))
        self.assertEqual((created["created"], created["revision"]), (True, 1))
        self.assertFalse(service.bind(DOC_A, parse_bind_request(bind_body()))["changed"])
        saved = service.save_segments(DOC_A, parse_patch_request(
            {"expected_revision": 1, "segments": {"1#0": {"text": "Eins", "comment": "c"}}}))
        self.assertEqual(saved["revision"], 2)
        same = service.save_segments(DOC_A, parse_patch_request(
            {"expected_revision": 2, "segments": {"1#0": {"text": "Eins", "comment": "c"}}}))
        self.assertEqual((same["revision"], same["changed"]), (2, False))
        with self.assertRaises(StaleRevision):
            service.save_segments(DOC_A, parse_patch_request(
                {"expected_revision": 1, "segments": {"1#0": {"text": "Zwei"}}}))
        record = self.service().get(DOC_A)["segments"]["1#0"]
        self.assertEqual((record["user_final"], record["user_comment"]), ("Eins", "c"))

    def test_3_concurrent_saves_with_the_same_revision(self):
        self.migrate()
        service = self.service()
        service.bind(DOC_B, parse_bind_request(bind_body(original_id=ORIG_2)))
        results, barrier = [], threading.Barrier(2)

        def save(text):
            barrier.wait()
            try:
                service.save_segments(DOC_B, parse_patch_request(
                    {"expected_revision": 1, "segments": {"1#0": {"text": text}}}))
                results.append(("ok", text))
            except StaleRevision:
                results.append(("stale", text))

        threads = [threading.Thread(target=save, args=(t,)) for t in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(sorted(r[0] for r in results), ["ok", "stale"])
        winner = [text for status, text in results if status == "ok"][0]
        workspace = self.service().get(DOC_B)
        self.assertEqual(workspace["revision"], 2)
        self.assertEqual(workspace["segments"]["1#0"]["user_final"], winner)

    def test_4_concurrent_first_binds_create_one_workspace(self):
        self.migrate()
        document_id = "c" * 64
        results, barrier = [], threading.Barrier(2)

        def bind():
            barrier.wait()
            results.append(self.service().bind(
                document_id, parse_bind_request(bind_body(original_id=ORIG_1))))

        threads = [threading.Thread(target=bind) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(sorted(r["created"] for r in results), [False, True])
        self.assertEqual(self.service().get(document_id)["revision"], 1)

    def test_5_isolation_and_unsupported_version(self):
        self.migrate()
        service = self.service()
        a, b = "d" * 64, "e" * 64
        service.bind(a, parse_bind_request(bind_body()))
        service.bind(b, parse_bind_request(bind_body(original_id=ORIG_2)))
        service.save_segments(a, parse_patch_request(
            {"expected_revision": 1, "segments": {"1#0": {"text": "Nur A"}}}))
        self.assertEqual(service.get(b)["segments"]["1#0"]["user_final"], "")

        q = quote_schema(self.schema)
        self.sql("UPDATE %s.workspaces SET state_version = 99 WHERE document_id = :d" % q, d=b)
        before = self.sql("SELECT revision, snapshot::text FROM %s.workspaces "
                          "WHERE document_id = :d" % q, d=b)
        for call in (lambda: service.get(b),
                     lambda: service.bind(b, parse_bind_request(bind_body(original_id=ORIG_2))),
                     lambda: service.save_segments(b, parse_patch_request(
                         {"expected_revision": 1, "segments": {"1#0": {"text": "x"}}}))):
            with self.assertRaises(UnsupportedStateVersion):
                call()
        after = self.sql("SELECT revision, snapshot::text FROM %s.workspaces "
                         "WHERE document_id = :d" % q, d=b)
        self.assertEqual(after, before)


class TestServiceProcess(PostgresTestCase):
    """The real service as a separate process, started outside the repo root."""

    def start(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env = dict(os.environ)
        env.update({
            "PYTHONPATH": ROOT,
            config.ENV_DATABASE_URL: TEST_URL,
            config.ENV_DB_SCHEMA: self.schema,
            config.ENV_HOST: "127.0.0.1",
            config.ENV_PORT: str(port),
            config.ENV_LOG_LEVEL: "warning",
        })
        workdir = tempfile.mkdtemp(prefix="wstest-cwd-", dir=self.tmp)
        proc = subprocess.Popen([sys.executable, "-m", "services.workspace_service"],
                                cwd=workdir, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

    def test_saved_work_survives_a_restart(self):
        import requests
        self.migrate()
        proc, base = self.start()
        try:
            bound = requests.post(base + "/workspaces/%s/bind" % DOC_A, json=bind_body(),
                                  timeout=30)
            self.assertEqual(bound.status_code, 201, bound.text)
            saved = requests.patch(base + "/workspaces/%s/segments" % DOC_A, timeout=30, json={
                "expected_revision": 1,
                "segments": {"1#0": {"text": "Nach Neustart", "comment": "bleibt"},
                             "2#1": {"comment": "nur Kommentar"}}})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["revision"], 2)
        finally:
            self.stop(proc)

        proc, base = self.start()
        try:
            workspace = requests.get(base + "/workspaces/%s" % DOC_A, timeout=30).json()
            self.assertEqual(workspace["revision"], 2)
            self.assertEqual(workspace["segments"]["1#0"]["user_final"], "Nach Neustart")
            self.assertEqual(workspace["segments"]["1#0"]["user_comment"], "bleibt")
            self.assertEqual(workspace["segments"]["2#1"]["user_comment"], "nur Kommentar")
            self.assertEqual(workspace["segments"]["2#1"]["user_final"], "")
            rebind = requests.post(base + "/workspaces/%s/bind" % DOC_A, json=bind_body(),
                                   timeout=30)
            self.assertFalse(rebind.json()["changed"])
            self.assertEqual(requests.get(base + "/workspaces/%s" % DOC_B,
                                          timeout=30).status_code, 404)
        finally:
            self.stop(proc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
