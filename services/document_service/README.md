# document-service

Stores original memoQ MQXLIFF files, parses them and serves their segments.
It is one of the backend services being extracted from the Streamlit app; the
app does not call it yet (that comes in a later slice).

## Identifiers

| Identifier | Meaning |
| --- | --- |
| `document_id` | The logical source document: `MqxliffDocument.source_fingerprint()`. Files whose fingerprinted source is identical share it, even if their targets differ. |
| `original_id` | The exact uploaded bytes: SHA-256 of the file. |

* An upload returns both. Uploading the same bytes again is idempotent
  (HTTP 200, `created: false`, the first upload's record is returned).
* Different bytes with the same `document_id` are separate originals, each
  addressable by its own `original_id`. Nothing selects a "latest" file.
* Segments are always read from one specific original.

## API

| Method and path | Purpose |
| --- | --- |
| `GET /healthz` | Liveness. No dependency checks; 200 while the process serves HTTP. |
| `GET /readyz` | Readiness. 200 only if the database is reachable **and** migrated to the latest version, and the storage directory accepts a create/delete probe; otherwise 503 with `checks.database` (`ok`, `unavailable`, `not_migrated`) and `checks.storage` (`ok`, `unavailable`). |
| `POST /documents` | Upload: `multipart/form-data`, field `file`. 201 new original, 200 repeated upload. Rejections store nothing: 400 empty/missing file, 411 no Content-Length, 413 too large, 415 not multipart, 422 not readable MQXLIFF. |
| `GET /documents/{document_id}` | The document and all of its originals. |
| `GET /originals/{original_id}` | Metadata of one original. |
| `GET /originals/{original_id}/content` | The stored bytes, unchanged. |
| `GET /originals/{original_id}/segments` | Parsed segments. Query: `tag_source` (`target` default, or `source`) and the filter flags `skip_confirmed`, `skip_locked`, `use_status_attribute`, `use_target_state`, `require_nonempty_source` (`true`/`false`, default `true`). Every segment is returned with its `skip_reason`. |

Errors are JSON: `{"error": "<code>", "message": "<text>"}`. Messages and logs
never contain document text, the database URL or credentials. 503 means the
database or storage is unavailable; 500 `storage_inconsistent` means a
recorded file is missing or altered.

## Configuration

All settings are environment variables; see
[`document-service.env.example`](document-service.env.example).

| Variable | Required | Default |
| --- | --- | --- |
| `DOCUMENT_DATABASE_URL` | yes | - |
| `DOCUMENT_STORAGE_DIR` | yes (absolute path) | - |
| `DOCUMENT_SERVICE_HOST` | no | `127.0.0.1` |
| `DOCUMENT_SERVICE_PORT` | no | `8101` |
| `DOCUMENT_DB_SCHEMA` | no | `document` |
| `DOCUMENT_DB_TIMEOUT_SECONDS` | no | `10` |
| `DOCUMENT_MAX_UPLOAD_BYTES` | no | `52428800` |
| `DOCUMENT_LOG_LEVEL` | no | `info` |

The service does not depend on its working directory. The repository root
must be importable (`PYTHONPATH=<repo root>`), because the service reuses the
`memoq_harness` package.

## Running

From any directory, with the variables set:

```sh
pip install -r services/document_service/requirements.txt
python -m services.document_service.migrate --status   # show applied/pending
python -m services.document_service.migrate            # apply pending migrations
python -m services.document_service                    # start the HTTP server
```

`migrate` creates the schema if needed, applies pending `migrations/NNNN_*.sql`
files in their own transactions under an advisory lock, records them in
`<schema>.schema_migrations`, and never drops anything. The database role
needs CREATE on the database, or an existing schema it owns. Exit codes: 0
success, 1 database failure, 2 configuration error.

Startup exits with code 2 and a one-line message if configuration is missing
or invalid. Run `migrate` before starting a new version; until then
`/readyz` reports `not_migrated`.

## Storage

Originals are stored as `<DOCUMENT_STORAGE_DIR>/originals/<aa>/<original_id>.mqxliff`,
written via a temporary file and an atomic rename. Client file names are kept
as metadata only and never used in paths. A file is written before its
database row. If the database insert fails, the file is **kept**: an
overlapping upload of the same bytes may already own it, and no pre-delete
check could rule that out without a race. Such a file is never served without
a database record, and the next successful upload of the same bytes reuses it.
There is no garbage collection of unreferenced files yet.

## Tests

```sh
pip install -r services/document_service/requirements-test.txt
python -m unittest -v tests.test_document_service      # offline unit tests
python -m unittest -v tests.test_document_service_pg   # needs HARNESS_TEST_DATABASE_URL
```

The PostgreSQL tests are skipped unless `HARNESS_TEST_DATABASE_URL` names a
disposable database. Per run they create one schema `doctest_<random hex>`
per test class and drop exactly that schema at the end; nothing else is
modified. The service-process test also starts the service twice as a
separate process from an unrelated working directory.
