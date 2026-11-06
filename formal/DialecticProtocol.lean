/-
  UNITARES Dialectic Protocol & Resolution Authority State Machine
  ================================================================

  Mechanized Lean 4 specification of the Python peer-review dialectic state machine
  (`src/dialectic_protocol.py`), reviewer recusal (`src/dialectic_reviewed_work.py`),
  reviewer reassignment (`src/mcp_handlers/dialectic/resolution_helpers.py`), and
  standing-rejection facilitation exit (`src/mcp_handlers/dialectic/rejection_stands.py`
  and `src/mcp_handlers/dialectic/auto_resolve.py`).

  Pinned to `formal/lean-toolchain` (`leanprover/lean4:v4.35.0`) and verified
  in GitHub Actions (`smoke` job in `.github/workflows/tests.yml`) and via
  `test_lean4_kernel_typechecks_dialectic_protocol` with zero `sorry`s and
  zero user-defined axioms, plus compile-time `#guard` trace vectors parsed
  and cross-checked by `test_lean4_guard_scenarios_match_python_mirror_and_live_session`.
-/

namespace Unitares.DialecticProtocol

/-- Discrete agent identifier (`0..3` for participants/reviewers, `99` for `"system"`). -/
abbrev AgentId := Nat

/-- System audit agent identifier used by `_apply_reviewer_reassignment`. -/
def systemAgentId : AgentId := 99

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
  | resolved
  | failed
  | consult
  deriving DecidableEq, Repr

/-- Map a session `Phase` to the `MsgPhase` recorded on system reassignment messages. -/
@[simp] def Phase.toMsgPhase : Phase → MsgPhase
  | .thesis => .thesis
  | .antithesis => .antithesis
  | .synthesis => .synthesis
  | .resolved => .resolved
  | .failed => .failed

/-- Topological rank of each phase in the protocol DAG. -/
def Phase.rank : Phase → Nat
  | .thesis => 0
  | .antithesis => 1
  | .synthesis => 2
  | .resolved => 3
  | .failed => 3

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

/-- True iff the transition was accepted (`StepOutcome.ok`). -/
@[simp] def accepted : StepOutcome → Bool
  | .ok _ _ => true
  | .refused _ => false

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
  Sender of the most recent independent synthesis verdict (`agrees.isSome` and
  `sender != pausedAgent`), used by `assess_rejection_stands` (`rejection_stands.py:195-205`)
  to verify that the standing rejection was authored by the current `reviewerAgent`
  rather than a reviewer who has since been replaced.
-/
def scanLastIndepVerdictSender (pausedAgent : AgentId) : List Message → Option AgentId
  | [] => none
  | msg :: rest =>
    if (msg.phase == .synthesis) && (msg.sender != pausedAgent) && msg.agrees.isSome then
      some msg.sender
    else
      scanLastIndepVerdictSender pausedAgent rest

def lastIndepVerdictSender (s : State) : Option AgentId :=
  scanLastIndepVerdictSender s.pausedAgent s.transcript.reverse

/--
  True when the reviewer reconsidered a paused-agent reply to that same reviewer's
  earlier synthesis verdict and still rejected (`BASIS_REVIEWER_ANSWERED` in
  `src/mcp_handlers/dialectic/rejection_stands.py:207-224`).
-/
def scanReviewerAnsweredReply (pausedAgent : AgentId) (rev : AgentId) : List Message → Bool :=
  fun revTranscript =>
    -- Walk reversed transcript:
    -- 1. No paused-agent synthesis after `last` (before seeing the latest independent verdict).
    -- 2. At least one paused-agent synthesis between `previous` (the second-to-last independent verdict)
    --    and `last`, and `previous.sender == rev`.
    let rec loop (stage : Nat) (seenPausedBetween : Bool) : List Message → Bool
      | [] => false
      | msg :: rest =>
        if msg.phase != .synthesis then
          loop stage seenPausedBetween rest
        else if msg.sender == pausedAgent then
          if stage == 0 then
            -- A paused-agent reply exists AFTER the latest verdict (`replies_after` non-empty)
            false
          else if stage == 1 then
            loop 1 true rest
          else
            false
        else
          match msg.agrees with
          | none => loop stage seenPausedBetween rest
          | some _ =>
            if stage == 0 then
              -- Latest independent verdict (`last`)
              if msg.sender == rev then
                loop 1 false rest
              else
                false
            else
              -- Second-to-last independent verdict (`previous`)
              (msg.sender == rev) && seenPausedBetween
    loop 0 false revTranscript

