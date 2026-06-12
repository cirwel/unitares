#!/usr/bin/env python3
"""
EISV Trajectory Plotter

Standalone script (no server dependency) that visualizes EISV dynamics
under three scenarios: convergence, degradation, and recovery.

Usage:
    python scripts/analysis/plot_eisv_trajectories.py

Output:
    scripts/analysis/eisv_trajectories.png
"""

import sys
import os

# Add project root to path so governance_core is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import matplotlib.pyplot as plt
import numpy as np

from governance_core.dynamics import State, compute_dynamics
from governance_core.parameters import DEFAULT_THETA, get_active_params
from governance_core.scoring import phi_objective


def run_scenario(state, delta_eta, steps, complexity=0.5, dt=0.1):
    """Run dynamics and return arrays of E, I, S, V, phi over time."""
    params = get_active_params()
    theta = DEFAULT_THETA

    E, I, S, V, phi = [state.E], [state.I], [state.S], [state.V], []
    phi.append(phi_objective(state, delta_eta))

    for _ in range(steps):
        state = compute_dynamics(
            state=state, delta_eta=delta_eta, theta=theta,
            params=params, dt=dt, complexity=complexity,
        )
        E.append(state.E)
        I.append(state.I)
        S.append(state.S)
        V.append(state.V)
        phi.append(phi_objective(state, delta_eta))

    t = np.arange(len(E)) * dt
    return t, np.array(E), np.array(I), np.array(S), np.array(V), np.array(phi)


def plot_scenario(axes, t, E, I, S, V, phi, title):
    """Plot E, I, S, V on 4 subplots for one scenario."""
    labels = ["E (Energy)", "I (Integrity)", "S (Entropy)", "V (Void)"]
    data = [E, I, S, V]
    colors = ["#2196F3", "#4CAF50", "#FF9800", "#9C27B0"]

    for ax, y, label, color in zip(axes, data, labels, colors):
        ax.plot(t, y, color=color, linewidth=1.5)
        ax.set_ylabel(label, fontsize=9)
        ax.set_ylim(min(-0.5, y.min() - 0.1), max(1.1, y.max() + 0.1))
        ax.grid(True, alpha=0.3)
        ax.tick_params(labelsize=8)

    axes[0].set_title(title, fontsize=11, fontweight="bold")
    axes[-1].set_xlabel("Time", fontsize=9)


def main():
    fig, all_axes = plt.subplots(4, 3, figsize=(14, 10), sharex="col")
    fig.suptitle("EISV Dynamics — Three Scenarios", fontsize=14, fontweight="bold")

    # Scenario 1: Convergence from default
    t, E, I, S, V, phi = run_scenario(
        State(E=0.7, I=0.8, S=0.2, V=0.0),
        delta_eta=[0.0], steps=200,
    )
    plot_scenario(all_axes[:, 0], t, E, I, S, V, phi, "Convergence (default)")

    # Scenario 2: Degradation under stress
    t, E, I, S, V, phi = run_scenario(
        State(E=0.7, I=0.8, S=0.2, V=0.0),
        delta_eta=[0.5, 0.5], steps=150, complexity=0.9,
    )
    plot_scenario(all_axes[:, 1], t, E, I, S, V, phi, "Degradation (high drift + complexity)")

    # Scenario 3: Recovery from degraded
    t, E, I, S, V, phi = run_scenario(
        State(E=0.4, I=0.3, S=0.8, V=-0.3),
        delta_eta=[0.0], steps=200,
    )
    plot_scenario(all_axes[:, 2], t, E, I, S, V, phi, "Recovery (zero drift)")

    plt.tight_layout()

    # Phase portrait bonus: E vs I colored by time
    fig2, phase_axes = plt.subplots(1, 3, figsize=(14, 4))
    fig2.suptitle("Phase Portrait: E vs I", fontsize=14, fontweight="bold")

    scenarios = [
        ("Convergence", State(E=0.7, I=0.8, S=0.2, V=0.0), [0.0], 200, 0.5),
        ("Degradation", State(E=0.7, I=0.8, S=0.2, V=0.0), [0.5, 0.5], 150, 0.9),
        ("Recovery", State(E=0.4, I=0.3, S=0.8, V=-0.3), [0.0], 200, 0.5),
    ]

    for ax, (name, s0, de, steps, cx) in zip(phase_axes, scenarios):
        t, E, I, S, V, phi = run_scenario(s0, de, steps, cx)
        scatter = ax.scatter(E, I, c=t, cmap="viridis", s=3, alpha=0.7)
        ax.plot(E[0], I[0], "ro", markersize=8, label="start")
        ax.plot(E[-1], I[-1], "gs", markersize=8, label="end")
        ax.set_xlabel("E (Energy)")
        ax.set_ylabel("I (Integrity)")
        ax.set_title(name, fontsize=10)
        ax.set_xlim(-0.05, 1.05)
        ax.set_ylim(-0.05, 1.05)
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)
        plt.colorbar(scatter, ax=ax, label="Time", shrink=0.8)

    fig2.tight_layout()

    # Save
    out_dir = os.path.dirname(__file__)
    path1 = os.path.join(out_dir, "eisv_trajectories.png")
    path2 = os.path.join(out_dir, "eisv_phase_portrait.png")
    fig.savefig(path1, dpi=150, bbox_inches="tight")
    fig2.savefig(path2, dpi=150, bbox_inches="tight")
    print(f"Saved: {path1}")
    print(f"Saved: {path2}")
    plt.close("all")


if __name__ == "__main__":
    main()
