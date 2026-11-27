"""Read-only presentation adapter for remote mode.

The app never holds a ``ProjectState`` in remote mode, so it has nothing to
mutate or save. This module is the one place that turns the two read
responses - the document service's parsed segments and the workspace service's
snapshot - into the small, immutable view the read-only UI renders. It is a
*presentation* adapter, deliberately not a second ``ProjectState``: it carries
only the fields a read-only grid needs and no save path.

Three fields are kept distinct on purpose (a clarification the operator set):

* ``mt_target``  - the *original target*, memoQ's own AGT text, taken from the
  document segment's ``target_text``.
* ``user_final`` - the human's saved text, taken from the workspace record.
* ``llm_suggestion`` - the model's last proposal, taken from the workspace
  record.

Collapsing them into a single "target" would hide which side produced the text
the operator is looking at, so the merge maps each source field to its own
destination and never overwrites one with another.

The module knows nothing of ``memoq_harness``; it only reads JSON-shaped dicts,
so it stays importable from the transport-only ``harness_remote`` package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence

DEFAULT_BATCH_SIZE = 100


@dataclass(frozen=True)
class RemoteViewRecord:
    """One segment as the read-only grid sees it.

    Immutable on purpose: the grid can display it but not write back through it.
    ``mt_target`` is the original target; ``user_final`` and ``llm_suggestion``
    are distinct, so the UI can show memoQ's text, the model's proposal and the
    human's saved text side by side without conflating them.
    """

    key: str
    segment_id: str
    ordinal: int
    source_text: str
    mt_target: str
    llm_suggestion: str = ""
    user_final: str = ""
    user_comment: str = ""
    status: str = "pending"
    edited_by_human: bool = False
    tag_report: str = ""
    translatable: bool = True
    expected_numbers: List[int] = ()

    @property
    def is_dirty(self) -> bool:
        """True when the human has saved text that differs from the original."""
        final = (self.user_final or "").strip()
        base = (self.mt_target or "").strip()
        return bool(final) and final != base


@dataclass(frozen=True)
class BatchProgress:
    total: int
    processed: int
    applied: int
    edited: int


class RemoteWorkspaceView:
    """A read-only projection of one bound workspace over its document segments.

    Built once from a successful Open (document upload + bind + read) or a
    successful Reload (read + identity check). The app holds at most one of
    these at a time; a failed Open never replaces the live one.
    """

    def __init__(
        self,
        *,
        document_id: str,
        original_id: str,
        tag_source: str,
        revision: int,
        batch_size: int,
        order: Sequence[str],
        records: Mapping[str, RemoteViewRecord],
        source_file: str = "",
        current_batch: int = 0,
        counts: Optional[Mapping[str, int]] = None,
        corrections: Optional[Sequence[Mapping[str, object]]] = None,
        analysis: Optional[Mapping[str, str]] = None,
        layout_version: int = 0,
    ):
        self.document_id = document_id
        self.original_id = original_id
        """The original this workspace is bound to; Reload verifies it."""
        self.tag_source = tag_source
        """Verified on Reload: a different tag_source means a silent mismatch."""
        self.revision = revision
        self.batch_size = batch_size or DEFAULT_BATCH_SIZE
        # The working layout's version. Feedback is scoped to the current
        # layout, so a re-batch (a new layout) never adopts an earlier
        # layout's correction even when the batch indices collide. 0 is the
        # version of every snapshot written before layout versioning.
        self.layout_version = int(layout_version or 0)
        self.order: List[str] = list(order)
        self._records: Dict[str, RemoteViewRecord] = dict(records)
        self.source_file = source_file
        self.current_batch = current_batch
        self._counts = dict(counts) if counts else None
        # Stored human corrections (the workspace persists them in its snapshot,
        # so they survive a re-read). Each is a JSON-shaped dict with the
        # Correction fields: segment_id, source, llm_suggestion, human_final,
        # comment, batch_index. Fed to the next batch's feedback below.
        self.corrections: List[dict] = [dict(c) for c in (corrections or [])]
        # The per-batch corrections critique, a separate slot keyed by the batch
        # string (like the local ``proj.analysis``); it never overwrites the
        # saved text, the proposal or the original target.
        self.analysis: Dict[str, str] = dict(analysis or {})

    # ---------------------------------------------------------- construction

    @classmethod
    def from_remote(
        cls,
        doc_segments: Sequence[Mapping[str, object]],
        workspace: Mapping[str, object],
        *,
        batch_size_default: int = DEFAULT_BATCH_SIZE,
    ) -> "RemoteWorkspaceView":
        """Merge document segments over the workspace snapshot.

        The workspace is authoritative for identity (which original, which
        tag_source, the active ``order`` and the saved per-segment work). The
        document segments supply the *current* source text and, crucially, the
        original target. A key that is active in the workspace but absent from
        the document segments still renders from its saved record, and vice
        versa is impossible because the active order comes from the workspace.
        """
        doc_by_key = {str(s.get("key")): s for s in doc_segments if s.get("key")}
        ws_segments = workspace.get("segments") or {}
        order = list(workspace.get("order") or [])

        records: Dict[str, RemoteViewRecord] = {}
        for key in order:
            key = str(key)
            rec = ws_segments.get(key) or {}
            doc = doc_by_key.get(key) or {}
            records[key] = _merge_record(key, rec, doc)

        batch_size = int(workspace.get("batch_size") or batch_size_default)
        return cls(
            document_id=str(workspace.get("document_id") or ""),
            original_id=str(workspace.get("bound_original_id") or ""),
            tag_source=str(workspace.get("tag_source") or "target"),
            revision=int(workspace.get("revision") or 0),
            batch_size=batch_size,
            order=order,
            records=records,
            source_file=str(workspace.get("source_file") or ""),
            current_batch=int(workspace.get("current_batch") or 0),
            counts=workspace.get("counts"),
            corrections=workspace.get("corrections"),
            analysis=workspace.get("analysis"),
            layout_version=int(workspace.get("layout_version", 0) or 0),
        )

    # ---------------------------------------------------------- accessors

    @property
    def is_empty(self) -> bool:
        return not self.order

    @property
    def batch_count(self) -> int:
        if not self.order:
            return 0
        return (len(self.order) + self.batch_size - 1) // self.batch_size

    def batch_keys(self, batch_index: int) -> List[str]:
        start = batch_index * self.batch_size
        return self.order[start : start + self.batch_size]

    def batch_records(self, batch_index: int) -> List[RemoteViewRecord]:
        return [self._records[k] for k in self.batch_keys(batch_index)
                if k in self._records]

    def record(self, key: str) -> Optional[RemoteViewRecord]:
        return self._records.get(key)

    def progress(self, batch_index: int) -> BatchProgress:
        records = self.batch_records(batch_index)
        return BatchProgress(
            total=len(records),
            processed=sum(1 for r in records if r.status in ("processed", "applied")),
            applied=sum(1 for r in records if r.status == "applied"),
            edited=sum(1 for r in records if r.edited_by_human),
        )

    def counts(self) -> Dict[str, int]:
        if self._counts is not None:
            return dict(self._counts)
        return {
            "segments": len(self._records),
            "active": len(self.order),
            "with_saved_work": sum(1 for r in self._records.values()
                                   if _has_saved_work(r)),
        }

    def current_batch_index(self, fallback: int = 0) -> int:
        if self.batch_count == 0:
            return 0
        return min(max(0, self.current_batch or fallback), self.batch_count - 1)

    def expected_tags(self, segment_ids=None) -> Dict[str, List[int]]:
        """The expected number-tags per segment, the parity signal the local
        ``run_analysis`` feeds ``analyze_corrections``.

        With no ``segment_ids`` it covers every active record; otherwise only the
        requested ids. A segment whose record is absent, or that carries no
        expected numbers, contributes nothing, mirroring the local guard
        ``if c.segment_id in by_id`` and ``expected_numbers`` being possibly empty.
        """
        ids = list(self.order) if segment_ids is None else list(segment_ids)
        tags: Dict[str, List[int]] = {}
        for sid in ids:
            rec = self._records.get(str(sid))
            if rec is not None and rec.expected_numbers:
                tags[str(sid)] = list(rec.expected_numbers)
        return tags

    def corrections_block(self, upto_batch: int = -1, limit: int = 40,
                          detailed: bool = True) -> str:
        """Render the stored corrections that predate ``upto_batch`` for the
        next batch's feedback.

        This mirrors the local ``corrections_for_prompt`` / ``feedback_block`` /
        ``Correction.render`` semantics, so the remote model receives the same
        signal the local workflow does, but it runs on JSON-shaped dicts and must
        not import ``memoq_harness`` (this layer is transport-only). A batch
        cannot correct itself, so only corrections recorded before ``upto_batch``
        feed it; the newest are kept first, capped to ``limit``. An empty result
        yields the empty string (the local ``feedback_block`` contract).

        Feedback is scoped to the *current layout*: only corrections produced
        under this view's ``layout_version`` are eligible. A re-batch starts a
        new layout, so an earlier layout's correction (which keeps its original
        batch_index) never leaks into the new layout's prompt even when the batch
        indices collide. Corrections and views written before layout versioning
        carry version 0, so a pre-rebatch correction still feeds feedback.
        """
        layout = int(self.layout_version or 0)
        eligible = [c for c in self.corrections
                    if int(c.get("batch_index", 0) or 0) < upto_batch
                    and int(c.get("layout_version", 0) or 0) == layout]
        eligible.sort(key=lambda c: int(c.get("batch_index", 0) or 0), reverse=True)
        items = eligible[:limit]
        rendered = [_render_correction(c, detailed) for c in items]
        rendered.reverse()
        return "\n".join(rendered)


def _merge_record(key: str, rec: Mapping[str, object],
                  doc: Mapping[str, object]) -> RemoteViewRecord:
    """Map one workspace record and its document segment onto a view record.

    The original target comes from the document segment's ``target_text`` (a
    clarification: it must not be conflated with the human's ``user_final`` or
    the model's ``llm_suggestion``). The saved human work and the model's
    proposal come from the workspace record. A field the workspace omits falls
    back to the document segment only for the document-owned source text.
    """
    segment_id = str(rec.get("segment_id") or doc.get("unit_id") or key)
    ordinal = rec.get("ordinal")
    if ordinal is None:
        ordinal = doc.get("ordinal", 0)
    ordinal = int(ordinal or 0)

    source_text = _as_str(doc.get("source_text")) or _as_str(rec.get("source_text"))
    # The original target is the document's target_text; fall back to the
    # workspace's stored mt_target only when the document segment is absent.
    mt_target = _as_str(doc.get("target_text")) or _as_str(rec.get("mt_target"))

    return RemoteViewRecord(
        key=key,
        segment_id=segment_id,
        ordinal=ordinal,
        source_text=source_text,
        mt_target=mt_target,
        llm_suggestion=_as_str(rec.get("llm_suggestion")),
        user_final=_as_str(rec.get("user_final")),
        user_comment=_as_str(rec.get("user_comment")),
        status=str(rec.get("status") or "pending"),
        edited_by_human=bool(rec.get("edited_by_human", False)),
        tag_report=_as_str(rec.get("tag_report")),
        translatable=bool(doc.get("translatable", True)),
        expected_numbers=list(doc.get("expected_numbers") or []),
    )


def _as_str(value: object) -> str:
    return "" if value is None else str(value)


def _has_saved_work(record: RemoteViewRecord) -> bool:
    """Mirror of the service's has_saved_work, over the view's fields."""
    return bool(
        record.edited_by_human
        or record.user_comment.strip()
        or (record.user_final.strip() and record.user_final != record.mt_target)
    )


def _render_correction(correction: Mapping[str, object], detailed: bool = True) -> str:
    """Render one stored correction the way the local ``Correction.render`` does.

    Mirrors the local format exactly (a ``<segment_id: "human_final">`` line plus
    indented ``source`` / ``rejected`` / ``note`` lines when detailed) so the
    feedback block is byte-for-byte the local signal, but it reads a JSON-shaped
    dict and must not import ``memoq_harness``. A ``rejected`` line appears only
    when the model's suggestion differs from the human's final; an empty
    ``source`` is skipped.
    """
    segment_id = str(correction.get("segment_id") or "")
    human_final = _as_str(correction.get("human_final"))
    block = '<%s: "%s">' % (segment_id, human_final)
    if not detailed:
        return block
    lines = [block]
    source = _as_str(correction.get("source"))
    if source:
        lines.append('    source:   "%s"' % source)
    llm_suggestion = _as_str(correction.get("llm_suggestion"))
    if llm_suggestion and llm_suggestion != human_final:
        lines.append('    rejected: "%s"' % llm_suggestion)
    comment = _as_str(correction.get("comment"))
    if comment:
        lines.append('    note:     "%s"' % comment)
    return "\n".join(lines)
