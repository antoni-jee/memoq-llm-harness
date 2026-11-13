"""Project state, batching and the automatic human-correction feedback loop.

Everything the harness needs to resume a session lives in ``state.json``:
hard rules (glossary), the soft "vibe", per-segment history, and the
feedback blocks distilled from each batch the human has signed off.

The feedback loop is the point of the tool.  When the operator finishes batch
N, every row where the human text differs from what the model proposed is
turned into a correction record.  Those records are injected into the system
prompt for batch N+1 as in-context learning examples, so the model's later
batches drift toward the human's actual preferences instead of repeating the
same rejected phrasing a hundred times.

API keys are deliberately *never* written here - state.json is a project file
that users copy around and paste into issue reports.
"""

from __future__ import annotations

import copy
import csv
import functools
import io
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence

from . import defaults
from . import prompt_templates as templates_mod
from . import tags as tagmod

STATE_VERSION = 5
DEFAULT_BATCH_SIZE = 100

# v5 multi-document layout. Global settings live at the top level; everything
# below is per document and lives in one snapshot per document ID.
GLOBAL_FIELDS = (
    "batch_size", "vibe", "hard_rules_text", "detailed_feedback",
    "prompt_templates", "prompt_stage", "tag_source", "auto_merge_proposals",
    "rule_log", "termbase",
)
DOCUMENT_FIELDS = (
    "source_file", "source_path", "created_at", "updated_at", "current_batch",
    "analysis", "global_directive", "corrections", "order", "segments",
)
_STRUCTURE_FIELDS = ("version", "active_document", "documents", "unbound_document")

LEGACY_DOCUMENT_ID = "legacy"
"""Document ID under which a pre-v5 single-document state is kept.

It is never attached to a real document automatically; the verbatim original
JSON is kept inside that entry under ``legacy_original``.
"""

_MAX_BACKUP_ATTEMPTS = 1000


class StateBackupError(OSError):
    """A legacy state file could not be backed up, so it was not overwritten."""


def _is_v5_payload(data: object) -> bool:
    if not isinstance(data, dict) or "documents" not in data:
        return False
    try:
        return int(data.get("version", 1)) >= 5
    except (TypeError, ValueError):
        return False


