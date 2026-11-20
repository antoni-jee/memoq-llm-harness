# Project Context - memoQ LLM Harness

Goals, architecture, findings and the phased roadmap.
Maintained by the session. It is not a test report: figures here are the
latest recorded results, not re-run on read.

## 1. Goals (priority order, user-approved)
1. DONE - Phase 1 working baseline (§5).
2. Primary goal now: School 21 DO14 and a DevOps portfolio built around this
   application. Further commercial translation features are not the goal.
3. Budget is limited: prefer small, verifiable steps and local tooling.
Product enhancements are backlog unless a milestone below needs them.

## 2. Actual use vs. future infrastructure
- A self-authored memoQ post-editing tool developed around the author's
  technical-localization workflow. The current release is a local,
  single-application baseline (one Streamlit process).
- Current production workflow (unchanged): Translum export -> memoQ import ->
  TM/TB -> lock non-translatable -> pre-translate exact TM -> LiveDocs
  alignment -> LiveDocs pre-translation -> bilingual DOCX -> Perplexity ->
  manual transfer into memoQ -> HTML export -> external checker.
- Quality versus the Perplexity workflow is not established. A memoQ
  re-import of a harness export is not confirmed.
- Commercial-use claims and performance figures must be based on specific
  user-confirmed facts. Do not attribute future portfolio infrastructure
  to earlier commercial use.
- On feat/remote-workflow (up to the 1483731 baseline) the three backend
  services (document, workspace, processing) and a Postgres data layer
  (migrations, services/common/db.py) plus the remote HTTP transport
  (harness_remote/) now exist as committed code. Containers, Helm/Kubernetes,
  GitLab CI/CD, IaC, DNS/TLS, FluxCD and observability (§6 steps 3-6) are still
  not implemented.

## 3. Current architecture (single application)
- app.py: Streamlit UI AND orchestration (load, document switching, batch
  processing, Rerun, apply/diff, export, model Test handling). A remote mode
  (HARNESS_BACKEND=remote) instead talks to the services over HTTP and renders a
  read-only projection of the saved workspace via harness_remote/view.py; in
  that mode it never builds a local ProjectState.
- The remote slice's orchestration and UI are extracted from app.py into three
  Streamlit-coupled modules at the repo root (kept out of harness_remote, which
  stays Streamlit-free), a behavior-preserving refactor (uncommitted on top of
  fdbd6a7): ui_common.py (UI helpers both paths share - APP_TITLE, notify,
  flush_notice, upload_marker, upload_needs_load, _display), remote_workflow.py
  (Open/Reload orchestration, the segments-envelope validation, the bind-payload
  projection, and get_remote_clients with its call-time build_clients seam), and
  remote_ui.py (the remote sidebar, grid and row). app.py re-imports every
  shared name so its namespace and local behaviour are unchanged; no extracted
  module imports app, so there is no circular import.
- memoq_harness/: mqxliff.py (parse, fingerprint, export), tags.py
  (placeholders, target-canonical tags), state.py (state.json v5), llm.py
  (OpenAI-compatible and Anthropic adapters, bounded probe), prompts.py,
  diffing.py, rtf.py, xmlcompat.py.
- Generated, never hand-edited: defaults.py, prompt_templates.py
  (generator: tools/build_defaults.py).
- Data: config/termbase.csv. Local state: state.json. Exports: exports/.
- Backend services and a Postgres data layer exist (see §2); the frontend now
  has a remote mode that talks to them over HTTP. Containers, CI/CD and
  deployment do not exist yet.

## 4. Persistence (implemented)
- state.py: schema v5 with migrations; save via sibling .tmp + os.replace;
  .corrupt copy on JSON load failure. Keep both guarantees.
- One snapshot per document keyed by source_fingerprint() v2: SHA-256 of
  per-<file> language pairs and, per trans-unit, id, ordinal and canonical
  <source>/<seg-source>. Names, paths, targets, statuses, filter and tag mode
  are excluded.
- Legacy (<v5) state -> retained "legacy" entry plus verbatim copy; never
  auto-attached; byte-for-byte .vN.bak before first overwrite.
