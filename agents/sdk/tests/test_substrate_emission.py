"""Tests for unitares_sdk._substrate (RFC §7.13 SDK port).

Covers the substrate-state emission path used by UnitaresClient.checkin and
SyncUnitaresClient.checkin to write to lease_plane.surface_leases per
RFC §7.13.4 dual-run authority. Mocks the lease-plane SDK so the test runs
without the unitares parent process on path.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest


_SHIM_INSTALLED_KEYS: list[str] = []


def _install_lease_plane_shim() -> None:
    """Install minimal `src.lease_plane.{client,models}` shim before
    `unitares_sdk._substrate` performs its lazy imports — but ONLY when
    the real modules are not importable. In the unitares parent repo
    the real modules exist and shadowing them with a shim breaks
    downstream tests that depend on the real interface (e.g.
    test_lease_plane_deprecate_cli.py needs LeaseHTTPRequest from the
    real client module).

    We track which modules WE installed so the module-teardown finalizer
    can remove only our additions without nuking real modules.
    """
    try:
        import src.lease_plane.client  # noqa: F401
        import src.lease_plane.models  # noqa: F401
        # Real modules already importable — don't shadow.
        return
    except ImportError:
        pass

    for key in ("src", "src.lease_plane"):
        if key not in sys.modules:
            sys.modules[key] = types.ModuleType(key)
            _SHIM_INSTALLED_KEYS.append(key)

    client_mod = types.ModuleType("src.lease_plane.client")
    models_mod = types.ModuleType("src.lease_plane.models")

    class LeasePlaneClientConfig:
        def __init__(self, **kw):
            self.kw = kw

    class LeasePlaneClient:
        def __init__(self, config):
            self.config = config

        def acquire(self, request):
            raise NotImplementedError

        def renew(self, request):
            raise NotImplementedError

    class LeasePlaneDisabledClient:
        def acquire(self, request):
            return AcquireServiceUnavailable()

        def renew(self, request):
            return SimpleError(error="service_unavailable")

    class LeaseRecord:
        def __init__(self, lease_id):
            self.lease_id = lease_id

    class AcquireOk:
        def __init__(self, lease, idempotent=False):
            self.ok = True
            self.lease = lease
            self.idempotent = idempotent

    class AcquireServiceUnavailable:
        def __init__(self):
            self.ok = False
            self.error = "service_unavailable"

    class SimpleOk:
        def __init__(self):
            self.ok = True

    class SimpleError:
        def __init__(self, error="not_found"):
            self.ok = False
            self.error = error

    class AcquireRequest:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    class RenewRequest:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    client_mod.LeasePlaneClient = LeasePlaneClient
    client_mod.LeasePlaneClientConfig = LeasePlaneClientConfig
    client_mod.LeasePlaneDisabledClient = LeasePlaneDisabledClient
    models_mod.LeaseRecord = LeaseRecord
    models_mod.AcquireOk = AcquireOk
    models_mod.AcquireServiceUnavailable = AcquireServiceUnavailable
    models_mod.AcquireRequest = AcquireRequest
    models_mod.RenewRequest = RenewRequest
    models_mod.SimpleOk = SimpleOk
    models_mod.SimpleError = SimpleError

    sys.modules["src.lease_plane.client"] = client_mod
    sys.modules["src.lease_plane.models"] = models_mod
    _SHIM_INSTALLED_KEYS.extend(["src.lease_plane.client", "src.lease_plane.models"])


_install_lease_plane_shim()


def _restore_modules_after_test_module():
    """Remove shim keys we installed so subsequent test files don't see a
    shadowed `src.lease_plane.client`. Run as module-teardown so the test
    file's tests can still use the shim within their own scope."""
    import importlib

    # Drop our shim entries, then re-attempt real-import for the parent
    # tests' benefit. Real modules will repopulate sys.modules naturally
    # on next import attempt.
    for key in _SHIM_INSTALLED_KEYS:
        sys.modules.pop(key, None)
    try:
        importlib.import_module("src.lease_plane.client")
    except ImportError:
        pass


def teardown_module(_module):  # pytest module-level finalizer
    _restore_modules_after_test_module()


from unitares_sdk._substrate import (  # noqa: E402
    KNOWN_RESIDENT_NAMES,
    _build_substrate_state,
    _LeaseCache,
    _resolve_resident_name,
    emit_substrate_observation,
)


HOLDER_UUID = "abcdefab-cdef-4abc-8def-abcdefabcdef"


@pytest.fixture
def cache():
    return _LeaseCache()


def _metrics():
    return {"E": 0.36, "I": 0.81, "S": 0.22, "V": 0.07,
            "coherence": 0.7, "risk": 0.2}


