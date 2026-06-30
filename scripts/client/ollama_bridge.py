#!/usr/bin/env python3
"""
Ollama-UNITARES Bridge
Connects local Ollama models to the UNITARES governance MCP server via smolagents.

The raw MCP tool schemas have too many optional parameters for smaller models.
This script creates simplified wrapper tools with minimal signatures, then
exposes them to the model via CodeAgent (which writes Python, not JSON tool calls).

Configuration (env vars; CLI flags override):
    UNITARES_MCP_URL      governance MCP endpoint   (default http://127.0.0.1:8767/mcp/)
    UNITARES_OLLAMA_URL   Ollama OpenAI-compat API  (default http://127.0.0.1:11434/v1)
    UNITARES_LLM_MODEL    Ollama model to drive     (default gemma4:latest)

Identity posture (v2 ontology — see docs/ontology/identity.md):
    A fresh bridge run mints a fresh process-instance identity
    (`onboard(force_new=true, spawn_reason="new_session")`, no parent).
    Co-location is not lineage; the default declares no parent.

    UNITARES_AGENT_UUID   substrate-anchored continuity: resume a fixed UUID
                          across restarts (the long-lived-resident pattern,
                          `identity(agent_uuid=..., resume=true)`) instead of
                          minting fresh. Use only for a genuinely persistent
                          local agent that earns one durable identity.
    UNITARES_PARENT_AGENT_ID  declare causal lineage to a prior agent. Pair
                          with a causal --spawn-reason (e.g. "explicit" for a
                          handoff from an EXITED prior session). Declaring a
                          LIVE agent as parent is rejected server-side
                          (lineage_coincidental_rejected).
    UNITARES_SPAWN_REASON one of new_session|explicit|subagent|compaction|
                          dispatch (default new_session).

Usage:
    python3 ollama_bridge.py                          # interactive, configured model
    python3 ollama_bridge.py --model gemma4:31b       # use a specific model
    python3 ollama_bridge.py --task "check health"    # single task, then exit
    python3 ollama_bridge.py --agent-uuid <uuid>      # resume a durable identity
    python3 ollama_bridge.py --parent-agent-id <uuid> --spawn-reason explicit
"""

import argparse
import json
import os
import sys

from smolagents import tool, ToolCollection, OpenAIServerModel, CodeAgent

# Defaults match repo conventions: UNITARES_MCP_URL (client URL, cf.
# scripts/dev/with_checkin.py) and UNITARES_LLM_MODEL (cf. the call_model /
# llm_delegation local path). Endpoints are config, not identity.
DEFAULT_MCP_URL = os.getenv("UNITARES_MCP_URL", "http://127.0.0.1:8767/mcp/")
DEFAULT_OLLAMA_URL = os.getenv("UNITARES_OLLAMA_URL", "http://127.0.0.1:11434/v1")
DEFAULT_MODEL = os.getenv("UNITARES_LLM_MODEL", "gemma4:latest")

# Valid spawn_reason values per the v2 ontology. "new_session" is the honest
# fresh default (no parent); the rest are causal and expect a parent_agent_id.
VALID_SPAWN_REASONS = ("new_session", "explicit", "subagent", "compaction", "dispatch")
CAUSAL_SPAWN_REASONS = ("explicit", "subagent", "compaction", "dispatch")

INSTRUCTIONS = """\
You are a UNITARES governance agent running locally via Ollama.
You interact with the UNITARES governance system through Python tool calls.

## Available tools:
- check_health() — system health status
- register_agent(name) — register yourself (call once per session)
- checkin(description, complexity, confidence) — report what you did
- get_metrics() — see your EISV state, coherence, risk
- search_knowledge(query) — search the shared knowledge graph
- save_note(summary, tags) — save a finding to the knowledge graph
- list_agents() — see all registered agents
- ask_tool(tool_name) — get details about any governance tool

## First time? Call register_agent("ollama-local") to get started.
"""


