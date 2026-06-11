#!/usr/bin/env python3
"""
Compositionality Metrics for Lumen's Primitive Language

Measures whether Lumen's primitive utterances form a compositional mapping
from internal state space to signal space (topographic similarity).

Usage:
    python scripts/compositionality_metrics.py [--db PATH] [--synthetic] [--output-dir DIR]
"""

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Dict, Any, List, Tuple

import numpy as np

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))


# ---------------------------------------------------------------------------
# Levenshtein distance (with fast C-ext fallback)
# ---------------------------------------------------------------------------

try:
    import editdistance as _ed

    def levenshtein_distance(a: str, b: str) -> int:
        return _ed.eval(a, b)
except ImportError:
    def levenshtein_distance(a: str, b: str) -> int:
        """Pure-Python Levenshtein (DP)."""
        if len(a) < len(b):
            return levenshtein_distance(b, a)
        if len(b) == 0:
            return len(a)
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a):
            curr = [i + 1]
            for j, cb in enumerate(b):
                cost = 0 if ca == cb else 1
                curr.append(min(curr[j] + 1, prev[j + 1] + 1, prev[j] + cost))
            prev = curr
        return prev[-1]


# ---------------------------------------------------------------------------
# Embedded PRIMITIVES (lightweight copy from anima-mcp, avoids cross-dep)
# ---------------------------------------------------------------------------

# Each token: (category, {affinity_dimension: value}, base_weight)
PRIMITIVES = {
    # State layer
    "warm":   ("state",      {"warmth": 0.8},      1.0),
    "cold":   ("state",      {"warmth": -0.8},     1.0),
    "new":    ("state",      {"brightness": 0.8},  1.0),
    "soft":   ("state",      {"brightness": -0.8}, 1.0),
    "quiet":  ("state",      {"stability": 0.7},   1.0),
    "busy":   ("state",      {"stability": -0.5},  1.0),
    # Presence layer
    "here":   ("presence",   {"presence": 0.3},    1.0),
    "feel":   ("presence",   {},                    1.2),
    "sense":  ("presence",   {},                    1.1),
    # Relational layer
    "you":    ("relational", {"presence": 0.8},    1.0),
    "with":   ("relational", {"presence": 0.6},    1.0),
    # Inquiry layer
    "why":    ("inquiry",    {},                    1.3),
    "what":   ("inquiry",    {},                    1.2),
    "wonder": ("inquiry",    {},                    1.1),
    # Change layer
    "more":   ("change",     {},                    1.0),
    "less":   ("change",     {},                    1.0),
}

ALL_TOKENS = list(PRIMITIVES.keys())


# ---------------------------------------------------------------------------
# Data Loading
# ---------------------------------------------------------------------------

def load_primitive_history(
    db_path: Path,
    min_records: int = 30,
) -> List[Dict[str, Any]]:
    """Load primitive utterance history from an explicitly supplied SQLite DB."""
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT timestamp, tokens, category_pattern, "
        "warmth, brightness, stability, presence, score "
        "FROM primitive_history ORDER BY timestamp"
    ).fetchall()
    conn.close()

    records = []
    for row in rows:
        tokens_str = row['tokens']
        if not tokens_str:
            continue
        records.append({
            'timestamp': row['timestamp'],
            'tokens': tokens_str.split(),
            'category_pattern': row['category_pattern'],
            'warmth': float(row['warmth']) if row['warmth'] is not None else 0.5,
            'brightness': float(row['brightness']) if row['brightness'] is not None else 0.5,
            'stability': float(row['stability']) if row['stability'] is not None else 0.5,
            'presence': float(row['presence']) if row['presence'] is not None else 0.0,
            'score': float(row['score']) if row['score'] is not None else None,
        })

    if len(records) < min_records:
        raise ValueError(
            f"Only {len(records)} records found (need >= {min_records}). "
            f"Use --synthetic for development."
        )
    return records


