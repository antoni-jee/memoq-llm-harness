# workspace-service

Owns saved document work: per-segment manual translations, comments and
human-edit flags, one workspace per document, in PostgreSQL. It reuses the
existing `memoq_harness.state.ProjectState` logic (`bind_segments`,
`capture_edits`, `record_suggestions` and the v5 per-document snapshot) in
memory for one document per request; the snapshot is stored as JSONB instead
of `state.json`. It stores model suggestions supplied by the caller; it does
not call a model.

Not here yet: the Apply step that turns edits into corrections
(`calculate_diffs`), prompt context, global settings (`batch_size` is fixed
at 100) and the frontend, which does not call this service yet. The service never reads document files or calls document-service;
the caller supplies segment records it obtained from document-service.

## Identifiers

* `document_id` - document-service's source fingerprint. A workspace is keyed
  by it, because segment keys are only unique within one document.
* segment `key` - `<trans-unit id>#<ordinal>`, as returned by document-service.
* `original_id` - the original the caller last bound (`bound_original_id`).
  Different originals of the same `document_id` share one workspace.

## API

| Method and path | Purpose |
| --- | --- |
| `GET /healthz` | Liveness. No dependency checks. |
| `GET /readyz` | Readiness: 200 only if the database is reachable and migrated to the latest version; otherwise 503 with `checks.database` (`ok`, `unavailable`, `not_migrated`). |
| `POST /workspaces/{document_id}/bind` | Open or create the workspace and merge segment records. 201 created, 200 otherwise. |
| `GET /workspaces/{document_id}` | The saved workspace (`revision`, `order`, `segments` with every record field). 404 if unknown. |
| `PATCH /workspaces/{document_id}/segments` | Save manual text and/or comments. |
| `POST /workspaces/{document_id}/suggestions` | Record model suggestions; prefills rows not protected by a human edit. |

Errors are JSON `{"error", "message"}` plus `details` with identifiers only.
Messages and logs never contain segment text, the database URL or
credentials.

### Bind

```json
POST /workspaces/{document_id}/bind
{"original_id": "<sha256>", "source_file": "report.mqxliff", "tag_source": "target",
 "segments": [{"key": "1#0", "unit_id": "1", "source_text": "...", "target_text": "..."}],
 "present_keys": ["1#0", "2#1", "3#2"]}
```

`segments` is the active selection in order (rows the filter shows);
`present_keys` is every key of the full document, so rows hidden by the filter
keep their saved work. The whole request is validated before anything
changes: unknown fields, wrong types, duplicate keys, duplicate
`present_keys` or a segment key missing from `present_keys` -> 400.

Rebinding keeps human work (`user_final`, `user_comment`, `edited_by_human`)
and refreshes `mt_target` from the supplied target, also when the same
`document_id` is opened from a different original with changed targets.
Refused with 409 and nothing changed:

* `tag_source_mismatch` - the workspace was created with the other tag source
  (placeholder text would not match its saved work);
* `would_discard_work` - a key with saved human work is missing from
  `present_keys`, or its `source_text` changed. Saved human work means a
  manual-edit flag, a non-empty comment, or non-empty text different from
  memoQ's target.

### Save

```json
PATCH /workspaces/{document_id}/segments
{"expected_revision": 3,
 "segments": {"1#0": {"text": "...", "comment": "..."}, "2#1": {"comment": ""}}}
```

* `text` and `comment` are both optional per key, but the object must not be
  empty. Omitted means keep; `"comment": ""` clears the comment.
* Text goes through `ProjectState.capture_edits`, which sets
  `edited_by_human` when the text differs from the baseline. A comment-only
  update changes the comment and nothing else.
* Unknown keys -> 422 `unknown_segment`; bad shapes -> 400; unknown
  workspace -> 404. Nothing is applied unless the whole request is valid.

### Suggestions

```json
POST /workspaces/{document_id}/suggestions
{"expected_revision": 4,
 "suggestions": {"1#0": {"text": "...", "tag_report": "missing 2"}, "2#1": {"text": "..."}}}
```

