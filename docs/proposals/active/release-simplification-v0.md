# Simplify the server release process

**Status:** in progress — Steps 1–3, Decision 2, and Decision 7 implemented; Decision 1 (release-commit anchor implemented; weekly advisory issue workflow remains open) and Decision 4 (`workflow_call` / automated pin PR creation) remain open.
**Last Updated:** 2026-10-25

Scope: cirwel/unitares server releases (tags `vX.Y.Z`). The SDK release chain is mentioned only where it shares a fix. Evidence below was gathered read-only at worktree HEAD 50bece6b5 (VERSION and PUBLISHED_VERSION both 3.3.0, newest tag v3.3.0). The claims come from four reader reports and three design drafts; I did not re-run the scripts or workflows myself. See "What was not verified".

## Problem

The release has more hand-ordered acts than it needs, and most of the incident history comes from ordering and glue between steps, not from the approvals themselves.

**Count of manual acts.** `docs/operations/RELEASE_PROCESS.md` (346 lines) has 8 numbered steps (lines 43-167). Step 2 alone holds about 10 sub-actions (lines 47-77). One reader counted about 19 manual acts per release, including sub-actions, one-time visibility flips, the promotion approval and the pin-PR merge. Another counted about 7 coarse human acts (prep PR, review and merge, signed tag, release page, Promote dispatch, environment approval, pin PR). The two counts differ by granularity, not by disagreement about the steps.

**Prep is an unchained recipe.** `make version-bump` runs only `--bump` and `--update` (Makefile:16-19). The changelog fold, the `## [Unreleased]` header rewrite, the CITATION.cff `date-released` edit, the skill attestation prune and the Docker-input range check are separate hand steps (RELEASE_PROCESS.md:47-88). In a worktree under `wt/`, `check-skill-freshness.sh` resolves external sources wrongly unless `UNITARES_PROJECTS_ROOT` is exported; the only tell is a "cited source(s) absent from this checkout, not covered" note beside a FRESH result. The wrapper only sets the `UNITARES_PROJECTS_ROOT` default (`check-skill-freshness.sh:17`); the note itself is emitted by `scripts/client/_check_freshness.py:599` (RELEASE_PROCESS.md:63-77).

