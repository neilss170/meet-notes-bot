"""Render stored utterances into human- and LLM-readable transcripts.

Two audiences, two renderers:

* :func:`render_markdown` / :func:`render_text` are for people - timestamped,
  grouped by speaker turn.
* :func:`render_for_llm` is for the analysis prompt - no decorative markup,
  and a hard character budget so a four-hour meeting still fits in a request.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from meetbot.transcript.store import Utterance

#: Gap (seconds) above which consecutive utterances from the same speaker are
#: treated as separate turns rather than merged into one paragraph.
TURN_GAP_S = 8.0


def format_timestamp(seconds: float) -> str:
    """Format an offset in seconds as ``HH:MM:SS``.

    Negative inputs are clamped to zero.

    >>> format_timestamp(3725.4)
    '01:02:05'
    """
    total = int(max(0.0, seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class Turn:
    """A run of consecutive utterances by one speaker.

    Attributes:
        speaker: The speaker label shared by every utterance in the turn.
        start: Start offset of the first utterance, in seconds.
        end: End offset of the last utterance, in seconds.
        text: The utterances' text joined with single spaces.
    """

    __slots__ = ("speaker", "start", "end", "text")

    def __init__(self, speaker: str, start: float, end: float, text: str) -> None:
        self.speaker = speaker
        self.start = start
        self.end = end
        self.text = text

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Turn({self.speaker!r}, {self.start:.1f}-{self.end:.1f}, {self.text!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Turn):
            return NotImplemented
        return (
            self.speaker == other.speaker
            and self.start == other.start
            and self.end == other.end
            and self.text == other.text
        )


def group_turns(
    utterances: Iterable[Utterance], gap_s: float = TURN_GAP_S
) -> list[Turn]:
    """Merge consecutive same-speaker utterances into conversational turns.

    Diarized streaming output arrives in short fragments; reading a transcript
    of one-line-per-fragment is painful and wastes tokens in the analysis
    prompt. Utterances are merged while the speaker is unchanged and the
    silence between them is under ``gap_s``.

    Args:
        utterances: Utterances in chronological order.
        gap_s: Silence threshold, in seconds, that starts a new turn.

    Returns:
        The merged turns, in order.
    """
    turns: list[Turn] = []
    for utterance in utterances:
        text = utterance.text.strip()
        if not text:
            continue
        if (
            turns
            and turns[-1].speaker == utterance.speaker
            and utterance.start - turns[-1].end <= gap_s
        ):
            previous = turns[-1]
            previous.text = f"{previous.text} {text}"
            previous.end = max(previous.end, utterance.end)
        else:
            turns.append(Turn(utterance.speaker, utterance.start, utterance.end, text))
    return turns


def render_text(
    utterances: Sequence[Utterance], *, timestamps: bool = True
) -> str:
    """Render a plain-text transcript, one line per turn."""
    lines: list[str] = []
    for turn in group_turns(utterances):
        prefix = f"[{format_timestamp(turn.start)}] " if timestamps else ""
        lines.append(f"{prefix}{turn.speaker}: {turn.text}")
    return "\n".join(lines)


def render_markdown(
    utterances: Sequence[Utterance],
    *,
    title: str = "Meeting Transcript",
    meta: dict[str, object] | None = None,
) -> str:
    """Render a Markdown transcript with a short header.

    Args:
        utterances: Utterances in chronological order.
        title: Document title.
        meta: Optional key/value pairs (meeting URL, start time, ...) rendered
            as a bullet list under the title.

    Returns:
        A complete Markdown document. Empty transcripts render a clear
        "no speech captured" note rather than an empty file, so an operator can
        tell "the bot never heard anything" from "the file was never written".
    """
    parts: list[str] = [f"# {title}", ""]

    if meta:
        for key, value in meta.items():
            label = key.replace("_", " ").title()
            parts.append(f"- **{label}:** {value}")
        parts.append("")

    turns = group_turns(utterances)
    if not turns:
        parts.append("_No speech was captured during this meeting._")
        return "\n".join(parts) + "\n"

    speaker_labels = sorted({turn.speaker for turn in turns})
    duration = max(turn.end for turn in turns)
    parts.append(
        f"- **Duration (captured):** {format_timestamp(duration)}  "
        f"\n- **Speakers detected:** {len(speaker_labels)} "
        f"({', '.join(speaker_labels)})"
    )
    parts.extend(["", "---", ""])

    for turn in turns:
        parts.append(f"**[{format_timestamp(turn.start)}] {turn.speaker}**")
        parts.append("")
        parts.append(turn.text)
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def render_for_llm(
    utterances: Sequence[Utterance], *, max_chars: int | None = None
) -> str:
    """Render a compact transcript for an LLM prompt.

    Args:
        utterances: Utterances in chronological order.
        max_chars: Optional character budget. When the transcript exceeds it,
            the *middle* is dropped and replaced with an explicit elision
            marker - the opening (agenda, context) and the close (decisions,
            next steps) carry the most signal for a summary, so keeping both
            ends beats a head- or tail-truncation.

    Returns:
        A newline-delimited ``[HH:MM:SS] Speaker: text`` transcript.
    """
    body = render_text(utterances, timestamps=True)
    if max_chars is None or len(body) <= max_chars:
        return body

    marker = "\n\n[... transcript truncated in the middle for length ...]\n\n"
    budget = max(0, max_chars - len(marker))
    head_budget = budget // 2
    tail_budget = budget - head_budget

    # Take whole lines from each end. Truncating mid-line would hand the model
    # half a sentence and, worse, a speaker attribution with no speaker.
    lines = body.splitlines()

    head_lines: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > head_budget:
            break
        head_lines.append(line)
        used += len(line) + 1

    tail_lines: list[str] = []
    used = 0
    for line in reversed(lines[len(head_lines) :]):
        if used + len(line) + 1 > tail_budget:
            break
        tail_lines.append(line)
        used += len(line) + 1
    tail_lines.reverse()

    # Degenerate budgets (smaller than a single turn) would otherwise produce
    # nothing but the elision marker. Fall back to a hard character cut so the
    # caller still gets both ends of the meeting.
    head = "\n".join(head_lines) if head_lines else body[:head_budget]
    tail = "\n".join(tail_lines) if tail_lines else body[len(body) - tail_budget :]

    return f"{head}{marker}{tail}"
