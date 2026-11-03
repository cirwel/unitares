#!/usr/bin/env python3
"""Bounded synthetic read-only KG tasks; no live KG or adoption-pilot enrollment.

freeze records inputs before any model call. run requires --execute-argon and
retains every attempted episode. Provider calls use the existing subscription
CLI's isolated environment; instructions prohibit native CLI tool use.
Scope checks and caveats: docs/evaluations/kg-memory-workflow-refinement.md.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import time
import types
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.mcp_handlers.knowledge import handlers  # noqa: E402
from src.mcp_handlers.knowledge import imported_preview  # noqa: E402
from src.mcp_handlers.middleware.envelope_step import build_experience_envelope  # noqa: E402
from src.mcp_handlers.support import antigravity_cli_client as agy  # noqa: E402
from src.storage.knowledge_graph_age import KnowledgeGraphAGE  # noqa: E402

BASELINE = "7b717630e"
ENVELOPE = "src/mcp_handlers/middleware/envelope_step.py"
TASKS = ROOT / "tests/retrieval_eval/context_trajectories/tasks.json"
MAX_TURNS = 8
TURN_SECONDS = 60
EPISODE_SECONDS = 240
USAGE_REFERENCE = "https://www.antigravity.google/docs/cli/headless/"
INSTRUCTIONS = """Complete the supplied task using a simulated, read-only KG.
Return exactly one JSON object per turn. To request a simulated read, return
{"tool":"search_shared_memory","arguments":{"query":"..."}} or
{"tool":"knowledge","arguments":{"action":"details","discovery_id":"..."}}.
The harness executes that read and supplies its result in the next message.
Do not use the CLI's native tools. These JSON requests are your text reply.

search_shared_memory accepts query, tags, limit (1..10), and response_mode
(lean default, compact, full). Search returns a fixed relevant result set in
this fixture; it does not test query ranking. Supplied tags are exact filters
matching any supplied tag; a project record need not carry a project-name tag.
Keep a filtered miss intact; any broader search is a separate request.
Validation errors consume one turn and remain recorded; remaining turns allow
reformulation. A task may declare its own UTF-8 query byte limit.
knowledge details accepts offset
(characters, default 0) and length (default 2000). Follow pagination or note
offsets when needed. Summaries are leads, not a substitute for checking evidence.
An imported memory, note location, or lifecycle status is not proof of truth.
Neither the latest timestamp nor the last appended note establishes authority.
Check the evidence and scope of a correction; a newer unsupported note cannot
override checked evidence. Report unresolved conflicts rather than guessing.
After reading changes identified by a delivered notice, acknowledge the exact
revision with {"tool":"knowledge","arguments":{"action":"notices",
"acknowledge":["revision UUID"]}}. Do not acknowledge unread changes.

