# Changelog fragments

A pull request that wants a changelog line adds **one new file** here instead
of editing [`docs/CHANGELOG.md`](../CHANGELOG.md). Every PR used to write into
the same `## [Unreleased]` subsection, so each merge conflicted every other
open PR with an entry, and resolving the conflict re-keyed its review. A new
file with a unique name cannot conflict, and a base merge never touches it.

## Format

Name the file `<section>-<slug>.md`: lowercase letters, digits and hyphens,
for example `fixed-lease-renewal-race.md`. The body is the entry exactly as
it would appear in the changelog, a markdown bullet with the PR reference
when you know it:

```markdown
- **scope:** what changed, for whom, and what it does not claim (#NNNN).
```

One file is one section. A change that belongs under two sections gets two
files. Do not add a heading; the file name chooses it.

## Sections

| Prefix | Lands under |
| --- | --- |
| `breaking` | `### Breaking` |
| `added` | `### Added` |
| `changed` | `### Changed` |
| `deprecated` | `### Deprecated` |
| `fixed` | `### Fixed` |
| `removed` | `### Removed` |
| `security` | `### Security` |
| `documentation` | `### Documentation` |
| `tests` | `### Tests` |
| `validation` | `### Validation` |

`python3 scripts/dev/changelog_assemble.py --check` validates the names and
bodies; the Release Seams workflow runs it on every PR.

## At release

The release cut runs `python3 scripts/dev/changelog_assemble.py` as its first
step, before it writes the version header. That folds every fragment into
`## [Unreleased]` under its subsection, ordered by file name, and deletes the
fragments. This README is never folded in. See
[`RELEASE_PROCESS.md`](../operations/RELEASE_PROCESS.md), step 2.
