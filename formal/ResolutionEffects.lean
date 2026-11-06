/-
  formal/ResolutionEffects.lean

  Formal specification and machine-checked invariants for the UNITARES
  dialectic resolution-effect durability state machine and terminal-outcome
  classification protocol:
    - `src/dialectic_db.py`:
      - `DialecticDB.resolve_session` (`prepared` / `enqueued` CTEs, `lines 924-968`)
      - `DialecticDB.get_resolution_effect` (`lines 1065-1090`)
      - `DialecticDB.claim_resolution_effect` (`lines 1092-1166`)
      - `DialecticDB.complete_resolution_effect` (`lines 1168-1186`)
      - `DialecticDB.fail_resolution_effect` (`lines 1188-1216`)
      - `DialecticDB.acknowledge_resolution_effect` (`lines 1218-1255`)
      - `DialecticDB.quarantine_resolution_effect` (`lines 1257-1283`)
      - `DialecticDB.get_outcome_breakdown` (`lines 1806-1904`)
    - `src/dialectic_outcomes.py`:
      - `TERMINAL_STATUSES` (`line 81`)
      - `is_canary_label` (`lines 86-88`)
      - `classify_outcome` (`lines 91-157`)
      - `resolution_rate` (`lines 159-172`)

  Verified by the Lean 4 kernel (`lean formal/ResolutionEffects.lean`) in CI
  (`.github/workflows/tests.yml` `smoke` job) and paired with a Hypothesis
  `RuleBasedStateMachine` refinement test suite in
  `tests/test_resolution_effects_state_machine.py`.

  Zero `sorry` axioms are permitted in this file.
-/

namespace Unitares.ResolutionEffects

abbrev AgentId := Nat
abbrev OwnerToken := Nat
abbrev PauseToken := Nat
abbrev PayloadId := Nat

/-- Lifecycle states of `coordination.dialectic_resolution_effects.state`
    (`src/dialectic_db.py:952, 1115, 1179, 1206, 1245, 1274`). -/
inductive EffectState where
  | pending
  | applying
  | applied
  | unknownPartial
  deriving DecidableEq, Repr, Inhabited

/-- Whether an effect state blocks a `resolved` session from counting as
    `RESOLVED` in `classify_outcome` (`src/dialectic_outcomes.py:144-147`). -/
def EffectState.blocksResolvedOutcome : EffectState → Bool
  | .pending        => true
  | .applying       => true
  | .unknownPartial => true
  | .applied        => false

/-- Representation of `paused_agent_state_json->'pause_effect_required'`
    (`src/dialectic_db.py:926-940, 1137-1141`):
    - `legacyUnbound`: key absent or `paused_agent_state_json IS NULL`
    - `explicitFalse`: `'pause_effect_required': false`
    - `explicitTrue`:  `'pause_effect_required': true` -/
inductive PauseBinding where
  | legacyUnbound
  | explicitFalse
  | explicitTrue
  deriving DecidableEq, Repr, Inhabited

/-- `effect_required` computed by `resolve_session`'s `prepared` CTE
    (`src/dialectic_db.py:926-935`):
    `CASE WHEN COALESCE(paused_agent_state_json ? 'pause_effect_required', FALSE)
          THEN paused_agent_state_json->'pause_effect_required' = 'true'::jsonb
          ELSE FALSE END`. -/
def PauseBinding.enqueueEffectRequired : PauseBinding → Bool
  | .explicitTrue  => true
  | .explicitFalse => false
  | .legacyUnbound => false

/-- `legacy_unbound` computed by `resolve_session`'s `prepared` CTE
    (`src/dialectic_db.py:936-940`). -/
def PauseBinding.isLegacyUnbound : PauseBinding → Bool
  | .legacyUnbound => true
  | .explicitFalse => false
  | .explicitTrue  => false

/-- `COALESCE(s.paused_agent_state_json->'pause_effect_required' = 'true'::jsonb, TRUE)`
    evaluated inside `claim_resolution_effect`'s `EXISTS` subquery against
    `core.dialectic_sessions` (`src/dialectic_db.py:1137-1141`).
    In PostgreSQL, when `'pause_effect_required'` is absent (`legacyUnbound`),
    `paused_agent_state_json->'pause_effect_required'` is SQL `NULL`, so
    `NULL = 'true'::jsonb` is `NULL` and `COALESCE(NULL, TRUE)` yields `TRUE`. -/
def PauseBinding.sessionClaimCoalesce : PauseBinding → Bool
  | .explicitTrue  => true
  | .explicitFalse => false
  | .legacyUnbound => true

/-- Snapshot of the `core.dialectic_sessions` row inspected by `resolve_session`
    (`enqueued` CTE) and `claim_resolution_effect` (`EXISTS` subquery). -/
