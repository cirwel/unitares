"""Back-compat aliases — the canonical home of this package is
``unitares_sdk.lease_plane`` (moved so the SDK is standalone: the public
agent contract must not import ``src.*``, and the substrate-emission path
in ``unitares_sdk._substrate`` depends on this client).

These are *aliases*, not re-exports: ``sys.modules`` entries for every
``src.lease_plane[.submodule]`` name point at the SAME module objects as
the canonical names (with the server-local ``queries`` helper module
attached onto the aliased package so both ``from src.lease_plane.queries
import ...`` and ``import src.lease_plane.queries; src.lease_plane.queries``
hit that identical module object). Imports and ``patch()`` targets using
either spelling therefore hit identical module dicts — existing server code
and tests keep working unchanged, including attribute monkeypatching (e.g.
``src.lease_plane.PROTOCOL_VERSION``, ``src.lease_plane.client`` transport
internals).

New code should import from ``unitares_sdk.lease_plane`` directly.
"""

import sys

import unitares_sdk.lease_plane as _canonical
from unitares_sdk.lease_plane import advisory, canonicalize, client, models

# Load server-local SQL query helpers before self-replacement swaps
# ``sys.modules[__name__]`` to ``unitares_sdk.lease_plane`` (whose ``__path__``
# does not include ``src/lease_plane``). Attach it onto ``_canonical`` as well
# so attribute access (``import src.lease_plane.queries; src.lease_plane.queries``)
# resolves to the same module object as ``from src.lease_plane.queries import ...``.
from . import queries as _queries

setattr(_canonical, "queries", _queries)

for _name, _mod in (
    ("client", client),
    ("models", models),
    ("advisory", advisory),
    ("canonicalize", canonicalize),
    ("queries", _queries),
):
    sys.modules[f"{__name__}.{_name}"] = _mod

# Self-replacement: ``src.lease_plane`` *is* the canonical package object.
sys.modules[__name__] = _canonical
