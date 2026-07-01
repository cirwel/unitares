# Principal Rollup — counting the integral, not the point-value

**Status:** v0 proposal — measurement shipped (#877, #880); **Move 3 reframed by council 2026-06-18 (see amendment) — derive, do NOT store-at-mint**
**Surface:** identity / onboarding (single-writer; see CLAUDE.md)
**Relates to:** `docs/ontology/identity.md` research-agenda #3; the
`participated/never_participated` view (#822); the anon-ghost mint source
(dispatch_beam, closed 2026-06-18).

## Council amendment (2026-06-18)

A three-member council (ontology, implementation, live verification) reviewed
Move 3. **Unanimous verdict: do NOT resolve-or-create `principal_id` at onboard.
Keep the principal DERIVED.** Move 3 is reframed; the surface goal ("you are
instance K of principal P") is kept, delivered without a stored mint-time FK.

**Why (each member, independently):**
- *Ontology:* storing a point-in-time grouping contradicts the doc's own
  "identity as **integral**, not point-value" — an integral is *recomputed over a
  window*, not frozen. **Onboard is the moment of LEAST information about the
  component** (most lineage/thread edges arrive later). Freezing it at mint is
  systematically wrong for exactly the persistent-worker cases that motivate this.
- *Implementation:* the mint path (`resolution.py` PATH 3) is load-bearing and
  anyio-asyncio-hardened; adding union-find + a write there is the dangerous
  await class. Resolve-at-mint also re-opens the **false-archival** race
  (concurrent siblings on a shared thread/parent → incoherent root) and forces a
  stored-FK **re-point** on every late lineage edge, with no safe mechanism.
  Derived cost is sub-ms over in-memory dicts — storing is premature optimization.
- *Live-verifier (ground-truth):* root identities have **no** principal context
  at mint; **65% of active agents have no `parent_agent_id` at all**; lineage-at-
  mint covers only a minority; multi-root threads are the norm (15 active threads
  with >1 root); 5 active cross-thread parents would force after-the-fact merges.

**Reframed Move 3 (the smallest viable, off-hot-path increment):** a background
reconciler (like `deep_health_probe_task`) recomputes the rollup every ~60s into
an in-process `agent_uuid → principal_root` map; the onboard/identity **response**
appends `principal_id` + `instance_count` via a single post-mint dict read,
**fail-open null**, touching zero write paths and needing no migration. A *stored*
`core.principals` table (if ever) is a **post-2026-06-24-Wave-3** question, owned
by the BEAM side (correct write sequencing), populated by reconciler — **never
resolve-at-mint**.

**Must-fix before any implementation:**
1. **Invariant (code + doc + test): `principal_id` MUST NEVER authorize resume,
   rebind, write-attribution, or tier.** Display/count-only; rejected on every
   write path. (Currently safe — no resume path keys on `thread_id`/`parent`,
   verified — but a future "resume by principal" surface would re-open S19.)
2. **Unattachable anonymous onboard → `principal_id: null`, NOT "its own
   singleton."** A singleton-principal *object* lets the anon-ghost class
   re-inflate the count; pair with the mint-time non-persistence discipline.
3. **Split thread-union from lineage-union.** They are different layers (thread =
   conversation/Memory artifact; lineage = causal declaration). Report
   `principal_by_lineage` and `principal_by_thread` separately; the union is the
   *coarsest* view, labeled as such — do not silently assert thread≈causal.
4. **Gate the harness instance-key (Sentinel) on the substrate-earned appendix's
   three conditions.** A generic file-`id_path` is closer to the `session.json`
   performative failure case than to Lumen's hardware; only a true dedicated
   substrate earns it.
5. **Pin + label the population scope on every principal count** (active-7d vs
   all-time partition differently — 38 vs 117 for the same worker).

**Verification corrections to THIS doc / the tool (must fix for accuracy):**
- The tool (`octopus_rollup.py`) reads `thread_id` from `identities.metadata`,
  but **127 agents carry `thread_id` in `core.agents.thread_id` and NOT in
  metadata** → it undercounts connectivity, so "530 principals" slightly
  *over*counts (false singletons). Reconcile the thread_id source.
- **Dual-store drift is live:** `core.identities` active = 817 vs `core.agents`
  active = 761 — **56 identities are active in one store, archived in the other.**
  Any honest count must reconcile this (relates to `uuid-keyed-identity-migration`).
- **The `_shadow` tables are EMPTY (0 rows, dead surface)** — so a derived
  principal sidesteps the shadow-parity hazard entirely (another argument for
  derive-over-store).

The numbers reproduced at the tool level (817→530; 38 active / 117 all-time), but
the underlying population is soft per the two drift findings above.

## The gap, in the ontology's own words

`docs/ontology/identity.md` already contains this idea and already names the
problem — it just isn't first-class:

> **Statistical lineage (identity as integral, not point-value).** Many fresh
> process-instances under a role, each with declared lineage and observed
> behavioral consistency, accrue into something functionally identity-like over
> time. This is how role-level trust already works, but it is not yet
> first-class — **we aggregate by UUID, not by behavior-under-role.** *(research
> agenda #3)*

and lists the consequence under **Performative**:

> **Behavioral-continuity-by-UUID-match** — current trust tier assumes N
> observations under one UUID means N observations of the same subject; under
> process-instance ontology it means N observations across potentially many
> subjects sharing a role.

The system mints one **identity** per *process-instance × onboard event* (the
`force_new` posture — correct for **write-accountability**: every writer is a
distinct, attributable subject). That is the **point-value**. But the unit we
*count, display, and reason about* — "how many agents," the dashboard headline,
trust aggregation — is also the point-value. A persistent worker (the BEAM
Sentinel, the Discord Hermes harness, a Claude session-chain across `/clear`s)
sheds a fresh identity per episode, so the count reports **tentacles**, not the
**octopus**.

The **principal** is the integral the ontology describes, made first-class: the
logical worker that a stream of process-instances are facets of. The tentacle
stays the accountable write-unit; the principal becomes the counted/displayed
unit. Counting by principal does **not** claim any instance *is* another — it
says "these instances are facets of one worker," consistent with the doc's
honest-memory posture (#2: *"I inherit from X; I am not X"*).

## What a principal is

A principal is a **connected component** over only the edges the agent **itself
declared** — never inferred:

- **declared lineage** — `parent_agent_id → agent_id`, which the ontology
  defines as *causal, not coincidental* ("Lineage is causal, not coincidental");
- **shared `thread_id`** — same conversation / logical worker (the one shape the
  model already rolls up correctly, e.g. Hermes accumulating thread nodes).

Two grouping keys are **deliberately excluded**, because using them would
re-introduce exactly the performative continuity the ontology rejects:

- **IP:UA fingerprint** — spoofable and legitimately *shared* (a bridge and a
  gateway both on localhost `httpx` hash to one fingerprint). The prefix-bind
  hijack work (#802) already established IP:UA is not a credential. A principal
  built on it would mis-merge unrelated workers — coincidental, not causal.
- **The `<harness>_<date>` label** — `mcp_20260414` is hundreds of unrelated
  same-day process-instances. The ontology already names "label-as-identity" as
  performative. A daily cohort is not a worker.

True singletons — a one-shot session with no lineage and no thread reuse — stay
singular. That is honest: they *are* singular. The compression lands exactly on
the persistent/recurring workers, which is where the model already holds the
signal (thread, lineage) but refuses to count by it.

## Where it sits in the five-layer taxonomy

The principal is not a sixth layer; it is the **aggregation across
process-instances** at the Role + Lineage + Behavioral layers — the very layers
the doc says *can survive process death*. The tentacle is the **Process-instance**
layer (the only phenomenologically-continuous, shortest-lived layer). "No single
layer is identity; identity is the shape of the layers together" — the principal
is that shape made countable.

## Live numbers (2026-06-18, via `scripts/dev/octopus_rollup.py`)

```
scope: active identities
tentacles (identities): 817
octopi (principals):    530        (1.54x overall)
  41 multi-instance octopi hold 328 tentacles  (counted today as 328 agents)
  largest active worker: 38 process-instances shown as 38 separate agents
  largest all-time worker: 117 process-instances shown as 117 agents
```

The 1.54× overall is modest *because* most identities are honest singletons —
which is the point. The signal is **concentrated**: the persistent workers you
most want to track over time are precisely the ones shattered up to 38× (active)
/ 117× (all-time).

## Staged plan — each step reversible; the write-path steps are operator-gated

1. **Measure** *(shipped — `scripts/dev/octopus_rollup.py`, read-only, no schema
   change).* Turns the gap into a tracked number before anything touches the
   writer-locked path.
2. **Count the octopus, not the tentacle** *(operator-gated; additive, no
   migration).* Re-express the headline and the `participated/never_participated`
   view to group by principal: a principal *participated* if **any** tentacle
   checked in, *active* if **any** is live. (#822 filtered ghost tentacles; this
   rolls up — strictly more honest, and it subsumes #822.) Ship additively (a
   principal-grouped surface alongside the existing count) so nothing breaks.
3. **Promote to `principal_id`** *(operator-gated; ontology change + migration).*
   A FK resolved-or-created at `onboard` from declared lineage / shared thread /
   a **harness-persisted instance-key** (generalize `dispatch_beam`'s `id_path`,
   which already does this for one harness). The identity keeps its per-process
   uuid — **write-accountability is untouched**; it gains an octopus pointer.
   Surface `principal_id` + "instance K of principal P" in the onboard/identity
   response so **future agents can see their own octopus honestly** instead of
   mistaking the per-process uuid for selfhood.

## The honest hard edge

You cannot always know the octopus. A lineage-less, thread-less, anonymous
onboard has **no attachable principal** — by construction a singleton. **Do not
fabricate one.** This is why the rollup must be paired with **mint-time
discipline**: an unidentified, zero-work onboard should not durably persist a row
at all. (This is the anon-ghost class — 155 of which, from one dispatch_beam
bench path, were the dominant fresh-mint source until 2026-06-18. The rollup
addresses *connectable* tentacles; the mint-time fix stops *unconnectable, idle*
ones from accruing. They are complementary, not alternatives.)

The two together move the count from "tentacles" to "octopi + genuinely-singleton
workers," and stop the ghost noise from growing.

## Rejected alternatives

- **Make `force_new` resume one identity (token/anchor).** Rejected: re-opens the
  copyable-bearer / S19 vector that #802/#807 closed, and destroys per-process
  write-accountability. The tentacle *should* stay per-process; the principal is
  a rollup *on top*, never a replacement for minting.
- **Group by fingerprint or label.** Rejected above — coincidental, not causal;
  the ontology already names both as performative.
- **Only hide ghosts (#822, as-is).** Necessary but insufficient: it cleans the
  count but never yields "Hermes is one worker across 7 instances." The principal
  rollup subsumes it.

## Proposed `identity.md` addition (for operator review — not applied here)

A short subsection under *Layered taxonomy of continuity*, promoting research
agenda #3 from "not committed" to a named, first-class grain:

> ### Principal (the aggregate subject)
>
> A **principal** is the integral of process-instances that declared themselves
> connected — by causal lineage (`parent_agent_id`) or shared `thread_id`. It is
> the first-class form of research-agenda #3 ("identity as integral, not
> point-value"). It is the unit the fleet should **count and display**; the
> per-process identity remains the unit it **writes and attributes** with. A
> principal makes no claim that one instance *is* another — only that they are
> facets of one logical worker. It is built **only** from declared/causal edges;
> fingerprint and label are excluded, for the same reason they are performative
> everywhere else in this document. A process-instance with no attachable
> principal is its own singleton — honestly singular, never fabricated into a
> group.

## Open questions

1. **Instance-key for harnesses without a thread.** Threads cover conversational
   agents; persistent daemons (Sentinel) need a stable per-deployment key. The
   substrate-earned-identity appendix (the hardcoded-UUID pattern) is the
   strongest form; a harness-persisted `id_path` is the portable form. Which is
   canonical?
2. **Behavioral edges (research #1).** Should a *verified* behavioral-continuity
   match also union two instances into one principal, or stay advisory? Starting
   declared-only is the conservative, honest floor.
3. **Trust aggregation.** The doc flags UUID-keyed trust as performative. Should
   trust tiers aggregate at the principal grain once it exists? (Likely yes — but
   that is a separate, careful change.)
