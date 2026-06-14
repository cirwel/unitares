#!/usr/bin/env python3
"""Build or post a canonical dogfood friction finding envelope.

CI jobs and scheduled dogfood probes can use this script to turn a JSON
observation into the same `/api/findings` event shape used by resident agents.
The default is dry-run JSON output; pass ``--post`` only when a governance HTTP
surface is available and the job is meant to emit the finding.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.common.dogfood_friction import (  # noqa: E402
    DEFAULT_AGENT_ID,
    DEFAULT_AGENT_NAME,
    DogfoodFrictionValidationError,
    build_dogfood_friction_event,
    post_dogfood_friction,
)


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    """Load the input JSON payload from an arg string, a file, or stdin."""
    if args.input_json:
        raw = args.input_json
    elif args.input_file:
        raw = Path(args.input_file).read_text()
    else:
        raw = sys.stdin.read()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DogfoodFrictionValidationError(f"invalid JSON input: {exc}") from exc
    if not isinstance(data, dict):
        raise DogfoodFrictionValidationError("dogfood friction input must be a JSON object")
    return data


def _parser() -> argparse.ArgumentParser:
    """Create the CLI parser."""
    parser = argparse.ArgumentParser(
        description="Build or post a UNITARES dogfood friction finding event.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--input-json",
        help="Dogfood friction JSON object as a string. Defaults to stdin when omitted.",
    )
    source.add_argument(
        "--input-file",
        help="Path to a dogfood friction JSON object.",
    )
    parser.add_argument(
        "--post",
        action="store_true",
        help="Post to /api/findings instead of printing the dry-run event payload.",
    )
    parser.add_argument(
        "--agent-id",
        default=DEFAULT_AGENT_ID,
        help="Agent id to attribute to the finding poster.",
    )
    parser.add_argument(
        "--agent-name",
        default=DEFAULT_AGENT_NAME,
        help="Agent display name to attribute to the finding poster.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the dogfood friction finding CLI."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        payload = _load_payload(args)
        if args.post:
            posted = post_dogfood_friction(
                payload,
                agent_id=args.agent_id,
                agent_name=args.agent_name,
            )
            print(json.dumps({"posted": posted}, sort_keys=True))
            return 0 if posted else 1

        event = build_dogfood_friction_event(
            payload,
            agent_id=args.agent_id,
            agent_name=args.agent_name,
        )
        print(json.dumps(event, indent=2, sort_keys=True))
        return 0
    except DogfoodFrictionValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
