"""The lease plane and the governance server agree on the published bearer digest.

Both hold the sha256 of the LEASE_PLANE_BEARER_TOKEN default that
docker-compose.yml publishes: src/insecure_defaults.py for the server's
refusal and warning, and the lease plane's Application module for its boot
warning. tests/test_insecure_defaults.py pins the Python copy to the compose
file and published_default_test.exs pins the Elixir copy; this pins the two to
each other, so a rotation that updates one language only fails here too.
"""

import re
from pathlib import Path

from src.insecure_defaults import PUBLISHED_DEFAULT_SHA256

APPLICATION_EX = (
    Path(__file__).resolve().parents[1]
    / "elixir/lease_plane/lib/unitares_lease_plane/application.ex"
)


def test_elixir_and_python_hold_the_same_bearer_digest():
    match = re.search(r'@published_bearer_sha256 "([0-9a-f]{64})"', APPLICATION_EX.read_text())
    assert match, "application.ex no longer declares @published_bearer_sha256"
    assert match.group(1) == PUBLISHED_DEFAULT_SHA256["LEASE_PLANE_BEARER_TOKEN"]