def test_resident_roster_loaded_from_env():
    """The resident roster is deployment config read from UNITARES_RESIDENTS.

    Both the SDK (_substrate.KNOWN_RESIDENT_NAMES) and core
    (src/grounding/class_indicator.KNOWN_RESIDENT_LABELS) read the SAME env
    var — the env var NAME is the cross-package contract, since the SDK
    cannot import from src/. conftest.py configures the canonical fleet for
    this suite; assert the roster reflects that configuration rather than a
    hardcoded literal."""
    from unitares_sdk._substrate import RESIDENT_ROSTER_ENV

    assert RESIDENT_ROSTER_ENV == "UNITARES_RESIDENTS"
    assert KNOWN_RESIDENT_NAMES == {
        "Lumen", "Vigil", "Sentinel", "Watcher", "Steward", "Chronicler",
    }


def test_parse_resident_roster_default_empty():
    """Unset/empty roster => no named residents (user-agnostic default)."""
    from unitares_sdk._substrate import parse_resident_roster

    assert parse_resident_roster(None) == frozenset()
    assert parse_resident_roster("") == frozenset()
    assert parse_resident_roster("  ") == frozenset()
    assert parse_resident_roster("Vigil, Sentinel ,Lumen") == {
        "Vigil", "Sentinel", "Lumen",
    }


def test_resolve_resident_name_known_returns_name():
    for name in ("Vigil", "Sentinel", "Watcher", "Chronicler", "Lumen", "Steward"):
        assert _resolve_resident_name(name) == name


def test_resolve_resident_name_unknown_returns_none():
    assert _resolve_resident_name("ephemeral-worker") is None
    assert _resolve_resident_name("") is None
    assert _resolve_resident_name("vigil") is None  # case-sensitive


def test_build_substrate_state_includes_eisv_and_sensor():
    state = _build_substrate_state(_metrics())
    assert state["E"] == 0.36
    assert state["I"] == 0.81
    assert state["S"] == 0.22
    assert state["V"] == 0.07
    assert state["sensor"]["status"] == "healthy"


def test_build_substrate_state_handles_missing_keys():
    """Defensive: metrics from older governance servers may lack some keys."""
    state = _build_substrate_state({})
    assert state["E"] == 0.0
    assert state["I"] == 0.0
    assert state["sensor"]["status"] == "healthy"


def test_emit_skips_silently_for_non_resident_name(cache):
    """Non-resident names must not write to resident:/ surfaces. The DB CHECK
    `substrate_state_only_on_resident_kind` would reject anyway, but client-
    side gating is friendlier (no failed HTTP call, no log noise)."""
    client = MagicMock()
    result = emit_substrate_observation(
        resident_name="ephemeral-worker",
        holder_uuid=HOLDER_UUID,
        metrics=_metrics(),
        cache=cache,
        client=client,
    )
    assert result is False
    client.acquire.assert_not_called()
    client.renew.assert_not_called()


def test_emit_skips_for_empty_holder_uuid(cache):
    client = MagicMock()
    result = emit_substrate_observation(
        resident_name="Vigil",
        holder_uuid="",
        metrics=_metrics(),
        cache=cache,
        client=client,
    )
    assert result is False
    client.acquire.assert_not_called()


def test_emit_skips_for_empty_metrics(cache):
    client = MagicMock()
    result = emit_substrate_observation(
        resident_name="Vigil",
        holder_uuid=HOLDER_UUID,
        metrics={},
        cache=cache,
        client=client,
    )
    assert result is False
    client.acquire.assert_not_called()


def test_first_emit_does_acquire_with_correct_surface(cache):
    """First call after _LeaseCache reset triggers acquire with the per-resident
    surface_id and substrate-earned holder_class. surface_id is lowercased."""
    from src.lease_plane.models import AcquireOk, LeaseRecord

    client = MagicMock()
    _mock_lease = MagicMock()
    _mock_lease.lease_id = uuid4()
    _mock_ok = MagicMock(spec=AcquireOk)
    _mock_ok.lease = _mock_lease
    _mock_ok.idempotent = False
    client.acquire.return_value = _mock_ok

    result = emit_substrate_observation(
        resident_name="Vigil",
        holder_uuid=HOLDER_UUID,
        metrics=_metrics(),
        cache=cache,
        client=client,
    )

    assert result is True
    client.acquire.assert_called_once()
    request = client.acquire.call_args[0][0]
    assert request.surface_id == "resident:/vigil"
    assert request.holder_kind == "remote_heartbeat"
    assert request.holder_class == "substrate_earned"
    assert request.ttl_s == 1000  # §7.5 v0.9 measurement
    assert request.holder_agent_uuid == UUID(HOLDER_UUID)
    assert request.substrate_state["E"] == 0.36
    assert request.substrate_state["sensor"]["status"] == "healthy"
    client.renew.assert_not_called()
    assert cache.lease_id is not None


