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
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from . import prompts
from .state import Correction, SegmentRecord

try:
    from openai import OpenAI, OpenAIError
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]
    OpenAIError = None  # type: ignore[assignment]

try:
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None  # type: ignore[assignment]

# Base classes of every error the two SDKs raise for transport, HTTP status and
# response validation. The model Test probe catches exactly these - not
# arbitrary exceptions, so programming errors still surface.
_SDK_ERRORS: Tuple[type, ...] = tuple(
    cls for cls in (OpenAIError, getattr(anthropic, "AnthropicError", None))
    if isinstance(cls, type)
)

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

#: Appended to the Anthropic system prompt when assistant prefill is unavailable.
JSON_ONLY_CONSTRAINT = (
    "\n\nCRITICAL: Return ONLY a valid, raw JSON object. Do not include "
    "markdown wraps, triple backticks, introduction, or trailing "
    "conversational text."
)

#: Model-name fragments whose builds reject a prefilled assistant turn with a
#: 400. Observed in production on the newer Claude releases; older models keep
#: the prefill, which constrains JSON far more tightly than wording does.
PREFILL_INCOMPATIBLE = ("4-5", "4-6", "sonnet-5", "claude-5", "opus-5", "haiku-5")


def _rejects_prefill(model: str) -> bool:
    lowered = (model or "").lower()
    return any(fragment in lowered for fragment in PREFILL_INCOMPATIBLE)


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
    raw_responses: List[str] = field(default_factory=list)
    """Un-parsed model output, one entry per request, for the inspector."""

    proposed_rules: List[str] = field(default_factory=list)
    """New editorial rules the model offered, if any."""

    proposed_vibe: str = ""
    """A refinement to the article's stylistic direction, if offered."""

    def raw_text(self) -> str:
        """Every raw response joined with a separator per request."""
        if not self.raw_responses:
            return ""
        if len(self.raw_responses) == 1:
            return self.raw_responses[0]
        return "\n\n".join(
            "----- request %d of %d -----\n%s" % (i + 1, len(self.raw_responses), raw)
            for i, raw in enumerate(self.raw_responses)
        )

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
        """Kept for callers of the old name; see :func:`probe_model`."""
        return probe_model(self.config).message()

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
                parsed, raw = self._translate_chunk(chunk, system_prompt)
                result.suggestions.update(parsed)
                result.raw_responses.append(raw)
                _collect_proposals(raw, result)
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
                    parsed, raw = self._translate_chunk(chunk, system_prompt)
                    result.suggestions.update(parsed)
                    result.raw_responses.append(raw)
                except LLMError as exc:
                    result.errors.append("Retry: %s" % exc)
                result.requests += 1

        result.missing = [r.key for r in records if r.key not in result.suggestions]
        result.elapsed = time.time() - start
        return result

    def _translate_chunk(
        self, records: Sequence[SegmentRecord], system_prompt: str
    ) -> Tuple[Dict[str, str], str]:
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
        return out, raw

    def translate_one(
        self,
        record: SegmentRecord,
        system_prompt: str,
    ) -> Tuple[Optional[str], str]:
        """Re-run inference for a single segment.

        Used by the per-row Retry button when one row comes back empty, times
        out or trips a content filter. It is deliberately a separate path from
        :meth:`translate_batch`: the caller patches only this row, so the other
        segments in the batch keep the output the operator has already reviewed.

        Returns ``(text_or_None, raw_response)``. The raw text is handed back
        even on a parse failure so the inspector can show what arrived.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompts.build_user_payload([record])},
        ]
        raw = self._chat(messages, force_json=True)
        data = extract_json(raw)
        if data is None:
            return None, raw

        for item in _iter_segments(data):
            target = item.get("target")
            if isinstance(target, str) and target.strip():
                # A single-segment response is unambiguous, so accept the one
                # target that came back even if the model echoed a different id.
                if str(item.get("id", "")).strip() in ("", record.key):
                    return target, raw
                return target, raw
        return None, raw

    def analyze_corrections(
        self,
        corrections: Sequence[Correction],
        limit: int = 40,
        hard_rules_text: str = "",
        expected_tags: Optional[Dict[str, List[int]]] = None,
    ) -> str:
        """Critique the reviewer's edits, then summarise their preferences.

        The rulebook and the expected tag sets are passed through so the
        analyser can actually check the human's work rather than assume it is
        correct - see ``prompts.ANALYSIS_SYSTEM``.
        """
        if not corrections:
            return ""
        recent = list(corrections)[-limit:]
        messages = [
            {"role": "system", "content": prompts.ANALYSIS_SYSTEM},
            {"role": "user", "content": prompts.build_analysis_payload(
                recent, hard_rules_text, expected_tags)},
        ]
        return self._chat(messages, force_json=False, max_tokens=1600).strip()

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

        Four structural differences from the OpenAI shape are handled here:

        * the system prompt is a top-level field, not a message;
        * ``max_tokens`` is mandatory - there is no "server decides";
        * there is no ``response_format``, so JSON is obtained by prefilling
          the assistant turn with ``{``, which constrains the model far more
          reliably than asking politely; the prefill is stitched back onto the
          reply before parsing;
        * **``temperature`` is not accepted.** ``messages.create()`` has no
          such parameter in the current SDK, so passing it raises ``TypeError``
          before the request is ever sent. Sampling is left to the server
          default. The sidebar slider still governs every OpenAI-compatible
          preset, where it works normally.
        """
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m["role"] != "system"
        ]
        if not turns:
            turns = [{"role": "user", "content": system or "Hello"}]

        prefill = force_json and not _rejects_prefill(self.config.model)
        if prefill:
            turns = turns + [{"role": "assistant", "content": "{"}]
        elif force_json:
            # No prefill available: constrain the format in words instead.
            system = (system + JSON_ONLY_CONSTRAINT) if system else JSON_ONLY_CONSTRAINT.strip()

        # Anthropic requires a positive max_tokens; there is no "server decides".
        limit = self.config.max_tokens if max_tokens is None else max_tokens
        limit = limit if limit and limit > 0 else 4096

        last_error: Optional[Exception] = None
        for attempt in range(max(1, self.config.max_retries)):
            try:
                # Deliberately no temperature here - see the docstring.
                response = self._client.messages.create(
                    model=self.config.model,
                    system=system or anthropic.NOT_GIVEN,
                    messages=turns,
                    max_tokens=limit,
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


# ------------------------------------------------------------------ probe

PROBE_TIMEOUT = 30.0
"""Upper bound, in seconds, for the sidebar Test request."""

PROBE_MAX_TOKENS = 16
"""Output budget for the Test request. Always sent; never dropped."""

PROBE_PREVIEW_CHARS = 40

PROBE_MESSAGES: Tuple[Dict[str, str], ...] = (
    {"role": "system", "content": "Reply with the single word: ok"},
    {"role": "user", "content": "ping"},
)
"""The fixed synthetic probe. No document text, rules or corrections."""


PROBE_OK = "ok"
PROBE_INCONCLUSIVE = "inconclusive"

CREDENTIAL_UNDETERMINED = "SDK-managed credentials; source not determined"


@dataclass
class ProbeResult:
    """What one Test probe established - and, by omission, what it did not.

    Metadata fields are optional: endpoints differ in what they report, and a
    missing field is normal. Reasoning text is only ever measured, not kept.
    """

    status: str
    requested_model: str
    endpoint: str
    credential_source: str
    elapsed_ms: float
    preview: str = ""
    server_model: str = ""
    finish_reason: str = ""
    output_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    reasoning_present: bool = False
    reasoning_chars: int = 0

    @property
    def ok(self) -> bool:
        return self.status == PROBE_OK

    def message(self) -> str:
        """Markdown summary for the sidebar notice. Never contains credentials."""
        if self.ok:
            lines = ["Model test: the endpoint returned visible text in %.0f ms: %s"
                     % (self.elapsed_ms, _md_code(self.preview))]
        else:
            lines = [
                "Model test inconclusive: the endpoint returned a well-formed "
                "response with no visible text within the %d-token probe "
                "(%.0f ms). Text generation was not confirmed; this does not "
                "mean the model is unavailable. Test does not retry."
                % (PROBE_MAX_TOKENS, self.elapsed_ms)
            ]
        lines.append("Requested model: %s" % _md_code(self.requested_model))
        lines.append("Server-reported model: %s" % (
            _md_code(self.server_model) if self.server_model else "not reported"))
        details = []
        if self.finish_reason:
            details.append("finish reason %s" % _md_code(self.finish_reason))
        if self.output_tokens is not None:
            details.append("%d output tokens" % self.output_tokens)
        if self.reasoning_tokens is not None:
            details.append("%d reasoning tokens" % self.reasoning_tokens)
        if self.reasoning_present:
            details.append("reasoning text present (%d chars, not shown)"
                           % self.reasoning_chars)
        if details:
            lines.append("Response metadata: %s" % ", ".join(details))
        lines.append("Endpoint: %s" % _md_code(self.endpoint))
        lines.append("Credentials: %s" % self.credential_source)
        if self.ok:
            lines.append("This only shows that text came back; translation "
                         "quality and JSON output were not checked.")
        return "  \n".join(lines)


def sanitize_url(url: str) -> str:
    """Scheme, host, port and path only: no userinfo, query or fragment."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return "(unparseable URL)"
    if not parts.scheme or not host:
        return "(unparseable URL)"
    if ":" in host:
        host = "[%s]" % host
    netloc = host + (":%d" % port if port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def display_endpoint(config: ClientConfig) -> str:
    """The configured endpoint, sanitized for display."""
    return sanitize_url(config.base_url) or "SDK default endpoint; not determined here"


def credential_source(config: ClientConfig) -> str:
    """Where the request's credential comes from - a label, never a value.

    The OpenAI SDK is always handed a key (the placeholder when the field is
    empty), so it never falls back to the environment. The Anthropic SDK is
    handed ``None`` for an empty field and then resolves credentials itself:
    ``ANTHROPIC_API_KEY`` first, then ``ANTHROPIC_AUTH_TOKEN``, then profile or
    federation sources this harness does not inspect.
    """
    if config.api_key.strip():
        return "sidebar key"
    if not config.is_anthropic:
        return "none - local placeholder key (sidebar key empty)"
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.get(name):
            return "%s environment variable (sidebar key empty)" % name
    return "%s (sidebar key empty)" % CREDENTIAL_UNDETERMINED


def connection_summary(config: ClientConfig) -> str:
    """Markdown lines shown next to the Test button."""
    return "  \n".join((
        "Endpoint: %s" % _md_code(display_endpoint(config)),
        "Requested model: %s" % _md_code(config.model),
        "Credentials: %s" % credential_source(config),
    ))


def probe_model(config: ClientConfig) -> ProbeResult:
    """One bounded generation request against the selected endpoint and model.

    Backs the sidebar Test button. Unlike the translation path it makes exactly
    one request: no harness or SDK retries, no JSON-mode or ``max_tokens``
    fallback, and a timeout of at most :data:`PROBE_TIMEOUT`. It works on a
    private copy of ``config`` and never writes to the original.

    Returns a :class:`ProbeResult` - ``ok`` for visible text, ``inconclusive``
    for a well-formed reply without any. Transport, API and response-shape
    failures raise :class:`LLMError`.
    """
    probe_config = replace(
        config,
        timeout=min(float(config.timeout), PROBE_TIMEOUT),
        max_tokens=PROBE_MAX_TOKENS,
        max_retries=1,
    )
    client = LLMClient(probe_config)  # SDK clients are built with max_retries=0
    system, user = PROBE_MESSAGES[0]["content"], PROBE_MESSAGES[1]["content"]

    start = time.time()
    try:
        if probe_config.is_anthropic:
            response = client._client.messages.create(
                model=probe_config.model,
                system=system,
                messages=[{"role": "user", "content": user}],
                max_tokens=PROBE_MAX_TOKENS,
            )
        else:
            response = client._client.chat.completions.create(
                model=probe_config.model,
                messages=[dict(m) for m in PROBE_MESSAGES],
                temperature=probe_config.temperature,
                max_tokens=PROBE_MAX_TOKENS,
            )
    except _SDK_ERRORS as exc:
        raise LLMError(_probe_failure(exc, probe_config.timeout)) from exc
    except TypeError as exc:
        # The Anthropic SDK signals a missing key with a bare TypeError at
        # request time. Anything else is a genuine bug and must propagate.
        if "resolve authentication method" not in str(exc):
            raise
        raise LLMError(
            "No Anthropic API key: enter one in the sidebar or set "
            "ANTHROPIC_API_KEY. Test does not retry."
        ) from exc

    result = ProbeResult(
        status=PROBE_INCONCLUSIVE,
        requested_model=probe_config.model,
        endpoint=_client_endpoint(client) or display_endpoint(probe_config),
        credential_source=credential_source(probe_config),
        elapsed_ms=(time.time() - start) * 1000,
    )
    text = _read_probe_reply(response, probe_config.is_anthropic, result)
    preview = " ".join(text.split())
    if preview:
        if len(preview) > PROBE_PREVIEW_CHARS:
            preview = preview[:PROBE_PREVIEW_CHARS] + "..."
        result.status = PROBE_OK
        result.preview = preview
    return result


def _client_endpoint(client: "LLMClient") -> str:
    """The base URL the SDK client actually resolved, sanitized; '' if unknown."""
    value = getattr(client._client, "base_url", None)
    return sanitize_url(str(value)) if value is not None else ""


def _md_code(text: object) -> str:
    cleaned = " ".join(str(text).replace("`", "'").split())
    return "`%s`" % cleaned if cleaned else "`(empty)`"


def _str_field(obj: object, name: str) -> str:
    value = getattr(obj, name, None)
    return value if isinstance(value, str) else ""


def _count_field(obj: object, name: str) -> Optional[int]:
    value = getattr(obj, name, None)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _read_probe_reply(response: object, is_anthropic: bool, result: ProbeResult) -> str:
    """Visible text of a probe reply, filling optional metadata into ``result``.

    Raises :class:`LLMError` only when the required shape is wrong; optional
    metadata that is missing or oddly typed is simply left unset.
    """
    malformed = LLMError(
        "The endpoint answered, but not in the expected %s response format "
        "(got %s). Check the base URL and preset. Test does not retry." % (
            "Anthropic Messages" if is_anthropic else "OpenAI chat-completions",
            type(response).__name__,
        )
    )
    if is_anthropic:
        blocks = getattr(response, "content", None)
        if not isinstance(blocks, (list, tuple)):
            raise malformed
        result.server_model = _str_field(response, "model")
        result.finish_reason = _str_field(response, "stop_reason")
        result.output_tokens = _count_field(getattr(response, "usage", None), "output_tokens")
        texts = []
        for block in blocks:
            kind = _str_field(block, "type")
            if kind == "text":
                texts.append(_str_field(block, "text"))
            elif kind in ("thinking", "redacted_thinking"):
                result.reasoning_present = True
                result.reasoning_chars += len(_str_field(block, "thinking"))
        return "".join(texts)

    choices = getattr(response, "choices", None)
    if not isinstance(choices, (list, tuple)) or not choices:
        raise malformed
    message = getattr(choices[0], "message", None)
    if message is None:
        raise malformed
    content = getattr(message, "content", None)
    if content is not None and not isinstance(content, str):
        raise malformed
    result.server_model = _str_field(response, "model")
    result.finish_reason = _str_field(choices[0], "finish_reason")
    usage = getattr(response, "usage", None)
    result.output_tokens = _count_field(usage, "completion_tokens")
    result.reasoning_tokens = _count_field(
        getattr(usage, "completion_tokens_details", None), "reasoning_tokens")
    # Non-standard fields some servers (Ollama, DeepSeek, vLLM) add for
    # thinking models. Measured only; the text itself is never displayed.
    for name in ("reasoning_content", "reasoning"):
        reasoning = _str_field(message, name)
        if reasoning:
            result.reasoning_present = True
            result.reasoning_chars = len(reasoning)
            break
    return content or ""


def _probe_failure(exc: Exception, timeout: float) -> str:
    """Operator-facing explanation of an SDK error raised by the probe."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    message = str(exc)
    if "APITimeoutError" in names:
        lead = "No reply within %.0f s (the Test probe timeout)." % timeout
    elif "APIConnectionError" in names:
        lead = ("Could not connect to the endpoint. Check the base URL and "
                "that the server is running.")
    elif "APIResponseValidationError" in names:
        lead = ("The endpoint answered, but not in the expected response "
                "format. Check the base URL and preset.")
    elif "AuthenticationError" in names or "PermissionDeniedError" in names:
        lead = "The endpoint rejected the credentials."
    elif "max_tokens" in message.lower() and (
            "BadRequestError" in names or _is_param_rejection(message, "max_tokens")):
        lead = ("The endpoint rejected the probe's output-token limit "
                "(max_tokens=%d)." % PROBE_MAX_TOKENS)
    elif "NotFoundError" in names:
        lead = "Endpoint path or model not found. Check the base URL and model name."
    elif "RateLimitError" in names:
        lead = "The endpoint rate-limited the request."
    elif "InternalServerError" in names or "OverloadedError" in names:
        lead = "The endpoint returned a server error."
    else:
        lead = "The Test request failed."
    return "%s Test does not retry. Details: %s" % (lead, _clean_error(exc))


# ------------------------------------------------------------------ helpers


def _collect_proposals(raw: str, result: BatchResult) -> None:
    """Harvest ``proposed_rules`` / ``proposed_vibe`` from a raw response.

    Tolerant by design: a model that omits the keys, or emits them as a bare
    string instead of a list, must not break a batch that otherwise succeeded.
    """
    data = extract_json(raw)
    if not isinstance(data, dict):
        return

    rules = data.get("proposed_rules")
    if isinstance(rules, str):
        rules = [rules]
    if isinstance(rules, list):
        for rule in rules:
            text = str(rule).strip()
            if text and text not in result.proposed_rules:
                result.proposed_rules.append(text)

    vibe = data.get("proposed_vibe")
    if isinstance(vibe, str) and vibe.strip():
        result.proposed_vibe = vibe.strip()


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
