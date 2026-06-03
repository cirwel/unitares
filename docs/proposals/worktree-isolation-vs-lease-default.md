---
status: v0.1 — counter-note / companion to surface-lease-plane-v0.md (NOT a replacement)
authored: 2026-06-03
author_session: dispatch thread-1511767523715055857 (claude_code, opus-4-8)
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
