# Accountability journey rehearsal — v0 result

**Evidence class:** mechanism validation only.
**Evaluation stage:** retrieval control (stage 2 of 2).
**Headline comparison:** not evaluated.
**Frozen preregistration:** unaffected.

Incident: A writer reports a successful service reload, deterministic evidence contradicts the claim, a reviewer challenges it, the writer corrects it, and a fresh successor reconstructs the chain.

Both arms carry the same oracle fact, edge, and attribution manifest. This prevents a favorable result produced by withholding information from the Git plus structured-handoff baseline.

| Arm | Reconstruction accuracy | Attribution precision | Attribution coverage | Missing-edge rate | Records inspected | Fixture bytes |
|---|---:|---:|---:|---:|---:|---:|
| unitares | 1.000 | 1.000 | 1.000 | 0.000 | 5 | 1404 |
| structured handoff | 1.000 | 1.000 | 1.000 | 0.000 | 4 | 1323 |

## Observed failures

None in the deterministic fixture.

## Two-stage evaluation

1. **Capture quality — not run.** Let UNITARES and the operator's ordinary Git/handoff workflow record an incident naturally, then score both against an external oracle. Missing facts are findings, not a reason to equalize the inputs after the fact.
2. **Retrieval control — this rehearsal.** Give both paths equivalent facts and test reconstruction. This isolates retrieval from capture.

## Interpretation and limits

The result establishes that the scenario, scorer, and both reconstruction paths preserve the declared incident facts. It does not establish that UNITARES improves outcomes, reduces reconstruction time, or outperforms a structured handoff in real work.

The next step is stage 1 against actual retained UNITARES records and naturally produced Git/handoff artifacts. Only after capture coverage is measured should stage 2 compare retrieval effort. The frozen multi-scenario evaluation remains unchanged.

Oracle manifest SHA-256: `78fd4c0c161373dfd548022b63ae66a7b6e4a4e2c6ef15de10b74022f91b331e`

Reproduce from the repository root:

```bash
make accountability-journey
python3 -m pytest tests/test_accountability_journey.py -q
```
