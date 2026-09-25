"""Recall: ask every meeting at once, and see exactly where the answer came from.

The per-meeting Ask can hand the model a whole transcript, because one meeting
fits in a prompt. The archive does not, and it grows every week, so a question
asked of all of it is answered in two steps: find the few moments worth
reading, then answer from those alone.

**Finding them is keyword search, on purpose.** Embeddings would match meaning
rather than words, but the configured provider has no embeddings endpoint and a
local embedding model is a heavyweight dependency for an archive measured in
tens of meetings. Keyword search misses paraphrase - "how much are we spending"
never matches "budget" - so the model is first asked to turn the question into
search terms, synonyms and likely mis-hearings included. That one short call
buys back most of what embeddings would have.

**When everything fits, nothing is searched.** A small archive is sent whole.
Retrieval exists only because of the budget, and ranking a dozen short
recordings would add a way to miss the answer while gaining nothing.

**A citation cannot point at nothing.** Every transcript line handed to the
model carries an ID, the model cites IDs, and an ID it was not given is dropped
rather than rendered. A citation therefore always resolves to a real line in a
real meeting, with the lines around it - what prompted it and what was said
back - which is what lets somebody check an answer instead of trusting it. The
same machinery answers questions about a single meeting, so both chats cite
the same way.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

from meetbot.analysis.llm import LLMClient, LLMError
from meetbot.transcript.format import format_timestamp
from meetbot.transcript.store import Utterance

logger = logging.getLogger(__name__)

#: A cited line is at most about this long. Deepgram finalises speech in
#: fragments as short as a single word, which is useless as a quote, so
#: consecutive fragments from one speaker are joined up to a sentence or two.
SEGMENT_MAX_CHARS = 280

#: Silence that ends a segment even when the speaker has not changed.
SEGMENT_GAP_S = 6.0

#: Excerpt characters handed to a question asked across every meeting. Well
#: under the per-meeting budget: the question is interactive, and free tiers
#: meter tokens per minute.
ARCHIVE_CONTEXT_CHARS = 24_000

#: Transcript characters handed to a question about one meeting.
MEETING_CONTEXT_CHARS = 60_000

#: Rough prompt cost of the ID, timestamp and speaker in front of each line,
#: and of the heading in front of each meeting.
LINE_OVERHEAD = 32
HEADER_OVERHEAD = 64

#: Segments per scored window, and how far each window advances. Overlap means
#: a phrase that straddles a window boundary still scores.
WINDOW_SEGMENTS = 4
WINDOW_STRIDE = 2

#: Windows kept per meeting, so one long meeting that says a word fifty times
#: cannot crowd out the short meeting where it was actually decided.
MAX_WINDOWS_PER_MEETING = 4

#: Meetings opened, most recent first, when nothing matched at all.
FALLBACK_MEETINGS = 6

#: Prior turns replayed for a follow-up question.
HISTORY_TURNS = 4

#: Transcript segments kept either side of a citation.
CONTEXT_BEFORE = 3
CONTEXT_AFTER = 2

#: Output budget for an answer. Generous because reasoning models spend part
#: of it thinking before they write anything - see ``probe_llm``.
ANSWER_MAX_TOKENS = 3000

#: BM25 constants, and how much a search term counts for relative to a word
#: the person actually typed.
BM25_K1 = 1.2
BM25_B = 0.75
EXPANSION_WEIGHT = 0.7
TITLE_BONUS = 1.5
PHRASE_BONUS = 1.0


class RecallError(RuntimeError):
    """Raised when a question cannot be asked - empty, or nothing to search."""


# -- segments --------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """A citable stretch of one speaker's speech."""

    speaker: str
    start: float
    end: float
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "speaker": self.speaker,
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "text": self.text,
        }


def segment_utterances(
    utterances: Iterable[Utterance],
    *,
    max_chars: int = SEGMENT_MAX_CHARS,
    gap_s: float = SEGMENT_GAP_S,
) -> list[Segment]:
    """Join transcript fragments into lines worth quoting.

    Unlike :func:`~meetbot.transcript.format.group_turns`, a segment is capped
    in length. A single speaker talking for ten minutes is one turn, and
    citing a ten-minute turn tells nobody where to look.
    """
    segments: list[Segment] = []
    for utterance in utterances:
        text = " ".join(utterance.text.split())
        if not text:
            continue
        if segments:
            last = segments[-1]
            if (
                last.speaker == utterance.speaker
                and utterance.start - last.end <= gap_s
                and len(last.text) + 1 + len(text) <= max_chars
            ):
                segments[-1] = Segment(
                    last.speaker,
                    last.start,
                    max(last.end, utterance.end),
                    f"{last.text} {text}",
                )
                continue
        segments.append(Segment(utterance.speaker, utterance.start, utterance.end, text))
    return segments


