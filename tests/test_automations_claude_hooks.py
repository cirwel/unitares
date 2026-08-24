"""Tests for the claude-hooks collector in scripts/ops/unitares-automations.

The census answers "what runs on a clock" via launchd/cron/Actions. Session
lifecycle hooks were a whole class it never enumerated, so a hook wired in
settings.json was invisible to it. These tests pin the two behaviours that
make the collector trustworthy rather than merely present:

1. Plugin hooks are dispatched as ``run-hook.cmd <name>`` -- the script that
   actually runs is an ARGUMENT. Resolving only the command reports every
   real plugin hook as orphaned, which is a confident lie.
2. The orphan list must stay clean. Backup files keep their executable bit,
   and listing them trains people to ignore the orphan list entirely.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def census_mod():
    # The tool ships without a .py extension (it is a CLI on PATH), so
    # spec_from_file_location cannot infer a loader -- name the loader.
    module_path = (
        Path(__file__).resolve().parent.parent / "scripts" / "ops" / "unitares-automations"
    )
    loader = importlib.machinery.SourceFileLoader("unitares_automations", str(module_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec
    module = importlib.util.module_from_spec(spec)
    sys.modules["unitares_automations"] = module
    loader.exec_module(module)
    return module


def _exe(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


@pytest.fixture()
def fake_home(tmp_path, monkeypatch, census_mod):
    """A HOME with a user hooks dir, settings, and an installed plugin."""
    home = tmp_path / "home"
    hooks = home / ".claude/hooks"
    _exe(hooks / "wired-one.sh")
    _exe(hooks / "orphan-one.sh")
    _exe(hooks / "wired-one.sh.bak-20260731")  # backup: executable, not a hook

    plugin = home / "plugins/demo-plugin"
    _exe(plugin / "hooks/run-hook.cmd")
    _exe(plugin / "hooks/post-edit")
    # Declared only in codex-hooks.json, and with the ${PLUGIN_ROOT}
    # placeholder rather than ${CLAUDE_PLUGIN_ROOT}.
    _exe(plugin / "hooks/post-activity")
    _exe(plugin / "hooks/never-wired")
    _exe(plugin / "hooks/__init__.py", "")

    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude/settings.json").write_text(json.dumps({
        "hooks": {
            "PostToolUse": [{
                "matcher": "Write|Edit",
                "hooks": [{"type": "command", "command": str(hooks / "wired-one.sh")}],
            }]
        }
    }))

    installed = home / ".claude/plugins/installed_plugins.json"
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_text(json.dumps({
        "version": 2,
        "plugins": {
            "demo@local": [{"scope": "user", "installPath": str(plugin)}],
        },
    }))
    (plugin / "hooks/hooks.json").write_text(json.dumps({
        "hooks": {
            "PostToolUse": [{
                "matcher": "Edit",
                "hooks": [{
                    "type": "command",
                    "command": '"${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd" post-edit --host claude',
                }],
            }]
        }
    }))
    (plugin / "hooks/codex-hooks.json").write_text(json.dumps({
        "hooks": {
            "PostToolUse": [{
                "matcher": "Edit",
                "hooks": [{
                    "type": "command",
                    "command": '"${PLUGIN_ROOT}/hooks/run-hook.cmd" post-activity --host codex',
                    "commandWindows": '"%PLUGIN_ROOT%\\hooks\\run-hook.cmd" post-activity --host codex',
                }],
            }]
        }
    }))

    monkeypatch.setattr(census_mod, "HOME", home)
    return home


def _collect(census_mod):
    warnings: list[str] = []
    return census_mod.collect_claude_hooks(warnings), warnings


def test_settings_hook_is_wired(census_mod, fake_home):
    items, _ = _collect(census_mod)
    wired = [i for i in items if i.status == "wired" and "wired-one.sh" in i.name]
    assert len(wired) == 1
    assert wired[0].source == "claude-hooks"
    assert wired[0].kind == "hook"
    assert wired[0].cadence == "on:PostToolUse"
    assert "event=PostToolUse" in wired[0].notes


def test_dispatched_plugin_hook_is_not_orphaned(census_mod, fake_home):
    """run-hook.cmd post-edit -- the real hook is the argument, not the command."""
    items, _ = _collect(census_mod)
    orphan_names = {i.name for i in items if i.status == "orphaned"}
    assert not any("post-edit" in n for n in orphan_names), orphan_names
    # Scoped to the hooks.json declaration -- codex-hooks.json contributes
    # its own plugin entry, so counting all plugin hooks would be brittle.
    wired = [
        i for i in items
        if i.status == "wired" and i.scheduler == "claude-plugin" and "post-edit" in i.name
    ]
    assert len(wired) == 1, [i.name for i in items if i.status == "wired"]
    # Labelled by the dispatched target, not by the dispatcher.
    assert "run-hook.cmd" not in wired[0].name


def test_orphan_detected(census_mod, fake_home):
    items, _ = _collect(census_mod)
    orphans = {i.name for i in items if i.status == "orphaned"}
    assert any("orphan-one.sh" in n for n in orphans), orphans
    assert any("never-wired" in n for n in orphans), orphans


def test_backups_are_not_reported_as_orphans(census_mod, fake_home):
    items, _ = _collect(census_mod)
    orphans = {i.name for i in items if i.status == "orphaned"}
    assert not any(".bak" in n for n in orphans), orphans


def test_python_package_marker_is_not_reported_as_orphan(census_mod, fake_home):
    items, _ = _collect(census_mod)
    orphans = {i.name for i in items if i.status == "orphaned"}
    assert not any("__init__.py" in name for name in orphans), orphans


def test_dispatcher_itself_is_not_an_orphan(census_mod, fake_home):
    items, _ = _collect(census_mod)
    orphans = {i.name for i in items if i.status == "orphaned"}
    assert not any("run-hook.cmd" in n for n in orphans), orphans


def test_missing_script_is_flagged(census_mod, fake_home, monkeypatch):
    settings = fake_home / ".claude/settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "Stop": [{
                "hooks": [{
                    "type": "command",
                    "command": str(fake_home / ".claude/hooks/does-not-exist.sh"),
                }],
            }]
        }
    }))
    items, warnings = _collect(census_mod)
    missing = [i for i in items if i.status == "missing"]
    assert len(missing) == 1
    assert "attention:script_not_found_on_disk" in missing[0].notes
    assert any("no resolvable script" in w for w in warnings)


def test_orphaned_and_missing_reach_needs_attention(census_mod):
    """These statuses are the drift the census exists to surface."""
    orphan = census_mod.Automation(
        id="x", name="ORPHANED: a.sh", source="claude-hooks", kind="hook",
        scheduler="none", runner="shell", status="orphaned",
    )
    missing = census_mod.Automation(
        id="y", name="Stop: b.sh", source="claude-hooks", kind="hook",
        scheduler="claude-settings", runner="shell", status="missing",
    )
    healthy = census_mod.Automation(
        id="z", name="Stop: c.sh", source="claude-hooks", kind="hook",
        scheduler="claude-settings", runner="shell", status="wired",
    )
    monkey = {"launchd": [orphan, missing, healthy]}
    # Exercise the summary path directly rather than re-running every collector.
    attention = []
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    for item in monkey["launchd"]:
        status = (item.status or "").lower()
        if status in {"disabled", "paused", "failing", "failed", "orphaned", "missing"} or \
                census_mod.next_run_past_due(item, now):
            attention.append(item.id)
    assert attention == ["x", "y"]


def test_no_hooks_dir_is_not_an_error(census_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(census_mod, "HOME", tmp_path / "empty")
    items, warnings = _collect(census_mod)
    assert items == []
    assert warnings == []


def test_unreadable_settings_warns_but_does_not_raise(census_mod, tmp_path, monkeypatch):
    home = tmp_path / "home2"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude/settings.json").write_text("{not json")
    monkeypatch.setattr(census_mod, "HOME", home)
    items, warnings = _collect(census_mod)
    assert items == []
    assert any("failed to parse" in w for w in warnings)


def test_codex_only_hook_is_wired_not_orphaned(census_mod, fake_home):
    """A plugin declares hooks per HOST, in separate files.

    Reading only hooks.json (the Claude contract) reports every Codex-only
    hook as orphaned. Caught in the field: `post-activity` is wired in
    codex-hooks.json and the census listed it as dead.
    """
    items, _ = _collect(census_mod)
    orphans = {i.name for i in items if i.status == "orphaned"}
    assert not any("post-activity" in n for n in orphans), orphans
    wired = [i for i in items if i.status == "wired" and "post-activity" in i.name]
    assert len(wired) == 1, [i.name for i in items if i.status == "wired"]
    assert wired[0].cadence == "on:PostToolUse"


def test_cache_rollover_fallback_wrapper_is_wired(census_mod, fake_home):
    """unitares-governance-plugin#122 wraps the dispatch in a shell fallback
    (`_unitares_runner="<path>"; if [ ! -f ... ]; then ...; fi;
    "$_unitares_runner" <name> --host <host>`) so a plugin-cache rollover
    doesn't strand the hook. The assignment prefix and trailing `;` must
    not fuse to the path and make it unresolvable, and the conditional's
    shell keywords must not be mistaken for the dispatched hook name."""
    settings = fake_home / "plugins/demo-plugin/hooks/codex-hooks.json"
    settings.write_text(json.dumps({
        "hooks": {
            "PostToolUse": [{
                "matcher": "Edit",
                "hooks": [{
                    "type": "command",
                    "command": (
                        '_unitares_runner="${PLUGIN_ROOT}/hooks/run-hook.cmd"; '
                        'if [ ! -f "$_unitares_runner" ]; then '
                        'for _unitares_candidate in "${PLUGIN_ROOT%/*}"/*/hooks/run-hook.cmd; do '
                        '[ -f "$_unitares_candidate" ] && _unitares_runner="$_unitares_candidate"; '
                        'done; fi; "$_unitares_runner" post-activity --host codex'
                    ),
                }],
            }]
        }
    }))
    items, warnings = _collect(census_mod)
    assert not any("no resolvable script" in w for w in warnings), warnings
    wired = [i for i in items if i.status == "wired" and "post-activity" in i.name]
    assert len(wired) == 1, [(i.name, i.status) for i in items]
    assert "run-hook.cmd" not in wired[0].name


def test_plugin_root_placeholder_is_expanded(census_mod, fake_home):
    """codex-hooks.json uses ${PLUGIN_ROOT}; hooks.json uses
    ${CLAUDE_PLUGIN_ROOT}. Both must resolve, and expanding the shorter
    token first must not leave a dangling "CLAUDE_" prefix."""
    items, _ = _collect(census_mod)
    wired = [i for i in items if i.status == "wired"]
    for item in wired:
        assert "PLUGIN_ROOT" not in item.name, item.name
    # Both declaration files contributed a resolved entry.
    labels = {i.name for i in wired}
    assert any("post-edit" in n for n in labels), labels
    assert any("post-activity" in n for n in labels), labels


def test_cache_installed_plugin_hooks_are_discovered(census_mod, tmp_path, monkeypatch):
    """The exact cache version named by installed_plugins.json is live."""
    home = tmp_path / "home"
    plugin_root = home / ".claude/plugins/cache/some-marketplace/demo-cached-plugin/1.2.3"
    _exe(plugin_root / "hooks/run-hook.cmd")
    _exe(plugin_root / "hooks/post-edit")
    (plugin_root / "hooks/hooks.json").write_text(json.dumps({
        "hooks": {
            "PostToolUse": [{
                "matcher": "Edit",
                "hooks": [{
                    "type": "command",
                    "command": '"${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd" post-edit --host claude',
                }],
            }]
        }
    }))
    installed = home / ".claude/plugins/installed_plugins.json"
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_text(json.dumps({
        "version": 2,
        "plugins": {"demo@market": [{"installPath": str(plugin_root)}]},
    }))
    monkeypatch.setattr(census_mod, "HOME", home)
    items, _ = _collect(census_mod)
    wired = [i for i in items if i.status == "wired" and "post-edit" in i.name]
    assert len(wired) == 1, [(i.name, i.status) for i in items]
    assert "run-hook.cmd" not in wired[0].name


def test_historical_cache_version_is_not_scanned(census_mod, tmp_path, monkeypatch):
    home = tmp_path / "home"
    family = home / ".claude/plugins/cache/market/demo"
    live = family / "2.0.0"
    stale = family / "1.0.0"
    _exe(live / "hooks/live-hook")
    _exe(stale / "hooks/stale-orphan")
    for root, hook in ((live, "live-hook"), (stale, "stale-orphan")):
        (root / "hooks/hooks.json").write_text(json.dumps({
            "hooks": {"Stop": [{"hooks": [{
                "type": "command", "command": str(root / "hooks" / hook),
            }]}]},
        }))
    installed = home / ".claude/plugins/installed_plugins.json"
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_text(json.dumps({
        "version": 2,
        "plugins": {"demo@market": [{"installPath": str(live)}]},
    }))
    monkeypatch.setattr(census_mod, "HOME", home)

    items, _ = _collect(census_mod)
    assert any("live-hook" in item.name for item in items)
    assert not any("stale-orphan" in item.name for item in items)


def test_manifest_declared_claude_hooks_are_discovered(census_mod, tmp_path, monkeypatch):
    home = tmp_path / "home"
    plugin = home / "plugins/manifest-plugin"
    _exe(plugin / "hooks/run-hook.cmd")
    _exe(plugin / "hooks/prompt-submit")
    (plugin / "hooks/claude-hooks.json").write_text(json.dumps({
        "hooks": {"UserPromptSubmit": [{"hooks": [{
            "type": "command",
            "command": '"${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd" prompt-submit',
        }]}]},
    }))
    manifest = plugin / ".claude-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({
        "name": "manifest-plugin", "hooks": "./hooks/claude-hooks.json",
    }))
    installed = home / ".claude/plugins/installed_plugins.json"
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_text(json.dumps({
        "plugins": {"manifest-plugin@local": [{"installPath": str(plugin)}]},
    }))
    monkeypatch.setattr(census_mod, "HOME", home)

    items, _ = _collect(census_mod)
    wired = [item for item in items if "prompt-submit" in item.name]
    assert len(wired) == 1
    assert wired[0].status == "wired"


def test_codex_personal_plugin_symlink_is_a_runtime_root(census_mod, tmp_path, monkeypatch):
    home = tmp_path / "home"
    plugin = home / "src/demo-codex"
    _exe(plugin / "hooks/run-hook.cmd")
    _exe(plugin / "hooks/post-activity")
    (plugin / "hooks/codex-hooks.json").write_text(json.dumps({
        "hooks": {"PostToolUse": [{"hooks": [{
            "type": "command",
            "command": '"${PLUGIN_ROOT}/hooks/run-hook.cmd" post-activity',
        }]}]},
    }))
    link = home / ".codex/plugins/demo-codex"
    link.parent.mkdir(parents=True)
    link.symlink_to(plugin, target_is_directory=True)
    monkeypatch.setattr(census_mod, "HOME", home)

    items, _ = _collect(census_mod)
    assert any(item.status == "wired" and "post-activity" in item.name for item in items)


def test_true_cache_rollover_glob_fallback_resolves(census_mod, tmp_path):
    """Unit-tests `_hook_script_paths` directly against a genuine rollover:
    the direct `${PLUGIN_ROOT}/hooks/run-hook.cmd` path does NOT exist (a
    stale `plugin_root`, simulating the plugin cache having moved on to a
    newer version since this caller last resolved it) but a sibling
    version directory -- reachable only through the `${PLUGIN_ROOT%/*}`
    glob fallback -- does. This is the scenario unitares-governance-plugin
    #122 exists to survive; the parser must not regress to the pre-#122
    "no resolvable script" false positive here."""
    plugin_family = tmp_path / "plugins/cache/some-marketplace/demo-cached-plugin"
    stale_root = plugin_family / "0.4.11"  # does not exist on disk
    current_root = plugin_family / "0.4.12"
    _exe(current_root / "hooks/run-hook.cmd")
    _exe(current_root / "hooks/post-checkin")
    command = (
        '_unitares_runner="${PLUGIN_ROOT}/hooks/run-hook.cmd"; '
        'if [ ! -f "$_unitares_runner" ]; then '
        'for _unitares_candidate in "${PLUGIN_ROOT%/*}"/*/hooks/run-hook.cmd; do '
        '[ -f "$_unitares_candidate" ] && _unitares_runner="$_unitares_candidate"; '
        'done; fi; "$_unitares_runner" post-checkin --host codex'
    )
    scripts = census_mod._hook_script_paths(command, str(stale_root))
    assert scripts, "expected the glob fallback to resolve a sibling version directory"
    resolved_names = {Path(p).name for p in scripts}
    assert "run-hook.cmd" in resolved_names
    assert "post-checkin" in resolved_names
    assert all(str(current_root.resolve()) in p for p in scripts), scripts