structure SessionSnapshot where
  statusResolved    : Bool := true
  pausedAgent       : AgentId := 0
  reviewer          : Option AgentId := some 1
  resolutionPayload : PayloadId := 1
  pauseToken        : Option PauseToken := some 10
  pauseBinding      : PauseBinding := .explicitTrue
  deriving DecidableEq, Repr, Inhabited

/-- Durable row in `coordination.dialectic_resolution_effects`
    (`src/dialectic_db.py:1072-1077`). -/
structure ResolutionEffectRow where
  pausedAgent          : AgentId := 0
  reviewer             : Option AgentId := some 1
  effectRequired       : Bool := true
  pauseToken           : Option PauseToken := some 10
  resolutionPayload    : PayloadId := 1
  state                : EffectState := .pending
  ownerToken           : Option OwnerToken := none
  attemptCount         : Nat := 0
  lastError            : Option String := none
  hasExecutionEvidence : Bool := false
  noOpReason           : Option String := none
  manuallyReconciled   : Bool := false
  reconciliationReason : Option String := none
  claimedAt            : Option Nat := none
  appliedAt            : Option Nat := none
  uncertainAt          : Option Nat := none
  updatedAt            : Nat := 100
  deriving DecidableEq, Repr, Inhabited

/-- Enqueue a resolution effect row during `DialecticDB.resolve_session`
    when `status = 'resolved'` (`src/dialectic_db.py:924-968`). -/
def enqueueResolutionEffect (sess : SessionSnapshot) (now : Nat) : ResolutionEffectRow :=
  let effReq := sess.pauseBinding.enqueueEffectRequired
  if effReq then
    { pausedAgent := sess.pausedAgent
    , reviewer := sess.reviewer
    , effectRequired := true
    , pauseToken := sess.pauseToken
    , resolutionPayload := sess.resolutionPayload
    , state := .pending
    , ownerToken := none
    , attemptCount := 0
    , lastError := none
    , hasExecutionEvidence := false
    , noOpReason := none
    , manuallyReconciled := false
    , reconciliationReason := none
    , claimedAt := none
    , appliedAt := none
    , uncertainAt := none
    , updatedAt := now }
  else
    let reason :=
      if sess.pauseBinding.isLegacyUnbound then
        "legacy_unbound_session"
      else
        "active_at_session_open"
    { pausedAgent := sess.pausedAgent
    , reviewer := sess.reviewer
    , effectRequired := false
    , pauseToken := sess.pauseToken
    , resolutionPayload := sess.resolutionPayload
    , state := .applied
    , ownerToken := none
    , attemptCount := 0
    , lastError := none
    , hasExecutionEvidence := true
    , noOpReason := some reason
    , manuallyReconciled := false
    , reconciliationReason := none
    , claimedAt := none
    , appliedAt := some now
    , uncertainAt := none
    , updatedAt := now }

/-- Parameters bound by a caller of `DialecticDB.claim_resolution_effect`
    (`src/dialectic_db.py:1092-1102`). -/
structure ClaimExpectations where
  pausedAgent       : AgentId
  reviewer          : Option AgentId
  effectRequired    : Bool
  pauseToken        : Option PauseToken
  resolutionPayload : PayloadId
  deriving DecidableEq, Repr, Inhabited

/-- Strict SQL `=` comparison on nullable `reviewer_agent_id`
    (`e.reviewer_agent_id = $4` and `s.reviewer_agent_id = $4` in
    `src/dialectic_db.py:1122, 1133`): in PostgreSQL, `NULL = NULL` is `NULL`
    (falsy in `WHERE`), so both sides must be `some` and equal. -/
def strictReviewerEq (lhs rhs : Option AgentId) : Bool :=
  match lhs, rhs with
  | some a, some b => a == b
  | _, _           => false

/-- Combined `WHERE` guard for `DialecticDB.claim_resolution_effect`
    (`src/dialectic_db.py:1119-1142`). -/
def claimGuardOk (eff : ResolutionEffectRow) (sess : SessionSnapshot)
    (exp : ClaimExpectations) : Bool :=
  eff.state == .pending &&
  eff.pausedAgent == exp.pausedAgent &&
  strictReviewerEq eff.reviewer exp.reviewer &&
  eff.resolutionPayload == exp.resolutionPayload &&
  eff.pauseToken == exp.pauseToken &&
  eff.effectRequired == exp.effectRequired &&
  sess.statusResolved &&
  sess.pausedAgent == exp.pausedAgent &&
  strictReviewerEq sess.reviewer exp.reviewer &&
  sess.resolutionPayload == exp.resolutionPayload &&
  sess.pauseToken == exp.pauseToken &&
  sess.pauseBinding.sessionClaimCoalesce == exp.effectRequired

/-- Result of `claimResolutionEffect` (`src/dialectic_db.py:1092-1166`). -/
structure ClaimOutput where
  row     : ResolutionEffectRow
  claimed : Bool
  deriving DecidableEq, Repr, Inhabited

