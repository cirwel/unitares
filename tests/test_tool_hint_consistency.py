from pathlib import Path

from src.mcp_handlers.support.tool_hints import (
    KNOWLEDGE_OPEN_QUESTIONS_WORKFLOW,
    KNOWLEDGE_SEARCH_SUGGESTION,
    KNOWLEDGE_SEARCH_TOOL,
)


def test_canonical_knowledge_hint_strings_are_stable():
    assert KNOWLEDGE_SEARCH_TOOL == "knowledge"
    assert "knowledge(action='search')" in KNOWLEDGE_SEARCH_SUGGESTION
    assert "knowledge(action='search'" in KNOWLEDGE_OPEN_QUESTIONS_WORKFLOW


def test_no_legacy_search_guidance_strings_in_key_handlers():
    root = Path(__file__).resolve().parents[1]
    files = [
        root / "src/mcp_handlers/updates/phases.py",
        root / "src/mcp_handlers/knowledge/handlers.py",
        root / "src/mcp_handlers/tool_stability.py",
    ]
    forbidden_fragments = [
        "Use search_knowledge_graph",
        '"related_tools": ["search_knowledge_graph"]',
        "search_knowledge_graph(discovery_type='question')",
    ]

    for file_path in files:
        text = file_path.read_text(encoding="utf-8")
        for fragment in forbidden_fragments:
            assert fragment not in text, f"Found legacy guidance '{fragment}' in {file_path}"

