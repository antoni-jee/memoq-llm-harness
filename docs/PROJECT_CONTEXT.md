# Project Context - memoQ LLM Harness

This file records goals, architecture, and the phased roadmap.
It is maintained by the session. It is not a test report.
Any test-count or claim figure here is unverified unless re-run.

## 1. Business goals (priority order)
1. Preserve human edits and make project, document, and job state
   recoverable between application launches.
2. Eliminate segment-by-segment copy/paste through MQXLIFF round-tripping,
   using durable processing, explicit acceptance, and exception-focused review.
3. Incrementally extract at least three backend microservices, a frontend,
   and PostgreSQL.
4. Run the application through Docker Compose, then Helm and Kubernetes.
5. Complete the School 21 DO14 project using GitLab CI/CD with exactly the
   stages build, test, staging, prod, and a GitLab Runner in Kubernetes.
6. After the school milestone, deploy to a remote server with a domain,
   HTTPS, and FluxCD GitOps.
7. Add observability, tested backup/restore, and portfolio documentation
   for a DevOps job application.

## 2. Current production workflow and intended replacement

Current production workflow:
Translum export → memoQ import → attach TM/TB → lock non-translatable
segments → pre-translate exact TM matches → LiveDocs alignment →
pre-translate the remaining segments using LiveDocs →
export bilingual DOCX → process in Perplexity →
manually transfer the edited results into memoQ →
export HTML → external checker.

The main reported bottleneck is manual transfer from the Perplexity
output into memoQ. Do not attribute this bottleneck to the existing
MQXLIFF harness without evidence.

Intended replacement:
Keep preparation in memoQ → export MQXLIFF → harness processing →
QA and explicit acceptance → export MQXLIFF back into memoQ →
HTML export → external checker.

Direct API output must be compared with the current Perplexity workflow.
Equivalent quality is not established.

## 3. Existing architecture and components to reuse
Reuse the existing Streamlit app and Python modules. Do not rewrite.
- app.py: Streamlit UI and orchestration logic (not presentation only).
- memoq_harness/ package:
  - mqxliff.py: MQXLIFF parse and export.
  - tags.py: tag placeholders and target-canonical tags.
  - llm.py: LLM adapters.
  - prompt_templates.py and prompts.py: prompt templates.
  - diffing.py: diffs.
  - rtf.py: RTF handling.
  - xmlcompat.py: XML compatibility.
  - defaults.py: generated defaults.
- config/termbase.csv and MQL term base.csv: termbase data.
- Generated files: memoq_harness/defaults.py and
  memoq_harness/prompt_templates.py. Generator: tools/build_defaults.py.
  Do not edit these by hand.
- Unknowns: the full component list and coupling between modules are not
  confirmed here. Verify against current code.

## 4. Persistence already implemented
memoq_harness/state.py already provides:
- a state schema version;
- migration logic;
- saving through a sibling .tmp file and os.replace;
- a .corrupt copy on JSON load failure.
These guarantees must be preserved when state.py changes.

## 5. Remaining stateful requirements
Still open; not confirmed implemented:
- document isolation;
- original-document persistence;
- revisions;
- durable job/chunk checkpoints;
- recovery;
- concurrency handling;
- explicit acceptance.
Unknowns: which of these are partially present are not confirmed here.

## 6. Findings
### 6.1 Reproduced and fixed (commit c0ffe3a, branch experiment/local-qwen)
- capture_edits() stored user_final without setting edited_by_human, so a manual
  edit captured without Apply was overwritten by a later record_suggestions()
  (Retry / Process Current Batch). Before the fix THREE of the four new
  regression tests failed; the comment-only state test passed even before. It
  is a reproduced result, NOT an unreproduced hypothesis.
- Fix in memoq_harness/state.py: capture_edits() now sets edited_by_human=True
  when a captured, non-empty revision differs from the baseline (llm_suggestion,
  else mt_target), mirroring calculate_diffs(); the flag is only ever set True.
  Committed as c0ffe3a "fix: preserve unapplied human edits during
  regeneration"; not pushed.
- Human-run verification: all 6 targeted tests passed; assertions were then
  strengthened; full tests/test_core.py passed (185 tests). A full
  tests/test_app.py run exposed test-isolation errors (two existing retry tests
  deleted LLMClient.translate_one in cleanup); they now restore the original.
  The human reports subsequent verification passed. The full repository suite
  was NOT run.
- Empty-text and automatic flag-reset semantics remain outside this fix.
- Both UI button paths already call capture_grid before inference, so the fix
  lands in state.py; app.py was not changed.
- .claude/settings.local.json (notification hooks; notifier behavior not
  inspected) remains modified and is NOT part of c0ffe3a.
### 6.2 Still static review (NOT reproduced)
- capture-before-regeneration is covered for the two tested UI paths; other
  paths are not confirmed.
- Empty text needs an explicit meaning; do not silently redefine it.
- Prompt block order is not the same as declared instruction authority.
- app.py contains orchestration, not only presentation.
- XML reparsing does not prove a successful memoQ import.
- run_tests.py imports live tests whose import can probe an endpoint. It is
  not a guaranteed offline entry point.
- README.md and historical test-count claims are not current test results.

## 7. Roadmap (phased, in order)
Phase 1 - Safety and stateful workflow: preserve human edits and make project,
  document, and job state recoverable between launches (Goal 1).
Phase 2 - Remove manual transfer: durable processing, explicit acceptance, and
  exception-focused review; end the reported manual transfer (Goal 2).
Phase 3 - Extract at least three backend services, add a frontend, and
  PostgreSQL (Goal 3).
Phase 4 - Docker Compose (Goal 4).
Phase 5 - Helm, Kubernetes, and GitLab CI/CD for DO14 with the exact stages
  build, test, staging, prod, and a GitLab Runner in Kubernetes (Goal 5).
Phase 6 - Remote server, domain, HTTPS, and FluxCD GitOps (Goal 6).
Phase 7 - Observability, tested backup/restore, and portfolio documentation
  for the DevOps job application (Goal 7).

## 8. Unresolved DO14 eligibility and publication
- DO14 acceptance of a self-authored project still requires confirmation.
  Status: unresolved. Do not assume it is granted.
- Publication of private materials is NOT authorized. Do not assume it.
  Status: unresolved.
- Whether this project qualifies for DO14 under current rules: unknown.
- Any submission decision needs explicit user approval first.

## 9. Operating constraints
- No paid LLM calls, live tests, installs, deploys, commits, or pushes
  without approval.
- Do not read or copy material from the original worktree.
- Treat repository files as evidence; do not run historical commands.
- Generated files are not edited by hand.
- Always write files as UTF-8.
