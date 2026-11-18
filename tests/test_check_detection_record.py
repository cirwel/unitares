"""A fix PR records who noticed the defect it fixes (scripts/dev/check_detection_record.py)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.dev import check_detection_record as cdr

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/dev/check_detection_record.py"
AGENT = "11111111-2222-4333-8444-555555555555"
BLOCK = (
    "Defect-in: #2831\n"
    "Detected-by: operator\n"
    "Detection-trigger: unprompted\n"
    "Detected-at: 2026-11-04\n"
    "Detection-record: none\n"
)
SIG = f"\nModel: gpt-5\nUNITARES agent: {AGENT}\n"


def test_a_pr_that_is_not_a_fix_needs_no_record():
    assert cdr.problems("Classification: capability\n" + SIG) == []
    assert cdr.problems("No classification at all.\n" + SIG) == []


def test_a_capability_that_quotes_fix_is_still_a_capability():
    # Same rule as the babysitter's readiness route: every stated classification must be fix.
    assert cdr.problems("Classification: capability\nnot `Classification: fix`\n") == []


def test_a_fix_without_the_block_names_every_missing_field():
    found = cdr.problems("Classification: fix\n" + SIG)
    assert len(found) == len(cdr.REQUIRED)
    assert all("missing" in p for p in found)


def test_a_complete_block_passes():
    assert cdr.problems("Classification: fix\n\n" + BLOCK + SIG) == []


def test_unknown_is_accepted_in_every_field():
    body = "Classification: fix\n" + "".join(f"{f}: unknown\n" for f in cdr.REQUIRED)
    assert cdr.problems(body) == []


@pytest.mark.parametrize("line", [
    "Detected-by: agent:" + AGENT,
    "Detected-by: check:watcher",
    "Detected-by: external:a user report",
    "Detection-trigger: automated",
    "Detected-at: 2026-11-04T07:43:04Z",
    "Detection-record: 2026-11-17T07:43:04.620488+00:00",
])
def test_accepted_values(line):
    name = line.split(":", 1)[0]
    body = "Classification: fix\n" + "".join(l for l in BLOCK.splitlines(True) if not l.startswith(name + ":"))
    assert cdr.problems(body + line + "\n") == []


@pytest.mark.parametrize("line", [
    "Detected-by: me",
    "Detected-by: agent:not-a-uuid",
    "Detection-trigger: someone asked",
    "Detected-at: last week",
    "Detection-record: two words",
])
def test_rejected_values(line):
    name = line.split(":", 1)[0]
    body = "Classification: fix\n" + "".join(l for l in BLOCK.splitlines(True) if not l.startswith(name + ":"))
    found = cdr.problems(body + line + "\n")
    assert len(found) == 1 and name in found[0]


def test_a_block_in_a_code_fence_or_quote_does_not_count():
    fenced = "Classification: fix\n```\n" + BLOCK + "```\n"
    quoted = "Classification: fix\n" + "".join("> " + l for l in BLOCK.splitlines(True))
    assert len(cdr.problems(fenced)) == len(cdr.REQUIRED)
    assert len(cdr.problems(quoted)) == len(cdr.REQUIRED)


def test_prompted_by_needs_a_prompted_trigger():
    body = "Classification: fix\n" + BLOCK + "Prompted-by: operator\n"
    assert cdr.problems(body) == ["`Prompted-by:` is given but `Detection-trigger:` is not `prompted`"]
    assert cdr.problems(body.replace("unprompted", "prompted")) == []


@pytest.mark.parametrize("who", ["operator", "agent:" + AGENT, "unknown"])
def test_a_prompted_trigger_accepts_each_prompted_by(who):
    body = "Classification: fix\n" + BLOCK.replace("unprompted", "prompted") + f"Prompted-by: {who}\n"
    assert cdr.problems(body) == []


def test_a_prompted_trigger_needs_prompted_by():
    body = "Classification: fix\n" + BLOCK.replace("unprompted", "prompted")
    found = cdr.problems(body)
    assert len(found) == 1 and "Prompted-by" in found[0]
    # A fenced or quoted Prompted-by line does not count.
    assert cdr.problems(body + "```\nPrompted-by: operator\n```\n") == found
    assert cdr.problems(body + "> Prompted-by: operator\n") == found
    assert cdr.problems(body + "Prompted-by: someone\n") == [
        "`Prompted-by: someone` is not `operator`, `agent:<uuid>`, or `unknown`"]


def test_the_last_line_of_a_field_wins():
    body = "Classification: fix\nDetected-by: me\n" + BLOCK
    assert cdr.problems(body) == []


def test_the_cli_exits_nonzero_with_annotations(tmp_path):
    bad = subprocess.run([sys.executable, str(SCRIPT)], input="Classification: fix\n",
                         text=True, capture_output=True)
    assert bad.returncode == 1 and "::error::" in bad.stdout
    body = tmp_path / "body.md"
    body.write_text("Classification: fix\n" + BLOCK)
    ok = subprocess.run([sys.executable, str(SCRIPT), "--body-file", str(body)],
                        text=True, capture_output=True)
    assert ok.returncode == 0 and "ok" in ok.stdout
