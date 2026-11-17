# Session Handoff - memoQ LLM Harness

## 1. Environment
- Worktree: C:\memoq-harness\memoq-harness-qwen (git worktree; do not cd to
  the main checkout). Branch: feat/remote-workflow.
- HEAD: fdbd6a7 "feat: add read-only remote UI", which lands the remote
  read-only slice (Open/Reload, envelope unwrap, bind-contract projection, the
  read-only UI and its tests). The branch tracks origin/feat/remote-workflow
  (the user pushed fdbd6a7). On top of it the current extraction refactor
  (§2) is uncommitted working-tree work.
- History up to and including fdbd6a7 holds the document-, workspace- and
  processing-services plus the remote HTTP transport (9db7505, d19e5c7,
  f1f75cb, 59ca3ae, 1483731) and the remote read-only slice (fdbd6a7).
- Interpreter: `.venv/Scripts/python.exe`. Bare `python` on PATH is a broken
  WindowsApps stub; do not use it.

## 2. State
- Phase 1 is closed as a bounded working baseline (PROJECT_CONTEXT.md §5).
- Remote open + read-only rendering: committed as fdbd6a7 on
  feat/remote-workflow.
  - app.py (at fdbd6a7): a local/remote mode seam - remote mode loads the
    config first and never builds a ProjectState; a config error shows a
    sanitized message and stops with no local fallback; a read-only remote
    branch renders the saved workspace with editing, Save, Apply, Process,
    Rerun, Analyse and Export all absent or disabled, and batch/page
    navigation is presentation-only.
  - harness_remote/view.py: RemoteWorkspaceView, a presentation adapter that
    merges document segments over the workspace snapshot while keeping the three
    fields distinct (original target vs. user_final vs. llm_suggestion);
    exported from harness_remote/__init__.py.
  - tests/test_remote_view.py and tests/test_remote_app.py: focused adapter
    tests and headless AppTest over the read-only UI path.
- Extraction refactor (this session, uncommitted, on feat/remote-workflow on
  top of fdbd6a7): app.py's remote responsibilities were split into three new
  Streamlit-coupled modules at the repo root (kept out of harness_remote, which
  stays Streamlit-free). ui_common.py holds the UI helpers both paths share
  (APP_TITLE, notify, flush_notice, upload_marker, upload_needs_load, _display);
  remote_workflow.py owns the Open/Reload orchestration, the segments-envelope
  validation, the bind-payload projection and get_remote_clients (with its
  call-time build_clients seam); remote_ui.py owns the remote sidebar, grid and
  row. app.py re-imports every shared name, so its namespace and local behaviour
  are unchanged; no extracted module imports app, so there is no circular
  import. Behaviour-preserving: session_state/widget keys, bootstrap order, the
  client lifecycle and the three distinct text fields are untouched, and no test
  import/patch changed.
- Fixed a user-reported Open/Reload integration bug (uncommitted): the
  document-service /segments 2xx is the envelope
  {original_id, document_id, tag_source, filter, segments: [...]}, but
  app.py's remote_open/remote_reload iterated that dict as a bare list of
  segment dicts, so it read the envelope's string keys and raised
  AttributeError. A narrow helper _document_segments in app.py now unwraps the
  list and validates each item - an object, the four required string fields, and
  a boolean translatable - at those two call sites (not the HTTP client, not the
  view adapter), raising an explicit RemoteError on a malformed 2xx instead of
  an AttributeError or a silent empty view. The Reload caller catches that
  RemoteError too, mirroring the Open caller, so both paths report a safe,
  explicit error and keep the live view.
