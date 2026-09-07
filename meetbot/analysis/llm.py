"""LLM provider adapters for the post-meeting analysis step.

Both adapters expose the same one-shot JSON interface,
:meth:`LLMClient.complete_json`, so :mod:`meetbot.analysis.summarize` never
branches on the provider. Each provider uses its own native structured-output
mechanism, so the response is schema-valid JSON rather than free text we have
to scrape.

Provider SDKs are imported lazily inside the constructors: a deployment that
only ever uses Claude should not need ``openai`` installed, and the unit tests
need neither.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: Non-streaming default. Comfortably covers an analysis document while
#: staying inside the SDK's default HTTP timeout.
DEFAULT_MAX_TOKENS = 16_000


class LLMError(RuntimeError):
    """Raised when an LLM call fails or returns something unusable."""


@runtime_checkable
class LLMClient(Protocol):
    """Minimal provider interface used by the analysis module."""

    model: str

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str = "response",
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> dict[str, Any]:
        """Return the model's response parsed as a JSON object.

        Args:
            system: System prompt.
            user: User message content.
            schema: JSON Schema the response must conform to.
            schema_name: Name for the schema, where the provider requires one.
            max_tokens: Output token cap.

        Raises:
            LLMError: On API failure or unparseable output.
        """
        ...

    def complete_text(
        self, *, system: str, user: str, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> str:
        """Return the model's response as plain text.

        Raises:
            LLMError: On API failure.
        """
        ...


class AnthropicClient:
    """Claude adapter built on the official ``anthropic`` SDK.

    Uses structured outputs (``output_config.format``) so the response is
    guaranteed-valid JSON matching the requested schema.
    """

    def __init__(self, api_key: str, model: str, *, timeout_s: float = 300.0) -> None:
        """Create the client.

        Args:
            api_key: Anthropic API key.
            model: Model id, e.g. ``"claude-opus-5"``.
            timeout_s: Per-request timeout in seconds.

        Raises:
            LLMError: If the ``anthropic`` package is not installed.
        """
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - import guard
            raise LLMError(
                "The 'anthropic' package is required for LLM_PROVIDER=anthropic. "
                "Install it with: pip install anthropic"
            ) from exc

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout_s)
        self.model = model

    def _call(self, **kwargs: Any) -> Any:
        anthropic = self._anthropic
        try:
            return self._client.messages.create(model=self.model, **kwargs)
        except anthropic.AuthenticationError as exc:
            raise LLMError("Anthropic rejected the API key (401)") from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(f"Unknown Anthropic model {self.model!r} (404)") from exc
        except anthropic.RateLimitError as exc:
            retry_after = "unknown"
            response = getattr(exc, "response", None)
            if response is not None:
                retry_after = response.headers.get("retry-after", "unknown")
            raise LLMError(
                f"Anthropic rate limit hit (retry-after: {retry_after}s)"
            ) from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(
                f"Anthropic API error {exc.status_code}: {exc.message}"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Could not reach the Anthropic API: {exc}") from exc

    @staticmethod
    def _first_text(response: Any) -> str:
        for block in response.content:
            if getattr(block, "type", None) == "text":
                return block.text
        raise LLMError("Anthropic response contained no text block")

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str = "response",
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> dict[str, Any]:
        """See :meth:`LLMClient.complete_json`."""
        response = self._call(
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            raise LLMError(f"Claude declined to answer (stop_details={details})")
        text = self._first_text(response)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Claude returned invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise LLMError("Claude returned JSON that is not an object")
        return parsed

    def complete_text(
        self, *, system: str, user: str, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> str:
        """See :meth:`LLMClient.complete_text`."""
        response = self._call(
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            raise LLMError(f"Claude declined to answer (stop_details={details})")
        return self._first_text(response)


class OpenAIClient:
    """GPT adapter built on the official ``openai`` SDK.

    Kept deliberately separate from :class:`AnthropicClient` rather than
    unified behind a compatibility shim, so each provider uses its own
    structured-output and error-handling surface.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = "",
        timeout_s: float = 300.0,
    ) -> None:
        """Create the client.

        Args:
            api_key: OpenAI API key, or the key for the compatible provider
                named by ``base_url``.
            model: Model id, e.g. ``"gpt-4o"``.
            base_url: Override the API endpoint. Any OpenAI-compatible
                provider (Groq, OpenRouter, a local Ollama) can be driven
                through this adapter. Blank uses the SDK default.
            timeout_s: Per-request timeout in seconds.

        Raises:
            LLMError: If the ``openai`` package is not installed.
        """
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - import guard
            raise LLMError(
                "The 'openai' package is required for LLM_PROVIDER=openai. "
                "Install it with: pip install openai"
            ) from exc

        self._openai = openai
        self._client = openai.OpenAI(
            api_key=api_key, base_url=base_url or None, timeout=timeout_s
        )
        self.model = model

    def _call(self, **kwargs: Any) -> Any:
        openai = self._openai
        try:
            return self._client.chat.completions.create(model=self.model, **kwargs)
        except openai.AuthenticationError as exc:
            raise LLMError("OpenAI rejected the API key (401)") from exc
        except openai.NotFoundError as exc:
            raise LLMError(f"Unknown OpenAI model {self.model!r} (404)") from exc
        except openai.RateLimitError as exc:
            raise LLMError(f"OpenAI rate limit hit: {exc}") from exc
        except openai.APIStatusError as exc:
            raise LLMError(f"OpenAI API error {exc.status_code}: {exc}") from exc
        except openai.APIConnectionError as exc:
            raise LLMError(f"Could not reach the OpenAI API: {exc}") from exc

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str = "response",
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> dict[str, Any]:
        """See :meth:`LLMClient.complete_json`."""
        response = self._call(
            max_completion_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
        )
        content = response.choices[0].message.content
        if not content:
            raise LLMError("OpenAI returned an empty response")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"OpenAI returned invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise LLMError("OpenAI returned JSON that is not an object")
        return parsed

    def complete_text(
        self, *, system: str, user: str, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> str:
        """See :meth:`LLMClient.complete_text`."""
        response = self._call(
            max_completion_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content = response.choices[0].message.content
        if not content:
            raise LLMError("OpenAI returned an empty response")
        return content


def probe_llm(client: LLMClient) -> str:
    """Confirm the LLM credentials, model id and endpoint actually work.

    The summary is produced *after* the meeting ends, so a bad key or a
    retired model id would otherwise surface at the worst possible moment -
    with the recording already over. One trivial completion up front turns
    that into a startup error.

    ``max_tokens`` is deliberately generous rather than minimal: reasoning
    models (Groq's ``openai/gpt-oss-120b`` among them) spend tokens on
    internal reasoning before emitting any content, and a tight budget makes
    them return an empty string - which would look exactly like a failure.

    Returns:
        A short human-readable detail string for logging.

    Raises:
        LLMError: If the key, model, or endpoint is unusable.
    """
    reply = client.complete_text(
        system="You are a connectivity check. Reply with the single word OK.",
        user="Reply with OK.",
        max_tokens=256,
    )
    return f"key accepted, {getattr(client, 'model', '?')} replied {reply.strip()[:20]!r}"


def build_client(
    provider: str, api_key: str, model: str, base_url: str = ""
) -> LLMClient:
    """Instantiate the adapter for ``provider``.

    Args:
        provider: ``"anthropic"`` or ``"openai"``.
        api_key: Provider API key.
        model: Model id.
        base_url: Endpoint override, honoured by the ``"openai"`` provider
            only - it is the adapter whose wire format other vendors
            implement. Ignored for ``"anthropic"``.

    Raises:
        LLMError: For an unknown provider, a missing key, or a missing SDK.
    """
    if not api_key:
        raise LLMError(f"No API key configured for provider {provider!r}")
    if provider == "anthropic":
        return AnthropicClient(api_key, model)
    if provider == "openai":
        return OpenAIClient(api_key, model, base_url=base_url)
    raise LLMError(
        f"Unknown LLM provider {provider!r}; expected 'anthropic' or 'openai'"
    )
