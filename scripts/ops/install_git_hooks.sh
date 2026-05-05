#!/bin/bash
# Install git hooks for unitares
# Override project root via UNITARES_ROOT env var; otherwise auto-derived.

PROJECT_DIR="${UNITARES_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$PROJECT_DIR" || exit 1

echo "🪝 Installing Git Hooks"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Check if .git directory exists
if [ ! -d ".git" ]; then
    echo "❌ Error: Not a git repository"
    echo "   Run 'git init' first"
    exit 1
fi

# Create hooks directory if it doesn't exist
mkdir -p .git/hooks

# Install pre-commit hook
if [ -f ".git/hooks/pre-commit" ]; then
    echo "⚠️  Existing pre-commit hook found"
    read -p "Backup and replace? [y/N] " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        mv .git/hooks/pre-commit ".git/hooks/pre-commit.backup.$(date +%Y%m%d_%H%M%S)"
        echo "   ✅ Backed up existing hook"
    else
        echo "   ❌ Installation cancelled"
        exit 1
    fi
fi

# Copy and make executable
cp scripts/git-hooks/pre-commit-combined .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit

echo "✅ Pre-commit hook installed"
echo ""
echo "Hook functionality:"
echo "  • Warns when new scripts are added to scripts/"
echo "  • Checks markdown placement heuristics"
echo "  • Validates markdown formatting (warning-only)"
echo "  • Auto-updates generated tool docs when handlers change"
echo ""
echo "To test:"
echo "  1. Create a test script: touch scripts/test_script.sh"
echo "  2. Try to commit it: git add scripts/test_script.sh && git commit -m 'test'"
echo "  3. Hook should warn and ask for confirmation"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
