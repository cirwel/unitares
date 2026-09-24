# Execute-Plane Design Read — pre-registration (v0)

**Status:** Pre-registration only. This commit states the disconfirming
conditions and the shape the answer must take. The comparative analysis is not
in this commit and lands separately on this branch, so the ordering is visible
in history rather than asserted. **Re-read 2026-09-23 (proposals audit):** the analysis has since landed in this
file (c98f84fa, §§7–12); the sentence above describes the first commit only. Commissioned by the design-read charter in
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

---

# The read

**Added 2026-09-17, in a commit after the pre-registration above, which is
unchanged.** Evidence: the repository at `60bd222`, and `nemo_relay` 0.8.4 as
installed (`/usr/local/lib/python3.11/dist-packages/nemo_relay`). Every claim
below names the file it came from.

## 7. Findings against the pre-registered conditions

### b1 and b2 are refuted: Relay can host a synchronous, content-aware veto

This is the read's largest correction to its own starting picture. The type
signature in `nemo_relay/__init__.pyi`:

```python
ToolConditionalExecutionGuardrail: TypeAlias = Callable[
    [str, Json], Optional[str] | Awaitable[Optional[str]]
]
```

Three properties follow directly. The guardrail **receives the call's JSON
arguments**, so content is visible at decision time — b2 fails. It **may return
an awaitable**, so it can call the server and the runtime waits for the answer —
b1 fails. It returns `None` to allow or a string to block, and
`guardrails.py:265` states these "run before request intercepts and before the
tool callback is invoked," so the block is genuinely pre-execution.

`intercepts.register_tool_execution` goes further. Its callback is
`fn(tool_name, args, next_call)` and, per `intercepts.py:128`, "may await or
call `next_call(args)` to continue the chain, modify the result, or bypass
downstream execution entirely."

So the shipped integration's cadence-bounded, run-scoped, content-blind gate is
**a choice of that integration, not a ceiling of Relay**. The choice was
deliberate and is documented in the module's own docstring — Relay "dispatches
subscribers on its own thread and requires them to be infallible and quick," so
the integration enqueues and lets a worker thread do the talking. That is a
sound design for an exporter. It is not evidence about what the runtime can do,
and this read would have reached the wrong answer by generalising from it.

**Neither b1 nor b2 fires. Option (b) is not ruled out on mechanism.**

### b3 is disarmed, exactly as the pre-registration anticipated

The pre-registration disarmed b3 if the current design already concedes local
process compromise. It does, in two places.

`governed-effect-effect-binding-v0.md` §6 records the closure as
"remote-capture-only — a local co-resident reading process memory or the
per-user keystore defeats it, exactly as it defeats the token (#802
irreducible)." And its §3 threat model puts T3 (authoring a brand-new effect as
another agent) behind "bearer + §6 + `exp`", where the bearer is
`LEASE_PLANE_BEARER_TOKEN` read from `~/.config/cirwel/secrets.env`
(`http_auth.ex:11`) on a service that binds `127.0.0.1`
(`http_router.ex:6`). Both credentials are same-UID-readable files on the same
host.

An attacker with code execution inside the proposer's process, running as that
user, therefore holds everything option (a) requires. **(a) does not defend
against the adversary b3 was worried about either**, so moving the decision
in-process removes no boundary that exists today. b3 fires only against a remote
or cross-UID adversary, and against that adversary both options rely on the same
server-side HMAC verification.

**b3 does not fire. It must not be counted against (b), and this read does not
count it.**

### a1 fires against the enforcement claim — and the codebase says so first

The plane governs what is routed to it. For `file_write`, an agent able to call
the plane can ordinarily write the file directly; routing is voluntary.

The repository states the stronger version of this itself. The fleet's one
standing execute producer, `src/mcp_handlers/dialectic/governed_spawn.py:13-21`:

> the proposer here is a standing dispatcher identity whose continuity token is
> minted in-process by gov-mcp itself. For THIS producer the §6 behavioral veto
> and §7 tier recert are therefore structurally weak — gov-mcp is issuer, prover
> and verifier of the same credential. What the governed path buys is durable
> attribution, idempotent spawn dedup, and the architectural seam (spawns flow
> through `/v1/effects`); it is not, for this producer, an adversarial
> enforcement gain. **Do not cite it as one.**

**a1 fires against (a)'s adversarial-enforcement claim.** It does not touch
(a)'s attribution, exclusion, or recovery claims, which are what §8 below finds
are actually load-bearing.

### a2 does not fire for the first surface

