"""Requester-declared authorship conflicts for dialectic review.

Dialectic sessions may be opened by a proxy rather than by the author of the
work under review.  In that case excluding only the requestor is insufficient:
the work's author can otherwise be selected as the supposedly independent
reviewer.  The declaration in this module is deliberately narrow.  It records
canonical UNITARES agent UUIDs whose authored work is the review subject and
therefore only *removes* reviewer eligibility.

The declaration is requester-supplied and is not proof of authorship.  It is
stored under a server-owned key in ``paused_agent_state_json`` so existing
deployments need no schema migration.  Absence means ``not_supplied``; a
present but malformed reserved value fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Iterable, Mapping
from uuid import UUID


REVIEWED_WORK_AUTHORSHIP_SCHEMA = "unitares.dialectic-reviewed-work-authorship.v1"
REVIEWED_WORK_AUTHORSHIP_STATE_KEY = "_dialectic_reviewed_work_authorship"
MAX_REVIEWED_WORK_AUTHOR_AGENT_IDS = 32
CANONICAL_AGENT_UUID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class ReviewedWorkAuthorshipError(ValueError):
    """The caller supplied an unusable authorship declaration."""


def _canonical_uuid(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewedWorkAuthorshipError(f"{field} must contain non-empty UUID strings")
    raw = value.strip()
    try:
        canonical = str(UUID(raw))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ReviewedWorkAuthorshipError(
            f"{field} must contain canonical UNITARES agent UUIDs"
        ) from exc
    if raw != canonical:
        raise ReviewedWorkAuthorshipError(
            f"{field} must contain lowercase canonical UNITARES agent UUIDs"
        )
    return canonical


def parse_reviewed_work_author_agent_ids(value: Any) -> list[str]:
    """Validate and stably deduplicate the public request field.

    ``None`` is the schema-materialized form of omission.  An explicit empty
    list is refused because it is indistinguishable to a reader from a claim
    that the requester verified there were no authors; this field is only a
    declaration of known conflicts, never verification of their absence.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReviewedWorkAuthorshipError(
            "reviewed_work_author_agent_ids must be a list of agent UUIDs"
        )
    if not value:
        raise ReviewedWorkAuthorshipError(
            "reviewed_work_author_agent_ids cannot be empty; omit it when unspecified"
        )
    if len(value) > MAX_REVIEWED_WORK_AUTHOR_AGENT_IDS:
        raise ReviewedWorkAuthorshipError(
            "reviewed_work_author_agent_ids accepts at most "
            f"{MAX_REVIEWED_WORK_AUTHOR_AGENT_IDS} UUIDs"
        )

    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        agent_id = _canonical_uuid(
            item, field="reviewed_work_author_agent_ids"
        )
        if agent_id not in seen:
            seen.add(agent_id)
            result.append(agent_id)
    return result


@dataclass(frozen=True)
class ReviewedWorkAuthorship:
    """Normalized declaration read from a session record."""

    status: str
    author_agent_ids: tuple[str, ...] = ()
    declared_by_agent_id: str | None = None
    error: str | None = None

    @property
    def is_declared(self) -> bool:
        return self.status == "declared"

    @property
    def is_invalid(self) -> bool:
        return self.status == "invalid"

    def recuses(self, candidate_id: str | None, *, paused_agent_id: str | None = None) -> bool:
        """Whether ``candidate_id`` cannot count as an independent reviewer.

        Explicit ``reviewer_mode='self'`` remains representable: the paused
        agent is already labelled self-review and is not being counted as an
        independent peer.  Every other candidate fails closed when the marker
        is invalid, and declared work authors are recused exactly.
        """
        if not candidate_id or candidate_id == paused_agent_id:
            return False
        if self.is_invalid:
            return True
        return candidate_id in self.author_agent_ids

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": REVIEWED_WORK_AUTHORSHIP_SCHEMA,
            "status": self.status,
            "reviewed_work_author_agent_ids": list(self.author_agent_ids),
            "declared_by_agent_id": self.declared_by_agent_id,
            "authorship_verified": False,
        }
        if self.error:
            payload["error"] = self.error
        return payload


def declaration_for_request(
    author_agent_ids: Iterable[str], *, declared_by_agent_id: str
) -> ReviewedWorkAuthorship:
    """Build a server-owned declaration after request validation."""
    requested_ids = list(author_agent_ids)
    if not requested_ids:
        return ReviewedWorkAuthorship(status="not_supplied")
    ids = tuple(parse_reviewed_work_author_agent_ids(requested_ids))
    declarer = _canonical_uuid(
        declared_by_agent_id, field="declared_by_agent_id"
    )
    return ReviewedWorkAuthorship(
        status="declared",
        author_agent_ids=ids,
        declared_by_agent_id=declarer,
    )


def embed_reviewed_work_authorship(
    paused_agent_state: Mapping[str, Any] | None,
    declaration: ReviewedWorkAuthorship,
) -> dict[str, Any]:
    """Return a copied pause-state bundle containing the reserved declaration."""
    state = dict(paused_agent_state or {})
    # This namespace is server-owned.  Clear any inherited value even when the
    # requester supplied no declaration, so a stale/corrupt observation cannot
    # turn omission into a recusal claim.
    state.pop(REVIEWED_WORK_AUTHORSHIP_STATE_KEY, None)
    # ``reviewed_work_authorship`` is the public response projection.  It is
    # never part of stored pause evidence, and leaving an inherited projection
    # beside the reserved marker would let its ``not_supplied`` status mask the
    # declaration this server is embedding.
    state.pop("reviewed_work_authorship", None)
    if declaration.is_declared:
        state[REVIEWED_WORK_AUTHORSHIP_STATE_KEY] = declaration.as_dict()
    return state


