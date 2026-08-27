"""Regression coverage for the flag catalog's supported AST forms."""

import ast
import pathlib
import sys
import textwrap

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts/dev"))

import flag_catalog as fc  # noqa: E402


def _collect_snippet(source: str, rel: str) -> dict[str, fc.Flag]:
    tree = ast.parse(textwrap.dedent(source))
    collector = fc.Collector(rel, fc._module_string_constants(tree))
    collector.visit(tree)
    return collector.flags


def _fallbacks(flag: fc.Flag) -> set[str]:
    return set(flag.fallback_sites)


@pytest.mark.parametrize(
    ("rel", "call", "expected"),
    [
        (
            "src/mcp_listen_config.py",
            'env_truthy("UNITARES_TEST_FLAG", default=True)',
            "True",
        ),
        (
            "src/example.py",
            'os.getenv("UNITARES_TEST_FLAG", default="fallback")',
            "'fallback'",
        ),
    ],
)
def test_keyword_default_is_honored(rel, call, expected):
    flags = _collect_snippet(f"def read():\n    return {call}\n", rel)
    assert _fallbacks(flags["UNITARES_TEST_FLAG"]) == {expected}


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ('env_truthy("UNITARES_TEST_FLAG")', "False (via env_truthy)"),
        ('split_csv_env("UNITARES_TEST_FLAG")', "[] (via split_csv_env)"),
    ],
)
def test_registered_helper_uses_its_implicit_fallback(call, expected):
    flags = _collect_snippet(
        f"def read():\n    return {call}\n",
        "src/mcp_listen_config.py",
    )
    assert _fallbacks(flags["UNITARES_TEST_FLAG"]) == {expected}


def test_annotated_module_key_and_injected_env_map_are_resolved():
    flags = _collect_snippet(
        '''
        PLUGINS_ENV: str = "UNITARES_TEST_PLUGINS"

        def enabled(env=None):
            """True unless plugin discovery is disabled."""
            return (env if env is not None else os.environ).get(PLUGINS_ENV)
        ''',
        "src/plugins.py",
    )

    flag = flags["UNITARES_TEST_PLUGINS"]
    assert _fallbacks(flag) == {fc.NO_READER_FALLBACK}
    assert flag.purpose == "True unless plugin discovery is disabled."
    assert flag.sites == ["src/plugins.py:6"]


def test_direct_read_without_fallback_is_not_labeled_required():
    flags = _collect_snippet(
        'def read():\n    return os.getenv("UNITARES_TEST_FLAG")\n',
        "src/example.py",
    )
    assert _fallbacks(flags["UNITARES_TEST_FLAG"]) == {fc.NO_READER_FALLBACK}


def test_registered_helper_names_are_path_scoped():
    source = 'def read():\n    return _env_float("UNITARES_TEST_FLAG", 15.0)\n'
    expected = _collect_snippet(
        source,
        "agents/dialectic_reviewer/reviewer.py",
    )
    collision = _collect_snippet(source, "src/unrelated.py")

    assert _fallbacks(expected["UNITARES_TEST_FLAG"]) == {"15.0"}
    assert collision == {}


def test_dynamic_rebinding_and_parameters_shadow_static_names():
    dynamically_rebound = _collect_snippet(
        '''
        ENV_NAME = "UNITARES_WRONG_STATIC_KEY"
        ENV_NAME = choose_env_name()
        os.getenv(ENV_NAME)
        ''',
        "src/dynamic.py",
    )
    parameter_shadowed = _collect_snippet(
        '''
        ENV_NAME = "UNITARES_WRONG_PARAMETER_KEY"

        def read(ENV_NAME):
            return os.getenv(ENV_NAME)
        ''',
        "src/parameter.py",
    )

    assert dynamically_rebound == {}
    assert parameter_shadowed == {}


def test_literal_tuple_loop_keys_are_resolved():
    flags = _collect_snippet(
        '''
        def forward():
            names = ("UNITARES_TEST_A", "UNITARES_TEST_B")
            for name in names:
                os.environ.get(name)
        ''',
        "src/forwarder.py",
    )

    assert set(flags) == {"UNITARES_TEST_A", "UNITARES_TEST_B"}
    for flag in flags.values():
        assert _fallbacks(flag) == {fc.NO_READER_FALLBACK}
        assert flag.sites == ["src/forwarder.py:5"]
        assert flag.purpose == ""


