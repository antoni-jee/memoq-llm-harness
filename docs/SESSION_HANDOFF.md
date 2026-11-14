# Session Handoff - memoQ LLM Harness

## 1. Environment
- Worktree: C:\memoq-harness\memoq-harness-qwen (git worktree; do not cd to
  the main checkout). Branch: experiment/local-qwen. HEAD: 2ec77cf.
- Interpreter: `.venv/Scripts/python.exe`. Bare `python` on PATH is a broken
  WindowsApps stub; do not use it.
- Not pushed. No remote publication has happened.

## 2. State
- Phase 1 is closed as a bounded working baseline (PROJECT_CONTEXT.md §5).
  All Phase 1 code is committed; nothing is awaiting a code fix.
- Uncommitted after 2ec77cf: documentation updates to README.md,
  docs/PROJECT_CONTEXT.md and this file (awaiting review).
- Priority is now DO14 and a DevOps portfolio (PROJECT_CONTEXT.md §1, §6).

## 3. Latest evidence
- Agent-run, offline, at 2ec77cf: `-m unittest -v tests.test_core` 268 OK;
  `tests\test_app.py` 76 OK (after the final Rerun layout change).
- Not run: run_tests.py, test_live.py, any live endpoint.
- User-reported browser checks: document A/B isolation and restoration
  (including a real process restart), Rerun edit preservation, Rerun layout,
  local Qwen Test inconclusive (finish_reason=length, 16 tokens, reasoning
  present). Details: PROJECT_CONTEXT.md §5.

## 4. Known limitations
See PROJECT_CONTEXT.md §7 (reload originals after restart, no recovery UI,
no revision matching, shared state for identical source XML, memoQ re-import
unconfirmed, no uploader automation or concurrency safety).

## 5. Next steps (each needs explicit approval)
1. Publication-safety review of the repository (secrets, personal data,
   private or third-party material, history), then an approved push.
2. Review the exact DO14 requirements.
3. Write one bounded architecture ADR (service boundaries, storage, stack).
   Current boundaries are proposals only.

## 6. Constraints carried forward
- No commits, pushes, deploys, installs, live or paid calls, external service
  contact, or subagents without approval. Do not edit generated files by
  hand. Write files as UTF-8. Do not read the original worktree.
- Leave .claude/settings.local.json, Start-Claude-Ollama.ps1 and
  tests/fixtures/sample1.mqxliff untouched and uncommitted.
