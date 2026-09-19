# memoQ LLM Harness

A local Streamlit tool for hybrid translation / post-editing of memoQ bilingual
XLIFF (`*.mqxliff`) against **any** OpenAI-compatible endpoint — the OpenAI API,
OpenRouter, DeepSeek, or a local Ollama / llama.cpp / LM Studio server.

Its point of difference is the **feedback loop**: every correction you make by
hand becomes an in-context learning example for the next batch, so the model
stops repeating phrasing you have already rejected.

---

## Quick start

```powershell
pip install -r requirements.txt
streamlit run app.py
```

Then in the browser:

1. **Engine** — pick a preset in the sidebar. `Local Engine (Ollama)` is the
   default. Hit **Test** to confirm the endpoint answers, **List models** to
   pull the model names it serves.

   > **Tunnelled Ollama:** the preset reads `OLLAMA_HOST` rather than assuming
   > localhost. On the Parallels VM this repo was built on, `OLLAMA_HOST` is
   > `10.211.55.2:11434` (the macOS host), so the preset resolves to
   > `http://10.211.55.2:11434/v1` automatically — nothing listens on the VM's
   > own `localhost:11434`. If you move the tunnel, set `OLLAMA_HOST` and
   > restart, or just type the URL into the sidebar.
2. **Document** — upload your `.mqxliff`, or paste a path and click **Load**.
   Confirmed and locked rows are filtered out automatically.