def test_behavioral_consumer_purpose_beats_tuple_loop_forwarder():
    forwarded = _collect_snippet(
        '''
        def forward():
            names = ("UNITARES_TEST_FLAG",)
            for name in names:
                os.environ.get(name)
        ''',
        "src/forwarder.py",
    )["UNITARES_TEST_FLAG"]
    consumed = _collect_snippet(
        '''
        def enabled():
            """Enable the behavioral feature when configured."""
            return os.getenv("UNITARES_TEST_FLAG", "0")
        ''',
        "agents/consumer.py",
    )["UNITARES_TEST_FLAG"]

    fc._merge_flag(forwarded, consumed)

    assert forwarded.purpose == "Enable the behavioral feature when configured."
    assert forwarded.purpose_priority == 1


def test_merge_preserves_conflicting_reader_fallbacks():
    without_fallback = _collect_snippet(
        'os.getenv("GOVERNANCE_TEST_URL")\n',
        "src/first.py",
    )["GOVERNANCE_TEST_URL"]
    with_fallback = _collect_snippet(
        'os.getenv("GOVERNANCE_TEST_URL", "http://localhost")\n',
        "src/second.py",
    )["GOVERNANCE_TEST_URL"]

    fc._merge_flag(without_fallback, with_fallback)

    assert without_fallback.fallback_sites == {
        fc.NO_READER_FALLBACK: ["src/first.py:1"],
        "'http://localhost'": ["src/second.py:1"],
    }
    rendered = fc.render({without_fallback.name: without_fallback})
    assert "varies:" in rendered
    assert "src/first.py" in rendered
    assert "src/second.py" in rendered


def test_catalog_covers_current_nonliteral_runtime_reads():
    flags = fc.collect()
    expected_defaults = {
        "UNITARES_DIALECTIC_CONTINUATION_POLL_S": {
            "DEFAULT_CONTINUATION_POLL_S",
            fc.NO_READER_FALLBACK,
        },
        "UNITARES_DIALECTIC_EXTERNAL_API_KEY": {"''"},
        "UNITARES_GOVERNED_EFFECT_BINDING_AGENT_SPAWN": {
            fc.NO_READER_FALLBACK
        },
        "UNITARES_GOVERNED_EFFECT_BINDING_FILE_WRITE": {
            fc.NO_READER_FALLBACK
        },
        "UNITARES_MCP_ALLOW_NULL_ORIGIN": {"True"},
        "UNITARES_RESIDENT_PROGRESS_PLUGINS": {fc.NO_READER_FALLBACK},
        "UNITARES_SERVER_LOCK_FILE": {"'.mcp_server.lock'"},
        "UNITARES_SERVER_PID_FILE": {"'.mcp_server.pid'"},
    }

    assert {
        name: _fallbacks(flags[name]) for name in expected_defaults
    } == expected_defaults
    assert {
        site.rsplit(":", 1)[0]
        for site in flags["UNITARES_DIALECTIC_CONTINUATION_POLL_S"].sites
    } == {
        "agents/dialectic_reviewer/reviewer.py",
        "src/mcp_handlers/dialectic/orchestrator_dispatch.py",
    }


def test_dynamic_flags_are_linked_to_runtime_constants_and_effect_payloads():
    flags = fc.collect()

    host_path = fc.REPO / "agents/dialectic_reviewer/host_backends.py"
    host_tree = ast.parse(host_path.read_text(encoding="utf-8"))
    external_key = fc._module_string_constants(host_tree)["_DEFAULT_KEY_ENV"]
    assert _fallbacks(flags[external_key]) == {"''"}

    effects_path = fc.REPO / fc.BINDING_READER_PATH
    effects_tree = ast.parse(effects_path.read_text(encoding="utf-8"))
    binding_base = fc._module_string_constants(effects_tree)[fc.BINDING_FLAG_CONSTANT]
    effect_types = set()
    for scan_dir in fc.SCAN_DIRS:
        for path in (fc.REPO / scan_dir).rglob("*.py"):
            relative = path.relative_to(fc.REPO)
            if "tests" in relative.parts or path.name.startswith("test_"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            effect_types.update(value for value, _ in fc._literal_dict_values(tree, "effect_type"))
    expected = {
        f"{binding_base}_{''.join(c if c.isalnum() else '_' for c in effect_type).upper()}"
        for effect_type in effect_types
    }
    catalogued = {
        name for name in flags if name.startswith(f"{binding_base}_")
    }
    assert catalogued == expected
