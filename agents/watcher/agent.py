#!/usr/bin/env python3
"""
Watcher — Independent Bug-Pattern Observer

A non-blocking agent that scans recently edited code for known-bad patterns
using a local LLM (qwen3-coder-next via Ollama, called directly at localhost:11434).
Unlike Vigil (cron, janitorial) and Sentinel (continuous, analytical), Watcher
is event-driven — fired by PostToolUse hooks on Edit/Write.

Successor to Anthropic's deprecated Ogler (Claude Code 2.1.96 companion), built
local and governance-native so it survives any upstream feature churn.

Usage:
    watcher_agent.py --file <path>                  # scan a file
    watcher_agent.py --file <path> --region L1-L40  # scan a region
    watcher_agent.py --self-test                    # run on a synthetic bug
    watcher_agent.py --list-findings                # dump current findings

Architecture:
    1. Load pattern library (agents/watcher/patterns.md)
    2. Read target file + optional region
    3. Build prompt (pattern list + code)
    4. POST directly to Ollama at localhost:11434 (OpenAI-compat endpoint)
    5. Parse JSON findings, dedup against data/watcher/dedup.json
    6. Append new findings to data/watcher/findings.jsonl
    7. Route by severity:
       - low/medium → file only
       - high       → file + mark for SessionStart surfacing
       - critical   → file + surfacing + (optional) Lumen voice + KG discovery

Design notes:
    - Never blocks the editor. The PostToolUse hook forks this script and exits.
    - Persistent governance identity via SyncGovernanceClient (REST transport).
      Checks in after surface_pending; resolution events posted on --resolve/--dismiss.
      Inference does NOT route through governance call_model — that path has a
      30s server-side ceiling and drops token counts; direct Ollama is the
      natural path for a local-LLM pattern scanner.
    - Env-configurable: WATCHER_MODEL, WATCHER_TIMEOUT, WATCHER_OLLAMA_URL.
    - Findings-state mutations acquire the lease-plane surface for
      data/watcher. Default is advisory; set WATCHER_FINDINGS_LEASE_MODE=enforce
      to block on held_by_other / unavailable lease outcomes.
    - Findings are append-only; lifecycle (resolved/dismissed/aged-out) happens
      via the surface hook and explicit user action.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths & Config
# ---------------------------------------------------------------------------

# Put the repo root on sys.path before the first `agents.*` import. Hooks
# invoke this script by absolute path from arbitrary cwd (e.g. $HOME), so
# Python only adds `agents/watcher/` to sys.path by default — the `agents`
# package is not importable without this. PROJECT_ROOT is re-exported from
# _util as the canonical reference for downstream consumers, but we can't
# import _util until after the path is patched.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agents.watcher._util import (
    LOG_FILE,
    MAX_LOG_LINES,
    PROJECT_ROOT,
    hash_line_content,
    log,
    migrate_legacy_watcher_state,
    repo_relative_path,
)
from agents.common.log import trim_log as _common_trim_log
from agents.common.findings import post_finding
from agents.watcher import findings as _findings_mod
from agents.watcher.findings import (
    DEDUP_FILE,
    FINDINGS_FILE,
    FINDINGS_TTL_DAYS,
    GOV_REST_URL,
    MIN_FINGERPRINT_PREFIX,
    STATE_DIR,
    VALID_FINDING_STATUSES,
    Finding,
    _escalate_to_kg,
    _format_findings_block,
    _iter_findings_raw,
    _label_for_other_worktree,
    _partition_findings_by_scope,
    _resolve_session_scope_root,
    _sweep_stale_quiet,
    _sweep_token_drift_quiet,
    _write_findings_atomic,
    compact_findings,
    escalate,
    load_dedup,
    match_fingerprint,
    persist_finding,
    persist_findings,
    print_unresolved,
    save_dedup,
    sweep_stale_dedup,
    sweep_stale_findings,
    update_finding_status,
)

PATTERNS_FILE = Path(__file__).resolve().parent / "patterns.md"

OLLAMA_URL = os.environ.get(
    "WATCHER_OLLAMA_URL", "http://localhost:11434/v1/chat/completions"
)
DEFAULT_MODEL = os.environ.get("WATCHER_MODEL", "qwen3-coder-next:latest")
DEFAULT_TIMEOUT = int(os.environ.get("WATCHER_TIMEOUT", "90"))

WATCHER_FINDINGS_LEASE_MODE_ENV = "WATCHER_FINDINGS_LEASE_MODE"
WATCHER_FINDINGS_LEASE_TTL_ENV = "WATCHER_FINDINGS_LEASE_TTL_S"
WATCHER_FINDINGS_LEASE_DEFAULT_TTL_S = 120
WATCHER_FINDINGS_LEASE_BLOCK_RC = 3
_LEASE_ACQUIRED_OUTCOMES = {"acquired_new", "acquired_idempotent"}

# How many lines of context to include when no explicit region is given.
# Qwen3-Coder-Next (the current default detector) has a 256K context window,
# and should_skip() already caps at 256KB of file bytes (~6500 lines at
# typical density), so DEFAULT_CONTEXT_LINES is effectively a last-resort
# sanity cap rather than a real limit. The old 200-line value was a
# gemma4-era relic that silently truncated scans to the file head and
# missed every bug past line 200. Ogler's third-round self-review caught
# it on 2026-04-11.
DEFAULT_CONTEXT_LINES = 10000

# ---------------------------------------------------------------------------
# Identity — persistent governance presence
# ---------------------------------------------------------------------------

# Anchor-scoped: one Watcher identity per host, shared across every git
# worktree. The legacy per-worktree path (PROJECT_ROOT/.watcher_session)
# minted a fresh UUID on the first edit in each new worktree, producing
# N Watchers per developer instead of one.
SESSION_FILE = Path.home() / ".unitares" / "anchors" / "watcher.json"
LEGACY_SESSION_FILE = PROJECT_ROOT / ".watcher_session"

_watcher_identity: dict[str, str] | None = None


def _load_session() -> dict[str, str]:
    """Load persistent identity from the anchor, migrating from legacy if needed."""
    if SESSION_FILE.exists():
        try:
            return json.loads(SESSION_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    if LEGACY_SESSION_FILE.exists():
        try:
            from unitares_sdk.utils import atomic_write

            data = json.loads(LEGACY_SESSION_FILE.read_text())
            atomic_write(SESSION_FILE, json.dumps(data))
            log(f"migrated watcher identity from {LEGACY_SESSION_FILE} to {SESSION_FILE}")
            return data
        except (json.JSONDecodeError, OSError, ImportError) as e:
            log(f"legacy session migration failed: {e}", "warning")
    return {}


def _save_session(client_session_id: str, continuity_token: str, agent_uuid: str) -> None:
    """Persist identity state to the anchor.

    Uses atomic_write (0600) because this file carries a live continuity
    token — Watcher runs on every PostToolUse edit as the same UID as
    every other agent on this host, so a world-readable anchor would let
    any sibling process impersonate Watcher against the governance API.
    """
    data = {
        "client_session_id": client_session_id,
        "continuity_token": continuity_token,
        "agent_uuid": agent_uuid,
    }
    try:
        from unitares_sdk.utils import atomic_write

        atomic_write(SESSION_FILE, json.dumps(data))
    except (OSError, ImportError) as e:
        log(f"failed to save session: {e}", "warning")


def resolve_identity(client) -> None:
    """Resolve Watcher identity via proof-owned UUID-direct → fresh onboard.

    Name-resume (previous Step 2) was removed 2026-04-17 when the server-side
    name-claim path was deleted. Without it, every `identity(name="Watcher")`
    call forks a fresh UUID, which is exactly what happened: 21 Watcher
    forks in ~2h before this fix. PATH 0 (UUID-direct) takes its place —
    strongest signal, unambiguous, unchallengeable by name-collision bugs.

    Timeout discipline (added 2026-04-17 after a 34-fork incident):
    a transient governance timeout must NOT fall through to onboard — the
    stored UUID is probably still valid, and onboarding forks a new agent
    every time the server is slow. On GovernanceTimeoutError we just skip
    this cycle; a later cycle will retry PATH 0.

    Sets module-level _watcher_identity on success, leaves it None on failure.
    """
    from unitares_sdk.errors import GovernanceTimeoutError

    global _watcher_identity
    saved = _load_session()

    # Step 0: UUID-direct (PATH 0) — strongest resume signal when proof-owned.
    # Identity Honesty Part C + S1-c: server requires continuity_token alongside
    # agent_uuid for PATH 0 resume, and token-only resume is retired. Pass the
    # token explicitly; generic client auto-injection is intentionally token-free.
    if saved.get("agent_uuid"):
        token = saved.get("continuity_token") or getattr(client, "continuity_token", None)
        if token and not getattr(client, "continuity_token", None):
            client.continuity_token = token
        # Attempt PATH 0 even when token-less, provided we are UDS-anchored.
        # A substrate-enrolled resident connecting over UNITARES_UDS_SOCKET
        # resumes via kernel-attested peer match: the server's S19 PR3e gate
        # treats the UDS peer credential as ownership proof equivalent to the
        # continuity_token. The token-only anchor under UDS deliberately drops
        # the token at rest (anchor-disclosure defense — a leaked token would
        # otherwise satisfy PATH 0's _partc_owned and bypass peer attestation),
        # so REQUIRING a token here would make an enrolled resident permanently
        # unresumable (#727). Pass the token when we have it; otherwise rely on
        # the server's substrate gate. Over plain HTTP a token-less resume has
        # no proof channel and the server refuses, so we skip the pointless
        # round-trip and fall through to the fresh-onboard guard.
        uds_anchored = bool(os.environ.get("UNITARES_UDS_SOCKET"))
        if token or uds_anchored:
            try:
                identity_kwargs: dict = {
                    "agent_uuid": saved["agent_uuid"],
                    "resume": True,
                }
                if token:
                    identity_kwargs["continuity_token"] = token
                client.identity(**identity_kwargs)
                _sync_identity(client)
                return
            except GovernanceTimeoutError as e:
                # Transient server slowness — don't fork a new agent. Skip this
                # cycle; the stored UUID remains the ground truth.
                log(f"uuid-direct resume timed out ({e}) — skipping, will retry next cycle", "warning")
                _watcher_identity = None
                return
            except Exception as e:
                log(f"uuid-direct resume failed: {e}", "warning")
        else:
            log(
                "uuid-direct resume skipped: no continuity_token and not UDS-anchored "
                "(set UNITARES_UDS_SOCKET so a substrate-enrolled resident can resume "
                "via kernel-attested peer match)",
                "warning",
            )

    # Step 1: Fresh onboard — only when no proof-owned UUID rebind works.
    # Silent-fork guard (added 2026-04-19 anchor-resilience series): Watcher
    # is a resident; missing anchor + no UNITARES_FIRST_RUN means the
    # operator did not authorize a fresh identity. Refuse loudly rather
    # than silently forking.
    if os.environ.get("UNITARES_FIRST_RUN") != "1":
        log(
            "anchor missing and UNITARES_FIRST_RUN not set — refusing to fresh-onboard. "
            "Restore the anchor from a rotation backup, or set UNITARES_FIRST_RUN=1 "
            "to explicitly bootstrap a new Watcher identity.",
            "error",
        )
        _watcher_identity = None
        return
    try:
        client.onboard("Watcher", spawn_reason="resident_observer")
        _sync_identity(client)
        # Stamp the resident tag set:
        #   - 'persistent':  auto_archive_orphan_agents skips this identity
        #                    (is_agent_protected in src/agent_lifecycle.py).
        #                    Without it, low-activity windows cause orphan-sweep
        #                    false-positives and Watcher gets archived-then-
        #                    silently-resurrected every cycle.
        #   - 'autonomous':  exempts from loop-detection pattern 4
        #                    (agent_loop_detection.py:216). Watcher runs on
        #                    every edit — pattern-4 rejection would starve its
        #                    state writes (Steward 2026-04-20 regression).
        if _watcher_identity and _watcher_identity.get("agent_uuid"):
            from unitares_sdk.agent import RESIDENT_TAGS
            try:
                client.call_tool(
                    "update_agent_metadata",
                    {
                        "agent_id": _watcher_identity["agent_uuid"],
                        "tags": RESIDENT_TAGS,
                    },
                )
                log(f"stamped resident tags {RESIDENT_TAGS}")
            except Exception as e:
                log(f"failed to stamp resident tags: {e}", "warning")
    except GovernanceTimeoutError as e:
        # Onboard timeout is the worst case — don't assume it failed, it may
        # have partial-committed on the server side (which is exactly how
        # the 34-fork incident happened). Just give up for this cycle.
        log(f"onboard timed out ({e}) — skipping, will retry next cycle", "warning")
        _watcher_identity = None
    except Exception as e:
        log(f"onboard failed — identity unavailable: {e}", "warning")
        _watcher_identity = None


def _sync_identity(client) -> None:
    """Capture identity from client after successful resolution."""
    global _watcher_identity
    _watcher_identity = {
        "client_session_id": client.client_session_id or "",
        "continuity_token": client.continuity_token or "",
        "agent_uuid": client.agent_uuid or "",
    }
    _save_session(
        _watcher_identity["client_session_id"],
        _watcher_identity["continuity_token"],
        _watcher_identity["agent_uuid"],
    )


def get_watcher_identity() -> dict[str, str] | None:
    """Return resolved identity or None if governance is unavailable."""
    return _watcher_identity


def _make_identity_client():
    """Create a SyncGovernanceClient for identity resolution."""
    from unitares_sdk import SyncGovernanceClient
    return SyncGovernanceClient(rest_url=GOV_REST_URL, transport="rest", timeout=30)


def build_resolution_outcome_args(
    status: str, fingerprint: str, agent_uuid: str, reason: str | None = None
) -> dict:
    """Map a Watcher finding resolution to an external-truth ``outcome_event``.

    A *confirmed* finding means Watcher's analytical judgment was RIGHT (a good
    outcome); a *dismissed* finding is a false positive — Watcher was WRONG (a
    bad outcome). The adjudication is operator/agent review, i.e. ground truth
    from *outside* the governance loop, so ``verification_source='external_signal'``.

    This is the first exogenous ground-truth channel for an EISV-bearing resident:
    today every baselined agent's outcomes are self-referential (server_observation)
    or self-attested, so the EISV signal is structurally unfalsifiable for them
    (docs/proposals/eisv-maths-roadmap-v0.md Appendix B). The outcome_event handler
    auto-snapshots the agent's EISV by ``agent_id``, so we attribute to Watcher's
    governance UUID — creating the first row where a per-agent residual and an
    external label coexist.
    """
    confirmed = status == "confirmed"
    return {
        "agent_id": agent_uuid,
        "outcome_type": "watcher_finding_confirmed" if confirmed else "watcher_finding_dismissed",
        "is_bad": not confirmed,
        "verification_source": "external_signal",
        "detail": {
            "fingerprint": fingerprint,
            "resolution": status,
            "reason": reason or "",
        },
    }


def _emit_resolution_outcome(
    client, status: str, fingerprint: str, reason: str | None = None
) -> None:
    """Best-effort: record a finding resolution as an external-truth outcome_event.

    Failure here must never affect the resolve/dismiss itself — the mutation has
    already succeeded by the time this runs; this only enriches the ground-truth
    stream.
    """
    try:
        uuid = (_watcher_identity or {}).get("agent_uuid")
        if not uuid or client is None:
            log("resolution outcome skipped: no resolved Watcher identity", "warning")
            return
        args = build_resolution_outcome_args(status, fingerprint, uuid, reason)
        client.call_tool("outcome_event", args, timeout=15)
        log(f"recorded external-truth outcome ({status}) for finding {fingerprint}")
    except Exception as e:  # noqa: BLE001 — best-effort, never break resolution
        log(f"resolution outcome emit skipped: {e}", "warning")


# ---------------------------------------------------------------------------
# Lease-plane guard for Watcher findings mutations
# ---------------------------------------------------------------------------


def _watcher_findings_surface_id() -> str:
    """Canonical lease surface for the local Watcher state directory."""
    return f"file://{STATE_DIR}"


def _watcher_findings_lease_mode() -> str:
    raw = os.environ.get(
        WATCHER_FINDINGS_LEASE_MODE_ENV,
        os.environ.get("WATCHER_LEASE_MODE", "advisory"),
    )
    mode = raw.strip().lower()
    if mode in ("", "advisory", "advise", "phase_a", "phase-a"):
        return "advisory"
    if mode in ("enforce", "enforced", "required", "phase_b", "phase-b"):
        return "enforce"
    if mode in ("off", "disable", "disabled", "none"):
        return "off"
    log(
        f"{WATCHER_FINDINGS_LEASE_MODE_ENV}: unknown mode {raw!r}; "
        "falling back to advisory",
        "warning",
    )
    return "advisory"


def _watcher_findings_lease_ttl_s() -> int:
    raw = os.environ.get(
        WATCHER_FINDINGS_LEASE_TTL_ENV,
        str(WATCHER_FINDINGS_LEASE_DEFAULT_TTL_S),
    )
    try:
        ttl_s = int(raw)
    except ValueError:
        log(
            f"{WATCHER_FINDINGS_LEASE_TTL_ENV}: invalid value {raw!r}; "
            f"using {WATCHER_FINDINGS_LEASE_DEFAULT_TTL_S}s",
            "warning",
        )
        return WATCHER_FINDINGS_LEASE_DEFAULT_TTL_S
    if ttl_s <= 0 or ttl_s > 3600:
        log(
            f"{WATCHER_FINDINGS_LEASE_TTL_ENV}: out of range {ttl_s}; "
            f"using {WATCHER_FINDINGS_LEASE_DEFAULT_TTL_S}s",
            "warning",
        )
        return WATCHER_FINDINGS_LEASE_DEFAULT_TTL_S
    return ttl_s


def _watcher_findings_holder_uuid(agent_id: str | None):
    """Use the operator/Watcher UUID when valid; otherwise mint a process UUID."""
    from uuid import UUID

    from unitares_sdk.lease_plane.advisory import new_holder_uuid

    identity = get_watcher_identity() or {}
    for candidate in (agent_id, identity.get("agent_uuid")):
        if not candidate:
            continue
        try:
            return UUID(candidate)
        except ValueError:
            log(f"watcher lease: invalid holder UUID {candidate!r}; ignoring", "warning")
    return new_holder_uuid()


def _run_with_watcher_findings_lease(
    intent: str,
    mutation: Callable[[], int],
    *,
    holder_agent_id: str | None = None,
) -> int:
    """Run a Watcher state mutation under the lease plane.

    Default mode is Phase A advisory: the mutation still runs when the lease
    plane is unavailable or contended. Setting WATCHER_FINDINGS_LEASE_MODE to
    `enforce` promotes this one surface to Phase B-style caller enforcement.
    """
    mode = _watcher_findings_lease_mode()
    if mode == "off":
        return mutation()

    from unitares_sdk.lease_plane import (
        AcquireHeldByOther,
        AcquireOk,
        AcquirePermissionDenied,
        AcquireRequest,
        AcquireSchemaInvalid,
        AcquireServiceUnavailable,
    )
    from unitares_sdk.lease_plane.advisory import make_advisory_client, release_advisory

    client = make_advisory_client()
    lease_id = None
    surface_id = _watcher_findings_surface_id()
    audit_session = (get_watcher_identity() or {}).get("client_session_id") or None

    try:
        result = client.acquire(
            AcquireRequest(
                surface_id=surface_id,
                holder_agent_uuid=_watcher_findings_holder_uuid(holder_agent_id),
                holder_class="process_instance",
                holder_kind="remote_heartbeat",
                ttl_s=_watcher_findings_lease_ttl_s(),
                intent=intent,
                audit_session=audit_session,
            )
        )
    except Exception as exc:  # defensive; LeasePlaneClient should be no-raise
        outcome = "client_error"
        detail = f"client_error err={exc!r}"
    else:
        if isinstance(result, AcquireOk):
            outcome = "acquired_idempotent" if result.idempotent else "acquired_new"
            detail = f"{outcome} lease_id={result.lease.lease_id}"
            lease_id = result.lease.lease_id
        elif isinstance(result, AcquireHeldByOther):
            outcome = "held_by_other"
            detail = (
                f"held_by_other held_by={result.held_by_uuid} "
                f"blocking_lease={result.blocking_lease_id} "
                f"expires_at={result.expires_at.isoformat()} "
                f"retry_after_hint_ms={result.retry_after_hint_ms}"
            )
        elif isinstance(result, AcquirePermissionDenied):
            outcome = "permission_denied"
            detail = f"permission_denied reason={result.reason}"
        elif isinstance(result, AcquireSchemaInvalid):
            outcome = "schema_invalid"
            detail = f"schema_invalid detail={result.detail}"
        elif isinstance(result, AcquireServiceUnavailable):
            outcome = "service_unavailable"
            detail = "service_unavailable"
        else:
            outcome = "client_error"
            detail = f"unrecognized_result result={result!r}"

    if outcome == "held_by_other":
        print(
            f"warning: watcher findings lease {detail} for {surface_id}",
            file=sys.stderr,
        )
    if mode == "enforce" and outcome not in _LEASE_ACQUIRED_OUTCOMES:
        print(
            f"error: watcher findings mutation blocked: {detail} "
            f"surface={surface_id} intent={intent!r}",
            file=sys.stderr,
        )
        log(
            f"watcher findings mutation blocked by lease: {detail} "
            f"surface={surface_id} intent={intent!r}",
            "warning",
        )
        return WATCHER_FINDINGS_LEASE_BLOCK_RC

    try:
        return mutation()
    finally:
        if lease_id is not None:
            release_advisory(client, lease_id)


# ---------------------------------------------------------------------------
# Check-in — periodic EISV signal to governance
# ---------------------------------------------------------------------------


def compute_checkin_complexity(active_count: int) -> float:
    """Map active finding count to complexity: 0→0.1, 10+→0.6, linear between."""
    return min(0.6, 0.1 + active_count * 0.05)


def compute_checkin_confidence(confirmed: int, dismissed: int) -> float:
    """Posterior mean of Beta(0.5+confirmed, 0.5+dismissed).

    Replaces the old 'return 0.7 if total<5 else confirmed/total' warmup,
    which was overconfidence shipped to governance: a freshly deployed
    Watcher with zero observations was claiming 0.7 confidence in its
    own findings. With the Jeffreys prior (Beta(0.5, 0.5)), no
    observations yields exactly 0.5 (true neutrality) and the value
    tracks the data smoothly as it accumulates.
    """
    if confirmed < 0 or dismissed < 0:
        return 0.5
    alpha = 0.5 + confirmed
    beta = 0.5 + dismissed
    return alpha / (alpha + beta)


def _build_checkin_summary() -> tuple[str, float, float]:
    """Build check-in response_text, complexity, and confidence from findings.jsonl."""
    findings = _iter_findings_raw()
    if not findings:
        return "Watcher idle", 0.05, 0.9

    by_status: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for f in findings:
        status = f.get("status", "open")
        by_status[status] = by_status.get(status, 0) + 1
        if status in ("open", "surfaced"):
            sev = f.get("severity", "unknown")
            by_severity[sev] = by_severity.get(sev, 0) + 1

    active = by_status.get("open", 0) + by_status.get("surfaced", 0)
    confirmed = by_status.get("confirmed", 0)
    dismissed = by_status.get("dismissed", 0)

    sev_parts = ", ".join(f"{n} {s}" for s, n in sorted(by_severity.items()) if n > 0)
    summary_parts = []
    if active:
        summary_parts.append(f"{active} unresolved ({sev_parts})" if sev_parts else f"{active} unresolved")
    if confirmed:
        summary_parts.append(f"{confirmed} confirmed")
    if dismissed:
        summary_parts.append(f"{dismissed} dismissed")
    summary = f"Watcher: {', '.join(summary_parts)}" if summary_parts else "Watcher idle"

    complexity = compute_checkin_complexity(active)
    confidence = compute_checkin_confidence(confirmed, dismissed)
    return summary, complexity, confidence


def _do_checkin() -> None:
    """Post a check-in to governance. Called at the end of surface_pending()."""
    identity = get_watcher_identity()
    if identity is None:
        return

    summary, complexity, confidence = _build_checkin_summary()

    try:
        client = _make_identity_client()
        # Restore identity state so the client can inject session args
        client.client_session_id = identity["client_session_id"]
        client.continuity_token = identity["continuity_token"]
        client.agent_uuid = identity["agent_uuid"]

        # Pass the continuity token as an explicit arg so process_agent_update
        # resolves identity by cryptographic ownership proof (REST PATH 2.8
        # rebind) instead of relying solely on the client_session_id cache.
        # Observed mechanism (2026-06-05, verified against the live MCP/Redis):
        # the Watcher's csid->uuid binding lives in Redis with a ~24h TTL and no
        # durable row stands in for it (the agent_sessions table held it for no
        # Watcher row). A csid-only check-in carries no proof, so once that cache
        # entry lapses the resolver has nothing to rebind from and dispatch
        # returns "Identity not resolved" — failures began exactly 24h after the
        # last good check-in and stayed dark for ~11h. The token-bearing call
        # rebinds via proof and re-warms the cache (TTL reset confirmed live), so
        # check-ins self-heal across the TTL boundary. resolve_identity() runs
        # before this in main() and refreshes the token; the empty-token guard
        # below degrades gracefully to the old csid-only behavior if it is stale.
        # The SDK still injects client_session_id alongside the token.
        checkin_kwargs = {}
        token = identity.get("continuity_token")
        if token:
            checkin_kwargs["continuity_token"] = token

        client.checkin(
            response_text=summary,
            complexity=complexity,
            confidence=confidence,
            response_mode="compact",
            **checkin_kwargs,
        )
        log(f"check-in: {summary}")
    except Exception as e:
        log(f"check-in failed: {e}", "warning")


# Paths we never scan — too much churn, not worth the noise
SKIP_PATH_FRAGMENTS = (
    "/.git/",
    "/node_modules/",
    "/__pycache__/",
    "/.venv/",
    "/venv/",
    "/dist/",
    "/build/",
    "/.pytest_cache/",
    "/data/logs/",
    "/data/watcher/",  # never scan our own findings (legacy checkout-relative state)
    "/.unitares/watcher/",  # never scan our own findings (shared home-anchored state)
)

SKIP_EXTENSIONS = (
    ".pyc",
    ".log",
    ".lock",
    ".min.js",
    ".map",
    ".svg",
    ".png",
    ".jpg",
)


# ---------------------------------------------------------------------------
# Skip heuristics
# ---------------------------------------------------------------------------


def should_skip(file_path: str) -> tuple[bool, str]:
    """Return (skip, reason)."""
    if not file_path:
        return True, "no file path"
    p = Path(file_path)
    if not p.exists():
        return True, "file does not exist"
    if not p.is_file():
        return True, "not a regular file"
    abs_path = str(p.resolve())
    for frag in SKIP_PATH_FRAGMENTS:
        if frag in abs_path:
            return True, f"skip fragment {frag}"
    for ext in SKIP_EXTENSIONS:
        if abs_path.endswith(ext):
            return True, f"skip extension {ext}"
    try:
        if p.stat().st_size > 256 * 1024:
            return True, "file larger than 256KB"
    except OSError as e:
        return True, f"stat failed: {e}"
    return False, ""


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------


def read_file_region(
    file_path: str, region: str | None = None, max_lines: int = DEFAULT_CONTEXT_LINES
) -> tuple[str, int, int]:
    """Return (text, start_line, end_line). Lines are 1-indexed, inclusive."""
    p = Path(file_path)
    lines = p.read_text(errors="replace").splitlines()
    total = len(lines)

    if region:
        # Accept "L240-L290", "240-290", "L240-290", "240-L290".
        # The previous lstrip("L") only stripped the leading L of the start
        # token; the end token kept its L and int() raised, causing a silent
        # fallback to the file head.
        try:
            start_token, _, end_token = region.partition("-")
            if not end_token:
                raise ValueError("region missing '-' separator")
            start = max(1, int(start_token.lstrip("Ll")))
            end = min(total, int(end_token.lstrip("Ll")))
            if end < start:
                raise ValueError(f"end {end} before start {start}")
        except ValueError as e:
            log(f"bad region {region!r}: {e}; scanning head", "warning")
            start, end = 1, min(total, max_lines)
    else:
        start, end = 1, min(total, max_lines)

    snippet_lines = [f"{i:4d}: {lines[i - 1]}" for i in range(start, end + 1)]
    return "\n".join(snippet_lines), start, end


# ---------------------------------------------------------------------------
# Pattern library loading
# ---------------------------------------------------------------------------


def load_patterns() -> str:
    if not PATTERNS_FILE.exists():
        return "(no pattern library found)"
    return PATTERNS_FILE.read_text()


# Map pattern id → authoritative severity. The model is allowed to flag
# patterns but we override its severity field with the library's, since small
# local models tend to downgrade severities to "medium" by default.
def load_pattern_severities() -> dict[str, str]:
    import re

    severities: dict[str, str] = {}
    if not PATTERNS_FILE.exists():
        return severities
    text = PATTERNS_FILE.read_text()
    # Match headings like:  ### P001 — Fire-and-forget task leak (severity: high)
    pat = re.compile(r"^###\s+(P\d{3})\b.*?\(severity:\s*([a-zA-Z]+)", re.MULTILINE)
    for m in pat.finditer(text):
        severities[m.group(1)] = m.group(2).strip().lower()
    return severities


def load_pattern_violation_classes() -> dict[str, str]:
    """Map pattern id -> violation class from patterns.md headers."""
    import re

    classes: dict[str, str] = {}
    if not PATTERNS_FILE.exists():
        return classes
    text = PATTERNS_FILE.read_text()
    pat = re.compile(
        r"^###\s+((?:EXP-)?P\d{3})\b.*?violation_class:\s*([A-Z]+)",
        re.MULTILINE,
    )
    for m in pat.finditer(text):
        classes[m.group(1)] = m.group(2).strip()
    return classes


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def build_review_prompt(file_path: str, code_snippet: str) -> str:
    """Reasoning-based code review prompt — no pattern library, model thinks freely."""
    return f"""You are a senior code reviewer. Read the code below carefully and identify actual bugs, logic errors, resource leaks, race conditions, or security issues.

