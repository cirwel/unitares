#!/usr/bin/env python3
"""
Test Coverage Audit Script

Measures and reports test coverage for the governance system.
"""

import subprocess
import sys
from pathlib import Path

def run_coverage_check():
    """Run pytest with coverage and report results."""
    project_root = Path(__file__).parent.parent.parent
    
    print("=" * 70)
    print("Test Coverage Audit")
    print("=" * 70)
    print()
    
    # Check if pytest-cov is available
    try:
        import pytest_cov  # noqa: F401 — availability probe
    except ImportError:
        print("⚠️  pytest-cov not installed. Install with: pip install pytest-cov")
        print("Running basic test discovery instead...")
        print()
        
        # Basic test discovery
        result = subprocess.run(
            ["python3", "-m", "pytest", "tests/", "--co", "-q"],
            cwd=project_root,
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            print("✅ Tests discovered successfully")
            print(result.stdout)
        else:
            print("❌ Test discovery failed")
            print(result.stderr)
        
        return
    
    # Run coverage
    print("Running test coverage analysis...")
    print()
    
    result = subprocess.run(
        [
            "python3", "-m", "pytest",
            "tests/",
            "--cov=src",
            "--cov=governance_core",
            "--cov=config",
            "--cov-report=term-missing",
            "--cov-report=html:htmlcov",
            "-v"
        ],
        cwd=project_root,
        capture_output=True,
        text=True
    )
    
    print(result.stdout)
    
    if result.stderr:
        print("Warnings/Errors:")
        print(result.stderr)
    
    print()
    print("=" * 70)
    print("Coverage report generated in htmlcov/index.html")
    print("=" * 70)
    
    if result.returncode == 0:
        print("✅ Coverage check completed successfully")
    else:
        print("⚠️  Some tests failed - check output above")
        sys.exit(1)


if __name__ == "__main__":
    run_coverage_check()

