"""Tests for ``scripts/dev/check_component_boundaries.py``.

Every rule gets a fixture that breaks it and an assertion that the guard fails,
so no rule can go quietly inert. The ratchet mechanics follow the proposals
length ratchet's tests: the committed tree passes, growth fails, a stale entry
fails, and neither ``--lower`` nor ``--base`` lets the baseline grow.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dev" / "check_component_boundaries.py"

_SPEC = importlib.util.spec_from_file_location("check_component_boundaries", SCRIPT)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

PACKS = '''
PACK_SPECS: dict = {
    "demo": [("/v1/demo", "agents.demo.routes:handler", ["GET"])],
}
'''


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _baseline(root: Path, entries: dict[tuple[str, str], int]) -> None:
    mod.write_baseline(root / mod.BASELINE_REL, mod.HEADER, entries)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A minimal tree with one of everything, and no violations."""
    _write(tmp_path, "src/__init__.py", "")
    _write(tmp_path, "src/http_routes/__init__.py", "")
    _write(tmp_path, "src/http_routes/packs.py", PACKS)
    _write(tmp_path, "src/http_routes/access.py", "def check():\n    return True\n")
    _write(tmp_path, "src/logging_utils.py", "def get_logger():\n    return None\n")
    _write(tmp_path, "src/mcp_handlers/__init__.py", "")
    _write(tmp_path, "src/mcp_handlers/identity/__init__.py", "")
    _write(
        tmp_path,
        "src/mcp_handlers/identity/handlers.py",
        "def _private():\n    return 1\n\n"
        "def lookup(db):\n    return db.fetch(\"SELECT 1 FROM core.identities\")\n",
    )
    _write(tmp_path, "src/db/mixins/identity.py", 'Q = "SELECT * FROM core.identities"\n')
    _write(tmp_path, "agents/demo/agent.py", "import json\n\n\ndef run():\n    return json.dumps({})\n")
    _write(
        tmp_path,
        "agents/demo/routes.py",
        "from src.http_routes import access\nfrom src.logging_utils import get_logger\n",
    )
    _write(tmp_path, "agents/demo/tests/test_agent.py", "from src.http_routes import access\n")
    _baseline(tmp_path, {})
    return tmp_path


def _problems(root: Path) -> list[str]:
    problems, _ = mod.check(root)
    return problems


def test_clean_fixture_passes(root: Path):
    assert _problems(root) == []


def test_repository_currently_passes():
    problems, _ = mod.check(REPO_ROOT)
    assert not problems, problems


def test_committed_baseline_has_only_known_rules():
    _, entries = mod.read_baseline(REPO_ROOT / mod.BASELINE_REL)
    assert entries, "baseline unexpectedly empty"
    for (rule, key), count in entries.items():
        assert rule in mod.RULES, rule
        assert count > 0, key


# --- one mutation per rule ---------------------------------------------------


def test_b1_member_importing_src_fails(root: Path):
    _write(root, "agents/demo/agent.py", "from src.logging_utils import get_logger\n")
    assert any("new B1 violation in agents/demo/agent.py" in p for p in _problems(root))


def test_b1_function_local_import_is_seen(root: Path):
    _write(root, "agents/demo/agent.py", "def run():\n    import src.logging_utils\n")
    assert any("B1" in p for p in _problems(root))


def test_b1_sdk_returning_try_is_exempt_but_not_for_residents(root: Path):
    hook = (
        "def hook():\n"
        "    try:\n"
        "        from src.logging_utils import get_logger\n"
        "    except ImportError:\n"
        "        return None\n"
        "    return get_logger()\n"
    )
    _write(root, "agents/sdk/src/client.py", hook)
    assert _problems(root) == []
    _write(root, "agents/demo/agent.py", hook)
    assert any("new B1 violation in agents/demo/agent.py" in p for p in _problems(root))


