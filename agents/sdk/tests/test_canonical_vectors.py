"""Pins the Python half of the cross-language canonical payload form.

Consumes the shared fixture ``tests/vectors/effect_payload_canonical.json``
(repo root) — the same file the Elixir lease plane pins itself against in
``elixir/lease_plane/test/canonical_payload_test.exs``. A green run on both
sides is the byte-identity proof #1252 item 1 requires.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unitares_sdk.lease_plane.canonical import (
    CanonicalizationError,
    canonical_payload_bytes,
    canonical_payload_sha256,
)

_VECTORS = json.loads(
    (Path(__file__).resolve().parents[3] / "tests" / "vectors"
     / "effect_payload_canonical.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize(
    "vector", _VECTORS["vectors"], ids=[v["name"] for v in _VECTORS["vectors"]]
)
def test_vector_reproduces_byte_identically(vector):
    assert canonical_payload_bytes(vector["payload"]) == vector["canonical"].encode("utf-8")
    assert canonical_payload_sha256(vector["payload"]) == vector["sha256"]


@pytest.mark.parametrize(
    "reject", _VECTORS["rejects"], ids=[r["name"] for r in _VECTORS["rejects"]]
)
def test_reject_vectors_refused(reject):
    with pytest.raises(CanonicalizationError):
        canonical_payload_sha256(reject["payload"])


def test_non_mapping_and_unsupported_types_refused():
    with pytest.raises(CanonicalizationError):
        canonical_payload_bytes("nope")  # type: ignore[arg-type]
    with pytest.raises(CanonicalizationError):
        canonical_payload_sha256({"x": object()})
    with pytest.raises(CanonicalizationError):
        canonical_payload_sha256({1: "v"})  # type: ignore[dict-item]


def test_deep_float_and_control_rejection():
    with pytest.raises(CanonicalizationError):
        canonical_payload_sha256({"a": [{"b": [1.0]}]})
    with pytest.raises(CanonicalizationError):
        canonical_payload_sha256({"a": [{"b": "x\x1by"}]})
