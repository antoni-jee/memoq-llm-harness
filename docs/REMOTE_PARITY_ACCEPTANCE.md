# Remote Workflow Parity — Acceptance

Companion to `REMOTE_PARITY_PLAN.md` (goal + readiness gate) and
`REMOTE_PARITY_STATUS.md` (live checkpoint). This file is the **acceptance
checklist** for stages A–F plus the G gate. For every sub-item it records the
**preconditions**, the **offline test evidence** (test file + test names), the
**manual steps**, the **expected result**, the **link to the offline evidence**,
and a **separate manual-check status**.

- Checkpoint base: **223** (E closed at 222; E.2 no-term preservation check at
  223). Branch `review/remote-parity-snapshot`, HEAD `bb7ff63`.
- Offline result (the base): all six suites **289 OK, exit 0**
  (`tests.test_processing_service` 41, `tests.test_workspace_service` 62,
  `tests.test_remote_app` 42, `tests.test_remote_edit` 79, `tests.test_remote_
  view` 8, `tests.test_remote_client` 57).
- No live LLM call, no network. The `qwen3:4b` live 502 smoke stays **OPEN**
  (not retried). **G is NOT declared passed by the offline tests.**

## Status legend — two independent fields

Every sub-item carries TWO status fields, kept separate so an offline pass is
never mistaken for a human/live/memoQ pass:

1. **Offline evidence** — the headless AppTest / unit tests that exist now.
   For A–F below this is **Pass** at checkpoint 223 (run command in
   "Offline evidence" per item). `D.2` is **offline-only** by design and is
   **not** a browser item.
2. **Manual check** — the human leg: a browser scenario, a live stub +
   PostgreSQL run, or a real memoQ import. Values: **Not run / Pass / Fail /
   Blocked**. **Every manual check in this file is `Not run`** — the offline
   tests do not start any of them. `D.2` has no manual leg (it is offline-only
   and stays with its offline evidence; it is **not** moved into G as an
   unfinished implementation). `G`'s manual check is **Blocked** pending a
   separate infrastructure authorization.

