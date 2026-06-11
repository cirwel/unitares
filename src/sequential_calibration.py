"""
Sequential calibration evidence for hard exogenous tactical outcomes.

This module tracks an anytime-valid e-process against the null that each
reported confidence value matches the Bernoulli rate of an observed hard
exogenous outcome. It is intentionally narrow:

- tactical only (decision-time binary outcomes)
- exogenous only (tests, commands, files, lint, tool-result evidence)
- observational only (no governance coupling here)

We expose a bounded alarm transform for operator use and keep the raw
e-process internal to the tracker.

Null and construction
---------------------
- Null H0: for each eligible sample n, Y_n ~ Bernoulli(p_n) where p_n is
  the confidence reported by the agent before the outcome was observable.
  This is a sequential composite null — p_n varies across samples — not a
  single fixed Bernoulli.
- Alternative: Beta-Bernoulli predictive plug-in. q_n is the posterior
  mean of the success rate after n-1 observations, using a Beta(prior_success,
  prior_failure) prior (default Beta(1, 1)).
- Per-sample e-value: e_n = (q_n / p_n) if Y_n = 1 else ((1 - q_n) / (1 - p_n)).
  Clamped to avoid degenerate 0/1 confidences.
- q_n is computed from the state *before* the current sample is folded in,
  making the bet F_{n-1}-measurable. Under H0, E[e_n | F_{n-1}] = 1, so the
  running product is a nonnegative martingale with mean 1 and the cumulative
  log is a valid e-process for anytime-valid testing.
- When at least one eligible sample exists, the exposed alarm metric is
  capped_alarm = 1 - exp(-max(0, log_e_value)), which lives in [0, 1).
  log_evidence is similarly clamped at 0 from below so favorable trajectories
  do not produce negative alarms. No-data envelopes deliberately omit those
  e-process fields so consumers cannot read calibration starvation as a
  healthy zero alarm. Raw e-values remain internal to the tracker and are not
  exposed as governance state.

Known limitations
-----------------
- The prediction_id seam is operational at the outcome_event tool level:
  register_tactical_prediction (governance_monitor) mints; outcome_event consumes
  via consume_prediction. The remaining gap was the report path — closed by the
 Refined Phase-5 Evidence Contract,
  which adds recent_tool_results to process_agent_update and emits outcome_event
  server-side per item with verification_source="agent_reported_tool_result".
- Global and per-agent trackers update from the same samples. Each is
  individually a valid e-process under H0, but they are correlated by
  construction and must not be multiplied together.
- prior_success / prior_failure are constructor parameters but are not
  wired to configuration in v1. Defaults (Beta(1, 1)) are intentional and
  should not be tuned without also reviewing downstream alarm thresholds.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Optional
import fcntl
import json
import math
import os
import sys
import tempfile
from datetime import datetime, UTC

from config.governance_config import GovernanceConfig

# S10: the bucket name used when a write arrives without a class_tag, or when
# the classifier cannot resolve a class for an agent_id during a rebucket pass.
# Surfaced as a first-class row in compute_metrics_by_class so calibration
# starvation in the unclassified band reads as a deficit signal, not a silent
# fold into "global."
UNKNOWN_CLASS_BUCKET = "unknown"


def _merge_state(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    """Fold src counters into dst in place. Used by rebucket_from_agent_states
    to recompose class_states from per-agent slices.

    Sums extensive quantities (samples, successes, confidence_sum, log_e_value);
    forwards the most-recent last_* fields by timestamp; merges signal_sources
    and signal_source_outcomes additively. Last-value semantics for last_e_value
    and last_alt_probability are best-effort under aggregation — the e-process
    martingale property holds per-state, not across folded states.
    """
    dst["eligible_samples"] = int(dst.get("eligible_samples", 0)) + int(src.get("eligible_samples", 0))
    dst["successes"] = int(dst.get("successes", 0)) + int(src.get("successes", 0))
    dst["confidence_sum"] = float(dst.get("confidence_sum", 0.0)) + float(src.get("confidence_sum", 0.0))
    dst["log_e_value"] = float(dst.get("log_e_value", 0.0)) + float(src.get("log_e_value", 0.0))

    src_ts = src.get("last_updated")
    dst_ts = dst.get("last_updated")
    if src_ts and (dst_ts is None or src_ts > dst_ts):
        dst["last_updated"] = src_ts
        dst["last_e_value"] = float(src.get("last_e_value", dst.get("last_e_value", 1.0)))
        dst["last_alt_probability"] = float(
            src.get("last_alt_probability", dst.get("last_alt_probability", 0.5))
        )

    dst_sources = dst.setdefault("signal_sources", {})
    for k, v in (src.get("signal_sources") or {}).items():
        dst_sources[k] = int(dst_sources.get(k, 0)) + int(v)

    dst_outcomes = dst.setdefault("signal_source_outcomes", {})
    for channel, counts in (src.get("signal_source_outcomes") or {}).items():
        merged = dst_outcomes.setdefault(channel, {"samples": 0, "successes": 0})
        merged["samples"] = int(merged.get("samples", 0)) + int(counts.get("samples", 0))
        merged["successes"] = int(merged.get("successes", 0)) + int(counts.get("successes", 0))


def _empty_state() -> Dict[str, Any]:
    return {
        "eligible_samples": 0,
        "successes": 0,
        "confidence_sum": 0.0,
        "log_e_value": 0.0,
        "last_e_value": 1.0,
        "last_alt_probability": 0.5,
        "signal_sources": {},
        # Per-channel hygiene tracking — {channel: {samples: int, successes: int}}
        # Used by compute_per_channel_health to flag bad_rate_pinned_to_zero.
        "signal_source_outcomes": {},
        "last_updated": None,
    }


class SequentialCalibrationTracker:
    """Track exogenous tactical evidence with a predictable Bernoulli e-process."""

    def __init__(
        self,
        state_file: Path | None = None,
        *,
        prior_success: float = 1.0,
        prior_failure: float = 1.0,
    ):
        if state_file is None:
            state_file = Path(__file__).parent.parent / "data" / "sequential_calibration_state.json"
        self.state_file = Path(state_file)
        self.prior_success = float(prior_success)
        self.prior_failure = float(prior_failure)
        self.load_state()

    @contextmanager
    def _state_file_lock(self, *, exclusive: bool):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_file.with_name(f"{self.state_file.name}.lock")
        with open(lock_path, "a+") as lock_file:
            lock_mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(lock_file.fileno(), lock_mode)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def reset(self) -> None:
        self.global_state = _empty_state()
        self.agent_states = defaultdict(_empty_state)
        # S10: denormalized per-class rollup. Writes update class_states alongside
        # global_state and agent_states; reads via compute_metrics_by_class are O(1)
        # per class. Authoritative counters remain in agent_states — class_states is
        # a read cache that must be rebuilt via rebucket_from_agent_states when
        # class membership shifts (S8a promotion sweeps, manual re-tagging).
        self.class_states = defaultdict(_empty_state)
        # S10 bootstrap flag: True once class_states is known to represent the
        # full agent_states corpus (either because it was populated by live
        # writes only — a fresh epoch — or because rebucket_from_agent_states
        # has been run at least once). False indicates pre-S10 file load where
        # class_states is sparse relative to agent_states/global_state. Surfaced
        # in compute_metrics_by_class so dashboards/MCP consumers can label the
        # window honestly instead of showing a misleading-by-class breakdown.
        self.class_states_bootstrapped = True

    def _serialize(self) -> Dict[str, Any]:
        return {
            "global": dict(self.global_state),
            "agents": {agent_id: dict(state) for agent_id, state in self.agent_states.items()},
            # S10: additive field. Tracker state files predating S10 will load
            # cleanly (load_state defaults to empty class_states); the first
            # rebucket pass repopulates from agent_states.
            "classes": {class_tag: dict(state) for class_tag, state in self.class_states.items()},
            "class_states_bootstrapped": self.class_states_bootstrapped,
            "prior_success": self.prior_success,
            "prior_failure": self.prior_failure,
            "epoch": GovernanceConfig.CURRENT_EPOCH,
        }

    def _save_state_unlocked(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.state_file.parent),
            prefix=f".{self.state_file.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._serialize(), f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.state_file)
            self._loaded_mtime = self._file_mtime()
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def save_state(self) -> None:
        try:
            with self._state_file_lock(exclusive=True):
                self._save_state_unlocked()
        except Exception as e:
            print(f"Warning: Failed to save sequential calibration state: {e}", file=sys.stderr)

    def _file_mtime(self) -> float:
        try:
            return self.state_file.stat().st_mtime if self.state_file.exists() else 0.0
        except OSError:
            return 0.0

    def _reload_if_stale(self) -> None:
        """Reload from disk if the file was updated externally (e.g. by backfill)."""
        current_mtime = self._file_mtime()
        if current_mtime > getattr(self, "_loaded_mtime", 0.0):
            self.load_state()

    def _reload_if_stale_unlocked(self) -> None:
        current_mtime = self._file_mtime()
        if current_mtime > getattr(self, "_loaded_mtime", 0.0):
            self._load_state_unlocked()

    def _load_state_unlocked(self) -> None:
        try:
            if not self.state_file.exists():
                self.reset()
                self._loaded_mtime = 0.0
                return
            with open(self.state_file, "r") as f:
                data = json.load(f)

            # Epoch migration: when the truth-channel definition changes (or any
            # other governance epoch bump), historical state is no longer
            # comparable. Archive and reset rather than silently reinterpret.
            file_epoch = int(data.get("epoch", 1))
            if file_epoch != GovernanceConfig.CURRENT_EPOCH:
                archive_path = self.state_file.with_suffix(f".bak.epoch{file_epoch}")
                try:
                    self.state_file.rename(archive_path)
                except FileNotFoundError:
                    # Concurrent process already migrated; safe to no-op.
                    pass
                print(
                    f"Calibration epoch changed ({file_epoch} → {GovernanceConfig.CURRENT_EPOCH}); "
                    f"archived prior state to {archive_path}",
                    file=sys.stderr,
                )
                self.reset()
                self._loaded_mtime = 0.0
                return

            self.global_state = _empty_state()
            self.global_state.update(data.get("global", {}))

            self.agent_states = defaultdict(_empty_state)
            for agent_id, state in data.get("agents", {}).items():
                restored = _empty_state()
                restored.update(state or {})
                self.agent_states[agent_id] = restored

            # S10: load class_states if present; absent for pre-S10 state files,
            # in which case the next rebucket pass will repopulate from agent_states.
            self.class_states = defaultdict(_empty_state)
            classes_data = data.get("classes")
            for class_tag, state in (classes_data or {}).items():
                restored = _empty_state()
                restored.update(state or {})
                self.class_states[class_tag] = restored

            # S10 bootstrap flag. Honest gap labeling for the window between
            # pre-S10 file load and the first rebucket run. If the file lacks
            # the flag AND has no `classes` key AND has non-empty `agents`,
            # this is a pre-S10 file with history that class_states does not
            # represent — bootstrap is required.
            if "class_states_bootstrapped" in data:
                self.class_states_bootstrapped = bool(data["class_states_bootstrapped"])
            elif classes_data is None and data.get("agents"):
                self.class_states_bootstrapped = False
            else:
                self.class_states_bootstrapped = True
            self._loaded_mtime = self._file_mtime()
        except Exception as e:
            print(f"Warning: Failed to load sequential calibration state: {e}, resetting", file=sys.stderr)
            self.reset()
            self._loaded_mtime = 0.0

    def load_state(self) -> None:
        try:
            with self._state_file_lock(exclusive=True):
                self._load_state_unlocked()
        except Exception as e:
            print(f"Warning: Failed to load sequential calibration state: {e}, resetting", file=sys.stderr)
            self.reset()
            self._loaded_mtime = 0.0

    @staticmethod
    def _clamp_probability(value: float) -> float:
        return min(1.0 - 1e-6, max(1e-6, float(value)))

    def _predictive_alt_probability(self, state: Dict[str, Any]) -> float:
        total = float(state["eligible_samples"])
        successes = float(state["successes"])
        q = (self.prior_success + successes) / (self.prior_success + self.prior_failure + total)
        return self._clamp_probability(q)

    def _update_state(
        self,
        state: Dict[str, Any],
        *,
        confidence: float,
        outcome_correct: bool,
        signal_source: str,
        timestamp: str,
    ) -> Dict[str, float]:
        # Betting martingale step. See module docstring for the null and
        # construction. q is computed from the pre-update state to preserve
        # F_{n-1}-measurability; the state increments happen after e_value.
        p = self._clamp_probability(confidence)
        y = 1.0 if outcome_correct else 0.0
        q = self._predictive_alt_probability(state)
        e_value = (q / p) if y == 1.0 else ((1.0 - q) / (1.0 - p))
        e_value = max(e_value, 1e-12)

        state["eligible_samples"] += 1
        state["successes"] += int(y)
        state["confidence_sum"] += p
        state["log_e_value"] += math.log(e_value)
        state["last_e_value"] = e_value
        state["last_alt_probability"] = q
        state["last_updated"] = timestamp

        signal_sources = state.setdefault("signal_sources", {})
        signal_sources[signal_source] = int(signal_sources.get(signal_source, 0)) + 1

        # Per-channel sample/success tracking for the hygiene guard
        # (bad_rate_pinned_to_zero in compute_per_channel_health).
        source_outcomes = state.setdefault("signal_source_outcomes", {})
        ch_outcomes = source_outcomes.setdefault(signal_source, {"samples": 0, "successes": 0})
        ch_outcomes["samples"] = int(ch_outcomes.get("samples", 0)) + 1
        if y == 1.0:
            ch_outcomes["successes"] = int(ch_outcomes.get("successes", 0)) + 1

        return {
            "p": p,
            "q": q,
            "e_value": e_value,
            "log_e_value": state["log_e_value"],
        }

    def _record_exogenous_tactical_outcome_in_memory(
        self,
        *,
        confidence: float,
        outcome_correct: bool,
        agent_id: Optional[str] = None,
        class_tag: Optional[str] = None,
        signal_source: str,
        decision_action: Optional[str] = None,
        outcome_type: Optional[str] = None,
        timestamp: Optional[str] = None,
        prediction_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not signal_source:
            raise ValueError("signal_source is required")

        ts = timestamp or datetime.now(UTC).isoformat()

        global_update = self._update_state(
            self.global_state,
            confidence=confidence,
            outcome_correct=outcome_correct,
            signal_source=signal_source,
            timestamp=ts,
        )

        agent_update = None
        if agent_id:
            agent_update = self._update_state(
                self.agent_states[agent_id],
                confidence=confidence,
                outcome_correct=outcome_correct,
                signal_source=signal_source,
                timestamp=ts,
            )

        # S10: write-through to the class bucket. UNKNOWN_CLASS_BUCKET when the
        # caller did not pass a class_tag (rather than skipping entirely) keeps
        # the class breakdown lossless against the global rollup.
        bucket = class_tag or UNKNOWN_CLASS_BUCKET
        class_update = self._update_state(
            self.class_states[bucket],
            confidence=confidence,
            outcome_correct=outcome_correct,
            signal_source=signal_source,
            timestamp=ts,
        )

        return {
            "agent_id": agent_id,
            "class_tag": bucket,
            "prediction_id": prediction_id,
            "decision_action": decision_action,
            "outcome_type": outcome_type,
            "signal_source": signal_source,
            "global": global_update,
            "agent": agent_update,
            "class": class_update,
        }

    def record_exogenous_tactical_outcome(
        self,
        *,
        confidence: float,
        outcome_correct: bool,
        agent_id: Optional[str] = None,
        class_tag: Optional[str] = None,
        signal_source: str,
        decision_action: Optional[str] = None,
        outcome_type: Optional[str] = None,
        timestamp: Optional[str] = None,
        prediction_id: Optional[str] = None,
        persist: bool = True,
    ) -> Dict[str, Any]:
        """Record one eligible hard exogenous tactical outcome.

        prediction_id, if provided, is included in the return payload for
        forensic audit. The tracker state itself remains aggregate and is
        not indexed by prediction_id.

        class_tag, when provided, drives the parallel class_states rollup
        (S10). Callers should pass the agent's resolved class (per
        src/grounding/class_indicator.py::classify_agent). Omitted writes
        bucket into UNKNOWN_CLASS_BUCKET so the by-class view surfaces
        calibration starvation in the unclassified band as a deficit signal.
        """
        if persist:
            with self._state_file_lock(exclusive=True):
                self._reload_if_stale_unlocked()
                result = self._record_exogenous_tactical_outcome_in_memory(
                    confidence=confidence,
                    outcome_correct=outcome_correct,
                    agent_id=agent_id,
                    class_tag=class_tag,
                    signal_source=signal_source,
                    decision_action=decision_action,
                    outcome_type=outcome_type,
                    timestamp=timestamp,
                    prediction_id=prediction_id,
                )
                self._save_state_unlocked()
                return result

        return self._record_exogenous_tactical_outcome_in_memory(
            confidence=confidence,
            outcome_correct=outcome_correct,
            agent_id=agent_id,
            class_tag=class_tag,
            signal_source=signal_source,
            decision_action=decision_action,
            outcome_type=outcome_type,
            timestamp=timestamp,
            prediction_id=prediction_id,
        )

    def _drop_agent_state_in_memory(self, agent_id: str) -> bool:
        if not agent_id or agent_id not in self.agent_states:
            return False

        del self.agent_states[agent_id]
        # class_states is a derived read cache over agent_states. Once an agent
        # slice is pruned, the old class rollup may still contain that slice, so
        # clear it and force the existing rebucket sweeper to rebuild honestly.
        self.class_states = defaultdict(_empty_state)
        self.class_states_bootstrapped = False
        return True

    def drop_agent_state(self, agent_id: str, *, persist: bool = True) -> bool:
        """Drop per-agent calibration state when lifecycle archival succeeds.

        Global evidence remains intact. Per-agent slices are bounded by the
        lifecycle archive path; class rollups are cleared because they are
        derived from agent_states and must be rebuilt after a prune.
        """
        if persist:
            with self._state_file_lock(exclusive=True):
                self._load_state_unlocked()
                changed = self._drop_agent_state_in_memory(agent_id)
                if changed:
                    self._save_state_unlocked()
                return changed

        return self._drop_agent_state_in_memory(agent_id)

    def compute_per_channel_health(self, min_samples_for_pin: int = 100) -> Dict[str, Dict[str, Any]]:
        """
        Reporting-hygiene check on per-channel outcome stream.

        A channel "pinned to zero" means it has accumulated enough samples to
        be diagnostic but every observed outcome was a success — exactly the
        pathology the broadened truth channel was meant to escape. Sentinel
        can subscribe to this and raise an anomaly when a previously-non-zero
        channel pins.

        Args:
            min_samples_for_pin: minimum samples before pinned flag can fire.
        """
        out: Dict[str, Dict[str, Any]] = {}
        source_outcomes = self.global_state.get("signal_source_outcomes", {})
        for channel, counts in source_outcomes.items():
            samples = int(counts.get("samples", 0))
            successes = int(counts.get("successes", 0))
            bad_rate = 0.0 if samples == 0 else (samples - successes) / samples
            pinned = (samples >= min_samples_for_pin) and (bad_rate == 0.0)
            out[channel] = {
                "samples": samples,
                "successes": successes,
                "bad_rate": bad_rate,
                "bad_rate_pinned_to_zero": pinned,
            }
        return out

    def _state_to_metrics(
        self,
        state: Optional[Dict[str, Any]],
        *,
        scope: str,
        agent_id: Optional[str] = None,
        class_tag: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Render one state dict (global / agent_states[id] / class_states[tag])
        into the operator-facing metrics payload.

        ANYTIME-VALIDITY SCOPE (S10 council finding):
        - scope="global" / scope="agent" expose the full e-process envelope
          once at least one eligible sample exists, including `log_evidence`,
          `capped_alarm`, `last_alt_probability` — each is a single coherent
          filtration with valid martingale guarantees. No-data envelopes omit
          these fields so a missing signal cannot be mistaken for a healthy
          zero alarm.
        - scope="class" deliberately omits those fields. A class bucket is
          either (a) a parallel e-process under live writes — valid in
          isolation but not multipliable against global/agent (same warning
          the module docstring at the top of this file gives) — or (b) a
          rebucketed sum of agent log_e_values with different q-trajectories,
          which has no martingale interpretation. Rather than expose a field
          that is sometimes anytime-valid and sometimes not, the class
          envelope is restricted to descriptive statistics that survive
          aggregation: eligible_samples, mean_confidence, empirical_accuracy,
          calibration_gap, signal_sources, last_updated.
        """
        is_class_scope = scope == "class"

        empty_envelope: Dict[str, Any] = {
            "status": "no_data",
            "eligible_samples": 0,
            "scope": scope,
            "signal_sources": {},
        }
        if agent_id is not None:
            empty_envelope["agent_id"] = agent_id
        if class_tag is not None:
            empty_envelope["class_tag"] = class_tag

        if not state:
            return empty_envelope

        total = int(state["eligible_samples"])

        if total == 0:
            empty_envelope["signal_sources"] = dict(state.get("signal_sources", {}))
            return empty_envelope

        mean_confidence = float(state["confidence_sum"]) / total
        empirical_accuracy = float(state["successes"]) / total
        calibration_gap = empirical_accuracy - mean_confidence

        payload: Dict[str, Any] = {
            "status": "tracking",
            "scope": scope,
            "eligible_samples": total,
            "mean_confidence": round(mean_confidence, 4),
            "empirical_accuracy": round(empirical_accuracy, 4),
            "calibration_gap": round(calibration_gap, 4),
            "signal_sources": dict(state.get("signal_sources", {})),
            "last_updated": state.get("last_updated"),
        }
        if not is_class_scope:
            positive_log = max(0.0, float(state["log_e_value"]))
            payload["log_evidence"] = round(positive_log, 4)
            payload["capped_alarm"] = round(1.0 - math.exp(-positive_log), 4)
            payload["last_alt_probability"] = round(float(state["last_alt_probability"]), 4)
        if agent_id is not None:
            payload["agent_id"] = agent_id
        if class_tag is not None:
            payload["class_tag"] = class_tag
        return payload

    def compute_metrics(self, agent_id: Optional[str] = None) -> Dict[str, Any]:
        """Return bounded, operator-friendly metrics for the tracked e-process."""
        self._reload_if_stale()
        if agent_id:
            return self._state_to_metrics(
                self.agent_states.get(agent_id),
                scope="agent",
                agent_id=agent_id,
            )
        return self._state_to_metrics(self.global_state, scope="global")

    def compute_metrics_by_class(self) -> Dict[str, Any]:
        """S10: per-class rollup keyed by class_tag (substrate / session_like /
        engaged_ephemeral / ephemeral / UNKNOWN_CLASS_BUCKET).

        Returns an envelope:
            {
                "bootstrapped": bool,
                "by_class": {class_tag: descriptive_metrics, ...},
            }

        `bootstrapped=False` signals that class_states was loaded from a pre-S10
        state file and has not been reconciled against agent_states yet —
        consumers (dashboard, MCP responses) should label the by-class breakdown
        as a sparse bootstrap view rather than a fleet-representative summary.
        First successful rebucket_from_agent_states call flips this to True.

        Each by_class value carries only descriptive statistics
        (eligible_samples, mean_confidence, empirical_accuracy, calibration_gap,
        signal_sources, last_updated). The e-process fields (log_evidence,
        capped_alarm, last_alt_probability) are deliberately omitted at class
        scope — see _state_to_metrics docstring for the anytime-validity
        rationale.
        """
        self._reload_if_stale()
        return {
            "bootstrapped": bool(getattr(self, "class_states_bootstrapped", True)),
            "by_class": {
                class_tag: self._state_to_metrics(state, scope="class", class_tag=class_tag)
                for class_tag, state in self.class_states.items()
            },
        }

    def rebucket_from_agent_states(
        self,
        classifier: Callable[[str], Optional[str]],
        *,
        persist: bool = True,
    ) -> Dict[str, Any]:
        """S10: rebuild class_states from agent_states by re-asking the classifier
        for each tracked agent_id.

        classifier(agent_id) returns the current class_tag (or None to bucket the
        agent's counters into UNKNOWN_CLASS_BUCKET). The walk is full-replacement —
        old class_states are discarded so a promotion (ephemeral → engaged_ephemeral)
        moves the counters cleanly rather than leaving the donor bucket inflated.

        IMPORTANT: the merged class_states is a descriptive-stats rollup, not an
        e-process. `_merge_state` sums per-agent log_e_values across different
        q-trajectories, which has no martingale interpretation. By design,
        `_state_to_metrics(scope="class", ...)` omits log_evidence/capped_alarm
        from the output so this sum never appears in the operator-facing payload.

        Classifier exceptions are caught per-agent (a periodic sweep must not
        be fragile against transient DB lookup failures), routed to
        UNKNOWN_CLASS_BUCKET, and logged to stderr with the exception type +
        agent_id prefix so the operator surface can distinguish "agent not
        classified" from "classifier raised."

        Returns a telemetry dict (tracked_agents, unresolved_agents, buckets,
        classifier_errors) and flips class_states_bootstrapped to True.
        """
        new_class_states: Dict[str, Dict[str, Any]] = defaultdict(_empty_state)
        movement: Dict[str, int] = defaultdict(int)
        unresolved = 0
        classifier_errors = 0

        for agent_id, agent_state in self.agent_states.items():
            try:
                resolved = classifier(agent_id)
            except Exception as exc:
                resolved = None
                classifier_errors += 1
                print(
                    f"[S10 rebucket] classifier raised on agent_id={agent_id!r}: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            bucket = resolved or UNKNOWN_CLASS_BUCKET
            if resolved is None:
                unresolved += 1
            _merge_state(new_class_states[bucket], agent_state)
            movement[bucket] += 1

        self.class_states = new_class_states
        self.class_states_bootstrapped = True
        if persist:
            self.save_state()
        return {
            "tracked_agents": sum(movement.values()),
            "unresolved_agents": unresolved,
            "classifier_errors": classifier_errors,
            "buckets": {k: v for k, v in movement.items()},
        }


_sequential_calibration_tracker_instance: SequentialCalibrationTracker | None = None


def get_sequential_calibration_tracker() -> SequentialCalibrationTracker:
    global _sequential_calibration_tracker_instance
    if _sequential_calibration_tracker_instance is None:
        _sequential_calibration_tracker_instance = SequentialCalibrationTracker()
    return _sequential_calibration_tracker_instance


class _SequentialCalibrationTrackerProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(get_sequential_calibration_tracker(), name)


sequential_calibration_tracker = _SequentialCalibrationTrackerProxy()
