"""Gateway constants — ports, URLs, help text."""

import os

# Deployment-overridable via env; defaults suit a local single-host install.
GOVERNANCE_URL = os.getenv("GOVERNANCE_URL", "http://localhost:8767/mcp/")
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8768"))
GATEWAY_HOST = os.getenv("GATEWAY_HOST", "127.0.0.1")

# Circuit breaker defaults
CIRCUIT_THRESHOLD = 2
CIRCUIT_BACKOFF_BASE = 15.0
CIRCUIT_BACKOFF_MAX = 120.0

# httpx timeout (seconds)
REQUEST_TIMEOUT = 30.0

HELP_TEXT = {
    "tools": [
        {
            "name": "status",
            "description": "Get agent EISV state, coherence, verdict, basin",
            "parameters": {"agent_id": "(optional) Agent ID to query"},
            "example": 'status(agent_id="my-agent")',
        },
        {
            "name": "checkin",
            "description": "Report work and get a governance verdict",
            "parameters": {
                "summary": "What you did",
                "complexity": "(optional) 0.0-1.0, default 0.5",
                "confidence": "(optional) 0.0-1.0, default 0.7",
            },
            "example": 'checkin(summary="Fixed auth bug", complexity=0.4, confidence=0.8)',
        },
        {
            "name": "search",
            "description": "Search the shared knowledge graph",
            "parameters": {
                "query": "Search terms",
                "limit": "(optional) Max results, default 5",
            },
            "example": 'search(query="circuit breaker pattern")',
        },
        {
            "name": "note",
            "description": "Leave a note or discovery in the knowledge graph",
            "parameters": {
                "content": "Note text",
                "tags": "(optional) Comma-separated tags",
            },
            "example": 'note(content="Redis cache helps with session lookup", tags="redis,performance")',
        },
        {
            "name": "query",
            "description": "Natural language question — routed to the right tool automatically",
            "parameters": {"question": "Your question in plain English"},
            "example": 'query(question="What is my current coherence?")',
        },
        {
            "name": "help",
            "description": "Show this help text",
            "parameters": {},
            "example": "help()",
        },
    ]
}
