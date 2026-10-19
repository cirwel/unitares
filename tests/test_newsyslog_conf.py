"""The newsyslog template stays parseable and covers every launchd log file.

newsyslog skips a malformed line and carries on with the rest, so a typo in
the template silently stops one file from rotating. These checks pin the
field shape of every entry.
"""

from __future__ import annotations

from pathlib import Path

CONF = Path(__file__).resolve().parent.parent / "scripts" / "ops" / "newsyslog-unitares.conf"

# newsyslog.conf(5) flags on macOS, plus the "-" placeholder.
_VALID_FLAGS = set("BCDGJNUZ-")


def _entries() -> list[list[str]]:
    rows = []
    for raw in CONF.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(line.split())
    return rows


def test_every_entry_has_the_seven_newsyslog_fields():
    entries = _entries()
    assert entries, "template has no rotation entries"
    for fields in entries:
        assert len(fields) == 7, fields
        path, owner, mode, count, size, when, flags = fields
        assert path.startswith("__HOME__/Library/Logs/"), path
        assert owner == "__USER__:staff", fields
        int(mode, 8)
        assert int(count) > 0, fields
        assert int(size) > 0, fields
        assert when == "*", fields
        assert set(flags) <= _VALID_FLAGS, fields


def test_log_paths_are_unique():
    paths = [fields[0] for fields in _entries()]
    assert len(paths) == len(set(paths))


def test_bridge_stderr_log_is_rotated():
    # The bridge writes its main log itself; launchd's StandardOut/ErrorPath
    # goes to a separate stderr file, which launchd never rotates.
    paths = {fields[0] for fields in _entries()}
    assert "__HOME__/Library/Logs/unitares-discord-bridge.log" in paths
    assert "__HOME__/Library/Logs/unitares-discord-bridge.stderr.log" in paths
