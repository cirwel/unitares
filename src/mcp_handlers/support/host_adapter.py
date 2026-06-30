"""Strong-heterogeneous inference host adapter — subscription-CLI models via the orchestrator.

This wires the `codex:host-adapter` / `claude:host-adapter` registry placeholders
(see ``inference_registry.py``) into a *working* path. It does NOT add a metered
model-API dependency (CLAUDE.md execution-cost policy): it drives the operator's
**subscription-auth CLIs** — ``codex exec`` (ChatGPT subscription, ``~/.codex/auth.json``)
and ``claude -p`` (Claude subscription) — the same free-execution posture as Claude
Code / Codex themselves.

Architecture (the load-bearing decision): strong models run for *minutes*, so they
are dispatched **asynchronously via the agent-orchestrator** (`POST /v1/agents` →
`POST /v1/agents/:id/await`), NOT through the synchronous 30s ``call_model`` tool.
This is the §5.6 lesson — strong-heterogeneous reasoners route via BEAM coordination,
never a blocking compute endpoint. The orchestrator owns lifecycle (kill_tree,
max_runtime); this module only builds the spec and relays the result.

Gated by ``UNITARES_HOST_ADAPTER_ENABLED`` (default OFF — deferred, opt-in). Every
failure mode degrades to a structured error; it never raises into a handler.
"""

from __future__ import annotations

import os
import shutil
from typing import Any, Dict, List, Optional

import logging

logger = logging.getLogger(__name__)

# host_id -> (cli binary for PATH check, sh -c template, model family). Subscription-auth CLIs only.
#
# Run via ``sh -c ... </dev/null``: the orchestrator keeps the child's stdin open
# as a pipe, and ``codex exec`` / ``claude -p`` block on "Reading additional input
# from stdin..." (then get max_runtime-killed) unless stdin is closed. The prompt
# is passed via the ``HA_PROMPT`` env var (quoted, never argv-interpolated) so it
# is injection-safe. ``exec`` replaces the shell so the orchestrator's kill_tree
# signals the CLI directly. Verified live 2026-06-30 (codex exit 0, real answer).
_HOST_COMMANDS = {
    "codex:host-adapter": (
        "codex",
        'exec codex exec --sandbox "$HA_SANDBOX" --skip-git-repo-check "$HA_PROMPT" </dev/null',
        "openai_codex",
    ),
    "claude:host-adapter": (
        "claude",
        'exec claude -p "$HA_PROMPT" </dev/null',
        "anthropic_claude",
    ),
}