/-- `DialecticDB.claim_resolution_effect` (`src/dialectic_db.py:1092-1166`).
    When `claimGuardOk` holds, claims the row into `.applying` (`claimed = true`);
    otherwise performs the fallback `SELECT e.*, false AS claimed` returning the
    live row unchanged with `claimed = false`. -/
def claimResolutionEffect (eff : ResolutionEffectRow) (sess : SessionSnapshot)
    (owner : OwnerToken) (exp : ClaimExpectations) (now : Nat) : ClaimOutput :=
  if claimGuardOk eff sess exp then
    let nextRow := { eff with
      state := .applying
      ownerToken := some owner
      attemptCount := eff.attemptCount + 1
      claimedAt := some now
      updatedAt := now
      lastError := none
      uncertainAt := none
    }
    { row := nextRow, claimed := true }
  else
    { row := eff, claimed := false }

/-- Output of a guarded effect mutation (`complete`, `fail`, `quarantine`, `acknowledge`). -/
structure EffectStepOutput where
  row     : ResolutionEffectRow
  written : Bool
  deriving DecidableEq, Repr, Inhabited

/-- `DialecticDB.complete_resolution_effect` (`src/dialectic_db.py:1168-1186`).
    Marks the effect `.applied`, gated on `state = 'applying' AND owner_token = $2`. -/
def completeResolutionEffect (eff : ResolutionEffectRow) (owner : OwnerToken)
    (now : Nat) : EffectStepOutput :=
  if eff.state == .applying && eff.ownerToken == some owner then
    let nextRow := { eff with
      state := .applied
      hasExecutionEvidence := true
      appliedAt := some now
      updatedAt := now
      lastError := none
      uncertainAt := none
    }
    { row := nextRow, written := true }
  else
    { row := eff, written := false }

/-- `DialecticDB.fail_resolution_effect` (`src/dialectic_db.py:1188-1216`).
    Records a claimed effect whose final application is uncertain into
    `.unknownPartial`, gated on `state = 'applying' AND owner_token = $2`. -/
def failResolutionEffect (eff : ResolutionEffectRow) (owner : OwnerToken)
    (err : String) (provideExecutionResult : Bool) (now : Nat) : EffectStepOutput :=
  if eff.state == .applying && eff.ownerToken == some owner then
    let nextRow := { eff with
      state := .unknownPartial
      lastError := some err
      hasExecutionEvidence := if provideExecutionResult then true else eff.hasExecutionEvidence
      uncertainAt := some now
      updatedAt := now
    }
    { row := nextRow, written := true }
  else
    { row := eff, written := false }

/-- `DialecticDB.quarantine_resolution_effect` (`src/dialectic_db.py:1257-1283`).
    Fences an abandoned claimant into `.unknownPartial`, gated on
    `state = 'applying' AND owner_token = $2`, preserving `hasExecutionEvidence`. -/
def quarantineResolutionEffect (eff : ResolutionEffectRow) (owner : OwnerToken)
    (reason : String) (now : Nat) : EffectStepOutput :=
  if eff.state == .applying && eff.ownerToken == some owner then
    let nextRow := { eff with
      state := .unknownPartial
      lastError := some reason
      uncertainAt := some now
      updatedAt := now
    }
    { row := nextRow, written := true }
  else
    { row := eff, written := false }

/-- `DialecticDB.acknowledge_resolution_effect` (`src/dialectic_db.py:1218-1255`).
    Closes an `.unknownPartial` effect into `.applied` after manual operator
    reconciliation, gated on `state = 'unknown_partial' AND updated_at IS NOT DISTINCT FROM $3`.
    Retains original `lastError` and `uncertainAt` and merges additive reconciliation metadata. -/
def acknowledgeResolutionEffect (eff : ResolutionEffectRow) (reason : String)
    (expectedUpdatedAt : Option Nat) (now : Nat) : EffectStepOutput :=
  if eff.state == .unknownPartial && expectedUpdatedAt == some eff.updatedAt then
    let nextRow := { eff with
      state := .applied
      hasExecutionEvidence := true
      manuallyReconciled := true
      reconciliationReason := some reason
      appliedAt := some now
      updatedAt := now
    }
    { row := nextRow, written := true }
  else
    { row := eff, written := false }

/-!
  ## Terminal-Outcome Classification & Resolution Rate
  (`src/dialectic_outcomes.py:54-172` and `src/dialectic_db.py:1806-1904`)
-/

/-- Session status domain seen by `classify_outcome` (`src/dialectic_outcomes.py:62-81`). -/
inductive AnalyticsStatus where
  | active
  | resolved
  | failed
  | escalated
  | quorumVoting
  | timeout
  | abandoned
  | missing
  deriving DecidableEq, Repr, Inhabited

/-- Membership in `TERMINAL_STATUSES = frozenset({"resolved", "failed", "timeout", "abandoned"})`
    (`src/dialectic_outcomes.py:81`). Note that `escalated` and `quorumVoting`
    are deliberately non-terminal. -/
