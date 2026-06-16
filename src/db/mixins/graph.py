"""Graph (AGE) operations mixin for PostgresBackend."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)


class GraphMixin:
    """Apache AGE graph query operations."""

    # Set True once the configured AGE graph is confirmed to exist, so the
    # steady-state read path can skip the per-query ag_catalog.ag_graph
    # existence round-trip. Reset by the stale-graph repair path so existence
    # is re-validated after a drop/recreate.
    _graph_exists_confirmed: bool = False

    async def graph_available(self) -> bool:
        """Check if AGE graph queries are available."""
        async with self.acquire() as conn:
            try:
                await self._prepare_age_connection(conn)
                await self._ensure_age_graph_exists(conn)
                await self._probe_age_graph(conn)
                return True
            except Exception:
                return False

    async def _prepare_age_connection(self, conn) -> None:
        """Load AGE and configure the required search path on a connection."""
        await conn.execute("LOAD 'age'")
        await conn.execute("SET search_path = ag_catalog, core, audit, public")

    async def _ensure_age_graph_exists(self, conn, *, external_conn: bool = False) -> None:
        """Ensure the configured AGE graph exists, creating it when absent.

        Short-circuits once existence is confirmed (steady state). When
        ``external_conn`` is True the connection belongs to a caller-owned
        transaction (e.g. the dual-write in add_discovery): creating the graph
        here would issue DDL — an implicit COMMIT — mid-transaction and break
        the caller's atomicity. So we never create on a borrowed transaction
        connection; we surface a clear error and let the caller run graph setup
        outside the transaction.
        """
        if self._graph_exists_confirmed:
            return
        graph_exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1)",
            self._age_graph,
        )
        if graph_exists:
            self._graph_exists_confirmed = True
            return
        if external_conn:
            raise RuntimeError(
                f"AGE graph '{self._age_graph}' is missing and cannot be created on a "
                "caller-owned transaction connection (DDL would break the transaction). "
                "Run graph setup outside the transaction."
            )
        logger.warning(f"AGE graph '{self._age_graph}' missing, creating it")
        await conn.execute("SELECT * FROM ag_catalog.create_graph($1)", self._age_graph)
        self._graph_exists_confirmed = True

    async def _probe_age_graph(self, conn) -> None:
        """Verify the configured AGE graph can execute a trivial Cypher query."""
        await conn.fetch(
            f"SELECT * FROM cypher('{self._age_graph}', $$ RETURN 1 $$) as (result agtype)"
        )

    @staticmethod
    def _is_stale_age_graph_error(error: Exception) -> bool:
        """Detect AGE catalog drift where the named graph points at a dead OID."""
        message = str(error).lower()
        return "graph with oid" in message and "does not exist" in message

    @staticmethod
    def _is_column_arity_error(error: Exception) -> bool:
        """Detect a RETURN-arity vs output-column mismatch.

        graph_query declares a single ``result agtype`` output column, so a
        multi-column ``RETURN a, b`` is rejected by AGE/PostgreSQL. Surfaced as
        a clear, actionable error rather than a silently-swallowed empty result.
        """
        message = str(error).lower()
        return "column definition list" in message and (
            "do not match" in message
            or "does not match" in message
            or "same number" in message
        )

    @staticmethod
    def _decode_agtype_value(result: Any) -> Any:
        """Decode one ``agtype`` output-column value (text form) into Python.

        AGE annotates typed values with a ``::vertex``/``::edge``/``::path``
        suffix AFTER the object/array they annotate — INCLUDING inside a map or
        list, e.g. ``{"node": {...}::vertex, "depth": 0}``. We strip those
        suffixes wherever they follow a ``}``/``]`` (the lookbehind avoids
        touching identical text inside a quoted string value), plus a trailing
        scalar ``::agtype``, so the whole value parses as JSON. Non-string
        inputs (already-decoded dict/list/scalars) pass through unchanged.
        """
        if not isinstance(result, str):
            return result
        clean = re.sub(r"(?<=[}\]])::(?:vertex|edge|path)\b", "", result)
        if clean.endswith("::agtype"):
            clean = clean[: -len("::agtype")]
        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            return result

    def _interpolate_params(self, cypher: str, params: Optional[Dict[str, Any]]) -> str:
        """Substitute ``${key}`` placeholders with sanitized values in ONE pass.

        AGE has no native parameterization, so values are sanitized and
        interpolated. A single combined-pattern pass (rather than re-scanning
        the string per key) ensures a sanitized value that itself contains a
        ``${otherkey}`` sequence — e.g. discovery text describing template
        syntax — can never be re-interpolated on a later key's pass.
        """
        if not params:
            return cypher
        sanitized = {k: self._sanitize_cypher_param(v) for k, v in params.items()}
        pattern = re.compile(
            r"\$\{(" + "|".join(re.escape(k) for k in sanitized) + r")\}"
        )
        return pattern.sub(lambda m: sanitized[m.group(1)], cypher)

    async def _repair_stale_age_graph(self, conn, error: Exception) -> bool:
        """
        Repair a stale AGE catalog entry by recreating the configured graph.

        This handles cases where ag_catalog.ag_graph still references an old OID
        for a schema that has since been recreated.

        Uses a fresh non-transactional connection because drop_graph/create_graph
        are DDL operations that cannot run inside an active transaction.
        """
        if not self._is_stale_age_graph_error(error):
            return False

        logger.warning(
            f"Repairing stale AGE graph '{self._age_graph}' after error: {error}"
        )
        async with self.acquire() as fresh_conn:
            await fresh_conn.execute("SELECT * FROM ag_catalog.drop_graph($1, true)", self._age_graph)
            await fresh_conn.execute("SELECT * FROM ag_catalog.create_graph($1)", self._age_graph)
        # OID changed; force the next query to re-validate existence.
        self._graph_exists_confirmed = False
        return True

    async def graph_query(
        self,
        cypher: str,
        params: Optional[Dict[str, Any]] = None,
        conn=None,
    ) -> List[Dict[str, Any]]:
        """
        Execute a Cypher query against the AGE graph.

        Parameters are validated and safely interpolated since AGE doesn't support
        parameterized Cypher queries ($1, $2 style).

        Convention: a query must have a SINGLE return expression so it maps to
        the one ``result agtype`` output column. For multiple values, project a
        map literal: ``RETURN {a: expr1, b: expr2}`` (decoded to one dict per
        row). A bare multi-column ``RETURN a, b`` raises an AGE column-arity
        error.

        Args:
            cypher: Cypher query with ${param} placeholders
            params: Parameter dict for interpolation
            conn: Optional existing connection (for use within transactions).
                  When provided, reuses this connection instead of acquiring from pool.
        """
        return await self._execute_graph_query(cypher, params, conn)

    async def _execute_graph_query(
        self,
        cypher: str,
        params: Optional[Dict[str, Any]],
        conn=None,
    ) -> List[Dict[str, Any]]:
        """Internal graph query execution, supports both pooled and passed connections.

        A passed-in ``conn`` is a caller-owned transaction connection: it is
        flagged ``external_conn=True`` so the AGE-graph existence/repair paths
        never run DDL on it (which would break the caller's transaction).
        """
        if conn is not None:
            return await self._run_cypher_on_conn(conn, cypher, params, external_conn=True)

        async with self.acquire() as pooled_conn:
            return await self._run_cypher_on_conn(pooled_conn, cypher, params, external_conn=False)

    async def _run_cypher_on_conn(
        self,
        conn,
        cypher: str,
        params: Optional[Dict[str, Any]],
        *,
        _allow_repair: bool = True,
        external_conn: bool = False,
    ) -> List[Dict[str, Any]]:
        """Execute a Cypher query on a specific connection."""
        try:
            # Always LOAD + SET before every query. asyncpg's pool runs
            # RESET ALL on connection release, which clears search_path.
            # The 2 extra round-trips per query are worth correctness.
            await self._prepare_age_connection(conn)
            await self._ensure_age_graph_exists(conn, external_conn=external_conn)

            safe_cypher = self._interpolate_params(cypher, params)

            rows = await conn.fetch(
                f"SELECT * FROM cypher('{self._age_graph}', $$ {safe_cypher} $$) as (result agtype)"
            )

            return [self._decode_agtype_value(row["result"]) for row in rows]

        except Exception as e:
            if self._is_column_arity_error(e):
                logger.error(
                    "Cypher query failed (column arity): graph_query supports a "
                    "single return expression — use a map literal "
                    "RETURN {a: expr1, b: expr2} for multiple values. Query: %s",
                    cypher,
                )
                raise
            # Never attempt DDL-based repair on a caller-owned transaction
            # connection: drop/create_graph plus a retry on an already-aborted
            # transaction corrupts the caller's atomicity (dual-write loss).
            if _allow_repair and not external_conn and self._is_stale_age_graph_error(e):
                repaired = await self._repair_stale_age_graph(conn, e)
                if repaired:
                    return await self._run_cypher_on_conn(
                        conn,
                        cypher,
                        params,
                        _allow_repair=False,
                        external_conn=external_conn,
                    )
            logger.error(f"Cypher query failed: {e}")
            raise

    # Maximum byte length for a single string parameter (10 KB)
    _MAX_PARAM_LENGTH = 10_240
    # Maximum recursion depth for nested list/dict params
    _MAX_PARAM_DEPTH = 8

    def _sanitize_cypher_param(self, value: Any, _depth: int = 0) -> str:
        """
        Sanitize a parameter value for safe inclusion in a Cypher query.

        AGE doesn't support parameterized queries, so we must validate values.
        Enforces length limits, rejects null bytes, and escapes all dangerous chars.
        """
        if _depth > self._MAX_PARAM_DEPTH:
            raise ValueError(f"Cypher param nesting too deep (>{self._MAX_PARAM_DEPTH})")

        if value is None:
            return "NULL"
        elif isinstance(value, bool):
            return "true" if value else "false"
        elif isinstance(value, (int, float)):
            import math as _math
            if isinstance(value, float) and (_math.isnan(value) or _math.isinf(value)):
                raise ValueError(f"Cypher param cannot be NaN or Inf: {value}")
            return str(value)
        elif isinstance(value, str):
            if '\x00' in value:
                raise ValueError("Cypher param contains null byte")
            if len(value) > self._MAX_PARAM_LENGTH:
                raise ValueError(
                    f"Cypher param too long ({len(value)} > {self._MAX_PARAM_LENGTH})"
                )
            escaped = (
                value
                .replace("\\", "\\\\")
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                .replace("\t", "\\t")
                .replace("'", "\\'")
                .replace('"', '\\"')
            )
            return f"'{escaped}'"
        elif isinstance(value, list):
            sanitized_elements = [
                self._sanitize_cypher_param(item, _depth + 1) for item in value
            ]
            return f"[{', '.join(sanitized_elements)}]"
        elif isinstance(value, dict):
            json_str = json.dumps(value)
            if len(json_str) > self._MAX_PARAM_LENGTH:
                raise ValueError(
                    f"Cypher dict param too long ({len(json_str)} > {self._MAX_PARAM_LENGTH})"
                )
            escaped = json_str.replace("\\", "\\\\").replace("'", "\\'")
            return f"'{escaped}'"
        else:
            raise ValueError(f"Unsupported Cypher param type: {type(value)}")