- A second defect the same review found: remote_open passed the full 16-field
  serialized segments as the bind's active selection, which the workspace parser
  rejects - it accepts exactly the four fields key/unit_id/source_text/
  target_text per segment and requires every document key in present_keys. A
  narrow _bind_payload in app.py now projects the translatable selection to those
  four fields and passes every document key as present_keys, while the full
  canonical list still drives the read-only view (a row the filter hides is not
  discarded). The fake client was corrected to return the real envelope and to
  record the full projected items; regression tests were added (Open unwrap,
  Open/Reload malformed-safe, a malformed-item case, and a projection test that
  feeds the app's actual bind payload to the real parse_bind_request). Manual
  acceptance of these fixes is complete: the user confirmed the Open/Reload and
  bind-contract behaviors against the real backend services in a live browser
  session (see §3); this does not complete the remote workflow, which remains in
  progress.
- Unrelated, left untouched and uncommitted: .claude/settings.local.json,
  Start-Claude-Ollama.ps1, tests/fixtures/sample1.mqxliff,
  tests/fixtures/21133_242441_en.html_rus.mqxliff.
- DO14 and the DevOps portfolio remain later, manual work
  (PROJECT_CONTEXT.md §1, §6).

## 3. Latest evidence
- After the extraction refactor (this session, agent-run, offline, on top of
  fdbd6a7): the four app/view/client suites unchanged - `tests\test_remote_view.py`
  8 OK, `tests\test_remote_app.py` 14 OK, `tests\test_remote_client.py` 50 OK,
  `tests\test_app.py` 76 OK - 148 total, 0 failed, 0 skipped. The four modules
  (`app.py`, `ui_common.py`, `remote_workflow.py`, `remote_ui.py`) byte-compile
  and import cleanly (no circular import). `git diff --check` and `git status`
  were attempted but blocked by a transient model-classifier outage; the
  equivalent was verified by read-only means instead (no trailing whitespace or
  tabs in the four files; the edits were pure deletions plus a clean import
  block; the four excluded worktree items were not written to).
- Remote read-only slice (this session, agent-run, offline), after the
  Open/Reload envelope-unwrap fix and the bind-contract projection fix (see §2):
  `tests\test_remote_view.py` 8 OK, `tests\test_remote_app.py` 14 OK
  (11 + 3 new regression tests), `tests\test_remote_client.py` 50 OK,
  `tests\test_app.py` 76 OK - 148 total, 0 failed, 0 skipped.
- Phase 1 baseline (historical, agent-run, offline, at 2ec77cf):
  `-m unittest -v tests.test_core` 268 OK; `tests\test_app.py` 76 OK.
- Not run: run_tests.py, test_live.py, any live endpoint.
- User-run manual acceptance (this session, against the real backend services,
  NOT agent-run and NOT part of the 148 offline count): the user confirmed in a
  live browser session that - (1) Open sample.mqxliff works; (2) reopening the
  same file shows no duplicate rows; (3) Reload twice issues successful
  workspace/segments GET requests with no repeated upload or bind; (4)
  disconnecting the SSH tunnel yields a handled 'Could not connect to
  workspace.' error while the displayed table remains; (5) restoring the tunnel
  lets Reload succeed without restarting Streamlit or the backend; (6) switching
  document A -> B -> A works; (7) a 400+ segment document renders across five
  batches with working batch navigation; (8) an unsupported file extension is
  rejected without losing the table; and (9) a .mqxliff with invalid non-XML
  text produces a handled error, preserves the previous table, and a valid file
  can be opened afterward.
- User-reported browser checks: document A/B isolation and restoration
  (including a real process restart), Rerun edit preservation, Rerun layout,
  local Qwen Test inconclusive (finish_reason=length, 16 tokens, reasoning
  present). Details: PROJECT_CONTEXT.md §5.

## 4. Known limitations
See PROJECT_CONTEXT.md §7 (reload originals after restart, no recovery UI,
no revision matching, shared state for identical source XML, memoQ re-import
unconfirmed, no uploader automation or concurrency safety).

## 5. Next steps (each needs explicit approval)
0. Manual acceptance: the user confirmed the Open/Reload and bind-contract
   fixes, plus the remote read-only behaviors, in a live browser session against
   the real backend. Complete (see §3).
1. Commit the remote read-only slice on feat/remote-workflow. Done: fdbd6a7
   "feat: add read-only remote UI", pushed by the user to
   origin/feat/remote-workflow.
2. Bounded refactoring of the accepted slice. Done this session (uncommitted,
   on top of fdbd6a7): the three-module extraction into ui_common.py,
   remote_workflow.py and remote_ui.py (see §2). Awaiting an approved commit.
3. Remote saving / conflict handling as the next bounded mutation slice, with
   snapshot-consistency checks. Kept out of this read-only slice by design.
Process/Apply wiring and any publication step are deferred behind the above: the
exact DO14 requirements and a publication-safety review all remain later.

## 6. Constraints carried forward
- No commits, pushes, deploys, installs, live or paid calls, external service
  contact, or subagents without approval. Do not edit generated files by
  hand. Write files as UTF-8. Do not read the original worktree.
- Leave .claude/settings.local.json, Start-Claude-Ollama.ps1 and
  tests/fixtures/sample1.mqxliff untouched and uncommitted.
