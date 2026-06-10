"""S8a default-stamp: classify fresh identities at creation time.

Rule (2-branch, ):
  - ``name`` matches ``KNOWN_RESIDENT_LABELS`` → resident default tags.
  - otherwise → ``["ephemeral"]``.

If the caller already supplied tags, return ``None`` to signal "don't
override." This preserves backward compatibility with callers that stamp
their own class tag at creation time (SDK residents, custom harnesses).

Phase-1 (PR #121, 2026-04-23) wired this from ``onboard()``. Phase-2
(2026-04-30) wires the same stamp into the two ``process_agent_update``
auto-create sites in ``src/mcp_handlers/updates/phases.py`` so identities
born outside the explicit onboard handler aren't left untagged. See
```` for the day-7 audit that surfaced
the gap (72 of 200 in-window identities untagged, including a 441-update
``claude_desktop-claude`` row).

The ``ephemeral → engaged_ephemeral`` promotion rule (S8a Phase-2;
``session_like`` was the pre-ship name) lives in the Vigil sweep that runs
against the corpus this stamp produces; this module deliberately stamps
only the floor-level class.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from src.grounding.class_indicator import KNOWN_RESIDENT_LABELS

# Mirror of agents/sdk/src/unitares_sdk/agent.py:RESIDENT_TAGS. Residents
# need BOTH tags: 'persistent' protects from auto_archive_orphan_agents,
# 'autonomous' exempts from loop-detection pattern 4. Keeping the list in
# sync across SDK and server is a known Phase-1 cost.
RESIDENT_DEFAULT_TAGS = ["persistent", "autonomous"]
EPHEMERAL_DEFAULT_TAGS = ["ephemeral"]


def default_tags_for_onboard(
    name: Optional[str],
    existing_tags: Optional[Iterable[str]] = None,
) -> Optional[list[str]]:
    """Return the default tag list to stamp at onboard, or ``None``.

    Args:
      name: Display name supplied at onboard (e.g. "Lumen"). Matched
        exactly against ``KNOWN_RESIDENT_LABELS``; structured labels like
        "Lumen_abc123" do not match.
      existing_tags: Tags already on the identity metadata. If truthy,
        the caller has asserted class and the default must not override.

    Returns:
      ``None`` when ``existing_tags`` is non-empty (skip stamping).
      ``RESIDENT_DEFAULT_TAGS`` when ``name`` is a known resident.
      ``EPHEMERAL_DEFAULT_TAGS`` otherwise.
    """
    if existing_tags:
        return None

    if name and name in KNOWN_RESIDENT_LABELS:
        return list(RESIDENT_DEFAULT_TAGS)

    return list(EPHEMERAL_DEFAULT_TAGS)


async def stamp_default_class_tags(
    agent_uuid: str,
    name: Optional[str] = None,
    *,
    meta: Optional[Any] = None,
) -> Optional[list[str]]:
    """Stamp the default class tag on a freshly-created identity.

    Synchronous (not background) because the first ``process_agent_update``
    call can immediately follow identity creation, and the class tag governs
    class-conditional calibration, trust-tier routing, and
    archive-orphan-sweep exemptions. If tags aren't persisted before the
    creating call returns, the first check-in classifies as ``default``
    and uses the wrong scale maps.

    Called from three sites:
      1. ``handle_onboard`` in ``src/mcp_handlers/identity/handlers.py``
         (Phase-1, PR #121).
      2. ``execute_locked_update`` in ``src/mcp_handlers/updates/phases.py``
         after ``get_or_create_agent`` (Phase-2 fix for the auto-create gap).
      3. The ``record_agent_state`` ValueError-recovery branch in the same
         phases.py (Phase-2 fix; see also S21-b §1 hydration).

    Args:
      agent_uuid: The freshly-minted identity UUID.
      name: Display name to classify against ``KNOWN_RESIDENT_LABELS``.
        When ``None`` and ``meta`` carries a ``label``, that label is used
        instead — call sites that don't thread name explicitly (the
        recovery branch in particular) still get correct classification.
      meta: Optional in-memory metadata object. When provided, ``meta.tags``
        is checked to skip stamping (preserve caller-asserted classes) and
        ``meta.tags`` is updated in place after the DB write so subsequent
        reads in the same request see the stamp without a metadata reload.
        Pass ``None`` when the caller doesn't have access to the metadata
        cache; the function still writes to PG (source of truth).

    Returns:
      The tag list that was stamped, or ``None`` if no stamp was needed
      (existing_tags non-empty).

    Non-fatal on exception — caller catches and logs. The agent will be
    misclassified until a later ``update_agent_metadata`` call.
    """
    from src import agent_storage

    existing_tags = getattr(meta, "tags", None) if meta is not None else None

    resolved_name = name
    if resolved_name is None and meta is not None:
        resolved_name = getattr(meta, "label", None)

    default_tags = default_tags_for_onboard(resolved_name, existing_tags=existing_tags)
    if default_tags is None:
        return None

    await agent_storage.update_agent(agent_id=agent_uuid, tags=default_tags)
    if meta is not None:
        meta.tags = default_tags
    return default_tags
