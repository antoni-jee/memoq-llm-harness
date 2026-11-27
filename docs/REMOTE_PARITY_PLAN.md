# Remote Workflow Parity Plan

Stable goal and readiness gate for bringing the remote memoQ LLM harness to
parity with the local reference workflow. This is the recovery aid; `STATUS.md`
is the live checkpoint. On conflict with the code or test output, trust those.

## Goal

Finish the main remote workflow end to end: reproducible launch -> remote UI ->
Open MQXLIFF -> provider/model/batch/prompt settings -> Process and per-row
Rerun -> Copy proposal to draft -> edit -> Save / Discard -> Apply corrections
/ feedback / Analyze -> Export MQXLIFF through document-service.

Architecture: `Streamlit -> document / workspace / processing -> PostgreSQL`.
The local mode is the **reference behavior**, not a fallback for a missing
remote function. A successful local run (Haiku, batch 10, export) is an
established fact, not remote acceptance. DO14 and the deployment stack are
out of scope for this pass. After a stage applies: acceptance check, then a
publication-security check, then a separately agreed commit/push.

## Readiness criteria

- Every user-facing feature in the matrix is reachable in remote mode with the
  same observable behavior as local mode.
- No hidden fallback to local, to a stub engine, or to a live model.
- No write is auto-retried on a revision conflict or an uncertain outcome.
- Each claim is labeled by evidence tier: unit / headless AppTest / live HTTP
  with stub + PostgreSQL / real browser / memoQ import. XML parse alone does
  not prove memoQ import.

## Feature matrix

| # | Feature | Local reference | Remote status |
|---|---------|-----------------|---------------|
| 1 | Open MQXLIFF (upload, bind, open) | done | done |
| 2 | Reload / navigation (no model call) | done | done |
| 3 | Provider / model / batch / prompt settings | done | done (D done; E forwarding + USE done at checkpoint 222; E.2 no-term preservation check at checkpoint 223) |
| 4 | Process (batch) + per-row Process/Rerun | done | done |
| 5 | Copy proposal to draft (Apply) | done | A |
| 6 | Edit draft (text/comment) | done | done |
| 7 | Save / Discard | done | done |
| 8 | Apply corrections / feedback / Analyze | done | F |
| 9 | Export MQXLIFF via document-service | done | B |
| 10 | Reproducible launch | done | launcher done; new-SSH-tunnel-when-none unverified |

## Key guarantees (must not regress)

- Four distinct data kinds: original target (`mt_target`), model proposal
  (`llm_suggestion`), saved human text (`user_final`), held draft.
- Copy/Apply mutates only the draft; only an explicit Save persists.
- Process/Rerun write proposals with `prefill: false`.
- A filled `user_final` + comment is not destroyed by a later generation.
- `edited_by_human = False` after a verbatim accept is **not** permission to
  overwrite the saved `user_final`.
- Open / Reload / navigation never call the model.
- No hidden local / stub / model fallback in remote mode.
- Revision conflicts and uncertain outcomes are not auto-retried.
- Preserve unrelated / neighbor drafts on any single-row action.
- Preserve explicit empty-string edits (saved as empty).
- Old "remote function absent" tests reflect old scope: update them as the
  feature lands, keeping the ban on local orchestration.

## Order of remaining stages

A. Close Copy-proposal checks: Apply -> Discard; neighbor-draft preservation on
   Apply. Use the current implementation; do not rewrite without a found defect.
B. Remote Export: UI -> saved workspace -> document-service export -> user
   receives the file -> re-parse/verify. Handle unsaved drafts; carry selection,
   status and tag semantics from local; verify export carries the human text and
   does not change source/locked segments.
C. Per-row Process/Rerun: processing-service + record suggestions; each updates
   the proposal without erasing saved text/comment; verify Copy+Save survives a
   generation. Both are done: per-row Rerun (`rerun_remote_row`, one-segment
   `operation="rerun"`) and per-row Process (`process_remote_row`, one-segment
   batch, no `operation`), each with its own tests; per-row Process is not
   counted from the Rerun tests.
