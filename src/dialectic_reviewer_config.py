"""The spawned dialectic reviewer's host list and lifetime, from a settings map.

Two callers resolve these the same way. The governance server resolves them
from its own environment when it builds a raw reviewer spawn
(``src/mcp_handlers/dialectic/orchestrator_dispatch.py``). For the
reviewer's launch profile (inference-hosts-config-v0 §2.5), the profiles
generator (``scripts/ops/orchestrator-profiles.py``) resolves them from the
server's settings and fixes the result in the profile. Each function takes
the settings as a mapping, so the same code gives the same answer in either
place. The reviewer itself does not import this: members under ``agents/``
may not import ``src`` (scripts/dev/check_component_boundaries.py, B1).

The profile's host list is therefore the server's resolution at generation
time: after editing the inference hosts file, regenerate the profiles file
(the orchestrator's start.sh does so at boot, and the orchestrator reloads a
changed file without a restart).

Light by design: the generator imports this without the server's handler
package.
"""
from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, Mapping

from src import inference_config

# The reviewer reads this as an invalid host list: no host is called and the
# local floor answers without approval authority. Set when the operator's
# inference hosts file does not load, or when the dialectic_reviewer job
# leaves no host the reviewer can call (inference-hosts-config-v0 step 3).
# Never left empty-by-accident: an empty list reads as "no list", which
# restores the local reviewer's approval.
REVIEWER_HOSTS_ERROR_ENV = "UNITARES_DIALECTIC_REVIEWER_HOSTS_ERROR"

# Builtin host ids the reviewer can call, and the names it accepts for them.
REVIEWER_BUILTINS = {
    "codex:host-adapter": "codex:host-adapter",
    "claude:host-adapter": "claude:host-adapter",
    "antigravity:host-adapter": "antigravity:host-adapter",
}
# Reviewer-list names that correspond to a builtin, for the file's switch-off.
REVIEWER_NAME_TO_HOST = {
    "codex": "codex:host-adapter", "codex:host-adapter": "codex:host-adapter",
    "claude": "claude:host-adapter", "claude:host-adapter": "claude:host-adapter",
    "antigravity": "antigravity:host-adapter", "agy": "antigravity:host-adapter",
    "antigravity:host-adapter": "antigravity:host-adapter",
}

# Must match agents.dialectic_reviewer.reviewer.DEFAULT_CONTINUATION_WAIT_S.
# A contract test pins the two values together without importing the runner into
# the governance server. The extra grace covers the initial model call (host
# backends allow up to seven minutes), onboarding, and protocol writes before
# the continuation clock starts.
DEFAULT_CONTINUATION_WAIT_S = 3600.0
# Must match agents.dialectic_reviewer.reviewer.CONTINUATION_TOTAL_WAIT_FACTOR:
# the reviewer restarts its wait after each filed synthesis, so its lifetime is
# up to this many waits and the reaper cap must outlast it.
CONTINUATION_TOTAL_WAIT_FACTOR = 3
REVIEWER_RUNTIME_GRACE_S = 900.0

# The largest seconds override honoured; anything above it counts as unset.
# Seven days is far past any real reviewer timeout or continuation window, and
# keeps the summed cap (three hosts plus the wait, in milliseconds) finite.
MAX_SECONDS_OVERRIDE = 7 * 24 * 3600.0

# Per-call timeout settings and defaults of the hosts a reviewer list may name,
# by every name the list accepts. Must match agents.dialectic_reviewer.host_list
# (HOSTS and its aliases); a contract test pins the two together without
# importing the runner into the governance server.
HOST_TIMEOUTS = {
    "codex": ("UNITARES_DIALECTIC_CODEX_TIMEOUT_S", 420.0),
    "claude": ("UNITARES_DIALECTIC_CLAUDE_TIMEOUT_S", 420.0),
    "antigravity": ("UNITARES_DIALECTIC_ANTIGRAVITY_TIMEOUT_S", 420.0),
    "external": ("UNITARES_DIALECTIC_EXTERNAL_TIMEOUT_S", 180.0),
}
HOST_TIMEOUT_ALIASES = {
    "codex:host-adapter": "codex",
    "claude:host-adapter": "claude",
    "agy": "antigravity",
    "antigravity:host-adapter": "antigravity",
    "openai_compat": "external",
    "openai-compatible": "external",
    "gemini": "external",
}


