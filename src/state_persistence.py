"""Acknowledgement of one legacy agent-state write, independent of workflow success.

This is an assessment-row receipt, not a neutral checkpoint or an idempotency
key. An exception can follow a committed INSERT, so lack of acknowledgement
does not establish that no row exists.
"""

from dataclasses import dataclass
from typing import Any


STATE_PERSISTENCE_SCHEMA = "agent_state.persistence.v1"
STATE_PERSISTENCE_UNCONFIRMED_WARNING = (
    "State persistence is unconfirmed; a state row may or may not have been "
    "committed. Repeating this check-in can duplicate updates because retries "
    "are not deduplicated."
)


def _valid_state_id(value: Any) -> bool:
    return type(value) is int and value > 0


@dataclass(frozen=True)
class StatePersistenceReceipt:
    """Call-local acknowledgement; only a positive integer confirms the write."""

    state_id: int | None = None

    def __post_init__(self) -> None:
        if self.state_id is not None and not _valid_state_id(self.state_id):
            raise ValueError("state_id must be a positive integer or None")

    @classmethod
    def from_acknowledgement(cls, value: Any) -> "StatePersistenceReceipt":
        return cls(value) if _valid_state_id(value) else cls()

    @property
    def confirmed(self) -> bool:
        return self.state_id is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": STATE_PERSISTENCE_SCHEMA,
            "status": "recorded" if self.confirmed else "unconfirmed",
            "state_id": self.state_id,
            "checkin_retry_idempotent": False,
        }

    def to_json(self) -> str:
        """Primitive-only emergency projection, independent of JSON encoders."""
        state_id = str(self.state_id) if self.confirmed else "null"
        status = "recorded" if self.confirmed else "unconfirmed"
        return (
            '{"schema":"agent_state.persistence.v1","status":"' + status
            + '","state_id":' + state_id + ',"checkin_retry_idempotent":false}'
        )


def project_state_persistence(value: Any) -> dict[str, Any] | None:
    """Copy only a recognized, internally consistent receipt into a response."""
    if isinstance(value, StatePersistenceReceipt):
        return value.to_dict()
    if not isinstance(value, dict) or value.get("schema") != STATE_PERSISTENCE_SCHEMA:
        return None
    if value.get("checkin_retry_idempotent") is not False:
        return None
    state_id = value.get("state_id")
    if value.get("status") == "recorded" and _valid_state_id(state_id):
        return StatePersistenceReceipt(state_id).to_dict()
    if value.get("status") == "unconfirmed" and "state_id" in value and state_id is None:
        return StatePersistenceReceipt().to_dict()
    return None
