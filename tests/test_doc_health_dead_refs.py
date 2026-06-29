"""Regression tests for ``check_doc_health.check_dead_refs``.

Prior behaviour: the dead-ref check extracted backtick-quoted paths like
``src/foo.py:73`` verbatim and passed them through ``(REPO_ROOT / ref).exists()``,
which returned ``False`` because no file is literally named ``foo.py:73``. Every
doc that used ``path:line`` references — a deliberate and common style for
pointing readers at the exact code location — got flagged as having "dead refs."
That made power-user docs like ``docs/DATA_NOTES.md`` emit a wall of false
positives on every push.

The fix strips trailing ``:N`` and ``:N-M`` suffixes before the existence check
while preserving them in the displayed warning text (so authors can still
navigate to the location when diagnosing a real dead ref).

These tests lock in the fix and guard against regressions.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# --- Load the script as a module without needing __init__.py -----------------

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "diagnostics"
    / "check_doc_health.py"
)


@pytest.fixture(scope="module")
def doc_health():
    spec = importlib.util.spec_from_file_location("check_doc_health", _SCRIPT_PATH)
    assert spec and spec.loader, "could not load check_doc_health.py"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def stub_repo(tmp_path, monkeypatch, doc_health):
    """Stand up a minimal stub repo under tmp_path and point REPO_ROOT at it.

    The check walks the filesystem via ``REPO_ROOT / ref_path`` and computes
    ``fpath.relative_to(REPO_ROOT)`` when flagging warnings — so the test
    markdown file must live inside the same root the checker thinks it's
    scanning. Patching ``REPO_ROOT`` to tmp_path and creating a handful of
    known-existing files under it lets us test dead-ref detection in
    isolation without touching the real repo tree.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "audit_log.py").touch()
    (tmp_path / "src" / "governance_monitor.py").touch()
    (tmp_path / "src" / "services").mkdir()
    (tmp_path / "src" / "services" / "runtime_queries.py").touch()
    monkeypatch.setattr(doc_health, "REPO_ROOT", tmp_path)
    return tmp_path


def _warnings_for(stub_repo: Path, content: str, doc_health) -> list[str]:
    """Write *content* to a doc.md under the stub repo and return warnings."""
    md = stub_repo / "doc.md"
    md.write_text(content)
    return doc_health.check_dead_refs([md])


# --- Valid refs (should NOT warn) --------------------------------------------


def test_plain_valid_path_not_flagged(stub_repo, doc_health):
    """A plain backtick-quoted path to an existing file is not flagged."""
    warnings = _warnings_for(
        stub_repo, "See `src/audit_log.py` for details.", doc_health
    )
    assert warnings == []


def test_valid_path_with_line_number_not_flagged(stub_repo, doc_health):
    """The regression case: ``src/foo.py:73`` where foo.py exists.

    Before the fix, this was flagged because ``Path('src/audit_log.py:73').exists()``
    returns False. After the fix, the ``:73`` suffix is stripped before the
    existence check.
    """
    warnings = _warnings_for(
        stub_repo, "Look at `src/audit_log.py:73` for the risk_score field.", doc_health
    )
    assert warnings == [], (
        f"Valid path with line number should not be flagged. Got: {warnings}"
    )


def test_valid_path_with_line_range_not_flagged(stub_repo, doc_health):
    """``src/foo.py:99-124`` style range references should not be flagged."""
    warnings = _warnings_for(
        stub_repo,
        "The observation-first EMA logic lives at `src/services/runtime_queries.py:99-124`.",
        doc_health,
    )
    assert warnings == [], (
        f"Valid path with line range should not be flagged. Got: {warnings}"
    )


def test_markdown_link_with_line_number_not_flagged(stub_repo, doc_health):
    """Markdown link syntax with a ``:line`` suffix should also be handled."""
    warnings = _warnings_for(
        stub_repo,
        "See [the code](src/audit_log.py:73) for details.",
        doc_health,
    )
    assert warnings == [], (
        f"Markdown link with line number should not be flagged. Got: {warnings}"
    )


# --- Dead refs (should warn) -------------------------------------------------


_FAKE_PATH = "src/definitely_not_a_real_file_xyz_12345.py"


def test_fake_path_no_line_number_still_flagged(stub_repo, doc_health):
    """The original dead-ref detection must still work for plain paths."""
    warnings = _warnings_for(
        stub_repo, f"See `{_FAKE_PATH}` for details.", doc_health
    )
    assert any(_FAKE_PATH in w for w in warnings), (
        f"Dead path without line number must still be flagged. Got: {warnings}"
    )


