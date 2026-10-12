# Offline integrator shadow — 2026-10-12

The substepped candidate materially reduces numerical integration error in this
synthetic suite. It changes the frozen estimator's numerical outputs, although
none of the fixture's recorded categorical outputs changes. This supports the
repair in PR #2757 without authorizing its deployment or changing the preserved
study instrument.

## Exact comparison

- Preserved implementation: `6d742273434c6cc8a7cfa10ea4e29e969660e719`.
- Candidate: `106d45f48e28e00ba4ca35fcae7ab08b4fc2ed01` (PR #2757).
- The runner exports immutable Git source to temporary directories; the only
  allowed source difference in those exports is `governance_core/dynamics.py`.
- The preserved source must reproduce the unchanged golden fixture exactly,
  excluding its provenance envelope. Comparison uses its eight-decimal outputs.
- Both numerical workers must report identical parameters, scenarios and Radau
  reference trajectories; scenario identifiers must match and be unique.
- Recorded environment: Python 3.14.8, NumPy 2.4.0, SciPy 1.16.3.

This is a historical pinned comparison, not a comparison against whatever
happens to be deployed now. No production source, golden fixture, live input,
study outcome or runtime hook is changed or consumed by this analysis.

## Frozen estimator replay

All six synthetic fixture scenarios ran. There are **382 numerical differences**,
**zero categorical differences**, and **zero type/structure differences**.
The largest absolute numerical change is **0.00001002**, in `phi` at index 3
of `saturating_gap` (0.20959119 to 0.20960121). No recorded action, sub-action,
verdict, status or regime changes. These observations cover these fixtures only;
small changes near an untested decision boundary can still change a decision.
The state-roundtrip case restores behavioral state; it is not a process-restart
or deployment-persistence test.

## Boundary numerical suite

For each implementation: eight near-domain-face initial states, two integrators,
four external timesteps, and two frozen forcing configurations give **128
single-step cases** and **128 trajectories of 20 updates**. The 64 reference
trajectories are shared across integrator modes. The forcing configurations also
differ in drift, noise, complexity and entropy setpoint; this does not isolate
the effect of sensor coupling. An additional 16 pair witnesses measure the
original nearby-state example.

Reference integration uses SciPy Radau with relative tolerance `1e-10` and
absolute tolerance `1e-12`, followed by the same entropy floor once per external
update. It uses the same differential equations, independently of the RK4/Euler
integration method. It does not independently validate the equations or their
interpretation. All 1,280 reference steps per implementation succeeded and
stayed in the configured box at accepted solver nodes and 129 additional dense
output sample times. That is a sampled check, not a continuous-path enclosure.
No nonfinite outputs occurred.

Maximum absolute coordinate error across the 16 cases per row:

| Integrator | External dt | Preserved | Candidate |
|---|---:|---:|---:|
| RK4 | 0.10 | 0.0662798 | 0.00000813128 |
| RK4 | 0.25 | 0.200470 | 0.00000955592 |
| RK4 | 0.50 | 0.290787 | 0.00000916003 |
| RK4 | 1.00 | 0.415578 | 0.00000713328 |
| Euler | 0.10 | 0.182348 | 0.00366896 |
| Euler | 0.25 | 0.487522 | 0.00375831 |
| Euler | 0.50 | 0.988468 | 0.00357221 |
| Euler | 1.00 | 1.63848 | 0.00305854 |

For this suite, the maximum across all 20 updates equals the single-step maximum
in every row. This reports the observed maxima; no accuracy threshold was chosen
to turn them into a policy verdict.

For the original sensor-off pair `(0.5, 0.999, 0.3, 0)` versus an I-coordinate
increment of `1e-7`, using metric diagonal `(0.1, 0.2, 1, 0.08)`, the RK4
final/initial distance ratio falls from **4.60527 to 0.0481692** at dt 0.1,
and from **235.609 to 0.0206964** at dt 1. These are examples, not a proof of
global contraction of the nonlinear discrete map.

## Reproduce

Use a development environment with the repository's full dependencies plus
SciPy (the versions above identify this run), and Git objects for both pinned
commits. From the repository root:

```sh
python3 scripts/analysis/integrator_shadow/run.py --output integrator-shadow.json
python3 -m pytest tests/test_integrator_shadow.py -q --no-cov
```

`--fixtures-only` omits the numerical suite. `--baseline-ref` and
`--candidate-ref` allow another explicitly pinned comparison, subject to the
same source-difference and baseline-golden checks. The output includes resolved
commit IDs, export and source hashes, full fixture differences, per-scenario
outputs, reference eligibility, numerical errors, and compact summaries. A failed
worker or comparison-integrity check aborts instead of writing a success report.

Workers inherit no service configuration or credentials and run from source
exports with scratch storage redirected there. Python audit guards reject
network connections, child processes and writes outside the export. Exactly
three import-time Git metadata queries receive an emulated non-repository
failure; no Git child process runs in a worker. These are cooperative Python
safeguards, not an operating-system sandbox against hostile or native code.
The parent uses Git to export source and writes only the requested report.

## Delivery boundary

The instrument-preservation rule in
[the core-boundary proposal](../proposals/active/eisv-core-boundary-v0.md#9-scientific-validation)
remains in force. PR #2757 stays draft pending release of that preservation hold
or an explicit operator waiver. This offline report does not waive it, add a
live shadow, refresh the golden fixture, establish agent outcome quality, or
show how often these boundary states occur in production.
