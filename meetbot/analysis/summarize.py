"""LLM-generated post-meeting analysis.

Given a finished transcript, produce a summary, key discussion points, action
items (with an owner where the transcript makes one inferable) and an overall
sentiment reading.

Two paths, chosen by transcript length:

* **Single pass** - the whole transcript goes into one request.
* **Map-reduce** - long meetings are chunked, each chunk is condensed into
  notes, and the notes are analysed together. This keeps very long meetings
  inside a model's practical context without silently truncating anything.

The analysis step is *best effort by design*: the raw transcript is already on
disk before this module runs, and every failure path here leaves it there.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from meetbot.analysis.llm import LLMClient, LLMError
from meetbot.transcript.format import render_for_llm
from meetbot.transcript.store import Utterance, speakers

logger = logging.getLogger(__name__)

#: Transcripts longer than this (characters) go through map-reduce.
MAX_SINGLE_PASS_CHARS = 120_000

#: Character budget per chunk in the map phase.
CHUNK_CHARS = 40_000

#: Cap on the condensed notes fed into the reduce phase.
MAX_NOTES_CHARS = 100_000

SENTIMENTS = ("positive", "neutral", "mixed", "negative", "tense")

_SYSTEM_PROMPT = (
    "You are an experienced meeting analyst. You are given a transcript of a "
    "work meeting produced by automatic speech recognition.\n\n"
    "Work only from what the transcript actually says. Do not invent "
    "decisions, owners, dates, or numbers. The transcript may contain "
    "misrecognised words, missing speech, and generic speaker labels such as "
    "'Speaker 0' - when a speaker's real name is used in conversation you may "
    "attribute to that name, but do not guess. If the transcript is too short "
    "or too garbled to support a conclusion, say so plainly rather than "
    "padding."
)

_ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": (
                "A concise prose summary of the meeting, 3-8 sentences. What "
                "was discussed, what was decided, where things landed."
            ),
        },
        "key_points": {
            "type": "array",
            "description": "The main discussion points, most important first.",
            "items": {"type": "string"},
        },
        "action_items": {
            "type": "array",
            "description": (
                "Concrete follow-up tasks stated or clearly implied in the "
                "meeting. Empty if none were agreed."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "The task, phrased as an imperative.",
                    },
                    "owner": {
                        "type": ["string", "null"],
                        "description": (
                            "Who owns it, if the transcript makes that clear. "
                            "Null when unassigned - do not guess."
                        ),
                    },
                    "due": {
                        "type": ["string", "null"],
                        "description": (
                            "Any deadline mentioned, verbatim. Null if none."
                        ),
                    },
                },
                "required": ["description", "owner", "due"],
                "additionalProperties": False,
            },
        },
        "sentiment": {
            "type": "string",
            "enum": list(SENTIMENTS),
            "description": "Overall tone of the meeting.",
        },
        "sentiment_rationale": {
            "type": "string",
            "description": "One or two sentences justifying the sentiment.",
        },
    },
    "required": [
        "summary",
        "key_points",
        "action_items",
        "sentiment",
        "sentiment_rationale",
    ],
    "additionalProperties": False,
}


class AnalysisError(RuntimeError):
    """Raised when the analysis could not be produced."""


@dataclass(frozen=True)
class ActionItem:
    """A follow-up task extracted from the meeting."""

    description: str
    owner: str | None = None
    due: str | None = None

    def render(self) -> str:
        """Render as a Markdown checklist line."""
        owner = self.owner or "Unassigned"
        suffix = f" _(due: {self.due})_" if self.due else ""
        return f"- [ ] **{owner}** - {self.description}{suffix}"


@dataclass(frozen=True)
class MeetingAnalysis:
    """The structured output of the analysis step."""

    summary: str
    key_points: list[str] = field(default_factory=list)
    action_items: list[ActionItem] = field(default_factory=list)
    sentiment: str = "neutral"
    sentiment_rationale: str = ""
    model: str = ""
    provider: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise to plain JSON-compatible types."""
        return {
            **asdict(self),
            "action_items": [asdict(item) for item in self.action_items],
        }


def _coerce_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _coerce_optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    # Models sometimes fill these fields with a placeholder instead of null.
    if not cleaned or cleaned.lower() in {"null", "none", "n/a", "unassigned", "tbd"}:
        return None
    return cleaned