def AnalyticsStatus.isTerminal : AnalyticsStatus → Bool
  | .resolved     => true
  | .failed       => true
  | .timeout      => true
  | .abandoned    => true
  | .active       => false
  | .escalated    => false
  | .quorumVoting => false
  | .missing      => false

/-- Six-way partition returned by `classify_outcome` (`src/dialectic_outcomes.py:55-60`). -/
inductive OutcomeClass where
  | resolved
  | resolvedEffectPending
  | unresolvedAwaitingFacilitation
  | failed
  | canary
  | open
  deriving DecidableEq, Repr, Inhabited

/-- `classify_outcome` (`src/dialectic_outcomes.py:91-157`). -/
def classifyOutcome (status : AnalyticsStatus) (awaitingFacilitation : Bool)
    (isCanary : Bool) (standingRejection : Option Bool)
    (effectState : Option EffectState) : OutcomeClass :=
  if isCanary then
    .canary
  else if !status.isTerminal then
    .open
  else if status == .resolved &&
      (match effectState with | some st => st.blocksResolvedOutcome | none => false) then
    .resolvedEffectPending
  else if status == .resolved then
    .resolved
  else if standingRejection == some true then
    .unresolvedAwaitingFacilitation
  else if standingRejection == none && awaitingFacilitation then
    .unresolvedAwaitingFacilitation
  else
    .failed

/-- Aggregated outcome buckets in `DialecticDB.get_outcome_breakdown`
    (`src/dialectic_db.py:1875-1903`). -/
structure OutcomeCounts where
  resolved                       : Nat := 0
  resolvedEffectPending          : Nat := 0
  unresolvedAwaitingFacilitation : Nat := 0
  failed                         : Nat := 0
  canary                         : Nat := 0
  openCount                      : Nat := 0
  deriving DecidableEq, Repr, Inhabited

/-- Increment the bucket corresponding to `o` (`src/dialectic_db.py:1891`). -/
def recordOutcome (c : OutcomeCounts) (o : OutcomeClass) : OutcomeCounts :=
  match o with
  | .resolved                       => { c with resolved := c.resolved + 1 }
  | .resolvedEffectPending          => { c with resolvedEffectPending := c.resolvedEffectPending + 1 }
  | .unresolvedAwaitingFacilitation => { c with unresolvedAwaitingFacilitation := c.unresolvedAwaitingFacilitation + 1 }
  | .failed                         => { c with failed := c.failed + 1 }
  | .canary                         => { c with canary := c.canary + 1 }
  | .open                           => { c with openCount := c.openCount + 1 }

/-- Denominator `resolved + failed` used by `resolution_rate` and
    `get_outcome_breakdown` (`src/dialectic_outcomes.py:169`, `src/dialectic_db.py:1894`). -/
def rateDenominator (c : OutcomeCounts) : Nat :=
  c.resolved + c.failed

/-!
  ## Machine-Checked Protocol Invariants (Theorems 1–11)
-/

/-- **Theorem 1 (Non-Required Effect Enqueues Directly As `.applied`):**
    When `sess.pauseBinding` is `.legacyUnbound` or `.explicitFalse`
    (`enqueueEffectRequired = false`), `enqueueResolutionEffect sess now`
    creates the row directly in `.applied` with `hasExecutionEvidence = true`
    and `appliedAt = some now` (`src/dialectic_db.py:952-964`). -/
theorem enqueue_non_required_immediately_applied (sess : SessionSnapshot) (now : Nat)
    (hNotReq : sess.pauseBinding.enqueueEffectRequired = false) :
    let eff := enqueueResolutionEffect sess now
    eff.state = .applied ∧
    eff.effectRequired = false ∧
    eff.hasExecutionEvidence = true ∧
    eff.appliedAt = some now := by
  dsimp only
  unfold enqueueResolutionEffect
  rw [hNotReq]
  simp

/-- **Theorem 2 (Claim Requires `.pending` State):**
    Whenever `eff.state ≠ .pending`, `claimResolutionEffect eff sess owner exp now`
    returns `claimed = false` and leaves `eff` unchanged
    (`src/dialectic_db.py:1120, 1146-1155`). -/
theorem claim_requires_pending_state (eff : ResolutionEffectRow) (sess : SessionSnapshot)
    (owner : OwnerToken) (exp : ClaimExpectations) (now : Nat)
    (hNotPending : (eff.state == .pending) = false) :
    (claimResolutionEffect eff sess owner exp now).claimed = false ∧
    (claimResolutionEffect eff sess owner exp now).row = eff := by
  have hGuard : claimGuardOk eff sess exp = false := by
    unfold claimGuardOk
    rw [hNotPending]
    simp
  unfold claimResolutionEffect
  rw [hGuard]
  simp

