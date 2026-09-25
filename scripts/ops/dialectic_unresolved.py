#!/usr/bin/env python3
"""List dialectic reviews that ended with a standing objection nobody answered.

A reviewer that refuses a thesis is the dialectic working. Measured over 30
days, the reviewer returned ``agrees=false`` on 61 of 95 real syntheses, and
every session that ended ``failed`` had a reviewer antithesis on the record --
none failed for want of a reviewer. What fails is the lifecycle around it: the
requesting agent is a short-lived process, so by the time the reviewer objects
and proposes conditions it has exited, the self-clear guard correctly refuses
anyone answering in its place, and the session ages out.

The conditions survive that. Measured 2026-09-09: 55 of 56 sessions that ended
``failed`` in 60 days carry reviewer conditions, averaging 4.3 each -- roughly
240 specific, actionable conditions. And nothing reads them. There is no HTTP
route, no dashboard panel, no digest, and no operator queue keyed on any of
this; they are written to Postgres and never seen.

So this does not try to make sessions resolve. Chasing that means either
keeping agents alive for hours or weakening the guard that stops an agent
clearing its own objection, and both are worse than the problem. It makes the
OUTPUT land: one list, newest first, that an operator surface can print when
there is something to say and stay silent when there is not.

Read-only against the database. Prints text by default, ``--json`` for
machine use.

Acknowledging a review (``ack``)
--------------------------------
Every row here is already ``status='failed'``, and short of a reassign (which
reopens the SAME session id) nothing takes it off the list, so without an escape
hatch the list re-prints forever. Triage on 2026-09-24 found
46 listed, of which 31 were superseded (the reviewed PR merged, or a later
same-subject review resolved), 8 stale, 5 test sessions, and 2 that genuinely
needed the operator. The 2 were lost in the 44.

``ack`` takes a session off the default listing by appending a row to a LOCAL,
append-only ledger (``~/.unitares/dialectic-acks.jsonl``, overridable with
``DIALECTIC_ACK_LEDGER``). It is deliberately not a database write:

* ⛔``status`` is protocol state, not a verdict, and ``agrees`` is the
  reviewer's verdict. An acknowledgement is neither. Writing it into
  ``core.dialectic_sessions`` or ``core.dialectic_messages`` would falsify the
  record the dialectic produced; the objection still stands, the operator has
  only decided it no longer needs surfacing. So the dialectic rows are never
  touched and the database connection is opened READ ONLY.
* The ledger records WHO took it off and WHY, with a disposition, so the
  decision is itself on a record rather than a silent deletion.
* The default listing says how many acknowledged reviews it hid, so nothing
  vanishes silently; ``--all`` shows them with their dispositions.
* An acknowledgement is bound to the session AS IT WAS when acknowledged: the
  ledger row records the session's ``updated_at`` and newest objection time.
  If either has moved since (a reassign reopened it, a new objection landed),
  the acknowledgement no longer applies and the session is listed again.

Pass ``--seen-at`` with the time you read the listing: a session that changed
after that is refused, so a batch ack never hides an objection you have not seen.

Session ids may be given as unambiguous prefixes. A prefix matching more than
one session is refused, never guessed, and a batch with any bad id writes
nothing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import sys
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence

CONNECT_TIMEOUT_S = 5
STATEMENT_TIMEOUT_MS = 5000

DEFAULT_DSN = os.environ.get(
    "GOVERNANCE_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/governance",
)

# Operator state lives under ~/.unitares/ beside the doctors' state files, and
# is overridable by env the same way (DOCTOR_FINDINGS_STATE, LUMEN_DOCTOR_STATE)
# so tests never touch the real ledger.
LEDGER_ENV = "DIALECTIC_ACK_LEDGER"
DEFAULT_LEDGER = "~/.unitares/dialectic-acks.jsonl"

# What an acknowledgement may say. None of these is a verdict: each says why
# the operator no longer needs the review SURFACED, not whether the objection
# was right.
#   superseded -- the reviewed change merged, or a later review of the same
#                 subject resolved
#   stale      -- no longer actionable (the subject moved on, empty topic, ...)
#   test       -- a smoke test or probe the label filter did not catch
DISPOSITIONS = ("superseded", "stale", "test")

# Prefixes shorter than this match by accident, not by intent. Session ids are
# 16 hex characters; the triage notes cite 8.
MIN_PREFIX_LEN = 6

# The ack path checks ids against the WHOLE unresolved backlog, not the
# listing window, so an old review can still be acknowledged.
ACK_WINDOW_DAYS = 36500

# ⛔starts_with, not LIKE: a user-typed prefix containing `_` or `%` would be a
# wildcard under LIKE and could match a session the operator never named.
MATCH_QUERY = """
SELECT p.prefix, s.session_id
FROM unnest(%(prefixes)s::text[]) AS p(prefix)
JOIN core.dialectic_sessions s ON starts_with(s.session_id, p.prefix)
"""

# ⛔Probe traffic is partitioned on the agent LABEL, never on `trigger_source`.
# Measured 2026-09-09: trigger_source is 'manual' for all 51 canary sessions
# AND for 127 non-canary ones, so it has no discriminating power at all.
#
# ⛔The rule is `(probe|canary)|^RP[0-9]`, and it is NOT invented here -- it is
# the frozen exclusion already used by scripts/dev/dialectic_verdict_labels.py
# (PROBE_FAMILY_RE, pinned by tests/test_dialectic_verdict_labels.py), whose
# comment says changing it requires a new cohort ID. An earlier draft of this
# file filtered only `canary_dialectic%` and let 22 rate-probe sessions
# (RP1..RP18, RateProbe*, AgreeRateProbe -- topics literally "run 1".."run 18")
# through, inflating the operator's headline count from 41 to 63.
# src/mcp_handlers/dialectic/session.py names this exact hazard: "pooling
# probes with organic traffic manufactures a rate describing no regime that
# ever existed."
#
# ⛔Do NOT widen this to a substring test for 'test' or 'demo': the label
# `claude-federation-attestation` contains "test" and is a real review.
#
# ⛔The join to core.agents is a LEFT JOIN with COALESCE, not an inner join.
# 12 sessions reference a paused_agent_id with no row in core.agents; an inner
# join would silently drop real unresolved work while looking like a filter.
#
# "Unresolved" is `status <> 'resolved'`, NOT `status = 'active'`. There are no
# active sessions at all -- every session reaches a terminal status -- so
# keying on 'active' returns an empty list forever and reads as "all clear".
QUERY = """
WITH unresolved AS (
    SELECT s.session_id, s.topic, s.reason, s.reviewer_agent_id,
           s.awaiting_facilitation, s.status, s.phase, s.created_at, s.updated_at
    FROM core.dialectic_sessions s
    LEFT JOIN core.agents pa ON pa.id = s.paused_agent_id
    WHERE s.status <> 'resolved'
      AND COALESCE(pa.label, '') !~* '(probe|canary)'
      AND COALESCE(pa.label, '') !~ '^RP[0-9]'
      AND s.created_at >= now() - interval '1 day' * %(window_days)s
)
SELECT u.session_id,
       COALESCE(NULLIF(u.topic, ''), u.reason, '') AS topic,
       COALESCE(ra.label, u.reviewer_agent_id, '') AS reviewer,
       u.awaiting_facilitation,
       u.status,
       u.phase,
       u.created_at,
       u.updated_at,
       r.timestamp AS standing_since,
       now() AS now,
       COALESCE(r.reasoning, '') AS reviewer_reasoning,
       CASE
         WHEN jsonb_typeof(r.proposed_conditions) = 'array'
           THEN ARRAY(SELECT jsonb_array_elements_text(r.proposed_conditions))
         ELSE ARRAY[]::text[]
       END AS conditions
