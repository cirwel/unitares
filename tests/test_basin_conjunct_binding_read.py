from scripts.analysis import basin_conjunct_binding_read as read


def test_recomputation_uses_only_complete_classifier_inputs():
    for field in ("i", "st", "v", "coh"):
        assert f"{field} IS NOT NULL" in read._ROWS_CTE

    for query in (
        read.Q_AGREEMENT,
        read.Q_E_MARGIN,
        read.Q_FAILING_SETS,
        read.Q_SUB_BY_BASIN,
        read.Q_SHOWN_NOT_DECIDING,
    ):
        assert "FROM complete" in query


def test_completeness_query_reports_null_fallthrough_inputs():
    assert "FROM eligible" in read.Q_COMPLETENESS
    assert "missing_coherence" in read.Q_COMPLETENESS
    assert "coh IS NULL" in read.Q_COMPLETENESS


def test_observed_input_read_does_not_claim_a_counterfactual():
    assert "does **not** answer" in read.__doc__
    assert "cannot estimate a recursive counterfactual" in read.__doc__
