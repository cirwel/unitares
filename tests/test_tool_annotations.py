"""Every advertised tool carries MCP annotations, on every surface that serves them.

Annotations are the machine-readable half of a tool definition: four booleans
and a display title a client can branch on without parsing the description.
They are cheap to add and cheap to lose — nothing else in the suite reads
``Tool.annotations``, so a tool added without a record in
``src/tool_annotations.py`` would ship silently unannotated, and a hint written
under the wrong key would ship silently wrong.

The tests enumerate the live advertised surface rather than a name list, so a
new tool fails here on the day it lands rather than on the day someone counts.

Three failure modes worth naming, because none of them raise on their own:

* **A missing record.** ``tool_annotations`` returns None, the field is omitted
  from the wire, and the tool looks like every other unannotated tool.
* **A snake_case key.** ``ToolAnnotations`` is declared ``extra='allow'`` on mcp
  1.x, so ``read_only_hint=True`` is accepted, stored, and serialized under a
  wire name no client reads. Held by the key-shape test below.
* **An outputSchema.** Not added by this change, and it must stay that way:
  every tool registers ``structured_output=False``, so the low-level server's
  post-call validator (``mcp/server/lowlevel/server.py``) would find an
  outputSchema with no structured content and turn *every* tool call into an
  "Output validation error". Held by the last test, which is a tripwire rather
  than a description of anything this file does.
"""

from __future__ import annotations

import pytest

import src.mcp_handlers  # noqa: F401  -- settles every @mcp_tool decorator
from src.interface_contract import get_public_tool_definitions
from src.tool_annotations import (
    ANNOTATION_KEYS,
    TOOL_ANNOTATIONS,
    annotation_payload,
    tool_annotations,
)
from src.tool_meta import TOOL_META


pytestmark = pytest.mark.usefixtures("first_party_tool_surface")


def advertised() -> list:
    """Every advertised definition, as this process builds them locally.

    NOT the surface Glama scores, which is the correction this file used to get
    wrong. The deployed container does not serve stdio from these objects at
    all: docker/glama/supervise.py:178 sets ``UNITARES_STDIO_PROXY_HTTP_URL``,
    so src/mcp_server_std.py takes its proxy branch, fetches /v1/tools, and
    rebuilds each ``Tool`` from the JSON. Annotations reach a scoring client
    only if that payload carries them and that rebuild reads them back —
    held by ``test_annotations_survive_the_rest_proxy_round_trip`` below, and
    by nothing else in the suite.
    """
    return get_public_tool_definitions("full")


def test_every_advertised_tool_has_an_annotation_record():
    """A new tool lands with hints or fails here — not silently unannotated."""
    missing = sorted(
        tool.name for tool in advertised() if tool.name not in TOOL_ANNOTATIONS
    )
    assert not missing, (
        f"advertised tools with no record in src/tool_annotations.py: {missing}. "
        "Read the handler, decide the four hints, and add one entry."
    )


def test_no_annotation_record_for_an_unadvertised_name():
    """A record for a name nothing advertises is a rename that half-landed."""
    stale = sorted(set(TOOL_ANNOTATIONS) - {tool.name for tool in advertised()})
    assert not stale, (
        f"src/tool_annotations.py records names that are not advertised: {stale}"
    )


def test_stdio_tool_definitions_carry_annotations():
    """The surface a scoring client actually reads, wire tools and aliases alike.

    Both halves matter: the 42 wire tools are annotated in
    ``src/tool_schemas.py``'s post-processing loop, the 8 workflow aliases in
    ``build_alias_tool_definition`` — two separate call sites, one table.
    """
    unannotated = sorted(
        tool.name for tool in advertised() if tool.annotations is None
    )
    assert not unannotated, (
        f"advertised on stdio/REST without annotations: {unannotated}"
    )


def test_alias_definitions_carry_annotations():
    """build_alias_tool_definition is reached directly by the registrar too."""
    from src.interface_contract import build_alias_tool_definition
    from src.tool_meta import WORKFLOW_ALIAS_NAMES

    for alias_name in WORKFLOW_ALIAS_NAMES:
        tool = build_alias_tool_definition(alias_name)
        assert tool.annotations is not None, alias_name
        assert tool.annotations.title, alias_name


