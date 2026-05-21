# Makefile for governance-mcp-v1
.PHONY: help test test-quick test-smoke version version-check version-bump restart logs serve docs clean

help: ## Show this help message
	@echo "Available commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Version ──────────────────────────────────────────────

version: ## Show current version
	@cat VERSION

version-check: ## Check all version references are in sync
	@python3 scripts/ops/version_manager.py --check

version-bump: ## Bump version (usage: make version-bump PART=patch)
	@python3 scripts/ops/version_manager.py --bump $(PART)
	@python3 scripts/ops/version_manager.py --update
	@echo "Don't forget to commit and restart the server"

# ── Testing ──────────────────────────────────────────────

test: ## Run full test suite with coverage
	@python3 -m pytest \
		--cov=src --cov=agents/sdk/src/unitares_sdk --cov=agents \
		--cov-report=term-missing --cov-fail-under=25

test-quick: ## Run tests without coverage
	@python3 -m pytest -o addopts= -q

test-smoke: ## Run fast critical-path tests
	@python3 -m pytest -o addopts= -q --maxfail=1 \
		tests/test_version_sync.py \
		tests/test_mcp_server_std.py::TestLoadVersion \
		tests/test_mcp_server_std.py::TestAutoArchiveOrphanAgents

# ── Server ───────────────────────────────────────────────

serve: ## Start server locally (foreground)
	@python3 src/mcp_server.py --port 8767

restart: ## Restart governance-mcp launchd service
	@launchctl unload ~/Library/LaunchAgents/com.unitares.governance-mcp.plist
	@launchctl load ~/Library/LaunchAgents/com.unitares.governance-mcp.plist
	@echo "Restarted. Checking health..."
	@sleep 2
	@curl -s http://localhost:8767/health | python3 -m json.tool

logs: ## Tail server logs
	@tail -f data/logs/mcp_server.log

logs-err: ## Tail server error logs
	@tail -f data/logs/mcp_server_error.log

# ── Documentation ────────────────────────────────────────

docs: ## Generate tool documentation from @mcp_tool decorators
	@python3 scripts/generate_tool_docs.py

validate: ## Run CI validation checks locally
	@python3 scripts/dev/check_ci_python_version_sync.py
	@python3 scripts/diagnostics/check_ci_python_matrix_sync.py
	@python3 scripts/dev/update_docs_tool_count.py --check
	@python3 scripts/ops/version_manager.py --check
	@echo "All checks passed"

# ── Hooks ────────────────────────────────────────────────

install-hooks: ## Install git pre-commit hooks
	@chmod +x scripts/git-hooks/pre-commit-combined
	@ln -sf ../../scripts/git-hooks/pre-commit-combined .git/hooks/pre-commit
	@echo "Pre-commit hook installed"

uninstall-hooks: ## Remove git pre-commit hooks
	@rm -f .git/hooks/pre-commit
	@echo "Pre-commit hook removed"

# ── Demo ─────────────────────────────────────────────────

demo: ## Run a 60-second governance trajectory against a live server on :8767
	@python3 scripts/demo/quick_demo.py

# ── Cleanup ──────────────────────────────────────────────

clean: ## Remove generated artifacts (htmlcov, coverage, pycache)
	@rm -rf htmlcov coverage.xml .coverage
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "Cleaned"
