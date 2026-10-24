#!/usr/bin/env python3
"""Find private deployment defaults beyond the installed Python packages.

Scans executable literals/configuration throughout agents, ops, Elixir, and
product code. Comments, docstrings, synthetic tests and attribution are not
runtime selections. The identity guard separately detects label dispatch in
core and served prose; this guard detects private paths, hosts and accounts.
This is bounded static coverage, not a proof of all possible semantic coupling.
"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
ROOTS = ('src/', 'governance_core/', 'config/', 'agents/', 'scripts/ops/', 'elixir/')
SUFFIXES = {'.py', '.sh', '.bash', '.ex', '.exs', '.json', '.toml', '.yaml', '.yml', '.plist', '.template', '.env'}
PATTERNS = (
    re.compile(r'/Users/cirwel\b|/home/(?:cirwel|pi)\b', re.I),
    re.compile(r'(?:~|\$HOME|\$\{HOME\})/(?:projects|\.config/cirwel)(?:/|\b)'),
    re.compile(r'(?<![\w.-])(?:[\w-]+\.)*cirwel\.org\b', re.I),
    re.compile(r'(?:https?://|ssh\s+)(?:lumen|pi-anima)(?=[:/\s"\x27]|$)', re.I),
    re.compile(r'(?<![\w/.-])cirwel/[\w-]+', re.I),
    re.compile(r'github\.cirwel\.', re.I),
)
# Exact repository metadata/release tooling, not deployment defaults. URLs to
# the public source and published artifacts remain valid project attribution.
ATTRIBUTION = re.compile(r'(?:https://github\.com|git@github\.com:|https?://ghcr\.io)/?cirwel/[\w.-]+', re.I)
PROJECT_TOOLS = {
    'scripts/ops/version_manager.py': 'updates this project\'s published release references',
}


def executable_text(path: Path):
    """Yield line/text; Python literals avoid flagging historical commentary."""
    text = path.read_text(encoding='utf-8')
    if path.suffix == '.py':
        tree = ast.parse(text)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docstrings.add(id(first.value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                yield node.lineno, node.value
        return
    # Plist templates may carry long installation examples in XML comments.
    text = re.sub(r'<!--.*?-->', lambda m: '\n' * m.group().count('\n'), text, flags=re.S)
    # Elixir multiline module docs are provenance, like Python docstrings.
    text = re.sub(r'@(?:module)?doc\s+""".*?"""', lambda m: '\n' * m.group().count('\n'), text, flags=re.S)
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith('#'):
            continue
        yield number, line


def scan_file(path: Path, relative: str) -> list[str]:
    if relative in PROJECT_TOOLS:
        return []
    hits = []
    try:
        for line, value in executable_text(path):
            value = ATTRIBUTION.sub('', value)
            for pattern in PATTERNS:
                match = pattern.search(value)
                if match:
                    hits.append(f'{relative}:{line}: private deployment default {match.group()!r}')
    except (SyntaxError, UnicodeError) as exc:
        hits.append(f'{relative}: cannot inspect runtime source: {exc}')
    return hits


def runtime_files(root: Path):
    paths = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                           check=True, capture_output=True).stdout.decode().split('\0')
    for relative in sorted(set(paths)):
        path = Path(relative)
        if (not relative.startswith(ROOTS) or path.suffix not in SUFFIXES
                or any(p in {'tests', 'test', 'fixtures', '__pycache__', 'vendor', 'deps', '_build', 'archive'} for p in path.parts)
                or path.name.startswith(('test_', 'test-'))):
            continue
        if (root / path).is_file():
            yield relative


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    files = list(runtime_files(args.root))
    findings = [hit for rel in files for hit in scan_file(args.root / rel, rel)]
    print('\n'.join(findings) if findings else f'Operator defaults: {len(files)} runtime files checked; no private paths, hosts or account selections.')
    return int(bool(findings))


if __name__ == '__main__':
    raise SystemExit(main())
