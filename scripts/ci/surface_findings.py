#!/usr/bin/env python3
"""CI issue-surfacing bridge.

Runs deterministic "issue surfacing" collectors in CI and emits a normalized,
fingerprinted findings feed. A companion GitHub Actions step
(``.github/workflows/surface-findings.yml``) turns each *new* fingerprint into
a deduped GitHub issue.

This is the CI-side counterpart to the in-editor / in-server surfacing agents
(``agents/watcher``, ``agents/vigil``). Those run against a live governance
server and an LLM detector, neither of which exists on a stock GitHub runner.
The collectors here are stdlib/CLI-only so they run on a vanilla
``ubuntu-latest`` with no Postgres, no Ollama, and no secrets beyond the
workflow's ``GITHUB_TOKEN``.

Collectors (select with ``--collectors``):

    ruff     ``ruff check ... --output-format=json`` lint diagnostics.
    doctor   ``scripts/dev/unitares_doctor.py --json`` (surfaces fail/warn checks).
    watcher  OPTIONAL LLM scan. Skipped unless ``--enable-watcher`` is passed
             AND a ``WATCHER_OLLAMA_URL`` endpoint answers. Off by default so
             CI output stays deterministic.

Fingerprints reuse ``agents.common.findings.compute_fingerprint`` so a finding
surfaced from CI shares the dedup identity scheme with the rest of UNITARES —
the same ``(source, rule, file, line)`` tuple always hashes to the same 16-hex
id, which is what lets the workflow skip issues it has already opened.

The script never touches the GitHub API. It only emits JSON (so it is trivially
unit-testable and runs identically on a laptop); issue creation/dedup lives in
the workflow's ``github-script`` step.

Usage:
    python3 scripts/ci/surface_findings.py                       # all default collectors, repo root
    python3 scripts/ci/surface_findings.py --paths src agents    # scope ruff to a subset
    python3 scripts/ci/surface_findings.py --collectors ruff     # one collector
    python3 scripts/ci/surface_findings.py --output findings.json
    python3 scripts/ci/surface_findings.py --fail-on high        # exit 1 if any high+ finding

Exit code is 0 (advisory) unless ``--fail-on <severity>`` is set and a finding
at or above that severity is present.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

# Repo root used to normalize paths to repo-relative form before hashing.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def compute_fingerprint(parts: Iterable[Any]) -> str:
    """16-hex-char SHA-256 prefix of a pipe-joined identity string.

    Kept byte-identical with ``agents.common.findings.compute_fingerprint`` (a
    parity test pins this) so a finding surfaced from CI shares the dedup id
    with the rest of UNITARES. Inlined rather than imported so this script stays
    stdlib-only — the canonical module pulls in ``httpx`` at import time, which
    we do not want on the CI surfacing path or on a laptop dry-run.
    """
    normalized = "|".join(str(p) for p in parts)
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


# Ordered worst-first so ``--fail-on`` threshold comparisons are simple index
# math and the summary sorts criticals to the top.
# ``doctor`` is intentionally NOT a default: it checks for a working install
# (Postgres reachable, schema present, anchor dir), which is meaningful on a
# deployment host but is pure noise on a stock CI runner that has none of it.
# Enable it explicitly (``--collectors ruff doctor``) only on a job that
# provisions the governance stack.
SEVERITY_ORDER = ("critical", "high", "medium", "low")
DEFAULT_COLLECTORS = ("ruff",)


@dataclass
class Finding:
    """One normalized, fingerprinted CI finding.

    ``fingerprint`` names the finding's *identity* (stable across reruns) and is
    the dedup key the workflow uses to decide whether an issue already exists.
    """

    source: str  # ruff | doctor | watcher
    severity: str  # critical | high | medium | low
    title: str
    message: str
    file: str = ""
    line: int = 0
    rule: str = ""  # lint code / check name / pattern
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if self.severity not in SEVERITY_ORDER:
            self.severity = "medium"
        if not self.fingerprint:
            self.fingerprint = compute_fingerprint(
                [self.source, self.rule, _repo_rel(self.file), self.line]
            )


def _repo_rel(path: str) -> str:
    """Best-effort repo-relative path so the same file hashes identically no
    matter the absolute checkout prefix (CI runner vs. laptop)."""
    if not path:
        return ""
    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT))
    except (ValueError, OSError):
        return path


# ---------------------------------------------------------------------------
# Collectors
#
# Each returns ``(findings, note)``. ``note`` is a human-readable status line
# ("12 diagnostics", "ruff not installed — skipped") surfaced in the summary so
# a skipped collector is visible rather than silently absent.
# ---------------------------------------------------------------------------


def collect_ruff(paths: Sequence[str]) -> tuple[list[Finding], str]:
    targets = list(paths) or ["."]
    try:
        proc = subprocess.run(
            ["ruff", "check", *targets, "--output-format=json"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError:
        return [], "ruff not installed — skipped"
    except subprocess.TimeoutExpired:
        return [], "ruff timed out after 300s — skipped"

    raw = (proc.stdout or "").strip()
    if not raw:
        # ruff prints nothing on a clean tree (exit 0) and diagnostics-as-JSON
        # on a dirty one (exit 1). A non-empty stderr with empty stdout means a
        # real invocation error worth surfacing in the note.
        if proc.returncode not in (0, 1):
            return [], f"ruff errored (rc={proc.returncode}): {(proc.stderr or '').strip()[:200]}"
        return [], "0 diagnostics"

    try:
        diagnostics = json.loads(raw)
    except json.JSONDecodeError:
        return [], f"ruff JSON parse failed (rc={proc.returncode})"

    findings: list[Finding] = []
    for d in diagnostics:
        code = d.get("code") or "RUFF"
        loc = d.get("location") or {}
        filename = d.get("filename", "")
        findings.append(
            Finding(
                source="ruff",
                severity=_ruff_severity(code),
                title=f"ruff {code}: {d.get('message', '').strip()[:80]}",
                message=d.get("message", "").strip(),
                file=filename,
                line=int(loc.get("row", 0) or 0),
                rule=code,
            )
        )
    return findings, f"{len(findings)} diagnostics"


def _ruff_severity(code: str) -> str:
    """Map a ruff rule code to a finding severity.

    Pyflakes F8xx (undefined name / redefinition / f-string-missing-
    placeholders) are genuine bug smells -> high. Other F (e.g. F401 unused
    import) and bug-bear/security families -> medium. Stylistic E/W -> low.
    """
    if code.startswith("F8"):
        return "high"
    if code[:1] in {"F", "B", "S"}:  # pyflakes, bugbear, bandit-style security
        return "medium"
    if code[:1] in {"E", "W", "I", "Q", "C"}:
        return "low"
    return "medium"


def collect_doctor(_paths: Sequence[str]) -> tuple[list[Finding], str]:
    doctor = PROJECT_ROOT / "scripts" / "dev" / "unitares_doctor.py"
    if not doctor.exists():
        return [], "unitares_doctor.py not found — skipped"
    try:
        proc = subprocess.run(
            [sys.executable, str(doctor), "--mode", "local", "--json"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return [], "doctor timed out after 120s — skipped"

    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return [], f"doctor JSON parse failed (rc={proc.returncode})"

    findings: list[Finding] = []
    for r in payload.get("results", []):
        status = r.get("status")
        if status not in {"fail", "warn"}:
            continue
        severity = "high" if status == "fail" else "medium"
        name = r.get("name", "check")
        findings.append(
            Finding(
                source="doctor",
                severity=severity,
                title=f"doctor {status}: {name}",
                message=r.get("message", "") + (f"\n\n{r['detail']}" if r.get("detail") else ""),
                file="",
                line=0,
                rule=name,
            )
        )
    return findings, f"{len(findings)} fail/warn checks"


def collect_watcher(paths: Sequence[str]) -> tuple[list[Finding], str]:
    """Optional LLM-backed scan. Off unless the endpoint actually answers.

    The watcher's detector talks to an Ollama-compatible endpoint that does not
    exist on a stock GitHub runner, so this collector probes the endpoint first
    and skips cleanly when it is absent — the common CI case.
    """
    url = os.environ.get("WATCHER_OLLAMA_URL", "http://localhost:11434")
    probe = url.rstrip("/").rsplit("/v1", 1)[0] + "/api/tags"
    try:
        with urllib.request.urlopen(probe, timeout=3):
            pass
    except (urllib.error.URLError, OSError, ValueError):
        return [], f"watcher endpoint unreachable ({probe}) — skipped"

    # Endpoint is live: defer to the real watcher agent for the actual scan.
    findings: list[Finding] = []
    notes: list[str] = []
    try:
        from agents.watcher.agent import scan_file  # type: ignore
    except Exception as e:  # pragma: no cover - import guarded for CI
        return [], f"watcher agent import failed: {e}"

    for path in paths:
        p = PROJECT_ROOT / path if not Path(path).is_absolute() else Path(path)
        if not p.is_file() or p.suffix != ".py":
            continue
        try:
            for wf in scan_file(str(p)) or []:
                findings.append(
                    Finding(
                        source="watcher",
                        severity=getattr(wf, "severity", "medium"),
                        title=f"watcher {getattr(wf, 'pattern', '?')}: {getattr(wf, 'hint', '')[:80]}",
                        message=getattr(wf, "hint", ""),
                        file=getattr(wf, "file", str(p)),
                        line=int(getattr(wf, "line", 0) or 0),
                        rule=getattr(wf, "pattern", ""),
                    )
                )
        except Exception as e:  # pragma: no cover - per-file resilience
            notes.append(f"{path}: {e}")
    note = f"{len(findings)} findings"
    if notes:
        note += f" ({len(notes)} files errored)"
    return findings, note


COLLECTORS = {
    "ruff": collect_ruff,
    "doctor": collect_doctor,
    "watcher": collect_watcher,
}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class Report:
    generated_at: str
    collectors: dict[str, str] = field(default_factory=dict)  # name -> status note
    findings: list[Finding] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        sev_idx = {s: i for i, s in enumerate(SEVERITY_ORDER)}
        ordered = sorted(
            self.findings,
            key=lambda f: (sev_idx.get(f.severity, 99), f.source, f.file, f.line),
        )
        counts = {s: 0 for s in SEVERITY_ORDER}
        for f in ordered:
            counts[f.severity] += 1
        return {
            "generated_at": self.generated_at,
            "collectors": self.collectors,
            "summary": {"total": len(ordered), "by_severity": counts},
            "findings": [asdict(f) for f in ordered],
        }


def run(collectors: Sequence[str], paths: Sequence[str], enable_watcher: bool) -> Report:
    report = Report(generated_at=datetime.now(timezone.utc).isoformat())
    seen: set[str] = set()
    for name in collectors:
        if name == "watcher" and not enable_watcher:
            report.collectors[name] = "disabled (pass --enable-watcher)"
            continue
        fn = COLLECTORS.get(name)
        if fn is None:
            report.collectors[name] = "unknown collector — skipped"
            continue
        findings, note = fn(paths)
        report.collectors[name] = note
        # Cross-collector dedup: identical fingerprint from two sources is one
        # finding, not two issues.
        for f in findings:
            if f.fingerprint in seen:
                continue
            seen.add(f.fingerprint)
            report.findings.append(f)
    return report


def _split_csv(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        out.extend(part for part in v.replace(",", " ").split() if part)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--collectors",
        nargs="*",
        default=list(DEFAULT_COLLECTORS),
        help=f"collectors to run (default: {' '.join(DEFAULT_COLLECTORS)})",
    )
    parser.add_argument(
        "--paths",
        nargs="*",
        default=[],
        help="paths to scope file-oriented collectors to (default: repo root)",
    )
    parser.add_argument("--enable-watcher", action="store_true", help="run the optional LLM watcher collector")
    parser.add_argument("--output", help="write the findings report JSON to this path")
    parser.add_argument(
        "--fail-on",
        choices=SEVERITY_ORDER,
        help="exit 1 if any finding at or above this severity is present",
    )
    args = parser.parse_args(argv)

    collectors = _split_csv(args.collectors)
    paths = _split_csv(args.paths)
    report = run(collectors, paths, enable_watcher=args.enable_watcher)
    payload = report.to_json()

    if args.output:
        Path(args.output).write_text(json.dumps(payload, indent=2) + "\n")

    # Human summary to stderr so stdout stays a clean JSON channel when piped.
    print("CI issue-surfacing report", file=sys.stderr)
    for name, note in payload["collectors"].items():
        print(f"  {name:8} {note}", file=sys.stderr)
    counts = payload["summary"]["by_severity"]
    print(
        f"  total: {payload['summary']['total']} "
        f"(critical={counts['critical']} high={counts['high']} "
        f"medium={counts['medium']} low={counts['low']})",
        file=sys.stderr,
    )

    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")

    if args.fail_on:
        threshold = SEVERITY_ORDER.index(args.fail_on)
        worst = min(
            (SEVERITY_ORDER.index(f["severity"]) for f in payload["findings"]),
            default=len(SEVERITY_ORDER),
        )
        if worst <= threshold:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
