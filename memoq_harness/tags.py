"""Inline memoQ tag isolation and restoration.

memoQ segments carry inline formatting/placeable tags (``<bpt>``, ``<ept>``,
``<ph>``, ``<it>``, ``<x/>``, ``<g>``, ``<mrk>``).  Feeding raw XML to an LLM
invites mangled markup, so each tag is swapped for a compact numbered
placeholder before prompting and restored verbatim afterwards.

The placeholder form mirrors what memoQ shows its own users (``{1}``, ``{2}``),
which is also the form LLMs have most often seen for CAT-tool tags.  When a
segment's literal text already contains something that looks like a
placeholder, an alternate scheme is selected for that segment so restoration
can never mis-target.

Guarantee: for any input, restoring an *unmodified* placeholder string
reproduces XML semantically identical to the original - every inline tag is
byte-identical and in its original position.  Character data is normalised
(``&quot;``/``&apos;``/numeric references become literal characters), which
memoQ and any XML parser read as the same content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# Paired tags whose entire subtree is atomic (``<bpt id="1">&lt;b&gt;</bpt>``).
_PAIRED = r"<(?P<ptag>bpt|ept|ph|it)\b[^>]*>.*?</(?P=ptag)\s*>"
# Self-closing forms of any inline tag.
_SELFCLOSE = r"<(?:bpt|ept|ph|it|x|g|mrk|sub)\b[^>]*?/>"
# Wrapper tags that surround content, kept as separate open/close placeholders.
_OPEN = r"<(?:g|mrk)\b[^>]*>"
_CLOSE = r"</(?:g|mrk)\s*>"

_TAG_RE = re.compile(
    "|".join("(?:%s)" % p for p in (_PAIRED, _SELFCLOSE, _OPEN, _CLOSE)),
    re.DOTALL,
)

# Placeholder schemes, tried in order; the first that cannot collide wins.
_SCHEMES: Sequence[Tuple[str, str, str]] = (
    ("brace", "{%d}", r"\{(\d+)\}"),
    ("bracket", "[[%d]]", r"\[\[(\d+)\]\]"),
    ("guard", "\u27e6mq%d\u27e7", r"\u27e6mq(\d+)\u27e7"),
)

_ENTITY_RE = re.compile(r"&(#\d+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);")

_NAMED_ENTITIES = {
    "lt": "<",
    "gt": ">",
    "quot": '"',
    "apos": "'",
    "amp": "&",
}


@dataclass
class TaggedText:
    """A segment split into LLM-safe text plus the tags removed from it."""

    placeholder_text: str
    """Plain text with each inline tag replaced by a numbered placeholder."""

    tags: Dict[int, str] = field(default_factory=dict)
    """Placeholder number -> the exact XML fragment it stands for."""

    scheme: str = "brace"
    """Which placeholder scheme was used for this segment."""

    @property
    def placeholder_pattern(self) -> re.Pattern:
        return re.compile(_scheme_by_name(self.scheme)[2])

    def format_placeholder(self, number: int) -> str:
        return _scheme_by_name(self.scheme)[1] % number

    @property
    def expected_numbers(self) -> List[int]:
        return sorted(self.tags)

    def numbers_in(self, text: str) -> List[int]:
        """Placeholder numbers present in an arbitrary string, in order."""
        return [int(m.group(1)) for m in self.placeholder_pattern.finditer(text)]


def _scheme_by_name(name: str) -> Tuple[str, str, str]:
    for scheme in _SCHEMES:
        if scheme[0] == name:
            return scheme
    return _SCHEMES[0]


def unescape_text(value: str) -> str:
    """Resolve the XML entities and numeric references found in character data.

    A single left-to-right pass, so ``&amp;lt;`` correctly yields the literal
    text ``&lt;`` rather than being unescaped twice.  Numeric references are
    resolved to real characters; leaving them as ``&#160;`` would cause
    :func:`escape_text` to double-escape the ampersand on the way back out.
    """

    def _resolve(match: re.Match) -> str:
        body = match.group(1)
        if body.startswith("#"):
            try:
                code = int(body[2:], 16) if body[1:2].lower() == "x" else int(body[1:])
            except ValueError:
                return match.group(0)
            if 0 < code <= 0x10FFFF:
                return chr(code)
            return match.group(0)
        if body in _NAMED_ENTITIES:
            return _NAMED_ENTITIES[body]
        # Unknown entity: leave the reference intact rather than corrupting it.
        return match.group(0)

    return _ENTITY_RE.sub(_resolve, value)


def escape_text(value: str) -> str:
    """Inverse of :func:`unescape_text` for re-embedding as character data."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def isolate(inner_xml: str) -> TaggedText:
    """Replace every inline tag in a serialised segment with a placeholder.

    ``inner_xml`` is the serialised *content* of a ``<source>``/``<target>``
    element.  The returned text is human/LLM readable: entities are resolved and
    tags become ``{1}``-style markers.
    """
    tags: Dict[int, str] = {}
    pieces = _tokenize(inner_xml)
    scheme_name, template, _ = _pick_scheme([p for is_tag, p in pieces if not is_tag])

    out: List[str] = []
    counter = 0
    for is_tag, payload in pieces:
        if is_tag:
            counter += 1
            tags[counter] = payload
            out.append(template % counter)
        else:
            out.append(unescape_text(payload))

    return TaggedText(placeholder_text="".join(out), tags=tags, scheme=scheme_name)


