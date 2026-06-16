"""Calibration harness (v1) for the UNITARES governance server.

Purpose
-------
A *synthetic calibration fixture*. It drives the confidence -> prediction_id ->
outcome binding through the governance API under controlled, ground-truth-known
episodes so we can prove the **measurement plumbing** works:

  * the tactical calibration channel populates and its ECE moves, and
  * once injected failures make bad_rate > 0, discrimination (AUC) is computable.

What v1 does NOT claim
----------------------
It does not measure any real agent's calibration. Confidences are *drawn to land
in a target bin* behind a known outcome, so the resulting ECE/AUC describe the
fixture, not a fleet. Read every number here as "the harness can measure," never
"the fleet is well-calibrated." (See report.py header.)

Design corrections baked in from the live-API review (2026-06-16)
-----------------------------------------------------------------
1. Tactical calibration only registers a (confidence, outcome) pair when the
   outcome's ``evidence_weight >= 0.65`` (``GRADE_WEIGHTS[TOOL_OBSERVED]``, the
   server's ``_MIN_TACTICAL_EVIDENCE_WEIGHT``). Below that the row is silently
   dropped from the tactical denominator. So the single-episode gate is
   "tactical bin populated AND evidence_weight == 1.0", not "> 0.1".
2. Quarantine is by **agent_id**, not a provenance tag: ``calibration(check)``
   filters on agent_id and has no locus/agent_class filter. Each harness class
   runs under its own dedicated agent UUID, so the fleet's pool is untouched.
3. ``verification_source`` is the *source*; the corroboration *grade*
   (claim_only ... externally_verified) is derived from it plus structured
   detail. ``external_signal`` short-circuits to externally_verified (weight
   1.00). The grader also includes the real ``exit_code``/``command`` in detail
   so the grade is grounded in tool observation, not just the label.
"""