RULES:
1. Only report issues you are confident about. No style nitpicks, no "consider using X" suggestions.
2. Each finding must explain WHY it's a bug — what breaks, under what conditions.
3. Ignore comments. Only analyze executable code.
4. If the code looks correct, return an empty findings list. That is the right answer most of the time.

OUTPUT FORMAT — JSON only, no prose, no markdown fences:
{{"findings":[{{"line":<int>,"severity":"high|medium|low","hint":"<what's wrong, <=15 words>","reasoning":"<1-2 sentences: what breaks and when>"}}]}}

CODE TO REVIEW (from {file_path}):
```
{code_snippet}
```

Remember: JSON only. Empty findings list is correct if nothing is wrong. Quality over quantity — one real bug beats ten maybes.
"""


def build_prompt(patterns_md: str, file_path: str, code_snippet: str) -> str:
    return f"""You are Watcher — a bug-pattern matcher for this codebase. You do NOT need to decide if a bug is "real" or "standard practice". Your job is to flag every occurrence of a known-bad pattern from the library below, without second-guessing.

CRITICAL RULES — read carefully before scanning:

1. **Code only, never comments.** Lines starting with `#`, lines inside `'''...'''` or `\"\"\"...\"\"\"` triple-quoted blocks, and lines inside `/* ... */` or `//` are COMMENTS. They are documentation, not code. NEVER flag a pattern that only appears in a comment — even if the comment uses words like "leak", "transient", "mutation", "fire-and-forget", or describes a past bug. Comments often EXPLAIN fixes for past bugs and will mention the bug words in plain English. Ignore them.