def reviewerAnsweredReply (s : State) (rev : AgentId) : Bool :=
  scanReviewerAnsweredReply s.pausedAgent rev s.transcript.reverse

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
  Reviewer reassignment (`handle_reassign_reviewer`, `_validate_explicit_reviewer_candidate`,
  and `_apply_reviewer_reassignment` in `src/mcp_handlers/dialectic/resolution_helpers.py:581-599, 645-715, 1761-1775`).
  - Refuses if `newReviewer == s.pausedAgent` (`SELF_REVIEW`) or `isRecused s newReviewer` (`REVIEWER_AUTHOR_CONFLICT`).
  - Refuses if `s.phase` is not `.thesis` or `.antithesis`, unless `s.awaitingFacilitation`
    is true or `s.phase == .synthesis && assignedReviewerRecused s`.
  - Reopens a terminal (`.resolved` / `.failed`) session that still carries
    `awaitingFacilitation = true` to `.antithesis` (if a thesis is in the transcript)
    or `.thesis` (otherwise).
  - Rewinds a `.synthesis` session with `(awaitingFacilitation || assignedReviewerRecused s)`
    and a self-reviewer or recused reviewer (`oldRev == pausedAgent || isRecused s oldRev`)
    to `.antithesis` so the newly assigned independent reviewer gets an antithesis turn.
  - Appends a system audit message (`sender := systemAgentId`, `agrees := none`)
    and clears `awaitingFacilitation`.
-/
def reassignReviewer (s : State) (newReviewer : AgentId) : StepOutcome :=
  if (newReviewer == s.pausedAgent) || isRecused s newReviewer then
    .refused s
  else
    let phaseAllowed :=
      (s.phase == .thesis) ||
      (s.phase == .antithesis) ||
      s.awaitingFacilitation ||
      ((s.phase == .synthesis) && assignedReviewerRecused s)
    if !phaseAllowed then
      .refused s
    else
      let wasTerminal := (s.phase == .resolved) || (s.phase == .failed)
      let oldSelfOrConflicted :=
        match s.reviewerAgent with
        | none => false
        | some oldRev => (oldRev == s.pausedAgent) || isRecused s oldRev
      let nextPhase :=
        if wasTerminal && s.awaitingFacilitation then
          if s.transcript.any (fun m => m.phase == .thesis) then .antithesis else .thesis
        else if (s.awaitingFacilitation || assignedReviewerRecused s) && oldSelfOrConflicted && (s.phase == .synthesis) then
          .antithesis
        else
          s.phase
      let auditMsg : Message := {
        phase := nextPhase.toMsgPhase,
        sender := systemAgentId,
        hasRootCause := false,
        hasConditions := false,
        hasReasoning := true,
        agrees := none
      }
      .ok { s with
        reviewerAgent := some newReviewer,
        awaitingFacilitation := false,
        phase := nextPhase,
        transcript := s.transcript ++ [auditMsg]
      } false

/--
  Pure assessment predicate for `rejection_stands` (`assess_rejection_stands` in
  `src/mcp_handlers/dialectic/rejection_stands.py:166-260`).
  Note that `assess_rejection_stands` itself does not inspect `s.phase`, but requires:
  - `s.awaitingFacilitation == true`
  - `s.reviewerAgent == some rev` with `rev != s.pausedAgent`
  - `reviewerObjectionStands s == true`
  - `lastIndepVerdictSender s == some rev` (standing rejection belongs to the current reviewer)
  - `reviewerAnsweredReply s rev || (isOrchestrated && windowEnded)`
