"""Orchestrated dialectic reviewer — the agent-orchestrator's first consumer.

See docs/proposals/orchestrated-dialectic-reviewer-v0.md.
"""
from .reviewer import (
    Thesis,
    Verdict,
    build_review_prompt,
    parse_reviewer_verdict,
)

__all__ = ["Thesis", "Verdict", "build_review_prompt", "parse_reviewer_verdict"]