def generate_synthetic_data(
    n_samples: int = 500,
    seed: int = 42,
) -> List[Dict[str, Any]]:
    """
    Generate synthetic data with controlled compositionality.

    70% of samples use state-aligned tokens (compositional).
    30% use random tokens (noise floor).
    """
    rng = np.random.default_rng(seed)
    records = []

    for i in range(n_samples):
        warmth = rng.uniform(0.0, 1.0)
        brightness = rng.uniform(0.0, 1.0)
        stability = rng.uniform(0.0, 1.0)
        presence = rng.uniform(-1.0, 1.0)

        count = rng.choice([1, 2, 3], p=[0.2, 0.5, 0.3])

        if rng.random() < 0.7:
            # Compositional: pick tokens aligned to state
            tokens = []
            warmth_norm = (warmth - 0.5) * 2
            bright_norm = (brightness - 0.5) * 2

            # Pick a state token aligned to dominant dimension
            if abs(warmth_norm) > abs(bright_norm):
                tokens.append("warm" if warmth_norm > 0 else "cold")
            else:
                tokens.append("new" if bright_norm > 0 else "soft")

            if count >= 2:
                if presence > 0.3:
                    tokens.append(rng.choice(["you", "here", "feel"]))
                elif stability > 0.6:
                    tokens.append("quiet")
                else:
                    tokens.append(rng.choice(["why", "what", "wonder"]))

            if count >= 3:
                remaining = [t for t in ALL_TOKENS if t not in tokens]
                tokens.append(rng.choice(remaining))
        else:
            # Random: pick any tokens
            tokens = list(rng.choice(ALL_TOKENS, size=count, replace=False))

        records.append({
            'timestamp': f"2026-02-{(i // 24) + 1:02d}T{i % 24:02d}:00:00",
            'tokens': tokens,
            'category_pattern': "-".join(PRIMITIVES[t][0] for t in tokens),
            'warmth': warmth,
            'brightness': brightness,
            'stability': stability,
            'presence': presence,
            'score': rng.uniform(0.3, 0.8),
        })

    return records


# ---------------------------------------------------------------------------
# Distance Computation
# ---------------------------------------------------------------------------

def compute_meaning_distances(
    records: List[Dict[str, Any]],
    normalize_presence: bool = True,
) -> np.ndarray:
    """Compute pairwise Euclidean distances in meaning (state) space."""
    n = len(records)
    states = np.zeros((n, 4))
    for i, r in enumerate(records):
        p = r['presence']
        if normalize_presence:
            p = (p + 1.0) / 2.0  # [-1,1] -> [0,1]
        states[i] = [r['warmth'], r['brightness'], r['stability'], p]

    # Pairwise Euclidean distances
    diff = states[:, np.newaxis, :] - states[np.newaxis, :, :]
    dists = np.sqrt(np.sum(diff ** 2, axis=2))
    return dists


def compute_signal_distances(
    records: List[Dict[str, Any]],
) -> np.ndarray:
    """Compute pairwise Levenshtein distances in signal (token) space."""
    n = len(records)
    signals = [" ".join(r['tokens']) for r in records]
    dists = np.zeros((n, n))
    for i in range(n):
        for j in range(i + 1, n):
            d = levenshtein_distance(signals[i], signals[j])
            dists[i, j] = d
            dists[j, i] = d
    return dists


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def topographic_similarity(
    meaning_distances: np.ndarray,
    signal_distances: np.ndarray,
) -> Dict[str, Any]:
    """
    Compute topographic similarity (Spearman correlation between
    meaning-space and signal-space distance matrices).
    """
    from scipy.stats import spearmanr

    n = meaning_distances.shape[0]
    # Extract upper triangle (excluding diagonal)
    idx = np.triu_indices(n, k=1)
    md_flat = meaning_distances[idx]
    sd_flat = signal_distances[idx]

    rho, p_value = spearmanr(md_flat, sd_flat)

    if rho > 0.5:
        interp = "strong compositionality"
    elif rho > 0.3:
        interp = "moderate compositionality"
    elif rho > 0.1:
        interp = "weak compositionality"
    elif rho > -0.1:
        interp = "no compositionality (random)"
    else:
        interp = "anti-compositional"

    return {
        'rho': float(rho),
        'p_value': float(p_value),
        'n_pairs': len(md_flat),
        'interpretation': interp,
    }