def parse_analysis_payload(
    payload: dict[str, Any], *, model: str = "", provider: str = ""
) -> MeetingAnalysis:
    """Convert a raw LLM JSON payload into a :class:`MeetingAnalysis`.

    Tolerant by design. Structured outputs make a schema-valid response very
    likely, but this is the boundary between "a model wrote it" and "our code
    depends on it", so every field is coerced and a missing optional field
    degrades to an empty value rather than an exception.

    Raises:
        AnalysisError: If the payload has no usable summary at all.
    """
    summary = _coerce_str(payload.get("summary"))
    if not summary:
        raise AnalysisError("Model response contained no summary")

    key_points = [
        cleaned
        for point in payload.get("key_points") or []
        if (cleaned := _coerce_str(point))
    ]

    action_items: list[ActionItem] = []
    for raw in payload.get("action_items") or []:
        if isinstance(raw, str):
            description = _coerce_str(raw)
            owner = None
            due = None
        elif isinstance(raw, dict):
            description = _coerce_str(raw.get("description") or raw.get("task"))
            owner = _coerce_optional_str(raw.get("owner"))
            due = _coerce_optional_str(raw.get("due"))
        else:
            logger.warning("Ignoring action item of unexpected type %r", type(raw))
            continue
        if description:
            action_items.append(ActionItem(description, owner, due))

    sentiment = _coerce_str(payload.get("sentiment")).lower() or "neutral"
    if sentiment not in SENTIMENTS:
        logger.warning(
            "Model returned unrecognised sentiment %r; keeping it verbatim",
            sentiment,
        )

    return MeetingAnalysis(
        summary=summary,
        key_points=key_points,
        action_items=action_items,
        sentiment=sentiment,
        sentiment_rationale=_coerce_str(payload.get("sentiment_rationale")),
        model=model,
        provider=provider,
    )


def chunk_transcript(text: str, chunk_chars: int = CHUNK_CHARS) -> list[str]:
    """Split a rendered transcript into chunks on line boundaries.

    Splitting on newlines keeps each speaker turn intact, so no chunk starts or
    ends mid-sentence.
    """
    if len(text) <= chunk_chars:
        return [text] if text else []

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        line_size = len(line) + 1
        if size + line_size > chunk_chars and current:
            chunks.append("\n".join(current))
            current = []
            size = 0
        current.append(line)
        size += line_size
    if current:
        chunks.append("\n".join(current))
    return chunks


def _build_context_header(
    participants: Sequence[str], meeting_url: str, duration: str
) -> str:
    lines = ["Meeting context:"]
    if meeting_url:
        lines.append(f"- Meeting URL: {meeting_url}")
    if duration:
        lines.append(f"- Captured duration: {duration}")
    if participants:
        lines.append(f"- Speaker labels present: {', '.join(participants)}")
        lines.append(
            "- Note: labels like 'Speaker 0' come from automatic diarization "
            "and are not real names."
        )
    return "\n".join(lines)


def _map_phase(client: LLMClient, chunks: Sequence[str]) -> str:
    """Condense each chunk into notes for the reduce phase."""
    notes: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        logger.info("Analysing transcript chunk %d/%d", index, len(chunks))
        try:
            note = client.complete_text(
                system=_SYSTEM_PROMPT,
                user=(
                    f"This is part {index} of {len(chunks)} of a meeting "
                    "transcript. Write dense notes covering what was "
                    "discussed, any decisions, any commitments or tasks with "
                    "who committed to them, and the tone. Do not summarise "
                    "away specifics such as names, numbers or dates. Output "
                    "notes only.\n\n"
                    f"--- TRANSCRIPT PART {index} ---\n{chunk}"
                ),
                max_tokens=4_000,
            )
        except LLMError as exc:
            # One bad chunk should not cost the whole analysis.
            logger.error("Chunk %d/%d failed: %s", index, len(chunks), exc)
            notes.append(f"[Part {index}: analysis failed - {exc}]")
            continue
        notes.append(f"--- NOTES FOR PART {index} ---\n{note.strip()}")

    combined = "\n\n".join(notes)
    if len(combined) > MAX_NOTES_CHARS:
        logger.warning(
            "Condensed notes are %d chars; trimming to %d for the final pass",
            len(combined),
            MAX_NOTES_CHARS,
        )
        combined = combined[:MAX_NOTES_CHARS]
    return combined


