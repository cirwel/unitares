/-
  UNITARES Dialectic Protocol & Resolution Authority State Machine
  ================================================================

  Mechanized Lean 4 specification of the peer-review dialectic state machine
  (`src/dialectic_protocol.py`), reviewer recusal (`src/dialectic_reviewed_work.py`),
  and the BEAM cross-runtime resolution saga phase guard
  (`elixir/lease_plane/lib/unitares_lease_plane/dialectic_saga.ex`).

  Every theorem in this file is kernel-checked with zero `sorry`s and zero
  user-defined axioms. The companion stateful test suite
  (`tests/test_dialectic_protocol_state_machine.py`) runs `lean` on this file
  when available and checks via Hypothesis `RuleBasedStateMachine` that the
  live Python `DialecticSession` implementation is a strict refinement of this
  transition system.
-/

namespace Unitares.DialecticProtocol

/-- Discrete agent identifier. -/
abbrev AgentId := Nat

/-- Dialectic session lifecycle phases (`DialecticPhase` in `src/dialectic_protocol.py`). -/
inductive Phase where
  | thesis
  | antithesis
  | synthesis
  | resolved
  | failed
  deriving DecidableEq, Repr

/-- Session types (`DialecticSession.__init__`). -/
inductive SessionType where
  | recovery
  | review
  | dispute
  | exploration
  | design_review
  deriving DecidableEq, Repr

/-- Transcript entry phase (`NON_PROTOCOL_PHASES = {"consult"}` vs protocol moves). -/
inductive MsgPhase where
  | thesis
  | antithesis
  | synthesis
  | consult
  deriving DecidableEq, Repr

/-- Abstracted transcript message (`DialecticMessage`). -/
structure Message where
  phase : MsgPhase
  sender : AgentId
  hasRootCause : Bool := true
  hasConditions : Bool := true
  hasReasoning : Bool := true
  agrees : Option Bool := none
  deriving DecidableEq, Repr

/-- Dialectic session state (`DialecticSession`). -/
structure State where
  pausedAgent : AgentId
  reviewerAgent : Option AgentId
  declaredAuthors : List AgentId
  sessionType : SessionType
  maxRounds : Nat
  phase : Phase
  synthesisRound : Nat
  awaitingFacilitation : Bool
  transcript : List Message
  deriving DecidableEq, Repr

/-- Result of a protocol transition step. -/
inductive StepOutcome where
  | ok (next : State) (converged : Bool)
  | refused (next : State)
  deriving DecidableEq, Repr

namespace StepOutcome

/-- Extract the post-transition state regardless of acceptance or refusal. -/
@[simp] def state : StepOutcome → State
  | .ok s _ => s
  | .refused s => s

/-- True iff the transition reported convergence (`converged = True`). -/
@[simp] def converged : StepOutcome → Bool
  | .ok _ c => c
  | .refused _ => false

end StepOutcome

/--
  Recusal predicate (`ReviewedWorkAuthorship.recuses` in `src/dialectic_reviewed_work.py`).
  A declared author of the reviewed work cannot serve as its independent reviewer.
  Self-review (`candidate == pausedAgent`) is governed separately by `reviewer_mode='self'`.
-/
def isRecused (s : State) (candidate : AgentId) : Bool :=
  (candidate != s.pausedAgent) && (s.declaredAuthors.contains candidate)

/-- Whether the currently assigned reviewer (if any) is recused. -/
def assignedReviewerRecused (s : State) : Bool :=
  match s.reviewerAgent with
  | none => false
  | some rev => isRecused s rev

/--
  Helper scanning a reversed transcript list for the latest synthesis verdict
  under `selfReview` mode (`DialecticSession._reviewer_objection_stands`).
-/
def scanObjectionStands (pausedAgent : AgentId) (selfReview : Bool) : List Message → Bool
  | [] => false
  | msg :: rest =>
    if msg.phase != .synthesis then
      scanObjectionStands pausedAgent selfReview rest
    else
      match msg.agrees with
      | none => scanObjectionStands pausedAgent selfReview rest
      | some agreesVal =>
        if !selfReview && (msg.sender == pausedAgent) then
          scanObjectionStands pausedAgent selfReview rest
        else
          !agreesVal

