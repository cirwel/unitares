# S20 — Client cache scope narrowing

**Date:** 2026-04-25
**Scope:** Open row. Addresses cache *placement* / *sharing* — orthogonal to S2 (auto-resume retirement, semantic) and S1 (token format). Closes the passive-siphon surface created by a workspace-flat cache file readable by every co-resident process.
**Stance:** Descriptive + recommendation. Implementation now partly shipped; remaining open item is the operator cleanup/runbook.
**Stacks with:** S1-A′ (PID/nonce token binding) + S11 (cache contents lineage-only) + S19 (substrate attestation). Orthogonal to S2.
**Authors:** Kenny Wang (CIRWEL) + process-instance `a61763e1` (Claude Opus 4.7, claude_code, 2026-04-25), declared lineage from `02fa2672`.
**Companion:** S11-a (skill text drift from the S11 contract) — separate row, ships independently.
**Review provenance:** Drafted 2026-04-25, reviewed by `dialectic-knowledge-architect` (ontology stress) and `feature-dev:code-reviewer` (call-site accuracy) before landing. Findings folded in: §1a/§1b honesty fixes; missed call-site `onboard_helper.py` added; §3d skill-drift carved out to S11-a; renumbered S2-a → S20 (orthogonal to S2, not sub); §2 bootstrap gated on empirical session-ID stability check (§5 step S20.0); §4 siphon-closure claim narrowed; §9 axiom #14 relabeled "convention-level, advisory." **Amendment 2026-04-25 (post-ship):** verified hook source directly — code-reviewer's claim about `hooks/session-start:102` was stale. PR #19 (`fix(session-start): only read slot-scoped workspace cache, never bare session.json`) already shipped the §3b proposal at the hook layer. §1c corrected; §3b scope reduced to "verify hook contract + ensure remaining readers comply"; S20.0 promoted to **answered** (Claude session ID stable within session lifetime, fresh on `/clear` — fine for writer key, scan-newest covers cross-`/clear` lineage). Real remaining scope is §3a helper enforcement + §3c direct-writer parity + §3d migration of pre-PR-19 flat caches. **Amendment 2026-04-26 (pre-implementation audit):** discovered §3a blast radius before opening the implementation worktree. Two plugin hooks call `set session` without `--slot` (`hooks/post-checkin:50`, `hooks/post-edit:221`) and `hooks/post-edit:192` defaults `SLOT="${SLOT:-default}"` to a literal `"default"` string when unset — defeats slot-scoping. Hard helper rejection per original §3a would silently brick the auto-checkin milestone pipeline fleet-wide (errors swallowed via `\|\| true`). New §3.5 "Pre-implementation audit" added; §5 sequencing split S20.1 → S20.1a (hook write-path fixes) → S20.1b (helper rejection); operator decision recorded in §3.5: PR1 (hooks) lands and proves coverage before PR2 (helper rejection); optional warning-only grace only if PR1 reveals an uncovered client path. **Amendment 2026-05-06 (Codex-side closure):** unitares-side `scripts/client/session_cache.py` now mirrors the plugin helper's S20.1b contract: session writes require `--slot` unless `--allow-shared` is explicit, non-empty `continuity_token` is rejected, legacy tokens are stripped on merge, `list` exposes slotted and flat-legacy lineage candidates, and temp files are unlinked on write failure. Codex command docs (`commands/governance-start.md`, `checkin.md`, `diagnose.md`, `dialectic-respond.md`) now use `session_cache.py list` and slotted writes. Focused tests live in `tests/test_client_session_cache_contract.py` plus the existing direct-writer mode tests.

---

## TL;DR

The plugin cache helper supports per-slot files (`session-<slot>.json`) and is in active use — many such files exist on disk. The original siphon was the flat fallback: when callers omitted `slot`, the helper fell back to `session.json`, the workspace's de-facto "current owner" file readable by every same-UID process. S20.1b and S20.4 now reject that helper path by default. Independently, the unitares-side `scripts/client/onboard_helper.py` bypasses the helper; S20.3 made that direct writer mode-0600 and lineage-only.

S11 (2026-04-21) made the cache **content** lineage-only at the post-identity hook (no `continuity_token` write at v2). S20 makes the cache **location** per-process and the **enforcement** helper-level rather than hook-aspirational. It also closes the umask gap on the direct-writer path.