/-- **Theorem 3 (Single-Claimant Mutual Exclusion):**
    If claimant `owner1` claims `eff` (`out1.claimed = true`), then any
    concurrent or subsequent `claimResolutionEffect` by `owner2` against
    `out1.row` returns `claimed = false`, keeps `state = .applying`, and
    preserves `ownerToken = some owner1` and `attemptCount = eff.attemptCount + 1`
    (`src/dialectic_db.py:1114-1155`). -/
theorem single_claimant_mutual_exclusion (eff : ResolutionEffectRow)
    (sess1 sess2 : SessionSnapshot) (owner1 owner2 : OwnerToken)
    (exp1 exp2 : ClaimExpectations) (now1 now2 : Nat)
    (hClaim1 : (claimResolutionEffect eff sess1 owner1 exp1 now1).claimed = true) :
    let out1 := claimResolutionEffect eff sess1 owner1 exp1 now1
    let out2 := claimResolutionEffect out1.row sess2 owner2 exp2 now2
    out2.claimed = false ∧
    out2.row = out1.row ∧
    out2.row.state = .applying ∧
    out2.row.ownerToken = some owner1 ∧
    out2.row.attemptCount = eff.attemptCount + 1 := by
  dsimp only
  unfold claimResolutionEffect at hClaim1 ⊢
  cases hG1 : claimGuardOk eff sess1 exp1
  · rw [hG1] at hClaim1
    contradiction
  · simp only [↓reduceIte]
    have hG2 : claimGuardOk
        { eff with
          state := .applying,
          ownerToken := some owner1,
          attemptCount := eff.attemptCount + 1,
          claimedAt := some now1,
          updatedAt := now1,
          lastError := none,
          uncertainAt := none } sess2 exp2 = false := by
      unfold claimGuardOk
      simp
    rw [hG2]
    simp

/-- **Theorem 4 (Owner-Token Fencing Protects `.applying` Claims):**
    When `eff.ownerToken ≠ some callerOwner`, `completeResolutionEffect`,
    `failResolutionEffect`, and `quarantineResolutionEffect` all return
    `written = false` and leave `eff` unchanged
    (`src/dialectic_db.py:1183, 1212, 1280`). -/
theorem owner_token_fencing_protects_applying_row (eff : ResolutionEffectRow)
    (callerOwner : OwnerToken) (err reason : String) (provideRes : Bool) (now : Nat)
    (hMismatch : (eff.ownerToken == some callerOwner) = false) :
    (completeResolutionEffect eff callerOwner now).written = false ∧
    (completeResolutionEffect eff callerOwner now).row = eff ∧
    (failResolutionEffect eff callerOwner err provideRes now).written = false ∧
    (failResolutionEffect eff callerOwner err provideRes now).row = eff ∧
    (quarantineResolutionEffect eff callerOwner reason now).written = false ∧
    (quarantineResolutionEffect eff callerOwner reason now).row = eff := by
  unfold completeResolutionEffect failResolutionEffect quarantineResolutionEffect
  rw [hMismatch]
  simp

/-- **Theorem 5 (`unknown_partial` Blocks Automatic Replay & Worker Mutations):**
    When `eff.state = .unknownPartial`, `claimResolutionEffect`,
    `completeResolutionEffect`, `failResolutionEffect`, and
    `quarantineResolutionEffect` all refuse (`claimed = false` / `written = false`)
    and leave `eff` unchanged (`src/dialectic_db.py:1120, 1183, 1198-1201, 1212, 1280`). -/
theorem unknown_partial_blocks_claim_and_worker_writes (eff : ResolutionEffectRow)
    (sess : SessionSnapshot) (owner : OwnerToken) (exp : ClaimExpectations)
    (err reason : String) (provideRes : Bool) (now : Nat)
    (hPartial : eff.state = .unknownPartial) :
    (claimResolutionEffect eff sess owner exp now).claimed = false ∧
    (claimResolutionEffect eff sess owner exp now).row = eff ∧
    (completeResolutionEffect eff owner now).written = false ∧
    (completeResolutionEffect eff owner now).row = eff ∧
    (failResolutionEffect eff owner err provideRes now).written = false ∧
    (failResolutionEffect eff owner err provideRes now).row = eff ∧
    (quarantineResolutionEffect eff owner reason now).written = false ∧
    (quarantineResolutionEffect eff owner reason now).row = eff := by
  have hNotPend : (eff.state == .pending) = false := by
    rw [hPartial]; rfl
  have hClaim := claim_requires_pending_state eff sess owner exp now hNotPend
  unfold completeResolutionEffect failResolutionEffect quarantineResolutionEffect
  rw [hPartial]
  simp [hClaim.1, hClaim.2]

/-- **Theorem 6 (Operator Acknowledgement Requires `.unknownPartial` & Exact `updatedAt` CAS, Preserving Evidence):**
    `acknowledgeResolutionEffect eff reason expUpd now` writes (`written = true`)
    iff `eff.state = .unknownPartial` and `expUpd = some eff.updatedAt`; when it
    writes, it transitions to `.applied`, sets `manuallyReconciled = true`, and
    preserves `lastError`, `uncertainAt`, and `ownerToken`
    (`src/dialectic_db.py:1218-1255`). -/
