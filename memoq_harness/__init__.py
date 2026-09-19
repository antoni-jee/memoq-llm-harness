"""memoQ LLM Harness - hybrid translation/post-editing pipeline for .mqxliff.

Layers, bottom up:

``xmlcompat``  XML backend shim (lxml preferred, ElementTree fallback)
``tags``       inline memoQ tag isolation/restoration
``mqxliff``    document parsing, segment filtering, re-export
``state``      state.json, batching, the human-correction feedback loop
``prompts``    system/user prompt assembly
``llm``        OpenAI-compatible client with endpoint presets

``app.py`` at the repository root is the Streamlit UI and holds no logic that
is not about presentation.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["llm", "mqxliff", "prompts", "state", "tags", "xmlcompat"]
