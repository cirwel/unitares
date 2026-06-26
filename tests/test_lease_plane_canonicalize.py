"""
PR 2 — surface_id canonicalization helper (RFC v0.8 §7.12).

Tests the per-scheme normalization rule, error semantics, and
filesystem case-detection probe.

Spec: §7.12.0–§7.12.3
 PR 2
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest


def test_canonicalize_case_detection_uses_tmpfile_probe():
    """Live-verifier DRIFT-3: pathconf(_PC_CASE_SENSITIVE) is REFUTED on macOS Python.
    The detection MUST use a tmpfile probe, not pathconf."""
    from src.lease_plane import canonicalize as canon

    # The detection function should not raise on macOS.
    result = canon._detect_case_insensitive()
    assert isinstance(result, bool), (
        f"_detect_case_insensitive must return bool, got {type(result).__name__}"
    )

    # Negative-test the wrong API: confirm it doesn't depend on PC_CASE_SENSITIVE
    # (would raise ValueError if it did, on macOS).
    if "PC_CASE_SENSITIVE" not in os.pathconf_names:
        # Patch pathconf to detect any rogue use.
        with mock.patch.object(os, "pathconf", side_effect=AssertionError("must not use os.pathconf")):
            # Re-detection must succeed via tmpfile, not pathconf.
            assert isinstance(canon._detect_case_insensitive(), bool)


def test_canonicalize_resolves_var_to_private_var_on_macos():
    """Live-verifier DRIFT-2: os.path.realpath on macOS must double-resolve so
    /var/folders/.../tmpfile and /private/var/folders/.../tmpfile produce the same form."""
    from src.lease_plane import canonicalize as canon

    if not Path("/private/var").exists() or not Path("/var").exists():
        pytest.skip("not on a macOS-style filesystem with /var → /private/var symlink")

    # Pick a real path that exists under /var/folders to exercise the realpath chain.
    with tempfile.NamedTemporaryFile(prefix="canon_var_test_", delete=False) as f:
        tmp_path = f.name
    try:
        # tempfile gives a /var/folders/... path; realpath should produce /private/var/folders/...
        var_form = f"file://{tmp_path}"
        # Construct the equivalent /private/var/ form manually.
        if tmp_path.startswith("/var/"):
            private_form = f"file:///private{tmp_path}"
        else:
            # Already under /private/var/ — both forms should canonicalize identically.
            private_form = var_form
        canonical_var = canon.canonicalize(var_form)
        canonical_private = canon.canonicalize(private_form)
        assert canonical_var == canonical_private, (
            f"Expected /var/ and /private/var/ forms to produce the same canonical "
            f"surface_id, got:\n  var_form     -> {canonical_var}\n  private_form -> {canonical_private}"
        )
        assert "/private/var/" in canonical_var, (
            f"Expected canonical form to contain /private/var/, got {canonical_var}"
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def test_capture_canonicalizes_member_ordering():
    """capture:/A,B,C and capture:/B,A,C represent the same calibration window;
    canonicalize sorts members lexically so they share one surface_id."""
    from src.lease_plane import canonicalize as canon

    a = canon.canonicalize("capture:/window_b,window_a,window_c")
    b = canon.canonicalize("capture:/window_a,window_b,window_c")
    c = canon.canonicalize("capture:/window_c,window_a,window_b")

    assert a == b == c, f"capture:/ members must canonicalize to lexical order, got {a!r} {b!r} {c!r}"
    assert "window_a,window_b,window_c" in a, (
        f"Expected lexically-sorted members in canonical form, got {a!r}"
    )


def test_agent_scheme_canonicalizes_as_presence_surface():
    """agent:/ — ephemeral-agent presence surface (migration 042). Opaque,
    case-sensitive, strip trailing /, same reserved chars as resident:/."""
    from src.lease_plane import canonicalize as canon
    from src.lease_plane.canonicalize import CANONICAL_SCHEMES, CanonicalizeError

    assert "agent" in CANONICAL_SCHEMES

    assert canon.canonicalize("agent:/ag-7SDzA2Tm") == "agent:/ag-7SDzA2Tm"
    assert canon.canonicalize("agent:/ag-abc/") == "agent:/ag-abc"  # trailing / stripped
    # url-safe base64 ids (the orchestrator's generate_agent_id output) are valid
    assert canon.canonicalize("agent:/FQSzK8iT_-x") == "agent:/FQSzK8iT_-x"

    for bad in ("agent:/with space", "agent:/h#frag", "agent:/a&b", "agent:/q?x=1"):
        with pytest.raises(CanonicalizeError):
            canon.canonicalize(bad)


def test_maintenance_scheme_canonicalizes_as_cleanup_surface():
    """maintenance:/ — cleanup/repair coordination surface (migration 049).
    Opaque, case-sensitive, strip trailing /, same reserved chars as resident:/."""
    from src.lease_plane import canonicalize as canon
    from src.lease_plane.canonicalize import CANONICAL_SCHEMES, CanonicalizeError

    assert "maintenance" in CANONICAL_SCHEMES

    assert canon.canonicalize("maintenance:/worktree_reaper") == "maintenance:/worktree_reaper"
    assert canon.canonicalize("maintenance:/vigil_hygiene_sweep/") == (
        "maintenance:/vigil_hygiene_sweep"
    )
    assert canon.canonicalize("maintenance:/Cleanup_Job") == "maintenance:/Cleanup_Job"

    for bad in (
        "maintenance:/with space",
        "maintenance:/h#frag",
        "maintenance:/a&b",
        "maintenance:/q?x=1",
    ):
        with pytest.raises(CanonicalizeError):
            canon.canonicalize(bad)


def test_canonicalize_error_semantics():
    """Helper raises CanonicalizeError with named reasons for bounded failure modes."""
    from src.lease_plane.canonicalize import CanonicalizeError, canonicalize

    # NUL byte: caller-side rejection. Stdlib raises ValueError; helper propagates.
    with pytest.raises((ValueError, CanonicalizeError)):
        canonicalize("file:///tmp/nul\x00byte.py")

    # Symlink loop: realpath raises OSError(ELOOP); helper wraps as CanonicalizeError.
    with tempfile.TemporaryDirectory() as d:
        loop_a = os.path.join(d, "loop_a")
        loop_b = os.path.join(d, "loop_b")
        os.symlink(loop_b, loop_a)
        os.symlink(loop_a, loop_b)
        with pytest.raises(CanonicalizeError) as exc_info:
            canonicalize(f"file://{loop_a}")
        assert exc_info.value.reason == "symlink_loop", (
            f"Expected reason='symlink_loop', got {exc_info.value.reason!r}"
        )

    # Nonexistent path: realpath returns the un-resolved input, helper proceeds without raising.
    nonexistent = "file:///tmp/this_path_definitely_does_not_exist_xyzzy_abc123"
    result = canonicalize(nonexistent)
    assert result.startswith("file://"), (
        f"Nonexistent path should still produce a canonical form, got {result!r}"
    )


def test_acquire_request_rejects_query_string_in_surface_id():
    """RFC v0.8 §7.12.4: ?-bearing surface_id reserved for v1 modifier form;
    v0 callers must use plain canonical form."""
    from pydantic import ValidationError

    from src.lease_plane import AcquireRequest
    from uuid import uuid4

    with pytest.raises(ValidationError) as exc_info:
        AcquireRequest(
            surface_id="file:///tmp/x.py?canon=inode",
            holder_agent_uuid=uuid4(),
            holder_class="process_instance",
            holder_kind="remote_heartbeat",
            ttl_s=60,
        )
    assert "?" in str(exc_info.value) or "query" in str(exc_info.value).lower(), (
        f"Expected error message to mention query string, got: {exc_info.value}"
    )


def test_acquire_request_has_no_surface_kind_field():
    """RFC v0.8 §7.2.3 / §9 — `surface_kind` was removed from AcquireRequest;
    it is now derived server-side from the surface_id scheme via migration
    026's generated column. Caller cannot supply a conflicting value because
    the field doesn't exist on the request model.

    Pins the §9 named gate `test_acquire_request_has_no_surface_kind_field`
    (post-removal)."""
    from src.lease_plane import AcquireRequest

    assert "surface_kind" not in AcquireRequest.model_fields, (
        "AcquireRequest must not carry a surface_kind field — RFC §7.2.3 "
        "moved derivation to migration 026's generated column."
    )


def test_file_canonicalization_case_insensitive_apfs():
    """RFC v0.8 §7.12.1 / §9 — on a case-insensitive filesystem (default macOS
    APFS), two surface_ids that differ only by case canonicalize to the same
    string. Skipped when the host filesystem is case-sensitive (e.g. Linux
    ext4) since the gate's premise is filesystem-dependent.

    Pins the §9 named gate `test_file_canonicalization_case_insensitive_apfs`."""
    import tempfile

    from src.lease_plane.canonicalize import canonicalize, _is_case_insensitive

    if not _is_case_insensitive():
        pytest.skip("filesystem is case-sensitive; gate premise N/A")

    with tempfile.TemporaryDirectory() as d:
        # Materialize both spellings — realpath(strict=True) needs the path
        # to exist so we exercise the canonicalization path, not the ENOENT fallback.
        upper = Path(d) / "X.py"
        upper.write_text("")
        a = canonicalize(f"file://{upper}")
        b = canonicalize(f"file://{Path(d) / 'x.py'}")
        assert a == b, f"case-insensitive APFS must canonicalize equal: {a!r} != {b!r}"


def test_file_canonicalization_relative_components():
    """RFC v0.8 §7.12.1 / §9 — `..`-bearing paths canonicalize to the same form
    as the directly-spelled equivalent (`file:///x/../y/z.py` → `file:///y/z.py`).

    Pins the §9 named gate `test_file_canonicalization_relative_components`."""
    import tempfile

    from src.lease_plane.canonicalize import canonicalize

    with tempfile.TemporaryDirectory() as d:
        sub = Path(d) / "y"
        sub.mkdir()
        target = sub / "z.py"
        target.write_text("")
        via_dotdot = canonicalize(f"file://{d}/x/../y/z.py")
        direct = canonicalize(f"file://{target}")
        assert via_dotdot == direct, (
            f"`..`-component path must canonicalize to direct form: "
            f"{via_dotdot!r} != {direct!r}"
        )


def test_acquire_request_rejects_invalid_scheme():
    """RFC v0.8 §7.2 / §9 — Pydantic field_validator rejects surface_ids whose
    scheme is not in the canonical list (`AcquireRequest(surface_id='potato:foo', ...)`
    raises ValidationError).

    The implementation is at `src/lease_plane/models.py::AcquireRequest._validate_surface_id`.
    This test pins the §9 named gate `test_acquire_request_rejects_invalid_scheme`.
    """
    from pydantic import ValidationError

    from src.lease_plane import AcquireRequest
    from uuid import uuid4

    with pytest.raises(ValidationError) as exc_info:
        AcquireRequest(
            surface_id="potato:foo",
            holder_agent_uuid=uuid4(),
            holder_class="process_instance",
            holder_kind="remote_heartbeat",
            ttl_s=60,
        )
    msg = str(exc_info.value)
    assert "potato" in msg or "scheme" in msg.lower(), (
        f"Expected error to mention the offending scheme; got: {msg}"
    )


def test_acquire_request_surface_id_field_validator_wired():
    """RFC v0.8 §7.12.5: AcquireRequest auto-canonicalizes surface_id at the model boundary.
    Two equivalent inputs must produce the same .surface_id."""
    from src.lease_plane import AcquireRequest
    from uuid import uuid4

    holder = uuid4()

    def make(sid: str) -> AcquireRequest:
        return AcquireRequest(
            surface_id=sid,
            holder_agent_uuid=holder,
            holder_class="process_instance",
            holder_kind="remote_heartbeat",
            ttl_s=60,
        )

    a = make("capture:/win_b,win_a")
    b = make("capture:/win_a,win_b")
    assert a.surface_id == b.surface_id, (
        f"AcquireRequest must auto-canonicalize via field_validator; "
        f"got {a.surface_id!r} != {b.surface_id!r}"
    )

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make("not_a_real_scheme:foo")