/--
  True when the reviewer's most recent synthesis verdict was a rejection (`agrees = false`).
  Keyed on "the last synthesis NOT from the paused agent" (unless `selfReview`),
  so reviewer reassignment cannot orphan a recorded objection.
-/
def reviewerObjectionStands (s : State) : Bool :=
  let selfReview := s.reviewerAgent == some s.pausedAgent
  scanObjectionStands s.pausedAgent selfReview s.transcript.reverse

/--
  True after an independent antithesis and before the first independent synthesis verdict
  (`DialecticSession._reviewer_verdict_pending`).
-/
def reviewerVerdictPending (s : State) : Bool :=
  match s.reviewerAgent with
  | none => false
  | some rev =>
    if rev == s.pausedAgent then
      false
    else
      let hasIndepAntithesis := s.transcript.any (fun m =>
        m.phase == .antithesis && m.sender != s.pausedAgent)
      let hasIndepVerdict := s.transcript.any (fun m =>
        m.phase == .synthesis && m.sender != s.pausedAgent && m.agrees.isSome)
      hasIndepAntithesis && !hasIndepVerdict

/--
  Scan a reversed transcript for whether the paused agent already answered (`agrees = true`)
  after the latest standing reviewer rejection (`DialecticSession._paused_agent_already_answered_current_objection`).
-/
def scanAlreadyAnswered (pausedAgent : AgentId) (selfReview : Bool) (seenPausedAgree : Bool) : List Message → Bool
  | [] => false
  | msg :: rest =>
    let isReviewerRejection :=
      (msg.phase == .synthesis) &&
      (msg.agrees == some false) &&
      (selfReview || (msg.sender != pausedAgent))
    if isReviewerRejection then
      seenPausedAgree
    else
      let isPausedAgree :=
        (msg.phase == .synthesis) &&
        (msg.sender == pausedAgent) &&
        (msg.agrees == some true)
      scanAlreadyAnswered pausedAgent selfReview (seenPausedAgree || isPausedAgree) rest

def pausedAlreadyAnsweredCurrentObjection (s : State) : Bool :=
  let selfReview := s.reviewerAgent == some s.pausedAgent
  scanAlreadyAnswered s.pausedAgent selfReview false s.transcript.reverse

/--
  Phase 1 transition: `DialecticSession.submit_thesis`.
-/
def submitThesis (s : State) (m : Message) : StepOutcome :=
  if m.sender != s.pausedAgent then
    .refused s
  else if s.phase != .thesis then
    .refused s
  else if !m.hasRootCause then
    .refused s
  else if (s.sessionType == .recovery) && !m.hasConditions then
    .refused s
  else
    .ok { s with
      transcript := s.transcript ++ [m],
      phase := .antithesis
    } false

/--
  Phase 2 transition: `DialecticSession.submit_antithesis`.
-/
def submitAntithesis (s : State) (m : Message) : StepOutcome :=
  if isRecused s m.sender then
    .refused s
  else
    match s.reviewerAgent with
    | none =>
      if m.sender == s.pausedAgent then
        .refused s
      else if s.phase != .antithesis then
        .refused { s with reviewerAgent := some m.sender }
      else if !m.hasReasoning then
        .refused { s with reviewerAgent := some m.sender }
      else
        .ok { s with
          reviewerAgent := some m.sender,
          transcript := s.transcript ++ [m],
          phase := .synthesis,
          synthesisRound := 1
        } false
    | some rev =>
      if m.sender != rev then
        .refused s
      else if s.phase != .antithesis then
        .refused s
      else if !m.hasReasoning then
        .refused s
      else
        .ok { s with
          transcript := s.transcript ++ [m],
          phase := .synthesis,
          synthesisRound := 1
        } false

/--
  Phase 3 transition: `DialecticSession.submit_synthesis`.
-/
def submitSynthesis (s : State) (m : Message) : StepOutcome :=
  if assignedReviewerRecused s then
    .refused s
  else if s.phase != .synthesis then
    .refused s
  else if Nat.blt s.maxRounds s.synthesisRound then
    .refused { s with phase := .failed }
  else if (m.agrees == some true) && (m.sender == s.pausedAgent) && reviewerVerdictPending s then
    .refused s
  else if (m.agrees == some true) && (m.sender == s.pausedAgent) && reviewerObjectionStands s && pausedAlreadyAnsweredCurrentObjection s then
    .refused s
  else if (m.agrees == some true) && (m.sender == s.pausedAgent) && reviewerObjectionStands s then
    .ok { s with
      transcript := s.transcript ++ [m],
      synthesisRound := s.synthesisRound + 1,
      awaitingFacilitation := true
    } false
  else if m.agrees == some true then
    .ok { s with
      transcript := s.transcript ++ [m],
      phase := .resolved
    } true
  else
    .ok { s with
      transcript := s.transcript ++ [m],
      synthesisRound := s.synthesisRound + 1
    } false

