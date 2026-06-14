"""
Code-Doc Drift Detection Tests

Validates that skill documentation claims match actual code behavior.
Each assertion maps a documented claim to a testable validation function.
No DB or server needed — runs fast (<1s) as part of the standard test suite.

To add new assertions:
1. Identify a specific claim in a SKILL.md file
2. Write a validation function that checks the code
3. Add it to SKILL_ASSERTIONS
"""

import re
import importlib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Imports from the codebase under test
# ---------------------------------------------------------------------------
from config.governance_config import GovernanceConfig
from src.auto_ground_truth import (
    evaluate_test_outcome,
    evaluate_command_outcome,
    evaluate_file_operation,
    evaluate_lint_outcome,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent
SKILLS_DIR = PROJECT_ROOT / "skills"


def _read_skill(name: str) -> str:
    """Read a skill file's content."""
    path = SKILLS_DIR / name / "SKILL.md"
    if not path.exists():
        pytest.skip(f"Skill file not found: {path}")
    return path.read_text()


# ---------------------------------------------------------------------------
# Assertion registry
# ---------------------------------------------------------------------------

class TestGovernanceFundamentalsClaims:
    """Validate claims in governance-fundamentals/SKILL.md."""

    def test_coherence_critical_threshold_not_hardcoded(self):
        """Skill should reference get_governance_metrics(), not hardcode the threshold value."""
        content = _read_skill("governance-fundamentals")
        assert "get_governance_metrics()" in content, (
            "Coherence threshold should point to get_governance_metrics() tool, not hardcode a number"
        )
        # The actual hardcoded "threshold at 0.40" should not appear
        assert "threshold at 0.40" not in content, (
            "Found hardcoded threshold value. Reference the tool's thresholds field instead."
        )

    def test_skills_do_not_reference_config_as_agent_lookup(self):
        """Skills should not tell agents to look at governance_config.py — it's an internal detail."""
        if not SKILLS_DIR.exists():
            pytest.skip("Skills directory not found")

        violations = []
        for skill_dir in sorted(SKILLS_DIR.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            content = skill_file.read_text()
            # Check for patterns that direct agents to read config files
            if re.search(r"defined in.*governance_config\.py", content):
                violations.append(
                    f"{skill_dir.name}: tells agents to look at governance_config.py — "
                    f"use get_governance_metrics() thresholds field instead"
                )
            if re.search(r"governance_config\.py\s*→", content):
                violations.append(
                    f"{skill_dir.name}: references governance_config.py with arrow notation — "
                    f"use get_governance_metrics() thresholds field instead"
                )

        assert not violations, "Skills reference config as agent-facing lookup:\n" + "\n".join(violations)

    def test_coherence_full_range_documented(self):
        """Skill claims coherence full range is [0, 1]."""
        content = _read_skill("governance-fundamentals")
        assert "Full range is [0, 1]" in content

    def test_ground_truth_from_objective_signals(self):
        """Skill claims ground truth comes from objective signals, not just humans."""
        content = _read_skill("governance-fundamentals")
        assert "objective signals" in content.lower() or "auto_ground_truth" in content

    def test_auto_ground_truth_evaluators_exist(self):
        """The objective evaluators referenced in the skill actually exist."""
        assert callable(evaluate_test_outcome)
        assert callable(evaluate_command_outcome)
        assert callable(evaluate_file_operation)
        assert callable(evaluate_lint_outcome)

    def test_eisv_ranges_documented_correctly(self):
        """EISV dimension ranges match config."""
        content = _read_skill("governance-fundamentals")
        # E and I: [0, 1]
        assert "| **E** (Energy) | [0, 1]" in content
        assert "| **I** (Information Integrity) | [0, 1]" in content
        # S: [0, 1]
        assert "| **S** (Entropy) | [0, 1]" in content
        # V: [-1, 1]
        assert "| **V** (Valence) | [-1, 1]" in content

    def test_target_coherence_value(self):
        """Config TARGET_COHERENCE should be 0.50."""
        assert GovernanceConfig.TARGET_COHERENCE == 0.50


class TestDialecticReasoningClaims:
    """Validate claims in dialectic-reasoning/SKILL.md."""

    def test_no_narrow_coherence_range_claim(self):
        """The old misleading '(range is ~0.45-0.55)' without full-range context should not exist."""
        content = _read_skill("dialectic-reasoning")
        # The narrow range alone (without mentioning full range) should not appear
        narrow_only = re.search(r"\(range is ~0\.45-0\.55\)", content)
        assert narrow_only is None, (
            "Found '(range is ~0.45-0.55)' without full-range context. "
            "Should say 'typical governed range ~0.45-0.55, full range [0, 1]'."
        )

    def test_coherence_uses_tool_reference(self):
        """Dialectic skill should reference get_governance_metrics() for actual values."""
        content = _read_skill("dialectic-reasoning")
        assert "get_governance_metrics()" in content, (
            "Dialectic skill should point agents to get_governance_metrics() for actual range/values"
        )


class TestCoreHandlerClaims:
    """Validate that the MCP handler output matches documented ranges."""

    def test_coherence_range_in_lite_metrics(self):
        """core.py lite_metrics should report coherence range as [0, 1], not [0.45, 0.55]."""
        core_path = PROJECT_ROOT / "src" / "mcp_handlers" / "core.py"
        if not core_path.exists():
            pytest.skip("core.py not found")
        content = core_path.read_text()
        # The old misleading range should not be the reported range
        assert "'range': '[0.45, 0.55]'" not in content, (
            "core.py still reports coherence range as [0.45, 0.55]. "
            "Should be [0, 1] with equilibrium note."
        )


class TestNoHardcodedConfigValues:
    """Detect config values hardcoded in skills that should be pointers."""

    # Map: config attr -> regex that would indicate hardcoding in a skill
    # Only include values that could realistically change.
    CONFIG_VALUES_TO_CHECK = [
        ("COHERENCE_CRITICAL_THRESHOLD", r"threshold\s+(?:at|of|is)\s+0\.\d+"),
        ("TARGET_COHERENCE", r"target\s+coherence\s+(?:at|of|is)\s+0\.\d+"),
        ("RISK_THRESHOLD_MEDIUM", r"risk\s+threshold\s+(?:at|of|is)\s+0\.\d+"),
    ]

    def test_no_hardcoded_thresholds_in_skills(self):
        """Skills should not hardcode config values that can change."""
        if not SKILLS_DIR.exists():
            pytest.skip("Skills directory not found")

        violations = []
        for skill_dir in sorted(SKILLS_DIR.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            content = skill_file.read_text()
            for config_attr, pattern in self.CONFIG_VALUES_TO_CHECK:
                match = re.search(pattern, content, re.IGNORECASE)
                if match:
                    violations.append(
                        f"{skill_dir.name}: hardcodes {config_attr} as '{match.group()}' "
                        f"— use get_governance_metrics() thresholds field instead"
                    )

        assert not violations, "Hardcoded config values in skills:\n" + "\n".join(violations)

    def test_dialectic_skill_uses_tool_not_hardcoded_range(self):
        """Dialectic skill should point agents to get_governance_metrics(), not state ranges."""
        content = _read_skill("dialectic-reasoning")
        # Should not state a specific numeric coherence range as fact
        hardcoded_range = re.search(r"range is[^.]*0\.\d+\s*-\s*0\.\d+", content)
        assert hardcoded_range is None, (
            f"Found hardcoded range '{hardcoded_range.group()}'. "
            f"Point to get_governance_metrics() instead."
        )


class TestSkillFreshnessMetadata:
    """Verify all skills have freshness metadata."""

    def test_all_skills_have_freshness_fields(self):
        """Every SKILL.md should have last_verified and freshness_days."""
        if not SKILLS_DIR.exists():
            pytest.skip("Skills directory not found")

        missing = []
        for skill_dir in sorted(SKILLS_DIR.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            content = skill_file.read_text()
            has_verified = "last_verified:" in content
            has_freshness = "freshness_days:" in content
            if not (has_verified and has_freshness):
                missing.append(skill_dir.name)

        assert not missing, f"Skills missing freshness metadata: {missing}"