2. **Pattern matches must be literal code occurrences.** P001 requires a literal `create_task(` call. P003 requires a literal `UNITARESMonitor(` constructor. P011 requires an assignment statement followed by no `await` to a persist function. Function names containing "task" are NOT P001 matches; comments containing "monitor" are NOT P003 matches.

3. **Asyncio supervisor exception.** A `while True:` (or `while self.running:`) loop that contains a `try: ... except asyncio.CancelledError:` handler is a STANDARD asyncio supervisor task pattern. Do NOT flag it as P009. P009 is for polling loops that lack ANY cancellation/timeout, not for long-running supervisors with proper shutdown handling.

4. **For every finding you emit, include a 1-sentence justification field `evidence` quoting the literal code (not a comment) that matches.** If you can't quote actual code, drop the finding.

OUTPUT FORMAT — JSON only, no prose, no markdown fences:
{{"findings":[{{"pattern":"P001","line":<int>,"hint":"<<=12 words>","evidence":"<literal code line>"}}]}}

5. Empty findings list is valid and correct if nothing matches.
6. Do NOT invent pattern IDs. Only use IDs present in the library.
7. Do NOT rationalize. Either the literal code matches the literal pattern or it doesn't.
8. The `line` field is the line number shown in the snippet (the number before the colon).

PATTERN LIBRARY:
{patterns_md}

CODE TO SCAN (from {file_path}):
```
{code_snippet}
```

Remember: JSON only. No prose. No markdown fences around the JSON. Comments don't count.
"""


# ---------------------------------------------------------------------------
# Model call
# ---------------------------------------------------------------------------


def call_ollama(prompt: str, model: str, timeout: int) -> dict[str, Any]:
    """Call Ollama's OpenAI-compatible endpoint directly.

    Configuration via OLLAMA_URL / DEFAULT_MODEL / DEFAULT_TIMEOUT at module load
    (driven by WATCHER_OLLAMA_URL / WATCHER_MODEL / WATCHER_TIMEOUT env vars).

    max_tokens=1024 matches the Qwen3 token economy (~40 tokens per finding);
    temperature=0.0 keeps the detector output deterministic.
    """
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1024,
            "temperature": 0.0,
        }
    ).encode()
    req = urllib.request.Request(
        OLLAMA_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())

    choice = data["choices"][0]["message"]
    text = choice.get("content", "") or choice.get("reasoning", "") or ""
    usage = data.get("usage", {})
    return {
        "text": text,
        "model_used": data.get("model", model),
        "tokens_used": usage.get("total_tokens", 0),
    }


def call_model(prompt: str, model: str = DEFAULT_MODEL, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Entry point used by scan_file; thin wrapper over call_ollama."""
    return call_ollama(prompt, model, timeout)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _looks_like_comment(line: str) -> bool:
    """Cheap heuristic: does this line look like a comment rather than code?"""
    stripped = line.strip()
    if not stripped:
        return True
    # Strip the leading "  NNN: " line-number prefix the watcher emits
    if ":" in stripped:
        head, _, rest = stripped.partition(":")
        if head.strip().isdigit():
            stripped = rest.strip()
    if not stripped:
        return True
    if stripped.startswith("#"):
        return True
    if stripped.startswith(('"""', "'''")):
        return True
    if stripped.startswith(("//", "/*", "*")):
        return True
    return False