class IdentityConfig:
    """Operator-determined identity posture for this bridge run.

    Identity is substrate config, not a model decision: the model calls
    register_agent(name) with a cosmetic label, and the bridge applies the
    configured posture (fresh / substrate-anchored / declared-lineage).
    """

    def __init__(self, agent_uuid=None, parent_agent_id=None, spawn_reason="new_session"):
        self.agent_uuid = agent_uuid or None
        self.parent_agent_id = parent_agent_id or None
        self.spawn_reason = spawn_reason or "new_session"

    @classmethod
    def from_args(cls, args):
        cfg = cls(
            agent_uuid=args.agent_uuid or os.getenv("UNITARES_AGENT_UUID"),
            parent_agent_id=args.parent_agent_id or os.getenv("UNITARES_PARENT_AGENT_ID"),
            spawn_reason=(args.spawn_reason or os.getenv("UNITARES_SPAWN_REASON") or "new_session"),
        )
        cfg.validate()
        return cfg

    def validate(self):
        if self.spawn_reason not in VALID_SPAWN_REASONS:
            raise SystemExit(
                f"Invalid --spawn-reason '{self.spawn_reason}'. "
                f"Expected one of: {', '.join(VALID_SPAWN_REASONS)}."
            )
        # A causal spawn_reason names a real spawn/handoff and needs a parent.
        if self.spawn_reason in CAUSAL_SPAWN_REASONS and not self.parent_agent_id:
            raise SystemExit(
                f"--spawn-reason '{self.spawn_reason}' is causal and requires "
                "--parent-agent-id (the UUID of the prior agent). For a fresh "
                "run, use spawn_reason 'new_session' (the default)."
            )

    def describe(self):
        if self.agent_uuid:
            return f"substrate-anchored resume of {self.agent_uuid}"
        if self.parent_agent_id:
            return f"fresh, lineage={self.spawn_reason} from {self.parent_agent_id}"
        return f"fresh, {self.spawn_reason} (no lineage)"


class Session:
    """In-process identity binding for one bridge run.

    Per the v2 ontology, `client_session_id` maintains identity within a single
    process. register_agent captures it from the onboard/resume response, and
    every later call echoes it so the model's successive tool calls form ONE
    trajectory — not a scatter of fingerprint-resolved writes. Without this the
    strict-identity gate refuses writes ("identity resolved by transport
    fingerprint, not a proof you supplied") or, worse, lands them on a sibling
    identity sharing the httpx fingerprint.
    """

    def __init__(self):
        self.client_session_id = None
        self.agent_uuid = None

    def capture(self, raw: str) -> str:
        """Extract client_session_id / agent_uuid from a tool response string."""
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return raw
        csid = _deep_find(data, "client_session_id")
        if csid:
            self.client_session_id = csid
        uuid = _deep_find(data, "agent_uuid")
        if uuid:
            self.agent_uuid = uuid
        return raw

    def bind(self, **kwargs) -> dict:
        """Add the captured client_session_id to a call's kwargs if we have one."""
        if self.client_session_id:
            kwargs.setdefault("client_session_id", self.client_session_id)
        return kwargs


def _deep_find(obj, key):
    """First value for `key` anywhere in a nested dict/list, or None."""
    if isinstance(obj, dict):
        if obj.get(key):
            return obj[key]
        for v in obj.values():
            found = _deep_find(v, key)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find(v, key)
            if found:
                return found
    return None


def make_wrappers(mcp_tools: dict, identity: "IdentityConfig", session: "Session"):
    """Create simplified tool wrappers over the raw MCP tools."""

    @tool
    def check_health() -> str:
        """Check UNITARES governance system health, database, and uptime."""
        return mcp_tools["health_check"](lite="true")

    @tool
    def register_agent(name: str) -> str:
        """Register as a governance agent. Call this once at the start of a session.
        Args:
            name: Display name for this agent (e.g. 'ollama-local')
        """
        # Identity posture is operator-configured, not chosen here; `name` is
        # the cosmetic label only (not used for resolution — name-claim removed
        # 2026-04-17). Three ontology-correct paths:
        #   1. substrate-anchored: resume a durable UUID across restarts (the
        #      long-lived-resident pattern).
        #   2. declared lineage: fresh mint that names a real causal parent.
        #   3. fresh new_session (default): a fresh process-instance is a fresh
        #      agent; co-location is not lineage.
        # Either way we capture client_session_id so later calls stay one
        # trajectory (strict-identity requires a caller-proven binding).
        if identity.agent_uuid:
            return session.capture(
                mcp_tools["identity"](agent_uuid=identity.agent_uuid, resume=True)
            )
        kwargs = dict(name=name, force_new=True, spawn_reason=identity.spawn_reason)
        if identity.parent_agent_id:
            kwargs["parent_agent_id"] = identity.parent_agent_id
        return session.capture(mcp_tools["onboard"](**kwargs))

    @tool
    def checkin(description: str, complexity: float, confidence: float = 0.7) -> str:
        """Report what you did to governance. Returns EISV state and verdict.
        Args:
            description: What you just did or accomplished
            complexity: How complex the task was, from 0.0 (trivial) to 1.0 (very hard)
            confidence: How confident you are in your work, from 0.0 to 1.0 (default 0.7)
        """
        return mcp_tools["sync_state"](**session.bind(
            response_text=description,
            complexity=complexity,
            confidence=confidence,
        ))

    @tool
    def get_metrics() -> str:
        """Get your current EISV state vector, coherence, risk score, and verdict."""
        return mcp_tools["check_working_state"](**session.bind(include_state="true", lite="true"))

    @tool
    def search_knowledge(query: str) -> str:
        """Search the shared knowledge graph for existing findings and insights.
        Args:
            query: What to search for (e.g. 'WiFi reliability', 'test failures')
        """
        return mcp_tools["knowledge"](**session.bind(action="search", query=query, limit=5))

    @tool
    def save_note(summary: str, tags: str = "") -> str:
        """Save a finding or insight to the knowledge graph.
        Args:
            summary: A concise description of the finding
            tags: Comma-separated tags (e.g. 'ollama,mcp,bridge')
        """
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        return mcp_tools["leave_note"](**session.bind(summary=summary, tags=tag_list))

    @tool
    def list_agents() -> str:
        """List all registered governance agents and their current status."""
        return mcp_tools["agent"](**session.bind(action="list"))

    @tool
    def ask_tool(tool_name: str) -> str:
        """Get full details and usage examples for a specific governance tool.
        Args:
            tool_name: Name of the tool to describe (e.g. 'dialectic', 'observe')
        """
        return mcp_tools["describe_tool"](tool_name=tool_name)

    return [
        check_health, register_agent, checkin, get_metrics,
        search_knowledge, save_note, list_agents, ask_tool,
    ]