def region_consistency(
    records: List[Dict[str, Any]],
    n_bins: int = 5,
) -> Dict[str, Any]:
    """
    Compute token consistency per region of state space.

    Low entropy = consistent. High entropy = random.
    """
    n_tokens = len(ALL_TOKENS)
    max_entropy = math.log2(n_tokens) if n_tokens > 1 else 1.0

    # Bin each dimension
    def bin_val(v, lo, hi):
        frac = (v - lo) / (hi - lo) if hi > lo else 0.5
        return min(n_bins - 1, max(0, int(frac * n_bins)))

    # Collect token counts per bin
    bin_tokens: Dict[Tuple, List[str]] = {}
    for r in records:
        w_bin = bin_val(r['warmth'], 0, 1)
        b_bin = bin_val(r['brightness'], 0, 1)
        s_bin = bin_val(r['stability'], 0, 1)
        p_bin = bin_val(r['presence'], -1, 1)
        key = (w_bin, b_bin, s_bin, p_bin)
        if key not in bin_tokens:
            bin_tokens[key] = []
        bin_tokens[key].extend(r['tokens'])

    # Compute entropy per bin
    entropies = []
    per_dim_tokens = {d: {} for d in ['warmth', 'brightness', 'stability', 'presence']}

    for key, tokens in bin_tokens.items():
        if len(tokens) < 3:
            continue  # Too few for meaningful entropy
        # Token frequency distribution
        counts = {}
        for t in tokens:
            counts[t] = counts.get(t, 0) + 1
        total = len(tokens)
        entropy = 0.0
        for c in counts.values():
            p = c / total
            if p > 0:
                entropy -= p * math.log2(p)
        entropies.append(entropy)

    mean_entropy = float(np.mean(entropies)) if entropies else max_entropy
    normalized = mean_entropy / max_entropy if max_entropy > 0 else 1.0

    return {
        'mean_entropy': mean_entropy,
        'max_possible_entropy': max_entropy,
        'normalized_entropy': normalized,
        'n_occupied_bins': len(bin_tokens),
        'n_analyzed_bins': len(entropies),
        'consistency_score': 1.0 - normalized,
    }


