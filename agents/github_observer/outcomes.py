"""Graded outcomes from GitHub events: the opt-in stage 4 writer.

``docs/proposals/active/github-outcome-observer-v0.md`` §5 kept the observer
out of ``audit.outcome_events`` until the operator ruled on promotion (§8.4).
The operator directed this writer on 2026-11-20, ahead of the 12-01 read; §6b
records the direction and what it leaves open. It is off unless the
deployment passes ``--record-outcomes``.

Mapping: ``github.pr_merged`` -> ``task_completed``, and nothing else. A CI
result stays a timeline event. Grading every red run on an open pull request
as ``test_failed`` would make the process working (§2) a bad outcome, once per
workflow and per re-run; which CI verdict should grade, if any, is left to the
operator (§6b). A close without a merge is not graded either: a pull request
is closed when it is superseded as often as when it is wrong.

Attribution. An outcome is queued just before its event is posted, from that
event as posted, and is sent only once ``POST /v1/external/events`` accepts
the event. That route checks the subject uuid against UNITARES and the PR
author against the trusted list before its dedup, so an outcome inherits both
checks. A 201 releases the outcome built from the event as just posted. A 409
releases an outcome only when this store queued it on an earlier attempt whose
answer was lost, and then releases that attempt's body, so a footer edited in
between cannot move the outcome to another agent. A 409 for an event this
store never queued (another writer, or an event stored before the flag was
on) records nothing.

Delivery. Outcomes post to ``POST /v1/harness/outcome`` under the operator
credential with ``verification_source="external_signal"``. No ``confidence``
is sent: the observer has no prediction of its own, so the server stamps the
row ``calibration_excluded``. No ``prediction_id`` is sent; that route ignores
one by design. The queue is retried on every poll, independent of whether
GitHub still lists the pull request. The route has no idempotency key, so an
outcome whose write committed but whose answer was lost is sent again: a
duplicate row is possible there, never a lost one (§6b).
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable, Mapping, Protocol, TextIO

OUTCOME_KEY_PREFIX = "outcome:"
VERIFICATION_SOURCE = "external_signal"
PRODUCER = "github_observer"

# Event fields copied into the outcome's ``detail``.
_DETAIL_FIELDS = (
    "repo",
    "pr",
    "head_sha_at_observation",
    "merge_commit_sha",
    "observed_at",
)

# Event route answers that mean the event is not stored, and that sending the same event again would not
# change. ``failed`` is not here: a write whose answer was lost may have
# committed, so its queued outcome is kept for the next attempt's answer.
REFUSED = frozenset({"unknown_agent", "untrusted_author", "future_timestamp", "rejected"})


def outcome_key(event: Mapping[str, Any]) -> str:
    return OUTCOME_KEY_PREFIX + str(event["fingerprint"])


def outcome_type_for(event: Mapping[str, Any]) -> str | None:
    """The outcome type an event grades as, or None when it grades as nothing."""
    if event.get("type") == "github.pr_merged":
        return "task_completed"
    return None


def outcome_payload(event: Mapping[str, Any]) -> dict | None:
    """The ``POST /v1/harness/outcome`` body for an event, or None."""
    outcome_type = outcome_type_for(event)
    if outcome_type is None:
        return None
    detail: dict[str, Any] = {
        "producer": PRODUCER,
        "github_event_type": event["type"],
        "github_event_fingerprint": event["fingerprint"],
    }
    for field in _DETAIL_FIELDS:
        value = event.get(field)
        if value is not None:
            detail[field] = value
    return {
        "agent_uuid": event["agent_uuid"],
        "outcome_type": outcome_type,
        "verification_source": VERIFICATION_SOURCE,
        "detail": detail,
    }


class PendingStore(Protocol):
    """Where queued outcomes wait. ``FingerprintStore`` is the durable one."""

    def pending_state(self, key: str) -> str | None: ...
    def put_pending(self, key: str, payload: dict, *, accepted: bool) -> None: ...
    def accept_pending(self, key: str) -> None: ...
    def drop_pending(self, key: str) -> None: ...
    def accepted_pending(self) -> Iterable[tuple[str, dict]]: ...
    def has(self, fingerprint: str) -> bool: ...
    def add(self, fingerprint: str, event_type: str) -> bool: ...


class MemoryPendingStore:
    """The dry run's queue: nothing outlives the process."""

    def __init__(self, known: Iterable[str] = ()):
        self._known = set(known)
        self._pending: dict[str, tuple[dict, bool]] = {}

    def pending_state(self, key):
        if key not in self._pending:
            return None
        return "accepted" if self._pending[key][1] else "queued"

    def put_pending(self, key, payload, *, accepted):
        self._pending[key] = (payload, accepted)

    def accept_pending(self, key):
        if key in self._pending:
            self._pending[key] = (self._pending[key][0], True)

    def drop_pending(self, key):
        self._pending.pop(key, None)

    def accepted_pending(self):
        return [(k, p) for k, (p, accepted) in sorted(self._pending.items()) if accepted]

    def has(self, fingerprint):
        return fingerprint in self._known

    def add(self, fingerprint, event_type):
        new = fingerprint not in self._known
        self._known.add(fingerprint)
        return new


