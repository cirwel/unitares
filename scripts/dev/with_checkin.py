#!/usr/bin/env python3
"""Run a command and emit a best-effort UNITARES check-in.

This is intentionally opt-in. It is for meaningful workflow boundaries such as
tests, diagnostics, commits, and pushes; it does not install hooks or turn every
tool call into governance noise.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_URL = "http://127.0.0.1:8767"
DEFAULT_TOOL_SURFACE = ["terminal", "mcp:unitares"]
WORKFLOWS = ("auto", "test", "commit", "push", "diagnostic", "command")


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    exit_code: int
    duration_sec: float
    output_tail: list[str]


@dataclass(frozen=True)
class CheckinContext:
    workflow: str
    url: str = DEFAULT_URL
    session: str | None = None
    agent_id: str | None = None
    client_session_id: str | None = None
    task_type: str | None = None
    complexity: float | None = None
    confidence: float | None = None
    harness_type: str | None = "codex-cli"
    harness_id: str | None = None
    model_provider: str | None = "openai"
    model: str | None = "gpt-5.5"
    transport: str | None = "terminal"
    memory_context: str | None = None
    tool_surface: list[str] | None = None
    governance_mode: str | None = "explicit"
    verification_source: str | None = "agent_reported_tool_result"
    comparison_key: str | None = None
    task_label: str | None = None
    task_outcome: str | None = None
    episode_id: str | None = None
    invocation_id: str | None = None


def env_default(name: str, fallback: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value else fallback


def parse_tool_surface(values: list[str] | None, env_value: str | None = None) -> list[str]:
    raw_values = values if values is not None else ([env_value] if env_value else [])
    parsed: list[str] = []
    for value in raw_values:
        for item in value.split(","):
            item = item.strip()
            if item:
                parsed.append(item)
    return parsed or list(DEFAULT_TOOL_SURFACE)


def strip_command_separator(command: list[str]) -> list[str]:
    if command and command[0] == "--":
        return command[1:]
    return command


def command_text(argv: list[str]) -> str:
    try:
        return shlex.join(argv)
    except Exception:
        return " ".join(argv)


def truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return f"{text[: max_len - 3]}..."


def infer_workflow(argv: list[str]) -> str:
    if not argv:
        return "command"

    first = Path(argv[0]).name.lower()
    lowered = [part.lower() for part in argv]
    joined = " ".join(lowered)

    if "scripts/diagnostics/" in joined or "/diagnostics/" in joined:
        return "diagnostic"
    if first == "git" and len(lowered) > 1:
        if lowered[1] == "commit":
            return "commit"
        if lowered[1] == "push":
            return "push"
    if first in {"pytest", "tox"}:
        return "test"
    if first == "test-cache.sh":
        return "test"
    if first in {"python", "python3"} and len(lowered) > 2:
        if lowered[1] == "-m" and lowered[2] in {"pytest", "unittest"}:
            return "test"
    if first == "npm" and len(lowered) > 1 and lowered[1] in {"test", "t"}:
        return "test"
    if first == "make" and any(part in {"test", "smoke", "test-smoke"} for part in lowered[1:]):
        return "test"
    if first == "mix" and len(lowered) > 1 and lowered[1] == "test":
        return "test"
    return "command"


def infer_task_type(workflow: str) -> str:
    if workflow in {"test", "diagnostic"}:
        return "testing"
    if workflow == "push":
        return "deployment"
    return "mixed"


def infer_evidence_kind(workflow: str, argv: list[str]) -> str:
    if workflow in {"test", "diagnostic"}:
        return "test"
    if workflow == "commit":
        return "file_op"
    first = Path(argv[0]).name.lower() if argv else ""
    if first == "make" and any(part == "build" for part in argv[1:]):
        return "build"
    return "command"


def tool_name(argv: list[str]) -> str:
    if not argv:
        return "command"
    first = Path(argv[0]).name
    lowered = first.lower()
    if lowered in {"python", "python3"} and len(argv) >= 3 and argv[1] == "-m":
        return truncate(f"{first} -m {argv[2]}", 64)
    if lowered in {"git", "npm", "mix", "make"} and len(argv) >= 2:
        return truncate(f"{first} {argv[1]}", 64)
    return truncate(first, 64)


def default_complexity(workflow: str) -> float:
    if workflow in {"test", "diagnostic"}:
        return 0.35
    if workflow in {"commit", "push"}:
        return 0.25
    return 0.2


def default_confidence(exit_code: int) -> float:
    return 0.82 if exit_code == 0 else 0.42


def output_tail_text(lines: list[str], max_chars: int = 3500) -> str:
    if not lines:
        return "(no output captured)"
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    return f"...{text[-(max_chars - 3):]}"


def build_evidence_summary(result: CommandResult, workflow: str) -> str:
    outcome = "succeeded" if result.exit_code == 0 else "failed"
    summary = (
        f"{workflow} command {outcome}: {command_text(result.argv)} "
        f"(exit {result.exit_code}, {result.duration_sec:.2f}s)"
    )
    return truncate(summary, 512)


def build_response_text(result: CommandResult, workflow: str) -> str:
    return (
        f"with_checkin ran {workflow} workflow.\n"
        f"command: {command_text(result.argv)}\n"
        f"exit_code: {result.exit_code}\n"
        f"duration_sec: {result.duration_sec:.2f}\n"
        f"output_tail:\n{output_tail_text(result.output_tail)}"
    )


def put_if_present(payload: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        payload[key] = value


def build_checkin_payload(result: CommandResult, context: CheckinContext) -> dict[str, Any]:
    workflow = context.workflow
    payload: dict[str, Any] = {
        "response_text": build_response_text(result, workflow),
        "complexity": context.complexity
        if context.complexity is not None
        else default_complexity(workflow),
        "confidence": context.confidence
        if context.confidence is not None
        else default_confidence(result.exit_code),
        "task_type": context.task_type or infer_task_type(workflow),
        "response_mode": "compact",
        "recent_tool_results": [
            {
                "kind": infer_evidence_kind(workflow, result.argv),
                "tool": tool_name(result.argv),
                "summary": build_evidence_summary(result, workflow),
                "exit_code": result.exit_code,
                "is_bad": result.exit_code != 0,
            }
        ],
    }

    put_if_present(payload, "agent_id", context.agent_id)
    put_if_present(payload, "client_session_id", context.client_session_id)
    put_if_present(payload, "harness_type", context.harness_type)
    put_if_present(payload, "harness_id", context.harness_id)
    put_if_present(payload, "model_provider", context.model_provider)
    put_if_present(payload, "model", context.model)
    put_if_present(payload, "transport", context.transport)
    put_if_present(payload, "memory_context", context.memory_context)
    put_if_present(payload, "tool_surface", context.tool_surface or list(DEFAULT_TOOL_SURFACE))
    put_if_present(payload, "governance_mode", context.governance_mode)
    put_if_present(payload, "verification_source", context.verification_source)
    put_if_present(payload, "comparison_key", context.comparison_key)
    put_if_present(payload, "task_label", context.task_label)

    task_outcome = context.task_outcome
    if task_outcome is None and context.comparison_key:
        task_outcome = "succeeded" if result.exit_code == 0 else "failed"
    put_if_present(payload, "task_outcome", task_outcome)

    put_if_present(payload, "episode_id", context.episode_id)
    put_if_present(payload, "invocation_id", context.invocation_id)
    return payload


def run_command(argv: list[str], max_tail_lines: int = 80) -> CommandResult:
    started = time.monotonic()
    tail: deque[str] = deque(maxlen=max(0, max_tail_lines))

    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError:
        message = f"with_checkin: command not found: {argv[0]}"
        print(message, file=sys.stderr)
        return CommandResult(argv, 127, time.monotonic() - started, [message])
    except OSError as exc:
        message = f"with_checkin: failed to start command: {exc}"
        print(message, file=sys.stderr)
        return CommandResult(argv, 126, time.monotonic() - started, [message])

    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        tail.append(line.rstrip("\n"))

    exit_code = proc.wait()
    return CommandResult(argv, exit_code, time.monotonic() - started, list(tail))


def call_process_agent_update(
    base_url: str,
    payload: dict[str, Any],
    session_id: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/v1/tools/call"
    data = json.dumps(
        {"name": "process_agent_update", "arguments": payload},
        separators=(",", ":"),
    ).encode()
    headers = {"Content-Type": "application/json"}
    if session_id:
        headers["X-Session-ID"] = session_id

    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}: {exc.reason}", "body": exc.read().decode()}
    except urllib.error.URLError as exc:
        return {"error": f"Connection failed: {exc.reason}"}
    except Exception as exc:
        return {"error": str(exc)}


def is_call_error(result: dict[str, Any]) -> bool:
    return bool(result.get("error") or result.get("isError") or result.get("success") is False)


def extract_verdict(result: dict[str, Any]) -> str | None:
    if isinstance(result.get("verdict"), str):
        return result["verdict"]
    nested = result.get("result")
    if isinstance(nested, dict) and isinstance(nested.get("verdict"), str):
        return nested["verdict"]
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                continue
            try:
                parsed = json.loads(item["text"])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("verdict"), str):
                return parsed["verdict"]
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a command and emit a best-effort UNITARES check-in",
        epilog="Example: python3 scripts/dev/with_checkin.py --workflow test -- python3 -m pytest tests/test_core.py",
    )
    parser.add_argument("--url", default=env_default("UNITARES_MCP_URL", DEFAULT_URL))
    parser.add_argument("--session", default=env_default("UNITARES_SESSION_ID"))
    parser.add_argument("--agent-id", default=env_default("UNITARES_AGENT_ID"))
    parser.add_argument(
        "--client-session-id",
        default=env_default("UNITARES_CLIENT_SESSION_ID"),
    )
    parser.add_argument(
        "--continuity-token",
        default=env_default("UNITARES_CONTINUITY_TOKEN"),
        help=(
            "Deprecated compatibility flag; ignored for process_agent_update. "
            "Use continuity_token only with explicit identity(agent_uuid, "
            "continuity_token, resume=true) PATH 0 rebinds."
        ),
    )
    parser.add_argument("--workflow", choices=WORKFLOWS, default="auto")
    parser.add_argument("--task-type")
    parser.add_argument("--complexity", type=float)
    parser.add_argument("--confidence", type=float)
    parser.add_argument(
        "--harness-type",
        default=env_default("UNITARES_HARNESS_TYPE", "codex-cli"),
    )
    parser.add_argument("--harness-id", default=env_default("UNITARES_HARNESS_ID"))
    parser.add_argument(
        "--model-provider",
        default=env_default("UNITARES_MODEL_PROVIDER", "openai"),
    )
    parser.add_argument("--model", default=env_default("UNITARES_MODEL", "gpt-5.5"))
    parser.add_argument("--transport", default=env_default("UNITARES_TRANSPORT", "terminal"))
    parser.add_argument(
        "--memory-context",
        default=env_default("UNITARES_MEMORY_CONTEXT"),
    )
    parser.add_argument(
        "--tool-surface",
        action="append",
        help="Available tool family; may be repeated or comma-separated",
    )
    parser.add_argument(
        "--governance-mode",
        default=env_default("UNITARES_GOVERNANCE_MODE", "explicit"),
    )
    parser.add_argument(
        "--comparison-key",
        default=env_default("UNITARES_COMPARISON_KEY"),
    )
    parser.add_argument("--task-label", default=env_default("UNITARES_TASK_LABEL"))
    parser.add_argument("--task-outcome", default=env_default("UNITARES_TASK_OUTCOME"))
    parser.add_argument("--episode-id", default=env_default("UNITARES_EPISODE_ID"))
    parser.add_argument("--invocation-id")
    parser.add_argument("--max-tail-lines", type=int, default=80)
    parser.add_argument(
        "--no-submit",
        action="store_true",
        help="Run the command and print the process_agent_update payload to stderr",
    )
    parser.add_argument(
        "--checkin-required",
        action="store_true",
        help="Return 1 if the command succeeds but process_agent_update fails",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def build_context(args: argparse.Namespace, command: list[str]) -> CheckinContext:
    workflow = infer_workflow(command) if args.workflow == "auto" else args.workflow
    invocation_id = args.invocation_id or f"with_checkin:{uuid.uuid4()}"
    return CheckinContext(
        workflow=workflow,
        url=args.url,
        session=args.session,
        agent_id=args.agent_id,
        client_session_id=args.client_session_id,
        task_type=args.task_type,
        complexity=args.complexity,
        confidence=args.confidence,
        harness_type=args.harness_type,
        harness_id=args.harness_id,
        model_provider=args.model_provider,
        model=args.model,
        transport=args.transport,
        memory_context=args.memory_context,
        tool_surface=parse_tool_surface(
            args.tool_surface,
            env_default("UNITARES_TOOL_SURFACE"),
        ),
        governance_mode=args.governance_mode,
        verification_source="agent_reported_tool_result",
        comparison_key=args.comparison_key,
        task_label=args.task_label,
        task_outcome=args.task_outcome,
        episode_id=args.episode_id,
        invocation_id=invocation_id,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = strip_command_separator(args.command)
    if not command:
        parser.error("missing command; use -- before the command to run")

    context = build_context(args, command)
    result = run_command(command, max_tail_lines=args.max_tail_lines)
    payload = build_checkin_payload(result, context)

    if args.no_submit:
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return result.exit_code

    checkin_result = call_process_agent_update(context.url, payload, context.session)
    if is_call_error(checkin_result):
        print(
            f"with_checkin: process_agent_update failed: {checkin_result}",
            file=sys.stderr,
        )
        if args.checkin_required and result.exit_code == 0:
            return 1
        return result.exit_code

    verdict = extract_verdict(checkin_result) or "unknown"
    print(f"with_checkin: process_agent_update verdict={verdict}", file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
