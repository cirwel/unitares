"""Name the test that leaks ``os.environ``.

A test that writes ``os.environ`` directly and never restores it changes the
environment of every test after it, and of every subprocess those tests spawn.
The damage shows up far from the cause: on 2026-09-09 the session-scoped
``mcp_test_server`` fixture in ``test_unitares_cli_script.py`` copied
``os.environ`` into a server process and inherited pool bounds an earlier
Postgres test had set in its body.

An autouse function-scoped fixture that saves and restores the environment
cannot fix this. Higher-scoped fixtures set up before it, so it would snapshot
the already-polluted value and faithfully restore the pollution. This module
checks at the boundary instead: after every test's teardown it compares
``os.environ`` with what the environment should be at that point, and fails the
test whose teardown left a difference, naming it and the keys involved.

"What the environment should be" starts as the environment when the first test
begins, so anything a conftest or a test module sets while it imports is part
of the baseline. It then follows the fixtures above function scope: a session,
package, module or class fixture may set a variable for as long as it is alive,
and owes its prior value back when it is torn down. Its setup is measured and
folded into the expectation; its teardown is checked against the value it
found. A fixture that does not give the value back is named as the leaker.

On a reported leak the environment is put back to the expectation, so the run
continues hermetic and every leak is reported once, against the test that
caused it, instead of once per test after it.

Values are never printed, only key names: the environment carries database
URLs and tokens.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

# Variables pytest or the interpreter changes on its own between tests.
IGNORED_KEYS = frozenset({
    # pytest sets this during each phase and removes it after teardown.
    "PYTEST_CURRENT_TEST",
})

# Known leakers that are not fixed yet. Each entry is a test nodeid (or a
# ``fixture:<name>`` for a higher-scoped fixture), the keys it is allowed to
# leave behind, and a pointer to the issue or PR that owns the fix. An
# allowlisted leak is still repaired and still printed; it just does not fail
# the test. Add to this only with a pointer, and remove the entry with the fix.
KNOWN_LEAKS: dict[str, tuple[frozenset[str], str]] = {}


@dataclass(frozen=True)
class EnvDiff:
    """Keys whose value in ``actual`` differs from ``expected``."""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.changed)

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(self.added + self.removed + self.changed)

    def describe(self) -> str:
        parts = []
        for label, keys in (
            ("set", self.added),
            ("removed", self.removed),
            ("changed", self.changed),
        ):
            if keys:
                parts.append(f"{label}: {', '.join(keys)}")
        return "; ".join(parts)


def diff_env(
    expected: Mapping[str, str],
    actual: Mapping[str, str],
    ignore: frozenset[str] = IGNORED_KEYS,
) -> EnvDiff:
    """Compare two environments by key; never looks at a value beyond ``!=``."""
    added = sorted(k for k in actual.keys() - expected.keys() if k not in ignore)
    removed = sorted(k for k in expected.keys() - actual.keys() if k not in ignore)
    changed = sorted(
        k for k in expected.keys() & actual.keys()
        if k not in ignore and expected[k] != actual[k]
    )
    return EnvDiff(tuple(added), tuple(removed), tuple(changed))


def restore_env(target: Mapping[str, str], keys: frozenset[str]) -> None:
    """Make ``os.environ[k]`` match ``target`` for each of ``keys``."""
    for key in keys:
        if key in target:
            os.environ[key] = target[key]
        else:
            os.environ.pop(key, None)


@dataclass
class _FixtureSpan:
    """What a higher-scoped fixture changed in setup, and what it found."""

    argname: str
    scope: str
    prior: dict[str, str | None] = field(default_factory=dict)


class EnvLeakGuard:
    """pytest plugin; register one instance per session."""

    def __init__(self, known_leaks: Mapping[str, tuple[frozenset[str], str]] = KNOWN_LEAKS):
        self.known_leaks = dict(known_leaks)
        self.expected: dict[str, str] | None = None
        self._spans: dict[int, list[_FixtureSpan]] = {}
        self._fixture_leaks: list[tuple[str, str, EnvDiff]] = []

    # -- baseline ---------------------------------------------------------

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item):
        if self.expected is None:
            self.expected = dict(os.environ)

    # -- higher-scoped fixtures ------------------------------------------

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        if fixturedef.scope == "function" or self.expected is None:
            return (yield)
        before = dict(os.environ)
        try:
            return (yield)
        finally:
            delta = diff_env(before, os.environ)
            if delta:
                span = _FixtureSpan(fixturedef.argname, fixturedef.scope)
                for key in delta.keys:
                    span.prior[key] = before.get(key)
                    if key in os.environ:
                        self.expected[key] = os.environ[key]
                    else:
                        self.expected.pop(key, None)
                self._spans.setdefault(id(fixturedef), []).append(span)

    def pytest_fixture_post_finalizer(self, fixturedef, request):
        spans = self._spans.get(id(fixturedef))
        if not spans or self.expected is None:
            return
        span = spans.pop()
        if not spans:
            del self._spans[id(fixturedef)]
        prior = {k: v for k, v in span.prior.items() if v is not None}
        leaked = diff_env(
            prior, {k: os.environ[k] for k in span.prior if k in os.environ}
        )
        if leaked:
            self._fixture_leaks.append((span.argname, span.scope, leaked))
        # The fixture is gone; the expectation goes back to what it found,
        # whether or not it put it back itself.
        for key, value in span.prior.items():
            if value is None:
                self.expected.pop(key, None)
            else:
                self.expected[key] = value

    # -- the boundary check ------------------------------------------------

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_teardown(self, item, nextitem):
        try:
            result = yield
        except BaseException:
            # The teardown already failed; repair the environment but let its
            # own error be the one reported.
            self._check(item, report=False)
            raise
        self._check(item)
        return result

    def _check(self, item, report: bool = True) -> None:
        if self.expected is None:
            return
        fixture_leaks, self._fixture_leaks = self._fixture_leaks, []
        diff = diff_env(self.expected, os.environ)
        if not diff:
            return
        restore_env(self.expected, diff.keys)
        if not report:
            return

        culprits = []
        unexplained = set(diff.keys)
        for argname, scope, leaked in fixture_leaks:
            culprits.append((f"fixture:{argname}", f"{scope}-scoped fixture {argname!r}", leaked))
            unexplained -= leaked.keys
        if unexplained:
            culprits.append((
                item.nodeid,
                f"test {item.nodeid}",
                EnvDiff(
                    tuple(k for k in diff.added if k in unexplained),
                    tuple(k for k in diff.removed if k in unexplained),
                    tuple(k for k in diff.changed if k in unexplained),
                ),
            ))

        failures = []
        for key, who, leaked in culprits:
            allowed, pointer = self.known_leaks.get(key, (frozenset(), ""))
            if leaked.keys <= allowed:
                item.warn(pytest.PytestWarning(
                    f"known os.environ leak by {who} ({leaked.describe()}), "
                    f"repaired; tracked in {pointer}"
                ))
                continue
            failures.append(f"{who} left os.environ changed: {leaked.describe()}")
        if failures:
            pytest.fail(
                "os.environ leak detected at teardown of "
                f"{item.nodeid} (restored for the rest of the run):\n  "
                + "\n  ".join(failures)
                + "\nRestore it in the test (monkeypatch.setenv/delenv, or a "
                "try/finally), or in the fixture's teardown.",
                pytrace=False,
            )
