import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_outcome_and_basin_claims_keep_their_corrected_statuses():
    contract = _read("docs/ontology/eisv-proprioception-contract.md")

    assert "WITHDRAWN FOR TARGET INFERENCE" in contract
    assert "PATH BOUND for the direct same-check-in" in contract
    assert "recursive dynamic counterfactual is UNIDENTIFIED" in contract
    assert '"adequate power"' in contract
    assert "interpretations are withdrawn" in contract


def test_v7_mechanism_note_does_not_turn_a_mixture_weight_into_an_effect():
    note = _read(
        "docs/ontology/v7-observer-self-loop-identification-boundary.md"
    )
    normalized = " ".join(note.split())

    assert "unidentified by" in note
    assert "arithmetic summand of the regressand" not in note
    assert "not a regression coefficient" in normalized
    assert (
        "do not call the unidentified association a structural negative result"
        in normalized
    )


def test_containment_note_defines_non_refutation_statuses():
    note = _read(
        "docs/ontology/falsification-inference-containment-2026-08-22.md"
    )

    for status in (
        "BENCHMARK FAIL",
        "IMPLEMENTATION MISMATCH",
        "PATH BOUND",
        "DEPLOYMENT SNAPSHOT",
        "INCONCLUSIVE",
        "UNIDENTIFIED",
        "WITHDRAWN",
    ):
        assert f"`{status}`" in note


def test_distributional_probe_is_not_greenlit_without_retiring_the_capability():
    probe = _read(
        "docs/proposals/resolved/eisv-distributional-signal-probe-v0.md"
    )
    normalized = " ".join(probe.split())

    assert "stronger KILL inference withdrawn" in normalized
    assert (
        "does not identify the observation-versus-representation bottleneck"
        in normalized
    )
    assert "capability is not killed" in normalized


def test_reference_benchmark_does_not_refute_the_warmed_deployed_ema():
    contract = _read("docs/ontology/eisv-proprioception-contract.md")
    result = _read("docs/proposals/eisv-individuality-v2-result.md")
    normalized = " ".join((contract + "\n" + result).split())

    assert "BENCHMARK FAIL for the cold-start reconstruction" in normalized
    assert "UNIDENTIFIED for the warmed deployed EMA" in normalized
    assert (
        "The per-agent reference does useful work** → **REFUTED as deployed"
        not in normalized
    )


def test_propagation_surfaces_do_not_restore_withdrawn_inferences():
    index = _read("docs/EVALUATION_INDEX.md")
    gate_note = _read("docs/proposals/2026-06-24-wave-3-gate-framing.md")

    assert "guide self-loop cannot flip the basin as deployed" not in index
    assert "recursive guide-loop counterfactual is `UNIDENTIFIED`" in index
    assert "eisv-probe KILL already recorded" not in gate_note
    assert "stronger `KILL` inference was withdrawn" in gate_note


def test_tested_claim_headings_do_not_label_engineering_findings_refuted():
    contract = _read("docs/ontology/eisv-proprioception-contract.md")
    ledger = contract.split("## Tested claims — ledger", 1)[1].split(
        "## Preferred wording", 1
    )[0]
    entries = re.findall(
        r"(?ms)^\*\*(\d+)\.(.*?)(?=^\*\*\d+\.|\Z)",
        ledger,
    )

    assert len(entries) == 45
    for number, entry in entries:
        first_paragraph = entry.split("\n\n", 1)[0]
        match = re.search(r"[→—]\s*\*\*(.*?)\*\*", first_paragraph, re.S)
        assert match is not None, f"row {number} has no machine-readable status"
        assert "REFUTED" not in match.group(1), (
            f"row {number} still promotes a non-scientific result to REFUTED"
        )


def test_system_audit_discloses_scope_power_and_repeated_reads():
    audit = _read(
        "docs/ontology/falsification-design-system-audit-2026-08-23.md"
    )
    normalized = " ".join(audit.split())

    assert "36 of the 45 numbered claim headings" in normalized
    assert "about 3% power" in normalized
    assert "51 | 42 | 9" in audit
    assert "52 | 43 | 9" in audit
    for check in (
        "Target",
        "Counterfactual",
        "Unit",
        "Support and power",
        "Decision rule",
        "Protocol",
    ):
        assert f"| {check} |" in audit
    assert "does not automatically earn `REFUTED`" in normalized


def test_reviewer_path_does_not_instruct_an_interim_live_matrix_read():
    guide = _read("docs/REVIEWER_GUIDE.md")

    assert "python3 scripts/analysis/eisv_ablation_matrix.py \\" not in guide
    assert "Running that command again still exposes outcome discrimination" in guide
    assert "only the support inventory is permitted here" in guide
