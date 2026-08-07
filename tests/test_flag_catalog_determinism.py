"""The generated flag catalog must not depend on filesystem walk order.

`scripts/dev/flag_catalog.py` walks SCAN_DIRS with rglob, whose order is
filesystem-dependent (APFS vs ext4). Two rendered fields are order-sensitive:
`purpose` takes the first non-empty docstring encountered, and `sites` preserves
insertion order. Without a sort, the generated docs/FLAGS.md is platform-specific
and its CI freshness gate (`--check`, run in the smoke job) passes only on
whichever OS last ran the generator.

Observed 2026-08-06: regenerating on macOS turned the Linux CI gate red with
"docs/FLAGS.md is stale", and the diff implicated four flags the author had never
touched (UNITARES_AGENT_LOCK_BACKEND, UNITARES_FIRST_RUN, UNITARES_LLM_MODEL,
UNITARES_UDS_SOCKET). The failure is invisible locally — every interpreter on the
authoring machine agrees with the file it just wrote.
"""

import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts/dev"))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import flag_catalog as fc  # noqa: E402


def _render_with_walk_order(seed: int) -> str:
    """Render the catalog with rglob deliberately returning a foreign order."""
    original = pathlib.Path.rglob

    def shuffled(self, pattern):
        items = list(original(self, pattern))
        random.Random(seed).shuffle(items)
        return iter(items)

    pathlib.Path.rglob = shuffled
    try:
        return fc.render(fc.collect())
    finally:
        pathlib.Path.rglob = original


def test_output_is_independent_of_walk_order():
    """Any two filesystem orders must produce byte-identical output."""
    a = _render_with_walk_order(1234)
    b = _render_with_walk_order(999)
    assert a == b, (
        "flag_catalog output depends on filesystem walk order — docs/FLAGS.md "
        "will be stale on any machine whose rglob order differs from the "
        "author's, reddening CI with a diff on untouched flags"
    )


def test_collect_sorts_its_walk():
    """Guard the mechanism, not just the symptom: a future refactor that drops
    the sort would reintroduce a failure that is invisible on the machine that
    causes it."""
    import inspect

    src = inspect.getsource(fc.collect)
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "sorted(" in code and "rglob" in code, (
        "collect() must sort its rglob walk"
    )