theorem acknowledge_requires_unknown_partial_and_exact_updated_at
    (eff : ResolutionEffectRow) (reason : String) (expUpd : Option Nat) (now : Nat) :
    ((acknowledgeResolutionEffect eff reason expUpd now).written = true ↔
      (eff.state == .unknownPartial && expUpd == some eff.updatedAt) = true) ∧
    ((eff.state == .unknownPartial && expUpd == some eff.updatedAt) = true →
      let out := acknowledgeResolutionEffect eff reason expUpd now
      out.row.state = .applied ∧
      out.row.manuallyReconciled = true ∧
      out.row.reconciliationReason = some reason ∧
      out.row.lastError = eff.lastError ∧
      out.row.uncertainAt = eff.uncertainAt ∧
      out.row.ownerToken = eff.ownerToken) := by
  constructor
  · unfold acknowledgeResolutionEffect
    cases hG : (eff.state == .unknownPartial && expUpd == some eff.updatedAt) <;> simp
  · intro hG
    dsimp only
    unfold acknowledgeResolutionEffect
    rw [hG]
    simp

/-- **Theorem 7 (`.applied` Is Terminal / Absorbing Across All Effect Operations):**
    Once `eff.state = .applied`, `claimResolutionEffect`, `completeResolutionEffect`,
    `failResolutionEffect`, `quarantineResolutionEffect`, and
    `acknowledgeResolutionEffect` all refuse to mutate the row
    (`src/dialectic_db.py:1120, 1183, 1212, 1251, 1279`). -/
theorem applied_is_absorbing (eff : ResolutionEffectRow) (sess : SessionSnapshot)
    (owner : OwnerToken) (exp : ClaimExpectations) (err reason : String)
    (provideRes : Bool) (expUpd : Option Nat) (now : Nat)
    (hApplied : eff.state = .applied) :
    (claimResolutionEffect eff sess owner exp now).claimed = false ∧
    (claimResolutionEffect eff sess owner exp now).row = eff ∧
    (completeResolutionEffect eff owner now).written = false ∧
    (completeResolutionEffect eff owner now).row = eff ∧
    (failResolutionEffect eff owner err provideRes now).written = false ∧
    (failResolutionEffect eff owner err provideRes now).row = eff ∧
    (quarantineResolutionEffect eff owner reason now).written = false ∧
    (quarantineResolutionEffect eff owner reason now).row = eff ∧
    (acknowledgeResolutionEffect eff reason expUpd now).written = false ∧
    (acknowledgeResolutionEffect eff reason expUpd now).row = eff := by
  have hNotPend : (eff.state == .pending) = false := by
    rw [hApplied]; rfl
  have hClaim := claim_requires_pending_state eff sess owner exp now hNotPend
  unfold completeResolutionEffect failResolutionEffect quarantineResolutionEffect acknowledgeResolutionEffect
  rw [hApplied]
  simp [hClaim.1, hClaim.2]

/-- **Theorem 8 (Canary Traffic Always Classifies As `.canary`):**
    Whenever `isCanary = true`, `classifyOutcome` returns `.canary` regardless
    of `status`, `awaitingFacilitation`, `standingRejection`, or `effectState`
    (`src/dialectic_outcomes.py:100-102, 140-141`). -/
theorem canary_always_classified_canary (status : AnalyticsStatus)
    (fac : Bool) (standing : Option Bool) (effState : Option EffectState) :
    classifyOutcome status fac true standing effState = .canary := by
  rfl

/-- **Theorem 9 (Unapplied Resolution Effect Withholds `.resolved` Classification):**
    For any non-canary session with `status = .resolved` and an effect in
    `.pending`, `.applying`, or `.unknownPartial` (`st.blocksResolvedOutcome = true`),
    `classifyOutcome .resolved fac false standing (some st)` returns
    `.resolvedEffectPending` (and never `.resolved`)
    (`src/dialectic_outcomes.py:144-147`). -/
theorem unapplied_effect_withholds_resolved_classification (fac : Bool)
    (standing : Option Bool) (st : EffectState)
    (hPending : st.blocksResolvedOutcome = true) :
    classifyOutcome .resolved fac false standing (some st) = .resolvedEffectPending := by
  unfold classifyOutcome
  simp [AnalyticsStatus.isTerminal, hPending]

/-- **Theorem 10 (Three-Valued `standing_rejection` Precedence Over `awaiting_facilitation`):**
    For any non-canary failed session (`status = .failed`):
    1. When `standingRejection = some true` (even if `awaitingFacilitation = false`,
       as on sessions closed via `rejection_stands`), `classifyOutcome` returns
       `.unresolvedAwaitingFacilitation`.
    2. When `standingRejection = some false` (even if `awaitingFacilitation = true`),
       `classifyOutcome` overrides the fallback flag and returns `.failed`
    (`src/dialectic_outcomes.py:133-156`, `src/dialectic_db.py:1849-1858`). -/
