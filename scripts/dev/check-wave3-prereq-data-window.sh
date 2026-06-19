#!/usr/bin/env bash
# check-wave3-prereq-data-window.sh — Wave 3 §14 PR #8b data-window gate.
#
# RFC: docs/proposals/beam-wave-3-handler-dispatch.md §14 row 8b.
# The gate is intentionally mechanical: PR #8b cannot merge until the
# lease-plane measurement channel has produced >=14 days of
# measurement.lease_plane.request rows in audit.coordination_measurements.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

exec python3 "$ROOT/scripts/dev/wave3_prereq_data_window.py" "$@"