S20 does not make tokens unforgeable. S1-A′ does that with PID/nonce binding (server-verified). S19 closes the substrate-resume case. The four cuts are stackable:

| Layer | What gets fixed | Status |
|---|---|---|
| **Cache contents** (S11) | Token not at rest in v2 cache, at the post-identity hook path | Landed 2026-04-21 |
| **Cache location + writer parity** (S20, this doc) | No shared "current" file; helper-level enforcement; umask-gap closed | Mostly shipped; S20.5 runbook remains |
| **Token format** (S1-A′) | Server-verified PID/nonce binding | After S1-a grace |
| **Substrate resume** (S19) | Resident UUID claims attested, not bearer | In flight (PR #164) |

S20 is **convention-level**: helper rejects shape-violating writes, but anyone can `echo > .unitares/session-x.json` and bypass. It is descriptive partition, not earned defense. The earned defense lives in S1-A′ + S19. S20's job is to stop the system from *teaching* the shared-cache pattern; it does not prevent a determined caller from re-creating it.

## 1. The siphon surface today

### 1a. Where the cache lives (plugin helper)

`unitares-governance-plugin/scripts/session_cache.py:_cache_path` resolves:

- `slot` set → `<workspace>/.unitares/session-<safe(slot)>.json`
- `slot` unset → `<workspace>/.unitares/session.json` **(the shared file)**

`_write_json` writes mode 0600 via `os.fchmod`. Same-UID processes still read freely; mode protects against *other* users on multi-tenant hosts but not against co-resident agents under the operator's UID — which is the actual threat model on a workstation running Claude Code, Codex CLI, residents, dispatch workers, and ad-hoc scripts side by side.

Note: workspace = caller's CWD by default. For a session launched from `$HOME`, workspace `.unitares/` and `~/.unitares/` are the same directory; for a session launched from a project directory, they differ. The 80+ slot files I observed live under `/Users/cirwel/.unitares/` because this Claude Code session was launched from `$HOME`, not because the helper writes to `~/.unitares/` by default.

### 1b. Where the cache is also written (direct-writer path)

`unitares/scripts/client/onboard_helper.py` is a parallel writer that **bypasses `session_cache.py` entirely**:

| Site | File:line | Behavior |
|---|---|---|
| Cache write helper | `scripts/client/onboard_helper.py:98-101` | Pre-S20.3: `path.write_text(...)` inherited umask 022 → mode 0644. S20.3 now uses atomic mode-0600 writes. |
| Cache payload | `scripts/client/onboard_helper.py:234-245` | Pre-S20.3: included `"continuity_token": parsed.get("continuity_token", "")`, contra S11's intent at v2. S20.3 now omits cache-persisted token fields. |

`onboard_helper.py` slot-scopes when callers supply `slot` (line 99: `_slot_filename(slot)`), so it does not write the helper's flat fallback on slotted runs. S20.3 closed the independent umask and token-field gaps on that direct-writer path.

### 1c. Who reads what

- Plugin `hooks/session-start:109-140` reads `session_id` from Claude Code's SessionStart hook stdin payload, sanitizes it via the same `_slot_suffix` rules as `session_cache.py`, and points `WORKSPACE_CACHE` at `${PWD}/.unitares/session-<safe-claude-session-id>.json`. **Slot-scoped reader, not flat.** PR #19 (commit `87affc9`) explicitly removed flat-file reads after KG bug `2026-04-20T00:09:51` (cross-instance UUID menu inviting fresh agents to pattern-match and resume into other instances' identities). The hook block contains a permanent comment forbidding the flat read pattern.
- Plugin `unitares-governance-plugin/scripts/onboard_helper.py` (separate from unitares-side, same name) — slot-scoped writer.
- Pre-S20.4, the unitares-side `governance-start` command (Codex-side surface) wrote back via `session_cache.py set session --merge --stamp` without `--slot`, producing the flat `session.json` the hook deliberately ignored. S20.4 now uses `session_cache.py list` plus slotted writes; the plugin-side version was fixed earlier by S11-a.

The contract is "callers pass slot"; the siphon vector is "callers don't or are replaced." Helper-level enforcement now rejects compliant-tool slotless writes by default. This remains convention-level: a caller can still bypass the helper by writing files directly.

