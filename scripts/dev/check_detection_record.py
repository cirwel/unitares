#!/usr/bin/env python3
"""Check that a fix PR records who noticed the defect it fixes.

The 2026-11-17 census of EISV instrument changes found that the record says
what changed and why, but not who noticed that an earlier change was wrong:
no commit in the history named a detection. Silence there reads both as
"no defect" and as "not written down". A PR whose body classifies it as a fix
(the same rule as `scripts/ops/pr-babysitter.sh`: it states a classification,
and every one it states is `fix`) therefore carries a detection block, placed
directly above its provenance signature:

    Defect-in: <the change that introduced it: #N, a commit, an era id> | unknown
    Detected-by: operator | agent:<uuid> | check:<name> | external:<who> | unknown
    Detection-trigger: unprompted | prompted | automated | unknown
    Detected-at: <YYYY-MM-DD or ISO time> | unknown
    Detection-record: <shared-memory discovery id> | none
    Prompted-by: operator | agent:<uuid> | unknown     (only when prompted)

`unknown` is always accepted: the check asks for an honest record, not for
knowledge the author lacks. Lines inside fenced code blocks and block quotes
do not count, so a quoted example cannot satisfy it. The values are
self-reported, like the `Model:` line; the merge queue copies the block into
the squash commit beside the signature.

Usage: check_detection_record.py [--body-file FILE]   (default: $PR_BODY, else stdin)
Exit 0 when the PR is not a fix or its block is complete; 1 otherwise.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

REQUIRED = ("Defect-in", "Detected-by", "Detection-trigger", "Detected-at", "Detection-record")
OPTIONAL = ("Prompted-by",)
FIELDS = REQUIRED + OPTIONAL

# Same pattern as the babysitter's readiness route.
_CLASSIFICATION = re.compile(r"\b(?:classification|class)\s*:\s*[*` ]*(fix|capability)\b", re.IGNORECASE)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FIELD = re.compile(r"^(" + "|".join(re.escape(f) for f in FIELDS) + r"):[ \t]*(.*?)[ \t]*$", re.IGNORECASE)
_UUID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
_TOKEN = r"[A-Za-z0-9][A-Za-z0-9._/+:@#-]{0,99}"

_VALID = {
    "defect-in": re.compile(r"^(?!\s)[^\r\n]{1,200}$"),
    "detected-by": re.compile(
        rf"^(?:operator|unknown|agent:{_UUID}|check:{_TOKEN}|external:(?!\s)[^\r\n]{{1,100}})$", re.IGNORECASE),
    "detection-trigger": re.compile(r"^(?:unprompted|prompted|automated|unknown)$", re.IGNORECASE),
    "detected-at": re.compile(
        r"^(?:unknown|\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?)$",
        re.IGNORECASE),
    "detection-record": re.compile(rf"^(?:none|unknown|{_TOKEN})$", re.IGNORECASE),
    "prompted-by": re.compile(rf"^(?:operator|unknown|agent:{_UUID})$", re.IGNORECASE),
}

_HINT = {
    "defect-in": "a PR (#N), commit, era id, or `unknown`",
    "detected-by": "`operator`, `agent:<uuid>`, `check:<name>`, `external:<who>`, or `unknown`",
    "detection-trigger": "`unprompted`, `prompted`, `automated`, or `unknown`",
    "detected-at": "a date (YYYY-MM-DD), an ISO time, or `unknown`",
    "detection-record": "a shared-memory discovery id, `none`, or `unknown`",
    "prompted-by": "`operator`, `agent:<uuid>`, or `unknown`",
}


def is_fix(body: str) -> bool:
    """True when the body states a classification and every one it states is `fix`."""
    found = [m.group(1).lower() for m in _CLASSIFICATION.finditer(body or "")]
    return bool(found) and all(c == "fix" for c in found)


def detection_fields(body: str) -> dict[str, str]:
    """The last value of each detection field, outside fenced code and block quotes."""
    fields: dict[str, str] = {}
    fence = ""
    for line in (body or "").splitlines():
        run = _FENCE.match(line)
        if run:
            marker = run.group(1)
            if not fence:
                if not (marker[0] == "`" and "`" in run.group(2)):
                    fence = marker
                continue
            if marker[0] == fence[0] and len(marker) >= len(fence) and not run.group(2).strip():
                fence = ""
            continue
        if fence or line.lstrip().startswith(">"):
            continue
        match = _FIELD.match(line)
        if match:
            fields[match.group(1).lower()] = match.group(2)
    return fields


def problems(body: str) -> list[str]:
    """What a fix PR's detection block is missing or gets wrong; empty when it passes."""
    if not is_fix(body):
        return []
    fields = detection_fields(body)
    out: list[str] = []
    for name in REQUIRED:
        key = name.lower()
        if key not in fields:
            out.append(f"missing `{name}:` ({_HINT[key]})")
        elif not _VALID[key].match(fields[key]):
            out.append(f"`{name}: {fields[key]}` is not {_HINT[key]}")
    if "prompted-by" in fields:
        if not _VALID["prompted-by"].match(fields["prompted-by"]):
            out.append(f"`Prompted-by: {fields['prompted-by']}` is not {_HINT['prompted-by']}")
        elif fields.get("detection-trigger", "").lower() not in ("prompted", "unknown"):
            out.append("`Prompted-by:` is given but `Detection-trigger:` is not `prompted`")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--body-file", help="read the PR body from this file")
    args = parser.parse_args(argv)
    if args.body_file:
        with open(args.body_file, encoding="utf-8") as fh:
            body = fh.read()
    elif "PR_BODY" in os.environ:
        body = os.environ["PR_BODY"]
    else:
        body = sys.stdin.read()
    found = problems(body)
    if not found:
        print("detection record: ok" if is_fix(body) else "detection record: not a fix PR; nothing required")
        return 0
    print("::error::A `Classification: fix` PR records who noticed the defect it fixes "
          "(docs/operations/github-workflow-conventions.md, Detection record). `unknown` is accepted in every field.")
    for p in found:
        print(f"::error::{p}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
