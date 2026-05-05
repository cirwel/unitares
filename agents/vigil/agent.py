#!/usr/bin/env python3
"""
Vigil — The First Resident

A persistent agent that runs every 30 minutes via launchd, checks system health,
and checks in to governance. Builds the longest-running EISV trajectory in the
system. Leaves notes in the knowledge graph when something changes, creating
continuity between ephemeral Claude Code sessions.

Usage:
    python3 agents/vigil/agent.py              # Health checks only (default)
    python3 agents/vigil/agent.py --with-tests  # Also run test suites (~15 min)
    python3 agents/vigil/agent.py --daemon      # Continuous loop

What it does each cycle:
    1. Resumes persistent "Vigil" identity (same UUID across all cycles)
    2. Checks governance health (HTTP /health)
    3. Checks Lumen/anima health (HTTP /health, LAN → Tailscale fallback)
    4. (optional) Runs governance-mcp + anima-mcp pytest suites
    5. Detects changes from previous cycle, leaves notes in knowledge graph
    6. Checks in to governance with findings (process_agent_update)
    7. Self-recovers if paused
    8. Logs one-line summary
"""

import asyncio

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import httpx

from agents.common.config import GOV_MCP_URL
from unitares_sdk.agent import CycleResult, GovernanceAgent
from unitares_sdk.client import GovernanceClient
from unitares_sdk.utils import notify
from agents.common.findings import post_finding, compute_fingerprint
from agents.vigil.checks.registry import load_plugins
from agents.vigil.checks.runner import run_health_checks
from agents.watcher.floor_state import load_floor, recompute_floor

# Paths
ANIMA_PROJECT = Path(os.getenv("ANIMA_PROJECT", str(project_root.parent / "anima-mcp")))
SESSION_FILE = Path.home() / ".unitares" / "anchors" / "vigil.json"
LEGACY_SESSION_FILE = project_root / ".vigil_session"
STATE_FILE = project_root / ".vigil_state"
LOG_FILE = Path.home() / "Library" / "Logs" / "unitares-vigil.log"
MAX_LOG_LINES = 500

# Test timeout
TEST_TIMEOUT = 180  # 3 minutes per suite

# Wall-clock cap for a single heartbeat cycle.
CYCLE_TIMEOUT = int(os.getenv("HEARTBEAT_CYCLE_TIMEOUT", "120"))

# KG hygiene v1: retrieval-eval step configuration
RETRIEVAL_EVAL_DIR = project_root / "tests" / "retrieval_eval"
RETRIEVAL_EVAL_SCRIPT = project_root / "scripts" / "eval" / "retrieval_eval.py"
NDCG_REGRESSION_THRESHOLD = 0.05  # absolute drop in nDCG@10 that flags regression

# Watcher calibration: recompute the precision floor at most once per day.
# Vigil cycles every 30min, so a 24h gate keeps us from recomputing 48× per day.
WATCHER_FLOOR_MAX_AGE_HOURS = 24.0


def _last_floor_recompute_iso() -> str | None:
    """Read pattern_floor.json's updated_at field. Returns None if the
    file is missing/unparseable so the caller falls through to a recompute."""
    try:
        return load_floor().updated_at
    except Exception:
        return None