theorem standing_rejection_three_valued_precedence
    (fac : Bool) (effState : Option EffectState) :
    classifyOutcome .failed fac false (some true) effState = .unresolvedAwaitingFacilitation ∧
    classifyOutcome .failed fac false (some false) effState = .failed := by
  constructor <;> rfl

/-- **Theorem 11 (Non-Quality Outcomes Preserve `resolution_rate` Numerator & Denominator):**
    Recording `.canary`, `.open`, `.resolvedEffectPending`, or
    `.unresolvedAwaitingFacilitation` into `OutcomeCounts` leaves `c.resolved`,
    `c.failed`, and `rateDenominator c` (`resolved + failed`) unchanged
    (`src/dialectic_outcomes.py:159-172`). -/
theorem non_quality_outcomes_preserve_resolution_rate_denominator
    (c : OutcomeCounts) (o : OutcomeClass)
    (hNonQuality : o = .canary ∨ o = .open ∨ o = .resolvedEffectPending ∨ o = .unresolvedAwaitingFacilitation) :
    (recordOutcome c o).resolved = c.resolved ∧
    (recordOutcome c o).failed = c.failed ∧
    rateDenominator (recordOutcome c o) = rateDenominator c := by
  rcases hNonQuality with rfl | rfl | rfl | rfl <;>
  unfold recordOutcome rateDenominator <;>
  simp

/-!
  ## Compile-Time `#guard` Conformance Vectors
  Parsed by `tests/test_resolution_effects_state_machine.py` to cross-check
  these `#guard` trace vectors against `SpecResolutionEffect` and the live
  Python `DialecticDB` / `classify_outcome` / `resolution_rate` implementation.
-/

def mkInitEffectSession (pauseBinding : PauseBinding) : SessionSnapshot :=
  { statusResolved := true
  , pausedAgent := 0
  , reviewer := some 1
  , resolutionPayload := 1
  , pauseToken := some 10
  , pauseBinding := pauseBinding }

def defaultClaimExp : ClaimExpectations :=
  { pausedAgent := 0
  , reviewer := some 1
  , effectRequired := true
  , pauseToken := some 10
  , resolutionPayload := 1 }

-- Scenario 1: Pause-bound resolved session enqueues `.pending`; claimant 101
-- claims it (`attemptCount = 1`, `state = .applying`); concurrent claimant 202
-- loses (`claimed = false`, `ownerToken = some 101`); fenced claimant 202 cannot
-- complete it (`written = false`); claimant 101 completes it (`state = .applied`).
def effectScenario1CompetingClaim : ClaimOutput :=
  let sess := mkInitEffectSession .explicitTrue
  let e0 := enqueueResolutionEffect sess 100
  let c1 := claimResolutionEffect e0 sess 101 defaultClaimExp 110
  claimResolutionEffect c1.row sess 202 defaultClaimExp 115

def effectScenario1FencedComplete : EffectStepOutput :=
  completeResolutionEffect effectScenario1CompetingClaim.row 202 120

def effectScenario1 : ResolutionEffectRow :=
  let sess := mkInitEffectSession .explicitTrue
  let e0 := enqueueResolutionEffect sess 100
  let c1 := claimResolutionEffect e0 sess 101 defaultClaimExp 110
  let c2 := claimResolutionEffect c1.row sess 202 defaultClaimExp 115
  let sWrong := completeResolutionEffect c2.row 202 120
  let sOk := completeResolutionEffect sWrong.row 101 130
  sOk.row

#guard effectScenario1CompetingClaim.claimed == false
#guard effectScenario1CompetingClaim.row.state == .applying
#guard effectScenario1CompetingClaim.row.ownerToken == some 101
#guard effectScenario1CompetingClaim.row.attemptCount == 1
#guard effectScenario1FencedComplete.written == false
#guard effectScenario1.state == .applied
#guard effectScenario1.hasExecutionEvidence == true
#guard effectScenario1.appliedAt == some 130
#guard effectScenario1.updatedAt == 130

-- Scenario 2: Claimant 101 claims a `.pending` effect, then fails into
-- `.unknownPartial` (`uncertainAt = some 120`, `lastError = some "rpc_timeout"`);
-- a retry claim by 202 is refused (`claimed = false`); an operator acknowledge
-- with a stale `expectedUpdatedAt = some 110` is refused (`written = false`);
-- an operator acknowledge with `expectedUpdatedAt = some 120` transitions to
-- `.applied` while preserving `lastError` and `uncertainAt`.
def effectScenario2BlockedRetry : ClaimOutput :=
  let sess := mkInitEffectSession .explicitTrue
  let e0 := enqueueResolutionEffect sess 100
  let c1 := claimResolutionEffect e0 sess 101 defaultClaimExp 110
  let f1 := failResolutionEffect c1.row 101 "rpc_timeout" true 120
  claimResolutionEffect f1.row sess 202 defaultClaimExp 125

