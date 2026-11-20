# Session Handoff - memoQ LLM Harness

## 1. Environment
- Worktree: C:\memoq-harness\memoq-harness-qwen (git worktree; do not cd to
  the main checkout). Branch: feat/remote-workflow.
- HEAD: fcba7a3 "feat: add remote editing and saving" (pushed to
  origin/feat/remote-workflow), which commits the edit/save slice (§2) on top of
  5258ffc "refactor: split remote UI and workflow", on top of fdbd6a7 "feat: add
  read-only remote UI". The model / process-one-batch slice (§2) is the current
  uncommitted working-tree work on top of fcba7a3; no commit or push of it has
  happened.
- History up to and including 5258ffc holds the document-, workspace- and
  processing-services plus the remote HTTP transport (9db7505, d19e5c7,
  f1f75cb, 59ca3ae, 1483731), the remote read-only slice (fdbd6a7) and the
  extraction refactor (5258ffc).
- Interpreter: `.venv/Scripts/python.exe`. Bare `python` on PATH is a broken
  WindowsApps stub; do not use it.

## 2. State
- Remote edit/save slice (committed as fcba7a3 on top of 5258ffc; behaviour in
  PROJECT_CONTEXT.md §5b). Automated checks pass and the user's browser
  acceptance is recorded in §3; the user committed it.
  - Taken over from an interrupted agent whose WIP had a per-document draft
    store, Save/Discard and a dirty-Open block. Kept that shape and corrected:
    drafts record their base values (only edited fields are sent; a concurrent
    change to an edited field is a conflict); dirty Open and Reload both ask
    save/discard(/keep)/cancel; drafts are synced from widget state at the top
    of every run; 409 and ambiguous failures re-read and reconcile; the in-file
    fake's save mirror keeps the revision on a no-op and rejects unknown keys.
  - Fix pass 1 (after first browser test): Save/Discard in the sidebar above
    Reload (always visible, disabled when not applicable, drafts note);
    Conflicts panel with draft / now-saved / started-from values and Keep mine
    / Use server / Show row; write embargo while a conflict is open; open
    document shown apart from the uploader selection; Cancel empties the
    uploader; reselecting the open file version is a no-op.
  - Fix pass 2 (two-tab mixed case): widgets are re-keyed per row, only where
    the shown value changes, so kept drafts are never remounted after a 409 or
    re-read. Offline tracing did not reproduce the live loss (see §3); the
    user's browser retest after this change succeeded.
  - Files: remote_workflow.py, remote_ui.py, tests/test_remote_app.py (fake
    fidelity, Save-disabled assertion), new tests/test_remote_edit.py,
    docs/PROJECT_CONTEXT.md, docs/SESSION_HANDOFF.md. No backend, client,
    schema or generated-file change.
