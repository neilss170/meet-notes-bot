"""The notepad: your rough notes, enhanced against the transcript.

:mod:`meetbot.analysis.summarize` answers "what happened in this meeting?"
from the transcript alone. That is useful, and it is also the weakest thing an
AI can do with a meeting, because it has no idea what *you* thought mattered.
Two people in the same call want different notes out of it.

So this module inverts the relationship. You type whatever you can while the
meeting runs - fragments, half-sentences, a name and an arrow - and the
transcript is used to *fill those in* rather than to replace them. Your notes
are the spine: their order, their emphasis and their wording survive. The
model's job is to expand the shorthand, attach the numbers and owners and
dates you did not have time to write down, and add the things you missed.

Three consequences worth knowing:

**Empty notes are a supported case, not a degenerate one.** The bot often runs
unattended, so :func:`enhance_notes` with no notes produces notes shaped by
the chosen :class:`Template` instead. That keeps templates useful whether or
not anybody was typing.

**Templates steer the shape, not the content.** A one-to-one and a client call
want different headings out of the same machinery, which is the same idea as
Tastefully's per-case-type documents. A template contributes prompt guidance
and nothing else - there is no separate code path per template.

**Nothing here invents.** A note the transcript cannot support is left exactly
as you wrote it rather than being elaborated into something plausible. That
rule is repeated in the prompts because it is the failure mode that would make
enhanced notes worse than no notes at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from meetbot.analysis.llm import LLMClient, LLMError
from meetbot.analysis.summarize import CHUNK_CHARS, chunk_transcript
from meetbot.transcript.format import render_for_llm
from meetbot.transcript.store import Utterance, speakers

logger = logging.getLogger(__name__)

#: Above this many characters of rendered transcript, condense it in parts
#: before enhancing. Lower than the summariser's threshold because the
#: enhancement prompt also carries the notes and the template guidance.
MAX_SINGLE_PASS_CHARS = 90_000

#: Transcript characters handed to a single :func:`answer_question` call.
#: Questions are asked interactively, so latency matters more here than the
#: last few minutes of a very long meeting.
ASK_TRANSCRIPT_CHARS = 60_000

#: Prior question/answer pairs replayed as context for a follow-up.
ASK_HISTORY_TURNS = 6


class NotepadError(RuntimeError):
    """Raised when notes could not be enhanced or a question not answered."""


@dataclass(frozen=True)
class Template:
    """A note shape for one kind of meeting.

    Attributes:
        id: Stable identifier used by the API and stored with the meeting.
        name: Short label for the UI.
        blurb: One line explaining when to pick it.
        guidance: Prompt fragment describing the sections to produce and what
            belongs in each. Written as instructions to the model.
    """

    id: str
    name: str
    blurb: str
    guidance: str

    def to_dict(self) -> dict[str, str]:
        """Serialise for the API. ``guidance`` is prompt internals, not UI."""
        return {"id": self.id, "name": self.name, "blurb": self.blurb}


_TEMPLATE_LIST: tuple[Template, ...] = (
    Template(
        id="general",
        name="General",
        blurb="A balanced set of notes for any meeting.",
        guidance=(
            "Produce these sections, omitting any that would be empty:\n"
            "- '## Summary' - two to four sentences on what the meeting was "
            "for and where it landed.\n"
            "- '## Notes' - the substance, as bullets grouped under '### ' "
            "sub-headings when there were distinct topics.\n"
            "- '## Decisions' - only things the meeting actually settled.\n"
            "- '## Action items' - as '- [ ] Task - Owner (due)', with the "
            "owner and due date omitted when the meeting did not say."
        ),
    ),
    Template(
        id="one_on_one",
        name="One-to-one",
        blurb="Check-in with a manager or report.",
        guidance=(
            "This was a one-to-one. Produce these sections, omitting any that "
            "would be empty:\n"
            "- '## Discussed' - what was talked through, as bullets.\n"
            "- '## Wins' - progress, things that went well.\n"
            "- '## Blockers and concerns' - anything raised as difficult, "
            "stuck, or worrying. Keep the speaker's own framing; do not "
            "soften it.\n"
            "- '## Feedback' - feedback given in either direction, attributed.\n"
            "- '## Action items' - as '- [ ] Task - Owner (due)'.\n"
            "- '## For next time' - anything explicitly deferred."
        ),
    ),
    Template(
        id="standup",
        name="Standup",
        blurb="Short status round with a team.",
        guidance=(
            "This was a status standup. Organise the notes **by person**: a "
            "'### <name or speaker label>' heading for each participant who "
            "spoke, and under it bullets for what they did, what they are "
            "doing next, and anything blocking them. Then:\n"
            "- '## Blockers' - every blocker raised, collected in one place, "
            "each attributed to whoever raised it.\n"
            "- '## Action items' - as '- [ ] Task - Owner (due)'.\n"
            "Keep it terse. A standup should not produce prose."
        ),
    ),
    Template(
        id="client",
        name="Client call",
        blurb="External call with a client or customer.",
        guidance=(
            "This was a call with an external client or customer. Produce "
            "these sections, omitting any that would be empty:\n"
            "- '## Summary' - two to four sentences.\n"
            "- '## What they asked for' - requirements, requests and "
            "expectations, in their words where possible.\n"
            "- '## Concerns raised' - objections, risks and hesitations. "
            "These matter more than they sound in the moment, so record them "
            "even when they were brushed past.\n"
            "- '## Commitments we made' - anything promised to the client, "
            "with any date attached to it.\n"
            "- '## Action items' - as '- [ ] Task - Owner (due)'.\n"
            "- '## Follow-up' - what to send or confirm after the call."
        ),
    ),
    Template(
        id="interview",
        name="Interview",
        blurb="Candidate interview or user research.",
        guidance=(
            "This was an interview. Produce these sections, omitting any that "
            "would be empty:\n"
            "- '## Background' - what the interviewee said about themselves "
            "and their experience.\n"
            "- '## Questions and answers' - each substantive question asked, "
            "with a condensed version of the answer beneath it.\n"
            "- '## Signals' - concrete evidence for or against, quoting the "
            "transcript where a specific phrase carried the signal. Do not "
            "grade or score the interviewee; record what they said and let "
            "the reader judge.\n"
            "- '## Follow-up questions' - anything left unanswered."
        ),
    ),
    Template(
        id="decision",
        name="Planning",
        blurb="Working through options towards a decision.",
        guidance=(
            "This meeting was working towards a decision. Produce these "
            "sections, omitting any that would be empty:\n"
            "- '## The question' - what was actually being decided.\n"
            "- '## Options considered' - a '### ' sub-heading per option, "
            "with the arguments for and against it as bullets underneath.\n"
            "- '## Where it landed' - the decision, or plainly that no "
            "decision was reached. Do not manufacture a conclusion from a "
            "conversation that did not reach one.\n"
            "- '## Open questions' - what still has to be resolved.\n"
            "- '## Action items' - as '- [ ] Task - Owner (due)'."
        ),
    ),
)

#: Every template, by id, in display order.
TEMPLATES: dict[str, Template] = {t.id: t for t in _TEMPLATE_LIST}

#: Used when the caller names no template, or names one that no longer exists.
DEFAULT_TEMPLATE = "general"


def get_template(template_id: str | None) -> Template:
    """Look up a template, falling back to :data:`DEFAULT_TEMPLATE`.

    Unknown ids fall back rather than raise: a template id is stored on disk
    with the meeting, and renaming one in a later version should not make old
    meetings unopenable.
    """
    if template_id and template_id in TEMPLATES:
        return TEMPLATES[template_id]
    return TEMPLATES[DEFAULT_TEMPLATE]


_ENHANCE_SYSTEM = (
    "You turn a person's rough meeting notes into good ones, using the "
    "meeting's transcript as your source of fact.\n\n"
    "THE NOTES ARE THE SPINE. Whatever the person wrote is what they thought "
    "mattered, so keep their structure, their ordering and their emphasis. "
    "Expand their shorthand into full sentences, attach the specifics they "
    "did not have time to write - names, numbers, dates, owners - and add "
    "what they missed. You are filling in a skeleton, not writing over it.\n\n"
    "NEVER INVENT. Every fact you add must be supported by the transcript. If "
    "one of their notes is not supported by the transcript, keep it exactly "
    "as they wrote it rather than elaborating it into something plausible - "
    "they may have been looking at a screen you cannot hear. Enhanced notes "
    "that contain invented detail are worse than no notes at all.\n\n"
    "The transcript comes from automatic speech recognition, so it contains "
    "misheard words and generic labels like 'Speaker 0'. Where the person's "
    "notes give a real name to a speaker, use the name. Never guess at one. "
    "Where ASR clearly mangled a term the notes spell correctly, prefer the "
    "notes' spelling.\n\n"
    "Write plain Markdown: '## ' and '### ' headings, '- ' bullets, and "
    "'- [ ] ' for action items. No preamble, no sign-off, no meta-commentary "
    "about the transcript or about what you were asked to do. Start directly "
    "with the first heading. Say less rather than padding: an empty section "
    "should be dropped, not filled."
)

_NO_NOTES_INSTRUCTION = (
    "The person did not type any notes during this meeting, so there is no "
    "spine to preserve. Write the notes from the transcript alone, in the "
    "shape described below."
)

_CONDENSE_SYSTEM = (
    "You are condensing one part of a long meeting transcript so it can be "
    "written up later. Keep every concrete fact: decisions, numbers, dates, "
    "names, commitments, disagreements, and anything left unresolved. Drop "
    "small talk and repetition. Attribute statements to the speaker labels "
    "used in the transcript. Do not summarise into conclusions - later steps "
    "need the detail, not your verdict."
)


def _condense_parts(
    client: LLMClient, chunks: Sequence[str], notes: str
) -> str:
    """Condense each chunk of a long transcript into dense notes.

    The person's notes are passed along as a lens: in a two-hour meeting, the
    parts touching what they wrote down are the parts worth keeping in
    detail. Everything else is kept, but tersely.
    """
    lens = ""
    if notes.strip():
        lens = (
            "\n\nThe person taking notes wrote the following. Keep anything "
            "bearing on it in more detail than the rest:\n"
            f"{notes.strip()}"
        )
    condensed: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        try:
            part = client.complete_text(
                system=_CONDENSE_SYSTEM,
                user=(
                    f"Part {index} of {len(chunks)} of the transcript."
                    f"{lens}\n\n--- TRANSCRIPT PART ---\n{chunk}"
                ),
                max_tokens=4000,
            )
        except LLMError as exc:
            raise NotepadError(
                f"Condensing part {index} of the transcript failed: {exc}"
            ) from exc
        condensed.append(f"--- PART {index} ---\n{part.strip()}")
    return "\n\n".join(condensed)


def _context_header(
    utterances: Sequence[Utterance], meeting_url: str, duration: str
) -> str:
    lines = ["Meeting context:"]
    if meeting_url:
        lines.append(f"- Meeting URL: {meeting_url}")
    if duration:
        lines.append(f"- Captured duration: {duration}")
    labels = speakers(utterances)
    if labels:
        lines.append(f"- Speaker labels present: {', '.join(labels)}")
        if any(label.startswith("Speaker ") for label in labels):
            lines.append(
                "- Labels like 'Speaker 0' come from automatic diarization "
                "and are not real names."
            )
    return "\n".join(lines)


def enhance_notes(
    utterances: Sequence[Utterance],
    notes: str,
    client: LLMClient,
    *,
    template_id: str | None = None,
    meeting_url: str = "",
    duration: str = "",
    max_single_pass_chars: int = MAX_SINGLE_PASS_CHARS,
) -> str:
    """Rewrite ``notes`` into finished notes, grounded in the transcript.

    Args:
        utterances: The meeting's utterances, in chronological order.
        notes: Whatever the person typed. May be empty, in which case the
            notes are written from the transcript under ``template_id``.
        client: An :class:`~meetbot.analysis.llm.LLMClient`.
        template_id: Which :class:`Template` shapes the output. Unknown ids
            fall back to :data:`DEFAULT_TEMPLATE`.
        meeting_url: Included as prompt context.
        duration: Human-readable captured duration, e.g. ``"00:42:10"``.
        max_single_pass_chars: Threshold above which the transcript is
            condensed in parts first.

    Returns:
        Markdown. Never empty.

    Raises:
        NotepadError: If there is nothing to work from, or the model call
            fails, or it returns nothing usable.
    """
    template = get_template(template_id)
    typed = notes.strip()

    if not utterances and not typed:
        raise NotepadError(
            "There is nothing to enhance - no notes were typed and nothing "
            "was transcribed."
        )

    if utterances:
        transcript = render_for_llm(utterances)
        if len(transcript) > max_single_pass_chars:
            chunks = chunk_transcript(transcript, CHUNK_CHARS)
            logger.info(
                "Transcript is %d chars; condensing %d part(s) before enhancing",
                len(transcript),
                len(chunks),
            )
            body = (
                "The meeting was long, so the transcript was condensed in "
                "parts. Below are dense notes for each part, in order.\n\n"
                + _condense_parts(client, chunks, typed)
            )
        else:
            body = f"--- TRANSCRIPT ---\n{transcript}"
    else:
        # Notes without a transcript still deserve tidying - this is what a
        # meeting that failed to capture audio looks like, and losing the
        # typed notes to an error message would be gratuitous.
        body = (
            "--- TRANSCRIPT ---\n(Nothing was transcribed for this meeting. "
            "Work from the notes alone: tidy and structure them, but add no "
            "facts of your own.)"
        )

    if typed:
        spine = f"--- THE PERSON'S NOTES ---\n{typed}"
        instruction = (
            "Enhance the notes below using the transcript, following every "
            "rule you were given."
        )
    else:
        spine = ""
        instruction = _NO_NOTES_INSTRUCTION

    user_prompt = "\n\n".join(
        part
        for part in (
            _context_header(utterances, meeting_url, duration),
            instruction,
            f"--- REQUIRED SHAPE ---\n{template.guidance}",
            spine,
            body,
        )
        if part
    )

    logger.info(
        "Enhancing notes with %s (template=%s, %d chars typed, %d chars of prompt)",
        getattr(client, "model", "?"),
        template.id,
        len(typed),
        len(user_prompt),
    )
    try:
        enhanced = client.complete_text(system=_ENHANCE_SYSTEM, user=user_prompt)
    except LLMError as exc:
        raise NotepadError(f"The enhancement request failed: {exc}") from exc

    enhanced = enhanced.strip()
    if not enhanced:
        raise NotepadError("The model returned empty notes.")
    return enhanced


_ASK_SYSTEM = (
    "You answer questions about one meeting, using its transcript and the "
    "notes taken on it.\n\n"
    "Answer only from what you were given. If the meeting did not cover it, "
    "say so plainly in one sentence - do not reason outwards from what was "
    "said into what was probably meant, and do not fall back on general "
    "knowledge. 'That did not come up' is a good answer.\n\n"
    "The transcript comes from automatic speech recognition and contains "
    "misheard words and generic labels like 'Speaker 0'. Quote it when a "
    "specific phrase is the answer, and say when a quote looks garbled.\n\n"
    "Be brief and direct. Answer in prose for a question about what happened, "
    "and in a short list when the question asks for several things. No "
    "preamble - do not restate the question before answering it."
)


def answer_question(
    utterances: Sequence[Utterance],
    question: str,
    client: LLMClient,
    *,
    notes: str = "",
    history: Sequence[dict[str, Any]] = (),
    transcript_chars: int = ASK_TRANSCRIPT_CHARS,
) -> str:
    """Answer a question about the meeting.

    Args:
        utterances: The meeting's utterances, in chronological order.
        question: What was asked.
        client: An :class:`~meetbot.analysis.llm.LLMClient`.
        notes: The finished notes, if any. Passed alongside the transcript
            because they carry real names and corrected spellings that the
            transcript does not.
        history: Prior turns as ``{"question": ..., "answer": ...}`` dicts,
            oldest first. Only the last :data:`ASK_HISTORY_TURNS` are used,
            so a long conversation does not crowd out the transcript.
        transcript_chars: Transcript characters to include.

    Returns:
        The answer as plain text or short Markdown.

    Raises:
        NotepadError: If the question is empty, there is nothing to search,
            or the model call fails.
    """
    question = question.strip()
    if not question:
        raise NotepadError("Ask a question first.")
    if not utterances and not notes.strip():
        raise NotepadError(
            "There is nothing to search - this meeting has no transcript and "
            "no notes yet."
        )

    parts: list[str] = []
    if notes.strip():
        parts.append(f"--- NOTES ON THE MEETING ---\n{notes.strip()}")
    if utterances:
        parts.append(
            "--- TRANSCRIPT ---\n"
            + render_for_llm(utterances, max_chars=transcript_chars)
        )

    recent = list(history)[-ASK_HISTORY_TURNS:]
    if recent:
        replay = "\n\n".join(
            f"Q: {turn.get('question', '')}\nA: {turn.get('answer', '')}"
            for turn in recent
        )
        parts.append(f"--- EARLIER IN THIS CONVERSATION ---\n{replay}")

    parts.append(f"--- QUESTION ---\n{question}")

    try:
        answer = client.complete_text(
            system=_ASK_SYSTEM, user="\n\n".join(parts), max_tokens=2000
        )
    except LLMError as exc:
        raise NotepadError(f"The question could not be answered: {exc}") from exc

    answer = answer.strip()
    if not answer:
        raise NotepadError("The model returned an empty answer.")
    return answer


__all__ = [
    "ASK_HISTORY_TURNS",
    "ASK_TRANSCRIPT_CHARS",
    "DEFAULT_TEMPLATE",
    "MAX_SINGLE_PASS_CHARS",
    "TEMPLATES",
    "NotepadError",
    "Template",
    "answer_question",
    "enhance_notes",
    "get_template",
]
