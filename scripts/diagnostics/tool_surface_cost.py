#!/usr/bin/env python3
"""What each GOVERNANCE_TOOL_MODE profile costs on the wire.

`count_tools.py` answers "how many tools are there". This answers the question
a context budget actually asks: **how much does advertising them cost**, in the
bytes a client receives from `tools/list` before the agent has decided it wants
any of them.

The two are not the same question, and the difference is where the surprises
live. Measured 2026-09-08, the profile named `lite` costs 4.8x the profile
named `minimal` and 1.7x the default `standard` -- the ladder is ordered
minimal < standard < lite < full, so `lite` is a name for the second-widest
surface. Nothing in the repo could see that before this script, because every
existing instrument counted names.

Bytes are MEASURED. Tokens are ESTIMATED by dividing bytes by
`--bytes-per-token` (default 4), which is a rough heuristic for JSON, not a
tokenizer result; every output labels them as estimates and reports the
divisor. Do not quote the token figure as a measurement.

Counting requires the runtime dependency tree, because the advertised surface
is built by importing the handler package. Where those dependencies are absent
the cost is *unavailable* -- a different fact from "this surface is free" --
and every output mode keeps the two distinguishable, following the same rule
as `count_tools.py`.

Usage:
    python3 scripts/diagnostics/tool_surface_cost.py                 # all profiles
    python3 scripts/diagnostics/tool_surface_cost.py --mode standard # per-tool breakdown
    python3 scripts/diagnostics/tool_surface_cost.py --mode standard --params
    python3 scripts/diagnostics/tool_surface_cost.py --json
    python3 scripts/diagnostics/tool_surface_cost.py --check-ladder  # CI invariant
"""

from __future__ import annotations

import copy
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

#: Printed in plain mode when the advertised surface could not be built. Chosen
#: to be non-numeric so a caller that captures stdout cannot render it as a cost.
UNAVAILABLE_SENTINEL = "unavailable"

#: Exit status for `--require-registry` when the surface cannot be built.
EXIT_REGISTRY_UNAVAILABLE = 2

#: Exit status for a `--check-ladder` violation.
EXIT_LADDER_VIOLATION = 3

#: Bytes per token. A heuristic for JSON schema text, NOT a tokenizer result.
#: Every rendering that uses it says so and prints the divisor.
DEFAULT_BYTES_PER_TOKEN = 4

#: The agent-facing profiles, in intended order from narrowest to widest. The
#: operator profiles are deliberately absent: they are a different axis (who is
#: calling), not a rung on this ladder, and comparing them to it is meaningless.
LADDER = ("minimal", "standard", "lite", "full")

#: Profiles reported by default. The operator profiles are measured on request
#: via --mode but do not clutter the ladder comparison.
ALL_PROFILES = LADDER + ("operator_readonly", "operator_recovery")


@dataclass(frozen=True)
class ToolCost:
    """The wire cost of one advertised tool."""

    name: str
    total_bytes: int
    description_bytes: int
    schema_bytes: int
    param_count: int
    required: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProfileCost:
    """The wire cost of one profile, or an explicit statement that it is unknown.

    `available=False` means the surface could not be built here; `total_bytes`
    is then None rather than 0, so no caller can read the absence as a cheap
    surface.
    """

    mode: str
    available: bool
    total_bytes: Optional[int] = None
    tools: List[ToolCost] = field(default_factory=list)
    reason: Optional[str] = None

    @property
    def tool_count(self) -> Optional[int]:
        return len(self.tools) if self.available else None

    def estimated_tokens(self, bytes_per_token: int = DEFAULT_BYTES_PER_TOKEN) -> Optional[int]:
        if not self.available or self.total_bytes is None:
            return None
        return self.total_bytes // max(1, bytes_per_token)


def _wire_bytes(payload: Any) -> int:
    """Bytes of `payload` serialized the way a transport sends it.

    Separators are the compact form so the measurement reflects content, not a
    particular encoder's whitespace.
    """
    return len(json.dumps(payload, separators=(",", ":"), default=str))


def _input_schema(tool: Any) -> dict:
    """The tool's input schema across both supported mcp majors.

    1.x exposes `inputSchema`; 2.x renames it `input_schema`. Reading only one
    silently measures zero-byte schemas on the other.
    """
    schema = getattr(tool, "inputSchema", None)
    if schema is None:
        schema = getattr(tool, "input_schema", None)
    return schema or {}


