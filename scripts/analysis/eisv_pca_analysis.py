#!/usr/bin/env python3
"""
Principal Component Analysis & Variance Decomposition over EISV state vectors.

Supports two modes:
  --postgres              Query ``core.agent_state`` within ONE EISV instrument
                          era (``config/eisv_instrument_eras.json``), reading
                          ``E`` from ``state_json->>'E'`` and ``S`` from the
                          ``entropy`` column (``core.agent_state.entropy`` holds
                          ``S``, not ``E``), and splitting strictly by
                          ``sensor_eisv_source`` (``behavioral``, ``physical``,
                          ``unrecorded``). Does not query ``audit.outcome_events``.
  --legacy-sqlite-db ...  Explicit opt-in for retired SQLite snapshots; also
                          requires ``--i-understand-this-is-stale``.

Usage:
  python3 scripts/analysis/eisv_pca_analysis.py --postgres --era SN1
  python3 scripts/analysis/eisv_pca_analysis.py --postgres --era RR1 --json
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.analysis import eisv_era_sql  # noqa: E402
from scripts.analysis.behavioral_self_loop_shadow_read import (  # noqa: E402
    DEFAULT_DB_URL,
    SENSOR_CLASS_ORDER,
    STAMP_FILTERED_ID_COUNTS_SQL,
    EraResolutionError,
    EraWindow,
    resolve_era_window,
)
from src import eisv_eras  # noqa: E402

SCHEMA = "eisv.pca_analysis.v1"
POSTGRES_FEATURES = ("E", "I", "S", "V", "coherence", "risk")
CORE_FEATURES = ("E", "I", "S", "V")

LEGACY_DB_PATH = _REPO_ROOT / "data" / "governance.db"
LEGACY_WARNING = (
    "This script's SQLite path reads the removed SQLite governance backend, "
    "which is stale and not canonical. Use --postgres --era <ERA> for current "
    "Postgres analysis, or pass --legacy-sqlite-db with --i-understand-this-is-stale."
)

# core.agent_state stores S in the `entropy` column and stores E in `state_json->>'E'`.
POSTGRES_FETCH_SQL = """
SELECT
    s.identity_id::text,
    COALESCE(NULLIF(s.state_json->>'sensor_eisv_source', ''), 'unrecorded') AS sensor_class,
    NULLIF(s.state_json->>'E', '')::float AS e_val,
    s.integrity::float AS i_val,
    s.entropy::float AS s_val,
    s.volatility::float AS v_val,
    s.coherence::float AS coh_val,
    NULLIF(s.state_json->>'risk_score', '')::float AS risk_val
FROM core.agent_state s
WHERE s.synthetic IS NOT TRUE
  AND s.recorded_at >= %(start)s
  AND s.recorded_at < %(end)s
  AND (%(stamp_era)s::text IS NULL OR s.state_json->'eisv_instrument'->>'era' = %(stamp_era)s::text)