class OutcomeWriter(Protocol):
    def write(self, payload: dict) -> str: ...


class OutcomeQueue:
    """Ties outcomes to the event writes they follow. See the module docstring."""

    def __init__(self, store: PendingStore, writer: OutcomeWriter):
        self._store = store
        self._writer = writer

    def before_event(self, event: Mapping[str, Any]) -> tuple[str, bool, dict] | None:
        """Queue the event's outcome before the event is posted.

        Returns ``(key, queued_earlier, payload)``, or None when the event
        grades as nothing or its outcome is already written. An outcome queued
        by an earlier attempt is kept as it was: it is the body that matches
        what that attempt may have stored.
        """
        payload = outcome_payload(event)
        if payload is None:
            return None
        key = outcome_key(event)
        if self._store.has(key):
            return None
        queued_earlier = self._store.pending_state(key) is not None
        if not queued_earlier:
            self._store.put_pending(key, payload, accepted=False)
        return key, queued_earlier, payload

    def after_event(self, ticket: tuple[str, bool, dict] | None, result: Any) -> None:
        """Release, keep or drop the queued outcome by the event route's answer."""
        if ticket is None:
            return
        key, queued_earlier, payload = ticket
        if result is True or result == "written":
            # The server stored this event just now, as posted: the outcome
            # follows that body, not one queued by an attempt that never landed.
            self._store.put_pending(key, payload, accepted=True)
        elif result == "duplicate":
            if queued_earlier:
                # An earlier attempt of ours stored it and its answer was lost.
                self._store.accept_pending(key)
            else:
                # Stored by someone else, or before the flag was on.
                self._store.drop_pending(key)
        elif result in REFUSED:
            self._store.drop_pending(key)
        # ``failed`` and anything unknown: keep it queued, unreleased.

    def flush(self, counts: dict[str, int]) -> None:
        """Send every released outcome; keep the ones that failed for the next poll."""
        for key, payload in list(self._store.accepted_pending()):
            result = self._writer.write(payload)
            if result == "written":
                self._store.add(key, payload["outcome_type"])
                self._store.drop_pending(key)
                counts["outcomes_written"] += 1
            elif result == "rejected":
                # The same body would be refused again. Recorded so it is not
                # resent, and counted so the poll exits nonzero.
                self._store.add(key, payload["outcome_type"])
                self._store.drop_pending(key)
                counts["outcomes_rejected"] += 1
            else:
                counts["outcomes_failed"] += 1


class DryRunOutcomeWriter:
    """Prints the outcome body a write would post, one JSON line each."""

    def __init__(self, stream: TextIO):
        self._stream = stream

    def write(self, payload: dict) -> str:
        self._stream.write(json.dumps({"outcome": payload}, sort_keys=True) + "\n")
        return "written"


class UnitaresOutcomeWriter:
    """Post graded outcomes to ``POST /v1/harness/outcome``."""

    def __init__(self, base_url: str, operator_token: str, *, opener: Callable[..., Any]):
        self._endpoint = base_url.rstrip("/") + "/v1/harness/outcome"
        self._operator_token = operator_token
        self._opener = opener

    def write(self, payload: dict) -> str:
        """``written``, ``rejected`` (the body itself was refused) or ``failed``."""
        request = urllib.request.Request(
            self._endpoint,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Unitares-Operator": self._operator_token,
                "User-Agent": "unitares-github-observer",
            },
        )
        try:
            with self._opener(request, timeout=30) as response:
                status = getattr(response, "status", None)
                if status is None and hasattr(response, "getcode"):
                    status = response.getcode()
                if status not in (200, 201):
                    return "failed"
                ack = json.loads(response.read(16384).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 400 and 422 are the body; 401, 403 and 404 are credentials or a
            # deployment a config fix can clear, so those are retried.
            return "rejected" if exc.code in (400, 422) else "failed"
        except (OSError, http.client.HTTPException, UnicodeDecodeError, ValueError, AttributeError):
            return "failed"
        if (
            isinstance(ack, dict)
            and ack.get("success") is True
            and ack.get("outcome_id")
            and ack.get("agent_uuid") == payload["agent_uuid"]
            and ack.get("outcome_type") == payload["outcome_type"]
        ):
            return "written"
        return "failed"