- Fingerprint limits: identical source XML shares state despite different
  translations. Changes to fingerprinted source content or structure—including
  segment IDs, order and segmentation markup—create a new document identity.
  Namespace prefixes, attribute order, comments and processing instructions
  are ignored. No revision matching or transfer.

## 5. Phase 1 baseline (closed)
Commits on experiment/local-qwen:
- c0ffe3a: unapplied human edits survive regeneration.
- bc68986: applied edit recovery after session restart (test).
- 5c3a918: saved segment edits survive filter changes.
- f1d9ae3: per-document state isolation and restoration.
- 2ec77cf: bounded model Test (ok / inconclusive / error), sanitized
  endpoint/model/credential labels, key cleared on provider switch, Rerun
  queued and run with full-width progress, readable Rerun button.

Automated evidence (agent-run, offline, fake model clients, at 2ec77cf):
- `.venv\Scripts\python.exe -m unittest -v tests.test_core`: 268 tests OK.
- `.venv\Scripts\python.exe tests\test_app.py`: 76 tests OK.
- AppTest "restart" = fresh session sharing state.json, not a process restart.
- run_tests.py and test_live.py were not run.

User-reported manual browser checks (not automated):
- A -> B -> A restores each document's translations and comments.
- Same after a real Streamlit stop/start and reloading the original files.
- Rerun keeps existing human text and updates the model suggestion.
- An unapplied edit in another row is captured by Rerun and survives restart;
  the model suggestion survives too. (Captured by a Rerun action; arbitrary
  typing is not claimed to autosave.)
- Final Rerun layout visually accepted.
- Local Qwen (Ollama) Test: inconclusive, finish_reason=length, 16 output
  tokens, reasoning present, no visible final text.

## 5a. Remote-workflow read-only baseline (committed as fdbd6a7; user-run acceptance complete)
Starting baseline: 1483731 on feat/remote-workflow - the last commit at the
start of this slice; it is NOT a commit containing the slice's work. Up to that
point the history adds the three backend services and a Postgres data layer
(9db7505, d19e5c7, f1f75cb, 59ca3ae) and the remote HTTP transport (1483731).
The slice itself (a local/remote mode seam in app.py, a read-only remote UI
built on harness_remote/view.py, and tests/test_remote_view.py plus
tests/test_remote_app.py) is committed as fdbd6a7 "feat: add read-only remote
UI", which the user pushed to origin/feat/remote-workflow. The service-level
test suites (tests/test_{document,workspace,processing}_service*.py) exist but
were not run this session; only the four app/view/client suites were (148
passed, 0 failed, 0 skipped - see SESSION_HANDOFF.md §3). The Open/Reload path
unwraps and validates the document-service segments envelope via a narrow
helper, and the active selection is projected to the four fields the workspace
bind parser accepts while present_keys carries every document key; a regression
feeds the app's actual bind payload to the real parse_bind_request (see
SESSION_HANDOFF.md §2). The Open/Reload and bind-contract fixes, and the remote
read-only behaviors, were manually accepted by the user against the real
backend services in a browser session (see SESSION_HANDOFF.md §3); this does not
complete the remote workflow, which remains in progress.
On top of fdbd6a7, a behavior-preserving extraction refactor splits app.py's
remote responsibilities into ui_common.py, remote_workflow.py and remote_ui.py
(§3); committed and pushed as 5258ffc "refactor: split remote UI and
workflow" (148 passing in the four suites).

## 5b. Remote edit/save slice (committed as fcba7a3 on top of 5258ffc; user browser acceptance recorded)
- Editable "Your text" (text_area) and comment (text_input) cells per row feed a
  session-scoped draft store in remote_workflow.py:
  {document_id: {key: {text, comment, base_text, base_comment}}}. Original
  target, model suggestion, saved user text and the unsaved draft stay
  distinct (the saved value is shown under an edited cell). Drafts survive
  reruns and batch/page navigation; they do not survive a browser refresh.
- Explicit Save sends only edited fields (an explicit "" is sent as "") through
  the existing PATCH /workspaces/{id}/segments contract with the view's
  revision as expected_revision; no edited field means no request. No backend
  or client code changed.