ORDER BY s.recorded_at
"""


def _default_pg_connect(db_url: str) -> Any:
    import psycopg2

    return psycopg2.connect(db_url)


def load_eisv_histories(conn, min_updates=15, max_updates=5000):
    """Extract per-timepoint EISV vectors from legacy SQLite state_json histories."""
    cursor = conn.execute(
        "SELECT agent_id, state_json FROM agent_state "
        "WHERE update_count >= ? AND update_count <= ?",
        (min_updates, max_updates),
    )

    rows = []
    agent_ids = []
    for agent_id, state_json_str in cursor:
        state = json.loads(state_json_str)
        n = len(state.get("E_history", []))
        if n < min_updates:
            continue

        E_hist = state["E_history"]
        I_hist = state["I_history"]
        S_hist = state["S_history"]
        V_hist = state["V_history"]
        coh_hist = state.get("coherence_history", [0.5] * n)
        risk_hist = state.get("risk_history", [0.5] * n)
        lam_hist = state.get("lambda1_history", [0.125] * n)

        length = min(
            len(E_hist),
            len(I_hist),
            len(S_hist),
            len(V_hist),
            len(coh_hist),
            len(risk_hist),
            len(lam_hist),
        )

        for i in range(length):
            rows.append(
                [
                    E_hist[i],
                    I_hist[i],
                    S_hist[i],
                    V_hist[i],
                    coh_hist[i],
                    risk_hist[i],
                    lam_hist[i],
                ]
            )
            agent_ids.append(agent_id)

    return np.array(rows), agent_ids


def load_complexity_signals(conn):
    """Extract raw dual-log signals from legacy SQLite complexity_derivation events."""
    cursor = conn.execute(
        "SELECT agent_id, details_json FROM audit_events "
        "WHERE event_type = 'complexity_derivation'"
    )

    rows = []
    for _agent_id, details_str in cursor:
        d = json.loads(details_str)
        reported = d.get("reported_complexity")
        derived = d.get("derived_complexity")
        discrepancy = d.get("discrepancy")
        resp_len = d.get("response_length")

        if None in (reported, derived, discrepancy, resp_len):
            continue

        rows.append(
            [
                float(reported),
                float(derived),
                float(discrepancy),
                float(resp_len),
            ]
        )

    return np.array(rows) if rows else np.empty((0, 4))


def extract_postgres_matrices(
    records: Sequence[Sequence[Any]],
) -> dict[str, tuple[np.ndarray, list[str]]]:
    """Group Postgres ``core.agent_state`` rows by ``sensor_eisv_source``.

    Record layout: ``(identity_id, sensor_class, E, I, S, V, coherence, risk)``.
    Rows with missing or non-finite feature values are skipped per sensor class.
    """
    grouped_rows: dict[str, list[list[float]]] = defaultdict(list)
    grouped_ids: dict[str, list[str]] = defaultdict(list)

    for rec in records:
        identity_id = str(rec[0])
        sensor_class = str(rec[1] or "unrecorded")
        raw_vals = rec[2:8]
        if any(v is None for v in raw_vals):
            continue
        floats = [float(v) for v in raw_vals]
        if not all(math.isfinite(v) for v in floats):
            continue
        grouped_rows[sensor_class].append(floats)
        grouped_ids[sensor_class].append(identity_id)

    ordered = [c for c in SENSOR_CLASS_ORDER if c in grouped_rows] + sorted(
        c for c in grouped_rows if c not in SENSOR_CLASS_ORDER
    )
    return {
        c: (np.array(grouped_rows[c], dtype=float), grouped_ids[c])
        for c in ordered
    }


def fetch_postgres_rows(
    conn: Any, window: EraWindow
) -> tuple[dict[Optional[str], int], list[tuple[Any, ...]]]:
    """Read single-era EISV snapshot rows from Postgres ``core.agent_state``."""
    with conn.cursor() as cur:
        if window.stamp_era is None:
            cur.execute(
                eisv_era_sql.instrument_id_counts_sql("%s", "%s"),
                (window.start, window.end),
            )
        else:
            cur.execute(
                STAMP_FILTERED_ID_COUNTS_SQL,
                (window.start, window.end, window.stamp_era),
            )
        id_counts = {r[0]: int(r[1]) for r in cur.fetchall()}
        cur.execute(
            POSTGRES_FETCH_SQL,
            {"start": window.start, "end": window.end, "stamp_era": window.stamp_era},
        )
        rows = list(cur.fetchall())
    return id_counts, rows


def _safe_correlation_matrix(X: np.ndarray) -> np.ndarray:
    """Compute correlation matrix without NaN division warnings on constant features."""
    n_obs, n_feat = X.shape
    mu = X.mean(axis=0)
    centered = X - mu
    std = X.std(axis=0)
    active = std >= 1e-10
    corr = np.eye(n_feat, dtype=float)
    if n_obs < 2 or not np.any(active):
        return corr
    normed = np.zeros_like(centered)
    normed[:, active] = centered[:, active] / std[active]
    active_idx = np.where(active)[0]
    sub = (normed[:, active_idx].T @ normed[:, active_idx]) / float(n_obs)
    sub = np.clip(sub, -1.0, 1.0)
    for row_pos, i in enumerate(active_idx):
        for col_pos, j in enumerate(active_idx):
            corr[i, j] = float(sub[row_pos, col_pos])
        corr[i, i] = 1.0
    return corr


def compute_pca_summary(
    X: np.ndarray, feature_names: Sequence[str]
) -> Optional[dict[str, Any]]:
    """Compute PCA eigendecomposition and safe correlation matrix."""
    if X.ndim != 2 or X.shape[0] < 2 or X.shape[1] < 2:
        return None
    mu = X.mean(axis=0)
    std = X.std(axis=0)
    active = std >= 1e-10
    if not np.any(active):
        return None

    scale = np.where(active, std, 1.0)
    Z = (X - mu) / scale
    cov = np.cov(Z, rowvar=False)
    eigenvalues, eigenvectors = np.linalg.eigh(cov)

    idx = np.argsort(eigenvalues)[::-1]
    eigenvalues = eigenvalues[idx]
    eigenvectors = eigenvectors[:, idx]

    total_var = float(eigenvalues.sum())
    if total_var <= 1e-12:
        return None

    explained = eigenvalues / total_var
    cumulative = np.cumsum(explained)
    corr = _safe_correlation_matrix(X)

    high_correlations = []
    for i in range(len(feature_names)):
        for j in range(i + 1, len(feature_names)):
            r = float(corr[i, j])
            if abs(r) > 0.5:
                high_correlations.append(
                    {"a": feature_names[i], "b": feature_names[j], "r": round(r, 4)}
                )

    return {
        "n_observations": int(X.shape[0]),
        "feature_names": list(feature_names),
        "constant_features": [
            feature_names[i] for i in range(len(feature_names)) if not active[i]
        ],
        "eigenvalues": eigenvalues,
        "eigenvectors": eigenvectors,
        "explained": explained,
        "cumulative": cumulative,
        "corr": corr,
        "n90": int(np.searchsorted(cumulative, 0.90) + 1),
        "n95": int(np.searchsorted(cumulative, 0.95) + 1),
        "high_correlations": high_correlations,
    }


def run_pca(X, feature_names, label=""):
    """Run PCA and print results."""
    print(f"\n{'='*70}")
    print(f"PCA: {label}")
    print(f"{'='*70}")

    summary = compute_pca_summary(X, feature_names)
    if summary is None:
        print(
            f"Skipped PCA ({X.shape[0]} observations, {X.shape[1]} features): "
            "need >=2 observations and non-zero feature variance."
        )
        return None

    eigenvalues = summary["eigenvalues"]
    eigenvectors = summary["eigenvectors"]
    explained = summary["explained"]
    cumulative = summary["cumulative"]
    corr = summary["corr"]

    print(f"Observations: {X.shape[0]:,}  Features: {X.shape[1]}")
    if summary["constant_features"]:
        print(f"Constant features (zero variance): {', '.join(summary['constant_features'])}")

    print("\n--- Explained Variance ---")
    for i, (_ev, ex, cum) in enumerate(zip(eigenvalues, explained, cumulative)):
        bar = "#" * int(ex * 50)
        print(f"  PC{i+1}: {ex:6.1%}  (cumul: {cum:6.1%})  {bar}")

    print(f"\n  Components for 90% variance: {summary['n90']}")
    print(f"  Components for 95% variance: {summary['n95']}")

    n_show = min(4, len(eigenvalues))
    print(f"\n--- Loadings (top {n_show} components) ---")
    header = "  Feature" + "".join(f"{'PC'+str(i+1):>10}" for i in range(n_show))
    print(header)
    print("  " + "-" * (len(header) - 2))

    for j, name in enumerate(feature_names):
        vals = "".join(f"{eigenvectors[j, i]:10.3f}" for i in range(n_show))
        dominant = [i for i in range(n_show) if abs(eigenvectors[j, i]) > 0.45]
        marker = f"  <-- dominant in PC{','.join(str(d+1) for d in dominant)}" if dominant else ""
        print(f"  {name:<18}{vals}{marker}")

    print("\n--- Correlation Matrix ---")
    header = "  " + "".join(f"{n[:7]:>8}" for n in feature_names)
    print(header)
    for i, name in enumerate(feature_names):
        vals = "".join(f"{corr[i, j]:8.3f}" for j in range(len(feature_names)))
        print(f"  {name[:7]:<8}{vals}")

    print("\n--- High Correlations (|r| > 0.5) ---")
    if summary["high_correlations"]:
        for pair in summary["high_correlations"]:
            print(f"  {pair['a']} <-> {pair['b']}: r = {pair['r']:.3f}")
    else:
        print("  None found.")

    return eigenvalues, eigenvectors, explained, cumulative


def compute_variance_decomposition(
    X: np.ndarray, agent_ids: Sequence[str], feature_names: Sequence[str]
) -> list[dict[str, Any]]:
    """Compute between-agent vs within-agent variance per feature."""
    agents = defaultdict(list)
    for i, aid in enumerate(agent_ids):
        agents[aid].append(X[i])

    eligible_rows = [np.array(rows) for rows in agents.values() if len(rows) >= 2]
    if not eligible_rows or X.shape[0] == 0:
        return []

    total_n = sum(len(arr) for arr in eligible_rows)
    grand_mean = np.vstack(eligible_rows).mean(axis=0)
    within_var = np.zeros(X.shape[1], dtype=float)
    between_var = np.zeros(X.shape[1], dtype=float)

    for arr in eligible_rows:
        n = len(arr)
        agent_mean = arr.mean(axis=0)
        within_var += arr.var(axis=0) * n
        between_var += n * (agent_mean - grand_mean) ** 2

    within_var /= total_n
    between_var /= total_n

    rows_out: list[dict[str, Any]] = []
    for j, name in enumerate(feature_names):
        total = within_var[j] + between_var[j]
        if total < 1e-10:
            continue
        ratio = float(between_var[j] / within_var[j]) if within_var[j] > 1e-10 else float("inf")
        interp = "IDENTITY" if ratio > 2.0 else "BEHAVIOR" if ratio < 0.5 else "mixed"
        rows_out.append(
            {
                "feature": name,
                "between": float(between_var[j]),
                "within": float(within_var[j]),
                "ratio_bw": ratio if math.isfinite(ratio) else None,
                "interpretation": interp,
            }
        )
    return rows_out


def analyze_per_agent_variance(X, agent_ids, feature_names):
    """Check if variance is between-agent or within-agent."""
    rows_out = compute_variance_decomposition(X, agent_ids, feature_names)
    if not rows_out:
        return []

    print("\n--- Variance Decomposition (between vs within agent) ---")
    print(f"  {'Feature':<18} {'Between':>10} {'Within':>10} {'Ratio B/W':>10} {'Interpretation'}")
    print("  " + "-" * 65)
    for row in rows_out:
        ratio_str = f"{row['ratio_bw']:10.2f}" if row["ratio_bw"] is not None else f"{'inf':>10}"
        print(
            f"  {row['feature']:<18} {row['between']:10.6f} "
            f"{row['within']:10.6f} {ratio_str} {row['interpretation']}"
        )

    print("\n  IDENTITY = mostly varies between agents (fingerprint)")
    print("  BEHAVIOR = mostly varies within agents (state changes)")
    return rows_out


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "PCA and between/within variance analysis over EISV snapshots "
            "(single-era Postgres or explicit legacy SQLite)."
        )
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--postgres",
        action="store_true",
        help="Query live Postgres core.agent_state within one instrument era.",
    )
    mode_group.add_argument(
        "--legacy-sqlite-db",
        type=Path,
        default=None,
        help="Explicit path to a retired SQLite governance snapshot.",
    )
    parser.add_argument(
        "--i-understand-this-is-stale",
        action="store_true",
        help="Required acknowledgement that SQLite is not current governance truth.",
    )
    parser.add_argument(
        "--era",
        default=None,
        help="Single EISV instrument era id from config/eisv_instrument_eras.json (default: current era).",
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--json", action="store_true", help="Emit JSON summary (--postgres mode).")
    return parser.parse_args(argv)


def _open_legacy_sqlite(db_path: Path):
    # Read-only URI mode prevents accidental writes to a legacy snapshot.
    uri = f"file:{db_path.resolve()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _serialize_pca_summary(summary: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    if summary is None:
        return None
    return {
        "n_observations": summary["n_observations"],
        "feature_names": summary["feature_names"],
        "constant_features": summary["constant_features"],
        "explained_variance": [round(float(x), 6) for x in summary["explained"]],
        "cumulative_variance": [round(float(x), 6) for x in summary["cumulative"]],
        "n90": summary["n90"],
        "n95": summary["n95"],
        "high_correlations": summary["high_correlations"],
    }


def _run_postgres_mode(
    args: argparse.Namespace,
    *,
    now: Optional[datetime] = None,
    connect: Any = None,
    registry_path: Path = eisv_eras.REGISTRY_PATH,
) -> int:
    era_id = args.era or eisv_eras.current_era_id(registry_path)
    try:
        window = resolve_era_window(era_id, as_of=now, path=registry_path)
    except EraResolutionError as exc:
        print(f"Era resolution error: {exc}", file=sys.stderr)
        return 2

    stamp_filtered = window.stamp_era is not None
    if (
        eisv_eras.check_window(
            window.start, window.end, stamp_filtered=stamp_filtered, path=registry_path
        )
        is None
    ):
        return 2

    conn_fn = connect or _default_pg_connect
    conn = conn_fn(args.db_url)
    if hasattr(conn, "set_session"):
        conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
    try:
        id_counts, records = fetch_postgres_rows(conn, window)
    finally:
        conn.close()

    era_lines = eisv_eras.check_window(
        window.start,
        window.end,
        stamp_filtered=stamp_filtered,
        id_counts=id_counts,
        path=registry_path,
    )
    if era_lines is None:
        return 2

    by_sensor = extract_postgres_matrices(records)

    if args.json:
        sensor_payload: dict[str, Any] = {}
        for sensor_class, (X, agent_ids) in by_sensor.items():
            full_summary = compute_pca_summary(X, POSTGRES_FEATURES)
            core_summary = compute_pca_summary(X[:, :4], CORE_FEATURES)
            var_decomp = compute_variance_decomposition(X, agent_ids, POSTGRES_FEATURES)
            sensor_payload[sensor_class] = {
                "n_observations": int(X.shape[0]),
                "identities": len(set(agent_ids)),
                "full_pca": _serialize_pca_summary(full_summary),
                "core_eisv_pca": _serialize_pca_summary(core_summary),
                "variance_decomposition": var_decomp,
            }
        payload = {
            "schema": SCHEMA,
            "era": window.era,
            "window": {
                "start": window.start.isoformat(),
                "end": window.end.isoformat(),
                "stamp_era": window.stamp_era,
            },
            "era_lines": era_lines,
            "total_observations": sum(int(X.shape[0]) for X, _ in by_sensor.values()),
            "by_sensor_class": sensor_payload,
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(f"EISV PCA Analysis — Postgres era `{window.era}`")
    for line in era_lines:
        print(f"  {line}")
    if window.stamp_era:
        print(f"  Row stamp filter: state_json->'eisv_instrument'->>'era' = '{window.stamp_era}'")
    print("  Field mapping: E <- state_json->>'E', S <- entropy column")

    if not by_sensor:
        print("\nNo valid observations found in this era window.")
        return 0

    for sensor_class, (X, agent_ids) in by_sensor.items():
        n_ids = len(set(agent_ids))
        print(
            f"\n>>> Sensor class: {sensor_class} "
            f"(n={X.shape[0]:,}, identities={n_ids})"
        )
        run_pca(X, list(POSTGRES_FEATURES), f"{window.era} [{sensor_class}] — Full Snapshot")
        analyze_per_agent_variance(X, agent_ids, list(POSTGRES_FEATURES))
        run_pca(X[:, :4], list(CORE_FEATURES), f"{window.era} [{sensor_class}] — Core EISV Only")

    print(f"\n{'='*70}")
    print("Done.")
    return 0


def _run_legacy_sqlite_mode(args: argparse.Namespace) -> int:
    db_path = args.legacy_sqlite_db
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return 1

    conn = _open_legacy_sqlite(db_path)
    try:
        eisv_features = ["E", "I", "S", "V", "coherence", "risk", "lambda1"]

        print("\nLoading EISV histories...")
        X_eisv, agent_ids = load_eisv_histories(conn, min_updates=15, max_updates=5000)
        if X_eisv.shape[0] > 0:
            print(f"Loaded {X_eisv.shape[0]:,} observations from {len(set(agent_ids))} agents")
            run_pca(X_eisv, eisv_features, "EISV + Derived Signals (state_json histories)")
            analyze_per_agent_variance(X_eisv, agent_ids, eisv_features)
            run_pca(X_eisv[:, :4], ["E", "I", "S", "V"], "Core EISV Only (4 dimensions)")
        else:
            print("No EISV history data found with sufficient updates.")

        print("\n\nLoading complexity signals...")
        X_complexity = load_complexity_signals(conn)
        complexity_features = ["reported_cx", "derived_cx", "discrepancy", "resp_length"]
        if X_complexity.shape[0] > 0:
            print(f"Loaded {X_complexity.shape[0]:,} complexity observations")
            run_pca(X_complexity, complexity_features, "Raw Dual-Log Signals (complexity_derivation)")
        else:
            print("No complexity data found.")

        print("\n\nBuilding per-agent profiles...")
        cursor = conn.execute(
            "SELECT agent_id, E, I, S, V, coherence, update_count "
            "FROM agent_state WHERE update_count >= 10"
        )
        agent_profiles = {}
        for aid, E, I, S, V, coh, uc in cursor:
            agent_profiles[aid] = {
                "E": E,
                "I": I,
                "S": S,
                "V": V,
                "coherence": coh,
                "update_count": uc,
            }

        cursor = conn.execute(
            "SELECT agent_id, details_json FROM audit_events "
            "WHERE event_type = 'complexity_derivation'"
        )
        agent_cx = defaultdict(list)
        for aid, details_str in cursor:
            d = json.loads(details_str)
            if d.get("derived_complexity") is not None:
                agent_cx[aid].append(
                    {
                        "reported": d.get("reported_complexity", 0.5),
                        "derived": d["derived_complexity"],
                        "discrepancy": d.get("discrepancy", 0),
                        "resp_len": d.get("response_length", 0),
                    }
                )

        combined_rows = []
        combined_names = [
            "E",
            "I",
            "S",
            "V",
            "coherence",
            "mean_reported",
            "mean_derived",
            "mean_discrep",
            "mean_resp_len",
            "update_count",
        ]
        for aid, profile in agent_profiles.items():
            cx_list = agent_cx.get(aid, [])
            if len(cx_list) < 3:
                continue
            combined_rows.append(
                [
                    profile["E"],
                    profile["I"],
                    profile["S"],
                    profile["V"],
                    profile["coherence"],
                    np.mean([c["reported"] for c in cx_list]),
                    np.mean([c["derived"] for c in cx_list]),
                    np.mean([c["discrepancy"] for c in cx_list]),
                    np.mean([c["resp_len"] for c in cx_list]),
                    profile["update_count"],
                ]
            )

        if combined_rows:
            X_combined = np.array(combined_rows)
            print(f"Built {X_combined.shape[0]} agent profiles with both EISV and complexity data")
            run_pca(X_combined, combined_names, "Per-Agent Combined Profiles")
    finally:
        conn.close()

    print(f"\n{'='*70}")
    print("Done.")
    return 0


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    now: Optional[datetime] = None,
    connect: Any = None,
    registry_path: Path = eisv_eras.REGISTRY_PATH,
) -> int:
    args = parse_args(argv)
    if args.postgres:
        return _run_postgres_mode(
            args, now=now, connect=connect, registry_path=registry_path
        )

    if args.legacy_sqlite_db is None or not args.i_understand_this_is_stale:
        print(LEGACY_WARNING, file=sys.stderr)
        print(
            f"Legacy default was: {LEGACY_DB_PATH}. It is intentionally disabled.",
            file=sys.stderr,
        )
        return 2

    return _run_legacy_sqlite_mode(args)


if __name__ == "__main__":
    raise SystemExit(main())