def temporal_compositionality(
    records: List[Dict[str, Any]],
    window_size: int = 50,
    step: int = 25,
) -> Dict[str, Any]:
    """Track compositionality metrics over time windows."""
    from scipy.stats import linregress

    if len(records) < window_size:
        return {
            'windows': [],
            'ts_trend': 0.0,
            'consistency_trend': 0.0,
            'improving': False,
            'insufficient_data': True,
        }

    windows = []
    for start in range(0, len(records) - window_size + 1, step):
        window_records = records[start:start + window_size]
        md = compute_meaning_distances(window_records)
        sd = compute_signal_distances(window_records)
        ts = topographic_similarity(md, sd)
        rc = region_consistency(window_records)

        windows.append({
            'start_idx': start,
            'end_idx': start + window_size,
            'start_time': window_records[0].get('timestamp', ''),
            'end_time': window_records[-1].get('timestamp', ''),
            'topographic_similarity': ts['rho'],
            'consistency_score': rc['consistency_score'],
            'n_records': len(window_records),
        })

    if len(windows) >= 2:
        x = np.arange(len(windows))
        ts_vals = [w['topographic_similarity'] for w in windows]
        cs_vals = [w['consistency_score'] for w in windows]

        ts_slope = linregress(x, ts_vals).slope
        cs_slope = linregress(x, cs_vals).slope
    else:
        ts_slope = 0.0
        cs_slope = 0.0

    return {
        'windows': windows,
        'ts_trend': float(ts_slope),
        'consistency_trend': float(cs_slope),
        'improving': ts_slope > 0 and cs_slope > 0,
    }


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def plot_compositionality(results: Dict[str, Any], output_dir: Path) -> None:
    """Generate compositionality visualizations."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        'font.family': 'serif',
        'font.size': 10,
        'axes.labelsize': 11,
        'figure.dpi': 300,
        'savefig.bbox': 'tight',
    })

    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. TS scatter: meaning distance vs signal distance
    if 'meaning_distances' in results and 'signal_distances' in results:
        md = results['meaning_distances']
        sd = results['signal_distances']
        n = md.shape[0]
        idx = np.triu_indices(n, k=1)
        md_flat = md[idx]
        sd_flat = sd[idx]

        # Subsample for plotting if too many pairs
        if len(md_flat) > 5000:
            rng = np.random.default_rng(42)
            sample_idx = rng.choice(len(md_flat), 5000, replace=False)
            md_plot = md_flat[sample_idx]
            sd_plot = sd_flat[sample_idx]
        else:
            md_plot = md_flat
            sd_plot = sd_flat

        fig, ax = plt.subplots(figsize=(4.5, 3.5))
        ax.scatter(md_plot, sd_plot, s=2, alpha=0.3, color='#1f77b4', rasterized=True)
        ax.set_xlabel('Meaning distance (Euclidean)')
        ax.set_ylabel('Signal distance (Levenshtein)')
        rho = results['topographic_similarity']['rho']
        ax.set_title(f'Topographic Similarity (rho={rho:.3f})')
        fig.savefig(output_dir / 'ts_scatter.png')
        plt.close(fig)

    # 2. Temporal trends
    temporal = results.get('temporal', {})
    windows = temporal.get('windows', [])
    if len(windows) >= 2:
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(8, 3))

        x = range(len(windows))
        ts_vals = [w['topographic_similarity'] for w in windows]
        cs_vals = [w['consistency_score'] for w in windows]

        ax1.plot(x, ts_vals, 'o-', color='#1f77b4', markersize=4)
        ax1.set_xlabel('Window')
        ax1.set_ylabel('Topographic Similarity')
        ax1.set_title(f'TS over time (trend: {temporal["ts_trend"]:.4f}/window)')

        ax2.plot(x, cs_vals, 's-', color='#2ca02c', markersize=4)
        ax2.set_xlabel('Window')
        ax2.set_ylabel('Consistency Score')
        ax2.set_title(f'Consistency over time (trend: {temporal["consistency_trend"]:.4f}/window)')

        fig.tight_layout()
        fig.savefig(output_dir / 'temporal_trends.png')
        plt.close(fig)

    print(f"Plots saved to {output_dir}/")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(results: Dict[str, Any]) -> None:
    print("\n" + "=" * 60)
    print("COMPOSITIONALITY METRICS")
    print("=" * 60)

    ts = results['topographic_similarity']
    print(f"Topographic Similarity: rho = {ts['rho']:.4f} (p = {ts['p_value']:.2e})")
    print(f"  Interpretation: {ts['interpretation']}")
    print(f"  Pairs analyzed: {ts['n_pairs']}")

    rc = results['region_consistency']
    print(f"\nRegion Consistency:")
    print(f"  Mean entropy: {rc['mean_entropy']:.3f} / {rc['max_possible_entropy']:.3f} bits")
    print(f"  Consistency score: {rc['consistency_score']:.3f}")
    print(f"  Analyzed bins: {rc['n_analyzed_bins']} / {rc['n_occupied_bins']}")

    temporal = results.get('temporal', {})
    if temporal.get('windows'):
        print(f"\nTemporal Analysis ({len(temporal['windows'])} windows):")
        print(f"  TS trend: {temporal['ts_trend']:+.4f}/window")
        print(f"  Consistency trend: {temporal['consistency_trend']:+.4f}/window")
        print(f"  Improving: {temporal['improving']}")
    elif temporal.get('insufficient_data'):
        print("\nTemporal Analysis: insufficient data")

    print(f"\nData source: {results.get('data_source', 'unknown')}")
    print(f"Records: {results.get('n_records', '?')}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Compositionality Metrics')
    parser.add_argument('--db', type=str, default=None,
                        help='Path to anima.db')
    parser.add_argument('--synthetic', action='store_true',
                        help='Use synthetic data')
    parser.add_argument('--n-synthetic', type=int, default=500)
    parser.add_argument('--output-dir', '-o', type=str,
                        default='data/analysis/compositionality')
    parser.add_argument('--no-plot', action='store_true')
    parser.add_argument('--window-size', type=int, default=50)
    args = parser.parse_args()

    if args.synthetic or args.db is None:
        print("Using synthetic data...")
        records = generate_synthetic_data(n_samples=args.n_synthetic)
        data_source = 'synthetic'
    else:
        print(f"Loading from {args.db}...")
        records = load_primitive_history(Path(args.db))
        data_source = str(args.db)

    print(f"Loaded {len(records)} records")
    print("Computing distances...")

    md = compute_meaning_distances(records)
    sd = compute_signal_distances(records)
    ts = topographic_similarity(md, sd)
    rc = region_consistency(records)
    temporal = temporal_compositionality(records, window_size=args.window_size)

    results = {
        'topographic_similarity': ts,
        'region_consistency': rc,
        'temporal': temporal,
        'data_source': data_source,
        'n_records': len(records),
        'meaning_distances': md,
        'signal_distances': sd,
    }

    print_summary(results)

    if not args.no_plot:
        plot_compositionality(results, Path(args.output_dir))

    # Save results (without distance matrices)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    save_data = {k: v for k, v in results.items()
                 if k not in ('meaning_distances', 'signal_distances')}
    with open(out_dir / 'compositionality_results.json', 'w') as f:
        json.dump(save_data, f, indent=2, default=str)
    print(f"Results saved to {out_dir / 'compositionality_results.json'}")


if __name__ == '__main__':
    main()
