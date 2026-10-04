"""The operator's ordered list of reviewer hosts. Pure.

Design: docs/proposals/active/dialectic-reviewer-hosts-v0.md, sections 2.1
and 2.2 (step 1).

``UNITARES_DIALECTIC_REVIEWER_HOSTS`` names up to three hosts in the order the
operator wants them tried. The reviewer may only shorten that order, by
skipping a host that returned no reply; it never adds, reorders or chooses
between hosts. ``UNITARES_DIALECTIC_REVIEWER_HOST`` is read as a one-item list
when the new name is unset.

An invalid list does not fall back to a guess: the plan carries the error, no
host is called, and the local floor answers without approval authority. The
operator asked for listed hosts, so nothing else may approve in their place.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Mapping, Optional

from urllib.parse import urlsplit

from src.local_inference_env import (
    endpoint_reaches_local_address,
    model_base_url,
    same_endpoint,
)

MAX_REVIEWER_HOSTS = 3

# Names that mean the local endpoint. In the legacy single-host setting they
# mean "no host" (the local model is the reviewer, as before the list existed);
# inside a list they are an error, because the floor is never a listed host.
FLOOR_NAMES = frozenset({"local", "ollama", "ollama:local"})


@dataclass(frozen=True)
class ListedHost:
    """A reviewer host a list may name.

    ``may_approve``: whether a verdict from this host may release a paused
    agent. ``family``: the model family the host's verdict is recorded under.
    Antigravity and the external host can serve several families and report
    no model, so theirs is recorded as unknown until a host pins its model.
    """

    key: str
    host_id: str
    family: str
    may_approve: bool
    timeout_env: str
    default_timeout_s: float


HOSTS: dict[str, ListedHost] = {
    "codex": ListedHost(
        "codex", "codex:host-adapter", "openai", True,
        "UNITARES_DIALECTIC_CODEX_TIMEOUT_S", 420.0,
    ),
    "claude": ListedHost(
        "claude", "claude:host-adapter", "anthropic", True,
        "UNITARES_DIALECTIC_CLAUDE_TIMEOUT_S", 420.0,
    ),
    "antigravity": ListedHost(
        "antigravity", "antigravity:host-adapter", "unknown", True,
        "UNITARES_DIALECTIC_ANTIGRAVITY_TIMEOUT_S", 420.0,
    ),
    # The single operator-configured OpenAI-compatible host. It could approve
    # before the list existed and keeps that; declared hosts (design step 4)
    # will default to may_approve=False.
    "external": ListedHost(
        "external", "external:openai-compatible", "unknown", True,
        "UNITARES_DIALECTIC_EXTERNAL_TIMEOUT_S", 180.0,
    ),
}

_ALIASES = {
    "codex:host-adapter": "codex",
    "claude:host-adapter": "claude",
    "agy": "antigravity",
    "antigravity:host-adapter": "antigravity",
    "openai_compat": "external",
    "openai-compatible": "external",
    # Selects the configured external host, never vendor logic.
    "gemini": "external",
}


@dataclass(frozen=True)
class HostPlan:
    """What the reviewer will try, in order.

    ``listed``: the operator configured at least one host (or an invalid list).
    With no list the local model is the reviewer, as it always was, and keeps
    approval authority; with a list, only a listed host's verdict may approve.
    """

    hosts: tuple[ListedHost, ...]
    listed: bool
    error: Optional[str] = None
    raw: str = ""
    digest: str = ""

    @property
    def keys(self) -> list[str]:
        return [host.key for host in self.hosts]


def _digest(env: Mapping[str, str], keys: list[str]) -> str:
    """A short fingerprint of the configuration a verdict was produced under,
    so two verdicts can be told apart when the list or a host's model moved."""
    parts = [",".join(keys)]
    for name in (
        "UNITARES_DIALECTIC_CLAUDE_MODEL",
        "UNITARES_DIALECTIC_EXTERNAL_BASE_URL",
        "UNITARES_DIALECTIC_EXTERNAL_MODEL",
    ):
        parts.append(f"{name}={env.get(name, '')}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def reviewer_host_plan(
    env: Optional[Mapping[str, str]] = None, *, local_base_url: Optional[str] = None
) -> HostPlan:
    """The operator's ordered reviewer host list, read as a plan. Pure given ``env``.

    An external host that can reach a local address (loopback in any
    spelling, numeric shorthand, a trusted network, a name listed as local or
    resolving to a local address) is refused: it can be the local
    floor under another name, and listing it would launder the local model
    into an approver. So is the configured local endpoint itself
    (``local_base_url``, default ``model_base_url()``), wherever it sits. A
    strong model on the operator's own network is a declared host with an
    explicit ``may_approve`` (design step 4), not this.

    The check reads DNS once, here; the call resolves again when it connects.
    That guards against misconfiguration, not a hostile resolver: whoever
    controls the external host's DNS can answer an approval from a server of
    their own, which is more than rebinding to the local model would give.
    """
    if env is None:
        # Literal reads, so scripts/dev/flag_catalog.py indexes each setting.
        env = {
            "UNITARES_DIALECTIC_REVIEWER_HOSTS": os.environ.get("UNITARES_DIALECTIC_REVIEWER_HOSTS", ""),
            "UNITARES_DIALECTIC_REVIEWER_HOST": os.environ.get("UNITARES_DIALECTIC_REVIEWER_HOST", ""),
            "UNITARES_DIALECTIC_CLAUDE_MODEL": os.environ.get("UNITARES_DIALECTIC_CLAUDE_MODEL", ""),
            "UNITARES_DIALECTIC_EXTERNAL_BASE_URL": os.environ.get("UNITARES_DIALECTIC_EXTERNAL_BASE_URL", ""),
            "UNITARES_DIALECTIC_EXTERNAL_MODEL": os.environ.get("UNITARES_DIALECTIC_EXTERNAL_MODEL", ""),
        }
    raw_list = env.get("UNITARES_DIALECTIC_REVIEWER_HOSTS", "").strip()
    if raw_list:
        names = [part.strip().lower() for part in raw_list.split(",") if part.strip()]
        raw = raw_list
    else:
        legacy = env.get("UNITARES_DIALECTIC_REVIEWER_HOST", "").strip().lower()
        raw = legacy
        if not legacy or legacy in FLOOR_NAMES:
            return HostPlan(hosts=(), listed=False, raw=raw)
        names = [legacy]

    def invalid(reason: str) -> HostPlan:
        return HostPlan(
            hosts=(), listed=True, error=f"Invalid reviewer host list '{raw}': {reason}", raw=raw,
        )

    if not names:
        return invalid("no host named")
    if len(names) > MAX_REVIEWER_HOSTS:
        return invalid(f"at most {MAX_REVIEWER_HOSTS} hosts")
    hosts: list[ListedHost] = []
    for name in names:
        if name in FLOOR_NAMES:
            return invalid(f"'{name}' is the local floor, which cannot be listed")
        key = _ALIASES.get(name, name)
        host = HOSTS.get(key)
        if host is None:
            return invalid(f"unknown host '{name}'")
        if host in hosts:
            return invalid(f"'{name}' is listed twice")
        hosts.append(host)
    if any(host.key == "external" for host in hosts):
        external = env.get("UNITARES_DIALECTIC_EXTERNAL_BASE_URL", "").strip()
        if external:
            # The error lands in persisted provenance, so it names the endpoint
            # by scheme, host and port only: never userinfo, path or query,
            # any of which can carry a credential.
            try:
                parts = urlsplit(external)
                port = parts.port
            except ValueError:
                return invalid("the external host URL does not parse")
            label = f"{parts.scheme}://{parts.hostname or ''}" + (f":{port}" if port else "")
            if endpoint_reaches_local_address(external):
                return invalid(
                    f"the external host {label} is a local endpoint, which may "
                    "object but not approve"
                )
            # The configured local model can sit on a public address (with
            # UNITARES_MODEL_PRIVACY=local); it is still the floor.
            if same_endpoint(external, local_base_url or model_base_url()):
                return invalid(
                    f"the external host {label} is the local model endpoint, "
                    "which may object but not approve"
                )
    keys = [host.key for host in hosts]
    return HostPlan(hosts=tuple(hosts), listed=True, raw=raw, digest=_digest(env, keys))