@dataclass
class MeetingSource:
    """One meeting, as recall sees it."""

    id: str
    title: str
    #: Epoch seconds the meeting started, or 0 when that is unknown.
    started_at: float
    utterances: Sequence[Utterance]
    #: Real speaker label to the label the model should see instead. Empty
    #: unless anonymisation is on - the citations shown locally always carry
    #: the real names.
    speaker_alias: Mapping[str, str] = field(default_factory=dict)
    segments: list[Segment] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.segments = segment_utterances(self.utterances)

    @property
    def day(self) -> date | None:
        moment = self._moment()
        return moment.date() if moment else None

    @property
    def when(self) -> str:
        moment = self._moment()
        return moment.strftime("%a %d %b %Y, %H:%M") if moment else "date unknown"

    def _moment(self) -> datetime | None:
        if self.started_at <= 0:
            return None
        try:
            return datetime.fromtimestamp(self.started_at)
        except (OverflowError, OSError, ValueError):
            return None

    def prompt_line(self, index: int, cite_id: str) -> str:
        segment = self.segments[index]
        speaker = self.speaker_alias.get(segment.speaker, segment.speaker)
        return f"[{cite_id}] {format_timestamp(segment.start)} {speaker}: {segment.text}"

    def cost(self, indices: Iterable[int]) -> int:
        return sum(len(self.segments[i].text) + LINE_OVERHEAD for i in indices)


# -- search ----------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+")

