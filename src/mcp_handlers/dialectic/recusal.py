"""Recusal: keep the review system from adjudicating claims about itself.

Courts do not ask a judge to notice their own conflict. Recusal is structural —
bright-line rules plus a motion any party can file — precisely because
self-detection is least reliable in the cases where it matters most. This module
imports that move: a thesis whose SUBJECT is the reviewing apparatus does not get
auto-assigned to the reviewing apparatus.

The failure this prevents was produced deliberately on 2026-08-19, session
``def32eb2b4b2ce93``. A thesis claiming "the reviewer-quality track can be
closed, post-fix refutation is 81%" was submitted with two objections withheld as
an answer key. The orchestrated reviewer found both unprompted and rejected
closure — a good review — but it never raised the one objection that required
noticing its own position: that it was the instrument under test and stood to be
rated favourably by ratifying. It engaged the corpus-contamination framing it was
handed, not the conflict of interest it was in.

⛔That is not a reviewer defect and must not be fixed by asking reviewers to
introspect harder. The same generative process produced both the judgment and the
introspection, so a better model narrows the gap without closing it. You close it
by adding a DIFFERENT process — here, a routing rule that runs before the
reviewer sees anything.

## What this is not

This is a heuristic over text, and heuristics over text are how the pre-2026-07-02
reviewer produced hollow critique. Two things keep it honest:

* It never renders a verdict. It routes. The worst false positive costs one
  session a human look, which on this system's volume is cheap.
* It fails toward facilitation — a person decides — rather than toward silent
  auto-review by a party with a stake.

It also does NOT detect the reviewer-is-the-paused-agent case. That is a
different conflict, tracked separately (issue #1585, ``reviewer_mode="self"``),
and is not what this module is for.

## Measured cost, so it is not a surprise later

Run over the 65 historical non-canary theses on 2026-08-19: **6 recused (9%)**.
Five are unambiguous — two copies of a "Dialectic System Exploitability
Analysis", one on the dialectic protocol's timeout, one on whether dialectic
adoption is measurable, and the self-test above. One is arguable: a thesis about
a dashboard bug that happened to name ``dialectic_sessions`` and ``antithesis``.

⛔**Note what that includes.** The adoption-measurability session (message 615)
is the single best review in the corpus — the reviewer produced three checkable
refutations and the thesis author conceded all three. This rule would have
recused it. That is a real cost and it is accepted deliberately: that reviewer
was adjudicating a claim about its own usage, so the quality of the outcome does
not retire the conflict. Recusal relocates a review; it does not cancel one. The
price is a human routing step on roughly one session in eleven.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

# Terms that only appear when someone is talking ABOUT the review apparatus,
# not merely using governance vocabulary. One of these is enough.
_DECISIVE = {
    "unitares_dialectic_reviewer_host": "names the reviewer backend switch",
    "reviewer quality": "the reviewer's own capability is the subject",
    "reviewer-quality": "the reviewer's own capability is the subject",
    "orchestrated reviewer": "names the reviewing process itself",
    "rubber-stamp": "alleges a failure mode of reviewing",
    "rubber stamp": "alleges a failure mode of reviewing",
    "reviewer independence": "the reviewer's own independence is the subject",
    "dialectic reviewer": "names the reviewing process itself",
}

# Protocol machinery. Any ONE of these can appear incidentally in an ordinary
# thesis, so a single hit is not enough — two distinct ones are required.
_APPARATUS = {
    "antithesis": "dialectic protocol term",
    "synthesis round": "dialectic protocol term",
    "dialectic session": "dialectic protocol term",
    "dialectic protocol": "dialectic protocol term",
    "reviewer verdict": "dialectic protocol term",
    "reviewer slot": "dialectic protocol term",
    "self_clear_refused": "dialectic protocol guard",
    "awaiting_facilitation": "dialectic protocol state",
    "dialectic_messages": "the reviewing system's own table",
    "dialectic_sessions": "the reviewing system's own table",
}

RECUSAL_ENV = "UNITARES_DIALECTIC_RECUSAL"


@dataclass(frozen=True)
class RecusalFinding:
    """A structural reason this session should not be auto-reviewed."""

    reason: str
    matched: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "recused": True,
            "reason": self.reason,
            "matched_terms": list(self.matched),
            "basis": "subject_matter_conflict",
            "note": (
                "The thesis is about the review system, so the review system is "
                "an interested party. Routed for facilitation rather than "
                "auto-assigned. This is a routing decision, not a verdict on the "
                "thesis."
            ),
        }


def recusal_mode() -> str:
    """``enforce`` (default), ``flag``, or ``off``.

    Default enforces because the fail direction is a person looking at one
    session, and the sessions affected are exactly the ones where an interested
    reviewer is least appropriate. ``flag`` records the conflict and reviews
    anyway; ``off`` disables detection entirely.
    """
    raw = (os.getenv(RECUSAL_ENV) or "enforce").strip().lower()
    return raw if raw in {"enforce", "flag", "off"} else "enforce"


def _normalise(*texts: Optional[str]) -> str:
    joined = " ".join(t for t in texts if t)
    # Collapse whitespace so "reviewer\nquality" still matches.
    return re.sub(r"\s+", " ", joined).lower()


def detect_subject_matter_conflict(*texts: Optional[str]) -> Optional[RecusalFinding]:
    """Return a finding when the text's subject is the reviewing apparatus.

    One decisive term, or two distinct protocol terms. The two-term rule exists
    because a thesis may legitimately mention ``antithesis`` once while being
    about something else entirely; sustained use of the machinery's vocabulary is
    what indicates the machinery is the topic.
    """
    if recusal_mode() == "off":
        return None

    haystack = _normalise(*texts)
    if not haystack:
        return None

    for term, why in _DECISIVE.items():
        if term in haystack:
            return RecusalFinding(
                reason=f"thesis is about the review system: {why} ({term!r})",
                matched=(term,),
            )

    hits = tuple(term for term in _APPARATUS if term in haystack)
    if len(hits) >= 2:
        return RecusalFinding(
            reason=(
                "thesis is about the review system: sustained use of the "
                "dialectic's own protocol vocabulary"
            ),
            matched=hits,
        )
    return None