@pytest.mark.parametrize("name", sorted(TOOL_ANNOTATIONS))
def test_annotation_serializes_to_the_spec_wire_keys(name):
    """camelCase in, camelCase out — the mcp 1.x ``extra='allow'`` trap.

    Asserting on the dumped keys rather than on construction is the whole
    point: constructing with a snake_case key does not raise on 1.x.
    """
    annotations = tool_annotations(name)
    assert annotations is not None
    dumped = annotations.model_dump(by_alias=True, exclude_none=True)
    assert set(dumped) == ANNOTATION_KEYS, (
        f"{name} serialized {sorted(dumped)}; expected {sorted(ANNOTATION_KEYS)}"
    )
    assert isinstance(dumped["title"], str) and dumped["title"].strip()
    for key in ANNOTATION_KEYS - {"title"}:
        assert isinstance(dumped[key], bool), f"{name}.{key} is not a bool"


def test_payload_keys_are_the_wire_keys():
    """The table itself, before pydantic ever sees it."""
    for name, payload in TOOL_ANNOTATIONS.items():
        assert set(payload) == ANNOTATION_KEYS, f"{name}: {sorted(payload)}"


def test_annotation_payload_is_a_copy():
    """A REST-style envelope must not be able to edit the table it read."""
    payload = annotation_payload("health_check")
    payload["readOnlyHint"] = False
    assert TOOL_ANNOTATIONS["health_check"]["readOnlyHint"] is True
    assert annotation_payload("no_such_tool") is None
    assert tool_annotations("no_such_tool") is None


def test_read_only_hint_never_contradicts_the_recorded_operation():
    """No tool claims readOnlyHint while tool_meta records it as write/admin.

    Only this direction is checked. The reverse is legitimate and common: a
    router's ``operation`` carries the most privileged class among its actions,
    and several tools recorded ``read`` still write on some path
    (``simulate_update`` appends an audit event, ``identity`` mints an agent),
    so the annotation is deliberately the more conservative of the two.
    """
    operations = {meta.name: meta.operation for meta in TOOL_META}
    contradictions = sorted(
        name
        for name, payload in TOOL_ANNOTATIONS.items()
        if payload["readOnlyHint"] and operations.get(name) in {"write", "admin"}
    )
    assert not contradictions, (
        "readOnlyHint=True on tools src/tool_meta.py records as write/admin: "
        f"{contradictions}. One of the two is wrong; the handler decides."
    )


def test_destructive_hint_is_false_wherever_read_only_is_true():
    """destructiveHint is meaningless on a read; leave it False rather than unset."""
    for name, payload in TOOL_ANNOTATIONS.items():
        if payload["readOnlyHint"]:
            assert payload["destructiveHint"] is False, name
            assert payload["idempotentHint"] is True, (
                f"{name} is read-only but not idempotent"
            )