D. Batch / processing settings: working batch size vs segments-per-request vs UI
   pagination are distinct; implement the needed local settings in remote, with
   view rebuild + draft persistence. Pagination (Rows shown at once) is done;
   Batch size and Segments per request are separate tasks.
E. Rules / prompts: user hard/soft rules, templates and generation settings are
   passed to processing and used; verify the request / built-prompt content
   without paid calls. **Forwarding and USE are done (checkpoint 222; E.2 no-term
   preservation check at checkpoint 223).** The
   sidebar's "Editorial context" controls (Hard Rules / Vibe / Batch Directive),
   the language pair, a per-batch stage-template override (`context.template`) and
   the operator's termbase (`context.termbase`, a list of `{source, target,
   note?}`) are forwarded as the `context` block on every Process call (batch,
   re-run, single-row) via `_remote_context_block`; `tests.test_remote_edit.
   RemoteContextTestCase` proves a typed hard rule reaches the prompt the *real*
   processing service builds, with the stub engine (fail-closed: any non-stub
   engine construction is mocked to raise, so a wrong preset can't open a
   socket), and that the template override and termbase reach the forwarded
   context (omitted when unset, so the service falls back to its own default
   templates / no termbase = the local default). The service already accepted
   both fields and re-filters `termbase` with the same `relevant_terms()`, so
   the remote only forwards. E remainders are now CLOSED (checkpoint 222, see
   the option-5 road map and `docs/REMOTE_PARITY_STATUS.md` "Stage E remainder
   evidence - USE half"): E.1 confirms the user template and a relevant term are
   *applied in the assembled prompt* (not merely forwarded) —
   `test_template_override_applies_to_the_built_prompt` and
   `test_relevant_termbase_term_reaches_the_built_prompt_and_irrelevant_is_
   filtered` in `tests.test_remote_edit.RemoteContextTestCase`, through the real
   processing service + `StubClient` (fail-closed: any non-stub engine
   construction is mocked to raise, so a wrong preset can't open a socket);
   E.2 adds a user-reachable termbase data source — a memoQ-CSV uploader
   (`st.file_uploader`, after the XLIFF uploader, before `if view is None:
   return`) that parses state-free via `state.parse_termbase_csv` (shared by
   local `ProjectState.load_termbase_csv` and remote
   `load_termbase_csv_into_session`; **no `ProjectState` on the remote path**,
   session-only, no server storage, no editor) — proven by
   `test_termbase_csv_upload_reaches_the_built_prompt` (a relevant term reaches
   the built prompt), `test_malformed_termbase_csv_shows_a_clear_error` (a clear
   error for a non-loadable file), and checkpoint 223's `test_no_term_csv_does_
   not_clobber_the_active_termbase` (a no-term CSV surfaces the error WITHOUT
   wiping an already-loaded termbase; the minimal `if not parsed: return report`
   guard closes that clobber); and E.3 separates non-default
   generation settings into "reached the payload" (`test_non_default_
   temperature_and_max_tokens_reach_the_payload`, remote UI -> `/process` config
   payload via the fake client) vs "applied by the client" (`test_non_default_
   temperature_and_max_tokens_reach_the_client_call`, the config block through
   the real service `_client_config_from_block` -> `llm.ClientConfig` -> the
   OpenAI-compatible backend's mocked `create()` kwargs, no live model).
F. Apply corrections / feedback / Analyze: a separate workflow, not
   Copy-to-draft; port local semantics through service contracts; extend
   backend/client if needed, with tests + persistence. The first scenario (a
   human edit -> stored correction -> re-read -> the next batch's feedback) is
   done and offline-tested; the finish of F (per the option-5 road map above) is:
   drive "Apply Changes & Calculate Diffs" separately from "Copy proposal"
   (DONE, checkpoint 215); an offline two-batch scenario — a batch-0 correction
   reaching batch 1's assembled prompt while a batch never uses its own feedback
   (DONE, checkpoint 216); repeated Apply and a stale revision show no
   duplication / no auto-retry (DONE, checkpoint 217, driven via the button);
   Rerun's feedback use reconciled with local (DONE, checkpoint 218) and the
   per-row Rerun's stage/batch framing closed (DONE, checkpoint 219, distinct
   from D.2's rebatch/feedback policy, which stays OPEN); and Analyze as a
   separate action returning warnings/critique to the operator without
   overwriting their translation, with its result persistence checked against
   local (DONE, checkpoint 220 — a new `/analyze` endpoint + a separate
   `analysis[str(batch_index)]` slot; distinct from D.2, now closed at
   checkpoint 221). All F finish items are now DONE + offline-tested
   (checkpoints 215-220); D.2's 100->10 rebatch / feedback policy is DONE
   (checkpoint 221).
G. Overall regression + acceptance: first the targeted offline suites of the
   touched modules, then both remote suites; after a separate authorization for
   infrastructure, live HTTP with the stub + PostgreSQL at the agreed service
   versions, a browser scenario on a test document, save/restore after restart,
   export, and a separate memoQ-import check (XML parse != import); a limited
   real-model test only after approval.

## Road map after option 5 (agreed; not a re-plan)

Option 5 = "full D in the sense of option 3, but WITHOUT rewriting `StubClient`
and WITHOUT removing `session_state` from pagination." The agreed order after it
is **D -> finish F -> close E remainders -> G**. A dependency that forces a
reorder is recorded in STATUS (with the reason); a stage is never struck through
or re-marked done, and no feature is silently dropped. Per-stage reports do not
end the pass.

- **D — working batches, request chunks, pagination** (D.1/D.2/D.3/D.4 all done):
  * *Batch size* (**D.1 done**) is stored in the workspace (the server
    snapshot) and is the
    single source of truth for View, Process and Apply — they use one set of
    boundaries. Choosing 10 on 23 segments yields working batches 10/10/3;
    Reload preserves the size; held drafts stay bound to their row keys. The
    revision change on a size change is checked.
  * *Boundary change* (**D.2 done, checkpoint 221**): an explicit,
    revision-checked `set_batch_size` operation that re-slices the document and
    starts a new layout (`layout_version` bumps on a real size change only;
    Reload / repeat-bind / same-size do NOT bump). Corrections keep their
    original `batch_index` and are tagged with the `layout_version` they were
    produced under, so the new layout's feedback never picks up an earlier
    layout's correction even when the batch indices collide. A 100->10 rebatch
    on 23 rows: the correction on row 23 (batch_index 0 in the old layout) stays
    in history with index 0 and does NOT reach the new batch 2's prompt; a 10->
    100 return does not reactivate the old correction. `RemoteSetBatchSizeTest
    Case` (7 tests) covers the new-layout start, old-correction exclusion, return
    no-op, stale revision, draft survival by key, same-size no-op, and apply-in-
    new-layout not erasing old history.
  * *Segments per request* (**D.3 done**): proved separately that (a)
    changing the correct UI control changes the `/process` payload
    (`test_non_default_chunk_size_reaches_multi_row_batch`, via
    `session_state["remote_chunk_size_0"]=10`), and (b) the REAL chunk split
    of 23 segments at `chunk_size=10` yields groups 10/10/3 into a MOCKED
    (locked) model call (`test_real_split_groups_23_segments_into_10_10_3`,
    driving `LLMClient.translate_batch` with `_chat` patched). The control was
    renamed from "Context chunks" to "Segments per request" to match the
    local reference. `StubClient` is NOT changed for this.
  * *Pagination* (**D.4 done**): drive the real control transitions WITHOUT a
    direct `page`/`page_size` (or `remote_page`/`remote_page_size`) write
    standing in for a user action. The "Rows shown at once" picker move IS
    headlessly driven (`test_rows_shown_picker_drives_page_size_without_direct_write`):
    `set_value(25)` on the selectbox fires `on_change` ->
    `_on_rows_per_page_change`, persists `remote_page_size=25`, shows the page
    radio, and renders page 1 (25 of 29 rows) — no direct write. The page-2 /
    return-to-page-1 transitions are NOT drivable headlessly: the "Page" radio
    re-keys on each page change (`remote_page_radio_%d_%d`), so AppTest drops
    the scripted `set_value`; that browser navigation is recorded as a concrete
    scenario for G. The application `session_state` and the existing state
    tests (incl. the page-2/return path via an explicit `remote_page` write)
    are kept.
- **F — finish the corrections loop** (after D): the first scenario (a human
  edit becomes a stored `Correction`, survives a re-read, and feeds the next
  batch's feedback) is DONE + offline-tested; do not redo it. All finish items
  are now DONE + offline-tested (the batch-level Apply button at checkpoint 215,
  the offline two-batch scenario at checkpoint 216, the repeated-Apply +
  stale-revision no-auto-retry via the button at checkpoint 217, the per-row
  Rerun's feedback use at checkpoint 218, the per-row Rerun's stage/batch
  framing at checkpoint 219, and Analyze as a separate action at checkpoint 220;
  D.2's rebatch/feedback policy stays OPEN, recorded so it is not silently
  dropped):
  * *"Apply Changes & Calculate Diffs" as a distinct UI action, separate from
    "Copy proposal."* **DONE (checkpoint 215).** The batch-level button is wired
    in `remote_ui.py` (driving `apply_remote_corrections`, which saves held
    drafts then runs the server's `apply_corrections`), `tests/test_remote_app.
    py` now asserts the control is present (it was previously a forbidden
    control), and `RemoteCorrectionsTestCase.test_apply_button_persists_batch_
    correction_through_ui` drives it end to end: a held draft -> Apply -> a
    stored correction that survives a re-read and reaches the next batch's
    feedback, and a no-op re-Apply neither duplicates it nor advances the
    revision. The backend was already in git and verified (service
    `apply_corrections`, client `apply_corrections`, orchestration
    `apply_remote_corrections`); this item closed the UI-wiring gap.
  * *Offline two-batch scenario:* **DONE (checkpoint 216).** edit + comment via
    UI -> Apply -> re-read -> Process batch 0 and batch 1; batch 0's prompt does
    NOT carry its own correction (a batch never feeds itself) and batch 1's
    assembled prompt carries the batch-0 correction.
    `RemoteCorrectionsTestCase.test_two_batch_scenario_correction_feeds_next_
    batch_prompt` drives it through the UI: a 3-row document at batch size 2
    (batches [k1, k2] and [k3]), a model proposal seeded on k1, k1 edited +
    commented through the per-row UI cells, the batch-level "Apply Changes &
    Calculate Diffs" button clicked (persisting a batch-0 correction), then
    "Process Current Batch" for batch 0 (its captured prompt must NOT carry the
    correction) and, after navigating "Next", for batch 1 (its captured prompt
    MUST carry the `<u1: "Le chat humain.">` line, the `rejected:` line, and the
    operator's comment).
  * *Repeated Apply + a stale revision:* **DONE (checkpoint 217).** No
    duplication: the no-op re-Apply case (re-apply replaces rather than
    appends, and a no-change apply keeps the revision) is covered by
    `RemoteCorrectionsTestCase.test_apply_button_persists_batch_correction_
    through_ui`. No auto-retry on a 409 / stale revision:
    `RemoteCorrectionsTestCase.test_apply_button_stale_revision_is_not_retried_
    or_rebased` drives the button against a concurrent change that advances the
    revision after the view's read, so the apply's expected revision is stale
    and the server 409s; the apply is not rebased or retried (revision
    unchanged, no correction stored, live view not re-read), the run is marked
    `remote_process_uncertain` so the next Process/Apply is blocked until a
    Reload, and the operator is told the apply was not retried.
  * *Rerun feedback use reconciled with local* — **DONE (checkpoint 218).** The
    remote per-row Rerun (`rerun_remote_row`) previously called
    `_remote_context_block(ss)` *without* the `view` argument, so it omitted the
    `corrections_block` — the human corrections recorded in earlier batches —
    that the local reference's per-row retry (`retry_segment` ->
    `build_prompt_for([record], current_batch)`, shared with the whole-batch run)
    carries. The fix passes `view` and the current `batch_index` to
    `_remote_context_block(ss, view, batch_index)` (mirroring
    `process_remote_batch` and `process_remote_row`), so a re-run of a batch-N
    row is judged against the same corrections as its batch; the operation stays
    a single segment (`operation="rerun"`, `len(segments) == 1`), and the
    service's `process()` routes `operation="rerun"` to
    `client.translate_one(records[0], system_prompt)`, where `build_prompt`
    honors `context.corrections_block` (checkpoint 219 additionally sends the
    current batch's `batch={"index", "count"}` block, so the stage/framing
    follows the current batch too).
    `RemoteCorrectionsTestCase.test_rerun_carry_corrections_block_from_earlier_
    batch` drives the UI to batch 1, spys the assembled prompt at
    `StubClient.translate_one`, applies a batch-0 correction, advances to batch
    1, and asserts the batch-0 correction (`<u1: "Le chat humain.">`, the
    `rejected:` line, and the operator's comment) appears in the per-row Rerun
    prompt.
  * *Per-row Rerun stage/batch framing* — **CLOSED (checkpoint 219).** The
    per-row Rerun now stages the re-run with the current batch's stage and
    framing, matching the local reference. `rerun_remote_row` sends
    `batch={"index": batch_index, "count": view.batch_count}` (where
    `batch_index = min(ss.get("remote_batch", 0), view.batch_count - 1)`), exactly
    like `process_remote_row` and `process_remote_batch`, so the service computes
    the stage from `stage_for_batch(batch_index, batch_count)` (e.g. batch 1 of 2
    -> "mt3") and the "N of M" framing rather than the single-segment "mt1" / "1
    of 1" default. The operation stays a single segment
    (`operation="rerun"`, `len(segments) == 1`) and the corrections_block is
    still carried via `_remote_context_block(ss, view, batch_index)`, so the
    re-run is judged against the same feedback as its batch. Because the default
    `remote_template` is `""` and the default stage templates carry no
    `{{BATCH_NUMBER}}`/`{{BATCH_COUNT}}` tokens, the framing is observable only
    via a template override; `RemoteCorrectionsTestCase.test_rerun_uses_current_
    batch_stage_framing_in_nonfirst_batch` drives a 3-row doc to batch 1 of 2,
    sets the "Stage template override" to a template carrying
    `{{BATCH_NUMBER}}`/`{{BATCH_COUNT}}`, spys the assembled prompt at
    `StubClient.translate_one`, clicks "Rerun", and asserts the prompt carries
    "batch=2 of 2" (not "batch=1 of 1"). This closes the stage/framing gap and is
    distinct from D.2's rebatch/feedback policy (which governs how the new batch
    map interacts with the corrections block, not the per-row operation's stage);
    D.2 remains OPEN and is NOT moved to browser acceptance.
  * *Analyze as a separate action* — **CLOSED (checkpoint 220).** A new
    processing-service `/analyze` endpoint (`ProcessingService.analyze_
    corrections`, resolving the client via `_resolve_analyze_client` and
    converting the request's dict corrections to `state_mod.Correction` so the
    critique layer can read their attributes) builds a warning-aware critique and
    returns `{"operation":"analyze","engine","corrections_analyzed","critique",
    "elapsed_ms"}`; a new workspace `/workspaces/{document_id}/analysis` route
    (`WorkspaceService.record_analysis`, revision-checked, mirroring
    `apply_corrections`) stores it in a SEPARATE `analysis[str(batch_index)]`
    slot WITHOUT overwriting user_final/llm_suggestion/mt_target/draft;
    `run_remote_analysis` orchestrates it (no draft flush, unlike
    `apply_remote_corrections`) and the "Analyse my corrections" panel renders
    the result. `RemoteCorrectionsTestCase.test_analyze_button_persists_critique_
    without_overwriting_translation` drives Apply -> Analyse through the real
    client seam and asserts the critique lands in the `analysis` slot while the
    operator's translation slots stay untouched. Distinct from D.2's rebatch /
    feedback policy, which stays OPEN.
- **E — confirm USE of settings, not only forwarding** — **CLOSED
  (checkpoint 222).** The three remainders are closed offline (no live model,
  no socket), per `docs/REMOTE_PARITY_STATUS.md` "Stage E remainder evidence -
  USE half":
  * *E.1 — applied in the assembled prompt, not merely forwarded:* the user
    template override and a relevant termbase term reach the prompt the real
    processing service + `StubClient` build (`test_template_override_applies_
    to_the_built_prompt`, `test_relevant_termbase_term_reaches_the_built_prompt_
    and_irrelevant_is_filtered`, both fail-closed: any non-stub engine
    construction is mocked to raise so a wrong preset opens no socket). Do NOT
    re-run the green hard-rule -> built-prompt test as fresh E evidence.
  * *E.2 — a user-reachable termbase data source:* a memoQ-CSV uploader
    (`st.file_uploader`, after the XLIFF uploader, before `if view is None:
    return`) parses state-free via `state.parse_termbase_csv` (shared by local
    `ProjectState.load_termbase_csv` and remote `load_termbase_csv_into_session`;
    **no `ProjectState` on the remote path**, session-only, no server storage,
    no editor), proven by `test_termbase_csv_upload_reaches_the_built_prompt`
    (a relevant term reaches the built prompt), `test_malformed_termbase_csv_
    shows_a_clear_error` (a clear error for a non-loadable file), and
    checkpoint 223's `test_no_term_csv_does_not_clobber_the_active_termbase`
    (a no-term CSV surfaces the error without wiping an already-loaded termbase;
    the minimal `if not parsed: return report` guard closes that clobber). A
    missing control is NOT counted as a browser-acceptance item.
  * *E.3 — "reached the payload" vs "applied by the client" for non-default
    generation settings:* `test_non_default_temperature_and_max_tokens_reach_
    the_payload` (remote UI -> `/process` config payload via the fake client)
    and `test_non_default_temperature_and_max_tokens_reach_the_client_call`
    (the config block through the real service `_client_config_from_block` ->
    `llm.ClientConfig` -> the OpenAI-compatible backend's mocked `create()`
    kwargs, no live model) are separate tests on a supported processing path.
- **G — separate acceptance, not a stand-in for implementation**: first the
  targeted offline suites of the touched modules, then both remote suites. After
  a separate authorization for infrastructure: live HTTP with the stub +
  PostgreSQL at the agreed service versions, a browser scenario on a test
  document, save/restore after restart, export, and a separate memoQ-import
  check (XML check != import check). The live `qwen3:4b` smoke is still OPEN
  (HTTP 502 `engine_failed`, exact cause unknown): no new live calls or network
  diagnostics now; G is NOT declared passed by the offline tests.
- **DO14** only after the application is complete and published to GitHub — not
  in this pass.

Dependencies may force a reordering; record the reason in STATUS and continue.
Do not silently drop a feature. Per-stage reports do not end the pass.
