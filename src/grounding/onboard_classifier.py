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


# Process-local memo of resident UUIDs already reconciled (or confirmed
# healthy) this server process, so the common per-check-in path short-circuits
# without even re-comparing tags. Bounded by the resident roster, never the
# ephemeral population (reconcile is a no-op and never memoizes non-residents).
_reconciled_residents: set[str] = set()

# One-shot guard so the empty-roster warning fires once per process, not on
# every check-in for every agent.
_empty_roster_warned = False


def _warn_empty_roster_once() -> None:
    global _empty_roster_warned
    if _empty_roster_warned:
        return
    _empty_roster_warned = True
    import logging

    logging.getLogger(__name__).warning(
        "resident-tag reconcile: a resident label arrived but "
        "KNOWN_RESIDENT_LABELS is empty — UNITARES_RESIDENTS is unset or "
        "misconfigured. Resident tag reconcile AND the tag_audit endpoint are "
        "no-ops until it is set; resident tag gaps will not self-heal."
    )


async def reconcile_resident_tags(
    agent_uuid: str,
    name: Optional[str] = None,
    *,
    meta: Optional[Any] = None,
) -> Optional[list[str]]:
    """Re-union ``RESIDENT_DEFAULT_TAGS`` onto an EXISTING resident identity.

    The creation-time stamp (``stamp_default_class_tags``) only fires when an
    identity is first minted. Residents resume an existing UUID, so that stamp
    never re-runs. Two ways the required tags then go missing without anything
    re-adding them:

      - an identity minted before ``autonomous`` was required, an
        archive/resume cycle, or a tag-replacing ``update_agent`` write; and
      - a resident that never calls ``onboard`` at all. The BEAM Sentinel
        only ever attaches ``agent_id`` to ``process_agent_update`` (see
        ``elixir/sentinel/lib/unitares_sentinel/governance_checkin.ex``), so
        it has no onboard handshake to stamp from — the SDK-side fix
        (``GovernanceAgent._reconcile_resident_tags``, #754) never reaches it.

    This is the substrate-agnostic server-side equivalent of #754: every
    client checks in through ``process_agent_update``, so reconciling on the
    check-in path covers SDK, BEAM, and REST residents alike. Without it,
    Vigil's ``resident_tag_hygiene`` flags the gap on every cycle until
    someone re-tags by hand (Sentinel 2026-06-14/15).

    Additive on purpose: writes the UNION so role/cadence tags (e.g.
    ``cadence.10min``) survive — ``update_agent`` REPLACES the list. Cheap on
    the healthy path: a memo hit, or a subset test on in-memory tags; only the
    rare gap touches the DB. No-op (returns ``None``) for non-resident labels
    or when the tags are already present. Memoized per UUID per process.

    Semantics note: these are safety-exemption tags, not behavioral signals, so
    re-adding them restores intended protection rather than hiding a fault.
    There is no opt-out — a *deliberate* removal of a required tag from a
    resident is silently reverted on the next check-in (matching the SDK's
    #754 behavior). If a resident must be subjected to loop-detection
    pattern 4, use a different lever than tag removal.

    Args:
      agent_uuid: The resident's identity UUID.
      name: Label to classify against ``KNOWN_RESIDENT_LABELS``. Falls back to
        ``meta.label`` when ``None``.
      meta: In-memory metadata. ``meta.tags`` is the cheap tag source and is
        updated in place after a write so later reads in the same process see
        the reconciled set. When ``None`` the current tags are read from PG.

    Returns:
      The merged tag list that was written, or ``None`` when no write was
      needed (non-resident, already healthy, or already memoized).

    Non-fatal on exception — caller catches and logs; the gap stays visible to
    Vigil until a later check-in succeeds.
    """
    if agent_uuid in _reconciled_residents:
        return None

    resolved_name = name
    if resolved_name is None and meta is not None:
        resolved_name = getattr(meta, "label", None)
    if not resolved_name or resolved_name not in KNOWN_RESIDENT_LABELS:
        # Surface the silent-inert failure mode: if a real label arrives but
        # the roster is empty, UNITARES_RESIDENTS is unset/misconfigured and
        # this reconcile (and the audit endpoint) are no-ops fleet-wide. Warn
        # once per process so a dropped env var on deploy is visible rather
        # than masquerading as a healthy fleet.
        if resolved_name and not KNOWN_RESIDENT_LABELS:
            _warn_empty_roster_once()
        return None

    from src import agent_storage

    if meta is not None and getattr(meta, "tags", None) is not None:
        current = list(meta.tags)
    else:
        record = await agent_storage.get_agent(agent_uuid)
        current = list(getattr(record, "tags", None) or []) if record is not None else []

    missing = [t for t in RESIDENT_DEFAULT_TAGS if t not in current]
    if not missing:
        # Already healthy — memoize so we don't re-compare every check-in.
        _reconciled_residents.add(agent_uuid)
        return None

    merged = current + missing  # additive — preserves role/cadence tags
    # Persist before the in-memory mutation and before memoizing (P011): a
    # failed DB write must leave meta.tags untouched and the UUID un-memoized
    # so the next check-in retries. Two concurrent check-ins that both race
    # past the memo here will issue an idempotent double-write (same merged
    # list) — harmless, and bounded to at most once per process per resident.
    await agent_storage.update_agent(agent_id=agent_uuid, tags=merged)
    if meta is not None:
        meta.tags = merged
    _reconciled_residents.add(agent_uuid)
    return merged
