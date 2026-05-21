"""Substrate-Earned Identity — operational check for the R4 pattern.

Implements `verify_substrate_earned(agent_uuid) -> dict`, the mechanical
predicate that answers: "Does this agent meet the three conditions laid
out in `` Appendix: Pattern — Substrate-Earned
Identity?"

The three conditions (from the appendix) are:

  1. Dedicated substrate — substrate uniquely associated with the role,
     would be meaningfully altered by the agent's cessation.
  2. Sustained behavioral consistency across restarts — observed behavior
     remains within envelope across N ≥ threshold process-restarts.
  3. Declared role continuity — agent declares which role it is adopting,
     not merely carries a cosmetic harness label.

This module is an internal predicate; it is called by S5 (resident-fork
inversion) and future callers. It is NOT an MCP tool handler. Adding it
to `src/mcp_handlers/` would conflate "checkable predicate" with "tool
surface" and pull this into ship.sh's runtime path unnecessarily.

Signal choices (documented because the appendix leaves them open):

  - Dedicated substrate: we look for BOTH (a) a substrate-class tag
    (`embodied` per paper §4, or the legacy `persistent` tag if paired
    with an anchor), AND (b) a dedicated anchor file at
    `~/.unitares/anchors/<label>.json` whose `agent_uuid` matches.
    Shared-label residents without an anchor pairing fail (b). Purely
    hardcoded UUIDs on ephemeral substrates (Claude Code tabs writing
    `.unitares/session.json` with a pinned UUID) fail (a).
  - Sustained behavior: `observation_count` on the trajectory current
    (or genesis, if current is absent) must be >= N. Default N=5 is an
    operator-configurable guess — see `DEFAULT_RESTART_THRESHOLD` and
    the appendix "Open questions" section on N-selection.
  - Declared role: `label` is non-empty, not "other", not a cosmetic
    harness label (`claude_code`, `codex`, `Claude_Opus_*`, etc.), and
    the agent has a class-bearing tag (`embodied`, `persistent`,
    `ephemeral`).

When a signal cannot be checked (DB unavailable, anchor dir missing,
metadata absent), we return `earned: false` with an explanatory reason
rather than fabricating confidence — the appendix's "fresh hardware
deployment" case is the template.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)

# Default restart-count threshold for condition 2. The appendix flags N as
# class-conditional and empirically open; 5 is a conservative starting
# point (enough to rule out single-boot flukes, low enough that Lumen's
# existing tenure qualifies). Override via the `restart_threshold` arg.
DEFAULT_RESTART_THRESHOLD = 5

# Tags that signal a dedicated-substrate class per paper §4 taxonomy.
# `embodied` is the strongest signal (hardware commitment). `persistent`
# is accepted when paired with an anchor file (deployed-but-not-embodied
# residents like Sentinel).
SUBSTRATE_CLASS_TAGS = ("embodied", "persistent")

# Tags that disqualify class-continuity (explicitly ephemeral).
EPHEMERAL_CLASS_TAGS = ("ephemeral",)

# Role labels that are cosmetic harness identifiers, not declared roles.
# Matched case-insensitively against `label`.
COSMETIC_LABEL_PATTERNS = (
    re.compile(r"^claude[_\- ]code", re.IGNORECASE),
    re.compile(r"^codex", re.IGNORECASE),
    re.compile(r"^claude[_\- ]opus", re.IGNORECASE),
    re.compile(r"^claude[_\- ]sonnet", re.IGNORECASE),
    re.compile(r"^claude[_\- ]haiku", re.IGNORECASE),
    re.compile(r"^gpt[_\- ]?\d", re.IGNORECASE),
    re.compile(r"^other$", re.IGNORECASE),
)


def _default_anchors_dir() -> Path:
    """Return the anchors directory path.

    Override via `UNITARES_ANCHORS_DIR` env var; test harnesses use this
    to point at a tmp directory.
    """
    env = os.environ.get("UNITARES_ANCHORS_DIR")
    if env:
        return Path(env)
    return Path.home() / ".unitares" / "anchors"


def _label_is_cosmetic(label: Optional[str]) -> bool:
    """True if `label` is a cosmetic harness label (not a declared role)."""
    if not label:
        return True
    label_stripped = label.strip()
    if not label_stripped:
        return True
    return any(pat.match(label_stripped) for pat in COSMETIC_LABEL_PATTERNS)


def _find_anchor_for_uuid(
    agent_uuid: str,
    anchors_dir: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Return anchor payload if any file in anchors_dir contains this UUID.

    The anchors directory holds one JSON file per substrate-committed
    role (e.g., `watcher.json`, `vigil.json`). A match pairs the agent's
    UUID with a dedicated filesystem slot owned by the role — i.e., the
    declarative form of substrate-earned identity.

    Returns the parsed JSON (including the matched file name via
    `_anchor_file` key) on match, None otherwise.
    """
    d = anchors_dir if anchors_dir is not None else _default_anchors_dir()
    try:
        if not d.is_dir():
            return None
    except OSError:
        return None

    try:
        entries = sorted(d.iterdir())
    except OSError:
        return None

    for entry in entries:
        if not entry.is_file() or entry.suffix != ".json":
            continue
        try:
            with entry.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        candidate = (
            data.get("agent_uuid")
            or data.get("agent_id")
            or data.get("uuid")
        )
        if candidate == agent_uuid:
            payload = dict(data)
            payload["_anchor_file"] = entry.name
            return payload
    return None