def p008_actually_fires(file_path: str, line: int) -> bool:
    """AST post-filter for P008 (shell injection).

    The LLM keeps flagging list-form subprocess calls as P008 even though
    the rule text says list-form is exempt. This deterministic check parses
    the target file and suppresses the finding when no call at/spanning the
    flagged line actually uses the shell (shell=True or os.system/os.popen).

    Conservative on errors: if we can't parse the file or resolve the call,
    return True so the finding is NOT suppressed — human review is still
    valuable when we can't verify the exemption.

    Returns True  → finding is a possible real P008; keep it
    Returns False → verified false positive; suppress it
    """
    import ast

    try:
        source = Path(file_path).read_text()
    except (OSError, UnicodeDecodeError):
        return True  # can't read → don't hide from humans

    if not file_path.endswith(".py"):
        # P008 is Python-shell-injection; other languages not covered here.
        # Don't suppress for now — falls back to the LLM's judgment.
        return True

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        return True  # can't parse → don't hide

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        start = getattr(node, "lineno", 0)
        end = getattr(node, "end_lineno", start)
        if not (start <= line <= end):
            continue

        # os.system(...) / os.popen(...) are always shell-bound
        func = node.func
        name = None
        if isinstance(func, ast.Attribute):
            name = func.attr
            module = func.value.id if isinstance(func.value, ast.Name) else None
            if module == "os" and name in ("system", "popen"):
                return True
            if module == "subprocess":
                # subprocess.run / Popen / call / check_call / check_output
                for kw in node.keywords:
                    if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        return True
        elif isinstance(func, ast.Name):
            # Bare `system(...)` or `popen(...)` — unlikely but possible via `from os import system`
            if func.id in ("system", "popen"):
                return True

    # No call at this line uses shell=True or os.system/popen → verified FP.
    return False


