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

## 5b. Remote edit/save slice (uncommitted on top of 5258ffc; user browser acceptance recorded)
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
- Tests: tests/test_remote_edit.py (29) drives the app against the real
  WorkspaceClient + workspace Starlette app + WorkspaceService over an
  in-memory repository; final-code results (177 across five suites) and the
  user's browser acceptance are in SESSION_HANDOFF.md §3. Not confirmed in the
  browser: Save -> Reload after a workspace tunnel reconnects.

## 6. Roadmap (proposed sequence; not an approved ADR)
1. Completed working baseline (§5).
2. Incrementally extract at least three backend services, keeping the
   existing frontend; add PostgreSQL and storage for original/export files.
   In progress on feat/remote-workflow: the three services and a Postgres data
   layer exist (see §5a); the frontend remote mode opens, renders, edits and
   saves through them (§5b); Process/Apply/Export are not wired yet.
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
