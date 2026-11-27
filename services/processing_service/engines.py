"""Engines: the deterministic offline stub and server-configured real models.

Both expose the interface of :class:`memoq_harness.llm.LLMClient` that the
app already uses: ``translate_batch(records, system_prompt)`` returning a
``BatchResult`` and ``translate_one(record, system_prompt)`` returning
``(text or None, raw)``.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from memoq_harness import llm, prompts
from memoq_harness.state import SegmentRecord

from .config import ENGINE_ANTHROPIC, ENGINE_STUB, EngineSettings

STUB_PREFIX = "MT "


class EngineUnavailable(RuntimeError):
    """The configured engine cannot be used (e.g. its SDK is not installed)."""


class StubClient:
    """Deterministic, offline, free. The default engine.

    Rule, consistent with the request's ``tag_source``: take the placeholder
    text of the canonical side - memoQ's target (``mt_target``) in "target"
    mode, the source in "source" mode - falling back to the other side when
    the canonical side is empty, and prefix it with ``"MT "``. Using the
    canonical side keeps the placeholders the tag check expects in the usual
    case; a row whose canonical side has text but no tags while the other
    side has tags will show a tag warning, exactly as a real model output
    without tags would. Output may equal what a human would also write; it
    is not guaranteed to differ from ``mt_target``.
    """

    name = ENGINE_STUB

    def __init__(self, tag_source: str):
        self.tag_source = tag_source

    def text_for(self, record: SegmentRecord) -> str:
        if self.tag_source == "source":
            canonical, other = record.source_text, record.mt_target
        else:
            canonical, other = record.mt_target, record.source_text
        base = canonical if canonical.strip() else other
        return STUB_PREFIX + base if base.strip() else ""

    def translate_batch(self, records: Sequence[SegmentRecord], system_prompt: str,
                        progress=None) -> llm.BatchResult:
        # Build the user message as the real path does, so prompt assembly is
        # exercised offline too; the stub itself ignores it.
        prompts.build_user_payload(records)
        result = llm.BatchResult()
        for record in records:
            text = self.text_for(record)
            if text:
                result.suggestions[record.key] = text
        result.missing = [r.key for r in records if r.key not in result.suggestions]
        result.requests = 1
        return result

    def translate_one(self, record: SegmentRecord,
                      system_prompt: str) -> Tuple[Optional[str], str]:
        prompts.build_user_payload([record])
        return (self.text_for(record) or None), ""

    def analyze_corrections(self, corrections, hard_rules_text: str = "",
                            expected_tags=None) -> str:
        """Deterministic offline critique for the stub engine.

        Mirrors the real ``llm.LLMClient.analyze_corrections`` contract: an
        empty string when there are no corrections, otherwise a critique. It
        builds the same payload the real path does (so prompt assembly is
        exercised offline), but, being the offline stub, it never runs a model:
        it returns a minimal translation brief whose heading lets the UI split
        it into ``(warnings, brief)`` with no warnings. The stub asserts no
        style rule, so it cannot harden a defect into one.
        """
        recent = list(corrections)
        if not recent:
            return ""
        prompts.build_analysis_payload(recent, hard_rules_text, expected_tags)
        return ("TRANSLATION BRIEF\n"
                "Stub analysis of %d correction(s): no consistent style "
                "pattern is asserted offline." % len(recent))


def sdk_available(settings: EngineSettings) -> bool:
    if settings.kind == ENGINE_STUB:
        return True
    if settings.kind == ENGINE_ANTHROPIC:
        return llm.anthropic is not None
    return llm.OpenAI is not None


def sdk_available_for_config(client_config) -> bool:
    """Whether the SDK for a per-request config's backend is importable.

    The per-request model config names a backend (``openai`` or ``anthropic``),
    never the stub, so the stub's always-True branch is absent here.
    """
    if client_config.backend == ENGINE_ANTHROPIC:
        return llm.anthropic is not None
    return llm.OpenAI is not None


def real_client(settings: EngineSettings) -> llm.LLMClient:
    """An LLMClient for the configured engine. Never called for the stub."""
    if not sdk_available(settings):
        raise EngineUnavailable("The SDK for the configured engine is not installed.")
    config = llm.ClientConfig(
        preset="processing-service",
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        chunk_size=settings.chunk_size,
        timeout=settings.timeout,
        max_retries=settings.max_retries,
        backend=llm.BACKEND_ANTHROPIC if settings.kind == ENGINE_ANTHROPIC else llm.BACKEND_OPENAI,
    )
    try:
        return llm.LLMClient(config)
    except llm.LLMError:
        raise EngineUnavailable("The configured engine could not be initialised.") from None