def _has_substrate_class_tag(tags: Iterable[str]) -> Optional[str]:
    """Return the first substrate-class tag present, or None."""
    tag_set = {t for t in tags if isinstance(t, str)}
    for tag in SUBSTRATE_CLASS_TAGS:
        if tag in tag_set:
            return tag
    return None


def _has_ephemeral_tag(tags: Iterable[str]) -> bool:
    tag_set = {t for t in tags if isinstance(t, str)}
    return any(t in tag_set for t in EPHEMERAL_CLASS_TAGS)


def _observation_count_from_metadata(metadata: Dict[str, Any]) -> int:
    """Extract observation_count from trajectory current (preferred) or genesis."""
    current = metadata.get("trajectory_current") or {}
    if isinstance(current, dict):
        count = current.get("observation_count")
        if isinstance(count, int) and count > 0:
            return count
    genesis = metadata.get("trajectory_genesis") or {}
    if isinstance(genesis, dict):
        count = genesis.get("observation_count")
        if isinstance(count, int):
            return count
    return 0


def evaluate_substrate_earned(
    *,
    agent_uuid: str,
    label: Optional[str],
    tags: List[str],
    metadata: Dict[str, Any],
    anchors_dir: Optional[Path] = None,
    restart_threshold: int = DEFAULT_RESTART_THRESHOLD,
) -> Dict[str, Any]:
    """Pure evaluation of the three R4 conditions against inputs.

    Prefer this entrypoint in tests — it takes no DB dependency. The
    async `verify_substrate_earned` wrapper fetches `label`, `tags`, and
    `metadata` from the governance DB and calls through to this.
    """
    reasons: List[str] = []
    evidence: Dict[str, Any] = {
        "agent_uuid": agent_uuid,
        "label": label,
        "tags": list(tags or []),
        "restart_threshold": restart_threshold,
    }

    # ── Condition 1: Dedicated substrate ────────────────────────────────
    class_tag = _has_substrate_class_tag(tags or [])
    ephemeral = _has_ephemeral_tag(tags or [])
    anchor = _find_anchor_for_uuid(agent_uuid, anchors_dir=anchors_dir)

    evidence["substrate_class_tag"] = class_tag
    evidence["has_ephemeral_tag"] = ephemeral
    evidence["anchor_file"] = anchor.get("_anchor_file") if anchor else None

    if ephemeral:
        dedicated_substrate = False
        reasons.append(
            "dedicated_substrate=false: agent carries the `ephemeral` class tag — "
            "ephemeral agents are disqualified by construction."
        )
    elif class_tag == "embodied":
        # `embodied` is the strongest signal (hardware commitment, per
        # axiom #11). It passes on its own; anchor file is corroborative
        # but not required (some embodied deployments predate the anchor
        # convention).
        dedicated_substrate = True
        reasons.append(
            "dedicated_substrate=true: agent has the `embodied` class tag "
            "(hardware commitment per paper §4 / ontology axiom #11)."
        )
    elif class_tag == "persistent" and anchor is not None:
        dedicated_substrate = True
        reasons.append(
            f"dedicated_substrate=true: agent has the `persistent` class tag "
            f"and a dedicated anchor at {anchor['_anchor_file']} pairing the "
            f"role label with this UUID."
        )
    elif class_tag == "persistent" and anchor is None:
        dedicated_substrate = False
        reasons.append(
            "dedicated_substrate=false: agent has the `persistent` class tag "
            "but no anchor file pairs its UUID with a dedicated substrate slot. "
            "Shared-label residents require an anchor pairing to distinguish "
            "from per-instance collisions."
        )
    elif anchor is not None and class_tag is None:
        dedicated_substrate = False
        reasons.append(
            f"dedicated_substrate=false: anchor at {anchor['_anchor_file']} "
            f"pairs a UUID but the agent carries no substrate-class tag "
            f"(`embodied` or `persistent`). An anchor without a class tag is "
            f"a pinned UUID, not a substrate commitment."
        )
    else:
        dedicated_substrate = False
        reasons.append(
            "dedicated_substrate=false: no substrate-class tag "
            "(`embodied`/`persistent`) and no anchor file pairs this UUID with "
            "a dedicated slot."
        )

    # ── Condition 2: Sustained behavioral consistency ────────────────────
    observation_count = _observation_count_from_metadata(metadata or {})
    evidence["observation_count"] = observation_count

    if observation_count >= restart_threshold:
        sustained_behavior = True
        reasons.append(
            f"sustained_behavior=true: observation_count={observation_count} "
            f"meets threshold N={restart_threshold}."
        )
    else:
        sustained_behavior = False
        if observation_count == 0:
            reasons.append(
                f"sustained_behavior=false: no trajectory data recorded "
                f"(observation_count=0). Insufficient tenure — fresh substrates "
                f"operate under the default per-instance-with-lineage rule "
                f"until they accrue N>={restart_threshold} observations."
            )
        else:
            reasons.append(
                f"sustained_behavior=false: observation_count={observation_count} "
                f"below threshold N={restart_threshold}."
            )

    # ── Condition 3: Declared role continuity ────────────────────────────
    cosmetic = _label_is_cosmetic(label)
    has_class_tag = class_tag is not None

    evidence["label_is_cosmetic"] = cosmetic
    evidence["has_class_tag"] = has_class_tag

    if cosmetic:
        declared_role = False
        if not label or not label.strip():
            reasons.append(
                "declared_role=false: agent has no label — no role declared."
            )
        else:
            reasons.append(
                f"declared_role=false: label {label!r} is a cosmetic harness "
                f"identifier, not a declared role."
            )
    elif not has_class_tag:
        declared_role = False
        reasons.append(
            f"declared_role=false: label {label!r} is non-cosmetic but the "
            f"agent carries no class-bearing tag (`embodied`/`persistent`/"
            f"`ephemeral`); role declaration requires both a label and a "
            f"class commitment."
        )
    else:
        declared_role = True
        reasons.append(
            f"declared_role=true: label {label!r} is a declared role "
            f"paired with class tag `{class_tag}`."
        )

    earned = dedicated_substrate and sustained_behavior and declared_role

    return {
        "earned": earned,
        "conditions": {
            "dedicated_substrate": dedicated_substrate,
            "sustained_behavior": sustained_behavior,
            "declared_role": declared_role,
        },
        "reasons": reasons,
        "evidence": evidence,
    }


