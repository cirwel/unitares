"""
EISV Metric Formatting Utilities

This module enforces complete EISV reporting to prevent selection bias.
Never report E, I, S without V - always show all four together.

Design principle: Make incomplete reporting impossible at the type level.

NOTE: Two EISV engines coexist. The ODE dynamics engine (governance_core)
runs as a diagnostic parallel to the behavioral EISV system. Behavioral
verdicts are the primary decision source (see governance_monitor.py).
The ODE's E, I, S, V values in EISVMetrics may differ from the behavioral
state's E, I, S, V (which are EMA-smoothed observations).
"""

from typing import Dict, NamedTuple
from dataclasses import dataclass


class EISVMetrics(NamedTuple):
    """
    Complete EISV metrics - cannot be partially constructed.

    Use this instead of dict to ensure all four metrics are always present.

    Note on V: In Lumen's sensor layer, V = (1 - presence) * 0.3 [0, 0.3].
    In governance dynamics, V is a signed integrator: dV/dt = kappa(E-I) - delta*V [-2, 2].
    The observation-layer V seeds the ODE, which then evolves independently.
    """
    E: float  # Energy or presence
    I: float  # Information integrity
    S: float  # Entropy (disorder/uncertainty)
    V: float  # Accumulated E-I imbalance (positive: E>I, negative: I>E)

    def validate(self) -> None:
        """Validate metric ranges."""
        if not (0.0 <= self.E <= 1.0):
            raise ValueError(f"E must be in [0, 1], got {self.E}")
        if not (0.0 <= self.I <= 1.0):
            raise ValueError(f"I must be in [0, 1], got {self.I}")
        if not (0.0 <= self.S <= 1.0):
            raise ValueError(f"S must be in [0, 1], got {self.S}")
        if not (-1.0 <= self.V <= 1.0):
            raise ValueError(f"V must be in [-1, 1], got {self.V}")


@dataclass
class EISVTrajectory:
    """Track EISV changes over time - always complete."""
    start: EISVMetrics
    end: EISVMetrics

    def deltas(self) -> EISVMetrics:
        """Calculate changes (always all four)."""
        return EISVMetrics(
            E=self.end.E - self.start.E,
            I=self.end.I - self.start.I,
            S=self.end.S - self.start.S,
            V=self.end.V - self.start.V
        )

    def percent_changes(self) -> Dict[str, float]:
        """Calculate percentage changes (always all four)."""
        deltas = self.deltas()
        return {
            'E': (deltas.E / self.start.E * 100) if self.start.E != 0 else 0,
            'I': (deltas.I / self.start.I * 100) if self.start.I != 0 else 0,
            'S': (deltas.S / self.start.S * 100) if self.start.S != 0 else 0,
            'V': (deltas.V / self.start.V * 100) if self.start.V != 0 else float('inf')
        }


def format_eisv_compact(metrics: EISVMetrics) -> str:
    """
    Format EISV in compact form - always all four.

    Example: "E=0.80 I=1.00 S=0.03 V=-0.07"

    Args:
        metrics: Complete EISV metrics (NamedTuple enforces completeness)

    Returns:
        Formatted string with all four metrics
    """
    return f"E={metrics.E:.6f} I={metrics.I:.6f} S={metrics.S:.6f} V={metrics.V:.6f}"


def format_eisv_detailed(
    metrics: EISVMetrics,
    include_labels: bool = True,
    include_user_friendly: bool = False
) -> str:
    """
    Format EISV in detailed form - always all four.

    Example:
        E (Energy): 0.80
        I (Integrity): 1.00
        S (Entropy): 0.03
        V (Void): -0.07

    Args:
        metrics: Complete EISV metrics
        include_labels: Include metric names
        include_user_friendly: Include user-friendly descriptions

    Returns:
        Multi-line formatted string with all four metrics
    """
    labels = {
        'E': 'Energy',
        'I': 'Integrity',
        'S': 'Entropy',
        'V': 'Void'
    }

    user_friendly = {
        'E': 'Productive capacity; couples toward I, dragged down by entropy',
        'I': 'Signal fidelity; boosted by coherence, reduced by entropy',
        'S': 'Semantic uncertainty; rises with complexity and drift, decays naturally',
        'V': 'Accumulated E-I imbalance; positive=running hot, negative=running careful'
    }

    lines = []
    for key in ['E', 'I', 'S', 'V']:  # Always this order, always all four
        value = getattr(metrics, key)
        if include_labels:
            label = f" ({labels[key]})" if include_labels else ""
            line = f"{key}{label}: {value:.6f}"
        else:
            line = f"{key}: {value:.6f}"

        if include_user_friendly:
            line += f"  # {user_friendly[key]}"

        lines.append(line)

    return '\n'.join(lines)


