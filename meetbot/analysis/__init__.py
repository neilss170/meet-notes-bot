"""LLM-based post-meeting analysis."""

from __future__ import annotations

from meetbot.analysis.llm import (
    AnthropicClient,
    LLMClient,
    LLMError,
    OpenAIClient,
    build_client,
    probe_llm,
)
from meetbot.analysis.notepad import (
    DEFAULT_TEMPLATE,
    TEMPLATES,
    NotepadError,
    Template,
    enhance_notes,
    get_template,
)
from meetbot.analysis.recall import (
    Citation,
    MeetingSource,
    RecallAnswer,
    RecallError,
    answer_about_meeting,
    answer_across_meetings,
)
from meetbot.analysis.summarize import (
    ActionItem,
    AnalysisError,
    MeetingAnalysis,
    analyze_transcript,
    chunk_transcript,
    parse_analysis_payload,
    render_analysis_markdown,
)

__all__ = [
    "ActionItem",
    "AnalysisError",
    "AnthropicClient",
    "Citation",
    "DEFAULT_TEMPLATE",
    "LLMClient",
    "LLMError",
    "MeetingAnalysis",
    "MeetingSource",
    "NotepadError",
    "OpenAIClient",
    "RecallAnswer",
    "RecallError",
    "TEMPLATES",
    "Template",
    "analyze_transcript",
    "answer_about_meeting",
    "answer_across_meetings",
    "build_client",
    "chunk_transcript",
    "enhance_notes",
    "get_template",
    "parse_analysis_payload",
    "probe_llm",
    "render_analysis_markdown",
]
