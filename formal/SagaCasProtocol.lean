/-
  formal/SagaCasProtocol.lean

  Formal specification and machine-checked invariants for the UNITARES
  dual-writer Compare-And-Set (CAS), resolution-saga exclusion, time-correlated
  overlap probe, and sweeper cycle-accounting protocol:
    - `src/dialectic_db.py`:
      - `DialecticDB.resolve_session` (`lines 772-1063`)
      - `resolve_session_async` (`lines 2195-2253`)
      - `DialecticDB.add_message` (`lines 1399-1587`)
      - `DialecticDB.has_inflight_saga` / `probe_inflight_saga` (`lines 1285-1336`)
      - `DialecticDB.probe_saga_since` (`lines 1338-1384`)
    - `src/mcp_handlers/dialectic/auto_resolve.py`:
      - `_CycleCounts` (`lines 66-118`)
      - `_probe_write_overlap` (`lines 228-286`)
      - `_attempt_guarded_write` (`lines 289-432`)

  Verified by the Lean 4 kernel (`lean formal/SagaCasProtocol.lean`) in CI
  (`.github/workflows/tests.yml` `smoke` job) and paired with a Hypothesis
  `RuleBasedStateMachine` refinement test suite in
  `tests/test_saga_cas_protocol_state_machine.py`.

  Zero `sorry` axioms are permitted in this file.
-/

namespace Unitares.SagaCasProtocol

abbrev AgentId := Nat

/-- Durable session row status (`core.dialectic_sessions.status`). -/
inductive SessionStatus where
  | active
  | resolved
  | failed
  | escalated
  deriving DecidableEq, Repr, Inhabited

/-- Protocol phase (`core.dialectic_sessions.phase`). -/
inductive Phase where
  | thesis
  | antithesis
  | synthesis
  | resolved
  | failed
  | escalated
  deriving DecidableEq, Repr, Inhabited

def SessionStatus.isTerminal : SessionStatus → Bool
  | .resolved => true
  | .failed   => true
  | _         => false

def SessionStatus.toPhase : SessionStatus → Phase
  | .resolved  => .resolved
  | .failed    => .failed
  | .escalated => .escalated
  | .active    => .synthesis

/-- Resolution saga states (`coordination.session_resolution_sagas.state`). -/
inductive SagaState where
  | reserved
  | pausedAgentApplied
  | bothAgentsApplied
  | reverting
  | pgCommitted
  | reverted
  deriving DecidableEq, Repr, Inhabited

/-- Non-terminal saga states (`src/dialectic_db.py:1327-1328`). -/
def SagaState.isInflight : SagaState → Bool
  | .reserved           => true
  | .pausedAgentApplied => true
  | .bothAgentsApplied  => true
  | .reverting          => true
  | .pgCommitted        => false
  | .reverted           => false

/-- One row in `coordination.session_resolution_sagas`. -/
structure SagaRecord where
  sagaId    : Nat
  state     : SagaState
  createdAt : Nat
  deriving DecidableEq, Repr, Inhabited

/-- True if any saga on the session is in a non-terminal state
    (`DialecticDB.has_inflight_saga`, `src/dialectic_db.py:1285-1336`). -/
def hasInflightSaga (sagas : List SagaRecord) : Bool :=
  sagas.any (fun sg => sg.state.isInflight)

/-- Three-state return contract of `DialecticDB.probe_saga_since`
    (`src/dialectic_db.py:1338-1384`):
    - `queryFailed` corresponds to `None` (DB error / could not look)
    - `empty` corresponds to `{}` (query ran and found no matching saga)
    - `found sg` corresponds to a non-empty dict row -/
inductive ProbeQueryResult where
  | queryFailed
  | empty
  | found (sg : SagaRecord)
  deriving DecidableEq, Repr, Inhabited

/-- Predicate matching a saga in `DialecticDB.probe_saga_since`
    (`src/dialectic_db.py:1369-1373`):
    in-flight state OR (`since` is `some t` and `created_at >= t`). -/
def sagaMatchesProbe (since : Option Nat) (sg : SagaRecord) : Bool :=
  sg.state.isInflight ||
    match since with
    | some t => decide (sg.createdAt ≥ t)
    | none   => false

/-- `DialecticDB.probe_saga_since` (`src/dialectic_db.py:1338-1384`). -/
def probeSagaSince (sagas : List SagaRecord) (since : Option Nat) (probeHealthy : Bool) :
    ProbeQueryResult :=
  if !probeHealthy then
    .queryFailed
  else
    match sagas.filter (sagaMatchesProbe since) with
    | []      => .empty
    | sg :: _ => .found sg