- Remote model / process-one-batch slice (uncommitted, on top of fcba7a3;
  behaviour in PROJECT_CONTEXT.md §5c). The sidebar gains a "Check service
  readiness" control (a GET /readyz service/SDK readiness probe that reports the
  processing service's configured engine/status; it is NOT a real model test and
  never claims a model call succeeded) and an "Engine" selectbox; the grid gains
  a gated "Process Current Batch" that sends the selected batch through the
  processing service and persists the proposals as the model's llm_suggestion
  (record_suggestions with prefill OFF), distinct from user_final, so a Reload
  shows them - a model proposal is not a human acceptance, so Process does NOT
  populate user_final / user_comment / edited_by_human / the original target and
  does not advance the revision as a save would. Only
  keys sent in the current batch are accepted (other-batch keys are not
  persisted; malformed items are dropped). Guards: unsaved drafts / an open
  conflict / no translatable segment / an already-worked batch all block (full
  Rerun deferred). After a 409 stale revision, a transport/5xx/malformed write,
  a failed post-write refresh, or a bind-to-a-different-original result, the
  outcome is UNCERTAIN (the proposals may or may not have been stored): the app
  makes no "nothing was stored" claim and no claim that the server prevents
  another model call; it sets ss.remote_process_uncertain, which blocks another
  Process until a successful Reload or Open reconciles and clears the flag. There
  is no automatic model retry and no write replay. The response is validated
  before any workspace write: a non-object envelope or a non-object `suggestions`
  map, a non-string or empty `text`, and keys not sent in this batch are all
  rejected or dropped, and the write proceeds only on a confirmed 2xx. The
  processing service returns text-only proposals (no `tag_report`); the
  record-suggestions path forwards a valid string `tag_report` (capped) when one
  is present and otherwise leaves the row's `tag_report` untouched rather than
  clearing it. The prefill-off contract change (this task): state.py,
  services/workspace_service/service.py (SuggestionRequest + parse + record),
  harness_remote/client.py, services/workspace_service/app.py, README and
  remote_workflow.py now thread a `prefill` flag (default True, the local
  workflow and existing callers unchanged) and the remote Process path passes
  `prefill=False` so a model proposal is never written into user_final. The two
  formerly read-only tests were updated to allow the gated Process control; the
  process/readiness/per-request-config tests total 35 in tests/test_remote_app.py,
  and real-path record_suggestions contract tests (plus four prefill-off tests:
  proposal-only does not accept human text, keeps human text/comment, manual save
  persists human text, Discard restores) are in tests/test_remote_edit.py.
  - Per-request model-configuration / selection (this pass, on top of the
    prefill-off fix above; behaviour in PROJECT_CONTEXT.md §5c): the earlier
    "do not duplicate the local model configuration sidebar" restriction is
    withdrawn. The sidebar now mirrors the local model config and selection - a
    "Provider preset" selectbox defaulting to the local default preset "Local
    Engine (Ollama)" (the offline "Test backend (stub)" is an explicit option,
    not the default), an editable Base URL (noted as reachable FROM the
    processing service), a password key field for key-requiring presets, a Model
    selectbox/entry, and "Test model" / "List models" actions; the operator
    chooses the model in the UI and never edits an env var, starts a service or
    changes a port. The sidebar edits a live `llm.ClientConfig` and sends a
    validated per-request "config" block on /process, /probe and /models, from
    which the service selects the real backend/endpoint/model (a real preset
    sends config, no engine; the explicit stub sends engine "stub"; the two are
    mutually exclusive, enforced client-side and by the service). A call that
    omits config uses the service's own configured engine (backward compatible);
    an explicit config is always used as given and never falls back to env/stub,
    and an invalid explicit config fails safely. The key lives in the browser
    session only (sent per request; never persisted, logged or echoed in an
    error/repr); a key-requiring preset with an empty key fails with a sanitized
    "Enter an API key in the sidebar" error, while local Ollama (no key) still
    works. "Test model" is one bounded probe that stores nothing; "List models"
    is one list call, cached and invalidated (no provider call) when the
    connection settings change. Service/client changes: the processing service's
    `ModelConfig` / `_parse_config` / `_client_config_from_block` /
    `parse_config_request` / `parse_process_request` (config XOR engine;
    `MissingApiKey` -> 400 `missing_api_key`), `engines.py` `sdk_available_for_config`
    (no stub branch), and the client's `process` / `probe` / `list_models` (send
    a `config` block; `missing_api_key` allowlisted; a key never reaches a
    `RemoteError`). New tests: 12 per-request-config tests in
    tests/test_processing_service.py (36 total) and 6 in
    tests/test_remote_client.py (57 total).
- Extraction refactor (ui_common.py, remote_workflow.py, remote_ui.py split out
  of app.py; behaviour-preserving): committed and pushed as 5258ffc.
- Remote open + read-only rendering, including the Open/Reload envelope-unwrap
  and bind-contract projection fixes: committed as fdbd6a7 (user-accepted;
  see §3).