def _write_backup(path: str, data: bytes) -> bool:
    """Create ``path`` exclusively with ``data``, fsync it, and read it back.

    Raises FileExistsError if ``path`` exists (it is never overwritten) and
    OSError on any I/O failure. Returns whether the read-back matched.
    """
    with open(path, "xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    with open(path, "rb") as handle:
        return handle.read() == data

# Column positions in memoQ's termbase CSV export. Row 0 is
# "English,English,English,English,English,Russian,Russian" - five source
# synonym columns then two target ones - so the primary pair is 0 and 5.
MEMOQ_SOURCE_COL = 0
MEMOQ_TARGET_COL = 5


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def default_prompt_templates() -> Dict[str, str]:
    """Fresh copy of the operator's RTF-sourced stage templates."""
    return {stage.key: stage.template for stage in templates_mod.STAGES}


@dataclass
class TermEntry:
    """One termbase pair the model must honour verbatim."""

    source: str
    target: str
    note: str = ""

    def render(self) -> str:
        line = '"%s" -> "%s"' % (self.source, self.target)
        return "%s  (%s)" % (line, self.note) if self.note else line


# Former name, kept so older state.json files and any external callers still work.
HardRule = TermEntry


@dataclass
class TermbaseLoadReport:
    """What a termbase import actually did - surfaced in the UI."""

    loaded: int = 0
    header_rows: int = 0
    data_rows: int = 0
    blank_rows: int = 0
    incomplete_rows: int = 0
    duplicate_rows: int = 0
    encoding: str = ""
    delimiter: str = ","

    def summary(self) -> str:
        bits = ["%d terms loaded" % self.loaded]
        if self.incomplete_rows:
            bits.append("%d rows missing a source or target" % self.incomplete_rows)
        if self.duplicate_rows:
            bits.append("%d duplicate sources" % self.duplicate_rows)
        if self.blank_rows:
            bits.append("%d blank rows" % self.blank_rows)
        detail = "%s, delimiter %r" % (self.encoding, self.delimiter)
        return "%s (%s)" % ("; ".join(bits), detail)


def _decode_csv(data: bytes | str) -> tuple[str, str]:
    """Decode CSV bytes, sniffing the encodings memoQ actually emits."""
    if isinstance(data, str):
        return data, "str"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8 (lossy)"


def _sniff_delimiter(text: str) -> str:
    """Pick the delimiter from the header row; memoQ varies by host locale."""
    first = text.split("\n", 1)[0]
    counts = {d: first.count(d) for d in (",", ";", "\t")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


def _squash(value: str) -> str:
    """Collapse internal whitespace so a term stays on one prompt line."""
    return re.sub(r"\s+", " ", value).strip()


@functools.lru_cache(maxsize=8192)
def _term_pattern(term: str) -> re.Pattern:
    """Whole-word matcher for a termbase source term.

    Word boundaries are only applied where the term's own edge is alphanumeric;
    a term like ``C++`` or ``.mqh`` would never match otherwise.

    Capitalisation is treated as meaningful. A termbase entry carrying an
    uppercase letter is a proper noun or a UI label, so it is matched
    case-sensitively; otherwise real exports wreck ordinary prose - ``To`` ->
    "Кому" and ``File`` -> "Файл" are the MetaTrader UI's labels, and letting
    them fire on the words "to" and "file" would instruct the model to
    mistranslate them. All-lowercase entries stay case-insensitive so they
    still match at the start of a sentence.
    """
    left = r"\b" if term[:1].isalnum() else ""
    right = r"\b" if term[-1:].isalnum() else ""
    flags = 0 if any(c.isupper() for c in term) else re.IGNORECASE
    return re.compile(left + re.escape(term) + right, flags)


@dataclass
class Correction:
    """A single human edit, distilled for in-context learning."""

    segment_id: str
    source: str
    llm_suggestion: str
    human_final: str
    comment: str = ""
    batch_index: int = 0

    def render(self, detailed: bool = True) -> str:
        """Render as the feedback block injected into the next system prompt."""
        block = '<%s: "%s">' % (self.segment_id, self.human_final)
        if not detailed:
            return block
        lines = [block]
        if self.source:
            lines.append('    source:   "%s"' % self.source)
        if self.llm_suggestion and self.llm_suggestion != self.human_final:
            lines.append('    rejected: "%s"' % self.llm_suggestion)
        if self.comment:
            lines.append('    note:     "%s"' % self.comment)
        return "\n".join(lines)


@dataclass
class SegmentRecord:
    """Per-segment working state, persisted across runs."""

    key: str
    segment_id: str
    ordinal: int
    batch_index: int
    source_text: str = ""
    mt_target: str = ""
    """The target memoQ already had (AGT machine translation), untouched."""

    llm_suggestion: str = ""
    user_final: str = ""
    user_comment: str = ""
    status: str = "pending"  # pending | processed | applied
    edited_by_human: bool = False
    tag_report: str = ""

    def effective_text(self) -> str:
        """Best available text for this row, falling back to memoQ's own MT."""
        for candidate in (self.user_final, self.llm_suggestion, self.mt_target):
            if candidate.strip():
                return candidate
        return ""

    def new_text(self) -> str:
        """Text the harness produced, or "" if the row is untouched.

        Export uses this so rows nobody has worked on are left exactly as memoQ
        wrote them, rather than being rewritten with their own MT.
        """
        for candidate in (self.user_final, self.llm_suggestion):
            if candidate.strip():
                return candidate
        return ""

    def is_dirty(self) -> bool:
        text = self.new_text()
        return bool(text) and text != self.mt_target


class ProjectState:
    """Loads, mutates and persists ``state.json``."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.version: int = STATE_VERSION
        self.source_file: str = ""
        self.source_path: str = ""
        self.created_at: str = _now()
        self.updated_at: str = _now()
        self.batch_size: int = DEFAULT_BATCH_SIZE
        self.current_batch: int = 0
        # The editorial rulebook: free-form prose, injected verbatim. Distinct
        # from the termbase, which is a filtered table of term pairs.
        self.hard_rules_text: str = defaults.DEFAULT_HARD_RULES
        self.vibe: str = defaults.DEFAULT_VIBE
        self.termbase: List[TermEntry] = []
        # Operator-editable stage templates, keyed by stage ("init".."mt3").
        # Seeded from the RTF sources; overrides persist here.
        self.prompt_templates: Dict[str, str] = default_prompt_templates()
        self.prompt_stage: str = "auto"
        """"auto" picks a stage per batch; any stage key pins it."""

        self.tag_source: str = tagmod.TAG_SOURCE_TARGET
        self.global_directive: str = ""
        """Operator's batch-wide instruction, fed into the next run."""

        self.auto_merge_proposals: bool = True
        """Whether model-proposed rules/vibe are merged automatically."""

        self.rule_log: List[Dict[str, str]] = []
        """Audit trail of every automatic rule/vibe change."""

        self.segments: Dict[str, SegmentRecord] = {}
        self.order: List[str] = []
        self.corrections: List[Correction] = []
        self.analysis: Dict[str, str] = {}
        self.detailed_feedback: bool = True

        # Multi-document storage (v5). The public document-scoped attributes
        # above are the *active* document; the others live as JSON-shaped
        # snapshots and are never shared by reference with the active view.
        self.document_id: Optional[str] = None
        """Opaque ID of the active document, or None when unbound."""
        self._documents: Dict[str, dict] = {}
        self._active_extra: Dict[str, object] = {}
        """Unknown keys of the active document's entry, carried verbatim."""
        self._unbound: Optional[dict] = None
        """Unbound work stashed when a document was opened over it."""
        self._extra_top: Dict[str, object] = {}
        """Unknown top-level keys of a v5 file, carried verbatim."""
        self._nested_extra: Dict[int, tuple] = {}
        """id(record or correction) -> (that object, its unknown stored fields)."""

    # ------------------------------------------------------------ persistence

    @classmethod
    def load(cls, path: str) -> "ProjectState":
        state = cls(path)
        if not os.path.exists(path):
            return state
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            # A corrupt state file must not brick the session; keep a copy and
            # start clean so the operator can still work.
            try:
                shutil.copyfile(path, path + ".corrupt")
            except OSError:
                pass
            return state

        state.version = int(data.get("version", 1))
        state._load_globals(data)

        if _is_v5_payload(data):
            documents = data.get("documents") or {}
            state._documents = {
                str(key): copy.deepcopy(entry)
                for key, entry in documents.items() if isinstance(entry, dict)
            }
            unbound = data.get("unbound_document")
            unbound = copy.deepcopy(unbound) if isinstance(unbound, dict) else None
            active = data.get("active_document")
            if isinstance(active, str) and active in state._documents:
                state._apply_document(state._documents[active])
                state.document_id = active
                state._unbound = unbound
            else:
                state._apply_document(unbound or {})
                state.document_id = None
            state._extra_top = {
                key: copy.deepcopy(value) for key, value in data.items()
                if key not in _STRUCTURE_FIELDS + GLOBAL_FIELDS + DOCUMENT_FIELDS
            }
        else:
            # Pre-v5: one document at the top level. It becomes the active
            # "legacy" entry, and the original JSON is kept verbatim inside it
            # so no field - known, unknown or migrated away - is lost. Loading
            # never rewrites the file; the first save() backs it up first.
            state._apply_document(data)
            state._active_extra = {"legacy_original": copy.deepcopy(data)}
            state.document_id = LEGACY_DOCUMENT_ID
        return state

    def _load_globals(self, data: dict) -> None:
        """Global settings; the same keys in every schema version."""
        state = self
        state.batch_size = int(data.get("batch_size", DEFAULT_BATCH_SIZE)) or DEFAULT_BATCH_SIZE
        state.detailed_feedback = bool(data.get("detailed_feedback", True))

        # Empty strings are meaningful ("the operator cleared this box"), so
        # only fall back to the defaults when the key is absent entirely.
        state.vibe = data["vibe"] if "vibe" in data else defaults.DEFAULT_VIBE
        state.hard_rules_text = (
            data["hard_rules_text"] if "hard_rules_text" in data
            else defaults.DEFAULT_HARD_RULES
        )

        state.prompt_stage = data.get("prompt_stage", "auto")
        state.tag_source = data.get("tag_source", tagmod.TAG_SOURCE_TARGET)
        state.auto_merge_proposals = bool(data.get("auto_merge_proposals", True))
        state.rule_log = list(data.get("rule_log", []))
        # v3 -> v4: stage templates. Unknown keys are ignored and missing ones
        # fall back to the shipped default, so adding a stage later is safe.
        stored = data.get("prompt_templates") or {}
        state.prompt_templates = default_prompt_templates()
        for key, text in stored.items():
            if key in state.prompt_templates and isinstance(text, str):
                state.prompt_templates[key] = text

        # v2 -> v3: "hard_rules" used to be the glossary table. It is the
        # termbase now, and hard rules became prose.
        raw_terms = data.get("termbase")
        if raw_terms is None:
            raw_terms = data.get("hard_rules", [])
        state.termbase = [
            TermEntry(**{k: v for k, v in entry.items()
                         if k in ("source", "target", "note")})
            for entry in raw_terms
        ]

    def _apply_document(self, entry: dict) -> None:
        """Make ``entry`` the active document view, building fresh objects.

        Nothing here aliases ``entry``: records, lists and dicts are rebuilt,
        and unknown keys are deep-copied into ``_active_extra``.
        """
        self.source_file = entry.get("source_file", "")
        self.source_path = entry.get("source_path", "")
        self.created_at = entry.get("created_at", _now())
        self.updated_at = entry.get("updated_at", _now())
        self.current_batch = int(entry.get("current_batch", 0))
        self.analysis = dict(entry.get("analysis", {}))
        self.global_directive = entry.get("global_directive", "")
        # Unknown nested fields (e.g. written by a newer harness) are kept
        # aside, tied to the exact object built from that stored item. They go
        # back out only while that same object is still in place, so they can
        # never drift onto another record or a correction at a shifted index.
        self._nested_extra = {}
        self.corrections = [
            self._build(Correction, item, {}) for item in entry.get("corrections", [])
        ]
        self.segments = {}
        for key, record in (entry.get("segments") or {}).items():
            self.segments[key] = self._build(SegmentRecord, record, {"key": key})
        if "order" in entry:
            # Present, even if empty: an empty selection (every row hidden by
            # the filter) is valid and must stay empty.
            self.order = list(entry["order"])
        else:
            # Absent (very old files): rebuild it from the records.
            self.order = sorted(self.segments, key=lambda k: self.segments[k].ordinal)
        self._active_extra = {
            key: copy.deepcopy(value) for key, value in entry.items()
            if key not in DOCUMENT_FIELDS
        }

    def _document_snapshot(self) -> dict:
        """Deep, JSON-shaped copy of the active document, unknown keys kept."""
        snapshot = copy.deepcopy(self._active_extra)
        snapshot.update(json.loads(json.dumps({
            "source_file": self.source_file,
            "source_path": self.source_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "current_batch": self.current_batch,
            "analysis": self.analysis,
            "global_directive": self.global_directive,
            "corrections": [self._dump(c) for c in self.corrections],
            "order": self.order,
            "segments": {k: self._dump(v) for k, v in self.segments.items()},
        }, ensure_ascii=False)))
        return snapshot

    def _build(self, cls, item: dict, defaults_: dict):
        """Construct ``cls`` from known fields only; remember the rest."""
        known = {f.name for f in fields(cls)}
        kwargs = dict(defaults_)
        kwargs.update({k: v for k, v in item.items() if k in known})
        obj = cls(**kwargs)
        extra = {k: copy.deepcopy(v) for k, v in item.items() if k not in known}
        if extra:
            # Holding obj keeps id(obj) from being reused by another object.
            self._nested_extra[id(obj)] = (obj, extra)
        return obj

    def _dump(self, obj) -> dict:
        """Current known fields, over the unknown ones stored for this object."""
        out: dict = {}
        held = self._nested_extra.get(id(obj))
        if held is not None and held[0] is obj:
            out.update(copy.deepcopy(held[1]))
        out.update(asdict(obj))  # current known values always win
        return out

    def _has_document_work(self) -> bool:
        return bool(self.segments or self.order or self.corrections or self.analysis
                    or self.global_directive or self.source_file or self.source_path
                    or self._active_extra)

    # ------------------------------------------------------------- documents

    @property
    def document_ids(self) -> List[str]:
        """IDs of every stored document, the active one included."""
        ids = set(self._documents)
        if self.document_id is not None:
            ids.add(self.document_id)
        return sorted(ids)

    def switch_document(self, document_id: str, source_file: str = "",
                        source_path: str = "") -> None:
        """Make ``document_id`` the active document.

        ``document_id`` is opaque (the caller supplies e.g. a source
        fingerprint). The current document is snapshotted first; a known ID is
        restored, an unknown one starts empty. Global settings are untouched
        and no document-scoped data crosses between IDs. Switching to the
        active ID keeps all in-memory work and only refreshes the file
        metadata. Nothing is written to disk: call :meth:`save`.
        """
        if not isinstance(document_id, str) or not document_id:
            raise ValueError("document_id must be a non-empty string")
        if document_id != self.document_id:
            if self.document_id is not None:
                self._documents[self.document_id] = self._document_snapshot()
            elif self._has_document_work():
                # Work bound to no document is kept aside, never transferred.
                self._unbound = self._document_snapshot()
            entry = self._documents.get(document_id)
            self._apply_document(entry if entry is not None else {})
            self.document_id = document_id
        self.source_file = source_file
        self.source_path = source_path

    def reset_active_document(self) -> None:
        """Clear the active document's working state, and nothing else.

        Keeps the document ID and its file metadata, every other stored
        document, retained legacy/unbound work and all global settings. Refuses
        unbound and legacy state, which have no document of their own to
        reset. Nothing is written to disk: call :meth:`save`.
        """
        if self.document_id is None or self.document_id == LEGACY_DOCUMENT_ID:
            raise ValueError("reset_active_document needs an active document")
        self.segments = {}
        self.order = []
        self.corrections = []
        self.analysis = {}
        self.global_directive = ""
        self.current_batch = 0
        self.created_at = _now()
        self._active_extra = {}
        self._nested_extra = {}

    def reset_active_document_and_save(self) -> str:
        """Reset the active document and persist it, all or nothing.

        If saving fails, the pre-reset active work is restored in memory and
        the error propagates, so a failed reset never looks like a success.
        """
        snapshot = self._document_snapshot()
        self.reset_active_document()
        try:
            return self.save()
        except BaseException:
            self._apply_document(snapshot)
            raise

    # ------------------------------------------------------------------ save

    def save(self) -> str:
        self.updated_at = _now()
        active = self._document_snapshot()
        documents = copy.deepcopy(self._documents)
        if self.document_id is None:
            unbound = active
        else:
            documents[self.document_id] = active
            unbound = copy.deepcopy(self._unbound)
        payload = {
            "version": STATE_VERSION,
            "batch_size": self.batch_size,
            "vibe": self.vibe,
            "hard_rules_text": self.hard_rules_text,
            "detailed_feedback": self.detailed_feedback,
            "prompt_templates": self.prompt_templates,
            "prompt_stage": self.prompt_stage,
            "tag_source": self.tag_source,
            "auto_merge_proposals": self.auto_merge_proposals,
            "rule_log": self.rule_log,
            "termbase": [asdict(t) for t in self.termbase],
            "active_document": self.document_id,
            "documents": documents,
            "unbound_document": unbound,
        }
        for key, value in self._extra_top.items():
            payload.setdefault(key, copy.deepcopy(value))
        # Serialise before touching the disk so a bad value cannot leave a
        # half-written file behind.
        text = json.dumps(payload, ensure_ascii=False, indent=2)

        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # A pre-v5 file is backed up byte for byte before its first overwrite;
        # if that fails, StateBackupError propagates and nothing is written.
        self._backup_legacy_file()
        # Write to a sibling temp file then replace, so an interrupted save
        # cannot leave a half-written state.json behind.
        temp = self.path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, self.path)
        self.version = STATE_VERSION
        return self.path

    def _backup_legacy_file(self) -> Optional[str]:
        """Back up the on-disk state file if it is not in the v5 format.

        Decided from the bytes on disk at save time, not from memory. The
        backup is created with exclusive creation (never overwriting), fsynced
        and read back. An existing backup is reused only if it is byte-for-byte
        identical; otherwise a distinct numbered backup is created. Returns the
        backup path, or None when no backup was needed.
        """
        try:
            with open(self.path, "rb") as handle:
                current = handle.read()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StateBackupError(
                "Cannot read %s to back it up: %s" % (self.path, exc)) from exc
        try:
            data = json.loads(current.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            # Not JSON at all: the existing .corrupt copy made by load() is the
            # safeguard for that case, as before.
            return None
        if _is_v5_payload(data):
            return None

        version = data.get("version", 1) if isinstance(data, dict) else "unknown"
        base = "%s.v%s.bak" % (self.path, version)
        for attempt in range(_MAX_BACKUP_ATTEMPTS):
            candidate = base if attempt == 0 else "%s.%d" % (base, attempt)
            try:
                written = _write_backup(candidate, current)
            except FileExistsError:
                try:
                    with open(candidate, "rb") as handle:
                        if handle.read() == current:
                            return candidate  # a verified identical backup
                except OSError:
                    pass
                continue  # different or unreadable: never trust or overwrite
            except OSError as exc:
                raise StateBackupError(
                    "Could not back up %s to %s: %s" % (self.path, candidate, exc)
                ) from exc
            if not written:
                raise StateBackupError(
                    "Backup %s did not verify; %s was not overwritten."
                    % (candidate, self.path))
            return candidate
        raise StateBackupError(
            "No free backup name for %s; it was not overwritten." % self.path)

    # ---------------------------------------------------------------- binding

    def bind_segments(
        self,
        segments: Sequence,
        source_file: str,
        source_path: str = "",
        present_keys: Optional[set] = None,
    ) -> None:
        """Populate/refresh records for the translatable segments of a document.

        ``segments`` is the *active* selection under the current filter; it
        drives ``order`` and therefore batching and export. Existing human work
        is preserved: when a segment key is already known and its source text is
        unchanged, the stored suggestion/edit survives a reload of the same file.

        A row the filter currently hides is not discarded: ``present_keys`` is
        the identity of every segment in the full document (hidden rows
        included), so only a key the document no longer contains is pruned.
        Filtering changes processing scope without losing saved work.
        """
        self.source_file = source_file
        self.source_path = source_path
        self.order = []
        batch_size = self.batch_size or DEFAULT_BATCH_SIZE

        for index, segment in enumerate(segments):
            key = segment.key
            self.order.append(key)
            batch_index = index // batch_size
            existing = self.segments.get(key)
            if existing and existing.source_text == segment.source_text:
                existing.batch_index = batch_index
                existing.ordinal = index
                existing.mt_target = segment.target_text
                continue
            self.segments[key] = SegmentRecord(
                key=key,
                segment_id=segment.unit_id,
                ordinal=index,
                batch_index=batch_index,
                source_text=segment.source_text,
                mt_target=segment.target_text,
            )

        # A filtered-out row stays saved (merely hidden from processing); only a
        # segment genuinely absent from the document is dropped. When the full
        # document key set is unknown (present_keys is None) fall back to the
        # active order, the historical behaviour.
        if present_keys is not None:
            live = set(present_keys)
        else:
            live = set(self.order)
        for stale in [k for k in self.segments if k not in live]:
            del self.segments[stale]

    def rebatch(self, batch_size: int) -> None:
        """Re-slice segments after the operator changes the batch size."""
        self.batch_size = max(1, int(batch_size))
        for index, key in enumerate(self.order):
            record = self.segments.get(key)
            if record:
                record.ordinal = index
                record.batch_index = index // self.batch_size
        self.current_batch = min(self.current_batch, max(0, self.batch_count - 1))

    # --------------------------------------------------------------- batching

    @property
    def batch_count(self) -> int:
        if not self.order:
            return 0
        size = self.batch_size or DEFAULT_BATCH_SIZE
        return (len(self.order) + size - 1) // size

    def batch_keys(self, batch_index: int) -> List[str]:
        size = self.batch_size or DEFAULT_BATCH_SIZE
        start = batch_index * size
        return self.order[start : start + size]

    def batch_records(self, batch_index: int) -> List[SegmentRecord]:
        return [self.segments[k] for k in self.batch_keys(batch_index) if k in self.segments]

    def batch_progress(self, batch_index: int) -> Dict[str, int]:
        records = self.batch_records(batch_index)
        return {
            "total": len(records),
            "processed": sum(1 for r in records if r.status in ("processed", "applied")),
            "applied": sum(1 for r in records if r.status == "applied"),
            "edited": sum(1 for r in records if r.edited_by_human),
        }

    # --------------------------------------------------------- feedback loop

    def record_suggestions(self, suggestions: Dict[str, str], tag_reports: Optional[Dict[str, str]] = None) -> int:
        """Store model output for a batch and pre-fill the editable field."""
        tag_reports = tag_reports or {}
        count = 0
        for key, text in suggestions.items():
            record = self.segments.get(key)
            if record is None:
                continue
            record.llm_suggestion = text
            record.tag_report = tag_reports.get(key, "")
            # Only pre-fill when the human has not already typed something, so
            # re-running a batch never silently discards their work.
            if not record.edited_by_human:
                record.user_final = text
            record.status = "processed"
            count += 1
        return count

    def capture_edits(self, edits: Dict[str, str], comments: Optional[Dict[str, str]] = None) -> None:
        """Pull the grid's current values into the state records.

        A captured revision that is non-empty and differs from the baseline
        (the model's suggestion, or memoQ's MT when that is empty) is marked as
        human-edited so a later ``record_suggestions`` pass cannot overwrite it.
        This mirrors ``calculate_diffs``: a comment on otherwise-unchanged text
        is not an edit, and an empty field clears no flag.
        """
        comments = comments or {}
        for key, text in edits.items():
            record = self.segments.get(key)
            if record is None:
                continue
            final = (text or "").strip()
            baseline = (record.llm_suggestion or "").strip() or (record.mt_target or "").strip()
            if final and final != baseline:
                record.edited_by_human = True
            record.user_final = text
            record.user_comment = comments.get(key, record.user_comment)

    def calculate_diffs(self, batch_index: int) -> List[Correction]:
        """Turn this batch's human edits into correction records.

        A row counts as human-edited when the final text differs from what the
        model proposed - that difference is exactly the learning signal.  Rows
        the human left alone are implicit approval and carry no signal, so they
        are not injected into the next prompt.
        """
        produced: List[Correction] = []
        for record in self.batch_records(batch_index):
            final = (record.user_final or "").strip()
            proposed = (record.llm_suggestion or "").strip()
            if not final:
                record.status = "applied"
                continue

            baseline = proposed or (record.mt_target or "").strip()
            if final != baseline or record.user_comment.strip():
                record.edited_by_human = final != baseline
                produced.append(
                    Correction(
                        segment_id=record.segment_id,
                        source=record.source_text,
                        llm_suggestion=proposed,
                        human_final=final,
                        comment=record.user_comment.strip(),
                        batch_index=batch_index,
                    )
                )
            record.status = "applied"

        # Replace any earlier corrections from this batch so re-applying a
        # batch updates rather than duplicates its feedback.
        self.corrections = [c for c in self.corrections if c.batch_index != batch_index]
        self.corrections.extend(produced)
        return produced

    def corrections_for_prompt(self, upto_batch: int, limit: int = 40) -> List[Correction]:
        """Most recent corrections from batches before ``upto_batch``.

        Newest first and capped, because prompt budget is finite and the
        operator's latest preferences are the ones worth spending it on.
        """
        eligible = [c for c in self.corrections if c.batch_index < upto_batch]
        eligible.sort(key=lambda c: c.batch_index, reverse=True)
        return eligible[:limit]

    def feedback_block(self, upto_batch: int, limit: int = 40) -> str:
        """The rendered ICL block for the next batch's system prompt."""
        items = self.corrections_for_prompt(upto_batch, limit)
        if not items:
            return ""
        return "\n".join(c.render(self.detailed_feedback) for c in reversed(items))

    # -------------------------------------------------------- rules & termbase

    def reset_hard_rules(self) -> None:
        self.hard_rules_text = defaults.DEFAULT_HARD_RULES

    def reset_vibe(self) -> None:
        self.vibe = defaults.DEFAULT_VIBE

    # -------------------------------------------- model-proposed rule merge

    AUTO_RULES_HEADING = "Правила, добавленные автоматически по ходу перевода:"

    def merge_proposals(
        self,
        rules: Sequence[str],
        vibe_note: str = "",
        batch_index: int = 0,
    ) -> List[str]:
        """Fold model-proposed rules and vibe into the live blocks.

        Appended under a clearly marked heading rather than woven into the
        operator's own prose, so machine-authored rules stay visually separable
        and can be deleted in one go. Every change is recorded in
        :attr:`rule_log`: this mutates the document's governing rules without a
        human in the loop, so it has to be auditable and reversible.

        Duplicates are dropped by exact text, and anything already present
        anywhere in the rulebook is skipped.
        """
        applied: List[str] = []
        existing = self.hard_rules_text

        fresh = []
        for rule in rules:
            text = " ".join(str(rule).split()).strip(" -—\t")
            if not text or text in existing or text in fresh:
                continue
            fresh.append(text)

        if fresh:
            block = self.hard_rules_text.rstrip("\n")
            if self.AUTO_RULES_HEADING not in block:
                block += "\n\n" + self.AUTO_RULES_HEADING
            block += "\n" + "\n".join("- %s" % r for r in fresh)
            self.hard_rules_text = block + "\n"
            applied.extend(fresh)
            self.rule_log.append({
                "at": _now(),
                "batch": str(batch_index + 1),
                "kind": "rules",
                "detail": " | ".join(fresh),
            })

        note = " ".join(vibe_note.split()).strip()
        if note and note not in self.vibe:
            self.vibe = self.vibe.rstrip("\n") + "\n" + note + "\n"
            applied.append(note)
            self.rule_log.append({
                "at": _now(),
                "batch": str(batch_index + 1),
                "kind": "vibe",
                "detail": note,
            })

        return applied

    def revert_auto_rules(self) -> None:
        """Strip the automatically appended rule block and clear its log."""
        if self.AUTO_RULES_HEADING in self.hard_rules_text:
            head, _, _ = self.hard_rules_text.partition(self.AUTO_RULES_HEADING)
            self.hard_rules_text = head.rstrip("\n") + "\n"
        self.rule_log = []

    # ------------------------------------------------------- prompt stages

    def template_for(self, stage_key: str) -> str:
        return self.prompt_templates.get(stage_key, "")

    def set_template(self, stage_key: str, text: str) -> None:
        if stage_key in self.prompt_templates:
            self.prompt_templates[stage_key] = text

    def reset_template(self, stage_key: str) -> None:
        stage = templates_mod.STAGE_BY_KEY.get(stage_key)
        if stage:
            self.prompt_templates[stage_key] = stage.template

    def reset_all_templates(self) -> None:
        self.prompt_templates = default_prompt_templates()

    def active_stage(self, batch_index: int) -> str:
        """Stage key for a batch, honouring an explicit operator override."""
        if self.prompt_stage and self.prompt_stage != "auto":
            return self.prompt_stage
        # Imported here to keep prompts -> state the only import direction.
        from . import prompts as prompts_mod

        return prompts_mod.stage_for_batch(batch_index, self.batch_count)

    # ------------------------------------------------------------ rules TXT

    def rules_export_text(self) -> str:
        """Everything the operator has authored, as one plain-text document.

        Deliberately plain: this is a record to paste into a chat, mail to a
        colleague or diff against last week's, not a re-importable format.
        """
        lines: List[str] = [
            "memoQ LLM Harness - rules export",
            "Generated: %s" % _now(),
            "Document:  %s" % (self.source_file or "(none loaded)"),
            "Batches:   %d of %d segments each" % (self.batch_count, self.batch_size),
            "",
            "=" * 72,
            "HARD RULES",
            "=" * 72,
            "",
            self.hard_rules_text.strip(),
            "",
            "=" * 72,
            "VIBE / SOFT RULES",
            "=" * 72,
            "",
            self.vibe.strip(),
            "",
            "=" * 72,
            "TERMBASE (%d entries)" % len(self.termbase),
            "=" * 72,
            "",
        ]
        lines.extend("%s -> %s" % (t.source, t.target) for t in self.termbase)

        lines += ["", "=" * 72, "PROMPT TEMPLATES", "=" * 72, ""]
        for stage in templates_mod.STAGES:
            text = self.prompt_templates.get(stage.key, "")
            modified = "" if text == stage.template else "  (edited)"
            lines += ["-" * 72, "[%s] %s%s" % (stage.key, stage.label, modified),
                      "-" * 72, "", text.strip(), ""]

        if self.corrections:
            lines += ["=" * 72,
                      "HUMAN CORRECTIONS (%d)" % len(self.corrections),
                      "=" * 72, ""]
            lines.extend(c.render(detailed=True) for c in self.corrections)
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def export_rules_txt(self, directory: str) -> str:
        """Write :meth:`rules_export_text` to a timestamped file."""
        os.makedirs(directory, exist_ok=True)
        name = "rules_export_%s.txt" % datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(self.rules_export_text())
        return path

    def load_termbase_csv(
        self,
        data: bytes | str,
        source_col: int = MEMOQ_SOURCE_COL,
        target_col: int = MEMOQ_TARGET_COL,
        skip_rows: int = 1,
        replace: bool = True,
    ) -> "TermbaseLoadReport":
        """Load a memoQ termbase CSV export by *column position*.

        memoQ's export carries no meaningful header: row 0 reads
        ``English,English,English,English,English,Russian,Russian`` - one column
        per synonym, per language. There is nothing to match names against, so
        columns are addressed positionally: column 0 is the primary source term,
        column 5 the primary target term.

        Robustness matters more than strictness here - a 1000-row termbase that
        silently drops rows is worse than useless:

        * encoding is sniffed (UTF-8 with/without BOM, UTF-16 LE/BE, cp1251),
          because memoQ's export encoding depends on the host locale;
        * the delimiter is sniffed from the header row (``,`` ``;`` or tab);
        * parsing goes through :mod:`csv`, so quoted fields containing commas
          *or embedded line breaks* stay intact - splitting on newlines by hand
          is what usually corrupts these files;
        * CR, LF and CRLF are all accepted.

        Returns a :class:`TermbaseLoadReport` so the UI can show what was
        skipped rather than quietly losing entries.
        """
        text, encoding = _decode_csv(data)
        # newline="" lets csv handle embedded line breaks inside quoted fields.
        handle = io.StringIO(text, newline="")
        delimiter = _sniff_delimiter(text)

        report = TermbaseLoadReport(encoding=encoding, delimiter=delimiter)
        parsed: List[TermEntry] = []
        seen: Dict[str, int] = {}

        for index, row in enumerate(csv.reader(handle, delimiter=delimiter)):
            if index < skip_rows:
                report.header_rows += 1
                continue
            report.data_rows += 1
            if not any(cell.strip() for cell in row):
                report.blank_rows += 1
                continue

            source = row[source_col].strip() if source_col < len(row) else ""
            target = row[target_col].strip() if target_col < len(row) else ""
            if not source or not target:
                report.incomplete_rows += 1
                continue

            # Collapse internal whitespace/newlines so a term stays one line in
            # the prompt even if the export wrapped it.
            source, target = _squash(source), _squash(target)
            # Dedup on the exact source, NOT case-folded. Real memoQ termbases
            # distinguish by case on purpose - "Connect to an Account" is a UI
            # heading ("Подключение к счету") while "connect to an account" is
            # a verb phrase ("подключиться к счету"). Folding case would throw
            # one of them away. Lookup stays case-insensitive, so both are
            # offered and the model picks by context.
            if source in seen:
                report.duplicate_rows += 1
                continue
            seen[source] = len(parsed)
            parsed.append(TermEntry(source=source, target=target))

        report.loaded = len(parsed)
        if replace:
            self.termbase = parsed
        else:
            existing = {t.source for t in self.termbase}
            self.termbase.extend(t for t in parsed if t.source not in existing)
        return report

    def termbase_csv(self) -> str:
        """Export the termbase in the same positional shape memoQ produces."""
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["English"] * MEMOQ_TARGET_COL + ["Russian", "Russian"])
        for entry in self.termbase:
            row = [""] * (MEMOQ_TARGET_COL + 2)
            row[MEMOQ_SOURCE_COL] = entry.source
            row[MEMOQ_TARGET_COL] = entry.target
            writer.writerow(row)
        return buffer.getvalue()

    def relevant_terms(self, texts: Iterable[str], limit: int = 120) -> List[TermEntry]:
        """Termbase entries whose source term actually occurs in the batch.

        A 1000-entry termbase costs more tokens than the segments themselves and
        buries the handful of terms that apply. Matching is whole-word (a plain
        substring test would fire ``side`` on "consider"), case-aware for
        proper nouns and UI labels, and prefers the most specific term where
        several nest inside one another.
        """
        blob = "\n".join(texts)
        if not blob.strip():
            return []

        hits: List[TermEntry] = []
        for entry in self.termbase:
            term = entry.source.strip()
            if term and _term_pattern(term).search(blob):
                hits.append(entry)

        return _drop_subsumed(hits)[:limit]

    # ----------------------------------------------------------------- stats

    def totals(self) -> Dict[str, int]:
        records = list(self.segments.values())
        return {
            "segments": len(records),
            "batches": self.batch_count,
            "processed": sum(1 for r in records if r.status in ("processed", "applied")),
            "applied": sum(1 for r in records if r.status == "applied"),
            "edited": sum(1 for r in records if r.edited_by_human),
            "corrections": len(self.corrections),
        }


def _drop_subsumed(hits: Sequence[TermEntry]) -> List[TermEntry]:
    """Keep the most specific term where matches nest inside one another.

    "order filling type" and "filling type" can both be in a termbase with
    different translations ("тип исполнения ордера" vs "тип заливки"). When
    both match the same text the longer one is the one the author meant, and
    sending both invites the model to pick the false friend. Same for "Magic"
    inside "magic number".
    """
    kept: List[TermEntry] = []
    for entry in hits:
        term = entry.source.strip()
        if any(
            other is not entry
            and len(other.source) > len(term)
            and _term_pattern(term).search(other.source)
            for other in hits
        ):
            continue
        kept.append(entry)
    return kept
