"""Spawnable entry point so the agent-orchestrator can run the reviewer as
`python3 -m agents.dialectic_reviewer` (cmd/args/env spec, POST /v1/agents).

All input arrives via env (the orchestrator spawn payload), parsed by
``Thesis.from_env`` + the ``UNITARES_GOVERNANCE_URL`` / ``UNITARES_PARENT_AGENT_ID``
vars ``reviewer.main`` already reads. Keep this a thin shim — the testable logic
lives in ``reviewer``.
"""
from .reviewer import main

raise SystemExit(main())