- Phase 1 is closed as a bounded working baseline (PROJECT_CONTEXT.md §5).
- Unrelated, left untouched and uncommitted: .claude/settings.local.json,
  Start-Claude-Ollama.ps1, .agents/, .claude/skills/,
  tests/fixtures/sample1.mqxliff,
  tests/fixtures/21133_242441_en.html_rus.mqxliff,
  tests/fixtures/invalid-remote-test.mqxliff.
- DO14 and the DevOps portfolio remain later, manual work
  (PROJECT_CONTEXT.md §1, §6).

## 3. Latest evidence
### Automated (agent-run, offline)
- Final code of the model-configuration / selection slice (current uncommitted
  state on top of fcba7a3): the five agreed suites total 210 OK, 0 failed,
  0 skipped - `tests\test_remote_view.py` 8, `tests\test_remote_app.py` 35,
  `tests\test_remote_client.py` 57, `tests\test_app.py` 76,
  `tests\test_remote_edit.py` 34. The affected processing-service suite passes:
  `tests\test_processing_service.py` 36 OK (12 new per-request-config tests this
  pass; the workspace-service suites were not re-run this pass as no
  workspace-service code changed - their prior counts, `test_workspace_service.py`
  45 OK and `test_workspace_service_pg.py` 8 OK when
  `HARNESS_TEST_DATABASE_URL` is set, still hold). `git diff --check` is clean
  (only CRLF warnings). The process tests in
  test_remote_app.py are store-and-reload, blocked-by-drafts, already-processed
  blocked, the readiness probe caches the engine without a model call, a model
  failure claims nothing and does not set the uncertain flag, a stale revision
  does not replay, a suggestion for another batch is ignored, malformed
  suggestions are dropped, and an uncertain write blocks the next Process until a
  Reload reconciles. The per-request model-configuration tests in
  test_remote_app.py add a real-preset case (a non-stub preset sends a validated
  per-request "config" block and no engine to /process), a stub-preset case (the
  explicit stub sends engine "stub" and no config), a "Test model" case (drives
  exactly one bounded probe and records no suggestions), and a "List models" case
  (drives exactly one list call and no generation); the per-request config
  parsing, backend/endpoint selection, and `missing_api_key` behaviour are in
  tests/test_processing_service.py (36 total), and the client-side config routing
  plus key-not-leaked-in-error cases are in
  tests/test_remote_client.py (57 total). Six further regression tests were added: a non-object
  envelope and a non-object `suggestions` map are rejected before any write, a
  non-string or empty `text` is dropped, a valid string `tag_report` is
  forwarded through the write, an over-long `tag_report` is dropped (the text is
  still sent so the batch is not blocked), and a confirmed write followed by a
  failed refresh sets the uncertain flag. The test_remote_app fakes mirror the
  workspace suggestion-write contract (409 on revision mismatch, 422 on an
  unknown/inactive key, llm_suggestion written and revision advanced on change,
  human-edited rows protected) with a configurable /process and /readyz on the
  fake processing client; a real-path record_suggestions contract test was added
  to test_remote_edit.py. Not run: any live endpoint. NOTE: these are headless /
  mocked-app tests (a real HTTP transport over a Starlette app, but no live
  model and no real browser); they are NOT a substitute for the live smoke below
  nor for real-service browser acceptance.
- Live HTTP smoke of the model/process path (agent-run, real services, point 4):
  a real processing service was started with `PROCESSING_ENGINE=openai`
  (OpenAI-compatible backend talking to a reachable local Ollama,
  `PROCESSING_BASE_URL=http://10.211.55.2:11434/v1`, model `qwen3:4b-instruct`),
  and `GET /readyz` reported `{"status":"ready","engine":"openai",
  "checks":{"engine":"ok"}}` - NOT stub. `POST /process` with a small synthetic
  3-segment EN->RU batch returned real model output
  (`Откройте панель управления.`, `Сохраните месячный отчёт.`,
  `Выйдите из системы.`), stage `mt1`, system_prompt_chars 8983, requests 1,
  service_elapsed_ms ~9509 / wall ~9713, no missing or errors; the service log
  confirmed the real Ollama call (`POST .../v1/chat/completions 200 OK`). The 3
  proposals were recorded via `record_suggestions` into a disposable in-memory
  ProjectState and a save/load round-trip confirmed persistence; the workspace
  contract held (original `mt_target` untouched, a human-edited row's `user_final`
  protected, `status` advanced to `processed`). The service returns text-only,
  so `tag_report` is `''` - which is correct, confirming the point-1/3 finding
  that the builder now leaves a row's `tag_report` untouched when the service
  sends none. This is a live-HTTP smoke, NOT a real-service browser acceptance.
