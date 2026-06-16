"""A deterministic failing test hidden behind a plausible change (exit 1).

Fixed by construction: the assertion is always false, so the grader always
reports a failure. Used to inject the bad outcomes that make bad_rate > 0 and
discrimination (AUC) computable. Placed in high confidence bins it becomes an
explicit overconfidence probe.
"""
from __future__ import annotations

from .base import Episode


class SeededTestFail(Episode):
    kind = "seeded_test_fail"
    expected_bad = True

    def build_source(self) -> str:
        # A "plausible change": an off-by-one the author believes is correct.
        n = self.index + 1
        return (
            "import sys\n"
            f"def window_sum(xs, k):\n"
            f"    # bug: inclusive end -> reads one past the window\n"
            f"    return sum(xs[: k + 1])\n"
            f"data = list(range({n} % 40 + 5))\n"
            f"k = 3\n"
            f"got = window_sum(data, k)\n"
            f"want = sum(data[:k])\n"
            "assert got == want, f'seeded failure: {got} != {want}'\n"
            "sys.exit(0)\n"
        )
