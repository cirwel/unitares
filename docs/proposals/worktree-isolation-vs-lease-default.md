---
status: v0.2 — counter-note / companion to surface-lease-plane-v0.md (NOT a replacement). Consolidates the duplicate #584 (worktree-isolation-as-default-2026-06-03.md, written in parallel) into this doc; the duplication is itself analyzed in §8.
authored: 2026-06-03
author_session: dispatch thread-1511767523715055857 (claude_code, opus-4-8); consolidation by a second parallel session (claude_code, opus-4-8) folding #584
relates_to:
  - docs/proposals/surface-lease-plane-v0.md (the canonical Surface Lease Plane / Plexus contract; SHIPPED)
  - docs/proposals/plexus-scope.md (boundary doc; names repo file paths as the initial forcing function)
  - KG 2026-04-14T23:37:36 (multi-agent git reset destroyed ~400 lines of WIP — the surface-collision incident the lease plane cites)
stance: |
  The Surface Lease Plane is well-designed and correct for un-forkable shared
  surfaces. This note argues ONE narrow thing: for the repo-file / working-tree
  surface class — named as Plexus's initial forcing function — per-agent worktree
  isolation is the more fundamental fix, and leases should cover the residue
  isolation cannot, not the front line of file edits.
---

# Worktree isolation as default; leases for the residue

> **Design record.** A planning / RFC document kept as design provenance; it captures intent at a point in time and may lag the running code. For current behavior see [`UNIFIED_ARCHITECTURE.md`](../UNIFIED_ARCHITECTURE.md) and the runtime sources it points to.

## 0. What this note is and isn't