def test_fake_path_with_line_number_still_flagged(stub_repo, doc_health):
    """A truly dead path with a line number must still be flagged.

    The fix strips the ``:N`` suffix *before* the existence check, so it still
    correctly identifies that the underlying file doesn't exist.
    """
    warnings = _warnings_for(
        stub_repo, f"See `{_FAKE_PATH}:42` for details.", doc_health
    )
    assert any(_FAKE_PATH in w for w in warnings), (
        f"Dead path with line number must still be flagged. Got: {warnings}"
    )


def test_fake_path_with_line_range_still_flagged(stub_repo, doc_health):
    """A truly dead path with a line range must still be flagged."""
    warnings = _warnings_for(
        stub_repo, f"See `{_FAKE_PATH}:99-124` for details.", doc_health
    )
    assert any(_FAKE_PATH in w for w in warnings), (
        f"Dead path with line range must still be flagged. Got: {warnings}"
    )


# --- Warning display format --------------------------------------------------


def test_warning_preserves_line_number_suffix_for_navigation(stub_repo, doc_health):
    """The warning text shown to authors keeps the original ``:line`` suffix.

    This matters for debugging: when a ref IS genuinely dead, the author wants
    to see which line the doc was pointing at, not just the stripped file path.
    """
    fake_with_line = f"{_FAKE_PATH}:42"
    warnings = _warnings_for(
        stub_repo, f"See `{fake_with_line}` for details.", doc_health
    )
    assert any(":42" in w for w in warnings), (
        f"Warning should preserve the :42 navigation suffix. Got: {warnings}"
    )


# --- Mixed content (regression coverage) -------------------------------------


def test_mixed_valid_and_dead_refs(stub_repo, doc_health):
    """A doc with both valid and dead refs should flag only the dead ones."""
    content = (
        f"Valid: `src/audit_log.py:73`\n"
        f"Also valid: `src/governance_monitor.py`\n"
        f"Dead: `{_FAKE_PATH}:1`\n"
        f"Valid with range: `src/audit_log.py:10-20`\n"
    )
    warnings = _warnings_for(stub_repo, content, doc_health)

    # Exactly one warning, and it must be for the fake path
    assert len(warnings) == 1, (
        f"Expected exactly one dead-ref warning, got {len(warnings)}: {warnings}"
    )
    assert _FAKE_PATH in warnings[0]


def test_dead_ref_check_skips_private_ontology_plan_ledger(tmp_path, monkeypatch, doc_health):
    """The ontology plan ledger preserves private/internal refs removed from public master."""
    plan = tmp_path / "docs" / "ontology" / "plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("Historical handoff: `docs/handoffs/removed-private-note.md`\n")

    monkeypatch.setattr(doc_health, "REPO_ROOT", tmp_path)

    assert doc_health.check_dead_refs([plan]) == []


def test_dead_ref_check_skips_operator_local_handoffs_refs(tmp_path, monkeypatch, doc_health):
    """Refs into docs/handoffs/ are operator-local (the dir is gitignored).

    Public docs cite private handoffs by filename as provenance — e.g. the
    AGENTS.md strict-identity stage-1 burn-in note. Those paths are expected
    to be unresolvable in the public tree, from any doc, not just the
    skip-listed ledger files.
    """
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        "Stage 1 run (`docs/handoffs/strict-identity-stage1-burnin-2026-06-11.md`)\n"
    )

    monkeypatch.setattr(doc_health, "REPO_ROOT", tmp_path)

    assert doc_health.check_dead_refs([agents]) == []


def test_get_db_call_is_internal_helper_not_ghost_tool(tmp_path, monkeypatch, doc_health):
    """AGENTS.md mentions get_db() as an internal helper, not an MCP tool claim."""
    agents = tmp_path / "AGENTS.md"
    agents.write_text("New handlers can use `get_db()` for DB access.\n")

    monkeypatch.setattr(doc_health, "REPO_ROOT", tmp_path)

    assert doc_health.check_ghost_tools([agents], {"onboard"}) == []