**Hidden and manual glue after the tag.**
- Tag push alone does not publish containers. `publish-container.yml` triggers on `release: published` (lines 3-12), and a grep of `.github/workflows`, `scripts/ci` and `scripts/ops` found no automation that creates a release page or a tag. The hand-written release page is therefore a hidden prerequisite.
- `promote-release.yml` is dispatch-only; the `pin` job pushes `publish/<tag>` but only prints a `gh pr create` command in the step summary (promote-release.yml:6-8, 515-521).
- Pin PR lag after the tag was reported as 8h09m, 2h13m, 27h17m and 7h53m for v3.0.0 to v3.3.0 (n=4; a reader's figures, not independently confirmed), and 14.5 days for v2.22.1 (PR #2341, confirmed: tag 2026-09-08, merged 2026-09-22). Until it merges, public install examples name the previous release.
- `publish-mcp-registry.yml` fires automatically when the pin PR merges (lines 10-13) and is not described in RELEASE_PROCESS.md.
- The last five Promote Release runs all concluded success, so the verify, promote and pin chain itself is not the unreliable part.

**The post-merge window and the gate treadmill.** `changelog_coverage.py` measures `git log --first-parent <prev>..HEAD`, where HEAD is the tree CI checked out (changelog_coverage.py:212-226, 359-371). VERSION is the only stand-down switch: the gates arm while `v<VERSION>` is untagged (lines 319-331). Between the release PR merging and the tag, every later master merge is uncited.
- A reader's simulation in a scratch clone (not preserved, so not re-derivable) with the v3.3.0 tag deleted reported that the gate passed at the release commit f351177ed (47 of 47 cited, 14 exempt) and reported 145 uncited changes plus 81 leftover fragments at 50bece6b5. This is a simulation, not a production run.
- Two ordinary PRs failed for this reason: run 36525337899 (2026-09-29) and run 37277244368 (2026-10-05), both with "1 changelog fragment(s) are still in docs/changelog.d/". The fragment convention (the `changelog_direct_edit.py` module docstring, lines 1-10, and `docs/changelog.d/README.md`) and the release-tree fragment check (`changelog_coverage.py:333-344`) contradict each other in that window.
- Merge-to-tag across the 7 tags from v2.21.0 to v3.3.0: 2m23s, 1h12m, about 0 (v2.22.1), 54s, 3m51s, 49m48s, 36m (median 36m, n=7). Three of 7 exceeded 30 minutes. The prose rule "tag immediately, arm no other auto-merge" (RELEASE_PROCESS.md:97-102) is enforced by nothing.
- The v2.21.0 cut left Release Seams red on master for about 22h45m (PR #2073 body).

**Hand curation of the changelog entry.** The 3.3.0 entry declares 14 exemptions; the whole CHANGELOG at that commit holds 80 `changelog-coverage-exempt` comments, 76 of them `no-user-effect`. Cumulative counts at v3.0.0, v3.1.0, v3.2.0 and v3.3.0 were 27, 42, 66 and 80, about 14 to 24 added per release. Release PRs rebase and re-cite whenever master moves (runs 36517065089, 36340406519, 36320301071). Release PR #2647 was open 22h33m with 25 comments and 6 reviews (n=1; v3.2.0 had a contested version choice).

**Incident record that shaped the doc** (each is a prose patch, not tooling):
- v2.22.0: three dependabot merges raced into the untagged range and turned the coverage gate red (RELEASE_PROCESS.md:97-102; PR #2096). A post-tag changelog edit (PR #2092) forced errata and a maintenance release, v2.22.1. One reader counted at least 11 follow-up PRs between 2026-09-07 and 2026-10-03. The tagged compose file also failed to forward `GOVERNANCE_TOOL_MODE` (docs/releases/2.22.0-errata.md:16-45).
- v2.19.0: the coverage check printed "covers 131/131" while 13 merged PRs were omitted, because the denominator excluded merge-commit PRs (docs/releases/2.19.0-errata.md:1-58).
- v2.18.0: the Docker quickstart was "delegated to CI" without checking that the CI run contained it (docs/releases/2.18.0-errata.md:36-41).

**The doc itself is stale.** RELEASE_PROCESS.md:14-41 still describes a "v2.22.1 maintenance candidate" although docs/releases/2.22.1.md says it shipped on 2026-09-08. The last three edits to the file (#2522, #2575, #2656) were each fixes to the release flow, and #2774 changed release tooling on 2026-10-13. One reader noted that docs/releases/ has no v3.x notes, so I could not tell where 3.x release evidence is recorded.

## Proposal

Ranked so the smallest independently shippable change comes first. Steps 1 to 3 touch no operator-reserved surface. Step 4 onward waits on the decisions listed below.

### Step 1. One current checklist (docs only)

Rewrite RELEASE_PROCESS.md as a single numbered checklist grouped by who acts (agent: prep PR; operator: tag, release page, environment approval; agent: pin PR).
- Move the "v2.22.1 maintenance candidate" section (lines 14-41) and the "v3.2.0 and earlier" compose note into `docs/releases/` with a link, not a deletion.
- Add the omitted facts: the release page triggers container publish; the MCP registry publish fires on pin-PR merge; the SDK section begins with its bump PR.
- Fix the closing paragraph of docs/releases/2.22.1.md, which says PUBLISHED_VERSION "remains 2.21.0 on master".
- Keep incident rationale as one-line footnotes.
- Before moving text, grep `tests/` for `read_text` or `inspect.getsource` references to it, and run `make validate` (published-claims and README volatility checks).

### Step 2. A new `scripts/ops/prepare_release.py`, run through a new `release-prep` Makefile target (tooling)

A thin orchestrator over existing scripts, in the style of `scripts/dev/prepare_interface_release.py`. `make version-bump` stays unchanged. In order, on a clean named branch:
1. Derive `UNITARES_PROJECTS_ROOT` from `git rev-parse --git-common-dir`.
2. Run `changelog_assemble.py`, then `version_manager.py --bump` and `--update`.
3. Rewrite `## [Unreleased]` to `## [X.Y.Z] - <date>` and set CITATION.cff `date-released`.
4. Run `check-skill-freshness.sh --prune 3`, then the bare check, and exit nonzero in the script if the output contains "cited source(s) absent". This is local script behavior only; CI is unchanged, and the string comes from `_check_freshness.py:599`.
5. Run `changelog_coverage.py`, `release_series_drift.py` and `version_manager --check`.
6. Run the range-scoped Docker-input `git log vLAST..origin/master -- <paths>` from RELEASE_PROCESS.md:80-82 and print NEEDS-QUICKSTART or NOT-REQUIRED. It never pre-fills a quickstart result.
7. Emit a PR body skeleton: version-choice prompt, exemption count, series-drift declarations, skill freshness output, and a fixed section for the independent review record.
8. For uncited PRs, write a candidate list to a file for a human to review. It never applies exemption lines. The gate script deliberately never prints paste-ready lines (changelog_coverage.py:288-311), and auto-proposed `no-user-effect` exemptions are the failure behind the v2.19.0 errata.

Add a smoke test against a fixture or temp clone so flag drift in the shelled-out scripts fails loudly, plus a unit test for the worktree path resolution and the absent-source failure.

### Step 3. Operator card and sync-issue text (tooling text)

- Add `prepare_release.py --after-merge`, which prints copy-paste commands with the merged release SHA filled in: `git tag -s vX.Y.Z <sha>`, `git push origin vX.Y.Z`, `gh release create ... --notes-file`, the Promote Release dispatch, the approval link, and the pin-PR command. The operator still performs each act; this removes wrong-SHA risk and improvisation.
- Add a check that runs after container publication and before the Promote Release dispatch. It anonymously inspects each GHCR image and separates three cases: the image is public, the image is private (it then prints the package settings URL), or the image is missing (publication failed or has not run). A pre-tag check cannot do this for a new package, because `publish-container.yml` creates the package only at publication and new packages start private. The one-time visibility flip then fails before promotion instead of at `verify`. The flip itself cannot be done with GITHUB_TOKEN.
- Make `server_release_sync.py` and `sdk_release_sync.py` issue bodies carry the exact next command. Issue #2671 (SDK 0.4.1 declared, `sdk-v0.4.1` untagged, open since 2026-10-05) is the live example of a weekly reminder that does not say what to type.
- Draft the release page body from the tagged CHANGELOG section plus compatibility, migration and rollback headings, reviewed in the prep PR, so the page is a paste of a reviewed file.

### Step 4. Anchor the gate range at the release commit (reserved, see decision 1)

This is the only change in this document that targets the root of the treadmill. The measured range would end at the commit that introduced the current VERSION (or an explicit release commit) instead of HEAD. The release-chore exclusion and the base-ancestor clause already let that commit pass, and a tagged commit already acts as such an anchor, which is why tagging clears the gate. If the leftover-fragment check were evaluated at the same commit, it would also stop failing ordinary PRs. It would make the "tag immediately, arm nothing else" rule unnecessary for gate purposes. The design is inferred; I did not build or test it. Costs: `tests/test_release_seam_gates.py` (716 lines; stand-down behavior pinned at lines 99-105, 217, 232) needs rewriting, and the PR merge-ref case needs care because the synthetic merge commit changes VERSION. A related, smaller change in the same decision is picking the previous tag as the nearest ancestor (`git describe --tags --abbrev=0 --match 'v[0-9]*'`) instead of newest by creatordate, which removes the v2.22.1 lightweight-tag distortion (changelog_coverage.py:195-199).

Alternative not recommended: a per-release branch cut at a chosen SHA with VERSION on master moved to the next dev value. It would fix the window by construction, but VERSION drives the docker-compose image tags (version_manager.py:66-124), so master's compose would point at unpublished images; that design question is unresolved.

### Step 5. Automate the glue after the tag (partly reserved, see decisions 2 to 4)

- Have the `pin` job open the pin PR as a draft with the evidence run URL in the body. This needs a token that can open PRs (GITHUB_TOKEN cannot in this repo; promote-release.yml:6-8). Whether the shared unitares-agents App grant allows it was not checked. If it does not, behavior stays as today. Readying, the `approved-to-merge` label and merging remain the owning agent's act.
- Make the release page a non-prerequisite: either add a `v*` tag-push trigger to `publish-container.yml`, or add a `workflow_call`/dispatch path that a later workflow can invoke. Note that events created by GITHUB_TOKEN do not trigger other workflows, so any workflow-created release page would not fire `release: published`; use an explicit call.
- Chain Promote Release dispatch after publish, leaving the `release-promotion` approval as the human gate.
- Optional larger form: a `release-cut.yml` behind a new `release-cut` environment, where one reviewer approval of a named SHA creates the tag and page and runs publish and verify. This would cut human acts to about 5 but moves tag creation from the operator's terminal to a workflow, and is the most authority-sensitive item here (decision 3).

### Step 6. Small gate fixes (reserved)

Run Release Seams on PRs into `release/**` (release-seams.yml:24-29 triggers on master and main, and also on `merge_group` at line 25; RELEASE_PROCESS.md:24-26 lists the seam checks for maintenance PRs). The workflow header (lines 18-23) records that an earlier path-filtered version was removed because a filtered job cannot be a required check, so any trigger addition must stay unfiltered. Tighten the skill-freshness absent-source condition in CI (`_check_freshness.py:599`) rather than only in the local script.

### Cadence

Cut when PRs since the last tag reach about 50, or at most every one to two weeks, and do the tag, release page and pin PR in one sitting. Recent minors had 49 to 61 first-parent commits in range; v3.0.0 had 348. The cadence is a suggestion only; it raises the count of operator acts per month even though each act gets cheaper, so it is decision 7.

## Other surfaces a release touches

Checked by grep in the worktree at 50bece6b5 and not exercised.

- **Skills and the plugin mirror.** No file under `skills/`, `commands/` or `agents/` references the `unitares` CLI subcommands (one watcher test does), so these steps need no skill change. The skill mirror stays automated by `plugin-skill-sync.yml`; nothing here should hand-sync it.
- **MCP registry listing.** `publish-mcp-registry.yml` fires when the pin PR merges and follows `PUBLISHED_VERSION`. Making the pin PR open automatically (step 5) therefore also moves the registry listing sooner. The tracked `server.json` says 3.2.0 while `PUBLISHED_VERSION` is 3.3.0; I did not check whether the workflow rewrites it at publish time.
- **Glama listing.** `glama.json` carries only `maintainers`, so no release step changes it.
- **Citation and DOI.** Release prep edits `CITATION.cff` (`version`, `date-released`), which carries a Zenodo DOI. No workflow in this repo mentions Zenodo, but the Zenodo-GitHub integration is a repository setting that cannot be read from the tree. If it is enabled, publishing a GitHub Release (including one created by a workflow, step 5) could archive a version immutably. Confirm that setting before step 5.
- **Paper repos.** They are tagged separately (`paper-v*`) and are not touched by a server release. I did not read them; a cadence change should not alter any paper citation, but that is unchecked.

## What stays manual

- Tagging: the operator creates and pushes `vX.Y.Z`. Steps 1 to 3 only print the command with the SHA pinned. Tags stay immutable; corrections remain errata or a patch release.
- The independent review of release notes, version choice, compatibility claims and upgrade path (RELEASE_PROCESS.md step 4), recorded separately from author validation.
- The Docker quickstart on a Docker-capable host when container build inputs changed anywhere in `vLAST..master`. The wrapper reports the trigger but does not run it or fill in a result.
- The `release-promotion` environment approval before `latest` moves.
- The one-time GHCR package visibility flips.
- Merging the pin PR (the queue or the operator), and any `capability`-class PR staying draft.
- The first-ever-release manual `latest` procedure and the manual fallback if Promote Release is unavailable (RELEASE_PROCESS.md:184-213).
- SDK tagging (`sdk-v*`) and the clean-environment install check.
- Runtime deployment, which this document does not touch.

## Operator-reserved decisions

CLAUDE.md as read by the readers does not list "tag a release" or "CI gate semantics" as rows in the reserved table (it lists auth, identity-enforcement, governance-sensitive paths, spending, new external commitments and changes to the table). The task states these are reserved and RELEASE_PROCESS.md:93-102 says the tag is the operator's act, so each is treated as reserved here. Please confirm that reading.

1. Do you approve changing `changelog_coverage.py` and `release_series_drift.py` so the range ends at the release commit instead of HEAD, with the leftover-fragment check evaluated at that commit and the previous tag chosen as the nearest ancestor? (yes/no)
2. Do you approve adding `release/**` to the Release Seams triggers and to required checks and the merge queue? (yes/no)
3. Do you delegate the tagging act to a workflow, so that an approval click on a new `release-cut` environment for a named SHA counts as your tag? (yes/no) If yes: is an Actions-created annotated tag, not signed with your key, acceptable against the "signed when available" rule? (yes/no) And do you want a tag ruleset restricting `v*` creation to that actor? (yes/no)
4. Do you approve granting the unitares-agents App (or the workflow token) permission to open the pin PR, and approve a tag-push or `workflow_call` trigger on `publish-container.yml` and an automatic Promote Release dispatch after publish? (yes/no; the `release-promotion` approval stays either way)
5. Do you approve a merge-queue hold while VERSION is untagged? (yes/no; only needed if decision 1 is declined)
6. Do you approve making the absent-source skill-freshness failure a CI-enforced check rather than a local wrapper behavior? (yes/no)
7. Do you adopt the suggested cadence (about 50 PRs or one to two weeks)? (yes/no)

## Decisions, 2026-10-22

**Basis:** operator delegation, 2026-10-22: "can you make the calls for the federation". The working agent made each choice below under that delegation; nothing here claims that the operator weighed each option. Merging this document is the ratification, and the operator vetoes by removing the merge label.

**Changed since this was drafted.** On 2026-10-17 the operator gave the agents every release act except the `release-promotion` approval, including the tag and the release page (KG `2026-10-18T05:54:48.538440+00:00`). On 2026-10-18 the agents' App was granted `actions: write` for the Promote Release dispatch (#2898). `RELEASE_PROCESS.md` now says both. Read "the operator tags" and "the operator performs each act" above in that light: the releasing agent does those acts.

| # | Decision | Taken | Why |
|---|---|---|---|
| 1 | Anchor the gate range at the release commit | **Yes** | It is the only change that removes the cause of the post-merge red window and the leftover-fragment failures. It is bound by the fail-closed rule under *Risks*: a detached HEAD, a merge-group ref or a PR merge ref that cannot be resolved to the anchor must keep the gates armed. The weekly advisory run that opens an issue is part of this change, not a follow-up. |
| 2 | Run Release Seams on `release/**` | **Yes** | The trigger must stay unfiltered so that the job can be a required check. Adding it to the required checks for `release/**` is a branch-protection setting the App cannot change. That one setting is done once on the operator account, as for other repository administration. |
| 3 | Delegate tagging to a `release-cut` workflow | **No** | Superseded. Since 2026-10-17 the releasing agent tags from its own terminal, so a workflow and environment would add an authority surface and remove no act. Tags stay signed when available, and the existing rule against replacing a public tag stands. The two sub-questions fall with it. |
| 4 | Pin PR opened by the App; a `workflow_call` path into `publish-container.yml`; Promote Release dispatched after publish | **Yes**, `workflow_call` rather than a tag-push trigger | A tag-push trigger would fire alongside `release: published` and publish twice. The pin PR opens as a draft; readying, labelling and merging stay with the owning agent. If the App cannot open it, behaviour stays as today. The `release-promotion` approval stays the human gate. The release page is still created by the releasing agent from the reviewed commit, because publishing it archives the tree on Zenodo. |
| 5 | Merge-queue hold while VERSION is untagged | **No** | It is only needed if decision 1 is declined. Until decision 1 ships, the "tag immediately, arm nothing else" rule stays in force. |
| 6 | Make the absent-source skill-freshness note fail in CI | **No** | A CI checkout has no sibling repositories, so any skill citing an outside source reports absent sources there by construction, and a CI failure on that note would fire whenever such a skill is checked. The failure belongs in `prepare_release.py` (step 2, item 4), which runs where the siblings are present. |
| 7 | Cadence of about 50 PRs or one to two weeks | **Yes, as guidance** | The operator's cost per release is now one approval, so the reason given for treating cadence as reserved no longer applies. Cut at about 50 first-parent PRs since the last tag or two weeks, whichever comes first. This is not a gate. |

Order of work: step 1, then step 2 with its smoke test, then step 3, then decision 1, then decisions 2 and 4. Each is its own PR under the usual review and queue.

## Risks

- Wrapper rot: `prepare_release.py` shells out to scripts whose flags have changed in recent releases. The smoke test is the mitigation; without it drift is silent.
- False confidence: a green wrapper only repeats existing checks. The PR body must say NEEDS-QUICKSTART loudly and never pre-fill a result, and the independent review still applies.
- Laundering uncited changes: auto-proposed `no-user-effect` exemptions (76 of 80 existing) are the v2.19.0 failure mode. The tool must list candidates and never write them.
- Gate rewrite (step 4): if the new anchor is read wrongly (detached HEAD, merge-group or PR merge refs), gates stand down and a release could go out unchecked. The existing code treats a same-named tag on a non-ancestor commit as unreleased to avoid this (changelog_coverage.py:327-331); the new rule needs the same fail-closed behavior and test coverage. Anchoring also means coverage debt on master is not visible until the cut, so a non-blocking weekly advisory run that opens an issue, like `server-release-sync`, would be needed.
- Authority drift (step 5 optional form): if the `release-cut` reviewer or prevent-self-review setting is wrong, an agent could approve its own cut. `promote-release.yml` already fails closed when no reviewer is configured (lines 75-90, 359-367); a new workflow must copy that. A partial failure (tag exists, publish failed) cannot be repaired by moving the tag, so the workflow must be re-runnable from the tag and never delete it.
- Silent no-op trigger: workflow-created release pages and tags from GITHUB_TOKEN do not trigger other workflows, so publish could silently never run unless called explicitly.
- Doc consolidation could drop history or break a test or CI check that reads moved text; moved text should be linked from docs/releases/.
- Maintenance branches are not fixed by steps 1 to 3: a branch whose `publish-container.yml` predates the lease-plane image still needs a backport before tagging (RELEASE_PROCESS.md:134-137), and promote's "newest server tag and ahead of PUBLISHED_VERSION" check may fail for a maintenance tag cut after a newer mainline tag (flagged by readers, untested).
- Steps 1 to 3 shorten prep and remove wrong-SHA risk but do not remove the post-merge red window; only step 4 or a queue hold does.

## What was not verified

- Nothing was executed by me. Counts and timings come from reader reports that ran `gh` and `git` reads; the coverage and drift runs were in a session scratch clone with the tag deleted, so they are simulations that cannot be re-derived from this PR. An adversarial verifier confirmed file names, line numbers (after the corrections above), the 80 exemption comments, the 81 leftover fragments, the f351177ed commit, and the 14.5-day lag, and could not confirm the four pin-PR lags or the simulated gate counts.
- The gate-anchoring design (step 4), the branch-per-release design and every workflow-chaining change are inferred. None was built or tested.
- Repository and environment settings were not read: required reviewers on `release-promotion` and `pypi`, branch protection, merge-queue configuration, whether the unitares-agents App can open PRs, and whether tags are GPG-signed (6 of 7 tags are annotated; v2.22.1 is lightweight).
- Red-master durations other than v2.21.0 (about 22h45m, from the PR #2073 body) are not measured; `gh run list` returned only current runs. The Promote approval wait time was not separated from the pin-PR lag. Human effort per release is not obtainable from git or GitHub.
- `tests/test_release_seam_gates.py` was read by test names only. `release_promotion_freshness.sh`, `compat_published_row.py` and `_check_freshness.py` were not read in full.
- Whether Release Seams ever ran for PRs into `release/2.22` historically was not checked; only the current trigger filter was.
- The task premise of six server releases is off by one: seven tags exist from v2.21.0 to v3.3.0, including the maintenance tag v2.22.1.
- The ancillary claims that no automation creates tags or release pages rest on a grep of `.github/workflows`, `scripts/ci` and `scripts/ops`, not an exhaustive search of the repository.
