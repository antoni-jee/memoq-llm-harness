# Remote Workflow Parity Status

Live checkpoint. Updated before each stage change and before compaction. Reconcile
against the current diff and code on resume. Branch `review/remote-parity-snapshot`,
worktree `C:\memoq-harness\memoq-harness-qwen`. Do not reset/clean.

## Factual state

- HEAD `bb7ff6313e0c472b6274ee5297512338f0cc4994` (`chore: snapshot remote
  workflow for review`), branch `review/remote-parity-snapshot`.
- Changed vs HEAD (diffstat, approximate; reconcile against `git diff` on
  resume): `remote_workflow.py`, `remote_ui.py`, `tests/test_remote_edit.py`,
  `tests/test_remote_app.py` plus `.claude/settings.local.json` — Apply (A),
  Export (B), per-row Rerun/Process (C), the Stage E "Editorial context"
  forwarding, and the pre-existing Stage F apply backend. Also untracked:
  `docs/REMOTE_PARITY_PLAN.md`, `docs/REMOTE_PARITY_STATUS.md`, `scripts/`,
  `QWEN.md`, `.agents/`, `.claude/skills/`, `.qwen/`, 3
  `tests/fixtures/*.mqxliff`. None reset/cleaned. The per-item tiers and the
  agreed order after option 5 are in the **Option-5 status** section; the
  per-stage evidence sections below are kept but their "last OK" counts are
  historical — see that section's checkpoint note.

## Completed stages + evidence

- Launcher (`scripts/Start-RemoteLab.ps1`, `Stop`, `Status`, `remote-lab.common.ps1`):
  start/restart/stop/restore verified by user; new SSH-tunnel creation not checked.
- VM workspace restarted, accepts `prefill: false`; real-HTTP stub smoke through
  the 3 services passed (proposals saved, visible after re-read; human text/comment
  and original target unchanged).
- Stage A (done): `apply_proposal(record)` in `remote_workflow.py`; Apply button in
  `remote_ui.py` `_render_remote_row`. 6 tests in `tests/test_remote_edit.py`: apply
  makes an unsaved draft then Save persists; preserves held comment; no-op when
  already shown; button absent without a proposal; Apply->Discard restores saved
  human text + keeps the proposal (no PATCH, revision frozen); neighbor-draft kept.
- Stage B (done): `export_remote_document(mark_confirmed, only_changed)` in
  `remote_workflow.py` (~L793) + "4. Export" sidebar in `remote_ui.py` (2 checkboxes
  + "Export Valid MQXLIFF" + download of `last_export`). Carries `user_final` ONLY;
  skips non-translatable / empty user_final / only-changed equal-mt_target; warns on
  unsaved drafts without dropping them; "Nothing to export" if empty; document-service
  `export`->`export_content`->writes `EXPORT_DIR/<source_file>`->re-parses->`ss.last_export`.
  New test `test_export_carries_saved_human_text_and_preserves_rest` routes the
  document side through the REAL service (`_real_document_client`+`SMALL_XML`): saved
  user_final written; unaccepted proposal + unsaved draft NOT; source
  `source_fingerprint()` unchanged; excluded/empty rows keep memoQ target; empty
  user_final parity vs local `new_text()`=="". Old
  `test_read_only_grid_has_no_mutation_controls` updated to allow the export control;
  the local-orchestration ban (no `translate_batch`/`analyze_corrections`/`save`)
  stays in `test_local_mutation_entry_points_not_invoked`.

## Current stage, functions/files touched

- **Stage D.2 DONE (checkpoint 221)**: the 100->10 rebatch/feedback policy.
  `set_batch_size` in `services/workspace_service/service.py` (an explicit,
  revision-checked operation that re-slices the document and bumps
  `layout_version` on a real size change only); `RemoteSetBatchSizeTestCase`
  (7 tests) in `tests/test_remote_edit.py` drives the "Change batch size"
  button end-to-end through the real client seam; the processing-service
  `parse_analyze_request` now accepts `layout_version` as an optional
  correction field.