def test_ghost_tool_skip_file_suppresses_internal_fn_names(tmp_path, monkeypatch, doc_health):
    """The dormant-capability-registry names internal functions, not MCP tools.

    It lives under operations/ (not a _GHOST_SKIP_DIRS dir), so it is skipped
    by exact path via _GHOST_SKIP_FILES — without blinding the check across
    all of operations/.
    """
    reg = tmp_path / "docs" / "operations" / "dormant-capability-registry.md"
    reg.parent.mkdir(parents=True)
    reg.write_text("Repoint to `get_active_table_name()`; gate on `has_exogenous_signals()`.\n")

    monkeypatch.setattr(doc_health, "REPO_ROOT", tmp_path)
    assert doc_health.check_ghost_tools([reg], {"onboard"}) == []


def test_ghost_tool_still_checks_other_operations_docs(tmp_path, monkeypatch, doc_health):
    """The per-file skip must not leak to other operations/ runbooks."""
    runbook = tmp_path / "docs" / "operations" / "OPERATOR_RUNBOOK.md"
    runbook.parent.mkdir(parents=True)
    runbook.write_text("Call `not_a_real_tool()` to recover.\n")

    monkeypatch.setattr(doc_health, "REPO_ROOT", tmp_path)
    warnings = doc_health.check_ghost_tools([runbook], {"onboard"})
    assert len(warnings) == 1 and "not_a_real_tool" in warnings[0]


def test_dedup_stripped_paths(stub_repo, doc_health):
    """Multiple refs to the same file with different line numbers dedupe to one warning."""
    content = (
        f"First: `{_FAKE_PATH}:10`\n"
        f"Second: `{_FAKE_PATH}:20`\n"
        f"Third: `{_FAKE_PATH}:30-40`\n"
        f"Fourth plain: `{_FAKE_PATH}`\n"
    )
    warnings = _warnings_for(stub_repo, content, doc_health)
    # All four refs point to the same (nonexistent) file; dedupe to 1 warning
    assert len(warnings) == 1, (
        f"Expected dedupe to one warning per unique file, got {len(warnings)}: {warnings}"
    )


# --- check_relative_links (bare/relative .md links) --------------------------


def test_relative_link_to_existing_sibling_not_flagged(tmp_path, monkeypatch, doc_health):
    """A relative link to a sibling doc that exists is fine."""
    ops = tmp_path / "docs" / "operations"
    ops.mkdir(parents=True)
    (ops / "OPERATOR_RUNBOOK.md").write_text("runbook")
    doc = ops / "database_architecture.md"
    doc.write_text("See [runbook](OPERATOR_RUNBOOK.md) and [arch](../UNIFIED_ARCHITECTURE.md).\n")
    (tmp_path / "docs" / "UNIFIED_ARCHITECTURE.md").write_text("arch")

    monkeypatch.setattr(doc_health, "REPO_ROOT", tmp_path)
    assert doc_health.check_relative_links([doc]) == []


def test_broken_relative_link_flagged(tmp_path, monkeypatch, doc_health):
    """The regression case: a relative link resolving to nothing is flagged.

    `docs/operations/x.md` linking `[a](UNIFIED_ARCHITECTURE.md)` is broken —
    the file lives one level up at `docs/UNIFIED_ARCHITECTURE.md`.
    """
    ops = tmp_path / "docs" / "operations"
    ops.mkdir(parents=True)
    (tmp_path / "docs" / "UNIFIED_ARCHITECTURE.md").write_text("arch")
    doc = ops / "x.md"
    doc.write_text("See [arch](UNIFIED_ARCHITECTURE.md).\n")

    monkeypatch.setattr(doc_health, "REPO_ROOT", tmp_path)
    warnings = doc_health.check_relative_links([doc])
    assert len(warnings) == 1 and "UNIFIED_ARCHITECTURE.md" in warnings[0]


def test_relative_link_check_skips_proposals(tmp_path, monkeypatch, doc_health):
    """proposals/ reference paths that don't exist yet by design (skip dir)."""
    prop = tmp_path / "docs" / "proposals"
    prop.mkdir(parents=True)
    doc = prop / "path1.md"
    doc.write_text("Companion: [audit](./uuid-leak-audit.md).\n")

    monkeypatch.setattr(doc_health, "REPO_ROOT", tmp_path)
    assert doc_health.check_relative_links([doc]) == []


def test_repo_root_prefixed_links_left_to_dead_ref_check(tmp_path, monkeypatch, doc_health):
    """`docs/...`-prefixed links are check_dead_refs' job, not this one."""
    d = tmp_path / "docs"
    d.mkdir()
    doc = d / "a.md"
    doc.write_text("See [x](docs/nonexistent.md).\n")

    monkeypatch.setattr(doc_health, "REPO_ROOT", tmp_path)
    assert doc_health.check_relative_links([doc]) == []