/-- Three-state classification returned by `_probe_write_overlap`
    (`src/mcp_handlers/dialectic/auto_resolve.py:228-286`). -/
inductive OverlapOutcome where
  | clean
  | detected
  | probeFailed
  deriving DecidableEq, Repr, Inhabited

/-- `_probe_write_overlap` (`src/mcp_handlers/dialectic/auto_resolve.py:228-286`). -/
def probeWriteOverlap (q : ProbeQueryResult) : OverlapOutcome :=
  match q with
  | .queryFailed => .probeFailed
  | .empty       => .clean
  | .found _     => .detected

/-- Durable session row (`core.dialectic_sessions`) plus attached sagas. -/
structure SessionRow where
  status                 : SessionStatus := .active
  phase                  : Phase := .synthesis
  pausedAgent            : AgentId := 0
  reviewer               : Option AgentId := some 1
  transcriptRevision     : Nat := 0
  resolutionSagaRevision : Nat := 0
  awaitingFacilitation   : Bool := false
  updatedAt              : Nat := 0
  pauseEffectRequired    : Bool := true
  declaredAuthors        : List AgentId := []
  authorshipValid        : Bool := true
  sagas                  : List SagaRecord := []
  deriving DecidableEq, Repr, Inhabited

/-- Compare-And-Set expectations passed to `resolve_session` or `add_message`
    (`src/dialectic_db.py:778-785, 1412-1415`). -/
structure CasExpectations where
  expectedPhase                  : Option Phase := none
  expectedUpdatedAt              : Option Nat := none
  expectedReviewer               : Option (Option AgentId) := none
  expectedTranscriptRevision     : Option Nat := none
  expectedAwaitingFacilitation   : Option Bool := none
  expectedResolutionSagaRevision : Option Nat := none
  excludeInflightSaga            : Bool := false
  clearFacilitation              : Bool := false
  deriving DecidableEq, Repr, Inhabited

/-- Evaluate whether all supplied CAS predicates match `s`
    (`src/dialectic_db.py:831-851`). -/
def casMatches (s : SessionRow) (cas : CasExpectations) : Bool :=
  let phaseOk := match cas.expectedPhase with
    | none   => true
    | some p => s.phase == p
  let updatedOk := match cas.expectedUpdatedAt with
    | none   => true
    | some u => s.updatedAt == u
  let reviewerOk := match cas.expectedReviewer with
    | none   => true
    | some r => s.reviewer == r
  let revOk := match cas.expectedTranscriptRevision with
    | none   => true
    | some r => s.transcriptRevision == r
  let facOk := match cas.expectedAwaitingFacilitation with
    | none   => true
    | some f => s.awaitingFacilitation == f
  let sagaRevOk := match cas.expectedResolutionSagaRevision with
    | none   => true
    | some r => s.resolutionSagaRevision == r
  phaseOk && updatedOk && reviewerOk && revOk && facOk && sagaRevOk

/-- Evaluate the `status == 'resolved'` reviewer authority SQL guard in
    `DialecticDB.resolve_session` (`src/dialectic_db.py:862-911`).
    In PostgreSQL, when `declaredAuthors` is non-empty (`paused_agent_state_json`
    contains `_dialectic_reviewed_work_authorship`) and `reviewer` is `none` (`NULL`),
    `reviewer_agent_id = paused_agent_id OR NOT (authors ? reviewer_agent_id)`
    evaluates to `NULL` (falsy in `WHERE`), so `declaredAuthors.isEmpty` is
    required when `reviewer = none`. -/
def resolveAuthorityOk (s : SessionRow) : Bool :=
  let notSelfPauseBound := !(s.pauseEffectRequired && s.reviewer == some s.pausedAgent)
  let authorOk := s.authorshipValid &&
    match s.reviewer with
    | none   => s.declaredAuthors.isEmpty
    | some r => (r == s.pausedAgent) || !(s.declaredAuthors.contains r)
  notSelfPauseBound && authorOk

/-- Combined `WHERE` guard for `DialecticDB.resolve_session` (`src/dialectic_db.py:920`). -/
def resolveGuardOk (s : SessionRow) (targetStatus : SessionStatus) (cas : CasExpectations) : Bool :=
  !s.status.isTerminal &&
  casMatches s cas &&
  !(cas.excludeInflightSaga && hasInflightSaga s.sagas) &&
  (targetStatus != .resolved || resolveAuthorityOk s)

/-- `detail.get("refused")` populated by `DialecticDB.resolve_session` on refusal
    (`src/dialectic_db.py:1015-1063`). Computed from the row's current state
    whenever `targetStatus = .resolved` and the row is non-terminal (regardless
    of whether a CAS or saga predicate also failed); returns `none` when the row
    is terminal, `targetStatus != .resolved`, or the row's reviewer/authorship
    state passes Python's post-refusal check. -/
