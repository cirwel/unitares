"""Tests for dependabot_triage's coverage gate (#finder-2026-08-21 c1).

The gate's contract: unreachability is ALWAYS reported, but only an
unexpected unreachable repo fails the run. An unreachable set that is
exactly a subset of the declared allowlist is partial-by-policy (green
with an explicit caveat in the markdown) — anything else exits 1.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dev" / "dependabot_triage.py"


@pytest.fixture(scope="module")
def triage():
    spec = importlib.util.spec_from_file_location("dependabot_triage", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dependabot_triage"] = mod
    spec.loader.exec_module(mod)
    return mod


def _u(*repos):
    return [{"repo": r, "error": "not readable: HTTP 404"} for r in repos]


class TestCoverageExitCode:
    def test_full_coverage_is_green(self, triage):
        assert triage.coverage_exit_code([], set()) == 0
        assert triage.coverage_exit_code([], {"EXAMPLE/private-repo"}) == 0

    def test_allowlisted_unreachable_is_green(self, triage):
        assert triage.coverage_exit_code(
            _u("EXAMPLE/private-repo"), {"EXAMPLE/private-repo"}
        ) == 0

    def test_unexpected_unreachable_is_red(self, triage):
        assert triage.coverage_exit_code(_u("example/server"), set()) == 1
        assert triage.coverage_exit_code(
            _u("example/server"), {"EXAMPLE/private-repo"}
        ) == 1

    def test_mixed_set_is_red_even_with_partial_allowlist(self, triage):
        assert triage.coverage_exit_code(
            _u("EXAMPLE/private-repo", "example/client"),
            {"EXAMPLE/private-repo"},
        ) == 1

    def test_empty_allowlist_keeps_the_original_contract(self, triage):
        # With no allowlist the gate is byte-equivalent to the old behavior:
        # any unreachable repo fails.
        assert triage.coverage_exit_code(_u("EXAMPLE/private-repo"), set()) == 1

    def test_repo_matching_is_case_insensitive(self, triage):
        # GitHub repo names are case-insensitive; spelling must not decide.
        assert triage.coverage_exit_code(
            _u("example/private-repo"), {"EXAMPLE/private-repo"}
        ) == 0
        assert triage.coverage_exit_code(
            _u("EXAMPLE/Private-Repo"), {"example/private-repo"}
        ) == 0


class TestMarkdownCaveat:
    def _report(self, triage, unreachable):
        r = triage.Report()
        r.unreachable = unreachable
        return r

    def test_partial_by_policy_banner_present_when_allowed(self, triage):
        md = triage.render_markdown(
            self._report(triage, _u("EXAMPLE/private-repo")),
            allowed_partial=True,
        )
        assert "Partial-by-policy" in md
        # The honesty contract's original warning must survive alongside it.
        assert "Do not read this report as fleet-wide" in md
        assert "EXAMPLE/private-repo" in md

    def test_no_banner_on_a_real_coverage_failure(self, triage):
        md = triage.render_markdown(
            self._report(triage, _u("example/server")), allowed_partial=False,
        )
        assert "Partial-by-policy" not in md
        assert "Do not read this report as fleet-wide" in md

    def test_no_banner_and_no_unreachable_section_on_full_coverage(self, triage):
        md = triage.render_markdown(self._report(triage, []))
        assert "Partial-by-policy" not in md
        assert "All target repos were read successfully" in md


class TestRepositorySelection:
    @pytest.mark.parametrize("arguments", [[], ["--repos", ""], ["--repos", "  "],
                                            ["--repos", ",,"], ["--repos", "example/server,"],
                                            ["--repos", "server"]])
    def test_missing_or_invalid_selection_fails_before_reads(self, triage, monkeypatch, arguments):
        monkeypatch.setattr(sys, "argv", [str(SCRIPT), *arguments])
        monkeypatch.setattr(triage, "triage_repo", lambda *args, **kwargs: pytest.fail("unexpected GitHub read"))
        with pytest.raises(SystemExit) as error:
            triage.main()
        assert error.value.code == 2

    def test_only_explicit_repositories_are_read(self, triage, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--repos", " example/server , other/client ", "--json"])
        requested = []
        monkeypatch.setattr(triage, "triage_repo", lambda repo, report, **kwargs: requested.append(repo))
        assert triage.main() == 0
        assert requested == ["example/server", "other/client"]
        assert '"unreachable": []' in capsys.readouterr().out

    def test_repository_workflow_has_explicit_local_scope(self):
        import yaml

        workflow = yaml.load((REPO_ROOT / ".github/workflows/dependabot-triage.yml").read_text(),
                             Loader=yaml.BaseLoader)
        assert set(workflow["on"]) == {"workflow_dispatch"}
        step = next(step for step in workflow["jobs"]["triage"]["steps"] if step.get("id") == "triage")
        assert '--repos "$GITHUB_REPOSITORY"' in step["run"]
        assert step["env"]["GH_TOKEN"] == "${{ github.token }}"


class TestGitHubExecutable:
    def test_custom_reader_receives_original_argv_for_every_read(self, triage, monkeypatch):
        from types import SimpleNamespace

        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout='[]', stderr='')

        monkeypatch.setattr(triage.subprocess, "run", run)
        monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--repos", "example/server",
                                         "--gh-executable", "/private tools/reader"])
        assert triage.main() == 0
        assert calls == [
            ["/private tools/reader", "repo", "view", "example/server", "--json", "name"],
            ["/private tools/reader", "pr", "list", "--repo", "example/server", "--author",
             "app/dependabot", "--state", "open", "--limit", "50", "--json",
             "number,title,mergeable,mergeStateStatus,statusCheckRollup,"
             "headRefOid,state,isDraft,labels,autoMergeRequest,url"],
        ]

    @pytest.mark.parametrize("missing", [False, True])
    def test_reader_failure_is_reported_without_fallback(self, triage, monkeypatch, missing):
        from types import SimpleNamespace

        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if missing:
                raise FileNotFoundError("reader missing")
            return SimpleNamespace(returncode=2, stdout='', stderr='reader refused')

        monkeypatch.setattr(triage.subprocess, "run", run)
        report = triage.Report()
        triage.triage_repo("example/server", report, gh_executable="configured-reader")
        assert len(calls) == 1
        assert calls[0][0] == "configured-reader"
        assert report.repos_scanned == []
        assert report.unreachable[0]["repo"] == "example/server"


class TestStewardshipReceipt:
    def _verdict(self, triage, **overrides):
        values = {
            "repo": "example/server",
            "number": 17,
            "title": "chore(deps): bump pydantic from 2.1.0 to 2.1.1",
            "verdict": triage.MERGE,
            "reason": "patch bump",
            "checks": "SUCCESS:2",
            "mergeable": "MERGEABLE",
            "check_gate": "pass",
            "head_sha": "abc123",
            "merge_state": "CLEAN",
            "github_state": "OPEN",
            "approved": False,
            "armed": False,
            "url": "https://github.com/example/server/pull/17",
        }
        values.update(overrides)
        return triage.Verdict(**values)

    def test_receipt_claims_a_stable_role_and_not_a_process_identity(self, triage):
        report = triage.Report(
            verdicts=[self._verdict(triage)],
            repos_scanned=["example/server"],
        )
        receipt = triage.build_stewardship_receipt(
            report,
            repos=["example/server"],
            observed_at="2026-10-27T12:00:00Z",
        )
        item = receipt["items"][0]
        assert receipt["schema"] == "unitares.dependency_steward.v1"
        assert receipt["owner_role"] == item["owner_role"] == "dependency-steward"
        assert item["phase"] == "ready"
        assert item["terminal_state"] is None
        assert triage.receipt_needs_attention(receipt)

    def test_only_github_merged_or_closed_completes_a_tracked_pr(self, triage, monkeypatch):
        prior_report = triage.Report(
            verdicts=[self._verdict(triage, armed=True, merge_state="BLOCKED")],
            repos_scanned=["example/server"],
        )
        prior = triage.build_stewardship_receipt(
            prior_report,
            repos=["example/server"],
            observed_at="2026-10-27T12:00:00Z",
        )
        calls = []

        def merged(args, *, executable="gh"):
            calls.append((args, executable))
            return {
                "state": "MERGED",
                "mergedAt": "2026-10-27T12:15:00Z",
                "closedAt": "2026-10-27T12:15:00Z",
                "headRefOid": "abc123",
                "url": "https://github.com/example/server/pull/17",
            }

        monkeypatch.setattr(triage, "gh_json", merged)
        receipt = triage.build_stewardship_receipt(
            triage.Report(repos_scanned=["example/server"]),
            repos=["example/server"],
            previous=prior,
            gh_executable="scoped-reader",
            observed_at="2026-10-27T12:20:00Z",
        )
        item = receipt["items"][0]
        assert item["phase"] == "terminal"
        assert item["terminal_state"] == "MERGED"
        assert item["completed_at"] == "2026-10-27T12:15:00Z"
        assert calls == [
            (["pr", "view", "17", "--repo", "example/server", "--json",
              "state,mergedAt,closedAt,headRefOid,url"], "scoped-reader")
        ]

    def test_missing_open_pr_is_not_completion_when_terminal_read_fails(self, triage, monkeypatch):
        prior_report = triage.Report(
            verdicts=[self._verdict(triage, armed=True)],
            repos_scanned=["example/server"],
        )
        prior = triage.build_stewardship_receipt(
            prior_report,
            repos=["example/server"],
            observed_at="2026-10-27T12:00:00Z",
        )

        def refused(*args, **kwargs):
            raise RuntimeError("reader refused")

        monkeypatch.setattr(triage, "gh_json", refused)
        receipt = triage.build_stewardship_receipt(
            triage.Report(repos_scanned=["example/server"]),
            repos=["example/server"],
            previous=prior,
            observed_at="2026-10-27T12:20:00Z",
        )
        item = receipt["items"][0]
        assert item["phase"] == "unreachable"
        assert item["terminal_state"] is None
        assert receipt["reconciliation_errors"] == [
            {"key": "example/server#17", "error": "reader refused"}
        ]
        assert triage.receipt_needs_attention(receipt)

    def test_armed_blocked_pr_with_passing_checks_is_explicit_attention(self, triage):
        report = triage.Report(
            verdicts=[self._verdict(triage, armed=True, merge_state="BLOCKED")],
            repos_scanned=["example/server"],
        )
        receipt = triage.build_stewardship_receipt(
            report,
            repos=["example/server"],
            observed_at="2026-10-27T12:00:00Z",
        )
        assert receipt["items"][0]["phase"] == "opaque_blocked"
        assert triage.receipt_needs_attention(receipt)

    def test_receipt_write_is_valid_json(self, triage, tmp_path):
        path = tmp_path / "state" / "receipt.json"
        payload = {"schema": "unitares.dependency_steward.v1", "items": []}
        triage.write_receipt(path, payload)
        assert json.loads(path.read_text()) == payload
        assert list(path.parent.glob(".*.tmp.*")) == []
