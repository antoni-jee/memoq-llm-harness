"""OpenAI-compatible client layer.

Anything that speaks the OpenAI chat-completions protocol works here: the
OpenAI API itself, OpenRouter, DeepSeek, or a local Ollama/llama.cpp/LM Studio
server.  Only the base URL, key and model name change, so the presets below are
just starting values for three text fields.

Two behaviours matter for local models in particular:

* ``response_format={"type": "json_object"}`` is an OpenAI extension that many
  local servers reject outright.  The client probes once, remembers the answer,
  and falls back to plain prompting rather than failing the batch.
* Small local models return *almost* JSON - fenced, prefixed with prose, or
  truncated.  :func:`extract_json` is deliberately forgiving.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import prompts
from .state import Correction, SegmentRecord

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]

try:
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None  # type: ignore[assignment]

BACKEND_OPENAI = "openai"
BACKEND_ANTHROPIC = "anthropic"


@dataclass(frozen=True)
class Preset:
    """A starting configuration for one kind of endpoint."""

    label: str
    base_url: str
    default_model: str
    requires_key: bool = True
    hint: str = ""
    model_suggestions: Tuple[str, ...] = ()
    backend: str = BACKEND_OPENAI
    """Which SDK speaks to this endpoint - the OpenAI one or Anthropic's."""


def ollama_base_url() -> str:
    """Where this machine's Ollama actually lives.

    ``OLLAMA_HOST`` is the variable Ollama's own CLI honours, and it is what a
    tunnelled setup sets - e.g. a Windows VM pointed at the macOS host over
    Parallels holds ``10.211.55.2:11434`` there. Defaulting blindly to
    localhost would leave exactly that setup unable to connect.
    """
    host = (os.environ.get("OLLAMA_HOST") or "").strip()
    if not host:
        return "http://localhost:11434/v1"
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    return host.rstrip("/") + "/v1"


PRESETS: Dict[str, Preset] = {
    "OpenAI API": Preset(
        label="OpenAI API",
        # The public API base is api.openai.com/v1 - openai.com is the website
        # and returns HTML, not a chat-completions endpoint.
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o",
        requires_key=True,
        hint="Standard OpenAI endpoint. Point base URL at your own deployment if you host one.",
        model_suggestions=("gpt-4o", "gpt-4o-mini", "gpt-4.1", "o4-mini"),
    ),
    "Anthropic API": Preset(
        label="Anthropic API",
        base_url="https://api.anthropic.com",
        # Requested default. Newer, more capable models are listed first in the
        # suggestions below and can be picked straight from the dropdown.
        default_model="claude-3-5-sonnet-latest",
        requires_key=True,
        backend=BACKEND_ANTHROPIC,
        hint=(
            "Native Anthropic Messages API via the 'anthropic' SDK - not an "
            "OpenAI-compatible shim. The system prompt is sent as a top-level "
            "field and JSON is coaxed with an assistant prefill."
        ),
        model_suggestions=(
            "claude-opus-5",
            "claude-sonnet-5",
            "claude-haiku-4-5-20251001",
            "claude-3-5-sonnet-latest",
        ),
    ),
    "OpenRouter": Preset(
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        default_model="anthropic/claude-sonnet-4.5",
        requires_key=True,
        hint="Aggregator - model names are namespaced, e.g. 'openai/gpt-4o'.",
        model_suggestions=(
            "anthropic/claude-sonnet-4.5",
            "openai/gpt-4o",
            "deepseek/deepseek-chat",
            "qwen/qwen-2.5-72b-instruct",
        ),
    ),
    "DeepSeek": Preset(
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
        requires_key=True,
        hint="DeepSeek's own endpoint.",
        model_suggestions=("deepseek-chat", "deepseek-reasoner"),
    ),
    "Local Engine (Ollama)": Preset(
        label="Local Engine (Ollama)",
        base_url=ollama_base_url(),
        default_model="qwen2.5:32b-instruct",
        requires_key=False,
        hint=(
            "Ollama's OpenAI-compatible endpoint, taken from OLLAMA_HOST when set. "
            "The API key is ignored but the SDK requires a non-empty string, so a "
            "placeholder is sent automatically."
        ),
        model_suggestions=(
            "qwen2.5:32b-instruct",
            "qwen2.5:72b-instruct",
            "gemma2:27b",
            "mistral-small",
            "llama3.3:70b",
        ),
    ),
}

