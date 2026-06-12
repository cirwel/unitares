#!/usr/bin/env python3
"""
Auto-update CHANGELOG.md from git commits using conventional commit format.

Parses commits since the last release tag and updates CHANGELOG.md with:
- Conventional commit categorization (feat, fix, docs, etc.)
- Breaking changes
- Automatic version bumping (semver)

Conventional Commit Format:
  <type>(<scope>): <description>

  [optional body]

  [optional footer]

Types:
  - feat: New feature (minor version bump)
  - fix: Bug fix (patch version bump)
  - docs: Documentation changes
  - style: Code style changes
  - refactor: Code refactoring
  - perf: Performance improvements
  - test: Test changes
  - build: Build system changes
  - ci: CI/CD changes
  - chore: Other changes
  - BREAKING CHANGE: Breaking changes (major version bump)
"""

import re
import subprocess
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional
from dataclasses import dataclass, field
from collections import defaultdict


@dataclass
class Commit:
    """Represents a parsed git commit."""
    hash: str
    type: str
    scope: Optional[str]
    description: str
    body: str
    breaking: bool
    raw_message: str


@dataclass
class ChangelogSection:
    """Represents a section in the changelog."""
    title: str
    items: List[str] = field(default_factory=list)


class ConventionalCommitParser:
    """Parse conventional commits from git history."""

    # Conventional commit pattern
    COMMIT_PATTERN = re.compile(
        r'^(?P<type>\w+)(\((?P<scope>[\w\-]+)\))?: (?P<description>.+)$'
    )

    # Type to changelog section mapping
    TYPE_SECTIONS = {
        'feat': 'Added',
        'fix': 'Fixed',
        'docs': 'Documentation',
        'style': 'Style',
        'refactor': 'Changed',
        'perf': 'Performance',
        'test': 'Tests',
        'build': 'Build',
        'ci': 'CI/CD',
        'chore': 'Chore',
    }

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path

    def get_commits_since_tag(self, tag: Optional[str] = None) -> List[str]:
        """Get commits since a tag (or all commits if no tag)."""
        try:
            if tag:
                cmd = ['git', 'log', f'{tag}..HEAD', '--pretty=format:%H|||%s|||%b']
            else:
                # Get commits since last tag
                try:
                    last_tag = subprocess.check_output(
                        ['git', 'describe', '--tags', '--abbrev=0'],
                        cwd=self.repo_path,
                        text=True
                    ).strip()
                    cmd = ['git', 'log', f'{last_tag}..HEAD', '--pretty=format:%H|||%s|||%b']
                except subprocess.CalledProcessError:
                    # No tags found, get all commits
                    cmd = ['git', 'log', '--pretty=format:%H|||%s|||%b']

            output = subprocess.check_output(cmd, cwd=self.repo_path, text=True)
            return [line for line in output.split('\n') if line.strip()]

        except subprocess.CalledProcessError as e:
            print(f"⚠️  Error getting commits: {e}")
            return []

    def parse_commit(self, commit_line: str) -> Optional[Commit]:
        """Parse a single commit line."""
        parts = commit_line.split('|||')
        if len(parts) < 2:
            return None

        commit_hash = parts[0]
        subject = parts[1]
        body = parts[2] if len(parts) > 2 else ""

        # Try to parse conventional commit
        match = self.COMMIT_PATTERN.match(subject)

        if match:
            commit_type = match.group('type')
            scope = match.group('scope')
            description = match.group('description')
        else:
            # Non-conventional commit - categorize as 'chore'
            commit_type = 'chore'
            scope = None
            description = subject

        # Check for breaking changes
        breaking = 'BREAKING CHANGE' in body or description.startswith('!')

        return Commit(
            hash=commit_hash[:7],
            type=commit_type,
            scope=scope,
            description=description,
            body=body,
            breaking=breaking,
            raw_message=subject
        )

    def categorize_commits(self, commits: List[Commit]) -> Dict[str, List[Commit]]:
        """Group commits by type."""
        categorized = defaultdict(list)

        for commit in commits:
            section = self.TYPE_SECTIONS.get(commit.type, 'Other')
            categorized[section].append(commit)

        return categorized

    def determine_version_bump(self, commits: List[Commit], current_version: str) -> str:
        """Determine next version using semver."""
        # Parse current version
        match = re.match(r'(\d+)\.(\d+)\.(\d+)', current_version)
        if not match:
            return "0.1.0"

        major, minor, patch = map(int, match.groups())

        # Check for breaking changes (major bump)
        if any(c.breaking for c in commits):
            return f"{major + 1}.0.0"

        # Check for features (minor bump)
        if any(c.type == 'feat' for c in commits):
            return f"{major}.{minor + 1}.0"

        # Otherwise patch bump
        return f"{major}.{minor}.{patch + 1}"