Evidence tiers (from the plan's readiness criteria): unit / headless AppTest /
live HTTP with stub + PostgreSQL / real browser / memoQ import. **XML parse
alone does not prove memoQ import.**

## Shared preconditions (apply to every item)

- Architecture `Streamlit -> document / workspace / processing -> PostgreSQL`;
  the local mode is the **reference behavior**, not a fallback.
- Remote mode has **no `ProjectState`** (proven by
  `tests.test_remote_app.RemoteAppTestCase.test_remote_mode_has_no_project_
  state`); no server-side termbase storage; no termbase editor; no hidden local
  / stub / live-model fallback.
- Four distinct data kinds must stay distinct: original target (`mt_target`),
  model proposal (`llm_suggestion`), saved human text (`user_final`), held
  draft. Copy/Apply mutates only the draft; only an explicit Save persists.
- Fail-closed engine guard on every built-prompt test: construction of any non-
  stub engine is mocked to raise (`a non-stub engine was constructed; the test
  must stay offline`), the chosen preset and
  `remote_workflow._process_target(ss) == (None, "stub")` are asserted before
  the call, so a wrong preset opens no socket.
- Run command per item: `.venv\Scripts\python.exe -m unittest
  tests.<module>[.<class>.<test>]`. The full six-suite run is in "G — overall
  regression".
- The launcher (`scripts/Start-RemoteLab.ps1` etc.) is verified by the user;
  new-SSH-tunnel-when-none is **not** checked (feature-matrix item 10).

---

## A. Apply → Discard + neighbor-draft preservation

Stage A = close the Copy-proposal checks (Apply → Discard; neighbor-draft
preservation on Apply). Implementation: `apply_proposal(record)` in
`remote_workflow.py`; the Apply button in `remote_ui.py`
`_render_remote_row`.

**Offline evidence: Pass.** `tests/test_remote_edit.py`,
`RemoteEditTestCase` (6 apply tests):
- `test_apply_proposal_creates_unsaved_draft_then_save_persists`
- `test_apply_proposal_preserves_held_comment`
- `test_apply_proposal_noop_when_already_shown`
- `test_apply_proposal_button_absent_without_proposal`
- `test_apply_then_discard_restores_saved_and_keeps_proposal` — Apply → Discard
  restores the saved human text and keeps the proposal (no PATCH; revision
  frozen).
- `test_apply_proposal_keeps_neighbour_draft` — a single-row Apply does not
  clobber an unrelated neighbor's held draft.

**Steps (offline):** the six tests above.
**Steps (manual):** in the browser, open a document, Process a batch, Copy a
proposal on one row, verify the row now holds an unsaved draft; Discard it and
verify the saved text returns and the proposal is still listed; Apply a second
row and verify a held draft on a third (unrelated) row is untouched.
**Expected result:** Apply → Discard restores saved text + keeps the proposal;
neighbor drafts are preserved; the four data kinds stay distinct.
**Offline evidence link:** `docs/REMOTE_PARITY_STATUS.md` "Completed stages +
evidence" (Stage A).
**Manual check: Not run.**

## B. Remote Export

Stage B = UI → saved workspace → document-service export → user receives the
file → re-parse/verify. Implementation: `export_remote_document(mark_confirmed,
only_changed)` in `remote_workflow.py` (~L793) + the "4. Export" sidebar in
`remote_ui.py` (two checkboxes + "Export Valid MQXLIFF" + download of
`last_export`). Carries `user_final` ONLY; skips non-translatable / empty
`user_final` / only-changed-equal-`mt_target`; warns on unsaved drafts without
dropping them; "Nothing to export" if empty; the document service `export` →
`export_content` → writes `EXPORT_DIR/<source_file>` → re-parses →
`ss.last_export`.

**Offline evidence: Pass.** `tests/test_remote_edit.py`,
`RemoteEditTestCase.test_export_carries_saved_human_text_and_preserves_rest`
routes the document side through the **real** service
(`_real_document_client` + `SMALL_XML`): saved `user_final` is written; an
unaccepted proposal + an unsaved draft are NOT; the source
`source_fingerprint()` is unchanged; excluded/empty rows keep the memoQ target;
empty-`user_final` parity vs local `new_text() == ""`. The old
`tests/test_remote_app.py` `RemoteAppTestCase.test_read_only_grid_has_no_
mutation_controls` was updated to allow the export control; the local-
orchestration ban (no `translate_batch`/`analyze_corrections`/`save`) stays in
`test_local_mutation_entry_points_not_invoked`.

**Steps (offline):** `test_export_carries_saved_human_text_and_preserves_rest`.
**Steps (manual):** in the browser, Save a human edit, open "4. Export", choose
the confirmation / "only changed" options, click "Export Valid MQXLIFF",
download the file. Then **separately** import the exported MQXLIFF into **real
memoQ** and confirm the human text lands in the target — this memoQ-import step
is distinct from the offline XML re-parse (XML parse != import).
**Expected result:** the exported file carries the saved `user_final`; source
fingerprint unchanged; excluded/empty rows keep the memoQ target; unsaved
drafts are warned but not dropped; a real memoQ import accepts the file and the
human text appears in the target.
**Offline evidence link:** `docs/REMOTE_PARITY_STATUS.md` "Completed stages +
evidence" (Stage B).
**Manual check: Not run** (incl. the real-memoQ-import leg, kept separate from
the XML re-parse).

## C. Per-row Process / Rerun

Stage C = processing-service + record suggestions; each per-row action updates
the proposal without erasing saved text/comment; Copy + Save survives a
generation. Both are done: per-row Rerun (`rerun_remote_row`, one-segment
`operation="rerun"`) and per-row Process (`process_remote_row`, one-segment
batch, no `operation`); per-row Process is not counted from the Rerun tests.

**Offline evidence: Pass.** `tests/test_remote_edit.py`,
`RemoteEditTestCase`:
- `test_rerun_remote_row_survives_copy_save`
- `test_rerun_remote_row_stale_write_is_not_retried`
- `test_process_remote_row_sends_one_segment_and_keeps_drafts`
- `test_process_remote_row_stale_write_is_not_retried`
- Supporting: `test_process_proposal_only_does_not_accept_human_text`,
  `test_process_proposal_only_keeps_human_text_and_comment`,
  `test_manual_save_after_proposal_only_persists_human_text`,
  `test_apply_proposal_creates_unsaved_draft_then_save_persists`
  (Copy + Save survives a generation).

**Steps (offline):** the four per-row tests + the proposal-only / Copy-Save
support.
**Steps (manual):** in the browser, Process a batch, then on a single row click
the per-row "Process" and the per-row "Rerun"; after each, verify the row's
proposal updates while its saved `user_final` + comment are untouched; Copy a
proposal, Save, then generate again and verify the saved text + comment survive.
**Expected result:** each per-row action updates the proposal with
`prefill: false` and preserves a filled `user_final` + comment; a stale write
is not retried; neighbor drafts are preserved.
**Offline evidence link:** `docs/REMOTE_PARITY_STATUS.md` "Order of remaining
stages" / "Stage C" (per-row Process + Rerun).
**Manual check: Not run.**

## D. Batch / processing settings (D.1–D.4)

Working batch size (D.1), the boundary-change policy (D.2), segments-per-
request (D.3) and UI pagination (D.4) are **distinct**. Pagination and the
others are done; D.2 is **offline-only** (kept with its offline evidence, **not**
moved into G as an unfinished implementation).

### D.1 — working batch size (done, source of truth)
Batch size is stored in the workspace (the server snapshot) and is the single
source of truth for View / Process / Apply. Choosing 10 on 23 segments yields
working batches 10/10/3; Reload preserves the size; held drafts stay bound to
their row keys; the revision change on a size change is checked.

- **Offline evidence: Pass.** `tests/test_remote_edit.py`,
  `RemoteBatchSizeTestCase`: `test_batch_size_view_rebuild_preserves_drafts`,
  `test_chosen_batch_size_splits_into_working_batches_and_survives_reload`,
  `test_apply_processes_the_same_rows_the_ui_shows_for_batch`.
- **Steps (manual):** set batch size to 10 on a 23-segment document; verify
  batches 10/10/3; Reload; verify size and drafts survive.
- **Expected result:** 10/10/3 working batches; Reload preserves size + drafts;
  Apply processes exactly the rows the UI shows for the batch.
- **Offline evidence link:** `docs/REMOTE_PARITY_PLAN.md` "D — working batches…"
  (D.1); `docs/REMOTE_PARITY_STATUS.md` "Stage D.1".
- **Manual check: Not run.**

### D.2 — boundary change (100 → 10 rebatch / feedback) — offline-only
An explicit, revision-checked `set_batch_size` re-slices the document and bumps
`layout_version` on a real size change only (Reload / repeat-bind / same-size do
NOT bump). Corrections keep their original `batch_index`, tagged with the
`layout_version` produced under, so a new layout's feedback never picks up an
earlier layout's correction even when batch indices collide.

- **Offline evidence: Pass.** `tests/test_remote_edit.py`,
  `RemoteSetBatchSizeTestCase` (7 tests): `test_change_batch_size_100_to_10_
  starts_new_layout`, `test_correction_in_old_layout_excluded_from_new_layout_
  prompt`, `test_return_to_original_size_does_not_reactivate_old_correction`,
  `test_stale_revision_does_not_mutate_or_start_layout`,
  `test_drafts_survive_rebatch_by_key`, `test_same_size_is_no_op_and_keeps_
  revision`, `test_apply_in_new_layout_does_not_erase_old_history`.
- **Status:** **offline-only by design — no manual/browser leg, and NOT carried
  into G as an unfinished implementation.** It is closed at checkpoint 221 with
  the evidence above; do not move it to G.
- **Offline evidence link:** `docs/REMOTE_PARITY_PLAN.md` "D.2 done, checkpoint
  221"; `docs/REMOTE_PARITY_STATUS.md` "Stage D.2 DONE (checkpoint 221)" and
  "Stage F evidence" (D.2 closed at checkpoint 221).
- **Manual check: N/A (offline-only).**

### D.3 — Segments per request (done; headless test substitutes state)
Proved separately that (a) changing the **correct control** changes the
`/process` payload (`test_non_default_chunk_size_reaches_multi_row_batch`, via
`session_state["remote_chunk_size_0"] = 10`), and (b) the **real** chunk split
of 23 segments at `chunk_size=10` yields groups 10/10/3 into a MOCKED (locked)
model call (`test_real_split_groups_23_segments_into_10_10_3`, driving
`LLMClient.translate_batch` with `_chat` patched). The control was renamed from
"Context chunks" to **"Segments per request"** to match the local reference.
`StubClient` is NOT changed for this.

- **Offline evidence: Pass.** `tests/test_remote_edit.py`,
  `RemoteChunkSizeTestCase`: `test_non_default_chunk_size_reaches_multi_row_
  batch`, `test_real_split_groups_23_segments_into_10_10_3`.
- **Important gap for the manual leg:** the headless test (a) **substitutes
  state** (`remote_chunk_size_0 = 10`) rather than driving the widget. The
  manual check must therefore **change the "Segments per request" widget itself**
  and **verify `chunk_size` in the request** (`/process` config payload), not
  rely on the substituted state.
- **Steps (manual):** in the browser, set the "Segments per request" widget to
  10 on a multi-row batch and Process; confirm the request carries
  `chunk_size = 10` (the widget, not a back-door state write, drove it).
- **Expected result:** the widget change reaches the request as
  `chunk_size`; 23 segments split 10/10/3.
- **Offline evidence link:** `docs/REMOTE_PARITY_PLAN.md` "D.3 done";
  `docs/REMOTE_PARITY_STATUS.md` "Stage D.3".
- **Manual check: Not run** (must drive the actual widget; the offline test
  substitutes state).

### D.4 — UI pagination (done; page-2/return is a G browser scenario)
Drive the real control transitions WITHOUT a direct `remote_page` /
`remote_page_size` write standing in for a user action. The "Rows shown at
once" picker move IS headlessly driven
(`test_rows_shown_picker_drives_page_size_without_direct_write`):
`set_value(25)` on the selectbox fires `on_change` → `_on_rows_per_page_change`,
persists `remote_page_size=25`, shows the page radio, and renders page 1 (25 of
29 rows) — no direct write. The **page-2 / return-to-page-1** transitions are
NOT drivable headlessly (the "Page" radio re-keys on each page change,
`remote_page_radio_%d_%d`, so AppTest drops the scripted `set_value`); that
browser navigation is recorded as a concrete scenario for **G**. The
application `session_state` and the existing state tests (incl. the page-2 /
return path via an explicit `remote_page` write,
`test_drafts_survive_reruns_and_page_navigation` /
`test_large_fixture_pagination_visible_count_and_draft_survival`) are kept.

- **Offline evidence: Pass.** `tests/test_remote_edit.py`,
  `RemotePaginationTestCase`: `test_rows_shown_picker_drives_page_size_without_
  direct_write` (picker leg), `test_large_fixture_pagination_visible_count_and_
  draft_survival` (page-2/return via an explicit `remote_page` write),
  `test_large_fixture_whole_batch_shows_all_rows`; plus
  `RemoteEditTestCase.test_drafts_survive_reruns_and_page_navigation`.
- **Important for the manual leg:** the manual check must drive **page 1 → 2 →
  1** through the "Page" radio and confirm a **held draft survives** the
  navigation (the headless leg cannot drive the re-keyed radio; the browser can).
- **Steps (manual):** in the browser, set "Rows shown at once", navigate Page 1
  → 2 → 1, hold a draft on a row, and verify it survives each transition.
- **Expected result:** the picker drives page size with no direct write; page 1
  → 2 → 1 navigation preserves the held draft.
- **Offline evidence link:** `docs/REMOTE_PARITY_PLAN.md` "D.4 done";
  `docs/REMOTE_PARITY_STATUS.md` "Stage D.4" (page-2/return recorded for G).
- **Manual check: Not run** (page 1 → 2 → 1 with draft preservation is a G
  browser scenario).

## E. Rules / prompts (E.1–E.3) — closed at checkpoint 222 (E.2 preservation
at 223)