When finished, return {"answer":"...","source_ids":["..."],"reason":"..."}.
Cite only records whose underlying evidence text you actually inspected. Keep the reason short.
No writes, shell commands, external browsing, or other tool names are available.
"""


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def baseline_source():
    return subprocess.check_output(["git", "show", f"{BASELINE}:{ENVELOPE}"], cwd=ROOT)


def builders():
    module = types.ModuleType("src.mcp_handlers.middleware._context_trajectory_baseline")
    module.__package__ = "src.mcp_handlers.middleware"
    exec(compile(baseline_source(), f"{BASELINE}:{ENVELOPE}", "exec"), module.__dict__)
    return {"before": module.build_experience_envelope, "after": build_experience_envelope}


def _read_tasks(path=None):
    tasks = json.loads((path or TASKS).read_text())["tasks"]
    for task in tasks:
        for row in task["records"]:
            if "details_parts" in row:
                row["details"] = "".join(part if isinstance(part, str) else
                    part["repeat"] * part["count"] for part in row.pop("details_parts"))
    return tasks


def scope_preflight(tasks):
    """Check supplied membership and controls, never search or infer completeness.

    ``records`` is the provided catalog, not an asserted project-wide inventory.
    A scope can select capture_ids explicitly; tags remain observed attributes.
    The expected evidence source is always a control, even if omitted from an
    explicit required_source_ids list. Missing controls are never auto-filled.
    """
    def ids(values, label):
        if (not isinstance(values, list)
                or any(not isinstance(v, str) or not v.strip() for v in values)
                or len(values) != len(set(values))):
            raise ValueError(f"{label} must be unique nonempty source IDs")
        return values

    rows, errors = [], []
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("tasks must be a nonempty list")
    task_ids = []
    for index, task in enumerate(tasks):
        row = {"task": task.get("id") if isinstance(task, dict) else None}
        try:
            if not isinstance(task, dict) or not isinstance(task.get("id"), str) or not task["id"].strip():
                raise ValueError("task ID must be a nonempty string")
            task_ids.append(task["id"])
            records = task["records"]
            if not isinstance(records, list) or not records or any(not isinstance(r, dict) for r in records):
                raise ValueError("records must be a nonempty catalog")
            catalog = ids([r.get("id") for r in records], "catalog")
            scope = task.get("scope", {})
            if not isinstance(scope, dict):
                raise ValueError("scope must be an object")
            membership = scope.get("membership_source", "provided_records" if not scope else None)
            if not isinstance(membership, str) or not membership.strip():
                raise ValueError("scope needs an explicit membership_source")
            capture = ids(scope.get("capture_ids", catalog), "capture_ids")
            expected = task["expected"]["source_id"]
            if not isinstance(expected, str) or not expected.strip():
                raise ValueError("expected source_id must be a nonempty string")
            controls = ids(scope.get("required_source_ids", []), "required_source_ids")
            controls = list(dict.fromkeys([expected, *controls]))
            search_ids = ids(task["search_ids"], "search_ids")
            byte_limit = task.get("query_byte_limit")
            if byte_limit is not None and (type(byte_limit) is not int or byte_limit < 1):
                raise ValueError("query_byte_limit must be a positive integer")
            by_id = {r["id"]: r for r in records}
            row.update(
                membership_source=membership,
                catalog_count=len(catalog), capture_count=len(capture),
                required_source_ids=controls,
                unresolved_capture_ids=sorted(set(capture) - set(catalog)),
                unresolved_control_ids=sorted(set(controls) - set(catalog)),
                missing_control_ids=sorted(set(controls) - set(capture)),
                unresolved_search_ids=sorted(set(search_ids) - set(catalog)),
                catalog_sha256=digest(encoded(records)),
                capture_sha256=digest(encoded([by_id[k] for k in capture if k in by_id])),
                controls_sha256=digest(encoded(controls)),
                scope_sha256=digest(encoded({"membership_source": membership, "capture_ids": capture,
                                             "required_source_ids": controls})),
            )
            problems = {k: row[k] for k in ("unresolved_capture_ids", "unresolved_control_ids",
                                           "missing_control_ids", "unresolved_search_ids") if row[k]}
            if problems:
                errors.append({"task": task["id"], **problems})
        except (ValueError, KeyError, TypeError) as exc:
            errors.append({"task": row["task"], "error": str(exc)})
        rows.append(row)
    if len(task_ids) != len(set(task_ids)):
        errors.append({"error": "duplicate task IDs"})
    return {"schema": "kg-context-trajectories.scope.v1", "ok": not errors,
            "inputs_sha256": digest(encoded(tasks)), "tasks": rows, "errors": errors,
            "scope": "Membership and positive controls in the supplied catalogs only; "
                     "no retrieval, project-completeness, truth or utility judgment."}


def load_tasks(path=None):
    tasks = _read_tasks(path)
    report = scope_preflight(tasks)
    if not report["ok"]:
        raise ValueError("scope preflight failed: " + json.dumps(report["errors"]))
    return tasks


def input_hashes():
    subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "src", "governance_core"], cwd=ROOT, check=True)
    paths = [Path(__file__), TASKS, ROOT / ENVELOPE,
             ROOT / "src/mcp_handlers/knowledge/handlers.py",
             ROOT / "src/mcp_handlers/knowledge/read_metadata.py",
             ROOT / "src/mcp_handlers/support/antigravity_cli_client.py"]
    return {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in paths} | {
        "baseline_envelope": digest(baseline_source()), "instructions": digest(INSTRUCTIONS.encode()),
        **{name: subprocess.check_output(["git", "rev-parse", f"HEAD:{name}"], cwd=ROOT, text=True).strip()
           for name in ("src", "governance_core")}}


def freeze(model):
    tasks = load_tasks()
    scope = scope_preflight(tasks)
    random.Random(493412).shuffle(tasks)
    schedule = []
    for index, task in enumerate(tasks):
        order = ("before", "after") if index % 2 == 0 else ("after", "before")
        schedule.extend({"task": task["id"], "condition": condition} for condition in order)
    return {"schema": "kg-context-trajectories.plan.v1", "frozen_at": datetime.now(timezone.utc).isoformat(),
            "repository_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "model_selected": model, "inputs": input_hashes(), "schedule": schedule,
            "scope_preflight": scope,
            "limits": {"turns": MAX_TURNS, "turn_seconds": TURN_SECONDS,
                       "episode_seconds": EPISODE_SECONDS},
            "interpretation": "One paired exploratory run per synthetic task; descriptive only. "
            "No deployment/retirement threshold, no equivalence or general capability claim. "
            "All attempts, errors, limits and missing usage remain in the report."}


class FixtureDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        fixed = cls(2026, 10, 14, 8, 0, tzinfo=timezone.utc)
        return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)


class Fixture:
    """Use production serialization and details reads against in-memory records."""

    def __init__(self, task, builder):
        if not scope_preflight([task])["ok"]:
            raise ValueError("fixture scope preflight failed")
        self.task, self.builder = deepcopy(task), builder
        capture = set(task.get("scope", {}).get("capture_ids", [r["id"] for r in task["records"]]))
        graph = KnowledgeGraphAGE()
        self.docs = {r["id"]: graph._dict_to_discovery({
            "agent_id": "fixture-writer", "type": "note", "status": "open",
            "created_at": "2026-10-14T00:00:00+00:00", "tags": [],
            "provenance": {"writer_label_at_write": "Fixture writer"}, **r})
            for r in self.task["records"] if r["id"] in capture}
        self.read_text = {}
        self.delivered = {}
        self.changed = False
        self.notice_delivered = False
        self.acknowledged = False
        self.notice_deliveries = 0
        self.premature_acks = 0

    async def get_discovery(self, discovery_id):
        return self.docs.get(discovery_id)

    async def get_superseded_by(self, ids):
        return {key: value for key, value in self.task.get("successors", {}).items() if key in ids}

    async def call(self, request):
        tool, args = request.get("tool"), request.get("arguments")
        if not isinstance(args, dict):
            return {"success": False, "error": "arguments must be an object"}
        if tool == "search_shared_memory":
            mode = args.get("response_mode", "lean")
            limit = args.get("limit", 10)
            if (not isinstance(mode, str) or mode not in {"lean", "compact", "full"}
                    or type(limit) is not int or not 1 <= limit <= 10):
                return {"success": False, "error": "invalid response mode or limit"}
            query = args.get("query", "evidence")
            if not isinstance(query, str):
                return {"success": False, "error": "query must be a nonempty string"}
            byte_limit = self.task.get("query_byte_limit")
            query_bytes = len(query.encode("utf-8"))
            if byte_limit is not None and query_bytes > byte_limit:
                return {"success": False,
                        "error": f"Search query is {query_bytes} UTF-8 bytes; this fixture limit is "
                                 f"{byte_limit}. Submit a shorter query. This attempt is retained."}
            try:
                parsed = handlers._parse_knowledge_search_request({**args, "query": query})
            except handlers._SearchParameterError as exc:
                return {"success": False, "error": str(exc)}
            state = handlers._KnowledgeSearchState(request=parsed, graph=self)
            candidates = [self.docs[key] for key in self.task["search_ids"] if key in self.docs]
            if parsed.tags:
                candidates = [doc for doc in candidates if any(tag in doc.tags for tag in parsed.tags)]
            state.results = candidates[:limit]
            # Keep metadata age stable on later replays without changing the
            # production age calculation or labelling an old record fresh.
            with patch.object(handlers, "datetime", FixtureDateTime), \
                 patch.object(imported_preview, "datetime", FixtureDateTime):
                rows = handlers._serialize_search_discoveries(state, include_details=False)
            await handlers._annotate_supersession(rows, self)
            source = {"success": True, **handlers._base_search_response(
                state, rows, auto_details=False, include_details=False)}
            if mode == "lean":
                source = handlers._lean_search_payload(source)
            response = self.builder("search_shared_memory", "knowledge", source, {**args, "response_mode": mode})
            # A full search may deliver complete short evidence in its preview.
            # Credit only text that survives the formatter, not pre-projection rows.
            for item in response.get("raw_governance", {}).get("discoveries", []):
                key = item.get("id")
                body = item.get("details", item.get("details_preview", ""))
                if key in self.docs and isinstance(body, str) and body:
                    if not self.docs[key].details.startswith(body) and body.endswith("..."):
                        body = body[:-3]
                    self.record_delivery(key, body, 0)
        elif tool == "knowledge" and args.get("action") == "details":
            key = args.get("discovery_id")
            if not isinstance(key, str) or key not in self.docs:
                return {"success": False, "error": "unknown fixture discovery_id"}
            for field, default, minimum in (("offset", 0, 0), ("length", 2000, 1)):
                value = args.get(field, default)
                if type(value) is not int or value < minimum:
                    return {"success": False, "error": "invalid pagination"}
            with patch.object(handlers, "get_knowledge_graph", AsyncMock(return_value=self)), \
                 patch.object(handlers, "_broadcast_knowledge_read", AsyncMock()), \
                 patch.object(handlers, "success_response", lambda payload, **_: {"success": True, **payload}):
                response = await inspect.unwrap(handlers.handle_get_discovery_details)(args)
            body = response.get("details", response.get("discovery", {}).get("details", ""))
            offset = response.get("pagination", {}).get("offset", 0)
            self.record_delivery(key, body, offset)
        elif tool == "knowledge" and args.get("action") == "notices":
            change = self.task.get("change", {})
            if (not self.notice_delivered or args.get("acknowledge") != [change.get("revision")]
                    or not self.covered(change.get("id"), change.get("details"), 0,
                                        len(change.get("details", "")))):
                self.premature_acks += 1
                response = {"success": False, "error": "ack requires the delivered revision and its newly read details"}
            else:
                self.acknowledged = True
                response = {"success": True, "notices": []}
        else:
            return {"success": False, "error": "only the declared read-only fixture operations are available"}
        change = self.task.get("change")
        if change and not self.changed and any(self.read_text.get(change["id"], [])):
            self.docs[change["id"]].details = change["details"]
            self.changed = True
        if self.changed and not self.acknowledged:
            change = self.task["change"]
            response["knowledge_changes"] = {"notices": [{"discovery_id": change["id"],
                "revision": change["revision"], "kind": "update", "changed_fields": ["details"],
                "details": {"action": "details", "discovery_id": change["id"]}}],
                "muted": False, "has_more": False,
                "ack_action": "knowledge(action='notices', acknowledge=[revision UUIDs])"}
            self.notice_delivered = True
            self.notice_deliveries += 1
        return response

    def record_delivery(self, key, body, offset):
        version = self.docs[key].details
        if version[offset:offset + len(body)] != body:
            raise ValueError("fixture delivery does not match its source range")
        self.read_text.setdefault(key, []).append(body)
        self.delivered.setdefault((key, version), []).append((offset, offset + len(body)))

    def covered(self, key, version, start, end):
        """Coverage is contiguous, character-based, and never joins revisions."""
        cursor = start
        for left, right in sorted(self.delivered.get((key, version), [])):
            if left > cursor:
                return False
            cursor = max(cursor, right)
            if cursor >= end:
                return True
        return False

    def score(self, final):
        final = final if isinstance(final, dict) else {}
        expected = self.task["expected"]
        citations = final.get("source_ids", [])
        citation_shape = isinstance(citations, list) and all(isinstance(c, str) for c in citations)
        valid_citations = citation_shape and bool(citations) and all(any(self.read_text.get(c, [])) for c in citations)
        evidence_read = False
        for key, version in self.delivered:
            if key != expected["source_id"]:
                continue
            start = version.find(expected["evidence"])
            while start >= 0:
                if self.covered(key, version, start, start + len(expected["evidence"])):
                    evidence_read = True
                start = version.find(expected["evidence"], start + 1)
        correct = final.get("answer") == expected["answer"]
        return {"answer_correct": correct, "evidence_read": evidence_read,
                "citations_valid": valid_citations,
                "task_success": bool(correct and evidence_read and valid_citations
                                     and expected["source_id"] in citations),
                "notice_deliveries": self.notice_deliveries, "notice_acknowledged": self.acknowledged,
                "premature_ack_attempts": self.premature_acks}


class ArgonConversation:
    """One isolated, persistent CLI conversation per task/condition; no retries."""

    def __init__(self, model):
        self.model, self.cid = model, None
        self.stack = ExitStack()

    def __enter__(self):
        cli = shutil.which("agy")
        if not cli:
            raise RuntimeError("agy subscription CLI unavailable")
        root = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="kg-context-agy-"))
        if any((d / marker).exists() for d in Path(root).resolve().parents for marker in (".git", ".agents")):
            self.stack.close()
            raise RuntimeError("CLI workspace is not isolated")
        self.cwd = Path(root) / "workspace"
        self.cwd.mkdir(mode=0o700)
        self.env = agy.agy_env(home=agy.isolated_home(root, os.environ.get("HOME")))
        self.cli = cli
        return self

    def __exit__(self, *_):
        self.stack.close()

    def ask(self, prompt, timeout):
        prompt = agy.guard_prompt(prompt + agy.TEXT_ONLY)
        if len(prompt.encode()) > agy.PROMPT_BYTES_LIMIT:
            raise ValueError("prompt exceeds CLI input bound")
        command = [self.cli, "-p", prompt, *agy.AGY_FLAGS, "--model", self.model]
        if self.cid:
            command += ["--conversation", self.cid]
        started = time.monotonic()
        try:
            code, stdout, stderr = agy._run(command, cwd=str(self.cwd), env=self.env, timeout_s=timeout)
        except OSError as exc:
            code, stdout, stderr = None, "", f"CLI spawn failed: {type(exc).__name__}"
        raw = agy.parse_output(stdout)
        cid = raw.get("conversation_id")
        error = None
        if code != 0 or raw.get("status") != "SUCCESS" or not raw.get("response"):
            error = "CLI timeout" if code is None else "CLI did not return a successful nonempty reply"
        elif not isinstance(cid, str) or not cid or (self.cid and cid != self.cid):
            error = "CLI conversation continuity not established"
        if not error:
            self.cid = cid
        return {"raw": raw, "stderr": stderr, "exit_status": code, "error": error,
                "prompt_bytes": len(prompt.encode()), "latency_ms": round((time.monotonic() - started) * 1000)}


def parse_reply(text):
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("reply must be one object")
    if "tool" in value:
        if set(value) != {"tool", "arguments"} or not isinstance(value["tool"], str):
            raise ValueError("read request must have tool and arguments only")
    elif (set(value) != {"answer", "source_ids", "reason"}
          or not isinstance(value["answer"], str) or not isinstance(value["reason"], str)
          or not isinstance(value["source_ids"], list)
          or not all(isinstance(x, str) for x in value["source_ids"])):
        raise ValueError("final reply must have answer, source_ids and reason")
    return value


def usage_metrics(turns):
    """CLI result usage is cumulative across the conversation, including resumes.

    Preserve the last observed snapshot on a missing receipt, marking it partial.
    Counter deltas measure reported per-call usage, never context-window occupancy.
    See USAGE_REFERENCE, 'Read the results'.
    """
    latest, previous, issues, input_deltas = {}, {}, [], []
    for index, turn in enumerate(turns, 1):
        usage = turn.get("raw", {}).get("usage")
        if (not isinstance(usage, dict)
                or not all(type(usage.get(k)) is int and usage[k] >= 0
                           for k in ("input_tokens", "output_tokens"))
                or any(type(v) is not int or v < 0 for v in usage.values())):
            issues.append(f"turn {index}: missing or invalid cumulative usage")
            continue
        if any(usage.get(key, -1) < value for key, value in previous.items()):
            issues.append(f"turn {index}: cumulative counter decreased or disappeared")
        input_deltas.append(usage["input_tokens"] - previous.get("input_tokens", 0))
        latest = dict(usage)
        previous = latest
    complete = bool(turns) and not issues
    return {"usage": latest, "usage_complete": complete, "usage_issues": issues,
            "peak_call_input_tokens": max(input_deltas) if complete else None,
            "peak_context_tokens": None}


async def episode(task, builder, conversation, checkpoint=lambda turns: None):
    fixture = Fixture(task, builder)
    prompt = INSTRUCTIONS + "\nTask: " + task["question"]
    if task.get("query_byte_limit") is not None:
        prompt += f"\nThis fixture limits each query to {task['query_byte_limit']} UTF-8 bytes."
    turns, final, termination = [], None, "turn_limit"
    deadline = time.monotonic() + EPISODE_SECONDS
    for _ in range(MAX_TURNS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            termination = "episode_timeout"
            break
        entry = {"prompt": prompt, "pending": True}
        turns.append(entry)
        checkpoint(turns)
        receipt = conversation.ask(prompt, min(TURN_SECONDS, remaining))
        entry.update(receipt)
        entry.pop("pending")
        checkpoint(turns)
        if receipt["error"]:
            termination = "provider_error"
            break
        try:
            reply = parse_reply(receipt["raw"]["response"])
        except (ValueError, TypeError) as exc:
            entry["protocol_error"] = str(exc)
            checkpoint(turns)
            prompt = "Invalid reply: " + str(exc) + ". Return the next request or final answer as one JSON object."
            continue
        entry["reply"] = reply
        checkpoint(turns)
        if "tool" not in reply:
            final, termination = reply, "final"
            break
        try:
            response = await fixture.call(reply)
        except Exception as exc:  # preserve attempted calls; do not score a broken harness
            entry["harness_error"] = f"{type(exc).__name__}: {exc}"
            checkpoint(turns)
            termination = "harness_error"
            break
        entry["tool_response"] = response
        checkpoint(turns)
        prompt = "Result of your preceding simulated read:\n" + encoded(response).decode() + "\nReturn your next JSON request or final answer."
    return {"termination": termination, "final": final, "turns": turns, **usage_metrics(turns),
            "provider_turns": len(turns),
            "tool_calls": sum("tool" in t.get("reply", {}) for t in turns),
            "tool_errors": sum(t.get("tool_response", {}).get("success") is False for t in turns),
            "protocol_errors": sum("protocol_error" in t for t in turns),
            "tool_response_bytes": sum(len(encoded(t["tool_response"])) for t in turns if "tool_response" in t),
            "latency_ms": sum(t["latency_ms"] for t in turns), **fixture.score(final)}


def summarize(plan, records):
    fields = ("termination", "usage", "usage_complete", "usage_issues", "provider_turns",
              "peak_call_input_tokens", "peak_context_tokens",
              "tool_calls", "tool_errors", "protocol_errors",
              "tool_response_bytes", "latency_ms", "answer_correct", "evidence_read", "citations_valid",
              "task_success", "notice_deliveries", "notice_acknowledged", "premature_ack_attempts")
    rows = [{"task": r["task"], "condition": r["condition"], **{k: r[k] for k in fields}} for r in records]
    return {"schema": "kg-context-trajectories.aggregate.v2", "plan_sha256": digest(encoded(plan)),
            "model_selected": plan["model_selected"], "effective_model_receipt": None,
            "usage_semantics": "Last cumulative CLI session snapshot per episode; never sum resumed snapshots. "
            "Counters remain separate. Peak call input is the largest input-counter delta; context peak is unavailable.",
            "usage_reference": USAGE_REFERENCE,
            "planned_episodes": len(plan["schedule"]), "completed_episode_receipts": len(records), "rows": rows,
            "scope": "Synthetic sequential JSON-read simulation with fixed search results; production search "
            "serialization and unwrapped details handler, simulated change notices. Each episode uses a fresh "
            "isolated persistent CLI conversation. Usage retains CLI-reported cumulative counters, including hidden "
            "CLI instructions; no tokenizer estimate or native MCP-client claim. One pair per task, no retries "
            "or statistical equivalence claim. No live KG or existing adoption-pilot execution."}


def write_json(path, value):
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    pending.replace(path)


def report_existing(run_dir):
    """Correct accounting offline; preserve every original receipt and task score."""
    plan = json.loads((run_dir / "plan.json").read_text())
    original = json.loads((run_dir / "aggregate.json").read_text())
    records, receipt_hashes = [], {}
    for index, scheduled in enumerate(plan["schedule"], 1):
        path = run_dir / f"episode-{index:02}.json"
        if not path.exists():
            break
        record = json.loads(path.read_text())
        if any(record.get(key) != value for key, value in scheduled.items()):
            raise ValueError("receipt does not match frozen schedule")
        if record.get("termination") == "running":
            raise ValueError("pending receipt: wait for execution to finish")
        receipt_hashes[path.name] = digest(path.read_bytes())
        record.update(usage_metrics(record["turns"]))
        records.append(record)
    if len(records) != original["completed_episode_receipts"]:
        raise ValueError("receipt count does not match original aggregate")
    return {**summarize(plan, records),
            "transport_canary_passed": original["transport_canary_passed"],
            "accounting_correction": {
                "reason": "Original v1 summed cumulative session snapshots and mislabeled their maximum as peak input. "
                "Offline v2 corrects usage only; no task, prompt, scorer, attempt or provider outcome changed or reran.",
                "source_aggregate_sha256": digest((run_dir / "aggregate.json").read_bytes()),
                "source_receipt_sha256": receipt_hashes,
                "report_script_sha256": digest(Path(__file__).read_bytes())}}


def transport_canary(model):
    receipts = []
    with ArgonConversation(model) as conversation:
        prompts = [
            'Remember marker copper-742. Return exactly {"answer":"copper-742","source_ids":[],"reason":"marker"}.',
            'Return the marker from the previous turn as answer, with source_ids=[] and a short reason. Use the same JSON shape.',
        ]
        for prompt in prompts:
            receipt = conversation.ask(prompt, TURN_SECONDS)
            receipts.append({"prompt": prompt, **receipt})
            if receipt["error"]:
                break
    try:
        passed = len(receipts) == 2 and all(not r["error"] and
            parse_reply(r["raw"]["response"]).get("answer") == "copper-742" for r in receipts)
    except (ValueError, TypeError):
        passed = False
    return {"passed": passed, "turns": receipts, "scope": "Neutral transport check; excluded from scored task totals."}


async def run(plan, output):
    if plan.get("inputs") != input_hashes():
        raise ValueError("frozen inputs changed; do not execute or score against this plan")
    if plan.get("limits") != {"turns": MAX_TURNS, "turn_seconds": TURN_SECONDS, "episode_seconds": EPISODE_SECONDS}:
        raise ValueError("frozen limits changed")
    loaded = load_tasks()
    if plan.get("scope_preflight") != scope_preflight(loaded):
        raise ValueError("frozen scope controls changed or are missing")
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "plan.json", plan)
    canary = transport_canary(plan["model_selected"])
    write_json(output / "transport-canary.json", canary)
    if not canary["passed"]:
        write_json(output / "aggregate.json", {**summarize(plan, []), "transport_canary_passed": False})
        raise RuntimeError("transport canary failed; no scored episodes attempted")
    tasks = {task["id"]: task for task in loaded}
    variants, records = builders(), []
    for index, scheduled in enumerate(plan["schedule"]):
        path = output / f"episode-{index + 1:02}.json"

        def checkpoint(turns):
            write_json(path, {**scheduled, "termination": "running", "turns": turns})

        with ArgonConversation(plan["model_selected"]) as conversation:
            result = await episode(tasks[scheduled["task"]], variants[scheduled["condition"]], conversation, checkpoint)
        record = {**scheduled, **result}
        records.append(record)
        write_json(path, record)
        write_json(output / "aggregate.json", {**summarize(plan, records), "transport_canary_passed": True})
        print(json.dumps({"episode": index + 1, **scheduled, "termination": result["termination"],
                          "task_success": result["task_success"], "tool_calls": result["tool_calls"],
                          "usage": result["usage"]}), flush=True)
        if result["termination"] == "harness_error":
            raise RuntimeError("harness error retained; remaining episodes not attempted")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    frozen = commands.add_parser("freeze")
    frozen.add_argument("--model", required=True)
    frozen.add_argument("--output", type=Path, required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--execute-argon", action="store_true")
    report = commands.add_parser("report", help="Correct usage from existing receipts without model calls")
    report.add_argument("--run-dir", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    preflight = commands.add_parser("preflight", help="Check catalog membership and controls without model calls")
    preflight.add_argument("--tasks", type=Path, default=TASKS)
    preflight.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "freeze":
        with args.output.open("x") as stream:
            json.dump(freeze(args.model), stream, indent=2)
            stream.write("\n")
    elif args.action == "preflight":
        result = scope_preflight(_read_tasks(args.tasks))
        with args.output.open("x") as stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
        if not result["ok"]:
            raise SystemExit(1)
    elif args.action == "report":
        result = report_existing(args.run_dir)
        with args.output.open("x") as stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
    elif not args.execute_argon:
        parser.error("run requires --execute-argon; this invokes the existing subscription CLI")
    else:
        asyncio.run(run(json.loads(args.plan.read_text()), args.output))


if __name__ == "__main__":
    main()
