#!/usr/bin/env bash
# wave3-shadow-replay.sh — Wave 3 §8.3 load-amplification harness (entrypoint).
#
# Replays captured production traffic at an amplified rate (default 2×)
# against the Wave 3 shadow path. The 7-day zero-divergence clock starts
# AFTER a replay completes with zero divergence events (§8.3).
#
# The shadow path itself ships with the Wave 3 implementation, NOT with this
# prereq PR — until then this harness is runnable only against a test target.
# It therefore REQUIRES an explicit --target; there is no default, so it can
# never accidentally replay amplified traffic at the live MCP.
#
# Capture format (one JSON object per line):
#   {"ts": "<ISO8601>", "method": "POST", "path": "/v1/...", "body": {...}}
#
# Usage:
#   scripts/ops/wave3-shadow-replay.sh --capture <file.jsonl> --target <base-url> [--rate 2.0] [--dry-run]
set -euo pipefail
exec python3 "$(cd "$(dirname "$0")" && pwd)/wave3_shadow_replay.py" "$@"