class ChangelogGenerator:
    """Generate changelog entries from commits."""

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        self.parser = ConventionalCommitParser(repo_path)

    def get_current_version(self) -> str:
        """Get current version from VERSION file or git tags."""
        version_file = self.repo_path / "VERSION"

        if version_file.exists():
            return version_file.read_text().strip()

        # Try to get from git tags
        try:
            tag = subprocess.check_output(
                ['git', 'describe', '--tags', '--abbrev=0'],
                cwd=self.repo_path,
                text=True
            ).strip()
            # Remove 'v' prefix if present
            return tag.lstrip('v')
        except subprocess.CalledProcessError:
            return "0.0.0"

    def generate_entry(self, version: str, commits: List[Commit]) -> str:
        """Generate a changelog entry for a version."""
        sections = self.parser.categorize_commits(commits)

        lines = []
        lines.append(f"## [{version}] - {datetime.now().strftime('%Y-%m-%d')}\n")

        # Breaking changes first
        breaking_commits = [c for c in commits if c.breaking]
        if breaking_commits:
            lines.append("### ⚠️ BREAKING CHANGES\n")
            for commit in breaking_commits:
                scope_str = f"**{commit.scope}:** " if commit.scope else ""
                lines.append(f"- {scope_str}{commit.description} ({commit.hash})\n")
            lines.append("\n")

        # Other sections
        section_order = ['Added', 'Changed', 'Fixed', 'Performance', 'Documentation', 'Tests', 'Build', 'CI/CD', 'Other']

        for section in section_order:
            if section in sections and sections[section]:
                lines.append(f"### {section}\n\n")
                for commit in sections[section]:
                    scope_str = f"**{commit.scope}:** " if commit.scope else ""
                    lines.append(f"- {scope_str}{commit.description} ({commit.hash})\n")
                lines.append("\n")

        lines.append("---\n\n")
        return ''.join(lines)

    def update_changelog(self, dry_run: bool = False):
        """Update CHANGELOG.md with new commits."""
        changelog_path = self.repo_path / "docs" / "CHANGELOG.md"

        # Get commits since last tag/version
        commit_lines = self.parser.get_commits_since_tag()

        if not commit_lines:
            print("ℹ️  No new commits since last release")
            return

        print(f"📊 Found {len(commit_lines)} commits since last release")

        # Parse commits
        commits = []
        for line in commit_lines:
            commit = self.parser.parse_commit(line)
            if commit:
                commits.append(commit)

        if not commits:
            print("ℹ️  No parseable commits found")
            return

        # Determine version
        current_version = self.get_current_version()
        next_version = self.parser.determine_version_bump(commits, current_version)

        print(f"📈 Version: {current_version} → {next_version}")
        print(f"   Breaking changes: {sum(1 for c in commits if c.breaking)}")
        print(f"   Features: {sum(1 for c in commits if c.type == 'feat')}")
        print(f"   Fixes: {sum(1 for c in commits if c.type == 'fix')}")

        # Generate new entry
        new_entry = self.generate_entry(next_version, commits)

        if dry_run:
            print("\n📝 Generated changelog entry (dry run):\n")
            print(new_entry)
            return

        # Read existing changelog
        if changelog_path.exists():
            existing_content = changelog_path.read_text()

            # Find where to insert (after the header, before first version)
            # Look for pattern like "## [version]" or "## Unreleased"
            insert_pattern = re.compile(r'^## \[', re.MULTILINE)
            match = insert_pattern.search(existing_content)

            if match:
                # Insert before first version entry
                insert_pos = match.start()
                new_content = (
                    existing_content[:insert_pos] +
                    new_entry +
                    existing_content[insert_pos:]
                )
            else:
                # Append to end
                new_content = existing_content + "\n" + new_entry
        else:
            # Create new changelog
            header = """# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

"""
            new_content = header + new_entry

        # Write updated changelog
        changelog_path.write_text(new_content)
        print(f"✅ Updated {changelog_path}")

        # Update VERSION file
        version_file = self.repo_path / "VERSION"
        version_file.write_text(next_version + '\n')
        print(f"✅ Updated VERSION: {next_version}")

        # Propagate to all version references (pyproject.toml, etc.) via the
        # version_manager helpers so the bump doesn't leave pyproject lagging
        # and trip the pre-commit version-mismatch check.
        try:
            import sys
            sys.path.insert(0, str(self.repo_path / "scripts" / "ops"))
            from version_manager import VERSION_REFERENCES, update_file_versions
            for ref_file, patterns in VERSION_REFERENCES:
                ref_path = self.repo_path / ref_file
                if ref_path.exists() and update_file_versions(ref_path, patterns, next_version):
                    print(f"✅ Synced {ref_file} → {next_version}")
        except ImportError:
            print("⚠️  version_manager unavailable; pyproject.toml not auto-synced")


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description='Update CHANGELOG.md from conventional commits')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be generated without writing')
    parser.add_argument('--since-tag', help='Generate changelog since specific tag')
    args = parser.parse_args()

    project_root = Path(__file__).parent.parent.parent

    print("🔍 Analyzing git commits...")
    generator = ChangelogGenerator(project_root)
    generator.update_changelog(dry_run=args.dry_run)

    print("\n✅ Done!")


if __name__ == "__main__":
    main()