- Dirty Open/Reload is held for an explicit answer: Save and open/reload,
  Discard and open/reload, Reload keeping changes, or Cancel. A failed or
  stale Save stops the transition and keeps the drafts. A successful Open
  starts with an empty draft store, so drafts never move between documents.
- Conflicts: after a 409 stale_revision the app re-reads (read only) and
  rebases the drafts per field; an edited field whose saved value changed
  elsewhere is a conflict that disables Save until the operator picks Keep
  mine or Use server on that row. Nothing is replayed automatically.
- Ambiguous failures (transport error, 5xx, malformed 2xx): no "saved" claim;
  the app re-reads to reconcile and marks clean only changes the server holds.
  If the re-read fails, drafts stay unsaved and a later Save at the old
  revision is refused by the server, so nothing is overwritten.
- Fix pass after the first browser test: Save/Discard moved to the sidebar
  above Reload (always visible; disabled when not applicable; Save primary
  when an eligible draft exists) with a "drafts until Save; a browser refresh
  can lose them" note. A Conflicts panel above the grid lists every conflict
  (any batch/page) with draft / now-saved / started-from values and Keep
  mine / Use server / Show row; choices only change the draft. While a
  conflict is unresolved, Save, Save-and-open and Save-and-reload are disabled
  and refused. The sidebar shows the open document (name, revision, id)
  apart from the uploader's selection; Cancel empties the uploader (fresh
  widget key). Reselecting the open file version (same name and bytes) does
  nothing - no upload, bind or prompt.
- Widget re-keying is per row: within one document a handler re-keys only
  the cells whose shown value changes (Use server, Discard, a saved value that
  moved); kept drafts (after a 409/re-read, Keep mine, a just-saved edit) keep
  their widget and are not remounted in the browser. Open re-keys everything.
- UI follow-ups (deferred, not started): compact conflict layout; Show row
  behaviour on the same page (it only switches batch/page, no scroll);
  network-error dialog UX; copy-to-draft buttons.
- Tests: tests/test_remote_edit.py (30) drives the app against the real
  WorkspaceClient + workspace Starlette app + WorkspaceService over an
  in-memory repository. These are headless/mocked-app tests (a real HTTP
  transport over a Starlette app, but no live model and no real browser); they
  are NOT a substitute for real-service browser acceptance. Final-code results
  (193 across the five agreed suites: test_remote_view 8, test_remote_app 29,
  test_remote_client 50, test_app 76, test_remote_edit 30) and the user's
  separate browser acceptance are in SESSION_HANDOFF.md §3. Not confirmed in the
  browser: Save -> Reload after a workspace tunnel reconnects.

## 5c. Remote model / process-one-batch slice (uncommitted on top of fcba7a3; live-HTTP smoke passed; browser acceptance FAILED on automatic human-text acceptance - not ready to commit)
- The remote sidebar now mirrors the LOCAL model configuration and selection
  workflow (the earlier restriction not to duplicate it is withdrawn): a "Check
  service readiness" button (a GET /readyz service/SDK check that reports the
  service's own configured engine; it is NOT a model test and never calls the
  model), a "Provider preset" selectbox defaulting to the local default preset
  "Local Engine (Ollama)" (the offline "Test backend (stub)" is an explicit,
  labelled option alongside it, not the default), an editable Base URL (noted as
  reachable FROM the processing service), a password API-key field shown only for
  key-requiring presets, a Model selectbox/entry, and "Test model" / "List models"
  actions. The operator chooses the model in the UI; they never edit an
  environment variable, start a service or change a port. It reads nothing from
  Qwen Code's config (~/.qwen, agent credentials).
- Per-request model selection: the sidebar edits a live `llm.ClientConfig` and
  builds from it a validated "config" block (preset, backend, base URL, the
  session-only key, model, and the generation controls) that the processing
  service selects the real backend/endpoint/model from on POST /process,
  POST /probe and POST /models. A real preset sends the config (no engine); the
  explicit stub sends engine "stub" instead - the two are mutually exclusive on
  the client and enforced again by the service (both = 400). A call that omits
  config falls back to the service's own configured engine (backward compatible),
  but an explicit config is always used as given and never falls back to the
  service environment or the stub; an invalid explicit config fails safely
  (400/503). "Test model" runs one bounded probe and stores nothing; "List
  models" runs one list call whose result is cached and invalidated (the provider
  is not called) when the connection settings change.