TAG_SOURCE_TARGET = "target"
TAG_SOURCE_SOURCE = "source"


def isolate_pair(
    source_xml: str,
    target_xml: str,
    prefer: str = TAG_SOURCE_TARGET,
) -> Tuple[TaggedText, TaggedText, TaggedText]:
    """Isolate a source/target pair under one shared placeholder numbering.

    Isolating the two sides independently would be a correctness trap: ``{1}``
    in the source and ``{1}`` in the target would be numbered per-call and
    could stand for different tags, so a model asked to "keep the tags" would
    silently swap them. One side must therefore define the numbering, and the
    XML it holds is what gets written back on export.

    **That side is the target by default, and this matters.** memoQ's MT layer
    localises the *insides* of inline tags - an ``<a href="/en/articles/123">``
    in the source routinely arrives as ``/ru/articles/123`` in the pre-translated
    target. Numbering from the source and restoring source XML would silently
    revert every one of those localised links. Taking the target as canonical
    preserves them.

    The source's own tags are then mapped onto the target's numbers - by exact
    XML where they are unchanged, positionally where the target has localised
    them - so the model still sees one consistent set of placeholders on both
    sides. A source tag with no target counterpart gets a fresh number above
    the target's range rather than being dropped.

    ``prefer=TAG_SOURCE_SOURCE`` restores the old source-canonical behaviour.
    The target is also skipped automatically when it carries no tags at all
    (an empty or untranslated row), since there would be nothing to anchor to.

    Returns ``(source, target, lookup)``. ``lookup`` carries every tag either
    side contributed and is what :func:`restore` resolves against.
    """
    source_pieces = _tokenize(source_xml)
    target_pieces = _tokenize(target_xml)
    scheme_name, template, _ = _pick_scheme(
        [p for is_tag, p in source_pieces + target_pieces if not is_tag]
    )

    source_tag_list = [p for is_tag, p in source_pieces if is_tag]
    target_tag_list = [p for is_tag, p in target_pieces if is_tag]

    use_target = prefer == TAG_SOURCE_TARGET and bool(target_tag_list)
    primary_list = target_tag_list if use_target else source_tag_list
    secondary_list = source_tag_list if use_target else target_tag_list

    # The primary side owns numbers 1..n and supplies the canonical XML.
    lookup: Dict[int, str] = {i + 1: xml for i, xml in enumerate(primary_list)}
    secondary_numbers = _map_secondary(primary_list, secondary_list, lookup)

    primary_tags = {i + 1: xml for i, xml in enumerate(primary_list)}
    secondary_tags = {n: secondary_list[j] for j, n in secondary_numbers.items()}

    if use_target:
        target_tags, source_tags = primary_tags, secondary_tags
        target_numbers = list(range(1, len(primary_list) + 1))
        source_numbers = [secondary_numbers[j] for j in range(len(secondary_list))]
    else:
        source_tags, target_tags = primary_tags, secondary_tags
        source_numbers = list(range(1, len(primary_list) + 1))
        target_numbers = [secondary_numbers[j] for j in range(len(secondary_list))]

    source_text = _render(source_pieces, source_numbers, template)
    target_text = _render(target_pieces, target_numbers, template)

    return (
        TaggedText(source_text, source_tags, scheme_name),
        TaggedText(target_text, target_tags, scheme_name),
        TaggedText("", lookup, scheme_name),
    )


