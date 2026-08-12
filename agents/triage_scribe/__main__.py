"""Spawn entrypoint: python3 -m agents.triage_scribe

Mirrors agents/dialectic_reviewer/__main__.py so the orchestrator can spawn this
resident with the same spec shape (cmd=python3, args=["-m", "agents.triage_scribe"]).
"""
from agents.triage_scribe.scribe import main

raise SystemExit(main())
