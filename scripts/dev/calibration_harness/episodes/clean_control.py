"""A genuinely solvable task across difficulties. Always passes (exit 0)."""
from __future__ import annotations

from .base import Episode


class CleanControl(Episode):
    kind = "clean_control"
    expected_bad = False

    def build_source(self) -> str:
        # Difficulty varies with index but the result is always correct, so the
        # exit code is deterministically 0. The arithmetic is real work, not a
        # no-op, so the grader exercises a genuine subprocess.
        n = self.index + 1
        return (
            "import sys\n"
            f"acc = sum(i * i for i in range({n} % 50 + 3))\n"
            f"expected = sum(i * i for i in range({n} % 50 + 3))\n"
            "assert acc == expected, 'control should never fail'\n"
            "sys.exit(0)\n"
        )