def _map_secondary(
    primary_list: Sequence[str],
    secondary_list: Sequence[str],
    lookup: Dict[int, str],
) -> Dict[int, int]:
    """Map each secondary tag index onto a placeholder number.

    Exact XML matches bind first, so unchanged tags keep their obvious number.
    Whatever is left binds positionally, which is the right guess when the MT
    layer has rewritten a tag's attributes but kept the tag order. Anything
    still unmatched gets a number of its own.
    """
    free = list(range(len(primary_list)))
    mapping: Dict[int, int] = {}

    for j, xml in enumerate(secondary_list):
        for i in free:
            if primary_list[i] == xml:
                mapping[j] = i + 1
                free.remove(i)
                break

    leftovers = [j for j in range(len(secondary_list)) if j not in mapping]
    for j in leftovers:
        if free:
            # Prefer the slot at the same ordinal when it is still available.
            i = j if j in free else free[0]
            mapping[j] = i + 1
            free.remove(i)
        else:
            number = (max(lookup) + 1) if lookup else 1
            lookup[number] = secondary_list[j]
            mapping[j] = number
    return mapping


def _render(pieces: Sequence[Tuple[bool, str]], numbers: Sequence[int], template: str) -> str:
    """Rebuild placeholder text, substituting the assigned tag numbers."""
    out: List[str] = []
    index = 0
    for is_tag, payload in pieces:
        if is_tag:
            out.append(template % numbers[index])
            index += 1
        else:
            out.append(unescape_text(payload))
    return "".join(out)


def _tokenize(inner_xml: str) -> List[Tuple[bool, str]]:
    """Split serialised segment content into ``(is_tag, payload)`` runs."""
    pieces: List[Tuple[bool, str]] = []
    cursor = 0
    for match in _TAG_RE.finditer(inner_xml):
        if match.start() > cursor:
            pieces.append((False, inner_xml[cursor : match.start()]))
        pieces.append((True, match.group(0)))
        cursor = match.end()
    if cursor < len(inner_xml):
        pieces.append((False, inner_xml[cursor:]))
    return pieces


def _pick_scheme(text_runs: Sequence[str]) -> Tuple[str, str, str]:
    """Choose the first placeholder scheme that the literal text cannot mimic."""
    joined = unescape_text("".join(text_runs))
    for scheme in _SCHEMES:
        if not re.search(scheme[2], joined):
            return scheme
    return _SCHEMES[-1]


@dataclass
class RestoreReport:
    """Tag-integrity outcome for one restored segment."""

    missing: List[int] = field(default_factory=list)
    """Placeholders that were in the source but absent from the model output."""

    unknown: List[int] = field(default_factory=list)
    """Placeholder numbers the model invented that have no source tag."""

    duplicated: List[int] = field(default_factory=list)
    """Placeholders the model emitted more than once."""

    @property
    def ok(self) -> bool:
        return not (self.missing or self.unknown or self.duplicated)

    def summary(self) -> str:
        if self.ok:
            return "tags OK"
        bits = []
        if self.missing:
            bits.append("missing %s" % ", ".join(str(n) for n in self.missing))
        if self.unknown:
            bits.append("unknown %s" % ", ".join(str(n) for n in self.unknown))
        if self.duplicated:
            bits.append("duplicated %s" % ", ".join(str(n) for n in self.duplicated))
        return "; ".join(bits)


def restore(
    text: str,
    tagged: TaggedText,
    append_missing: bool = True,
    expected: Optional[Sequence[int]] = None,
) -> Tuple[str, RestoreReport]:
    """Rebuild segment XML from placeholder text.

    ``tagged`` supplies the number -> XML lookup; pass the ``lookup`` returned
    by :func:`isolate_pair` so both source and target tags resolve.  ``expected``
    overrides which placeholders *should* be present - for a translation that is
    the source's tag numbers, not the lookup's union.

    Returns the XML fragment plus a report describing any tag damage the model
    introduced.  Unknown placeholders are dropped; missing ones are appended at
    the end when ``append_missing`` is set, so a damaged response still yields a
    file memoQ will open rather than one it rejects.
    """
    report = RestoreReport()
    wanted = list(expected) if expected is not None else tagged.expected_numbers
    pattern = tagged.placeholder_pattern
    seen: Dict[int, int] = {}

    out: List[str] = []
    cursor = 0
    for match in pattern.finditer(text):
        if match.start() > cursor:
            out.append(escape_text(text[cursor : match.start()]))
        number = int(match.group(1))
        seen[number] = seen.get(number, 0) + 1
        if number in tagged.tags:
            out.append(tagged.tags[number])
        else:
            report.unknown.append(number)
        cursor = match.end()
    if cursor < len(text):
        out.append(escape_text(text[cursor:]))

    report.missing = [n for n in wanted if n not in seen]
    report.duplicated = sorted(n for n, count in seen.items() if count > 1)

    if append_missing and report.missing:
        for number in report.missing:
            if number in tagged.tags:
                out.append(tagged.tags[number])

    return "".join(out), report


def strip_placeholders(text: str, tagged: TaggedText) -> str:
    """Text with placeholders removed - for length/similarity comparisons."""
    return tagged.placeholder_pattern.sub("", text).strip()
