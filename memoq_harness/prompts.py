"""System/user prompt construction for the translation and analysis calls.

The system prompt is assembled from four layers, in increasing order of
authority: the base task description, the soft "vibe", the hard glossary rules,
and finally the human corrections harvested from previous batches.  The
ordering is deliberate - the model is told explicitly that a later layer wins,
so a correction the operator made by hand in batch 3 overrides a stylistic
instruction they wrote in batch 1 without them having to restate anything.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .state import Correction, SegmentRecord, TermEntry


def render_termbase(termbase: Sequence[TermEntry]) -> str:
    """Termbase entries as the plain lines a template token expands to."""
    return "\n".join("- %s" % entry.render() for entry in termbase)

BASE_ROLE = """You are a professional translator and post-editor working inside a \
memoQ-based CAT workflow. You revise machine-translated segments so they read as \
if written by a native professional linguist.

You are given segments from a single document. For each segment you receive the \
source text and the existing machine translation (memoQ AGT MT). Produce the best \
final target text for each one."""

TAG_RULES = """INLINE TAG RULES (critical - violations corrupt the file):
- Placeholders like {1}, {2} stand for memoQ inline formatting tags.
- Reproduce every placeholder from the SOURCE exactly once in your target.
- Never renumber, invent, merge or drop a placeholder.
- Move them to wherever the target language's word order requires.
- Never translate or alter the text inside a placeholder; it is not words."""

OUTPUT_CONTRACT = """OUTPUT FORMAT:
Return a single JSON object, nothing else. No prose, no markdown fence.
Shape: {"segments": [{"id": "<segment id exactly as given>", "target": "<your final target text>"}]}
Include every segment you were given, once each, in the order given.
If a segment needs no change, still return it with the text you consider final.

OPTIONAL - PROPOSING RULES:
If this batch reveals a genuinely new editorial rule or a shift in the article's
style that the next batch should inherit, you may add either of these top-level
keys to the same JSON object:
  "proposed_rules": ["one short, concrete, testable rule per string"]
  "proposed_vibe":  "a short note refining this article's stylistic direction"
