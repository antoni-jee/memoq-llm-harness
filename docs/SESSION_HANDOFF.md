# Session Handoff - memoQ LLM Harness

## 1. Environment
- Worktree: C:\memoq-harness\memoq-harness-qwen (git worktree; do not cd to
  the main checkout). Branch: experiment/local-qwen.
- Interpreter: Python 3.13.15 via `py -3.13`; deps in an ignored local `.venv`
  (`.venv/Scripts/python.exe`). Bare `python` on PATH is a broken WindowsApps
  stub (exit 49); do not use it for tests.

## 2. Checkpoint
- Code/test commit: c0ffe3a "fix: preserve unapplied human edits during
  regeneration" (state.py, tests/test_core.py, tests/test_app.py). Not pushed.
- Defect: a manual edit captured WITHOUT Apply was overwritten by a later
  Retry / Process Current Batch. Root cause: capture_edits() stored user_final
  but never set edited_by_human, so record_suggestions() overwrote it.
- Fix: capture_edits sets edited_by_human=True when a captured, non-empty
  revision differs from the baseline (llm_suggestion, else mt_target),
  mirroring calculate_diffs(); the flag is only ever set True. app.py,
  generated files, schema, migration, and serialization were not changed.
- Empty-text and automatic flag-reset semantics remain OUTSIDE this fix.

## 3. Verification (human-run)
- Before the fix, 3 of 4 new regression tests FAILED (reproduced):
  test_unapplied_manual_edit_survives_rerun, test_retry_preserves_manual_edit,
  test_reprocess_preserves_manual_edit. The comment-only state test
  (test_comment_does_not_freeze_target_text) passed even before.
- After the fix, all 6 targeted tests passed (4 regression + 2 sibling state
  tests). Assertions were then strengthened.
- Full tests/test_core.py passed: 185 tests.
- A full tests/test_app.py run exposed test-isolation errors: two existing
  retry tests deleted LLMClient.translate_one during cleanup. They now restore
  the original method. The human reports subsequent verification passed; no
  final UI test count or duration is recorded.
- The full repository suite was NOT run.

## 4. Test-runner caution
- run_tests.py imports live tests; its import may contact an endpoint. It is
  NOT a guaranteed offline entry point. Do not run it without approval.
- For targeted runs use the .venv interpreter and name tests explicitly.

## 5. Local state outside c0ffe3a
- .claude/settings.local.json remains modified: reviewed as adding Stop,
  Notification, and StopFailure notification hooks; no permission, model, or
  routing changes. The notifier script's behavior has NOT been inspected.
- CLAUDE.md and docs/ are untracked.

## 6. Constraints carried forward
- No commits, pushes, deploys, installs, live or paid calls, external service
  contact, or subagents without approval. Do not edit generated files by hand.
  Write files as UTF-8. Do not read the original worktree.

## 7. Next steps (not yet authorized)
- Decide on .claude/settings.local.json; inspect the notifier first.
- Decide whether to commit docs, push, and run the full repository suite.
- Address remaining stateful requirements (PROJECT_CONTEXT.md §5).

## 8. Unknowns
- Full repository suite count and status; final test_app.py count.
- DO14 eligibility and publication of private material: unresolved.