-/
def assessRejectionStands (s : State) (isOrchestrated : Bool) (windowEnded : Bool) : Bool :=
  if !s.awaitingFacilitation then
    false
  else
    match s.reviewerAgent with
    | none => false
    | some rev =>
      if rev == s.pausedAgent then
        false
      else if !reviewerObjectionStands s then
        false
      else if lastIndepVerdictSender s != some rev then
        false
      else
        reviewerAnsweredReply s rev || (isOrchestrated && windowEnded)

/--
  Sweeper transition closing a facilitation-awaiting session whose rejection stands
  (`_close_standing_rejections` in `src/mcp_handlers/dialectic/auto_resolve.py:540-650`).
  Selects active `synthesis` candidates with an unrecused independent reviewer
  where `assessRejectionStands` holds, transitions `phase` to `.failed`, and
  clears `awaitingFacilitation := false` (`clear_facilitation=True` and
  `_sync_cached_session(session_id, phase="failed", awaiting_facilitation=False)`).
-/
def closeStandingRejection (s : State) (isOrchestrated : Bool) (windowEnded : Bool) : StepOutcome :=
  if s.phase != .synthesis then
    .refused s
  else if assignedReviewerRecused s then
    .refused s
  else if !assessRejectionStands s isOrchestrated windowEnded then
    .refused s
  else
    .ok { s with phase := .failed, awaitingFacilitation := false } false

/--
  Non-protocol advisory `consult` transcript entry (`NON_PROTOCOL_PHASES = {"consult"}`
  in `src/dialectic_protocol.py:207`). Appends a `.consult` message to `s.transcript`
  without altering `phase`, `reviewerAgent`, `synthesisRound`, or `awaitingFacilitation`.
-/
def appendConsult (s : State) (sender : AgentId) : State :=
  let consultMsg : Message := {
    phase := .consult,
    sender := sender,
    hasRootCause := false,
    hasConditions := false,
    hasReasoning := true,
    agrees := none
  }
  { s with transcript := s.transcript ++ [consultMsg] }

/-!
  ## Mechanized Safety & Authority Theorems
-/

/--
  Theorem 1a (Terminal Resolved State Is Absorbing Under Message Submissions):
  Once a session reaches `.resolved`, `submitThesis`, `submitAntithesis`, and
  `submitSynthesis` leave `phase = .resolved`, and `submitThesis` and
  `submitSynthesis` return `.refused s` with the entire `State` (including
  `transcript`) unchanged. (`submitAntithesis` when `reviewerAgent = none` may
  populate `reviewerAgent` before refusing on phase, while keeping `phase = .resolved`
  and `transcript = s.transcript`.)
-/
theorem terminal_resolved_absorbing (s : State) (m : Message)
    (h : s.phase = .resolved) :
    submitThesis s m = .refused s ∧
    (submitAntithesis s m).state.phase = .resolved ∧
    (submitAntithesis s m).state.transcript = s.transcript ∧
    submitSynthesis s m = .refused s := by
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
  · unfold submitAntithesis
    cases isRecused s m.sender
    · cases s.reviewerAgent
      · cases (m.sender == s.pausedAgent) <;> simp [h]
      · rename_i rev
        cases (m.sender != rev) <;> simp [h]
    · simp
  · unfold submitSynthesis
    cases assignedReviewerRecused s <;> simp [h]

/--
  Theorem 1b (Terminal Failed State Is Absorbing Under Message Submissions):
  Once a session reaches `.failed`, `submitThesis`, `submitAntithesis`, and
  `submitSynthesis` leave `phase = .failed`, and `submitThesis` and
  `submitSynthesis` return `.refused s` with the entire `State` unchanged.
