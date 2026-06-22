"""Trust-contract §7 provenance linting (mechanical, not judgment-based).

`docs/trust-contract.md` §1 states the core guarantee:

> Every value UNITARES emits carries provenance: **measured**, **derived**,
> **prior/default**, or **unknown**. The system never presents a prior with
> the confidence of an observation.

§7 calls for *"a response-schema validator that rejects any emitted
numeric/label field lacking a provenance class. Mechanical, not
judgment-based."* — and notes that §6 violations 1–3 *"would have been caught
at design time by a single test: 'no endpoint emits a qualitative label when
history_size == 0.'"*

This module is that validator for the governance-metrics read surface. It
encodes three mechanical rule families, each guarding a violation class the
trust contract already paid to fix (PR #605, #607, #608) but that nothing
otherwise pins:

A. **Labeled-field presence** (§2). A field that ships with an explicit
   provenance mechanism must keep it. `primary_eisv` must have a non-empty
   sibling `primary_eisv_source`; `saturation_diagnostics` must carry an
   inline `source`. Drop the label and the value reads as a measurement it is
   not — §6 rows 3 and the saturation re-probe finding.

B. **Ignorance honesty** (§3.2 / §3.6). When the response is in a first-class
   ignorance state (uninitialized / unbound / zero history), every assessment
   field present must be `null` or itself ignorance-labeled — never a confident
   qualitative label derived from the seed vector. This is the generalized form
   of the single test §7 names; it subsumes §6 rows 1–3.

C. **Scope honesty** (§3.3). Fleet-level calibration numbers appearing in an
   agent-scoped response must self-identify with a `scope` key — §6 row 4.

The registry below is deliberately the subset of fields that *already* have a
provenance mechanism in the live response, so the linter guards regressions of
shipped fixes rather than asserting a shape that was never built. Extending it
to a new field means wiring that field's provenance first, then registering it
here — never the reverse (that would re-introduce the very "appears more alive
than it is" gap this guards against).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


# First-class ignorance tokens (§3.6). A value or a value's `status`/`value`
# entry containing one of these is honest, not a confident claim. Matched as a
# case-insensitive substring so "⚪ uninitialized", "pending (first check-in
# required)", and "uninitialized | no observations yet" all qualify.
IGNORANCE_TOKENS: tuple[str, ...] = (
    "uninitialized",
    "unbound",
    "pending",
    "insufficient_data",
    "no observations",
    "unknown",
    "warmup",
)

# Assessment fields: qualitative/derived labels that must not appear as
# confident claims in an ignorance state (rule B).
#
# `guidance` is deliberately excluded: in an ignorance state it is a legitimate
# call-to-action ("Submit one check-in to activate governance") that the
# contract §3.2 explicitly allows, and a mechanical substring check cannot
# separate that from the `"Pattern may be shifting"` violation (§6 row 2)
# without false-positiving the honest case. Guidance honesty is pinned by
# test_zero_observation_honesty instead.
ASSESSMENT_FIELDS: tuple[str, ...] = (
    "summary",
    "state",
    "stability",
    "regime",
    "phi",
    "trajectory",
    "mode",
    "basin",
)


@dataclass(frozen=True)
class LabeledField:
    """A field that must carry an explicit provenance label (rule A)."""

    name: str
    # A sibling key (e.g. ``primary_eisv_source``) that must be present and
    # non-empty when ``name`` is present and non-null.
    sibling_source: str | None = None
    # A key that must exist and be non-empty *inside* the field's own dict
    # (e.g. ``saturation_diagnostics["source"]``).
    inline_source: str | None = None


# Governance-metrics read surface (`get_governance_metrics`). Each entry maps a
# shipping provenance mechanism; see module docstring for why the registry is a
# subset, not "every numeric field".
GOVERNANCE_METRICS_LABELED: tuple[LabeledField, ...] = (
    LabeledField("primary_eisv", sibling_source="primary_eisv_source"),
    LabeledField("saturation_diagnostics", inline_source="source"),
)

# Fleet-scoped sub-blocks that must self-identify (rule C): path is
# (container_field, sub_block) and the sub_block must carry a ``scope`` key.
GOVERNANCE_METRICS_FLEET_SCOPED: tuple[tuple[str, str], ...] = (
    ("calibration_feedback", "confidence"),
)


@dataclass
class ProvenanceViolation:
    """A single mechanical finding. ``rule`` is one of A/B/C above."""

    rule: str
    field: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        return f"[{self.rule}] {self.field}: {self.detail}"


@dataclass
class LintSpec:
    """Declarative spec applied mechanically by :func:`lint_response`."""

    labeled: Sequence[LabeledField] = ()
    assessment_fields: Sequence[str] = field(default_factory=lambda: ASSESSMENT_FIELDS)
    fleet_scoped: Sequence[tuple[str, str]] = ()


GOVERNANCE_METRICS_SPEC = LintSpec(
    labeled=GOVERNANCE_METRICS_LABELED,
    assessment_fields=ASSESSMENT_FIELDS,
    fleet_scoped=GOVERNANCE_METRICS_FLEET_SCOPED,
)


def _is_ignorance_text(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    low = value.lower()
    return any(tok in low for tok in IGNORANCE_TOKENS)


def _carries_ignorance(value: Any) -> bool:
    """True when ``value`` is null or honestly labels itself as not-yet-known.

    Handles the three shapes the live response uses: a bare ignorance string
    (``summary``), a glossary wrapper ``{"value": "uninitialized", ...}``
    (``verdict``), and a status dict ``{"status": "pending (...)", ...}``
    (``state`` / ``stability``).
    """
    if value is None:
        return True
    if _is_ignorance_text(value):
        return True
    if isinstance(value, Mapping):
        for key in ("status", "value"):
            if _is_ignorance_text(value.get(key)):
                return True
    return False


def is_ignorance_state(response: Mapping[str, Any]) -> bool:
    """Detect whether the response describes an agent with no observations.

    Mechanical: an explicit ``initialized: False``, ``history_size: 0``, or a
    ``status`` carrying an ignorance token. Mirrors the ``is_uninitialized``
    flag in ``runtime_queries.get_governance_metrics_data`` plus the
    ``⚪ unbound`` read-purity shape.
    """
    if response.get("initialized") is False:
        return True
    if response.get("history_size") == 0:
        return True
    if _is_ignorance_text(response.get("status")):
        return True
    # The standard-verbosity shape carries no top-level status/history_size;
    # its ignorance signal lives in the self-describing summary and the
    # glossary-wrapped verdict. Both are token-free in any initialized
    # response, so reading them as signals does not false-positive.
    if _is_ignorance_text(response.get("summary")):
        return True
    verdict = response.get("verdict")
    if isinstance(verdict, Mapping) and _is_ignorance_text(verdict.get("value")):
        return True
    if _is_ignorance_text(verdict):
        return True
    return False


def _nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (Mapping, list, tuple, set)):
        return len(value) > 0
    return True


def lint_response(
    response: Mapping[str, Any],
    spec: LintSpec = GOVERNANCE_METRICS_SPEC,
) -> list[ProvenanceViolation]:
    """Return every provenance violation in ``response`` under ``spec``.

    Empty list == clean. Mechanical and side-effect free, so it is safe to call
    on a live response in a contract test or a CI gate.
    """
    violations: list[ProvenanceViolation] = []
    ignorance = is_ignorance_state(response)

    # Rule A — labeled-field presence.
    for lf in spec.labeled:
        value = response.get(lf.name)
        if not _nonempty(value):
            continue
        if lf.sibling_source is not None and not _nonempty(response.get(lf.sibling_source)):
            violations.append(
                ProvenanceViolation(
                    "A",
                    lf.name,
                    f"present without provenance label '{lf.sibling_source}' "
                    f"(§2: a value emitted without its source reads as a measurement)",
                )
            )
        if lf.inline_source is not None:
            inline = value.get(lf.inline_source) if isinstance(value, Mapping) else None
            if not _nonempty(inline):
                violations.append(
                    ProvenanceViolation(
                        "A",
                        lf.name,
                        f"present without inline provenance key '{lf.inline_source}'",
                    )
                )

    # Rule B — ignorance honesty.
    if ignorance:
        for name in spec.assessment_fields:
            if name not in response:
                continue
            value = response[name]
            if not _carries_ignorance(value):
                violations.append(
                    ProvenanceViolation(
                        "B",
                        name,
                        "confident assessment emitted in an ignorance state "
                        "(§3.2: a zero-observation agent produces no labels) — "
                        f"got {value!r}",
                    )
                )

    # Rule C — scope honesty.
    for container_name, sub_name in spec.fleet_scoped:
        container = response.get(container_name)
        if not isinstance(container, Mapping):
            continue
        sub = container.get(sub_name)
        if isinstance(sub, Mapping) and not _nonempty(sub.get("scope")):
            violations.append(
                ProvenanceViolation(
                    "C",
                    f"{container_name}.{sub_name}",
                    "fleet-scoped numbers in an agent-scoped response lack a "
                    "'scope' label (§3.3)",
                )
            )

    return violations


def assert_clean(
    response: Mapping[str, Any],
    spec: LintSpec = GOVERNANCE_METRICS_SPEC,
) -> None:
    """Raise ``AssertionError`` listing every violation. For use in tests."""
    violations = lint_response(response, spec)
    if violations:
        rendered = "\n".join(f"  - {v}" for v in violations)
        raise AssertionError(
            f"trust-contract provenance lint found {len(violations)} "
            f"violation(s):\n{rendered}"
        )


def format_violations(violations: Iterable[ProvenanceViolation]) -> str:
    """Human-readable block for CLI / CI output."""
    items = list(violations)
    if not items:
        return "provenance lint: clean"
    lines = [f"provenance lint: {len(items)} violation(s)"]
    lines.extend(f"  - {v}" for v in items)
    return "\n".join(lines)