def test_b1_sdk_try_that_swallows_without_returning_is_not_exempt(root: Path):
    _write(
        root,
        "agents/sdk/src/client.py",
        "try:\n    from src.logging_utils import get_logger\nexcept ImportError:\n    pass\n",
    )
    assert any("agents/sdk/src/client.py" in p for p in _problems(root))


def test_b1p_pack_importing_outside_the_allowlist_fails(root: Path):
    _write(root, "agents/demo/routes.py", "from src.mcp_handlers.identity import handlers\n")
    assert any("new B1p violation in agents/demo/routes.py" in p for p in _problems(root))


def test_b1p_resolves_submodules_imported_from_a_package(root: Path):
    # ``from src.http_routes import access`` binds src.http_routes.access, which
    # is allowed; a sibling route module imported the same way is not.
    _write(root, "src/http_routes/overview.py", "def view():\n    return 1\n")
    _write(root, "agents/demo/routes.py", "from src.http_routes import access, overview\n")
    problems = _problems(root)
    assert any("imports src.http_routes.overview, not on the pack allowlist" in p for p in problems)
    assert not any("src.http_routes.access" in p for p in problems)


def test_b1_sdk_handler_that_returns_only_in_a_nested_def_is_not_exempt(root: Path):
    _write(
        root,
        "agents/sdk/src/client.py",
        "try:\n"
        "    from src.logging_utils import get_logger\n"
        "except ImportError:\n"
        "    def fallback():\n"
        "        return None\n"
        "    raise\n",
    )
    assert any("agents/sdk/src/client.py" in p for p in _problems(root))


def test_b2_member_sql_fails_and_docstrings_do_not(root: Path):
    _write(root, "agents/demo/agent.py", '"""Reads FROM core.agent_state in prose only."""\n')
    assert _problems(root) == []
    _write(root, "agents/demo/agent.py", 'Q = "SELECT count(*) FROM lease_plane.surface_leases"\n')
    assert any("new B2 violation in agents/demo/agent.py" in p for p in _problems(root))


def test_subprocess_target_carries_the_breach(root: Path):
    _write(root, "scripts/eval/probe.py", "from src.mcp_handlers.identity import handlers\n")
    _write(
        root,
        "agents/demo/agent.py",
        "from pathlib import Path\nSCRIPT = Path(__file__).parents[2] / 'scripts' / 'eval' / 'probe.py'\n",
    )
    assert any("agents/demo/agent.py -> scripts/eval/probe.py" in p for p in _problems(root))


def test_subprocess_target_sql_and_string_paths(root: Path):
    _write(root, "scripts/ops/peek.py", 'Q = "SELECT * FROM audit.events"\n')
    _write(root, "agents/demo/agent.py", 'CMD = ["python3", "scripts/ops/peek.py"]\n')
    assert any("B2" in p and "-> scripts/ops/peek.py" in p for p in _problems(root))


def test_b3_private_reach_across_components_fails(root: Path):
    _write(root, "src/dialectic_db.py", "from src.mcp_handlers.identity.handlers import _private\n")
    assert any("new B3 violation in src/dialectic_db.py" in p for p in _problems(root))


def test_b3_applies_to_route_packs(root: Path):
    _write(root, "agents/demo/routes.py", "from src.mcp_handlers.identity.handlers import _private\n")
    problems = _problems(root)
    assert any("new B3 violation in agents/demo/routes.py" in p for p in problems)


def test_b3_private_reach_within_a_component_passes(root: Path):
    _write(root, "src/identity/helper.py", "from src.mcp_handlers.identity.handlers import _private\n")
    assert _problems(root) == []


def test_b4_foreign_sql_fails_and_owner_sql_passes(root: Path):
    _write(root, "src/http_routes/overview.py", 'Q = "SELECT * FROM core.identities"\n')
    assert any("new B4 violation in src/http_routes/overview.py" in p for p in _problems(root))


