"""The Wave 3 competing-writer inventory must match the code, in both runtimes.

The gate defines its competing-writer set by rule: every code path that writes
``status``, ``phase``, ``reviewer_agent_id`` or ``awaiting_facilitation`` of
``core.dialectic_sessions``, or its sweep eligibility (``updated_at``, which
every persisted message bumps). ``scripts/ops/wave3_writer_inventory.py`` is that
set. This test scans ``src/`` and ``elixir/lease_plane/lib`` and fails in both
directions: a writer the inventory does not list (a new fallback nobody
counted), and an inventory entry whose writer is gone (a list that has drifted
into fiction).
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "wave3_writer_inventory", REPO_ROOT / "scripts" / "ops" / "wave3_writer_inventory.py"
)
inventory = importlib.util.module_from_spec(SPEC)
sys.modules["wave3_writer_inventory"] = inventory
SPEC.loader.exec_module(inventory)

SESSION_WRITE_RE = re.compile(
    r"\b(UPDATE\s+core\.dialectic_sessions|INSERT\s+INTO\s+core\.dialectic_sessions)\b",
    re.IGNORECASE,
)
ELIXIR_LIB = REPO_ROOT / "elixir" / "lease_plane" / "lib"


def _sets_a_coordination_column(sql: str) -> bool:
    """True for an INSERT, or an UPDATE whose SET list names a rule column."""
    if re.search(r"INSERT\s+INTO\s+core\.dialectic_sessions", sql, re.IGNORECASE):
        return True
    m = re.search(r"\bSET\b(.*?)(\bWHERE\b|\bRETURNING\b|$)", sql, re.IGNORECASE | re.DOTALL)
    if not m:
        return True  # unparseable: treat as a writer so it must be listed
    assigned = {a.split("=")[0].strip().lower() for a in m.group(1).split(",") if "=" in a}
    return bool(assigned & set(inventory.RULE_COLUMNS))


def _rel(path: pathlib.Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def _python_scan():
    """(sql_writers, non_writers, call_sites) found in src/."""
    sql_writers, non_writers, call_sites = set(), set(), {}
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = _rel(path)
        alias = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and (
                "dialectic_db" in node.module or "beam_resolve_client" in node.module
            ):
                for a in node.names:
                    if a.name in inventory.PY_WRITER_HELPERS:
                        alias[a.asname or a.name] = a.name

        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.stack = []

            def _scope(self, node):
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope

            def visit_Constant(self, node):
                if isinstance(node.value, str) and SESSION_WRITE_RE.search(node.value):
                    key = (rel, ".".join(self.stack))
                    (sql_writers if _sets_a_coordination_column(node.value)
                     else non_writers).add(key)

            def visit_Call(self, node):
                if isinstance(node.func, ast.Name) and node.func.id in alias:
                    key = (rel, ".".join(self.stack))
                    call_sites.setdefault(key, set()).add(alias[node.func.id])
                self.generic_visit(node)

        Visitor().visit(tree)
    return sql_writers, non_writers, call_sites


def _elixir_scan():
    """(sql_writers, call_sites) found in the lease plane."""
    sql_writers, call_sites = set(), {}
    scope_re = re.compile(r'^\s*(?:(defp?)\s+([a-z_0-9?!]+)|(post|get|put|delete)\s+"([^"]+)")')
    call_re = re.compile(
        r"DialecticSaga\.(%s)\(" % "|".join(inventory.EX_WRITER_FUNCTIONS)
    )
    for path in sorted(ELIXIR_LIB.rglob("*.ex")):
        rel = _rel(path)
        text = path.read_text(encoding="utf-8")
        scope = None
        # SQL lives in heredocs; attribute each statement to the def it is in.
        for match in re.finditer(r'"""(.*?)"""', text, re.DOTALL):
            body = match.group(1)
            if SESSION_WRITE_RE.search(body) and _sets_a_coordination_column(body):
                before = text[: match.start()].splitlines()
                for line in reversed(before):
                    m = scope_re.match(line)
                    if m:
                        sql_writers.add((rel, m.group(2) or f"{m.group(3).upper()} {m.group(4)}"))
                        break
        for line in text.splitlines():
            m = scope_re.match(line)
            if m:
                scope = m.group(2) or f"{m.group(3).upper()} {m.group(4)}"
            stripped = line.strip()
            if stripped.startswith("#") or "`" in line:
                continue  # prose that names the function, not a call
            c = call_re.search(line)
            if c and not path.name == "dialectic_saga.ex":
                call_sites.setdefault((rel, scope), set()).add(c.group(1))
    return sql_writers, call_sites


def test_every_sql_writer_is_inventoried_and_every_entry_exists():
    py_sql, _non, _calls = _python_scan()
    ex_sql, _ex_calls = _elixir_scan()
    found = py_sql | ex_sql
    listed = set(inventory.SQL_WRITERS)

    unlisted = found - listed
    assert not unlisted, (
        f"session-row writers not in the Wave 3 inventory: {sorted(unlisted)}. "
        "Add them to scripts/ops/wave3_writer_inventory.py SQL_WRITERS with the "
        "trace they leave, and to the collision report if they leave one."
    )
    stale = listed - found
    assert not stale, f"inventory lists writers the code no longer has: {sorted(stale)}"


def test_every_session_update_is_a_writer_by_the_rule():
    """Every UPDATE of the row moves updated_at, the sweeper's staleness clock,
    so none is exempt -- a message insert included."""
    sql, non_writers, _calls = _python_scan()
    assert non_writers == set()
    assert (inventory.DIALECTIC_DB, "DialecticDB.add_message") in sql


def test_every_python_call_site_is_inventoried():
    _sql, _non, calls = _python_scan()
    listed = {k: set(v["calls"]) for k, v in inventory.CALL_SITES.items()
              if k[0].startswith("src/")}
    # The helpers' own definitions are the SQL layer, not call sites.
    calls = {k: v for k, v in calls.items() if k[0] not in (
        "src/dialectic_db.py", "src/mcp_handlers/dialectic/beam_resolve_client.py")}

    unlisted = {k: sorted(v - listed.get(k, set())) for k, v in calls.items()
                if v - listed.get(k, set())}
    assert not unlisted, (
        f"code paths that write a session row but are not in the inventory: {unlisted}"
    )
    stale = {k: sorted(v - calls.get(k, set())) for k, v in listed.items()
             if v - calls.get(k, set())}
    assert not stale, f"inventory call sites that no longer make those calls: {stale}"


def test_every_elixir_call_site_is_inventoried():
    _sql, calls = _elixir_scan()
    listed = {k: set(v["calls"]) for k, v in inventory.CALL_SITES.items()
              if k[0].startswith("elixir/")}
    assert calls == listed, (
        f"BEAM callers of DialecticSaga writers differ from the inventory.\n"
        f"  code:      {sorted((k, sorted(v)) for k, v in calls.items())}\n"
        f"  inventory: {sorted((k, sorted(v)) for k, v in listed.items())}"
    )


def test_the_scanner_would_catch_a_new_writer():
    """Guard the guard: the column rule recognises each shape it must."""
    assert _sets_a_coordination_column(
        "UPDATE core.dialectic_sessions SET reviewer_agent_id = $1 WHERE session_id = $2")
    assert _sets_a_coordination_column(
        "UPDATE core.dialectic_sessions SET status = $1, phase = $1, updated_at = now()")
    assert _sets_a_coordination_column("INSERT INTO core.dialectic_sessions (session_id) VALUES ($1)")
    # Sweep eligibility is in the rule: a bare updated_at bump is a writer.
    assert _sets_a_coordination_column(
        "UPDATE core.dialectic_sessions SET updated_at = now() WHERE session_id = $1")
    assert not _sets_a_coordination_column(
        "UPDATE core.dialectic_sessions SET topic = $1 WHERE session_id = $2")


def _functions(path: pathlib.Path):
    """Top-level and method function nodes of a module, by qualified name."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{node.name}.{sub.name}"] = sub
    return out