- Earlier model/process slice state (superseded): `tests\test_remote_app.py` 20
  OK (14 carried over, 6 new). Full `run_tests.py` at that state: 353 total, 0
  failed, 0 skipped. (run_tests.py includes only test_core, test_app, test_live,
  not the remote suites, and is not a guaranteed offline entry point.)
- Final code of the edit/save slice (after fix pass 2; run after the last code
  change): `tests\test_remote_view.py` 8 OK, `tests\test_remote_app.py` 14 OK,
  `tests\test_remote_client.py` 50 OK, `tests\test_app.py` 76 OK,
  `tests\test_remote_edit.py` 29 OK - 177 total, 0 failed, 0 skipped.
  `git diff --check` clean. test_remote_edit drives the app through the real
  WorkspaceClient into the real workspace Starlette app / WorkspaceService
  (in-memory repository, no DB). Deliberate mutations (conflict detection off,
  "" edits dropped, same-file no-op off, conflict embargo off, per-row re-key
  reverted) were each caught and reverted. Service suites not run (no service
  code changed). Not run: run_tests.py, test_live.py, any live endpoint.
- Two-tab mixed-case investigation (fix pass 2): the live loss of segment 9
  was not reproduced offline. AppTest traces showed both edits captured, both
  in the rejected PATCH, both drafts kept after the 409 with only 10
  conflicted. No browser automation was available, so the live root cause is
  unproven; the per-row re-key change removed the one browser-specific step
  found (remounting every cell after a re-read). The user's retest after the
  change passed (below).
- Earlier counts (166, then 173) belong to earlier code states and are
  superseded by the 177 above.

### User-run browser acceptance of the edit/save slice (NOT agent-run, real services)
- Save then Reload persisted text and comments, including a deliberate empty
  text.
- Drafts survived batch navigation and "Reload, keep my changes".
- Dirty Open -> Cancel kept the open document (and its draft) and cleared the
  uploader. Earlier: Discard and open did not persist the draft; Save and open
  did.
- Discard and reload dropped only the draft and kept the same saved revision.
- Two-tab conflict: 409, nothing written, draft kept; both Keep mine and Use
  server worked. The mixed case (two drafts, one conflicted) was retested
  successfully after fix pass 2.
- Workspace tunnel outage during Save did not lose the draft. The final Save
  -> Reload after reconnection was NOT confirmed (no result was supplied).
- During a live conflict, sidebar Save and Save and reload were confirmed
  disabled. Save and open was not separately verified in the browser.
- Show row was tried for a segment on the current page; it did not scroll
  to the row or produce a visible change. Tracked as a deferred UI issue.
- Reselecting the same file after Save preserved the saved text. Whether
  reselection avoided upload/bind was not independently verified in the
  browser.

### Model / process-one-batch slice browser status - FAILED, then corrected
- Browser acceptance FAILED: on a clean document, Process persisted each proposal
  into user_final immediately, so "Your text" read as accepted human text, the
  revision advanced, Discard could not reject the auto-accepted text, and a
  Reload restored it. This is now corrected by the prefill-off write
  (record_suggestions prefill=False on the remote Process path; a model proposal
  is not a human acceptance). The slice is NOT described as accepted or ready to
  commit; a browser re-run of the retest steps below is required before any
  commit.