FROM unresolved u
LEFT JOIN core.agents ra ON ra.id = u.reviewer_agent_id
JOIN LATERAL (
    SELECT dm.agrees, dm.proposed_conditions, dm.reasoning, dm.timestamp
    FROM core.dialectic_messages dm
    WHERE dm.session_id = u.session_id
      AND dm.message_type = 'synthesis'
      AND dm.agent_id = u.reviewer_agent_id
    ORDER BY dm.timestamp DESC, dm.message_id DESC
    LIMIT 1
) r ON r.agrees IS FALSE
ORDER BY u.created_at DESC
"""


def _run_read_only(dsn: str, sql: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    import psycopg2
    import psycopg2.extras

    # ⛔The timeout lives HERE, not in the caller. The SessionStart hook that
    # consumes this originally wrapped it in shell `timeout`, which is not
    # installed on macOS -- so the guard silently did nothing on the only
    # machine that runs it, and a wedged database could have blocked every
    # session start indefinitely. connect_timeout and statement_timeout are
    # portable and bound both halves.
    conn = psycopg2.connect(dsn, connect_timeout=CONNECT_TIMEOUT_S)
    try:
        # ⛔READ ONLY is enforced by the server, not by convention: this tool,
        # including `ack`, must never change a dialectic row's status or verdict.
        conn.set_session(readonly=True)
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SET LOCAL statement_timeout = %s", (STATEMENT_TIMEOUT_MS,))
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def fetch(dsn: str, window_days: int) -> List[Dict[str, Any]]:
    return _run_read_only(dsn, QUERY, {"window_days": window_days})


def fetch_matching_session_ids(dsn: str, prefixes: Sequence[str]) -> Dict[str, List[str]]:
    """Every session (any status) whose id starts with each prefix.

    Ambiguity is judged against ALL sessions, not just the backlog, so a prefix
    that is unique only because its twin happens to be resolved is still refused.
    """
    rows = _run_read_only(dsn, MATCH_QUERY, {"prefixes": list(prefixes)})
    out: Dict[str, List[str]] = {p: [] for p in prefixes}
    for r in rows:
        out.setdefault(r["prefix"], []).append(r["session_id"])
    return out


# ── acknowledgement ledger ──────────────────────────────────────────────────


def ledger_path() -> str:
    return os.path.expanduser(os.environ.get(LEDGER_ENV) or DEFAULT_LEDGER)


def load_acks(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Latest acknowledgement per session id.

    Fails OPEN toward showing more: a missing ledger, an unreadable one, or a
    malformed line hides nothing. A corrupt ledger must never make real
    backlog disappear.
    """
    path = path or ledger_path()
    acks: Dict[str, Dict[str, Any]] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return acks
    except (OSError, ValueError) as exc:  # ValueError covers UnicodeDecodeError
        print(f"dialectic_unresolved: ack ledger unreadable, hiding nothing: {exc}",
              file=sys.stderr)
        return acks
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            print(f"dialectic_unresolved: ack ledger line {n} is not JSON, ignored",
                  file=sys.stderr)
            continue
        if (not isinstance(row, dict) or not isinstance(row.get("session_id"), str)
                or row.get("disposition") not in DISPOSITIONS):
            print(f"dialectic_unresolved: ack ledger line {n} is malformed, ignored",
                  file=sys.stderr)
            continue
        acks[row["session_id"]] = row  # append-only: the latest row wins
    return acks