Forwarding + USE are done; the E remainders are closed offline (no live model,
no socket). See `docs/REMOTE_PARITY_STATUS.md` "Stage E remainder evidence -
USE half (E.1/E.2/E.3, done, checkpoint 222)".

### E.1 — applied in the assembled prompt, not merely forwarded
The user template override and a relevant termbase term reach the prompt the
**real** processing service + `StubClient` build (not just the forwarded
context key). Both tests are **fail-closed**.

- **Offline evidence: Pass.** `tests/test_remote_edit.py`,
  `RemoteContextTestCase`: `test_template_override_applies_to_the_built_prompt`,
  `test_relevant_termbase_term_reaches_the_built_prompt_and_irrelevant_is_
  filtered`.
- **Steps (manual):** none specific to E.1 (covered by the live G browser run,
  where the operator confirms the template + relevant term behave in a real
  generation); do NOT re-run the green hard-rule → built-prompt test as fresh E
  evidence.
- **Expected result:** the template override and a relevant term appear in the
  built prompt the engine receives; irrelevant terms are filtered.
- **Offline evidence link:** `docs/REMOTE_PARITY_STATUS.md` "Stage E remainder
  evidence - USE half" (E.1).
- **Manual check: Not run.**

### E.2 — a user-reachable memoQ-CSV termbase source
A `Termbase CSV` uploader (`st.file_uploader`, index 1, after the XLIFF
uploader at index 0, before `if view is None: return`) parses state-free via
`state.parse_termbase_csv` (shared by local `ProjectState.load_termbase_csv`
and remote `remote_workflow.load_termbase_csv_into_session`; **no
`ProjectState` on the remote path**, session-only, no server storage, no
editor). The valid-format example is `MQL term base.csv` at the project root
(7 columns `English,English,English,English,English,Russian,Russian`; source =
column 0, target = column 5).

