#!/usr/bin/env python3
"""
Ollama-UNITARES Bridge
Connects local Ollama models to the UNITARES governance MCP server via smolagents.

The raw MCP tool schemas have too many optional parameters for smaller models.
This script creates simplified wrapper tools with minimal signatures, then
exposes them to the model via CodeAgent (which writes Python, not JSON tool calls).

Usage:
    python3 ollama_bridge.py                          # interactive, gemma4:latest
    python3 ollama_bridge.py --model gemma4:31b       # use a specific model
    python3 ollama_bridge.py --task "check health"    # single task, then exit
"""

import argparse
import sys

from smolagents import tool, ToolCollection, OpenAIServerModel, CodeAgent

GOVERNANCE_URL = "http://127.0.0.1:8767/mcp/"
OLLAMA_URL = "http://127.0.0.1:11434/v1"

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


def make_wrappers(mcp_tools: dict):
    """Create simplified tool wrappers over the raw MCP tools."""

    @tool
    def check_health() -> str:
        """Check UNITARES governance system health, database, and uptime."""
        return mcp_tools["health_check"](lite="true")

    @tool
    def register_agent(name: str) -> str:
        """Register as a new governance agent. Call this once at the start of a session.
        Args:
            name: Display name for this agent (e.g. 'ollama-local')
        """
        # force_new=True creates a fresh UUID; `name` is stored as the
        # cosmetic label (not used for resolution — name-claim removed
        # 2026-04-17). Persistent Ollama sessions should cache the returned
        # agent_uuid and call identity(agent_uuid=..., resume=true) next time.
        return mcp_tools["onboard"](name=name, force_new=True, spawn_reason="explicit")

    @tool
    def checkin(description: str, complexity: float, confidence: float = 0.7) -> str:
        """Report what you did to governance. Returns EISV state and verdict.
        Args:
            description: What you just did or accomplished
            complexity: How complex the task was, from 0.0 (trivial) to 1.0 (very hard)
            confidence: How confident you are in your work, from 0.0 to 1.0 (default 0.7)
        """
        return mcp_tools["process_agent_update"](
            response_text=description,
            complexity=complexity,
            confidence=confidence,
        )

    @tool
    def get_metrics() -> str:
        """Get your current EISV state vector, coherence, risk score, and verdict."""
        return mcp_tools["get_governance_metrics"]()

    @tool
    def search_knowledge(query: str) -> str:
        """Search the shared knowledge graph for existing findings and insights.
        Args:
            query: What to search for (e.g. 'WiFi reliability', 'test failures')
        """
        return mcp_tools["knowledge"](action="search", query=query, limit=5)

    @tool
    def save_note(summary: str, tags: str = "") -> str:
        """Save a finding or insight to the knowledge graph.
        Args:
            summary: A concise description of the finding
            tags: Comma-separated tags (e.g. 'ollama,mcp,bridge')
        """
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        return mcp_tools["leave_note"](summary=summary, tags=tag_list)

    @tool
    def list_agents() -> str:
        """List all registered governance agents and their current status."""
        return mcp_tools["agent"](action="list")

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


def build_agent(model_id: str, wrapper_tools: list):
    """Create a CodeAgent connected to Ollama with simplified tools."""
    model = OpenAIServerModel(
        model_id=model_id,
        api_base=OLLAMA_URL,
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
        "--model", default="gemma4:latest",
        help="Ollama model to use (default: gemma4:latest)",
    )
    parser.add_argument(
        "--task", default=None,
        help="Single task to run (omit for interactive mode)",
    )
    parser.add_argument(
        "--max-steps", type=int, default=6,
        help="Max agent reasoning steps (default: 6)",
    )
    args = parser.parse_args()

    print(f"Connecting to UNITARES MCP at {GOVERNANCE_URL} ...")
    print(f"Using Ollama model: {args.model}")

    with ToolCollection.from_mcp(
        {"url": GOVERNANCE_URL, "transport": "streamable-http"},
        trust_remote_code=True,
        structured_output=False,
    ) as tc:
        mcp_tools = {t.name: t for t in tc.tools}
        print(f"Connected. {len(mcp_tools)} MCP tools available.")

        wrapper_tools = make_wrappers(mcp_tools)
        print(f"Exposing {len(wrapper_tools)} simplified tools to model.")

        agent = build_agent(args.model, wrapper_tools)
        agent.max_steps = args.max_steps

        if args.task:
            run_single(agent, args.task)
        else:
            run_interactive(agent)


if __name__ == "__main__":
    main()
