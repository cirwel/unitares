"""The deployment environment overlay that deploy-mcp.sh applies to the live
governance plist (scripts/ops/apply_plist_env_overlay.py)."""

from __future__ import annotations

import importlib.util
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts/ops/apply_plist_env_overlay.py"
OVERLAY = REPO / "scripts/ops/governance-mcp.env"
DEPLOY = REPO / "scripts/ops/deploy-mcp.sh"

_spec = importlib.util.spec_from_file_location("apply_plist_env_overlay", SCRIPT)
overlay_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(overlay_mod)


def _plist(tmp_path: Path, env: dict[str, str] | None, mode: int = 0o600) -> Path:
    path = tmp_path / "com.unitares.governance-mcp.plist"
    payload = {"Label": "com.unitares.governance-mcp", "ProgramArguments": ["python3"]}
    if env is not None:
        payload["EnvironmentVariables"] = env
    path.write_bytes(plistlib.dumps(payload))
    path.chmod(mode)
    return path


def _overlay(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "governance-mcp.env"
    path.write_text(text)
    return path


def _env(plist: Path) -> dict[str, str]:
    return plistlib.loads(plist.read_bytes())["EnvironmentVariables"]


def test_sets_missing_and_changed_keys_and_leaves_the_rest(tmp_path):
    plist = _plist(tmp_path, {"UNITARES_HTTP_API_TOKEN": "secret", "A_KEY": "old"})
    overlay = _overlay(tmp_path, "# comment\n\nA_KEY=new\nB_KEY=100.64.0.0/10\n")
    assert overlay_mod.apply_overlay(plist, overlay) == ["A_KEY", "B_KEY"]
    assert _env(plist) == {"UNITARES_HTTP_API_TOKEN": "secret", "A_KEY": "new",
                           "B_KEY": "100.64.0.0/10"}


HAND_WRITTEN = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <!-- an operator's note -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>A_KEY</key>
        <string>v</string>
    </dict>
</dict>
</plist>
"""


def test_an_applied_overlay_is_a_no_op_and_does_not_rewrite(tmp_path):
    # An unchanged plist keeps its hash, so the deploy kickstarts instead of
    # reloading (deploy-lib.sh's plist-hash sidecar). The fixture is not in
    # plistlib's own layout, so a needless rewrite would show.
    plist = tmp_path / "com.unitares.governance-mcp.plist"
    plist.write_bytes(HAND_WRITTEN)
    overlay = _overlay(tmp_path, "A_KEY=v\n")
    before = plist.read_bytes()
    assert overlay_mod.apply_overlay(plist, overlay) == []
    assert plist.read_bytes() == before


@pytest.mark.parametrize("mode", [0o600, 0o644])
def test_creates_the_environment_dict_and_keeps_the_mode(tmp_path, mode):
    # 0o644 would come out 0o600 if the mode were not carried over (mkstemp's own).
    plist = _plist(tmp_path, None, mode=mode)
    overlay_mod.apply_overlay(plist, _overlay(tmp_path, "A_KEY=v\n"))
    assert _env(plist) == {"A_KEY": "v"}
    assert stat.S_IMODE(plist.stat().st_mode) == mode


def test_a_failed_write_leaves_the_plist_and_no_temp_file(tmp_path, monkeypatch):
    plist = _plist(tmp_path, {})
    before = plist.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(overlay_mod.plistlib, "dump", boom)
    with pytest.raises(OSError):
        overlay_mod.apply_overlay(plist, _overlay(tmp_path, "A_KEY=v\n"))
    assert plist.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "com.unitares.governance-mcp.plist", "governance-mcp.env"]


def test_a_symlinked_plist_stays_a_symlink(tmp_path):
    # The Claude review on #2585: os.replace on the link would detach it.
    (tmp_path / "real").mkdir()
    real = _plist(tmp_path / "real", {})
    link = tmp_path / "link.plist"
    link.symlink_to(real)
    overlay_mod.apply_overlay(link, _overlay(tmp_path, "A_KEY=v\n"))
    assert link.is_symlink() and _env(real) == {"A_KEY": "v"}


def test_a_failed_backup_leaves_no_partial_original(tmp_path, monkeypatch):
    # Codex on #2585: a backup cut short (a full disk) must not be kept as the
    # write-once original, and the plist must not be rewritten without one.
    plist = tmp_path / "com.unitares.governance-mcp.plist"
    plist.write_bytes(HAND_WRITTEN)
    backups = tmp_path / "state"
    real_fsync = overlay_mod.os.fsync

    def full_disk(fd):
        if stat.S_ISREG(overlay_mod.os.fstat(fd).st_mode):
            raise OSError(28, "No space left on device")
        return real_fsync(fd)

    monkeypatch.setattr(overlay_mod.os, "fsync", full_disk)
    with pytest.raises(OSError):
        overlay_mod.apply_overlay(plist, _overlay(tmp_path, "B_KEY=w\n"), backup_dir=backups)
    assert list(backups.iterdir()) == []
    assert plist.read_bytes() == HAND_WRITTEN
    monkeypatch.setattr(overlay_mod.os, "fsync", real_fsync)
    overlay_mod.apply_overlay(plist, _overlay(tmp_path, "B_KEY=w\n"), backup_dir=backups)
    assert (backups / "com.unitares.governance-mcp.plist.pre-overlay").read_bytes() == HAND_WRITTEN


def test_the_original_bytes_are_kept_before_a_change(tmp_path):
    # Rewriting through plistlib drops XML comments; the original survives.
    plist = tmp_path / "com.unitares.governance-mcp.plist"
    plist.write_bytes(HAND_WRITTEN)
    backups = tmp_path / "state"
    overlay_mod.apply_overlay(plist, _overlay(tmp_path, "B_KEY=w\n"), backup_dir=backups)
    kept = backups / "com.unitares.governance-mcp.plist.pre-overlay"
    assert kept.read_bytes() == HAND_WRITTEN
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    # Codex on #2585: a second change must not replace the original with the
    # already-rewritten copy.
    overlay_mod.apply_overlay(plist, _overlay(tmp_path, "B_KEY=x\n"), backup_dir=backups)
    assert kept.read_bytes() == HAND_WRITTEN


def test_the_backup_entry_is_synced_before_the_plist_is_replaced(tmp_path, monkeypatch):
    # Codex on #2585: syncing the file does not persist its new name, so the
    # backup directory is synced before the plist rewrite.
    plist = tmp_path / "com.unitares.governance-mcp.plist"
    plist.write_bytes(HAND_WRITTEN)
    backups = tmp_path / "state"
    events: list[str] = []
    real_fsync, real_replace = overlay_mod.os.fsync, overlay_mod.os.replace

    def fsync(fd):
        if stat.S_ISDIR(overlay_mod.os.fstat(fd).st_mode):
            events.append("sync-dir")
        return real_fsync(fd)

    def replace(src, dst):
        events.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(overlay_mod.os, "fsync", fsync)
    monkeypatch.setattr(overlay_mod.os, "replace", replace)
    backups.mkdir()
    overlay_mod.apply_overlay(plist, _overlay(tmp_path, "B_KEY=w\n"), backup_dir=backups)
    assert events == ["sync-dir", "replace"]


def test_a_new_backup_directory_is_synced_into_its_parents(tmp_path, monkeypatch):
    # Codex on #2585: on a fresh host the state directory is created here, and
    # its own entry must be durable before the plist is replaced, or a crash
    # can keep the rewritten plist and lose the backup directory with it.
    plist = tmp_path / "com.unitares.governance-mcp.plist"
    plist.write_bytes(HAND_WRITTEN)
    backups = tmp_path / "home" / "state"
    synced: list[object] = []  # directory (device, inode) pairs, in order
    real_fsync, real_replace = overlay_mod.os.fsync, overlay_mod.os.replace

    def fsync(fd):
        info = overlay_mod.os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            synced.append((info.st_dev, info.st_ino))
        return real_fsync(fd)

    def replace(src, dst):
        synced.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(overlay_mod.os, "fsync", fsync)
    monkeypatch.setattr(overlay_mod.os, "replace", replace)
    overlay_mod.apply_overlay(plist, _overlay(tmp_path, "B_KEY=w\n"), backup_dir=backups)
    ident = [(d.stat().st_dev, d.stat().st_ino) for d in (tmp_path, tmp_path / "home", backups)]
    assert synced == [*ident, "replace"]


def test_dry_run_reports_without_writing(tmp_path):
    plist = _plist(tmp_path, {})
    before = plist.read_bytes()
    assert overlay_mod.apply_overlay(plist, _overlay(tmp_path, "A_KEY=v\n"), dry_run=True) == ["A_KEY"]
    assert plist.read_bytes() == before


@pytest.mark.parametrize("text", ["no equals sign\n", "lower=1\n", "A_KEY=1\nA_KEY=2\n", "=v\n",
                                  "A_KEY=100.64.0.0/10  # tailnet\n", "A_KEY= v\n"])
def test_a_malformed_overlay_is_refused_and_the_plist_untouched(tmp_path, text):
    plist = _plist(tmp_path, {"A_KEY": "old"})
    before = plist.read_bytes()
    result = subprocess.run([sys.executable, str(SCRIPT), "--plist", str(plist),
                             "--overlay", str(_overlay(tmp_path, text))],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert plist.read_bytes() == before


def test_values_are_never_printed(tmp_path):
    plist = _plist(tmp_path, {})
    result = subprocess.run([sys.executable, str(SCRIPT), "--plist", str(plist),
                             "--overlay", str(_overlay(tmp_path, "A_KEY=sekrit-value\n"))],
                            capture_output=True, text=True, check=True)
    assert "A_KEY" in result.stdout and "sekrit-value" not in result.stdout + result.stderr


@pytest.mark.parametrize("text", ["a_key=sekrit-value\n", "sekrit-value\n"])
def test_a_refused_line_does_not_print_its_value(tmp_path, text):
    # Codex on #2585: a mistyped key must not echo the value it guards.
    result = subprocess.run([sys.executable, str(SCRIPT), "--plist", str(_plist(tmp_path, {})),
                             "--overlay", str(_overlay(tmp_path, text))],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "line 1" in result.stderr and "sekrit-value" not in result.stdout + result.stderr


def test_an_undecodable_overlay_is_refused_without_its_bytes(tmp_path):
    # The independent review on #2585: a decode error used to escape as a
    # traceback quoting the offending byte of a value.
    plist = _plist(tmp_path, {})
    overlay = tmp_path / "governance-mcp.env"
    overlay.write_bytes(b"A_KEY=caf\xe9-value\n")
    result = subprocess.run([sys.executable, str(SCRIPT), "--plist", str(plist),
                             "--overlay", str(overlay)], capture_output=True, text=True)
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "0xe9" not in result.stderr and "caf" not in result.stdout + result.stderr


@pytest.mark.parametrize("body", [b"<?xml version='1.0'?><plist><dict><key>A</key>",
                                  b"not a plist at all"])
def test_an_unreadable_plist_is_refused_with_exit_2(tmp_path, body):
    # The independent review on #2585: malformed XML raised ExpatError, which
    # the handler did not catch.
    plist = tmp_path / "com.unitares.governance-mcp.plist"
    plist.write_bytes(body)
    result = subprocess.run([sys.executable, str(SCRIPT), "--plist", str(plist),
                             "--overlay", str(_overlay(tmp_path, "A_KEY=v\n"))],
                            capture_output=True, text=True)
    assert result.returncode == 2 and "Traceback" not in result.stderr
    assert plist.read_bytes() == body


def test_the_tracked_overlay_parses_and_the_deploy_applies_it():
    values = overlay_mod.parse_overlay(OVERLAY.read_text())
    assert values, "the deployment overlay must set something"
    deploy = DEPLOY.read_text()
    apply_at = deploy.index('if ! deploy_lib_apply_env_overlay "$TAG"')
    assert apply_at < deploy.index('deploy_lib_restart_service "$TAG"'), \
        "the overlay must land before the restart that reloads a changed plist"


def test_the_overlay_is_second_family_reviewed():
    # It can widen the auth bypass (UNITARES_TRUSTED_NETWORKS), like access.py.
    import json
    paths = json.loads((REPO / "scripts/dev/review_policy.json").read_text())["second_family_paths"]
    assert "scripts/ops/governance-mcp.env" in paths


LIB = REPO / "scripts/ops/deploy-lib.sh"
LABEL = "com.unitares.governance-mcp"


def _deploy(tmp_path: Path, plist: Path, overlay: Path, *, baseline: str | None,
            applier: Path = SCRIPT, prelude: str = "", bootstrap_fails: bool = False,
            check: bool = True, postlude: str = "") -> list[str]:
    """Run deploy-lib's overlay step and then its restart, with a stub
    launchctl that records its calls. Returns the recorded subcommands of
    this run. ``prelude`` runs after deploy-lib is sourced (to stub one of
    its helpers)."""
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if baseline is not None:
        (state / f"{LABEL}.plist.sha256").write_text(baseline)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "launchctl.log"
    log.write_text("")
    stub = bin_dir / "launchctl"
    # `print` fails: the label is gone after bootout, and the env-key check
    # treats empty output as "cannot tell". Everything else succeeds, unless
    # bootstrap is told to fail.
    failing = "[ \"$1\" = bootstrap ] && exit 5\n" if bootstrap_fails else ""
    stub.write_text(f'#!/bin/sh\necho "$1" >> "{log}"\n[ "$1" = print ] && exit 1\n{failing}exit 0\n')
    stub.chmod(0o755)
    script = (f'set -euo pipefail; . "{LIB}"; {prelude}\n'
              f'deploy_lib_apply_env_overlay t {LABEL} "{plist}" "{overlay}" "{applier}"; '
              f'deploy_lib_restart_service t gui/501 {LABEL} "{plist}"; {postlude}')
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path),
           "UNITARES_DEPLOY_STATE_DIR": str(state)}
    subprocess.run(["bash", "-c", script], env=env, check=check, capture_output=True, text=True)
    return [line for line in log.read_text().split() if line != "print"]


def _sha(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("baseline", ["none", "matching"])
def test_a_changed_overlay_is_reloaded_even_without_a_baseline(tmp_path, baseline):
    # Codex on #2585: with no plist baseline the restart adopts the current
    # hash and kickstarts, and a kickstart never re-reads the plist. The
    # overlay step records the pre-overlay hash so the restart reloads.
    plist = _plist(tmp_path, {"UNITARES_HTTP_API_TOKEN": "secret"})
    before = _sha(plist)
    calls = _deploy(tmp_path, plist, _overlay(tmp_path, "A_KEY=v\n"),
                    baseline=None if baseline == "none" else before)
    assert calls == ["bootout", "bootstrap"]
    assert (tmp_path / "state" / f"{LABEL}.plist.sha256").read_text() == _sha(plist)


def test_a_hand_edit_the_overlay_undoes_is_reloaded(tmp_path):
    # The independent review on #2585: the operator blanked an overlay key by
    # hand and reloaded, so the recorded hash is of the plist the overlay is
    # about to write back. The restart must still reload, or the blank value
    # stays live while the plist on disk says otherwise.
    overlaid = _plist(tmp_path, {"A_KEY": "v"})
    baseline = _sha(overlaid)
    plist = _plist(tmp_path, {"A_KEY": ""})
    calls = _deploy(tmp_path, plist, _overlay(tmp_path, "A_KEY=v\n"), baseline=baseline)
    assert _sha(plist) == baseline  # the overlay wrote the recorded bytes back
    assert calls == ["bootout", "bootstrap"]


def test_an_unwritable_baseline_aborts_before_overlay_and_reload(tmp_path):
    plist = _plist(tmp_path, {})
    original = plist.read_bytes()
    state = tmp_path / "state"
    (state / f"{LABEL}.plist.sha256").mkdir(parents=True)
    calls = _deploy(tmp_path, plist, _overlay(tmp_path, "A_KEY=v\n"),
                    baseline=None, bootstrap_fails=True, check=False)
    assert calls == []
    assert plist.read_bytes() == original
    # The next deploy must still see pending changes, rather than adopt a
    # modified disk definition whose first reload failed.
    (state / f"{LABEL}.plist.sha256").rmdir()
    assert _deploy(tmp_path, plist, _overlay(tmp_path, "A_KEY=v\n"), baseline=None) == ["bootout", "bootstrap"]


def test_an_unchanged_overlay_keeps_the_kickstart(tmp_path):
    plist = _plist(tmp_path, {"A_KEY": "v"})
    calls = _deploy(tmp_path, plist, _overlay(tmp_path, "A_KEY=v\n"), baseline=_sha(plist))
    assert calls == ["kickstart"]


@pytest.mark.parametrize("apply_sibling_overlay", [False, True])
def test_overlay_reload_does_not_leak_to_a_sibling_service(tmp_path, apply_sibling_overlay):
    sibling_dir = tmp_path / "sibling"
    sibling_dir.mkdir()
    sibling = _plist(sibling_dir, {"A_KEY": "v"})
    sibling_label = "com.example.sibling"
    sibling_overlay = _overlay(sibling_dir, "A_KEY=v\n")
    state = tmp_path / "state"
    state.mkdir()
    (state / f"{sibling_label}.plist.sha256").write_text(_sha(sibling))
    postlude = ""
    if apply_sibling_overlay:
        postlude += (f'deploy_lib_apply_env_overlay t {sibling_label} "{sibling}" '
                     f'"{sibling_overlay}" "{SCRIPT}"; ')
    postlude += f'deploy_lib_restart_service t gui/501 {sibling_label} "{sibling}"'

    plist = _plist(tmp_path, {})
    calls = _deploy(tmp_path, plist, _overlay(tmp_path, "A_KEY=v\n"),
                    baseline=None, postlude=postlude)
    assert calls == ["bootout", "bootstrap", "kickstart"]


def test_a_change_the_applier_wrote_before_failing_is_reloaded(tmp_path):
    # The independent review on #2585: the applier can fail after its write
    # (a dead stdout pipe). The deploy must compare the plist anyway, not
    # take the failure as "nothing written" and kickstart.
    applier = tmp_path / "applier.py"
    applier.write_text(f"import subprocess, sys\n"
                       f"result = subprocess.run([sys.executable, {str(SCRIPT)!r}, *sys.argv[1:]])\n"
                       f"sys.exit(result.returncode if '--check' in sys.argv else 1)\n")
    plist = _plist(tmp_path, {})
    calls = _deploy(tmp_path, plist, _overlay(tmp_path, "A_KEY=v\n"), baseline=None, applier=applier)
    assert _env(plist) == {"A_KEY": "v"}
    assert calls == ["bootout", "bootstrap"]


def test_a_matching_baseline_that_cannot_be_rewritten_aborts(tmp_path):
    # The independent review on #2585: a hand edit the overlay undoes leaves
    # the recorded hash equal to the plist the overlay writes back. When the
    # pre-overlay sidecar write also fails, only the force flag keeps the
    # restart from kickstarting.
    baseline = _sha(_plist(tmp_path, {"A_KEY": "v"}))
    plist = _plist(tmp_path, {"A_KEY": ""})
    calls = _deploy(tmp_path, plist, _overlay(tmp_path, "A_KEY=v\n"), baseline=baseline,
                    prelude="_deploy_lib_write_sidecar() { return 0; }", check=False)
    assert _env(plist) == {"A_KEY": ""}
    assert calls == []


def test_a_failed_reload_is_retried_by_the_next_deploy(tmp_path):
    # The independent review on #2585: the pre-overlay hash is what makes the
    # next deploy reload when this one's reload failed, because the overlay
    # then finds nothing left to change.
    plist = _plist(tmp_path, {})
    overlay = _overlay(tmp_path, "A_KEY=v\n")
    first = _deploy(tmp_path, plist, overlay, baseline=None, bootstrap_fails=True, check=False)
    assert first[:2] == ["bootout", "bootstrap"]
    assert _deploy(tmp_path, plist, overlay, baseline=None) == ["bootout", "bootstrap"]


def test_a_failed_baseline_write_keeps_the_previous_hash(tmp_path):
    # Codex on #2585: the sidecar write used to truncate before writing, so a
    # write that failed (a full disk) left it empty. After a refused reload
    # the next deploy read "no baseline" and kickstarted the stale definition.
    plist = _plist(tmp_path, {})
    overlay = _overlay(tmp_path, "A_KEY=v\n")
    full_disk = ('printf() { if [[ "${2:-}" =~ ^[0-9a-f]{64}$ ]]; then return 1; fi; '
                 'builtin printf "$@"; }')
    _deploy(tmp_path, plist, overlay, baseline="0" * 64, prelude=full_disk,
            bootstrap_fails=True, check=False)
    state = tmp_path / "state"
    assert (state / f"{LABEL}.plist.sha256").read_text() == "0" * 64
    assert [p.name for p in state.iterdir() if ".tmp." in p.name] == []
    assert _deploy(tmp_path, plist, overlay, baseline=None) == ["bootout", "bootstrap"]


@pytest.mark.parametrize("invalid", [[], "", 0, False])
def test_falsey_invalid_environment_is_rejected_without_rewrite(tmp_path, invalid):
    plist = _plist(tmp_path, invalid)
    before = plist.read_bytes()
    result = subprocess.run([sys.executable, str(SCRIPT), "--plist", str(plist),
                             "--overlay", str(_overlay(tmp_path, "A_KEY=v\n"))],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "EnvironmentVariables is not a dict" in result.stderr
    assert "Traceback" not in result.stderr
    assert plist.read_bytes() == before


@pytest.mark.parametrize("kind", ["directory", "corrupt", "symlink", "dangling", "public"])
def test_invalid_existing_backup_prevents_rewrite(tmp_path, kind):
    plist = _plist(tmp_path, {})
    original = plist.read_bytes()
    backups = tmp_path / "state"
    backups.mkdir()
    kept = backups / f"{plist.name}.pre-overlay"
    if kind == "directory":
        kept.mkdir()
    elif kind in {"symlink", "dangling"}:
        kept.symlink_to(plist if kind == "symlink" else tmp_path / "missing")
    else:
        kept.write_bytes(b"invalid" if kind == "corrupt" else original)
        kept.chmod(0o644 if kind == "public" else 0o600)
    with pytest.raises(overlay_mod.OverlayError):
        overlay_mod.apply_overlay(plist, _overlay(tmp_path, "A_KEY=v\n"), backup_dir=backups)
    assert plist.read_bytes() == original


def test_missing_applier_aborts_before_restart(tmp_path):
    plist = _plist(tmp_path, {})
    original = plist.read_bytes()
    assert _deploy(tmp_path, plist, _overlay(tmp_path, "A_KEY=v\n"),
                   baseline=None, applier=tmp_path / "missing.py", check=False) == []
    assert plist.read_bytes() == original


@pytest.mark.parametrize("character", ["\x01", "\ufffe"])
def test_invalid_xml_value_returns_clean_error_and_keeps_plist(tmp_path, character):
    plist = _plist(tmp_path, {})
    original = plist.read_bytes()
    result = subprocess.run([sys.executable, str(SCRIPT), "--plist", str(plist),
                             "--overlay", str(_overlay(tmp_path, f"A_KEY=v{character}\n"))],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert plist.read_bytes() == original