§5b's content-hash reconciliation is decidable for `file_write`: on restart the
surface's hash equals the intended payload (commit forward), the pre-image
(nothing to undo), or neither (a third party wrote — quarantine, do not
clobber). The bytes are the truth rather than the mark, which is the point. It
is *not* decidable for the irreversible classes, and those are already barred
from default-on by the compile-time allowlist in `validate_execute_type_flags!`
(`application.ex:210`). **a2 does not fire.**

### a3 fires: the execute plane is not in the installable artifact

`pyproject.toml:112-114` ships `src`, `governance_core`, and `config`. The
execute plane is Elixir under `elixir/lease_plane/` and is not packaged; the
install playbook does not stand up a BEAM node. The execute-type flags default
off (`application.ex:65-81`).

So option (a) is a capability available where an operator additionally runs an
Erlang/Elixir service and sets two environment variables. That is a real
limitation on reach and it is not a criticism of the design's correctness.
**a3 fires.**

### c1 fires for terminal effects; c2 bars the argument that would otherwise carry (c)

`governed-effect-effect-binding-v0.md` §9 already records the asymmetry: a spawn
is downstream-governed, so a captured-token spawn "yields a *governed* entity
and §6 has something to act on," whereas a `file_write` or commit "is a terminal
effect with no downstream governance — it completes before any behavioral signal
accrues." A record-only shadow over a terminal effect records that it happened
and cannot prevent it. **c1 fires for every terminal class the operator intends
to govern.**

c2 stands as written: no usage count decides (c). Nothing in this read consults
one, and if the surviving case for (c) were that nothing has executed a governed
effect, that case is out of scope rather than weak.

## 8. The failure-mode and authority matrix

Each cell: **who decides · what they see · what happens when that step fails.**

| Axis | (a) custodian plane | (b) in-process intercept | (c) record-only shadow |
|---|---|---|---|
| Identity binding | gov-mcp · HMAC + `exp` + `aid == proposer`, verified server-side (`recertify_strong_tier`) · fail closed, effect vetoed | gov-mcp · identical — the intercept forwards the same token to the same endpoint · identical | gov-mcp · re-verified tier stamped on the shadow row · row marked low-attribution |
| Intent | custodian · the actual payload bytes it will apply · rejected at propose | intercept · the tool call's JSON args · rejection string returned, call never runs | shadow · `sha256` + ≤512-char summary, never the bytes · nothing to stop |
| Decision | gov-mcp, synchronously, before the mutation · `effect_id`, proposer, surface, `payload_sha256` · `governance_blocked` | gov-mcp, synchronously if the guardrail awaits · tool name and args · rejection string | none — recording only · — · — |
| Veto | binding: the custodian, a separate process, performs the commit · — · denial is final for that effect | **advisory against the host process**: the party being denied hosts the denier · — · an agent that removes the guardrail is unrefused | not applicable |
| Actual execution | `EffectCustodian`, not the proposer · re-checks custody validity **in the same transaction as the mutation** · lease lost mid-flight → no commit | the proposer's own runtime, after the intercept returns · nothing further is checked · — | the proposer or an external actor, ungoverned |
| Replacement | not available — the custodian applies the payload the veto hashed | `next_call` may be called with **different args** than those judged (`intercepts.py:128`) · — · judged X, executed Y | not applicable |
| Retry | tombstone on `commit_failed` makes a same-key retry re-execute and be re-judged (§5b) | `next_call` "may be awaited **repeatedly or concurrently**" (`intercepts.py:140`) · one judgement, N executions | not applicable |
| Timeout | custody expires at `min(lease.expires_at)`; propose-time min-TTL floor covers restart + compensation | the guardrail's own timeout, unconstrained by the design; fail-open or fail-closed is the integration's choice | not applicable |
| Audit durability | `audit.events` written by the custodian, a process that outlives the proposer | written by the process whose behaviour is being recorded; dies with it | `audit.events` + `effect_lane`, durable |
| Recovery | boot-ordered `EffectRecovery` scanner drains orphaned pre-images **before** the router accepts requests; re-acquires leases first | none — the intercept dies with the process; no surviving party reconciles | nothing to recover; no mutation was held |

### What the matrix rules in and out

**Split-brain is ruled IN for (b)**, at two named axes, on Relay's own
documented semantics rather than on speculation. Replacement: the execution
intercept may call `next_call` with arguments other than those the record
judged. Retry: `next_call` "may be awaited repeatedly or concurrently," so one
recorded decision can authorise an unbounded number of executions. The
conditional-execution guardrail avoids both — it decides and does not execute —
but it also cannot bind its decision to the call that eventually runs, because a
later request intercept may rewrite the args after the guardrail has passed
them (`guardrails.py:265`: guardrails run *before* request intercepts).