Use them sparingly and only for something durable and repeatable. Do not restate
a rule you were already given, do not propose anything you are unsure of, and
never use these keys to comment on an individual segment."""


#: Tokens the operator may place in a stage template. Anything the harness does
#: not recognise is left exactly as typed, so their own bracketed notes such as
#: ``[НОМЕР СТАТЬИ]`` survive untouched for them to fill in by hand.
TEMPLATE_PLACEHOLDERS = (
    "{{HARD_RULES}}",
    "{{VIBE}}",
    "{{TERMBASE}}",
    "{{CORRECTIONS}}",
    "{{SOURCE_LANG}}",
    "{{TARGET_LANG}}",
    "{{BATCH_NUMBER}}",
    "{{BATCH_COUNT}}",
    "{{SEGMENT_COUNT}}",
)


def stage_for_batch(batch_index: int, batch_count: int) -> str:
    """Which prompt stage a batch belongs to.

    The templates encode a conversation: ``Init`` sets the model up, ``MT1``
    opens the article, ``MT2`` is the workhorse that carries corrections back,
    ``MT3`` closes the run. Batch 0 is the opening exchange, the last batch is
    the closing one, and everything between is MT2.
    """
    if batch_index <= 0:
        return "mt1"
    if batch_count > 1 and batch_index >= batch_count - 1:
        return "mt3"
    return "mt2"


def render_template(template: str, values: Dict[str, str]) -> Tuple[str, Set[str]]:
    """Substitute known ``{{TOKEN}}`` placeholders in a stage template.

    Returns the rendered text plus the set of tokens that were actually
    present, so the caller can avoid emitting the same material twice.
    """
    used: Set[str] = set()
    out = template
    for token in TEMPLATE_PLACEHOLDERS:
        if token in out:
            used.add(token)
            out = out.replace(token, values.get(token, "").strip())
    return out, used


def build_system_prompt(
    hard_rules_text: str,
    vibe: str,
    corrections_block: str,
    termbase: Sequence[TermEntry] = (),
    source_lang: str = "",
    target_lang: str = "",
    extra_instructions: str = "",
    template: str = "",
    template_values: Optional[Dict[str, str]] = None,
) -> str:
    """Assemble the full system prompt for a translation batch.

    ``hard_rules_text`` is the operator's editorial rulebook, injected verbatim
    - it is prose, not a table, and is deliberately not reformatted. ``termbase``
    is the separate memoQ glossary, already filtered to the terms occurring in
    this batch.

    ``template`` is the operator's own stage text (Init/MT1/MT2/MT3). When
    given it replaces the built-in role description, and any ``{{TOKEN}}`` it
    contains is filled in. Material a token already placed is not emitted a
    second time further down, so an operator who writes ``{{HARD_RULES}}`` into
    their template gets the rulebook exactly where they put it rather than
    twice.
    """
    used_tokens: Set[str] = set()
    if template.strip():
        rendered, used_tokens = render_template(template, template_values or {})
        parts: List[str] = [rendered.strip()]
    else:
        parts = [BASE_ROLE]

    if source_lang or target_lang:
        parts.append(
            "LANGUAGE PAIR: %s -> %s"
            % (source_lang or "(source)", target_lang or "(target)")
        )

    if vibe.strip() and "{{VIBE}}" not in used_tokens:
        parts.append(
            "STYLE / VIBE (soft rules for this article - apply throughout):\n%s"
            % vibe.strip()
        )

    if hard_rules_text.strip() and "{{HARD_RULES}}" not in used_tokens:
        parts.append(
            "HARD EDITORIAL RULES (non-negotiable - these override the style "
            "notes above):\n%s" % hard_rules_text.strip()
        )

    if termbase and "{{TERMBASE}}" not in used_tokens:
        rendered = "\n".join("- %s" % entry.render() for entry in termbase)
        parts.append(
            "TERMBASE ENTRIES RELEVANT TO THIS BATCH:\n"
            "%s\n"
            "Use the given target term whenever the source term occurs, adjusted "
            "only for grammatical inflection required by the target language.\n"
            "This termbase is general terminological support, not article law: "
            "where the style notes or editorial rules above give a different "
            "rendering for this article, follow those instead. A termbase entry "
            "that is plainly the wrong sense in context is a false friend - "
            "translate by meaning and note it." % rendered
        )

    if corrections_block.strip() and "{{CORRECTIONS}}" not in used_tokens:
        parts.append(
            "HUMAN CORRECTIONS FROM EARLIER BATCHES OF THIS DOCUMENT.\n"
            "A reviewer edited your previous output into the text shown. These are "
            "the highest authority in this prompt: they override the style notes "
            "and demonstrate exactly what this reviewer wants. Infer the pattern "
            "behind each edit and apply it to every comparable segment from now "
            "on - do not merely copy these strings.\n\n%s" % corrections_block.strip()
        )

    if extra_instructions.strip():
        parts.append(
            "OPERATOR DIRECTIVE FOR THIS BATCH.\n"
            "The reviewer wrote this about the batch you are about to handle. "
            "It applies to every segment in it and outranks the style notes; "
            "only the per-segment corrections above take precedence.\n\n%s"
            % extra_instructions.strip()
        )

    parts.append(TAG_RULES)
    parts.append(OUTPUT_CONTRACT)
    return "\n\n".join(parts)


def build_batch_prompt(
    *,
    hard_rules_text: str,
    vibe: str,
    termbase: Sequence[TermEntry],
    corrections_block: str,
    source_lang: str,
    target_lang: str,
    directive: str,
    template: str,
    batch_index: int,
    batch_count: int,
    segment_count: int,
) -> str:
    """System prompt for one batch, or a single-row rerun within it.

    The pure part of the app's prompt assembly, shared by the Streamlit app
    and processing-service: the caller resolves the stage template, filters
    the termbase to the relevant terms and supplies the corrections block;
    this fills the ``{{...}}`` template values and builds the prompt.
    """
    return build_system_prompt(
        hard_rules_text=hard_rules_text,
        termbase=termbase,
        vibe=vibe,
        corrections_block=corrections_block,
        source_lang=source_lang,
        target_lang=target_lang,
        extra_instructions=directive,
        template=template,
        template_values={
            "{{HARD_RULES}}": hard_rules_text,
            "{{VIBE}}": vibe,
            "{{TERMBASE}}": render_termbase(termbase),
            "{{CORRECTIONS}}": corrections_block,
            "{{SOURCE_LANG}}": source_lang,
            "{{TARGET_LANG}}": target_lang,
            "{{BATCH_NUMBER}}": str(batch_index + 1),
            "{{BATCH_COUNT}}": str(batch_count),
            "{{SEGMENT_COUNT}}": str(segment_count),
        },
    )


def build_user_payload(records: Sequence[SegmentRecord]) -> str:
    """Serialise a chunk of segments as the user message."""
    payload = {
        "segments": [
            {
                "id": record.key,
                "source": record.source_text,
                "current_mt": record.mt_target,
                **({"reviewer_note": record.user_comment} if record.user_comment.strip() else {}),
            }
            for record in records
        ]
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


OPERATOR_WARNING_HEADING = "⚠️ OPERATOR CORRECTION WARNINGS"

ANALYSIS_SYSTEM = """You analyse a translation reviewer's edits to infer their \
preferences. You are a critical second pair of eyes, not a rubber stamp.