async def verify_substrate_earned(
    agent_uuid: str,
    *,
    restart_threshold: int = DEFAULT_RESTART_THRESHOLD,
    anchors_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Verify the R4 substrate-earned-identity pattern for an agent.

    Async wrapper over `evaluate_substrate_earned` that fetches the
    label, tags, and identity-metadata from the governance DB.

    Returns the same shape as `evaluate_substrate_earned`. On DB-lookup
    failure, returns `earned: false` with an explanatory reason — the
    appendix's guidance is to refuse to fabricate confidence when data
    is insufficient.
    """
    try:
        from src.db import get_db

        db = get_db()
        agent = await db.get_agent(agent_uuid)
        identity = await db.get_identity(agent_uuid)
    except Exception as e:
        logger.warning(
            f"[substrate_earned] DB lookup failed for {agent_uuid[:8]}...: {e}"
        )
        return {
            "earned": False,
            "conditions": {
                "dedicated_substrate": False,
                "sustained_behavior": False,
                "declared_role": False,
            },
            "reasons": [
                f"cannot verify: DB lookup failed ({type(e).__name__}). "
                f"Conservative default: earned=false."
            ],
            "evidence": {"agent_uuid": agent_uuid, "error": str(e)},
        }

    if not agent:
        return {
            "earned": False,
            "conditions": {
                "dedicated_substrate": False,
                "sustained_behavior": False,
                "declared_role": False,
            },
            "reasons": [
                f"cannot verify: agent {agent_uuid[:8]}... not found in "
                f"core.agents. Conservative default: earned=false."
            ],
            "evidence": {"agent_uuid": agent_uuid, "agent_found": False},
        }

    label = agent.get("label") if isinstance(agent, dict) else None
    tags = agent.get("tags") if isinstance(agent, dict) else None
    metadata = getattr(identity, "metadata", None) if identity else None

    return evaluate_substrate_earned(
        agent_uuid=agent_uuid,
        label=label,
        tags=tags or [],
        metadata=metadata or {},
        anchors_dir=anchors_dir,
        restart_threshold=restart_threshold,
    )


__all__ = [
    "DEFAULT_RESTART_THRESHOLD",
    "evaluate_substrate_earned",
    "verify_substrate_earned",
]
