"""Strong-heterogeneous inference host adapter — subscription-CLI models via the orchestrator.

This wires the `codex:host-adapter` / `claude:host-adapter` registry placeholders
(see ``inference_registry.py``) into a *working* path. It does NOT add a metered
model-API dependency (CLAUDE.md execution-cost policy): it drives the operator's
**subscription-auth CLIs** — Codex app-server with a ``codex exec`` compatibility
fallback (ChatGPT subscription, ``~/.codex/auth.json``), and ``claude -p`` (Claude
subscription). Provider-reported usage and cost metadata are preserved when the
CLI exposes them; subscription-backed does not mean zero-cost.

Architecture (the load-bearing decision): strong models run for *minutes*, so they
are dispatched **asynchronously via the agent-orchestrator** (`POST /v1/agents` →
`POST /v1/executions/:id/await`), NOT through the synchronous 30s ``call_model`` tool.
This is the §5.6 lesson — strong-heterogeneous reasoners route via BEAM coordination,
never a blocking compute endpoint. The orchestrator owns lifecycle (kill_tree,
max_runtime); this module only builds the spec and relays the result.

Gated by ``UNITARES_HOST_ADAPTER_ENABLED`` (default OFF — deferred, opt-in). Every
failure mode degrades to a structured error; it never raises into a handler.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import logging

logger = logging.getLogger(__name__)


_CODEX_EXEC_COMMAND = (
    'if [ -n "$HA_MODEL" ]; then '
    'exec "$HA_CLI" exec --ignore-user-config --ephemeral --json '
    '--sandbox "$HA_SANDBOX" --skip-git-repo-check '
    '-m "$HA_MODEL" "$HA_PROMPT" </dev/null; '
    'else exec "$HA_CLI" exec --ignore-user-config --ephemeral --json '
    '--sandbox "$HA_SANDBOX" --skip-git-repo-check '
    '"$HA_PROMPT" </dev/null; fi'
)

_CODEX_INSTRUMENTED_COMMAND = (
    'if [ "$HA_CODEX_APP_SERVER" = "1" ]; then '
    '"$HA_PYTHON" "$HA_CODEX_APP_SERVER_CLIENT"; app_server_status=$?; '
    'if [ "$app_server_status" -ne 75 ]; then exit "$app_server_status"; fi; '
    'fi; '
    f"{_CODEX_EXEC_COMMAND}"
)

# host_id -> (CLI binary, sh -c template, model family). Subscription-auth CLIs only.
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
        # App-server reports the selected model and explicit service reroutes.
        # The one-shot client exits 75 only before a turn request is sent, which
        # is the sole point where retrying through exec JSONL cannot duplicate
        # inference. The fallback keeps older Codex CLIs usable and retains the
        # typed agent_message boundary from the original adapter.
        _CODEX_INSTRUMENTED_COMMAND,
        "openai_codex",
    ),
    "claude:host-adapter": (
        "claude",
        (
            'if [ -n "$HA_MODEL" ]; then '
            'exec "$HA_CLI" --safe-mode -p "$HA_PROMPT" --tools "" '
            '--no-session-persistence --output-format json --model "$HA_MODEL" </dev/null; '
            'else exec "$HA_CLI" --safe-mode -p "$HA_PROMPT" --tools "" '
            '--no-session-persistence --output-format json </dev/null; fi'
        ),
        "anthropic_claude",
    ),
}

_CLI_ENV_OVERRIDES = {
    "codex:host-adapter": "UNITARES_CODEX_CLI",
    "claude:host-adapter": "UNITARES_CLAUDE_CLI",
}

_TERMINAL_ANSWER_SCHEMA = "unitares.terminal_answer.v1"
_CODEX_APP_SERVER_RESULT_SCHEMA = "unitares.codex_app_server_result.v1"
_TERMINAL_ANSWER_STATUSES = frozenset({"complete", "needs_input", "declined"})
_TERMINAL_ANSWER_CONTRACT = f"""
Return exactly one JSON object and no Markdown fence or surrounding text:
{{"schema":"{_TERMINAL_ANSWER_SCHEMA}","status":"complete","answer":"your final answer"}}
Use status "complete" only when answer is the final answer to the request. Use
"needs_input" when required information is missing, and "declined" when you
cannot answer. A plan, progress report, promise of future work, or description
of what you would do is not a complete answer. The object must contain exactly
the keys schema, status, and answer; answer must be a string.
""".strip()


def host_cli_env_var(host_id: str) -> Optional[str]:
    """The operator variable that pins this host's CLI, for recovery text.

    Exists so callers can name the right knob without reaching into this
    module's tables: a recovery action that says UNITARES_CLAUDE_CLI to
    somebody whose Codex call failed sends them to configure the wrong thing.
    """
    return _CLI_ENV_OVERRIDES.get(host_id)


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _configured_cli_override(host_id: str) -> str:
    """Return the operator-pinned CLI path for a known adapter, if any."""
    if host_id == "claude:host-adapter":
        return os.environ.get("UNITARES_CLAUDE_CLI", "").strip()
    if host_id == "codex:host-adapter":
        return os.environ.get("UNITARES_CODEX_CLI", "").strip()
    return ""


def resolve_host_cli(host_id: str) -> Optional[str]:
    """Resolve a host CLI even when a launchd service has a sparse ``PATH``.

    Operator overrides win, then the inherited PATH, then conservative
    per-user/Homebrew locations. Only executable files are returned. The
    resolved absolute path is passed to the orchestrator child explicitly, so
    availability probing and execution cannot disagree because their PATHs do.
    """
    spec = _HOST_COMMANDS.get(host_id)
    if spec is None:
        return None

    override = _configured_cli_override(host_id)
    if override:
        expanded = os.path.abspath(os.path.expanduser(override))
        return expanded if _is_executable(expanded) else None

    cli = spec[0]
    discovered = shutil.which(cli)
    if discovered:
        return os.path.abspath(discovered)

    candidates = [
        Path.home() / ".local" / "bin" / cli,
        Path("/opt/homebrew/bin") / cli,
        Path("/usr/local/bin") / cli,
    ]
    for candidate in candidates:
        candidate_text = str(candidate)
        if _is_executable(candidate_text):
            return candidate_text
    return None


def _current_username() -> str:
    """The username to hand a subscription CLI, never empty if it can be helped.

    `getpass.getuser()` reads the environment first and falls back to a uid
    lookup in the password database, which is what makes this useful under a
    service manager that strips USER. Its failure mode is an exception, not a
    wrong answer, so an empty string is the honest last resort.
    """
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - no uid entry is pathological
        return os.environ.get("USER", "")


def host_adapter_enabled() -> bool:
    """Opt-in flag. Default OFF — the strong-het path is deferred/opt-in per the cost policy."""
    return os.environ.get("UNITARES_HOST_ADAPTER_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def codex_app_server_instrumentation_enabled() -> bool:
    """Whether Codex consults use model-aware app-server before exec fallback."""
    return os.environ.get(
        "UNITARES_CODEX_APP_SERVER_INSTRUMENTATION", "1"
    ).strip().lower() not in ("0", "false", "no", "off")


def _orchestrator_url() -> str:
    return os.environ.get("AGENT_ORCHESTRATOR_URL", "http://127.0.0.1:8789").rstrip("/")


def host_adapter_available(host_id: str) -> bool:
    """A host adapter is available only when: opt-in flag on, its CLI resolves,
    and a bearer token for the orchestrator is configured. Orchestrator reachability
    is checked at call time (fail-safe), not here, to keep this cheap for the registry."""
    if not host_adapter_enabled():
        return False
    spec = _HOST_COMMANDS.get(host_id)
    if spec is None:
        return False
    if resolve_host_cli(host_id) is None:
        return False
    return bool(os.environ.get("AGENT_ORCHESTRATOR_BEARER_TOKEN"))


# The bare marker line ``codex exec`` prints immediately before the assistant
# turn. Everything above the LAST one is banner, echoed prompt, and exec traces.
_CODEX_ANSWER_MARKER = "codex"


def codex_answer_region_located(raw: str) -> bool:
    """True when Codex output carries a typed or legacy answer region.

    Current adapters consume the JSONL ``agent_message`` event. The legacy bare
    marker remains recognized so failures from older deployed CLIs stay
    diagnosable during rollout.
    """
    lines = raw.splitlines()
    app_server_answer = any(
        payload.get("schema") == _CODEX_APP_SERVER_RESULT_SCHEMA
        and isinstance(payload.get("text"), str)
        and bool(payload["text"].strip())
        for payload in _codex_jsonl_payloads(lines)
    )
    return app_server_answer or bool(_codex_agent_messages(lines)) or any(
        line.strip() == _CODEX_ANSWER_MARKER for line in lines
    )


def _codex_jsonl_payloads(output_lines: List[str]) -> List[Dict[str, Any]]:
    """Parse only complete JSONL event lines, ignoring stderr diagnostics."""
    payloads: List[Dict[str, Any]] = []
    for line in output_lines:
        try:
            value = json.loads(line.strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            payloads.append(value)
    return payloads


def _codex_agent_messages(output_lines: List[str]) -> List[str]:
    messages: List[str] = []
    for payload in _codex_jsonl_payloads(output_lines):
        if payload.get("type") != "item.completed":
            continue
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            messages.append(text)
    return messages


def _extract_codex_app_server_result(
    payload: Dict[str, Any],
) -> tuple[str, Dict[str, Any]]:
    """Normalize the one-shot app-server client's richer result envelope."""
    text = payload.get("text")
    if not isinstance(text, str):
        text = ""
    warnings = payload.get("warnings")
    if not isinstance(warnings, list):
        warnings = []
    warnings = [value for value in warnings if isinstance(value, str)]
    models_used = payload.get("models_used")
    if not isinstance(models_used, list):
        models_used = []
    models_used = [value for value in models_used if isinstance(value, str)]
    usage = payload.get("provider_usage")
    if not isinstance(usage, dict):
        usage = {}
    reroutes = payload.get("model_reroutes")
    if not isinstance(reroutes, list):
        reroutes = []
    errors = payload.get("errors")
    if not isinstance(errors, list):
        errors = []

    return text, {
        "model_used": payload.get("model_used"),
        "models_used": models_used,
        "model_selected": payload.get("model_selected"),
        "model_effective": payload.get("model_effective"),
        "model_provider": payload.get("model_provider"),
        "service_tier": payload.get("service_tier"),
        "model_reroutes": reroutes,
        "model_reporting_status": payload.get("model_reporting_status"),
        "provider_usage": usage,
        "tokens_used": payload.get("tokens_used", 0),
        "finish_reason": payload.get("finish_reason"),
        "provider_thread_id": payload.get("thread_id"),
        "provider_turn_id": payload.get("turn_id"),
        "provider_user_agent": payload.get("provider_user_agent"),
        "codex_cli_version": payload.get("codex_cli_version"),
        "provider_errors": [value for value in errors if isinstance(value, str)],
        "codex_transport": "app_server",
        "warnings": warnings,
    }