- **Offline evidence: Pass.** `tests/test_remote_edit.py`,
  `RemoteContextTestCase`:
  - `test_termbase_csv_upload_reaches_the_built_prompt` — a seven-column memoQ
    export uploaded through the uploader parses into `ss.remote_termbase` as a
    `[{source, target}]` list and a relevant term reaches the built prompt, an
    irrelevant one is filtered.
  - `test_malformed_termbase_csv_shows_a_clear_error` — a header-only CSV loads
    no terms and surfaces a clear `st.error` "No termbase terms loaded" instead
    of silently doing nothing (starts from an empty termbase).
  - `test_no_term_csv_does_not_clobber_the_active_termbase` **(checkpoint 223,
    the narrow E.2 preservation check the acceptance step required)** — loads a
    VALID CSV first (terms `cat`/`dashboard` populate `ss.remote_termbase`),
    then a header-only (no usable terms) CSV, and asserts BOTH that the clear
    "No termbase terms loaded" error surfaces AND that the previously active
    termbase is **preserved, not clobbered**. This caught a clobbering bug
    (default `replace=True` set `ss.remote_termbase = parsed`, so a no-term CSV
    wiped an active termbase to empty); the minimal fix is an early
    `if not parsed: return report` in `load_termbase_csv_into_session` (only the
    clobber case; the replace/append branches are unchanged).