3. **Left panel** — three separate things, in increasing order of authority:
   **Hard Rules** (the editorial rulebook, free prose), **Vibe** (this
   article's stylistic direction), and **Termbase** (the memoQ glossary CSV).
   The first two ship with defaults and are editable in place; **Restore** puts
   a block back to its default.
4. **Process Current Batch** — sends the batch to the model; suggestions land in
   the editable column.
5. Edit anything that is wrong, and add a short note saying *why* where it helps.
6. **Apply Changes & Calculate Diffs** (or just click **Next**) — your edits
   become correction records that are injected into the next batch's prompt.
7. **Export Valid MQXLIFF** — writes to `exports/` and re-parses the result to
   prove it is well-formed before telling you it succeeded.

---

## Layout

```
app.py                      Streamlit UI (presentation only)
memoq_harness/
    xmlcompat.py            XML backend shim: lxml preferred, ElementTree fallback
    tags.py                 inline memoQ tag isolation / restoration
    mqxliff.py              document parsing, segment filtering, re-export
    state.py                state.json, batching, the correction feedback loop
    prompts.py              system / user prompt assembly, stage selection
    llm.py                  endpoint presets; OpenAI *and* Anthropic SDKs
    diffing.py              word-level MT vs. suggestion diff
    rtf.py                  RTF -> text reader for the operator's sources
    defaults.py             GENERATED: Hard Rules and Vibe text blocks
    prompt_templates.py     GENERATED: Init / MT1 / MT2 / MT3 templates
tools/build_defaults.py     regenerates the two GENERATED modules from .rtf
config/termbase.csv         sample memoQ termbase, auto-loaded at first start
exports/                    timestamped exports (created on first export)
state.json                  project state (created on first load)
tests/                      200 tests
```

Run the tests with:

```powershell
python run_tests.py           # everything
python tests\test_core.py     # 153 tests: parsing, tags, state, termbase, prompts
python tests\test_app.py      # 43 tests: the real app.py, driven headlessly
python tests\test_live.py     # 4 tests: a real endpoint; skips if unreachable
```

Only `test_live.py` touches the network, and it skips itself when no endpoint
answers. Point it elsewhere with `HARNESS_LIVE_URL`, `HARNESS_LIVE_MODEL` and
`HARNESS_LIVE_KEY`.

---

## How the pieces work

### Segment filtering

The harness loads a row only when it is **neither confirmed nor locked**.

memoQ is not consistent about how it records this — depending on version you may
get `mq:confirmed="true"`, `mq:locked="true"`, `mq:status="Confirmed"`, or the
XLIFF-standard `<target state="final">`. All four are consulted, and each signal
can be switched off independently under **Segment filter** in the sidebar if your
export disagrees. The sidebar always reports how many rows were skipped and why,
so a mis-detection is visible immediately rather than silently costing you rows.

### Endpoints

Five presets. Four speak the OpenAI chat-completions protocol (OpenAI,
OpenRouter, DeepSeek, Ollama); **Anthropic API** uses the native `anthropic`
SDK instead, because the Messages API differs structurally: the system prompt
is a top-level field rather than a message, `max_tokens` is mandatory with no
server default, and there is no `response_format`. JSON is obtained there by
prefilling the assistant turn with `{`, which constrains the model far more
reliably than asking, and the prefill is stitched back on before parsing.

The Anthropic preset defaults to `claude-3-5-sonnet-latest` as specified.
Newer and more capable models — `claude-opus-5`, `claude-sonnet-5` — are listed
first in the model dropdown and can be selected without editing any code.

Two Anthropic-specific quirks are handled automatically:

* **No `temperature`.** `messages.create()` has no such parameter in the
  installed SDK; passing it raises `TypeError` before the request is sent. The
  sidebar slider still governs every other preset. A test introspects the real
  SDK signature so future parameter drift fails the suite rather than crashing
  at runtime.
* **Prefill bypass.** Newer Claude builds (`sonnet-5`, `opus-5`, `4-5`, `4-6`,
  `claude-5`) reject a prefilled assistant turn with a 400. For those models
  the prefill is dropped and an explicit "raw JSON only" constraint is appended
  to the system prompt instead. Older models keep the prefill, which constrains
  the format far more tightly than wording does.

### The analysis is bidirectional

"Analyse my corrections" does not assume your edits are correct. The analyser
receives the rulebook and each row's expected placeholder set, and runs a
validation pass *before* extracting style: it looks for dropped or duplicated
tags, typos in technical terms, and violations of your own hard rules. Anything
it finds is reported under a **⚠️ OPERATOR CORRECTION WARNINGS** block rendered
separately from the brief, and flagged rows are excluded from the style
extraction so a slip never hardens into a rule.

### Inline tags

memoQ inline tags (`<bpt>`, `<ept>`, `<ph>`, `<it>`, `<x/>`, `<g>`, `<mrk>`) are
replaced with numbered placeholders — `{1}`, `{2}` — before the text reaches the
model, and restored to the exact original XML afterwards.

Details that matter:

* **The target owns the tags, not the source.** memoQ's MT layer localises the
  *insides* of inline tags — an `<a href="/en/articles/21742">` in the source
  routinely arrives as `/ru/articles/21742` in the pre-translated target.
  Numbering from the source and restoring source XML silently reverted every
  one of those localised links. The target is canonical by default; the source
  maps onto its numbers by exact XML where unchanged and positionally where the
  MT rewrote attributes. Rows whose target carries no tags fall back to the
  source automatically, and the sidebar can force source-canonical behaviour.
* **Source and target share one numbering.** Numbering each side independently
  would mean `{1}` in the source and `{1}` in the target could be different
  tags, and a model told to "keep the tags" would silently swap them.
* **The raw XML is inspectable, both sides.** Every grid row with tags has a
  `{ }` popover with two tabs — *Tags from Source* and *Tags from Current
  Target* — showing exactly what hides behind each placeholder.
* **Every distinct tag has exactly one global number.** Where the two sides
  carry the same tag it shares a number and appears in both tabs; where the MT
  localised it, each variant gets its own number and **both stay injectable**.
  Previously the source's version of a localised tag was discarded outright
  and could not be recovered.
* **Tag buttons under each row** insert a placeholder into the editing box.
  Streamlit exposes no caret position for a `text_area` — there is no API that
  reports where the cursor is, and a component iframe cannot write into another
  widget's state — so insertion appends at the end, which is where a reattached
  tag belongs anyway.
* **Literal `{1}` in the text is handled.** If a segment's own text already
  contains something that looks like a placeholder, that segment switches to an
  alternate scheme so restoration cannot mis-target.
* **Tag damage is reported, not propagated.** If the model drops, invents or
  duplicates a placeholder, the row is flagged in red in the grid and the export
  still produces a file memoQ can open (missing tags are reattached at the end).

### Hard Rules vs. Termbase

These are two different things and the distinction matters:

**Hard Rules** is the editorial rulebook — free prose, edited in the left panel,
injected into the system prompt **verbatim and in full**. It is never
reformatted, filtered or summarised. It ships with a default in
`memoq_harness/defaults.py`; **Restore** resets to it.

**Termbase** is the memoQ glossary CSV — a table of term pairs, filtered per
batch so only the terms that actually occur are spent on tokens. A 1000-entry
termbase would otherwise cost more than the segments themselves and bury the
handful of terms that apply.

Prompt authority runs: vibe → hard rules → termbase → human corrections, with
each later layer told explicitly that it wins — **except** that the termbase is
framed as general terminological support rather than article law. A general
termbase will contain false friends for any specific article (the real export
maps `filling type` → «тип заливки», the paint sense, while the MQL5 article
needs «тип исполнения ордера»), so the article's own rules override it.

### The memoQ termbase CSV

memoQ's export is **headless and positional**. Row 0 reads
`English,English,English,English,English,Russian,Russian` — five source synonym
columns, then two target ones — so there is nothing to match column names
against. The loader takes **column 0** as the source term and **column 5** as
the target, skipping row 0 entirely.

Robustness is deliberate, because a 1000-row termbase that silently drops rows
is worse than useless:

* encoding is sniffed — UTF-8 with or without BOM, UTF-16 LE/BE, cp1251;
* the delimiter is sniffed from the header row (`,` `;` or tab);
* parsing goes through Python's `csv` module, so quoted fields containing
  commas *or embedded line breaks* stay intact — splitting on newlines by hand
  is the usual way these files get corrupted;
* CR, LF and CRLF are all accepted; whitespace is trimmed; duplicate sources
  collapse.

Every import returns a report (`34 terms loaded; 2 rows missing a source or
target; utf-8-sig, delimiter ','`) shown in the UI, so anything skipped is
visible rather than silently lost. Column positions are overridable via
`load_termbase_csv(source_col=..., target_col=...)`.

Term matching is deliberately conservative, because a real 1250-entry export is
full of traps:

* **whole-word** — a substring test fires `side` on "con**side**r";
* **case-aware for capitalised entries** — `To` → «Кому» and `File` → «Файл»
  are MetaTrader UI labels. Matching them case-insensitively made every batch
  instruct the model to mistranslate the ordinary words "to" and "file".
  Entries containing an uppercase letter now match case-sensitively;
  all-lowercase entries stay case-insensitive so they still match at the start
  of a sentence;
* **most specific wins** — where matches nest, the longer term is kept
  (`magic number` over `Magic`, `order filling type` over `filling type`).

On the real export, two ordinary MQL5 sentences pull in 5 relevant terms
(~28 tokens) rather than the whole 1250 (~10 500 tokens).

### The feedback loop

When you leave a batch — via **Next** or **Apply Changes & Calculate Diffs** —
every row whose final text differs from what the model proposed becomes a
`Correction`. These are rendered into the next batch's system prompt as:

```
<12: "Bericht als PDF exportieren">
    source:   "Export the report to PDF."
    rejected: "Exportieren Sie den Bericht nach PDF."
    note:     "imperative, not Sie-form"
```

The bare `<id: "text">` form is what the spec asks for; the indented context is
added because a before/after pair teaches the pattern far better than the final
string alone. Toggle it off under **Feedback block sent to the model** in the
left panel if you want to save tokens.

Rows you left untouched carry no signal and are not injected. Corrections are
capped at the 40 most recent, newest-first, because prompt budget is finite.

The prompt is layered in increasing order of authority — base role, then vibe,
then glossary, then your corrections — and the model is told explicitly that
later layers win. An edit you made in batch 3 therefore overrides a style note
you wrote in batch 1 without you having to restate anything.

### Prompt templates (the RTF prompt engine)

The operator's four `MT *.rtf` files are imported into named stages that encode
a conversation rather than four unrelated prompts:

    Init  ->  MT1  ->  MT2 (repeated)  ->  MT3

`Init` establishes role, termbase and rulebook; `MT1` opens an article; `MT2` is
the workhorse that carries the previous batch's human corrections back; `MT3`
closes the run. **Stage for the next batch** defaults to *Auto* (MT1 first,
MT3 last, MT2 between) and can be pinned. Every template is editable in the
left panel and persists to `state.json`; **Restore** reverts one to its RTF
original.

A template replaces the built-in role description entirely. These tokens are
substituted at send time:

`{{HARD_RULES}}` `{{VIBE}}` `{{TERMBASE}}` `{{CORRECTIONS}}` `{{SOURCE_LANG}}`
`{{TARGET_LANG}}` `{{BATCH_NUMBER}}` `{{BATCH_COUNT}}` `{{SEGMENT_COUNT}}`

Anything else — including the operator's own bracketed notes like
`[НОМЕР СТАТЬИ]` — is passed through untouched. Material placed by a token is
not emitted a second time further down, so writing `{{HARD_RULES}}` into a
template puts the rulebook exactly where you want it rather than twice.

### Per-row retry

Each grid row has a **Retry** button. It isolates that one segment, rebuilds
the prompt with the same rules, termbase slice, corrections and operator
directive the batch used, runs a single-row inference pass, and patches only
that row's suggestion and diff. The other rows keep the output you have already
reviewed. Useful when one segment comes back empty, times out, or trips a
content filter.

### Global batch directive

Above the grid, **Global Batch Directive / Operator Comment** is an optional
instruction that applies to the whole batch. It is injected into the prompt as
a distinct layer ranked above the style notes and below the per-segment
corrections, and it persists to `state.json`.

### Raw response inspector

**Raw LLM Response Stream Inspector** at the bottom of the workspace shows the
complete un-parsed text the model returned for the current batch (or the last
retry), with a download button. When a batch parses badly, this is where you
look first.

### Model-proposed rules

The output contract invites the model to add optional `proposed_rules` and
`proposed_vibe` keys when a batch reveals something durable. With **Auto-merge
rules the model proposes** enabled (default), these are appended to the live
blocks so the next batch inherits them.

Because this mutates the document's governing rules with no human in the loop,
it is deliberately constrained: new rules land under a marked heading rather
than being woven into your prose, duplicates and anything already present are
dropped, every change is recorded in an audit log with batch number and
timestamp, and **Revert automatic rules** removes the whole machine-authored
block in one click.

### Text normalization: none

There is deliberately **no Python-side normalization** of quotes, dashes or
`ё`. The model handles all punctuation and styling through the rules in the
prompt, and the harness never rewrites its output. The only text transformation
anywhere is XML entity handling, which is required for well-formed output.

### Pre-editing diff

Under each processed row, a word-level diff shows what the model changed
relative to memoQ's MT, before you start editing. Word granularity, not
character: character diffs on inflected Russian turn every word ending into
noise. Placeholders are single tokens, so a moved tag reads as one move.
Deletions are struck through and insertions underlined, so the information
does not depend on colour alone. Toggle with **Show diffs**.

`difflib` is quadratic in the worst case, so pairs above
`diffing.MAX_DIFF_TOKENS` fall back to a coarse whole-segment diff — a frozen
UI would be a far worse outcome than a rough diff.

### Layout

**Collapse Rules Panel** hides the left column so the grid takes the full
width for focused editing; the button flips to **Show Rules Panel**.

### Batches vs. pages

The **batch** (default 100) is the unit of processing and of the feedback cycle.
**Rows shown at once** (default 25) is display paging only. They are separate
because Streamlit re-runs the whole script on every widget interaction, and
rendering 200 live text areas at once makes the grid sluggish — but the batch
should still be large enough to be worth a round trip. Set rows to
*Whole batch* if your machine is happy with it.

---

## Notes and limits

* **API keys are never written to `state.json`.** They live in the browser
  session only. `state.json` is a project file people copy around and paste into
  bug reports; a key in it would leak. There is a test asserting this.
* **Export is non-destructive.** Only `<target>` elements for rows the harness
  produced text for are rewritten. Skeleton, comments, memoQ metadata, and every
  confirmed or locked row pass through untouched. By default, rows nobody worked
  on are not rewritten at all.
* **Exports are verified.** After writing, the file is re-parsed; if it is not
  well-formed you get an error instead of a false success.
* **Local models need small chunks.** A 100-segment batch is split into requests
  of `Segments per request` (default 20). One oversized request risks the model
  truncating its JSON halfway and costing the whole batch. Drop it to 5–10 for
  smaller local models.
* **JSON mode is probed, not assumed.** Many local servers reject OpenAI's
  `response_format={"type":"json_object"}`. The client detects this once and
  falls back to plain prompting; the response parser tolerates fenced, prefixed
  and truncated JSON.
* **`lxml` is optional.** It is preferred for round-tripping, but the app falls
  back to the stdlib `xml.etree.ElementTree` if no wheel is available — relevant
  on Windows-on-ARM. The sidebar shows which backend is live.

### Verification status

The whole stack was exercised end to end against the live Ollama on the macOS
host (`qwen3:4b-instruct` via `http://10.211.55.2:11434/v1`): load → filter
(7 of 12 rows) → real translation → human correction → diff → export → re-parse,
with the confirmed row provably untouched and the human edit landing in the
file. The feedback loop was checked behaviourally too — given a correction
teaching the short infinitive form, the model returned
`Bericht als PDF exportieren` instead of the `Exportieren Sie ...` phrasing it
had been shown as rejected.

The later feature set was verified the same way end to end against the live
Ollama: the Anthropic preset switching backends, the four prompt stages, tag
re-targeting on a segment with a localised `/en/` → `/ru/` link (the exported
target kept `/ru/`, the source kept `/en/`, and no namespace noise leaked into
the tags), the diff layer, the rules TXT export and the collapsible panel.

Untested: **no live call has been made against the Anthropic API**, nor against
OpenAI, OpenRouter or DeepSeek — only Ollama. The Anthropic request shaping
(system hoisting, JSON prefill, mandatory `max_tokens`) is covered by unit
tests with a stubbed client, not by a real request. Retry and rate-limit
branches are likewise unit-tested, not exercised with real 429s.

### Regenerating the text constants

`defaults.py` and `prompt_templates.py` are **generated**, not hand-edited.
They are baked from the operator's RTF sources so the app has no runtime
dependency on those files:

| Source | Becomes |
| --- | --- |
| `rules.rtf` | `defaults.DEFAULT_HARD_RULES` |
| `vibe.rtf` | `defaults.DEFAULT_VIBE` |
| `MT 0 init.rtf` | `prompt_templates.INIT_TEMPLATE` |
| `MT 1 start.rtf` | `prompt_templates.MT1_TEMPLATE` |
| `MT 2 intermediate.rtf` | `prompt_templates.MT2_TEMPLATE` |
| `MT 3 final.rtf` | `prompt_templates.MT3_TEMPLATE` |

Edit the `.rtf`, then:

```powershell
python tools\build_defaults.py
```

Tests assert the generated constants still match their sources, so a forgotten
regeneration fails the suite rather than drifting silently.

`memoq_harness/rtf.py` implements just enough RTF to read these: control words
and groups, `\'hh` byte escapes via the declared codepage, `\uN` escapes with
the `\ucN` skip count, and skipped metadata destinations. It handles the macOS
TextEdit convention where a paragraph break is `\` + newline rather than `\par`.

### Unicode note for contributors

`memoq_harness/defaults.py`, `prompt_templates.py` and `config/termbase.csv`
are deliberately non-ASCII (Cyrillic). **Never bulk-rewrite them with Windows
PowerShell `Get-Content`/`Set-Content`** — those default to the system ANSI
codepage and will double-encode every Cyrillic character. Edit them with a
UTF-8–aware tool.

Elsewhere, keep source ASCII and write non-ASCII as `\uXXXX` escapes.

`state.json` is written with `ensure_ascii=False`, so Cyrillic is stored as real
UTF-8 rather than `Р` escapes; there is a test covering the round trip.
