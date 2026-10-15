"""LLM provider adapters for the post-meeting analysis step.

Every adapter exposes the same one-shot JSON interface,
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

#: Google serves the OpenAI wire format here, so Gemini rides the OpenAI
#: adapter instead of needing its own SDK. The compatibility is not total -
#: see :class:`GeminiClient` for the two places it runs out.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


class LLMError(RuntimeError):
    """Raised when an LLM call fails or returns something unusable."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        """Create the error.

        Args:
            message: What went wrong, phrased for whoever has to fix it.
            status_code: HTTP status behind it, where there was one. It lets a
                caller tell "this endpoint rejected the shape of my request"
                (400) from "it is busy" (429) without matching on prose.
        """
        super().__init__(message)
        self.status_code = status_code


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

    Other vendors implement this wire format without implementing all of it,
    so the two places they diverge are class attributes rather than literals -
    see :class:`GeminiClient`.
    """

    #: Named in error messages, so a Gemini failure does not read as an OpenAI
    #: one to somebody who never configured OpenAI.
    _label = "OpenAI"

    #: Field carrying the output cap. OpenAI renamed it from ``max_tokens``;
    #: the endpoints that copied this format did not all follow.
    _max_tokens_field = "max_completion_tokens"

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
        label = self._label
        try:
            return self._client.chat.completions.create(model=self.model, **kwargs)
        except openai.AuthenticationError as exc:
            raise LLMError(
                f"{label} rejected the API key (401)", status_code=401
            ) from exc
        except openai.NotFoundError as exc:
            raise LLMError(
                f"Unknown {label} model {self.model!r} (404)", status_code=404
            ) from exc
        except openai.RateLimitError as exc:
            raise LLMError(f"{label} rate limit hit: {exc}", status_code=429) from exc
        except openai.APIStatusError as exc:
            raise LLMError(
                f"{label} API error {exc.status_code}: {exc}",
                status_code=getattr(exc, "status_code", None),
            ) from exc
        except openai.APIConnectionError as exc:
            raise LLMError(f"Could not reach the {label} API: {exc}") from exc

    def _tokens(self, max_tokens: int) -> dict[str, int]:
        """The output cap, under the field name this endpoint understands."""
        return {self._max_tokens_field: max_tokens}

    def _json_format(
        self, schema: dict[str, Any], schema_name: str
    ) -> dict[str, Any]:
        """The ``response_format`` that asks for schema-valid JSON."""
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": schema,
            },
        }

    def _parse_json(self, response: Any) -> dict[str, Any]:
        """The response body as a JSON object, or an :class:`LLMError`."""
        content = response.choices[0].message.content
        if not content:
            raise LLMError(f"{self._label} returned an empty response")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"{self._label} returned invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise LLMError(f"{self._label} returned JSON that is not an object")
        return parsed

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
            **self._tokens(max_tokens),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format=self._json_format(schema, schema_name),
        )
        return self._parse_json(response)

    def complete_text(
        self, *, system: str, user: str, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> str:
        """See :meth:`LLMClient.complete_text`."""
        response = self._call(
            **self._tokens(max_tokens),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content = response.choices[0].message.content
        if not content:
            raise LLMError(f"{self._label} returned an empty response")
        return content


def _plain_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``schema`` without the keywords Gemini refuses.

    Its schema dialect is a subset of OpenAPI rather than JSON Schema, and
    ``additionalProperties`` - which both of our schemas set - is rejected
    outright. Dropping it loosens validation and changes nothing else: the
    caller parses what comes back and ignores fields it did not ask for.
    """
    dropped = ("additionalProperties", "$schema")
    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key in dropped:
            continue
        if isinstance(value, dict):
            cleaned[key] = _plain_schema(value)
        elif isinstance(value, list):
            cleaned[key] = [
                _plain_schema(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            cleaned[key] = value
    return cleaned


def _schema_instruction(schema: dict[str, Any]) -> str:
    """Prompt text standing in for a schema the endpoint would not accept."""
    return (
        "Reply with a single JSON object and nothing else - no prose, no code "
        "fence. It must conform to this JSON Schema:\n"
        f"{json.dumps(_plain_schema(schema), indent=2)}"
    )


class GeminiClient(OpenAIClient):
    """Gemini, through Google's OpenAI-compatible endpoint.

    Google serves this wire format at :data:`GEMINI_BASE_URL`, so the whole
    OpenAI adapter is reused and only the incomplete parts are replaced. A
    Gemini key therefore buys the notes, the enhancement and the chat with no
    new dependency: the ``openai`` package talks to it unchanged.

    **The output cap is ``max_tokens``.** OpenAI's newer
    ``max_completion_tokens`` is not understood here.

    **Strict JSON schemas are not accepted.** ``strict`` and
    ``additionalProperties`` are both refused, so neither is sent; and if the
    endpoint turns down ``json_schema`` altogether, the request is retried in
    plain JSON mode with the schema written into the prompt. Losing schema
    enforcement beats losing the feature, because the caller validates what
    comes back either way.
    """

    _label = "Gemini"
    _max_tokens_field = "max_tokens"

    def _json_format(
        self, schema: dict[str, Any], schema_name: str
    ) -> dict[str, Any]:
        """See :meth:`OpenAIClient._json_format`. Without ``strict``."""
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": _plain_schema(schema),
            },
        }

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str = "response",
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> dict[str, Any]:
        """See :meth:`LLMClient.complete_json`.

        Falls back to JSON mode when the endpoint rejects the schema, which it
        does with a 400. Any other failure - a bad key, a rate limit, an
        unknown model - is raised as it stands, because retrying would spend
        the same quota to be told the same thing.
        """
        try:
            return super().complete_json(
                system=system,
                user=user,
                schema=schema,
                schema_name=schema_name,
                max_tokens=max_tokens,
            )
        except LLMError as exc:
            if exc.status_code != 400:
                raise
            logger.info(
                "Gemini would not take the JSON schema (%s); asking for plain "
                "JSON instead",
                exc,
            )
        response = self._call(
            **self._tokens(max_tokens),
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": f"{user}\n\n{_schema_instruction(schema)}",
                },
            ],
            response_format={"type": "json_object"},
        )
        return self._parse_json(response)


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
        provider: ``"anthropic"``, ``"openai"`` or ``"gemini"``.
        api_key: Provider API key.
        model: Model id.
        base_url: Endpoint override, honoured by the adapters built on the
            OpenAI wire format - it is the one other vendors implement.
            ``"gemini"`` defaults to :data:`GEMINI_BASE_URL`; a base URL
            overrides it, which is how a proxy in front of Gemini works.
            Ignored for ``"anthropic"``.

    Raises:
        LLMError: For an unknown provider, a missing key, or a missing SDK.
    """
    if not api_key:
        raise LLMError(f"No API key configured for provider {provider!r}")
    if provider == "anthropic":
        return AnthropicClient(api_key, model)
    if provider == "openai":
        return OpenAIClient(api_key, model, base_url=base_url)
    if provider == "gemini":
        return GeminiClient(api_key, model, base_url=base_url or GEMINI_BASE_URL)
    raise LLMError(
        f"Unknown LLM provider {provider!r}; "
        "expected 'anthropic', 'openai' or 'gemini'"
    )
