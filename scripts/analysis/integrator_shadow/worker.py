"""Internal isolated worker; only the parent runner should invoke this file."""

import argparse
import json
import logging
import os
from pathlib import Path
import runpy
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--kind", choices=("preservation", "numerics"), required=True)
    args = parser.parse_args()
    root = args.source_dir.resolve()
    denied = []

    # Source archives deliberately have no .git. Three import-time metadata
    # probes normally discover that via Git; emulate that failure without
    # launching a child (which would escape this Python audit hook).
    original_run = subprocess.run
    metadata_commands = [
        ["git", "-C", str(root), "log", "-1", "--format=%cs"],
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
        ["git", "-C", str(root), "rev-parse", "HEAD"],
    ]

    def offline_run(command, *positional, **kwargs):
        if command in metadata_commands and not positional and not kwargs.get("shell"):
            return subprocess.CompletedProcess(
                command, 128, "", "offline source archive"
            )
        return original_run(command, *positional, **kwargs)

    subprocess.run = offline_run

    descriptors = {}
    original_open = os.open
    original_close = os.close

    def resolved(path, dir_fd=None):
        if isinstance(path, int):
            return descriptors.get(path)
        target = Path(os.fsdecode(path))
        if not target.is_absolute() and dir_fd not in (None, -1):
            base = descriptors.get(dir_fd)
            if base is None:
                return None
            target = base / target
        return target.resolve()

    def inside(path, dir_fd=None):
        if isinstance(path, int) and path in (0, 1, 2):
            return True
        target = resolved(path, dir_fd)
        return target is not None and target.is_relative_to(root)

    def offline_open(path, flags, mode=0o777, *, dir_fd=None):
        # CPython's open audit event omits dir_fd. Resolve it here so the
        # audit hook receives an absolute path, including for shutil cleanup.
        target = resolved(path, dir_fd)
        if target is None:
            denied.append("os.open:unknown_dir_fd")
            raise RuntimeError(
                "offline shadow worker denied unknown directory descriptor"
            )
        fd = original_open(target, flags, mode)
        descriptors[fd] = target
        return fd

    def offline_close(fd):
        try:
            return original_close(fd)
        finally:
            descriptors.pop(fd, None)

    os.open = offline_open
    os.close = offline_close

    def guard(event, values):
        blocked = event in {
            "socket.connect",
            "socket.getaddrinfo",
            "socket.sendto",
            "socket.bind",
            "subprocess.Popen",
            "os.system",
            "os.posix_spawn",
            "os.exec",
            "os.fork",
            "os.forkpty",
        }
        if event == "open":
            path, mode, flags = values
            writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            blocked = writing and not inside(path)
        elif event in {
            "os.mkdir",
            "os.remove",
            "os.rmdir",
            "os.chmod",
            "os.truncate",
            "os.chown",
            "os.utime",
        }:
            descriptor_index = {
                "os.mkdir": 2,
                "os.remove": 1,
                "os.rmdir": 1,
                "os.chmod": 2,
                "os.chown": 3,
                "os.utime": 3,
            }.get(event)
            directory_fd = (
                values[descriptor_index] if descriptor_index is not None else None
            )
            blocked = not inside(values[0], directory_fd)
        elif event in {"os.rename", "os.link", "os.symlink"}:
            if event == "os.symlink":
                blocked = not inside(values[0]) or not inside(values[1], values[2])
            else:
                blocked = not inside(values[0], values[2]) or not inside(
                    values[1], values[3]
                )
        if blocked:
            denied.append(event)
            raise RuntimeError(f"offline shadow worker denied {event}")

    sys.addaudithook(guard)
    sys.dont_write_bytecode = True
    logging.disable(logging.CRITICAL)
    sys.path.insert(0, str(root))
    if args.kind == "preservation":
        from tests import eisv_preservation_driver as driver

        result = driver.run_all()
        if denied:
            raise RuntimeError(f"worker attempted forbidden operations: {denied}")
        print(json.dumps(result, sort_keys=True, allow_nan=False))
    else:
        script = Path(__file__).with_name("numerics.py")
        sys.argv = [str(script), "--source-dir", str(root)]
        runpy.run_path(str(script), run_name="__main__")
        if denied:
            raise RuntimeError(f"worker attempted forbidden operations: {denied}")


if __name__ == "__main__":
    main()