-/
theorem terminal_failed_absorbing (s : State) (m : Message)
    (h : s.phase = .failed) :
    submitThesis s m = .refused s ∧
    (submitAntithesis s m).state.phase = .failed ∧
    (submitAntithesis s m).state.transcript = s.transcript ∧
    submitSynthesis s m = .refused s := by
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
  · unfold submitAntithesis
    cases isRecused s m.sender
    · cases s.reviewerAgent
      · cases (m.sender == s.pausedAgent) <;> simp [h]
      · rename_i rev
        cases (m.sender != rev) <;> simp [h]
    · simp
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
  Helper lemma: appending a message with `agrees = none` at the end of a transcript
  prepends it to `transcript.reverse`, and `scanObjectionStands` skips any head
  message with `agrees = none`.
-/
theorem scanObjectionStands_cons_agrees_none (pausedAgent : AgentId) (selfReview : Bool)
    (m : Message) (rest : List Message) (hNone : m.agrees = none) :
    scanObjectionStands pausedAgent selfReview (m :: rest) =
      scanObjectionStands pausedAgent selfReview rest := by
  cases hPhase : (m.phase != .synthesis) <;> simp [scanObjectionStands, hNone, hPhase]

/--
  Theorem 5 (Reviewer Reassignment Preserves Standing Objections & Never Resolves):
  Reassigning a session from an independent reviewer (`r ≠ s.pausedAgent`)
  to any candidate `r'` preserves `reviewerObjectionStands` identically
  (`reviewerObjectionStands (reassignReviewer s r').state = reviewerObjectionStands s` —
  note that `r' = s.pausedAgent` is refused by `reassignReviewer`), never converges
  (`(reassignReviewer s r').converged = false`), and never transitions a non-resolved
  (or facilitation-awaiting) session to `.resolved`.
