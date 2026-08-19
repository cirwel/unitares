"""Disclosure tests for the runtime-activity read model and the dialectic label set.

Both cover the same failure: a value read as a stronger claim than it is.
`epistemic_class` records who COMPOSED a check-in, and gets read as whether the
agent CHOSE to check in. An inferred reviewer label, written into the column a
reviewer would set, becomes indistinguishable from the reviewer's own verdict.

These pin the disclosure, not the behaviour — the read model already exposes
each epistemic class separately, which was always correct.
"""



class TestAgentReportIsNotVolition:
    """`agent_report` says who composed the text, never who chose the row.

    Measured 2026-08-19 over 30 days: 98.5% of `agent_report` came from
    scheduled residents (a timer firing into a script that composes its own
    status line), while ~92% of live session-agent activity is classified
    `substrate_interpretation` because the plugin's post-stop hook composes it.
    A consumer filtering on `agent_report` to answer "what did the agents do"
    gets almost exclusively cron output and misses almost all of the work.

    These tests pin the DISCLOSURE, not behaviour. The read model already
    exposes each class separately, which is correct; what regressed was the
    docstring calling `agent_report` "the only agent-authored check-ins".
    """

    def test_read_model_defines_the_class_by_composition(self):
        """Assert the CORRECTION is present, not that a phrase is absent.

        An absence test on prose is brittle — the docstring deliberately quotes
        the wording it replaced so the next reader knows what changed, which
        would trip a naive substring check.
        """
        import inspect
        import src.runtime_observations as ro

        doc = (inspect.getdoc(ro.summarize_runtime_activity) or "").lower()
        assert "composed by the agent process" in doc, (
            "the docstring must define agent_report by who COMPOSED the text"
        )

    def test_read_model_states_that_the_class_is_not_volition(self):
        import inspect
        import src.runtime_observations as ro

        doc = (inspect.getdoc(ro.summarize_runtime_activity) or "").lower()
        assert "is not" in doc and "chose to check in" in doc, (
            "the docstring must say outright that this class is not a record of "
            "the agent choosing to check in"
        )

    def test_read_model_carries_the_measured_split(self):
        """The numbers are the argument; a bare warning decays into folklore."""
        import inspect
        import src.runtime_observations as ro

        doc = inspect.getdoc(ro.summarize_runtime_activity) or ""
        assert "22057" in doc and "3791" in doc, (
            "the docstring must carry the measured producer split so a reader "
            "can see the size of the gap rather than take it on faith"
        )

    def test_module_docstring_names_the_ceiling(self):
        import src.runtime_observations as ro

        doc = (ro.__doc__ or "").lower()
        assert "composed" in doc, (
            "module docstring must state what epistemic_class can establish"
        )

    def test_both_classes_remain_separately_exposed(self):
        """The structural fix was already right — keep it that way.

        Collapsing these into one 'activity' number would re-create the blur
        the docstring now warns about.
        """
        import inspect
        import src.runtime_observations as ro

        src_text = inspect.getsource(ro)
        assert "'agent_report'" in src_text
        assert "'substrate_interpretation'" in src_text
        assert "agent_report_count" in src_text
        assert "interpretation_count" in src_text


class TestDialecticLabelSetIntegrity:
    """The labelled antithesis set is a derived artifact, not a backfill.

    Inferred labels written into `core.dialectic_messages.agrees` would be
    indistinguishable from a reviewer's own verdict. Every row therefore
    carries `source_of_truth: false` and an attributed labeller, and the file
    must stay internally consistent with the doc that quotes its counts.
    """

    def _rows(self):
        import json
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[1]
            / "docs/evaluation/dialectic-reviewer-labels-20260819.jsonl"
        )
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_every_row_disclaims_first_party_authority(self):
        rows = self._rows()
        assert rows, "label set is empty"
        for r in rows:
            assert r["source_of_truth"] is False, (
                f"message {r['message_id']}: an inferred label must never claim "
                "to be the reviewer's own verdict"
            )
            assert r["labeled_by"] and r["labeled_on"], (
                f"message {r['message_id']}: labels need an attributed author and date"
            )

    def test_message_ids_are_unique(self):
        rows = self._rows()
        ids = [r["message_id"] for r in rows]
        assert len(ids) == len(set(ids))

    def test_labels_use_the_documented_taxonomy(self):
        allowed = {
            "refutes_substantive", "concurs_with_conditions", "ratifies",
            "formulaic", "non_verdict",
        }
        for r in self._rows():
            assert r["label"] in allowed, f"undocumented label {r['label']!r}"

    def test_every_row_records_its_era(self):
        """The corpus straddles the 2026-07-02 reviewer-backend change.

        Without the era on each row, the only reproducible number is the pooled
        one, and the pooled one averages two different instruments.
        """
        for r in self._rows():
            assert r["era"] in {"pre_codex_reviewer", "post_codex_reviewer"}
            assert r["message_date"], f"message {r['message_id']} has no date"
            expected = (
                "post_codex_reviewer" if r["message_date"] >= "2026-07-02"
                else "pre_codex_reviewer"
            )
            assert r["era"] == expected, f"message {r['message_id']}: era/date disagree"

    def test_published_split_matches_the_data(self):
        """The doc quotes per-era counts; if the data moves, the doc must too."""
        from collections import Counter
        from pathlib import Path

        rows = self._rows()
        pre = Counter(r["label"] for r in rows if r["era"] == "pre_codex_reviewer")
        post = Counter(r["label"] for r in rows if r["era"] == "post_codex_reviewer")
        doc = (
            Path(__file__).resolve().parents[1]
            / "docs/evaluation/dialectic-reviewer-labels.md"
        ).read_text()

        assert f"pre 07-02 (n={sum(pre.values())})" in doc
        assert f"on/after 07-02 (n={sum(post.values())})" in doc
        for label in set(pre) | set(post):
            assert f"`{label}`" in doc
            assert f"{pre[label]} — " in doc and f"{post[label]} — " in doc, (
                f"published split is stale for {label}: "
                f"data says pre={pre[label]} post={post[label]}"
            )