## 2. Bootstrap problem

Session-start runs *before* the agent has been issued a `client_session_id`. That's why flat `session.json` exists — it's the "no slot yet known" sentinel. Killing the flat path requires a replacement bootstrap.

Two viable replacements:

### 2a. Platform-session slot (preferred, but unverified)

Use the harness-provided session identifier as the slot at bootstrap:

| Harness | Slot source candidate |
|---|---|
| Claude Code | hook-provided session ID, or `$CLAUDE_PROJECT_DIR` + transcript path hash |
| Codex CLI | Codex's own session ID (already passed through to slot in some paths) |
| Dispatch worker | Dispatch task ID |
| SDK `GovernanceAgent` | PID + start-time hash |

**Stability empirically resolved 2026-04-25 (S20.0 answered).** Direct observation: ~80 distinct slot files accumulated across April 17–25 in `/Users/cirwel/.unitares/`, each named `session-<claude-session-id>.json`. Density (~10/day for one operator) is consistent with `/clear` and conversation restarts spawning fresh Claude session IDs while a single Claude session retains its ID for its lifetime. Conclusion: **session ID is stable within a Claude session, fresh on `/clear`.** This is the desired property for the writer key (each session writes its own slot, no cross-session collision). It does not give cross-`/clear` lineage continuity — but lineage was always going to require scan-newest (§2b), not platform-slot lookup. §3b therefore composes correctly: platform slot for write, scan-newest for prior-lineage reads.

This makes the cache file **process-stable for the lifetime of the harness session** (if the ID is stable) and **process-distinct across concurrent harness sessions**. Slot is *not* a security primitive — it's a partition. A misdeclared slot leaks identity to whoever guesses the slot value, which is a fingerprintable string. The strength comes from the helper not *teaching* a shared file as the canonical surface; it does not raise attacker work meaningfully against a same-UID process willing to `ls`.

### 2b. Scan-newest fallback

If platform session ID is not available or not stable, the bootstrap reader scans `<workspace>/.unitares/session-*.json`, sorts by `updated_at`, and uses the newest as a **lineage candidate** — never as a resume credential. This is a `parent_agent_id` hint, not a UUID claim. Under v2 ontology this is honest: lineage is declared, not inherited.

The combination — platform slot when known, scan-newest fallback when not — covers every client. **Flat `session.json` becomes write-forbidden in the helper.**

## 3. Proposal

Change set, in dependency order:

### 3a. Helper-side enforcement (`session_cache.py`)