def _records_a_pair(fn: ast.AST) -> bool:
    """The function enters `record_session_write(...)` as an async context."""
    for node in ast.walk(fn):
        if isinstance(node, ast.AsyncWith):
            for item in node.items:
                call = item.context_expr
                if isinstance(call, ast.Call) and getattr(call.func, "id", None) == \
                        "record_session_write":
                    return True
    return False


def test_every_python_sql_writer_is_reachable_only_through_a_recording_helper():
    """The gate's observation rule, enforced: each DialecticDB writer method is
    called only from its `wrapped_by` helper, and that helper records the
    attempt/response pair around the call."""
    db_path = REPO_ROOT / "src" / "dialectic_db.py"
    fns = _functions(db_path)
    for (path, qualname), entry in inventory.SQL_WRITERS.items():
        if not path.startswith("src/"):
            continue
        method = qualname.split(".")[-1]
        helper = entry["wrapped_by"]
        assert helper in fns, f"{qualname}: wrapper {helper} does not exist"
        assert _records_a_pair(fns[helper]), (
            f"{helper} writes a session row without recording a "
            "dialectic_session_write attempt/response pair"
        )
        # Nothing else in src/ may call the raw method. Outside dialectic_db.py
        # only a module that can obtain a DialecticDB can call one (the main
        # DB backend has its own, unrelated `create_session`).
        callers = set()
        for src_path in sorted((REPO_ROOT / "src").rglob("*.py")):
            text = src_path.read_text(encoding="utf-8")
            if src_path != db_path and "get_dialectic_db" not in text \
                    and "DialecticDB" not in text:
                continue
            tree = ast.parse(text)
            for fn_name, fn in (_functions(src_path).items()
                                if src_path == db_path else [(None, tree)]):
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr == method
                            and isinstance(node.func.value, ast.Name)
                            and node.func.value.id in ("db", "self", "_db", "dialectic_db")):
                        callers.add((_rel(src_path), fn_name))
        allowed = {(inventory.DIALECTIC_DB, helper)}
        assert callers <= allowed, (
            f"{qualname} is called outside its recording wrapper: {sorted(callers - allowed)}"
        )


def test_every_beam_writer_records_a_pair():
    fns = _functions(REPO_ROOT / inventory.BEAM_CLIENT)
    for name in inventory.BEAM_WRITERS:
        assert name in fns, f"BEAM writer {name} is gone; update the inventory"
        assert _records_a_pair(fns[name]), (
            f"{name} sends a session write to BEAM without recording its attempt/response"
        )


def test_every_elixir_writer_says_how_it_is_observed():
    for key, entry in list(inventory.SQL_WRITERS.items()) + list(inventory.CALL_SITES.items()):
        if key[0].startswith("elixir/"):
            assert entry.get("observed_via"), f"{key} does not say how it is observed"
    liveness = inventory.CALL_SITES[(inventory.LIVENESS_EX, "fail_stuck")]
    assert "benign by construction" in liveness["observed_via"]
