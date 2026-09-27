"""Tests for the /v1/metrics{,/series,/catalog} HTTP endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request


def _make_request(
    method: str,
    path: str,
    body: bytes | None = None,
    query: str = "",
    headers: dict[str, str] | None = None,
    client: tuple[str, int] | None = ("127.0.0.1", 55555),
) -> Request:
    """Construct a Starlette Request suitable for handler unit-testing."""
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": [
            (k.lower().encode(), v.encode())
            for k, v in (headers or {}).items()
        ],
        "client": client,
        "scheme": "http",
        "http_version": "1.1",
        "root_path": "",
        "server": ("127.0.0.1", 8767),
        "state": {},
    }

    async def receive():
        return {
            "type": "http.request",
            "body": body or b"",
            "more_body": False,
        }

    return Request(scope, receive=receive)


async def _read_json(response):
    body = response.body
    import json
    return json.loads(body.decode())


# ---------------------------------------------------------------------------
# POST /v1/metrics
# ---------------------------------------------------------------------------


class TestPostMetric:
    @pytest.mark.asyncio
    async def test_valid_known_metric_returns_201(self):
        from src.http_api import http_post_metric

        req = _make_request(
            "POST", "/v1/metrics",
            body=b'{"name":"kg.entries.count","value":70000}',
            headers={"content-type": "application/json"},
        )
        with patch("src.fleet_metrics.record", new=AsyncMock(return_value=None)) as mock_rec:
            resp = await http_post_metric(req)
        assert resp.status_code == 201
        mock_rec.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_metric_returns_404(self):
        from src.http_api import http_post_metric

        req = _make_request(
            "POST", "/v1/metrics",
            body=b'{"name":"no.such.metric","value":1}',
        )
        # Patch record to raise KeyError as the real catalog-gated version would
        async def _raise(*_a, **_kw):
            raise KeyError("Metric 'no.such.metric' is not in the catalog.")

        with patch("src.fleet_metrics.record", new=_raise):
            resp = await http_post_metric(req)
        assert resp.status_code == 404
        body = await _read_json(resp)
        assert body["success"] is False
        assert "catalog" in body["error"]

    @pytest.mark.asyncio
    async def test_missing_name_returns_400(self):
        from src.http_api import http_post_metric

        req = _make_request("POST", "/v1/metrics", body=b'{"value":1}')
        resp = await http_post_metric(req)
        assert resp.status_code == 400
        body = await _read_json(resp)
        assert "name" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_non_numeric_value_returns_400(self):
        from src.http_api import http_post_metric

        req = _make_request(
            "POST", "/v1/metrics",
            body=b'{"name":"kg.entries.count","value":"not-a-number"}',
        )
        resp = await http_post_metric(req)
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_bool_value_rejected(self):
        """True/False are Python ints — explicitly reject to avoid surprise writes."""
        from src.http_api import http_post_metric

        req = _make_request(
            "POST", "/v1/metrics",
            body=b'{"name":"kg.entries.count","value":true}',
        )
        resp = await http_post_metric(req)
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_ts_returns_400(self):
        from src.http_api import http_post_metric

        req = _make_request(
            "POST", "/v1/metrics",
            body=b'{"name":"kg.entries.count","value":1,"ts":"not-a-timestamp"}',
        )
        resp = await http_post_metric(req)
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self):
        from src.http_api import http_post_metric

        req = _make_request("POST", "/v1/metrics", body=b"not-json")
        resp = await http_post_metric(req)
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_non_local_caller_without_token_returns_401(self):
        from src.http_api import http_post_metric

        req = _make_request(
            "POST", "/v1/metrics",
            body=b'{"name":"kg.entries.count","value":1}',
            client=("203.0.113.7", 55555),  # TEST-NET-3, not in trusted nets
        )
        with patch.dict("os.environ", {"UNITARES_HTTP_API_TOKEN": "secret"}):
            resp = await http_post_metric(req)
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /v1/metrics/series
# ---------------------------------------------------------------------------


class TestGetMetricsSeries:
    @pytest.mark.asyncio
    async def test_returns_points(self):
        from src.http_api import http_get_metrics
        from src.fleet_metrics.storage import MetricPoint

        ts = datetime(2026, 4, 19, tzinfo=timezone.utc)
        fake_points = [MetricPoint(ts=ts, value=123.0)]
        with patch("src.fleet_metrics.query", new=AsyncMock(return_value=fake_points)):
            req = _make_request("GET", "/v1/metrics/series", query="name=kg.entries.count")
            resp = await http_get_metrics(req)
        body = await _read_json(resp)
        assert resp.status_code == 200
        assert body["success"] is True
        assert body["name"] == "kg.entries.count"
        assert body["count"] == 1
        assert body["points"][0]["value"] == 123.0

    @pytest.mark.asyncio
    async def test_missing_name_returns_400(self):
        from src.http_api import http_get_metrics

        req = _make_request("GET", "/v1/metrics/series", query="")
        resp = await http_get_metrics(req)
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_since_returns_400(self):
        from src.http_api import http_get_metrics

        req = _make_request(
            "GET", "/v1/metrics/series",
            query="name=kg.entries.count&since=not-a-date",
        )
        resp = await http_get_metrics(req)
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_limit_returns_400(self):
        from src.http_api import http_get_metrics

        req = _make_request(
            "GET", "/v1/metrics/series",
            query="name=kg.entries.count&limit=abc",
        )
        resp = await http_get_metrics(req)
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /v1/metrics/catalog
# ---------------------------------------------------------------------------


class TestGetMetricsCatalog:
    @pytest.mark.asyncio
    async def test_lists_initial_entry(self):
        from src.http_api import http_get_metrics_catalog

        req = _make_request("GET", "/v1/metrics/catalog")
        resp = await http_get_metrics_catalog(req)
        body = await _read_json(resp)
        assert resp.status_code == 200
        assert body["success"] is True
        names = {m["name"] for m in body["metrics"]}
        assert "kg.entries.count" in names

    @pytest.mark.asyncio
    async def test_includes_last_point_ts_field(self):
        """Each catalog entry surfaces last_point_ts (ISO string or null) so
        the dashboard can suppress empty `.error` twins without N+1 probes."""
        from src.http_api import http_get_metrics_catalog

        ts = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        with patch(
            "src.fleet_metrics.storage.latest_ts_for_names",
            new=AsyncMock(return_value={"kg.entries.count": ts}),
        ):
            req = _make_request("GET", "/v1/metrics/catalog")
            resp = await http_get_metrics_catalog(req)
            body = await _read_json(resp)

        by_name = {m["name"]: m for m in body["metrics"]}
        assert by_name["kg.entries.count"]["last_point_ts"] == ts.isoformat()
        # An entry not in the latest_ts dict reports None — `.error` twins
        # without points hit this branch and the dashboard hides them.
        assert by_name["kg.entries.count.error"]["last_point_ts"] is None

    @pytest.mark.asyncio
    async def test_last_ts_probe_failure_degrades_to_null(self):
        """If the aggregation query fails, the endpoint still returns the
        catalog with last_point_ts=null rather than 500ing the dashboard."""
        from src.http_api import http_get_metrics_catalog

        with patch(
            "src.fleet_metrics.storage.latest_ts_for_names",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            req = _make_request("GET", "/v1/metrics/catalog")
            resp = await http_get_metrics_catalog(req)
            body = await _read_json(resp)

        assert resp.status_code == 200
        assert body["success"] is True
        assert all(m["last_point_ts"] is None for m in body["metrics"])
