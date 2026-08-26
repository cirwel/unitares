#!/usr/bin/env python3
"""Degeneracy check for the effort-profile label channel — REFUTED, no authority.

REFUTED 2026-08-26 by three adversarial passes. See section 0 of
docs/proposals/eisv-effort-profile-channel-v0.md. In short:

  * The authority split this implements does not hold. `turns` was granted KILL
    authority for being judgement-free; `_is_human_turn` embeds four judgement
    calls and, measured on a real corpus, 55% of what it counts as human turns
    are harness-injected `task-notification` wakes. Injected `system-reminder`
    blocks arrive as `type: "attachment"` records this never sees.
  * The thresholds cannot fire on any plausible corpus of agent sessions.
  * `read_session` drops zero-turn and single-timestamp sessions, an undeclared
    selection rule that removes the shortest sessions and so biases away from
    the `median turns <= 2` kill condition.
  * CYCLE_MARKERS scored zero precision and zero recall on the only real corpus.

The KILL and PASS verdicts below are therefore NOT authoritative and must not be
cited as closing or licensing anything. The module is retained as a worked
example of a pre-registered check designed to pass. Do not repair it in place --
a replacement needs a validated turn classifier and a dependent variable that is
not constructed from the predictor's inputs.

Original docstring follows.

Degeneracy check for the effort-profile label channel (read-only, local files).

Implements §14 of docs/proposals/eisv-effort-profile-channel-v0.md, whose
thresholds were committed before any corpus was measured. This script does not
re-derive them and must not be edited to move them; a threshold change is a
change to that section, reviewed as such.

What this asks: does the effort profile have any variance to model at all? It is
upstream of every other question in that document. If sessions do not differ
from one another, no judge, no EISV model, and no answer to D7 rescues the
channel.

Deliberate non-capabilities, each load-bearing:

  * No database. It reads harness-local transcript files and nothing else, so it
    cannot reach audit.outcome_events or core.agent_state even by mistake. That
    is what keeps it outside the 2026-12-01 registered read's embargo.
  * No model. turns and duration are message counts and timestamps; the cycle
    markers are a frozen literal set below. Nothing here calls inference.
  * No network.

Split of authority (§14): turns and duration need no judgement, so they can
KILL. Cycles needs a marker set, a marker set is a judgement, so it cannot --
near-total zeros beside varying turns and duration is a statement about the
instrument (MARKER-SET-BLIND), not about the world.

Usage:
    python3 scripts/analysis/effort_profile_degeneracy.py
    python3 scripts/analysis/effort_profile_degeneracy.py --root ~/.claude/projects --json out.json
"""
from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime
from pathlib import Path

# --- Thresholds. Mirrors §14; do not edit here to change a verdict. -----------
MIN_SESSIONS = 30
MIN_IDENTITIES = 3
MIN_SESSIONS_PER_IDENTITY = 10
KILL_MEDIAN_TURNS = 2          # KILL if median turns <= this
KILL_DURATION_CV = 0.25        # KILL if coefficient of variation < this
MARKER_BLIND_ZERO_FRACTION = 0.90

# --- Frozen cycle markers. Changing this set is visible as a diff (§14). ------
# Matched case-insensitively against human-authored turn text. Deliberately
# conservative: it under-counts implicit re-direction, and is reported as a
# floor rather than as a measurement of re-direction.
CYCLE_MARKERS = (
    "actually", "instead", "not quite", "that's not", "thats not",
    "wrong", "revert", "undo", "try again", "redo", "no -", "no,",
    "isn't right", "isnt right", "don't", "dont ", "stop ", "rather",
)


def _parse_ts(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_human_turn(rec):
    """A user record whose content is text, not a tool_result envelope.

    In Claude Code transcripts a `type: user` record is usually a tool result.
    Only string content is authored text. Harness-injected reminders are
    excluded so automated wakes do not inflate the turn count.
    """
    if rec.get("type") != "user" or rec.get("isSidechain"):
        return None
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, str):
        return None
    stripped = content.strip()
    if not stripped or stripped.startswith(("<system-reminder>", "[Request interrupted")):
        return None
    return stripped


