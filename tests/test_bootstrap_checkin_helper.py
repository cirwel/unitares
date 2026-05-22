"""Unit tests for the bootstrap-checkin helper (no DB required).

Covers the digest contract, default-fill behavior, and state_json composition.
DB-touching paths (write_bootstrap, is_substrate_earned) are exercised in
test_bootstrap_checkin_handler.py.

"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.identity.bootstrap_checkin import (
    build_state_json,
    compute_bootstrap_digest,
    derive_bootstrap_response_text,
    fill_defaults,
)
from src.mcp_handlers.schemas.core import BootstrapStateParams


def test_digest_excludes_server_defaults():
    """Two callers passing the same explicit fields produce the same digest,
    regardless of which defaults the server would fill."""
    a = BootstrapStateParams(complexity=0.7, confidence=0.6)
    b = BootstrapStateParams(complexity=0.7, confidence=0.6)
    assert compute_bootstrap_digest(a) == compute_bootstrap_digest(b)


def test_digest_changes_when_payload_changes():
    a = BootstrapStateParams(complexity=0.7, confidence=0.6)
    b = BootstrapStateParams(complexity=0.7, confidence=0.5)
    assert compute_bootstrap_digest(a) != compute_bootstrap_digest(b)


def test_digest_omits_unset_fields():
    """A field set to None is treated as 'not supplied' — same digest as omitting it."""
    a = BootstrapStateParams()
    b = BootstrapStateParams(complexity=None, confidence=None)
    assert compute_bootstrap_digest(a) == compute_bootstrap_digest(b)


def test_digest_is_canonical_sha256_hex():
    digest = compute_bootstrap_digest(BootstrapStateParams(complexity=0.5))
    assert len(digest) == 64
    int(digest, 16)  # raises ValueError if not hex


def test_response_text_caller_wins():
    params = BootstrapStateParams(response_text="explicit")
    assert derive_bootstrap_response_text(
        params, client_hint="hook-fired", purpose="testing"
    ) == "explicit"


def test_response_text_falls_back_to_client_hint_then_purpose():
    params = BootstrapStateParams()
    assert derive_bootstrap_response_text(
        params, client_hint="hook-fired", purpose="testing"
    ) == "[bootstrap] hook-fired"
    assert derive_bootstrap_response_text(
        params, client_hint=None, purpose="testing"
    ) == "[bootstrap] testing"
    assert derive_bootstrap_response_text(
        params, client_hint=None, purpose=None
    ) == "[bootstrap] session-start"


def test_fill_defaults_caller_wins():
    params = BootstrapStateParams(complexity=0.9, confidence=0.1, task_type="bugfix")
    filled = fill_defaults(params, client_hint="claude-code", purpose=None)
    assert filled["complexity"] == 0.9
    assert filled["confidence"] == 0.1
    assert filled["task_type"] == "bugfix"


def test_fill_defaults_applies_when_absent():
    filled = fill_defaults(BootstrapStateParams(), client_hint=None, purpose=None)
    assert filled["complexity"] == 0.5
    assert filled["confidence"] == 0.5
    assert filled["task_type"] == "introspection"
    assert filled["ethical_drift"] == [0.0, 0.0, 0.0]


def test_state_json_marks_source_and_carries_digest():
    params = BootstrapStateParams(complexity=0.7)
    filled = fill_defaults(params)
    sj = build_state_json(filled)
    assert sj["source"] == "bootstrap"
    assert sj["bootstrap_digest"] == filled["bootstrap_digest"]
    assert sj["complexity"] == 0.7
    assert sj["epistemic_class"] == "synthetic"


def test_pydantic_rejects_extra_fields():
    """`extra='forbid'` on the model means {synthetic: false} cannot sneak in."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        BootstrapStateParams(synthetic=False)
    with pytest.raises(ValidationError):
        BootstrapStateParams(arbitrary_key="value")


def test_pydantic_rejects_out_of_range_floats():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        BootstrapStateParams(complexity=1.5)
    with pytest.raises(ValidationError):
        BootstrapStateParams(confidence=-0.1)


def test_pydantic_rejects_invalid_task_type():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        BootstrapStateParams(task_type="not-a-real-task-type")


def test_pydantic_rejects_wrong_drift_length():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        BootstrapStateParams(ethical_drift=[0.0, 0.0])
    with pytest.raises(ValidationError):
        BootstrapStateParams(ethical_drift=[0.0, 0.0, 0.0, 0.0])
