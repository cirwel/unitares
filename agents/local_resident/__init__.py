"""Scaffolding for a local model that participates in the fleet as a member.

Extracted from ``agents/dialectic_reviewer/reviewer.py``, which has been the
fleet's only local-model member since 2026-06-23 and works: it onboards as its
own governance identity, runs gemma4 in its own process, forms a verdict that
can disagree, submits through the ordinary protocol tools, and exits under
orchestrator supervision. What it is not is reusable — every one of those steps
is interleaved with dialectic specifics.

This module is the half that generalises: identity lifecycle, the in-process
model call, and the exit contract. The job itself stays with the resident.

The reviewer is deliberately NOT refactored onto this yet. It is live, it is the
only working instance, and rewriting the proof while extracting from it would
leave nothing to check the extraction against. Port it once a second resident
has shown the shape holds.
"""

from .runner import (
    ResidentSpec,
    call_local_model,
    extract_model_text,
    run_local_resident,
)

__all__ = [
    "ResidentSpec",
    "call_local_model",
    "extract_model_text",
    "run_local_resident",
]