def analyze_transcript(
    utterances: Sequence[Utterance],
    client: LLMClient,
    *,
    provider: str = "",
    meeting_url: str = "",
    duration: str = "",
    max_single_pass_chars: int = MAX_SINGLE_PASS_CHARS,
) -> MeetingAnalysis:
    """Produce a :class:`MeetingAnalysis` for a finished meeting.

    Args:
        utterances: The meeting's utterances, in chronological order.
        client: An :class:`~meetbot.analysis.llm.LLMClient`.
        provider: Provider name, recorded on the result for provenance.
        meeting_url: Meeting URL, included as prompt context.
        duration: Human-readable captured duration, e.g. ``"00:42:10"``.
        max_single_pass_chars: Threshold above which map-reduce kicks in.

    Returns:
        The parsed analysis.

    Raises:
        AnalysisError: If there is nothing to analyse, or the model call fails.
    """
    if not utterances:
        raise AnalysisError(
            "The transcript is empty - nothing was captured, so there is "
            "nothing to analyse."
        )

    transcript = render_for_llm(utterances)
    header = _build_context_header(speakers(utterances), meeting_url, duration)

    if len(transcript) > max_single_pass_chars:
        chunks = chunk_transcript(transcript)
        logger.info(
            "Transcript is %d chars; using map-reduce over %d chunk(s)",
            len(transcript),
            len(chunks),
        )
        body = (
            "The meeting was long, so it was analysed in parts. Below are "
            "dense notes for each part, in order. Produce a single analysis "
            "of the meeting as a whole.\n\n" + _map_phase(client, chunks)
        )
    else:
        body = f"--- TRANSCRIPT ---\n{transcript}"

    user_prompt = (
        f"{header}\n\n"
        "Analyse this meeting. Produce a concise summary, the key discussion "
        "points, the action items (assign an owner only when the transcript "
        "makes it clear who owns it, otherwise leave the owner null), and the "
        "overall sentiment.\n\n"
        f"{body}"
    )

    logger.info(
        "Requesting meeting analysis from %s/%s (%d chars of prompt input)",
        provider or "llm",
        getattr(client, "model", "?"),
        len(user_prompt),
    )
    try:
        payload = client.complete_json(
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            schema=_ANALYSIS_SCHEMA,
            schema_name="meeting_analysis",
        )
    except LLMError as exc:
        raise AnalysisError(f"The analysis request failed: {exc}") from exc

    logger.debug("Raw analysis payload: %s", json.dumps(payload)[:2000])
    return parse_analysis_payload(
        payload, model=getattr(client, "model", ""), provider=provider
    )


def render_analysis_markdown(
    analysis: MeetingAnalysis,
    *,
    title: str = "Meeting Analysis",
    meta: dict[str, object] | None = None,
) -> str:
    """Render an analysis as a readable Markdown document."""
    parts: list[str] = [f"# {title}", ""]

    if meta:
        for key, value in meta.items():
            parts.append(f"- **{key.replace('_', ' ').title()}:** {value}")
        parts.append("")

    parts.extend(["## Summary", "", analysis.summary, ""])

    parts.extend(["## Key Discussion Points", ""])
    if analysis.key_points:
        parts.extend(f"- {point}" for point in analysis.key_points)
    else:
        parts.append("_None identified._")
    parts.append("")

    parts.extend(["## Action Items", ""])
    if analysis.action_items:
        parts.extend(item.render() for item in analysis.action_items)
    else:
        parts.append("_No action items were agreed in this meeting._")
    parts.append("")

    parts.extend(["## Sentiment", "", f"**Overall tone:** {analysis.sentiment}"])
    if analysis.sentiment_rationale:
        parts.extend(["", analysis.sentiment_rationale])
    parts.append("")

    provenance = " / ".join(p for p in (analysis.provider, analysis.model) if p)
    if provenance:
        parts.extend(
            [
                "---",
                "",
                f"_Generated by {provenance}. Automatically produced from an "
                "ASR transcript - verify anything you plan to act on._",
            ]
        )

    return "\n".join(parts).rstrip() + "\n"