def measure_tool(tool: Any) -> ToolCost:
    """The wire cost of a single advertised tool definition."""
    description = tool.description or ""
    schema = _input_schema(tool)
    properties = schema.get("properties") if isinstance(schema, dict) else None
    required = schema.get("required") if isinstance(schema, dict) else None
    return ToolCost(
        name=tool.name,
        total_bytes=_wire_bytes(
            {"name": tool.name, "description": description, "inputSchema": schema}
        ),
        description_bytes=len(description),
        schema_bytes=_wire_bytes(schema),
        param_count=len(properties) if isinstance(properties, dict) else 0,
        required=list(required) if isinstance(required, list) else [],
    )


def measure_profile(mode: str) -> ProfileCost:
    """Measure one profile, reporting unavailability as a state rather than a zero."""
    try:
        from src.interface_contract import get_public_tool_definitions
    except ModuleNotFoundError as exc:
        return ProfileCost(mode=mode, available=False, reason=str(exc))

    try:
        definitions = get_public_tool_definitions(mode)
    except ModuleNotFoundError as exc:
        return ProfileCost(mode=mode, available=False, reason=str(exc))

    tools = sorted(
        (measure_tool(tool) for tool in definitions),
        key=lambda cost: cost.total_bytes,
        reverse=True,
    )
    return ProfileCost(
        mode=mode,
        available=True,
        total_bytes=sum(cost.total_bytes for cost in tools),
        tools=tools,
    )


def measure_profiles(modes: tuple[str, ...] = ALL_PROFILES) -> Dict[str, ProfileCost]:
    return {mode: measure_profile(mode) for mode in modes}


def _without_property_titles(schema: dict) -> dict:
    """The schema with every property `title` removed.

    Pydantic emits a `title` for each property that is a titleized copy of the
    key -- `client_session_id` -> "Client Session Id". It carries no
    information the key does not, JSON Schema does not use it for validation,
    and every client pays for it on every listing.
    """
    out = copy.deepcopy(schema)
    for body in (out.get("properties") or {}).values():
        if isinstance(body, dict):
            body.pop("title", None)
    return out


def _without_null_unions(schema: dict) -> dict:
    """The schema with `anyOf: [{type: X}, {type: "null"}]` flattened to X.

    Measured for comparison ONLY. Unlike a title, this is a real contract
    change: after flattening, an explicit `null` no longer validates. Reported
    here so the size of the lever is known; do not apply it without deciding
    that question separately.
    """
    out = copy.deepcopy(schema)
    for body in (out.get("properties") or {}).values():
        if not isinstance(body, dict) or "anyOf" not in body:
            continue
        options = [
            option
            for option in body["anyOf"]
            if isinstance(option, dict) and option.get("type") != "null"
        ]
        if len(options) == 1 and set(options[0]) <= {"type"}:
            body.pop("anyOf")
            body["type"] = options[0]["type"]
    return out


def boilerplate_savings(mode: str) -> Optional[Dict[str, int]]:
    """Bytes recoverable from the STRUCTURAL half of the advertised schemas.

    `src/schema_brief.py` trimmed the prose half (descriptions) in 2026-09.
    This measures the half it deliberately left alone. The two are very
    different propositions and the output keeps them apart:

    - `property_title` is a machine-generated echo of the property name. It is
      removable with no information loss and no contract change.
    - `null_union` is a real narrowing of what validates. Measured, not
      recommended.
    """
    try:
        from src.interface_contract import get_public_tool_definitions
    except ModuleNotFoundError:
        return None

    try:
        definitions = list(get_public_tool_definitions(mode))
    except ModuleNotFoundError:
        return None

    def total(transform) -> int:
        return sum(
            _wire_bytes(
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "inputSchema": transform(_input_schema(tool)),
                }
            )
            for tool in definitions
        )

    baseline = total(lambda schema: schema)
    return {
        "baseline": baseline,
        "property_title": baseline - total(_without_property_titles),
        "null_union": baseline - total(_without_null_unions),
        "both": baseline
        - total(lambda schema: _without_null_unions(_without_property_titles(schema))),
    }


