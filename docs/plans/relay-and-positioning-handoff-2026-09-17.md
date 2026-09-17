# Handoff: Relay integration, claims audit, and the positioning edits

**Created:** 2026-09-17
**Status:** Handoff record, point in time. Nothing here changes runtime behavior.
**Scope:** what one Claude session landed between 2026-09-16 21:50 UTC and 2026-09-17 01:00 UTC, what it left open, and which decisions the operator still owns. Written so the next session (Claude, Codex, or the operator) can continue without the transcript.

## What landed on master

| PR | What it is | Where |
|---|---|---|
| #2255 | NeMo Relay exporter and policy gate as an optional SDK extra, with the re-layering decision packet | `agents/sdk/src/unitares_sdk/integrations/nemo_relay.py`, `agents/sdk/tests/test_nemo_relay_plugin.py`, `agents/sdk/README.md` (section "NeMo Relay integration"), `docs/proposals/relay-substrate-relayering-v0.md` |
| #2256 | Claims falsification audit and market map, superseding the June map | `docs/ontology/competitive-analysis-2026-09.md`; index entry in `docs/ontology/README.md` |
| #2256, commit `b9a103c` | The `discord-bridge` skill re-verification stamp, ported from #2258 so the smoke job's calendar gate stopped failing every branch | `skills/discord-bridge/SKILL.md`, `skills/unitares-governance/SKILL.md`, `skills/SKILLS_MANIFEST.sha256` |

Also merged in the same window, not from this session: #2250 (Codex, constraint-pressure replay primitive). Its proposals-index row and this session's collided on the counts line; the second lander recounted to `Active 27`.

## Open queue at handoff (2026-09-17 01:00 UTC)

| PR | Owner | State | What it waits on |
|---|---|---|---|
| #2257 competitive survival audit | Codex | draft | Its author marks it ready. It records #2256 as a prerequisite and says it will not fork another positioning rewrite. It merges cleanly with master now that #2256 is in. |
| #2258 independent review of #2257 | Claude (another session) | draft | Its author. Its stamp commit no-ops against master. |
| #2259 `prediction_id` advertised after the Phase-5 mint | Claude (another session) | draft | Its author; a runtime fix that needs the full suite. |
| #2254 session-key rotation test | Claude (another session) | draft | Its author marks it ready; test-only. |

## Decisions the operator owns (none taken by this session)

1. **The three parking questions in the Relay packet.** The host hook chain, the governed-effect execute half, and the lease plane: whether each stays here or is delegated to Relay middleware. The packet names the evidence that would decide each and a ninety-day gate. It does not reopen the Wave 3 go-decision.
2. **The positioning rewrites.** The audit's "Recommended rewrites" table lists three README lines, one line under the public site's "What operators get" table, a product-definition sentence, the preprint title, and a roadmap line. None is applied. Two facts to weigh first: the public site's opening paragraph repeats the README's "federation kernel" sentence verbatim, so a README change is a site change too; and the GitHub repository description and the MCP server's instructions string carry the same phrase and sit outside the doc guards. `docs/plans/public-presentation-critique-2026-09-16.md` argues for an editorial contract before any further rewrite, and #2257's reconciliation note defers positioning ownership to the audit. Pick one owner and one sentence before opening the PR. If the edits go ahead, register the canonical wording in `docs/dev/CANONICAL_SOURCES.md` and keep competitor names out of the README's closing sentence.
3. **The two record gaps the audit exposed.** No tamper-evident (hash-chained) export, and no standard trace emission beyond the Relay exporter. Recorded as proposals for the roadmap, not commitments.
4. **The Mercor Safety Research Grants expression of interest.** Drafted on 2026-09-16 and deliberately kept out of the repository (PR #2252 closed unmerged). The draft lives in the operator's Claude Docs; its placeholders (team, prior work, budget) are still open. The closed PR's branch `claude/mercor-careers-page-3840lr` still exists and needs deleting from the GitHub UI, because the session's push policy refuses ref deletions.

## Hazards the next session will meet

- **The skill-freshness calendar gate.** `scripts/client/check-skill-freshness.sh` runs in the smoke job and fails when any skill's `last_verified` is older than the larger of its `freshness_days` and the thirty-day floor. Four skills were stamped 2026-09-14, so the gate fires again for every branch on 2026-10-15 unless they are re-verified and stamped (`--stamp NAME` after re-checking the cited sources; the bridge digests need `unitares-discord-bridge` checked out beside the repo). A failure there is calendar-driven and is not the PR's; port the stamp, do not skip the check.
- **Draft Base Refresh bot merges.** `.github/workflows/draft-base-refresh.yml` merges master into drafts. On #2250 every `pull_request` workflow on the bot's merge commit sat in "action required" until a maintainer approved it. An author push clears it too.
- **The proposals index counts line.** Two concurrent PRs that each add a proposal merge cleanly and then fail `scripts/dev/check_proposals_index.py` on master. Whoever lands second recounts; a trial merge before merging catches it.
- **The test aggregator on superseded heads.** `test (3.12)` reports "one or more test shards failed or were cancelled" when a newer push cancels a run. Check the run's head SHA before diagnosing.
- **Strict status checks.** A PR that falls behind master shows "behind" and cannot merge until master is merged in. Use a merge commit, not a rebase, on a branch someone else may have checked out.
- **An unverified report.** #2257's body says `tests/test_dashboard_redesign_route.py::test_serves_entry_page_by_default` fails on master locally; CI does not reproduce it. Nobody has confirmed either way.

## First commands for the next session

```bash
git fetch origin master
python3 scripts/diagnostics/check_doc_health.py --strict
python3 scripts/dev/check_proposals_index.py
bash scripts/client/check-skill-freshness.sh
bash scripts/dev/check-repo-scope.sh --base origin/master
```

For the Relay integration: Python 3.12 (the SDK floor), `pip install "unitares-sdk[nemo-relay]"`, then `python -m pytest agents/sdk/tests/test_nemo_relay_plugin.py -q -o pythonpath=`. The end-to-end test skips when `nemo_relay` is absent.
