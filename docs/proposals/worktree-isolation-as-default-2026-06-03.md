# Counter-note: worktree isolation as default; leases for the un-forkable residue

**Created:** June 3, 2026
**Status:** position note — a counter-framing to `surface-lease-plane-v0.md`, for the proposals trail. NOT a proposal to remove the lease plane (it is shipped, Phase A/B live, and correct for what it should own). This argues the **front line is drawn one layer too low**.
**Bias disclosure:** the author's standing bias (memory `feedback_substrate-migration-status-quo-bias`) is to resist substrate migrations — and this note leans that pole (less BEAM-side machinery, more git-native isolation). It is written with that thumb-on-scale acknowledged; the falsifier section is where it tries to earn the position rather than assume it.

## Thesis

Two different problems are being solved with one tool:

- **Forkable surfaces** — working-tree files. Two sessions editing `src/x.py` collide *only if they share a checkout*. Give each code session its own `git worktree` + branch and the collision class disappears — not "is made safe," **disappears**.
- **Un-forkable surfaces** — the live MCP process, the `governance` DB and its singleton rows, resident agents (Vigil/Sentinel/Watcher), dialectic sessions, shared runtime state. You cannot fork these; coordination is genuinely required.

The lease plane is the right tool for the second set and the wrong tool for the first. Today its **file:// surface is the advertised front line** (the plugin's per-edit hook leases every file edit), which optimizes making *shared-checkout editing* safe-ish instead of making it *unnecessary*. Leasing is the right tool for the un-forkable; isolation is the right tool for the forkable; **a working tree is forkable.**

## The RFC argues this against itself

This isn't a strawman — `surface-lease-plane-v0.md` states the load-bearing fact in its own worktree-handling clause (§7.12.3, lines 937 & 1442):

> "A lease on `file:///…/unitares/src/x.py` and a lease on `file:///…/unitares/.worktrees/foo/src/x.py` are distinct leases by design — different physical files even though they're 'the same logical source.' This is correct and intended."

Distinct leases means **no contention** between worktree-isolated sessions on the same logical file. So for isolated sessions the file lease acquires successfully every time and coordinates nothing — it is pure overhead. The lease only does work when sessions **share a physical path**, i.e. a shared checkout. The RFC's value is therefore conditional on the exact practice (shared-checkout editing) that worktree-default removes.

Corroborating internal tension: §-level thundering-herd analysis (line 653) reasons about "ship.sh fans out N parallel session worktrees, all attempting `lease_acquire('file:///<path>')`" with one winner and N-1 `held_by_other`. That contention can only occur if those N worktrees resolve to the **same** `file://` path — which directly contradicts the distinct-lease rule at line 937. The convoy vector the RFC defends against is an artifact of *not* isolating; under worktree-default it cannot arise.

The motivating incident in the RFC's own `unblocks` list — KG 2026-04-14, "multi-agent git reset destroyed ~400 lines of WIP — surface collision" — is the canonical case. A `git reset` in session B cannot destroy session A's WIP if A is on its own branch in its own tree. That incident is a **shared-checkout** failure, not a file-leasing gap.

## What this implies for scope

The RFC already gestures at the right destination in its `out_of_scope_explicit` deferrals — "per-agent runtime state ownership in BEAM," "resident supervision tree." **Those are the un-forkable residue.** The recommendation is to promote them from deferred-future to the *primary* surface and demote file:// from front line to backstop:

1. **Default to per-agent worktree + branch isolation for all code work.** (Already the operator's stated working style — memory `feedback_worktree-for-code-work` — and already how ship.sh routes. This note asks to make it the *architectural* default, so file leasing is positioned as a fallback for the un-isolated path, not the main event.)
2. **Re-aim the lease plane at the un-forkable:** singletons, the live runtime, resident lifecycle, dialectic-session ownership, shared state files (deploy checkout, migration slots). These cannot be forked and are where TTL leases earn their keep.
3. **Keep file:// as a narrow backstop** for callers that genuinely cannot isolate (an in-place editor on the deploy checkout, a single shared working dir), explicitly framed as the exception.

## Falsifiers — what would defeat this position

Held honestly, the position loses if any of these is true at fleet scale:

- **Isolation cost dominates.** N worktrees cost disk + per-spawn setup (~200–500ms observed in the agent-orchestrator slice) + duplicated deps/build caches. If the fleet runs many short ephemeral code-agents, the per-worktree tax may exceed the lease's coordination cost. (Mitigation: shared build cache, `git worktree` shares the object store; but deps/`_build` are per-tree.)
- **The shared surface is unavoidably a file.** The deploy checkout (`~/projects/unitares-deploy`) and migration slots are single physical paths that multiple actors must touch. Isolation can't fork these — they are un-forkable surfaces that happen to be files. The lease plane is correct for them; this note's "files are forkable" claim is about *working* trees, not these.
- **Cross-session handoff needs the file as the rendezvous.** If the coordination is "hand this exact file's edit-ownership from A to B" (the per-edit hook's handoff model), a branch-per-session model needs a different merge/handoff story. Worktree-default trades lease-handoff for git-merge — which is the right trade for code, but it *is* a trade, not a free win.

If none of these holds for a given surface, that surface should be isolated, not leased.

## One-line summary

Lease the un-forkable; isolate the forkable. A working tree is forkable, so file-edit leasing should be the backstop, not the front line — and the RFC's own distinct-worktree-lease rule is the proof.