def _extract_codex_jsonl(output_lines: List[str]) -> tuple[str, Dict[str, Any]]:
    """Extract the final Codex answer and usage from documented JSONL events."""
    payloads = _codex_jsonl_payloads(output_lines)
    for payload in reversed(payloads):
        if payload.get("schema") == _CODEX_APP_SERVER_RESULT_SCHEMA:
            return _extract_codex_app_server_result(payload)

    messages = _codex_agent_messages(output_lines)
    usage: Dict[str, Any] = {}
    completed = False
    for payload in payloads:
        if payload.get("type") == "turn.completed":
            completed = True
            candidate = payload.get("usage")
            if isinstance(candidate, dict):
                usage = candidate

    if messages:
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        tokens_used = sum(
            value for value in (input_tokens, output_tokens) if isinstance(value, int)
        )
        return messages[-1], {
            "model_used": None,
            "models_used": [],
            "model_reporting_status": "unavailable_from_exec_jsonl",
            "provider_usage": usage,
            "tokens_used": tokens_used,
            "finish_reason": "completed" if completed else None,
            "codex_transport": "exec_jsonl",
            "warnings": ["CLI did not report an exact model identifier"],
        }

    # Compatibility for a pre-JSONL transcript during a rolling deployment.
    return _extract_text(output_lines, family="openai_codex"), {
        "model_used": None,
        "models_used": [],
        "model_reporting_status": "unavailable_from_legacy_transcript",
        "codex_transport": "legacy_transcript",
        "warnings": [
            "Codex CLI returned no typed agent_message event; used legacy transcript parsing",
            "CLI did not report an exact model identifier",
        ],
    }


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
            if ln.strip() == _CODEX_ANSWER_MARKER:
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


