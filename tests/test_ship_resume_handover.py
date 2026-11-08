"""A retained author signature cannot resume a handed-over PR."""
import unittest
from unittest.mock import patch

from scripts.dev import _ship_resume as ship


AUTHOR = "00000000-0000-4000-8000-000000000042"
SUCCESSOR = "00000000-0000-4000-8000-000000000043"
HEAD = "a" * 40


class HandoverResumeTests(unittest.TestCase):
    def pr(self, *, handed_over):
        marker = "Delivery mode: manual\n" if handed_over else ""
        return {"baseRefName": "master", "headRefName": "codex/workflow",
                "headRefOid": HEAD, "state": "OPEN", "isCrossRepository": False,
                "body": f"{marker}Model: gpt-6.1-sol\nUNITARES agent: {AUTHOR}\n"}

    @staticmethod
    def git_run(*args):
        return {("git", "branch", "--show-current"): "codex/workflow\n",
                ("git", "rev-parse", "HEAD"): HEAD + "\n",
                ("git", "status", "--porcelain=v1", "-uall"): ""}[args]

    def test_original_owner_can_resume_before_any_handover(self):
        with patch.object(ship, "run", side_effect=self.git_run):
            self.assertEqual(ship.owned_head(self.pr(handed_over=False), AUTHOR), HEAD)

    def test_handover_parks_every_actor_despite_retained_author_footer(self):
        # Includes readiness-only, branch-writing-only and complete handovers:
        # their scopes live in the record; none permits automatic resume.
        for actor in (AUTHOR, SUCCESSOR):
            with self.subTest(actor=actor), patch.object(ship, "run", side_effect=self.git_run):
                with self.assertRaisesRegex(ship.Parked, "manual PR path"):
                    ship.owned_head(self.pr(handed_over=True), actor)

    def test_contributing_successor_signature_does_not_replace_terminal_author(self):
        pr = self.pr(handed_over=True)
        pr["body"] = (f"Model: gemini-4-argon\nUNITARES agent: {SUCCESSOR}\n\n" + pr["body"])
        with patch.object(ship, "run", side_effect=self.git_run):
            with self.assertRaisesRegex(ship.Parked, "manual PR path"):
                ship.owned_head(pr, AUTHOR)
