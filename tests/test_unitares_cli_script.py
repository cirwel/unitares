"""Regression tests for scripts/unitares — the bash CLI wrapper.

The script is a thin REST client for the governance MCP server, so these tests
require a live server. We start a sacrificial mcp_server.py instance against
the governance_test database on a random port — never the production server
on 8767. Skips if governance_test is not provisioned (see tests/test_db_utils).

What we verify:
    * URL sanitization strips trailing "/mcp" and "/" so a stale
      UNITARES_URL with the streamable-http path still hits the REST API.
    * Clear error reporting on unreachable hosts (no Python traceback leaks).
    * The end-to-end happy path: diag → health → tools → onboard → metrics →
      update, all against a sacrificial agent name and temp session file.
    * Session file persistence of uuid + client_session_id + continuity_token.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

from tests.test_db_utils import (
    TEST_DB_URL,
    can_connect_to_test_db,
    ensure_test_database_schema,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "scripts" / "unitares"


def _unique_agent_name() -> str:
    """Give each test a fresh identity so repeat runs don't collide with
    the server's trajectory-verification guard on resume."""
    return f"cli-pytest-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_ready(url: str, timeout_s: float = 60.0) -> None:
    """Wait until /health returns 200. The CLI uses /health (deep check that
    includes DB probe completion), so /health/live is not sufficient — the
    background probe must have run at least once."""
    deadline = time.time() + timeout_s
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=3) as resp:
                if resp.status == 200:
                    return
                last_err = RuntimeError(f"/health returned {resp.status}")
        except Exception as e:
            last_err = e
        time.sleep(0.5)
    raise TimeoutError(f"mcp_server at {url} not ready after {timeout_s}s: {last_err}")


