# Project Governance

UNITARES is currently a solo-maintained open-source research project. Kenny Wang
([@cirwel](https://github.com/cirwel)) is the maintainer and release authority.
That ownership model is explicit so users can evaluate the project's bus factor
and support expectations without guessing.

## Decisions

- Bugs, proposals, and evidence gaps are tracked in public issues.
- Material changes land through pull requests with automated checks.
- Deployed behavior, research targets, and historical proposals are documented
  as separate categories.
- Decisions prefer reproducible evidence, compatibility, and a smaller public
  contract over feature count.
- The maintainer makes the final merge and release decision and records
  non-obvious rationale in the PR, issue, or proposal that carried the work.

Technical disagreement and negative results are welcome. Participation is
governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), and security reports follow
[SECURITY.md](SECURITY.md).

## Contributions and maintainership

Open an issue before a substantial contribution; see
[CONTRIBUTING.md](CONTRIBUTING.md). Sustained contributors may be granted triage
or maintenance responsibility after demonstrating sound review judgment,
respect for compatibility and security boundaries, and reliable follow-through.

[`CODEOWNERS`](.github/CODEOWNERS) records current ownership. A second maintainer would be added there
before branch protection begins requiring an external approval; the project
does not simulate independent review while only one maintainer exists.

## Releases and project direction

The [roadmap](ROADMAP.md) describes current priorities without promising dates.
The [release process](docs/operations/RELEASE_PROCESS.md) defines version,
artifact, and provenance checks. Release notes and the changelog are the record
of shipped behavior; proposals are not commitments.