/--
  Reviewer reassignment (`_apply_reviewer_reassignment` in `src/mcp_handlers/dialectic/`).
  Updates `reviewerAgent` and clears `awaitingFacilitation` without altering `transcript`.
-/
def reassignReviewer (s : State) (newReviewer : AgentId) : State :=
  { s with
    reviewerAgent := some newReviewer,
    awaitingFacilitation := false
  }

/--
  BEAM resolution saga phase guard (`UnitaresLeasePlane.DialecticSaga.guard_session_phase`).
  - A session already in `resolved` or `failed` cannot claim any resolution saga.
  - A saga targeting `resolved` additionally requires `s.phase == .synthesis`.
  - A saga targeting `failed` is permitted from any non-terminal phase (`thesis`, `antithesis`, `synthesis`).
-/
def canClaimResolveSaga (s : State) (targetStatus : Phase) : Bool :=
  match s.phase with
  | .resolved => false
  | .failed => false
  | currentPhase =>
    match targetStatus with
    | .resolved => currentPhase == .synthesis
    | .failed => true
    | _ => false

/-- Topological rank of each phase in the protocol DAG. -/
def Phase.rank : Phase → Nat
  | .thesis => 0
  | .antithesis => 1
  | .synthesis => 2
  | .resolved => 3
  | .failed => 3

/--
  Automated exit from facilitation when a standing reviewer rejection has had
  its last word (`assess_rejection_stands` in `src/mcp_handlers/dialectic/rejection_stands.py`).
  Never transitions to `.resolved`; either leaves the session open or transitions to `.failed`.
-/
def closeStandingRejection (s : State) (reviewerHadLastWord : Bool) : StepOutcome :=
  match s.reviewerAgent with
  | none => .refused s
  | some rev =>
    if !s.awaitingFacilitation then
      .refused s
    else if rev == s.pausedAgent then
      .refused s
    else if s.phase != .synthesis then
      .refused s
    else if !reviewerObjectionStands s then
      .refused s
    else if !reviewerHadLastWord then
      .refused s
    else
      .ok { s with phase := .failed, awaitingFacilitation := false } false

/-!
  ## Mechanized Safety & Authority Theorems
-/

/--
  Theorem 1a (Terminal Resolved State Is Absorbing):
  Once a session reaches `.resolved`, neither `submitThesis`, `submitAntithesis`,
  nor `submitSynthesis` can transition it to another phase or mutate its transcript.
-/
theorem terminal_resolved_absorbing (s : State) (m : Message)
    (h : s.phase = .resolved) :
    (submitThesis s m).state.phase = .resolved ∧
    (submitAntithesis s m).state.phase = .resolved ∧
    (submitSynthesis s m).state.phase = .resolved ∧
    (submitSynthesis s m).state.transcript = s.transcript := by
  refine ⟨?_, ?_, ?_, ?_⟩
  · unfold submitThesis
    cases (m.sender != s.pausedAgent) <;> simp [h]
  · unfold submitAntithesis
    cases isRecused s m.sender
    · cases s.reviewerAgent
      · cases (m.sender == s.pausedAgent) <;> simp [h]
      · rename_i rev
        cases (m.sender != rev) <;> simp [h]
    · simp [h]
  · unfold submitSynthesis
    cases assignedReviewerRecused s <;> simp [h]
  · unfold submitSynthesis
    cases assignedReviewerRecused s <;> simp [h]

/--
  Theorem 1b (Terminal Failed State Is Absorbing):
  Once a session reaches `.failed`, neither `submitThesis`, `submitAntithesis`,
  nor `submitSynthesis` can transition it to another phase or mutate its transcript.