- The per-request model-configuration / selection UI added in this pass (the
  sidebar that mirrors the local model config and selection: "Provider preset",
  Base URL, key, Model, "Test model", "List models") is covered only by the
  offline/mocked-app tests above; it has NOT yet been browser-accepted. A
  real-service browser re-run that selects the model IN the UI (steps 1-2 below)
  precedes any commit of the slice.
- Three evidence kinds, kept distinct: (a) offline /
  mocked-app tests (above: 210 across the five suites - the 14 prefill-era
  process tests, the per-request-config / "Test model" / "List models" tests, and
  the 12 per-request-config processing-service tests and 6 client config-routed /
  key-not-leaked tests; a real HTTP transport over a Starlette app but no live
  model and no real browser) cover the readiness control, the batch Process and
  the uncertain-outcome block, plus the four
  prefill-off contract tests (proposal-only does not accept human text; keeps
  human text/comment; manual save persists human text; Discard restores the prior
  saved human text while keeping the proposal); (b) a live-HTTP
  smoke (above) drove the real processing service against a reachable local Ollama
  (`qwen3:4b-instruct`) with a 3-segment batch and confirmed persistence and the
  workspace contract, but still with no real browser; the "Check service
  readiness" probe itself only shows the configured engine/status and is NOT a
  real model test. (c) A real-service browser acceptance of the model/process
  slice FAILED and must be re-run after the prefill-off fix - that is the
  user-run step that must come before the Process commit.

### Browser retest steps (after the prefill-off fix; user-run, real services)
Run on a clean `sample1.mqxliff` with a live processing service. Select the model
IN THE UI first (no env var, service start, or port change is needed):
1. In the "Model" subsection, confirm "Provider preset" defaults to "Local
   Engine (Ollama)". If Ollama is not at the local default, set "Base URL" to the
   endpoint reachable FROM the processing service; for a key-requiring preset,
   enter the API key (password field); pick the model in "Model". (Stub: select
   the explicit "Test backend (stub)" option.)
2. Click "Test model": one bounded probe reports a status; no proposal is written
   and the revision does not change.
3. Click "List models": the model list populates from one call; it is cached and
   re-queries (no provider call) only when the connection settings change.
Then, on a clean document:
4. Open the document: "Your text" is empty and there are no proposals - Open
   must not call processing.
5. Click "Process Current Batch": the "Model proposal" column now shows the
   proposals, but "Your text" stays empty and the row is not saved (revision
   does not advance as a save would). The proposal survives a Reload (it lives in
   `llm_suggestion`), and a Reload still shows "Your text" empty.
6. Manually edit a "Your text" cell (an ordinary unsaved draft): the draft note
   appears. Save it: "Your text" now shows the human text and `edited_by_human`
   is set; the proposal is kept in "Model proposal".
7. On a row with a saved human text and a proposal, make a new manual edit, then
   click "Discard" before Save: "Your text" reverts to the prior saved human
   text and the proposal remains in "Model proposal".
8. A second "Process Current Batch" on a row that now has a proposal or saved
   text is a no-op (already-worked batch is not re-processed); a full Rerun is
   deferred.
These steps must all hold for the slice to be browser-accepted. Until they pass,
the slice is NOT accepted and is NOT ready to commit.

### Earlier, for the committed read-only slice
- After the extraction refactor (5258ffc) and the read-only slice (fdbd6a7):
  the four suites 148 total, 0 failed (agent-run, offline).
- Phase 1 baseline (historical, agent-run, offline, at 2ec77cf):
  `-m unittest -v tests.test_core` 268 OK; `tests\test_app.py` 76 OK.
- User-run read-only acceptance against the real backend: Open, reopen without
  duplicate rows, Reload without re-upload/bind, handled tunnel outage with the
  table kept and recovery without restart, document A -> B -> A, a 400+
  segment document across five batches, unsupported extension and invalid XML
  rejected safely.
- User-reported local-mode checks: document A/B isolation and restoration
  (including a real process restart), Rerun edit preservation, Rerun layout,
  local Qwen Test inconclusive (finish_reason=length, 16 tokens, reasoning
  present). Details: PROJECT_CONTEXT.md §5.

