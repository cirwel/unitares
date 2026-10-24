"""Focused tests for the REST tool request byte boundary."""

from __future__ import annotations

import json

import pytest

from src.services import http_request_parser as parser


class StreamingRequest:
    def __init__(self, chunks: list[bytes], *, content_length: str | None = None):
        self._chunks = chunks
        self.chunks_yielded = 0
        self.headers = {}
        if content_length is not None:
            self.headers["content-length"] = content_length

    async def stream(self):
        for chunk in self._chunks:
            self.chunks_yielded += 1
            yield chunk


@pytest.mark.asyncio
async def test_chunked_json_without_content_length_is_bounded_and_decoded(monkeypatch):
    monkeypatch.setattr(parser, "MAX_HTTP_TOOL_REQUEST_SIZE", 64)
    request = StreamingRequest([b'{"name":', b' "checkin"}'])

    assert await parser.read_bounded_json_body(request) == {"name": "checkin"}


@pytest.mark.asyncio
async def test_actual_chunked_body_over_limit_stops_before_full_buffer(monkeypatch):
    monkeypatch.setattr(parser, "MAX_HTTP_TOOL_REQUEST_SIZE", 8)
    request = StreamingRequest([b"1234", b"56789", b"unread"])

    with pytest.raises(parser.HttpToolRequestError) as exc_info:
        await parser.read_bounded_json_body(request)

    assert exc_info.value.status_code == 413
    assert exc_info.value.payload["error"] == "Request body too large"
    assert request.chunks_yielded == 2


@pytest.mark.asyncio
async def test_declared_oversize_rejected_before_stream_read(monkeypatch):
    monkeypatch.setattr(parser, "MAX_HTTP_TOOL_REQUEST_SIZE", 8)
    request = StreamingRequest([b"{}"], content_length="9")

    with pytest.raises(parser.HttpToolRequestError) as exc_info:
        await parser.read_bounded_json_body(request)

    assert exc_info.value.status_code == 413
    assert request.chunks_yielded == 0


@pytest.mark.parametrize("value", ["not-a-number", "-1", "1.5", ""])
def test_malformed_content_length_is_rejected(value):
    with pytest.raises(parser.HttpToolRequestError) as exc_info:
        parser.validate_http_tool_content_length(value)

    assert exc_info.value.status_code == 400
    assert exc_info.value.payload["error"] == "Invalid Content-Length header"


@pytest.mark.asyncio
async def test_invalid_json_preserves_json_decode_error_contract():
    request = StreamingRequest([b"not json"])

    with pytest.raises(json.JSONDecodeError):
        await parser.read_bounded_json_body(request)
