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
    ):
        self.document_id = document_id
        self.original_id = original_id
        """The original this workspace is bound to; Reload verifies it."""
        self.tag_source = tag_source
        """Verified on Reload: a different tag_source means a silent mismatch."""
        self.revision = revision
        self.batch_size = batch_size or DEFAULT_BATCH_SIZE
        self.order: List[str] = list(order)
        self._records: Dict[str, RemoteViewRecord] = dict(records)
        self.source_file = source_file
        self.current_batch = current_batch
        self._counts = dict(counts) if counts else None

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
