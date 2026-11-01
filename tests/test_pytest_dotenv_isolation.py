"""Cold, focused pytest runs must not import account dotenv configuration."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("inherited_opt_out", [None, "0"])
def test_root_test_bootstrap_blocks_dotenv_unless_a_test_explicitly_opts_in(
    tmp_path, inherited_opt_out,
):
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts =\n")
    fixture = tmp_path / ".env"
    fixture.write_text("CHECKOUT_DOTENV_CANARY=fixture-only\n")
    (tmp_path / "test_cold.py").write_text(textwrap.dedent(f"""
        import os
        from dotenv import load_dotenv

        def test_implicit_loading_is_disabled():
            assert load_dotenv({str(fixture)!r}) is False
            assert os.getenv('CHECKOUT_DOTENV_CANARY') is None

        def test_explicit_fixture_loading_still_works(monkeypatch):
            with monkeypatch.context() as patch:
                patch.delenv('PYTHON_DOTENV_DISABLED', raising=False)
                patch.setenv('CHECKOUT_DOTENV_CANARY', 'original-fixture')
                assert load_dotenv({str(fixture)!r}, override=True) is True
                assert os.environ['CHECKOUT_DOTENV_CANARY'] == 'fixture-only'
            assert os.getenv('CHECKOUT_DOTENV_CANARY') is None
    """))
    # No account credentials reach the probe, even when its assertions fail.
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT")
           if key in os.environ}
    home = tmp_path / "home"
    home.mkdir()
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    if inherited_opt_out is not None:
        env["PYTHON_DOTENV_DISABLED"] = inherited_opt_out
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "conftest", "-p", "tests.conftest",
         "-p", "no:cacheprovider", "-q", str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
