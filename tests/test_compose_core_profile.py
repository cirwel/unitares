"""docker-compose.core.yml stays a valid overlay of docker-compose.yml.

The core profile names services from the default stack. The Minimal Core Matrix
runs it with Docker, but only when its own files change, so a rename in
docker-compose.yml would break the profile without that workflow noticing.
These checks run on every change and need no Docker.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that reads Compose's `!override` / `!reset` merge tags."""


def _tagged(loader, _suffix, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _tagged)


def _services(name: str) -> dict:
    return yaml.load((ROOT / name).read_text(), Loader=_ComposeLoader)["services"]


def test_core_profile_names_only_default_stack_services():
    default = _services("docker-compose.yml")
    core = _services("docker-compose.core.yml")
    assert set(core) <= set(default), set(core) - set(default)


def test_core_profile_turns_off_redis_and_the_lease_plane():
    core = _services("docker-compose.core.yml")
    for service in ("redis", "lease-plane"):
        assert core[service]["profiles"] == ["off"], service
    env = core["governance-mcp"]["environment"]
    assert env["REDIS_ENABLED"] == "0"
    assert env["LEASE_PLANE_BEARER_TOKEN"] == ""


def test_core_server_waits_only_on_a_database_the_default_stack_starts():
    default = _services("docker-compose.yml")
    core = _services("docker-compose.core.yml")
    depends = core["governance-mcp"]["depends_on"]
    assert set(depends) == {"postgres-age"}
    assert "postgres-age" in default
    # The default server depends on the services the core profile removes;
    # the override must replace that list, or Compose would still start them.
    assert set(default["governance-mcp"]["depends_on"]) - {"postgres-age"}
    assert "!override" in (ROOT / "docker-compose.core.yml").read_text()


def test_the_matrix_measures_the_shipped_file():
    workflow = (ROOT / ".github/workflows/minimal-core-matrix.yml").read_text()
    assert "'docker-compose.core.yml'" in workflow  # path filter
    assert '"-f docker-compose.core.yml"' in workflow  # the minimal-core leg