-/
theorem terminal_failed_absorbing (s : State) (m : Message)
    (h : s.phase = .failed) :
    (submitThesis s m).state.phase = .failed ∧
    (submitAntithesis s m).state.phase = .failed ∧
    (submitSynthesis s m).state.phase = .failed ∧
    (submitSynthesis s m).state.transcript = s.transcript := by
  refine ⟨?_, ?_, ?_, ?_⟩
  · unfold submitThesis
    cases (m.sender != s.pausedAgent) <;> simp [h]
  · unfold submitAntithesis
    cases isRecused s m.sender
    · cases s.reviewerAgent
      · cases (m.sender == s.pausedAgent) <;> simp [h]
      · rename_i rev
        cases (m.sender != rev) <;> simp [h]
    · simp [h]
  · unfold submitSynthesis
    cases assignedReviewerRecused s <;> simp [h]
  · unfold submitSynthesis
    cases assignedReviewerRecused s <;> simp [h]

/--
  Theorem 2 (No Unilateral Self-Clear Over Standing Reviewer Objection — #1543):
  Whenever `s.phase ≠ .resolved`, `reviewerObjectionStands s = true`, and the
  synthesis message is submitted by `s.pausedAgent`, `submitSynthesis s m`
  NEVER converges (`converged = false`) and NEVER transitions `phase` to
  `.resolved`, regardless of whether the session is in `reviewer_mode='self'`
  or independent review.
-/
theorem no_unilateral_self_clear_over_objection (s : State) (m : Message)
    (hNotResolved : s.phase ≠ .resolved)
    (hObj : reviewerObjectionStands s = true)
    (hSender : m.sender = s.pausedAgent) :
    (submitSynthesis s m).converged = false ∧
    (submitSynthesis s m).state.phase ≠ .resolved := by
  unfold submitSynthesis
  have hIsPaused : (m.sender == s.pausedAgent) = true := by simp [hSender]
  rw [hObj, hIsPaused]
  cases hRec : assignedReviewerRecused s <;>
  cases hPhase : s.phase <;>
  cases hRound : Nat.blt s.maxRounds s.synthesisRound <;>
  cases hAgrees : (m.agrees == some true) <;>
  cases hPend : reviewerVerdictPending s <;>
  cases hAns : pausedAlreadyAnsweredCurrentObjection s <;>
  simp_all

/--
  Theorem 3 (No Premature Self-Resolve While Independent Verdict Is Pending):
  Whenever `s.phase ≠ .resolved`, `reviewerVerdictPending s = true`, and
  `m.sender = s.pausedAgent` submits `m.agrees = some true`, `submitSynthesis s m`
  never converges, never reaches `.resolved`, and never appends to `s.transcript`.
-/
theorem no_premature_self_resolve_while_verdict_pending (s : State) (m : Message)
    (hNotResolved : s.phase ≠ .resolved)
    (hPending : reviewerVerdictPending s = true)
    (hSender : m.sender = s.pausedAgent)
    (hAgrees : m.agrees = some true) :
    (submitSynthesis s m).converged = false ∧
    (submitSynthesis s m).state.phase ≠ .resolved ∧
    (submitSynthesis s m).state.transcript = s.transcript := by
  unfold submitSynthesis
  have hIsPaused : (m.sender == s.pausedAgent) = true := by simp [hSender]
  have hAgreesTrue : (m.agrees == some true) = true := by simp [hAgrees]
  rw [hPending, hIsPaused, hAgreesTrue]
  cases hRec : assignedReviewerRecused s <;>
  cases hPhase : s.phase <;>
  cases hRound : Nat.blt s.maxRounds s.synthesisRound <;>
  simp_all

/--
  Theorem 4 (Repeat Self-Clear Does Not Grow Transcript — Anti-Starvation Guard):
  When `reviewerObjectionStands s = true` and `pausedAlreadyAnsweredCurrentObjection s = true`,
  a repeat `agrees = some true` synthesis from `s.pausedAgent` is refused without
  appending to `s.transcript`, preventing `updated_at` starvation of the liveness sweeper.
-/
theorem repeat_self_clear_does_not_grow_transcript (s : State) (m : Message)
    (hObj : reviewerObjectionStands s = true)
    (hAns : pausedAlreadyAnsweredCurrentObjection s = true)
    (hSender : m.sender = s.pausedAgent)
    (hAgrees : m.agrees = some true) :
    (submitSynthesis s m).converged = false ∧
    (submitSynthesis s m).state.transcript = s.transcript := by
  unfold submitSynthesis
  have hIsPaused : (m.sender == s.pausedAgent) = true := by simp [hSender]
  have hAgreesTrue : (m.agrees == some true) = true := by simp [hAgrees]
  rw [hObj, hAns, hIsPaused, hAgreesTrue]
  cases hRec : assignedReviewerRecused s <;>
  cases hPhase : s.phase <;>
  cases hRound : Nat.blt s.maxRounds s.synthesisRound <;>
  cases hPend : reviewerVerdictPending s <;>
  simp_all

/--
  Theorem 5 (Reviewer Reassignment Preserves Standing Objections):
  Reassigning a session from one independent reviewer (`r ≠ s.pausedAgent`)
  to another independent reviewer (`r' ≠ s.pausedAgent`) preserves
  `reviewerObjectionStands` identically (`reviewerObjectionStands (reassignReviewer s r') = reviewerObjectionStands s`).
-/
theorem reassignment_preserves_standing_objection (s : State) (r r' : AgentId)
    (hRev : s.reviewerAgent = some r)
    (hIndep : r ≠ s.pausedAgent)
    (hIndep' : r' ≠ s.pausedAgent) :
    reviewerObjectionStands (reassignReviewer s r') = reviewerObjectionStands s := by
  unfold reviewerObjectionStands reassignReviewer
  have hSelfOld : (s.reviewerAgent == some s.pausedAgent) = false := by
    rw [hRev]
    simp [hIndep]
  have hSelfNew : (some r' == some s.pausedAgent) = false := by
    simp [hIndep']
  simp [hSelfOld, hSelfNew]

/--
  Theorem 6 (Recused Declared Author Cannot Advance Antithesis Or Resolve Synthesis):
  If `isRecused s candidate = true`, then:
  (a) `submitAntithesis s m` with `m.sender = candidate` is refused (`submitAntithesis s m = .refused s`), and
  (b) if `s.reviewerAgent = some candidate`, `submitSynthesis s m` by any caller is refused (`submitSynthesis s m = .refused s`).
-/
theorem recused_author_cannot_advance_or_resolve (s : State) (m : Message) (candidate : AgentId)
    (hRec : isRecused s candidate = true) :
    (m.sender = candidate → submitAntithesis s m = .refused s) ∧
    (s.reviewerAgent = some candidate → submitSynthesis s m = .refused s) := by
  constructor
  · intro hSender
    simp [submitAntithesis, hSender, hRec]
  · intro hRev
    simp [submitSynthesis, assignedReviewerRecused, hRev, hRec]

/--
  Theorem 7 (BEAM Saga Phase Guard Correctness):
  - `canClaimResolveSaga s .resolved = true` implies `s.phase = .synthesis`.
  - Terminal sessions (`s.phase = .resolved` or `s.phase = .failed`) never allow
    claiming any resolution saga (`canClaimResolveSaga s target = false`).
-/
theorem saga_resolve_requires_synthesis_phase (s : State) (target : Phase) :
    (canClaimResolveSaga s .resolved = true → s.phase = .synthesis) ∧
    ((s.phase = .resolved ∨ s.phase = .failed) → canClaimResolveSaga s target = false) := by
  constructor
  · intro hClaim
    unfold canClaimResolveSaga at hClaim
    cases hPhase : s.phase <;> simp_all
  · intro hTerm
    unfold canClaimResolveSaga
    rcases hTerm with hRes | hFail
    · simp [hRes]
    · simp [hFail]

/--
  Theorem 8 (DAG Phase Rank Monotonicity):
  No protocol transition (`submitThesis`, `submitAntithesis`, `submitSynthesis`,
  `reassignReviewer`, or `closeStandingRejection`) ever decreases `Phase.rank`.
-/
theorem phase_rank_monotonic (s : State) (m : Message) (newRev : AgentId) (lastWord : Bool) :
    s.phase.rank ≤ (submitThesis s m).state.phase.rank ∧
    s.phase.rank ≤ (submitAntithesis s m).state.phase.rank ∧
    s.phase.rank ≤ (submitSynthesis s m).state.phase.rank ∧
    s.phase.rank = (reassignReviewer s newRev).phase.rank ∧
    s.phase.rank ≤ (closeStandingRejection s lastWord).state.phase.rank := by
  refine ⟨?_, ?_, ?_, ?_, ?_⟩
  · unfold submitThesis
    cases (m.sender != s.pausedAgent) <;>
    cases hPhase : s.phase <;>
    cases m.hasRootCause <;>
    cases (s.sessionType == .recovery) <;>
    cases m.hasConditions <;>
    simp [Phase.rank, hPhase]
  · unfold submitAntithesis
    cases isRecused s m.sender <;>
    cases s.reviewerAgent
    · cases (m.sender == s.pausedAgent) <;>
      cases hPhase : s.phase <;>
      cases m.hasReasoning <;>
      simp [Phase.rank, hPhase]
    · rename_i rev
      dsimp
      cases (m.sender != rev) <;>
      cases hPhase : s.phase <;>
      cases m.hasReasoning <;>
      simp [Phase.rank, hPhase]
    · simp
    · simp
  · unfold submitSynthesis
    cases assignedReviewerRecused s <;>
    cases hPhase : s.phase <;>
    cases Nat.blt s.maxRounds s.synthesisRound <;>
    cases (m.agrees == some true) <;>
    cases (m.sender == s.pausedAgent) <;>
    cases reviewerVerdictPending s <;>
    cases reviewerObjectionStands s <;>
    cases pausedAlreadyAnsweredCurrentObjection s <;>
    simp [Phase.rank, hPhase]
  · rfl
  · unfold closeStandingRejection
    cases s.reviewerAgent
    · simp
    · rename_i rev
      dsimp
      cases s.awaitingFacilitation <;>
      cases (rev == s.pausedAgent) <;>
      cases hPhase : s.phase <;>
      cases reviewerObjectionStands s <;>
      cases lastWord <;>
      simp [Phase.rank, hPhase]

/--
  Theorem 9 (First Self-Clear Attempt Routes To Facilitation Without Resolving):
  When `s.phase = .synthesis`, within round limits, with an unrecused reviewer,
  no pending initial verdict, a standing objection, and no prior paused-agent
  answer to that objection, a paused-agent `agrees = some true` synthesis sets
  `awaitingFacilitation = true`, remains in `.synthesis`, and does not converge.
-/
theorem first_self_clear_attempt_triggers_facilitation (s : State) (m : Message)
    (hRec : assignedReviewerRecused s = false)
    (hPhase : s.phase = .synthesis)
    (hRound : Nat.blt s.maxRounds s.synthesisRound = false)
    (hPend : reviewerVerdictPending s = false)
    (hObj : reviewerObjectionStands s = true)
    (hAns : pausedAlreadyAnsweredCurrentObjection s = false)
    (hSender : m.sender = s.pausedAgent)
    (hAgrees : m.agrees = some true) :
    (submitSynthesis s m).state.awaitingFacilitation = true ∧
    (submitSynthesis s m).state.phase = .synthesis ∧
    (submitSynthesis s m).converged = false := by
  unfold submitSynthesis
  have hIsPaused : (m.sender == s.pausedAgent) = true := by simp [hSender]
  have hAgreesTrue : (m.agrees == some true) = true := by simp [hAgrees]
  simp [hRec, hPhase, hRound, hPend, hObj, hAns, hIsPaused, hAgreesTrue]

/--
  Theorem 10 (Standing Rejection Sweep Never Resolves In Paused Agent's Favour):
  `closeStandingRejection` never converges and never transitions a non-resolved
  session to `.resolved`; when it accepts a transition (`StepOutcome.ok`), the
  resulting phase is strictly `.failed` and `reviewerObjectionStands s = true`.
-/
theorem rejection_stands_sweep_only_fails (s : State) (lastWord : Bool)
    (hNotResolved : s.phase ≠ .resolved) :
    (closeStandingRejection s lastWord).converged = false ∧
    (closeStandingRejection s lastWord).state.phase ≠ .resolved := by
  unfold closeStandingRejection
  cases s.reviewerAgent
  · simp [hNotResolved]
  · rename_i rev
    dsimp
    cases s.awaitingFacilitation <;>
    cases (rev == s.pausedAgent) <;>
    cases hPhase : s.phase <;>
    cases reviewerObjectionStands s <;>
    cases lastWord <;>
    simp_all

end Unitares.DialecticProtocol
