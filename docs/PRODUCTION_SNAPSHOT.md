# Production snapshot

Frozen public snapshot from June 16, 2026 (single-operator — the author's own
traffic). Headline: **3.7M+ governance events processed · ≈714K in the last 7
days**. Running continuously since November 2025 and dogfooded — the agents
building UNITARES run under it. The falsifiability checks run on a fresh clone;
the live deployment metrics below need governance-DB access to reproduce — see
the [Reviewer Guide](REVIEWER_GUIDE.md#falsifiability-grade-eisv-yourself-dont-trust-this-doc).

## Full metrics table

| Metric | Value |
|--------|-------|
| Agents onboarded | 3,777 total process-instances — overwhelmingly ephemeral CLI sessions from one operator's workstation plus a handful of long-running resident agents (launchd crons) |
| Distinct event-emitting identities (last 21 days) | 510; mostly ephemeral local CLI sessions (lower than earlier snapshots as identity-consolidation work cut phantom per-session identities) |
| Unique agents active (last 7 days) | 369 distinct event emitters |
| Governance events processed | 3,748,000+ (≈714K in the last 7 days) |
| Knowledge graph discoveries | 1,054 |
| V operating range | Active agents often within [-0.1, 0.1] |
| Tests | 8,500+ collected · smoke/pre-push subset plus 75% min coverage gate |

*What these numbers show:* the pipeline holds up under sustained volume. *What
they don't show:* product-market traction. External adoption is the open question.

## Dashboard views

<p align="center">
  <img src="assets/dashboard-pulse.png" width="80%" alt="Pulse — live event feed and EISV time series"/>
</p>
<p align="center"><em>Pulse — live event feed, drift indicators, and EISV time series charts</em></p>

<p align="center">
  <img src="assets/dashboard-agents.png" width="80%" alt="Agents and Discoveries panels"/>
</p>
<p align="center"><em>Agents (sorted by recency, with trust tiers) and Discoveries (filterable by type and time range)</em></p>

<p align="center">
  <img src="assets/dashboard-dialectic.png" width="80%" alt="Dialectic sessions — recovery and review history"/>
</p>
<p align="center"><em>Peer-review sessions — failed, resolved, and active recovery sessions with message counts</em></p>

<p align="center">
  <img src="assets/dashboard-activity.png" width="80%" alt="Activity timeline — check-ins, verdicts, discoveries"/>
</p>
<p align="center"><em>Activity timeline — filterable event log across all agents</em></p>