Recorded with `ProjectState.record_suggestions`, the rule the app uses after
Process Current Batch and Rerun. For every named segment it sets
`llm_suggestion`, `tag_report` (the supplied text, or empty) and
`status: "processed"`, and copies the suggestion into `user_final` unless the
row is protected by `edited_by_human`. A row with only a comment is not
protected: its text is prefilled and its comment kept. This slice does not
create corrections or mark rows applied.

```json
{"document_id": "...", "revision": 5, "changed": true,
 "recorded_keys": ["1#0", "2#1"], "prefilled_keys": ["2#1"], "protected_keys": ["1#0"]}
```

* `text` is required and must not be empty or whitespace-only (the app keeps
  memoQ's target when the model returns nothing). Accepted text is stored
  exactly as sent. `tag_report` is optional, computed by the caller (this
  service has no tag data), at most 1000 characters.
* Rejected as a whole, nothing applied: bad shapes -> 400; unknown keys ->
  422 `unknown_segment`; keys outside the active selection (rows hidden by
  the filter) -> 422 `inactive_segment`; stale revision -> 409.

### Revisions

* A new workspace starts at revision 1.
* A bind that changes nothing (same selection, same original, same stored
  snapshot) is a no-op and keeps the revision; any other bind increments it.
* A save or a suggestions submission must send `expected_revision`; it is
  compared first. If the workspace has changed since, the request is
  rejected with 409 `stale_revision` (`details.current_revision`) and
  nothing is applied - this includes replaying an identical earlier request
  with its old revision. Identical content sent with the current revision is
  a no-op that keeps the revision; an effective change increments it.
* A processing result computed from revision N is therefore refused if any
  edit, comment or rebind happened after N; re-read and resubmit.
* Every change runs in one transaction holding the workspace row lock, so
  concurrent requests serialise and a bind never overwrites a save.

## Storage

Table `<schema>.workspaces`: `document_id` (primary key), `snapshot` (JSONB,
the v5 per-document entry of `state.json`, unknown fields kept),
`state_version`, `revision`, `tag_source`, `bound_original_id`, timestamps.
No foreign key into document-service's schema. A row whose `state_version`
is not the supported one (5) is refused with 409 `unsupported_state_version`
and left untouched. Importing an existing `state.json` is not supported.

## Configuration

All settings are environment variables; see
[`workspace-service.env.example`](workspace-service.env.example).

| Variable | Required | Default |
| --- | --- | --- |
| `WORKSPACE_DATABASE_URL` | yes | - |
| `WORKSPACE_SERVICE_HOST` | no | `127.0.0.1` |
| `WORKSPACE_SERVICE_PORT` | no | `8102` |
| `WORKSPACE_DB_SCHEMA` | no | `workspace` |
| `WORKSPACE_DB_TIMEOUT_SECONDS` | no | `10` |
| `WORKSPACE_MAX_BODY_BYTES` | no | `20971520` |
| `WORKSPACE_LOG_LEVEL` | no | `info` |

The repository root must be importable (`PYTHONPATH=<repo root>`); the
service does not depend on its working directory and writes no files.

## Running

```sh
pip install -r services/workspace_service/requirements.txt
python -m services.workspace_service.migrate --status
python -m services.workspace_service.migrate
python -m services.workspace_service
```

`migrate` uses the runner shared with document-service
(`services/common/migrations.py`) with its own advisory-lock namespace;
exit codes 0 success, 1 database failure, 2 configuration error. Startup exits
with code 2 on missing or invalid configuration.

## Tests

```sh
pip install -r services/workspace_service/requirements-test.txt
python -m unittest -v tests.test_workspace_service      # offline
python -m unittest -v tests.test_workspace_service_pg   # needs HARNESS_TEST_DATABASE_URL
```

The PostgreSQL tests create one schema `wstest_<random hex>` per test class
and drop exactly that schema; they cover migrations, revisions, concurrent
saves and binds, a suggestions submission racing a save, isolation, an
unsupported stored version and a restart of the service process with saved
edits and suggestions.

## Limitations

* `batch_size` is fixed at 100 until global settings move to a service.
* The service trusts the caller's segment records; it does not verify them
  against document-service.
* Identical source XML shares one workspace (document-service identity rule).
* No Apply/corrections step, prompt context or export selection yet.
* Suggestions for rows changed since `expected_revision` are refused as a
  whole; there is no per-row merge.
