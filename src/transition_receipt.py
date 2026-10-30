"""Server-verifiable receipts for accepted UNITARES transitions.

``unitares.transition_receipt.v1`` is an unsigned, content-addressed statement
made by the UNITARES server inside its existing authority domain.  It proves
that the server accepted a named transition and persisted the covered subject
projection under the recorded actor.  It does *not* prove that the subject is
correct, independently reviewed, operator-approved, or externally
non-repudiable.  Portable deployment signatures remain a separate trust model.

The outer receipt is generic, while each ``projection_profile`` owns an exact
subject projection.  Callers cannot choose covered fields.  This avoids making
two different durable objects look equivalent merely because both can be
serialized as JSON.

Canonical JSON permits only null, booleans, strings, safe integers, lists, and
string-keyed mappings.  Floats and implicit type coercions are rejected so a
receipt cannot depend on Python/PostgreSQL/other-language number rendering.
Timestamps in a profile are normalized to UTC with microsecond precision before
hashing.  Unicode is preserved and is not normalized.

A detached receipt is only a content-addressed statement: server authorship is
established by reading it from the server-controlled provenance field of the
authoritative subject.  The verifier below checks that the supplied durable
subject still equals the accepted projection.  A later legitimate edit returns
``subject_hash_mismatch``; future mutation profiles must append a new receipt,
never rewrite this historical one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Final


RECEIPT_SCHEMA: Final = "unitares.transition_receipt.v1"
KNOWLEDGE_PROMOTION_PROFILE: Final = "unitares.projection.knowledge_promotion.v1"
KNOWLEDGE_PROMOTION_TRANSITION: Final = "knowledge.promote_memory_claim"
KNOWLEDGE_SUBJECT_KIND: Final = "knowledge.discovery"

_SUBJECT_DOMAIN: Final = b"unitares.transition_subject.v1\x00"
_RECEIPT_DOMAIN: Final = b"unitares.transition_receipt.v1\x00"
_MAX_SAFE_INTEGER: Final = (1 << 53) - 1
_KNOWLEDGE_PROMOTION_SCHEMA: Final = "unitares.knowledge_promotion.v1"
_PROMOTION_RECEIPT_KEYS: Final = (
    "schema",
    "source_id",
    "evidence_ids",
    "promoted_by",
    "promoted_at",
    "verification_basis",
    "decision_standard",
)
_RUNTIME_CONTEXT_KEYS: Final = (
    "model",
    "model_provider",
    "model_source",
    "harness_type",
    "harness_version",
)
_VERIFICATION_BASIS: Final = (
    "session ownership, knowledge-promotion policy checks, and same-object persistence"
)
_VERIFIED_FIELDS: Final = (
    "actor.agent_uuid",
    "accepted_at",
    "status",
    "subject.id",
    "subject_sha256",
)
_UNVERIFIED_FIELDS: Final = (
    "independent_review",
    "operator_approval",
    "subject.claim_correctness",
)
_RECEIPT_KEYS: Final = frozenset(
    {
        "schema",
        "projection_profile",
        "transition_type",
        "status",
        "subject",
        "subject_sha256",
        "actor",
        "accepted_at",
        "verification",
        "stance",
        "authorizes",
        "receipt_id",
    }
)


class TransitionReceiptError(ValueError):
    """Receipt construction failed closed under a stable diagnostic code."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def canonical_bytes(value: Any) -> bytes:
    """Return strict canonical JSON bytes suitable for receipt hashing."""
    _validate_canonical_value(value, path="$")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _validate_canonical_value(value: Any, *, path: str) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise TransitionReceiptError(
                "unsafe_integer",
                f"{path} exceeds the cross-language safe integer range",
            )
        return
    if isinstance(value, float):
        raise TransitionReceiptError(
            "float_not_canonical",
            f"{path} is a float; encode measured numbers as canonical strings",
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TransitionReceiptError(
                    "non_string_key", f"{path} contains a non-string mapping key"
                )
            _validate_canonical_value(item, path=f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _validate_canonical_value(item, path=f"{path}[{index}]")
        return
    raise TransitionReceiptError(
        "unsupported_type",
        f"{path} has unsupported canonical type {type(value).__name__}",
    )


def canonical_utc_timestamp(value: Any) -> str:
    """Normalize one aware datetime/ISO timestamp to fixed UTC microseconds."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TransitionReceiptError(
                "invalid_timestamp", f"invalid ISO timestamp: {value!r}"
            ) from exc
    else:
        raise TransitionReceiptError(
            "invalid_timestamp", "timestamp must be an aware datetime or ISO string"
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TransitionReceiptError(
            "naive_timestamp", "timestamp must include an offset"
        )
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def knowledge_promotion_projection(subject: Any) -> dict[str, Any]:
    """Project a promoted discovery onto its durable receipt-bearing fields."""
    return _knowledge_promotion_projection(subject, validate_profile=True)


def _knowledge_promotion_projection(
    subject: Any, *, validate_profile: bool
) -> dict[str, Any]:
    """Project exact stored values, optionally enforcing creation invariants."""
    provenance = _mapping_value(subject, "provenance") or {}
    authority = provenance.get("knowledge_authority")
    if not isinstance(authority, Mapping):
        raise TransitionReceiptError(
            "missing_promotion_receipt",
            "knowledge promotion subject lacks provenance.knowledge_authority",
        )
    promotion_receipt = {
        key: authority[key] for key in _PROMOTION_RECEIPT_KEYS if key in authority
    }
    missing = [key for key in _PROMOTION_RECEIPT_KEYS if key not in promotion_receipt]
    if missing:
        raise TransitionReceiptError(
            "incomplete_promotion_receipt",
            "knowledge promotion receipt lacks: " + ", ".join(missing),
        )
    promotion_receipt["promoted_at"] = canonical_utc_timestamp(
        promotion_receipt["promoted_at"]
    )

    projection: dict[str, Any] = {
        "id": _required_text(subject, "id"),
        "agent_id": _required_text(subject, "agent_id"),
        "type": _required_text(subject, "type"),
        "summary": _required_text(subject, "summary"),
        "details": _details_text(subject),
        "tags": _text_list(_value(subject, "tags")),
        # Both durable KG backends store an omitted severity as "low".
        "severity": _optional_text(_value(subject, "severity"), "severity", "low"),
        "timestamp": canonical_utc_timestamp(_value(subject, "timestamp")),
        "status": _optional_text(_value(subject, "status"), "status", "open"),
        "related_to": _text_list(_value(subject, "related_to")),
        "references_files": _text_list(_value(subject, "references_files")),
        "provenance": {
            "source": _optional_text(provenance.get("source"), "source", ""),
            "knowledge_authority": promotion_receipt,
        },
    }

    s22_context = provenance.get("s22_context")
    if isinstance(s22_context, Mapping):
        runtime_context: dict[str, str] = {}
        for key in _RUNTIME_CONTEXT_KEYS:
            value = s22_context.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise TransitionReceiptError(
                    "invalid_subject_field",
                    f"runtime context field {key} must be a string",
                )
            runtime_context[key] = value
        if runtime_context:
            projection["provenance"]["runtime_context"] = runtime_context

    if validate_profile:
        _validate_knowledge_promotion_projection(projection)
    # Validating here makes projection failures happen before persistence.
    canonical_bytes(projection)
    return projection


def build_knowledge_promotion_receipt(subject: Any) -> dict[str, Any]:
    """Build the v1 descriptive receipt for one promoted knowledge row."""
    projection = knowledge_promotion_projection(subject)
    accepted_at = projection["timestamp"]
    actor_uuid = projection["agent_id"]
    runtime_context = projection["provenance"].get("runtime_context")

    body: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "projection_profile": KNOWLEDGE_PROMOTION_PROFILE,
        "transition_type": KNOWLEDGE_PROMOTION_TRANSITION,
        "status": "accepted",
        "subject": {
            "kind": KNOWLEDGE_SUBJECT_KIND,
            "id": projection["id"],
        },
        "subject_sha256": _subject_sha256(KNOWLEDGE_PROMOTION_PROFILE, projection),
        "actor": {
            "agent_uuid": actor_uuid,
            "authentication": "caller_owned_registered_identity",
        },
        "accepted_at": accepted_at,
        "verification": {
            "issuer": "unitares-server",
            "basis": _VERIFICATION_BASIS,
            "verified_fields": list(_VERIFIED_FIELDS),
            "unverified_fields": list(_UNVERIFIED_FIELDS),
        },
        "stance": "descriptive",
        "authorizes": [],
    }
    if runtime_context:
        body["runtime_provenance"] = runtime_context
        body["verification"]["unverified_fields"].extend(
            f"runtime_provenance.{key}" for key in sorted(runtime_context)
        )
    _validate_verification_fields(body["verification"])
    body["receipt_id"] = _receipt_id(body)
    return body


def verify_transition_receipt(
    receipt: Mapping[str, Any], subject: Any
) -> tuple[bool, list[str]]:
    """Verify a receipt against a durable subject without server or DB access."""
    errors: list[str] = []
    if receipt.get("schema") != RECEIPT_SCHEMA:
        errors.append("schema_mismatch")
    profile = receipt.get("projection_profile")
    if profile != KNOWLEDGE_PROMOTION_PROFILE:
        errors.append("unsupported_projection_profile")
        return False, errors

    try:
        projection = _knowledge_promotion_projection(subject, validate_profile=False)
    except TransitionReceiptError as exc:
        errors.append(exc.code)
        return False, errors

    receipt_keys = set(receipt)
    allowed_receipt_keys = (_RECEIPT_KEYS, _RECEIPT_KEYS | {"runtime_provenance"})
    if receipt_keys not in allowed_receipt_keys:
        _append_error(errors, "receipt_contract_mismatch")
    provenance = _mapping_value(subject, "provenance") or {}
    embedded_receipt = provenance.get("transition_receipt")
    if not isinstance(embedded_receipt, Mapping):
        errors.append("subject_receipt_missing")
    elif dict(embedded_receipt) != dict(receipt):
        errors.append("subject_receipt_mismatch")

    expected_subject = {
        "kind": KNOWLEDGE_SUBJECT_KIND,
        "id": projection["id"],
    }
    receipt_subject = receipt.get("subject")
    if not isinstance(receipt_subject, Mapping) or set(receipt_subject) != {
        "kind",
        "id",
    }:
        _append_error(errors, "receipt_contract_mismatch")
    if receipt.get("subject") != expected_subject:
        errors.append("subject_mismatch")
    if receipt.get("accepted_at") != projection["timestamp"]:
        errors.append("accepted_at_mismatch")
    if receipt.get("subject_sha256") != _subject_sha256(profile, projection):
        errors.append("subject_hash_mismatch")
    try:
        if receipt.get("receipt_id") != _receipt_id(receipt):
            errors.append("receipt_id_mismatch")
    except TransitionReceiptError as exc:
        errors.append(exc.code)

    if receipt.get("transition_type") != KNOWLEDGE_PROMOTION_TRANSITION:
        errors.append("transition_type_mismatch")
    if receipt.get("status") != "accepted":
        errors.append("status_mismatch")
    if receipt.get("stance") != "descriptive":
        errors.append("stance_mismatch")
    if receipt.get("authorizes") != []:
        errors.append("authorizes_not_empty")
    actor = receipt.get("actor")
    if not isinstance(actor, Mapping) or set(actor) != {
        "agent_uuid",
        "authentication",
    }:
        _append_error(errors, "receipt_contract_mismatch")
    if (
        not isinstance(actor, Mapping)
        or actor.get("agent_uuid") != projection["agent_id"]
        or actor.get("authentication") != "caller_owned_registered_identity"
    ):
        errors.append("actor_mismatch")
    runtime_receipt = receipt.get("runtime_provenance")
    if "runtime_provenance" in receipt and (
        not isinstance(runtime_receipt, Mapping)
        or not runtime_receipt
        or not set(runtime_receipt).issubset(_RUNTIME_CONTEXT_KEYS)
        or not all(isinstance(value, str) for value in runtime_receipt.values())
    ):
        _append_error(errors, "receipt_contract_mismatch")
    if receipt.get("runtime_provenance") != projection["provenance"].get(
        "runtime_context"
    ):
        errors.append("runtime_provenance_mismatch")
    try:
        verification = receipt.get("verification")
        if not isinstance(verification, Mapping):
            raise TransitionReceiptError("invalid_verification")
        if set(verification) != {
            "issuer",
            "basis",
            "verified_fields",
            "unverified_fields",
        }:
            _append_error(errors, "receipt_contract_mismatch")
        _validate_verification_fields(verification)
        if verification.get("issuer") != "unitares-server":
            errors.append("issuer_mismatch")
        expected_unverified = [
            *_UNVERIFIED_FIELDS,
            *(
                f"runtime_provenance.{key}"
                for key in sorted(
                    receipt.get("runtime_provenance", {})
                    if isinstance(receipt.get("runtime_provenance"), Mapping)
                    else {}
                )
            ),
        ]
        if (
            verification.get("basis") != _VERIFICATION_BASIS
            or verification.get("verified_fields") != list(_VERIFIED_FIELDS)
            or verification.get("unverified_fields") != expected_unverified
        ):
            errors.append("verification_contract_mismatch")
    except TransitionReceiptError as exc:
        errors.append(exc.code)
    return not errors, errors


def _subject_sha256(profile: str, projection: Mapping[str, Any]) -> str:
    material = (
        _SUBJECT_DOMAIN
        + profile.encode("utf-8")
        + b"\x00"
        + canonical_bytes(projection)
    )
    return hashlib.sha256(material).hexdigest()


def _append_error(errors: list[str], code: str) -> None:
    if code not in errors:
        errors.append(code)


def _validate_knowledge_promotion_projection(
    projection: Mapping[str, Any],
) -> None:
    provenance = projection["provenance"]
    authority = provenance["knowledge_authority"]
    actor_uuid = projection["agent_id"]
    accepted_at = projection["timestamp"]

    if provenance.get("source") != "explicit_promotion":
        raise TransitionReceiptError(
            "invalid_promotion_source", "promotion source must be explicit_promotion"
        )
    if authority.get("schema") != _KNOWLEDGE_PROMOTION_SCHEMA:
        raise TransitionReceiptError(
            "invalid_promotion_schema", "unexpected knowledge promotion schema"
        )
    if authority.get("promoted_by") != actor_uuid:
        raise TransitionReceiptError(
            "promoter_mismatch", "promoted_by must equal the durable writer UUID"
        )
    if canonical_utc_timestamp(authority.get("promoted_at")) != accepted_at:
        raise TransitionReceiptError(
            "promotion_timestamp_mismatch",
            "promoted_at must equal the durable subject timestamp",
        )

    source_value = authority.get("source_id")
    source_id = source_value if isinstance(source_value, str) else ""
    evidence_value = authority.get("evidence_ids")
    evidence_ids = (
        list(evidence_value)
        if isinstance(evidence_value, list)
        and all(isinstance(item, str) and item.strip() for item in evidence_value)
        else []
    )
    if not source_id.strip() or not evidence_ids:
        raise TransitionReceiptError(
            "invalid_promotion_evidence",
            "promotion requires one source and at least one evidence row",
        )
    verification_basis = authority.get("verification_basis")
    if not isinstance(verification_basis, str) or not verification_basis.strip():
        raise TransitionReceiptError(
            "invalid_promotion_verification_basis",
            "promotion requires a nonblank verification basis",
        )
    decision_standard = authority.get("decision_standard")
    if not isinstance(decision_standard, str) or not decision_standard.strip():
        raise TransitionReceiptError(
            "invalid_promotion_decision_standard",
            "promotion requires a nonblank decision standard",
        )
    if source_id in evidence_ids:
        raise TransitionReceiptError(
            "invalid_promotion_evidence", "source memory cannot corroborate itself"
        )
    if projection["related_to"] != [source_id, *evidence_ids]:
        raise TransitionReceiptError(
            "promotion_links_mismatch",
            "durable related_to links must match source and evidence order",
        )
    required_tags = {"authority-governed", "promoted-from-memory"}
    if not required_tags.issubset(projection["tags"]):
        raise TransitionReceiptError(
            "promotion_tags_missing", "durable promotion tags are incomplete"
        )


def _receipt_id(receipt: Mapping[str, Any]) -> str:
    body = dict(receipt)
    body.pop("receipt_id", None)
    digest = hashlib.sha256(_RECEIPT_DOMAIN + canonical_bytes(body)).hexdigest()
    return "tr_v1_" + digest


def _validate_verification_fields(verification: Mapping[str, Any]) -> None:
    verified = verification.get("verified_fields")
    unverified = verification.get("unverified_fields")
    if not isinstance(verified, list) or not isinstance(unverified, list):
        raise TransitionReceiptError(
            "invalid_verification", "verified and unverified fields must be lists"
        )
    if not all(
        isinstance(item, str) and item.strip() for item in [*verified, *unverified]
    ):
        raise TransitionReceiptError(
            "invalid_verification",
            "verified and unverified fields must contain nonblank strings",
        )
    if len(set(verified)) != len(verified) or len(set(unverified)) != len(unverified):
        raise TransitionReceiptError(
            "invalid_verification",
            "verified and unverified fields cannot contain duplicates",
        )
    overlap = set(verified).intersection(unverified)
    if overlap:
        raise TransitionReceiptError(
            "verification_overlap",
            "fields cannot be both verified and unverified: "
            + ", ".join(sorted(overlap)),
        )


def _value(subject: Any, key: str) -> Any:
    if isinstance(subject, Mapping):
        if key == "timestamp" and key not in subject:
            return subject.get("created_at")
        return subject.get(key)
    return getattr(subject, key, None)


def _mapping_value(subject: Any, key: str) -> Mapping[str, Any] | None:
    value = _value(subject, key)
    return value if isinstance(value, Mapping) else None


def _required_text(subject: Any, key: str) -> str:
    value = _value(subject, key)
    if not isinstance(value, str):
        raise TransitionReceiptError(
            "invalid_subject_field", f"knowledge promotion field {key} must be a string"
        )
    if not value.strip():
        raise TransitionReceiptError(
            "missing_subject_field", f"knowledge promotion subject lacks {key}"
        )
    return value


def _optional_text(value: Any, key: str, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise TransitionReceiptError(
            "invalid_subject_field", f"knowledge promotion field {key} must be a string"
        )
    return value


def _details_text(subject: Any) -> str:
    if isinstance(subject, Mapping) and "details" not in subject:
        if subject.get("has_details") or "details_preview" in subject:
            raise TransitionReceiptError(
                "subject_projection_incomplete",
                "receipt verification requires the complete details field",
            )
        return ""
    value = _value(subject, "details")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TransitionReceiptError(
            "invalid_subject_field",
            "knowledge promotion field details must be a string",
        )
    return value


def _text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TransitionReceiptError(
            "invalid_subject_field", "expected a list of strings"
        )
    if not all(isinstance(item, str) for item in value):
        raise TransitionReceiptError(
            "invalid_subject_field", "list fields must contain only strings"
        )
    return list(value)