def computeRefusalReason (s : SessionRow) (targetStatus : SessionStatus) : Option String :=
  if s.status.isTerminal || targetStatus != .resolved then
    none
  else if s.pauseEffectRequired && s.reviewer == some s.pausedAgent then
    some "self_review_not_authorizing"
  else if !s.authorshipValid then
    some "invalid_review_authorship"
  else match s.reviewer with
    | some r =>
      if r != s.pausedAgent && s.declaredAuthors.contains r then
        some "reviewer_recused"
      else
        none
    | none => none

/-- Result of `resolveSession` combining `DialecticDB.resolve_session` and
    `resolve_session_async` (`src/dialectic_db.py:772-1063, 2195-2253`). -/
structure ResolveOutput where
  state            : SessionRow
  written          : Bool
  idempotentReplay : Bool
  rawResult        : Bool
  sweeperWritten   : Bool
  winnerStatus     : Option SessionStatus
  refusedReason    : Option String
  deriving DecidableEq, Repr, Inhabited

/-- `DialecticDB.resolve_session` + `resolve_session_async`
    (`src/dialectic_db.py:772-1063, 2195-2253`). -/
def resolveSession (s : SessionRow) (targetStatus : SessionStatus)
    (cas : CasExpectations) (now : Nat) : ResolveOutput :=
  if resolveGuardOk s targetStatus cas then
    let nextPhase := if targetStatus == .resolved then Phase.resolved else targetStatus.toPhase
    let nextState := { s with
      status := targetStatus
      phase := nextPhase
      awaitingFacilitation := false
      updatedAt := now
    }
    { state := nextState
    , written := true
    , idempotentReplay := false
    , rawResult := true
    , sweeperWritten := true
    , winnerStatus := none
    , refusedReason := none }
  else if s.status == targetStatus then
    -- Idempotent no-op (`src/dialectic_db.py:991-1014, 2223-2246`):
    -- `resolve_session` returns True (`rawResult := true`), `detail["written"]` is False,
    -- and `resolve_session_async(..., winner=winner)` returns False (`sweeperWritten := false`).
    { state := s
    , written := false
    , idempotentReplay := true
    , rawResult := true
    , sweeperWritten := false
    , winnerStatus := some s.status
    , refusedReason := none }
  else
    { state := s
    , written := false
    , idempotentReplay := false
    , rawResult := false
    , sweeperWritten := false
    , winnerStatus := some s.status
    , refusedReason := computeRefusalReason s targetStatus }

/-- Result of `DialecticDB.add_message` (`src/dialectic_db.py:1399-1587`). -/
structure AddMessageOutput where
  state       : SessionRow
  written     : Bool
  newRevision : Option Nat
  deriving DecidableEq, Repr, Inhabited

/-- Combined guard for `DialecticDB.add_message` (`src/dialectic_db.py:1531-1546`). -/
def addMessageGuardOk (s : SessionRow) (cas : CasExpectations)
    (nextReviewer : Option AgentId) (reopenFailedFacilitation : Bool) : Bool :=
  let statusGuardOk :=
    if reopenFailedFacilitation then
      s.status == .failed && s.awaitingFacilitation == true
    else
      s.status == .active
  let casOk := casMatches s cas
  let nextRevAuthOk :=
    match nextReviewer with
    | none   => true
    | some r =>
      s.authorshipValid && ((r == s.pausedAgent) || !(s.declaredAuthors.contains r))
  statusGuardOk && casOk && nextRevAuthOk

/-- `DialecticDB.add_message` (`src/dialectic_db.py:1399-1587`).
    Atomically bumps `transcript_revision = transcript_revision + 1` and
    `updated_at = now` under the session row lock when the status and CAS
    guards pass. -/
