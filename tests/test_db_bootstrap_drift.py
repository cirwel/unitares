"""The shared-test-database drift detector.

`governance_test` is shared across every worktree and never reset, while the
bootstrap applies every migration in the CURRENT checkout. The database
accumulates the union of every branch that has run tests, so an unmerged
branch's migration silently becomes part of the schema master's tests read.

Observed 2026-08-28: unmerged migration 069 seeded a `topic` row into
`lease_plane.surface_kind_catalog`, and a migration-027 test asserting exact
catalog contents failed on master and on every other branch simultaneously.
The cost was not the failure — it was that the failure looked like a flake, so
it invited a retry rather than a diagnosis.
"""

from __future__ import annotations

import pytest

from tests import test_db_utils as bootstrap


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, *_args, **_kwargs):
        return self._rows


def _on_disk_versions():
    return sorted(
        int(p.name.split("_", 1)[0])
        for p in (bootstrap._PROJECT_ROOT / "db/postgres/migrations").glob("*.sql")
        if p.name.split("_", 1)[0].isdigit()
    )


@pytest.mark.asyncio
async def test_silent_when_database_matches_the_checkout(capsys):
    """No warning when every applied migration exists in this tree.

    A detector that always fires is noise, and noise is how the next real drift
    gets scrolled past.
    """
    rows = [{"version": v, "name": f"m{v}"} for v in _on_disk_versions()]
    await bootstrap._report_foreign_migrations(_Conn(rows))
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_names_a_migration_the_checkout_does_not_have(capsys):
    rows = [{"version": v, "name": f"m{v}"} for v in _on_disk_versions()]
    rows.append({"version": 999, "name": "from_another_branch"})

    await bootstrap._report_foreign_migrations(_Conn(rows))
    out = capsys.readouterr().out

    # Named, not merely counted — "1 unexpected migration" sends nobody anywhere.
    assert "999_from_another_branch" in out
    assert "AHEAD of this branch" in out
    # The reader is told what to suspect before reaching for "flaky".
    assert "flaky" in out


@pytest.mark.asyncio
async def test_reports_but_never_mutates(capsys):
    """The remedy is printed, not taken.

    Dropping a database that other worktrees are concurrently using would trade
    a confusing failure for a destructive one, so the detector only ever writes
    to stdout. `_Conn` exposes no execute(); a mutating implementation raises.
    """
    rows = [{"version": 999, "name": "foreign"}]
    await bootstrap._report_foreign_migrations(_Conn(rows))
    assert "dropdb governance_test" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_absent_tracking_table_is_not_an_error(capsys):
    """A fresh database has no core.schema_migrations yet — and a bootstrap
    helper must never be the reason the suite cannot start."""

    class _Missing:
        async def fetch(self, *_args, **_kwargs):
            raise RuntimeError('relation "core.schema_migrations" does not exist')

    await bootstrap._report_foreign_migrations(_Missing())
    assert capsys.readouterr().out == ""
