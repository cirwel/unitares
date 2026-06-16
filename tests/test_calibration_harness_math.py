"""Unit tests for the calibration harness's pure math.

Guards the ECE / AUC / injected-miscalibration computation the council
scrutinized (scripts/dev/calibration_harness, PR #770). No server needed — these
are pure functions.
"""
from __future__ import annotations

import math

import pytest

from scripts.dev.calibration_harness.miscalibration import (
    _folded_normal_mean,
    expected_recovered_ece,
    injected_ece,
    true_accuracy,
)
from scripts.dev.calibration_harness.report import Pair, compute_auc, compute_ece

BINS = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0)]


# --- true_accuracy / injected_ece ------------------------------------------

def test_true_accuracy_subtracts_gap_and_clamps():
    assert true_accuracy(0.5, 0.2) == pytest.approx(0.3)
    assert true_accuracy(0.9, 0.0) == pytest.approx(0.9)
    assert true_accuracy(0.1, 0.2) == 0.0   # clamped at 0
    assert true_accuracy(1.5, 0.0) == 1.0   # clamped at 1


def test_injected_ece_is_mean_abs_gap():
    # |c - (c-gap)| = gap where the clamp is inactive
    assert injected_ece([0.5, 0.7, 0.9], 0.2) == pytest.approx(0.2)
    # clamp active: true_accuracy(0.1,0.2)=0 -> |0.1-0|=0.1
    assert injected_ece([0.1], 0.2) == pytest.approx(0.1)
    assert injected_ece([], 0.2) == 0.0


# --- folded normal ----------------------------------------------------------

def test_folded_normal_mean_zero_mu():
    # E|N(0, s^2)| = s * sqrt(2/pi)
    assert _folded_normal_mean(0.0, 0.1) == pytest.approx(0.1 * math.sqrt(2 / math.pi))


def test_folded_normal_mean_zero_sigma_is_abs_mu():
    assert _folded_normal_mean(-0.3, 0.0) == pytest.approx(0.3)
    assert _folded_normal_mean(0.25, 0.0) == pytest.approx(0.25)


def test_folded_normal_mean_large_mu_approaches_abs_mu():
    # when mu >> sigma the folded mean ~ |mu|
    assert _folded_normal_mean(1.0, 0.01) == pytest.approx(1.0, abs=1e-3)


# --- expected_recovered_ece (bias-aware target) -----------------------------

def test_expected_ece_is_at_least_injected_and_converges():
    confs = [i / 1000 for i in range(50, 1000)]  # dense, spread across bins
    inj = injected_ece(confs, 0.3)
    exp = expected_recovered_ece(confs, 0.3, BINS)
    # bias is non-negative, and with many samples per bin it is small
    assert exp >= inj - 1e-9
    assert exp == pytest.approx(inj, abs=0.02)


def test_expected_ece_has_positive_floor_at_gap_zero():
    # at perfect calibration injected ECE is 0 but a finite-n estimator is > 0
    confs = [i / 200 for i in range(10, 200)]  # ~95 samples
    assert injected_ece(confs, 0.0) == pytest.approx(0.0)
    assert expected_recovered_ece(confs, 0.0, BINS) > 0.01


# --- compute_ece ------------------------------------------------------------

def test_compute_ece_single_bin_known_value():
    # 4 rows in bin 0.8-1.0, conf 0.9, half succeed -> accuracy 0.5, gap 0.4
    pairs = [Pair(0.9, True), Pair(0.9, True), Pair(0.9, False), Pair(0.9, False)]
    ece, table = compute_ece(pairs)
    assert ece == pytest.approx(0.4)
    last = [r for r in table if r["bin"] == "0.8-1.0"][0]
    assert last["accuracy"] == pytest.approx(0.5)
    assert last["mean_conf"] == pytest.approx(0.9)


def test_compute_ece_empty_is_zero_no_crash():
    ece, table = compute_ece([])
    assert ece == 0.0
    assert all(r["count"] == 0 for r in table)


def test_compute_ece_bin_boundary_half_open_and_one_inclusive():
    # 0.2 lands in [0.2,0.4), 1.0 lands in the last bin (hi==1.0 inclusive)
    pairs = [Pair(0.2, True), Pair(1.0, True)]
    _, table = compute_ece(pairs)
    by_bin = {r["bin"]: r["count"] for r in table}
    assert by_bin["0.2-0.4"] == 1
    assert by_bin["0.0-0.2"] == 0  # 0.2 is NOT in [0.0,0.2)
    assert by_bin["0.8-1.0"] == 1  # 1.0 included


# --- compute_auc ------------------------------------------------------------

def test_compute_auc_perfect_separation():
    pairs = [Pair(0.1, False), Pair(0.2, False), Pair(0.8, True), Pair(0.9, True)]
    assert compute_auc(pairs) == pytest.approx(1.0)


def test_compute_auc_inverted_separation():
    # successes at LOW confidence -> worst discrimination
    pairs = [Pair(0.1, True), Pair(0.2, True), Pair(0.8, False), Pair(0.9, False)]
    assert compute_auc(pairs) == pytest.approx(0.0)


def test_compute_auc_one_class_is_none():
    assert compute_auc([Pair(0.5, True), Pair(0.6, True)]) is None
    assert compute_auc([Pair(0.5, False)]) is None


def test_compute_auc_ties_average_to_half():
    # identical confidences, one of each class -> chance discrimination
    pairs = [Pair(0.5, True), Pair(0.5, False)]
    assert compute_auc(pairs) == pytest.approx(0.5)