def addMessage (s : SessionRow) (cas : CasExpectations)
    (nextPhase : Option Phase) (nextReviewer : Option AgentId)
    (nextAwaitingFacilitation : Option Bool) (nextStatus : Option SessionStatus)
    (reopenFailedFacilitation : Bool) (now : Nat) : AddMessageOutput :=
  if addMessageGuardOk s cas nextReviewer reopenFailedFacilitation then
    let newRev := s.transcriptRevision + 1
    let s' := { s with
      updatedAt := now
      transcriptRevision := newRev
      phase := nextPhase.getD s.phase
      reviewer := match nextReviewer with | some r => some r | none => s.reviewer
      awaitingFacilitation := nextAwaitingFacilitation.getD s.awaitingFacilitation
      status := nextStatus.getD s.status
    }
    { state := s', written := true, newRevision := some newRev }
  else
    { state := s, written := false, newRevision := none }

/-- Sweeper cycle counters (`_CycleCounts` in `src/mcp_handlers/dialectic/auto_resolve.py:66-118`). -/
structure CycleCounts where
  writeAttemptCount       : Nat := 0
  writeSucceededCount     : Nat := 0
  skippedCount            : Nat := 0
  writeErrorCount         : Nat := 0
  overlapCleanCount       : Nat := 0
  overlapDetectedCount    : Nat := 0
  overlapProbeFailedCount : Nat := 0
  deriving DecidableEq, Repr, Inhabited

/-- Conservation predicate on `CycleCounts` (`src/mcp_handlers/dialectic/auto_resolve.py:305-309`):
    1. `attempted = succeeded + refused + error`
    2. `succeeded = clean + detected + probe_failed` -/
def countsBalanced (c : CycleCounts) : Bool :=
  (c.writeAttemptCount == c.writeSucceededCount + c.skippedCount + c.writeErrorCount) &&
  (c.writeSucceededCount == c.overlapCleanCount + c.overlapDetectedCount + c.overlapProbeFailedCount)

/-- Outcome of the guarded write callback + post-write probe inside `_attempt_guarded_write`. -/
inductive GuardedAttemptInput where
  | writeError
  | writeRefused
  | writeSucceeded (probe : ProbeQueryResult)
  deriving DecidableEq, Repr, Inhabited

/-- `_attempt_guarded_write` counter transition (`src/mcp_handlers/dialectic/auto_resolve.py:289-432`). -/
def attemptGuardedWrite (c : CycleCounts) (inp : GuardedAttemptInput) : CycleCounts :=
  let c1 := { c with writeAttemptCount := c.writeAttemptCount + 1 }
  match inp with
  | .writeError =>
    { c1 with writeErrorCount := c1.writeErrorCount + 1 }
  | .writeRefused =>
    { c1 with skippedCount := c1.skippedCount + 1 }
  | .writeSucceeded probe =>
    let c2 := { c1 with writeSucceededCount := c1.writeSucceededCount + 1 }
    match probeWriteOverlap probe with
    | .clean =>
      { c2 with overlapCleanCount := c2.overlapCleanCount + 1 }
    | .detected =>
      { c2 with overlapDetectedCount := c2.overlapDetectedCount + 1 }
    | .probeFailed =>
      { c2 with overlapProbeFailedCount := c2.overlapProbeFailedCount + 1 }

/-!
  ## Machine-Checked Theorems
-/

/-- **Theorem 1 (Terminal Write Immutability):**
    Once a session row is in a terminal status (`.resolved` or `.failed`),
    `resolveSession` never mutates the row (`written = false` and `state = s`). -/
theorem terminal_write_immutability (s : SessionRow) (targetStatus : SessionStatus)
    (cas : CasExpectations) (now : Nat)
    (hTerm : s.status.isTerminal = true) :
    (resolveSession s targetStatus cas now).written = false ∧
    (resolveSession s targetStatus cas now).state = s := by
  have hGuard : resolveGuardOk s targetStatus cas = false := by
    unfold resolveGuardOk
    rw [hTerm]
    rfl
  unfold resolveSession
  rw [hGuard]
  cases hEq : (s.status == targetStatus) <;> simp

/-- **Theorem 2 (Idempotent Replay Is Not Counted As A Sweeper Write):**
    When `s.status` is already terminal and equals `targetStatus`,
    `resolveSession` returns `idempotentReplay = true` and `rawResult = true`,
    but `written = false` and `sweeperWritten = false` (`src/dialectic_db.py:2227-2246`). -/
theorem sweeper_idempotent_replay_not_counted_as_written (s : SessionRow)
    (targetStatus : SessionStatus) (cas : CasExpectations) (now : Nat)
    (hTerm : s.status.isTerminal = true)
    (hSame : s.status = targetStatus) :
    let out := resolveSession s targetStatus cas now
    out.idempotentReplay = true ∧
    out.rawResult = true ∧
    out.written = false ∧
    out.sweeperWritten = false := by
  dsimp only
  have hGuard : resolveGuardOk s targetStatus cas = false := by
    unfold resolveGuardOk
    rw [hTerm]
    rfl
  unfold resolveSession
  rw [hGuard, hSame]
  simp

/-- **Theorem 3 (Terminal Write Always Clears `awaitingFacilitation`):**
    Whenever `resolveSession` performs a write (`out.written = true`), the
    resulting row has `awaitingFacilitation = false` and `status = targetStatus`
    (`src/dialectic_db.py:912, 917-918`). -/
theorem terminal_write_always_clears_facilitation (s : SessionRow)
    (targetStatus : SessionStatus) (cas : CasExpectations) (now : Nat)
    (hWritten : (resolveSession s targetStatus cas now).written = true) :
    (resolveSession s targetStatus cas now).state.awaitingFacilitation = false ∧
    (resolveSession s targetStatus cas now).state.status = targetStatus := by
  unfold resolveSession at hWritten ⊢
  cases hGuard : resolveGuardOk s targetStatus cas
  · rw [hGuard] at hWritten
    cases hEq : (s.status == targetStatus) <;> simp_all
  · simp

/-- **Theorem 4 (In-Flight Saga Blocks `excludeInflightSaga` Writes):**
    If any saga on the session is in a non-terminal state (`hasInflightSaga s.sagas = true`)
    and `cas.excludeInflightSaga = true`, `resolveSession` is refused (`written = false`
    and `state = s`) (`src/dialectic_db.py:852-860`). -/
theorem inflight_saga_blocks_excluded_resolve (s : SessionRow)
    (targetStatus : SessionStatus) (cas : CasExpectations) (now : Nat)
    (hInflight : hasInflightSaga s.sagas = true)
    (hExclude : cas.excludeInflightSaga = true) :
    (resolveSession s targetStatus cas now).written = false ∧
    (resolveSession s targetStatus cas now).state = s := by
  have hGuard : resolveGuardOk s targetStatus cas = false := by
    unfold resolveGuardOk
    rw [hExclude, hInflight]
    simp
  unfold resolveSession
  rw [hGuard]
  cases hEq : (s.status == targetStatus) <;> simp

/-- Helper lemma: whenever `cas.expectedTranscriptRevision = some rev` and
    `s'.transcriptRevision ≠ rev`, `resolveSession` is refused. -/
theorem stale_transcript_revision_blocks_resolve (s' : SessionRow)
    (targetStatus : SessionStatus) (cas : CasExpectations) (now : Nat) (rev : Nat)
    (hPinned : cas.expectedTranscriptRevision = some rev)
    (hNe : s'.transcriptRevision ≠ rev) :
    (resolveSession s' targetStatus cas now).written = false ∧
    (resolveSession s' targetStatus cas now).state = s' := by
  have hCas : casMatches s' cas = false := by
    unfold casMatches
    rw [hPinned]
    dsimp only
    have hBeq : (s'.transcriptRevision == rev) = false := beq_eq_false_iff_ne.mpr hNe
    rw [hBeq]
    simp
  have hGuard : resolveGuardOk s' targetStatus cas = false := by
    unfold resolveGuardOk
    rw [hCas]
    simp
  unfold resolveSession
  rw [hGuard]
  cases hEq : (s'.status == targetStatus) <;> simp

/-- Helper lemma: whenever `addMessage` writes (`written = true`), the resulting
    session has `transcriptRevision = s.transcriptRevision + 1`. -/
theorem addMessage_bumps_transcript_revision (s : SessionRow) (cas : CasExpectations)
    (nextPhase : Option Phase) (nextReviewer : Option AgentId)
    (nextFac : Option Bool) (nextStatus : Option SessionStatus)
    (reopen : Bool) (now : Nat)
    (hOk : (addMessage s cas nextPhase nextReviewer nextFac nextStatus reopen now).written = true) :
    (addMessage s cas nextPhase nextReviewer nextFac nextStatus reopen now).state.transcriptRevision =
      s.transcriptRevision + 1 := by
  unfold addMessage at hOk ⊢
  cases hGuard : addMessageGuardOk s cas nextReviewer reopen
  · rw [hGuard] at hOk
    contradiction
  · rfl

/-- **Theorem 5 (Concurrent Message Bump Invalidates Stale Transcript-Revision CAS):**
    If a caller snapshots `casResolve.expectedTranscriptRevision = some s.transcriptRevision`
    and a concurrent `addMessage` lands first (`msgOut.written = true`), then
    `resolveSession msgOut.state targetStatus casResolve now2` is guaranteed to be
    refused (`written = false` and `state = msgOut.state`). -/
theorem concurrent_message_bump_invalidates_stale_cas (s : SessionRow)
    (casMsg casResolve : CasExpectations)
    (nextPhase : Option Phase) (nextReviewer : Option AgentId)
    (nextFac : Option Bool) (nextStatus : Option SessionStatus)
    (reopen : Bool) (now1 now2 : Nat) (targetStatus : SessionStatus)
    (hMsgOk : (addMessage s casMsg nextPhase nextReviewer nextFac nextStatus reopen now1).written = true)
    (hPinned : casResolve.expectedTranscriptRevision = some s.transcriptRevision) :
    let s' := (addMessage s casMsg nextPhase nextReviewer nextFac nextStatus reopen now1).state
    (resolveSession s' targetStatus casResolve now2).written = false ∧
    (resolveSession s' targetStatus casResolve now2).state = s' := by
  dsimp only
  have hBump := addMessage_bumps_transcript_revision s casMsg nextPhase nextReviewer nextFac nextStatus reopen now1 hMsgOk
  apply stale_transcript_revision_blocks_resolve _ _ _ _ s.transcriptRevision hPinned
  omega

/-- **Theorem 6 (Pause-Bound Self-Review Blocks `.resolved` Terminal Writes While Permitting `.failed`):**
    When `s.pauseEffectRequired = true` and `s.reviewer = some s.pausedAgent`,
    `resolveSession s .resolved cas now` never writes (`written = false` and `state = s`),
    whereas `resolveSession s .failed cas now` writes (`written = true`) whenever the
    non-authority guards (`!s.status.isTerminal`, `casMatches`, and saga exclusion) hold
    (`src/dialectic_db.py:862-875, 1024-1036`). -/
theorem pause_bound_self_review_blocks_resolved_not_failed (s : SessionRow)
    (cas : CasExpectations) (now : Nat)
    (hPause : s.pauseEffectRequired = true)
    (hSelf : s.reviewer = some s.pausedAgent) :
    (resolveSession s .resolved cas now).written = false ∧
    (resolveSession s .resolved cas now).state = s ∧
    ((!s.status.isTerminal && casMatches s cas &&
      !(cas.excludeInflightSaga && hasInflightSaga s.sagas)) = true →
      (resolveSession s .failed cas now).written = true) := by
  have hAuth : resolveAuthorityOk s = false := by
    unfold resolveAuthorityOk
    rw [hPause, hSelf]
    simp
  have hGuardRes : resolveGuardOk s .resolved cas = false := by
    unfold resolveGuardOk
    rw [hAuth]
    simp
  refine ⟨?_, ?_, ?_⟩
  · unfold resolveSession
    rw [hGuardRes]
    cases hEq : (s.status == .resolved) <;> simp
  · unfold resolveSession
    rw [hGuardRes]
    cases hEq : (s.status == .resolved) <;> simp
  · intro hNonAuth
    have hGuardFail : resolveGuardOk s .failed cas = true := by
      unfold resolveGuardOk
      rw [hNonAuth]
      rfl
    unfold resolveSession
    rw [hGuardFail]
    rfl

/-- **Theorem 7a (Unhealthy Overlap Probe Never Reports Clean):**
    When `probeHealthy = false`, `probeWriteOverlap (probeSagaSince sagas since false)`
    equals `.probeFailed` (and never `.clean`)
    (`src/mcp_handlers/dialectic/auto_resolve.py:249-268`). -/
theorem unhealthy_probe_returns_probe_failed (sagas : List SagaRecord) (since : Option Nat) :
    probeWriteOverlap (probeSagaSince sagas since false) = .probeFailed := by
  rfl

/-- **Theorem 7b (Time-Correlated Probe Catches Sagas Created At Or After `earlyCheckTs`):**
    When `probeHealthy = true` and any saga `sg ∈ sagas` has `sg.createdAt ≥ t`
    (even if `sg.state` is already `.pgCommitted` or `.reverted`),
    `probeWriteOverlap (probeSagaSince sagas (some t) true)` returns `.detected`
    (`src/dialectic_db.py:1343-1384`). -/
theorem time_correlated_probe_catches_window_saga (sagas : List SagaRecord)
    (sg : SagaRecord) (t : Nat)
    (hMem : sg ∈ sagas)
    (hCreated : sg.createdAt ≥ t) :
    probeWriteOverlap (probeSagaSince sagas (some t) true) = .detected := by
  unfold probeSagaSince probeWriteOverlap
  simp only [Bool.not_true, Bool.false_eq_true, ↓reduceIte]
  have hMatch : sagaMatchesProbe (some t) sg = true := by
    unfold sagaMatchesProbe
    dsimp only
    have hDec : decide (sg.createdAt ≥ t) = true := decide_eq_true hCreated
    rw [hDec, Bool.or_true]
  have hNonEmpty : sagas.filter (sagaMatchesProbe (some t)) ≠ [] := by
    intro hEmpty
    have hInFilter : sg ∈ sagas.filter (sagaMatchesProbe (some t)) :=
      List.mem_filter.mpr ⟨hMem, hMatch⟩
    rw [hEmpty] at hInFilter
    contradiction
  cases hFilt : sagas.filter (sagaMatchesProbe (some t)) with
  | nil => contradiction
  | cons hd tl => rfl

/-- **Theorem 8 (Sweeper Cycle Accounting Conservation):**
    Every execution of `attemptGuardedWrite` preserves both `_CycleCounts`
    balance identities (`attempted = succeeded + refused + error` and
    `succeeded = clean + detected + probe_failed`)
    (`src/mcp_handlers/dialectic/auto_resolve.py:305-309`). -/
theorem cycle_counts_conservation (c : CycleCounts) (inp : GuardedAttemptInput)
    (hBal : countsBalanced c = true) :
    countsBalanced (attemptGuardedWrite c inp) = true := by
  unfold countsBalanced at hBal ⊢
  rw [Bool.and_eq_true, beq_iff_eq, beq_iff_eq] at hBal ⊢
  rcases hBal with ⟨h1, h2⟩
  cases inp with
  | writeError =>
    unfold attemptGuardedWrite
    dsimp only
    omega
  | writeRefused =>
    unfold attemptGuardedWrite
    dsimp only
    omega
  | writeSucceeded probe =>
    unfold attemptGuardedWrite
    cases hOv : probeWriteOverlap probe <;>
    simp only [hOv] <;>
    omega

/-!
  ## Compile-Time `#guard` Conformance Vectors
  Parsed by `tests/test_saga_cas_protocol_state_machine.py` to cross-check
  these `#guard` trace vectors against `SpecSagaState` and the live Python
  `DialecticDB` / `_attempt_guarded_write` / `_probe_write_overlap` code.
-/

def mkInitSession (paused : AgentId) (reviewer : Option AgentId)
    (authors : List AgentId) (pauseReq : Bool) : SessionRow :=
  { status := .active
  , phase := .synthesis
  , pausedAgent := paused
  , reviewer := reviewer
  , transcriptRevision := 0
  , resolutionSagaRevision := 0
  , awaitingFacilitation := true
  , updatedAt := 100
  , pauseEffectRequired := pauseReq
  , declaredAuthors := authors
  , authorshipValid := true
  , sagas := [] }

-- Scenario 1: Concurrent message append bumps `transcriptRevision` (0 -> 1) and
-- `updatedAt` (100 -> 110), causing a stale sweeper `resolveSession` pinned to
-- `expectedTranscriptRevision = some 0` to be refused (`written = false`), followed
-- by a fresh `resolveSession` at `expectedTranscriptRevision = some 1` that lands
-- as `.failed` and clears `awaitingFacilitation = false`, and an idempotent replay
-- that returns `rawResult = true` and `sweeperWritten = false`.
def sagaScenario1Stale : ResolveOutput :=
  let s0 := mkInitSession 0 (some 1) [] true
  let m1 := addMessage s0 { expectedPhase := some .synthesis, expectedTranscriptRevision := some 0 } (some .synthesis) none none none false 110
  resolveSession m1.state .failed { expectedPhase := some .synthesis, expectedTranscriptRevision := some 0, excludeInflightSaga := true, clearFacilitation := true } 120

def sagaScenario1Replay : ResolveOutput :=
  let rFresh := resolveSession sagaScenario1Stale.state .failed { expectedPhase := some .synthesis, expectedTranscriptRevision := some 1, excludeInflightSaga := true, clearFacilitation := true } 130
  resolveSession rFresh.state .failed { excludeInflightSaga := true, clearFacilitation := true } 140

def sagaScenario1 : SessionRow :=
  let s0 := mkInitSession 0 (some 1) [] true
  let m1 := addMessage s0 { expectedPhase := some .synthesis, expectedTranscriptRevision := some 0 } (some .synthesis) none none none false 110
  let rStale := resolveSession m1.state .failed { expectedPhase := some .synthesis, expectedTranscriptRevision := some 0, excludeInflightSaga := true, clearFacilitation := true } 120
  let rFresh := resolveSession rStale.state .failed { expectedPhase := some .synthesis, expectedTranscriptRevision := some 1, excludeInflightSaga := true, clearFacilitation := true } 130
  let rReplay := resolveSession rFresh.state .failed { excludeInflightSaga := true, clearFacilitation := true } 140
  rReplay.state

#guard sagaScenario1Stale.written == false
#guard sagaScenario1Stale.winnerStatus == some .active
#guard sagaScenario1Stale.refusedReason == none
#guard sagaScenario1Replay.written == false
#guard sagaScenario1Replay.idempotentReplay == true
#guard sagaScenario1Replay.rawResult == true
#guard sagaScenario1Replay.sweeperWritten == false
#guard sagaScenario1.status == .failed
#guard sagaScenario1.phase == .failed
#guard sagaScenario1.awaitingFacilitation == false
#guard sagaScenario1.transcriptRevision == 1
#guard sagaScenario1.updatedAt == 130

-- Scenario 2: An in-flight saga (`state = .reserved`, created at 105) blocks
-- a sweeper `resolveSession` with `excludeInflightSaga = true`; once the saga
-- commits (`state = .pgCommitted`), `hasInflightSaga` becomes `false`, and
-- `probeSagaSince` with `since = some 100` still detects the committed saga
-- (`probeWriteOverlap = .detected`).
def sagaScenario2Blocked : ResolveOutput :=
  let s0 := mkInitSession 0 (some 1) [] true
  let s1 := { s0 with sagas := [{ sagaId := 1, state := .reserved, createdAt := 105 }] }
  resolveSession s1 .failed { expectedTranscriptRevision := some 0, excludeInflightSaga := true, clearFacilitation := true } 115

def sagaScenario2 : SessionRow :=
  let s0 := mkInitSession 0 (some 1) [] true
  let s1 := { s0 with sagas := [{ sagaId := 1, state := .reserved, createdAt := 105 }] }
  let rBlocked := resolveSession s1 .failed { expectedTranscriptRevision := some 0, excludeInflightSaga := true, clearFacilitation := true } 115
  let s2 := { rBlocked.state with sagas := [{ sagaId := 1, state := .pgCommitted, createdAt := 105 }] }
  let rAfter := resolveSession s2 .failed { expectedTranscriptRevision := some 0, excludeInflightSaga := true, clearFacilitation := true } 125
  rAfter.state

#guard sagaScenario2Blocked.written == false
#guard sagaScenario2Blocked.winnerStatus == some .active
#guard sagaScenario2Blocked.refusedReason == none
#guard sagaScenario2.status == .failed
#guard sagaScenario2.awaitingFacilitation == false
#guard hasInflightSaga sagaScenario2.sagas == false
#guard probeWriteOverlap (probeSagaSince sagaScenario2.sagas (some 100) true) == .detected
#guard probeWriteOverlap (probeSagaSince sagaScenario2.sagas (some 100) false) == .probeFailed

-- Scenario 3: Pause-bound self-review (`pauseEffectRequired = true`, `reviewer = some 0`)
-- refuses `resolveSession` with `.resolved` (`self_review_not_authorizing`), leaving
-- the session `.active`, whereas `resolveSession` with `.failed` succeeds.
def sagaScenario3SelfResolve : ResolveOutput :=
  let s0 := mkInitSession 0 (some 0) [] true
  resolveSession s0 .resolved { expectedTranscriptRevision := some 0 } 110

def sagaScenario3 : SessionRow :=
  let s0 := mkInitSession 0 (some 0) [] true
  let rSelfResolve := resolveSession s0 .resolved { expectedTranscriptRevision := some 0 } 110
  let rFail := resolveSession rSelfResolve.state .failed { expectedTranscriptRevision := some 0, clearFacilitation := true } 120
  rFail.state

#guard sagaScenario3SelfResolve.written == false
#guard sagaScenario3SelfResolve.winnerStatus == some .active
#guard sagaScenario3SelfResolve.refusedReason == some "self_review_not_authorizing"
#guard sagaScenario3.status == .failed
#guard sagaScenario3.phase == .failed
#guard sagaScenario3.awaitingFacilitation == false
#guard sagaScenario3.updatedAt == 120

-- Scenario 4: Sweeper cycle counters across a mixed sequence of 5 guarded write
-- attempts (clean, detected, probeFailed, refused, error) preserve both conservation
-- laws at every step.
def sagaScenario4Counts : CycleCounts :=
  let c0 : CycleCounts := {}
  let c1 := attemptGuardedWrite c0 (.writeSucceeded .empty)
  let c2 := attemptGuardedWrite c1 (.writeSucceeded (.found { sagaId := 1, state := .pgCommitted, createdAt := 105 }))
  let c3 := attemptGuardedWrite c2 (.writeSucceeded .queryFailed)
  let c4 := attemptGuardedWrite c3 .writeRefused
  let c5 := attemptGuardedWrite c4 .writeError
  c5

#guard countsBalanced sagaScenario4Counts == true
#guard sagaScenario4Counts.writeAttemptCount == 5
#guard sagaScenario4Counts.writeSucceededCount == 3
#guard sagaScenario4Counts.skippedCount == 1
#guard sagaScenario4Counts.writeErrorCount == 1
#guard sagaScenario4Counts.overlapCleanCount == 1
#guard sagaScenario4Counts.overlapDetectedCount == 1
#guard sagaScenario4Counts.overlapProbeFailedCount == 1

end Unitares.SagaCasProtocol
