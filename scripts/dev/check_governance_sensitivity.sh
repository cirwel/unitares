#!/usr/bin/env bash
# check_governance_sensitivity.sh — advisory detector for governance-weakening
# diffs (unitares #1671, threat-model "the governor is authored by the
# governed" subsection).
#
# Modes:
#   --check          verify every manifest path still exists (run in CI so a
#                    refactor that moves a sensitive file cannot silently
#                    detach it from the manifest)
#   --diff <base>    list manifest entries touched by the diff base...HEAD.
#                    Whole-file entries (symbol_regex `-`) match on any
#                    change to the file; symbol entries match only when an
#                    added/removed line hits the regex. Prints one line per
#                    hit: path<TAB>why. Exit 0 always — this is an advisory,
#                    not a gate: a gate that fights the maintainer gets
#                    routed around, which reproduces the problem one layer up.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MANIFEST="$ROOT/scripts/dev/governance_sensitivity_manifest.tsv"

manifest_rows() {
    grep -v '^#' "$MANIFEST" | grep -v '^[[:space:]]*$'
}

case "${1:-}" in
--check)
    missing=0
    while IFS=$'\t' read -r path _symbol _why; do
        if [ ! -e "$ROOT/$path" ]; then
            echo "governance_sensitivity_manifest.tsv names a missing path: $path" >&2
            missing=1
        fi
    done < <(manifest_rows)
    if [ "$missing" -ne 0 ]; then
        echo "A sensitive file moved without a manifest update — re-anchor the entry." >&2
        exit 1
    fi
    echo "governance sensitivity manifest paths all exist."
    ;;
--diff)
    base="${2:?usage: $0 --diff <base-ref>}"
    changed="$(git -C "$ROOT" diff --name-only "$base"...HEAD)"
    while IFS=$'\t' read -r path symbol why; do
        printf '%s\n' "$changed" | grep -qxF "$path" || continue
        if [ "$symbol" = "-" ]; then
            printf '%s\t%s\n' "$path" "$why"
        elif git -C "$ROOT" diff -U0 "$base"...HEAD -- "$path" \
                | grep -E '^[+-][^+-]' | grep -Eq "$symbol"; then
            printf '%s\t%s\n' "$path" "$why"
        fi
    done < <(manifest_rows)
    ;;
*)
    echo "usage: $0 --check | --diff <base-ref>" >&2
    exit 2
    ;;
esac