def append_acks(rows: Iterable[Dict[str, Any]], path: Optional[str] = None) -> str:
    """Append a batch and verify every byte landed; a short write raises OSError.

    A torn trailing line is skipped by ``load_acks`` (hiding nothing), and the
    caller reports failure instead of success.
    """
    path = path or ledger_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() > 0:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    # A torn previous write: terminate it so this batch's first
                    # row is not glued onto the fragment and silently lost.
                    payload = "\n" + payload
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        data = payload.encode("utf-8")
        written = 0
        while written < len(data):
            n = os.write(fd, data[written:])
            if n <= 0:
                raise OSError(f"short write to {path}: {written} of {len(data)} bytes")
            written += n
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def resolve_ids(
    prefixes: Sequence[str],
    matches: Dict[str, List[str]],
    backlog_ids: Iterable[str],
) -> tuple:
    """Map each requested prefix to exactly one backlog session, or say why not.

    Returns ``(resolved, errors)``. The caller writes nothing unless ``errors``
    is empty -- one bad id refuses the whole batch.
    """
    backlog = set(backlog_ids)
    resolved: List[str] = []
    errors: List[str] = []
    for p in prefixes:
        if len(p) < MIN_PREFIX_LEN:
            errors.append(f"{p!r}: prefix shorter than {MIN_PREFIX_LEN} characters")
            continue
        hits = sorted(set(matches.get(p) or []))
        if not hits:
            errors.append(f"{p!r}: no dialectic session has this id")
        elif len(hits) > 1:
            errors.append(f"{p!r}: ambiguous, matches {len(hits)} sessions "
                          f"({', '.join(hits)}); give more of the id")
        elif hits[0] not in backlog:
            errors.append(f"{p!r}: {hits[0]} is not in the unresolved backlog "
                          f"(resolved, a probe, or no standing objection)")
        elif hits[0] not in resolved:
            resolved.append(hits[0])
    return resolved, errors


