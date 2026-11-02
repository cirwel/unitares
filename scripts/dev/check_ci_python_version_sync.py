"""Check the running interpreter against the package floor and dev default."""

from pathlib import Path
import re
import sys
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    print(f"Running Python {sys.version}")
    try:
        project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
        requirement = project["project"]["requires-python"]
        match = re.fullmatch(r">=\s*(\d+)\.(\d+)", requirement.strip())
        if not match:
            raise ValueError(f"unsupported requires-python: {requirement!r}")
        minimum = tuple(map(int, match.groups()))
    except (OSError, KeyError, ValueError) as exc:
        print(f"ERROR: cannot read the package Python floor: {exc}")
        return 1

    if sys.version_info[:2] < minimum:
        print(f"ERROR: Python {minimum[0]}.{minimum[1]}+ required, got {sys.version}")
        return 1

    version_file = PROJECT_ROOT / ".python-version"
    if version_file.exists():
        expected = version_file.read_text().strip()
        actual = f"{sys.version_info.major}.{sys.version_info.minor}"
        if expected != actual and not expected.startswith(actual + "."):
            print(f"WARNING: .python-version says {expected}, running {actual}")
            # The supported floor and development default may intentionally differ.

    print("Python version check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