def build_agent(model_id: str, ollama_url: str, wrapper_tools: list):
    """Create a CodeAgent connected to Ollama with simplified tools."""
    model = OpenAIServerModel(
        model_id=model_id,
        api_base=ollama_url,
        api_key="ollama",
    )
    return CodeAgent(
        tools=wrapper_tools,
        model=model,
        max_steps=6,
        instructions=INSTRUCTIONS,
    )


def run_interactive(agent):
    """Interactive REPL loop."""
    print("\n--- UNITARES Governance Shell (Ollama) ---")
    print("Commands: 'quit' to exit, 'onboard' to register, or ask anything.\n")

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Bye.")
            break

        if user_input.lower() == "onboard":
            user_input = "Call register_agent('ollama-local') to register as a new agent."

        try:
            result = agent.run(user_input)
            print(f"\n{result}\n")
        except Exception as e:
            print(f"\n[error] {e}\n")


def run_single(agent, task: str):
    """Run a single task and exit."""
    try:
        result = agent.run(task)
        print(result)
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Ollama-UNITARES Bridge")
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"Ollama model to use (default: {DEFAULT_MODEL}; env UNITARES_LLM_MODEL)",
    )
    parser.add_argument(
        "--mcp-url", default=DEFAULT_MCP_URL,
        help=f"Governance MCP URL (default: {DEFAULT_MCP_URL}; env UNITARES_MCP_URL)",
    )
    parser.add_argument(
        "--ollama-url", default=DEFAULT_OLLAMA_URL,
        help=f"Ollama OpenAI-compat API (default: {DEFAULT_OLLAMA_URL}; env UNITARES_OLLAMA_URL)",
    )
    parser.add_argument(
        "--task", default=None,
        help="Single task to run (omit for interactive mode)",
    )
    parser.add_argument(
        "--max-steps", type=int, default=6,
        help="Max agent reasoning steps (default: 6)",
    )
    parser.add_argument(
        "--agent-uuid", default=None,
        help="Substrate-anchored continuity: resume a fixed identity by UUID "
             "(env UNITARES_AGENT_UUID). Use only for a durable persistent agent.",
    )
    parser.add_argument(
        "--parent-agent-id", default=None,
        help="Declare causal lineage to a prior agent's UUID "
             "(env UNITARES_PARENT_AGENT_ID). Pair with a causal --spawn-reason.",
    )
    parser.add_argument(
        "--spawn-reason", default=None,
        help="One of " + "|".join(VALID_SPAWN_REASONS) +
             " (default new_session; env UNITARES_SPAWN_REASON).",
    )
    args = parser.parse_args()

    identity = IdentityConfig.from_args(args)

    print(f"Connecting to UNITARES MCP at {args.mcp_url} ...")
    print(f"Using Ollama model: {args.model} via {args.ollama_url}")
    print(f"Identity posture: {identity.describe()}")

    with ToolCollection.from_mcp(
        {"url": args.mcp_url, "transport": "streamable-http"},
        trust_remote_code=True,
        structured_output=False,
    ) as tc:
        mcp_tools = {t.name: t for t in tc.tools}
        print(f"Connected. {len(mcp_tools)} MCP tools available.")

        session = Session()
        wrapper_tools = make_wrappers(mcp_tools, identity, session)
        print(f"Exposing {len(wrapper_tools)} simplified tools to model.")

        agent = build_agent(args.model, args.ollama_url, wrapper_tools)
        agent.max_steps = args.max_steps

        if args.task:
            run_single(agent, args.task)
        else:
            run_interactive(agent)


if __name__ == "__main__":
    main()
