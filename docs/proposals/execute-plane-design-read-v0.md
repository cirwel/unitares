# Execute-Plane Design Read — pre-registration (v0)

**Status:** Pre-registration only. This commit states the disconfirming
conditions and the shape the answer must take. The comparative analysis is not
in this commit and lands separately on this branch, so the ordering is visible
in history rather than asserted. Commissioned by the design-read charter in
[`relay-substrate-relayering-v0.md`](relay-substrate-relayering-v0.md) (row 3,
decision recorded 2026-09-17), which records **no decision** on the
governed-effect execute plane and requires this document to state disconfirming
conditions for all three options before the read begins.

**Created:** 2026-09-17 · **Carries no implementation authority.** The signed
Wave 3 go-decision authorises scope and explicitly not an implementation start;
a gate document under Wave 3's own terms still owes ratification before anything
is built. Nothing here parks, retires, flags off, or prioritises any option.

## 1. The question

From the charter, unchanged:

> Whether an intercept-based commit path can carry the S7 strong-tier
> re-certification and the per-effect veto without a second credential boundary.

The three options are live and none holds default status:

| | Option | Shape |
|---|---|---|
| (a) | Keep the plane as designed | BEAM `EffectCustodian` holds the leases, owns the payload, calls the §6/§7 veto, and commits — "agents propose, governance commits" |
| (b) | Park the execute half | Express the same property as a NeMo Relay tool-execution intercept plus the existing governance veto |
| (c) | Keep the record-only shadow, retire the execute half | Governed effects are recorded, never committed under custody |

## 2. What was in hand when these conditions were written

Stating this matters, because a condition written after the deciding evidence is
not a condition, it is a conclusion. Read before writing §3, at
`60bd222`:

- The design corpus that defines the options: `governed-effect-plane-v0.md`
  (contract v0.3), `governed-effect-s7-strong-tier-recert.md` (§7, recorded as
  shipped in #1074), `governed-effect-effect-binding-v0.md` (the successor that
  names §7's residual), and the Relay packet itself.
- Three code facts confirmed against source: `/v1/effect-veto` is routed in
  `src/http_api.py`; `recertify_strong_tier` exists in
  `src/mcp_handlers/identity/session.py` with the fail-closed semantics its
  design describes; the execute-type flags in
  `elixir/lease_plane/lib/unitares_lease_plane/application.ex` default off and a
  boot guard refuses an inconsistent pair.
- One property of the shipped Relay integration, from its own module docstring
  and `gate_for_scope`: the policy gate reads worker-maintained state and never
  calls the server inline, so its enforcement latency is bounded by the check-in
  cadence.

**Not yet read, and decision-relevant:** the `nemo_relay` package's middleware
API. Whether the shipped integration's cached, run-scoped gate reflects a limit
of Relay or a choice of that integration is the single largest unknown, and §3
is written without it deliberately.

## 3. Disconfirming conditions

Each condition is a finding that would rule its option out. They are not
weighted, and no option needs a condition to fail in order for another to win.

### Option (a) — keep the plane as designed

- **a1.** If, for every effect class the operator intends to govern, the
  proposer can already perform the same mutation directly with the access it
  holds, then custody intercepts nothing and the plane's enforcement claim is
  ceremony. Decided by enumerating the classes and asking whether the plane sits
  on the only path to the surface.
- **a2.** If the §5b crash reconciliation is undecidable for the classes
  intended — if the surface's post-crash state cannot distinguish "applied" from
  "never started" from "a third party wrote" — then the guarantee (a) rests on,
  that an effect which did not reach `committed` leaves the surface as if it
  never started, cannot be delivered.
- **a3.** If the plane's standing cost — a second runtime, a second credential,
  a boot-ordered recovery scanner, a compile-time per-type allowlist — is one the
  default install will not carry, then (a) describes a capability that exists
  only where the maintainer runs it.

### Option (b) — Relay intercept plus the governance veto

- **b1.** If Relay's middleware cannot deliver a **synchronous** decision that
  the runtime waits for at the call boundary, it cannot host a pre-commit veto at
  all, and (b) fails on mechanism rather than on preference.
- **b2.** If the middleware cannot see the call's **content** when it decides,
  it cannot carry an authorization bound to *this* effect, and (b) delivers a
  run-scoped verdict (§6-class) but not the per-effect veto the question names.
- **b3.** If moving the decision into the proposer's own process removes a
  boundary the current design depends on, (b) trades a real property for a
  cheaper one. **This condition is disarmed in advance if the existing threat
  model already concedes local process compromise.** If the current design does
  not defend against an attacker inside the proposer's process, its absence in
  (b) is not a loss and must not be counted as one.

### Option (c) — keep the record-only shadow, retire the execute half

- **c1.** If any effect class the operator intends to govern is **terminal** —
  it completes before any behavioral signal accrues, so nothing downstream is
  governed — then a record-only shadow records that it happened and cannot
  prevent it, and (c) fails for that class.
- **c2.** A bar, not a test. (c) is admissible **only on a design finding.** No
  usage count may decide it, including a count of zero governed effects executed
  to date: a usage count may retire an instrument, never a capability, and the
  four states a zero cannot distinguish all apply here. If the only surviving
  argument for (c) is that nothing has used it, (c) is out of this read's scope
  and the read must say so rather than quietly grading it down.

## 4. The shape the answer must take

The charter names it: a failure-mode and authority matrix. One row per axis,
one column per option, each cell naming **who decides**, **what they can see
when they decide**, and **what happens when that step fails**.

| Axis | Why it is on the list |
|---|---|
| Identity binding | Which party's identity is proven, and to whom |
| Intent | Whether the deciding party sees the action that was intended |
| Decision | Where the allow/deny is computed and on what state |
| Veto | Whether a denial can still be defeated by the denied party |
| Actual execution | Who performs the mutation, and whether that is the party that was judged |
| Replacement | What happens when the intercept substitutes a different call |
| Retry | Whether a retried effect is re-judged or replays a prior judgement |
| Timeout | What the surface looks like when the decision does not arrive |
| Audit durability | Whether the record survives the failure of the deciding party |
| Recovery | What re-establishes a known state after a crash mid-effect |

Split-brain accountability is the risk the matrix exists to rule in or out: an
in-process intercept can block, replace, retry or fail before the record sees
the intended action, and the record can veto on state the runtime cannot
atomically observe. A finding of "no split-brain" must be shown per axis, not
asserted once.

## 5. What decides nothing in this read

- Any count of effects proposed, executed, or vetoed. This is a design read.
  Adoption figures are not evidence about whether a mechanism can carry a
  guarantee, and none is consulted.
- Which option is cheaper to build. Cost is reported where it bears on a3, and
  it decides nothing on its own.
- That one option is already partly built. Sunk work is not an argument, in
  either direction.

## 6. What this read cannot do

It cannot authorise an implementation start, flip a flag, park a surface, or
change any option's status. Its output is a matrix, a finding per condition
above, and a recommendation the operator is free to take or leave. Row 3 of the
decision record stays undecided until the operator acts on it — the read
concluding is not the operator deciding.