def is_usable_seconds(raw: Any) -> bool:
    """Finite and no larger than MAX_SECONDS_OVERRIDE (negatives pass: each
    reader gives a negative its own meaning)."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return False
    return math.isfinite(value) and value <= MAX_SECONDS_OVERRIDE


def seconds(raw: Any, default: float) -> float:
    """A seconds override, or ``default`` when it is not a usable number.

    ``float()`` accepts ``inf``, ``nan`` and ``1e309`` (which overflows to
    infinity), and a finite ``1e308`` still overflows once the cap is summed
    and scaled to milliseconds; ``int()`` of that cap raises. That raise
    happens in ``_build_spec``, before the dispatcher's exception handler, so
    it would escape instead of degrading to the in-process reviewer.
    """
    return float(raw) if is_usable_seconds(raw) else default


def reviewer_host_selection(environ: Mapping[str, str] | None = None) -> Dict[str, str]:
    """The reviewer's host-selection settings, resolved from ``environ``.

    With a ``dialectic_reviewer`` job in the operator's inference hosts file
    (``environ`` names it), the list is that job's order with hosts the
    reviewer cannot call (declared hosts until the reviewer design's step 4,
    the local and HF lanes) or that the file switches off removed. Without a
    job, UNITARES_DIALECTIC_REVIEWER_HOSTS / _HOST apply as before, minus hosts
    the file switches off. A file that does not load, or a selection that
    leaves no host, becomes an explicit error so the reviewer fails closed.
    """
    loaded = inference_config.load(environ)
    if loaded.state == "error" or loaded.config is None:
        return {
            "UNITARES_DIALECTIC_REVIEWER_HOSTS": "",
            "UNITARES_DIALECTIC_REVIEWER_HOST": "",
            REVIEWER_HOSTS_ERROR_ENV: f"the inference hosts file does not load ({loaded.error})",
        }
    config = loaded.config

    def switched_off(host_id: str) -> bool:
        entry = config.hosts.get(host_id)
        return entry is not None and not entry.enabled

    job = config.jobs.get("dialectic_reviewer")
    if job is not None:
        hosts = [h for h in job.prefer if h in REVIEWER_BUILTINS and not switched_off(h)]
        if not hosts:
            return {
                "UNITARES_DIALECTIC_REVIEWER_HOSTS": "",
                "UNITARES_DIALECTIC_REVIEWER_HOST": "",
                REVIEWER_HOSTS_ERROR_ENV: (
                    "no host in the dialectic_reviewer job can review ("
                    + ", ".join(job.prefer) + ")"
                ),
            }
        return {
            "UNITARES_DIALECTIC_REVIEWER_HOSTS": ",".join(REVIEWER_BUILTINS[h] for h in hosts),
            "UNITARES_DIALECTIC_REVIEWER_HOST": "",
            REVIEWER_HOSTS_ERROR_ENV: "",
        }
    hosts_raw = (os.environ if environ is None else environ).get("UNITARES_DIALECTIC_REVIEWER_HOSTS", "")
    host_raw = (os.environ if environ is None else environ).get("UNITARES_DIALECTIC_REVIEWER_HOST", "")
    selection = {
        "UNITARES_DIALECTIC_REVIEWER_HOSTS": hosts_raw,
        "UNITARES_DIALECTIC_REVIEWER_HOST": host_raw,
        REVIEWER_HOSTS_ERROR_ENV: "",
    }
    if hosts_raw.strip():
        key = "UNITARES_DIALECTIC_REVIEWER_HOSTS"
        names = [part.strip() for part in hosts_raw.split(",") if part.strip()]
    else:
        # The single-host setting names one host. Never split it: filtering a
        # malformed "codex,local" down to "local" would turn a value the
        # reviewer rejects into the floor with approval authority.
        key = "UNITARES_DIALECTIC_REVIEWER_HOST"
        names = [host_raw.strip()] if host_raw.strip() else []
    kept = [n for n in names if not switched_off(REVIEWER_NAME_TO_HOST.get(n.lower(), ""))]
    if names and not kept:
        selection.update({
            "UNITARES_DIALECTIC_REVIEWER_HOSTS": "",
            "UNITARES_DIALECTIC_REVIEWER_HOST": "",
            REVIEWER_HOSTS_ERROR_ENV: "every listed reviewer host is switched off in the inference hosts file",
        })
    elif kept != names:
        selection[key] = ",".join(kept)
    return selection


def extra_host_budget_s(environ: Mapping[str, str] | None = None) -> float:
    """Time for the listed hosts after the first.

    REVIEWER_RUNTIME_GRACE_S covers one model call. When the first listed host
    fails, each later one may take its full timeout before the verdict, so the
    lifetime cap grows by those timeouts (design: docs/proposals/active/
    dialectic-reviewer-hosts-v0.md 2.7). Unknown names add nothing: the
    reviewer refuses such a list and calls no host.
    """
    raw = reviewer_host_selection(environ)["UNITARES_DIALECTIC_REVIEWER_HOSTS"]
    names = [part.strip().lower() for part in raw.split(",") if part.strip()]
    extra = 0.0
    for name in names[1:3]:
        key = HOST_TIMEOUT_ALIASES.get(name, name)
        if key not in HOST_TIMEOUTS:
            continue
        env_name, default = HOST_TIMEOUTS[key]
        extra += max(0.0, seconds((os.environ if environ is None else environ).get(env_name, default), default))
    return extra


def reviewer_max_runtime_ms(environ: Mapping[str, str] | None = None) -> int:
    """Lifetime cap for one spawned reviewer.

    The agent-orchestrator defaults to thirty minutes, which is shorter than
    the dialectic protocol's one-hour synthesis window. Size this spawn's cap
    from the same operator override the reviewer consumes, plus bounded setup
    and model-call grace. Invalid/negative overrides fail back to the default
    window rather than disabling the orchestrator's reaper.
    """
    raw = (os.environ if environ is None else environ).get("UNITARES_DIALECTIC_CONTINUATION_WAIT_S", "3600")
    wait_s = seconds(raw, DEFAULT_CONTINUATION_WAIT_S)
    if wait_s < 0:
        wait_s = DEFAULT_CONTINUATION_WAIT_S
    total_s = wait_s * CONTINUATION_TOTAL_WAIT_FACTOR
    return max(1, int((total_s + REVIEWER_RUNTIME_GRACE_S + extra_host_budget_s(environ)) * 1000))


DEFAULT_GOVERNANCE_URL = "http://127.0.0.1:8767/mcp/"


def governance_url(environ: Mapping[str, str] | None = None) -> str:
    """The governance MCP URL the reviewer connects to.

    The reviewer passes this straight to GovernanceClient(mcp_url=...), whose
    streamable-http transport needs the /mcp/ path; a bare base URL makes
    session.initialize() hang then cancel (live-found 2026-06-23). A pathless
    URL is normalized, so either form (base or full mcp_url) works.
    """
    from urllib.parse import urlparse

    raw = (
        (os.environ if environ is None else environ).get("UNITARES_GOVERNANCE_URL")
        or (os.environ if environ is None else environ).get("GOVERNANCE_URL")
        or DEFAULT_GOVERNANCE_URL
    )
    if urlparse(raw).path in ("", "/"):
        raw = raw.rstrip("/") + "/mcp/"
    return raw


# The server's settings the reviewer reads, fixed into its launch profile as
# the server holds them. None is a credential: the two credentials the
# reviewer uses are bound by name (reviewer_profile_env). Literal names, so
# scripts/dev/flag_catalog.py can find the readers.
REVIEWER_PROFILE_SETTINGS = (
    "UNITARES_DIALECTIC_REVIEWER_HOSTS",
    "UNITARES_DIALECTIC_REVIEWER_HOST",
    "UNITARES_DIALECTIC_CLAUDE_MODEL",
    "UNITARES_DIALECTIC_CLAUDE_TIMEOUT_S",
    "UNITARES_DIALECTIC_CODEX_TIMEOUT_S",
    "UNITARES_DIALECTIC_ANTIGRAVITY_TIMEOUT_S",
    "UNITARES_DIALECTIC_EXTERNAL_BASE_URL",
    "UNITARES_DIALECTIC_EXTERNAL_MODEL",
    "UNITARES_DIALECTIC_EXTERNAL_API_KEY_ENV",
    "UNITARES_DIALECTIC_EXTERNAL_TIMEOUT_S",
    "UNITARES_DIALECTIC_EXTERNAL_MAX_TOKENS",
    "UNITARES_DIALECTIC_REVIEW_MAX_TOKENS",
    "UNITARES_DIALECTIC_CONTINUATION_WAIT_S",
    "UNITARES_DIALECTIC_CONTINUATION_POLL_S",
    "UNITARES_MODEL_BASE_URL",
    "UNITARES_MODEL_ID",
    "UNITARES_MODEL_LOCAL_HOSTS",
    "UNITARES_MODEL_PRIVACY",
    "UNITARES_TRUSTED_NETWORKS",
)

#: The reviewer's MCP credential, when the server's /mcp gate is configured.
MCP_BEARER_ENV = "UNITARES_MCP_BEARER_TOKEN"
#: The external host's key variable when UNITARES_DIALECTIC_EXTERNAL_API_KEY_ENV
#: names none (agents/dialectic_reviewer/host_backends._DEFAULT_KEY_ENV).
DEFAULT_EXTERNAL_KEY_ENV = "UNITARES_DIALECTIC_EXTERNAL_API_KEY"

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


def reviewer_profile_env(environ: Mapping[str, str] | None = None) -> tuple[Dict[str, str], Dict[str, str]]:
    """(fixed settings, secret bindings) for the reviewer's launch profile.

    Fixed: each REVIEWER_PROFILE_SETTINGS entry ``environ`` sets, with a
    seconds value that is not usable replaced by its default (as the raw
    spawn does); the normalized governance URL; and the host selection as
    reviewer_host_selection resolves it from ``environ`` (the inference hosts
    file it names included), always all three keys, error included, as the
    raw spawn forwards them. A setting ``environ`` leaves unset is left out:
    the profile's child starts from an empty environment, so it reads as
    unset, which is what the server used.

    Secrets, as child variable -> orchestrator variable, both the same name:
    the MCP bearer when the server has one, and, when an external host is
    configured, the key variable UNITARES_DIALECTIC_EXTERNAL_API_KEY_ENV names
    or else, when the server has it, DEFAULT_EXTERNAL_KEY_ENV. An external
    endpoint that needs no key binds none (the reviewer then sends a
    placeholder). Only the names come from ``environ``; the orchestrator
    resolves each value at spawn, and a profile whose secret is unset there
    refuses to start.
    """
    fixed = {name: value for name in REVIEWER_PROFILE_SETTINGS
             if (value := (os.environ if environ is None else environ).get(name))}
    seconds_defaults = dict(HOST_TIMEOUTS.values())
    seconds_defaults["UNITARES_DIALECTIC_CONTINUATION_WAIT_S"] = DEFAULT_CONTINUATION_WAIT_S
    for name, default in seconds_defaults.items():
        if name in fixed and not is_usable_seconds(fixed[name]):
            fixed[name] = f"{default:g}"
    fixed["UNITARES_GOVERNANCE_URL"] = governance_url(environ)
    fixed.update(reviewer_host_selection(environ))

    secrets: Dict[str, str] = {}
    if (os.environ if environ is None else environ).get(MCP_BEARER_ENV):
        secrets[MCP_BEARER_ENV] = MCP_BEARER_ENV
    key_env = ((os.environ if environ is None else environ).get("UNITARES_DIALECTIC_EXTERNAL_API_KEY_ENV") or "").strip()
    if not key_env and (os.environ if environ is None else environ).get(DEFAULT_EXTERNAL_KEY_ENV):
        key_env = DEFAULT_EXTERNAL_KEY_ENV
    if key_env and fixed.get("UNITARES_DIALECTIC_EXTERNAL_BASE_URL"):
        if not _ENV_NAME.fullmatch(key_env) or key_env in fixed:
            raise ValueError(
                "UNITARES_DIALECTIC_EXTERNAL_API_KEY_ENV must name an environment variable")
        secrets[key_env] = key_env
    return fixed, secrets
