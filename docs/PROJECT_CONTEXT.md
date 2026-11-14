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
- All infrastructure in §6 is future portfolio work; the proposed services,
  database and containers are not implemented.

## 3. Current architecture (single application)
- app.py: Streamlit UI AND orchestration (load, document switching, batch
  processing, Rerun, apply/diff, export, model Test handling).
- memoq_harness/: mqxliff.py (parse, fingerprint, export), tags.py
  (placeholders, target-canonical tags), state.py (state.json v5), llm.py
  (OpenAI-compatible and Anthropic adapters, bounded probe), prompts.py,
  diffing.py, rtf.py, xmlcompat.py.
- Generated, never hand-edited: defaults.py, prompt_templates.py
  (generator: tools/build_defaults.py).
- Data: config/termbase.csv. Local state: state.json. Exports: exports/.
- No services, database, containers or CI exist yet.

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

## 6. Roadmap (proposed sequence; not an approved ADR)
1. Completed working baseline (§5).
2. Incrementally extract at least three backend services, keeping the
   existing frontend; add PostgreSQL and storage for original/export files.
3. Reproducible containers and Docker Compose, introduced alongside step 2.
4. DO14: Helm/Kubernetes; GitLab CI/CD with exactly the stages build, test,
   staging, prod; GitLab Runner in Kubernetes.
5. Server provisioning/IaC, DNS/TLS and FluxCD GitOps.
6. Observability, tested backup/restore, failure/recovery demonstrations.
7. Portfolio packaging.
Service boundaries and the deployment stack are proposals only. One bounded
ADR is to be written after the exact DO14 requirements are reviewed.

## 7. Known limitations (carried into later phases)
- Original files are not stored; reload them after a restart.
- No legacy/unbound recovery UI (warning only).
- No revision matching or automatic transfer across changed source XML.
- Identical source identity can share state despite different translations.
- Successful memoQ re-import/round-trip not manually confirmed.
- Browser uploader: only helper logic is tested. No concurrency safety.
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