- `cmd_set` rejects `kind=session` when `slot` is unset, except when `--allow-shared` is passed. `--allow-shared` is reserved for substrate-earned single-tenant deployments (Lumen on dedicated Pi) where the workspace genuinely has one owner. Otherwise: error, exit 2.
- `cmd_set` rejects payloads containing `continuity_token` at v2. Helper becomes the gate the hook layer was supposed to be. (S11 landed the *intent* at the post-identity hook layer; S20 moves the *check* into the helper, so out-of-tree callers can't bypass through it. Direct writers like `onboard_helper.py` are addressed in §3c.)
- New `cmd_list` returns slot inventory `(slot, uuid, updated_at)` tuples sorted by recency. Bootstrap callers use this for the scan-newest fallback.

### 3b. Bootstrap rewiring (hooks) — already shipped at the plugin hook

PR #19 (`fix(session-start): only read slot-scoped workspace cache, never bare session.json`, commit `87affc9`) already shipped the hook-layer change this section originally proposed. `hooks/session-start:109-140` reads Claude Code's `session_id` from SessionStart stdin and slot-scopes the cache file. The proposal here reduces to:

- **Verify** the hook contract holds across plugin updates (regression test that the hook never falls back to flat `session.json` even on absent stdin).
- **Add scan-newest fallback** as a secondary lineage hint when the slot-scoped cache misses (cross-`/clear` lineage discovery, per §2b). Surface to the agent as "this workspace was last run by `<UUID>` (slot `<X>`, `<N>` minutes ago) — declare `parent_agent_id=<UUID>` if you inherit." Strictly additive; does not change the existing slot-scoped read.
- **Audit other readers** (Codex commands, dispatch worker, SDK, ad-hoc scripts) for slot-scope compliance. Anything still defaulting to flat `session.json` is the residual leak surface.

The load-bearing S20 work is at the helper layer (§3a) and the direct-writer (§3c). The hook is already correct.

### 3c. Direct-writer parity (`onboard_helper.py`)

The unitares-side `scripts/client/onboard_helper.py` bypasses the helper entirely. Two options:

- **Option C1 (preferred): converge on `session_cache.py`.** Replace `_write_cache` with a subprocess call to the plugin helper. Single enforcement point. Requires the unitares repo to depend on the plugin helper at runtime — already true via the bundled CLI.
- **Option C2 (fallback): mirror the contract locally.** Set mode 0600 in `_write_cache`, add the `continuity_token` rejection. Two enforcement points stay in sync by convention.

C1 is the descriptive-floor move (single source of truth); C2 is the pragmatic move if the dependency tax is real. Decision deferred to PR; either satisfies S20.

### 3.5 Pre-implementation audit (2026-04-26)

Before opening the §3a implementation worktree, audited every plugin caller of `session_cache.py set session`. Findings:

| Caller | Slot? | Behavior |
|---|---|---|
| `commands/governance-start.md` | `--slot=<client_session_id>` | Fixed in S11-a (`unitares-governance-plugin@ad4dfef`). |
| `hooks/post-identity:139` | `--slot "${SLOT}"` (line 135 sets from hook stdin) | Compliant. |
| `hooks/post-edit:208` (`bump-edit`) | `--slot "${SLOT}"` | Compliant for the bump-edit subcommand. |
| `hooks/post-edit:221` (`set session`) | **None** | **In-file inconsistency** — uses `SLOT` for `bump-edit` 13 lines earlier, then drops it for the milestone-timestamp write. |
| `hooks/post-edit:192` (`SLOT` default) | `"${SLOT:-default}"` | **Defeats slot-scoping** — literal string `"default"` collapses every slot-less process onto one shared `session-default.json`. |
| `hooks/post-checkin:50` (`set session`) | **None** | No `SLOT` variable in scope at all; total flat-file writer. Errors swallowed via `\|\| true`. |

**Implication:** S20.1 as originally written ("`cmd_set` rejects slotless writes") would silently brick the auto-checkin milestone pipeline on every channel that goes through `post-checkin` and `post-edit` (errors swallowed; pipeline degrades quietly). Helper-level enforcement is only honest *after* the hook write paths conform.

**Operator decision (2026-04-26, accepted):** sequence implementation as

1. **PR1: hook fixes.** `post-checkin` reads `CLAUDE_SESSION_ID` from its hook stdin (same pattern as `session-start`) and passes `--slot`. `post-edit:221` adds `--slot "${SLOT}"` to the second invocation. The `SLOT="${SLOT:-default}"` fallback either errors out when SLOT is missing (safer) or scopes per-PID with `pid_$$` style (functional fallback). Tests prove `last_checkin_ts` is written on every supported channel under realistic hook-stdin shapes.
2. **PR2: helper rejection.** Once PR1 is in tree and the auto-checkin pipeline is proven on the slot path, `cmd_set` hard-rejects slotless writes (with `--allow-shared` escape).
3. **Optional warning-only grace** *only* if PR1 reveals an uncovered client path that can't be fixed in tree (e.g., third-party hooks). Mirrors S1's grace pattern but only as fallback — not the default.

Reasoning Kenny offered (folded in): a warning period is useful when consumers need migration time, but here the first problem is that the *replacement path is not fully audited*. Audit first, then enforce.

### 3d. Migration

- Existing flat `<workspace>/.unitares/session.json` files: read-only legacy, ignored by writes, surfaced by `cmd_list` for one release as a lineage candidate. Operator-runbook entry: `rm <workspace>/.unitares/session.json` after upgrade.
- Existing slotted `session-<x>.json` files: unchanged. Pruning policy is out of scope (separate cleanup row — too many on disk over time).

(Skill/command-text alignment was previously bundled here as §3d in the first draft; it carved out to **S11-a** because it is an S11 regression, not S20 scope. S20 lands independently of the skill fix and vice versa.)

## 4. Threat closure (honest accounting)

What S20 reduces:

- **Teaching-surface convergence on a shared file.** The system stops *recommending* a single workspace cache as the "current" identity surface. Hooks, helpers, and skills route through slot-scoped paths or scan-and-pick-newest. This is descriptive — it brings the system's behavior in line with the ontology, nothing more.
- **Channel partition** (caveats below). Codex sessions and Claude sessions in the same workspace stop colliding on flat `session.json`; each writes its own slot file. CLAUDE.md's stated invariant ("`.unitares/session.json` is the Codex cache; Claude shouldn't reference it") is a *read prohibition* that S20 does not satisfy — slot-scoping just gives Claude its own file *to read*, which is a different invariant. S20 makes the channel-bleed concern moot at the file-collision level; the original CLAUDE.md invariant remains a separate concern.
- **The S11 backslide** (post-identity hook path). Helper-level rejection of `continuity_token` in v2 cache prevents future regressions on the hook path. The skill-text regression (S11-a) is a separate fix.
- **Umask gap on the direct-writer path.** §3c ships mode 0600 parity (or removes the writer entirely via C1).

What S20 does **not** reduce meaningfully:

- **Same-UID file enumeration.** A same-UID process can `ls <workspace>/.unitares/` and read every slotted file trivially. The reduction is "attacker reads N files instead of 1" — negligible under the threat model. Real defense is S1-A′ (token can't be replayed without server-side process attestation).
- **Active fork-time clone.** A child process that inherits memory or copies `~/.unitares/` wholesale gets every slot. Slot-scoping is a partition against passive-read habits, not against active duplication. Fork-detection ontology is the right tool for that branch.
- **Same-process introspection.** A debugger or `/proc` reader against the live process still extracts the token. Out of scope; substrate-level concern.
- **Token forgery.** Bearer tokens remain bearer until S1-A′.
- **Substrate resume hijack.** S19's territory.

The honest summary: **S20 stops the system from teaching the shared-cache pattern. It does not stop a determined attacker who wants to recreate it.** The earned defenses are S1-A′ and S19; S20 makes them composable by removing the easier passive vector.

## 5. Sequencing

| Step | What | Depends on | Gating |
|---|---|---|---|
| ~~S20.0~~ | ~~Empirical check~~ | — | **Answered 2026-04-25** — session ID stable within session, fresh on `/clear`; slot-scope is correct writer key, scan-newest is correct lineage reader |
| **S20.1a** | **PR1 — hook write-path fixes** (`post-checkin`, `post-edit:221`, `post-edit:192` `default` fallback). Tests cover `last_checkin_ts` on every channel under realistic hook stdin. | none | **Blocks S20.1b** |
| **S20.1b** | **PR2 — helper-side `cmd_set` rejection** of slotless writes; `--allow-shared` gate; v2 token-write block; new `cmd_list`. | S20.1a | none |
| S20.1c | (Optional) Warning-only grace period in helper, only if S20.1a surfaces an uncovered client path | S20.1a | none |
| S20.2 | `hooks/session-start` audit + scan-newest secondary fallback (additive — slot-scoped read already in place via PR #19) | S20.1b | none |
| S20.3 | `onboard_helper.py` parity (C1 preferred; C2 fallback) | S20.1 | none |
| ~~S20.4~~ | ~~Codex equivalents (commands + post-identity hook on Codex side)~~ | S20.1 | **Resolved 2026-05-06** — unitares-side `scripts/client/session_cache.py` mirrors slot enforcement/token rejection/list inventory, and Codex command docs no longer teach flat cache writes |
| S20.5 | Operator-runbook migration note + flat-`session.json` cleanup guidance for pre-PR-19 files on disk | S20.2, S20.3, S20.4 | none |
| ~~S20.6~~ | ~~Tests: helper rejects slotless write; helper rejects token-bearing payload at v2; hook regression-tests slot-scope-only read; `onboard_helper.py` writes mode 0600 (if C2) or routes through helper (if C1)~~ | S20.1–S20.4 | **Resolved 2026-05-06 for unitares-side surfaces** — `tests/test_client_session_cache_contract.py` covers helper rejection, list inventory, legacy-token strip, and command-doc drift; `tests/test_client_session_cache_perms.py` + `tests/test_onboard_helper.py` cover mode 0600/direct-writer parity; plugin hook regression tests shipped with S20.1/S20.2 |

No grace period needed beyond the helper-level rejection: flat `session.json` is treated as read-only-legacy from S20.1 forward. Existing files keep working as lineage candidates; new writes are slotted.

## 6. Open questions

- **`--allow-shared` policy.** Substrate-earned single-tenant case (Lumen) genuinely wants a stable shared file. How is the gate gated? Env var? Config field? Operator declaration? Defer to S19's substrate-claim work — same registry.
- **Slot-pruning.** Many slot files accumulate over weeks of dogfooding. Storage and auditability are fine; stale slots show up in `cmd_list` and skew "newest" scans if a long-dead session has a high `updated_at` (it shouldn't, but worth a TTL on the scan side: ignore slots updated > 30 days ago for lineage-candidate selection).
- **Slot leakage as fingerprint.** Slot strings reveal harness identity (`claude_code-…`, `codex-…`). For a single-operator workstation this is fine; for shared-CI hosts it leaks who runs what. Acceptable for the dev-fleet scope this targets; flag for any future multi-tenant deployment.
- **`bind_session` and other token consumers.** S20 does not touch them; they read tokens from arguments, not from cache. Out of scope. S1-A′ is where they get tightened.

## 7. Relationship to other rows

- **S1**: token format. Orthogonal — S20 removes the cache-as-shared-credential pattern; S1-A′ removes the token-as-bearer pattern. Either alone leaves a hole. Together they get to "earned, narrowed."
- **S2**: auto-resume retirement. Orthogonal. S2 is *semantic* (don't auto-resume on read); S20 is *spatial* (don't share the file). Composable but independent. (Earlier framing as "S2-a sub-spec" was wrong; corrected per architect review.)
- **S5**: resident-fork inversion (resolved). S20 doesn't change resident-fork semantics. Residents that legitimately use shared substrate-earned caches go through `--allow-shared` (§3a).
- **S11**: cache contents (resolved at hook path). S20 moves the enforcement from "hook is supposed to" to "helper rejects" — closing the convention-drift gap that S11 alone leaves open.
- **S11-a**: skill text drift (companion row). Carves out the skill/command-text fix that was incorrectly bundled here in the first draft. Ships independently.
- **S19**: substrate attestation. Independent. S19 fixes resume-time verification; S20 fixes pre-resume cache placement. A substrate-earned agent under S19 can still use `--allow-shared` for its single-tenant cache.

## 8. What this document does not commit to

- The exact slot string for each harness. §2a lists candidates; harness-by-harness verification needed before code (S20.0 covers Claude Code).
- The migration cutover date. Steps S20.1 through S20.6 can ship over multiple weeks; no big-bang.
- A field rename. S20 keeps `session.json` filenames, just with slot suffixes. A broader cache-format reorganization is out of scope.
- C1 vs C2 for the direct-writer path. PR-time decision.

## 9. Stance check (axiom gate)

- **Axiom #3 (build nothing that appears more alive than it is).** Slot-scoping is descriptive — it stops the *teaching surface* of "the workspace's current identity" from existing. It does not raise attacker work meaningfully against a same-UID process. Honestly labeled: **"convention-level partition, not earned defense."** The earned defense is S1-A′. ✓ (descriptive)
- **Axiom #5 (process-instance boundaries are real).** Per-slot files honor the boundary at the *teaching surface* level. ✓ (descriptive)
- **Axiom #10 (memory is not identity).** The cache becomes a lineage hint surface, not a resume credential. ✓
- **Axiom #11 (let embodiment anchor expression).** Substrate-earned single-tenant agents retain `--allow-shared` opt-in. ✓
- **Axiom #14 (let learning deepen reality, not theater).** Helper-level rejection is **convention-level, advisory** — bypassable by any caller writing JSON directly to the path. It is not a daemon; it is a checked subroutine that well-behaved callers route through. The deeper move (server-side process attestation, S1-A′ + S19) is where the reality-deepening happens. S20's contribution is removing the *taught* shared-cache pattern so the deeper move composes cleanly. ✓ (with this honesty caveat — earlier draft over-claimed)

Pattern holds at the descriptive layer. The inventive-stance work continues to live in S1-A′ and S19.
