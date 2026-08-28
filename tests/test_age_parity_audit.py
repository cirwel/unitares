"""Regression tests for scripts/dev/age_parity_audit.py.

The audit's only DB-free logic is how it reads a single-value Cypher result.
That reader is worth pinning because getting it wrong is silent: the previous
version could only ever report 0 for the RESPONDS_TO edge count, which reads
as "dead capability" whether or not any edge exists.
"""

from scripts.dev import age_parity_audit as audit


class TestScalarReader:
    """`graph_query` returns [decoded_value_per_row], not [{alias: value}]."""

    def test_reads_a_bare_scalar_row(self):
        # RETURN count(d) AS n decodes to a bare int; the alias does not survive.
        assert audit._scalar([7], "n") == 7

    def test_reads_a_mapped_row(self):
        # RETURN {n: count(d)} projects a map, which decodes to a dict.
        assert audit._scalar([{"n": 7}], "n") == 7

    def test_zero_is_reported_as_zero_not_as_absence(self):
        assert audit._scalar([0], "n") == 0

    def test_no_rows_is_none_not_zero(self):
        # The distinction the original conflated: "AGE returned nothing" is not
        # "there are no edges". Returning 0 here manufactures a finding.
        assert audit._scalar([], "n") is None

    def test_missing_key_in_a_mapped_row_is_none(self):
        assert audit._scalar([{"other": 1}], "n") is None

    def test_a_scalar_row_is_not_read_as_absent(self):
        # The specific regression: an isinstance(row, dict) guard sends every
        # scalar return down the "no data" path.
        assert audit._scalar([3], "n") is not None
