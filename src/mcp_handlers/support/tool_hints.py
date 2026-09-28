"""Canonical, reusable agent-facing tool hints.

Centralizing these strings prevents copy drift across handlers when APIs evolve.
"""

# Canonical knowledge search tool name/path
KNOWLEDGE_SEARCH_TOOL = "knowledge"

# Canonical messaging snippets
KNOWLEDGE_SEARCH_SUGGESTION = (
    "Use knowledge(action='search') to find relevant discoveries by tags or type."
)
KNOWLEDGE_OPEN_QUESTIONS_WORKFLOW = (
    "1. knowledge(action='search', discovery_type='question', status='open') "
    "2. Use the discovery_id in response_to"
)