def host_adapter_enabled() -> bool:
    """Opt-in flag. Default OFF — the strong-het path is deferred/opt-in per the cost policy."""
    return os.environ.get("UNITARES_HOST_ADAPTER_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _orchestrator_url() -> str:
    return os.environ.get("AGENT_ORCHESTRATOR_URL", "http://127.0.0.1:8789").rstrip("/")


def host_adapter_available(host_id: str) -> bool:
    """A host adapter is available only when: opt-in flag on, its CLI is on PATH,
    and a bearer token for the orchestrator is configured. Orchestrator reachability
    is checked at call time (fail-safe), not here, to keep this cheap for the registry."""
    if not host_adapter_enabled():
        return False
    spec = _HOST_COMMANDS.get(host_id)
    if spec is None:
        return False
    cli = spec[0]
    if shutil.which(cli) is None:
        return False
    return bool(os.environ.get("AGENT_ORCHESTRATOR_BEARER_TOKEN"))


def _extract_text(output_lines: List[str], *, family: str) -> str:
    """Best-effort: pull the model's answer out of the captured CLI stdout.

    ``codex exec`` prints warnings + a bare ``codex`` marker line, then the answer,
    then a ``tokens used`` footer. We return everything after the LAST ``codex``
    marker, trimming the token footer. ``claude -p`` prints the answer directly.
    The raw output is always preserved separately by the caller.
    """
    lines = [ln.rstrip("\n") for ln in output_lines]
    if family == "openai_codex":
        marker_idx = None
        for i, ln in enumerate(lines):
            if ln.strip() == "codex":
                marker_idx = i
        if marker_idx is not None:
            tail = lines[marker_idx + 1 :]
            # drop the trailing "tokens used / <n>" footer if present
            for j, ln in enumerate(tail):
                if ln.strip().lower() == "tokens used":
                    tail = tail[:j]
                    break
            return "\n".join(tail).strip()
    return "\n".join(lines).strip()


async def invoke_host_adapter(
    host_id: str,
    prompt: str,
    *,
    timeout_s: int = 240,
    sandbox: str = "read-only",
    cd: Optional[str] = None,
) -> Dict[str, Any]:
    """Invoke a strong-heterogeneous model host via the orchestrator. Async, fail-safe.

    Returns a dict:
      {ok: bool, host_id, text, raw, exit_status, agent_id, provenance, [error|status]}
    ``status="still_running"`` (with ``agent_id``) is returned on await-timeout so a
    caller can poll the orchestrator rather than block — strong models can exceed any
    single budget. Never raises.
    """
    spec_def = _HOST_COMMANDS.get(host_id)
    if spec_def is None:
        return {"ok": False, "host_id": host_id, "error": f"unknown host adapter '{host_id}'"}
    if not host_adapter_enabled():
        return {"ok": False, "host_id": host_id, "error": "host adapter disabled (UNITARES_HOST_ADAPTER_ENABLED unset)"}

    cli, shell_cmd, family = spec_def
    if shutil.which(cli) is None:
        return {"ok": False, "host_id": host_id, "error": f"CLI '{cli}' not on PATH"}
    bearer = os.environ.get("AGENT_ORCHESTRATOR_BEARER_TOKEN")
    if not bearer:
        return {"ok": False, "host_id": host_id, "error": "AGENT_ORCHESTRATOR_BEARER_TOKEN unset"}

    spec: Dict[str, Any] = {
        "cmd": "/bin/sh",
        "args": ["-c", shell_cmd],
        # Prompt via env (not argv) = injection-safe; orchestrator merges with inherited
        # env so the CLI keeps PATH/HOME and its subscription auth (~/.codex, ~/.claude).
        "env": {"HA_PROMPT": prompt, "HA_SANDBOX": sandbox},
        "lease": False,  # read-only advisor lane, no presence/lineage
        "max_runtime_ms": int(timeout_s * 1000) + 30_000,  # orchestrator backstop
    }
    if cd:
        spec["cd"] = cd

    base = _orchestrator_url()
    headers = {"Authorization": f"Bearer {bearer}"}
    provenance = {
        "transport": "host_adapter",
        "host_id": host_id,
        "model_family": family,
        "cost_class": "subscription_backed",
        "via": "agent_orchestrator",
    }
    try:
        import httpx

        async with httpx.AsyncClient(timeout=15.0) as client:
            sp = await client.post(f"{base}/v1/agents", json=spec, headers=headers)
        if sp.status_code not in (200, 201, 202):
            return {"ok": False, "host_id": host_id, "error": f"spawn {sp.status_code}: {sp.text[:200]}", "provenance": provenance}
        agent_id = (sp.json() or {}).get("agent_id")
        if not agent_id:
            return {"ok": False, "host_id": host_id, "error": "spawn returned no agent_id", "provenance": provenance}

        async with httpx.AsyncClient(timeout=timeout_s + 15.0) as client:
            aw = await client.post(
                f"{base}/v1/agents/{agent_id}/await",
                json={"timeout_ms": int(timeout_s * 1000)},
                headers=headers,
            )
        if aw.status_code == 504:
            return {"ok": False, "host_id": host_id, "status": "still_running", "agent_id": agent_id,
                    "hint": f"poll {base}/v1/agents/{agent_id}/await", "provenance": provenance}
        if aw.status_code != 200:
            return {"ok": False, "host_id": host_id, "error": f"await {aw.status_code}: {aw.text[:200]}",
                    "agent_id": agent_id, "provenance": provenance}

        result = (aw.json() or {}).get("result") or {}
        output = result.get("output") or []
        if isinstance(output, str):
            output = output.splitlines()
        exit_status = result.get("exit_status")
        text = _extract_text(output, family=family)
        return {
            "ok": exit_status == 0,
            "host_id": host_id,
            "text": text,
            "raw": "\n".join(output),
            "exit_status": exit_status,
            "agent_id": agent_id,
            "provenance": provenance,
        }
    except Exception as exc:  # noqa: BLE001 — any failure degrades to a structured error
        logger.warning("[host_adapter] %s invocation failed: %r", host_id, exc)
        return {"ok": False, "host_id": host_id, "error": f"orchestrator dispatch failed: {exc!r}", "provenance": provenance}