This is **not** a proposal to remove or replace the Surface Lease Plane. The lease
plane shipped (PR #305, Phase A; #476 resident enforcement), the RFC discipline is
exemplary, and leasing is the right primitive for the surfaces named below. This note
disputes only the **default layer for repo-file collisions**, which `plexus-scope.md`
calls "the initial forcing function." Concretely: prefer isolation where the resource
is forkable; reserve leases for where it isn't.

## 1. Claim

For the **repo working-tree** surface class, **per-agent git worktree + branch
isolation** dominates advisory file-path leasing on every axis that motivated the
lease plane, because a working tree is *forkable* and git is a DVCS built for
parallel divergent work plus merge.

| | File-path lease (advisory) | Worktree-per-agent + branch |
|---|---|---|
| Two agents edit same file | serialized (one waits) | both proceed; git merges / flags conflict at integration |
| `git reset --hard` blast radius (the 2026-04-14 incident) | **not protected** — reset is tree/HEAD-level, not file-level | impossible — each agent only owns its own worktree |
| Holder dies mid-edit | corpse lock until TTL/`:DOWN` reap | no lock to leak; branch just sits there |
| Unaware / non-integrated caller | bypasses advisory entirely (see §3) | cannot collide — it has no handle to others' trees |
| Parallelism | bounded by contention on hot files | full; N agents, N trees |

## 2. The granularity mismatch

The lease plane's headline incident is a `git reset --hard HEAD` that destroyed ~400
lines of another holder's uncommitted WIP. A lease on `file:///…/x.py` would **not**
have prevented it: `reset --hard` operates on the whole working tree and HEAD, not on
a path. So even accepting the lease framing, the correct git surface is the
**worktree/branch**, not individual files — and once the surface is "the whole
working tree," the natural implementation of "one holder per working tree" is simply
*give each agent its own working tree.* The lease collapses into isolation.

## 2a. The RFC concedes this in its own text (folded from #584)

Two clauses in `surface-lease-plane-v0.md` make the case for us:

- **§7.12.3 (worktree handling, lines 937 & 1442):** "A lease on `…/src/x.py` and a
  lease on `…/.worktrees/foo/src/x.py` are distinct leases by design — different
  physical files even though they're 'the same logical source.' This is correct and
  intended." Distinct leases means **no contention** between worktree-isolated sessions
  on the same logical file — so the file lease coordinates *nothing* for them. It only
  does work on a shared checkout. The RFC's file-surface value is conditional on the
  exact practice (shared-checkout editing) that worktree-default removes.
- **Internal tension:** the thundering-herd analysis (line 653) reasons about "ship.sh
  fans out N parallel session worktrees, all attempting `lease_acquire('file:///<path>')`"
  with one winner and N−1 `held_by_other`. That convoy can only occur if those N
  worktrees resolve to the *same* `file://` path — which contradicts the distinct-lease
  rule above. Under worktree-default the contention the RFC defends against cannot arise.

## 3. Advisory-on-a-shared-filesystem cannot bind the unaware actor

Advisory coordination only helps callers who opt in. Live evidence from this very
thread: an agent (me) edited the operator's live `~/projects/unitares` checkout
directly, acquired no lease, and only discovered the collision after a concurrent
commit landed mid-session. No lease infrastructure prevents that, because an advisory
HTTP service cannot stop a POSIX `write()`. The only real enforcement for the file
class is the OS: separate worktrees / permissions / containers. So the strict-mode
endgame for repo files *reduces to isolation anyway* — better to start there.

## 4. Where leasing is genuinely right (the residue)

Isolation only dissolves collisions on **forkable** resources. These are NOT forkable
and remain squarely lease-plane territory — this note does not touch them:

- the single deploy worktree + `com.unitares.governance-mcp` LaunchAgent (one deploy at a time);
- the one Postgres `governance` DB and shared `data/agents/*.json` runtime state;
- `resident:/…` lifecycle restart/upgrade windows;
- `dialectic:/…` session ownership (reviewer-assignment races);
- `capture:/…` windows on real sensors; Lumen's hardware/display.

For all of these there is no "fork a copy" move; you must coordinate. Leases (TTL +
holder-UUID + OTP `:DOWN`) are the right tool, and the corpse-lock fix is real value.

## 5. The steelman against this note

Worktrees isolate *source* but not *runtime state*. UNITARES agents share the running
MCP, the DB, and per-agent state files — so isolation alone is insufficient and you'd
still need leasing for that shared state. **Agreed** — which is exactly §4. The
disagreement is only about the *default front line*: today it's file-path leasing;
this note argues it should be worktree isolation, with leases scoped to §4.

## 6. Proposal

1. Make **worktree-per-agent + branch-per-task** the default execution context for any
   agent doing code work (the dispatch harness already mints a scratch worktree; the
   gap is that agents reach *out* of it to the live checkout — close that gap).
2. Add a **start guard** that refuses code edits when CWD is a shared/live checkout
   rather than an agent-owned worktree (extends the existing "workspace start guard").
3. Keep the Surface Lease Plane as the coordinator for the §4 residue. Demote the
   `file://` repo-path surface from "forcing function" to "fallback for the rare
   genuinely-shared file edit" once isolation is the default.
4. Integration stays via normal branch → PR → merge; conflicts resolved by git, not by
   serializing access.

## 7. Open questions

- Worktree lifecycle/GC: who prunes abandoned agent worktrees, and on what signal?
  (Mirrors the lease-reaper question — same `:DOWN`/TTL thinking applies.)
- Shared `data/`: should agent worktrees get isolated `data/` dirs, or is that the
  shared-runtime-state residue that stays under leases? (Probably the latter.)
- Cost: bge-m3 load and per-worktree venv/state overhead vs. the concurrency tax the
  lease plane is paying. Needs a measurement, not a guess.

## 8. Live evidence: this note collided with itself

This note existed twice. Two agents on the same account, in parallel dispatch sessions,
independently wrote the same counter-note — this doc (#582) and
`worktree-isolation-as-default-2026-06-03.md` (#584) — both green, both merged within
~25 minutes. This consolidation folds the second in. The collision is the most useful
data point here, because it **refines the thesis**:

- **Isolation prevented the mechanical collision.** Different filenames → no git
  conflict → both merged clean. The forkable-surface argument (§1–§3) held perfectly:
  no `write()` contention, no lock, no lost work.
- **Isolation did NOT prevent the semantic collision.** Two agents did the *same work*
  because nothing coordinated them at the *task* level. Worktree isolation dissolves
  collisions on the bytes; it does nothing about two agents deciding to write the same
  bytes.

So the residue that genuinely needs coordination (§4) is larger than "un-forkable
runtime surfaces" — it includes **task-level work claims**. The control for that is
already written down: CLAUDE.md's "Before Starting Work on a Single-Writer Surface" rule
(run `gh pr list --search` before touching a hot proposal doc). It did not fire here
because neither session ran it. That is the honest correction to a naive "just isolate"
reading: **isolate the bytes, but still coordinate the intent** — via the open-PR check,
an advisory task-lease, or dispatch-level dedup. The lease plane's advisory model is one
valid implementation of that intent layer; this note's whole argument is that it was
pointed at the *byte* layer (files) instead of the *intent* layer (tasks).

**Bias disclosure (folded from #584):** the standing bias of both authoring sessions
(memory `feedback_substrate-migration-status-quo-bias`) is to resist substrate
migrations, which this note's "less leasing, more git-native isolation" conclusion leans
toward. §5 (steelman) and §7 (cost, unresolved) are where it tries to earn the position
rather than assume it. The cost question is genuinely open and needs measurement, not
advocacy — and the fact that two biased sessions converged here is itself weak evidence
the bias is doing some of the work.