def _parse_json_output(raw: str) -> Optional[Dict[str, Any]]:
    """Parse a CLI JSON envelope, tolerating warning lines around it."""
    candidates = [raw.strip(), *(line.strip() for line in reversed(raw.splitlines()))]
    for candidate in candidates:
        if not candidate or not candidate.startswith("{"):
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _terminal_answer_prompt(prompt: str) -> str:
    """Add the response contract without interpolating the prompt into a shell."""
    return f"{prompt.rstrip()}\n\n--- UNITARES response contract ---\n{_TERMINAL_ANSWER_CONTRACT}"


def _validate_terminal_answer(text: str) -> tuple[str, Dict[str, str], Optional[str]]:
    """Validate and unwrap the model-authored terminal-answer envelope.

    Process exit and provider completion only establish that the CLI stopped.
    They do not establish that the model supplied a final answer. This parser is
    deliberately stricter than the provider-envelope parser above: warning text,
    Markdown fences, extra keys, and guessed defaults all fail closed.
    """
    try:
        payload = json.loads(text.strip())
    except (json.JSONDecodeError, TypeError):
        return "", {
            "schema": _TERMINAL_ANSWER_SCHEMA,
            "status": "malformed",
        }, "Host CLI returned a malformed terminal answer envelope"

    if not isinstance(payload, dict) or set(payload) != {"schema", "status", "answer"}:
        return "", {
            "schema": _TERMINAL_ANSWER_SCHEMA,
            "status": "malformed",
        }, "Host CLI returned an invalid terminal answer envelope"

    schema = payload.get("schema")
    status = payload.get("status")
    answer = payload.get("answer")
    if (
        schema != _TERMINAL_ANSWER_SCHEMA
        or status not in _TERMINAL_ANSWER_STATUSES
        or not isinstance(answer, str)
    ):
        return "", {
            "schema": _TERMINAL_ANSWER_SCHEMA,
            "status": "malformed",
        }, "Host CLI returned an invalid terminal answer envelope"

    terminal_answer = {"schema": schema, "status": status}
    if status != "complete":
        return "", terminal_answer, f"Host CLI returned nonterminal answer status '{status}'"
    if not answer.strip():
        return "", terminal_answer, "Host CLI returned an empty complete answer"
    return answer.strip(), terminal_answer, None


