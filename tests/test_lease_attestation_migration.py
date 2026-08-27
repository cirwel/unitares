"""Static contract for the durable single-use lease-attestation ledger."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_migration_067_is_registered_single_use_and_expiry_indexed() -> None:
    sql = (ROOT / "db/postgres/migrations/067_lease_attestation_nonces.sql").read_text()
    assert "lease_plane.consumed_identity_attestations" in sql
    assert "PRIMARY KEY (issuer, jti)" in sql
    assert "expires_at" in sql
    assert "VALUES (67, 'lease_attestation_nonces'" in sql


def test_fresh_docker_database_applies_migration_067() -> None:
    init = (ROOT / "db/postgres/docker-initdb.sh").read_text()
    assert 'if (( 10#$version >= 31 )); then' in init
