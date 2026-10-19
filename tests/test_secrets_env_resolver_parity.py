"""Every copy of the secrets-env-file resolver must agree.

The secrets env file is resolved in many places: Python helpers (the doctor,
the installer, the lease-plane deprecation CLI, the sentinel plist tool) and a
shell snippet copied into each BEAM ``start.sh`` and several ops scripts. The
order is the same everywhere:

1. ``$UNITARES_SECRETS_ENV`` when set (even if that file does not exist);
2. else ``~/.config/unitares/secrets.env``;
3. else the pre-2026-09 ``~/.config/cirwel/secrets.env``, but ONLY when the
   neutral file is absent and the legacy one exists.

An existing deployment may hold only the legacy file and depend on step 3, so
the legacy literal here is deliberate: it is the backward-compatibility path
under test, not operator data. Every case runs against a tmp HOME, never the
real one.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

NEUTRAL_REL = Path(".config/unitares/secrets.env")
LEGACY_REL = Path(".config/cirwel/secrets.env")

# name -> (files to create under HOME, UNITARES_SECRETS_ENV relative to HOME or
# None, expected path relative to HOME)
SCENARIOS = {
    "env_set": (
        [NEUTRAL_REL, LEGACY_REL, Path("custom/secrets.env")],
        Path("custom/secrets.env"),
        Path("custom/secrets.env"),
    ),
    "env_set_missing_file": (
        [LEGACY_REL],
        Path("custom/absent.env"),
        Path("custom/absent.env"),
    ),
    "neutral_only": ([NEUTRAL_REL], None, NEUTRAL_REL),
    "legacy_only": ([LEGACY_REL], None, LEGACY_REL),
    "both_neutral_wins": ([NEUTRAL_REL, LEGACY_REL], None, NEUTRAL_REL),
    "neither_reports_neutral": ([], None, NEUTRAL_REL),
}


def _prepare(tmp_path: Path, monkeypatch, scenario: str) -> tuple[Path, Path]:
    files, env_rel, expected_rel = SCENARIOS[scenario]
    home = tmp_path / "home"
    home.mkdir()
    for rel in files:
        target = home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("PLACEHOLDER=1\n")
    monkeypatch.setenv("HOME", str(home))
    if env_rel is None:
        monkeypatch.delenv("UNITARES_SECRETS_ENV", raising=False)
    else:
        monkeypatch.setenv("UNITARES_SECRETS_ENV", str(home / env_rel))
    return home, home / expected_rel


def _load(path: Path, name: str):
    # A fresh module per call: the doctor binds its default paths from
    # Path.home() at import time, so it must be imported under the tmp HOME.
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses need the module registered
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def _doctor(home: Path) -> Path:
    mod = _load(REPO / "scripts/dev/unitares_doctor.py", "_parity_unitares_doctor")
    return mod.resolve_secrets_file()


def _setup(home: Path) -> Path:
    mod = _load(REPO / "scripts/install/setup.py", "_parity_unitares_setup")
    return mod.resolve_secrets_path(home)


def _deprecate(home: Path) -> Path:
    mod = _load(
        REPO / "scripts/dev/lease_plane_deprecate.py", "_parity_lease_plane_deprecate"
    )
    return mod._secrets_env_path()


def _sentinel_plist(home: Path) -> Path:
    mod = _load(REPO / "scripts/ops/sentinel-plist.py", "_parity_sentinel_plist")
    return mod._sourced_secrets_file()


PYTHON_RESOLVERS = {
    "unitares_doctor.resolve_secrets_file": _doctor,
    "setup.resolve_secrets_path": _setup,
    "lease_plane_deprecate._secrets_env_path": _deprecate,
    "sentinel-plist._sourced_secrets_file": _sentinel_plist,
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
@pytest.mark.parametrize("resolver", sorted(PYTHON_RESOLVERS))
def test_python_resolvers_agree(tmp_path, monkeypatch, resolver, scenario):
    home, expected = _prepare(tmp_path, monkeypatch, scenario)
    assert Path.home() == home, "the resolver must see the tmp HOME"
    assert PYTHON_RESOLVERS[resolver](home) == expected


# --- shell copies -----------------------------------------------------------

_SHELL_ASSIGN = re.compile(
    r'^(?P<var>[A-Z_]+)="\$\{UNITARES_SECRETS_ENV:-\$\{?HOME\}?'
    r'/\.config/unitares/secrets\.env\}"$',
    re.MULTILINE,
)


def _extract_block(text: str, match: re.Match) -> str:
    """The assignment line through the closing ``fi`` of its fallback ``if``."""
    lines = text[match.start():].splitlines()
    block = []
    for line in lines:
        block.append(line)
        if line.strip() == "fi":
            return "\n".join(block)
    raise AssertionError("resolver block has no closing fi")


def _shell_resolver_blocks() -> dict[str, tuple[str, str]]:
    blocks: dict[str, tuple[str, str]] = {}
    skip_parts = {"deps", "_build", "node_modules", ".venv"}
    for path in sorted([*REPO.glob("elixir/**/*.sh"), *REPO.glob("scripts/**/*.sh")]):
        if not path.is_file() or skip_parts & set(path.relative_to(REPO).parts):
            continue  # dangling local symlinks, vendored or build trees
        text = path.read_text(encoding="utf-8", errors="replace")
        match = _SHELL_ASSIGN.search(text)
        if match:
            rel = str(path.relative_to(REPO))
            blocks[rel] = (match.group("var"), _extract_block(text, match))
    return blocks


SHELL_BLOCKS = _shell_resolver_blocks()


def test_every_beam_start_script_carries_the_resolver():
    start_scripts = sorted(
        str(p.relative_to(REPO)) for p in REPO.glob("elixir/*/scripts/start.sh")
    )
    assert len(start_scripts) >= 5
    missing = [s for s in start_scripts if s not in SHELL_BLOCKS]
    assert not missing, f"start.sh without the secrets resolver snippet: {missing}"


def test_every_shell_copy_keeps_the_legacy_fallback():
    for rel, (_, block) in SHELL_BLOCKS.items():
        assert "/.config/cirwel/secrets.env" in block, rel
        assert "UNITARES_SECRETS_ENV" in block.splitlines()[1], rel


def _run_shell(block: str, var: str, home: Path, env_value: str | None) -> Path:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    if env_value is not None:
        env["UNITARES_SECRETS_ENV"] = env_value
    script = f'set -euo pipefail\n{block}\nprintf "%s" "${var}"\n'
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=True
    )
    return Path(result.stdout)


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
@pytest.mark.parametrize("script", sorted(SHELL_BLOCKS))
def test_shell_resolvers_agree(tmp_path, monkeypatch, script, scenario):
    home, expected = _prepare(tmp_path, monkeypatch, scenario)
    var, block = SHELL_BLOCKS[script]
    env_rel = SCENARIOS[scenario][1]
    env_value = None if env_rel is None else str(home / env_rel)
    assert _run_shell(block, var, home, env_value) == expected


# --- discord bridge env file (same shape, its own names) --------------------

BRIDGE_NEUTRAL_REL = Path(".config/unitares/discord-bridge.env")
BRIDGE_LEGACY_REL = Path(".config/cirwel/discord-bridge.env")

BRIDGE_SCENARIOS = {
    "env_set": (
        [BRIDGE_NEUTRAL_REL, BRIDGE_LEGACY_REL, Path("custom/bridge.env")],
        Path("custom/bridge.env"),
        Path("custom/bridge.env"),
    ),
    "env_set_missing_file": (
        [BRIDGE_LEGACY_REL],
        Path("custom/absent.env"),
        Path("custom/absent.env"),
    ),
    "neutral_only": ([BRIDGE_NEUTRAL_REL], None, BRIDGE_NEUTRAL_REL),
    "legacy_only": ([BRIDGE_LEGACY_REL], None, BRIDGE_LEGACY_REL),
    "both_neutral_wins": (
        [BRIDGE_NEUTRAL_REL, BRIDGE_LEGACY_REL],
        None,
        BRIDGE_NEUTRAL_REL,
    ),
    "neither_reports_neutral": ([], None, BRIDGE_NEUTRAL_REL),
}


def _bridge_block() -> str:
    text = (REPO / "scripts/ops/deploy-bridge.sh").read_text(encoding="utf-8")
    match = re.search(
        r'^ENV_FILE="\$\{BRIDGE_ENV_FILE:-\$HOME/\.config/unitares/discord-bridge\.env\}"$',
        text,
        re.MULTILINE,
    )
    assert match, "deploy-bridge.sh must default ENV_FILE to the neutral path"
    block = _extract_block(text, match)
    assert "/.config/cirwel/discord-bridge.env" in block, "legacy fallback kept"
    return block


@pytest.mark.parametrize("scenario", sorted(BRIDGE_SCENARIOS))
def test_deploy_bridge_env_file_resolution(tmp_path, scenario):
    files, env_rel, expected_rel = BRIDGE_SCENARIOS[scenario]
    home = tmp_path / "home"
    home.mkdir()
    for rel in files:
        target = home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("PLACEHOLDER=1\n")
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    if env_rel is not None:
        env["BRIDGE_ENV_FILE"] = str(home / env_rel)
    script = f'set -euo pipefail\n{_bridge_block()}\nprintf "%s" "$ENV_FILE"\n'
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=True
    )
    assert Path(result.stdout) == home / expected_rel


def _bridge_link_block() -> str:
    text = (REPO / "scripts/ops/deploy-bridge.sh").read_text(encoding="utf-8")
    start = text.index("# -L, not -e:")
    end = text.index("\nfi\n", start) + len("\nfi\n")
    return text[start:end]


@pytest.mark.parametrize(
    ("existing", "selected", "relinked"),
    [
        (BRIDGE_LEGACY_REL, BRIDGE_NEUTRAL_REL, True),  # neutral file created later
        (BRIDGE_LEGACY_REL, BRIDGE_LEGACY_REL, False),  # legacy-only host: untouched
        (BRIDGE_NEUTRAL_REL, BRIDGE_NEUTRAL_REL, False),
    ],
)
def test_deploy_bridge_env_symlink_follows_selected_file(tmp_path, existing, selected, relinked):
    home = tmp_path / "home"
    for rel in (BRIDGE_NEUTRAL_REL, BRIDGE_LEGACY_REL):
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text("PLACEHOLDER=1\n")
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / ".env").symlink_to(home / existing)
    env = {
        "PATH": "/usr/bin:/bin",
        "TAG": "test",
        "DEPLOY": str(deploy),
        "ENV_FILE": str(home / selected),
    }
    script = f"set -euo pipefail\n{_bridge_link_block()}"
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=True
    )
    assert (deploy / ".env").readlink() == home / selected
    assert ("linking .env" in result.stdout) is relinked
