# Session Handoff - memoQ LLM Harness

## 1. Environment
- Worktree: C:\memoq-harness\memoq-harness-qwen (git worktree; do not cd to
  the main checkout). Branch: experiment/local-qwen. HEAD: 5c3a918.
- Interpreter: `.venv/Scripts/python.exe` (Python 3.13). Bare `python` on PATH
  is a broken WindowsApps stub; do not use it.

## 2. Checkpoint (UNCOMMITTED, awaiting human review)
- Document isolation/restoration on top of 5c3a918 in app.py,
  memoq_harness/mqxliff.py, memoq_harness/state.py, tests/test_core.py,
  tests/test_app.py, plus these docs. Details: PROJECT_CONTEXT.md §6.3.
- Documents are identified by source_fingerprint() v2; state.json schema v5
  stores one snapshot per document; legacy files migrate to a retained
  "legacy" entry with a byte-for-byte backup before first overwrite.
- Reset is "Reset active document": confirmed, transactional, unloads editor.

## 3. Latest executed tests (agent-run, .venv interpreter)
- Before the fix: the two document-switching UI regressions ran 2 tests with
  10 assertion failures. After integration: 2 tests, OK.
- Failed-reset regression reproduced (editor unloaded), then fixed.
- `python -m unittest -v tests.test_core`: 240 tests, OK.
- `python tests/test_app.py`: 68 tests, OK.
- run_tests.py and live tests were NOT run.

## 4. Coverage caveats
- "Restart" UI tests use a fresh AppTest session sharing state.json; this is
  NOT a real process restart.
- Browser upload: only upload_marker()/upload_needs_load() are tested; AppTest
  cannot drive st.file_uploader. No end-to-end uploader coverage.
- No real browser run; real user state.json was never touched.

## 5. Known limitations
- No recovery/adoption UI for retained legacy or unbound work (warning only).
- Identical source XML with different translations shares one state; any
  source XML change (incl. segmentation markup) starts a new document.
- parse_segments still selects `seg-source or source` (element truthiness).
- Other save() call sites do not catch StateBackupError/OSError.
- Re-uploading byte-identical content without emptying the uploader is
  ignored. Filter/tag-mode changes still drop unsaved widget edits.
- Unknown fields of termbase entries / prompt templates are still dropped.

## 6. Test-runner caution
- run_tests.py imports live tests and may contact an endpoint. Do not run it
  without approval. Name tests explicitly for targeted runs.

## 7. Constraints carried forward
- No commits, pushes, deploys, installs, live or paid calls, external service
  contact, or subagents without approval. Do not edit generated files by hand.
  Write files as UTF-8. Do not read the original worktree.
- .claude/settings.local.json and Start-Claude-Ollama.ps1 are unrelated local
  changes; leave untouched.

## 8. Next steps (not yet authorized)
- Human review and commit of the document-isolation change set.
- Legacy/unbound recovery flow; parse_segments seg-source alignment.