# --- check_index_orphans -----------------------------------------------------


def test_unreferenced_doc_is_orphan(tmp_path, monkeypatch, doc_health):
    """A doc no other file mentions by name is flagged as an orphan."""
    d = tmp_path / "docs" / "operations"
    d.mkdir(parents=True)
    orphan = d / "lonely.md"
    orphan.write_text("Nobody links here.\n")

    monkeypatch.setattr(doc_health, "REPO_ROOT", tmp_path)
    warnings = doc_health.check_index_orphans([orphan])
    assert len(warnings) == 1 and "lonely.md" in warnings[0]


def test_doc_referenced_from_index_not_orphan(tmp_path, monkeypatch, doc_health):
    """A doc linked from a README index is not an orphan."""
    d = tmp_path / "docs" / "operations"
    d.mkdir(parents=True)
    (d / "README.md").write_text("- [thing](thing.md)\n")
    thing = d / "thing.md"
    thing.write_text("content")

    monkeypatch.setattr(doc_health, "REPO_ROOT", tmp_path)
    assert doc_health.check_index_orphans([thing]) == []


def test_doc_referenced_only_from_code_not_orphan(tmp_path, monkeypatch, doc_health):
    """A doc cited from a source file (not any .md) is still reachable."""
    (tmp_path / "docs" / "proposals").mkdir(parents=True)
    spec = tmp_path / "docs" / "proposals" / "lineage-causal-only-semantics.md"
    spec.write_text("design")
    src = tmp_path / "src"
    src.mkdir()
    (src / "helpers.py").write_text("# see docs/proposals/lineage-causal-only-semantics.md\n")

    monkeypatch.setattr(doc_health, "REPO_ROOT", tmp_path)
    assert doc_health.check_index_orphans([spec]) == []


def test_dated_record_exempt_from_orphan_check(tmp_path, monkeypatch, doc_health):
    """Dated point-in-time records may be intentionally unlinked."""
    d = tmp_path / "docs" / "operations"
    d.mkdir(parents=True)
    dated = d / "ablation-finding-2026-06-16.md"
    dated.write_text("a finding, preserved in place")

    monkeypatch.setattr(doc_health, "REPO_ROOT", tmp_path)
    assert doc_health.check_index_orphans([dated]) == []


# --- check_demotion_candidates (shipped-status docs in active locations) -----


def _demotion_for(tmp_path, monkeypatch, doc_health, rel, body):
    """Write *body* to docs/<rel> and return demotion warnings for it."""
    p = tmp_path / "docs" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    monkeypatch.setattr(doc_health, "REPO_ROOT", tmp_path)
    return doc_health.check_demotion_candidates([p])


def test_fully_shipped_proposal_flagged(tmp_path, monkeypatch, doc_health):
    """A proposal whose status says shipped, with no remaining work, is a candidate."""
    warnings = _demotion_for(
        tmp_path, monkeypatch, doc_health, "proposals/thing-v0.md",
        "# Thing\n\n**Status:** shipped 2026-05-03 via PR #305. Enforcement deployed.\n",
    )
    assert len(warnings) == 1 and "fully shipped" in warnings[0]


def test_partially_shipped_proposal_flagged_as_split(tmp_path, monkeypatch, doc_health):
    """Shipped + named remaining work → 'partially shipped' (split, don't move whole)."""
    warnings = _demotion_for(
        tmp_path, monkeypatch, doc_health, "proposals/wave.md",
        "# Wave\n\n**Status:** Wave 1 shipped; Wave 3 write-path not started.\n",
    )
    assert len(warnings) == 1 and "partially shipped" in warnings[0]


def test_active_proposal_without_shipped_marker_not_flagged(tmp_path, monkeypatch, doc_health):
    """A genuinely-active draft (no shipped marker in header) is not a candidate."""
    warnings = _demotion_for(
        tmp_path, monkeypatch, doc_health, "proposals/idea-v0.md",
        "# Idea\n\n**Status:** draft, in progress. Proposed design under review.\n",
    )
    assert warnings == []


def test_resolved_proposal_not_flagged(tmp_path, monkeypatch, doc_health):
    """Already-demoted docs under proposals/resolved/ are not re-flagged."""
    warnings = _demotion_for(
        tmp_path, monkeypatch, doc_health, "proposals/resolved/old.md",
        "# Old\n\n**Status:** shipped and complete.\n",
    )
    assert warnings == []


