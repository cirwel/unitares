# Fact-by-fact ledger — incident #2168, artifact pass (P1) vs UNITARES pass (P2)

Comparator: read-only. Ids: `P1 Fn/An/Dn/Mn` = pass-1 item; `P2 Fn/An/Dn/Mn` = pass-2 item.
`[RE-OPENED]` = settled by this comparator's own read (5 UNITARES calls, 2026-09-17T02:34–02:35Z, bound as `agent-4fc52fef-741`); each such row names the call.
Classes: **BOTH** = both arms recovered it (possibly by different evidence classes) · **ART** = artifact arm only · **UNI** = UNITARES arm only · **NEITHER**.
`ART(principle)` = UNITARES-only in practice, but an artifact read the artifact pass did not perform would have reached it — not counted as uniquely added.
Labels FACT / INFERENCE / GAP are the passes' own.

---

## PART A — Pass-1 fact families

### A1. The branch and its PRs (P1 F1–F8, F81)

| # | Item | Class | P1 ids | P2 ids / evidence |
|---|---|---|---|---|
| A1.1 | First branch commit `92a5e6012` 02:27:57Z, parent `be117c268` (#2165) | ART | F1 | P2 F7, F48: no roster/audit/outcome/bridge row names the branch; F1/F2 fts 0 |
| A1.2 | PR #2166 head `2760307fc`, base `dcd35e861`, created 02:28:36Z, squash-merged 04:04:38Z → `9a9af2efa` (3 commits, 5 files, +99/−5) | ART | F2, F81 | P2 F8: `fts "2166"` count 0; F13 the day's outcome rows never name #2166 |
| A1.3 | `2760307fc` is a merge commit whose tree equals the squash's tree | ART | F3, F81 | — |
| A1.4 | PR #2167 (**different** branch `claude/tools-audit-29w0vc`) squash-merged 04:15:27Z → `a66786fcd`, one file `docs/operations/tool-surface-audit-2026-09-12.md` +398 | **BOTH** | F4 | P2 F9/F10 [SEEDED:#2167] KG 2026-09-12T07:17:58 "Restart also brought a66786fc live (docs-only #2167, zero migrations)"; F11 resolution note 18:17:15Z "fix_verified against running build a66786fc"; F12 [UNSEEDED] "Seven drafts came out of the audit (#2167)". **The only object both documents describe.** P2 A8 keeps the two characterizations unreconciled |
| A1.5 | PR #2170 title, head `8fa84c2e9`, base `a66786fcd`, created 04:35:31Z, merged 07:55:44Z, 5 commits, 10 files, +296/−21 | ART | F5 | P2 F8 `fts "2170"` 0; F13/F48 never name it |
| A1.6 | #2170's commit list = the four pushed SHAs + `8fa84c2e9` | ART | F6 | — |
| A1.7 | Zero reviews / zero review comments on #2166, #2167, #2170; every comment a doc-validation bot; each body self-declares no independent reviewer | **BOTH (agreeing negative)** | F7 | P2 F6, F18, M13: no dialectic session on any of the three; four dialectics existed that day for *other* PRs |
| A1.8 | Exactly two PRs ever had this head | ART | F8 | — |
| A1.9 | "Squash-merged" is a topology derivation, not an API field | ART | F81 | class-level analogue: P2 F54 (typed outcome is not a verdict) |

### A2. What was pushed after the merge (P1 F9–F15, F72, F74)

| # | Item | Class | P1 ids | P2 ids / evidence |
|---|---|---|---|---|
| A2.1 | Four commits `c52e03b5b` 04:28:38Z, `bbd7d79b9` 04:30:29Z, `16d46464a` 04:32:20Z, `1dc22177f` 04:33:22Z; linear, author==committer `Claude` | ART | F9 | P2 A1/A2: records never carry a git actor; F48 zero matches |
| A2.2 | Per-file footprint: dashboard `_AUTHENTICATED_ONLY_FILES` {snapshot.js,preview.html,PLAN.md}; doctor SKIP-on-zero-comparisons; eisv selective-null seed pin; packet "## Council review — recorded 2026-09-12" (+93) | ART | F10, F44 | P2 F43 [SEEDED]: `constraint_drift` 6 rows all pre-incident; `snapshot.js` / `preview.html` / `_AUTHENTICATED_ONLY_FILES` / `open-decisions-packet-v0` → 0 each. UNITARES holds the topic's pre-history, never the fix |
| A2.3 | GitHub resolves all four to login `claude`; three of four bodies say "Found by a review pass over the D3 / D1 / D6+D4 entries" | ART | F11 | P2 F17, M18: no record of any D-entry review pass |
| A2.4 | `2760307fc` not an ancestor of `1dc22177f`; merge-bases | ART | F12 | — |
| A2.5 | `rev-list 2760307fc..1dc22177f` reproduces the issue's six SHAs in order | ART | F13 | — |
| A2.6 | Two of the six are master's own squashes; ordinals `origin/master~55`/`~54` | ART | F14, A1 | — |
| A2.7 | `8fa84c2e9` = skills re-stamp, three skills files, +15/−10 | ART | F15 | P2 §6 skills row holds the *current* registry (unitares-dashboard last_verified 2026-09-14) and no history |
| A2.8 | No session trailer on any of the seven branch commits; #2166's body has no session link | **BOTH FAILED** (process→commit join) | F74, M1 | P2: the one joining identifier — the harness session id in #2170's body (P1 F61) — is absent from the KG. `[RE-OPENED]` `knowledge(search, query="01MHSRK7SK9je2EHeh8drrJh", search_mode=fts, include_archived, include_cold)` → count 0, `search_mode_used=fts`, success=true, 2026-09-17T02:34Z |

### A3. What the guard did (P1 F16–F24, F77, F79, F84, F86)

| # | Item | Class | P1 ids | P2 ids / evidence |
|---|---|---|---|---|
| A3.1 | Five guard runs on the branch; 34673352946 fails 04:34:23–27Z; "ORPHAN PUSH … received `1dc22177f` after PR #2166 was MERGED" | ART | F16, F18 | P2 M15: `fts "orphan-push-guard"` 0, `tags=[orphan-push]` 0, skills bundle (93,821 chars) 0 hits for orphan/push guard/strand/re-land/auto-delete |
| A3.2 | Guard log prints nothing between env block and error; the orphan set exists only in the issue body | ART | F19 | — |
| A3.3 | Guard anchors on newest MERGED PR's `headRefOid` and lists the compare **unfiltered**; PRUNABLE only on zero commits; no close path; docstring's known false positive ("ten branch names carried multiple PRs in the last 400") | ART | F20, F84 | P2 F40 records a *different* guard defect (branch resolved from shell CWD), UNI |
| A3.4 | Issue template: reland branch name, "close once re-landed or discarded", "reusing the name on purpose? open the PR and close as false positive" | ART | F21 | — |
| A3.5 | Dedup = in-process fingerprint substring match over open `ci-finding` issues | ART | F22 | — |
| A3.6 | YAML triggers, `if: github.event.deleted == false`, env carries only BRANCH/PUSHED_SHA/DEFAULT_BRANCH — no `before`/`forced`/`created` anywhere | **BOTH FAILED** (push mechanism) | F23, F79, M2 | P2 M3: no audit event type for git operations; `event_type="%"` → 0; 84 guessed names → none |
| A3.7 | Clearing run 07:10:28Z: "has an open PR — healthy" | ART | F24 | — |
| A3.8 | Guard checkout logged origin's ref → `1dc22177f` at 04:34:23Z (earliest artifact of origin on the new line) | ART | F86 | — |
| A3.9 | Branch-name reuse is recurring (#2117/#2118/#2164 on one branch; #2010–#2036 on another) | ART | F77 | P2 holds no record of any of these issues; A18 shows its vocabulary would not index them |

### A4. The issue (P1 F25–F30, F75, F80)

| # | Item | Class | P1 ids | P2 ids / evidence |
|---|---|---|---|---|
| A4.1 | #2168: open, created 04:34:26Z by github-actions[bot], label `ci-finding`, fingerprint marker, comments 1, `closed_by_pull_requests=0`, updated 2026-09-14T19:29:13Z | ART | F25 | P2 F1 (fts "2168"/"#2168" → 0 at four separate times), F7, F47, F48, M1. Absence established against controls (P2 F46) |
| A4.2 | Issue asserts "Nothing will merge this branch again … the branch cleanup sweep will eventually delete them" — both halves false | ART | F26, A13, A17 | — |
| A4.3 | Its only comment is the weekly audit's table under a *different* fingerprint, naming the branch nowhere | ART | F27, A10, D6 | — |
| A4.4 | Nothing anywhere cites #2168 | **BOTH (agreeing negative)** | F28, M9 | P2 M1, M14: zero across KG/dialectic/audit/outcome/bridge |
| A4.5 | #2168 is the only OPEN one of 13 guard issues; the other 12 closed, mostly within minutes (#2164 in 2 m 37 s, 3.5 h earlier) | ART | F29, F75 | — |
| A4.6 | Five weekly-audit issues and seven merge-content issues all CLOSED; #2202's body lacks the audit words | ART | F80 | — |

### A5. CI on the orphan tip (P1 F31–F34, F76)

| # | Item | Class | P1 ids | P2 ids / evidence |
|---|---|---|---|---|
| A5.1 | Tests run 34673410703 failed: 1 of 22 jobs ('smoke') at skill freshness — "[STALE] unitares-dashboard: dashboard.py changed since 2026-09-09" — after `skills_manifest.py --check` passed | ART | F31 | P2 §6 skills row: the registry surface exists and is current-state only; no gate history |
| A5.2 | #2170's head at open time was `1dc22177f` (eight pull_request-event runs at 04:35:33–34Z carrying #2170's title) | ART | F76 | — |
| A5.3 | All ten runs on `8fa84c2e9` succeeded | ART | F32 | — |
| A5.4 | Merge Content Check ran twice, `conclusion=success`, both logs "DEGRADED … this run verified nothing" | ART | F33, A12, D9 | — |
| A5.5 | 42 check runs on #2170's head all success/skipped | ART | F34 | — |

### A6. Recoverability of the four branch-only commits (P1 F35–F38, F72, A17)

| # | Item | Class | P1 ids | P2 ids |
|---|---|---|---|---|
| A6.1 | `git fetch origin <sha>` exit 0 for all seven SHAs; refs unchanged | ART | F35 | — |
| A6.2 | Commits API serves all four (HTTP 200) | ART | F36 | — |
| A6.3 | No remote ref points *directly* at the four; `refs/pull/{2166,2167,2170}/head` still advertised | ART | F37 | — |
| A6.4 | Locally reachable from no local ref because `refs/pull/*` is not fetched | ART | F38 | — |
| A6.5 | All four are ancestors of `refs/pull/2170/head` → retained on GitHub independent of the branch; refutes the issue's "will eventually delete them" | ART | F72, A17 | — |

### A7. Whether the work was re-landed (P1 F39–F46, F82)

| # | Item | Class | P1 ids | P2 ids |
|---|---|---|---|---|
| A7.1 | Master squash `e34fa2c7b` single parent `a66786fcd`, tree == `8fa84c2e9`, message mentions none of 2168/orphan/4a3hgz/reland | ART | F39 | — |
| A7.2 | **Per-file `patch-id --stable` MATCH for all four commits against `e34fa2c7b`** — the decisive "the work landed" fact | ART | F40 | P2 has no analogue on any surface |
| A7.3 | `diff 1dc22177f e34fa2c7b` = only the three skills files | ART | F41 | — |
| A7.4 | No whole-commit patch-id match in `a66786fcd..origin/master`; `git cherry` marks all four `+` | ART | F42 | — |
| A7.5 | Pickaxe attributes the five new tests and all new strings to `e34fa2c7b` | ART | F43, F46 | — |
| A7.6 | Later unrelated follow-ons on the same paths: #2175, PR #2243 (merge `9a4b547` 09-14T09:32:57Z), #2227 (merged 09-14T22:03:56Z) | **BOTH (partial, different aspects)** | F45, F82 | P2 F50 names #2242/#2234/#2235 as merged or replacement PRs in the same 09-13/09-14 queue; P1 F73 independently shows `codex/doctor-host-binary-refresh` [#2243] head gone |

### A8. Branch existence and deletion (P1 F47–F52, F73, F78, A7–A9, D10, M3, M7)

| # | Item | Class | P1 ids | P2 ids / evidence |
|---|---|---|---|---|
| A8.1 | Branch absent from origin: `ls-remote` (286/287 heads), `list_branches` 3 pages = 286, `list_commits` 404, and absent from the 09-14 audit's 285 `[new branch]` lines | ART | F47, F48, F49 | — |
| A8.2 | No workflow or script deletes branches; the only origin-deleter is operator-local `vigil_hygiene`, dry-run by default, which HOLDs branches with `+` commits | ART | F51, F52, A8 | P2 M15: no record that any guard or sweep ran |
| A8.3 | `list_workflow_runs event=delete` → 0 is **structurally guaranteed** (no workflow has `on: delete`) and non-diagnostic | ART | F50, C10 | artifact-side instance of the four-state trap |
| A8.4 | Auto-delete-on-merge is the mechanism: the repo's own audit script states it as premise; 15/15 merged heads absent vs 9/9 closed-unmerged heads present ⇒ deleted at #2166's merge and **re-created** by the 04:34Z push (INFERENCE; the setting itself unreadable, M7) | **BOTH (independent evidence classes)** | F73, F78, A7, A9, M7 | `[RE-OPENED]` outcome `076dd1a0-ae4f-4da8-b044-7cb826e8353c` (agent `3ed9280b`, 2026-09-12T08:13:57.412Z, task_completed, claim_only, evidence_weight 0.1): "PR #2174 is merged, the new README is live on master, **and GitHub's own auto-delete already removed the branch**." `observe(outcome_evidence, since=08:10Z, until=08:20Z, outcome_type=task_completed, diagnostic=events, include_detail=true)`, 17 rows, all claim_only. Same-day, same-repo eyewitness of the setting P1 could only infer |
| A8.5 | Deletion actor and exact instant | **NEITHER** | F5+F49 give bounds (07:55:44Z, 2026-09-14T19:25:36Z]; M3 | P2: nothing |

### A9. The stranded-work audit (P1 F53–F59, D6, D7, D12, M12, M16)

| # | Item | Class | P1 ids | P2 ids / evidence |
|---|---|---|---|---|
| A9.1 | Run 34886797770 (`event=schedule`, 19:25:28–19:29:17Z) vs cron `17 14 * * 1`; table 1 STRANDED / 1 DANGLING-REVIEW / 19 DANGLING-STALE / 225 PRUNABLE; `4a3hgz` occurs 0 times | ART | F53, F54, F55 | — |
| A9.2 | The audit's own tally: "post-merge push fired on 147 branch(es) … 1 carried work that never landed … 146 carried nothing new" | ART | F78 | — |
| A9.3 | Its issue step commented on #2168 via `--search 'stranded-work-audit in:body'`; close step skipped; the selector cannot be reproduced with the allowed tool | ART | F56, F57, D6, M16 | — |
| A9.4 | Audit issue #2086 closed "completed" with sdk-release-sync's sentence — another producer's cross-match | ART | F59, D7 | — |
| A9.5 | The audit script enumerates only `refs/remotes/origin`, so absence-from-fetch ⇒ absence-from-table | ART | F58, D12 | — |
| A9.6 | Why it fired 5 h 08 m late | **NEITHER** | M12, A15 | P2: nothing |
| A9.7 | Prior instance of *this defect class* in this repo: a false STRANDED "instructs the reader to re-land work already in master, which means branch surgery on a merged-PR branch — the operation that lost two pushed commits on 2026-08-19"; Issue #1722, fixed in PR #1757 | **ART(principle)** — recovered here only via UNITARES | P1 F80 lists #1722 as a closed weekly-audit issue but never opened it | P2 F39, timeline 2026-08-20T01:56Z (KG `2026-08-20T01:56:01.580830`) |

### A10. What PR #2170, PR #2166 and the docs say (P1 F60–F71, F74, F83, F87, F88)

| # | Item | Class | P1 ids | P2 ids / evidence |
|---|---|---|---|---|
| A10.1 | #2170's body: the guard fired because the branch was restarted from master after its PR merged; "that is the shape the guard's own module documents as a known false positive"; "the branch name is fixed by this session's operating instructions, which pull against the guard's advice … worth settling separately"; ends with a a Claude Code web-session link link | ART | F60, F61 | P2: nothing; the session id is not in the KG (`[RE-OPENED]`, see A2.8). **The incident's only self-explanation lives in a PR body** |
| A10.2 | Issue #2169 filed 04:34:56Z by cirwel out of a D2 review pass | ART | F62 | P2 M1: OR-fts over 2169/2171/2177/… → 4 rows, none a re-land |
| A10.3 | #2152 closed by #2166 at 04:04:40Z; its one "worth deciding separately" item | ART | F63 | P2 F8: `fts "2152"` 0 |
| A10.4 | Conventions doc: the runbook rule ("stop pushing … open the new PR and close the finding as a false positive"), "branches with unique local work are held for review, never auto-deleted", "the absence of a guard run is a coverage gap, not a clean verdict", "three confirmed incidents, 2026-08-12/19" | **BOTH at class level, different form** | F64, F65 | P2 F38 KG 2026-09-03 rule + remedy; F39 the 08-19/08-20 content behind P1's bare count; F40 the guard's CWD defect |
| A10.5 | CHANGELOG, `plan.md` and `docs/proposals/README.md` never record the incident, #2166, #2168 or #2170 | **BOTH (agreeing negative)** | F66, F67, M10 | P2 F36, M14: no KG resolution note, related_to, supersede or tag; no dialectic |
| A10.6 | No tracked file mentions the branch; `.unitares/`, `.claude/`, `docs/handoffs/` are gitignored and absent | **BOTH FAILED** | F68 | P2 D7, M19: the fleet's canonical class write-up `[[project_pr-merge-dropped-pushed-commit]]` carries no KG row, and KG 2026-07-01T22:22:06 is withheld as "memory-kg policy: local (not shareable)" |
| A10.7 | The clone is shallow; history bottoms at `fcb4f43` (four `.git/shallow` boundaries) | ART limit | F69, F83, M11, A14, D11 | — |
| A10.8 | Neither guard test file covers the rebased-compare shape or a deleted branch | ART | F71 | P2 M13: no review of the guard either |
| A10.9 | `merge_content_check.py` reads `repos/{repo}/events` 3×100, so the 04:34Z PushEvent payload is unrecoverable | **BOTH FAILED** | F88, M14 | P2 M3 |
| A10.10 | Three different "councils" in tracked docs; `1dc22177f` says "seven-seat council review" while the packet says "Seven independent reviewers"; `seven-seat` exists in no tracked file | **BOTH obscure it, differently** | D17, D18, D19, F43 | P2 F17 `fts "seven-seat"` 0, `fts "open-decisions-packet-v0"` 0; F21/D13 "council" only as five self-descriptions |

---

## PART B — Pass-2 fact families

### B1. The incident itself (P2 F1–F7, F46–F48, M1–M3, M13–M15)

| # | Item | Class | P2 ids | Note |
|---|---|---|---|---|
| B1.1 | UNITARES holds **no** record of #2168, the branch, an orphan push on 2026-09-12, any review of it, or any decision about it — established against positive controls (4-digit PR numbers, hex ids, hyphenated branch names, superseded and cold rows all index), the complete KG store stream 09-11T20:00Z→09-17T00:18Z, and the complete fleet task_completed/task_failed enumeration (860 rows) | UNI (methodological) | F1–F7, F46, F47, F48 | Only knowable from UNITARES; it is the capture result |
| B1.2 | In 04:00–05:00Z on 2026-09-12 — the hour containing all four commits, the push, the guard failure and the issue filing — the entire fleet's outcome record is **32 rows, every one `substrate_observed`** (avg evidence_weight 0.85) from `69a1a4f7` 12, `907e3195` 13, `e55caaf1` 1, `f92dcea8` 6; **zero** task_completed, zero claim_only, zero test rows | UNI `[RE-OPENED]` | sharpens F48, F55 | `observe(outcome_evidence, since=04:00Z, until=05:00Z, diagnostic=agent_summary)` → raw_row_count 32, by_grade {substrate_observed: 32}; `… outcome_type=test_passed, diagnostic=events, include_detail=true` → raw_row_count 0 |
| B1.3 | The zeros were produced while recording was live: five sessions registered 04:21:24–04:36:03Z and audit rows run to 04:35:56Z, i.e. through the guard failure (04:34:23–29Z) and the issue filing (04:34:26Z). The non-capture is **not** a silence artifact | UNI (INFERENCE) | F24, F25 + P1 F16, F18, F25 | Distinguishes "not recorded" from "nothing was running" |

### B2. Work on the branch and the PRs (P2 F8–F16, F49, F50)

| # | Item | Class | P2 ids | Note |
|---|---|---|---|---|
| B2.1 | The incident-day master tip `a66786fc` was the **running governance build**; plist edit + `launchctl bootout`/`bootstrap`; "304 tool calls / 70 agents in the first 3 min, zero ERROR lines" | UNI (SEEDED:#2167) | F9, F10, F11, A5, A11, D2 | Deployment state is machine-local; P1's `.claude/` overlay is gitignored (P1 F68) |
| B2.2 | The concurrent cast: outcome rows on 09-12 name #2163, #2172–#2178, #2181, #2182, #2184, #2185 — and never #2166/#2168/#2170 | ART(principle) for the PR list; UNI for the per-session attribution | F13, F48, F49 | Artifacts hold those PRs; P1 did not enumerate the day's other PRs |
| B2.3 | Five read-only reviewer sessions (`a79b0543` #2176, `fb5e09b4` #2178, `945043b6` #2184, `d46c963c` two heads, `6eb066ff` #2177) reviewed code on the incident day, each explicitly leaving no artifact ("No repository, KG, or application-data changes made") | UNI | F49, F21 | These reviews are invisible on GitHub — which is also why P1 F7's "zero reviews" on #2166/#2170 is a real absence, not a recording gap |
| B2.4 | Post-incident PR-queue practice: an "operator-authorized serial PR merge queue" (09-14T02:14–06:21Z) merging #2192/#2224/#2232 and replacing stale #2206/#2207/#2222/#2223 with fresh current-master PRs #2234/#2235; then "**Closed all 17 verified superseded PRs, each with a comment linking its merged replacement. Branches were retained.**" | UNI for actor+intent+authorization | F50, A20, M14 | `[RE-OPENED]` outcome `44852adb-018c-423c-885e-3112237ff8d1`, agent `b6ee99d4-5a1b-4e07-bdfa-91ceee37606d` (codex harness), 2026-09-14T19:25:58.906Z, claim_only, excerpt truncated at "Four…". `observe(outcome_evidence, since=19:25:30Z, until=19:26:30Z, outcome_type=task_completed, diagnostic=events, include_detail=true)` → raw_row_count 1. **P1 recorded the same minute as an unexplained coincidence** (ten PRs closed unmerged 19:25:05–19:25:34Z, P1 timeline/V2, M12) |

### B3. The council review (P2 F17–F22, F51, F19, D13, M18)

| # | Item | Class | P2 ids | Note |
|---|---|---|---|---|
| B3.1 | No council exists as a review record anywhere in UNITARES: `fts "seven-seat"` 0, `fts "open-decisions-packet-v0"` 0, council tags → 3 rows all April 2026; "council" survives only as five self-descriptions | UNI (structural) + the sharpest FAILED-TO-PRESERVE | F17, F21, D13, M18 | The incident's council review is a git artifact: P1 F10 (+93 lines), F44 (packet section on master), D18 (wording) |
| B3.2 | The dialectic schema holds exactly one `reviewer` and one `paused_agent` per session and cannot be listed by date, so a multi-seat review **cannot be represented as one row** | UNI (capability limit) | F19 | Mechanism for B3.1 |
| B3.3 | Class-level precedent: dialectic `076449b0ba21c1fd` (2026-08-28) — "Four review seats were dispatched … as subagents, filed as one session with one reviewer"; `reviewer_kind='external_consult'` had **zero** rows against 136 review sessions since 2026-04-19 | UNI (class-level rule) | F20, D10 | The harness's multi-seat review is systematically mis-filed; recorded once, then repeated at #2168 |
| B3.4 | Exactly four dialectics were created on 09-12, one reviewer each; the two "rejects" exist only as transcript antithesis messages, not as audit rows | UNI | F18 | Review activity existed that day — for other PRs |
| B3.5 | Every in-window antithesis records `reviewer_backend {backend: codex, models_used: [], warnings: ["Codex CLI did not report an exact model identifier"]}` and `attestation {state: unsigned}` | UNI | F22 | The reviewer seat is a backend, not an identified reviewer |
| B3.6 | The four pre-incident failed sessions' topics (fermata #59, unitares #2128, #2116, MCP tool-surface design) — none about orphan pushes | UNI | F51, M17 | Rules out topical adjacency |

### B4. Who was active and when (P2 F23–F31, F52, F53, D2–D4)

| # | Item | Class | P2 ids | Note |
|---|---|---|---|---|
| B4.1 | Sessions registered 04:21:24Z, 04:31:37Z, 04:35:37Z, 04:35:51Z, 04:36:03Z with worktree labels (dazzling-meninsky-73b261, unitares-readme-mark, festive-pare, optimistic-cartwright) | UNI | F24, F26, F27, A6 | Narrows P1's M1/A5 to a candidate set; identifies no pusher — no label, roster row, audit row or outcome row names the branch (F7) |
| B4.2 | **Fleet-wide recording silence 2026-09-12T04:36:11Z → 07:10:56Z** on three surfaces (audit rows, bridge deliveries, check-ins/registrations); Vigil misses six 30-min cycles; bridge integer ids restart at 1 at 04:12:05Z and 07:11:10Z; silence-monitor cadence implies starts ≈04:12:46Z and ≈07:12:36Z; the bridge re-announces `agent_new` for an agent created 04:35:37Z at 07:13:50Z | UNI | F25, F53, D2, D3, M16 | Covers the interval between the orphan push and the 07:09:48Z fix commit (P1 F15). No artifact instrument exists for it |
| B4.3 | Watcher produced **no** finding for 21.32 days (last 2026-08-21T20:31:30Z) and then 31 on the incident day, all P016 on two Claude Code worktrees | UNI | F28, M23 | A live four-state case: a producer that was silent, not a fleet that was clean |
| B4.4 | `admin(tool_usage, window_hours=168)` records calls from **exactly one agent — the reading driver** (143 → 213 → 389 → 487 across the pass) while the roster shows 296–300 active | UNI (+ obscured) | F29, A19 | The instrument shows only the reader |
| B4.5 | `server_info` lists two processes started ≈09-09T06:20Z and ≈09-16T04:13Z — **neither on 09-12** — contradicting the same-day restart narrative | UNI (record-vs-record) | F30, D3 | |
| B4.6 | Time bases: `observe(agent)` histories are naive server-local (UTC−6) while every other surface is UTC; structured agent ids carry the local date; one record says "09-11 22:40 MDT" with no offset | UNI (+ obscured) | F31, F23, A11, A12, §7 | |
| B4.7 | Thread `t-344c7ed8d5549e0e` joins the writer of the 2026-09-03 class rule (node 10) to a 09-12 session (node 14); but `parent_agent_id` is null and `lineage_state` is `no_lineage_declared` on **every** successor, and `spawn_reason` is `compaction` | UNI | F52, F26, A10 | The declared-lineage feature the audit and its review call novel was **not exercised** by any incident-day session |
| B4.8 | `lifecycle_silent` / `_critical` rows emitted for an agent the roster lists archived since 2026-09-01 | UNI (record-vs-record) | D4 | |

### B5. Outcomes (P2 F32–F35, F54–F56)

| # | Item | Class | P2 ids | Note |
|---|---|---|---|---|
| B5.1 | Every outcome row carries an explicit corroboration grade and reasons; across 11 dumps: claim_only 176 / externally_verified 266 / substrate_observed 422 / tool_observed 24 | UNI (capability) | F32 | Audit measure 4. `[RE-OPENED]` corroborates the shape: the 08:10–08:20Z rows carry `evidence_weight 0.1`, `claim_risk high`, reasons "no independent evidence beyond the claim" |
| B5.2 | `externally_verified` test rows retain the executed command line (e.g. "git worktree add -q -b claude/sdk-summary …"), so a git action **can** leave a row of this class | UNI | F55 | With B1.2 this is the tightest capture-side negative: the surface that could have held it had zero rows fleet-wide in the push hour |
| B5.3 | `outcome_type` does not track excerpt sentiment: rows typed `task_failed`, score 0.0, `is_bad true` whose text reads "Now it's ready … 33 passed, 0 failed" and "Marked PR #2175 ready … 15709 passed" | UNI (+ obscured) | F54 | |
| B5.4 | A graded outcome re-grades an earlier check-in: `5bbd1061` (score 0.6) — "The graded check-in asserted the degraded path was covered … That was false … A later council review found it and it was reproduced end to end" | UNI (outcome) | F21, F32 | Audit measure 9, demonstrated on adjacent work (#2176), never on #2168 |
| B5.5 | Cross-agent reads are refused: `export(history)` for another agent → identity_mismatch; `knowledge(get, agent_id=<other>)` ×3 → identity_mismatch; `dashboard` → identity_required; `outcome_correlation` → NO_DATA (caller-scoped); self `export` → empty | UNI (+ obscured, + burden) | F35, M4, M5, M6, M7 | Bears on the audit's decision-rule condition 4 (reproducible from exports) |
| B5.6 | `observe(outcome_evidence, target_agent_id=<label>)` returns `raw_row_count 0` with **no error**; `agent(get)` resolves labels but redacts the UUID; only `observe(agent)` yields the UUID | UNI (+ obscured) | F56, M8, M9 | A silent zero that reads as "this agent has no outcomes" |
| B5.7 | Record noise: 39 `task_completed` claim_only rows with empty detail from one agent in 5 minutes; `[RE-OPENED]` 12 identical "Watcher: 10 unresolved…" claim_only rows in the 10 minutes 08:10–08:20Z | UNI | F27 + re-read | Signal-to-noise on the retained-text surface |

### B6. Decisions to re-land / discard / close (P2 F36–F45)

| # | Item | Class | P2 ids | Note |
|---|---|---|---|---|
| B6.1 | No decision about #2168 anywhere: no resolution note, related_to, response_to, superseded_by, tag, dialectic resolution or outcome row | **BOTH (agreeing negative)** | F36, F37, M14 | P1 M4 reached the same negative from the artifact side |
| B6.2 | The class rule existed nine days before the incident: "⛔Rule reinforced: **a push after the PR's merge lands nowhere; open a fresh PR for post-merge commits**", with a worked remedy ("Re-land = cherry-pick aa42c15 …") and a content-verified third instance | **BOTH at class level, different form** — UNI as a *recorded claim with instances* | F38 | The artifact form (guard code + runbook, P1 F20/F21/F64) fired; the UNITARES form was read by nobody on 09-12 (F13, F47, F48 name no such read) |
| B6.3 | Cross-repository work-loss taxonomy: 2026-02-25 force-push losing ~80 commits; 2026-04-14 `git reset --hard` destroying another agent's WIP (closed 08-09, "no reflog watcher was built"); 2026-06-30 wip-guardian after 38 branches ahead of master; 2026-08-27/28 revenue-engine PR #7 strand; 2026-08-28 merge-order defect "never trust a MERGED badge"; 2026-08-28 `d9a149c` "lands nowhere"; 2026-09-03 two codex worktrees holding PR-less work | UNI (the non-unitares instances) | F38, F39, F40, timeline 2026-02-25 / 04-14 / 06-30 | Different repositories are outside the artifact arm's scope as run |
| B6.4 | The guard-family defect recorded only in UNITARES: "Merged-PR push guard resolves branch from SHELL CWD, not the `cd` in a compound command — use `git -C <worktree>` for push or it false-positives" | UNI (class-level rule) | F40, A9 | A *different* false-positive mechanism from P1 F20/F84's unfiltered compare; unresolvable to a repo from UNITARES alone (A9) |
| B6.5 | The absence-claim standard: dialectic `ac929021d65301d9` requester synthesis 2026-09-11T20:44:16Z — "before any claim that a search found nothing, assert the command exited 0 and name the producer" | UNI (standing condition) | F37, §5 note, preamble | It changed later work: pass 2 reported every zero against it |
| B6.6 | Corrections attached to the claims they correct: `2026-09-12T18:41:44` superseded by `19:11:39` ("wrong in three places", "corrected after council review"), still searchable; `2026-09-09T06:07:12` resolution note "THE ORIGINAL DIAGNOSIS IN THIS FINDING WAS WRONG"; `2026-08-19T06:34:13`'s rule corrected by `2026-09-09T04:48:54` | UNI (correction mechanism) | F15, D9, D11, D12, F45 | Audit measure 7; all on adjacent work |
| B6.7 | Instrument state: 1855 discoveries / 593 agents / 16,310 graph edges (point value at read time) / 2,648 tags; embedding coverage 1855/1855; open/resolved moved 531/84 → 530/85 during the pass; three different totals by scope (1855 / 1206 / 1813) | UNI | F44, §7 scope hazards | |
| B6.8 | A record that exists and cannot be read: archived KG 2026-07-01T22:22:06, body "Resolution notes … memory-kg policy: local (not shareable)" | UNI (+ obscured) | F41, M19 | |

---

## PART C — NEITHER arm (with the passes' own gap ids)

| # | Item | P1 | P2 |
|---|---|---|---|
| C1 | Push mechanism: force-push vs delete-and-recreate (`forced`/`created` flags) | M2, F79 | M3 |
| C2 | The GitHub PushEvent payload for 04:34Z | M14, F88 | M3 |
| C3 | Whether a human was present; the intent behind the post-merge push | M1, A5 | A1, A2 |
| C4 | The harness instruction that fixed the branch name (quoted in #2170, never recorded) | F60 (quote only) | A9, M15 |
| C5 | Any decision to close, discard or keep #2168 open | M4 | M14 |
| C6 | `delete_branch_on_merge` as a read setting | M7 | only a claim_only witness (A8.4) |
| C7 | Branch-deletion actor and instant | M3 (bounds) | — |
| C8 | Why the 09-14 audit fired 5 h 08 m late | M12, A15 | — |
| C9 | Cause of the 04:36–07:11Z recording silence | no instrument | M16 |
| C10 | The canonical class write-up `[[project_pr-merge-dropped-pushed-commit]]` | F68 (gitignored, absent) | D7, M19 |
| C11 | Whether `vigil_hygiene` ran at all in the window | M15 | M15 |
| C12 | The PR number truncated to "#21" in one outcome row | — | M25 |
| C13 | Which model occupied the reviewer seat | — | F22 |
| C14 | Any review of the guard's own false-positive class | F71 (no test) | M13 (no dialectic) |
| C15 | Report text of any 09-12 check-in ("not retained as durable history") | — | M21 |

---

## PART D — Counts

- Pass-1 families: 10; ledger rows A1.1–A10.10 = 52. Of these: ART 38, BOTH 8, BOTH-FAILED 4, NEITHER 1, ART(principle) 1.
- Pass-2 families: 6; ledger rows B1.1–B6.8 = 30. Of these: UNI 25, BOTH(negative) 2, BOTH(class-level, different form) 2, ART(principle) 1.
- NEITHER rows: 15.
- Overlap on a positively-stated incident object: **one** (A1.4, `a66786fc`/#2167). Every other BOTH row is either an agreeing negative, a class-level rule in two different forms, or the same mechanism reached by two independent evidence classes (A8.4).