## 4. Known limitations
See PROJECT_CONTEXT.md §7 (reload originals after restart, no recovery UI,
no revision matching, shared state for identical source XML, memoQ re-import
unconfirmed, session-only remote drafts).

## 5. Next steps (each needs explicit approval)
1. Optional: confirm Save -> Reload after the workspace tunnel reconnects.
2. Commit the model/process slice on feat/remote-workflow (user decision; not
   done). The edit/save slice is already committed as fcba7a3.
3. (Real-service, user-run) browser-accept the model/process slice: the "Check
   service readiness" probe, the gated Process, and the uncertain-outcome block.
   The agent-run live-HTTP smoke (§3) already exercised the real processing path
   against a local Ollama, but a real browser acceptance still precedes the
   commit.
4. UI follow-ups (deferred, not started): compact conflict layout; Show row
   behaviour when the row is already on the current page (and scrolling to
   it); network-error dialog UX; copy-to-draft buttons (original target /
   model suggestion into the draft).
5. Later remote slices: a full Process Rerun UX, Apply, corrections/feedback,
   Analyze and Export (Process one selected batch is now wired; see
   PROJECT_CONTEXT.md §5c). Then run end-to-end acceptance, review publication
   safety, and publish the completed application on GitHub. The work on
   DO14 will start manually only after that.

## 5a. Parity effort: local->remote mapping + backlog
Goal (verbatim): bring remote mode into functional and UX parity with the working
LOCAL mode, which is the reference implementation - port working mechanisms,
defaults and settings; do not redesign them or build parallel implementations.
Keep the service architecture: Streamlit handles UI and session drafts,
processing-service calls the model, workspace-service persists work. No direct
Streamlit->provider call in remote mode, no silent local fallback, no regression
to local mode. Order: (1) prove/fix proposal persistence without a paid call
(DONE - PRIORITY 0); (2) map local to remote for the paths touched (this
section); (3) port and test in small slices. Remaining gaps go in the backlog
below, not implemented all at once.

### PRIORITY 0 - verify the write path (DONE, reported; one user restart owed)
- Proved the workspace service accepts a proposal-only write with prefill=false:
  `record_suggestions(..., prefill=False)` stores the model text as
  `llm_suggestion` only (survives a Reload) and never populates `user_final`
  (protected by `edited_by_human`), so a proposal is not presented as accepted
  human text.
- Diagnosed the running 18102 workspace as STALE: it predates commit f1f75cb, so
  it rejects the `prefill` field the client now sends -> 400 "rejected as
  malformed". `/readyz` carries no version marker, so the app cannot tell a stale
  workspace from a live one.
- Built a non-mutating compat probe (`record_suggestions(os.urandom(32).hex(), 1,
  {"1#1":{"text":"probe"}}, prefill=False)`; a 400 verdicts "stale", a 404
  verdicts "ok", anything else "unknown"), cached on
  `ss._remote_workspace_compat` and cleared by Open/Reload, plus a guard in
  `process_block_reason`/`process_remote_batch` so a stale workspace blocks a
  paid model call before it is spent. `prefill` was added to the workspace
  `SUGGESTION_FIELDS` in f1f75cb.
- One-time service restart still owed: the 18102 workspace predates f1f75cb and
  must be restarted by the user (not the agent) to accept the `prefill` field.
  No restart, reset, config change, stage/commit/push or live model call was made.

### Step 2 mapping - item 4 (Process Current Batch, DONE this session)
Format: local entry point + settings -> shared code to reuse -> remote adapter ->
test.
- Local entry point + settings: `app.py` `process_batch(batch_index)` and its
  local guards (an "already worked" skip + a "save/discard unsaved drafts first"
  skip) send a whole batch to the model; local has no copy-target/copy-proposal
  buttons and no per-row eligibility.
