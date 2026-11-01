# UNITARES repository artifacts

*Accountability infrastructure for long-running AI agents.*

UNITARES is self-hosted accountability infrastructure for operators running
multiple AI agents. Its single-operator federation kernel connects independent
runtimes to one operator-controlled server over MCP or HTTP, where they share a
durable record while keeping their own models, tools, and runtimes. They
interoperate with each other over their own transports or A2A; UNITARES is the
record behind them, not the transport between them.

Product overview, architecture, and installation guides live at
**[cirwel.org](https://cirwel.org)** ([Architecture](https://cirwel.org/ecosystem) ·
[Build](https://cirwel.org/build) · [Research](https://cirwel.org/research)) and in
the [repository README](https://github.com/cirwel/unitares#readme).

## Generated GitHub Pages artifacts

This GitHub Pages directory (`cirwel.github.io/unitares/`) hosts the read-only
HTML artifacts built directly from canonical files in
[`cirwel/unitares`](https://github.com/cirwel/unitares) on each push to `master`:

| Artifact | Source in repository | What it shows |
|---|---|---|
| **[Incident walkthrough (`demo.html`)](demo.html)** | [`docs/public-site/demo.md`](https://github.com/cirwel/unitares/blob/master/docs/public-site/demo.md) + [`tests/accountability_journey/incident-v0.json`](https://github.com/cirwel/unitares/blob/master/tests/accountability_journey/incident-v0.json) | Four-step browser walkthrough of one recorded incident from claim to challenge and correction, with no server install required. |
| **[Public glossary (`glossary.html`)](glossary.html)** | [`docs/ontology/glossary.md`](https://github.com/cirwel/unitares/blob/master/docs/ontology/glossary.md) | Question-keyed vocabulary glossary, homonym disambiguation, and drift corrections. |
| **[Glossary viewer (`glossary-viewer.html`)](glossary-viewer.html)** | [`docs/ontology/glossary.md`](https://github.com/cirwel/unitares/blob/master/docs/ontology/glossary.md) | Interactive search and filter view across homonyms, single-sense terms, the cross-register Rosetta map, and open gaps. |
| **[Drift audit (`drift-audit.html`)](drift-audit.html)** | [`docs/ontology/`](https://github.com/cirwel/unitares/tree/master/docs/ontology) | Latest dated vocabulary drift audit report. |

## Published releases and reference links

- **Current public releases:** [server v3.6.0](https://github.com/cirwel/unitares/releases/tag/v3.6.0) · [Python SDK 0.6.1](https://pypi.org/project/unitares-sdk/0.6.1/) (`python -m pip install unitares-sdk==0.6.1`) · [container `ghcr.io/cirwel/unitares:v3.6.0`](https://github.com/cirwel/unitares/pkgs/container/unitares) · [Compatibility matrix](https://github.com/cirwel/unitares/blob/master/docs/COMPATIBILITY.md) · Apache-2.0
- **Repository documentation:** [User Manual](https://github.com/cirwel/unitares/blob/master/docs/manual/README.md) · [Evidence and limits](https://github.com/cirwel/unitares/blob/master/docs/EVIDENCE_AND_LIMITS.md) · [Reviewer Guide](https://github.com/cirwel/unitares/blob/master/docs/REVIEWER_GUIDE.md) · [Scope and threat model](https://github.com/cirwel/unitares/blob/master/docs/SCOPE_AND_THREAT_MODEL.md) · [GitHub Discussions](https://github.com/cirwel/unitares/discussions)