@pytest.fixture(scope="session")
def mcp_test_server(tmp_path_factory):
    """Start a sacrificial mcp_server.py against governance_test on a random
    port. Tears it down after the test session.

    Skips the whole module if governance_test is not reachable — we will not
    fall back to the production server on 8767.
    """
    if not can_connect_to_test_db():
        pytest.skip(
            "governance_test database not available — see db/postgres/README.md "
            "for setup (createdb governance_test + init-extensions.sql)"
        )

    asyncio.run(ensure_test_database_schema())

    port = _pick_free_port()
    url = f"http://127.0.0.1:{port}"
    state_dir = tmp_path_factory.mktemp("cli-server-state")

    env = os.environ.copy()
    env["DB_POSTGRES_URL"] = TEST_DB_URL
    env["UNITARES_MCP_HOST"] = "127.0.0.1"
    env["UNITARES_SERVER_PID_FILE"] = str(state_dir / ".mcp_server.pid")
    env["UNITARES_SERVER_LOCK_FILE"] = str(state_dir / ".mcp_server.lock")
    env.pop("UNITARES_BIND_ALL_INTERFACES", None)
    # continuity_token is HMAC-signed; the server only emits one when a
    # secret is configured. Provide a deterministic test secret so the
    # CLI tests can assert on the token field.
    env.setdefault("UNITARES_CONTINUITY_TOKEN_SECRET", "pytest-fixture-secret")
    # Redirect the tool-usage tracker to a fresh tmp file so the subprocess
    # server doesn't read the developer-machine data/tool_usage.jsonl
    # (~1M lines / 177MB) on every process_update — that file is parsed
    # line-by-line by get_usage_stats and dominates CLI test wall-clock.
    env["UNITARES_TOOL_USAGE_LOG"] = str(state_dir / "tool_usage.jsonl")

    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "src" / "mcp_server.py"),
         "--port", str(port), "--force"],
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    try:
        _wait_for_ready(url, timeout_s=60.0)
    except Exception:
        proc.terminate()
        try:
            _, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            stderr = b""
        pytest.fail(
            f"test mcp_server failed to start on {url}\n"
            f"stderr:\n{stderr.decode(errors='replace')[:4000]}"
        )

    yield url

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture
def cli_env(tmp_path, mcp_test_server):
    """Minimal env for the CLI: isolated test-server URL, unique per-test
    agent, temp session file.

    A unique agent name avoids collisions with the server's
    trajectory-verification guard when the test is rerun in the same
    governance_test database.

    Archives the test agent on teardown so the test DB stays tidy.
    """
    env = os.environ.copy()
    env["UNITARES_URL"] = mcp_test_server
    agent_name = _unique_agent_name()
    env["UNITARES_AGENT"] = agent_name
    env["UNITARES_SESSION_FILE"] = str(tmp_path / "session.json")
    env["UNITARES_TIMEOUT"] = "30"
    yield env
    try:
        req = urllib.request.Request(
            f"{mcp_test_server}/v1/tools/call",
            data=json.dumps({
                "name": "agent",
                "arguments": {"action": "archive", "agent_id": agent_name}
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _run(env, *args, check=True):
    result = subprocess.run(
        [str(CLI), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"CLI exited {result.returncode}\n"
            f"cmd: {args}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def test_cli_is_executable():
    assert CLI.exists(), f"missing {CLI}"
    assert os.access(CLI, os.X_OK), f"{CLI} is not executable"


def test_diag_reports_localhost_reachable(cli_env, mcp_test_server):
    result = _run(cli_env, "diag")
    assert f"UNITARES_URL    : {mcp_test_server}" in result.stdout
    assert f"UNITARES_AGENT  : {cli_env['UNITARES_AGENT']}" in result.stdout
    assert "Reachability    : OK" in result.stdout


def test_url_sanitization_strips_mcp_suffix(cli_env, mcp_test_server):
    """A stale URL with /mcp baked on should still hit the REST API."""
    cli_env["UNITARES_URL"] = f"{mcp_test_server}/mcp"
    result = _run(cli_env, "diag")
    # After sanitization, the printed URL must NOT contain /mcp.
    assert f"UNITARES_URL    : {mcp_test_server}" in result.stdout
    assert "/mcp" not in result.stdout.split("UNITARES_URL")[1].split("\n")[0]
    assert "Reachability    : OK" in result.stdout


def test_url_sanitization_strips_trailing_slash(cli_env, mcp_test_server):
    cli_env["UNITARES_URL"] = f"{mcp_test_server}/"
    result = _run(cli_env, "diag")
    assert f"UNITARES_URL    : {mcp_test_server}" in result.stdout


def test_unreachable_host_fails_cleanly_without_traceback(cli_env):
    """Dead host must exit non-zero with a readable error, not a python traceback."""
    # RFC 5737 TEST-NET-1 — guaranteed non-routable.
    cli_env["UNITARES_URL"] = "http://192.0.2.1:9"
    cli_env["UNITARES_TIMEOUT"] = "2"
    result = _run(cli_env, "health", check=False)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "error" in result.stderr.lower()


def test_health_reports_status_and_version(cli_env):
    result = _run(cli_env, "health")
    assert "Status:" in result.stdout
    assert "Version:" in result.stdout
    assert "DB:" in result.stdout


def test_tools_lists_core_governance_tools(cli_env):
    result = _run(cli_env, "tools")
    assert "Tools:" in result.stdout
    # A few tools are always exposed in lite mode.
    assert "onboard" in result.stdout
    assert "process_agent_update" in result.stdout
    assert "get_governance_metrics" in result.stdout


def test_onboard_persists_session_and_continuity_token(cli_env, tmp_path):
    session_file = Path(cli_env["UNITARES_SESSION_FILE"])
    assert not session_file.exists()

    agent = cli_env["UNITARES_AGENT"]
    result = _run(cli_env, "onboard", agent, "pytest regression run")
    assert "Welcome:" in result.stdout
    assert "Session:" in result.stdout

    assert session_file.exists(), "onboard should create the session file"
    payload = json.loads(session_file.read_text())
    assert payload.get("agent_id") == agent
    assert payload.get("uuid"), "uuid not persisted"
    # Server returns client_session_id — CLI must persist it.
    assert payload.get("client_session_id"), "session id not persisted"
    # The token is retained for in-process proof-owned calls, not startup resume.
    assert payload.get("continuity_token"), "continuity token not persisted"


def test_metrics_after_onboard_shows_eisv(cli_env):
    _run(cli_env, "onboard", cli_env["UNITARES_AGENT"], "pytest")
    result = _run(cli_env, "metrics")
    assert "EISV:" in result.stdout
    assert "E=" in result.stdout and "I=" in result.stdout


def test_update_returns_verdict(cli_env):
    _run(cli_env, "onboard", cli_env["UNITARES_AGENT"], "pytest")
    result = _run(cli_env, "update", "pytest regression cli update", "0.2", "0.75")
    assert "Verdict:" in result.stdout
    # The parser should surface a real governance outcome, not a placeholder.
    assert "Verdict: ?" not in result.stdout


def test_session_command_shows_config(cli_env, mcp_test_server):
    agent = cli_env["UNITARES_AGENT"]
    _run(cli_env, "onboard", agent, "pytest")
    result = _run(cli_env, "session")
    assert f"Agent ID:     {agent}" in result.stdout
    assert f"URL:          {mcp_test_server}" in result.stdout
    assert "UUID:" in result.stdout
    assert "Continuity:   present" in result.stdout


def test_reset_removes_session_file(cli_env):
    _run(cli_env, "onboard", cli_env["UNITARES_AGENT"], "pytest")
    session_file = Path(cli_env["UNITARES_SESSION_FILE"])
    assert session_file.exists()
    _run(cli_env, "reset")
    assert not session_file.exists()


def _run_parser(parser_name: str, body: dict):
    """Invoke a CLI parser function by sourcing the script and piping a
    synthetic response body. Returns the CompletedProcess.

    This is how we regression-test the nested-success-false handling
    (trajectory_required and friends) without needing the live server to
    actually produce that response, which depends on an agent having an
    established trajectory — something hard to stage deterministically in
    a unit test against a shared dev database.
    """
    script = (
        f". {CLI} help >/dev/null 2>&1; "
        f"cat | {parser_name}"
    )
    return subprocess.run(
        ["bash", "-c", script],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        timeout=10,
    )


def _source_cli_and_run(script: str, env: dict[str, str]):
    """Source scripts/unitares, override shell functions, and run a snippet."""
    wrapped = f". {CLI} help >/dev/null 2>&1; {script}"
    return subprocess.run(
        ["bash", "-c", wrapped],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_parse_onboard_detects_nested_success_false():
    """Regression for the silent-failure bug: when the server returns
    result.success:false (e.g. established trajectory), parse_onboard
    must exit non-zero with a readable error and hint, not print
    "Welcome: onboarded" and return success.

    This was the bug that caused test_onboard_persists_session_and_
    continuity_token to fail in the full suite on 2026-04-10: the CLI
    wrote an empty session file because it treated a tool-level error
    as success.
    """
    response = {
        "name": "onboard",
        "success": True,  # outer envelope is "OK"
        "result": {
            "success": False,  # but the tool itself failed
            "error": "Identity 'cli-test' has an established trajectory.",
            "recovery": {
                "reason": "trajectory_required",
                "hint": "Provide trajectory_signature or use force_new=true",
            },
        },
    }
    result = _run_parser("parse_onboard", response)
    assert result.returncode != 0, (
        "parse_onboard must exit non-zero on nested success:false\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "Traceback" not in result.stderr
    assert "trajectory" in result.stderr.lower()
    assert "hint:" in result.stderr.lower()


def test_parse_update_prefers_governance_action_over_metric_verdict():
    body = {
        "success": True,
        "result": {
            "action": "proceed",
            "metrics": {"verdict": "safe"},
            "identity_assurance": {"tier": "strong", "session_source": "uuid"},
        },
    }
    result = _run_parser("parse_update", body)
    assert result.returncode == 0
    assert "Verdict: proceed" in result.stdout
    assert "Identity: strong (uuid)" in result.stdout
    # Update parser should stay within the update surface, not onboard output.
    assert "Welcome" not in result.stdout


def test_parse_update_unwraps_nested_result_payload():
    body = {
        "success": True,
        "result": {
            "success": True,
            "result": {
                "action": "continue",
                "margin": 0.12,
            },
        },
    }
    result = _run_parser("parse_update", body)
    assert result.returncode == 0
    assert "Verdict: continue" in result.stdout
    assert "Margin:  0.12" in result.stdout


def test_parse_onboard_accepts_valid_response():
    """Happy path for the parser: success:true, expected fields present."""
    response = {
        "name": "onboard",
        "success": True,
        "result": {
            "success": True,
            "welcome": "Welcome! Session established.",
            "display_name": "cli-happy-path",
            "agent_id": "mcp_20260410",
            "uuid": "11111111-2222-3333-4444-555555555555",
        },
    }
    result = _run_parser("parse_onboard", response)
    assert result.returncode == 0, f"stderr:\n{result.stderr}"
    assert "Welcome" in result.stdout
    assert "cli-happy-path" in result.stdout
    assert "11111111" in result.stdout


def test_curl_post_tool_does_not_inject_continuity_when_force_new(tmp_path):
    """S1-b: startup onboard sends force_new + lineage, not cached token/session."""
    session_file = tmp_path / "session.json"
    include_session_file = tmp_path / "include-session.txt"
    session_file.write_text(json.dumps({
        "uuid": "parent-uuid",
        "client_session_id": "cached-session",
        "continuity_token": "cached-token",
    }))
    env = os.environ.copy()
    env["UNITARES_SESSION_FILE"] = str(session_file)
    env["UNITARES_AGENT"] = "cli-unit"
    env["INCLUDE_SESSION_FILE"] = str(include_session_file)

    script = (
        "_curl_fetch() { printf '%s' \"$4\" > \"$INCLUDE_SESSION_FILE\"; printf '%s' \"$3\"; }; "
        "_curl_post_tool onboard '{\"force_new\": true, \"parent_agent_id\": \"parent-uuid\"}'"
    )
    result = _source_cli_and_run(script, env)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    args = payload["arguments"]
    assert args["force_new"] is True
    assert args["parent_agent_id"] == "parent-uuid"
    assert "client_session_id" not in args
    assert "continuity_token" not in args
    assert include_session_file.read_text() == "0"


def test_curl_post_tool_does_not_auto_inject_continuity_token(tmp_path):
    """S2/S3: ordinary calls use client_session_id, not token auto-injection."""
    session_file = tmp_path / "session.json"
    session_file.write_text(json.dumps({
        "uuid": "parent-uuid",
        "client_session_id": "cached-session",
        "continuity_token": "cached-token",
    }))
    env = os.environ.copy()
    env["UNITARES_SESSION_FILE"] = str(session_file)
    env["UNITARES_AGENT"] = "cli-unit"

    script = (
        "_curl_fetch() { printf '%s' \"$3\"; }; "
        "_curl_post_tool process_agent_update '{\"response_text\": \"hi\"}'"
    )
    result = _source_cli_and_run(script, env)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    args = payload["arguments"]
    assert args["client_session_id"] == "cached-session"
    assert "continuity_token" not in args


def test_cmd_onboard_declares_cached_uuid_as_parent(tmp_path):
    """The CLI onboard command should use cached uuid as lineage, not resume."""
    session_file = tmp_path / "session.json"
    capture = tmp_path / "payload.json"
    include_session_file = tmp_path / "include-session.txt"
    session_file.write_text(json.dumps({
        "uuid": "parent-uuid",
        "client_session_id": "cached-session",
        "continuity_token": "cached-token",
    }))
    env = os.environ.copy()
    env["UNITARES_SESSION_FILE"] = str(session_file)
    env["UNITARES_AGENT"] = "cli-unit"
    env["CAPTURE"] = str(capture)
    env["INCLUDE_SESSION_FILE"] = str(include_session_file)

    response = json.dumps({
        "success": True,
        "result": {
            "success": True,
            "welcome": "Welcome",
            "display_name": "cli-unit",
            "agent_id": "mcp_cli_unit",
            "uuid": "child-uuid",
            "client_session_id": "child-session",
            "continuity_token": "child-token",
        },
    })
    script = (
        "_curl_fetch() { printf '%s' \"$3\" > \"$CAPTURE\"; "
        "printf '%s' \"$4\" > \"$INCLUDE_SESSION_FILE\"; "
        f"printf '%s' '{response}'; }}; "
        "cmd_onboard cli-unit pytest"
    )
    result = _source_cli_and_run(script, env)
    assert result.returncode == 0, result.stderr

    payload = json.loads(capture.read_text())
    args = payload["arguments"]
    assert args["force_new"] is True
    assert args["parent_agent_id"] == "parent-uuid"
    assert args["spawn_reason"] == "new_session"
    assert "client_session_id" not in args
    assert "continuity_token" not in args
    assert include_session_file.read_text() == "0"

    written = json.loads(session_file.read_text())
    assert written["uuid"] == "child-uuid"
    assert written["client_session_id"] == "child-session"
    assert written["continuity_token"] == "child-token"
    assert written["parent_agent_id"] == "parent-uuid"


def test_onboard_with_force_creates_fresh_identity(cli_env):
    """Passing 'force' as the 3rd arg should set force_new=true and let
    the same agent name re-onboard cleanly."""
    agent = cli_env["UNITARES_AGENT"]
    _run(cli_env, "onboard", agent, "pytest force-first")

    result = _run(cli_env, "onboard", agent, "pytest force-second", "force")
    assert "Welcome:" in result.stdout
    second = json.loads(Path(cli_env["UNITARES_SESSION_FILE"]).read_text())
    assert second.get("client_session_id"), "force onboard should persist a new session id"
    assert "parent_agent_id" not in second, "force onboard should ignore cached lineage"
    # The continuity token must be present (value may or may not differ;
    # what we care about is that the path didn't silently produce an empty
    # write, which is what the trajectory-required regression was about).
    assert second.get("continuity_token")


def test_call_command_returns_pretty_json(cli_env):
    result = _run(cli_env, "call", "get_governance_metrics", "{}")
    # Pretty-printed JSON should be parseable.
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"call output is not valid JSON:\n{result.stdout}")
    assert parsed.get("name") == "get_governance_metrics"
    assert "result" in parsed


def test_call_with_invalid_json_arguments_errors_cleanly(cli_env):
    result = _run(cli_env, "call", "get_governance_metrics", "{not-json}", check=False)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "invalid JSON" in result.stderr or "error" in result.stderr.lower()
