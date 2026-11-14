# processing-service

Stateless prompt building and model calls. It builds the system prompt with
`memoq_harness.prompts.build_batch_prompt` - the same assembly the Streamlit
app uses - and runs either the deterministic offline **stub** engine
(default) or a real engine configured on the server, through the existing
`memoq_harness.llm.LLMClient` (`translate_batch` / `translate_one`).

No database, no document files, no calls to other services. The caller
(later the Streamlit remote mode) supplies the segments and the prompt
context, adds tag reports from document-service and submits the suggestions
to workspace-service.

## API

| Method and path | Purpose |
| --- | --- |
| `GET /healthz` | Liveness. No dependency checks. |
| `GET /readyz` | 200 if the configured engine is usable (`checks.engine`: `ok` or `sdk_missing`). Never calls a model. |
| `POST /process` | Suggestions for explicit segments. |

```json
POST /process
{"operation": "batch",
 "engine": "stub",
 "tag_source": "target",
 "batch": {"index": 0, "count": 3},
 "context": {"source_lang": "en", "target_lang": "de", "hard_rules_text": "...",
             "vibe": "...", "termbase": [{"source": "dashboard", "target": "...", "note": ""}],
             "corrections_block": "", "directive": "", "stage": "auto", "template": null},
 "segments": [{"key": "1#0", "source_text": "...", "mt_target": "...", "user_comment": ""}]}
```

* `operation`: `batch` (default; one or more segments; `translate_batch`,
  chunked, one retry for skipped rows) or `rerun` (exactly one segment;
  `translate_one`, as the app's per-row Rerun). Never inferred from the
  number of segments.
* `segments`: the workspace records of the rows to process (`mt_target` is
  memoQ's target placeholder text). Keys must be unique.
* `context`: every field is optional and falls back to the shipped defaults
  (rules, vibe, stage templates); `stage: "auto"` picks the stage per batch
  like the app. The termbase is filtered with the app's own
  `ProjectState.relevant_terms`. Nothing is stored between requests.
* `engine`: optional; only `stub` or the server-configured engine. A client
  can never supply an endpoint, model or key.

Response 200:

```json
{"operation": "batch", "engine": "stub", "tag_source": "target", "stage": "mt1",
 "suggestions": {"1#0": {"text": "..."}}, "missing": [], "errors": [],
 "requests": 1, "elapsed_ms": 2, "system_prompt_chars": 5120}
```

`suggestions` already has workspace-service's submission shape; add
`tag_report` from document-service's `POST /originals/{id}/tag-reports` and
submit with the workspace revision read before processing. Empty results are
listed in `missing`, never in `suggestions`.

Errors: 400 invalid request (whole request rejected), 413 body too large,
415 not JSON, 503 `engine_unavailable` (e.g. SDK not installed), 502
`engine_failed` (every engine request failed). Partial failures are 200 with
`missing` and a sanitized `errors` line. Responses and logs never contain
provider error text, raw model responses, prompts, segment text or keys.

## The stub engine

Deterministic, offline and free. For each segment it returns `"MT "` plus the
placeholder text of the canonical side for the request's `tag_source`
(memoQ's target in `target` mode, the source in `source` mode), falling back
to the other side when the canonical side is empty. Output therefore keeps
the placeholders the tag check expects; tests verify against
document-service's real tag-report logic in both modes, including rows with
an empty target. A row whose canonical side has text but no tags while the
other side has tags would get a tag warning, like real model output without
tags. Stub output may coincide with memoQ's target text.

## Configuration

See [`processing-service.env.example`](processing-service.env.example).

| Variable | Default |
| --- | --- |
| `PROCESSING_SERVICE_HOST` / `PROCESSING_SERVICE_PORT` | `127.0.0.1` / `8103` |
| `PROCESSING_ENGINE` | `stub` (`openai`, `anthropic`) |
| `PROCESSING_MODEL` | required for real engines |
| `PROCESSING_BASE_URL` | SDK default |
| `PROCESSING_API_KEY` | empty (local endpoints need none) |
| `PROCESSING_TIMEOUT_SECONDS`, `_MAX_RETRIES`, `_CHUNK_SIZE`, `_TEMPERATURE`, `_MAX_TOKENS` | `300`, `3`, `20`, `0.2`, `4096` |
| `PROCESSING_MAX_SEGMENTS`, `PROCESSING_MAX_BODY_BYTES` | `200`, `20971520` |
| `PROCESSING_LOG_LEVEL` | `info` |

The repository root must be importable (`PYTHONPATH=<repo root>`); the
service does not depend on its working directory and writes no files.

## Running

```sh
pip install -r services/processing_service/requirements.txt          # stub only
pip install -r services/processing_service/requirements-engines.txt  # + real engines
python -m services.processing_service
```

Startup exits with code 2 on invalid configuration. A real engine may be
billable; the stub never makes a network call.

## Tests

```sh
pip install -r services/processing_service/requirements-test.txt
python -m unittest -v tests.test_processing_service
```

Offline; outbound connections are blocked in the tests and real engines are
exercised only through a patched `LLMClient._chat`.

## Limitations

* No persistent settings: the caller supplies rules, vibe, templates and
  termbase per request (session-scoped in the frontend) or gets defaults.
* Corrections block defaults to empty (Apply/corrections is deferred).
* Model-proposed rules/vibe are not returned or applied.
* Tag reports are computed by document-service, not here.