- Shared code to reuse: `cell_seed`/the draft store (a held draft, including a
  cleared one, is the row's effective 'Your text', else the saved human text);
  the prefill-off `record_suggestions` through the workspace client (proposal only,
  never auto-saved human text); the compat probe (block a paid call to a stale
  workspace). No new local helper; the draft/rebase machinery is reused as-is.
- Remote adapter: `remote_workflow.py` adds `_effective_your_text`/`_eligible_records`
  /`eligible_count` (per-row eligibility: a translatable row whose effective 'Your
  text' is blank after trim), rewrites `process_block_reason` to the guard order
  (conflict -> uncertain prior Process -> stale workspace -> no translatable ->
  nothing eligible; the blanket "already worked" and "save unsaved drafts first"
  guards are removed) and `_batch_already_processed` is deleted;
  `process_remote_batch` builds its segment inputs from the eligible rows only
  (no row sent twice; no request when the eligible count is zero) and keeps the
  sent-keys filter + uncertain-flag + post-write refresh; `remote_ui.py` shows the
  eligible count (`eligible_count`) and the gated "Process Current Batch" button.
- Test: `tests/test_remote_app.py` - the two stale tests
  (`test_process_blocked_by_unsaved_drafts`, `test_process_already_processed_is_
  blocked`) are replaced by five per-eligibility tests: unsaved-draft-does-not-
  block-neighbour, previous-proposal-does-not-block, mixed-batch-sends-only-blank,
  cleared-draft-is-eligible, and no-eligible-rows-sends-no-request (button
  disabled, zero process/record calls). The compat/stale/uncertain tests remain.

### Backlog - remaining parity gaps (NOT yet implemented; small slices, each
  needs approval before work)
- Item 1 - Provider/model selection: reuse local presets/URL/credential/discovery/
  defaults; an EMPTY detected model list (no hard-coded names); Ollama shows the
  endpoint models, Anthropic uses the API model list with the current
  credentials; auto-fetch ONCE when config is ready (not per rerender/keystroke)
  and clear on a connection change; keep "List models" manual; allow an explicit
  model ID; discovery must not run inference; a model change must not need an env
  edit, a port change or a restart.
- Item 2 - Test model parity: reuse the local probe; a well-formed response with
  reasoning but no visible text reports "Connection OK - visible text not
  confirmed" (NOT a successful translation/JSON test); a bounded probe with no
  hidden retries; failures are actionable without logging or displaying
  credentials.
- Item 3 - Batch size / Segments per request / Timeout as SEPARATE settings with
  local defaults/limits/behavior; preserve proposals/saved text/comments/drafts by
  stable segment key when regrouping; verify settings reach the processing service;
  trace an Ollama timeout to its actual layer (provider/service/HTTP client),
  report elapsed + chunk size, do not just raise timeouts.
- Item 5 - Process/Retry one segment: port the local row-level retry (batch
  context + tag handling); send exactly the selected translatable segment; update
  only its proposal; preserve its Your text/comment and all other rows.
- Item 6 - Copy memoQ target -> Your text: a per-row action that updates the
  editor draft only (not saved); no model call, no implicit Save.
- Item 7 - Copy Model proposal -> Your text: same draft semantics; Save persists;
  Discard restores the prior saved value while keeping the Model proposal;
  disabled when there is no proposal.
- Cross-cutting - carry behavior not labels; only an explicit "Test model" and an
  explicit "Process/Retry" may invoke a model; keep credentials request-/session-
  scoped out of logs, diagnostics, tracked files, persistent state and shared
  caches.
- Multi-process startup (launch topology) is added to this backlog, not
  redesigned: the agent does not redesign the launch topology or start DO14.

## 6. Constraints carried forward
- No commits, pushes, deploys, installs, live or paid calls, external service
  contact, or subagents without approval. Do not edit generated files by
  hand. Write files as UTF-8. Do not read the original worktree.
- Leave .claude/settings.local.json, Start-Claude-Ollama.ps1, .agents/,
  .claude/skills/ and the untracked fixtures listed in §2 untouched and
  uncommitted.
