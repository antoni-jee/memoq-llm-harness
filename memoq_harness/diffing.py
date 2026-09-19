"""Word-level diff between memoQ's MT and the model's suggestion.

Shown in the grid before the operator starts editing, so the change the model
actually proposes is visible at a glance instead of having to be spotted by
reading two similar paragraphs side by side.

The diff is word-level rather than character-level: character diffs on
inflected Russian turn every word ending into noise, while word granularity
shows "this word was replaced" which is the decision the operator is making.
Placeholders like ``{1}`` are kept as single tokens so a moved tag reads as one
move rather than three punctuation edits.
"""

from __future__ import annotations

import difflib
import html
import re
from dataclasses import dataclass
from typing import List, Tuple

EQUAL = "equal"
INSERT = "insert"
DELETE = "delete"

# Words, placeholders and punctuation as separate tokens; whitespace is carried
# along with the preceding token so rebuilt text keeps its spacing.
_TOKEN_RE = re.compile(
    r"\{\d+\}|\[\[\d+\]\]|⟦mq\d+⟧"   # placeholders, all schemes
    r"|\w+"                                     # words (unicode-aware)
    r"|[^\w\s]"                                 # punctuation
    r"|\s+",                                    # whitespace
    re.UNICODE,
)


@dataclass
class DiffStats:
    """How much actually changed."""

    added: int = 0
    removed: int = 0
    unchanged: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)

    @property
    def ratio(self) -> float:
        """Share of words touched, 0.0 - 1.0."""
        total = self.added + self.removed + self.unchanged
        return (self.added + self.removed) / total if total else 0.0

    def summary(self) -> str:
        if not self.changed:
            return "no change"
        bits = []
        if self.added:
            bits.append("+%d" % self.added)
        if self.removed:
            bits.append("-%d" % self.removed)
        return "%s words (%.0f%%)" % (" / ".join(bits), self.ratio * 100)


#: Above this combined token count the fine diff is abandoned. difflib's
#: matcher is quadratic in the worst case, and a pathological pair of long
#: segments would otherwise stall the whole Streamlit run - a frozen UI is a
#: far worse outcome than a coarse diff. Real memoQ segments are a sentence or
#: two, so this ceiling is never reached in normal use.
MAX_DIFF_TOKENS = 2000


def tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall(text or "")


def word_diff(old: str, new: str) -> List[Tuple[str, str]]:
    """``(op, text)`` runs turning ``old`` into ``new``.

    Adjacent tokens with the same op are merged so the rendered output has one
    span per change rather than one per word.
    """
    old_tokens, new_tokens = tokenize(old), tokenize(new)

    if len(old_tokens) + len(new_tokens) > MAX_DIFF_TOKENS:
        # Coarse fallback: report the pair as wholly replaced.
        coarse: List[Tuple[str, str]] = []
        if old:
            coarse.append((DELETE, old))
        if new:
            coarse.append((INSERT, new))
        return coarse

    matcher = difflib.SequenceMatcher(a=old_tokens, b=new_tokens, autojunk=False)

    runs: List[Tuple[str, str]] = []

    def push(op: str, text: str) -> None:
        if not text:
            return
        if runs and runs[-1][0] == op:
            runs[-1] = (op, runs[-1][1] + text)
        else:
            runs.append((op, text))

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            push(EQUAL, "".join(old_tokens[i1:i2]))
        elif tag == "delete":
            push(DELETE, "".join(old_tokens[i1:i2]))
        elif tag == "insert":
            push(INSERT, "".join(new_tokens[j1:j2]))
        else:  # replace
            push(DELETE, "".join(old_tokens[i1:i2]))
            push(INSERT, "".join(new_tokens[j1:j2]))
    return runs


def stats(old: str, new: str) -> DiffStats:
    """Word counts for the change, ignoring pure whitespace tokens."""
    result = DiffStats()
    for op, text in word_diff(old, new):
        count = sum(1 for token in tokenize(text) if token.strip())
        if op == INSERT:
            result.added += count
        elif op == DELETE:
            result.removed += count
        else:
            result.unchanged += count
    return result


def render_html(
    old: str,
    new: str,
    max_chars: int = 4000,
    max_runs: int = 80,
) -> str:
    """Inline diff as a self-contained HTML fragment.

    Colours are given explicitly with translucent backgrounds so the block
    stays readable in both Streamlit themes, and shape (strikethrough vs
    underline) carries the same information as colour for anyone who cannot
    separate red from green.
    """
    runs = word_diff(old, new)
    pieces: List[str] = []
    budget = max_chars

    # Each run costs ~90 characters of inline style, so a heavily fragmented
    # diff can emit far more markup than text. Cap the run count too.
    truncated_runs = len(runs) > max_runs
    if truncated_runs:
        runs = runs[:max_runs]

    for op, text in runs:
        if budget <= 0:
            pieces.append('<span style="opacity:.6">...</span>')
            break
        chunk = text[:budget]
        budget -= len(chunk)
        escaped = html.escape(chunk).replace("\n", "<br>")
        if op == EQUAL:
            pieces.append('<span style="opacity:.75">%s</span>' % escaped)
        elif op == DELETE:
            pieces.append(
                '<span style="background:rgba(220,38,38,.18);'
                'text-decoration:line-through;'
                'text-decoration-thickness:1px;border-radius:3px;">%s</span>' % escaped
            )
        else:
            pieces.append(
                '<span style="background:rgba(22,163,74,.20);'
                'border-bottom:2px solid rgba(22,163,74,.75);'
                'border-radius:3px;">%s</span>' % escaped
            )

    if truncated_runs:
        pieces.append('<span style="opacity:.6"> ...</span>')

    return (
        '<div style="font-size:0.82rem;line-height:1.5;padding:6px 9px;'
        'border-left:3px solid rgba(125,125,125,.45);border-radius:3px;'
        'background:rgba(125,125,125,.07);white-space:pre-wrap;'
        'word-break:break-word;">%s</div>' % "".join(pieces)
    )


def render_markdown(old: str, new: str, max_chars: int = 1200) -> str:
    """Plain-markdown fallback for contexts where HTML is not rendered."""
    out: List[str] = []
    budget = max_chars
    for op, text in word_diff(old, new):
        if budget <= 0:
            out.append(" ...")
            break
        chunk = text[:budget].replace("\n", " ")
        budget -= len(chunk)
        if not chunk:
            continue
        if op == EQUAL:
            out.append(chunk)
        elif op == DELETE:
            out.append("~~%s~~" % chunk.strip() if chunk.strip() else chunk)
        else:
            out.append("**%s**" % chunk.strip() if chunk.strip() else chunk)
    return "".join(out)