def parse_findings(
    text: str, file_path: str, model_used: str, region_start: int
) -> list[tuple[Finding, str]]:
    """Parse the model's JSON response into Finding objects.

    Tolerant of:
      - leading/trailing whitespace
      - markdown code fences (```json ... ```)
      - extra prose before the JSON block
    """
    cleaned = text.strip()

    # Strip markdown fences
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        # typically ['', 'json\n{...}', ''] or ['', '{...}', '']
        for part in parts:
            stripped = part.strip()
            if stripped.startswith(("json\n", "json ")):
                stripped = stripped[5:].strip()
            if stripped.startswith("{"):
                cleaned = stripped
                break

    # Find the first '{' and last '}' as a last resort
    if not cleaned.startswith("{"):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]

    if not cleaned:
        return []

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        log(f"failed to parse model output as JSON: {e}; raw={text[:300]!r}", "warning")
        return []

    raw_findings = data.get("findings", []) if isinstance(data, dict) else []
    if not isinstance(raw_findings, list):
        return []

    library_severities = load_pattern_severities()
    library_violation_classes = load_pattern_violation_classes()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    findings: list[tuple[Finding, str]] = []
    for rf in raw_findings:
        if not isinstance(rf, dict):
            continue
        pattern = str(rf.get("pattern", "")).strip()
        if not pattern:
            continue
        # Drop findings whose pattern id we don't recognize — the model is
        # only allowed to flag library patterns, not invent new ones.
        if pattern not in library_severities:
            log(f"dropping unknown pattern id from model: {pattern!r}", "warning")
            continue
        try:
            line_in_snippet = int(rf.get("line", 0))
        except (TypeError, ValueError):
            line_in_snippet = 0
        # The model sees line numbers within the snippet — they are already
        # the actual file line numbers since we emit `{i}: <content>` where
        # `i` is the original file line number.
        line = line_in_snippet if line_in_snippet > 0 else region_start
        hint = str(rf.get("hint", "")).strip()[:200]
        evidence = str(rf.get("evidence", "")).strip()[:300]
        # Authoritative severity comes from the library, never the model.
        severity = library_severities[pattern]

        # P008 post-filter: the LLM keeps flagging list-form subprocess as
        # shell injection. Verify with an AST scan; suppress when no call
        # at this line uses shell=True / os.system / os.popen.
        if pattern == "P008" and not p008_actually_fires(file_path, line):
            log(
                f"suppressing P008 false-positive at {file_path}:{line} "
                f"(no shell=True / os.system at that line)",
                "debug",
            )
            continue

        findings.append(
            (
                Finding(
                    pattern=pattern,
                    file=file_path,
                    line=line,
                    hint=hint,
                    severity=severity,
                    detected_at=now,
                    model_used=model_used,
                    violation_class=library_violation_classes.get(pattern, ""),
                ),
                evidence,
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Finding lifecycle helpers — _post_resolution_event stays here because it
# needs get_watcher_identity from the identity block above. All the rest
# (model, dedup, persistence, status transitions, format/print, compact,
# escalate) lives in agents/watcher/findings.py.
# ---------------------------------------------------------------------------


def _post_resolution_event(
    finding: dict,
    action: str,
    resolver_agent_id: str | None,
    reason: str | None = None,
) -> None:
    """Post a watcher_resolution event to the governance event stream."""
    identity = get_watcher_identity()
    if identity is None:
        return

    base_msg = f"[{action}] {finding.get('pattern', '?')} {finding.get('file', '?')}:{finding.get('line', '?')} — {finding.get('hint', '')}"
    message = f"{base_msg} · {reason}" if reason else base_msg

    extra = {
        "action": action,
        "pattern": finding.get("pattern", ""),
        "file": finding.get("file", ""),
        "line": finding.get("line", 0),
        "violation_class": finding.get("violation_class", ""),
        "resolved_by": resolver_agent_id,
    }
    if reason:
        extra["resolution_reason"] = reason

    try:
        post_finding(
            event_type="watcher_resolution_finding",
            severity=finding.get("severity", "unknown"),
            message=message,
            agent_id=identity["agent_uuid"],
            agent_name="Watcher",
            fingerprint=finding.get("fingerprint", ""),
            extra=extra,
        )
    except Exception as e:
        log(f"resolution event failed: {e}", "warning")


# ---------------------------------------------------------------------------
# Commit-trail scanner — close the loop between operator-side fixes and
# Watcher's audit trail. CLAUDE.md asks for fingerprints in commit messages
# but no machinery existed to consume them; clusters were drifting into
# fixed-but-still-confirmed limbo. Scan recent commits, find fingerprint
# references, transition matching findings to confirmed with the commit
# subject as resolution_reason.
# ---------------------------------------------------------------------------

# Watcher fingerprints are 16 hex chars; accept 8+ to allow short references
# in commit messages. Anchored at word boundaries so 8-char prefixes don't
# collide with the start of a commit-SHA mention.
_FINGERPRINT_RE = re.compile(r"\b([0-9a-f]{8,16})\b")

# git log subject prefixes that indicate a revert. Skipped to avoid auto-
# resolving a finding whose fix was just rolled back.
_REVERT_PREFIXES = ("revert ", "revert:", 'revert "', "revert: ")


def scan_commits(since: str = "30 days ago", repo_path: Path | None = None) -> int:
    """Scan recent commits for fingerprint references and resolve matches.

    For each commit in ``git log --since=<since>``, extracts 8-16 char hex
    sequences from subject and body. Each unique-prefix match against an
    existing finding is transitioned to ``confirmed`` with reason
    ``referenced in <sha>: <subject>``.

    Skips:
      - Findings already with a ``resolution_reason`` set (idempotent).
      - Findings already in terminal ``dismissed`` state (do not auto-
        resurrect false positives).
      - Revert commits (subject starts with ``revert``) — avoid auto-
        resolving a finding whose fix was reverted.
      - Ambiguous prefixes that match more than one fingerprint.

    Phase A lease-plane integration: each scan acquires an advisory-mode
    lease at `surface_id=resident:/watcher_scan_commits_<repo_root_sanitized>`
    (migrated from pre-canonical `watcher:scan_commits:<repo_root>` per RFC
    v0.8 §7.2.1 canonical scheme list). The lease is *telemetry only* —
    Watcher proceeds whether the lease is acquired, held by another
    scanner, or unavailable. RFC v0.5 §6.1.

    Returns:
        The number of findings resolved this scan. Always returns 0 on git
        failure rather than raising, so this is safe to call from a cron.
    """
    repo_root = repo_path or PROJECT_ROOT

    from unitares_sdk.lease_plane.advisory import lease_advisory_scope, new_holder_uuid

    # Sanitize repo path into a resident:/ surface_id (slashes → underscores).
    sanitized = str(repo_root).replace("/", "_").strip("_")
    surface_id = f"resident:/watcher_scan_commits_{sanitized}"
    with lease_advisory_scope(
        surface_id=surface_id,
        holder_agent_uuid=new_holder_uuid(),
        ttl_s=60,
        intent=f"scan_commits since={since!r}",
    ):
        return _scan_commits_inner(since, repo_root)


def _scan_commits_inner(since: str, repo_root: Path) -> int:
    try:
        result = subprocess.run(
            ["git", "log", f"--since={since}", "--format=%H%x00%s%x00%b%x1e"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        log("scan_commits: git log timed out", "warning")
        return 0
    except FileNotFoundError:
        log("scan_commits: git not on PATH", "warning")
        return 0
    if result.returncode != 0:
        log(f"scan_commits: git log failed: {result.stderr.strip()}", "warning")
        return 0

    findings = _iter_findings_raw()
    if not findings:
        return 0

    # Map full fingerprint → live status. Updated in-place after each
    # successful resolve so subsequent commits in the same scan see the
    # already-resolved state and skip re-emitting events. Only carries
    # findings in the active queue — confirmed/dismissed/aged_out are
    # terminal and a coincidental hex match must NOT re-stamp confirmed_at
    # or re-emit a governance event on the next scan.
    fp_state: dict[str, str] = {
        f.get("fingerprint", ""): f.get("status", "open")
        for f in findings
        if f.get("fingerprint") and f.get("status", "open") in ("open", "surfaced")
    }
    if not fp_state:
        return 0

    resolved_count = 0
    commits_seen = 0
    for record in result.stdout.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        parts = record.split("\x00", 2)
        if len(parts) < 3:
            continue
        sha, subject, body = parts
        commits_seen += 1
        subject_l = subject.lstrip().lower()
        if any(subject_l.startswith(p) for p in _REVERT_PREFIXES):
            continue

        text = subject + "\n" + body
        # Dedup prefixes within a single commit so one mention per finding
        seen_prefixes: set[str] = set()
        for prefix in _FINGERPRINT_RE.findall(text):
            if prefix in seen_prefixes:
                continue
            seen_prefixes.add(prefix)
            matches = [fp for fp in fp_state if fp.startswith(prefix)]
            if len(matches) != 1:
                continue
            full_fp = matches[0]
            reason = f"referenced in {sha[:8]}: {subject[:80]}"
            rc = update_finding_status(
                full_fp,
                "confirmed",
                resolver_agent_id="watcher_scan_commits",
                reason=reason,
            )
            if rc == 0:
                # Drop from active map so a later commit-in-the-same-scan
                # mentioning the same fingerprint doesn't re-stamp it.
                del fp_state[full_fp]
                resolved_count += 1

    print(
        f"scan complete: {resolved_count} finding(s) resolved across "
        f"{commits_seen} commit(s) since {since}"
    )
    return resolved_count


# ---------------------------------------------------------------------------
# Surfacing coordinator — surface_pending stays here because it also triggers
# a governance check-in (_do_checkin). The read-only print_unresolved, the
# shared _format_findings_block, and sweep/compact/escalate all live in
# agents/watcher/findings.py.
# ---------------------------------------------------------------------------


def surface_pending() -> int:
    """Chime mode: print findings with status == 'open' and transition ONLY
    THOSE ACTUALLY DISPLAYED to 'surfaced'. Called by the UserPromptSubmit
    hook so each prompt the user sends gets a delta of "what Watcher caught
    since your last prompt".

    After this runs, the findings that were actually shown in the block are
    recorded as surfaced. Any findings dropped by the severity display cap
    (typically medium-severity findings crowded out by a wall of
    critical/high) stay `open` so they'll appear on a later chime once the
    high-severity queue drains. This prevents the silent-drop bug where
    medium findings were previously marked surfaced without the user ever
    seeing them.

    Auto-sweep on entry: drops findings whose target file no longer
    exists before computing the chime. Closes the failure mode observed
    on 2026-05-07 dogfood — 36% of open findings (48/132) were dangling
    against deleted worktree paths, inflating chime severity rankings.
    A second quiet sweep ages out findings whose flagged line has drifted
    off the pattern's required token (file still present, but the stored
    line no longer holds the construct — the line-drift FP class behind
    repeated P003/P001/P016 dismissal sweeps). Both are quiet so they
    don't pollute the chime block stdout.
    """
    _sweep_stale_quiet()
    _sweep_token_drift_quiet()
    all_findings = _iter_findings_raw()
    open_findings = [f for f in all_findings if f.get("status", "open") == "open"]

    block, shown = _format_findings_block(
        open_findings,
        header=(
            "Watcher caught the following while you were working. These are\n"
            "new since your last prompt. Look them over before proceeding — or\n"
            "dismiss any false positives with --dismiss <fingerprint>."
        ),
    )
    # Always check in to governance, even when there's nothing new to surface.
    # Otherwise Watcher goes silent between finding bursts.
    _do_checkin()

    if block is None:
        return 0

    print(block)

    # Only transition findings that made it past the display cap. The ones
    # the user saw → surfaced. The ones crowded out → stay open.
    surfaced_fps = {f.get("fingerprint") for f in shown}
    updated: list[dict[str, Any]] = []
    changed = False
    for f in all_findings:
        if f.get("fingerprint") in surfaced_fps and f.get("status", "open") == "open":
            f = {**f, "status": "surfaced"}
            changed = True
        updated.append(f)
    if changed:
        _write_findings_atomic(updated)
        log(
            f"surface_pending: marked {len(surfaced_fps)} open → surfaced "
            f"({len(open_findings) - len(surfaced_fps)} left pending for next chime)"
        )

    return 0


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


_PERSIST_VERB_PATTERN = re.compile(
    r"\bawait\s+\w*(persist|save|store|write|archive|insert|update|commit|flush|sync)\w*\s*\("
)

# Required literal substrings on the flagged line, by pattern id. If a pattern
# is in this map and the substring is missing from the flagged line, the
# finding is dropped as a false positive.
_PATTERN_REQUIRED_TOKENS: dict[str, tuple[str, ...]] = {
    "P001": ("create_task(",),
    # P002 needs the actual growth operation on the flagged line — an
    # append/extend call or a subscript assignment (`x[key] = ...`,
    # spaced or not). The model otherwise associates the finding with a
    # nearby def line, docstring, dict literal, or loop header.
    # False-positive sweep 2026-06-10: 7 of 9 dismissed lifetime P002
    # findings had no growth op on the flagged line
    # (agent_metadata_model.py:212-213 def+docstring of a bounded
    # mutator, event_detector.py:261/:344 loop header + dict-literal
    # value lines, identity_step.py:64/:106 drifted lines,
    # agent_metadata_model.py:194 a bare asdict return).
    "P002": (".append(", ".extend(", "] =", "]="),
    "P003": ("UNITARESMonitor(",),
    # P004 needs a literal Redis call marker on the flagged line.
    # Narrowed to Redis-only on 2026-05-23: asyncpg ops in MCP handlers are
    # safe post-PR #218 (2026-04-27), which wraps the asyncpg pool in
    # ExecutorPool so direct `await conn.fetchval(...)` no longer collides
    # with the MCP SDK's anyio task group. Redis is NOT wrapped — those
    # awaits still need `asyncio.wait_for` guards per CLAUDE.md
    # "Substrate Tax" section, so the rule still fires on raw Redis ops.
    # The literal-token guard (vs. just "any await") protects against
    # qwen3-coder-next associating the pattern with nearby unrelated lines;
    # caught when it flagged http_api.py:736 and :907 on 2026-04-14.
    "P004": ("await redis", "redis.get(", "redis.set(", "redis.delete(",
             "redis.hget(", "redis.hset(", "redis.expire(", "redis.exists("),
    # P005 needs a literal acquire/cursor call on the flagged line.
    # Without this, the model sometimes associates a P005 "resource leak"
    # finding with the method DEFINITION (async def __aexit__, etc.)
    # instead of the acquire call itself. Caught when qwen3-coder-next
    # flagged postgres_backend.py:199 on 2026-04-11.
    "P005": (".acquire(", ".cursor(", ".connect(", ".lock("),
    "P008": ("shell=True", "os.system(", "subprocess.run(", "subprocess.call("),
    "P012": ("json.loads(", "yaml.load(", "yaml.safe_load("),
    # P016 is about double-envelope dict parsing (`data["success"]`,
    # `data.get("success")`). Pure attribute access on a typed pydantic model
    # (`result.success`, `audit_result.success`) is by construction flat — the
    # schema makes the shape explicit. Requiring a quoted "success" literal
    # drops the typed-attribute false positives while keeping the real shape.
    # Caught when qwen3-coder-next flagged 4 SDK-typed call sites in
    # agents/vigil/agent.py:292,308,318,324 on 2026-04-14.
    "P016": ('"success"', "'success'"),
}

# File-path substrings that MUST be present in finding.file for the pattern
# to apply. P004 (Redis-in-MCP-handler) is only relevant to code under
# src/mcp_handlers/ — the pattern library explicitly excludes Starlette REST
# routes in src/http_api.py, which run outside the MCP anyio task group.
_PATTERN_FILE_PATH_CONSTRAINTS: dict[str, tuple[str, ...]] = {
    "P004": ("/src/mcp_handlers/",),
}

# Regex: `name = ...create_task(...)` on one line. If this matches the P001
# flagged line, the task reference is stored — not fire-and-forget.
_P001_TASK_ASSIGNMENT = re.compile(r"\b[a-zA-Z_]\w*\s*=\s*[^=].*create_task\(")

# Regex: project's blessed tracked-task wrapper. By construction stores the
# task ref in a tracked set; P001 should not flag call sites of it. The
# required-token check still keeps `create_task(` matches because
# `create_tracked_task` contains the substring `create_task(`. Caught when
# qwen3-coder-next flagged 2 sites in mcp_server_std.py on 2026-04-17.
_P001_TRACKED_HELPER = re.compile(r"\bcreate_tracked_task\s*\(")

# Regex: bounded-growth cues near a flagged P002 growth op — a len-cap
# trim check (`if len(x) > CAP:`), an explicit eviction (`.pop(0)`,
# `.popleft()`), or a deque bounded at construction (`maxlen=`). The
# P002 library text requires growth "without a cap, LRU eviction, or
# periodic sweep"; the model judges the append line in isolation and
# misses the trim that sits right next to it. False-positive sweep
# 2026-06-10: event_detector.py:291/:414 both have the len-cap trim on
# the very next line; agent_metadata_model.py's bounded mutators trim
# within three lines of the append.
_P002_BOUND_CUE = re.compile(
    r"\bif\s+len\s*\(|\bmaxlen\s*=|\.popleft\s*\(|\.pop\s*\(\s*0\s*\)"
)
# Window: multi-line `.append({...})` literals push the trim a few lines
# past the flagged call (add_lifecycle_event's 5-line append → trim at
# +5), so the after-window is wider than the before-window (pop-before-
# append idioms sit within a line or two above).
_P002_CUE_LINES_BEFORE = 3
_P002_CUE_LINES_AFTER = 6

# Regex: header line of `def get_or_create_monitor(` — when a P003 flag
# lands inside the body of this function (which IS the cache), the
# "instantiated outside the cache" rule does not apply. Caught when
# qwen3-coder-next flagged agent_lifecycle.py:26 on 2026-04-17.
_P003_CACHE_FUNC_HEADER = re.compile(
    r"^\s*(?:async\s+)?def\s+get_or_create_monitor\s*\("
)
_P003_OTHER_DEF = re.compile(r"^\s*(?:async\s+)?def\s+\w+\s*\(")

# Regex: `getattr(<obj>, "success", ...)` — defensive typed-attribute access.
# By construction this targets a flat object's attribute and cannot mask a
# nested envelope. The quoted "success" satisfies the required-token check,
# so an extra drop rule is needed to handle this shape.
_P016_GETATTR_SUCCESS = re.compile(
    r"""\bgetattr\s*\([^,]+,\s*['"]success['"]"""
)

# Regex: an inner-layer assertion cue appearing AFTER the flagged outer
# `.get("success")` check. The two-layer envelope-parsing shape is:
#
#     if not data.get("success", False):   # ← outer (flagged)
#         raise ...
#     result = data.get("result")
#     if isinstance(result, dict) and result.get("isError"):   # ← inner cue
#         raise ...
#     # or: _raise_for_tool_failure(name, result)
#     # or: <Typed>Result.model_validate(result)  (raises on inner failure)
#     # or: _parse_mcp_result(result)
#
# When any of these cues appear in the lines following a flagged success check,
# the operator has already wired the inner-layer assertion; P016 is satisfied.
_P016_INNER_LAYER_FOLLOWUP = re.compile(
    r"""\.get\(\s*['"]isError['"]\s*\)"""
    r"""|\b_raise_for_tool_failure\s*\("""
    r"""|\b_parse_mcp_result\s*\("""
    r"""|\b\w*Result\.model_validate\s*\("""
)

# Regex: helper-function definitions whose body operates on already-unwrapped
# inner results (the caller did the outer-envelope check before invoking them).
# `_raise_for_tool_failure(tool_name, raw)` is the canonical SDK shape — single
# `raw.get("success") is False` check + raise. By convention these helpers do
# not double-check an envelope they were never handed.
_P016_INNER_ASSERTION_HELPER_DEF = re.compile(
    r"""^\s*(?:async\s+)?def\s+_raise_for_\w+\s*\("""
)

# Regex: P005 resource-leak false positive when the acquire/cursor/connect/lock
# call sits inside an `async with` (or plain `with`) header — the context
# manager guarantees release on `__aexit__`, so by construction it is not a
# leak. The model still flags these because the keyword `acquire` triggers
# the pattern. Caught when qwen3-coder-next flagged `async with db.acquire()`
# sites on 2026-04-24 (KG 2026-04-24T02:01:05).
_P005_CONTEXT_MANAGED = re.compile(
    r"\b(?:async\s+)?with\b[^#]*\.(?:acquire|cursor|connect|lock)\s*\("
)

# Regex pieces: P005 false positive on the `<var> = None` + try/finally pattern,
# where the acquire is INSIDE try and a None pre-init guards finally's close.
# This is the canonical safe pattern when the resource type does not implement
# the async-context-manager protocol (e.g. asyncpg.Connection has no __aenter__).
# Cancel-between-acquire-and-try is impossible because the acquire is inside
# try, and `finally: if <var> is not None: await <var>.close()` is well-defined
# whether the acquire raised or succeeded. Caught when qwen3-coder-next refired
# on scrapers.py:43 after the chronicler fix adopted this pattern (KG
# 2026-04-25T20:07).
_P005_VAR_AWAIT_ACQUIRE = re.compile(
    r"^\s*(\w+)\s*=\s*await\s+\S+\.(?:acquire|cursor|connect|lock)\s*\("
)
_P005_VAR_NONE_INIT = re.compile(r"^\s*(\w+)\s*=\s*None\s*$")
_P005_TRY_HEADER = re.compile(r"^\s*try\s*:\s*$")
_P005_RETURN_RESOURCE_FACTORY = re.compile(
    r"^\s*return\s+\S+\.(acquire|cursor|connect|lock)\s*\("
)
_P005_RESOURCE_FACTORY_DEF = re.compile(
    r"^\s*(?:async\s+)?def\s+(acquire|cursor|connect|lock)\s*\("
)
_P004_WAIT_FOR_GUARDED_PIN_HELPER = re.compile(
    r"^\s*async\s+def\s+(_lookup_onboard_pin_inner|_set_onboard_pin_inner)\s*\("
)


def _is_resource_factory_passthrough(
    flagged_line: int,
    snippet_lines_by_num: dict[int, str],
    lookback: int = 8,
) -> bool:
    """Detect same-name wrappers that return a resource factory to callers.

    Shape:
        def acquire(self):
            return self._pool.acquire()

    The wrapper does not await, enter, or assign the acquire result; it transfers
    the context-manager/resource factory to the caller. P005 should evaluate the
    eventual caller site, not the pass-through wrapper itself.
    """
    src_line = snippet_lines_by_num.get(flagged_line, "")
    return_match = _P005_RETURN_RESOURCE_FACTORY.match(src_line)
    if not return_match:
        return False
    returned_method = return_match.group(1)

    for line_no in range(flagged_line - 1, flagged_line - lookback - 1, -1):
        line = snippet_lines_by_num.get(line_no, "")
        if not line.strip() or _looks_like_comment(line):
            continue
        wrapper_match = _P005_RESOURCE_FACTORY_DEF.match(line)
        if wrapper_match:
            return wrapper_match.group(1) == returned_method
        if _P003_OTHER_DEF.match(line) or _P003_CACHE_FUNC_HEADER.match(line):
            return False
    return False


def _is_acquire_inside_try_with_none_init(
    flagged_line: int,
    snippet_lines_by_num: dict[int, str],
    lookback: int = 12,
) -> bool:
    """Detect the safe `<var> = None ... try: <var> = await X.connect(...)` shape.

    Walks back from ``flagged_line`` up to ``lookback`` non-blank, non-comment
    lines. The flagged line must itself be ``<var> = await SOMETHING.acquire(...)``
    (or .cursor/.connect/.lock — same set the LLM matches). We require:
      - a ``try:`` line strictly between the None-init and the flagged line
        (i.e. the acquire is inside try), AND
      - a ``<var> = None`` line for the SAME variable above that try.

    The matching ``finally:`` block is not separately verified — once the
    structural shape is present, the operator's intent is clear and the
    only remaining failure mode (forgot the close) is a different bug class
    than P005 catches.
    """
    src_line = snippet_lines_by_num.get(flagged_line, "")
    m = _P005_VAR_AWAIT_ACQUIRE.match(src_line)
    if not m:
        return False
    var = m.group(1)

    saw_try = False
    for line_no in range(flagged_line - 1, flagged_line - lookback - 1, -1):
        line = snippet_lines_by_num.get(line_no, "")
        if not line or _looks_like_comment(line):
            continue
        # Function boundary stops the walk — pre-init must live in same scope.
        if _P003_OTHER_DEF.match(line) or _P003_CACHE_FUNC_HEADER.match(line):
            return False
        if _P005_TRY_HEADER.match(line):
            saw_try = True
            continue
        none_match = _P005_VAR_NONE_INIT.match(line)
        if none_match and none_match.group(1) == var:
            # We require try: to appear BETWEEN None-init and the flagged line,
            # i.e. we must have already seen it on the way down.
            return saw_try
    return False


def _is_acquire_then_try_with_unconditional_close(
    flagged_line: int,
    snippet_lines_by_num: dict[int, str],
    lookahead: int = 4,
) -> bool:
    """Detect the canonical `acquire-then-try` idiom.

    Shape:
        <var> = await <expr>.(acquire|cursor|connect|lock)(...)
        try:
            ...
        finally:
            await <var>.close()  # or .release()

    The flagged line must be the acquire itself. We walk forward up to
    ``lookahead`` lines, skipping blanks and comments; the first real line
    must be ``try:``. Mirrors `_is_acquire_inside_try_with_none_init` in
    only verifying the structural cue — the matching `finally:` is not
    separately checked. Once the operator wrote `<var> = await ... ; try:`,
    the only failure mode is "forgot the close inside finally", which is
    a different bug class than P005 catches.

    Caught when qwen3-coder-next flagged 4 sites in
    `scripts/dev/lease_plane_deprecate.py` on 2026-05-01 (issue #268,
    fingerprints ab83f5e0, f67aebf6, 0f4ceac4, 4ba2a281).
    """
    src_line = snippet_lines_by_num.get(flagged_line, "")
    if not _P005_VAR_AWAIT_ACQUIRE.match(src_line):
        return False
    for line_no in range(flagged_line + 1, flagged_line + lookahead + 1):
        line = snippet_lines_by_num.get(line_no, "")
        if not line.strip() or _looks_like_comment(line):
            continue
        # Function boundary: a new def header before try: means the acquire
        # was the last stmt of its function — not the canonical idiom.
        if _P003_OTHER_DEF.match(line) or _P003_CACHE_FUNC_HEADER.match(line):
            return False
        return bool(_P005_TRY_HEADER.match(line))
    return False


def _is_wait_for_guarded_onboard_pin_helper(
    flagged_line: int,
    snippet_lines_by_num: dict[int, str],
) -> bool:
    """Suppress P004 for Redis pin helpers guarded by public wait_for wrappers.

    `lookup_onboard_pin()` and `set_onboard_pin()` bound their inner Redis
    work with `asyncio.wait_for(..., timeout=_PIN_REDIS_TIMEOUT)`. P004's
    actionable condition is unbounded async Redis in an MCP handler;
    this shape has the timeout mitigation and regression tests already.
    """
    helper_name = None
    helper_def_line = None
    for line_no in range(flagged_line - 1, min(snippet_lines_by_num.keys()) - 1, -1):
        line = snippet_lines_by_num.get(line_no, "")
        if not line.strip():
            continue
        match = _P004_WAIT_FOR_GUARDED_PIN_HELPER.match(line)
        if match:
            helper_name = match.group(1)
            helper_def_line = line_no
            break
        if _P003_OTHER_DEF.match(line):
            return False

    if not helper_name or helper_def_line is None:
        return False

    ordered_lines = sorted(snippet_lines_by_num)
    for idx, line_no in enumerate(ordered_lines):
        if line_no >= helper_def_line:
            break
        line = snippet_lines_by_num[line_no]
        if "asyncio.wait_for" not in line:
            continue
        call_window = "\n".join(
            snippet_lines_by_num[window_line]
            for window_line in ordered_lines
            if line_no <= window_line <= line_no + 12
        )
        if f"{helper_name}(" in call_window:
            return True
    return False


def _is_inside_get_or_create_monitor(
    flagged_line: int, snippet_lines_by_num: dict[int, str]
) -> bool:
    """Return True if the flagged line sits inside the body of the
    ``def get_or_create_monitor`` function. Walks back through the snippet:
    the first def header we hit decides — if it's our cache function, we're
    inside; if it's any other def at the same/outer indent, we're not.
    """
    for line_no in sorted(snippet_lines_by_num.keys(), reverse=True):
        if line_no >= flagged_line:
            continue
        line = snippet_lines_by_num.get(line_no, "")
        if not line.strip():
            continue
        if _P003_CACHE_FUNC_HEADER.match(line):
            return True
        if _P003_OTHER_DEF.match(line):
            return False
    return False


def _has_preceding_persist_call(
    flagged_line: int, snippet_lines_by_num: dict[int, str], lookback: int = 8
) -> bool:
    """Check if any of the `lookback` lines BEFORE flagged_line contains an
    `await <persist-like>(` call. Used to suppress false-positive P011 hits
    where the mutation correctly comes AFTER the persistence call."""
    for line_no in range(flagged_line - lookback, flagged_line):
        line = snippet_lines_by_num.get(line_no, "")
        if not line or _looks_like_comment(line):
            continue
        if _PERSIST_VERB_PATTERN.search(line):
            return True
    return False


def _is_p016_followed_by_inner_layer_check(
    flagged_line: int,
    snippet_lines_by_num: dict[int, str],
    lookahead: int = 20,
) -> bool:
    """Detect the canonical two-layer envelope-parsing shape where the
    flagged outer `.get("success")` is followed within `lookahead` lines
    by an inner-layer assertion — `result.get("isError")`, a typed
    `XxxResult.model_validate(...)`, `_raise_for_tool_failure(...)`, or
    `_parse_mcp_result(...)`. The operator has already wired the inner
    leg; P016 is satisfied. Caught when qwen3 reflagged the SDK's
    `_rest_call` (sync_client.py:342, client.py equivalent) on 2026-05-20.
    """
    for line_no in range(flagged_line + 1, flagged_line + lookahead + 1):
        line = snippet_lines_by_num.get(line_no, "")
        if not line.strip():
            continue
        # A new `def`/`class` at the same-or-lower indent ends the function.
        if _P003_OTHER_DEF.match(line):
            return False
        if _P016_INNER_LAYER_FOLLOWUP.search(line):
            return True
    return False


def _is_p016_inside_inner_assertion_helper(
    flagged_line: int,
    snippet_lines_by_num: dict[int, str],
    lookback: int = 6,
) -> bool:
    """Detect that the flagged success check sits inside a helper named
    `_raise_for_*`. By project convention these helpers operate on
    already-unwrapped inner results — the caller did the outer-envelope
    check before invoking them. Shape:

        def _raise_for_tool_failure(tool_name: str, raw: dict) -> None:
            if raw.get("success") is False:          # ← flagged
                raise GovernanceConnectionError(...)

    Caught when qwen3 reflagged sync_client.py:458 (and client.py's
    equivalent at :545) on 2026-05-20.
    """
    for line_no in range(flagged_line - 1, flagged_line - lookback - 1, -1):
        line = snippet_lines_by_num.get(line_no, "")
        if not line.strip():
            continue
        if _P016_INNER_ASSERTION_HELPER_DEF.match(line):
            return True
        # A different def header above means we walked past our enclosing fn.
        if _P003_OTHER_DEF.match(line):
            return False
    return False


def _verify_finding_against_source(
    finding: Finding, raw_evidence: str, snippet_lines_by_num: dict[int, str]
) -> bool:
    """Drop a finding if it can't be substantiated against actual code.

    Returns True if the finding survives verification.
    """
    # File-path constraint: some patterns only apply under certain paths
    # (e.g. P004 only applies to files under src/mcp_handlers/).
    path_constraints = _PATTERN_FILE_PATH_CONSTRAINTS.get(finding.pattern)
    if path_constraints and not any(seg in finding.file for seg in path_constraints):
        log(
            f"drop {finding.pattern} {finding.file}:{finding.line} — file outside "
            f"required path segments {path_constraints!r}",
            "warning",
        )
        return False
    src_line = snippet_lines_by_num.get(finding.line, "")
    if not src_line:
        log(
            f"drop {finding.pattern} {finding.file}:{finding.line} — line not in scanned region",
            "warning",
        )
        return False
    if _looks_like_comment(src_line):
        log(
            f"drop {finding.pattern} {finding.file}:{finding.line} — flagged line is a comment: {src_line.strip()[:80]}",
            "warning",
        )
        return False
    # Required-token verifier: some patterns must have a literal substring on
    # the flagged line. Without it, the finding is a false positive (model
    # matched the function name or a comment, not the actual code construct).
    required_tokens = _PATTERN_REQUIRED_TOKENS.get(finding.pattern)
    if required_tokens and not any(tok in src_line for tok in required_tokens):
        log(
            f"drop {finding.pattern} {finding.file}:{finding.line} — required token "
            f"{required_tokens!r} not on line: {src_line.strip()[:80]}",
            "warning",
        )
        return False
    # P004 specifically: the onboard-pin Redis helpers in identity/session.py
    # are deliberately wrapped by their public entrypoints with
    # asyncio.wait_for(timeout=_PIN_REDIS_TIMEOUT). They are still in
    # src/mcp_handlers/, but this is the timeout mitigation P004 asks for.
    if (
        finding.pattern == "P004"
        and finding.file.endswith("/src/mcp_handlers/identity/session.py")
        and _is_wait_for_guarded_onboard_pin_helper(finding.line, snippet_lines_by_num)
    ):
        log(
            f"drop P004 {finding.file}:{finding.line} — onboard-pin Redis helper "
            "is bounded by public asyncio.wait_for wrapper",
            "warning",
        )
        return False
    # P001 specifically: if the flagged line assigns create_task() to a name,
    # the task reference is stored somewhere (even if not in a set); the
    # pattern's own library note says "assigned to a variable or added to a
    # collection in the same block → NOT fire-and-forget".
    if finding.pattern == "P001" and _P001_TASK_ASSIGNMENT.search(src_line):
        log(
            f"drop P001 {finding.file}:{finding.line} — task ref assigned on flagged line "
            f"(not fire-and-forget): {src_line.strip()[:80]}",
            "warning",
        )
        return False
    # P001 specifically: `create_tracked_task(...)` is the project's blessed
    # wrapper that stores the task ref in a tracked set. Call sites of it
    # are by construction not fire-and-forget.
    if finding.pattern == "P001" and _P001_TRACKED_HELPER.search(src_line):
        log(
            f"drop P001 {finding.file}:{finding.line} — create_tracked_task() "
            f"wrapper stores ref by construction: {src_line.strip()[:80]}",
            "warning",
        )
        return False
    # P002 specifically: the library text requires growth "without a
    # cap, LRU eviction, or periodic sweep". If a bound cue (len-cap
    # trim, pop/popleft eviction, deque maxlen) sits within a few lines
    # of the flagged growth op, the cap the rule asks for is present —
    # the model just can't see past the single line.
    if finding.pattern == "P002":
        for cue_line_no in range(
            finding.line - _P002_CUE_LINES_BEFORE,
            finding.line + _P002_CUE_LINES_AFTER + 1,
        ):
            nearby = snippet_lines_by_num.get(cue_line_no, "")
            if nearby and _P002_BOUND_CUE.search(nearby):
                log(
                    f"drop P002 {finding.file}:{finding.line} — bound cue at "
                    f"line {cue_line_no}: {nearby.strip()[:80]}",
                    "warning",
                )
                return False
    # P003 specifically: if the flagged line is inside the body of
    # get_or_create_monitor itself (the cache function), the
    # "instantiated outside the cache" rule does not apply.
    if finding.pattern == "P003" and _is_inside_get_or_create_monitor(
        finding.line, snippet_lines_by_num
    ):
        log(
            f"drop P003 {finding.file}:{finding.line} — flag lands inside "
            f"get_or_create_monitor body (the cache itself): {src_line.strip()[:80]}",
            "warning",
        )
        return False
    # P005 specifically: `async with X.acquire() as conn:` (or plain `with`)
    # is the canonical context-managed acquire — release is guaranteed by
    # `__aexit__`, so this is not a leak even though `acquire` appears.
    if finding.pattern == "P005" and _P005_CONTEXT_MANAGED.search(src_line):
        log(
            f"drop P005 {finding.file}:{finding.line} — context-managed acquire "
            f"(async with / with): {src_line.strip()[:80]}",
            "warning",
        )
        return False
    # P005 specifically: a same-name wrapper like `def acquire(...): return
    # self._pool.acquire()` transfers the context manager/resource factory to
    # the caller. The wrapper itself does not await, enter, assign, or consume
    # the resource, so the eventual caller site is where P005 should fire.
    if finding.pattern == "P005" and _is_resource_factory_passthrough(
        finding.line, snippet_lines_by_num
    ):
        log(
            f"drop P005 {finding.file}:{finding.line} — same-name resource "
            f"factory pass-through: {src_line.strip()[:80]}",
            "warning",
        )
        return False
    # P005 specifically: `<var> = None; try: <var> = await X.connect(...)` is
    # the safe manual-release pattern for resource types without async context
    # manager support (e.g. asyncpg.Connection). The pre-init guards finally's
    # None-check and the in-try acquire closes the cancel-between-acquire-and-try
    # gap, so this is not a leak even though the acquire keyword appears.
    if finding.pattern == "P005" and _is_acquire_inside_try_with_none_init(
        finding.line, snippet_lines_by_num
    ):
        log(
            f"drop P005 {finding.file}:{finding.line} — acquire inside try with "
            f"<var> = None pre-init: {src_line.strip()[:80]}",
            "warning",
        )
        return False
    # P005 specifically: `<var> = await X.connect(...); try: ...; finally:
    # await <var>.close()` is the canonical asyncpg idiom for resources
    # without async-context-manager support. Release is unconditional, so
    # this is not a leak even though `acquire`/`connect` appears.
    if finding.pattern == "P005" and _is_acquire_then_try_with_unconditional_close(
        finding.line, snippet_lines_by_num
    ):
        log(
            f"drop P005 {finding.file}:{finding.line} — acquire-then-try idiom "
            f"with unconditional close: {src_line.strip()[:80]}",
            "warning",
        )
        return False
    # P016 specifically: `getattr(<obj>, "success", ...)` is defensive typed-
    # attribute access on a flat object — the quoted "success" string is just
    # the attribute name, not a dict key probing a nested envelope.
    if finding.pattern == "P016" and _P016_GETATTR_SUCCESS.search(src_line):
        log(
            f"drop P016 {finding.file}:{finding.line} — getattr-style typed "
            f"attribute access (no nested envelope): {src_line.strip()[:80]}",
            "warning",
        )
        return False
    # P016 specifically: the flagged outer success check is followed within
    # ~20 lines by an inner-layer assertion (`isError`, `_raise_for_tool_failure`,
    # `_parse_mcp_result`, or `<Result>.model_validate`). This is the canonical
    # SDK envelope-parsing shape: outer envelope failure raises on transport
    # error, then inner-layer assertion raises on tool failure. Caught when
    # qwen3 reflagged sync_client.py:342 on 2026-05-20.
    if finding.pattern == "P016" and _is_p016_followed_by_inner_layer_check(
        finding.line, snippet_lines_by_num
    ):
        log(
            f"drop P016 {finding.file}:{finding.line} — outer success check "
            f"followed by inner-layer assertion (isError / _raise_for_tool_failure "
            f"/ model_validate): {src_line.strip()[:80]}",
            "warning",
        )
        return False
    # P016 specifically: the flagged success check sits inside a `_raise_for_*`
    # helper that operates on an already-unwrapped inner result. By project
    # convention the caller validated the outer envelope before invoking it.
    # Caught when qwen3 reflagged sync_client.py:458 (and client.py:545) on
    # 2026-05-20.
    if finding.pattern == "P016" and _is_p016_inside_inner_assertion_helper(
        finding.line, snippet_lines_by_num
    ):
        log(
            f"drop P016 {finding.file}:{finding.line} — inside _raise_for_* "
            f"inner-layer assertion helper: {src_line.strip()[:80]}",
            "warning",
        )
        return False
    # P011 specifically: if there's an `await persist|archive|save|...` call in
    # the lines preceding the flagged mutation, the temporal ordering is
    # correct and this is a false positive.
    if finding.pattern == "P011" and _has_preceding_persist_call(
        finding.line, snippet_lines_by_num
    ):
        log(
            f"drop P011 {finding.file}:{finding.line} — preceding persist call found, ordering is correct",
            "warning",
        )
        return False
    if raw_evidence:
        # If the model quoted "evidence", verify it (a) isn't comment-like and
        # (b) actually appears somewhere near the flagged line. We allow ±2
        # lines of slack to forgive minor model line-counting drift.
        if _looks_like_comment(raw_evidence):
            log(
                f"drop {finding.pattern} {finding.file}:{finding.line} — evidence is comment-like: {raw_evidence[:80]}",
                "warning",
            )
            return False
        evidence_norm = raw_evidence.strip()
        nearby = " ".join(
            snippet_lines_by_num.get(finding.line + offset, "")
            for offset in range(-2, 3)
        )
        if evidence_norm and evidence_norm[:40] not in nearby:
            log(
                f"drop {finding.pattern} {finding.file}:{finding.line} — evidence not found near line: {evidence_norm[:80]}",
                "warning",
            )
            return False
    return True


def scan_file(
    file_path: str,
    region: str | None = None,
    persist: bool = True,
) -> list[Finding]:
    """Scan a file and return findings.

    ``persist`` controls whether findings are appended to ``findings.jsonl``
    and whether high/critical severity findings get escalated. The self-test
    harness calls this with ``persist=False`` so synthetic results don't
    pollute the real findings feed.
    """
    _common_trim_log(LOG_FILE, MAX_LOG_LINES)
    skip, reason = should_skip(file_path)
    if skip:
        log(f"skip {file_path}: {reason}")
        return []

    log(f"scan {file_path} region={region or 'head'}")
    try:
        code_snippet, region_start, region_end = read_file_region(file_path, region)
    except (OSError, UnicodeDecodeError) as e:
        log(f"failed to read {file_path}: {e}", "error")
        return []

    # Build a line_number → raw line content lookup so verification can compare
    # findings against the actual source.
    snippet_lines_by_num: dict[int, str] = {}
    for raw in code_snippet.splitlines():
        head, _, rest = raw.partition(":")
        try:
            n = int(head.strip())
        except ValueError:
            continue
        snippet_lines_by_num[n] = rest.lstrip()

    patterns_md = load_patterns()
    prompt = build_prompt(patterns_md, file_path, code_snippet)

    try:
        result = call_model(prompt)
    except Exception as e:
        log(f"model call failed: {e}", "error")
        return []

    parsed = parse_findings(
        result["text"], file_path, result.get("model_used", DEFAULT_MODEL), region_start
    )
    findings: list[Finding] = []
    for f, raw_evidence in parsed:
        if not _verify_finding_against_source(f, raw_evidence, snippet_lines_by_num):
            continue
        # Stamp a content hash onto the finding so its fingerprint encodes
        # WHAT the code looked like, not just where it lived. Fixes the
        # silent-dedup bug where bug B arriving at the same line as fixed
        # bug A would never resurface.
        source_line = snippet_lines_by_num.get(f.line, "")
        f.line_content_hash = hash_line_content(source_line)
        f.fingerprint = f.compute_fingerprint()
        findings.append(f)
    if persist:
        fresh = persist_findings(findings)
    else:
        fresh = findings

    log(
        f"scan complete: {len(findings)} raw, {len(fresh)} new, "
        f"tokens={result.get('tokens_used')}, region=L{region_start}-L{region_end}"
        + ("" if persist else " (persist=False)")
    )

    if persist:
        for f in fresh:
            if f.severity in ("high", "critical"):
                escalate(f)

    return fresh


def review_file(
    file_path: str,
    region: str | None = None,
    persist: bool = True,
) -> list[Finding]:
    """Reasoning-based code review — model thinks freely, no pattern library.

    Findings use pattern ID 'R000'. Fingerprint is hint-aware: the hint text
    substitutes for line_content_hash, so two different review observations
    at the same line produce distinct fingerprints while an unchanged
    observation dedupes across runs. ``persist=True`` (default) appends fresh
    findings to findings.jsonl and escalates high/critical through post_finding,
    matching scan_file's persistence contract.
    """
    _common_trim_log(LOG_FILE, MAX_LOG_LINES)
    skip, reason = should_skip(file_path)
    if skip:
        log(f"skip {file_path}: {reason}")
        return []

    log(f"review {file_path} region={region or 'head'}")
    try:
        code_snippet, region_start, region_end = read_file_region(file_path, region)
    except (OSError, UnicodeDecodeError) as e:
        log(f"failed to read {file_path}: {e}", "error")
        return []

    prompt = build_review_prompt(file_path, code_snippet)

    try:
        result = call_model(prompt)
    except Exception as e:
        log(f"model call failed: {e}", "error")
        return []

    raw_text = result["text"]
    # Parse the JSON — review mode returns a simpler schema
    try:
        # Strip thinking tags if present (qwen3 sometimes wraps in <think>)
        cleaned = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to extract JSON from surrounding text
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                log(f"review parse failed: could not extract JSON", "warning")
                return []
        else:
            log(f"review parse failed: no JSON found", "warning")
            return []

    findings = []
    for item in data.get("findings", []):
        line = item.get("line", 0)
        hint = str(item.get("hint", ""))[:80]
        f = Finding(
            pattern="R000",
            file=file_path,
            line=int(line),
            hint=hint,
            severity=item.get("severity", "medium"),
            detected_at=datetime.now(timezone.utc).isoformat(),
            model_used=result.get("model_used", DEFAULT_MODEL),
        )
        # Hint-aware fingerprint: hash the hint text into the content slot so
        # two different review observations at the same line do not silently
        # collapse to one R000|file|line key. Without this, pattern mode's
        # content-aware dedup rule (hash the source line) has no analogue for
        # free-form review observations.
        f.line_content_hash = hash_line_content(hint)
        f.fingerprint = f.compute_fingerprint()
        findings.append(f)

    if persist:
        fresh = persist_findings(findings)
    else:
        fresh = findings

    log(
        f"review complete: {len(findings)} raw, {len(fresh)} new, "
        f"tokens={result.get('tokens_used')}, region=L{region_start}-L{region_end}"
        + ("" if persist else " (persist=False)")
    )

    if persist:
        for f in fresh:
            if f.severity in ("high", "critical"):
                escalate(f)

    return fresh


SELF_TEST_CODE = """async def stuck_agent_recovery_task(self):
    while self.running:
        stale_ephemerals = await self.fetch_stale_ephemerals()
        for ephemeral in stale_ephemerals:
            monitor = self.create_stuck_monitor(ephemeral)
            asyncio.create_task(monitor.watch())
        await asyncio.sleep(300)
"""


def self_test() -> int:
    """Run the watcher against a synthetic known-buggy file and verify that
    at least one P001 (fire-and-forget) finding comes back."""
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", suffix="_selftest.py", delete=False
    ) as tf:
        tf.write(SELF_TEST_CODE)
        tmp_path = tf.name

    try:
        # persist=False so synthetic findings never land in the real
        # findings.jsonl — keeps the self-test entry point safe to run
        # ad-hoc without polluting the live findings feed.
        findings = scan_file(tmp_path, persist=False)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if not findings:
        print("SELF-TEST: FAIL — no findings produced")
        return 1

    hit_p001 = any(f.pattern == "P001" for f in findings)
    for f in findings:
        print(
            f"  [{f.severity}] {f.pattern} {f.file}:{f.line} — {f.hint}"
        )
    if hit_p001:
        print(f"SELF-TEST: PASS — got {len(findings)} finding(s), P001 detected")
        return 0
    print(
        f"SELF-TEST: PARTIAL — {len(findings)} finding(s) but no P001; "
        "pattern library may need a stronger hint"
    )
    return 2


def list_findings(only_open: bool = False) -> int:
    findings = _iter_findings_raw()
    if not findings:
        print("(no findings file yet)")
        return 0
    shown = 0
    for d in findings:
        status = d.get("status", "open")
        if only_open and status not in ("open", "surfaced"):
            continue
        fp = d.get("fingerprint", "?")[:8]
        print(
            f"{fp}  {status:9s} {d.get('severity','?'):8s} {d.get('pattern','?'):6s} "
            f"{d.get('file','?')}:{d.get('line','?')} — {d.get('hint','')}"
        )
        shown += 1
    if shown == 0:
        print("(nothing to show)")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Carry forward any legacy checkout-relative state to the shared dir once
    # per process, before anything reads/writes findings.
    migrate_legacy_watcher_state()

    parser = argparse.ArgumentParser(description="UNITARES Watcher bug-pattern agent")
    parser.add_argument("--file", help="file to scan")
    parser.add_argument("--region", help="line range within file, e.g. L10-L40")
    parser.add_argument(
        "--review", action="store_true",
        help="reasoning-based review (no pattern library, model thinks freely)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="run pattern scan AND reasoning review in one process (hook default)",
    )
    parser.add_argument(
        "--self-test", action="store_true", help="scan a synthetic buggy file and verify"
    )
    parser.add_argument(
        "--list-findings", action="store_true", help="dump current findings.jsonl"
    )
    parser.add_argument(
        "--only-open",
        action="store_true",
        help="with --list-findings, show only open/surfaced entries",
    )
    parser.add_argument(
        "--resolve",
        metavar="FINGERPRINT",
        help="mark a finding as confirmed by fingerprint (or unique prefix, min 4 chars)",
    )
    parser.add_argument(
        "--dismiss",
        metavar="FINGERPRINT",
        help="mark a finding as dismissed (false positive) by fingerprint",
    )
    parser.add_argument(
        "--sweep-stale",
        action="store_true",
        help="drop findings whose target file no longer exists on disk",
    )
    parser.add_argument(
        "--agent-id",
        metavar="UUID",
        help="governance UUID of the agent resolving/dismissing (for audit trail)",
    )
    parser.add_argument(
        "--reason",
        metavar="TEXT",
        help="short rationale for --resolve/--dismiss; stored on the finding "
             "and included in the governance event. For --dismiss, must be one "
             "of {fp, wont_fix, out_of_scope, dup, unclear, stale} — only 'fp' "
             "counts as a true negative in precision math.",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="drop resolved/dismissed/aged_out findings older than the TTL",
    )
    parser.add_argument(
        "--compact-days",
        type=int,
        default=7,
        help="age cutoff for --compact (default: 7 days)",
    )
    parser.add_argument(
        "--print-unresolved",
        action="store_true",
        help="print the unresolved-findings block (open+surfaced) without mutating state",
    )
    parser.add_argument(
        "--surface-pending",
        action="store_true",
        help="print open findings as a chime block and transition them to surfaced",
    )
    parser.add_argument(
        "--recompute-floor",
        action="store_true",
        help="recompute pattern_floor.json from findings.jsonl and persist atomically",
    )
    parser.add_argument(
        "--scan-commits",
        action="store_true",
        help="scan recent git commits for fingerprint references and "
             "auto-resolve matching open/surfaced findings; skips reverts, "
             "dismissed findings, and findings that already have a resolution_reason",
    )
    parser.add_argument(
        "--scan-since",
        metavar="GIT_SINCE",
        default="30 days ago",
        help='git --since=<value> for --scan-commits (default: "30 days ago")',
    )
    args = parser.parse_args(argv)

    # --- Identity resolution (best-effort) ---
    client = None
    try:
        client = _make_identity_client()
        resolve_identity(client)
    except Exception as e:
        log(f"identity resolution skipped: {e}", "warning")

    if args.self_test:
        return self_test()
    if args.list_findings:
        return list_findings(only_open=args.only_open)
    if args.resolve:
        rc = _run_with_watcher_findings_lease(
            f"resolve {args.resolve}",
            lambda: update_finding_status(
                args.resolve,
                "confirmed",
                resolver_agent_id=args.agent_id,
                reason=args.reason,
            ),
            holder_agent_id=args.agent_id,
        )
        if rc == 0:
            _emit_resolution_outcome(client, "confirmed", args.resolve, args.reason)
        return rc
    if args.dismiss:
        rc = _run_with_watcher_findings_lease(
            f"dismiss {args.dismiss}",
            lambda: update_finding_status(
                args.dismiss,
                "dismissed",
                resolver_agent_id=args.agent_id,
                reason=args.reason,
            ),
            holder_agent_id=args.agent_id,
        )
        if rc == 0:
            _emit_resolution_outcome(client, "dismissed", args.dismiss, args.reason)
        return rc
    if args.sweep_stale:
        return _run_with_watcher_findings_lease(
            "sweep stale findings",
            sweep_stale_findings,
            holder_agent_id=args.agent_id,
        )
    if args.compact:
        return _run_with_watcher_findings_lease(
            f"compact findings older than {args.compact_days}d",
            lambda: compact_findings(max_age_days=args.compact_days),
            holder_agent_id=args.agent_id,
        )
    if args.print_unresolved:
        return print_unresolved()
    if args.surface_pending:
        return _run_with_watcher_findings_lease(
            "surface pending findings",
            surface_pending,
            holder_agent_id=args.agent_id,
        )
    if args.scan_commits:
        return 0 if scan_commits(since=args.scan_since) >= 0 else 1
    if args.recompute_floor:
        from agents.watcher.floor_state import recompute_floor
        state = recompute_floor()
        log(
            f"recompute_floor: {len(state.buckets)} bucket(s) "
            f"updated_at={state.updated_at}"
        )
        print(f"ok: {len(state.buckets)} bucket(s) at {state.updated_at}")
        return 0
    if not args.file:
        parser.print_help()
        return 1

    if args.all:
        # Pattern scan first — cheap, bounded, library-driven.
        # Review scan second — broader reasoning, persists R000 findings.
        # Both persist through findings.jsonl; failures in either do not
        # abort the other (hook-invoked path must stay best-effort).
        pattern_fresh: list[Finding] = []
        try:
            pattern_fresh = scan_file(args.file, args.region)
        except Exception as e:
            log(f"--all: scan_file failed: {e}", "error")
        try:
            review_file(args.file, args.region)
        except Exception as e:
            log(f"--all: review_file failed: {e}", "error")
        for f in pattern_fresh:
            print(f"[{f.severity}] {f.pattern} {f.file}:{f.line} — {f.hint}")
        return 0

    if args.review:
        findings = review_file(args.file, args.region)
        if findings:
            for f in findings:
                print(f"[{f.severity}] {f.file}:{f.line} — {f.hint}")
        else:
            print("No issues found.")
        return 0

    fresh = scan_file(args.file, args.region)
    if fresh:
        for f in fresh:
            print(
                f"[{f.severity}] {f.pattern} {f.file}:{f.line} — {f.hint}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
