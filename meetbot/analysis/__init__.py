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
    answer_question,
    enhance_notes,
    get_template,
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
    "DEFAULT_TEMPLATE",
    "LLMClient",
    "LLMError",
    "MeetingAnalysis",
    "NotepadError",
    "OpenAIClient",
    "TEMPLATES",
    "Template",
    "analyze_transcript",
    "answer_question",
    "build_client",
    "chunk_transcript",
    "enhance_notes",
    "get_template",
    "parse_analysis_payload",
    "probe_llm",
    "render_analysis_markdown",
]
