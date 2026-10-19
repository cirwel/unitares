"""The os.environ boundary check names the test that leaked, and only that one."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from tests.env_leak_guard import EnvDiff, diff_env

REPO = Path(__file__).resolve().parent.parent


# --- diff_env ------------------------------------------------------------------


def test_diff_env_reports_set_removed_and_changed_keys():
    diff = diff_env(
        {"KEEP": "1", "GONE": "x", "MOVED": "a"},
        {"KEEP": "1", "MOVED": "b", "NEW": "y"},
    )
    assert diff == EnvDiff(added=("NEW",), removed=("GONE",), changed=("MOVED",))
    assert diff.keys == {"NEW", "GONE", "MOVED"}
    assert diff.describe() == "set: NEW; removed: GONE; changed: MOVED"


def test_diff_env_is_empty_and_falsy_for_equal_environments():
    assert not diff_env({"A": "1"}, {"A": "1"})


def test_diff_env_skips_ignored_keys():
    assert not diff_env({}, {"PYTEST_CURRENT_TEST": "x"})
    assert not diff_env({"X": "1"}, {}, ignore=frozenset({"X"}))


def test_describe_names_keys_never_values():
    secret = "postgresql://user:hunter2@db/prod"
    text = diff_env({"DB_URL": "old"}, {"DB_URL": secret}).describe()
    assert "DB_URL" in text
    assert "hunter2" not in text and "old" not in text


# --- the plugin, in a real pytest run --------------------------------------------


def _run(tmp_path: Path, files: dict[str, str], known_leaks: str = "{}"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts =\n")
    (tmp_path / "conftest.py").write_text(textwrap.dedent(f"""
        from tests.env_leak_guard import EnvLeakGuard

        def pytest_configure(config):
            config.pluginmanager.register(
                EnvLeakGuard(known_leaks={known_leaks}), "env-leak-guard"
            )
    """))
    for name, body in files.items():
        (tmp_path / name).write_text(textwrap.dedent(body))
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("LEAK_", "PYTEST_"))
    }
    env["PYTHONPATH"] = str(REPO)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "no:cov",
         "-q", "-rA", str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )


def test_a_test_body_leak_is_named_and_repaired_for_the_next_test(tmp_path):
    result = _run(tmp_path, {
        "test_a.py": """
            import os

            def test_leaks():
                os.environ["LEAK_BODY"] = "1"
                os.environ["LEAK_SECOND"] = "1"

            def test_after_sees_a_clean_environment():
                assert "LEAK_BODY" not in os.environ
        """,
    })
    out = result.stdout
    assert result.returncode == 1, out
    assert "ERROR at teardown of test_leaks" in out
    assert "test test_a.py::test_leaks left os.environ changed: set: LEAK_BODY, LEAK_SECOND" in out
    assert "PASSED test_a.py::test_after_sees_a_clean_environment" in out
    assert "1 error" in out and "2 passed" in out


def test_monkeypatch_and_try_finally_are_clean(tmp_path):
    result = _run(tmp_path, {
        "test_a.py": """
            import os

            def test_monkeypatch(monkeypatch):
                monkeypatch.setenv("LEAK_MP", "1")
                monkeypatch.delenv("HOME")

            def test_finally():
                os.environ["LEAK_FINALLY"] = "1"
                try:
                    pass
                finally:
                    del os.environ["LEAK_FINALLY"]
        """,
    })
    assert result.returncode == 0, result.stdout
    assert "2 passed" in result.stdout


def test_higher_scoped_fixtures_may_hold_env_while_alive(tmp_path):
    """The case a function-scoped save/restore fixture gets wrong: a session
    fixture first requested mid-run, and a module fixture, set variables
    that stay set across several tests and are restored at their own
    teardown. None of that is a leak."""
    result = _run(tmp_path, {
        "test_a.py": """
            def test_before_any_fixture():
                pass
        """,
        "test_b.py": """
            import os
            import pytest

            @pytest.fixture(scope="session")
            def session_env():
                mp = pytest.MonkeyPatch()
                mp.setenv("LEAK_SESSION", "1")
                yield
                mp.undo()

            @pytest.fixture(scope="module")
            def module_env():
                os.environ["LEAK_MODULE"] = "1"
                yield
                del os.environ["LEAK_MODULE"]

            def test_one(session_env, module_env):
                assert os.environ["LEAK_SESSION"] == "1"

            def test_two(module_env):
                assert os.environ["LEAK_MODULE"] == "1"
        """,
        "test_c.py": """
            import os

            def test_session_value_still_held():
                assert os.environ["LEAK_SESSION"] == "1"
                assert "LEAK_MODULE" not in os.environ
        """,
    })
    assert result.returncode == 0, result.stdout
    assert "4 passed" in result.stdout


def test_a_higher_scoped_fixture_that_keeps_its_value_is_named(tmp_path):
    result = _run(tmp_path, {
        "test_a.py": """
            import os
            import pytest

            @pytest.fixture(scope="module")
            def sticky():
                os.environ["LEAK_FIXTURE"] = "1"
                yield

            def test_uses_it(sticky):
                pass
        """,
        "test_b.py": """
            import os

            def test_later_module_is_clean():
                assert "LEAK_FIXTURE" not in os.environ
        """,
    })
    out = result.stdout
    assert result.returncode == 1, out
    assert "module-scoped fixture 'sticky' left os.environ changed: set: LEAK_FIXTURE" in out
    # The test that merely used the fixture is not blamed for it.
    assert "test test_a.py::test_uses_it left" not in out
    assert "PASSED test_b.py::test_later_module_is_clean" in out


def test_an_allowlisted_leak_warns_instead_of_failing(tmp_path):
    result = _run(
        tmp_path,
        {
            "test_a.py": """
                import os

                def test_known():
                    os.environ["LEAK_KNOWN"] = "1"

                def test_unlisted_key():
                    os.environ["LEAK_KNOWN"] = "1"
            """,
        },
        known_leaks=(
            '{"test_a.py::test_known": (frozenset({"LEAK_KNOWN"}), "issue #0")}'
        ),
    )
    out = result.stdout
    assert result.returncode == 1, out
    assert "known os.environ leak by test test_a.py::test_known" in out
    assert "tracked in issue #0" in out
    assert "ERROR at teardown of test_known" not in out
    assert "ERROR at teardown of test_unlisted_key" in out


_NESTED_SETUP = """
    import os
    import pytest

    @pytest.fixture(scope="session")
    def session_env():
        os.environ["LEAK_SESSION"] = "1"
        yield
        SESSION_TEARDOWN

    @pytest.fixture(scope="module")
    def module_env(request):
        request.getfixturevalue("session_env")
        os.environ["LEAK_OUTER"] = "1"
        yield
        del os.environ["LEAK_OUTER"]

    def test_uses_module_fixture(module_env):
        assert os.environ["LEAK_SESSION"] == "1"
