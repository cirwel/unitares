# Orchestrator-Vouched Identity — v0 (design + inert PoC seam)

**Created:** June 17, 2026
**Status:** DESIGN-FIRST RFC, **council-reviewed 2026-06-17** (dialectic-knowledge-architect:
sound-with-changes; feature-dev:code-reviewer: 2 critical + 3 important, all folded;
live-verifier: O4 feasibility **CONFIRMED**, 1 refuted claim corrected). No live
cutover. Wave 3 deferred to the 2026-06-24 gate read. This doc is the gate artifact;
an inert/flag-gated PoC seam may open after this review.
**Author identity:** `04d9ae79-fb64-4cba-b5f3-6b88feb5121a` (fresh process-instance, no lineage).
**Companion to:** `docs/ontology/identity.md` (§"Transport-level continuity",
Appendix "Substrate-Earned Identity"); `docs/proposals/resolved/s19-attestation-mechanism.md`;
`docs/proposals/agent-orchestrator-beam-v0.md`; issues #807, #810, #805 (S19 substrate-gate).
**Writer-locked surface:** identity (docs + `src/mcp_handlers/identity/` + gov-plugin).
Operator is the merge gate. Do NOT auto-merge.

---

## 0. The one-sentence claim

An agent that the BEAM orchestrator actually **spawned and owns** can be moved
from the "non-substrate, unverifiable" identity class into a **runtime-attested**
class — by having the orchestrator (itself S19-enrolled, speaking to governance
over the already-trusted UDS peer-cred channel) **vouch** the child's
`(uuid, os_pid, start_tvsec)` at spawn, so the child's later calls resolve at a
genuine `strong` tier by *proof*, not by echoing a copyable string.