def test_dated_record_with_shipped_status_not_flagged(tmp_path, monkeypatch, doc_health):
    """A dated point-in-time record may say 'shipped' — it's a record, kept in place."""
    warnings = _demotion_for(
        tmp_path, monkeypatch, doc_health, "proposals/audit-2026-06-14.md",
        "# Audit\n\n**Status:** shipped; deployed.\n",
    )
    assert warnings == []


def test_ontology_anchor_not_flagged(tmp_path, monkeypatch, doc_health):
    """ontology/ anchor docs (identity.md etc.) are living, not plans to demote."""
    warnings = _demotion_for(
        tmp_path, monkeypatch, doc_health, "ontology/identity.md",
        "# Identity\n\nThe v2 ontology is deployed and complete in production.\n",
    )
    assert warnings == []


def test_ontology_plan_flagged_with_location_hint(tmp_path, monkeypatch, doc_health):
    """A shipped non-anchor doc in ontology/ is flagged, naming the identity tree."""
    warnings = _demotion_for(
        tmp_path, monkeypatch, doc_health, "ontology/coordination-plan.md",
        "# Coordination Plan\n\n**Status:** lease plane live; Wave 1 shipped.\n",
    )
    assert len(warnings) == 1 and "ontology/ (identity tree)" in warnings[0]


def test_widely_referenced_doc_exempt_as_living_reference(tmp_path, monkeypatch, doc_health):
    """A doc cited by >= threshold other docs is a living reference, not a stale plan."""
    target_rel = "proposals/contract-v0.md"
    target = tmp_path / "docs" / target_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Contract\n\n**Status:** shipped; the canonical spec.\n")
    # Create threshold-many sibling docs that cite it by basename.
    for i in range(doc_health._DEMOTION_LIVING_REF_THRESHOLD):
        sib = tmp_path / "docs" / f"ref{i}.md"
        sib.write_text("See [contract](contract-v0.md) for the spec.\n")
    monkeypatch.setattr(doc_health, "REPO_ROOT", tmp_path)
    assert doc_health.check_demotion_candidates([target]) == []


def test_demotion_does_not_gate_strict_exit(tmp_path, monkeypatch, doc_health, capsys):
    """A demotion candidate alone must NOT make even --strict exit non-zero."""
    p = tmp_path / "docs" / "proposals" / "shipped-v0.md"
    p.parent.mkdir(parents=True)
    p.write_text("# X\n\n**Status:** shipped, deployed, complete.\n")
    # Reference it from an index so it isn't also flagged as an orphan (which
    # WOULD gate strict). We want a clean case: demotion candidate and nothing else.
    (p.parent / "README.md").write_text("- [x](shipped-v0.md)\n")
    monkeypatch.setattr(doc_health, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        doc_health.sys,
        "argv",
        ["check_doc_health.py", "--strict", "--demotion-candidates"],
    )
    assert doc_health.main() == 0
    assert "Demotion candidates" in capsys.readouterr().out


def test_demotion_advisory_is_opt_in(tmp_path, monkeypatch, doc_health, capsys):
    """Default doc-health output stays quiet for advisory-only demotion candidates."""
    p = tmp_path / "docs" / "proposals" / "shipped-v0.md"
    p.parent.mkdir(parents=True)
    p.write_text("# X\n\n**Status:** shipped, deployed, complete.\n")
    (p.parent / "README.md").write_text("- [x](shipped-v0.md)\n")

    monkeypatch.setattr(doc_health, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(doc_health.sys, "argv", ["check_doc_health.py"])

    assert doc_health.main() == 0
    out = capsys.readouterr().out
    assert "Doc health: all clear" in out
    assert "Demotion candidates" not in out


def test_collect_md_files_skips_elixir_deps_and_build_dirs(tmp_path, monkeypatch, doc_health):
    """Vendored Elixir deps and Mix build output are not repo documentation."""
    (tmp_path / "docs").mkdir()
    real_doc = tmp_path / "docs" / "real.md"
    real_doc.write_text("Real project doc")

    dep_doc = tmp_path / "elixir" / "lease_plane" / "deps" / "bandit" / "README.md"
    dep_doc.parent.mkdir(parents=True)
    dep_doc.write_text("Vendored doc with upstream-relative refs")

    build_doc = tmp_path / "elixir" / "lease_plane" / "_build" / "test" / "README.md"
    build_doc.parent.mkdir(parents=True)
    build_doc.write_text("Generated build doc")

    monkeypatch.setattr(doc_health, "REPO_ROOT", tmp_path)

    assert doc_health.collect_md_files() == [real_doc]