def maybe_recompute_watcher_floor(
    *, max_age_hours: float = WATCHER_FLOOR_MAX_AGE_HOURS
) -> bool:
    """Trigger a watcher floor recompute if the last one is older than
    ``max_age_hours``. Returns True if a recompute fired.

    Called from Vigil's run_cycle. Atomic write means a concurrent
    surface hook can never see a half-written file.
    """
    last_iso = _last_floor_recompute_iso()
    if last_iso:
        try:
            last = datetime.strptime(last_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            age = datetime.now(timezone.utc) - last
            if age < timedelta(hours=max_age_hours):
                return False
        except (TypeError, ValueError):
            pass  # unparseable → recompute (safer to refresh than skip)
    recompute_floor()
    return True


_interactive = sys.stdout.isatty()


def log(message: str):
    """Append timestamped line to log file. Also prints if running interactively."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {message}"
    if _interactive:
        print(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def detect_changes(prev: Dict[str, Any], current: Dict[str, Any]) -> List[Dict[str, str]]:
    """Compare previous and current cycle state. Returns list of notable changes."""
    notes: List[Dict[str, str]] = []

    # Health status transitions
    for service in ("governance", "lumen"):
        prev_ok = prev.get(f"{service}_healthy")
        curr_ok = current.get(f"{service}_healthy")
        if prev_ok is not None and prev_ok != curr_ok:
            if curr_ok:
                notes.append({
                    "summary": f"{service.title()} recovered (was down)",
                    "tags": ["vigil", "recovery", service],
                })
            else:
                notes.append({
                    "summary": f"{service.title()} is down ({current.get(f'{service}_detail', '?')})",
                    "tags": ["vigil", "outage", service],
                })

    # Consecutive Lumen outage
    prev_streak = prev.get("lumen_down_streak", 0)
    curr_streak = current.get("lumen_down_streak", 0)
    if curr_streak >= 3 and curr_streak > prev_streak and curr_streak % 3 == 0:
        hours = curr_streak * 0.5  # 30-min cycles
        notes.append({
            "summary": f"Lumen unreachable for {curr_streak} consecutive cycles (~{hours:.0f}h)",
            "tags": ["vigil", "outage", "lumen", "sustained"],
        })

    # EISV drift
    prev_coherence = prev.get("coherence")
    curr_coherence = current.get("coherence")
    if prev_coherence is not None and curr_coherence is not None:
        if curr_coherence < 0.40 and prev_coherence >= 0.40:
            notes.append({
                "summary": f"Vigil coherence dropped below 0.40 ({curr_coherence:.3f})",
                "tags": ["vigil", "drift", "coherence"],
            })

    prev_verdict = prev.get("verdict")
    curr_verdict = current.get("verdict")
    if prev_verdict and curr_verdict and prev_verdict != curr_verdict:
        if curr_verdict in ("pause", "reject"):
            notes.append({
                "summary": f"Vigil verdict changed: {prev_verdict} -> {curr_verdict}",
                "tags": ["vigil", "verdict", curr_verdict],
            })

    # Groundskeeper: staleness spike detection
    prev_stale = prev.get("groundskeeper_stale", 0)
    curr_stale = current.get("groundskeeper_stale", 0)
    if curr_stale > prev_stale + 10:
        notes.append({
            "summary": f"KG staleness spike: {prev_stale} -> {curr_stale} stale entries",
            "tags": ["vigil", "groundskeeper", "drift"],
        })

    return notes


def _collect_health_state(
    check_results: List[Tuple[Any, Any]],
    prev_state: Dict[str, Any],
) -> Dict[str, Any]:
    """Turn check results into Vigil's state-dict fragments.

    Preserves the existing state file keys (``governance_healthy``,
    ``lumen_healthy``, ``gov_up_cycles``, ``lumen_up_cycles``,
    ``lumen_down_streak``, ``lumen_last_ok_url``) so the refactor is
    invisible to the persisted .vigil_state format. Plugin-persistent
    fields are pulled from ``result.detail``.
    """
    state: Dict[str, Any] = {}
    by_key = {c.service_key: r for c, r in check_results}

    gov_r = by_key.get("governance")
    gov_healthy = gov_r.ok if gov_r else True
    state["governance_healthy"] = gov_healthy
    state["governance_detail"] = gov_r.summary if gov_r else "no check"
    state["gov_up_cycles"] = prev_state.get("gov_up_cycles", 0) + (1 if gov_healthy else 0)

    lumen_r = by_key.get("lumen")
    anima_healthy = lumen_r.ok if lumen_r else True
    state["lumen_healthy"] = anima_healthy
    state["lumen_detail"] = lumen_r.summary if lumen_r else "not configured"
    state["lumen_up_cycles"] = prev_state.get("lumen_up_cycles", 0) + (1 if anima_healthy else 0)

    lumen_down_streak = 0
    if lumen_r and not anima_healthy:
        lumen_down_streak = prev_state.get("lumen_down_streak", 0) + 1
    state["lumen_down_streak"] = lumen_down_streak

    if lumen_r and lumen_r.detail:
        state.update(lumen_r.detail)

    return state


def run_pytest(project_dir: Path, label: str) -> Tuple[bool, int, int, str]:
    """Run pytest on a project. Returns (passed, n_passed, n_failed, summary)."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=line", "-x"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT,
        )
        output = result.stdout + result.stderr
        import re
        n_passed = 0
        n_failed = 0
        for line in output.splitlines():
            line_lower = line.lower()
            if "passed" in line_lower or "failed" in line_lower:
                passed_match = re.search(r"(\d+)\s+passed", line_lower)
                failed_match = re.search(r"(\d+)\s+failed", line_lower)
                if passed_match:
                    n_passed = int(passed_match.group(1))
                if failed_match:
                    n_failed = int(failed_match.group(1))

        passed = result.returncode == 0
        summary = f"{label}: {'PASS' if passed else 'FAIL'} ({n_passed} passed, {n_failed} failed)"
        return passed, n_passed, n_failed, summary
    except subprocess.TimeoutExpired:
        return False, 0, 0, f"{label}: TIMEOUT ({TEST_TIMEOUT}s)"
    except Exception as e:
        return False, 0, 0, f"{label}: ERROR ({e})"


# Sentinel findings that trigger a groundskeeper pass even when --no-audit is set.
# These are fleet-level symptoms that a KG audit can help surface or remediate.
# Names match what Sentinel actually emits (agents/sentinel/agent.py:249,266) and
# the canonical taxonomy at agents/common/violation_taxonomy.yaml.
_SENTINEL_AUDIT_TRIGGERS = frozenset({
    "verdict_shift",
    "correlated_events",
})


def _filter_sentinel_findings(
    results: List[Dict[str, Any]], since_iso: Optional[str]
) -> List[Dict[str, Any]]:
    """Filter raw search_knowledge results down to recent Sentinel high-severity notes.

    Sentinel writes notes tagged ``["sentinel", <finding_type>, "high"]``. We
    want only those, only created after ``since_iso`` (Vigil's last cycle time),
    and annotated with the extracted finding type.
    """
    out: List[Dict[str, Any]] = []
    for d in results:
        if not isinstance(d, dict):
            continue
        tags = d.get("tags") or []
        if "sentinel" not in tags or "high" not in tags:
            continue
        created_at = d.get("created_at")
        if since_iso and created_at and created_at <= since_iso:
            continue
        # Finding type is the tag that isn't "sentinel", "high", or a meta tag.
        finding_type = next(
            (t for t in tags if t not in ("sentinel", "high", "note")),
            "unknown",
        )
        out.append({
            "summary": d.get("summary", ""),
            "type": finding_type,
            "created_at": created_at,
            "id": d.get("id"),
        })
    return out