def test_b4_elixir_writer_needs_a_declared_co_owner(root: Path):
    sql = 'Repo.query!("INSERT INTO core.dialectic_sessions (id) VALUES ($1)")\n'
    _write(root, "elixir/lease_plane/lib/unitares_lease_plane/dialectic_saga.ex", sql)
    assert _problems(root) == []
    _write(root, "elixir/lease_plane/lib/unitares_lease_plane/other.ex", sql)
    assert any("new B4 violation in elixir/lease_plane/lib/unitares_lease_plane/other.ex" in p
               for p in _problems(root))


def test_b4_elixir_sql_split_across_lines_is_seen(root: Path):
    _write(
        root,
        "elixir/lease_plane/lib/unitares_lease_plane/other.ex",
        '# FROM core.identities in a comment is ignored\n'
        'Repo.query!("""\n  SELECT id\n  FROM\n    core.identities\n""")\n',
    )
    problems = _problems(root)
    assert any("other.ex:5 SQL against core.identities" in p for p in problems), problems
    assert not any("other.ex:1" in p for p in problems)


def test_subprocess_violation_names_the_script_line_not_the_member(root: Path):
    _write(root, "scripts/eval/probe.py", "import json\n\nfrom src.logging_utils import get_logger\n")
    _write(root, "agents/demo/agent.py", 'CMD = ["python3", "scripts/eval/probe.py"]\n')
    problem = next(p for p in _problems(root) if "-> scripts/eval/probe.py" in p)
    assert "scripts/eval/probe.py:3 imports src.logging_utils" in problem
    assert "agents/demo/agent.py:3" not in problem


def test_tests_are_out_of_scope(root: Path):
    # The fixture already holds agents/demo/tests/test_agent.py importing src.
    assert _problems(root) == []


# --- ratchet -------------------------------------------------------------------


def _with_one_b1(root: Path) -> tuple[str, str]:
    _write(root, "agents/demo/agent.py", "from src.logging_utils import get_logger\n")
    return ("B1", "agents/demo/agent.py")


def test_baselined_violation_passes_and_growth_fails(root: Path):
    pair = _with_one_b1(root)
    _baseline(root, {pair: 1})
    assert _problems(root) == []
    _write(
        root,
        "agents/demo/agent.py",
        "from src.logging_utils import get_logger\nfrom src.http_routes import access\n",
    )
    assert any("grew to 2" in p for p in _problems(root))


def test_dropped_count_fails_until_lowered(root: Path):
    pair = _with_one_b1(root)
    _baseline(root, {pair: 2})
    assert any("dropped to 1 (baseline 2)" in p for p in _problems(root))
    mod.lower(root)
    _, entries = mod.read_baseline(root / mod.BASELINE_REL)
    assert entries == {pair: 1}
    assert _problems(root) == []


def test_stale_entry_fails_and_lower_removes_it(root: Path):
    _baseline(root, {("B1", "agents/demo/agent.py"): 1})
    assert any("stale baseline entry" in p for p in _problems(root))
    mod.lower(root)
    _, entries = mod.read_baseline(root / mod.BASELINE_REL)
    assert entries == {}
    assert _problems(root) == []


def test_lower_never_adds_or_raises(root: Path):
    pair = _with_one_b1(root)
    _baseline(root, {})
    mod.lower(root)
    _, entries = mod.read_baseline(root / mod.BASELINE_REL)
    assert pair not in entries
    _baseline(root, {pair: 5})
    mod.lower(root)
    _, entries = mod.read_baseline(root / mod.BASELINE_REL)
    assert entries[pair] == 1


def test_base_rejects_added_or_raised_entries(root: Path):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"],
        cwd=root, check=True,
    )
    pair = _with_one_b1(root)
    _baseline(root, {pair: 1})
    assert mod.main(["--root", str(root)]) == 0
    assert mod.main(["--root", str(root), "--base", "HEAD"]) == 1
    escalations = mod.baseline_escalations({pair: 1}, {pair: 2})
    assert escalations and "raised" in escalations[0]


def test_unresolvable_base_ref_fails(root: Path):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    assert mod.main(["--root", str(root), "--base", "no-such-ref"]) == 1


def test_init_refuses_to_overwrite(root: Path):
    assert mod.init(root) == 1