def _as_utc(value: Any) -> Optional[dt.datetime]:
    """Parse a DB datetime or ISO string to an aware UTC datetime, else None."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, dt.datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _state_marks(row: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """The two timestamps an acknowledgement is bound to."""
    marks = {}
    for key in ("updated_at", "standing_since"):
        t = _as_utc(row.get(key))
        marks[f"session_{key}"] = t.isoformat() if t else None
    return marks


def build_ack_rows(session_ids: Sequence[str], disposition: str, reason: str,
                   acknowledged_by: str, now: Optional[dt.datetime] = None,
                   session_rows: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    ts = (now or dt.datetime.now(dt.timezone.utc)).isoformat()
    session_rows = session_rows or {}
    return [
        {
            "session_id": sid,
            "disposition": disposition,
            "reason": reason,
            "acknowledged_by": acknowledged_by,
            "timestamp": ts,
            **_state_marks(session_rows.get(sid, {})),
        }
        for sid in session_ids
    ]


def ack_still_applies(row: Dict[str, Any], ack: Dict[str, Any]) -> bool:
    """True only if the session has not moved since it was acknowledged.

    Fails toward SHOWING: an ack without state marks, or a session whose
    ``updated_at`` or newest objection is later than recorded, is not hidden.
    """
    for key in ("updated_at", "standing_since"):
        recorded = _as_utc(ack.get(f"session_{key}"))
        current = _as_utc(row.get(key))
        if current is None:
            continue
        if recorded is None or current > recorded:
            return False
    return True


def partition(rows: List[Dict[str, Any]], acks: Dict[str, Dict[str, Any]]) -> tuple:
    """Split into (visible, hidden). Hidden rows carry their acknowledgement."""
    visible, hidden = [], []
    for r in rows:
        ack = acks.get(r["session_id"])
        if ack and ack_still_applies(r, ack):
            hidden.append({**r, "acknowledgement": ack})
        else:
            visible.append(r)
    return visible, hidden


def hidden_summary(hidden: List[Dict[str, Any]]) -> str:
    """The one line that stops acknowledged reviews vanishing silently."""
    if not hidden:
        return ""
    counts = Counter(r["acknowledgement"]["disposition"] for r in hidden)
    parts = ", ".join(f"{d} {counts[d]}" for d in DISPOSITIONS if counts[d])
    return (f"{len(hidden)} acknowledged review(s) hidden ({parts}); "
            f"--all shows them. Ledger: {ledger_path()}")


def render(rows: List[Dict[str, Any]], max_conditions: int) -> str:
    """One block per review. Conditions are the point, so they are never elided
    silently -- a truncated list says how many it dropped."""
    if not rows:
        return ""

    out = [f"{len(rows)} dialectic review(s) ended with a standing objection nobody answered.", ""]
    for r in rows:
        topic = (r["topic"] or "(no topic)").strip().replace("\n", " ")
        if len(topic) > 140:
            topic = topic[:137] + "..."
        # ⛔The age of the OBJECTION, not the session's lifespan. An earlier
        # draft printed updated_at - created_at, which is how long the session
        # was open; 37 of 63 rows then showed "0d" for objections standing for
        # months, because most sessions open and die inside a day.
        standing_since = r.get("standing_since") or r["updated_at"] or r["created_at"]
        age_days = (r["now"] - standing_since).days if r.get("now") else 0
        out.append(f"  {r['session_id']}  [{r['status']}, {age_days}d]  reviewer={r['reviewer']}")
        ack = r.get("acknowledgement")
        if ack:
            why = " ".join(str(ack.get("reason") or "").split())
            out.append(f"    ACKNOWLEDGED {ack['disposition']} by {ack.get('acknowledged_by', '?')} "
                       f"at {ack.get('timestamp', '?')}: {why if len(why) <= 120 else why[:117] + '...'}")
        out.append(f"    {topic}")
        conds = r["conditions"] or []
        for c in conds[:max_conditions]:
            c = " ".join(str(c).split())
            out.append(f"      - {c if len(c) <= 160 else c[:157] + '...'}")
        if len(conds) > max_conditions:
            out.append(f"      ... and {len(conds) - max_conditions} more condition(s)")
        if not conds:
            out.append("      (reviewer objected but proposed no conditions)")
        out.append("")
    out.append("Reopen one with: dialectic(action='reassign', session_id=..., new_reviewer_id=...)")
    out.append("Take one off this list (local ledger, no DB write): dialectic_unresolved.py ack "
               "SESSION_ID --disposition {%s} --reason TEXT" % "|".join(DISPOSITIONS))
    return "\n".join(out)


def render_comment(row: Dict[str, Any]) -> str:
    """One review as a markdown block an operator can paste onto a PR.

    ⛔RENDER ONLY. There is deliberately no --apply and no posting path.
    Two measured reasons, not squeamishness:

    1. The operator's standing rule is that automated finders are REPORT-ONLY,
       and it names public commenting explicitly. This content is LLM-composed
       reviewer prose, which puts it squarely in the finder's class.
    2. PR comments are the one GitHub write surface nothing in this fleet
       lints. The local pre-PR guard matches only `pr create|edit` and the CI
       scope job reads only the PR body, so a comment can carry deliberation
       register, operator paths and agent UUIDs into a public repo unchecked.

    Pasting costs the operator one keystroke and keeps a human in the path
    where no guard exists. Only 15 of 63 unresolved reviews name a PR at all,
    so an automated poster would be high-risk machinery for a quarter of cases.
    """
    conds = row.get("conditions") or []
    lines = [
        "## Dialectic review: standing conditions",
        "",
        f"Review session `{row['session_id']}` ended unresolved with a standing",
        f"objection from `{row['reviewer']}`. The conditions below were never",
        "answered, and are reproduced here so the merge decision can see them.",
        "",
    ]
    if conds:
        lines += [f"{i}. {' '.join(str(c).split())}" for i, c in enumerate(conds, 1)]
    else:
        lines.append("_The reviewer objected but proposed no conditions._")
    reasoning = " ".join(str(row.get("reviewer_reasoning") or "").split())
    if reasoning:
        lines += ["", "<details><summary>Reviewer reasoning</summary>", "", reasoning, "", "</details>"]
    lines += ["", "This is a record of an unresolved review, not an approval or a block."]
    return "\n".join(lines)


def ack_main(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="dialectic_unresolved.py ack",
        description="Take unresolved reviews off the default listing by appending "
                    "to the local acknowledgement ledger. Never writes the database; "
                    "status and verdict are untouched.",
    )
    ap.add_argument("session_ids", nargs="+", metavar="SESSION_ID",
                    help="Full ids or unambiguous prefixes (>= %d chars)" % MIN_PREFIX_LEN)
    ap.add_argument("--disposition", required=True, choices=DISPOSITIONS)
    ap.add_argument("--reason", required=True,
                    help="Why it no longer needs surfacing, e.g. 'PR #2025 merged'")
    ap.add_argument("--by", dest="acknowledged_by", default=None,
                    help="Who is acknowledging (default: the login name)")
    ap.add_argument("--seen-at", default=None, metavar="ISO_TIMESTAMP",
                    help="When you read the listing you are acting on. Any session "
                         "updated, or given a new objection, after this is refused, "
                         "so an ack never hides something you have not seen.")
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    args = ap.parse_args(list(argv))
    seen_at = None
    if args.seen_at:
        seen_at = _as_utc(args.seen_at)
        if seen_at is None:
            print(f"dialectic_unresolved ack: --seen-at {args.seen_at!r} is not an ISO timestamp",
                  file=sys.stderr)
            return 2

    reason = " ".join(args.reason.split())
    if not reason:
        print("dialectic_unresolved ack: --reason must not be empty", file=sys.stderr)
        return 2
    try:
        by = args.acknowledged_by or getpass.getuser()
    except Exception:  # noqa: BLE001 -- getuser raises when no login name resolves
        by = ""
    by = " ".join(str(by).split())
    if not by:
        print("dialectic_unresolved ack: could not determine who is acknowledging; pass --by",
              file=sys.stderr)
        return 2

    prefixes = list(dict.fromkeys(s.strip() for s in args.session_ids if s.strip()))
    if not prefixes:
        print("dialectic_unresolved ack: no session ids given, nothing written",
              file=sys.stderr)
        return 2
    try:
        matches = fetch_matching_session_ids(args.dsn, prefixes)
        backlog_rows = {r["session_id"]: r for r in fetch(args.dsn, ACK_WINDOW_DAYS)}
        backlog = list(backlog_rows)
    except Exception as exc:  # noqa: BLE001
        print(f"dialectic_unresolved ack: query failed, nothing written: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    resolved, errors = resolve_ids(prefixes, matches, backlog)
    if seen_at is not None:
        for sid in resolved:
            row = backlog_rows.get(sid, {})
            moved = [k for k in ("updated_at", "standing_since")
                     if (_as_utc(row.get(k)) or seen_at) > seen_at]
            if moved:
                errors.append(f"{sid}: changed after --seen-at ({', '.join(moved)}); "
                              f"re-read it before acknowledging")
    if errors:
        print("dialectic_unresolved ack: refused, nothing written:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    rows = build_ack_rows(resolved, args.disposition, reason, by, session_rows=backlog_rows)
    try:
        path = append_acks(rows)
    except OSError as exc:
        print(f"dialectic_unresolved ack: could not write the ledger: {exc}", file=sys.stderr)
        return 2
    print(f"Acknowledged {len(rows)} review(s) as {args.disposition} in {path}:")
    for r in rows:
        print(f"  {r['session_id']}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "ack":
        return ack_main(argv[1:])

    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="Acknowledge reviews with: dialectic_unresolved.py ack ID [ID ...] "
               "--disposition {%s} --reason TEXT" % ",".join(DISPOSITIONS),
    )
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--window-days", type=int, default=90,
                    help="How far back to look (default 90)")
    ap.add_argument("--max-conditions", type=int, default=4,
                    help="Conditions shown per review before truncating (default 4)")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    ap.add_argument("--all", action="store_true", dest="show_all",
                    help="Include acknowledged reviews, with their dispositions")
    ap.add_argument("--session-id",
                    help="Render just this session as a paste-ready PR comment. "
                         "Renders only -- this tool never posts anywhere.")
    args = ap.parse_args(argv)

    try:
        rows = fetch(args.dsn, args.window_days)
    except Exception as exc:  # noqa: BLE001 -- a reporting tool reports, never raises
        print(f"dialectic_unresolved: query failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    if args.session_id:
        # Acknowledged or not: an acknowledgement hides a review from the
        # listing, it does not make its conditions unreadable.
        match = [r for r in rows if r["session_id"] == args.session_id]
        if not match:
            print(f"dialectic_unresolved: no unresolved review {args.session_id!r} "
                  f"in the last {args.window_days} days", file=sys.stderr)
            return 1
        print(render_comment(match[0]))
        return 0

    visible, hidden = partition(rows, load_acks())
    if args.show_all:
        acked = {r["session_id"]: r["acknowledgement"] for r in hidden}
        shown = [{**r, "acknowledgement": acked.get(r["session_id"])} for r in rows]
    else:
        shown = visible

    if args.json:
        print(json.dumps(
            {
                "count": len(shown),
                "window_days": args.window_days,
                "reviews": shown,
                # `reviews` excludes acknowledged sessions. These fields say what
                # was left out and why. The SessionStart hook currently reads only
                # `reviews`; a consumer that reports a backlog size should add
                # `acknowledged_hidden`.
                "acknowledged_hidden": 0 if args.show_all else len(hidden),
                "acknowledged_by_disposition": {} if args.show_all else dict(
                    Counter(r["acknowledgement"]["disposition"] for r in hidden)),
                "ledger_path": ledger_path(),
            },
            default=str, indent=2,
        ))
        return 0

    text = render(shown, args.max_conditions)
    summary = "" if args.show_all else hidden_summary(hidden)
    if text:
        print(text)
    if summary:
        print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