# ---------------------------------------------------------------------------
# KG hygiene v1: retrieval-eval helpers
# ---------------------------------------------------------------------------

def _derive_eval_config_tag() -> str:
    """Derive a config tag matching baseline filename suffix from env vars.

    Tag values mirror existing baselines in tests/retrieval_eval/:
    bge_m3, bge_m3_reranked, hybrid_rrf, hybrid_graph.
    """
    embedding = os.environ.get("UNITARES_EMBEDDING_MODEL", "").strip().lower()
    hybrid = os.environ.get("UNITARES_ENABLE_HYBRID", "").strip() == "1"
    graph = os.environ.get("UNITARES_ENABLE_GRAPH_EXPANSION", "").strip() == "1"
    reranker = os.environ.get("UNITARES_ENABLE_RERANKER", "").strip() == "1"

    base = "bge_m3" if "bge-m3" in embedding else (embedding.replace("-", "_") or "default")

    if graph:
        return "hybrid_graph"
    if hybrid:
        return "hybrid_rrf"
    if reranker:
        return f"{base}_reranked"
    return base


def _pick_eval_baseline(baseline_dir: Path, config_tag: str) -> Optional[Path]:
    """Return newest baseline_*_<config_tag>.json by mtime, or None."""
    if not baseline_dir.exists():
        return None
    matches = sorted(
        baseline_dir.glob(f"baseline_*_{config_tag}.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def _run_eval_subprocess() -> Dict[str, Any]:
    """Invoke the eval harness as a subprocess; return parsed JSON metrics.

    Sync function — designed to be called via ``run_in_executor`` from the
    async cycle. Returns ``{}`` on any failure (caller handles).
    """
    try:
        proc = subprocess.run(
            ["python3", str(RETRIEVAL_EVAL_SCRIPT), "--json"],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            log(f"eval subprocess returned {proc.returncode}: {proc.stderr[:200]}")
            return {}
        return json.loads(proc.stdout)
    except subprocess.TimeoutExpired:
        log("eval subprocess timed out after 300s")
        return {}
    except json.JSONDecodeError as e:
        log(f"eval subprocess JSON parse failed: {e}")
        return {}
    except Exception as e:
        log(f"eval subprocess failed: {e}")
        return {}


class VigilAgent(GovernanceAgent):
    def __init__(
        self,
        mcp_url: str = GOV_MCP_URL,
        label: str = "Vigil",
        heartbeat_interval: int = 1800,
        with_tests: bool = False,
        with_audit: bool = True,
        with_hygiene: bool = False,
        with_eval: bool = False,
        force_new: bool = False,
    ):
        super().__init__(
            name=label,
            mcp_url=mcp_url,
            session_file=SESSION_FILE,
            legacy_session_file=LEGACY_SESSION_FILE,
            state_dir=STATE_FILE.parent,
            timeout=30.0,
            persistent=True,
            refuse_fresh_onboard=True,
            cycle_timeout_seconds=CYCLE_TIMEOUT,
            log_file=LOG_FILE,
            max_log_lines=MAX_LOG_LINES,
            state_file=STATE_FILE,
        )
        self.heartbeat_interval = heartbeat_interval
        self.with_tests = with_tests
        self.with_audit = with_audit
        self.with_hygiene = with_hygiene
        self.with_eval = with_eval
        self.force_new = force_new
        # Vigil-specific cycle data (populated during run_cycle, used in post-checkin)
        self._cycle_state: Dict[str, Any] = {}
        self._cycle_prev_state: Dict[str, Any] = {}
        # Register built-in checks + any VIGIL_CHECK_PLUGINS externals.
        # Idempotent: safe to call from multiple VigilAgent instances in tests.
        load_plugins()

    async def _read_sentinel_findings(
        self, client: GovernanceClient, since_iso: Optional[str]
    ) -> List[Dict[str, Any]]:
        """Query KG for recent high-severity Sentinel notes newer than ``since_iso``.

        Returns empty list on any failure — coordination is best-effort, a
        broken search must not poison the cycle. Bounded to 15s so a hung MCP
        call can't eat the full cycle timeout.
        """
        try:
            result = await asyncio.wait_for(
                client.search_knowledge(
                    query="sentinel", tags=["sentinel"], limit=10, semantic=False,
                ),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            log("sentinel-read timed out after 15s; continuing cycle")
            return []
        except Exception as e:
            log(f"sentinel-read failed ({e}); continuing cycle")
            return []
        if not getattr(result, "success", False):
            return []
        return _filter_sentinel_findings(result.results or [], since_iso)

    async def _run_groundskeeper(
        self,
        client: GovernanceClient,
        prev_state: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """KG audit + lifecycle cleanup.

        Orphan agent archival is no longer part of the Groundskeeper cycle —
        the auto-sweep was hiding initializing-agent bugs behind archival.
        Operators can still invoke ``archive_orphan_agents`` manually.

        ``prev_state`` is the previous cycle's state dict; when supplied, the
        summary KG note is suppressed if (stale_found, archived) is unchanged
        from the prior cycle. Local log + findings still surface every cycle.
        """
        summary: Dict[str, Any] = {
            "audit_run": False,
            "stale_found": 0,
            "archived": 0,
            "errors": [],
        }

        try:
            audit_result = await client.audit_knowledge(scope="open", top_n=10)
            if audit_result.success:
                summary["audit_run"] = True
                # Server returns audit payload under `audit`, not `results`.
                # Iterating `.results` stays empty and leaves stale_found=0 —
                # the silent failure that hid 127 archive candidates for months.
                audit_data = getattr(audit_result, "audit", None) or {}
                buckets = audit_data.get("buckets", {}) if isinstance(audit_data, dict) else {}
                summary["stale_found"] = (
                    buckets.get("stale", 0) + buckets.get("candidate_for_archive", 0)
                )

                if summary["stale_found"] > 0:
                    cleanup_result = await client.cleanup_knowledge(dry_run=False)
                    if cleanup_result.success:
                        # Use cleaned_total (sums discoveries/ephemeral/deleted
                        # from the server's cleanup_result dict) rather than
                        # the legacy `cleaned` field that the server never
                        # populates.
                        summary["archived"] = cleanup_result.cleaned_total
            else:
                err = getattr(audit_result, "error", None) or "Audit failed"
                summary["errors"].append(str(err))
                log(f"GROUNDSKEEPER: audit_knowledge failed — {err}")

        except Exception as e:
            summary["errors"].append(str(e))
            log(f"GROUNDSKEEPER: exception during audit — {type(e).__name__}: {e}")

        if summary["audit_run"]:
            note_text = (
                f"Groundskeeper: {summary['stale_found']} stale, "
                f"{summary['archived']} archived"
            )
            prev = prev_state or {}
            unchanged = (
                prev.get("groundskeeper_stale") == summary["stale_found"]
                and prev.get("groundskeeper_archived") == summary["archived"]
            )
            if unchanged and prev:
                summary["note_suppressed"] = True
                log(f"GROUNDSKEEPER: {note_text} (note suppressed — unchanged)")
            else:
                try:
                    await client.leave_note(
                        summary=note_text,
                        tags=["vigil", "groundskeeper", "audit", "ephemeral"],
                    )
                except Exception:
                    pass
                log(f"GROUNDSKEEPER: {note_text}")

        return summary

    async def _run_aged_candidate_archive(
        self, client: GovernanceClient,
    ) -> Dict[str, Any]:
        """Auto-archive KG entries that have sat in candidate_for_archive too long.

        Closes the gap between the audit (which classifies aged-open entries
        as archive candidates) and ``cleanup_knowledge`` (which only acts on
        the lifecycle ladder — resolved→archived, ephemeral→archived, etc.,
        never on entries still in ``open`` status). Without this step, the
        candidate_for_archive bucket grows monotonically because authors
        rarely close conversational/exploration entries when sessions end.

        Conservative by design — exists in tension with the 2026-04-19
        vigil-aggression posture (auto-archive was once hiding initializing-
        agent bugs). Safeguards:

          - Gated on ``with_hygiene`` (default False; matches the
            propose-only sweep — operator opts in explicitly).
          - Only acts on bucket=``candidate_for_archive``. ``_score_discovery``
            excludes permanent types/tags via its policy check.
          - Defense in depth: requires entry.activity_score == 0 (no
            ``responses_from`` AND no ``related_to``). NOTE: the bucket
            classifier in ``_score_discovery`` only checks ``responses_from``
            for the healthy guard (not ``related_to``), so an entry that is
            cross-linked but unanswered can land in candidate_for_archive
            despite being referenced. We re-check activity_score here so
            cross-linked load-bearing notes are not auto-archived.
          - Requires last_activity_days > VIGIL_AUTO_ARCHIVE_AGE_DAYS
            (default 90, i.e., 3x the bucket-entry threshold) — extra margin
            so a freshly-classified entry gets weeks of grace before action.
          - Caps at VIGIL_AUTO_ARCHIVE_MAX_PER_CYCLE per run (default 20).
          - High-severity entries fall back to status="closed" — the
            server's cross-agent permission guard rejects ``archived`` for
            high-sev. Detection: ``error_code == "PERMISSION_DENIED"`` (the
            server's structured field) with substring fallback for older
            servers that may not emit error_code.
          - Per-entry try/except: one failure doesn't poison the rest.
          - Reversible: status mutation only, never deletion. Archived rows
            remain searchable with ``include_cold=true``.
        """
        summary: Dict[str, Any] = {
            "auto_archive_run": False,
            "candidates_seen": 0,
            "archived": 0,
            "errors": [],
        }

        if not self.with_hygiene:
            return summary

        threshold_days = int(os.getenv("VIGIL_AUTO_ARCHIVE_AGE_DAYS", "90"))
        max_per_cycle = int(os.getenv("VIGIL_AUTO_ARCHIVE_MAX_PER_CYCLE", "20"))

        try:
            result = await asyncio.wait_for(
                client.audit_knowledge(scope="open", top_n=max_per_cycle * 3),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            summary["errors"].append("audit_timeout")
            log("auto-archive: audit timed out after 15s; skipping cycle")
            return summary
        except Exception as e:
            summary["errors"].append(f"audit_failed: {type(e).__name__}")
            log(f"auto-archive: audit failed ({type(e).__name__}: {e}); skipping cycle")
            return summary

        if not getattr(result, "success", False):
            return summary

        audit_data = getattr(result, "audit", None) or {}
        if not isinstance(audit_data, dict):
            return summary

        top_stale = audit_data.get("top_stale", []) or []
        eligible = [
            e for e in top_stale
            if e.get("bucket") == "candidate_for_archive"
            and e.get("last_activity_days", 0) > threshold_days
            and e.get("activity_score", 0) == 0
        ][:max_per_cycle]

        summary["auto_archive_run"] = True
        summary["candidates_seen"] = len(eligible)

        for entry in eligible:
            eid = entry.get("id")
            if not eid:
                continue
            archived_ok = False
            try:
                raw = await asyncio.wait_for(
                    client.call_tool("knowledge", {
                        "action": "update",
                        "discovery_id": eid,
                        "status": "archived",
                    }),
                    timeout=5.0,
                )
                # Defense: server should always return a dict, but call_tool
                # could return None or a primitive on a malformed response.
                # Don't attempt the high-sev fallback in that case.
                if not isinstance(raw, dict):
                    summary["errors"].append(f"{eid[:24]}: non-dict response")
                    continue
                if raw.get("success"):
                    archived_ok = True
                elif (raw.get("error_code") == "PERMISSION_DENIED"
                      or "high-severity" in (raw.get("error") or "").lower()):
                    raw2 = await asyncio.wait_for(
                        client.call_tool("knowledge", {
                            "action": "update",
                            "discovery_id": eid,
                            "status": "closed",
                        }),
                        timeout=5.0,
                    )
                    if isinstance(raw2, dict) and raw2.get("success"):
                        archived_ok = True
                    else:
                        summary["errors"].append(f"{eid[:24]}: high-sev close failed")
                else:
                    err = raw.get("error") or "unknown"
                    summary["errors"].append(f"{eid[:24]}: {err[:80]}")
            except Exception as e:
                summary["errors"].append(f"{eid[:24]}: {type(e).__name__}")

            if archived_ok:
                summary["archived"] += 1
                log(
                    f"AUTO_ARCHIVE: {eid} "
                    f"(age={entry.get('last_activity_days')}d)"
                )

        if summary["archived"] > 0:
            log(
                f"AUTO_ARCHIVE: archived {summary['archived']}/{len(eligible)} "
                f"aged candidate_for_archive entries (>{threshold_days}d inactive)"
            )

        return summary

    async def _run_stale_opens_sweep(
        self, client: GovernanceClient, top_n: int = 20,
    ) -> List[Dict[str, Any]]:
        """KG hygiene v1: propose-only sweep of oldest stale-open KG entries.

        Reads via ``client.audit_knowledge(scope='open')``; the audit handler
        already scores each open entry (via _score_discovery in
        knowledge_graph_lifecycle.py) and returns ``top_stale`` ordered by
        last_activity_days desc with bucket classification + permanent-policy
        already applied. We just take up to ``top_n`` entries and surface them.

        Returns oldest-first (matches the audit's own ordering). Empty list
        on any failure or when ``with_hygiene`` is False — propose-only is
        best-effort, must not poison the cycle.
        """
        if not self.with_hygiene:
            return []

        try:
            result = await asyncio.wait_for(
                client.audit_knowledge(scope="open", top_n=top_n),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            log("stale-opens sweep timed out after 15s; continuing cycle")
            return []
        except Exception as e:
            log(f"stale-opens sweep failed ({e}); continuing cycle")
            return []

        if not getattr(result, "success", False):
            return []

        audit_data = getattr(result, "audit", None) or {}
        if not isinstance(audit_data, dict):
            return []
        top_stale = audit_data.get("top_stale", []) or []

        # The audit already orders by last_activity_days desc. Re-sort
        # defensively in case the contract ever changes.
        top_stale.sort(key=lambda x: x.get("last_activity_days", 0), reverse=True)
        return top_stale[:top_n]

    async def _run_eval_step(self) -> Dict[str, Any]:
        """KG hygiene v1: run retrieval_eval as a subprocess; diff against baseline.

        Subprocess runs via ``run_in_executor`` to avoid the anyio-asyncpg
        deadlock surface (the eval script itself awaits asyncpg internally).

        Returns a dict with keys: ``ran`` (bool), ``metrics`` (dict),
        ``baseline`` (str|None), ``delta`` (dict), ``regression`` (bool),
        plus optional ``no_baseline_warning``, ``diff_error``, ``reason``.
        Never raises — disabled or failed runs return ``{"ran": False, ...}``.
        """
        if not self.with_eval:
            return {"ran": False, "reason": "with_eval=False"}

        loop = asyncio.get_event_loop()
        try:
            metrics = await loop.run_in_executor(None, _run_eval_subprocess)
        except Exception as e:
            log(f"eval step failed: {e}")
            return {"ran": False, "reason": f"executor error: {e}"}

        if not metrics:
            return {"ran": False, "reason": "empty metrics"}

        config_tag = _derive_eval_config_tag()
        baseline_path = _pick_eval_baseline(RETRIEVAL_EVAL_DIR, config_tag)

        result: Dict[str, Any] = {
            "ran": True,
            "metrics": metrics,
            "config_tag": config_tag,
            "baseline": baseline_path.name if baseline_path else None,
            "delta": {},
            "regression": False,
        }

        if baseline_path is None:
            result["no_baseline_warning"] = (
                f"no matching baseline for config={config_tag}; "
                f"run output not compared. Promote manually if good."
            )
            log(result["no_baseline_warning"])
            return result

        try:
            baseline = json.loads(baseline_path.read_text())
            base_metrics = baseline.get("metrics", baseline)  # tolerate flat or nested
            for key in ("nDCG@10", "Recall@20", "MRR", "latency_p50", "latency_p95"):
                if key in metrics and key in base_metrics:
                    result["delta"][key] = metrics[key] - base_metrics[key]
            ndcg_delta = result["delta"].get("nDCG@10", 0.0)
            if ndcg_delta < -NDCG_REGRESSION_THRESHOLD:
                result["regression"] = True
        except Exception as e:
            log(f"baseline diff failed: {e}")
            result["diff_error"] = str(e)

        return result

    async def run_cycle(self, client: GovernanceClient) -> CycleResult | None:
        """Run one heartbeat cycle.

        Phase A advisory lease wraps the cycle so concurrent Vigil instances
        (rare but possible — operator running --once while launchd cron also
        fires) surface in telemetry. Outcome does NOT gate execution; held
        leases proceed normally per RFC v0.5 §6.1.
        """
        from src.lease_plane.advisory import lease_advisory_scope, new_holder_uuid

        # Migrated from "vigil:cycle" → "resident:/vigil_cycle" per RFC v0.8 §7.2.1.
        with lease_advisory_scope(
            surface_id="resident:/vigil_cycle",
            holder_agent_uuid=new_holder_uuid(),
            ttl_s=300,
            intent="vigil heartbeat cycle",
        ):
            return await self._run_cycle_inner(client)

    async def _run_cycle_inner(self, client: GovernanceClient) -> CycleResult | None:
        findings: List[str] = []
        issues = 0
        prev_state = self.load_state()
        self._cycle_prev_state = prev_state

        # --- 1. Run registered health checks (governance built-in + plugins) ---
        check_results = await run_health_checks(prev_state)
        health_state = _collect_health_state(check_results, prev_state)

        for _check, result in check_results:
            findings.append(result.summary)
            if not result.ok:
                issues += 1

        gov_healthy = health_state["governance_healthy"]
        gov_detail = health_state["governance_detail"]
        anima_healthy = health_state["lumen_healthy"]
        anima_detail = health_state["lumen_detail"]
        anima_ok_url = health_state.get("lumen_last_ok_url")
        lumen_down_streak = health_state["lumen_down_streak"]

        # --- Transition-emit: page once on healthy -> unhealthy per service ---
        for check, result in check_results:
            svc = check.service_key
            was_healthy = prev_state.get(f"{svc}_healthy", True)
            if not result.ok and was_healthy and result.fingerprint_key:
                notify("Vigil", result.summary)
                post_finding(
                    event_type="vigil_finding",
                    severity=result.severity,
                    message=result.summary,
                    agent_id="vigil",
                    agent_name="Vigil",
                    fingerprint=compute_fingerprint(["vigil", result.fingerprint_key]),
                    extra={"finding_type": result.fingerprint_key},
                )

        # --- 2.5. Read Sentinel findings since last cycle, route to action ---
        # First actual coordination arc: Sentinel observes fleet-level anomalies
        # and writes them to the KG as high-severity notes. Vigil reads them and
        # either runs an audit or references them in its check-in so the chain
        # shows up in the governance audit trail.
        sentinel_findings = await self._read_sentinel_findings(
            client, prev_state.get("cycle_time")
        )
        sentinel_force_audit = any(
            f["type"] in _SENTINEL_AUDIT_TRIGGERS for f in sentinel_findings
        )
        for f in sentinel_findings:
            findings.append(f"Sentinel/{f['type']}: {f['summary']}")
            log(f"SENTINEL-COORD: read '{f['type']}' finding")

        # --- 3. Run tests (optional, ~15 min) ---
        total_passed = 0
        total_failed = 0
        if self.with_tests:
            loop = asyncio.get_event_loop()
            gov_future = loop.run_in_executor(None, run_pytest, project_root, "governance")
            # anima-mcp is optional — skip cleanly if the sibling repo isn't present
            anima_future = (
                loop.run_in_executor(None, run_pytest, ANIMA_PROJECT, "anima")
                if ANIMA_PROJECT.exists()
                else None
            )

            gov_passed, gov_n_passed, gov_n_failed, gov_summary = await gov_future
            findings.append(gov_summary)
            total_passed += gov_n_passed
            total_failed += gov_n_failed
            if not gov_passed:
                issues += 1

            if anima_future is not None:
                anima_passed, anima_n_passed, anima_n_failed, anima_summary = await anima_future
                findings.append(anima_summary)
                total_passed += anima_n_passed
                total_failed += anima_n_failed
                if not anima_passed:
                    issues += 1

        # --- 4. Groundskeeper duties (optional) ---
        # Forced on when a Sentinel finding indicates KG-remediable symptoms
        # (verdict churn, correlated governance events). Per-cycle override
        # only — does not mutate self.with_audit.
        effective_audit = self.with_audit or sentinel_force_audit
        groundskeeper_summary: Dict[str, Any] = {}
        if effective_audit:
            groundskeeper_summary = await self._run_groundskeeper(client, prev_state)
            if groundskeeper_summary.get("stale_found", 0) > 0:
                findings.append(
                    f"KG: {groundskeeper_summary['stale_found']} stale, "
                    f"{groundskeeper_summary['archived']} archived"
                )
            if sentinel_force_audit and not self.with_audit:
                findings.append("Groundskeeper forced by Sentinel coordination")

        # --- 4.5. KG hygiene v1: stale-opens propose-only sweep (optional) ---
        stale_opens = await self._run_stale_opens_sweep(client)
        if stale_opens:
            oldest = stale_opens[0]
            findings.append(
                f"hygiene: {len(stale_opens)} stale opens (oldest "
                f"{oldest.get('id', '?')[:12]}, "
                f"age={oldest.get('last_activity_days', 0)}d)"
            )
            for item in stale_opens[:5]:  # top 5 inline; full count in cycle state
                summary_short = (item.get("summary") or "")[:60]
                age_days = item.get("last_activity_days", 0)
                findings.append(
                    f"stale_open: {item.get('id', '?')[:12]} \"{summary_short}\" age={age_days}d"
                )

        # --- 4.5a. KG hygiene v2: act on aged candidate_for_archive (optional) ---
        # Bridges the audit→cleanup gap: cleanup_knowledge only walks the
        # lifecycle ladder (resolved→archived, etc.) and never touches open
        # entries, so the candidate_for_archive bucket grew unbounded.
        # Outer wait_for: per-entry timeouts (5s × max 20) plus audit (15s)
        # could in principle reach ~115s. Cap the whole step at 60s so the
        # cycle stays under CYCLE_TIMEOUT (120s) with margin for the steps
        # that follow.
        try:
            auto_archive = await asyncio.wait_for(
                self._run_aged_candidate_archive(client),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            log("auto-archive: 60s budget exceeded; partial results lost")
            auto_archive = {"archived": 0, "candidates_seen": 0, "errors": ["budget_exceeded"]}
        if auto_archive.get("archived", 0) > 0:
            findings.append(
                f"hygiene: auto-archived {auto_archive['archived']} aged "
                f"candidate(s) of {auto_archive.get('candidates_seen', 0)}"
            )

        # --- 4.6. Watcher calibration floor (24h gated) ---
        # Recompute pattern_floor.json from findings.jsonl so the surface
        # hook's demotion logic stays current. The function is gated on
        # 24h staleness internally, so calling it every cycle is cheap.
        try:
            loop = asyncio.get_running_loop()
            recomputed = await loop.run_in_executor(None, maybe_recompute_watcher_floor)
            if recomputed:
                findings.append("watcher_floor: recomputed")
        except Exception as e:
            log(f"watcher_floor recompute skipped: {e}")

        # --- 4.7. KG hygiene v1: retrieval-eval step (optional) ---
        eval_result = await self._run_eval_step()
        if eval_result.get("ran"):
            metrics = eval_result["metrics"]
            delta = eval_result.get("delta", {})
            baseline_name = eval_result.get("baseline") or "no-baseline"
            ndcg = metrics.get("nDCG@10", 0.0)
            ndcg_delta = delta.get("nDCG@10", 0.0)
            p95 = metrics.get("latency_p95", 0)
            p95_delta = delta.get("latency_p95", 0)
            findings.append(
                f"eval: nDCG@10 {ndcg:.3f} (Δ {ndcg_delta:+.3f} vs {baseline_name}), "
                f"p95 {p95:.0f}ms (Δ {p95_delta:+.0f}ms)"
            )
            if eval_result.get("regression"):
                findings.append("⚠ eval regression: nDCG@10 dropped beyond threshold")
                issues += 1

        # --- 5. Compute complexity/confidence from actual signals ---
        complexity = 0.15
        if self.with_tests:
            complexity += 0.3
        if effective_audit:
            complexity += 0.15
        if sentinel_findings:
            complexity += min(0.15, 0.05 * len(sentinel_findings))
        complexity += min(0.3, issues * 0.1)
        complexity = min(1.0, complexity)

        confidence = 0.90
        confidence -= issues * 0.12
        if lumen_down_streak == 1:
            confidence -= 0.05
        if total_failed > 0:
            confidence -= 0.10
        confidence = max(0.3, min(0.95, confidence))

        summary = " | ".join(findings)
        test_info = f" Tests: {total_passed} passed, {total_failed} failed." if self.with_tests else ""
        gk_info = ""
        if groundskeeper_summary.get("audit_run"):
            gk_info = (
                f" Groundskeeper: {groundskeeper_summary['stale_found']} stale, "
                f"{groundskeeper_summary['archived']} archived."
            )
        checkin_text = f"Heartbeat cycle: {summary}.{test_info}{gk_info} Issues: {issues}"

        # --- 6. Detect changes for notes ---
        # Build cycle state (pre-checkin; coherence/verdict filled in post-checkin)
        total_cycles = prev_state.get("total_cycles", 0) + 1

        # Preserve groundskeeper counts across non-audit cycles — otherwise
        # they reset to 0, then the next audit cycle compares against 0 and
        # dedupe fails (note re-emits even though stale/archived didn't move).
        if groundskeeper_summary.get("audit_run"):
            gk_stale = groundskeeper_summary.get("stale_found", 0)
            gk_archived = groundskeeper_summary.get("archived", 0)
        else:
            gk_stale = prev_state.get("groundskeeper_stale", 0)
            gk_archived = prev_state.get("groundskeeper_archived", 0)

        self._cycle_state = {
            **health_state,
            # Use null-safe indirection (gk_stale/gk_archived computed above)
            # rather than direct groundskeeper_summary.get() — preserves the
            # None-summary fallback path that the if/else block establishes.
            "groundskeeper_stale": gk_stale,
            "groundskeeper_archived": gk_archived,
            "hygiene_stale_opens": len(stale_opens),
            "eval_ndcg10": eval_result.get("metrics", {}).get("nDCG@10"),
            "eval_baseline": eval_result.get("baseline"),
            "eval_regression": eval_result.get("regression", False),
            "total_cycles": total_cycles,
            "cycle_time": datetime.now(timezone.utc).isoformat(),
        }

        # Change notes (health transitions, coherence drift, etc.)
        changes = detect_changes(prev_state, self._cycle_state)
        note_tuples = [(c["summary"], c["tags"]) for c in changes]

        return CycleResult(
            summary=checkin_text,
            complexity=complexity,
            confidence=confidence,
            response_mode="compact",
            notes=note_tuples,
        )

    async def on_verdict_pause(
        self, client, checkin_result, cycle_result,
    ) -> bool:
        """Attempt quick self-recovery on pause; return True to retry once."""
        log("Paused — attempting self-recovery")
        try:
            await client.self_recovery(action="quick")
            log("Self-recovery succeeded, retrying check-in")
            return True
        except Exception as retry_err:
            log(f"Self-recovery failed: {retry_err}")
            self.save_state(self._cycle_state)
            return False

    async def on_after_checkin(
        self, client, checkin_result, cycle_result,
    ) -> None:
        """Track coherence changes, persist state, log a one-line EISV summary.

        Receives the FINAL checkin_result (post-retry if on_verdict_pause
        requested a retry).
        """
        coherence = checkin_result.coherence
        verdict = checkin_result.verdict
        metrics = checkin_result.metrics or {}

        self._cycle_state["coherence"] = coherence
        self._cycle_state["verdict"] = verdict

        # Post any late-appearing notes (coherence/verdict changes)
        late_changes = detect_changes(self._cycle_prev_state, self._cycle_state)
        existing_summaries = {n[0] for n in (cycle_result.notes or [])}
        for change in late_changes:
            if change["summary"] not in existing_summaries:
                try:
                    await client.leave_note(
                        summary=change["summary"], tags=change["tags"]
                    )
                    log(f"NOTE: {change['summary']}")
                except Exception:
                    pass

        self.save_state(self._cycle_state)

        if checkin_result.success:
            try:
                eisv = (
                    f"E={float(metrics['E']):.3f} "
                    f"I={float(metrics['I']):.3f} "
                    f"S={float(metrics['S']):.3f} "
                    f"V={float(metrics['V']):.3f}"
                )
            except (KeyError, TypeError, ValueError):
                eisv = "EISV=?"
            total_cycles = self._cycle_state.get("total_cycles", 0)
            gov_up = self._cycle_state.get("gov_up_cycles", 0)
            lumen_up = self._cycle_state.get("lumen_up_cycles", 0)
            uptime = (
                f" | uptime: gov={gov_up/total_cycles:.0%} lumen={lumen_up/total_cycles:.0%}"
                if total_cycles > 0 else ""
            )
            log(f"{verdict or '?'} | {eisv} | {cycle_result.summary}{uptime}")

    async def run_daemon(self):
        """Run continuously with interval sleeps."""
        log(f"Heartbeat daemon starting (interval={self.heartbeat_interval}s)")
        await self.run_forever(
            interval=self.heartbeat_interval,
            heartbeat_interval=self.heartbeat_interval,
        )
        log("Heartbeat daemon stopped")


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Vigil — The First Resident")
    parser.add_argument("--once", action="store_true", default=True, help="Run one cycle (default)")
    parser.add_argument("--daemon", action="store_true", help="Run continuously")
    parser.add_argument("--with-tests", action="store_true", help="Also run pytest suites (~15 min)")
    parser.add_argument("--no-audit", action="store_true", help="Skip KG audit/groundskeeper duties")
    parser.add_argument("--force-new", action="store_true", help="Bootstrap fresh identity (use once, then remove flag)")
    parser.add_argument("--url", default=GOV_MCP_URL, help="MCP URL")
    parser.add_argument("--label", default="Vigil", help="Agent label")
    parser.add_argument("--interval", type=int, default=1800, help="Daemon interval (seconds)")
    args = parser.parse_args()

    # with_hygiene activates two stale-open behaviors (gated under one flag):
    #
    #   1. _run_stale_opens_sweep — propose-only; surfaces oldest stale opens
    #      in cycle findings. No mutation.
    #   2. _run_aged_candidate_archive — auto-archives candidate_for_archive
    #      entries past VIGIL_AUTO_ARCHIVE_AGE_DAYS (default 90 days inactive).
    #      Capped at VIGIL_AUTO_ARCHIVE_MAX_PER_CYCLE per run (default 20).
    #      Reversible (status mutation, not deletion).
    #
    # Activated 2026-05-05 after PR #352 cleared the existing 106-entry
    # backlog and shipped the symmetric _score_discovery classifier (a6e3f871).
    # The 2026-04-19 vigil-aggression incident concerned auto-archive of
    # *agents*; this flag governs *KG discoveries* only — orphan-agent sweep
    # remains operator-only.
    #
    # Intentionally NOT a CLI flag: changes that affect resident-agent
    # behavior should be code-reviewed in PRs, not silently flipped.
    agent = VigilAgent(
        mcp_url=args.url,
        label=args.label,
        heartbeat_interval=args.interval,
        with_tests=args.with_tests,
        with_audit=not args.no_audit,
        with_hygiene=True,
        force_new=args.force_new,
    )

    if args.daemon:
        await agent.run_daemon()
    else:
        try:
            await agent.run_once()
        except asyncio.TimeoutError:
            sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Interrupted")
        sys.exit(0)