- **Steps (manual):** in the browser, upload a valid memoQ CSV (e.g. the shape
  of `MQL term base.csv`) and confirm the relevant term reaches the prompt; then
  upload a CSV with no usable terms and confirm the "No termbase terms loaded"
  error appears AND the previously loaded termbase is still active.
- **Expected result:** valid CSV loads and a relevant term reaches the built
  prompt; a no-term CSV surfaces a clear error **without wiping** the active
  termbase.
- **Offline evidence link:** `docs/REMOTE_PARITY_STATUS.md` "Stage E remainder
  evidence - USE half" (E.2, incl. the checkpoint 223 preservation test).
- **Manual check: Not run.**

### E.3 — non-default generation settings: "reached payload" vs "client call"
For non-default `temperature`/`max_tokens` on a supported processing path,
"reached the payload" and "applied by the client" are separated.

- **Offline evidence: Pass.**
  - (a) `tests/test_remote_edit.py`,
    `RemoteChunkSizeTestCase.test_non_default_temperature_and_max_tokens_reach_
    the_payload` — the remote UI inference-setting widgets' `temperature=0.35`
    and `max_tokens=1234` reach the `/process` config payload (via the fake
    `FakeProcessingClient`; no model call).
  - (b) `tests/test_processing_service.py`,
    `TestPerRequestConfig.test_non_default_temperature_and_max_tokens_reach_
    the_client_call` — the same values reach the OpenAI-compatible backend's
    `LLMClient._chat` `create(**kwargs)` kwargs through the real service
    `_client_config_from_block` → `llm.ClientConfig` (lower SDK call mocked;
    no live model/network). Anthropic omits `temperature` by design, so both
    values are asserted together on the OpenAI backend.
- **Steps (manual):** none specific to E.3 beyond the live G run; the two values
  are confirmed in the request payload and the client call offline.
- **Expected result:** non-default settings reach the payload and the client
  call on a supported path; no live model.
- **Offline evidence link:** `docs/REMOTE_PARITY_STATUS.md` "Stage E remainder
  evidence - USE half" (E.3).