def test_subsequent_emits_use_renew(cache):
    from src.lease_plane.models import AcquireOk, LeaseRecord, SimpleOk

    lease_id = uuid4()
    client = MagicMock()
    _mock_lease = MagicMock()
    _mock_lease.lease_id = lease_id
    _mock_ok = MagicMock(spec=AcquireOk)
    _mock_ok.lease = _mock_lease
    _mock_ok.idempotent = False
    client.acquire.return_value = _mock_ok
    client.renew.return_value = MagicMock(spec=SimpleOk)

    for _ in range(3):
        assert emit_substrate_observation(
            resident_name="Sentinel",
            holder_uuid=HOLDER_UUID,
            metrics=_metrics(),
            cache=cache,
            client=client,
        ) is True

    assert client.acquire.call_count == 1
    assert client.renew.call_count == 2

    for renew_call in client.renew.call_args_list:
        request = renew_call[0][0]
        assert request.lease_id == lease_id
        assert request.substrate_state["sensor"]["status"] == "healthy"


def test_acquire_exception_returns_false_does_not_raise(cache):
    """RFC §7.13.4 observational-only contract: lease failures MUST NOT
    propagate. The caller (SDK.checkin) wraps in try/except too, but
    defending at this layer prevents log noise and keeps semantics clear."""
    client = MagicMock()
    client.acquire.side_effect = RuntimeError("network error")

    result = emit_substrate_observation(
        resident_name="Watcher",
        holder_uuid=HOLDER_UUID,
        metrics=_metrics(),
        cache=cache,
        client=client,
    )
    assert result is False
    assert cache.lease_id is None


def test_renew_failure_resets_cache_for_reacquire(cache):
    """Lease loss (renew→non-OK or exception) clears the cache so the next
    emission re-acquires. This is the recovery semantic for lease expiry,
    server restart, etc."""
    from src.lease_plane.models import AcquireOk, LeaseRecord, SimpleError, SimpleOk

    client = MagicMock()
    _mock_lease = MagicMock()
    _mock_lease.lease_id = uuid4()
    _mock_ok = MagicMock(spec=AcquireOk)
    _mock_ok.lease = _mock_lease
    _mock_ok.idempotent = False
    client.acquire.return_value = _mock_ok
    client.renew.side_effect = [MagicMock(spec=SimpleError), MagicMock(spec=SimpleOk)]

    assert emit_substrate_observation(
        resident_name="Chronicler",
        holder_uuid=HOLDER_UUID,
        metrics=_metrics(),
        cache=cache,
        client=client,
    ) is True
    assert cache.lease_id is not None

    # Cycle 2: renew fails
    assert emit_substrate_observation(
        resident_name="Chronicler",
        holder_uuid=HOLDER_UUID,
        metrics=_metrics(),
        cache=cache,
        client=client,
    ) is False
    assert cache.lease_id is None  # reset

    # Cycle 3: re-acquire
    _mock_lease = MagicMock()
    _mock_lease.lease_id = uuid4()
    _mock_ok = MagicMock(spec=AcquireOk)
    _mock_ok.lease = _mock_lease
    _mock_ok.idempotent = False
    client.acquire.return_value = _mock_ok
    assert emit_substrate_observation(
        resident_name="Chronicler",
        holder_uuid=HOLDER_UUID,
        metrics=_metrics(),
        cache=cache,
        client=client,
    ) is True
    assert client.acquire.call_count == 2


def test_disabled_client_skips_silently(cache):
    """No LEASE_PLANE_BEARER_TOKEN → disabled client → returns
    AcquireServiceUnavailable on acquire → emit returns False without raising.
    Matches the §7.13.4 default-deny posture."""
    from src.lease_plane.client import LeasePlaneDisabledClient

    client = LeasePlaneDisabledClient()
    result = emit_substrate_observation(
        resident_name="Vigil",
        holder_uuid=HOLDER_UUID,
        metrics=_metrics(),
        cache=cache,
        client=client,
    )
    assert result is False


def test_each_resident_gets_distinct_surface_id(cache):
    """All four remaining residents (post-Steward) map to their own surface_id.
    Verifies the f-string uses the matched resident name, not a constant."""
    from src.lease_plane.models import AcquireOk, LeaseRecord

    for name in ("Vigil", "Sentinel", "Watcher", "Chronicler"):
        local_cache = _LeaseCache()
        client = MagicMock()
        _mock_lease = MagicMock()
        _mock_lease.lease_id = uuid4()
        _mock_ok = MagicMock(spec=AcquireOk)
        _mock_ok.lease = _mock_lease
        _mock_ok.idempotent = False
        client.acquire.return_value = _mock_ok
        emit_substrate_observation(
            resident_name=name,
            holder_uuid=HOLDER_UUID,
            metrics=_metrics(),
            cache=local_cache,
            client=client,
        )
        request = client.acquire.call_args[0][0]
        assert request.surface_id == f"resident:/{name.lower()}"
