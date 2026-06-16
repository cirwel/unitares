"""graph_query wrapper reliability — the AGE-canonical read-path gate.

These pin the wrapper-level behaviour that a council/live-diagnosis session
established about `src/db/mixins/graph.py`:

  * AGE itself is deterministic/correct at the psql layer; the only real
    defects were in the Python wrapper.
  * The single confirmed flakiness was a *multi-column* RETURN against the
    hardcoded single `result agtype` output column — surface it as a clear
    error and steer callers to the `RETURN {map}` convention instead.
  * agtype text embeds `::vertex`/`::edge` type suffixes INSIDE maps/lists,
    which the old trailing-only strip could not decode.
  * `${param}` interpolation must be single-pass so a sanitized value that
    contains a `${otherkey}` sequence can't be re-interpolated.
  * DDL (`create_graph`/`drop_graph`) must NEVER run on a caller-owned
    transaction connection (it would break dual-write atomicity).

All strings below are the actual agtype text captured live from the
`governance_graph` AGE graph, so the decode is pinned against reality.
"""

import pytest

from src.db.mixins.graph import GraphMixin


class _Graph(GraphMixin):
    """Bare mixin host with just the attributes the unit under test reads."""

    def __init__(self, age_graph: str = "test_graph"):
        self._age_graph = age_graph


class _FakeConn:
    """Minimal asyncpg-conn stand-in for _ensure_age_graph_exists."""

    def __init__(self, graph_exists: bool, *, fetchval_raises: bool = False):
        self._graph_exists = graph_exists
        self._fetchval_raises = fetchval_raises
        self.executed: list = []

    async def fetchval(self, *args):
        if self._fetchval_raises:
            raise AssertionError("fetchval should not be called (existence cached)")
        return self._graph_exists

    async def execute(self, *args):
        self.executed.append(args)


# --------------------------------------------------------------------------
# agtype decode (_decode_agtype_value)
# --------------------------------------------------------------------------

def test_decode_scalar_count():
    assert GraphMixin._decode_agtype_value("125") == 125


def test_decode_string_value():
    # `RETURN d.id AS id` came back as a quoted agtype string.
    assert GraphMixin._decode_agtype_value('"2025-11-28T04:10:24.346843"') == (
        "2025-11-28T04:10:24.346843"
    )


def test_decode_reachability_map():
    # The PR2 reachability shape: pure-string map, no nested vertex.
    raw = '{"ancestor": "da300b4a", "descendant": "0bc5ce0c"}'
    assert GraphMixin._decode_agtype_value(raw) == {
        "ancestor": "da300b4a",
        "descendant": "0bc5ce0c",
    }


def test_decode_map_with_embedded_vertex_suffix():
    # get_response_chain's `RETURN {node: d, depth: length(p)}` embeds a
    # `::vertex` suffix INSIDE the map — the old trailing-only strip failed here.
    raw = (
        '{"node": {"id": 844424930131969, "label": "Discovery", '
        '"properties": {"id": "2025-11-28T04:10:24", "status": "archived"}}'
        '::vertex, "depth": 0}'
    )
    decoded = GraphMixin._decode_agtype_value(raw)
    assert isinstance(decoded, dict)
    assert decoded["depth"] == 0
    assert decoded["node"]["properties"]["status"] == "archived"
    assert decoded["node"]["label"] == "Discovery"


def test_decode_bare_vertex_trailing_suffix():
    raw = '{"id": 1, "label": "Agent", "properties": {"id": "x"}}::vertex'
    decoded = GraphMixin._decode_agtype_value(raw)
    assert decoded["properties"]["id"] == "x"


def test_decode_does_not_strip_suffix_inside_string_value():
    # A property string that literally contains '::vertex' must survive — the
    # lookbehind only strips after a closing brace/bracket, not inside quotes.
    raw = '{"summary": "the foo::vertex pattern", "n": 1}'
    decoded = GraphMixin._decode_agtype_value(raw)
    assert decoded["summary"] == "the foo::vertex pattern"


def test_decode_none_passthrough():
    assert GraphMixin._decode_agtype_value(None) is None


def test_decode_unparseable_returns_raw():
    assert GraphMixin._decode_agtype_value("not json {") == "not json {"


# --------------------------------------------------------------------------
# single-pass param interpolation (_interpolate_params)
# --------------------------------------------------------------------------

def test_interpolate_basic():
    g = _Graph()
    out = g._interpolate_params(
        "MATCH (a:Agent {id: ${aid}}) RETURN a", {"aid": "abc"}
    )
    assert out == "MATCH (a:Agent {id: 'abc'}) RETURN a"


def test_interpolate_no_params_noop():
    g = _Graph()
    assert g._interpolate_params("RETURN 1", None) == "RETURN 1"
    assert g._interpolate_params("RETURN 1", {}) == "RETURN 1"


def test_interpolate_single_pass_no_recursive_substitution():
    # A value that itself contains another key's `${...}` placeholder must NOT
    # be re-interpolated on the later key's pass.
    g = _Graph()
    out = g._interpolate_params(
        "RETURN ${a}, ${b}", {"a": "${b}", "b": "real"}
    )
    # ${a} -> '${b}' (literal, single-quoted, sanitized), ${b} -> 'real'.
    # The injected ${b} text must stay literal, not become 'real'.
    assert out == "RETURN '${b}', 'real'"


def test_interpolate_escapes_quotes():
    g = _Graph()
    out = g._interpolate_params("RETURN ${v}", {"v": "O'Brien"})
    assert out == "RETURN 'O\\'Brien'"


# --------------------------------------------------------------------------
# column-arity error detection (_is_column_arity_error)
# --------------------------------------------------------------------------

def test_arity_error_detected():
    err = Exception("return row and column definition list do not match")
    assert GraphMixin._is_column_arity_error(err) is True


def test_non_arity_error_not_flagged():
    assert GraphMixin._is_column_arity_error(Exception("syntax error at or near")) is False


# --------------------------------------------------------------------------
# DDL never runs on a caller-owned transaction connection
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_graph_external_conn_missing_raises_not_creates():
    g = _Graph()
    conn = _FakeConn(graph_exists=False)
    with pytest.raises(RuntimeError):
        await g._ensure_age_graph_exists(conn, external_conn=True)
    # Critically, it did NOT issue create_graph DDL on the transaction conn.
    assert conn.executed == []
    assert g._graph_exists_confirmed is False


@pytest.mark.asyncio
async def test_ensure_graph_pooled_conn_missing_creates():
    g = _Graph()
    conn = _FakeConn(graph_exists=False)
    await g._ensure_age_graph_exists(conn, external_conn=False)
    assert any("create_graph" in str(a) for a in conn.executed)
    assert g._graph_exists_confirmed is True


@pytest.mark.asyncio
async def test_ensure_graph_present_marks_confirmed():
    g = _Graph()
    conn = _FakeConn(graph_exists=True)
    await g._ensure_age_graph_exists(conn, external_conn=True)
    assert g._graph_exists_confirmed is True


@pytest.mark.asyncio
async def test_ensure_graph_confirmed_short_circuits():
    g = _Graph()
    g._graph_exists_confirmed = True
    # fetchval raising proves we never hit the DB once existence is confirmed.
    conn = _FakeConn(graph_exists=True, fetchval_raises=True)
    await g._ensure_age_graph_exists(conn, external_conn=True)