- **Stage E DONE (checkpoint 222)**: the E remainders (E.1 built-prompt
  application, E.2 memoQ-CSV termbase import, E.3 "reached payload" vs "client
  call args") are now closed; the per-item evidence is in the **Stage E
  remainder evidence** section below. Next: **G**. The remote termbase import
  is session-only (parsed entries live in `ss.remote_termbase`, sent per-process
  in the "context" block); no `ProjectState` is created on the remote path, no
  server-side storage, no termbase editor (all out of scope).

## Last test commands + results

Checkpoint **223** (E.2 narrow preservation check closed + offline-tested; the
"prepare acceptance" step of the option-5 order — NOT a live G run): the narrow
E.2 check the directive required is `tests.test_remote_edit.py`
`RemoteContextTestCase.test_no_term_csv_does_not_clobber_the_active_termbase` —
it loads a VALID seven-column memoQ CSV first (terms `cat`/`dashboard` populate
`ss.remote_termbase`), then loads a header-only (no usable terms) CSV, and asserts
BOTH that the clear `st.error` "No termbase terms loaded" surfaces AND that the
previously active termbase is preserved. That test FAILED first (the new
`[] != ['cat', 'dashboard']` assertion) and revealed a clobbering bug:
`load_termbase_csv_into_session` (`remote_workflow.py`) with its default
`replace=True` did `ss.remote_termbase = parsed`, so a no-term CSV (`parsed == []`)
wiped an active termbase to empty even though the caller still showed the error.
The existing `test_malformed_termbase_csv_shows_a_clear_error` could not catch
this because it starts from an empty termbase. Minimal fix (only the clobber case):
an early `if not parsed: return report` before the `existing`/replace/append
branches, so a no-term CSV leaves the session's termbase in place while the
caller still surfaces `report.summary()`; the replace/append branches are
unchanged, and `test_malformed_termbase_csv_shows_a_clear_error` (which starts
and stays at `[]`) still passes. Per-suite results: `tests.test_processing_service`
41 OK (unchanged), `tests.test_workspace_service` 62 OK (unchanged),
`tests.test_remote_app` 42 OK (unchanged), `tests.test_remote_edit` 79 OK (+1 from
checkpoint 222's 78: the new preservation test), `tests.test_remote_view` 8 OK
(unchanged), `tests.test_remote_client` 57 OK (unchanged). All six suites run
together: 289 OK, exit 0 (288 at checkpoint 222, +1). No live LLM call; no
Dockerfile/Helm/CI/CD or DO14 work; no commit/push. The `qwen3:4b` live 502
smoke remains OPEN (not retried). Commands:
`.venv\Scripts\python.exe -m unittest tests.<module>` per suite, plus the full
`tests.test_processing_service tests.test_workspace_service tests.test_remote_
app tests.test_remote_view tests.test_remote_client tests.test_remote_edit`.

Checkpoint **222** (E remainders E.1/E.2/E.3 closed + offline-tested; option-5
work). E.1 proves the assembled-prompt application through the REAL processing
service + `StubClient` (no model, no socket): `test_template_override_applies_
to_the_built_prompt` (the user's stage-template override text reaches the prompt
the stub engine receives, not just the forwarded context key) and
`test_relevant_termbase_term_reaches_the_built_prompt_and_irrelevant_is_
filtered` (a term that occurs in the batch is applied into the built prompt while
an irrelevant one is filtered out, reusing the local `relevant_terms` filter
inside the service). E.2 proves the memoQ-CSV termbase import on the state-free
remote path: `test_termbase_csv_upload_reaches_the_built_prompt` (a seven-column
memoQ export dropped in the new `Termbase CSV` uploader — `app.file_uploader[1]`,
after the XLIFF uploader at index 0 — parses state-free into
`ss.remote_termbase` as a `[{source,target}]` list and a relevant term reaches
the built prompt, an irrelevant one is filtered) and
`test_malformed_termbase_csv_shows_a_clear_error` (a header-only CSV loads no
terms and surfaces a clear `st.error` "No termbase terms loaded" instead of
silently doing nothing). E.3 separates "reached the payload" from "applied by
the client" for non-default generation settings: part (a)
`test_non_default_temperature_and_max_tokens_reach_the_payload` (remote UI
inference-setting widgets `temperature=0.35`, `max_tokens=1234` reach the
`/process` config payload, via the fake `FakeProcessingClient`, no model call)
and part (b) `test_non_default_temperature_and_max_tokens_reach_the_client_call`
(the config block -> real service `_client_config_from_block` ->
`llm.ClientConfig` -> the OpenAI-compatible backend's `LLMClient._chat`
`create(**kwargs)` kwargs carry `temperature=0.35`/`max_tokens=1234`, with the
lower SDK call mocked and no live model/network; Anthropic omits temperature by
design, so both values are asserted together on the OpenAI backend). No
`ProjectState` on the remote path, no server-side termbase storage, no termbase
editor (out of scope). Per-suite results: `tests.test_processing_service` 41 OK
(+1 from checkpoint 221's 40: E.3 part b), `tests.test_workspace_service` 62 OK
(unchanged), `tests.test_remote_app` 42 OK (unchanged),
`tests.test_remote_edit` 78 OK (+5 from checkpoint 221's 73: E.1 +2, E.2 +2, E.3
part a +1), `tests.test_remote_view` 8 OK (unchanged), `tests.test_remote_
client` 57 OK (unchanged). All six suites run together: 288 OK, exit 0
(282 at checkpoint 221, +6). No live LLM call; no Dockerfile/Helm/CI/CD or DO14
work; no commit/push. The `qwen3:4b` live 502 smoke remains OPEN (not retried).
Commands: `.venv\Scripts\python.exe -m unittest tests.<module>` per suite, plus
the full `tests.test_processing_service tests.test_workspace_service
tests.test_remote_app tests.test_remote_view tests.test_remote_client
tests.test_remote_edit`.

Checkpoint **221** (D.2 rebatch/feedback policy done + offline-tested; option-5
work): `RemoteSetBatchSizeTestCase` (7 new tests in `tests/test_remote_edit.py`)
drives the "Change batch size" button end-to-end through the real client seam:
(1) a 100->10 rebatch on 23 rows starts a new layout (`layout_version` bumps to
1, `batch_size` is 10, `batch_count` is 3); (2) a correction from the old layout
(batch_index 0, layout_version 0) is excluded from the new layout's prompt
(batch 2's `corrections_block` is empty); (3) returning to 100 does not
reactivate the old correction; (4) a stale revision does not mutate or start a
new layout; (5) held drafts survive the rebatch by segment key; (6) a same-size
request is a no-op (revision frozen, no new layout); (7) an Apply in the new
layout creates a new correction (layout_version 1) without erasing the old
layout's history. The processing-service `parse_analyze_request` now accepts
`layout_version` as an optional correction field (non-negative integer), and
`analyze_corrections` passes it through when constructing `state_mod.Correction`.
Per-suite results: `tests.test_processing_service` 40 OK,
`tests.test_workspace_service` 62 OK (+13 from checkpoint 220's 49),
`tests.test_remote_app` 42 OK, `tests.test_remote_edit` 73 OK (+7 from
checkpoint 220's 66), `tests.test_remote_view` 8 OK, `tests.test_remote_client`
57 OK (total 282). All six suites: 282 OK, exit 0.

Checkpoint **220** (F "Analyze as a separate action" done + offline-tested;
option-5 work): a NEW processing-service `/analyze` endpoint +
`ProcessingService.analyze_corrections` (resolves the client via
`_resolve_analyze_client`, converts the request's dict corrections to
`state_mod.Correction` objects so `prompts.build_analysis_payload` can read
their attributes, and returns
`{"operation":"analyze","engine","corrections_analyzed","critique","elapsed_
ms"}`); a NEW workspace `/workspaces/{document_id}/analysis` route ->
`WorkspaceService.record_analysis`, persisting the critique to a SEPARATE
`analysis[str(batch_index)]` slot (revision-checked, mirroring `apply_
corrections`) WITHOUT overwriting user_final/llm_suggestion/mt_target/draft;
the `run_remote_analysis` orchestration (no draft flush, unlike
`apply_remote_corrections`) + the "Analyse my corrections" UI panel;
`harness_remote.view`/`client` thread the `analysis` slot and the two client
methods. Per-suite results: `tests.test_processing_service` 40 OK (the new
`TestAnalyze`, 5 tests — empty corrections -> empty critique; non-empty -> a
"TRANSLATION BRIEF" critique; a malformed corrections value is rejected
wholesale; engine+config are mutually exclusive; a missing/empty corrections
list is accepted as a "critique of nothing"), `tests.test_workspace_service`
49 OK, `tests.test_remote_app` 42 OK, `tests.test_remote_edit` 66 OK (the new
`RemoteCorrectionsTestCase.test_analyze_button_persists_critique_without_
overwriting_translation` drives Apply -> Analyse through the REAL workspace +
processing client seam and asserts the critique lands in the `analysis` slot
while user_final/llm_suggestion stay untouched), `tests.test_remote_view` 8
OK, `tests.test_remote_client` 57 OK (total 262). The workspace
`record_analysis` service method pre-existed this checkpoint; only its HTTP
route was missing. This is distinct from D.2 (the 100->10 rebatch/feedback
policy on row 23's correction), which stays OPEN and is NOT moved to browser
acceptance.

Checkpoint **219** (F per-row Rerun stage/batch framing closed; option-5 work):
`tests.test_remote_edit` 65 OK (the new
`RemoteCorrectionsTestCase.test_rerun_uses_current_batch_stage_framing_in_
nonfirst_batch`, plus the updated `test_rerun_remote_row_survives_copy_save`
now asserting `call["batch"] == {"index": 0, "count": 1}`). The other remote
suites were not changed by this checkpoint and stand at their **218** results.

Checkpoint **218** (F per-row Rerun feedback use reconciled with local;
option-5 work):
`tests.test_workspace_service` 49 OK, `tests.test_remote_app` 40 OK,
`tests.test_remote_edit` 64 OK, `tests.test_remote_view` 8 OK,
`tests.test_remote_client` 57 OK. (The **217** checkpoint marked the F repeated-
Apply + stale-revision no-auto-retry done: `tests.test_remote_edit` 63 OK. The
**216** checkpoint marked the F two-batch scenario done:
`tests.test_remote_edit` 62 OK. The **215** checkpoint marked the
F "Apply Changes & Calculate Diffs" UI wiring complete:
`tests.test_remote_edit` 61 OK. The **214** checkpoint marked D.4 complete:
`tests.test_remote_edit` 60 OK. The **213** checkpoint marked D.3 complete:
`tests.test_remote_edit` 59 OK. The **212** checkpoint marked D.1 complete:
`tests.test_remote_edit` 58 OK. The earlier **56 + 40** checkpoint preceded the
option-5 work: `tests.test_remote_edit` 56 OK, `tests.test_remote_app` 40 OK.)
After each run this section records the exact command + result per suite, not a
bare total.

- `.venv\Scripts\python.exe -m unittest tests.test_processing_service` -> 40
  OK, exit 0.
- `.venv\Scripts\python.exe -m unittest tests.test_workspace_service` -> 62
  OK, exit 0 (checkpoint 221 added `TestSetBatchSize`, 13 tests, for the
  `set_batch_size` operation).
- `.venv\Scripts\python.exe -m unittest tests.test_remote_app` -> 42 OK,
  exit 0.
- `.venv\Scripts\python.exe -m unittest tests.test_remote_edit` -> 73 OK,
  exit 0 (includes `RemoteBatchSizeTestCase`, 3 tests,
  `RemoteChunkSizeTestCase`, 2 tests, `RemotePaginationTestCase`, 3 tests,
  `RemoteCorrectionsTestCase`, 6 tests, and `RemoteSetBatchSizeTestCase`, 7
  tests — the D.2 layout-versioning tests
  `test_change_batch_size_100_to_10_starts_new_layout`,
  `test_correction_in_old_layout_excluded_from_new_layout_prompt`,
  `test_return_to_original_size_does_not_reactivate_old_correction`,
  `test_stale_revision_does_not_mutate_or_start_layout`,
  `test_drafts_survive_rebatch_by_key`,
  `test_same_size_is_no_op_and_keeps_revision`, and
  `test_apply_in_new_layout_does_not_erase_old_history`).
- `.venv\Scripts\python.exe -m unittest tests.test_remote_view` -> 8 OK,
  exit 0.
- `.venv\Scripts\python.exe -m unittest tests.test_remote_client` -> 57 OK,
  exit 0.
- All six suites: `.venv\Scripts\python.exe -m unittest
  tests.test_processing_service tests.test_workspace_service
  tests.test_remote_app tests.test_remote_edit tests.test_remote_view
  tests.test_remote_client` -> 282 OK, exit 0.
- (`run_tests.py` is deprecated and not used.)
- Stage D tests (5 tests, all pass, no duplicate execution):
  * `test_large_fixture_pagination_visible_count_and_draft_survival`
    (29-row fixture, `remote_page_size=25` -> 25 rows page 1, 4 rows
    page 2, draft on k1 survives, batch row set unchanged, Save
    persists).
  * `test_large_fixture_whole_batch_shows_all_rows`
    (`remote_page_size=0` -> all 29 rows, draft preserved, batch row
    set unchanged).
  * `test_rows_shown_picker_drives_page_size_without_direct_write`
    (D.4: the REAL "Rows shown at once" picker move via `set_value`
    fires `on_change` -> `_on_rows_per_page_change` writes
    `remote_page_size=25`, the page radio appears, page 1 shows the
    first 25 rows; a held draft on k1 is saved; the batch row set is
    unchanged. The page-2 / return-to-page-1 transitions are NOT
    driven headlessly - the "Page" radio re-keys on each page change
    (key `remote_page_radio_%d_%d`), so AppTest drops the scripted
    `set_value`; that browser navigation is recorded as a concrete
    scenario for G, while the picker->page-size path IS headlessly
    confirmed. The existing
    `test_large_fixture_pagination_visible_count_and_draft_survival`
    still covers the page-2/return path via an explicit `remote_page`
    write.)
  * `test_non_default_chunk_size_reaches_multi_row_batch`
    (inject `session_state["remote_chunk_size_0"]=10`, non-stub preset,
    Process Current Batch -> `process_calls[0]["config"]["chunk_size"]==10`).
  * `test_batch_size_view_rebuild_preserves_drafts`
    (view rebuild via `from_remote` with same batch_size -> batch_size
    unchanged, draft on k1 survives, batch row set unchanged).

## Known limitations + next step

The authoritative per-item tiers and the agreed order after option 5 are in the
**"Option-5 status"** section below; this section keeps the cross-cutting
limitations. Per-row Process (distinct from per-row Rerun) is done and verified
(`test_process_remote_row_*`) and is not counted from the Rerun tests.

- Live browser Apply/Export/Rerun not confirmed; XML parse != memoQ import.
- `notify` single-slot: the "unsaved drafts" warning is overwritten by the
  "Exported" success, so the test verifies the requirement, not the warning.
- **Stage D done (D.1-D.4 all done, checkpoint 221).**
  D.1 (batch size as a server-stored, operator-chosen field; re-bind preserves
  it), D.3 ("Segments per request" reaches the `/process` payload and the REAL
  `translate_batch` chunk split) and D.4 (the picker -> page-size transition is
  headlessly driven) are verified. The page-2 / return-to-page-1 transition is
  NOT drivable headlessly (the "Page" radio re-keys on each page change), so it
  is recorded as a concrete browser scenario for G. **D.2** (the fate of
  already-stored corrections when a 100->10 size change moves batch
  boundaries) is **DONE + offline-tested (checkpoint 221)**: a 100->10 rebatch
  on 23 rows starts a new layout (`layout_version` bumps), the old layout's
  correction stays in history with its original `batch_index` and is excluded
  from the new layout's prompt; a 10->100 return does not reactivate the old
  correction; a stale revision does not mutate; held drafts survive by key; a
  same-size request is a no-op; and an Apply in the new layout creates a new
  correction without erasing the old history.
- **Stage E — forwarding done, USE closed at checkpoint 222.** What is verified
  (offline, stub, fail-closed): Hard Rules, Vibe, Batch Directive, the language
  pair, a per-batch stage-template override (`context.template`) and the
  operator's termbase (`context.termbase`) are forwarded as the `context` block
  on every Process call via `_remote_context_block` (all three call sites), and a
  typed hard rule reaches the prompt the real processing service builds. See
  "Stage E evidence".
  **E items (all closed at checkpoint 222; E.2's narrow preservation check
  closed at checkpoint 223; see "Stage E remainder evidence - USE half"):**
  E.1 confirms the template and a relevant term are *applied* in the
  assembled prompt (`test_template_override_applies_to_the_built_prompt`,
  `test_relevant_termbase_term_reaches_the_built_prompt_and_irrelevant_is_
  filtered`); E.2 adds a user-reachable termbase source (a memoQ-CSV uploader —
  no `ProjectState` in remote mode, session-only, no server storage, no editor —
  `test_termbase_csv_upload_reaches_the_built_prompt`,
  `test_malformed_termbase_csv_shows_a_clear_error`, and checkpoint 223's
  `test_no_term_csv_does_not_clobber_the_active_termbase` proving a no-term CSV
  surfaces the error without wiping an active termbase); E.3 separates non-default
  generation settings into "reached the payload" (`test_non_default_
  temperature_and_max_tokens_reach_the_payload`) vs "applied by the client"
  (`test_non_default_temperature_and_max_tokens_reach_the_client_call`).
- **F items (all claimed done, checkpoints 215-220).** The first corrections-loop
  scenario (a human edit becomes a stored `Correction`, survives a re-read, feeds
  the next batch's feedback) is DONE + offline-tested. The batch-level "Apply
  Changes & Calculate Diffs" button wiring is DONE + offline-tested (checkpoint
  215), and the offline two-batch scenario is now DONE + offline-tested
  (checkpoint 216):
  `RemoteCorrectionsTestCase.test_two_batch_scenario_correction_feeds_next_
  batch_prompt` drives the loop through the UI — 3 rows at batch size 2, edit +
  comment k1, Apply (persisting a batch-0 correction), Process batch 0 (its
  prompt must NOT carry the batch-0 correction, a batch never feeds itself),
  navigate "Next" to batch 1 and Process it (its assembled prompt MUST carry the
  batch-0 correction, with the `rejected:` line and the operator's comment).
  The F finish items (all DONE):
  * **Repeated Apply + a stale revision:** DONE + offline-tested (checkpoint
    217) — `RemoteCorrectionsTestCase.test_apply_button_stale_revision_is_not_
    retried_or_rebased` drives the button: a concurrent change (another
    session) advances the revision after the view's read, so the apply's
    expected revision is stale and the server 409s; the apply is not rebased
    or retried (the revision is unchanged, no correction is stored, the live
    view is not re-read), the run is marked `remote_process_uncertain` so the
    next Process/Apply is blocked until a Reload, and the operator is told the
    apply was not retried. No duplication (re-apply replaces, per the service;
    the no-op re-Apply case is covered by
    `test_apply_button_persists_batch_correction_through_ui`).
  * **Rerun feedback use reconciled with local:** DONE + offline-tested
    (checkpoint 218) — `RemoteCorrectionsTestCase.test_rerun_carry_corrections_
    block_from_earlier_batch` drives the UI to batch 1 and asserts the batch-0
    correction reaches the per-row Rerun's assembled prompt; the per-row Rerun
    now passes `view` and the current `batch_index` to
    `_remote_context_block(ss, view, batch_index)` (mirroring
    `process_remote_batch` and `process_remote_row`), carrying the
    corrections_block. **The per-row Rerun's stage/batch framing is CLOSED
    (checkpoint 219), distinct from D.2:** `rerun_remote_row` now sends
    `batch={"index": batch_index, "count": view.batch_count}` (where
    `batch_index = min(ss.get("remote_batch", 0), view.batch_count - 1)`),
    exactly like `process_remote_row` and `process_remote_batch`, so the service
    computes the stage from `stage_for_batch(batch_index, batch_count)` (e.g.
    batch 1 of 2 -> "mt3") and the "N of M" framing rather than the single-
    segment "mt1" / "1 of 1" default; the operation stays a single segment
    (`operation="rerun"`, `len(segments) == 1`). `RemoteCorrectionsTestCase.
    test_rerun_uses_current_batch_stage_framing_in_nonfirst_batch` drives a 3-
    row doc to batch 1 of 2, sets the "Stage template override" to a template
    carrying `{{BATCH_NUMBER}}`/`{{BATCH_COUNT}}` (the default template is `""`
    and the default stage templates carry no such tokens), spys the assembled
    prompt at `StubClient.translate_one`, clicks "Rerun", and asserts the prompt
    carries "batch=2 of 2" (not "batch=1 of 1"). D.2's rebatch/feedback policy
    stays OPEN and is NOT moved to browser acceptance.
  * **Analyze as a separate action:** DONE + offline-tested (checkpoint 220).
    A new processing-service `/analyze` endpoint (`ProcessingService.analyze_
    corrections`, resolving the client via `_resolve_analyze_client` and
    converting the request's dict corrections to `state_mod.Correction` so the
    critique layer can read their attributes) builds a warning-aware critique and
    returns `{"operation":"analyze","engine","corrections_analyzed","critique",
    "elapsed_ms"}`; a new workspace `/workspaces/{document_id}/analysis` route
    (`WorkspaceService.record_analysis`, revision-checked, mirroring
    `apply_corrections`) stores it in a SEPARATE `analysis[str(batch_index)]`
    slot, WITHOUT overwriting user_final/llm_suggestion/mt_target/draft;
    `run_remote_analysis` orchestrates it (no draft flush, unlike
    `apply_remote_corrections`) and the "Analyse my corrections" panel renders
    the result. `RemoteCorrectionsTestCase.test_analyze_button_persists_critique_
    without_overwriting_translation` drives Apply -> Analyse through the real
    client seam and asserts the critique lands in the `analysis` slot while the
    operator's translation slots stay untouched. This is a corrections-analysis /
    operator-correction-warnings critique layer, distinct from D.2's rebatch /
    feedback policy, which stays OPEN.
- **Next (option 5, in order):** **Stage D is DONE** (D.1 working batch size,
  D.2 100->10 boundary/feedback policy, D.3 Segments per request, D.4
  pagination headlessly driven). **Stage F is DONE + offline-tested
  (checkpoints 215-220):** the batch-level "Apply Changes & Calculate Diffs" UI
  wiring, the offline two-batch scenario, the repeated-Apply + stale-revision
  no-auto-retry via the button, the per-row Rerun's feedback use, the per-row
  Rerun's stage/batch framing (checkpoint 219), and Analyze as a separate action
  (checkpoint 220 — a new `/analyze` endpoint and a separate `analysis` slot
  persisting the critique without overwriting the operator's translation,
  distinct from D.2). With D, F and E complete, **G** is next. The E remainders
  (E.1 built-prompt application, E.2 memoQ-CSV termbase import, E.3 "reached
  payload" vs "client call args") are done + offline-tested at checkpoint 222;
  the template + termbase forwarding was done earlier. The one-time `qwen3:4b`
  live
  smoke (a G gate) is
  now **OPEN**: tunnels up and VM->Ollama confirmed by the user. It ran, fixed a
  400 (`max_retries` floor 1), and now hits HTTP 502 `engine_failed`; the exact
  cause of the live failure is unknown (the service returns no provider/upstream
  detail); no new live calls, network diagnostics, or service restarts are made
  now. See "Live real-model smoke". Not a G pass; does not close D. A
  dependency that forces a reorder is recorded in the Option-5 status with its
  reason; no item is struck through or re-marked done.

## Option-5 status (agreed road map; per-item tier: implemented / offline-
## checked / manual-or-live acceptance / blocker)

Option 5 = "full D in the sense of option 3, WITHOUT rewriting `StubClient`
and WITHOUT removing `session_state` from pagination." Confirmed last passing
checkpoint **before** option 5: `tests.test_remote_edit` **56 OK**,
`tests.test_remote_app` **40 OK** — kept here until the next successful run;
after that, commands and results are recorded, not a bare total. Confirmed
constraints in force: no `run_tests.py`, no live models/services, no deploy or
VM-service restart, no commit/push/reset/clean, user + untracked changes
preserved, 502 stays OPEN, DO14 untouched.

Order after option 5: **D -> finish F -> close E remainders -> G**. A
dependency that forces a reorder is recorded below with its reason; no item is
struck through or re-marked done, and no feature is silently dropped.

- **D.1 — working batch size (server source of truth):** DONE. `batch_size`
  is server-stored in the snapshot and the single source of truth for View,
  Process and Apply. `RemoteBatchSizeTestCase` (3 tests) covers the 23-row/10
  split (working batches 10/10/3), Reload survival, and that Apply processes
  the same row set the UI shows; held drafts survive by segment key.
- **D.2 — boundary change (смена границ):** DONE (checkpoint 221). An explicit,
  revision-checked `set_batch_size` operation that re-slices the document and
  starts a new layout. `layout_version` bumps on a real size change only
  (Reload / repeat-bind / same-size do NOT bump). Corrections keep their
  original `batch_index` and are tagged with the `layout_version` they were
  produced under, so the new layout's feedback never picks up an earlier
  layout's correction even when the batch indices collide. A 100->10 rebatch
  on 23 rows: the correction on row 23 (batch_index 0 in the old layout) stays
  in history with index 0 and does NOT reach the new batch 2's prompt; a 10->
  100 return does not reactivate the old correction. `RemoteSetBatchSizeTest
  Case` (7 tests) covers the new-layout start, old-correction exclusion, return
  no-op, stale revision, draft survival by key, same-size no-op, and apply-in-
  new-layout not erasing old history. The processing-service `parse_analyze_
  request` now accepts `layout_version` as an optional correction field.
- **D.3 — Segments per request (two offline tests; `StubClient` unchanged):**
  DONE. (1) the payload test
  `RemoteChunkSizeTestCase.test_non_default_chunk_size_reaches_multi_row_batch`
  injects `session_state["remote_chunk_size_0"]=10` and asserts
  `process_calls[0]["config"]["chunk_size"]==10`; the control is labeled
  "Segments per request" (renamed from "Context chunks" for parity). (2) the
  REAL split path `test_real_split_groups_23_segments_into_10_10_3` drives 23
  segments at `chunk_size=10` through the real `LLMClient.translate_batch` with
  `_chat` mocked, asserting three model requests sized 10/10/3. `StubClient` is
  unchanged.
- **D.4 — pagination:** DONE (picker move headlessly driven; page-2/return
  recorded for G). The application `session_state` and the existing state tests
  are kept. `RemotePaginationTestCase.test_rows_shown_picker_drives_page_size_
  without_direct_write` drives the REAL "Rows shown at once" picker via
  `set_value` -> `on_change` -> `_on_rows_per_page_change`, which persists
  `remote_page_size=25`, shows the page radio, and renders page 1 (25 of 29
  rows) with no direct `remote_page`/`remote_page_size` write. The page-2 /
  return-to-page-1 transitions are NOT drivable headlessly (the "Page" radio
  re-keys on each page change, `remote_page_radio_%d_%d`, so AppTest drops the
  scripted `set_value`); that browser navigation is recorded as a concrete G
  scenario, and the existing `test_large_fixture_pagination_visible_count_and_
  draft_survival` still covers the page-2/return path via an explicit
  `remote_page` write.
- **F — finish the corrections loop:** first scenario DONE + offline-tested;
  the batch-level "Apply Changes & Calculate Diffs" button is DONE + offline-
  tested (checkpoint 215); the offline two-batch scenario is DONE + offline-
  tested (checkpoint 216, `test_two_batch_scenario_correction_feeds_next_batch_
  prompt`: a batch-0 correction reaches batch 1's assembled prompt while a
  batch never feeds its own feedback); the repeated-Apply + stale-revision no-
  auto-retry is DONE + offline-tested (checkpoint 217, `test_apply_button_
  stale_revision_is_not_retried_or_rebased`: a concurrent change makes the
  apply's revision stale, the server 409s, and the apply is not rebased or
  retried, the run is marked `remote_process_uncertain`, and the no-op re-Apply
  does not duplicate the correction); the per-row Rerun's feedback use is now
  DONE + offline-tested (checkpoint 218, `test_rerun_carry_corrections_block_
  from_earlier_batch`: a batch-1 row's Rerun carries the batch-0 correction in
  its assembled prompt, because `rerun_remote_row` now passes `view` and the
  current `batch_index` to `_remote_context_block`); the per-row Rerun's stage/
  batch framing is now DONE + offline-tested (checkpoint 219,
  `test_rerun_uses_current_batch_stage_framing_in_nonfirst_batch`: a batch-1 of
  2 Rerun carries "batch=2 of 2" framing, because `rerun_remote_row` now sends
  `batch={"index": batch_index, "count": view.batch_count}`, distinct from D.2
  whose rebatch/feedback policy stays OPEN); and Analyze as a separate action is
  now DONE + offline-tested (checkpoint 220,
  `test_analyze_button_persists_critique_without_overwriting_translation`: a new
  `/analyze` endpoint + a separate `analysis[str(batch_index)]` slot persist the
  critique, returned to the operator without overwriting their translation, with
  result persistence checked against local — distinct from D.2). **All F items
  are now DONE + offline-tested (checkpoints 215-220); D.2's 100->10 rebatch /
  feedback policy stays OPEN.**
- **E — confirm USE of settings, not only forwarding:** forwarding DONE
  (above); the three remainders are now DONE (checkpoint 222):
  - **E.1 — applied in the assembled prompt (not just forwarded):** DONE.
    `test_template_override_applies_to_the_built_prompt` and
    `test_relevant_termbase_term_reaches_the_built_prompt_and_irrelevant_is_
    filtered` (in `tests/test_remote_edit.py` `RemoteContextTestCase`) prove,
    offline through the REAL processing service + `StubClient` (no model, no
    socket), that the user's stage-template override text and a relevant
    termbase term reach the prompt the engine actually receives, while an
    irrelevant term is filtered out (the service re-filters with the same
    `relevant_terms` the local workflow uses). Fail-closed: a guard forbids
    construction of any non-stub engine, so a wrong preset raises instead of
    opening a socket.
  - **E.2 — a user-reachable termbase source (memoQ-CSV uploader, no
    `ProjectState` in remote mode):** DONE. A new `Termbase CSV` uploader
    (`st.file_uploader`, index 1, after the XLIFF uploader at index 0, before
    `if view is None: return`) in `remote_ui.py` drives a state-free
    `parse_termbase_csv` in `state.py` (shared by local
    `ProjectState.load_termbase_csv` and remote `load_termbase_csv_into_session`
    in `remote_workflow.py`; no `ProjectState` is created on the remote path).
    `test_termbase_csv_upload_reaches_the_built_prompt` proves the user action
    from upload to a relevant term in the built prompt;
    `test_malformed_termbase_csv_shows_a_clear_error` proves a clear error for a
    non-loadable file; `test_no_term_csv_does_not_clobber_the_active_termbase`
    (checkpoint 223) proves a no-term CSV surfaces the error WITHOUT wiping an
    already-loaded termbase (the minimal `if not parsed: return report` guard in
    `load_termbase_csv_into_session` closes that clobber). Session-only load
    (entries live in `ss.remote_termbase`, sent per-process in the "context"
    block); no server storage, no termbase editor (out of scope).
  - **E.3 — separate "reached the payload" from "applied by the client" for
    non-default generation settings on a supported processing path:** DONE.
    `test_non_default_temperature_and_max_tokens_reach_the_payload` (remote UI
    -> `/process` config payload, via the fake processing client) and
    `test_non_default_temperature_and_max_tokens_reach_the_client_call` (config
    block -> real service `_client_config_from_block` -> `llm.ClientConfig` ->
    the OpenAI-compatible backend's mocked `create()` kwargs) are separate tests.
    The lower SDK call is mocked, so no live model/network.
  - G is the remaining separate acceptance (see the G item below).
- **G — separate acceptance, not a stand-in:** NOT started; NOT declared passed
  by the offline tests. First the targeted offline suites of the touched
  modules, then both remote suites; after a separate infrastructure
  authorization, live HTTP with the stub + PostgreSQL at the agreed service
  versions, a browser scenario on a test document, save/restore after restart,
  export, and a separate memoQ-import check (XML parse != import). The live
  `qwen3:4b` smoke is still OPEN (HTTP 502 `engine_failed`, exact cause
  unknown) — no new live calls or network diagnostics now.

## Stage C evidence (Per-row Process/Rerun, done)

- `remote_workflow.py`: `rerun_block_reason(view, record)` +
  `rerun_remote_row(record)`. Rerun is a one-segment
  `operation="rerun"`, but carries the current batch's `batch={"index":
  batch_index, "count": view.batch_count}` block (exactly like
  `process_remote_row`/`process_remote_batch`), so the service stages the re-run
  with the current batch's stage/framing (`stage_for_batch(batch_index,
  batch_count)`), not the single-segment "mt1" / "1 of 1" default; the context
  block is `_remote_context_block(ss, view, batch_index)` (carrying the
  corrections_block), and `tag_source` is from the view. The proposal is
  persisted via `record_suggestions(..., prefill=False)` so a saved
  `user_final`/comment/`mt_target` is untouched; Copy+Save survives a
  generation. Guards mirror the batch: stale revision / 409 / transport / 5xx /
  failed-or-mismatched confirming read -> `remote_process_uncertain` set, NOT
  retried, NOT rebased; no local/stub/model fallback.
- Per-row Process: `process_block_reason_for_row(view, record)` (L1612) +
  `process_remote_row(record)` (L1649). A one-segment batch Process (NO
  `operation="rerun"`, but a `batch={"index","count"}` block, like the batch
  path), persisted via `record_suggestions(..., prefill=False)`; the same
  uncertain-outcome guards as the batch apply. The button is offered only for a
  translatable row with no effective 'Your text' (a filled row has nothing to
  send), so it is hidden rather than disabled for ineligible rows.
- `remote_ui.py` `_render_remote_row`: per-row "Process" button (gated by
  `process_block_reason_for_row` + `row_is_process_eligible`, `st.rerun()` on
  click) and per-row "Rerun" button (gated by `rerun_block_reason`). Module
  docstring notes both; no Analyse / no tag injection yet.
- Tests in `tests/test_remote_edit.py`: Rerun — `test_rerun_remote_row_survives_
  copy_save` (seed v1, Reload, Apply+Save with comment, Rerun -> v2 lands in
  `llm_suggestion`, `user_final`/comment unchanged; the single-batch call asserts
  `call["batch"] == {"index": 0, "count": 1}`; every `/suggestions` POST has
  `prefill is False`, no uncertain flag),
  `test_rerun_remote_row_stale_write_is_not_retried` (concurrent write ->
  process once, `remote_process_uncertain` True, v1 stays, Rerun button
  disabled), `test_rerun_carry_corrections_block_from_earlier_batch` (batch-1
  Rerun carries the batch-0 corrections_block), and
  `test_rerun_uses_current_batch_stage_framing_in_nonfirst_batch` (batch 1 of 2,
  "Stage template override" with `{{BATCH_NUMBER}}`/`{{BATCH_COUNT}}`, Rerun ->
  prompt carries "batch=2 of 2" not "batch=1 of 1"). Process —
  `test_process_remote_row_sends_one_segment_and_keeps_
  drafts` (neighbours filled -> only k1 eligible -> one "Process" button; a
  real unsaved draft on k2 set via `edit_text` survives the call; one
  segment, no `operation`, a batch block; `config.chunk_size` asserted = 20
  ("Segments per request" sent); proposal in `llm_suggestion`, `user_final`
  empty, neighbours' `user_final`/comment untouched, k1 has no auto-draft,
  k2's draft kept, every `/suggestions` POST `prefill is False`, no
  uncertain flag) and
  `test_process_remote_row_stale_write_is_not_retried` (blank k1 eligible,
  concurrent write on k2 -> process once, `remote_process_uncertain` True,
  k1 unchanged, Process button disabled).
- Old `test_read_only_grid_has_no_mutation_controls` updated: "Process" and
  "Rerun" are now allowed controls (asserted present); the local-orchestration
  ban stays.

## Stage E evidence (Rules / prompts, forwarding done; USE closed at
## checkpoint 222 — see "Stage E remainder evidence - USE half")

- `remote_ui.py`: a sidebar "Editorial context" subheader offers three
  `st.text_area` controls — "Hard Rules" -> `ss.remote_hard_rules`, "Vibe" ->
  `ss.remote_vibe`, "Batch Directive / Operator Comment" -> `ss.remote_directive`
  — mirroring the local sidebar. They are session-only, like the model key.
- `remote_workflow.py`: `_remote_context_block(ss)` forwards the three fields
  (each only when not None) as the `context` block on every Process call. All
  three call sites send it — batch (`process_remote_batch`), re-run and single-row
  (`process_remote_row`) — via the `ProcessingClient.process` `context` arg, which
  the processing service folds into the system prompt it builds.
- Tests `tests.test_remote_edit.RemoteContextTestCase` (2, both pass, offline):
  * `test_user_hard_rule_reaches_the_built_prompt` — full UI->prompt path with the
    *real* processing service and the stub engine. **Fail-closed**: construction of
    any non-stub engine (both real-client paths converge on `llm.LLMClient`) is
    mocked to raise, so a wrong preset can't open a socket. The preset is driven
    through its widget (`app.selectbox[label=="Provider preset"].set_value(...)`
    + run) — not a raw `remote_model_preset` write, which the UI reverts — and the
    actually-selected preset and `remote_workflow._process_target(ss) == (None,
    "stub")` are asserted *before* the call. The typed hard rule is then asserted
    in the prompt the service actually built, captured by a spy on
    `engines.StubClient.translate_batch` (no real model, no payload substitution).
  * `test_editorial_context_is_forwarded_in_the_process_call` — the UI->request
    wiring in isolation: the fake records the `/process` call and the typed rule
    is read back off its `context` block (`hard_rules_text`/`vibe`/`directive`).
- Regression fix: this Streamlit version lists the sidebar's three Editorial
  `st.text_area` controls in `app.text_area` too, so the per-row "Your text"
  counts in both test files now filter by the `remote_edit_` key prefix
  (`your_text_areas` helper on each mixin), not the raw length.
- **This proves forwarding (the `context` block reaches the built prompt), not
  USE.** The USE half is now CLOSED at checkpoint 222 — see "Stage E remainder
  evidence - USE half (E.1/E.2/E.3, done, checkpoint 222)" below: E.1 confirms
  the user template and a relevant termbase term are *applied* in the assembled
  prompt (not just forwarded), E.2 adds the user-reachable memoQ-CSV termbase
  source (no `ProjectState`), and E.3 separates "reached the payload" from
  "applied by the client" for non-default generation settings. Do NOT re-run this
  green hard-rule->built-prompt test as fresh E evidence.

## Stage E remainder evidence (template + termbase forwarding; done)

This is the **forwarding** half of E; the **USE** half (a template / relevant
term actually applied in the assembled prompt; a user-reachable termbase source
or an explicit scope-out; generation settings separated into "reached the
payload" vs "applied by the client") is now CLOSED at checkpoint 222 — see the
"Stage E remainder evidence - USE half (E.1/E.2/E.3, done, checkpoint 222)"
section below and the Option-5 status above. The E-remainder below closes the two
editorial fields the E evidence above left
unforwarded: a per-batch **stage-template override** and the operator's
**termbase**. The processing service already accepted both (`context.template`
str|None, `context.termbase` a list of `{source, target, note?}`; `Context` in
`services/processing_service/service.py`), and with `template=None` it falls
back to the same `default_prompt_templates()` the local reference seeds and
re-filters `termbase` with the same `relevant_terms()` — so the remote only had
to forward them; a blank field is parity with the local default.

- `remote_workflow.py` bootstrap (`_bootstrap_remote`): seeds
  `ss.remote_template = ""` and `ss.remote_termbase = []`, session-only, like the
  editorial controls. `_remote_context_block` now forwards `template` (only when
  a non-empty string) and `termbase` (only when a non-empty list, sent as a
  copy) after the lang fields and before `corrections_block`.
- `remote_ui.py`: the "Editorial context" subheader gains a fourth `st.text_area`
  — "Stage template override" -> `ss.remote_template` (blank = use the service
  default) — mirroring the Hard Rules / Vibe / Batch Directive controls. The
  termbase has no UI control this step (a memoQ-CSV uploader needs a standalone
  parser and remote mode must not create a `ProjectState`); it follows the
  `remote_source_lang`/`remote_target_lang` precedent (seeded + forwarded, no
  control yet) — a browser-accepted collection control is the follow-up.
- Tests `tests.test_remote_edit.RemoteContextTestCase` (3 new, all pass,
  offline): `test_template_override_is_forwarded_in_the_process_call` (drives the
  new control, asserts `context["template"]` on the recorded `/process` call);
  `test_termbase_is_forwarded_in_the_process_call` (direct `remote_termbase`
  write, asserts the `context["termbase"]` entries); and
  `test_template_and_termbase_omitted_when_unset` (a fresh session forwards
  neither key, so the service falls back to its defaults). `tests.test_remote_
  edit` is 56 OK; `tests.test_remote_app` is 40 OK.

## Stage E remainder evidence - USE half (E.1/E.2/E.3, done, checkpoint 222)

This closes the **USE** half the two sections above left open: confirm the user
template and a relevant termbase term are *applied* in the assembled prompt (not
just forwarded), a user-reachable termbase source exists (a memoQ-CSV uploader,
no `ProjectState` in remote mode), and non-default generation settings are
separated into "reached the payload" vs "applied by the client". All offline;
no live model, no socket. The "Stage E evidence" and "Stage E remainder
evidence (template + termbase forwarding)" sections above are the forwarding
half; this is the application half.

- **E.1 — applied in the assembled prompt (real service + `StubClient`),** in
  `tests.test_remote_edit.RemoteContextTestCase`:
  * `test_template_override_applies_to_the_built_prompt` — the user's stage-
    template override text reaches the prompt the engine actually receives (the
    service substitutes it for the built-in role when it assembles the prompt),
    asserted on the built prompt via a spy on `engines.StubClient.translate_
    batch`, not just the forwarded `context.template`.
  * `test_relevant_termbase_term_reaches_the_built_prompt_and_irrelevant_is_
    filtered` — a termbase term that occurs in the batch is applied into the
    built prompt (its rendered `"source" -> "target"` pair) while an irrelevant
    term is filtered out and absent; the service re-filters with the same
    `relevant_terms` the local workflow uses. The termbase has no UI control in
    this test (E.2 adds it), so it is driven by a direct `remote_termbase`
    session-state write, following the `remote_source_lang` precedent.
  * Both are **fail-closed**: a guard mocks construction of any non-stub engine
    (both real-client paths converge on `llm.LLMClient`) to raise, so a wrong
    preset opens no socket; the selected preset and
    `remote_workflow._process_target(ss) == (None, "stub")` are asserted before
    the call.
- **E.2 — a user-reachable memoQ-CSV termbase source, no `ProjectState`,** in
  `tests.test_remote_edit.RemoteContextTestCase`:
  * `test_termbase_csv_upload_reaches_the_built_prompt` — a seven-column memoQ
    export uploaded through the new `Termbase CSV` uploader (`app.file_uploader
    [1]`, after the XLIFF uploader at index 0, added in `remote_ui.py` before
    `if view is None: return`) parses state-free into `ss.remote_termbase` as a
    `[{source, target}]` list (via `state.parse_termbase_csv`, shared by local
    `ProjectState.load_termbase_csv` and remote
    `remote_workflow.load_termbase_csv_into_session`; **no `ProjectState` is
    created on the remote path**); a term that occurs in the batch then reaches
    the built prompt, an irrelevant one is filtered.
  * `test_malformed_termbase_csv_shows_a_clear_error` — a header-only CSV loads
    no terms, leaves `ss.remote_termbase == []`, and surfaces a clear
    `st.error` "No termbase terms loaded" instead of silently doing nothing
    (the clear error for a non-loadable file the E.2 scope requires).
  * `test_no_term_csv_does_not_clobber_the_active_termbase` (checkpoint 223, the
    narrow E.2 preservation check the acceptance step required) — loads a VALID
    seven-column memoQ CSV first (terms `cat`/`dashboard` populate
    `ss.remote_termbase`), then loads a header-only (no usable terms) CSV, and
    asserts BOTH that the clear "No termbase terms loaded" error surfaces AND
    that the previously active termbase survives. This caught a clobbering bug:
    `load_termbase_csv_into_session` (default `replace=True`) set
    `ss.remote_termbase = parsed`, so a no-term CSV (`parsed == []`) wiped an
    active termbase to empty even though the caller still showed the error.
    Minimal fix (only the clobber case): an early `if not parsed: return report`
    before the `existing`/replace/append branches; the replace/append branches
    are unchanged and the previous test (which starts and stays at `[]`) still
    passes.
  * Session-only: the parsed entries live in `ss.remote_termbase` and are sent
    per-process in the `context` block; **no server-side storage, no termbase
    editor** (both out of scope).
- **E.3 — non-default `temperature`/`max_tokens`, "reached payload" vs "client
  call args" separated:**
  * (a) `tests.test_remote_edit.RemoteChunkSizeTestCase.test_non_default_
    temperature_and_max_tokens_reach_the_payload` — the remote UI inference-
    setting widgets' non-default `temperature=0.35` and `max_tokens=1234` reach
    the `/process` config payload (read off the recorded call by the fake
    `FakeProcessingClient`; no model call). A non-stub preset
    (`remote_model_preset = "Local Engine (Ollama)"`) makes
    `_process_target` emit the config block.
  * (b) `tests.test_processing_service.TestPerRequestConfig.test_non_default_
    temperature_and_max_tokens_reach_the_client_call` — the same values reach
    the OpenAI-compatible backend's actual `LLMClient._chat` `create(**kwargs)`
    kwargs, through the real service `_client_config_from_block` ->
    `llm.ClientConfig`, with the lower SDK call mocked (no live model/network).
    Anthropic's call omits `temperature` by design (proven locally by
    `test_temperature_is_never_sent_to_anthropic`), so both values are asserted
    together on the OpenAI backend.
- Per-suite results (checkpoint 222): `tests.test_processing_service` 41 OK
  (+1: E.3 part b), `tests.test_workspace_service` 62 OK, `tests.test_remote_
  app` 42 OK, `tests.test_remote_edit` 78 OK (+5: E.1 +2, E.2 +2, E.3 part a +1),
  `tests.test_remote_view` 8 OK, `tests.test_remote_client` 57 OK; all six
  suites together 288 OK, exit 0. (Checkpoint 223 adds the E.2 preservation
  test: `tests.test_remote_edit` 79 OK (+1), all six suites together 289 OK,
  exit 0.)

## Stage F evidence - first scenario (human edit -> correction -> re-read ->
## next batch's feedback; first implementation + offline test, done)

The first F scenario is done + offline-tested; the **rest of F is now DONE +
offline-tested (checkpoints 215-220, option-5: finish the corrections loop,
incl. Analyze as a separate action)**. The "Known limitations + next step"
section below records the closed F items.

This is the **first** F scenario, NOT a replacement for the other Analyze
requirements (analyze_corrections / the "operator correction warnings" critique
layer, now DONE at checkpoint 220). It ports the local `calculate_diffs` / `corrections_for_prompt`
/ `feedback_block` / `Correction.render` semantics to the remote path and proves
one human edit becomes a stored correction, survives a re-read, and feeds the
next batch's prompt - offline, no model/Ollama/network.

- `harness_remote/view.py` (transport-only, never imports `memoq_harness`):
  `RemoteWorkspaceView` now carries `corrections` (from `workspace.get("corrections")`,
  which `workspace_to_dict` emits). `corrections_block(upto_batch, limit, detailed)`
  renders the stored corrections that predate `upto_batch` (a batch cannot correct
  itself), newest first, capped to `limit`, in the local `Correction.render` format
  (`<segment_id: "human_final">` + indented `source:` / `rejected:` (only when the
  model's suggestion differs) / `note:`), reversed to oldest-first in the block, ""
  when empty - mirroring `corrections_for_prompt` + `feedback_block` without a
  `memoq_harness` import.
- `remote_workflow.py`: `_remote_context_block(ss, view=None, batch_index=0)` now
  forwards `corrections_block` (only when non-empty) as part of the `context` block;
  the batch-process and one-segment-process call sites pass `(ss, view, batch_index)`.
  `apply_remote_corrections(batch_index=None)` is the remote analog of the local
  `apply_and_diff`: guards (conflicts / uncertain / no document) -> `save_remote_drafts`
  -> `apply_corrections(document_id, revision, batch_index)` -> re-read + adopt; a
  stale/uncertain apply is reported, NOT retried, NOT rebased (no local fallback).
- Backend/client (prior session, verified): `workspace_service`
  `apply_corrections(document_id, request)` runs `state.calculate_diffs(batch_index)`,
  stores the produced corrections in the document snapshot (so they survive a re-read),
  no-ops (keeps revision) when nothing changed; `workspace_to_dict` emits `corrections`;
  `WorkspaceClient.apply_corrections(document_id, expected_revision, batch_index)` POSTs
  `/workspaces/{id}/apply`. The processing service already consumes
  `context.corrections_block` in `build_prompt`, so the gap was purely the remote
  client/workflow not building/forwarding it - now closed.
- Offline test `tests.test_remote_edit.RemoteCorrectionsTestCase.test_human_edit_
  becomes_correction_survives_reread_and_feeds_next_batch` (the 53rd test in the file):
  bind -> `record_suggestions` (model proposal on k1) + `save_segments` (differing human
  text + comment) -> `apply_corrections(0)` produces exactly one correction
  (`segment_id` u1, `source` "The cat.", `llm_suggestion` "Le chat (LLM).",
  `human_final` "Le chat humain.", `comment` "tone", `batch_index` 0; revision advanced,
  row `edited_by_human`); a fresh `get_workspace` re-read returns the stored correction
  (it lives in the snapshot, not in memory); a view rebuilt from the re-read renders it
  into `corrections_block(upto_batch=1)` in the local format (with `rejected:` because the
  proposal differs), and `_remote_context_block(ss, view, 1)` forwards the same text into
  the next batch's `context`; `corrections_block(upto_batch=0)` is "" (a batch cannot
  correct itself) and an empty correction set omits the `context` key entirely.
- Suites: `tests.test_remote_edit` 53 OK (+1), `tests.test_remote_app` 40 OK.
- **Decision (flagged) — SUPERSEDED by checkpoints 218/219:** the single-segment
  `rerun_remote_row` path was originally left WITHOUT `corrections_block` and
  WITHOUT a batch block. Both are now closed: checkpoint 218 carries the
  `corrections_block` via `_remote_context_block(ss, view, batch_index)`, and
  checkpoint 219 carries the current batch's `batch={"index", "count"}` block so
  the re-run is staged with the current batch's stage/framing. The operation stays
  a single segment (`operation="rerun"`, one segment).
- The live `apply` endpoint needs a service restart to take effect, which is deferred to
  the user-authorized G gate; the offline test builds its own `WorkspaceService` over the
  in-memory repository (the PostgreSQL transaction contract) instead.
- **The 502 stays OPEN** (see "Live real-model smoke"): HTTP 502
  `engine_failed`; the exact cause of the live failure is unknown. A local-fake
  success and `curl /api/tags` do NOT rule out a processing-service-env /
  SDK-HTTP / model-response problem, and "a small model returned non-JSON" is
  one unproven hypothesis, not the diagnosis. No new live calls, network
  diagnostics, or service restarts are made.

## Stage F evidence - two-batch scenario (a batch-0 correction reaches batch
## 1's assembled prompt; a batch never feeds its own feedback; done)

This is the offline two-batch scenario, driven through the UI with the real
processing service and the stub engine (no real model, no network). It proves
the full loop: edit + comment a row in batch 0, Apply (persisting a batch-0
correction), Process batch 0 (whose prompt must NOT carry the batch-0
correction), then navigate to batch 1 and Process it (whose assembled prompt
MUST carry the batch-0 correction, with the `rejected:` line and the operator's
comment). It complements the first scenario (the service-level proof) with a UI-
driven, prompt-capture proof.

- Offline test `tests.test_remote_edit.RemoteCorrectionsTestCase.test_two_
  batch_scenario_correction_feeds_next_batch_prompt` (offline, stub, fail-
  closed): a 3-row document at batch size 2 -> batches [k1, k2] and [k3]. The
  operator sets the preset to "Test backend (stub)" through its widget and a
  non-stub-engine construction is mocked to raise, so the engine stays offline.
  A model proposal is seeded on k1 (`record_suggestions`) and re-read, then
  k1 (row 0 of batch 0) is edited + commented through the per-row UI cells
  (`your_text_areas` / the new `comment_inputs` helper, both filtered by key
  prefix), the batch-level "Apply Changes & Calculate Diffs" button is clicked
  (persisting a batch-0 correction, asserted in the snapshot), and
  "Process Current Batch" is clicked for batch 0 — its assembled prompt is
  captured by a spy on `engines.StubClient.translate_batch` and must NOT carry
  the batch-0 correction (a batch never feeds itself). Navigating "Next" to
  batch 1 and processing it, the captured prompt MUST carry the batch-0
  correction: the `HUMAN CORRECTIONS FROM EARLIER BATCHES` marker, the
  `<u1: "Le chat humain.">` line, the `rejected: "Le chat (LLM)."` line (the
  model's differing proposal), and the operator's comment "use the human
  register".
- `tests.test_remote_edit` 62 OK (+1, the new two-batch-scenario test); the
  5 permitted offline suites total 216 OK. The live `apply`/`process` endpoints
  need a service restart to take effect, which is deferred to the user-
  authorized G gate; the offline test builds its own `WorkspaceService` over the
  in-memory repository and the real `ProcessingService` over a TestClient
  instead. **The 502 stays OPEN** (see "Live real-model smoke"): HTTP 502
  `engine_failed`; the exact cause of the live failure is unknown. No new live
  calls, network diagnostics, or service restarts are made.

## Stage F evidence - repeated Apply + a stale revision (the apply button does
## not rebase or auto-retry a 409; done)

This is the stale-write half of the "repeated Apply + a stale revision" finish
item, driven through the UI with the real workspace contract. It proves that a
button-driven Apply against a stale revision is neither rebased nor auto-
retried: a concurrent change (another session) advances the revision after the
view's read, so the apply's expected revision is stale and the server 409s; the
apply leaves the revision unchanged, stores no correction, does not re-read or
rebase the live view, marks the run `remote_process_uncertain` (so the next
Process/Apply is blocked until a Reload), and tells the operator the apply was
not retried. The no-duplication half (a no-op re-Apply does not duplicate the
correction and keeps the revision) is covered by
`test_apply_button_persists_batch_correction_through_ui`.

- Offline test `tests.test_remote_edit.RemoteCorrectionsTestCase.test_apply_
  button_stale_revision_is_not_retried_or_rebased` (offline, real workspace
  contract, no model, no network): a model proposal is seeded on k1 and a
  differing saved human edit is stored, so an Apply *would* produce a
  correction were it not stale. A re-read makes the live view hold the current
  revision with no held draft (so the apply's save step is a no-op). A
  concurrent write through the real service (`other_writer`) then advances the
  revision once, so the view the Apply was built on is stale, while the live
  view is not re-read and still names the old revision. The batch-level
  "Apply Changes & Calculate Diffs" button is clicked: the apply uses the stale
  view revision, the server 409s (`stale_revision`), and the assertions confirm
  the revision is unchanged (not rebased or retried), no correction is stored,
  the live view is left as-is (not re-read or rebased), the run is marked
  `remote_process_uncertain`, and an error message reports the apply was not
  retried.
- `tests.test_remote_edit` 63 OK (+1, the new stale-revision test); the 5
  permitted offline suites total 217 OK. The live `apply` endpoint needs a
  service restart to take effect, which is deferred to the user-authorized G
  gate; the offline test builds its own `WorkspaceService` over the in-memory
  repository instead. **The 502 stays OPEN** (see "Live real-model smoke"):
  HTTP 502 `engine_failed`; the exact cause of the live failure is unknown. No
  new live calls, network diagnostics, or service restarts are made.

## Stage F evidence - per-row Rerun feedback use reconciled with local (a
## batch-N row's Rerun carries the earlier batches' corrections; done)

This closes the "Rerun feedback use reconciled with local" finish item. The
remote per-row Rerun (`rerun_remote_row`) previously called
`_remote_context_block(ss)` *without* the `view` argument, so it omitted the
`corrections_block` — the human corrections recorded in earlier batches — that
the local reference's per-row retry (`retry_segment` ->
`build_prompt_for([record], current_batch)`, shared with the whole-batch run)
carries. The fix passes `view` and the current `batch_index` to
`_remote_context_block(ss, view, batch_index)` (mirroring `process_remote_batch`
and `process_remote_row`), so a re-run of a batch-N row is judged against the
same corrections as its batch. The operation stays a single segment
(`operation="rerun"`, `len(segments) == 1`); checkpoint 219 additionally sends
the current batch's `batch={"index", "count"}` block so the stage/framing follow
the current batch; the service's `process()` routes `operation="rerun"` to
`client.translate_one(records[0], system_prompt)`, where `build_prompt` honors
`context.corrections_block` (the corrections block is
computed client-side, with the correct current batch index, so the feedback use
is reconciled regardless of the service's stage computation).

- Offline test `tests.test_remote_edit.RemoteCorrectionsTestCase.test_rerun_
  carry_corrections_block_from_earlier_batch` (offline, real workspace contract,
  stub engine, no live model, no network): a 3-row document at batch size 2
  (batches [k1, k2] and [k3]); a model proposal is seeded on k1, k1 is edited +
  commented through the per-row UI cells, the batch-level "Apply Changes &
  Calculate Diffs" button is clicked (persisting a batch-0 correction:
  `segment_id="u1"`, `human_final="Le chat humain."`, `batch_index=0`), then
  "Next" advances to batch 1 and "Rerun" is clicked; the captured prompt (spied
  at `StubClient.translate_one`) MUST carry the batch-0 correction: the
  `HUMAN CORRECTIONS FROM EARLIER BATCHES` marker, the
  `<u1: "Le chat humain.">` line, the `rejected: "Le chat (LLM)."` line, and the
  operator's comment "use the human register".
- `tests.test_remote_edit` 64 OK (+1, the new per-row Rerun feedback-use test);
  the 5 permitted offline suites total 218 OK. **The 502 stays OPEN** (see
  "Live real-model smoke"): HTTP 502 `engine_failed`; the exact cause of the live
  failure is unknown. No new live calls, network diagnostics, or service
  restarts are made.
- **Per-row Rerun stage/batch framing (CLOSED, checkpoint 219, distinct from
  D.2):** with the corrections_block carried client-side (218), the per-row
  Rerun's *feedback use* is reconciled; 219 closes the remaining stage/framing
  gap. `rerun_remote_row` now sends `batch={"index": batch_index, "count":
  view.batch_count}` (where `batch_index = min(ss.get("remote_batch", 0),
  view.batch_count - 1)`), exactly like `process_remote_row`/
  `process_remote_batch`, so the service computes the stage from
  `stage_for_batch(batch_index, batch_count)` (e.g. batch 1 of 2 -> "mt3") and
  the "N of M" framing rather than the single-segment "mt1" / "1 of 1" default;
  the operation stays a single segment (`operation="rerun"`, one segment) and the
  corrections_block is still carried. Because the default `remote_template` is
  `""` and the default stage templates carry no `{{BATCH_NUMBER}}`/
  `{{BATCH_COUNT}}` tokens, the framing is observable only via a template
  override; `RemoteCorrectionsTestCase.test_rerun_uses_current_batch_stage_
  framing_in_nonfirst_batch` drives a 3-row doc to batch 1 of 2, sets the
  "Stage template override" to a template carrying those tokens, spys the
  assembled prompt at `StubClient.translate_one`, clicks "Rerun", and asserts the
  prompt carries "batch=2 of 2" (not "batch=1 of 1"). D.2's rebatch/feedback
  policy stays OPEN and is NOT moved to browser acceptance.
- `tests.test_remote_edit` 65 OK (+1, the new per-row Rerun stage/framing test,
  plus the updated `test_rerun_remote_row_survives_copy_save` now asserting
  `call["batch"] == {"index": 0, "count": 1}`).
- **Analyze as a separate action (CLOSED, checkpoint 220, distinct from D.2):**
  a new processing-service `/analyze` endpoint (`ProcessingService.analyze_
  corrections`, resolving the client via `_resolve_analyze_client` and converting
  the request's dict corrections to `state_mod.Correction` so the critique layer
  can read their attributes) builds a warning-aware critique and returns
  `{"operation":"analyze","engine","corrections_analyzed","critique","elapsed_
  ms"}`; a new workspace `/workspaces/{document_id}/analysis` route
  (`WorkspaceService.record_analysis`, revision-checked, mirroring
  `apply_corrections`) stores it in a SEPARATE `analysis[str(batch_index)]` slot
  WITHOUT overwriting user_final/llm_suggestion/mt_target/draft;
  `run_remote_analysis` orchestrates it (no draft flush, unlike
  `apply_remote_corrections`) and the "Analyse my corrections" panel renders the
  result. `RemoteCorrectionsTestCase.test_analyze_button_persists_critique_
  without_overwriting_translation` drives Apply -> Analyse through the real
  client seam and asserts the critique lands in the `analysis` slot while the
  operator's translation slots stay untouched. D.2's rebatch/feedback policy
  stays OPEN and is NOT moved to browser acceptance.
- `tests.test_processing_service` 40 OK (the new `TestAnalyze`, 5 tests),
  `tests.test_workspace_service` 49 OK, `tests.test_remote_app` 42 OK,
  `tests.test_remote_edit` 66 OK (+1, the new Analyze-reach test),
  `tests.test_remote_view` 8 OK, `tests.test_remote_client` 57 OK; all six
  offline suites total 262 OK.

## Live real-model smoke (G gate, attempted - OPEN, not final)

A limited one-time live smoke on the user's Ollama `qwen3:4b` via the app's
remote processing path (one synthetic segment, one Process, chunk_size=1,
explicit timeout, small output-token limit, no retries / real doc / paid
cloud). This is a Stage-G gated item; it is NOT full remote acceptance and NOT
proof of D. The gate is now **OPEN**: the user started the tunnels and
confirmed VM->Ollama by running `curl http://10.211.55.2:11434/api/tags` from
inside the VM (model list returned). Findings:

- **Endpoint + preset (no invented IP, no localhost, no changed settings):**
  `OLLAMA_HOST=10.211.55.2:11434` is set in the environment, so the existing
  "Local Engine (Ollama)" preset's `base_url` (`ollama_base_url()`) resolves to
  `http://10.211.55.2:11434/v1` — exactly what the preset fills by default.
- **Model availability + exact tag:** `GET /api/tags` on `10.211.55.2:11434`
  is reachable and lists the exact tag `qwen3:4b` (4.0B, Q4_K_M, `qwen3`
  family, digest `359d7dd4…`). Present — no download / no substitution needed.
- **Gate OPEN + run:** tunnels up; VM->Ollama confirmed by the user via
  `curl /api/tags`. `GET /process` reaches the service (`/readyz` ->
  `{"status":"ready","engine":"stub"}`, the service's default engine).
- **Step 1 - 400, fixed.** First run failed at parse time:
  `{"error":"bad_request","message":"'config.max_retries' must be between 1 and
  10."}`. Contract finding: the service bounds `max_retries` to **1..10**
  (floor 1), so "no retries" must be `max_retries=1` (the loop
  `range(max(1, max_retries))` runs exactly one attempt), **not** 0. After the
  fix the request parsed.
- **Step 2 - 502 (current, OPEN).** With a valid request the service built the
  real Ollama client and attempted the call, which failed:
  `{"error":"engine_failed","message":"Every engine request failed.","details":
  {"failed_requests":1,"requests":1}}`. This is HTTP 502 `engine_failed`; the
  exact cause of the live failure is unknown - by design the service returns
  **no** provider/upstream detail (nothing from a provider is returned or
  logged), so the exact upstream cause is hidden. It is NOT a VM/Ollama failure
  (the user confirmed VM->Ollama and the request now parses), but it is not
  attributed to an engine/contract/client bug until a bounded local test
  pinpoints it. No new live calls, network diagnostics, or service restarts are
  made.
- **Status:** OPEN, not a G pass, does NOT close D. Ollama, the VM, and network
  routes are untouched. Next: bounded local reproduction of the engine path to
  find the concrete failure point and a minimal repro.

### Local diagnosis of the 502 (offline; no live model/Ollama/network)

Repro: `.qwen/tmp/remote-502-local-repro.py` (4 cases, all 127.0.0.1, no
Ollama/VM/network). Findings:

- **Concrete failure point:** `services/processing_service/service.py`
  `process()` L464-465 raises `EngineFailed("Every engine request failed.",
  {failed_requests, requests})` whenever `client.translate_batch(...)` returns
  `result.errors` non-empty with no suggestions. The per-chunk error text (the
  real cause) lives in `result.errors` but is **NOT** surfaced in
  `EngineFailed.details` - that is why the 502 looks opaque.
- **The happy path is correct (not a contract bug):** a local fake OpenAI
  `/v1/chat/completions` returning a valid completion makes `translate_batch`
  **succeed** (suggestions populated, no errors). So config block -> ClientConfig
  -> OpenAI SDK -> JSON parse -> `_iter_segments` is sound.
- **The exact 502 is reproduced in-process** by driving the real
  `service.process()` with a config whose `base_url` is a closed local port:
  `EngineFailed('Every engine request failed.', {failed_requests:1, requests:1})`
  - the live 502 shape.
- **Failure-mode enumeration (which live responses -> 502):**
  `200 + valid segments JSON` -> SUCCESS; `200 + unparseable content` -> 502
  ("Model did not return usable JSON (N chars)"); `200 + empty content` -> 502
  ("Model returned an empty response"); `404/500/401/429` -> 502.
- **Authoritative code fact (corrects a stale snapshot):** the current
  `llm._translate_chunk` L343-346 **raises** `LLMError("Model did not return
  usable JSON (%d chars).")` when `extract_json` returns `None` (it does NOT
  silently skip), and `translate_batch` L313-314 catches it into
  `result.errors` -> `EngineFailed`.
- **Conclusion:** HTTP 502 `engine_failed`; the exact cause of the live failure
  is unknown. It is NOT a VM/Ollama reachability issue (happy path proven;
  VM->Ollama user-confirmed) and NOT a happy-path code bug. The offline failure-
  mode enumeration shows which live responses yield a 502 (a non-200, or a 200
  with content `extract_json` cannot parse, or an empty response); these are
  candidate causes, not the confirmed one. In particular, "a small model
  returned non-JSON" is one unproven hypothesis, NOT the diagnosis. The exact
  live cause is hidden by `EngineFailed.details`; pinning it needs the real
  `result.errors` text (a diagnostic-only change to surface it, or the authorized
  live smoke) - no new live calls, network diagnostics, or service restarts now.
  No repo code changed; Ollama/VM/network untouched.

## Launch scheme

`scripts\Start-RemoteLab.ps1` opens 4 SSH tunnels (18101->8101, 18102->8102,
18103->8103, 15432->5432) to `vagrant@192.168.56.10` (key
`%USERPROFILE%\.ssh\memoq_lab`), starts VM services (`labctl.sh start-all`) + the
local Streamlit UI on `127.0.0.1:8502` with `HARNESS_BACKEND=remote` + the three
backend URLs. Stop/Status: `scripts\Stop-RemoteLab.ps1`, `scripts\Status-RemoteLab.ps1`;
diagnostics `scripts/remote-lab-diag.py`. Stub = "Test backend (stub)". No
credential or DB string printed or committed.