def effectScenario2StaleAck : EffectStepOutput :=
  acknowledgeResolutionEffect effectScenario2BlockedRetry.row "verified_resumed" (some 110) 130

def effectScenario2 : ResolutionEffectRow :=
  let sess := mkInitEffectSession .explicitTrue
  let e0 := enqueueResolutionEffect sess 100
  let c1 := claimResolutionEffect e0 sess 101 defaultClaimExp 110
  let f1 := failResolutionEffect c1.row 101 "rpc_timeout" true 120
  let cRetry := claimResolutionEffect f1.row sess 202 defaultClaimExp 125
  let ackStale := acknowledgeResolutionEffect cRetry.row "verified_resumed" (some 110) 130
  let ackOk := acknowledgeResolutionEffect ackStale.row "verified_resumed" (some 120) 140
  ackOk.row

#guard effectScenario2BlockedRetry.claimed == false
#guard effectScenario2BlockedRetry.row.state == .unknownPartial
#guard effectScenario2StaleAck.written == false
#guard effectScenario2.state == .applied
#guard effectScenario2.manuallyReconciled == true
#guard effectScenario2.reconciliationReason == some "verified_resumed"
#guard effectScenario2.lastError == some "rpc_timeout"
#guard effectScenario2.uncertainAt == some 120
#guard effectScenario2.appliedAt == some 140
#guard effectScenario2.updatedAt == 140

-- Scenario 3: Claimant 101 claims a `.pending` effect and stalls; operator
-- quarantines claimant 101 with `ownerToken = 101` (`state = .unknownPartial`);
-- stalled claimant 101 later wakes up and attempts `completeResolutionEffect`,
-- which is refused (`written = false`), leaving the row `.unknownPartial`.
def effectScenario3LateComplete : EffectStepOutput :=
  let sess := mkInitEffectSession .explicitTrue
  let e0 := enqueueResolutionEffect sess 100
  let c1 := claimResolutionEffect e0 sess 101 defaultClaimExp 110
  let q1 := quarantineResolutionEffect c1.row 101 "worker_stalled" 120
  completeResolutionEffect q1.row 101 130

def effectScenario3 : ResolutionEffectRow :=
  let sess := mkInitEffectSession .explicitTrue
  let e0 := enqueueResolutionEffect sess 100
  let c1 := claimResolutionEffect e0 sess 101 defaultClaimExp 110
  let q1 := quarantineResolutionEffect c1.row 101 "worker_stalled" 120
  let lateOk := completeResolutionEffect q1.row 101 130
  lateOk.row

#guard effectScenario3LateComplete.written == false
#guard effectScenario3.state == .unknownPartial
#guard effectScenario3.lastError == some "worker_stalled"
#guard effectScenario3.uncertainAt == some 120
#guard effectScenario3.updatedAt == 120

-- Scenario 4: Legacy unbound session (`pauseBinding = .legacyUnbound`) enqueues
-- directly as `.applied` (`effectRequired = false`, `noOpReason = some "legacy_unbound_session"`).
def effectScenario4 : ResolutionEffectRow :=
  let sess := mkInitEffectSession .legacyUnbound
  enqueueResolutionEffect sess 100

#guard effectScenario4.state == .applied
#guard effectScenario4.effectRequired == false
#guard effectScenario4.hasExecutionEvidence == true
#guard effectScenario4.noOpReason == some "legacy_unbound_session"
#guard effectScenario4.appliedAt == some 100

-- Scenario 5: Outcome classification & denominator invariance across all 6 buckets.
def outcomeScenario5 : OutcomeCounts :=
  let c0 : OutcomeCounts := {}
  let c1 := recordOutcome c0 (classifyOutcome .failed false true (some true) none)
  let c2 := recordOutcome c1 (classifyOutcome .escalated true false none none)
  let c3 := recordOutcome c2 (classifyOutcome .resolved false false none (some .unknownPartial))
  let c4 := recordOutcome c3 (classifyOutcome .resolved false false none (some .applied))
  let c5 := recordOutcome c4 (classifyOutcome .failed false false (some true) none)
  let c6 := recordOutcome c5 (classifyOutcome .failed true false (some false) none)
  c6

#guard outcomeScenario5.canary == 1
#guard outcomeScenario5.openCount == 1
#guard outcomeScenario5.resolvedEffectPending == 1
#guard outcomeScenario5.resolved == 1
#guard outcomeScenario5.unresolvedAwaitingFacilitation == 1
#guard outcomeScenario5.failed == 1
#guard rateDenominator outcomeScenario5 == 2

end Unitares.ResolutionEffects
