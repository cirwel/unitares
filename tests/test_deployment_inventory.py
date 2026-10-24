"""Deployment defaults cannot select a maintainer fleet; fixtures have no host effects."""

import json
import os
from pathlib import Path
import subprocess

import pytest

OPS = Path(__file__).resolve().parents[1] / "scripts/ops"


def run(tmp_path, rows=None, *args, env=None):
    environment = dict(os.environ)
    for key in ("UNITARES_DEPLOY_INVENTORY", "UNITARES_DEPLOY_INVENTORY_PROVIDER"):
        environment.pop(key, None)
    if rows is not None:
        manifest = tmp_path / "inventory.json"
        manifest.write_text(json.dumps(dict(version=1, services=rows)))
        environment["UNITARES_DEPLOY_INVENTORY"] = str(manifest)
    if env:
        environment.update(env)
    return subprocess.run(
        ["bash", str(OPS / "deploy-status.sh"), *args],
        env=environment,
        text=True,
        capture_output=True,
        timeout=15,
    )


def row(**changes):
    return dict(
        name="sample-worker",
        label="",
        checkout="/example/worktree",
        paths="",
        pickup="library",
        port="",
        **changes,
    )


def test_unconfigured_inventory_refuses_without_selecting_a_fleet(tmp_path):
    result = run(tmp_path, None, "--json")
    assert result.returncode == 2
    assert "configure exactly one" in result.stderr
    assert result.stdout == ""