You receive pairs of (machine suggestion, reviewer's final text) from a single \
document, plus any notes the reviewer wrote, the inline-tag placeholders each \
row is expected to contain, and the editorial rules in force.

Run TWO passes, in this order.

PASS 1 - VALIDATE THE HUMAN'S EDITS.
The reviewer is a human working at speed and can introduce defects. Do not \
treat an edit as correct merely because a human made it, and never infer a \
style rule from what is plainly a slip. Check every edited row for:
  1. Inline tag damage - a numbered placeholder such as {1} that the edit \
dropped, duplicated, renumbered or invented, judged against the expected set \
given for that row.
  2. Typos and syntax breaks, especially inside technical terms, code \
identifiers, function names and UI labels.
  3. Violations of the editorial rules quoted to you - for example a \
typographic quote where the rules mandate straight quotes, or a letter the \
rules forbid.
  4. Word-order or agreement errors that look accidental rather than chosen.

If you find NOTHING wrong, omit the warning section entirely.

If you find ANY issue, begin your reply with exactly this heading and nothing \
before it:

%s

Under it, one bullet per affected row: the segment id, what looks wrong, and \
the minimal correction you would suggest. Be specific and quote the offending \
fragment. Be direct but constructive - the reviewer is the authority on style, \
you are only flagging what looks like an accident rather than a decision. If \
you are unsure whether something is a slip or a deliberate choice, say so \
rather than asserting it is an error.

PASS 2 - EXTRACT THE STYLE BRIEF.
Then, under a heading "TRANSLATION BRIEF", write a short concrete brief for the \
translator handling the next batch: 3-6 bullets, each naming a specific, \
repeatable pattern - terminology, register, sentence length, punctuation, \
formatting. Quote short examples. Derive these ONLY from edits that passed \
pass 1; ignore rows you flagged as defective so their errors never harden into \
rules. Say nothing about segments left unchanged and do not pad with generic \
translation advice. If the clean edits show no consistent pattern, say so \
plainly in one line.""" % OPERATOR_WARNING_HEADING


def build_analysis_payload(
    corrections: Sequence[Correction],
    hard_rules_text: str = "",
    expected_tags: Optional[Dict[str, List[int]]] = None,
) -> str:
    """Serialise the edits for the analyser.

    The expected placeholder set and the rulebook are included so pass 1 has
    something concrete to validate against; without them the analyser can only
    guess whether a missing ``{2}`` was deliberate.
    """
    expected_tags = expected_tags or {}
    payload = [
        {
            "id": c.segment_id,
            "source": c.source,
            "machine": c.llm_suggestion,
            "reviewer": c.human_final,
            **({"note": c.comment} if c.comment else {}),
            **({"expected_tags": expected_tags[c.segment_id]}
               if c.segment_id in expected_tags else {}),
        }
        for c in corrections
    ]
    body: Dict[str, object] = {"edits": payload}
    if hard_rules_text.strip():
        body["editorial_rules_in_force"] = hard_rules_text.strip()
    return json.dumps(body, ensure_ascii=False, indent=2)


def split_analysis(text: str) -> Tuple[str, str]:
    """Separate the warning section from the style brief.

    Returns ``(warnings, brief)``; either may be empty. Keeping them apart lets
    the UI give the warnings their own high-visibility block instead of burying
    them in a wall of markdown.
    """
    if OPERATOR_WARNING_HEADING not in text:
        return "", text.strip()
    _, _, rest = text.partition(OPERATOR_WARNING_HEADING)
    for marker in ("TRANSLATION BRIEF", "## TRANSLATION BRIEF",
                   "**TRANSLATION BRIEF**"):
        if marker in rest:
            warnings, _, brief = rest.partition(marker)
            return warnings.strip(), brief.strip()
    return rest.strip(), ""


def estimate_tokens(text: str) -> int:
    """Rough token estimate - good enough for sizing chunks and warning users.

    Deliberately crude (~4 chars/token); the point is to keep a 100-segment
    batch from silently exceeding a local model's context window, not to bill
    anyone accurately.
    """
    return max(1, len(text) // 4)
