"""Unit check for the fixed-window rate scaling (the rest is live I/O)."""
from scripts.analysis.eisv_latent_label_supply import _to_qtr


def test_window_scaling_to_quarter():
    assert _to_qtr(90, 90) == 90          # a 90d count is already per-quarter
    assert _to_qtr(45, 45) == 90          # short window scales up
    assert _to_qtr(180, 180) == 90        # long window scales down
    assert _to_qtr(0, 90) == 0
    assert _to_qtr(7, 0) == _to_qtr(7, 1)  # guards divide-by-zero