def extract_cli_result(
    output_lines: List[str],
    *,
    family: str,
) -> tuple[str, Dict[str, Any]]:
    """Return answer text plus exact provider metadata when available."""
    raw = "\n".join(line.rstrip("\n") for line in output_lines)
    if family == "openai_codex":
        return _extract_codex_jsonl(output_lines)
    if family != "anthropic_claude":
        return _extract_text(output_lines, family=family), {"warnings": []}

    payload = _parse_json_output(raw)
    if payload is None:
        return _extract_text(output_lines, family=family), {
            "models_used": [],
            "warnings": ["Claude CLI output was not a parseable JSON envelope"],
        }

    result = payload.get("result")
    text = result if isinstance(result, str) else ""
    model_usage = payload.get("modelUsage")
    if not isinstance(model_usage, dict):
        model_usage = {}
    models_used = sorted(str(model_id) for model_id in model_usage)
    warnings: List[str] = []
    if not models_used:
        warnings.append("Claude CLI did not report an exact model identifier")
    elif len(models_used) > 1:
        warnings.append(
            "Claude CLI reported multiple models; model_used is intentionally unset"
        )

    usage = payload.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    token_fields = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    token_values = [usage.get(field) for field in token_fields]
    tokens_used = sum(value for value in token_values if isinstance(value, int))

    return text, {
        "model_used": models_used[0] if len(models_used) == 1 else None,
        "models_used": models_used,
        "provider_usage": usage,
        "provider_model_usage": model_usage,
        "tokens_used": tokens_used,
        "cost_usd": payload.get("total_cost_usd"),
        "finish_reason": payload.get("subtype"),
        "duration_api_ms": payload.get("duration_api_ms"),
        "provider_is_error": payload.get("is_error") is True,
        "warnings": warnings,
    }


# Backwards-compatible private spelling retained for tests/internal callers that
# predate the dialectic backend sharing this parser.
_extract_cli_result = extract_cli_result


