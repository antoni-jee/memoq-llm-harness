"""Shared, app-agnostic UI helpers for the Streamlit front end.

These helpers are used by *both* the local path and the remote read-only path,
so they live below both instead of in ``app.py``: that keeps the extracted
remote modules free of any back-dependency on ``app`` while leaving the local
path's behaviour byte-for-byte unchanged. ``app.py`` re-imports every name
here, so nothing in its namespace (or in the local behaviour) changes.

They know nothing of ``memoq_harness`` or ``harness_remote`` - only of
Streamlit and its session state - so this module stays importable without the
transport package.
"""

from __future__ import annotations

import hashlib
from typing import Optional

import streamlit as st

APP_TITLE = "memoQ LLM Harness"


def notify(kind: str, message: str) -> None:
    """Queue a message that survives the ``st.rerun`` following an action."""
    st.session_state.notice = (kind, message)


def flush_notice() -> None:
    notice = st.session_state.pop("notice", None)
    if not notice:
        return
    kind, message = notice
    {"success": st.success, "warning": st.warning, "error": st.error}.get(kind, st.info)(message)


def upload_marker(name: str, data: bytes) -> str:
    """Identity of one uploaded file *version*: its name plus its exact bytes.

    Deliberately not the source fingerprint: a target-only change keeps the
    fingerprint but is still a new file that must be loaded. Two different
    files sharing a basename get different markers.
    """
    digest = hashlib.sha256()
    digest.update(name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(data)
    return digest.hexdigest()


def upload_needs_load(marker: Optional[str], last_marker: Optional[str]) -> bool:
    """Load only when the uploader holds a file version not yet loaded.

    Streamlit keeps returning the same upload on every rerun, so reloading it
    each time would discard unsaved widget edits.
    """
    return marker is not None and marker != last_marker


def _display(text: str) -> str:
    """Escape markdown so segment text renders literally, tags included."""
    if not text:
        return ""
    for char in ("\\", "*", "_", "`", "[", "]", "#", "$"):
        text = text.replace(char, "\\" + char)
    return text.replace("\n", " ")