**Split-brain is ruled OUT for (a)** at those axes by construction: the payload
the veto hashed is the payload the custodian applies, and custody validity is
re-confirmed "in the same transaction as the mutation."

**The veto row is the decisive one.** Under (a) a denial is enforced by a
process the denied party does not control. Under (b) the denier is hosted by the
denied. That is not a credential problem, which is why the charter's question
does not quite reach it.

## 9. The answer to the charter's question

> Whether an intercept-based commit path can carry the S7 strong-tier
> re-certification and the per-effect veto without a second credential boundary.

**The S7 re-certification transplants cleanly — and needs no second credential
boundary in either option.** `recertify_strong_tier` verifies an HMAC the server
minted, against `exp`, against `aid == proposer_uuid`, entirely server-side. An
intercept that forwards the proposer's `continuity_token` to `/v1/effect-veto`
gets exactly the same answer the custodian gets. The credential boundary the
question worries about is not doing the work anyone assumed.

**The per-effect veto transplants as a decision but not as a commit.** An
intercept can decide per-effect, synchronously, with the content in hand. What
it cannot do is *hold* the decision: it cannot exclude a competing writer
(Relay 0.8.4 has no lease or cross-process exclusion primitive — verified
against the installed package, and the packet's original claim holds), it cannot
guarantee the judged bytes are the executed bytes, and it cannot survive its own
process to reconcile a surface after a crash.

So the answer is **yes on the credential, no on the custody**, and the useful
finding is that those were never the same question. What the custodian
contributes is not a second credential. It is exclusion, execution-by-a-third-party,
and crash-durable recovery — none of which a credential boundary would have
provided and none of which an in-process intercept can.

## 10. A defect in this read's own pre-registration, stated rather than repaired

The conditions in §3 tested whether each option could be ruled out. Option (b)
passes all three of its conditions: it is not content-blind, not asynchronous by
necessity, and not weakened by an in-process boundary the current design does
not have either.

**But b1–b3 tested only the intercept's ability to decide. They did not test its
ability to hold a decision** — exclusion, execution-by-a-third-party, and
crash-durable recovery are absent from the conditions entirely, and §8 is where
they surfaced. That is a defect in the pre-registration, and this read does not
repair it by adding a fourth condition now. Converting a finding into a
disconfirming criterion after the finding is exactly the move §3 exists to
prevent, and doing it here would be worse than leaving the gap visible.

Recorded consequence: on the standard this read pre-registered, **(b) is not
ruled out.** The three custody properties are reported as findings. Whether they
are disqualifying is a judgement about what the execute plane is for, which
belongs to the operator and not to this document.

## 11. What the read recommends, labelled as a recommendation

The evidence does not sort cleanly into the three option letters, and saying so
is more useful than picking the nearest one.

- The intercept is on the path the agent **actually uses** — its own tool calls
  — while the plane is on a path the agent must choose to enter. On coverage
  the intercept is better positioned, and a1 is why.
- The plane owns three properties the intercept structurally cannot carry, and
  those three are what the repository's own producer note says the governed path
  actually buys today: durable attribution, dedup, and the seam.

They are therefore complementary rather than substitutable, which is a reading
the option letters cannot express. A composition — the intercept as the
synchronous per-call decision point on the agent's real path, the plane retained
for effects that need exclusion or crash-durable custody — is what the evidence
supports. It is **not** one of (a), (b), or (c), and this read has no authority
to add a fourth option to a decision record it was commissioned by.

What the read asks the operator for is therefore narrow: decide whether row 3's
three options are still the right three, given that the evidence supports a
composition none of them names.

## 12. What this read still does not establish

- **No option was tested against a running system.** Everything here is read
  from source and type signatures. A guardrail that awaits a network call is
  supported by the type; nobody has measured what it does to a real agent's tool
  latency, and the shipped integration avoided finding out on purpose.
- **The effect classes the operator intends to govern were never enumerated.**
  a1 and c1 both turn on that list, and this read reasoned from the two classes
  the design documents name rather than from an operator statement.
- **The intercept's own failure modes are untested.** A guardrail that raises,
  hangs, or is removed at runtime has no specified behaviour in this read, and
  `deregister_tool_conditional_execution` exists and is callable from inside the
  agent's process.
- **Nothing is decided.** Row 3 stays open. A gate document under Wave 3's terms
  still owes ratification before anything is built.