- **Manual check: Not run.**

## F. Corrections loop — all closed at checkpoints 215–221

A separate workflow, not Copy-to-draft; local semantics ported through service
contracts. The first scenario (a human edit → stored `Correction` → survives a
re-read → feeds the next batch's feedback) is done; all finish items are done +
offline-tested.

- **F.0 first scenario — offline evidence: Pass.**
  `tests/test_remote_edit.py`,
  `RemoteCorrectionsTestCase.test_human_edit_becomes_correction_survives_
  reread_and_feeds_next_batch`.
- **F.1 "Apply Changes & Calculate Diffs" as a distinct UI action (checkpoint
  215) — Pass.** `test_apply_button_persists_batch_correction_through_ui`:
  held draft → Apply → a stored correction that survives a re-read and reaches
  the next batch's feedback; a no-op re-Apply neither duplicates nor advances
  the revision.
- **F.2 offline two-batch scenario (checkpoint 216) — Pass.**
  `test_two_batch_scenario_correction_feeds_next_batch_prompt`: 3 rows at batch
  size 2 (batches [k1,k2] and [k3]); batch 0's prompt does NOT carry its own
  correction (a batch never feeds itself) and batch 1's assembled prompt carries
  the batch-0 correction (`<u1: "Le chat humain.">`, the `rejected:` line, the
  operator's comment).
- **F.3 repeated Apply + a stale revision, no auto-retry (checkpoint 217) —
  Pass.** `test_apply_button_stale_revision_is_not_retried_or_rebased`: a
  concurrent change advances the revision after the view's read, so the apply's
  expected revision is stale and the server 409s; the apply is not rebased or
  retried (revision unchanged, no correction stored, live view not re-read), the
  run is marked `remote_process_uncertain` so the next Process/Apply is blocked
  until a Reload. No-duplication no-op re-Apply is covered by F.1's test.
- **F.4 Rerun feedback use reconciled with local (checkpoint 218) — Pass.**
  `test_rerun_carry_corrections_block_from_earlier_batch`: the per-row Rerun at
  batch 1 carries the batch-0 correction (the `<u1: "Le chat humain.">` line,
  the `rejected:` line, the operator's comment) into the per-row prompt.
- **F.5 per-row Rerun stage/batch framing (checkpoint 219) — Pass.**
  `test_rerun_uses_current_batch_stage_framing_in_nonfirst_batch`: a per-row
  Rerun at batch 1 of 2 stages with the current batch's stage/framing
  ("batch=2 of 2", not "batch=1 of 1").
- **F.6 Analyze as a separate action (checkpoint 220) — Pass.**
  `test_analyze_button_persists_critique_without_overwriting_translation`:
  Apply → Analyse through the real client seam; the critique lands in a separate
  `analysis[str(batch_index)]` slot while the operator's translation slots
  (user_final / llm_suggestion / mt_target / draft) stay untouched. (The
  workspace `record_analysis` route pre-existed; the `/analyze` endpoint closed
  the gap.)
- **F.7 D.2 rebatch/feedback policy (checkpoint 221) — offline-only.** See
  D.2 above: closed offline, **not** a browser item, **not** moved to G.
- **Steps (manual, for G):** in the browser, drive Apply → (next batch's
  feedback), a two-batch scenario, a stale-revision 409 (verify no auto-retry),
  and an Analyze that returns a critique without overwriting the translation.
- **Expected result:** corrections persist and feed the next batch; a batch never
  feeds itself; a stale revision is not retried; Analyze returns a critique
  without touching the translation.
- **Offline evidence link:** `docs/REMOTE_PARITY_STATUS.md` "Stage F evidence"
  (checkpoints 215–221); `docs/REMOTE_PARITY_PLAN.md` "F — finish the
  corrections loop".
- **Manual check: Not run.**

## G — overall regression + live acceptance (NOT declared passed offline)

First the targeted offline suites of the touched modules, then both remote
suites; after a **separate infrastructure authorization**, live HTTP with the
stub + PostgreSQL at the agreed service versions, a browser scenario on a test
document, save/restore after restart, export, and a **separate memoQ-import
check** (XML parse != import); a limited real-model test only after approval.

- **Offline evidence: Pass.** All six suites, 289 OK, exit 0:
  `.venv\Scripts\python.exe -m unittest tests.test_processing_service
  tests.test_workspace_service tests.test_remote_app tests.test_remote_edit
  tests.test_remote_view tests.test_remote_client`.
- **Manual check: Blocked.** Requires a separate authorization for
  infrastructure. The `qwen3:4b` live 502 smoke (`engine_failed`, exact cause
  unknown) is still **OPEN** — no new live calls or network diagnostics now. G
  is **NOT** declared passed by the offline tests. The browser scenarios that
  the offline tests cannot drive headlessly are gathered here from the sub-items:
  D.3 (drive the "Segments per request" widget + check `chunk_size` in the
  request), D.4 (page 1 → 2 → 1 with draft preservation), and B's real memoQ
  import of the exported MQXLIFF.
- **Offline evidence link:** `docs/REMOTE_PARITY_STATUS.md` "Last test commands +
  results" (checkpoint 223 entry) and the per-suite counts;
  `docs/REMOTE_PARITY_PLAN.md` "G — separate acceptance".

---

## Manual-run order (short)

This is the suggested order for the human leg. **All are Not run.** D.2 stays
offline-only and is **not** in this order. G is **Blocked** until a separate
infrastructure authorization.

1. **A — Apply → Discard + neighbor-draft preservation.** Open → Process → Copy
   one row → Discard (saved text returns, proposal kept) → Apply a second row →
   a neighbor's held draft is untouched.
2. **C — Per-row Process / Rerun.** Per-row Process then per-row Rerun; saved
   `user_final` + comment survive; a stale write is not retried.
3. **D.1 — working batch size.** Batch size 10 on 23 segments → batches
   10/10/3; Reload preserves size + drafts.
4. **D.3 — "Segments per request" widget (drive the widget, not state).** Set the
   "Segments per request" widget to 10 and Process a multi-row batch; **verify
   `chunk_size = 10` in the `/process` request** (the offline test substitutes
   `remote_chunk_size_0 = 10`, so the widget itself must be driven here).
5. **D.4 — pagination, page 1 → 2 → 1 with draft preservation.** Set "Rows shown
   at once", navigate Page 1 → 2 → 1, hold a draft on a row, verify it survives
   each transition (the re-keyed "Page" radio cannot be driven headlessly).
6. **E.2 — memoQ-CSV termbase import (valid + no-term).** Upload a valid memoQ
   CSV (shape of `MQL term base.csv`), confirm a relevant term reaches the
   prompt; then upload a no-term CSV, confirm the "No termbase terms loaded"
   error AND that the previously loaded termbase is preserved.
7. **F — corrections loop.** Apply → next-batch feedback; two-batch scenario;
   stale-revision 409 (no auto-retry); Analyze returns a critique without
   overwriting the translation.
8. **B — Export + real memoQ import.** Save a human edit → "4. Export" → download
   the MQXLIFF, then **separately** import it into **real memoQ** and confirm
   the human text lands in the target (distinct from the offline XML re-parse;
   XML parse != import).
9. **G — overall regression + live acceptance.** Blocked: after a separate
   infrastructure authorization, run the live stub + PostgreSQL, the browser
   scenario on a test document, save/restore after restart, and the separate
   memoQ-import check. The `qwen3:4b` live 502 smoke stays OPEN.

### Quick status table

| Item | Offline evidence | Manual check |
|------|------------------|--------------|
| A. Apply → Discard + neighbor-draft | Pass | Not run |
| B. Remote Export (+ real memoQ import) | Pass | Not run |
| C. Per-row Process / Rerun | Pass | Not run |
| D.1. Working batch size | Pass | Not run |
| D.2. Boundary change (100→10) | Pass | N/A (offline-only) |
| D.3. Segments per request | Pass | Not run (drive the widget; check `chunk_size`) |
| D.4. Pagination (page 1→2→1) | Pass | Not run (G browser scenario) |
| E.1. Applied in built prompt | Pass | Not run |
| E.2. memoQ-CSV termbase (+ preservation) | Pass | Not run |
| E.3. Non-default settings payload/client | Pass | Not run |
| F. Corrections loop (215–220) | Pass | Not run |
| G. Overall + live acceptance | Pass | Blocked |

No live LLM call, no deploy, no restart in producing this file. No
commit/push/reset. The `qwen3:4b` live 502 smoke remains OPEN and is not
retried.
