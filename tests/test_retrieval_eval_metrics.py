"""Unit tests for the KG retrieval-eval scoring math (`scripts/eval/metrics.py`).

The eval harness is the tool used to decide whether a ranking change is an
improvement (the 2026-06-13 "on-point hit ranked 4th at ~0.13 similarity"
friction is exactly the kind of regression it should catch). That decision is
only as trustworthy as the metrics underneath it, which previously ran only
against a live backend and had no direct coverage. These tests pin the pure
math so a refactor can't silently corrupt every baseline.
"""

import math

import pytest

from scripts.eval.metrics import dcg, mrr, ndcg_at_k, recall_at_k


class TestDCG:
    def test_empty_is_zero(self):
        assert dcg([], 10) == 0.0

    def test_single_relevant_at_rank1_no_discount(self):
        # rank 0 → 1 / log2(2) = 1.0
        assert dcg([1], 10) == 1.0

    def test_discount_follows_log2_of_position_plus_2(self):
        # ranks 0,1,2 → 1/log2(2) + 1/log2(3) + 1/log2(4)
        expected = 1 / math.log2(2) + 1 / math.log2(3) + 1 / math.log2(4)
        assert math.isclose(dcg([1, 1, 1], 10), expected)

    def test_k_truncates_the_gain_list(self):
        # only the first 2 positions count even though 3 are relevant
        assert math.isclose(dcg([1, 1, 1], 2), 1 / math.log2(2) + 1 / math.log2(3))

    def test_zero_gains_contribute_nothing(self):
        # a relevant doc at rank 3 (index 2) only
        assert math.isclose(dcg([0, 0, 1], 10), 1 / math.log2(4))


class TestNDCG:
    def test_perfect_ranking_is_one(self):
        ranked = ["a", "b", "c", "x", "y"]
        assert ndcg_at_k(ranked, {"a", "b", "c"}, 10) == 1.0

    def test_no_relevant_docs_is_zero(self):
        assert ndcg_at_k(["a", "b"], set(), 10) == 0.0

    def test_no_hit_in_topk_is_zero(self):
        assert ndcg_at_k(["x", "y", "z"], {"a"}, 10) == 0.0

    def test_is_normalized_between_zero_and_one(self):
        # one relevant doc buried at rank 4 (index 3)
        score = ndcg_at_k(["x", "y", "z", "a"], {"a"}, 10)
        assert 0.0 < score < 1.0
        assert math.isclose(score, (1 / math.log2(5)) / 1.0)

    def test_earlier_hit_scores_higher_than_later_hit(self):
        early = ndcg_at_k(["a", "x", "y", "z"], {"a"}, 10)
        late = ndcg_at_k(["x", "y", "z", "a"], {"a"}, 10)
        assert early > late

    def test_this_is_the_2026_06_13_friction_signal(self):
        # The logged complaint: the on-point hit landed at rank 4 instead of 1.
        # nDCG must visibly penalize that vs. the ideal, so a ranking fix that
        # lifts it to rank 1 registers as an improvement.
        relevant = {"on_point"}
        rank4 = ndcg_at_k(["noise1", "noise2", "noise3", "on_point"], relevant, 10)
        rank1 = ndcg_at_k(["on_point", "noise1", "noise2", "noise3"], relevant, 10)
        assert rank1 == 1.0
        assert rank4 < rank1

    def test_ideal_capped_at_k_when_more_relevant_than_k(self):
        # 3 relevant docs but k=2: ideal-DCG uses only the top 2 slots, so two
        # relevant docs in the top 2 is still a perfect score at k=2.
        ranked = ["a", "b", "c", "x"]
        assert ndcg_at_k(ranked, {"a", "b", "c"}, 2) == 1.0

    def test_partial_hit_below_perfect(self):
        # 2 relevant; only one is in the top-2 → below 1.0
        score = ndcg_at_k(["a", "x", "b"], {"a", "b"}, 2)
        assert score < 1.0


class TestRecall:
    def test_all_relevant_found(self):
        assert recall_at_k(["a", "b", "c"], {"a", "b"}, 3) == 1.0

    def test_half_found(self):
        assert recall_at_k(["a", "x", "y"], {"a", "b"}, 3) == 0.5

    def test_no_relevant_docs_is_zero_not_div_by_zero(self):
        assert recall_at_k(["a", "b"], set(), 5) == 0.0

    def test_k_bounds_the_window(self):
        # relevant doc sits at rank 3 but k=2 → not counted
        assert recall_at_k(["x", "y", "a"], {"a"}, 2) == 0.0

    def test_relevant_just_inside_window_counts(self):
        assert recall_at_k(["x", "y", "a"], {"a"}, 3) == 1.0


class TestMRR:
    def test_first_position_is_one(self):
        assert mrr(["a", "b", "c"], {"a"}) == 1.0

    def test_reciprocal_of_first_hit_rank(self):
        assert mrr(["x", "y", "a"], {"a"}) == pytest.approx(1 / 3)

    def test_no_hit_is_zero(self):
        assert mrr(["x", "y", "z"], {"a"}) == 0.0

    def test_uses_earliest_of_several_hits(self):
        # both "b" and "d" are relevant; first hit is "b" at rank 2
        assert mrr(["a", "b", "c", "d"], {"b", "d"}) == pytest.approx(1 / 2)

    def test_accepts_any_iterable(self):
        assert mrr(iter(["x", "a"]), {"a"}) == pytest.approx(1 / 2)