def test_explicit_empty_inventory_reports_empty(tmp_path):
    result = run(tmp_path, [], "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_inventory_interface_lists_resolved_paths_without_probing(tmp_path):
    result = run(tmp_path, [row()], "--list-checkouts")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/example/worktree"


@pytest.mark.parametrize(
    "change",
    [
        {"checkout": "relative"},
        {"name": "bad\nname"},
        {"deploy_script": "relative.sh"},
        {"paths": "../outside"},
        {"port": "99999"},
        {"pickup": "implicit-fleet"},
    ],
)
def test_invalid_inventory_refuses_before_status_probes(tmp_path, change):
    item = row()
    item.update(change)
    result = run(tmp_path, [item], "--list-checkouts")
    assert result.returncode == 2
    assert not result.stdout


def test_duplicate_service_names_are_rejected(tmp_path):
    result = run(tmp_path, [row(), row()], "--list-checkouts")
    assert result.returncode == 2


def test_status_json_preserves_paths_with_quotes_and_spaces(tmp_path):
    item = row()
    item["checkout"] = str(tmp_path / 'a "quoted" repository')
    result = run(tmp_path, [item], "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)[0]["checkout"] == item["checkout"]


def test_external_adapter_is_explicit_and_failure_is_loud(tmp_path):
    adapter = tmp_path / "adapter"
    adapter.write_text("#!/bin/sh\nexit 7\n")
    adapter.chmod(0o755)
    item = row()
    item.update(pickup="external", status_adapter=str(adapter))
    result = run(tmp_path, [item], "--json")
    assert result.returncode == 2
    assert "external status unavailable" in result.stderr


def test_external_adapter_is_not_promoted_by_local_checkout_state(tmp_path):
    adapter = tmp_path / "adapter"
    expected = dict(
        verdict="REMOTE-UNVERIFIED",
        branch="remote",
        commit="abc",
        behind="2",
        health="external marker",
    )
    adapter.write_text("#!/bin/sh\nprintf '%s\\n' '" + json.dumps(expected) + "'\n")
    adapter.chmod(0o755)
    item = row()
    item.update(pickup="external", status_adapter=str(adapter))
    result = run(tmp_path, [item], "--json")
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)[0]
    assert all(actual[k] == v for k, v in expected.items())


def test_inventory_canonicalizes_checkout_script_and_adapter_aliases(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    nested = checkout / "nested"
    nested.mkdir()
    link = tmp_path / "checkout-link"
    link.symlink_to(checkout, target_is_directory=True)
    script = tmp_path / "adapter"
    status = dict(
        verdict="CURRENT", branch="fixture", commit="abc", behind="0", health=""
    )
    script.write_text("#!/bin/sh\nprintf '%s\\n' '" + json.dumps(status) + "'\n")
    script.chmod(0o755)
    alias = tmp_path / "adapter-link"
    alias.symlink_to(script)
    items = []
    for name, path in [("first", link), ("second", nested)]:
        item = row()
        item.update(
            name=name,
            checkout=str(path),
            pickup="external",
            deploy_script=str(alias),
            status_adapter=str(alias),
        )
        items.append(item)
    result = run(tmp_path, items, "--json")
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    assert {item["checkout"] for item in actual} == {str(checkout.resolve())}
    assert {item["deploy_script"] for item in actual} == {str(script.resolve())}
    checkouts = run(tmp_path, items, "--list-checkouts")
    assert checkouts.stdout.splitlines() == [str(checkout.resolve())]


def test_resolved_inventory_paths_cannot_inject_field_delimiters(tmp_path):
    target = tmp_path / "invalid|target"
    target.mkdir()
    alias = tmp_path / "safe-alias"
    alias.symlink_to(target, target_is_directory=True)
    item = row()
    item["checkout"] = str(alias)
    result = run(tmp_path, [item], "--list-checkouts")
    assert result.returncode == 2
    assert "without field delimiters" in result.stderr
    assert result.stdout == ""


def dispatch_fixture(tmp_path, rows):
    import shutil

    directory = tmp_path / "ops"
    directory.mkdir()
    shutil.copy2(OPS / "deploy-apply.sh", directory / "deploy-apply.sh")
    (directory / "status.json").write_text(json.dumps(rows))
    status = directory / "deploy-status.sh"
    status.write_text('#!/bin/sh\ncat "$(dirname "$0")/status.json"\n')
    status.chmod(0o755)
    return subprocess.run(
        ["bash", str(directory / "deploy-apply.sh"), "--no-fetch"],
        env=dict(os.environ, TRACE=str(tmp_path / "trace")),
        text=True,
        capture_output=True,
        timeout=15,
    )


@pytest.mark.parametrize(
    "spelling", ["symlink", "parent-component", "git-subdirectory"]
)
def test_checkout_alias_cannot_advance_a_failed_shared_tree(tmp_path, spelling):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    if spelling == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(checkout, target_is_directory=True)
    elif spelling == "parent-component":
        staging = tmp_path / "staging"
        staging.mkdir()
        alias = staging / ".." / "checkout"
    else:
        alias = checkout / "nested"
        alias.mkdir()
    first, second = tmp_path / "first.sh", tmp_path / "second.sh"
    first.write_text('#!/bin/sh\necho first >> "$TRACE"\nexit 1\n')
    second.write_text('#!/bin/sh\necho second >> "$TRACE"\n')
    first.chmod(0o755)
    second.chmod(0o755)
    result = dispatch_fixture(
        tmp_path,
        [
            dict(
                name="first",
                verdict="STALE(1)",
                checkout=str(checkout),
                deploy_script=str(first),
            ),
            dict(
                name="second",
                verdict="STALE(1)",
                checkout=str(alias),
                deploy_script=str(second),
            ),
        ],
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert (tmp_path / "trace").read_text().splitlines() == ["first"]
    assert "HOLD  second" in result.stderr


def test_script_alias_cannot_restart_a_down_sibling(tmp_path):
    script = tmp_path / "paired.sh"
    script.write_text('#!/bin/sh\necho unexpected >> "$TRACE"\n')
    script.chmod(0o755)
    alias = tmp_path / "paired-alias.sh"
    alias.symlink_to(script)
    result = dispatch_fixture(
        tmp_path,
        [
            dict(
                name="running",
                verdict="STALE(1)",
                checkout=str(tmp_path / "one"),
                deploy_script=str(script),
            ),
            dict(
                name="stopped",
                verdict="DOWN",
                checkout=str(tmp_path / "two"),
                deploy_script=str(alias),
            ),
        ],
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert not (tmp_path / "trace").exists()
    assert "also controls a DOWN service" in result.stderr
