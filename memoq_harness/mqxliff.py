"""memoQ bilingual XLIFF (``*.mqxliff``) parsing, filtering and re-export.

memoQ writes XLIFF 1.2 with its own extension namespace.  Which attribute
actually carries "confirmed" and "locked" varies across memoQ versions - some
builds write ``mq:confirmed``/``mq:locked`` on the ``<trans-unit>``, others
encode it in ``mq:status``, and the XLIFF-standard ``<target state="...">`` is
a third signal.  :class:`SegmentFilter` consults all of them so the harness
does not depend on one particular writer.

The document keeps a live handle on the parsed tree, so exporting re-serialises
the *original* file with only the touched ``<target>`` elements rewritten.
Everything else - skeleton, comments, memoQ metadata, confirmed rows - passes
through untouched.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from . import tags as tagmod
from . import xmlcompat as xc

XLIFF_NS = xc.XLIFF_NS
MQ_NS = xc.MQ_NS

# ``mq:status`` values that mean a human has signed the row off.
CONFIRMED_STATUSES = {
    "confirmed",
    "manuallyconfirmed",
    "proofread",
    "reviewer1confirmed",
    "reviewer2confirmed",
}

# XLIFF ``<target state="...">`` values that mean the same.
CONFIRMED_TARGET_STATES = {"signed-off", "final"}

_TRUE = {"true", "1", "yes"}


@dataclass
class SegmentFilter:
    """Which rows count as "needs work".

    Defaults implement the harness rule: take a segment only when it is neither
    confirmed nor locked.  Each signal can be switched off independently when a
    particular memoQ export disagrees.
    """

    skip_confirmed: bool = True
    skip_locked: bool = True
    use_status_attribute: bool = True
    use_target_state: bool = True
    require_nonempty_source: bool = True


@dataclass
class Segment:
    """One translatable ``<trans-unit>`` row."""

    key: str
    """Stable identity for state.json - unit id plus ordinal."""

    unit_id: str
    ordinal: int

    source: tagmod.TaggedText
    target: tagmod.TaggedText
    lookup: tagmod.TaggedText
    """Every tag either side contributed, keyed by placeholder number."""

    tag_source: str = tagmod.TAG_SOURCE_TARGET
    """Which side supplied the canonical tag XML for this row."""

    confirmed: bool = False
    locked: bool = False
    status: str = ""
    target_state: str = ""
    skip_reason: str = ""

    _unit: object = field(default=None, repr=False, compare=False)

    @property
    def source_text(self) -> str:
        return self.source.placeholder_text

    @property
    def target_text(self) -> str:
        return self.target.placeholder_text

    @property
    def translatable(self) -> bool:
        return not self.skip_reason

    @property
    def expected_numbers(self) -> List[int]:
        """Placeholders a translation of this row must reproduce.

        These come from whichever side is canonical for the row - normally the
        target, so a model is held to the localised tag set it was shown.
        """
        canonical = self.target if self.tag_source == tagmod.TAG_SOURCE_TARGET else self.source
        if not canonical.tags:
            canonical = self.source if canonical is self.target else self.target
        return canonical.expected_numbers

    def raw_tags(self) -> List[Tuple[int, str]]:
        """``(number, XML)`` pairs for the operator's raw-tag inspector."""
        return sorted(self.lookup.tags.items())


class MqxliffError(RuntimeError):
    """Raised when a file cannot be read as memoQ bilingual XLIFF."""