async def invoke_host_adapter(
    host_id: str,
    prompt: str,
    *,
    timeout_s: int = 240,
    sandbox: str = "read-only",
    cd: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Invoke a strong-heterogeneous model host via the orchestrator. Async, fail-safe.

    Returns a dict:
      {ok: bool, host_id, text, raw, exit_status, execution_id, agent_id, provenance, [error|status]}
    ``status="still_running"`` (with ``execution_id``) is returned on await-timeout so a
    caller can poll the orchestrator rather than block — strong models can exceed any
    single budget. Never raises.
    """
    # ``dispatch_phase`` is a safety signal consumed by delegated inference.
    # Only ``preflight`` and an explicit HTTP ``spawn_rejected`` response prove
    # that no child can be running. Once the spawn POST begins, a lost response
    # is the classic distributed-systems lost-ack case: the orchestrator may
    # have accepted the child even when this client never receives its id.
    dispatch_phase = "preflight"
    spec_def = _HOST_COMMANDS.get(host_id)
    if spec_def is None:
        return {
            "ok": False,
            "host_id": host_id,
            "dispatch_phase": dispatch_phase,
            "error": f"unknown host adapter '{host_id}'",
        }
    if not host_adapter_enabled():
        return {
            "ok": False,
            "host_id": host_id,
            "dispatch_phase": dispatch_phase,
            "error": "host adapter disabled (UNITARES_HOST_ADAPTER_ENABLED unset)",
        }

    cli, shell_cmd, family = spec_def
    cli_path = resolve_host_cli(host_id)
    if cli_path is None:
        override = _CLI_ENV_OVERRIDES[host_id]
        return {
            "ok": False,
            "host_id": host_id,
            "dispatch_phase": dispatch_phase,
            "error": f"CLI '{cli}' not found or not executable; set {override}",
        }
    bearer = os.environ.get("AGENT_ORCHESTRATOR_BEARER_TOKEN")
    if not bearer:
        return {
            "ok": False,
            "host_id": host_id,
            "dispatch_phase": dispatch_phase,
            "error": "AGENT_ORCHESTRATOR_BEARER_TOKEN unset",
        }

    spec: Dict[str, Any] = {
        "cmd": "/bin/sh",
        "args": ["-c", shell_cmd],
        # Prompt via env (not argv) = injection-safe; orchestrator merges with inherited
        # env so the CLI keeps PATH/HOME and its subscription auth (~/.codex, ~/.claude).
        #
        # USER is passed explicitly because inheriting it cannot be relied on. A
        # service manager may hand the orchestrator a minimal environment — this
        # deployment's launchd job gets HOME, LANG, PATH, SSH_AUTH_SOCK and
        # nothing else — and a CLI whose credential lookup keys on the username
        # then finds nothing. `claude -p` fails that way, reporting the flatly
        # misleading "Not logged in · Please run /login" while HOME is correct and
        # the credentials are present; `codex exec` is unaffected because it
        # resolves ~/.codex/auth.json from HOME alone. Reproduce the failure with
        # `env -i HOME=$HOME PATH=/usr/bin:/bin <cli> ...` and watch adding USER
        # alone fix it. getpass falls back to a uid lookup, so this stays correct
        # when the variable is absent from our own environment too.
        "env": {
            "HA_CLI": cli_path,
            "HA_PROMPT": _terminal_answer_prompt(prompt),
            "HA_SANDBOX": sandbox,
            "HA_MODEL": model or "",
            "HA_CODEX_APP_SERVER": (
                "1"
                if host_id == "codex:host-adapter"
                and codex_app_server_instrumentation_enabled()
                else "0"
            ),
            "HA_CODEX_APP_SERVER_CLIENT": str(
                Path(__file__).with_name("codex_app_server_client.py")
            ),
            "HA_PYTHON": sys.executable,
            "USER": _current_username(),
            # Neutralise console-API credentials so this stays a SUBSCRIPTION
            # lane by construction. The orchestrator merges this map over its
            # own inherited environment, and that environment is whatever the
            # shell that bootstrapped the launchd job happened to export --
            # `.zshrc` exports ANTHROPIC_API_KEY here, so the job inherited it
            # at bootstrap even though the plist never mentions it. That makes
            # the leak invisible to `launchctl print`, whose `environment`
            # block shows only plist-declared vars.
            #
            # The consequence was not a crash but a silent billing switch: the
            # CLI preferred the key, spent metered API credit instead of the
            # operator subscription, and once that balance ran out the lane
            # started returning HTTP 400 "Credit balance is too low" as a bare
            # exit 1. That reads exactly like the USER/"not logged in" bug this
            # env map already fixes, which is how it stayed hidden behind it.
            #
            # Empty string rather than a true unset: the orchestrator's
            # /v1/agents contract validates env as string => string, so `false`
            # (the Erlang "remove this variable" form) is rejected before it
            # reaches Port.open. Verified live -- blanking these two turns the
            # lane's exit 1 into exit 0 with a subscription-billed result.
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_AUTH_TOKEN": "",
            "OPENAI_API_KEY": "",
        },
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
        "model_requested": model,
        "model_selection_source": "caller" if model else "cli_default",
    }
    execution_id: Optional[str] = None
    try:
        import httpx

        started = time.monotonic()

        dispatch_phase = "spawn_request_started"
        async with httpx.AsyncClient(timeout=15.0) as client:
            sp = await client.post(f"{base}/v1/agents", json=spec, headers=headers)
        if sp.status_code not in (200, 201, 202):
            return {
                "ok": False,
                "host_id": host_id,
                "dispatch_phase": "spawn_rejected",
                "error": f"spawn {sp.status_code}: {sp.text[:200]}",
                "provenance": provenance,
            }
        dispatch_phase = "spawn_acknowledged"
        spawn_payload = sp.json() or {}
        execution_id = spawn_payload.get("execution_id") or spawn_payload.get("agent_id")
        if not execution_id:
            return {
                "ok": False,
                "host_id": host_id,
                "dispatch_phase": dispatch_phase,
                "error": "spawn returned no execution_id or legacy agent_id",
                "provenance": provenance,
            }

        dispatch_phase = "spawned"
        async with httpx.AsyncClient(timeout=timeout_s + 15.0) as client:
            aw = await client.post(
                f"{base}/v1/executions/{execution_id}/await",
                json={"timeout_ms": int(timeout_s * 1000)},
                headers=headers,
            )
        if aw.status_code == 504:
            return {
                "ok": False,
                "host_id": host_id,
                "dispatch_phase": "await_timeout",
                "status": "still_running",
                "execution_id": execution_id,
                "agent_id": execution_id,
                "hint": f"poll {base}/v1/executions/{execution_id}/await",
                "provenance": provenance,
            }
        if aw.status_code != 200:
            return {
                "ok": False,
                "host_id": host_id,
                "dispatch_phase": "await_failed",
                "error": f"await {aw.status_code}: {aw.text[:200]}",
                "execution_id": execution_id,
                "agent_id": execution_id,
                "provenance": provenance,
            }

        result = (aw.json() or {}).get("result") or {}
        output = result.get("output") or []
        if isinstance(output, str):
            output = output.splitlines()
        exit_status = result.get("exit_status")
        provider_text, provider_metadata = extract_cli_result(output, family=family)
        text, terminal_answer, terminal_answer_error = _validate_terminal_answer(
            provider_text
        )
        provenance.update(provider_metadata)
        provenance["terminal_answer"] = terminal_answer
        provenance["latency_ms"] = int((time.monotonic() - started) * 1000)
        adapter_ok = exit_status == 0
        adapter_error = None
        if family == "anthropic_claude":
            if provider_metadata.get("provider_is_error"):
                adapter_ok = False
                adapter_error = "Claude CLI reported an error result"
        elif family == "openai_codex":
            provider_errors = provider_metadata.get("provider_errors")
            if isinstance(provider_errors, list) and provider_errors:
                adapter_ok = False
                adapter_error = f"Codex app-server reported: {provider_errors[-1]}"
        if adapter_ok and terminal_answer_error:
            adapter_ok = False
            adapter_error = terminal_answer_error
        return {
            "ok": adapter_ok,
            "host_id": host_id,
            "dispatch_phase": "terminal",
            "status": terminal_answer["status"],
            "text": text,
            "raw": "\n".join(output),
            "exit_status": exit_status,
            "execution_id": execution_id,
            "agent_id": execution_id,
            "provenance": provenance,
            **({"error": adapter_error} if adapter_error else {}),
        }
    except Exception as exc:  # noqa: BLE001 — any failure degrades to a structured error
        logger.warning("[host_adapter] %s invocation failed: %r", host_id, exc)
        return {
            "ok": False,
            "host_id": host_id,
            "dispatch_phase": dispatch_phase,
            "error": f"orchestrator dispatch failed: {exc!r}",
            "provenance": provenance,
            **(
                {"execution_id": execution_id, "agent_id": execution_id}
                if execution_id
                else {}
            ),
        }