-/
theorem reassignment_preserves_standing_objection (s : State) (r r' : AgentId)
    (hRev : s.reviewerAgent = some r)
    (hIndep : r ≠ s.pausedAgent) :
    reviewerObjectionStands (reassignReviewer s r').state = reviewerObjectionStands s ∧
    (reassignReviewer s r').converged = false ∧
    (s.phase ≠ .resolved → (reassignReviewer s r').state.phase ≠ .resolved) := by
  refine ⟨?_, ?_, ?_⟩
  · unfold reassignReviewer
    cases hSelfCand : (r' == s.pausedAgent)
    · dsimp
      cases isRecused s r'
      · dsimp
        cases ((s.phase == .thesis) || (s.phase == .antithesis) || s.awaitingFacilitation ||
          ((s.phase == .synthesis) && assignedReviewerRecused s))
        · simp
        · dsimp
          unfold reviewerObjectionStands
          have hSelfOld : (s.reviewerAgent == some s.pausedAgent) = false := by
            rw [hRev]
            simp [hIndep]
          have hSelfNew : (some r' == some s.pausedAgent) = false := by
            simpa using hSelfCand
          simp [hSelfOld, hSelfNew, List.reverse_append, scanObjectionStands]
      · simp
    · simp
  · unfold reassignReviewer
    cases (r' == s.pausedAgent) <;>
    cases isRecused s r' <;>
    cases ((s.phase == .thesis) || (s.phase == .antithesis) || s.awaitingFacilitation ||
      ((s.phase == .synthesis) && assignedReviewerRecused s)) <;>
    rfl
  · intro hNotRes
    unfold reassignReviewer
    cases (r' == s.pausedAgent) <;>
    cases isRecused s r' <;>
    cases hPhase : s.phase <;>
    cases s.awaitingFacilitation <;>
    cases assignedReviewerRecused s <;>
    cases (s.transcript.any (fun m => m.phase == .thesis)) <;>
    cases (match s.reviewerAgent with | none => false | some oldRev => (oldRev == s.pausedAgent) || isRecused s oldRev) <;>
    simp_all

/--
  Theorem 6 (Recused Declared Author Or Paused Agent Cannot Advance Or Be Reassigned):
  If `isRecused s candidate = true`, then:
  (a) `submitAntithesis s m` with `m.sender = candidate` is refused (`submitAntithesis s m = .refused s`),
  (b) if `s.reviewerAgent = some candidate`, `submitSynthesis s m` by any caller is refused (`submitSynthesis s m = .refused s`),
  (c) `reassignReviewer s candidate` is refused (`reassignReviewer s candidate = .refused s`), and
  (d) `reassignReviewer s s.pausedAgent` is refused (`SELF_REVIEW`).
-/
theorem recused_author_cannot_advance_or_resolve (s : State) (m : Message) (candidate : AgentId)
    (hRec : isRecused s candidate = true) :
    (m.sender = candidate → submitAntithesis s m = .refused s) ∧
    (s.reviewerAgent = some candidate → submitSynthesis s m = .refused s) ∧
    reassignReviewer s candidate = .refused s ∧
    reassignReviewer s s.pausedAgent = .refused s := by
  refine ⟨?_, ?_, ?_, ?_⟩
  · intro hSender
    simp [submitAntithesis, hSender, hRec]
  · intro hRev
    simp [submitSynthesis, assignedReviewerRecused, hRev, hRec]
  · simp [reassignReviewer, hRec]
  · simp [reassignReviewer]

/--
  Theorem 7 (Non-Protocol Consult Entries Preserve All Protocol & Authority Predicates):
  Appending an advisory `.consult` transcript entry (`appendConsult s sender`)
  preserves `phase`, `reviewerAgent`, `synthesisRound`, `awaitingFacilitation`,
  `reviewerObjectionStands`, `reviewerVerdictPending`,
  `pausedAlreadyAnsweredCurrentObjection`, `lastIndepVerdictSender`,
  `reviewerAnsweredReply`, and `assessRejectionStands` identically.
-/
theorem consult_preserves_authority_predicates (s : State) (sender : AgentId)
    (rev : AgentId) (isOrch : Bool) (winEnd : Bool) :
    (appendConsult s sender).phase = s.phase ∧
    (appendConsult s sender).reviewerAgent = s.reviewerAgent ∧
    (appendConsult s sender).synthesisRound = s.synthesisRound ∧
    (appendConsult s sender).awaitingFacilitation = s.awaitingFacilitation ∧
    reviewerObjectionStands (appendConsult s sender) = reviewerObjectionStands s ∧
    reviewerVerdictPending (appendConsult s sender) = reviewerVerdictPending s ∧
    pausedAlreadyAnsweredCurrentObjection (appendConsult s sender) =
      pausedAlreadyAnsweredCurrentObjection s ∧
    lastIndepVerdictSender (appendConsult s sender) = lastIndepVerdictSender s ∧
    reviewerAnsweredReply (appendConsult s sender) rev = reviewerAnsweredReply s rev ∧
    assessRejectionStands (appendConsult s sender) isOrch winEnd =
      assessRejectionStands s isOrch winEnd := by
  have hObj : reviewerObjectionStands (appendConsult s sender) = reviewerObjectionStands s := by
    simp [appendConsult, reviewerObjectionStands, List.reverse_append, scanObjectionStands]
  have hPend : reviewerVerdictPending (appendConsult s sender) = reviewerVerdictPending s := by
    unfold appendConsult reviewerVerdictPending
    cases s.reviewerAgent
    · rfl
    · rename_i r
      dsimp
      have hNotAnti : (MsgPhase.consult == MsgPhase.antithesis) = false := rfl
      have hNotSynth : (MsgPhase.consult == MsgPhase.synthesis) = false := rfl
      cases (r == s.pausedAgent) <;> simp [List.any_append, hNotAnti, hNotSynth]
  have hAns : pausedAlreadyAnsweredCurrentObjection (appendConsult s sender) =
      pausedAlreadyAnsweredCurrentObjection s := by
    simp [appendConsult, pausedAlreadyAnsweredCurrentObjection, List.reverse_append, scanAlreadyAnswered]
  have hLast : lastIndepVerdictSender (appendConsult s sender) = lastIndepVerdictSender s := by
    simp [appendConsult, lastIndepVerdictSender, List.reverse_append, scanLastIndepVerdictSender]
  have hRep : ∀ r, reviewerAnsweredReply (appendConsult s sender) r = reviewerAnsweredReply s r := by
    intro r
    simp [appendConsult, reviewerAnsweredReply, scanReviewerAnsweredReply,
      List.reverse_append, scanReviewerAnsweredReply.loop]
  have hAssess : assessRejectionStands (appendConsult s sender) isOrch winEnd =
      assessRejectionStands s isOrch winEnd := by
    unfold assessRejectionStands
    have hPhaseFac : (appendConsult s sender).awaitingFacilitation = s.awaitingFacilitation := rfl
    have hPhaseRev : (appendConsult s sender).reviewerAgent = s.reviewerAgent := rfl
    have hPhasePaused : (appendConsult s sender).pausedAgent = s.pausedAgent := rfl
    rw [hPhaseFac, hPhaseRev, hPhasePaused, hObj, hLast]
    cases s.awaitingFacilitation <;> cases s.reviewerAgent <;> simp [hRep]
  exact ⟨rfl, rfl, rfl, rfl, hObj, hPend, hAns, hLast, hRep rev, hAssess⟩

/--
  Theorem 8 (DAG Phase Rank Monotonicity & Controlled Reassignment Reopen):
  - Message submissions (`submitThesis`, `submitAntithesis`, `submitSynthesis`)
    and standing-rejection sweeps (`closeStandingRejection`) never decrease `Phase.rank`.
  - `reassignReviewer` preserves `Phase.rank` whenever `s.awaitingFacilitation = false`
    and `assignedReviewerRecused s = false`; it only lowers `Phase.rank` when
    reopening a facilitation-awaiting or recused-reviewer session to `.thesis` or `.antithesis`.
-/
theorem phase_rank_monotonic (s : State) (m : Message) (newRev : AgentId)
    (isOrch : Bool) (winEnd : Bool) :
    s.phase.rank ≤ (submitThesis s m).state.phase.rank ∧
    s.phase.rank ≤ (submitAntithesis s m).state.phase.rank ∧
    s.phase.rank ≤ (submitSynthesis s m).state.phase.rank ∧
    s.phase.rank ≤ (closeStandingRejection s isOrch winEnd).state.phase.rank ∧
    ((s.awaitingFacilitation = false ∧ assignedReviewerRecused s = false) →
      (reassignReviewer s newRev).state.phase.rank = s.phase.rank) := by
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
  · unfold closeStandingRejection
    cases hPhase : s.phase <;>
    cases assignedReviewerRecused s <;>
    cases assessRejectionStands s isOrch winEnd <;>
    simp [Phase.rank, hPhase]
  · rintro ⟨hNoFac, hNoRec⟩
    unfold reassignReviewer
    rw [hNoFac, hNoRec]
    cases (newRev == s.pausedAgent) <;>
    cases isRecused s newRev <;>
    cases hPhase : s.phase <;>
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
  Theorem 10 (Standing Rejection Sweep Only Transitions To Failed Over Standing Objection
  And Clears Facilitation):
  - `closeStandingRejection` never converges (`converged = false`).
  - If `s.phase ≠ .resolved`, `(closeStandingRejection s isOrch winEnd).state.phase ≠ .resolved`.
  - Whenever `closeStandingRejection s isOrch winEnd = .ok next c`, we have
    `next.phase = .failed`, `next.awaitingFacilitation = false`, `s.phase = .synthesis`,
    `s.awaitingFacilitation = true`, `reviewerObjectionStands s = true`, and
    `lastIndepVerdictSender s = s.reviewerAgent`.
-/
theorem rejection_stands_sweep_only_fails (s : State) (isOrch : Bool) (winEnd : Bool) :
    (closeStandingRejection s isOrch winEnd).converged = false ∧
    (s.phase ≠ .resolved → (closeStandingRejection s isOrch winEnd).state.phase ≠ .resolved) ∧
    (∀ next c, closeStandingRejection s isOrch winEnd = .ok next c →
      next.phase = .failed ∧
      next.awaitingFacilitation = false ∧
      s.phase = .synthesis ∧
      s.awaitingFacilitation = true ∧
      reviewerObjectionStands s = true ∧
      lastIndepVerdictSender s = s.reviewerAgent) := by
  refine ⟨?_, ?_, ?_⟩
  · unfold closeStandingRejection
    cases (s.phase != .synthesis) <;>
    cases assignedReviewerRecused s <;>
    cases assessRejectionStands s isOrch winEnd <;>
    rfl
  · intro hNotRes
    unfold closeStandingRejection
    cases hPhase : s.phase <;>
    cases assignedReviewerRecused s <;>
    cases assessRejectionStands s isOrch winEnd <;>
    simp_all
  · intro next c hOk
    cases next
    unfold closeStandingRejection assessRejectionStands at hOk
    cases hRev : s.reviewerAgent
    · cases hPhase : s.phase <;>
      cases hRec : assignedReviewerRecused s <;>
      cases hFac : s.awaitingFacilitation <;>
      simp_all
    · rename_i rev
      cases hPhase : s.phase <;>
      cases hRec : assignedReviewerRecused s <;>
      cases hFac : s.awaitingFacilitation <;>
      cases hSelf : (rev == s.pausedAgent) <;>
      cases hObj : reviewerObjectionStands s <;>
      cases hLast : (lastIndepVerdictSender s != some rev) <;>
      cases hAns : reviewerAnsweredReply s rev <;>
      cases isOrch <;>
      cases winEnd <;>
      simp_all

/-!
  ## Compile-Time `#guard` Conformance Vectors
  Parsed by `tests/test_dialectic_protocol_state_machine.py` to cross-check
  these `#guard` trace vectors against `SpecState` and the live Python
  implementation (while the Lean 4 kernel checks the theorems and `#guard`s
  when `lean` is installed).
-/

def mkInitState (paused : AgentId) (reviewer : Option AgentId)
    (authors : List AgentId) (st : SessionType) (maxR : Nat) : State :=
  { pausedAgent := paused
  , reviewerAgent := reviewer
  , declaredAuthors := authors
  , sessionType := st
  , maxRounds := maxR
  , phase := .thesis
  , synthesisRound := 0
  , awaitingFacilitation := false
  , transcript := [] }

-- Scenario 1: Paused agent submits thesis, independent reviewer submits antithesis
-- and rejects (`agrees = some false`), paused agent attempts self-clear (`agrees = some true`)
-- -> routed to facilitation (`awaitingFacilitation = true`, `phase = .synthesis`),
-- repeat self-clear is refused without growing transcript, standing-rejection sweep
-- after window end closes session as `.failed` with `awaitingFacilitation = false`,
-- and a subsequent `reassignReviewer` is refused because `awaitingFacilitation` is cleared.
def traceScenario1 : State :=
  let s0 := mkInitState 0 (some 1) [] .recovery 3
  let s1 := (submitThesis s0 { phase := .thesis, sender := 0 }).state
  let s2 := (submitAntithesis s1 { phase := .antithesis, sender := 1 }).state
  let s3 := (submitSynthesis s2 { phase := .synthesis, sender := 1, agrees := some false }).state
  let s4 := (submitSynthesis s3 { phase := .synthesis, sender := 0, agrees := some true }).state
  let s5 := (submitSynthesis s4 { phase := .synthesis, sender := 0, agrees := some true }).state
  let s6 := (closeStandingRejection s5 true true).state
  let s7 := (reassignReviewer s6 2).state
  s7

#guard traceScenario1.phase == .failed
#guard traceScenario1.reviewerAgent == some 1
#guard traceScenario1.awaitingFacilitation == false
#guard traceScenario1.synthesisRound == 3
#guard traceScenario1.transcript.length == 4
#guard reviewerObjectionStands traceScenario1 == true

-- Scenario 2: A session that raises `awaitingFacilitation = true` and then transitions
-- to `.failed` on `maxRounds` exhaustion (`maxRounds = 2`, `synthesisRound = 3 > 2`)
-- retains `awaitingFacilitation = true`; `reassignReviewer` reopens that `.failed`
-- session to `.antithesis`, clears `awaitingFacilitation = false`, preserves
-- `reviewerObjectionStands`, and prevents `assessRejectionStands` from firing
-- before the newly assigned reviewer speaks (`lastIndepVerdictSender != some 2`).
def traceScenario2 : State :=
  let s0 := mkInitState 0 (some 1) [] .recovery 2
  let s1 := (submitThesis s0 { phase := .thesis, sender := 0 }).state
  let s2 := (submitAntithesis s1 { phase := .antithesis, sender := 1 }).state
  let s3 := (submitSynthesis s2 { phase := .synthesis, sender := 1, agrees := some false }).state
  let s4 := (submitSynthesis s3 { phase := .synthesis, sender := 0, agrees := some true }).state
  let s5 := (submitSynthesis s4 { phase := .synthesis, sender := 0, agrees := none }).state
  let s6 := (reassignReviewer s5 2).state
  s6

#guard traceScenario2.phase == .antithesis
#guard traceScenario2.reviewerAgent == some 2
#guard traceScenario2.awaitingFacilitation == false
#guard traceScenario2.transcript.length == 5
#guard reviewerObjectionStands traceScenario2 == true
#guard assessRejectionStands traceScenario2 true true == false

-- Scenario 3: Declared work author (`2 ∈ declaredAuthors`) and paused agent (`0`)
-- are refused on `submitAntithesis` and `reassignReviewer`, while independent agent `3` succeeds.
def traceScenario3 : State :=
  let s0 := mkInitState 0 none [2] .review 3
  let s1 := (submitThesis s0 { phase := .thesis, sender := 0 }).state
  let s2 := (submitAntithesis s1 { phase := .antithesis, sender := 2 }).state
  let s3 := (reassignReviewer s2 2).state
  let s4 := (reassignReviewer s3 0).state
  let s5 := (submitAntithesis s4 { phase := .antithesis, sender := 3 }).state
  s5

#guard traceScenario3.phase == .synthesis
#guard traceScenario3.reviewerAgent == some 3
#guard traceScenario3.transcript.length == 2

-- Scenario 4: Thesis content validation refusal (`hasRootCause = false` or
-- `hasConditions = false` in `.recovery`), `reviewerVerdictPending == true`
-- after independent antithesis blocking premature self-clear, `appendConsult`
-- preserving all predicates, and `reviewerAnsweredReply` (`stage = 1` branch)
-- allowing `closeStandingRejection` even when `isOrchestrated = false` and `windowEnded = false`.
def traceScenario4 : State :=
  let s0 := mkInitState 0 (some 1) [] .recovery 4
  let s1 := (submitThesis s0 { phase := .thesis, sender := 0, hasRootCause := false, hasConditions := true }).state
  let s2 := (submitThesis s1 { phase := .thesis, sender := 0, hasRootCause := true, hasConditions := false }).state
  let s3 := (submitThesis s2 { phase := .thesis, sender := 0, hasRootCause := true, hasConditions := true }).state
  let s4 := (submitAntithesis s3 { phase := .antithesis, sender := 1 }).state
  let s5 := (submitSynthesis s4 { phase := .synthesis, sender := 0, agrees := some true }).state
  let s6 := appendConsult s5 2
  let s7 := (submitSynthesis s6 { phase := .synthesis, sender := 1, agrees := some false }).state
  let s8 := (submitSynthesis s7 { phase := .synthesis, sender := 0, agrees := some true }).state
  let s9 := (submitSynthesis s8 { phase := .synthesis, sender := 1, agrees := some false }).state
  let s10 := (closeStandingRejection s9 false false).state
  s10

#guard traceScenario4.phase == .failed
#guard traceScenario4.awaitingFacilitation == false
#guard traceScenario4.synthesisRound == 4
#guard traceScenario4.transcript.length == 6
#guard reviewerObjectionStands traceScenario4 == true

end Unitares.DialecticProtocol
