"""Canonical payload serialization for effect-binding (#1075 / #1252).

An effect grant binds ``payload_sha256`` — a hash the proposer computes at
mint time and the lease plane recomputes over the *parsed* payload when it
forwards the grant to ``/v1/effect-veto``. Those two computations happen in
different languages (Python producer here, Elixir plane in
``UnitaresLeasePlane.CanonicalPayload``), so both sides must serialize the
payload byte-identically before hashing. A mismatch fails CLOSED at the veto
(the effect is blocked), never open — which is exactly why the canonical form
has to be nailed down and pinned by the shared fixture
``tests/vectors/effect_payload_canonical.json``.

Canonical form:

- JSON, UTF-8, compact separators (``,`` / ``:``), object keys sorted
  bytewise. Python's ``sort_keys`` sorts ``str`` by codepoint, which equals
  UTF-8 byte order, so both languages agree.
- Non-ASCII characters are emitted raw (``ensure_ascii=False``), including
  non-BMP.
- Object keys must be strings. Values may be ``str``, ``int``, ``bool``,
  ``None``, ``dict``, ``list``.
- **Floats are rejected** (``CanonicalizationError``): float formatting is
  not stable across languages, and a silent divergence would veto every
  bound effect. Fail loudly at the producer instead.
- **Control characters (U+0000–U+001F) in strings and keys are rejected**:
  they are the one region where JSON escape spelling can differ between
  encoders (``\\u001b`` vs ``\\u001B``); real payloads (paths, base64
  content) never contain them, so refusing is cheaper than normalizing.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

__all__ = [
    "CanonicalizationError",
    "canonical_payload_bytes",
    "canonical_payload_sha256",
]


class CanonicalizationError(ValueError):
    """Payload cannot be canonically serialized (float, control char, bad key)."""


def _check_string(s: str, *, context: str) -> None:
    for ch in s:
        if ord(ch) < 0x20:
            raise CanonicalizationError(
                f"control character U+{ord(ch):04X} in {context}; canonical "
                "payloads must not contain control characters"
            )


def _validate(value: Any, *, context: str) -> None:
    # bool before int: bool is an int subclass but is always canonical.
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, float):
        raise CanonicalizationError(
            f"float in {context}; canonical payloads must not contain floats "
            "(cross-language float formatting is not stable)"
        )
    if isinstance(value, int):
        return
    if isinstance(value, str):
        _check_string(value, context=context)
        return
    if isinstance(value, Mapping):
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalizationError(
                    f"non-string key {k!r} in {context}; canonical payload "
                    "object keys must be strings"
                )
            _check_string(k, context=f"key in {context}")
            _validate(v, context=f"{context}.{k}")
        return
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _validate(item, context=f"{context}[{i}]")
        return
    raise CanonicalizationError(
        f"unsupported type {type(value).__name__} in {context}"
    )


def canonical_payload_bytes(payload: Mapping[str, Any]) -> bytes:
    """Serialize ``payload`` to its canonical UTF-8 byte form.

    Raises :class:`CanonicalizationError` on floats, control characters,
    non-string keys, or unsupported value types.
    """
    if not isinstance(payload, Mapping):
        raise CanonicalizationError(
            f"payload must be a mapping, got {type(payload).__name__}"
        )
    _validate(payload, context="payload")
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def canonical_payload_sha256(payload: Mapping[str, Any]) -> str:
    """Lowercase-hex SHA-256 of the canonical payload bytes."""
    return hashlib.sha256(canonical_payload_bytes(payload)).hexdigest()