"""

_LATER_MODULE = """
    import os

    def test_session_value_survives_the_module_teardown():
        assert os.environ.get("LEAK_SESSION") == "1"
        assert "LEAK_OUTER" not in os.environ
"""


def test_a_nested_fixture_setup_is_credited_to_the_innermost_fixture(tmp_path):
    """A module fixture that pulls in a session fixture with
    ``request.getfixturevalue`` did not make the session fixture's change. Its
    teardown must not be blamed for it, and must not strip the variable while
    the session fixture is still alive."""
    result = _run(tmp_path, {
        "test_a.py": _NESTED_SETUP.replace(
            "SESSION_TEARDOWN", 'del os.environ["LEAK_SESSION"]'
        ),
        "test_b.py": _LATER_MODULE,
    })
    out = result.stdout
    assert result.returncode == 0, out
    assert "left os.environ changed" not in out
    assert "PASSED test_b.py::test_session_value_survives_the_module_teardown" in out


def test_a_nested_session_fixture_is_still_checked_at_its_own_teardown(tmp_path):
    result = _run(tmp_path, {
        "test_a.py": _NESTED_SETUP.replace("SESSION_TEARDOWN", "pass"),
        "test_b.py": _LATER_MODULE,
    })
    out = result.stdout
    assert result.returncode == 1, out
    assert (
        "session-scoped fixture 'session_env' left os.environ changed: "
        "set: LEAK_SESSION" in out
    )
    assert "module-scoped fixture 'module_env'" not in out
    assert "PASSED test_b.py::test_session_value_survives_the_module_teardown" in out


_SHARED_KEY = """
    import os
    import pytest

    @pytest.fixture(scope="session")
    def session_env():
        mp = pytest.MonkeyPatch()
        mp.setenv("LEAK_SHARED", "session")
        yield
        mp.undo()

    @pytest.fixture(scope="module")
    def module_env(request):
        request.getfixturevalue("session_env")
        found = os.environ["LEAK_SHARED"]
        # Back to the value the run started with: no net change against
        # this fixture's own entry snapshot, but an override of the session
        # fixture's value all the same.
        os.environ["LEAK_SHARED"] = "original"
        yield
        MODULE_TEARDOWN

    def test_one(module_env):
        assert os.environ["LEAK_SHARED"] == "original"

    def test_two(module_env):
        assert os.environ["LEAK_SHARED"] == "original"
"""

_SHARED_KEY_LATER = """
    import os

    def test_session_value_is_back(session_env):
        assert os.environ["LEAK_SHARED"] == "session"
"""


def _run_shared_key(tmp_path, module_teardown):
    return _run(
        tmp_path,
        {
            "test_a.py": _SHARED_KEY.replace("MODULE_TEARDOWN", module_teardown),
            "test_b.py": _SHARED_KEY_LATER,
        },
        conftest_extra=_SHARED_SESSION_FIXTURE,
        extra_env={"LEAK_SHARED": "original"},
    )


def test_an_outer_override_back_to_the_entry_value_is_tracked(tmp_path):
    """original -> nested session fixture sets 'session' -> the module fixture
    sets it back to 'original'. The module's value is live configuration while
    its tests run, not a test leak, and its teardown owes back 'session'."""
    result = _run_shared_key(
        tmp_path, 'os.environ["LEAK_SHARED"] = found'
    )
    out = result.stdout
    assert result.returncode == 0, out
    assert "left os.environ changed" not in out
    assert "3 passed" in out


def test_an_outer_override_not_given_back_is_named(tmp_path):
    result = _run_shared_key(tmp_path, "pass")
    out = result.stdout
    assert result.returncode == 1, out
    assert (
        "module-scoped fixture 'module_env' left os.environ changed: "
        "changed: LEAK_SHARED" in out
    )
    assert "test test_a.py::test_one left" not in out
    assert "PASSED test_b.py::test_session_value_is_back" in out
