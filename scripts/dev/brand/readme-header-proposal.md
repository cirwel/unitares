<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/unitares-lockup-dark.svg">
  <img src="docs/assets/unitares-lockup.svg" width="420" alt="UNITARES">
</picture>

### A federation kernel for accountable AI agents.

Identity, claims and evidence, review, outcomes, and reconstruction across agent runtimes.

</div>

UNITARES is a self-hosted MCP server that gives agents a shared, attributed
record while they keep their own reasoning loops and tools. A new process can
recover durable claims earlier processes stored, inspect the retained evidence
and disagreement, and continue the work with its own identity.

Here, **federation kernel** means many independent agent runtimes and harnesses
sharing one operator-controlled server and authority domain. It does not promise
autonomous cross-server replication or a new agent runtime. The deployed building blocks are process identities, check-ins,
attributed findings, structured reviews, outcome events, and history retrieval.
Reconstruction uses those records; its fidelity and benefit over Git plus a
structured handoff still need comparative evaluation.

**Status:** v2.22.0. Running continuously since November 2025.

<div align="center">

[![Tests](https://github.com/cirwel/unitares/actions/workflows/tests.yml/badge.svg)](https://github.com/cirwel/unitares/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.12+-5C544A?style=flat-square&labelColor=1A1612)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache_2.0-5C544A?style=flat-square&labelColor=1A1612)](LICENSE)
[![DOI](https://img.shields.io/badge/DOI-10.5281%2Fzenodo.19647159-7A1F1F?style=flat-square&labelColor=1A1612)](https://doi.org/10.5281/zenodo.19647159)

[Quickstart](#quickstart) · [Evidence and limits](#evidence-and-limits) · [Docs](docs/README.md) · [Reviewer Guide](docs/REVIEWER_GUIDE.md)

</div>

---
