"""GitHub outcome observer: facts about agent-signed pull requests as events.

An optional member (``docs/proposals/active/github-outcome-observer-v0.md``).
It reads GitHub with a token the deployment supplies and turns what happened
to a pull request whose body carries a ``UNITARES agent: <uuid>`` footer into
``github.*`` events attributed to that agent: the PR opened, merged, closed or
reopened; a CI run or check completed; a review record was posted.

This package holds the GitHub outcome observer: the reading half, the dry run,
and the live writer that posts ``github.*`` events to ``POST /v1/external/events``
with durable local and server-side fingerprint deduplication.

It reads GitHub with GET requests only, never reads a UNITARES table, never
branches on an agent or resident name, and attaches no score, verdict or
severity to anything it reports. A failing CI run on an open pull request is
the process working.
"""