- Credential scope: the key is held in the Streamlit browser session only. It is
  sent to the processing service for one request and is never written to
  state.json, a tracked file or a log, and never echoed in an error or exception
  repr; a key-requiring preset with an empty key fails with an actionable
  sanitized "Enter an API key in the sidebar" error. Local Ollama (no key) still
  works; a legacy no-config request keeps the service's own environment behavior.
- One gated mutation is offered in the grid: "Process Current Batch" sends the
  currently selected batch's *eligible* rows to the model through the processing
  service with the current config (context=None, so the service builds the prompt
  from its shipped defaults and the batch stage) and persists the proposals
  through the existing workspace contract. A row is eligible on its own effective
  'Your text' - a held draft (including one the operator cleared to blank) when
  one is held, otherwise the saved human text - judged blank after trimming; a
  filled neighbour, a prior model proposal or an edited status never blocks an
  otherwise-eligible row, and a row is sent at most once. The grid shows the
  eligible count and sends no request when it is zero. There is no direct
  Streamlit->LLM call and no local workflow fallback.
- Proposals are persisted as the model's llm_suggestion via the existing
  POST /workspaces/{id}/suggestions (record_suggestions) with prefill OFF, NOT
  as user_final: the four data kinds stay distinct (original target / model
  suggestion / saved human text / draft), and a Reload shows the persisted
  proposals in the "Model proposal" column. The human's saved text, comments,
  edited_by_human flag and the original target are never touched by Process -
  a model proposal is not a human acceptance. Putting a proposal into "Your
  text" therefore requires an explicit user action (manual editing, already
  available) that creates an ordinary unsaved draft; only Save persists that
  human-text change, and Discard before Save restores the prior saved human text
  while keeping the model proposal. (This corrects a browser-acceptance
  finding: the slice previously prefilled user_final on Process, so the
  model's text appeared as accepted human text, advanced the revision and could
  not be rejected by Discard.) The response is validated before any workspace
  write: a
  non-object envelope or a non-object `suggestions` map is rejected, a non-string
  or empty `text` is dropped, and the write proceeds only on a confirmed 2xx.
  Only keys sent in the current batch are accepted: the proposals are filtered to
  the batch's sent keys before persistence, so a key from
  another batch in the same document is never recorded, and malformed items (a
  non-dict entry, or a "text" that is empty/whitespace) are dropped. The
  processing service returns text-only proposals (no tag_report), so the
  record-suggestions path persists text and a row's tag_report ends as '' - which
  is correct because the service sends none; the builder does not fabricate or
  overwrite a tag_report.