def _mapping_view(value: Any) -> Any:
    """Normalize asyncpg-style records without weakening type checks.

    ``asyncpg.Record`` exposes ``items()``/``get()`` but does not register as a
    ``collections.abc.Mapping``.  Database refusal diagnostics pass that object
    here directly, while JSONB payloads and ordinary session dictionaries are
    already real mappings.  Copy only objects with a usable ``items`` method;
    everything else keeps the existing malformed/absent handling.
    """
    if isinstance(value, Mapping):
        return value
    items = getattr(value, "items", None)
    if callable(items):
        try:
            return dict(items())
        except (TypeError, ValueError):
            pass
    return value


def _state_from_record(record_or_state: Any) -> tuple[Any, bool]:
    """Return the candidate pause state and whether a reserved key was visible."""
    value = _mapping_view(record_or_state)
    if isinstance(value, Mapping):
        if "paused_agent_state" in value:
            value = value.get("paused_agent_state")
        elif "paused_agent_state_json" in value:
            value = value.get("paused_agent_state_json")
        elif REVIEWED_WORK_AUTHORSHIP_STATE_KEY not in value:
            direct = value.get("reviewed_work_authorship")
            if isinstance(direct, Mapping):
                return direct, True

    if isinstance(value, str):
        mentions_reserved_key = REVIEWED_WORK_AUTHORSHIP_STATE_KEY in value
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None, mentions_reserved_key

    if not isinstance(value, Mapping):
        return None, False
    if REVIEWED_WORK_AUTHORSHIP_STATE_KEY not in value:
        return None, False
    return value.get(REVIEWED_WORK_AUTHORSHIP_STATE_KEY), True


def read_reviewed_work_authorship(
    record_or_state: Any,
    *,
    expected_declared_by_agent_id: str | None = None,
) -> ReviewedWorkAuthorship:
    """Read a declaration from a session dict, pause state, or JSONB string."""
    record_or_state = _mapping_view(record_or_state)
    if expected_declared_by_agent_id is None and isinstance(
        record_or_state, Mapping
    ):
        candidate = (
            record_or_state.get("paused_agent_id")
            or record_or_state.get("paused_agent")
        )
        if isinstance(candidate, str) and candidate != "unknown":
            expected_declared_by_agent_id = candidate
    if (
        isinstance(record_or_state, Mapping)
        and "paused_agent_state" not in record_or_state
        and "paused_agent_state_json" not in record_or_state
        and REVIEWED_WORK_AUTHORSHIP_STATE_KEY not in record_or_state
    ):
        public = record_or_state.get("reviewed_work_authorship")
        if isinstance(public, Mapping):
            if (
                public.get("schema") == REVIEWED_WORK_AUTHORSHIP_SCHEMA
                and public.get("status") == "not_supplied"
            ):
                return ReviewedWorkAuthorship(status="not_supplied")
            if public.get("status") == "invalid":
                return ReviewedWorkAuthorship(
                    status="invalid",
                    error=str(public.get("error") or "malformed_reserved_marker"),
                )

    marker, present = _state_from_record(record_or_state)
    if not present:
        return ReviewedWorkAuthorship(status="not_supplied")
    if not isinstance(marker, Mapping):
        return ReviewedWorkAuthorship(status="invalid", error="malformed_reserved_marker")

    # A top-level response produced by ``as_dict`` is accepted directly; the
    # stored form is the same object under the reserved state key.
    if marker.get("schema") != REVIEWED_WORK_AUTHORSHIP_SCHEMA:
        return ReviewedWorkAuthorship(status="invalid", error="unsupported_schema")
    if marker.get("status") != "declared":
        return ReviewedWorkAuthorship(status="invalid", error="invalid_status")
    if marker.get("authorship_verified") is not False:
        return ReviewedWorkAuthorship(status="invalid", error="invalid_provenance")

    try:
        raw_authors = marker.get("reviewed_work_author_agent_ids")
        # ``None`` means omission only on the public request field.  Once the
        # server-owned marker says ``declared``, the list is mandatory; check
        # before ``len`` so a corrupt row fails closed instead of raising.
        if not isinstance(raw_authors, list):
            raise ReviewedWorkAuthorshipError(
                "stored reviewed_work_author_agent_ids must be a non-empty list"
            )
        authors = parse_reviewed_work_author_agent_ids(raw_authors)
        if len(raw_authors) != len(authors):
            return ReviewedWorkAuthorship(
                status="invalid", error="duplicate_author_agent_id"
            )
        declarer = _canonical_uuid(
            marker.get("declared_by_agent_id"), field="declared_by_agent_id"
        )
        if (
            expected_declared_by_agent_id is not None
            and declarer != expected_declared_by_agent_id
        ):
            return ReviewedWorkAuthorship(
                status="invalid", error="declarer_session_mismatch"
            )
    except ReviewedWorkAuthorshipError as exc:
        return ReviewedWorkAuthorship(status="invalid", error=str(exc))

    return ReviewedWorkAuthorship(
        status="declared",
        author_agent_ids=tuple(authors),
        declared_by_agent_id=declarer,
    )
