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
A ``--seen-at`` later than the database's current time is refused too, since it
would let every change through.

Session ids may be given as unambiguous prefixes. A prefix matching more than
one session is refused, never guessed, and a batch with any bad id writes
nothing.

Supersession (automatic)
------------------------
Most of the backlog was never waiting on anyone: a LATER review of the same
subject was accepted, and this list could not see it. The 2026-09-18 triage
found 8 of 45 superseded that way; the 2026-09-24 one, 31 of 46. So a review is
also left off the default listing when a later session whose reviewer's latest
verdict is ``agrees=true`` either

* cites this session's id in its topic, reason or thesis (a493adde cites
  196acc37, edee829b cites e6dbfb10, 94001e07 cites 88dd3e27), or
* has the same subject PR: the first PR its topic names (9cd71f4a accepted
  unitares#2025 after 8da00e5b, 5db1710c and 0dc43b1d rejected earlier heads
  of it; faee73eb accepted #2348 twelve minutes after 78129de5 rejected it).
  Two subject PRs differ only by number, or by two EXPLICIT repos (a URL or
  ``owner/repo``); see ``PrRef``.

Only the subject PR counts, not every PR a thesis mentions: a review of #2064
"stacked on #2063" is not answered by an accepted review of #2063. A same-subject
review that names neither (83edd2d6 re-reviewing 2958a8ff's executor at a new
commit) is not machine-detectable and stays for ``ack``.

Like an acknowledgement, supersession changes nothing in the database: the
objection still stands in its own session, and nothing here says the later
review met its conditions. It is counted, never dropped silently, and ``--all``
shows each superseded review with the session that superseded it.

Subject PR finished (automatic)
-------------------------------
A review is also left off the default listing when its subject PR (the same
``subject_pr`` reading as above) was MERGED or CLOSED after the review was
opened. The change under review has moved on: either it landed, and the standing
objection is audit evidence about a merged change (``merged_with_standing_objection``),
or it was abandoned (``subject_closed_unmerged``). Neither is work for an operator.
Operator decision 2026-10-11 ("things should just work"), with an independent
Codex recommendation agreeing. Measured that day over every row in the 90-day
window: all 11 reviews whose subject PR finished after the review opened had
already been acknowledged by hand (5, all ``stale``) or superseded (6); none was
kept on the list.

* The PR must have finished AFTER the review was created. A PR that merged
  before it is cited context, or a post-merge review, and stays listed: that is
  also what stops a repo-less "fermata PR #59" being read as this repo's #59,
  which merged in April, months before the review.
* A repo-less reference is read as ``DEFAULT_PR_REPO``; an explicit repo (URL or
  ``owner/repo``) is used as written.
* The state comes from GitHub (``gh``), one batched query, bounded by a
  timeout. Any failure (no ``gh``, offline, unauthenticated, a timeout, a
  reference GitHub cannot resolve) hides nothing for that review. MERGED is
  final, so merged states are cached locally (``DIALECTIC_PR_STATE_CACHE``) and
  later runs make no network call for them; a CLOSED PR can be reopened, so it
  is looked up again every run.

Like supersession, this writes nothing to the database, is counted rather than
dropped silently, and ``--all`` shows the PR and how it finished.

Routing to whoever touches the subject (``--touching``)
--------------------------------------------------------
What is left after the above is mostly design reviews with no PR and reviews of
a PR that had already merged. The operator cannot answer those any better than
the next agent changing the same code, so they go to that agent instead of to a
person (operator, 2026-10-11). ``--touching PATH`` (repeatable) keeps only the
reviews whose topic or conditions name one of the given repo-relative files;
a pre-PR gate passes the branch's changed files and prints what it gets back.
Every emitted review carries ``subject_paths``, and a filtered one also carries
``matched_paths``.

Answered in a merged PR (automatic)
-----------------------------------
The agent that picks one up closes it by putting ``Answers dialectic <id>``
(full session id) in its PR body, the way ``Closes #N`` closes an issue. Once
that PR has merged, after the review opened, the review leaves the default
listing (``answered_in_merged_pr``). The line is required, not just the id: a PR
body that quotes a session id is usually citing it as an example (#2735 quotes
8973efacebb17e11 as a concession it measured and answers none of its
conditions). The lookup is one batched, time-bounded ``gh`` search; found
answers are cached (``DIALECTIC_ANSWER_CACHE``) because a merged PR stays
merged, and any failure hides nothing. As elsewhere, nothing is written to the
database and nothing here says the PR met the conditions: the PR body is the
answer, on the record where anyone can read it.

Awaiting the operator
---------------------
``--json`` also carries ``awaiting_operator``: sessions that are not terminal
and still ask for facilitation. Since #2735 a facilitation request whose
reviewer has had its last word closes on its own, so what remains here is the
narrow set nothing automated can answer (no reviewer verdict at all, a
self-review, a reviewer with no declared window). That list, not the standing
objections, is what an operator surface should interrupt for.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
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

# Subject-PR lookups. A repo-less "PR #N" is read as this repo's.
DEFAULT_PR_REPO = "cirwel/unitares"
PR_STATE_CACHE_ENV = "DIALECTIC_PR_STATE_CACHE"
DEFAULT_PR_STATE_CACHE = "~/.unitares/dialectic-pr-state.json"
# One batched GraphQL call. The SessionStart hook waits on this, so it is
# bounded here; a timeout hides nothing.
GH_TIMEOUT_S = 8
PR_FINISHED_STATES = ("MERGED", "CLOSED")
ANSWER_CACHE_ENV = "DIALECTIC_ANSWER_CACHE"
DEFAULT_ANSWER_CACHE = "~/.unitares/dialectic-answers.json"
# Bounds the one search query; rows past it are not checked and stay listed.
MAX_ANSWER_LOOKUPS = 40

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


# Sessions whose reviewer's latest verdict ACCEPTED: the only kind that can
# supersede an objection. Keyed on the verdict (`agrees` on the reviewer's
# synthesis), not on `status`, which is protocol state. A self-review
# (reviewer = paused agent) is excluded: it can never answer anyone's objection.
# Probe traffic is excluded by the same label rule as QUERY: a canary accepting
# the same PR, or citing an organic session, must not hide an organic objection.
SUPERSEDER_QUERY = """
SELECT s.session_id, s.created_at, v.timestamp AS accepted_at,
       COALESCE(NULLIF(s.topic, ''), s.reason, '') AS subject,
       concat_ws(E'\\n', s.topic, s.reason,
                 (SELECT string_agg(concat_ws(' ', t.root_cause, t.reasoning,
                                                    t.proposed_conditions::text), E'\\n')
                  FROM core.dialectic_messages t
                  WHERE t.session_id = s.session_id
                    AND t.message_type = 'thesis')) AS text
FROM core.dialectic_sessions s
LEFT JOIN core.agents pa ON pa.id = s.paused_agent_id
JOIN LATERAL (
    SELECT dm.agrees, dm.timestamp
    FROM core.dialectic_messages dm
    WHERE dm.session_id = s.session_id
      AND dm.message_type = 'synthesis'
      AND dm.agent_id = s.reviewer_agent_id
    ORDER BY dm.timestamp DESC, dm.message_id DESC
    LIMIT 1
) v ON v.agrees IS TRUE
WHERE s.reviewer_agent_id IS DISTINCT FROM s.paused_agent_id
  AND COALESCE(pa.label, '') !~* '(probe|canary)'
  AND COALESCE(pa.label, '') !~ '^RP[0-9]'
  AND s.created_at >= now() - interval '1 day' * %(window_days)s
"""

# Sessions still asking a human to facilitate. ⛔Terminal rows are excluded on
# `status`: 75 `failed` rows still carry awaiting_facilitation=true from before
# the close cleared the flag (measured 2026-10-11), and listing them would put
# the whole history back in front of the operator.
AWAITING_QUERY = """
SELECT s.session_id,
       COALESCE(NULLIF(s.topic, ''), s.reason, '') AS topic,
       COALESCE(ra.label, s.reviewer_agent_id, '') AS reviewer,
       s.status, s.phase, s.created_at, s.updated_at
FROM core.dialectic_sessions s
LEFT JOIN core.agents pa ON pa.id = s.paused_agent_id
LEFT JOIN core.agents ra ON ra.id = s.reviewer_agent_id
WHERE s.awaiting_facilitation
  AND s.status NOT IN ('resolved', 'failed')
  AND COALESCE(pa.label, '') !~* '(probe|canary)'
  AND COALESCE(pa.label, '') !~ '^RP[0-9]'
ORDER BY s.created_at DESC
"""

# A PR reference, most specific form first:
#   github.com/<owner>/<repo>/pull/<n>
#   <owner>/<repo>#<n>, <owner>/<repo> PR #<n>
#   PR #<n>, PRs #<n>
# A bare "#<n>" is NOT one: it names issues and clocks as often as PRs, and a
# canary probe's "(#1387 positive control)" matched a design review that cited
# the #1387 clock, hiding a review nothing had answered.
_PR_REF = re.compile(
    r"github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/pull/(?P<url_n>\d+)"
    r"|(?P<slug_repo>[\w.-]+/[\w.-]+)(?:#|\s+PRs?\s+#)(?P<slug_n>\d+)"
    r"|\bPRs?\s+#(?P<word_n>\d+)",
    re.IGNORECASE,
)
# Session ids are 16 hex; a citation shorter than 8 is not one.
_HEX_RUN = re.compile(r"(?<![0-9a-f])[0-9a-f]{8,16}(?![0-9a-f])")


@dataclass(frozen=True)
class PrRef:
    """A subject PR. ``repo`` is ``owner/repo`` when the text named it
    unambiguously (a URL or ``owner/repo``), else None; a fork's #42 is not
    upstream's #42. The word before "PR" is not trusted: it is a
    repo in "fermata PR #59" and an adjective in "the current PR #2348", and
    no stoplist separates the two."""

    number: int
    repo: Optional[str] = None

    def __str__(self) -> str:
        return f"{self.repo or ''}#{self.number}"

    def same_as(self, other: "PrRef") -> bool:
        if self.number != other.number:
            return False
        return self.repo is None or other.repo is None or self.repo == other.repo


def subject_pr(text: str) -> Optional[PrRef]:
    """The first PR a subject line names, or None."""
    m = _PR_REF.search(text or "")
    if not m:
        return None
    if m.group("url_n"):
        return PrRef(int(m.group("url_n")), m.group("url_repo").lower())
    if m.group("slug_n"):
        return PrRef(int(m.group("slug_n")), m.group("slug_repo").lower())
    return PrRef(int(m.group("word_n")))


def find_supersessions(
    rows: Sequence[Dict[str, Any]], superseders: Sequence[Dict[str, Any]]
) -> Dict[str, Dict[str, Any]]:
    """Map each superseded review's id to the later accepted session that
    superseded it, and the signal that matched. Pure.

    A session never supersedes itself, anything created after it, or an
    objection raised after its own acceptance.
    """
    out: Dict[str, Dict[str, Any]] = {}
    epoch = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    # Oldest first, so the reported superseder is the EARLIEST later acceptance.
    by_age = sorted(superseders, key=lambda s: _as_utc(s.get("created_at")) or epoch)
    prepared = [
        (s, _as_utc(s.get("created_at")), _as_utc(s.get("accepted_at")),
         set(_HEX_RUN.findall((s.get("text") or "").lower())),
         subject_pr(s.get("subject") or ""))
        for s in by_age
    ]
    for r in rows:
        sid = r["session_id"]
        created = _as_utc(r.get("created_at"))
        # The objection being hidden is the standing one; an acceptance that
        # predates it cannot have answered it.
        objection = _as_utc(r.get("standing_since")) or created
        own_pr = subject_pr(r.get("topic") or "")
        # Opened after this session, not merely accepted after its objection:
        # a review opened earlier was reviewing an earlier state, and the safe
        # failure here is to keep showing an objection (one `ack` clears it),
        # never to hide one it did not answer (review round 2 on #2511).
        for s, s_created, s_accepted, cited, s_pr in prepared:
            if (s["session_id"] == sid or created is None or s_created is None
                    or s_accepted is None or s_created <= created
                    or objection is None or s_accepted <= objection):
                continue
            if any(sid.startswith(h) for h in cited):
                out[sid] = {"session_id": s["session_id"], "signal": "cites_session_id"}
                break
            if own_pr and s_pr and own_pr.same_as(s_pr):
                out[sid] = {"session_id": s["session_id"], "signal": "same_subject_pr",
                            "pr": str(own_pr)}
                break
    return out


# ── subject PR finished ─────────────────────────────────────────────────────


def _pr_key(pr: PrRef) -> tuple:
    return ((pr.repo or DEFAULT_PR_REPO).lower(), pr.number)


def find_finished_subjects(
    rows: Sequence[Dict[str, Any]], states: Dict[tuple, Dict[str, Any]]
) -> Dict[str, Dict[str, Any]]:
    """Map each review whose subject PR finished after it opened to how. Pure.

    ``states`` maps ``(owner/repo, number)`` to ``{"state", "merged_at",
    "closed_at"}`` as GitHub reports them. A review with no subject PR, no
    known state, an open PR, or a PR that finished before the review opened is
    left out, so it stays listed.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        pr = subject_pr(r.get("topic") or "")
        created = _as_utc(r.get("created_at"))
        if pr is None or created is None:
            continue
        st = states.get(_pr_key(pr))
        if not st or st.get("state") not in PR_FINISHED_STATES:
            continue
        merged = st.get("state") == "MERGED"
        finished = _as_utc(st.get("merged_at") if merged else st.get("closed_at"))
        if finished is None or finished <= created:
            continue
        out[r["session_id"]] = {
            "pr": f"{_pr_key(pr)[0]}#{pr.number}",
            "state": st["state"],
            "finished_at": finished.isoformat(),
            "signal": "merged_with_standing_objection" if merged else "subject_closed_unmerged",
        }
    return out


def pr_state_cache_path() -> str:
    return os.path.expanduser(os.environ.get(PR_STATE_CACHE_ENV) or DEFAULT_PR_STATE_CACHE)


def _load_pr_cache(path: str) -> Dict[tuple, Dict[str, Any]]:
    """Cached MERGED states. Any problem reading it means an empty cache: the
    cost is a network lookup, never a hidden review."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    out: Dict[tuple, Dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for key, st in raw.items():
        repo, _, num = str(key).rpartition("#")
        if repo and num.isdigit() and isinstance(st, dict) and st.get("state") == "MERGED":
            out[(repo.lower(), int(num))] = st
    return out


def _save_pr_cache(path: str, cache: Dict[tuple, Dict[str, Any]]) -> None:
    merged = {f"{repo}#{n}": st for (repo, n), st in sorted(cache.items())
              if st.get("state") == "MERGED"}
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, sort_keys=True, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass  # a cache that cannot be written only costs a lookup next run


def _gh_pr_states(keys: Sequence[tuple]) -> Dict[tuple, Dict[str, Any]]:
    """One batched GraphQL query for every key. Empty on any failure."""
    if not keys:
        return {}
    parts = []
    aliases = {}
    for i, (repo, n) in enumerate(keys):
        owner, _, name = repo.partition("/")
        if not owner or not name or not re.fullmatch(r"[\w.-]+", owner + name):
            continue
        alias = f"p{i}"
        aliases[alias] = (repo, n)
        parts.append(f'{alias}: repository(owner: "{owner}", name: "{name}") '
                     f'{{ pullRequest(number: {int(n)}) {{ state mergedAt closedAt }} }}')
    if not parts:
        return {}
    query = "query { " + " ".join(parts) + " }"
    try:
        proc = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True, text=True, timeout=GH_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    # A partial answer (one repo GitHub cannot resolve) still exits non-zero
    # but carries data for the rest; use what resolved.
    try:
        data = (json.loads(proc.stdout or "{}") or {}).get("data") or {}
    except ValueError:
        return {}
    out: Dict[tuple, Dict[str, Any]] = {}
    for alias, key in aliases.items():
        pr = ((data.get(alias) or {}).get("pullRequest")) or None
        if isinstance(pr, dict) and pr.get("state"):
            out[key] = {"state": pr["state"], "merged_at": pr.get("mergedAt"),
                        "closed_at": pr.get("closedAt")}
    return out


def fetch_pr_states(rows: Sequence[Dict[str, Any]]) -> Dict[tuple, Dict[str, Any]]:
    """GitHub state of every subject PR the rows name, cache first."""
    path = pr_state_cache_path()
    cache = _load_pr_cache(path)
    wanted = {_pr_key(pr) for pr in (subject_pr(r.get("topic") or "") for r in rows) if pr}
    missing = sorted(k for k in wanted if k not in cache)
    fresh = _gh_pr_states(missing)
    if any(st.get("state") == "MERGED" for st in fresh.values()):
        _save_pr_cache(path, {**cache, **fresh})
    return {k: v for k, v in {**cache, **fresh}.items() if k in wanted}


# ── answered in a merged PR ─────────────────────────────────────────────────


def answer_line(session_id: str) -> re.Pattern:
    """The line a PR body carries to say it answers a review. Explicit, like
    ``Closes #N``: a bare id is how a PR cites a session as an EXAMPLE (#2735
    quotes 8973efacebb17e11 as a concession it measured, and answers none of
    that review's conditions)."""
    return re.compile(r"\banswers\s+dialectic\s+(?:session\s+)?`?" + re.escape(session_id)
                      + r"\b", re.IGNORECASE)


def find_answers(
    rows: Sequence[Dict[str, Any]], answers: Dict[str, List[Dict[str, Any]]]
) -> Dict[str, Dict[str, Any]]:
    """Map each review answered by a PR merged after it opened to the earliest
    such PR. Pure. ``answers`` maps a session id to the merged PRs whose body
    carries its ``answer_line``, as ``{"pr", "merged_at"}``."""
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        created = _as_utc(r.get("created_at"))
        if created is None:
            continue
        later = [(m, a) for a in answers.get(r["session_id"]) or []
                 if (m := _as_utc(a.get("merged_at"))) is not None and m > created]
        if later:
            m, a = min(later, key=lambda pair: pair[0])
            out[r["session_id"]] = {"pr": a["pr"], "merged_at": m.isoformat(),
                                    "signal": "answered_in_merged_pr"}
    return out


def answer_cache_path() -> str:
    return os.path.expanduser(os.environ.get(ANSWER_CACHE_ENV) or DEFAULT_ANSWER_CACHE)


def _load_answer_cache(path: str) -> Dict[str, List[Dict[str, Any]]]:
    """Cached answers. A merged PR stays merged, so a found answer is final; a
    review with none is looked up again next run. Unreadable means empty."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {sid: [a for a in found if isinstance(a, dict) and a.get("pr") and a.get("merged_at")]
            for sid, found in raw.items() if isinstance(found, list)}


def _save_answer_cache(path: str, cache: Dict[str, List[Dict[str, Any]]]) -> None:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({sid: found for sid, found in sorted(cache.items()) if found},
                      fh, sort_keys=True, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass  # a cache that cannot be written only costs a lookup next run


def _gh_answering_prs(session_ids: Sequence[str]) -> Dict[str, List[Dict[str, Any]]]:
    """One batched GraphQL search: merged PRs in ``DEFAULT_PR_REPO`` whose body
    carries each id's ``answer_line``. Empty on any failure."""
    parts = []
    aliases = {}
    for i, sid in enumerate(session_ids[:MAX_ANSWER_LOOKUPS]):
        if not re.fullmatch(r"[0-9a-f]{8,64}", sid or ""):
            continue
        alias = f"s{i}"
        aliases[alias] = sid
        parts.append(f'{alias}: search(query: "repo:{DEFAULT_PR_REPO} is:pr is:merged in:body {sid}", '
                     f'type: ISSUE, first: 5) {{ nodes {{ ... on PullRequest '
                     f'{{ number mergedAt body }} }} }}')
    if not parts:
        return {}
    query = "query { " + " ".join(parts) + " }"
    try:
        proc = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True, text=True, timeout=GH_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    try:
        data = (json.loads(proc.stdout or "{}") or {}).get("data") or {}
    except ValueError:
        return {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for alias, sid in aliases.items():
        nodes = ((data.get(alias) or {}).get("nodes")) or []
        line = answer_line(sid)
        found = [{"pr": f"{DEFAULT_PR_REPO}#{n['number']}", "merged_at": n["mergedAt"]}
                 for n in nodes
                 if isinstance(n, dict) and n.get("number") and n.get("mergedAt")
                 and line.search(n.get("body") or "")]
        if found:
            out[sid] = found
    return out


def fetch_answers(rows: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Merged PRs answering each row, cache first."""
    path = answer_cache_path()
    cache = _load_answer_cache(path)
    wanted = [r["session_id"] for r in rows]
    fresh = _gh_answering_prs([sid for sid in wanted if sid not in cache])
    if fresh:
        _save_answer_cache(path, {**cache, **fresh})
    merged = {**cache, **fresh}
    return {sid: merged[sid] for sid in wanted if sid in merged}


# ── routing by subject ──────────────────────────────────────────────────────

# A repo-relative file path: at least one directory, a known extension, and not
# the tail of a URL or an absolute/home path (the look-behind refuses a start
# right after "/", "~" is not in the class, so neither is ever matched).
_SUBJECT_PATH = re.compile(
    r"(?<![\w/.~-])((?:[\w-][\w.-]*/)+[\w.-]+\.(?:py|sh|ex|exs|md|ts|js|sql|json|ya?ml|toml))\b"
)


def subject_paths(row: Dict[str, Any]) -> List[str]:
    """Files a review names in its topic or conditions, in order, unique. The
    reviewer's reasoning is left out: it cites whatever it read along the way,
    and routing on that would interrupt agents for files the objection is not
    about."""
    text = " ".join([str(row.get("topic") or "")]
                    + [str(c) for c in (row.get("conditions") or [])])
    seen: List[str] = []
    for p in _SUBJECT_PATH.findall(text):
        if p not in seen:
            seen.append(p)
    return seen


def touching(rows: Sequence[Dict[str, Any]], paths: Sequence[str]) -> List[Dict[str, Any]]:
    """Rows that name at least one of ``paths``, with ``matched_paths``."""
    wanted = {p[2:] if p.startswith("./") else p for p in paths}
    out = []
    for r in rows:
        hit = [p for p in r.get("subject_paths") or subject_paths(r) if p in wanted]
        if hit:
            out.append({**r, "matched_paths": hit})
    return out


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


def fetch_superseders(dsn: str, window_days: int) -> List[Dict[str, Any]]:
    return _run_read_only(dsn, SUPERSEDER_QUERY, {"window_days": window_days})


def fetch_awaiting_operator(dsn: str) -> List[Dict[str, Any]]:
    return _run_read_only(dsn, AWAITING_QUERY, {})


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

    Not atomic across rows: if a write fails partway, rows written before the
    failure are valid and take effect, and a torn trailing line is skipped by
    ``load_acks``. The caller reports failure and says some rows may have
    landed; re-running the same ack is safe because the latest row wins.
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


def split_superseded(
    rows: List[Dict[str, Any]], supersessions: Dict[str, Dict[str, Any]]
) -> tuple:
    """Split into (open, superseded). Superseded rows carry ``superseded_by``."""
    open_rows, superseded = [], []
    for r in rows:
        by = supersessions.get(r["session_id"])
        if by:
            superseded.append({**r, "superseded_by": by})
        else:
            open_rows.append(r)
    return open_rows, superseded


def superseded_summary(superseded: List[Dict[str, Any]]) -> str:
    """The one line that stops superseded reviews vanishing silently."""
    if not superseded:
        return ""
    return (f"{len(superseded)} review(s) hidden as superseded by a later accepted "
            f"review; --all shows them.")


def split_finished(
    rows: List[Dict[str, Any]], finished: Dict[str, Dict[str, Any]]
) -> tuple:
    """Split into (open, finished). Finished rows carry ``subject_pr_finished``."""
    open_rows, done = [], []
    for r in rows:
        how = finished.get(r["session_id"])
        if how:
            done.append({**r, "subject_pr_finished": how})
        else:
            open_rows.append(r)
    return open_rows, done


def finished_summary(done: List[Dict[str, Any]]) -> str:
    """The one line that stops finished-subject reviews vanishing silently."""
    if not done:
        return ""
    merged = sum(1 for r in done if r["subject_pr_finished"]["state"] == "MERGED")
    closed = len(done) - merged
    parts = ", ".join(p for p in (f"merged {merged}" if merged else "",
                                  f"closed {closed}" if closed else "") if p)
    return (f"{len(done)} review(s) hidden because the reviewed PR finished after the "
            f"review opened ({parts}); --all shows them.")


def split_answered(
    rows: List[Dict[str, Any]], answered: Dict[str, Dict[str, Any]]
) -> tuple:
    """Split into (open, answered). Answered rows carry ``answered_in_pr``."""
    open_rows, done = [], []
    for r in rows:
        how = answered.get(r["session_id"])
        if how:
            done.append({**r, "answered_in_pr": how})
        else:
            open_rows.append(r)
    return open_rows, done


def answered_summary(done: List[Dict[str, Any]]) -> str:
    """The one line that stops answered reviews vanishing silently."""
    if not done:
        return ""
    return (f"{len(done)} review(s) hidden as answered by a merged PR "
            f"(\"Answers dialectic <id>\" in its body); --all shows them.")


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
        by = r.get("superseded_by")
        if by:
            how = (f"same subject PR {by['pr']}" if by.get("signal") == "same_subject_pr"
                   else "it cites this session")
            out.append(f"    SUPERSEDED by accepted review {by['session_id']} ({how})")
        done = r.get("subject_pr_finished")
        if done:
            out.append(f"    SUBJECT PR {done['pr']} {done['state']} at {done['finished_at']} "
                       f"({done['signal']})")
        answer = r.get("answered_in_pr")
        if answer:
            out.append(f"    ANSWERED by merged PR {answer['pr']} at {answer['merged_at']}")
        if r.get("matched_paths"):
            out.append(f"    NAMES {', '.join(r['matched_paths'])}")
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
    out.append("Answer one in your PR body with the line: Answers dialectic SESSION_ID "
               "(it leaves this list once that PR merges)")
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
    ap.add_argument("--seen-at", default=None, metavar="ISO_TIMESTAMP_WITH_OFFSET",
                    help="When you read the listing you are acting on. Any session "
                         "updated, or given a new objection, after this is refused, "
                         "so an ack never hides something you have not seen.")
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    args = ap.parse_args(list(argv))
    seen_at = None
    if args.seen_at:
        try:
            parsed = dt.datetime.fromisoformat(args.seen_at)
        except ValueError:
            parsed = None
        # Refuse a naive time: guessing UTC (or local) could let an unseen
        # objection through for an operator east of the guess.
        if parsed is None or parsed.tzinfo is None:
            print(f"dialectic_unresolved ack: --seen-at {args.seen_at!r} must be an ISO "
                  f"timestamp WITH an offset, e.g. 2026-09-24T10:00:00-06:00", file=sys.stderr)
            return 2
        seen_at = parsed.astimezone(dt.timezone.utc)

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
    # A --seen-at later than the database's clock would pass every session,
    # including one changed after the operator actually read the listing. The
    # listing is read before this query runs, so an honest --seen-at is never
    # later than the query's now(). No skew allowance: any allowance is a
    # window in which an unseen change gets through, and a refusal here only
    # costs a re-run with an earlier time.
    db_now = max((t for t in (_as_utc(r.get("now")) for r in backlog_rows.values())
                  if t is not None), default=None)
    if seen_at is not None and db_now is not None and seen_at > db_now:
        errors.append(f"--seen-at {seen_at.isoformat()} is later than the database's "
                      f"current time {db_now.isoformat()}; pass the time you read the listing")
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
        print(f"dialectic_unresolved ack: could not write the ledger: {exc}. Some of this "
              f"batch may already be recorded (the listing's hidden count shows it); "
              f"re-running the same ack is safe.", file=sys.stderr)
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
    ap.add_argument("--touching", action="append", metavar="PATH",
                    help="Only reviews naming this repo-relative file in their topic or "
                         "conditions (repeatable). For a pre-PR gate: pass the branch's "
                         "changed files.")
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

    try:
        supersessions = find_supersessions(rows, fetch_superseders(args.dsn, args.window_days))
    except Exception as exc:  # noqa: BLE001 -- fail toward showing, never hide on error
        print(f"dialectic_unresolved: supersession check failed, hiding nothing for it: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        supersessions = {}
    acks = load_acks()
    open_rows, superseded = split_superseded(rows, supersessions)
    try:
        finished = find_finished_subjects(open_rows, fetch_pr_states(open_rows))
    except Exception as exc:  # noqa: BLE001 -- fail toward showing, never hide on error
        print(f"dialectic_unresolved: subject-PR check failed, hiding nothing for it: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        finished = {}
    open_rows, done = split_finished(open_rows, finished)
    try:
        answered = find_answers(open_rows, fetch_answers(open_rows))
    except Exception as exc:  # noqa: BLE001 -- fail toward showing, never hide on error
        print(f"dialectic_unresolved: answering-PR check failed, hiding nothing for it: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        answered = {}
    open_rows, replied = split_answered(open_rows, answered)
    visible, hidden = partition(open_rows, acks)
    if args.show_all:
        # Acknowledgements are read for EVERY row, superseded or not: an
        # operator's recorded disposition must not vanish from --all because a
        # later review also superseded the session.
        acked = {r["session_id"]: r["acknowledgement"] for r in partition(rows, acks)[1]}
        by = {r["session_id"]: r["superseded_by"] for r in superseded}
        shown = [{**r, "acknowledgement": acked.get(r["session_id"]),
                  "superseded_by": by.get(r["session_id"]),
                  "subject_pr_finished": finished.get(r["session_id"]),
                  "answered_in_pr": answered.get(r["session_id"])} for r in rows]
    else:
        shown = visible
    shown = [{**r, "subject_paths": subject_paths(r)} for r in shown]
    if args.touching:
        shown = touching(shown, args.touching)

    try:
        awaiting = fetch_awaiting_operator(args.dsn)
    except Exception as exc:  # noqa: BLE001
        print(f"dialectic_unresolved: awaiting-operator read failed: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        awaiting = None

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
                # Superseded reviews are left out of `reviews` before the
                # acknowledgement ledger is consulted, so the two never overlap.
                "superseded_hidden": 0 if args.show_all else len(superseded),
                # Reviews whose subject PR merged or closed after they opened.
                # Taken out after supersession and before the ledger, so the
                # three hidden counts never overlap.
                "subject_pr_finished_hidden": 0 if args.show_all else len(done),
                # Reviews a merged PR declared it answers ("Answers dialectic
                # <id>"). Taken out after the finished check, before the ledger.
                "answered_hidden": 0 if args.show_all else len(replied),
                # The --touching filter, when given: `reviews` is then only the
                # rows naming one of these files, each with `matched_paths`.
                "touching": args.touching or None,
                # Non-terminal sessions still asking for facilitation: the only
                # thing here that is waiting on a person. null when the read
                # failed, so a consumer can tell "none" from "could not tell".
                "awaiting_operator": awaiting,
                "acknowledged_by_disposition": {} if args.show_all else dict(
                    Counter(r["acknowledgement"]["disposition"] for r in hidden)),
                "ledger_path": ledger_path(),
            },
            default=str, indent=2,
        ))
        return 0

    text = render(shown, args.max_conditions)
    summaries = [] if args.show_all or args.touching else [
        superseded_summary(superseded), finished_summary(done), answered_summary(replied),
        hidden_summary(hidden)]
    if text:
        print(text)
    for summary in summaries:
        if summary:
            print(summary)
    if awaiting:
        print(f"{len(awaiting)} session(s) still asking for facilitation (nothing automated "
              f"can answer these):")
        for a in awaiting:
            topic = " ".join(str(a.get("topic") or "(no topic)").split())
            print(f"  {a['session_id']}  reviewer={a.get('reviewer') or '-'}  "
                  f"{topic if len(topic) <= 100 else topic[:97] + '...'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