def check_ladder(costs: Dict[str, ProfileCost]) -> List[str]:
    """Violations of the ladder invariant, empty when the ladder holds.

    Two properties are checked, and they fail for different reasons:

    - **Containment**: each rung advertises a superset of the one below it. A
      break here means a capability is reachable on a narrower profile than on
      a wider one, which no operator would predict from the names.
    - **Monotonic weight**: each rung costs more than the one below it. A break
      here means a profile's NAME misdescribes its position, which is what
      `lite` (wider and heavier than `standard`) does today.

    A profile that could not be measured is skipped, never treated as zero.
    """
    violations: List[str] = []
    try:
        from src.tool_modes import get_tools_for_mode
    except ModuleNotFoundError as exc:
        return [f"ladder unchecked: {exc}"]

    rungs = [mode for mode in LADDER if costs.get(mode) and costs[mode].available]
    for narrower, wider in zip(rungs, rungs[1:]):
        narrow_names = get_tools_for_mode(narrower)
        wide_names = get_tools_for_mode(wider)
        missing = narrow_names - wide_names
        if missing:
            violations.append(
                f"containment: {wider!r} does not advertise "
                f"{sorted(missing)} which {narrower!r} does"
            )
        narrow_bytes = costs[narrower].total_bytes or 0
        wide_bytes = costs[wider].total_bytes or 0
        if wide_bytes <= narrow_bytes:
            violations.append(
                f"weight: {wider!r} ({wide_bytes:,} B) does not cost more than "
                f"{narrower!r} ({narrow_bytes:,} B); the ladder order is "
                f"{' < '.join(LADDER)}"
            )
    return violations


def _render_summary(costs: Dict[str, ProfileCost], bytes_per_token: int) -> None:
    print(
        f"{'profile':<20}{'tools':>7}{'bytes':>12}{'~tokens':>10}   "
        f"vs narrowest"
    )
    print("-" * 66)
    baseline = None
    for mode in ALL_PROFILES:
        cost = costs.get(mode)
        if cost is None:
            continue
        if not cost.available:
            print(f"{mode:<20}{UNAVAILABLE_SENTINEL:>7}{'':>12}{'':>10}")
            continue
        if baseline is None and mode in LADDER:
            baseline = cost.total_bytes or 1
        ratio = (
            f"{(cost.total_bytes or 0) / baseline:.1f}x"
            if baseline and mode in LADDER
            else ""
        )
        print(
            f"{mode:<20}{cost.tool_count:>7}{cost.total_bytes:>12,}"
            f"{cost.estimated_tokens(bytes_per_token):>10,}   {ratio}"
        )
    print()
    print(
        f"Bytes are measured. Tokens are ESTIMATED at {bytes_per_token} bytes/token "
        "(a heuristic for JSON, not a tokenizer)."
    )


def _render_profile(cost: ProfileCost, bytes_per_token: int, show_params: bool) -> None:
    if not cost.available:
        print(f"{cost.mode}: {UNAVAILABLE_SENTINEL} ({cost.reason})")
        return
    total = cost.total_bytes or 1
    print(
        f"{cost.mode}: {cost.tool_count} tools, {total:,} B "
        f"(~{cost.estimated_tokens(bytes_per_token):,} est. tokens "
        f"@ {bytes_per_token} B/token)"
    )
    print()
    print(f"{'bytes':>9}{'share':>7}{'params':>8}  tool")
    print("-" * 60)
    for tool in cost.tools:
        print(
            f"{tool.total_bytes:>9,}{100 * tool.total_bytes // total:>6}%"
            f"{tool.param_count:>8}  {tool.name}"
        )
    if show_params:
        _render_params(cost)


def _render_params(cost: ProfileCost) -> None:
    """Per-parameter cost for each tool in the profile.

    Parameter breadth, not tool count, is what a wide schema actually charges
    for: a router with fifty optional parameters costs more than four task
    verbs with six each.
    """
    from src.interface_contract import get_public_tool_definitions

    schemas = {
        tool.name: _input_schema(tool)
        for tool in get_public_tool_definitions(cost.mode)
    }
    for tool in cost.tools:
        properties = schemas.get(tool.name, {}).get("properties") or {}
        if not properties:
            continue
        print()
        print(f"--- {tool.name}: {tool.param_count} params, required={tool.required or []}")
        rows = sorted(
            ((_wire_bytes({name: body}), name) for name, body in properties.items()),
            reverse=True,
        )
        for size, name in rows:
            print(f"  {size:>6} B  {name}")


