"""SQL for the EISV era guard in analysis scripts (see ``src/eisv_eras.py``).

Kept out of ``src/`` because ``src/eisv_eras.py`` is a standalone read-side
module and the component-boundary guard keeps SQL against ``core.agent_state``
inside the EISV storage component.
"""

from __future__ import annotations


def instrument_id_counts_sql(
    start_param: str, end_param: str, *, include_synthetic: bool = False
) -> str:
    """Rows per stamped instrument id in a window, for ``require_single_instrument``.

    The placeholders are the caller's driver syntax (``$1::timestamptz`` for
    asyncpg, ``%s`` for psycopg2). The count covers every non-synthetic row in
    the window (every row with ``include_synthetic``, for a read that keeps
    synthetic rows), a superset of what a filtered read selects, so it can
    refuse a mix the read itself would not have touched; it cannot miss one.
    """
    synthetic = "" if include_synthetic else "AND synthetic = false "
    return (
        "SELECT state_json->'eisv_instrument'->>'id' AS instrument_id, count(*) AS n "
        "FROM core.agent_state "
        f"WHERE recorded_at >= {start_param} AND recorded_at < {end_param} "
        f"{synthetic}GROUP BY 1"
    )
