# Contributing to UNITARES

UNITARES is a solo-developed deep-tech project in active research-and-production use. Contributions are welcome but not the primary path — most issues get worked through directly by the maintainer, and the architecture is still evolving (paper v6 → v7).

If you're considering a contribution, please **open an issue first** describing what you'd like to change. Saves both of us from wasted work on something that doesn't fit the direction.

## Quick setup

```bash
git clone https://github.com/cirwel/unitares.git && cd unitares
docker compose up                  # Postgres + AGE + pgvector + Redis + server
make demo                          # 60-second scripted trajectory in another shell
```

Bare-metal install (Homebrew Postgres + native Python) is in [`docs/install/PLAYBOOK.md`](docs/install/PLAYBOOK.md). Architecture overview is in [`docs/UNIFIED_ARCHITECTURE.md`](docs/UNIFIED_ARCHITECTURE.md).

## Tests

```bash
make test                          # primary; uses tree-hash cache (.test-cache/)
pytest tests/test_<specific>.py    # single test file
```

New behavior needs a test. New tests should live in `tests/` alongside the existing ones; integration tests hit a real Postgres (not mocks) — see the test harness docs in `tests/` if you're touching the database layer.

## Pull request conventions

- **Commit messages:** imperative mood, scope prefix when useful (`docs:`, `feat(lease-plane):`, `fix(identity):`). Body explains the *why*. No AI-attribution footers.
- **One topic per PR.** If a change touches the identity ontology *and* the lease plane, those are two PRs unless you can convince the maintainer otherwise.
- **No `git add -A`** — stage by name. The `data/` tree contains runtime state that's easy to accidentally commit.
- **Update tests in the same PR.** Bug fixes that don't include a regression test won't merge.
- **Identity-touching changes** require reading [`docs/ontology/identity.md`](docs/ontology/identity.md) and [`AGENTS.md`](AGENTS.md) first. The identity layer is the most constraint-laden part of the system.

## Code style

- Python 3.12+, formatted with `ruff format`. Lint with `ruff check`. CI enforces both.
- Type hints encouraged but not enforced repo-wide; new modules should be fully typed.
- No new SQLite or in-process state stores — one Postgres, schema-isolated. See [`docs/operations/database_architecture.md`](docs/operations/database_architecture.md).

## Licensing

By submitting a contribution you agree it's licensed under the [Apache License 2.0](LICENSE), same as the rest of the project. No CLA required — `inbound = outbound`.

## What I won't merge

- Substrate-migration proposals without falsifying evidence — the current Python + Postgres stack is the deliberate choice; see commit history and `docs/proposals/` for prior considerations
- Backwards-compatibility shims for already-removed identity primitives (the `resolve_by_name_claim` / STRICT env / etc. removals were intentional; see `docs/ontology/s1-continuity-token-retirement.md`)
- Feature flags that exist only to soften a sharp behavioral edge — if the new behavior is right, ship it; if it isn't, don't ship it
- Cosmetic-only refactors without a behavior or readability win

## Security

See [`SECURITY.md`](SECURITY.md). Do not file security issues as public PRs or issues.

## Reporting issues that aren't security

Open a GitHub issue. Include version (`cat VERSION`), Python version, Postgres version, and the smallest reproduction you have. Logs in `data/logs/` and `data/audit_log.jsonl` are often what's needed — strip any agent identifiers you don't want to share.
