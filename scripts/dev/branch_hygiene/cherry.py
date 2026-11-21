"""Parse `git cherry` output to a hygiene verdict.

`git cherry <upstream> <head>` prints one line per commit on <head> not on
<upstream>:
  "+ <sha>"  commit not on upstream (genuine unmerged work)
  "- <sha>"  patch-equivalent commit found on upstream (squash-merged)

Empty output means no divergent commits exist between the two refs — could
be a freshly-created empty branch, a fetch failure, or a branch already at
the upstream tip. Treated as ambiguous: SKIP, do not delete.
"""
from __future__ import annotations

from enum import Enum
from typing import NamedTuple


class CherryVerdict(Enum):
    DELETE = "delete"
    HOLD = "hold"
    SKIP = "skip"


class CherryResult(NamedTuple):
    verdict: CherryVerdict
    plus_count: int
    minus_count: int
    reason: str


def parse_cherry(output: str) -> CherryResult:
    lines = [ln for ln in output.strip().split("\n") if ln.strip()]
    if not lines:
        return CherryResult(CherryVerdict.SKIP, 0, 0, "empty output (no divergent commits)")

    plus = 0
    minus = 0
    for ln in lines:
        if ln.startswith("+ "):
            plus += 1
        elif ln.startswith("- "):
            minus += 1
        else:
            return CherryResult(CherryVerdict.SKIP, plus, minus, f"unparseable line: {ln!r}")

    if plus > 0:
        return CherryResult(CherryVerdict.HOLD, plus, minus, f"{plus} unique commit(s)")
    return CherryResult(CherryVerdict.DELETE, plus, minus, f"all {minus} commit(s) squash-merged")