def read_session(path: Path):
    """Return one session's effort profile, or None if it carries no human turn."""
    turns, stamps, cycles = 0, [], 0
    for line in path.read_text(errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        ts = _parse_ts(rec.get("timestamp"))
        if ts:
            stamps.append(ts)
        text = _is_human_turn(rec)
        if text is None:
            continue
        turns += 1
        low = text.lower()
        if any(marker in low for marker in CYCLE_MARKERS):
            cycles += 1
    if not turns or len(stamps) < 2:
        return None
    return {
        "session": path.stem,
        "identity_proxy": path.parent.name,
        "turns": turns,
        "duration_s": (max(stamps) - min(stamps)).total_seconds(),
        "cycles": cycles,
    }


def _cv(values):
    mean = statistics.fmean(values)
    if mean <= 0 or len(values) < 2:
        return 0.0
    return statistics.stdev(values) / mean


def _iqr(values):
    if len(values) < 4:
        return 0.0
    q = statistics.quantiles(values, n=4)
    return q[2] - q[0]


def evaluate(sessions):
    """Apply §14 in order: support first, then the rule-free kills, then cycles."""
    findings, verdicts = [], []
    n = len(sessions)
    by_identity = {}
    for s in sessions:
        by_identity.setdefault(s["identity_proxy"], []).append(s)
    qualifying = [k for k, v in by_identity.items() if len(v) >= MIN_SESSIONS_PER_IDENTITY]

    # Support gate. Below it, no degeneracy claim runs in either direction.
    if n < MIN_SESSIONS or len(by_identity) < MIN_IDENTITIES:
        verdicts.append("UNDERPOWERED")
        findings.append(
            f"{n} sessions across {len(by_identity)} identity proxies; "
            f"floor is {MIN_SESSIONS} sessions and {MIN_IDENTITIES} identities. "
            "No degeneracy claim is made in either direction."
        )
        return {"verdicts": verdicts, "findings": findings, "kill": False}

    turns = [s["turns"] for s in sessions]
    durations = [s["duration_s"] for s in sessions]
    median_turns, turns_iqr, duration_cv = statistics.median(turns), _iqr(turns), _cv(durations)

    kill = False
    if median_turns <= KILL_MEDIAN_TURNS:
        kill = True
        findings.append(f"median turns {median_turns} <= {KILL_MEDIAN_TURNS}")
    if turns_iqr == 0:
        kill = True
        findings.append("interquartile range of turns is 0")
    if duration_cv < KILL_DURATION_CV:
        kill = True
        findings.append(f"duration coefficient of variation {duration_cv:.3f} < {KILL_DURATION_CV}")

    if kill:
        verdicts.append("KILL")
        findings.append(
            "The rule-free quantities are degenerate. Per §14 this closes the "
            "channel; it does not defer it pending tooling."
        )
    else:
        verdicts.append("PASS")
        findings.append(
            f"median turns {median_turns}, turns IQR {turns_iqr}, duration CV "
            f"{duration_cv:.3f}. Variance exists. This licenses continuing to "
            "ask; it is not evidence for any answer."
        )

    if len(qualifying) < MIN_IDENTITIES:
        verdicts.append("SINGLE-AGENT")
        findings.append(
            f"{len(qualifying)} identity proxies have >= {MIN_SESSIONS_PER_IDENTITY} "
            "sessions. No claim about between-agent spread; within-agent variance "
            "is not substituted for it."
        )

    # Cycles: reported, never granted kill authority.
    zero_fraction = sum(1 for s in sessions if s["cycles"] == 0) / n
    if zero_fraction >= MARKER_BLIND_ZERO_FRACTION and not kill:
        verdicts.append("MARKER-SET-BLIND")
        findings.append(
            f"{zero_fraction:.0%} of sessions show zero cycles while turns and "
            "duration vary. This is a finding about the marker set, not about "
            "effort. It licenses revising the markers and nothing else."
        )
    return {"verdicts": verdicts, "findings": findings, "kill": kill,
            "cycle_zero_fraction": zero_fraction}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="~/.claude/projects",
                    help="Directory of harness transcripts (default: ~/.claude/projects)")
    ap.add_argument("--json", help="Write the full result to this path")
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    if not root.exists():
        raise SystemExit(f"transcript root not found: {root}")

    sessions = [s for s in (read_session(p) for p in sorted(root.rglob("*.jsonl"))) if s]
    result = evaluate(sessions)
    result["corpus"] = {
        "root": str(root),
        "sessions": len(sessions),
        "identity_proxies": len({s["identity_proxy"] for s in sessions}),
        "note": ("identity_proxy is the transcript's project directory. Claude Code "
                 "transcripts carry no agent identity, so between-agent spread is not "
                 "derivable from this corpus alone -- see T6."),
    }
    result["thresholds"] = {
        "min_sessions": MIN_SESSIONS, "min_identities": MIN_IDENTITIES,
        "min_sessions_per_identity": MIN_SESSIONS_PER_IDENTITY,
        "kill_median_turns": KILL_MEDIAN_TURNS, "kill_duration_cv": KILL_DURATION_CV,
        "marker_blind_zero_fraction": MARKER_BLIND_ZERO_FRACTION,
        "committed": "2026-08-26, before any corpus was measured",
    }

    print(f"corpus: {len(sessions)} sessions, "
          f"{result['corpus']['identity_proxies']} identity proxies, root {root}")
    print("REFUTED INSTRUMENT - verdicts below are not authoritative; "
          "see section 0 of the effort-profile channel proposal")
    print(f"verdict: {' + '.join(result['verdicts'])}")
    for f in result["findings"]:
        print(f"  - {f}")

    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2, default=str))
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
