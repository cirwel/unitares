"""A retained author signature cannot resume a handed-over PR."""
import io
import re
import unittest
from unittest.mock import patch

from scripts.dev import _ship_resume as ship


AUTHOR = "00000000-0000-4000-8000-000000000042"
SUCCESSOR = "00000000-0000-4000-8000-000000000043"
HEAD = "a" * 40


class HandoverResumeTests(unittest.TestCase):
    def pr(self, *, handed_over, marker="Delivery mode: manual"):
        prefix = f"{marker}\n" if handed_over else ""
        return {"baseRefName": "master", "headRefName": "codex/workflow",
                "headRefOid": HEAD, "state": "OPEN", "isCrossRepository": False,
                "isDraft": True, "labels": [],
                "body": f"{prefix}Model: gpt-6.1-sol\nUNITARES agent: {AUTHOR}\n"}

    @staticmethod
    def git_run(*args):
        return {("git", "branch", "--show-current"): "codex/workflow\n",
                ("git", "rev-parse", "HEAD"): HEAD + "\n",
                ("git", "status", "--porcelain=v1", "-uall"): ""}[args]

    def test_original_owner_can_resume_before_any_handover(self):
        with patch.object(ship, "run", side_effect=self.git_run):
            self.assertEqual(ship.owned_head(self.pr(handed_over=False), AUTHOR), HEAD)
            with self.assertRaisesRegex(ship.Parked, "terminal author footer"):
                ship.owned_head(self.pr(handed_over=False), SUCCESSOR)

    def test_handover_parks_every_actor_despite_retained_author_footer(self):
        # Includes readiness-only, branch-writing-only and complete handovers:
        # their scopes live in the record; none permits automatic resume.
        for marker in (
            "Delivery mode: manual",
            "Delivery mode: manual\r",
            "- Delivery mode: manual",
            "**Delivery mode: manual**",
        ):
            for actor in (AUTHOR, SUCCESSOR):
                with self.subTest(marker=marker, actor=actor), patch.object(ship, "run", side_effect=self.git_run):
                    with self.assertRaisesRegex(ship.Parked, "manual PR path") as ctx:
                        ship.owned_head(self.pr(handed_over=True, marker=marker), actor)
                    self.assertFalse(ctx.exception.retryable)

    def test_contributing_successor_signature_does_not_replace_terminal_author(self):
        pr = self.pr(handed_over=False)
        pr["body"] = f"Model: gemini-4-argon\nUNITARES agent: {SUCCESSOR}\n\n" + pr["body"]
        footers = re.findall(r"^UNITARES agent: ([0-9a-fA-F-]+)\s*$", pr["body"], re.MULTILINE)
        self.assertEqual(footers, [SUCCESSOR, AUTHOR])
        self.assertEqual(footers[-1], AUTHOR)
        for actor in (AUTHOR, SUCCESSOR):
            with self.subTest(actor=actor), patch.object(ship, "run", side_effect=self.git_run):
                with self.assertRaisesRegex(ship.Parked, "multiple agent signatures.*manual PR path") as ctx:
                    ship.owned_head(pr, actor)
                self.assertFalse(ctx.exception.retryable)

    def test_permanent_handover_refusal_omits_retry_command(self):
        for is_draft, labels in ((True, []), (False, [{"name": "approved-to-merge"}])):
            with self.subTest(is_draft=is_draft, labels=labels):
                pr = self.pr(handed_over=True)
                pr["isDraft"] = is_draft
                pr["labels"] = labels
                stderr = io.StringIO()
                with patch.object(ship, "resume", side_effect=ship.Parked("Delivery ownership changed", retryable=False)), \
                     patch.object(ship, "snapshot", return_value=pr), \
                     patch("sys.argv", ["ship-resume"]), \
                     patch("sys.stderr", stderr):
                    self.assertEqual(ship.main(), 2)
                self.assertIn("[ship] parked: Delivery ownership changed", stderr.getvalue())
                self.assertNotIn("Delivery failed while the live PR", stderr.getvalue())
                self.assertNotIn("[ship] resume:", stderr.getvalue())
