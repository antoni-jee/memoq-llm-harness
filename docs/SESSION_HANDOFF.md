# Session Handoff - memoQ LLM Harness

## 1. Environment
- Worktree: C:\memoq-harness\memoq-harness-qwen (git worktree; do not cd to
  the main checkout). Branch: feat/remote-workflow.
- HEAD: 5258ffc "refactor: split remote UI and workflow" (pushed to
  origin/feat/remote-workflow), on top of fdbd6a7 "feat: add read-only remote
  UI". The remote edit/save slice (§2) is uncommitted working-tree work on top
  of 5258ffc; no commit or push of it has happened.
- History up to and including fdbd6a7 holds the document-, workspace- and
  processing-services plus the remote HTTP transport (9db7505, d19e5c7,
  f1f75cb, 59ca3ae, 1483731) and the remote read-only slice (fdbd6a7).
- Interpreter: `.venv/Scripts/python.exe`. Bare `python` on PATH is a broken
  WindowsApps stub; do not use it.

## 2. State
- Remote edit/save slice (uncommitted, on top of 5258ffc; behaviour in
  PROJECT_CONTEXT.md §5b). Automated checks pass and the user's browser
  acceptance is recorded in §3; ready for the user's commit decision.
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
2. Commit the edit/save slice on feat/remote-workflow (user decision; not done).
3. UI follow-ups (deferred, not started): compact conflict layout; Show row
   behaviour when the row is already on the current page (and scrolling to
   it); network-error dialog UX; copy-to-draft buttons (original target /
   model suggestion into the draft).
4. Later remote slices: Process/Rerun, Apply, corrections/feedback,
   Analyze and Export. Then run end-to-end acceptance, review publication
   safety, and publish the completed application on GitHub. The work on
   DO14 will start manually only after that.

## 6. Constraints carried forward
- No commits, pushes, deploys, installs, live or paid calls, external service
  contact, or subagents without approval. Do not edit generated files by
  hand. Write files as UTF-8. Do not read the original worktree.
- Leave .claude/settings.local.json, Start-Claude-Ollama.ps1, .agents/,
  .claude/skills/ and the untracked fixtures listed in §2 untouched and
  uncommitted.