> **Word choice (council 2026-06-17):** the child class is **runtime-attested**,
> *not* "substrate-attested." The child has **no dedicated substrate of its own**
> (§6.3 — the substrate is the orchestrator's); calling the child "substrate-X"
> would smuggle in a class membership it provably lacks (the Appendix's
> three-condition test, which a child fails at condition 1). What is
> substrate-anchored is the **voucher**; the child is *attested by* a
> substrate-anchored runtime, which is exactly S19's own mechanism applied
> dynamically. "Substrate attestation" names the voucher's S19 check; the child
> earns **runtime-attested process-instance identity**, never substrate class.

This is the **first honest `strong` cross-process credential for an ephemeral
agent** — the gap #810 names explicitly as having no answer today.

## 1. Why this exists (the problem #807/#810 left open)

Issues #807/#810 concluded, operator-ratified, now in `identity.md`:

> There is **no honest `strong` cross-process credential for a non-substrate
> agent, by construction.** An ephemeral process can only carry a *copyable
> string* across its own boundary, and a copyable string cannot be cryptographic
> proof. The only honest strong path is **substrate attestation** — a trusted
> runtime witnesses the process and the server asks *it*, not the process's
> self-claim.

Today that path is **S19**: OS kernel + launchd + UDS peer-cred, available to
long-lived launchd residents only. Live state confirms the narrowness — exactly
**one** agent is enrolled in `core.substrate_claims` (Sentinel,
`f92dcea8-…`); every other agent is in the unverifiable class.

#810's ratified framing draws the line:

- **Substrate-anchored agents** (residents): cross-process strong is earned via
  S19 attestation (Option C) — *not* a token.
- **Session-like agents** (Claude Code, Codex, SDK callers): **no honest strong
  cross-process tier.** Their legit writes are same-process; cross-process is
  genuinely unproven and should resolve weak/medium.

This RFC adds a **third row** to that table that #810 did not enumerate, because
the orchestrator did not yet vouch:

| Agent class | Spawned by | Honest cross-process strong path |
|---|---|---|
| Substrate-anchored resident | launchd | **S19** (static substrate_claim, peer-cred) |
| Session-like (Claude Code / Codex / external SDK) | its own harness | **none** — weak/medium, by construction (#810) |
| **Orchestrated ephemeral** (this RFC) | **BEAM orchestrator** | **orchestrator vouch** (dynamic substrate_claim, peer-cred) |

### The insight (from the mission framing)

The *earner* of strong identity isn't any specific runtime — it is **"having a
lifecycle-owner the server can interrogate."** S19's earner is launchd. The BEAM
OTP runtime is a **second instance of that same earner**: when the orchestrator
spawns a child via an OTP `Port`, BEAM witnessed the spawn, knows the child's
`os_pid` (it reads `Port.info(:os_pid)` — see `agent_runner.ex:498`), tracks its
liveness, and **a process cannot forge another process's PID**. So an
*orchestrated* agent has a runtime that can be asked "did you spawn pid X as
agent Y?" — which is precisely what a static launchd substrate_claim answers for
a resident, except asked **dynamically, per-spawn, of a trusted voucher** instead
of of a pre-seeded table row.

> **⚠ OPERATOR CALL — reinterpretation of #810's "by construction."** #810's
> ratified phrasing reads as a universal: "no honest strong cross-process
> credential for a non-substrate agent, *by construction*." This RFC's third row
> rests on reading that phrase as scoped to **self-asserting callers that lack a
> witnessing lifecycle-owner** — not as "no runtime can ever witness an ephemeral
> process." An orchestrated child *has* a witnessing runtime (the earner), so it
> is not a "non-substrate agent" in the relevant sense.
>
> **Adversarial council 2026-06-17 settled the MERITS half of this question:** the
> universal reading is **internally incoherent with the ratified frame**, because
> a launchd S19 resident is *also* an ephemeral process that admits only copyable
> strings from its own mouth — yet earns strong cross-process via a trusted
> third-party (kernel/launchd) attestation, which #810 explicitly **preserves** as
> the one honest strong path. A truly universal "ephemeral ⇒ no strong
> cross-process" would delete S19. So the only frame-coherent scope of "by
> construction" is **"no strong from the process's *own self-assertion*"** — which
> *is* the narrow reading. The narrow reading is therefore **forced by S19's
> survival**, not a free reinterpretation.
>
> **What remained genuinely operator-intent:** only whether the operator, when
> ratifying #810, *meant* the (now-shown-incoherent) universal or the (forced)
> narrow scope. **✅ RATIFIED narrow, operator, 2026-06-17.** The narrow scope —
> "no strong from the process's own self-assertion; substrate/runtime attestation
> by a trusted witness is the honest strong path" — is the committed reading. The
> third row is legitimate under it. (This unblocks the *design* gate only; the
> cutover is still deferred to the 2026-06-24 Wave-3 read.)

## 2. Scope boundary (state plainly)

This helps **only agents BEAM actually spawns and owns** through
`AgentOrchestrator.AgentRunner`. It does **not** help:

- **Claude Code / Codex / external SDK callers** — spawned by their own harness,
  not BEAM. They stay copyable-string callers, weak/medium cross-process, exactly
  as #810 ratified. Out of scope.
- **Routing the governance *request path* through BEAM** — that is the wrong end
  of the wire. Caller identity is established *upstream* of which server answers
  the call; moving the answering server to BEAM changes nothing about who the
  caller is. "BEAM" in this RFC means the **orchestration/lease plane**, NOT a
  governance-MCP rewrite (that is the separate Wave 1–3 handler-dispatch track).

The population this serves is **currently empty** — the orchestrator is inert
(`agent-orchestrator-beam-v0.md`: "NOT merged to any running surface… nothing
spawns through it yet"). That is *why* this is design-first: we define the honest
identity contract **before** anything spawns through the orchestrator, so the
first orchestrated agent is born attested rather than retrofitted.

## 3. The honest seam (state this loudest)

> **An orchestrator-vouch is only as strong as the orchestrator→governance
> channel.** If that channel is a bearer token, we have *relocated* the
> copyable-string problem (N copyable child strings → 1 copyable voucher token),
> not killed it.

Therefore the vouch channel **MUST be UDS peer-cred** (the orchestrator is itself
S19-enrolled and governance peer-creds *it*), **NOT a bearer token.** This is the
load-bearing constraint of the whole design. If implementation cannot put the
orchestrator on a UDS peer-cred channel to governance, the design does not ship —
a bearer-token vouch is explicitly rejected (it would re-introduce the S19/#802
copyable-secret vector at the voucher).

> **THE SINGLE SEAM (adversarial council 2026-06-17, both reviewers converged).**
> The entire merits-case for this design — everything that makes the child's
> `strong` *earned* rather than a better-dressed #807 — rests on **one
> implementation invariant**: the `vouch_child` write MUST be gated by the same
> `verify_substrate_at_resume` peer-cred check that proves the caller is the
> enrolled orchestrator (§4.2 step 3). The live UDS socket is **mode 0666**, so
> *any* local process can connect to it (a pre-existing S19 defect — see O4
> footnote). If `vouch_child` is ever an **ungated** endpoint on that socket, any
> local process calls `vouch_child(attacker_uuid, attacker_pid, attacker_start)`,
> the row is written, and its `strong` is as unchecked as any bearer token — the
> third row collapses straight back into #807. This invariant is **more important
> than the #810 interpretation question**: the #810 reading is forced by S19's
> survival (see §1), but *this* seam can be silently broken in code. A cutover
> acceptance test (§8) must assert a non-orchestrator UDS caller's `vouch_child`
> is refused.

What this design **does** honestly claim:

- **Trust concentration (re-shaped, not net-eliminated).** The number of
  *copyable secrets* at the child end drops to **zero** (the child presents a
  non-secret uuid, proven by kernel peer-cred). What replaces N copyable strings
  is **one anchorable voucher** (kernel-attested, binary/label-checked) **plus two
  named runtime assumptions**: (i) BEAM correctly reports the os_pid of its own
  Port child, and (ii) the `(pid, start_tvsec)` pair holds across the vouch
  window. This is honestly *fewer copyable secrets and a smaller, anchorable
  attack surface* — not *fewer total trust assumptions*. We do not claim net trust
  reduction; we claim the trust is moved onto an anchorable voucher and made
  kernel-attested at both ends.
- **End-to-end PID-attested binding** for the child: governance peer-creds the
  child's *own* PID when it connects over UDS, and matches it against the
  orchestrator-vouched `(uuid, pid, start_tvsec)`. Neither the orchestrator nor
  the child presents a copyable secret; both ends are kernel-attested.

What this design does **NOT** claim:

- Pure end-to-end cryptographic non-forgeability independent of the runtime. The
  guarantee is **conditioned on** (a) the kernel's peer-cred being honest, (b)
  the orchestrator's binary being the enrolled one, and (c) BEAM correctly
  reporting the os_pid of its own Port child. (a) is the project-wide substrate
  assumption (same as S19); (b) is the S19 enrollment check (label + exec-path +
  start_tvsec); (c) is OTP Port semantics. We claim trust *concentration onto an
  anchorable voucher*, not trust *elimination*.

## 4. Mechanism

### 4.1 Topology of the vouch

```
  ┌─────────────────────┐    (1) S19 enroll (operator, once)
  │ core.substrate_claims│◄───  voucher_uuid → label+exec_path of the
  └─────────────────────┘       BEAM node hosting the orchestrator
            ▲
            │ (3) peer-cred verify voucher  ── UDS, kernel-attested PID
  ┌─────────┴───────────┐
  │  governance MCP      │◄══════════════════════╗
  │  (UDS listener)      │   (4) vouch_child(     ║  trusted channel
  │                      │        child_uuid,     ║  (NOT a bearer token)
  │  core.vouched_       │        child_os_pid,   ║
  │  bindings (TTL'd)    │        child_start,    ║
  └─────────┬───────────┘        spawn_reason)   ║
            │                                     ║
            │ (6) peer-cred child PID,    ┌───────╨────────────┐
            │     match vouched binding   │  AgentOrchestrator  │
            ▼                             │  (BEAM, S19-enrolled)│
  ┌─────────────────────┐                └───────┬────────────┘
  │ child resolves at    │                        │ (2) Port.open → os_pid
  │ tier: strong         │   (5) child onboards    │     read start_tvsec
  │ proof_origin:        │◄───  over UDS  ─────────┤
  │ orchestrator_vouched │   (UNITARES_UDS_SOCKET  │  AgentRunner GenServer
  └─────────────────────┘    provisioned by orch)  └─ Port → child OS process
```

### 4.2 Step by step

1. **Voucher enrollment (operator, once).** The BEAM node hosting the
   orchestrator is S19-enrolled: a `core.substrate_claims` row keyed by the
   orchestrator's governance `voucher_uuid`, with `expected_launchd_label` and
   `expected_executable_path` matching its launchd job. **Live posture (verified
   by live-verifier 2026-06-17):** the orchestrator is a **standalone OTP app**
   (`agent_orchestrator/mix.exs`, its own `Application`, its own `start.sh`,
   Bandit control surface on :8789) — it is **NOT** embedded in or a dependency of
   the lease-plane node (lease-plane's mix deps are only postgrex/jason/plug/
   bandit/stream_data). It currently has **no launchd plist installed** and is not
   running. So enrollment targets a **new dedicated `com.unitares.agent-orchestrator`
   launchd job** — a clean S19 enrollment, not a shared one. Two deployment
   prerequisites, neither code: (i) install the orchestrator plist; (ii) enroll the
   voucher UUID. Note `scripts/ops/enroll_resident.py` has no BEAM-node enrollment
   path today — the `expected_executable_path` for a BEAM job is the `beam.smp` /
   release binary, not a Python interpreter; the enrollment tool needs a small
   extension (cutover-row work). No vouch is trusted until the voucher is enrolled.

2. **Spawn (existing code).** `AgentRunner.init/1` opens the Port and reads
   `os_pid` (`agent_runner.ex:498`). The orchestrator additionally reads the
   child's `start_tvsec` (BEAM has no built-in for this; candidate: a one-shot
   `proc_pidinfo` via a tiny port/NIF, or read it from the child's first
   governance call — see §7 open question O3). It already generates/owns the
   child's governance `holder_agent_uuid` (`agent_runner.ex:420`).

3. **Voucher verification (per orchestrator connection).** The orchestrator
   opens a UDS connection to governance. Governance reads the kernel-attested
   peer PID (`uds_listener.py` → `scope["unitares_peer_pid"]`) and runs the
   **existing** `verify_substrate_at_resume` flow against the voucher's
   substrate_claim. If the peer is not the enrolled orchestrator, every vouch on
   that connection is refused. This reuses S19 unchanged — the voucher is just
   another substrate-anchored agent.

   (Note: `verify_substrate_at_resume` is a read-only **check** returning a
   `VerificationResult` — it verifies the *caller is the enrolled orchestrator*
   but does **not** write the vouch. The vouch write in step 4 is entirely new
   code, not a mode of the existing gate — code-reviewer I3.)

4. **Vouch (new code, over the trusted channel).** The orchestrator calls a new
   governance operation `vouch_child(child_uuid, child_os_pid, child_start_tvsec,
   spawn_reason)`. Governance, having peer-cred-verified the *caller* is the
   enrolled orchestrator, writes a **short-TTL** `core.vouched_bindings` row:
   `(child_uuid, child_os_pid, child_start_tvsec, voucher_uuid, expires_at)`. This
   is a **dynamic, ephemeral substrate_claim** — semantically the same as the
   static resident table, but asserted by a trusted runtime per-spawn instead of
   pre-seeded by an operator. `child_start_tvsec` is **required, not optional** in
   this row (step 6 + O3) — it is the anti-PID-reuse anchor; a row without it must
   not confer strong.

5. **Child connects (provisioned).** The orchestrator provisions
   `UNITARES_UDS_SOCKET` into the child env (same explicit-wins provisioning
   mechanism already used for `UNITARES_PARENT_AGENT_ID` / `UNITARES_SERVER_URL`
   in `agent_runner.ex` `candidate_env/2`). The child's SDK routes governance
   calls over UDS (the S19 SDK change, `s19-attestation-mechanism.md §SDK changes`).

6. **Child resolution (new gate).** When the child onboards/calls over UDS,
   governance reads the child's *own* kernel-attested peer PID **and its live
   `start_tvsec`**, looks up `vouched_bindings` by pid, and confirms **all three**:
   the row's `start_tvsec` is present and equals the live `start_tvsec`, the pid
   matches, **and** the child's claimed `child_uuid` matches the row's
   `child_uuid`. **`start_tvsec` is mandatory-for-strong at resolution time** — a
   PID-only match (no verified start time) resolves weak, never strong (the
   start-time pair is the only thing defeating PID-reuse within the TTL window).
   On full match → confer the new strong tier. On mismatch or expiry → fall
   through to existing gating (weak/medium) — the vouch gate is **additive and
   fail-open to the status quo**, never a new denial path for non-orchestrated
   callers.

   **Cache caveat (code-reviewer I2):** S19's `VerifiedPairsCache`
   (`verification.py`) is keyed by `agent_id → (pid, start_tvsec)` and cannot be
   reused as-is — the vouch lookup is **pid → row** (the child's agent_id is not
   known until the row is found), and two ephemeral children cycling through the
   same OS pid within one TTL would collide on an agent_id-keyed cache. The vouched
   resolution path keys on `(pid, start_tvsec)` from the `vouched_bindings` row
   itself, not the S19 cache; the PoC's `verify_vouched_binding` keeps the
   `cache`-shaped signature for parity but the cutover must use a pid-keyed (or
   row-backed) structure.

### 4.3 Why the child PID can't be self-asserted into strong

The child never presents a copyable secret to *earn* strong. It presents its
`child_uuid` (which is not secret — it's just a label). Strong is conferred only
when the **kernel-attested PID of the connecting child** matches a binding that a
**peer-cred-verified orchestrator** vouched. An impostor would need to (a) be
running at the exact `os_pid` the orchestrator vouched, with (b) the matching
`start_tvsec` — i.e., *be* the process BEAM spawned. The copyable thing (the
uuid) is worthless without the PID the kernel attests, and the PID cannot be
forged.

## 5. Tier / proof_origin / label (the honest taxonomy)

**Live taxonomy (verified by live-verifier 2026-06-17 — corrects the v0 draft):**
the only two `proof_origin` values emitted anywhere are `caller_asserted`
(http_api.py:298, session.py:637-641) and `server_inferred` (http_api.py:338,
identity_step.py:437); the default is `None` (context.py:227). `caller_proven`
is computed by **three independent hardcoded equality checks**, not a shared
helper: `identity_payloads.py:601`, `phases.py:133`, and the source-classification
in `session.py` via the `_CALLER_ASSERTED_SOURCES` set (`session.py:619`). The
strict write-gate (`phases.py:300`) refuses **only** `proof_origin == "server_inferred"`;
`None`/`unknown` pass **fail-open**.

**Important correction:** a successful **S19** substrate resume currently stamps
**no** `proof_origin` at all (`handlers.py:760-767` sets `_partc_owned = True` and
logs `[SUBSTRATE_VERIFIED]` but never calls `set_session_proof_origin`). It passes
strict only because its `session_resolution_source` is `agent_uuid_direct[_fastpath]`
(already in `_STRONG_IDENTITY_SOURCES`) **and** `None != "server_inferred"`. So
`substrate_attested` **does not exist today** — it is net-new, not "shared with
S19." A vouched binding must therefore mint a positive proof_origin (so it passes
strict *by proof*, not by the fail-open accident the S19 path currently relies on).

A vouched binding is **not** "caller-asserted" (the child asserted nothing
provable; the *orchestrator* attested). Forcing it under `caller_asserted` would
lie about *who* proved it.

**Proposed (council-leaning — O1 resolved toward distinct):**

- New `proof_origin` value **`orchestrator_vouched`** (distinct, *not* shared with
  a hypothetical S19 `substrate_attested`). Audit-separability is the deciding
  argument: a dynamic runtime-asserted per-spawn vouch and a static
  operator-seeded launchd claim have different trust derivations and failure modes
  (O5: voucher death); collapsing them into one origin loses exactly the audit
  seam this section wants. `caller_proven` becomes
  `proof_origin in {caller_asserted, orchestrator_vouched}` — a small explicit
  widening, *not* a default-true.
- **This is a multi-site change, not one line (code-reviewer C1).** The widening
  must be applied at **all three** `caller_proven` sites (`identity_payloads.py:601`,
  `phases.py:133`) and the new source/origin wired consistently through
  `session.py`'s `_mark` path. Miss one and the strict gate keeps the old
  `== "caller_asserted"` test and refuses vouched writes — defeating the
  mechanism. The cutover should first **extract a shared helper**
  (`is_caller_proven(proof_origin)`) so the set is defined once. `_how_to_strengthen`
  in both files must also learn the new value or it emits a misleading
  "how to reach strong" hint to an already-strong agent.
- New `session_source` **`beam_orchestrated_attestation`** added to
  `_STRONG_IDENTITY_SOURCES` (`identity_payloads.py:11-20`). This is the honest,
  auditable label: "strong because the orchestrator attested over a peer-cred
  channel," distinct from the #807 tokenless-prefix-echo that *looks* strong but
  is unverifiable.
- The label must read true under the `identity.md` taxonomy: it is
  **runtime-attested process-instance identity** (the process-instance layer is
  "automatic — present whenever the process runs," `identity.md:44`), **not**
  substrate-*earned* continuity. "Earned" is the Appendix's reserved term for the
  three-condition cross-restart pattern, which a child cannot meet (its substrate
  is the orchestrator's, not its own — §6.3). The vouch attests **identity at an
  instant**, never continuity.

This is the **opposite** of the #807 debt: #807 is a binding that labels itself
strong without proof; this is a binding that is *actually* proven (peer-cred both
ends) and gets labeled strong honestly. The RFC must not let the label drift back
toward the #807 over-claim — hence the distinct `session_source` so audits can
tell the two apart.

## 6. Interactions

### 6.1 With #425 strict gate

This is a **legitimate way for an orchestrated agent to PASS strict by proof.**
Under strict, `server_inferred` writes are refused (`phases.py:300`). A vouched
child carries `proof_origin = orchestrator_vouched` → `caller_proven = true` →
passes. So when #425 defaults on (stage 4), orchestrated agents do not need a
copyable token to write — they pass by attestation. This is exactly the
"strong-by-proof, not by over-claim" path #807/#810 wanted to exist.

### 6.2 With `substrate_claims` enrollment

**The orchestrator's single enrollment covers all its children.** Children do
**not** each need a static `substrate_claims` row — they cannot (no launchd
label, no dedicated exec path; `claude -p` and a python worker share an
interpreter path with everything else, so a static exec-path check is
meaningless for them). The vouched binding is the *dynamic* analogue: one static
enrollment (the voucher) + N ephemeral vouched rows (the children). This is the
trust-concentration claim made concrete in the schema.

### 6.3 With the Appendix three conditions (`identity.md` "Substrate-Earned Identity")

The Appendix requires **dedicated substrate + sustained behavior + declared
role** for substrate-earned continuity. A child agent satisfies these *only for
the lifetime of its process-instance*, and the substrate is the **orchestrator's**,
not the child's own. So the honest framing is: the vouch confers strong identity
at the **process-instance layer** (the layer `identity.md` calls "the only layer
phenomenologically continuous"), **not** cross-restart substrate-earned
continuity. A child that exits and respawns is a **new** agent with a **new**
vouch — never a resumed one. This keeps us inside axiom #3 ("build nothing that
appears more alive than it is"): the vouch proves *this process is who the
orchestrator says it is, right now*, and claims nothing about continuity beyond
the process. Cross-restart continuity for orchestrated agents remains
unearned-by-design and is explicitly out of scope.

**On "process-instance layer, consumed cross-process" (council seam, resolved).**
The taxonomy sorts layers by *what survives a process boundary*, and
process-instance continuity does **not** survive — so a *cross-process credential*
cannot be a process-instance-*continuity* claim. The vouch is not one. What
crosses the boundary is **not the child's continuity** (that stays inside the
child process and dies with it) but an **external attestation of an instantaneous
fact** — "this PID is the process I spawned, right now" — made by one process
(orchestrator), read by another (governance). That is structurally identical to
how an S19 launchd `substrate_claim` already works: launchd witnesses a resident,
and a *different* process (governance) reads the attestation. Nobody calls S19 "a
cross-process use of within-process continuity"; it is a trusted third party
reporting an observation. The vouch is the same pattern on the process-instance
layer instead of the substrate layer. So the conferred tier is **attested
process-instance identity at an instant**, never "process-instance continuity
that travels."

## 7. Open questions (for council)

- **O1 — RESOLVED (council 2026-06-17) toward distinct `orchestrator_vouched`.**
  `substrate_attested` does not exist today (live-verifier) and the S19 path stamps
  nothing, so there is no value to "share." Audit-separability (distinct trust
  derivation + distinct failure mode under voucher death, O5) decides it: mint a
  distinct `orchestrator_vouched`. See §5. Re-opens only if the operator wants S19
  *also* given a positive stamp and a unified attestation origin.
- **O2 — TTL and re-vouch cadence.** The vouched binding is short-TTL (candidate:
  match the lease TTL, 300s, since the orchestrator already heartbeats the
  child's `agent:/` presence lease). Does the orchestrator re-vouch on a timer, or
  does the binding ride the existing presence-lease lifecycle (release on child
  exit → expire the vouch)? Tying it to the presence lease (already
  self-healing/reaped, `file-lease-leak` project) avoids a second timer.
- **O3 — How does the orchestrator learn the child's `start_tvsec`? RESOLVED:
  option (a) only.** BEAM gives `os_pid` but not start time, so the orchestrator
  needs a tiny `proc_pidinfo` shim (mirror `peer_attestation.read_process_start_time`
  on the BEAM side) and vouches the full `(uuid, pid, start_tvsec)`.
  **Option (b) — server-side TOFU (vouch only `(uuid, pid)`, let governance pin
  start_tvsec on first contact) — is REJECTED**, with the same status as the
  bearer-token channel in §3. TOFU-within-window means the *first* connector at
  that pid defines the binding, which is exactly the same-fingerprint co-resident
  free-ride class (S19/#802 residual); it reintroduces a self-asserted seam at the
  child end and would make the strong label partly performative
  (dialectic-knowledge-architect, seam 2). A vouch without a voucher-supplied
  `start_tvsec` may only ever resolve weak.
- **O4 — Can the orchestrator reach the UDS listener? CONFIRMED VIABLE
  (live-verifier 2026-06-17).** The orchestrator builds against **Erlang/OTP 28 /
  Elixir 1.19.5**, so `:gen_tcp.connect({:local, path}, 0, …)` AF_UNIX (OTP 19+) is
  available. The governance UDS listener is **live**: `UNITARES_UDS_SOCKET =
  /Users/cirwel/.unitares/governance.sock` is set in the gov-mcp plist and the
  socket exists (`srw-rw-rw-`, last bound today). It speaks HTTP framing over
  AF_UNIX (`uds_listener.py`, uvicorn H11). **The remaining cost, not a blocker:**
  `:httpc` cannot do AF_UNIX, and the existing `LeasePlaneClient` (`:httpc` + TCP)
  is **non-portable** to UDS — the vouch client is **net-new code** (raw HTTP/1.1
  over `:gen_tcp` `{:local, …}`, or a `Mint`/`Finch` custom transport;
  code-reviewer I1). The honest seam (§3) is therefore *satisfiable*: a kernel
  peer-cred UDS channel exists end-to-end; the only work is writing the BEAM-side
  HTTP-over-UDS client. (Footnote: the live socket is mode `0666`, not the `0600`
  the listener's `_tighten_socket_mode` intends — same-UID threat boundary still
  holds on this single-user host, but worth reconciling at cutover.)
- **O5 — Voucher liveness ↔ child validity.** If the orchestrator BEAM node
  dies, are outstanding vouched bindings still honored until TTL? Recommend: yes,
  TTL-bounded (the vouch was valid when made; the child is still the process it
  was). But a *re-vouch* requires a live, re-verified orchestrator. This mirrors
  S19's per-server-lifetime cache semantics.

## 8. Sequencing (design → inert PoC → deferred cutover)

1. **This RFC + council review.** (dialectic-knowledge-architect + feature-dev:
   code-reviewer + live-verifier.) Gate: design ratified, O4 feasibility
   confirmed.
2. **Inert PoC seam (flag-gated, no wiring) — SHIPPED IN THIS PR.** All behind
   `UNITARES_ORCHESTRATOR_VOUCH` (default off, inert):
   - `core.vouched_bindings` DDL **authored as a reviewable constant**
     (`vouch.VOUCHED_BINDINGS_DDL`), **not** a `db/postgres/migrations/NNN_*.sql`
     slot. Rationale: an unapplied migration slot shows as drift in the *local*
     `unitares_doctor` `schema_migrations` check until the operator applies it, and
     this PoC is deferred past 2026-06-24 — a real slot would leave the local
     doctor red for weeks. The cutover row promotes the constant verbatim into the
     next real slot and applies it manually (MANUAL-migration discipline).
   - A pure module `src/substrate/vouch.py` mirroring `verification.py`'s shape:
     `VouchedBinding` dataclass + `verify_vouched_binding(binding, *, peer_pid,
     live_start_tvsec, claimed_child_uuid, now_epoch) -> VerificationResult`,
     **with 24 unit tests** (`tests/test_substrate_vouch.py`, all green), and
     **NOT wired into `resolution.py`.** Pure logic, no DB, no async, no
     `set_session_proof_origin`. Signature differs from S19's `cache`-shaped one
     deliberately (code-reviewer I2): lookup is pid→row, anti-reuse anchor is the
     row's `start_tvsec` vs the child's live `start_tvsec`, not an agent_id-keyed
     cache.
   - The `proof_origin`/`session_source` additions left **unwired** (constants
     defined in `vouch.py` only, not referenced by the live tier path) until the
     cutover row.
   - **Inertness invariant (code-reviewer C2 — must hold for the PoC to be safe):**
     do **NOT** add `beam_orchestrated_attestation` to `_STRONG_IDENTITY_SOURCES`
     and do **NOT** add it to `session.py`'s `_CALLER_ASSERTED_SOURCES` (line 619)
     in the PoC — that set is a **live gating point** read at every
     `derive_session_key`. Adding the new source there (even "just to make a test
     pass") without the `vouched_bindings` lookup would let *any* caller who can
     produce that `session_resolution_source` reach `caller_proven` with no vouch
     check. The PoC's tests must construct `VouchedBinding`/`VerificationResult`
     objects directly, never route through the live resolution path. A drift-guard
     test should assert the new source is absent from both live sets while the flag
     is off.
   - Elixir side: **no code** in the PoC. A doc note in `agent_orchestrator`
     pointing at this RFC. The Elixir HTTP-over-UDS vouch client is cutover-row work.
3. **DEFERRED cutover (post-2026-06-24 gate, separate PRs).** UDS vouch client in
   Elixir; `vouch_child` handler; child-resolution gate in `resolution.py`; SDK
   UDS routing for orchestrated children; voucher enrollment. Each single-concern,
   operator-gated, identity-writer-locked. **Mandatory acceptance test (the §3
   single seam):** a UDS caller whose peer-cred does **not** verify as the enrolled
   orchestrator (`verify_substrate_at_resume` → not accepted) must have its
   `vouch_child` **refused** — assert an attacker-chosen `(uuid, pid, start_tvsec)`
   never writes a `vouched_bindings` row. Plus a PID-reuse-window test (O2/O3) and
   a child-resolution test that a `start_tvsec` mismatch resolves weak, never
   strong. **Prerequisite (pre-existing defect to fix first):** reconcile the live
   `governance.sock` to mode 0600 — it is currently 0666 (`_tighten_socket_mode`
   ran per the log but something re-bound it), which widens the connect population
   from same-UID to any local user for S19 *today*, independent of this design.

## 9. Non-goals

- Helping session-like agents (Claude Code / Codex / external SDK) reach strong
  cross-process — explicitly ruled out by #810; unchanged here.
- A bearer-token vouch channel — rejected at §3 (relocates the problem).
- Cross-restart continuity for orchestrated children — out of scope by §6.3.
- Routing governance request-handling through BEAM — wrong end of the wire (§2).
- Any live deploy before the 2026-06-24 Wave-3 gate read.
- Retiring `continuity_token` or changing the #807 tokenless-echo down-rating —
  that is #807/#810's own track; this RFC only *adds* the third honest row.

## 10. Reviewer gate state

- **Design selection: COUNCIL-REVIEWED 2026-06-17, sound-with-changes (all
  folded).** dialectic-knowledge-architect: ontology sound; the "earned" wording,
  O3-TOFU demotion, mandatory-`start_tvsec`, N→1 honesty, and #810 reinterpretation
  surfacing are incorporated. feature-dev:code-reviewer: C1 (multi-site
  `caller_proven`), C2 (`_CALLER_ASSERTED_SOURCES` inertness invariant), I1–I3 all
  incorporated. live-verifier: 5 claims confirmed, 1 refuted (S19 stamps no
  proof_origin → `substrate_attested` is net-new) and corrected in §5.
- **O4 feasibility (HTTP-over-UDS from BEAM): CONFIRMED VIABLE.** OTP 28,
  live `governance.sock`; client is net-new code, not a blocker (§7 O4).
- **Adversarial council 2026-06-17 (second pass, on the blocking decision):** two
  reviewers tasked to *break* the narrow reading.
  - **Merits verdict: the narrow reading SURVIVES** — forced by S19's survival
    (universal reading deletes S19, which #810 preserves). The earned/performative
    line is drawn at *verification*; kernel peer-cred is verification #807's
    fingerprint-pin structurally lacks, so the third row is different in **kind**,
    not merely strength.
  - **Two label wounds folded:** §0 "substrate-attested" → "runtime-attested" (the
    child has no substrate of its own); §6.3 clarified that what crosses processes
    is an *attestation of an instantaneous fact*, not within-process continuity.
  - **The single load-bearing seam elevated (§3 callout):** `vouch_child` MUST be
    peer-cred-gated — this is *more* fragile than the #810 question and gets a
    mandatory cutover acceptance test (§8).
  - **Pre-existing live defect surfaced:** `governance.sock` is mode 0666 not 0600
    (S19 connect population is wider than the listener claims) — flagged for a
    fix-first prerequisite at cutover, independent of this design.
- **✅ Operator call RESOLVED 2026-06-17: narrow #810 scope ratified** (§1 ⚠ box).
  Design gate clear; cutover still deferred to the 2026-06-24 Wave-3 read.
- **Implementation correctness: NOT GATED** — lives in the PoC tests + the
  deferred cutover's adversary suite.