#: Words that match everything. Includes the verbs people use to ask about a
#: meeting - "when was X *said*", "did anyone *mention* X" - which appear in
#: the question and say nothing about where the answer is.
_STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are as at be
    because been before being below between both but by can could did do
    does doing done down during each either else ever every few for from
    further get gets getting got had has have having he her here hers him
    his how i if in into is it its itself just let lets me more most much
    my no nor not now of off on once one only or other our ours out over
    same she should so some such than that the their theirs them then
    there these they this those through to too under until up upon us very
    was we were what whatever when whenever where which while who whom whose
    why will with within without would yes yet you your yours
    re ll ve don didn doesn isn wasn aren weren won wouldn couldn shouldn
    um uh ah oh okay ok yeah yep right like really actually basically gonna
    wanna kind sort thing things stuff something anything everything someone
    anyone everyone
    say says said saying tell told talk talked talking discuss discussed
    discussing mention mentioned mentioning ask asked asking meeting meetings
    call calls
    """.split()
)


def _stem(word: str) -> str:
    """Fold common English endings together, crudely.

    Not a real stemmer, and it does not need to be: it only has to agree with
    itself, so that "deadlines" finds "deadline" and "planned" finds
    "planning". The search terms the model suggests cover the rest.
    """
    if len(word) <= 3 or not word.isalpha():
        return word
    if word.endswith("ies") and len(word) > 4:
        word = word[:-3] + "y"
    elif word.endswith(("sses", "shes", "ches", "xes", "zes")):
        word = word[:-2]
    elif word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    for suffix in ("ing", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            if word[-1] == word[-2] and word[-1] not in "lsz":
                word = word[:-1]
            break
    if word.endswith("e") and len(word) > 3:
        word = word[:-1]
    return word


#: Longer stopwords as they stem, so "discusses" goes the way of "discussed".
#: Short ones are left out: "same" stems to "sam", and a stopword that
#: swallowed everybody called Sam would be far worse than one extra term.
_STOP_STEMS = frozenset(_stem(word) for word in _STOPWORDS if len(word) > 5)


def tokenize(text: str) -> list[str]:
    """Search tokens for ``text``: lower-cased, stemmed, stopwords dropped."""
    tokens: list[str] = []
    for word in _WORD_RE.findall(text.lower()):
        if word in _STOPWORDS or (len(word) < 2 and not word.isdigit()):
            continue
        stem = _stem(word)
        if stem not in _STOP_STEMS:
            tokens.append(stem)
    return tokens


def _normalise(text: str) -> str:
    return " ".join(_WORD_RE.findall(text.lower()))


@dataclass(frozen=True)
class SearchPlan:
    """What to look for, and in which period."""

    question: str
    #: Extra words and phrases likely to appear where the answer is.
    terms: tuple[str, ...] = ()
    from_day: date | None = None
    to_day: date | None = None

    @property
    def has_period(self) -> bool:
        return self.from_day is not None or self.to_day is not None

    def covers(self, day: date | None) -> bool:
        if not self.has_period:
            return True
        if day is None:
            return False
        if self.from_day is not None and day < self.from_day:
            return False
        if self.to_day is not None and day > self.to_day:
            return False
        return True

    def describe_period(self) -> str:
        start = f"{self.from_day:%d %b %Y}" if self.from_day else "the beginning"
        end = f"{self.to_day:%d %b %Y}" if self.to_day else "today"
        return f"{start} to {end}"


_PLAN_SYSTEM = (
    "You prepare a keyword search over a person's meeting transcripts. Given "
    "their question, list the words and short phrases most likely to appear in "
    "the transcript at the moment that answers it: the question's own key "
    "terms, their synonyms and closely related words, other forms of the same "
    "word, and plausible speech-recognition mis-hearings of names and jargon. "
    "Leave out words like 'meeting', 'said' or 'discussed' that match "
    "everything. Resolve a follow-up question using the earlier conversation. "
    "Give at most 15 terms.\n\n"
    "If the question limits itself to a period - 'last week', 'yesterday', "
    "'in August' - give that period as from_date and to_date (YYYY-MM-DD, "
    "inclusive), working from today's date. Otherwise leave both empty."
)

_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "terms": {"type": "array", "items": {"type": "string"}},
        "from_date": {"type": "string"},
        "to_date": {"type": "string"},
    },
    "required": ["terms", "from_date", "to_date"],
    "additionalProperties": False,
}


def _parse_day(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def plan_search(
    question: str,
    client: LLMClient,
    *,
    history: Sequence[Mapping[str, Any]] = (),
    today: date | None = None,
) -> SearchPlan:
    """Ask the model what to search for.

    Never raises for a model failure: searching for the question exactly as
    it was typed is a worse search, not a broken one.
    """
    today = today or date.today()
    parts = [f"Today is {today:%A} {today.isoformat()}."]
    replay = _replay(history, 2)
    if replay:
        parts.append(f"--- EARLIER IN THIS CONVERSATION ---\n{replay}")
    parts.append(f"--- QUESTION TO SEARCH FOR ---\n{question}")

    try:
        payload = client.complete_json(
            system=_PLAN_SYSTEM,
            user="\n\n".join(parts),
            schema=_PLAN_SCHEMA,
            schema_name="search_plan",
            max_tokens=2048,
        )
    except LLMError as exc:
        logger.warning("Searching the question as typed; term expansion failed: %s", exc)
        return SearchPlan(question)

    raw_terms = payload.get("terms") if isinstance(payload, dict) else None
    terms = tuple(
        " ".join(term.split())
        for term in (raw_terms if isinstance(raw_terms, list) else [])
        if isinstance(term, str) and term.strip()
    )[:24]
    from_day = _parse_day(payload.get("from_date")) if isinstance(payload, dict) else None
    to_day = _parse_day(payload.get("to_date")) if isinstance(payload, dict) else None
    if from_day and to_day and from_day > to_day:
        from_day, to_day = to_day, from_day
    return SearchPlan(question, terms, from_day, to_day)


def _windows(count: int) -> list[tuple[int, int]]:
    """Inclusive, overlapping ``(first, last)`` segment spans covering a meeting."""
    if count <= 0:
        return []
    if count <= WINDOW_SEGMENTS:
        return [(0, count - 1)]
    spans = [
        (i, i + WINDOW_SEGMENTS - 1)
        for i in range(0, count - WINDOW_SEGMENTS + 1, WINDOW_STRIDE)
    ]
    if spans[-1][1] < count - 1:
        spans.append((count - WINDOW_SEGMENTS, count - 1))
    return spans


def rank_windows(
    sources: Sequence[MeetingSource], plan: SearchPlan
) -> list[tuple[float, int, int, int]]:
    """Score every window of every meeting against the plan, best first.

    Returns ``(score, source index, first segment, last segment)`` for each
    window that matched anything at all.
    """
    weights: dict[str, float] = {token: 1.0 for token in tokenize(plan.question)}
    phrases: list[str] = []
    for term in plan.terms:
        tokens = tokenize(term)
        for token in tokens:
            weights.setdefault(token, EXPANSION_WEIGHT)
        if len(tokens) > 1:
            phrases.append(_normalise(term))
    if not weights:
        return []

    documents: list[tuple[int, int, int, Counter[str], int, str]] = []
    for s_index, source in enumerate(sources):
        per_segment = [tokenize(segment.text) for segment in source.segments]
        for first, last in _windows(len(source.segments)):
            bag: Counter[str] = Counter()
            for i in range(first, last + 1):
                bag.update(per_segment[i])
            text = _normalise(
                " ".join(s.text for s in source.segments[first : last + 1])
            )
            documents.append((s_index, first, last, bag, sum(bag.values()), text))
    if not documents:
        return []

    total = len(documents)
    average = sum(doc[4] for doc in documents) / total or 1.0
    frequency: Counter[str] = Counter()
    for doc in documents:
        frequency.update(term for term in weights if term in doc[3])
    idf = {
        term: math.log(1 + (total - frequency[term] + 0.5) / (frequency[term] + 0.5))
        for term in weights
    }
    # Naming the meeting in the question ("in the YOLO call") should find it
    # even when its transcript never repeats its own title.
    title_bonus = [
        TITLE_BONUS * sum(w for t, w in weights.items() if t in set(tokenize(s.title)))
        for s in sources
    ]

    ranked: list[tuple[float, int, int, int]] = []
    for s_index, first, last, bag, length, text in documents:
        score = title_bonus[s_index]
        for term, weight in weights.items():
            tf = bag.get(term, 0)
            if tf:
                score += weight * idf[term] * tf * (BM25_K1 + 1) / (
                    tf + BM25_K1 * (1 - BM25_B + BM25_B * length / average)
                )
        score += PHRASE_BONUS * sum(1 for phrase in phrases if phrase and phrase in text)
        if score > 0:
            ranked.append((score, s_index, first, last))
    ranked.sort(key=lambda r: (-r[0], -sources[r[1]].started_at, r[2]))
    return ranked


def _select(
    sources: Sequence[MeetingSource], plan: SearchPlan, budget: int
) -> tuple[dict[int, set[int]], str]:
    """Choose which segments of which meetings the model gets to read.

    Returns the chosen segment indices per source index, and a note for the
    model when the choice was not a normal search.
    """
    whole = sum(s.cost(range(len(s.segments))) + HEADER_OVERHEAD for s in sources)
    if whole <= budget:
        return {i: set(range(len(s.segments))) for i, s in enumerate(sources)}, ""

    chosen: dict[int, set[int]] = {}
    windows_used: Counter[int] = Counter()
    used = 0
    for _score, s_index, first, last in rank_windows(sources, plan):
        if windows_used[s_index] >= MAX_WINDOWS_PER_MEETING:
            continue
        source = sources[s_index]
        # One segment either side, so a hit is read with what led into it.
        low, high = max(0, first - 1), min(len(source.segments) - 1, last + 1)
        have = chosen.get(s_index, set())
        new = [i for i in range(low, high + 1) if i not in have]
        if not new:
            continue
        cost = source.cost(new) + (0 if have else HEADER_OVERHEAD)
        if used + cost > budget:
            continue
        chosen.setdefault(s_index, set()).update(new)
        windows_used[s_index] += 1
        used += cost
    if chosen:
        return chosen, ""

    # Nothing matched - "what did I do yesterday?" has no keyword to find.
    # The openings of the most recent meetings are a better guess than
    # refusing, and the model is told this is what it is looking at.
    recent = sorted(
        range(len(sources)), key=lambda i: sources[i].started_at, reverse=True
    )[:FALLBACK_MEETINGS]
    share = budget // max(1, len(recent))
    for s_index in recent:
        source = sources[s_index]
        taken: set[int] = set()
        cost = HEADER_OVERHEAD
        for i, segment in enumerate(source.segments):
            line = len(segment.text) + LINE_OVERHEAD
            if cost + line > share:
                break
            taken.add(i)
            cost += line
        if taken:
            chosen[s_index] = taken
    return chosen, (
        "Nothing matched a keyword search, so these are the openings of the "
        "most recent meetings rather than search results."
    )


def _render(
    sources: Sequence[MeetingSource], chosen: Mapping[int, set[int]]
) -> tuple[str, dict[str, tuple[int, int]]]:
    """Excerpts for the prompt, and the citation ID of every line in them."""
    ids: dict[str, tuple[int, int]] = {}
    blocks: list[str] = []
    order = sorted(
        (i for i, indices in chosen.items() if indices),
        key=lambda i: (sources[i].started_at, sources[i].title),
    )
    for s_index in order:
        source = sources[s_index]
        lines = [f"## {source.title} ({source.when})"]
        previous: int | None = None
        for seg_index in sorted(chosen[s_index]):
            if previous is not None and seg_index != previous + 1:
                lines.append("...")
            cite_id = f"S{len(ids) + 1}"
            ids[cite_id] = (s_index, seg_index)
            lines.append(source.prompt_line(seg_index, cite_id))
            previous = seg_index
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks), ids


# -- citations -------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    """One numbered source behind an answer."""

    n: int
    meeting_id: str
    meeting_title: str
    started_at: float
    start: float
    end: float
    speaker: str
    text: str
    #: What was said just before - usually the reason it was said.
    before: tuple[Segment, ...]
    after: tuple[Segment, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "meeting_id": self.meeting_id,
            "meeting_title": self.meeting_title,
            "started_at": self.started_at,
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "speaker": self.speaker,
            "text": self.text,
            "before": [s.to_dict() for s in self.before],
            "after": [s.to_dict() for s in self.after],
        }


#: A group of citation IDs as the model writes them: ``[S12]``, ``[S3, S7]``,
#: ``[S4-S6]``, and the ``【S12】`` form some open-weight models prefer.
#: Leading spaces and tabs are consumed so "claim [S1]." renders as "claim¹."
#: - but not newlines, which would join a citation onto the previous line.
_CITE_GROUP_RE = re.compile(
    r"[ \t]*[\[【(]\s*(S\d+(?:\s*(?:[,;]|[-–])\s*S?\d+)*)\s*[\]】)]",
    re.IGNORECASE,
)
_CITE_RANGE_RE = re.compile(r"S?(\d+)\s*[-–]\s*S?(\d+)", re.IGNORECASE)
_CITE_ONE_RE = re.compile(r"S?(\d+)", re.IGNORECASE)
_MARKER_RE = re.compile(r"\[\^(\d+)\]")


def _ids_in(group: str) -> list[str]:
    ids: list[str] = []
    for part in re.split(r"\s*[,;]\s*", group.strip()):
        span = _CITE_RANGE_RE.fullmatch(part)
        if span:
            low, high = int(span[1]), int(span[2])
            if low <= high and high - low <= 8:
                ids.extend(f"S{i}" for i in range(low, high + 1))
            continue
        single = _CITE_ONE_RE.fullmatch(part)
        if single:
            ids.append(f"S{int(single[1])}")
    return ids


def resolve_citations(
    text: str,
    ids: Mapping[str, tuple[int, int]],
    sources: Sequence[MeetingSource],
) -> tuple[str, list[Citation]]:
    """Turn the model's IDs into numbered ``[^n]`` markers and their sources.

    Numbers follow order of first appearance. IDs the model was not given are
    dropped, and a group with no real IDs left disappears entirely.
    """
    numbers: dict[tuple[int, int], int] = {}
    citations: list[Citation] = []

    def replace(match: re.Match[str]) -> str:
        markers: list[str] = []
        for cite_id in _ids_in(match.group(1)):
            key = ids.get(cite_id)
            if key is None:
                continue
            if key not in numbers:
                numbers[key] = len(numbers) + 1
                citations.append(_citation(numbers[key], sources[key[0]], key[1]))
            marker = f"[^{numbers[key]}]"
            if marker not in markers:
                markers.append(marker)
        return "".join(markers)

    resolved = _CITE_GROUP_RE.sub(replace, text)
    # "[S1] [S1]" in the source becomes the same marker twice in a row.
    resolved = re.sub(r"(\[\^\d+\])(?:\1)+", r"\1", resolved)
    return resolved.strip(), citations


def _citation(n: int, source: MeetingSource, index: int) -> Citation:
    segment = source.segments[index]
    return Citation(
        n=n,
        meeting_id=source.id,
        meeting_title=source.title,
        started_at=source.started_at,
        start=segment.start,
        end=segment.end,
        speaker=segment.speaker,
        text=segment.text,
        before=tuple(source.segments[max(0, index - CONTEXT_BEFORE) : index]),
        after=tuple(source.segments[index + 1 : index + 1 + CONTEXT_AFTER]),
    )


def strip_citations(text: str) -> str:
    """The answer with its ``[^n]`` markers removed."""
    return _MARKER_RE.sub("", text)


# -- answering -------------------------------------------------------------


@dataclass(frozen=True)
class RecallAnswer:
    """An answer, the sources behind it, and how widely it looked."""

    answer: str
    citations: tuple[Citation, ...]
    searched: int
    terms: tuple[str, ...] = ()

    def to_record(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "citations": [c.to_dict() for c in self.citations],
            "searched": self.searched,
            "terms": list(self.terms),
        }


_CITE_RULES = (
    "Every excerpt line begins with an ID in square brackets, like [S12], then "
    "the time into that meeting and the speaker. After each statement you "
    "make, cite the line or lines it rests on by writing their IDs in square "
    "brackets - 'The launch moved to May [S12].' or 'Both agreed [S3][S7].' "
    "Cite the specific line that says it, not a neighbouring one. Use only IDs "
    "that appear in the excerpts, and do not add a list of sources at the end: "
    "the IDs are turned into links."
)

_ASR_RULES = (
    "The transcripts come from automatic speech recognition: expect misheard "
    "words, and generic labels like 'Speaker 0' that are not real names. A "
    "speaker labelled 'You' is the person asking. Quote a line when its exact "
    "words are the answer, and say so when a quote looks garbled."
)

_ARCHIVE_SYSTEM = (
    "You answer questions about a person's past meetings, using excerpts from "
    "their transcripts.\n\n"
    f"{_CITE_RULES}\n\n"
    "Answer only from the excerpts. When they do not contain the answer, say "
    "plainly that you could not find it in the meetings searched, rather than "
    "guessing or falling back on general knowledge.\n\n"
    "When the question asks when or where something was said, name the "
    "meeting and its date, and give the time into the meeting.\n\n"
    f"{_ASR_RULES}\n\n"
    "Be brief and direct. Use a short list when several meetings or items are "
    "involved. No preamble - do not restate the question."
)

_MEETING_SYSTEM = (
    "You answer questions about one meeting, using its transcript and any "
    "notes taken on it.\n\n"
    f"{_CITE_RULES} Cite transcript lines only; the notes have no IDs.\n\n"
    "Answer only from what you were given. If the meeting did not cover it, "
    "say so plainly in one sentence - do not reason outwards from what was "
    "said into what was probably meant, and do not fall back on general "
    "knowledge. 'That did not come up' is a good answer.\n\n"
    f"{_ASR_RULES}\n\n"
    "Be brief and direct. Answer in prose for a question about what happened, "
    "and in a short list when the question asks for several things. No "
    "preamble - do not restate the question."
)


def _replay(history: Sequence[Mapping[str, Any]], turns: int) -> str:
    recent = list(history)[-turns:] if turns > 0 else []
    return "\n\n".join(
        f"Q: {turn.get('question', '')}\nA: {strip_citations(str(turn.get('answer', '')))}"
        for turn in recent
    )


def _ask(
    client: LLMClient,
    system: str,
    parts: Sequence[str],
    question: str,
    history: Sequence[Mapping[str, Any]],
) -> str:
    replay = _replay(history, HISTORY_TURNS)
    prompt = [part for part in parts if part]
    if replay:
        prompt.append(f"--- EARLIER IN THIS CONVERSATION ---\n{replay}")
    prompt.append(f"--- QUESTION ---\n{question}")
    answer = client.complete_text(
        system=system, user="\n\n".join(prompt), max_tokens=ANSWER_MAX_TOKENS
    ).strip()
    if not answer:
        raise RecallError("The model returned an empty answer.")
    return answer


def answer_across_meetings(
    sources: Sequence[MeetingSource],
    question: str,
    client: LLMClient,
    *,
    history: Sequence[Mapping[str, Any]] = (),
    today: date | None = None,
    budget: int = ARCHIVE_CONTEXT_CHARS,
) -> RecallAnswer:
    """Answer ``question`` from every meeting in ``sources``, with citations.

    Raises:
        RecallError: If the question is empty or no meeting has a transcript.
        LLMError: If the provider fails while answering.
    """
    question = question.strip()
    if not question:
        raise RecallError("Ask a question first.")
    searchable = [source for source in sources if source.segments]
    if not searchable:
        raise RecallError(
            "There is nothing to search yet - none of your meetings has a "
            "transcript."
        )
    today = today or date.today()

    whole = sum(s.cost(range(len(s.segments))) + HEADER_OVERHEAD for s in searchable)
    plan = (
        plan_search(question, client, history=history, today=today)
        if whole > budget
        else SearchPlan(question)
    )

    notes: list[str] = []
    in_period = [s for s in searchable if plan.covers(s.day)]
    if plan.has_period and not in_period:
        notes.append(
            f"No meetings fall between {plan.describe_period()}, so every "
            "meeting was searched instead. Say so in your answer."
        )
        in_period = searchable
    elif plan.has_period:
        notes.append(f"Only meetings from {plan.describe_period()} were searched.")

    chosen, note = _select(in_period, plan, budget)
    if note:
        notes.append(note)
    excerpts, ids = _render(in_period, chosen)

    logger.info(
        "Recall over %d meeting(s): %d excerpt line(s), %d search term(s)",
        len(in_period),
        len(ids),
        len(plan.terms),
    )
    raw = _ask(
        client,
        _ARCHIVE_SYSTEM,
        [
            f"Today is {today:%A %d %B %Y}.",
            f"Meetings searched: {len(in_period)}."
            + (" " + " ".join(notes) if notes else ""),
            f"--- EXCERPTS ---\n{excerpts}",
        ],
        question,
        history,
    )
    answer, citations = resolve_citations(raw, ids, in_period)
    return RecallAnswer(answer, tuple(citations), len(in_period), plan.terms)


def answer_about_meeting(
    source: MeetingSource,
    question: str,
    client: LLMClient,
    *,
    notes: str = "",
    history: Sequence[Mapping[str, Any]] = (),
    budget: int = MEETING_CONTEXT_CHARS,
) -> RecallAnswer:
    """Answer ``question`` about one meeting, with citations into its transcript.

    A transcript over ``budget`` keeps its opening and its close and loses
    the middle: the agenda and the conclusions carry the most, and a head- or
    tail-only cut would lose one of them.

    Raises:
        RecallError: If the question is empty, or the meeting has neither a
            transcript nor notes.
        LLMError: If the provider fails while answering.
    """
    question = question.strip()
    if not question:
        raise RecallError("Ask a question first.")
    if not source.segments and not notes.strip():
        raise RecallError(
            "There is nothing to search - this meeting has no transcript and "
            "no notes yet."
        )

    count = len(source.segments)
    indices = set(range(count))
    trimmed = source.cost(indices) > budget
    if trimmed:
        indices = set()
        used = 0
        for i in range(count):
            line = len(source.segments[i].text) + LINE_OVERHEAD
            if used + line > budget // 2:
                break
            indices.add(i)
            used += line
        used = 0
        for i in reversed(range(count)):
            line = len(source.segments[i].text) + LINE_OVERHEAD
            if i in indices or used + line > budget // 2:
                break
            indices.add(i)
            used += line
    excerpts, ids = _render([source], {0: indices})

    parts: list[str] = []
    if notes.strip():
        parts.append(f"--- NOTES ON THE MEETING ---\n{notes.strip()}")
    if ids:
        parts.append(
            "--- TRANSCRIPT ---\n"
            + (
                "(The meeting was long, so the middle of the transcript is "
                "left out.)\n"
                if trimmed
                else ""
            )
            + excerpts
        )
    else:
        parts.append("--- TRANSCRIPT ---\n(Nothing was transcribed for this meeting.)")

    raw = _ask(client, _MEETING_SYSTEM, parts, question, history)
    answer, citations = resolve_citations(raw, ids, [source])
    return RecallAnswer(answer, tuple(citations), 1)


__all__ = [
    "ARCHIVE_CONTEXT_CHARS",
    "HISTORY_TURNS",
    "MEETING_CONTEXT_CHARS",
    "Citation",
    "MeetingSource",
    "RecallAnswer",
    "RecallError",
    "SearchPlan",
    "Segment",
    "answer_about_meeting",
    "answer_across_meetings",
    "plan_search",
    "rank_windows",
    "resolve_citations",
    "segment_utterances",
    "strip_citations",
    "tokenize",
]