def _render_boilerplate(modes, bytes_per_token: int) -> None:
    print(
        "Structural boilerplate in the advertised schemas. `title` is a "
        "titleized copy of\nthe property key and carries no information; "
        "dropping it changes no contract.\n`anyOf` null-union flattening is "
        "measured for scale only -- it DOES change what\nvalidates, and is not "
        "a recommendation.\n"
    )
    print(
        f"{'profile':<20}{'baseline':>11}{'title':>16}{'null-union':>16}{'both':>16}"
    )
    print("-" * 79)
    for mode in modes:
        savings = boilerplate_savings(mode)
        if savings is None:
            print(f"{mode:<20}{UNAVAILABLE_SENTINEL:>11}")
            continue
        base = savings["baseline"] or 1

        def cell(key: str) -> str:
            return f"{savings[key]:,} ({100 * savings[key] // base}%)"

        print(
            f"{mode:<20}{base:>11,}{cell('property_title'):>16}"
            f"{cell('null_union'):>16}{cell('both'):>16}"
        )
    print()
    print(
        "Percentages: divide by baseline. Tokens estimated at "
        f"{bytes_per_token} B/token."
    )


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Measure the wire cost of each GOVERNANCE_TOOL_MODE profile"
    )
    parser.add_argument(
        "--mode",
        help="Show a per-tool breakdown for one profile instead of the summary",
    )
    parser.add_argument(
        "--params",
        action="store_true",
        help="With --mode, also break each tool down by parameter",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--bytes-per-token",
        type=int,
        default=DEFAULT_BYTES_PER_TOKEN,
        help=f"Divisor for the token ESTIMATE (default {DEFAULT_BYTES_PER_TOKEN})",
    )
    parser.add_argument(
        "--boilerplate",
        action="store_true",
        help=(
            "Report bytes recoverable from structural schema boilerplate "
            "(property titles, null unions) rather than from prose"
        ),
    )
    parser.add_argument(
        "--check-ladder",
        action="store_true",
        help=(
            "Verify the profile ladder: each rung a superset of the one below "
            "it, and heavier. Exits non-zero on a violation."
        ),
    )
    parser.add_argument(
        "--require-registry",
        action="store_true",
        help=(
            "Exit non-zero when the advertised surface cannot be built. Use "
            "where dependencies ARE installed, so unavailability is a real "
            "breakage rather than an accepted degradation."
        ),
    )
    args = parser.parse_args()

    modes = (args.mode,) if args.mode else ALL_PROFILES
    costs = measure_profiles(tuple(modes))
    unavailable = [cost for cost in costs.values() if not cost.available]

    for cost in unavailable:
        print(
            f"WARNING: tool surface unavailable for {cost.mode!r} ({cost.reason})",
            file=sys.stderr,
        )

    violations = check_ladder(costs) if args.check_ladder else []

    if args.json:
        print(
            json.dumps(
                {
                    "bytes_per_token": args.bytes_per_token,
                    "tokens_are_estimates": True,
                    "profiles": {
                        mode: {
                            "available": cost.available,
                            "reason": cost.reason,
                            "tool_count": cost.tool_count,
                            "total_bytes": cost.total_bytes,
                            "estimated_tokens": cost.estimated_tokens(
                                args.bytes_per_token
                            ),
                            "tools": [
                                {
                                    "name": tool.name,
                                    "total_bytes": tool.total_bytes,
                                    "description_bytes": tool.description_bytes,
                                    "schema_bytes": tool.schema_bytes,
                                    "param_count": tool.param_count,
                                    "required": tool.required,
                                }
                                for tool in cost.tools
                            ],
                        }
                        for mode, cost in costs.items()
                    },
                    "ladder_violations": violations,
                },
                indent=2,
            )
        )
    elif args.boilerplate:
        _render_boilerplate(modes, args.bytes_per_token)
    elif args.mode:
        _render_profile(costs[args.mode], args.bytes_per_token, args.params)
    else:
        _render_summary(costs, args.bytes_per_token)

    if violations and not args.json:
        print()
        print("LADDER VIOLATIONS:")
        for violation in violations:
            print(f"  - {violation}")

    if violations:
        return EXIT_LADDER_VIOLATION
    if unavailable and args.require_registry:
        return EXIT_REGISTRY_UNAVAILABLE
    return 0


if __name__ == "__main__":
    sys.exit(main())