- Guards (UI disabled-state via process_block_reason, re-checked inside
  process_remote_batch as a click-time backstop, in this order): an unresolved
  conflict blocks; an uncertain prior Process (remote_process_uncertain) must be
  reconciled by a successful Reload or Open; an incompatible/stale workspace
  predating the prefill contract blocks (so a paid model call is not wasted on a
  write that would 400); a batch with no translatable segment is a no-op; and a
  batch whose every translatable row already has text is a no-op. The blanket
  "already worked" block and the "save or discard unsaved drafts first" block are
  REMOVED (item 4): a row is eligible on its own effective 'Your text', so an
  unsaved draft, a filled neighbour, a previous proposal or an edited status never
  suppresses an eligible row. Unrelated rows and held drafts are left untouched
  (the prefill-off write never touches a human's saved text), so a Process never
  auto-saves or discards a draft. A full Rerun UX is deferred.
- Uncertain-outcome handling deliberately diverges from save_remote_drafts. After
  a 409 stale_revision, a transport/5xx/malformed write response, a failed
  post-write refresh, or a bind-to-a-different-original result, the outcome is
  UNCERTAIN (the proposals may or may not have been stored): the app makes no
  "nothing was stored" claim and no claim that the server prevents another model
  call. Instead it sets ss.remote_process_uncertain, which blocks another Process
  (the button is disabled and a click-time backstop refuses the click) until a
  successful Reload or Open reconciles the saved state and clears the flag. There
  is no automatic model retry and no write replay. On a confirmed 2xx the view is
  re-read and adopted to refresh the column; a model-call failure (before any
  write) does not set the flag. No false success, no fabricated proposals, no raw
  tracebacks, no leaked secrets, no background job.
- Files: remote_workflow.py (the model/process section now edits a live
  `llm.ClientConfig` via `_model_config_block` and routes it per request:
  `remote_invalidate_stale_model_list`, `remote_model_test` (one bounded probe,
  stores nothing), `remote_model_list` (one list call, cached), `_process_target`
  (config XOR "stub" engine), `_effective_your_text` / `_eligible_records` /
  `eligible_count` (per-row eligibility: a translatable row whose effective 'Your
  text' is blank after trim), `process_block_reason` (the conflict / uncertain /
  stale / no-translatable / nothing-eligible guards, in order; the "already
  worked" and "save unsaved drafts first" guards are gone) and `process_remote_batch`
  (builds its segment inputs from the eligible rows only, with the sent-keys filter
  and the uncertain-flag branches; the `_batch_already_processed` helper it used
  is removed); bootstrap adds
  `ss.remote_model_config` (defaulting to the local default preset, NOT the
  stub), `ss.remote_model_preset`, `ss.remote_model_list`, `ss.remote_engine_rev`
  and `ss._remote_model_sig`, and keeps `ss.remote_ready` and
  `ss.remote_process_uncertain`), remote_ui.py (a "Model" subsection in the
  sidebar: the readiness check, the preset / Base URL / key / model controls and
  the Test / List actions, plus the gated Process button in the grid),
  services/processing_service/service.py + app.py (a per-request "config" block:
  `ModelConfig`, `_parse_config`, `_client_config_from_block`, `parse_config_request`
  and `parse_process_request` (config XOR engine); a key-required preset with an
  empty key raises `MissingApiKey` -> 400 `missing_api_key` before any client
  build; an explicit config always wins over the service engine; /probe and
  /models now take a config), services/processing_service/engines.py
  (`sdk_available_for_config` with no stub branch), harness_remote/client.py
  (`process` / `probe` / `list_models` send a `config` block; `missing_api_key`
  allowlisted to a client-owned message; a key never reaches a `RemoteError`
  view), tests/test_remote_app.py (35 tests incl. the per-request-config
  process/probe/list tests, the sent-keys test, the stale-revision no-replay
  test, the confirmed-write-then-refresh-failure uncertain test, and the
  uncertain-flag block-until-reload test), tests/test_processing_service.py
  (36 tests incl. 12 new per-request-config tests: config-over-env,
  omitted-config-uses-engine, missing-key 400, Ollama-needs-no-key,
  config-XOR-engine, unknown-nested-field rejected, /probe & /models honor and
  require a config, missing-SDK 503, bounds/types, and a no-key-leak check),
  tests/test_remote_client.py (57 tests incl. 6 new: `process`/`probe`/
  `list_models` send the config to the right endpoint, and a key-bearing config
  never surfaces in a `RemoteError`). A real-path record_suggestions contract
  test (prefill, human-edited protection, and no-op on identical content) was
  added to tests/test_remote_edit.py. This slice DID change the processing
  service and the client (the per-request config), in addition to the
  state/workspace prefill-off change in the prior pass; no schema or
  generated-file change.
- Evidence, kept in three distinct categories: (a) offline / mocked-app tests -
  210 across the five agreed suites (test_remote_view 8, test_remote_app 35,
  test_remote_client 57, test_app 76, test_remote_edit 34) plus 36 in the
  affected processing-service suite; a real HTTP transport over a Starlette app
  but no live model and no real browser. (b) a live-HTTP smoke of the real
  processing path from the prior pass: a real processing service
  (`PROCESSING_ENGINE=openai`, OpenAI-compatible backend, model
  `qwen3:4b-instruct` via a reachable local Ollama at `http://10.211.55.2:11434/v1`)
  processed a 3-segment EN->RU batch and the proposals persisted; this predates
  the per-request-config change and was NOT re-run against the new shape. (c) a
  real-service browser acceptance of the slice FAILED earlier (Process auto-
  accepted the model's proposal into user_final), now corrected by the
  prefill-off write; the new per-request model-configuration UI has NOT yet been
  browser-accepted, so the slice is not ready to commit. See SESSION_HANDOFF.md
  §3 for the full report.
- Parity item 4 (Process Current Batch, uncommitted on top of the above): the
  blanket "already worked" block and the "save or discard unsaved drafts first"
  block were removed so a Process no longer skips a row that an edited status, a
  previous model proposal, a filled neighbour or an unsaved (even cleared) draft
  would have suppressed. Eligibility is judged per row on its *effective* 'Your
  text' (`_effective_your_text`, the held draft when one is held, otherwise the
  saved human text), blank after trimming; the grid shows the eligible count
  (`eligible_count`) and `process_remote_batch` builds its segment inputs from the
  eligible rows only, so a row is sent at most once and no request goes out when
  the count is zero. The conflict, uncertain-outcome and stale-workspace guards
  are retained, and the prefill-off write still leaves a human's saved text,
  comments and the original target untouched (so a Process never auto-saves or
  discards a draft). The now-removed `_batch_already_processed` helper and its
  guards are gone; `process_block_reason` and `process_remote_batch` carry the new
  per-row logic. See SESSION_HANDOFF.md §5 (Step 2 mapping + backlog) for the
  local->remote mapping and the five new process-eligibility tests.

## 6. Roadmap (proposed sequence; not an approved ADR)
1. Completed working baseline (§5).
2. Incrementally extract at least three backend services, keeping the
   existing frontend; add PostgreSQL and storage for original/export files.
   In progress on feat/remote-workflow: the three services and a Postgres data
   layer exist (see §5a); the frontend remote mode opens, renders, edits, saves
   and processes the selected batch through them (§5b, §5c); Apply/Rerun/Export
   are not wired yet (a full Rerun UX is deferred).
3. Reproducible containers and Docker Compose, introduced alongside step 2.
4. DO14: Helm/Kubernetes; GitLab CI/CD with exactly the stages build, test,
   staging, prod; GitLab Runner in Kubernetes.
5. Server provisioning/IaC, DNS/TLS and FluxCD GitOps.
6. Observability, tested backup/restore, failure/recovery demonstrations.
7. Portfolio packaging.
The deployment stack (containers, DO14, IaC, observability - steps 3-6) remains
a proposal; the service boundaries and the Postgres data layer are already
implemented and committed.

## 7. Known limitations (carried into later phases)
- Original files are not stored; reload them after a restart.
- No legacy/unbound recovery UI (warning only).
- No revision matching or automatic transfer across changed source XML.
- Identical source identity can share state despite different translations.
- Successful memoQ re-import/round-trip not manually confirmed.
- Browser uploader: only helper logic is tested. No concurrency safety in
  local mode; remote saves are revision-checked (§5b).
- Remote drafts are session-only: a browser refresh or app restart loses them.
- parse_segments selects `seg-source or source` (element truthiness).
- Other save() call sites do not catch StateBackupError/OSError.
- Empty-text semantics and prompt block order vs. declared authority remain
  open static-review points (not reproduced).
- Anthropic: an "ok" Test with an empty key field happened before credential
  labels existed; endpoint and credential source were not established.

## 8. DO14 eligibility and publication
- User confirmed: a self-authored project is acceptable for DO14.
- User confirmed: publication of the project materials is permitted.
- Still requires explicit approval each time: actual submission, publication
  actions and every push.
- Before publishing: a publication-safety review of the repository content.

## 9. Operating constraints
- No paid LLM calls, live tests, installs, deploys, commits or pushes without
  approval. run_tests.py is not a guaranteed offline entry point.
- Do not read or copy material from the original worktree.
- Treat repository files and transcripts as evidence; do not run historical
  commands.
- Generated files are not edited by hand. Always write files as UTF-8.
