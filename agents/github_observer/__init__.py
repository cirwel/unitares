"""GitHub outcome observer: facts about agent-signed pull requests as events.

An optional member (``docs/proposals/active/github-outcome-observer-v0.md``).
It reads GitHub with a token the deployment supplies and turns what happened
to a pull request whose body carries a ``UNITARES agent: <uuid>`` footer into
``github.*`` events attributed to that agent: the PR opened, merged, closed or
reopened; a CI run or check completed; a review record was posted.

This package holds the reading half and the dry run. It writes nothing to
UNITARES: the write path the operator chose on 2026-11-14 (a narrow
``POST /v1/external/events`` route behind the operator credential) lands with
that route.

It reads GitHub with GET requests only, never reads a UNITARES table, never
branches on an agent or resident name, and attaches no score, verdict or
severity to anything it reports. A failing CI run on an open pull request is
the process working.
"""
