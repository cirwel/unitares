"""Entry-point discovery for third-party resident-progress sources.

The resident *roster* has been deployment configuration since the
`UNITARES_RESIDENTS` / `UNITARES_RESIDENT_PROGRESS_MANIFEST` split (see
``docs/operations/resident-roster.md``). The *sources* those manifests point
at were not: ``background_tasks.py`` built a fixed dict of six first-party
classes, and a manifest entry naming anything else resolved to a ``KeyError``
on every tick. So an operator could declare their own resident but could not
supply the metric that says whether it is making progress — the last step of
"bring your own resident" required a PR to this repo.

This module closes that gap the idiomatic way: a distribution declares

    [project.entry-points."unitares.resident_progress_sources"]
    my_source = "mypkg.sources:MySource"

and the target is called with the db handle to produce a
:class:`~src.resident_progress.sources.ResidentProgressSource`.

**Why discovery is on by default.** The fleet convention is that a new
surface ships flag-off (effect-binding, governed spawn, dialectic). That
convention is about surfaces which *act*; this one only registers a
read-only batched SELECT, and it is inert unless the operator has already
pip-installed a package into the server environment — which is the same
trust boundary every other dependency sits behind. Requiring an extra env
var would defeat the plug-and-play property the seam exists to provide.
``UNITARES_RESIDENT_PROGRESS_PLUGINS=0`` disables it for a deployment that
wants the surface closed anyway.

**Why first-party names win a collision.** A plugin that could shadow
``kg_writes`` could silently redefine what "Vigil made progress" means, and
the snapshot rows would look identical. Builtins are therefore never
overridden; a colliding plugin is rejected with a logged reason.

**Why failures are contained, where Vigil's are not.** ``VIGIL_CHECK_PLUGINS``
(``agents/vigil/checks/registry.py``) deliberately lets a bad plugin raise —
"typos should be loud" — and that is right for Vigil, which runs ``--once``
on a 30-minute timer: the crash surfaces to launchd and the next cycle
retries. This probe is a long-lived in-server background task spawned through
``_supervised_create_task`` and is *not* in the restartable-task registry, so
a raise during setup kills progress monitoring until the next server restart
and cannot be unstuck from the dashboard. Same philosophy, different blast
radius: here loudness has to come from the log line rather than the traceback,
so every rejection is recorded and surfaced at WARNING by the caller.

The discovery mechanism also differs from Vigil's colon-separated module
paths, for two reasons: these sources need the ``db`` handle injected, so
import-side-effect self-registration does not fit; and entry points make
installing the distribution *sufficient*, with no edit to the server plist —
which is the property "bring your own resident" actually needs.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "unitares.resident_progress_sources"

# Opt-out, not opt-in — see module docstring.
PLUGINS_ENABLED_ENV = "UNITARES_RESIDENT_PROGRESS_PLUGINS"


@dataclass
class PluginLoadResult:
    """Outcome of one discovery pass.

    ``sources`` is safe to merge into the builtin dict as-is: every entry has
    already been validated and checked for collision. ``errors`` carries one
    human-readable line per rejected entry point — the caller logs them, so
    that this module stays testable without capturing log output.
    """
    sources: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def plugins_enabled(env: Optional[dict] = None) -> bool:
    """True unless the deployment explicitly disabled plugin discovery."""
    raw = (env if env is not None else os.environ).get(PLUGINS_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _iter_entry_points(group: str) -> Iterable[Any]:
    """Yield entry points in ``group``.

    Isolated so tests can monkeypatch a fake loader without constructing real
    distributions on disk.
    """
    from importlib.metadata import entry_points
    return entry_points(group=group)


def _validate(source: Any, expected_name: str) -> Optional[str]:
    """Return a rejection reason, or None when the source is usable.

    The contract is deliberately one string in three places: the entry-point
    name, the source's ``name`` attribute, and the manifest's ``source``
    field must all agree. A mismatch is rejected rather than papered over,
    because the manifest references ``source.name`` while the operator reads
    the entry-point name out of a pyproject — silently keying by one while
    they read the other is how a source ends up "installed but never used".
    """
    name = getattr(source, "name", None)
    if not isinstance(name, str) or not name:
        return "source has no non-empty str 'name' attribute"
    if name != expected_name:
        return (
            f"entry-point name {expected_name!r} does not match source.name "
            f"{name!r}; they must be identical (the manifest references it)"
        )
    fetch = getattr(source, "fetch", None)
    if not callable(fetch):
        return "source has no callable 'fetch'"
    return None


def discover_progress_sources(
    db: Any,
    *,
    builtin_names: Iterable[str],
    entry_point_loader: Optional[Callable[[str], Iterable[Any]]] = None,
    env: Optional[dict] = None,
) -> PluginLoadResult:
    """Load third-party progress sources declared via entry points.

    Args:
        db: the database handle passed to each plugin factory.
        builtin_names: first-party source names, which a plugin may not take.
        entry_point_loader: override for the entry-point lookup (tests).
        env: override for the process environment (tests).

    Never raises. One misbehaving distribution must not prevent the progress
    probe from starting — the probe is part of the detection layer, and a
    detection layer that fails to boot because a third party shipped a broken
    package is worse than one running with that source absent.
    """
    result = PluginLoadResult()
    if not plugins_enabled(env):
        logger.info(
            "[PROGRESS_FLAT] source plugin discovery disabled via %s",
            PLUGINS_ENABLED_ENV,
        )
        return result

    reserved = set(builtin_names)
    loader = entry_point_loader or _iter_entry_points

    try:
        eps = list(loader(ENTRY_POINT_GROUP))
    except Exception as exc:  # pragma: no cover - importlib.metadata failure
        result.errors.append(f"entry-point enumeration failed: {type(exc).__name__}: {exc}")
        return result

    for ep in eps:
        ep_name = getattr(ep, "name", "<unnamed>")
        if ep_name in reserved:
            result.errors.append(
                f"{ep_name!r}: shadows a first-party source; first-party wins"
            )
            continue
        if ep_name in result.sources:
            result.errors.append(
                f"{ep_name!r}: duplicate entry point; keeping the first"
            )
            continue
        try:
            factory = ep.load()
        except Exception as exc:
            result.errors.append(f"{ep_name!r}: load failed: {type(exc).__name__}: {exc}")
            continue
        try:
            source = factory(db)
        except Exception as exc:
            result.errors.append(
                f"{ep_name!r}: factory raised: {type(exc).__name__}: {exc}"
            )
            continue
        reason = _validate(source, ep_name)
        if reason is not None:
            result.errors.append(f"{ep_name!r}: {reason}")
            continue
        result.sources[ep_name] = source

    return result