@pytest.mark.asyncio
async def test_annotations_survive_the_live_fastmcp_listing():
    """/mcp/ rebuilds its own Tool objects, so the registrar passes them itself.

    This also covers ``mode_filtered_server_class`` / ``apply_listed_schema_policy``,
    whose ``copy.copy`` would drop the field if it ever became a deep rebuild.
    """
    from src import mcp_server

    listed = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}
    advertised_names = {tool.name for tool in advertised()}
    checked = advertised_names & set(listed)
    assert checked, "no advertised tool is mounted; the mount fixture changed"

    unannotated = sorted(
        name for name in checked if listed[name].annotations is None
    )
    assert not unannotated, f"mounted on /mcp/ without annotations: {unannotated}"

    health = listed["health_check"].annotations
    assert health.model_dump(by_alias=True, exclude_none=True) == {
        "title": "Server Health Snapshot",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


@pytest.mark.asyncio
async def test_no_tool_advertises_an_output_schema():
    """Tripwire, not a description of this module. See the header.

    Every tool is registered ``structured_output=False``. Give one an
    outputSchema at any listing layer and the low-level server caches it, finds
    no structured content on the way back, and fails every call to that tool.
    """
    from src import mcp_server

    for tool in advertised():
        assert getattr(tool, "outputSchema", None) is None, tool.name
    for tool in await mcp_server.mcp.list_tools():
        assert getattr(tool, "outputSchema", None) is None, tool.name


@pytest.mark.asyncio
async def test_annotations_survive_the_rest_proxy_round_trip(monkeypatch):
    """The surface a deployed scoring client actually reads, end to end.

    Both halves in one test on purpose, because either alone passes while the
    pair is broken: /v1/tools must EMIT an ``annotations`` member, and
    ``_proxy_http_list_tools`` must READ it back. Until this change neither did,
    so 50 annotated definitions became 50 unannotated ones with no error
    anywhere — a probe of the running container was the only way to see it.

    The stub stands in for the network hop only. The payload is the real one,
    built by the real route, so an emit-side regression fails here too.
    """
    import json
    from types import SimpleNamespace

    import src.mcp_server_std as stdio
    from src.http_routes.tools import http_list_tools

    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)
    request = SimpleNamespace(
        query_params={},
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    rest_body = json.loads((await http_list_tools(request)).body)

    emitted = {
        entry["function"]["name"]: entry.get("annotations")
        for entry in rest_body["tools"]
    }
    missing = sorted(name for name, ann in emitted.items() if not ann)
    assert not missing, f"/v1/tools emitted no annotations member for: {missing}"

    # Take the proxy branch, and answer its one HTTP call with that payload.
    monkeypatch.setattr(stdio, "STDIO_PROXY_HTTP_URL", "http://stub.invalid")
    monkeypatch.setattr(stdio, "STDIO_PROXY_URL", None)

    class _StubHTTPResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

    encoded = json.dumps(rest_body).encode("utf-8")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **kw: _StubHTTPResponse(encoded),
    )

    proxied = {tool.name: tool for tool in await stdio.list_tools()}
    assert proxied, "the proxy branch returned nothing; the stub did not land"

    dropped = sorted(
        name for name in emitted if proxied.get(name) and proxied[name].annotations is None
    )
    assert not dropped, f"annotations lost in the stdio-over-REST rebuild: {dropped}"

    health = proxied["health_check"]
    assert health.annotations.model_dump(by_alias=True, exclude_none=True) == {
        "title": "Server Health Snapshot",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    # The rebuild still produces valid Tool objects, annotations or not.
    # get_tool_input_schema because mcp 2.x renamed the field to input_schema.
    from src.mcp_compat import get_tool_input_schema

    assert get_tool_input_schema(health)
    assert health.description


@pytest.mark.asyncio
async def test_proxy_rebuild_tolerates_a_payload_with_no_annotations(monkeypatch):
    """An older server, or a non-UNITARES one, sends only `function`.

    That must cost the tools their hints, never the client its listing — the
    reason ``_proxied_tool_annotations`` swallows rather than raises.
    """
    import json

    import src.mcp_server_std as stdio

    monkeypatch.setattr(stdio, "STDIO_PROXY_HTTP_URL", "http://stub.invalid")
    monkeypatch.setattr(stdio, "STDIO_PROXY_URL", None)

    payload = {
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "health_check",
                    "description": "Server health.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_tools",
                    "description": "Catalog.",
                    "parameters": {"type": "object", "properties": {}},
                },
                "annotations": "not-a-dict",
            },
        ]
    }

    class _StubHTTPResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

    encoded = json.dumps(payload).encode("utf-8")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **kw: _StubHTTPResponse(encoded),
    )

    from src.mcp_compat import get_tool_input_schema

    rebuilt = {tool.name: tool for tool in await stdio.list_tools()}
    assert set(rebuilt) == {"health_check", "list_tools"}
    for tool in rebuilt.values():
        assert tool.annotations is None
        assert get_tool_input_schema(tool) == {"type": "object", "properties": {}}