class MqxliffDocument:
    """A parsed ``.mqxliff`` file."""

    def __init__(self, raw: bytes, filename: str = "document.mqxliff"):
        self.filename = filename
        self._raw = raw
        try:
            self.root = xc.parse_bytes(raw)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI verbatim
            raise MqxliffError("Could not parse XML: %s" % exc) from exc

        if xc.localname(self.root.tag) != "xliff":
            raise MqxliffError(
                "Root element is <%s>, expected <xliff>. Is this a memoQ "
                "bilingual XLIFF export?" % xc.localname(self.root.tag)
            )

        self.nsmap = xc.element_nsmap(self.root)
        self.nsmap.update(xc.harvest_namespaces(raw))
        self._units: List[object] = self._collect_units()
        if not self._units:
            raise MqxliffError("No <trans-unit> elements found in the file.")
        self.segments: List[Segment] = []

    # ------------------------------------------------------------------ load

    @classmethod
    def from_path(cls, path: str) -> "MqxliffDocument":
        with open(path, "rb") as handle:
            return cls(handle.read(), filename=os.path.basename(path))

    @classmethod
    def from_bytes(cls, raw: bytes, filename: str) -> "MqxliffDocument":
        return cls(raw, filename=filename)

    def _collect_units(self) -> List[object]:
        units = []
        for elem in self.root.iter():
            if xc.localname(elem.tag) == "trans-unit":
                units.append(elem)
        return units

    # --------------------------------------------------------------- parsing

    def parse_segments(
        self,
        seg_filter: Optional[SegmentFilter] = None,
        tag_source: str = tagmod.TAG_SOURCE_TARGET,
    ) -> List[Segment]:
        """Build the segment list, marking which rows the harness should touch.

        ``tag_source`` decides which side owns the inline tags. It defaults to
        the target so memoQ's localised tag attributes survive export; see
        :func:`memoq_harness.tags.isolate_pair`.
        """
        seg_filter = seg_filter or SegmentFilter()
        segments: List[Segment] = []

        for ordinal, unit in enumerate(self._units):
            unit_id = unit.get("id") or str(ordinal)
            source_el = _child(unit, "seg-source") or _child(unit, "source")
            target_el = _child(unit, "target")

            source_xml = xc.inner_xml(source_el, self.nsmap) if source_el is not None else ""
            target_xml = xc.inner_xml(target_el, self.nsmap) if target_el is not None else ""

            confirmed, status, target_state = self._resolve_confirmed(
                unit, target_el, seg_filter
            )
            locked = self._resolve_locked(unit)

            source_tt, target_tt, lookup_tt = tagmod.isolate_pair(
                source_xml, target_xml, prefer=tag_source
            )
            segment = Segment(
                tag_source=tag_source,
                key="%s#%d" % (unit_id, ordinal),
                unit_id=unit_id,
                ordinal=ordinal,
                source=source_tt,
                target=target_tt,
                lookup=lookup_tt,
                confirmed=confirmed,
                locked=locked,
                status=status,
                target_state=target_state,
                _unit=unit,
            )
            segment.skip_reason = self._skip_reason(segment, seg_filter)
            segments.append(segment)

        self.segments = segments
        return segments

    def _resolve_confirmed(
        self, unit, target_el, seg_filter: SegmentFilter
    ) -> Tuple[bool, str, str]:
        confirmed = _attr_localname(unit, "confirmed") or ""
        status = _attr_localname(unit, "status") or ""
        target_state = (target_el.get("state") or "") if target_el is not None else ""

        is_confirmed = confirmed.strip().lower() in _TRUE
        if seg_filter.use_status_attribute and status:
            if status.strip().lower().replace(" ", "") in CONFIRMED_STATUSES:
                is_confirmed = True
        if seg_filter.use_target_state and target_state:
            if target_state.strip().lower() in CONFIRMED_TARGET_STATES:
                is_confirmed = True
        return is_confirmed, status, target_state

    def _resolve_locked(self, unit) -> bool:
        locked = _attr_localname(unit, "locked") or ""
        return locked.strip().lower() in _TRUE

    def _skip_reason(self, segment: Segment, seg_filter: SegmentFilter) -> str:
        if seg_filter.skip_confirmed and segment.confirmed:
            return "confirmed"
        if seg_filter.skip_locked and segment.locked:
            return "locked"
        if seg_filter.require_nonempty_source and not segment.source_text.strip():
            return "empty source"
        return ""

    def translatable_segments(self) -> List[Segment]:
        return [s for s in self.segments if s.translatable]

    def stats(self) -> Dict[str, int]:
        total = len(self.segments)
        confirmed = sum(1 for s in self.segments if s.confirmed)
        locked = sum(1 for s in self.segments if s.locked)
        empty = sum(1 for s in self.segments if not s.source_text.strip())
        return {
            "total": total,
            "confirmed": confirmed,
            "locked": locked,
            "empty_source": empty,
            "translatable": len(self.translatable_segments()),
        }

    # --------------------------------------------------------------- writing

    def apply_translation(
        self,
        segment: Segment,
        text: str,
        mark_confirmed: bool = False,
    ) -> tagmod.RestoreReport:
        """Write ``text`` (placeholder form) into the segment's ``<target>``."""
        unit = segment._unit
        if unit is None:
            raise MqxliffError("Segment %s is not bound to a document." % segment.key)

        fragment, report = tagmod.restore(
            text, segment.lookup, expected=segment.expected_numbers
        )
        target_el = _child(unit, "target")
        if target_el is None:
            target_el = self._create_target(unit)

        xc.replace_children(target_el, fragment, self.nsmap)

        if mark_confirmed:
            target_el.set("state", "translated")
            for key in list(unit.attrib):
                if xc.localname(key) == "status":
                    unit.set(key, "Confirmed")
                    break
            else:
                unit.set(xc.qname(MQ_NS, "status"), "Confirmed")
        return report

    def _create_target(self, unit):
        """Insert a ``<target>`` right after ``<seg-source>``/``<source>``."""
        target_el = _make_element(unit, "target")
        anchor = _child(unit, "seg-source") or _child(unit, "source")
        if anchor is not None:
            index = list(unit).index(anchor)
            unit.insert(index + 1, target_el)
        else:
            unit.append(target_el)
        return target_el

    def to_bytes(self) -> bytes:
        return xc.serialize_document(self.root, self._raw)

    def save(self, path: str) -> str:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(self.to_bytes())
        return path


# ---------------------------------------------------------------- utilities


def _child(parent, name: str):
    """First direct child with the given local name, ignoring namespace."""
    if parent is None:
        return None
    for child in parent:
        if xc.localname(child.tag) == name:
            return child
    return None


def _attr_localname(elem, name: str) -> Optional[str]:
    """Attribute value matched by local name, whatever its namespace."""
    for key, value in elem.attrib.items():
        if xc.localname(key) == name or key == name:
            return value
    return None


def _make_element(sibling, name: str):
    """Create an element in the same default namespace as its siblings."""
    tag = sibling.tag
    if isinstance(tag, str) and tag.startswith("{"):
        namespace = tag[1:].split("}", 1)[0]
        return xc.etree.Element(xc.qname(namespace, name))
    return xc.etree.Element(name)


def export_filename(original: str, suffix: str = "harness") -> str:
    """``report.mqxliff`` -> ``report_harness_20260919-1430.mqxliff``."""
    stem, ext = os.path.splitext(os.path.basename(original))
    ext = ext or ".mqxliff"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return "%s_%s_%s%s" % (stem, suffix, stamp, ext)
