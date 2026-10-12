#!/usr/bin/env python3
"""Where E's and I's inputs come from, as deployed: a frozen-snapshot read.

The contract (``docs/ontology/eisv-proprioception-contract.md``, *The split*)
classes E as externally referenced ("decision outcomes, tool errors and outcome
success are world facts") and I's dominant term as calibration, with the
repair for I described as repointing its referent at the external signals E
already ingests. Before either axis can be validated against a referent, the
first question is how much of each, on real check-ins, is driven by an external
fact at all. Every behavioral check-in persists the full derivation
(``state_json.eisv_telemetry.derivation``: components, weights, adjustments,
outcome rows, calibration signal), so this is answerable by arithmetic.

What is decomposed is the behavioral sensor's OBSERVATION of E and I on each
check-in, the value persisted in the derivation. The deployed axes are that
observation passed through ``BehavioralEISV.update``'s EMA, with per-agent and
bootstrap-dependent rates (``measurement.behavioral.smoothed`` in the envelope).
Smoothing is linear, so it cannot add a source the observation lacks, but it can
change the variance shares when sources differ in autocorrelation.

This script reads ONLY a frozen JSONL snapshot. It never opens a database
connection: ``core.agent_state`` is pruned by retention, so a re-query is not a
reproduction. ``--print-export-sql`` prints the read-only query that produced
the snapshot.

Source classes (effective weight = component weight times the retained weight of
every later adjustment, so the classes sum to 1 per row):

* ``own_verdicts``: E ``decision_history``, the agent's own prior verdict actions.
* ``legacy_ode``: E ``legacy_coherence_level``, I ``legacy_coherence_trend``.
* ``self_report_text``: E ``complexity_calibration`` and the ``continuity_*``
  adjustments, derived from the agent's own report text and self-rating.
* ``fleet_calibration``: I ``calibration_accuracy``, a fleet-wide scalar.
* ``outcomes_external``: the outcome terms, in proportion to the expected share
  of the row's outcome history that is ``verification_source = 'external_signal'``.
* ``outcomes_internal``: the rest of the outcome terms (system-graded outcomes
  such as ``trajectory_validated``).
* ``tool_audit``: E ``tool_success_rate``, I ``unique_tools_ratio``.

The persisted outcome rows carry no provenance: with
``UNITARES_OUTCOME_PROVENANCE_FILTER`` off (the default) ``get_recent_outcomes``
selects ``outcome_type, is_bad, outcome_score, ts`` only, so ``verification_source``
is null in every envelope regardless of the real source. The split therefore
uses a second frozen snapshot, ``--outcome-provenance``: per-agent counts by
outcome type and verification source over the same window, from
``audit.outcome_events``. Each persisted outcome entry of type ``t`` is credited
as external with that agent's window share of external ``t`` rows. This is an
approximation of how ``outcome_success`` and ``outcome_score_consistency`` weigh
them (the producer uses a rolling 24-hour, 20-row window). No outcome is used as
a label here and no claim about outcomes follows.

Usage:
    python3 scripts/analysis/eisv_ei_referent_read.py SNAPSHOT.jsonl --outcome-provenance PROV.jsonl
    python3 scripts/analysis/eisv_ei_referent_read.py --print-export-sql
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics as st
import sys
from collections import Counter, defaultdict

PROVENANCE_SQL = """\
-- read-only; the state window plus the producer's 24-hour lookback
SET default_transaction_read_only = on;
COPY (
  -- outcome_events.agent_id is core.identities.agent_id; key on identity_id,
  -- the id core.agent_state rows carry
  SELECT jsonb_build_object('agent_id', i.identity_id::text, 'outcome_type', o.outcome_type,
                            'verification_source', o.verification_source, 'n', count(*))
  FROM audit.outcome_events o JOIN core.identities i ON i.agent_id = o.agent_id
  -- 24 hours earlier than the state window: the producer's outcome lookback
  WHERE o.ts > :window_end - interval '30 days' - interval '24 hours' AND o.ts <= :window_end
  GROUP BY i.identity_id, o.outcome_type, o.verification_source
  ORDER BY 1
) TO STDOUT;
"""

EXPORT_SQL = """\
-- read-only; run once, freeze the output, analyse the file
SET default_transaction_read_only = on;
COPY (
  SELECT jsonb_build_object(
    'id', identity_id::text, 'ts', recorded_at, 'synthetic', synthetic,
    'epistemic_class', epistemic_class,
    'E', d->'components'->'dimensions'->'E',
    'I', d->'components'->'dimensions'->'I',
    'cal_fleet', d->'calibration_signal'->'deployed'->'calibration_error',
    'cal_fleet_n', d->'calibration_signal'->'deployed'->'sample_count',
    'cal_agent_status', d->'calibration_signal'->'agent_candidate'->'evidence_status',
    'cal_agent', d->'calibration_signal'->'agent_candidate'->'calibration_error',
    'cal_agent_n', d->'calibration_signal'->'agent_candidate'->'sample_count',
    'features', d->'inputs'->'features',
    'outcomes', (SELECT jsonb_agg(jsonb_build_object(
        't', o->>'outcome_type', 'v', o->>'verification_source',
        'bad', o->'is_bad', 's', o->'outcome_score'))
      FROM jsonb_array_elements(coalesce(d->'inputs'->'outcomes', '[]'::jsonb)) o),
    'missing', d->'missing_inputs')
  FROM (SELECT identity_id, recorded_at, synthetic, epistemic_class,
               state_json->'eisv_telemetry'->'derivation' d
        FROM core.agent_state
        WHERE recorded_at >  :window_end - interval '30 days'
          AND recorded_at <= :window_end
          AND state_json->'eisv_telemetry'->'derivation'->>'kind' = 'behavioral_sensor') x
  ORDER BY 1
) TO STDOUT;
"""

CLASSES = ("own_verdicts", "legacy_ode", "self_report_text", "fleet_calibration",
           "outcomes_external", "outcomes_internal", "tool_audit")
COMPONENT_CLASS = {
    "decision_history": "own_verdicts",
    "legacy_coherence_level": "legacy_ode",
    "legacy_coherence_trend": "legacy_ode",
    "complexity_calibration": "self_report_text",
    "continuity_E_input": "self_report_text",
    "continuity_I_input": "self_report_text",
    "calibration_accuracy": "fleet_calibration",
    "tool_success_rate": "tool_audit",
    "unique_tools_ratio": "tool_audit",
}
OUTCOME_TERMS = {"outcome_success", "outcome_score_consistency"}


def _q(xs):
    xs = sorted(xs)
    if not xs:
        return "n=0"
    def pct(p):
        return xs[min(len(xs) - 1, int(p * (len(xs) - 1) + 0.5))]
    return f"n={len(xs)} mean={st.fmean(xs):.3f} p10={pct(.1):.3f} p50={pct(.5):.3f} p90={pct(.9):.3f}"


def shares(dim: dict, ext_frac: float | None) -> tuple[dict, dict]:
    """Effective weight and contribution per source class for one dimension."""
    adjustments = dim.get("adjustments") or []
    retained_after = [1.0] * (len(adjustments) + 1)
    for j in range(len(adjustments) - 1, -1, -1):
        retained_after[j] = retained_after[j + 1] * float(adjustments[j].get("retained_weight", 1.0))
    weight = dict.fromkeys(CLASSES, 0.0)
    contrib = dict.fromkeys(CLASSES, 0.0)

    def add(name, w, value):
        if name in OUTCOME_TERMS:
            frac = ext_frac or 0.0
            for cls, part in (("outcomes_external", frac), ("outcomes_internal", 1 - frac)):
                weight[cls] += w * part
                contrib[cls] += w * part * value
            return
        cls = COMPONENT_CLASS.get(name)
        if cls is None:
            raise KeyError(f"unclassified component {name!r}")
        weight[cls] += w
        contrib[cls] += w * value

    for comp in dim.get("components") or []:
        add(comp["name"], float(comp["weight"]) * retained_after[0], float(comp["value"]))
    for j, adj in enumerate(adjustments):
        if adj.get("retained_weight") == 1.0 and adj["name"] not in COMPONENT_CLASS:
            continue  # additive pressure terms (S only); not present on E or I
        add(adj["name"], float(adj["input_weight"]) * retained_after[j + 1], float(adj["input_value"]))
    return weight, contrib


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("snapshot", nargs="?")
    ap.add_argument("--outcome-provenance", help="frozen per-agent outcome provenance JSONL")
    ap.add_argument("--print-export-sql", action="store_true")
    args = ap.parse_args()
    if args.print_export_sql:
        print(EXPORT_SQL)
        print(PROVENANCE_SQL)
        return 0
    if not args.snapshot:
        ap.error("snapshot path required")
    if not args.outcome_provenance:
        # Without it every external share would print as a measured zero: the
        # persisted envelopes carry no provenance (see module docstring).
        ap.error("--outcome-provenance is required; the envelopes carry no outcome provenance")

    raw = open(args.snapshot, "rb").read()
    rows = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    print(f"snapshot sha256 {hashlib.sha256(raw).hexdigest()}  rows {len(rows)}")
    rows = [r for r in rows if not r.get("synthetic")]
    by_id = defaultdict(list)
    for r in rows:
        by_id[r["id"]].append(r)
    print(f"non-synthetic rows {len(rows)} across {len(by_id)} identities; "
          f"rows per identity {_q([len(v) for v in by_id.values()])}")
    print(f"window {min(r['ts'] for r in rows)} .. {max(r['ts'] for r in rows)}")

    # ── outcome supply ──────────────────────────────────────────────────────
    prov_total = Counter()
    prov_ext = Counter()
    prov_src = Counter()
    praw = open(args.outcome_provenance, "rb").read()
    print(f"outcome provenance sha256 {hashlib.sha256(praw).hexdigest()}")
    for line in praw.decode().splitlines():
        if not line.strip():
            continue
        p = json.loads(line)
        if p["agent_id"] not in by_id:
            continue
        key = (p["agent_id"], p["outcome_type"])
        prov_total[key] += p["n"]
        prov_src[p["verification_source"]] += p["n"]
        if p["verification_source"] == "external_signal":
            prov_ext[key] += p["n"]
    tot = sum(prov_src.values())
    print("\n== audit.outcome_events for these identities, by verification_source (window)")
    for src, n in prov_src.most_common():
        print(f"  {n:8d} ({n / tot:6.1%})  {src}")
    ext_agents = {a for (a, _t) in prov_ext}
    print(f"  identities with any external_signal outcome in the window: {len(ext_agents)}/{len(by_id)}")

    otype = Counter()
    with_outcomes = 0
    envelope_has_provenance = False
    unknown_entries = [0]
    for r in rows:
        outs = r.get("outcomes") or []
        if outs:
            with_outcomes += 1
        def expected_external(entries):
            # An entry whose (identity, type) has no provenance row is unknown,
            # not internal: it is left out of both numerator and denominator.
            known = [o for o in entries if prov_total[(r["id"], o.get("t"))]]
            unknown_entries[0] += len(entries) - len(known)
            if not known:
                return None
            return sum(prov_ext[(r["id"], o.get("t"))] / prov_total[(r["id"], o.get("t"))]
                       for o in known) / len(known)

        for o in outs:
            otype[o.get("t")] += 1
            if o.get("v") is not None:
                envelope_has_provenance = True
        # E's outcome_success reads every outcome row; I's
        # outcome_score_consistency reads only rows with a finite outcome_score.
        r["_ext_frac"] = expected_external(outs)
        scored = [o for o in outs if isinstance(o.get("s"), (int, float)) and math.isfinite(o["s"])]
        r["_ext_frac_I"] = expected_external(scored)
    print("\n== Outcome entries persisted in the E/I derivations (all check-ins)")
    total = sum(otype.values())
    for t, n in otype.most_common(10):
        print(f"  {n:8d} ({n / total:6.1%})  {t}")
    print(f"  check-ins with any outcome history: {with_outcomes}/{len(rows)} ({with_outcomes / len(rows):.1%})")
    print(f"  envelope carries verification_source: {envelope_has_provenance} "
          f"(expected False while the provenance filter is off)")
    print(f"  outcome entries with no provenance row (left out as unknown): {unknown_entries[0]} "
          f"(E and I subsets counted separately)")
    unknown_rows = sum(1 for r in rows if (r.get("outcomes") or []) and r["_ext_frac"] is None)
    print(f"  check-ins whose outcome provenance is entirely unknown: {unknown_rows}")
    fr = [r["_ext_frac"] for r in rows if r["_ext_frac"] is not None]
    print(f"  expected external share of a check-in's outcome history: {_q(fr)}")
    print(f"  check-ins whose expected external share exceeds 1%: {sum(1 for x in fr if x > 0.01)}")

    ter = [float(r["features"]["tool_error_rate"]) for r in rows
           if (r.get("features") or {}).get("tool_error_rate") is not None]
    print(f"\n== tool_error_rate input: present on {len(ter)}/{len(rows)} check-ins ({len(ter) / len(rows):.1%}); "
          f"nonzero on {sum(1 for x in ter if x > 0)}; {_q(ter)}")

    # ── effective weight shares ─────────────────────────────────────────────
    for dim_name in ("E", "I"):
        print(f"\n== {dim_name} observation: effective weight by source class "
              f"(row-level; then identity-level = per-identity mean, summarised across identities)")
        row_w = defaultdict(list)
        id_w = defaultdict(lambda: defaultdict(list))
        skipped = 0
        for r in rows:
            dim = r.get(dim_name)
            if not dim or not dim.get("components"):
                skipped += 1
                continue
            w, _ = shares(dim, r["_ext_frac" if dim_name == "E" else "_ext_frac_I"])
            for cls in CLASSES:
                row_w[cls].append(w[cls])
                id_w[cls][r["id"]].append(w[cls])
        print(f"  rows without a {dim_name} derivation: {skipped}")
        for cls in CLASSES:
            if max(row_w[cls], default=0) == 0:
                continue
            ids = [st.fmean(v) for v in id_w[cls].values()]
            print(f"  {cls:18s} rows {_q(row_w[cls])}")
            print(f"  {'':18s} ids  {_q(ids)}")

    # ── within-identity variance attribution ────────────────────────────────
    for dim_name in ("E", "I"):
        print(f"\n== {dim_name} observation: share of within-identity variance by source class "
              f"(cov(contribution, {dim_name}) / var({dim_name}); identities with >= 20 rows and var > 1e-8)")
        per_cls = defaultdict(list)
        n_ids = 0
        for ident, rs in by_id.items():
            pts = []
            for r in rs:
                dim = r.get(dim_name)
                if dim and dim.get("components"):
                    _, c = shares(dim, r["_ext_frac" if dim_name == "E" else "_ext_frac_I"])
                    pts.append((float(dim["value"]), c))
            if len(pts) < 20:
                continue
            vals = [p[0] for p in pts]
            var = st.pvariance(vals)
            if var <= 1e-8:
                continue
            n_ids += 1
            mv = st.fmean(vals)
            for cls in CLASSES:
                cs = [p[1][cls] for p in pts]
                mc = st.fmean(cs)
                cov = sum((a - mv) * (b - mc) for a, b in zip(vals, cs)) / len(vals)
                per_cls[cls].append(cov / var)
        print(f"  identities: {n_ids}")
        for cls in CLASSES:
            if per_cls[cls] and max(abs(x) for x in per_cls[cls]) > 1e-9:
                print(f"  {cls:18s} {_q(per_cls[cls])}")

    # ── I's calibration term ────────────────────────────────────────────────
    print("\n== I calibration term: fleet scalar vs per-agent candidate")
    by_hour = defaultdict(set)
    for r in rows:
        if r.get("cal_fleet") is not None:
            by_hour[r["ts"][:13]].add(round(float(r["cal_fleet"]), 9))
    multi = sum(1 for v in by_hour.values() if len(v) > 1)
    vals = sorted({x for v in by_hour.values() for x in v})
    print(f"  hours with check-ins: {len(by_hour)}; hours where identities saw more than one fleet value: {multi}")
    print(f"  distinct fleet calibration_error values over the window: {len(vals)}; "
          f"range {vals[0]:.4f} .. {vals[-1]:.4f}" if vals else "  no fleet values")
    ns = [r.get("cal_fleet_n") for r in rows if r.get("cal_fleet_n") is not None]
    print(f"  fleet sample_count: {_q(ns)}")
    status = Counter(r.get("cal_agent_status") for r in rows)
    print(f"  per-agent candidate evidence_status (rows): {dict(status)}")
    id_status = Counter()
    for ident, rs in by_id.items():
        id_status[rs[-1].get("cal_agent_status")] += 1
    print(f"  per-agent candidate evidence_status (identities, latest row): {dict(id_status)}")
    pairs = [(float(r["cal_agent"]), float(r["cal_fleet"])) for r in rows
             if r.get("cal_agent") is not None and r.get("cal_fleet") is not None]
    if pairs:
        diffs = [abs(a - f) for a, f in pairs]
        print(f"  rows with a per-agent value: {len(pairs)}; |agent - fleet| {_q(diffs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