def format_eisv_trajectory(trajectory: EISVTrajectory) -> str:
    """
    Format EISV trajectory - always all four with changes.

    Example:
        E (Energy): 0.71 → 0.80 (+12.5%)
        I (Integrity): 0.84 → 1.00 (+19.3%)
        S (Entropy): 0.14 → 0.03 (-80.6%)
        V (Void): -0.01 → -0.07 (↓5.3x)

    Args:
        trajectory: Start and end EISV metrics

    Returns:
        Multi-line formatted string with all four metrics and changes
    """
    deltas = trajectory.deltas()
    percent = trajectory.percent_changes()

    labels = {'E': 'Energy', 'I': 'Integrity', 'S': 'Entropy', 'V': 'Void'}

    lines = []
    for key in ['E', 'I', 'S', 'V']:  # Always all four
        start_val = getattr(trajectory.start, key)
        end_val = getattr(trajectory.end, key)
        delta = getattr(deltas, key)
        pct = percent[key]

        # Format direction indicator
        if delta > 0:
            direction = "↑"
        elif delta < 0:
            direction = "↓"
        else:
            direction = "="

        # Format percentage change
        if abs(pct) == float('inf'):
            pct_str = "∞"
        elif key == 'V' and start_val != 0:
            # For V, show multiplier if it's clearer
            multiplier = abs(end_val / start_val)
            if multiplier > 2:
                pct_str = f"{direction}{multiplier:.1f}x"
            else:
                pct_str = f"({delta:+.2f})"
        else:
            pct_str = f"({pct:+.1f}%)"

        line = f"{key} ({labels[key]}): {start_val:.6f} → {end_val:.6f} {pct_str}"
        lines.append(line)

    return '\n'.join(lines)


def validate_eisv_complete(data: Dict) -> bool:
    """
    Validate that a dict contains all EISV metrics.

    Use this to check API responses, CSV rows, etc.

    Args:
        data: Dictionary that should contain E, I, S, V

    Returns:
        True if all four present, False otherwise

    Raises:
        ValueError: If any metric is missing (with clear error message)
    """
    required = {'E', 'I', 'S', 'V'}
    present = set(data.keys())
    missing = required - present

    if missing:
        raise ValueError(
            f"Incomplete EISV metrics. Missing: {missing}. "
            f"Always report all four (E, I, S, V) to prevent selection bias."
        )

    return True


def eisv_from_dict(data: Dict) -> EISVMetrics:
    """
    Convert dict to EISVMetrics, validating completeness.

    Args:
        data: Dict with 'E', 'I', 'S', 'V' keys

    Returns:
        EISVMetrics (validated complete)

    Raises:
        ValueError: If any metric is missing
    """
    validate_eisv_complete(data)
    metrics = EISVMetrics(
        E=float(data['E']),
        I=float(data['I']),
        S=float(data['S']),
        V=float(data['V'])
    )
    metrics.validate()
    return metrics


# Convenience function for most common use case
def format_eisv(
    metrics: EISVMetrics,
    style: str = 'compact',
    **kwargs
) -> str:
    """
    Format EISV metrics - always all four.

    Args:
        metrics: Complete EISV metrics
        style: 'compact' or 'detailed'
        **kwargs: Passed to detailed formatter if style='detailed'

    Returns:
        Formatted string with all four metrics
    """
    if style == 'compact':
        return format_eisv_compact(metrics)
    elif style == 'detailed':
        return format_eisv_detailed(metrics, **kwargs)
    else:
        raise ValueError(f"Unknown style: {style}. Use 'compact' or 'detailed'")


if __name__ == '__main__':
    # Example usage
    print("=== EISV Formatting Examples ===\n")

    # Create metrics (can't forget any - NamedTuple enforces it)
    start = EISVMetrics(E=0.71, I=0.84, S=0.14, V=-0.01)
    end = EISVMetrics(E=0.80, I=1.00, S=0.03, V=-0.07)

    print("1. Compact format:")
    print(format_eisv_compact(end))
    print()

    print("2. Detailed format:")
    print(format_eisv_detailed(end, include_labels=True))
    print()

    print("3. Trajectory with changes:")
    trajectory = EISVTrajectory(start=start, end=end)
    print(format_eisv_trajectory(trajectory))
    print()

    print("4. Validation catches incomplete data:")
    try:
        incomplete = {'E': 0.8, 'I': 1.0, 'S': 0.03}  # Missing V!
        validate_eisv_complete(incomplete)
    except ValueError as e:
        print(f"✓ Caught incomplete metrics: {e}")