DEFAULT_PRESET = "Local Engine (Ollama)"

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


@dataclass
class ClientConfig:
    """Everything needed to make a call."""

    preset: str = DEFAULT_PRESET
    base_url: str = PRESETS[DEFAULT_PRESET].base_url
    api_key: str = ""
    model: str = PRESETS[DEFAULT_PRESET].default_model
    temperature: float = 0.2
    max_tokens: int = 4096
    chunk_size: int = 20
    timeout: float = 300.0
    max_retries: int = 3
    backend: str = BACKEND_OPENAI

    def resolved_key(self) -> str:
        # The SDK refuses to construct without a key; local servers ignore it.
        return self.api_key.strip() or "not-needed"

    @property
    def is_anthropic(self) -> bool:
        return self.backend == BACKEND_ANTHROPIC


class LLMError(RuntimeError):
    """A call failed in a way the operator needs to see."""


@dataclass
class BatchResult:
    """Outcome of translating one batch."""

    suggestions: Dict[str, str] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    requests: int = 0
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return bool(self.suggestions) and not self.missing


class LLMClient:
    """Thin wrapper over the OpenAI SDK with batching and JSON repair."""

    def __init__(self, config: ClientConfig):
        self.config = config
        self._supports_json_mode: Optional[bool] = None

        if config.is_anthropic:
            if anthropic is None:
                raise LLMError(
                    "The 'anthropic' package is not installed. "
                    "Run: pip install anthropic"
                )
            base_url = config.base_url.strip().rstrip("/") or None
            self._client = anthropic.Anthropic(
                api_key=config.api_key.strip() or None,
                base_url=base_url,
                timeout=config.timeout,
                max_retries=0,
            )
        else:
            if OpenAI is None:
                raise LLMError(
                    "The 'openai' package is not installed. Run: pip install openai"
                )
            self._client = OpenAI(
                base_url=config.base_url.strip().rstrip("/") or None,
                api_key=config.resolved_key(),
                timeout=config.timeout,
                max_retries=0,  # retries handled here so we can adapt the request
            )

    # ------------------------------------------------------------ diagnostics

    def list_models(self) -> List[str]:
        """Model ids the endpoint advertises (Ollama lists locally pulled tags)."""
        try:
            response = self._client.models.list()
        except Exception as exc:  # noqa: BLE001
            raise LLMError("Could not list models: %s" % _clean_error(exc)) from exc
        data = getattr(response, "data", None) or []
        return sorted(model.id for model in data)

    def ping(self) -> str:
        """Smallest possible round trip, for the sidebar's test button."""
        start = time.time()
        text = self._chat(
            [
                {"role": "system", "content": "Reply with the single word: ok"},
                {"role": "user", "content": "ping"},
            ],
            force_json=False,
            max_tokens=16,
        )
        return "%s (%.0f ms)" % (text.strip()[:40] or "(empty reply)", (time.time() - start) * 1000)

    # -------------------------------------------------------------- inference

    def translate_batch(
        self,
        records: Sequence[SegmentRecord],
        system_prompt: str,
        progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> BatchResult:
        """Translate a batch, chunked into several requests.

        A 100-segment batch is split because one oversized request risks the
        model truncating its JSON halfway and costing the whole batch; smaller
        chunks fail independently and are retried on their own.
        """
        result = BatchResult()
        start = time.time()
        chunk_size = max(1, self.config.chunk_size)
        chunks = [records[i : i + chunk_size] for i in range(0, len(records), chunk_size)]

        for index, chunk in enumerate(chunks):
            if progress:
                progress(index, len(chunks), "Translating %d segments" % len(chunk))
            try:
                parsed = self._translate_chunk(chunk, system_prompt)
                result.suggestions.update(parsed)
            except LLMError as exc:
                result.errors.append("Chunk %d/%d: %s" % (index + 1, len(chunks), exc))
            result.requests += 1

        # One focused retry for anything the model skipped.
        missing = [r for r in records if r.key not in result.suggestions]
        if missing and not result.errors:
            if progress:
                progress(len(chunks), len(chunks) + 1, "Retrying %d skipped" % len(missing))
            for chunk in [missing[i : i + 5] for i in range(0, len(missing), 5)]:
                try:
                    result.suggestions.update(self._translate_chunk(chunk, system_prompt))
                except LLMError as exc:
                    result.errors.append("Retry: %s" % exc)
                result.requests += 1

        result.missing = [r.key for r in records if r.key not in result.suggestions]
        result.elapsed = time.time() - start
        return result

    def _translate_chunk(
        self, records: Sequence[SegmentRecord], system_prompt: str
    ) -> Dict[str, str]:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompts.build_user_payload(records)},
        ]
        raw = self._chat(messages, force_json=True)
        data = extract_json(raw)
        if data is None:
            raise LLMError("Model did not return usable JSON (%d chars)." % len(raw))

        valid = {r.key for r in records}
        out: Dict[str, str] = {}
        for item in _iter_segments(data):
            key = str(item.get("id", "")).strip()
            target = item.get("target")
            if key in valid and isinstance(target, str):
                out[key] = target
        return out

    def analyze_corrections(self, corrections: Sequence[Correction], limit: int = 40) -> str:
        """Summarise what the reviewer's edits reveal about their preferences."""
        if not corrections:
            return ""
        recent = list(corrections)[-limit:]
        messages = [
            {"role": "system", "content": prompts.ANALYSIS_SYSTEM},
            {"role": "user", "content": prompts.build_analysis_payload(recent)},
        ]
        return self._chat(messages, force_json=False, max_tokens=900).strip()

    # ----------------------------------------------------------------- plumbing

    def _chat(
        self,
        messages: List[Dict[str, str]],
        force_json: bool,
        max_tokens: Optional[int] = None,
    ) -> str:
        if self.config.is_anthropic:
            return self._chat_anthropic(messages, force_json, max_tokens)
        return self._chat_openai(messages, force_json, max_tokens)

    def _chat_anthropic(
        self,
        messages: List[Dict[str, str]],
        force_json: bool,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Anthropic Messages API.

        Three structural differences from the OpenAI shape are handled here:
        the system prompt is a top-level field rather than a message,
        ``max_tokens`` is mandatory, and there is no ``response_format``. JSON
        is obtained instead by prefilling the assistant turn with ``{``, which
        constrains the model far more reliably than asking politely - the
        prefill is stitched back onto the reply before parsing.
        """
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m["role"] != "system"
        ]
        if not turns:
            turns = [{"role": "user", "content": system or "Hello"}]

        prefill = force_json
        if prefill:
            turns = turns + [{"role": "assistant", "content": "{"}]

        # Anthropic requires a positive max_tokens; there is no "server decides".
        limit = self.config.max_tokens if max_tokens is None else max_tokens
        limit = limit if limit and limit > 0 else 4096

        last_error: Optional[Exception] = None
        for attempt in range(max(1, self.config.max_retries)):
            try:
                response = self._client.messages.create(
                    model=self.config.model,
                    system=system or anthropic.NOT_GIVEN,
                    messages=turns,
                    max_tokens=limit,
                    temperature=self.config.temperature,
                )
            except Exception as exc:  # noqa: BLE001 - SDK raises many types
                last_error = exc
                if attempt + 1 < self.config.max_retries and _is_retryable(str(exc)):
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise LLMError(_clean_error(exc)) from exc

            text = "".join(
                block.text for block in getattr(response, "content", [])
                if getattr(block, "type", "") == "text"
            )
            if prefill:
                # Give back what the model would have written unprompted.
                text = "{" + text
            if text.strip():
                return text
            last_error = LLMError("Model returned an empty response.")

        raise LLMError(_clean_error(last_error) if last_error else "Request failed.")

    def _chat_openai(
        self,
        messages: List[Dict[str, str]],
        force_json: bool,
        max_tokens: Optional[int] = None,
    ) -> str:
        last_error: Optional[Exception] = None
        want_json = force_json and self._supports_json_mode is not False

        for attempt in range(max(1, self.config.max_retries)):
            kwargs: Dict[str, object] = {
                "model": self.config.model,
                "messages": messages,
                "temperature": self.config.temperature,
            }
            limit = self.config.max_tokens if max_tokens is None else max_tokens
            if limit and limit > 0:
                kwargs["max_tokens"] = limit
            if want_json:
                kwargs["response_format"] = {"type": "json_object"}

            try:
                response = self._client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001 - SDK raises many types
                message = str(exc)
                if want_json and _is_json_mode_rejection(message):
                    # Endpoint does not implement JSON mode. Remember it for the
                    # rest of the session and retry this call plainly.
                    self._supports_json_mode = False
                    want_json = False
                    continue
                if _is_param_rejection(message, "max_tokens"):
                    # Some endpoints renamed or dropped it; let the server default.
                    self.config.max_tokens = 0
                    max_tokens = 0
                    continue
                last_error = exc
                if attempt + 1 < self.config.max_retries and _is_retryable(message):
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise LLMError(_clean_error(exc)) from exc

            if want_json:
                self._supports_json_mode = True
            choice = response.choices[0] if response.choices else None
            content = (choice.message.content if choice and choice.message else "") or ""
            if content.strip():
                return content
            last_error = LLMError("Model returned an empty response.")

        raise LLMError(_clean_error(last_error) if last_error else "Request failed.")


# ------------------------------------------------------------------ helpers


def _iter_segments(data: object):
    """Yield segment dicts from the several shapes models actually return."""
    if isinstance(data, dict):
        for field_name in ("segments", "translations", "results", "data", "items"):
            value = data.get(field_name)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        yield item
                return
        # A bare {id: target} mapping.
        if all(isinstance(v, str) for v in data.values()) and data:
            for key, value in data.items():
                yield {"id": key, "target": value}
            return
        yield data
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield item


def extract_json(raw: str) -> Optional[object]:
    """Pull a JSON value out of a model response that may be wrapped in prose."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()

    for candidate in _json_candidates(text):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    # Last resort: salvage individual objects from a truncated array.
    salvaged = []
    for match in re.finditer(r"\{[^{}]*\"target\"\s*:\s*\"(?:[^\"\\]|\\.)*\"[^{}]*\}", text, re.DOTALL):
        try:
            salvaged.append(json.loads(match.group(0)))
        except json.JSONDecodeError:
            continue
    return salvaged or None


def _json_candidates(text: str) -> List[str]:
    candidates: List[str] = []
    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())
    candidates.append(text)
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if 0 <= start < end:
            candidates.append(text[start : end + 1])
    return candidates


def _is_json_mode_rejection(message: str) -> bool:
    lowered = message.lower()
    return "response_format" in lowered or (
        "json" in lowered and any(w in lowered for w in ("unsupported", "not supported", "invalid"))
    )


def _is_param_rejection(message: str, param: str) -> bool:
    lowered = message.lower()
    return param in lowered and any(
        w in lowered for w in ("unsupported", "not supported", "unrecognized", "unknown")
    )


def _is_retryable(message: str) -> bool:
    lowered = message.lower()
    return any(
        token in lowered
        for token in ("timeout", "timed out", "rate limit", "429", "500", "502", "503", "504",
                      "overloaded", "connection", "temporarily")
    )


def _clean_error(exc: object) -> str:
    """Trim SDK exception noise down to something a sidebar can display."""
    text = str(exc).strip()
    if len(text) > 400:
        text = text[:400] + "..."
    return text or exc.__class__.__name__
