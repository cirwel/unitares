"""Versioned descriptive model/harness provenance for governance writes.

The envelope built here is measurement context.  It is deliberately barred
from identity resolution, verdict selection, and policy dispatch.  Exact-model
analysis may use only prospectively captured envelopes whose source says that
the provider or harness reported the identifier; legacy display names and flat
``model`` fields are never upgraded into exact attribution.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any, Optional


RUNTIME_PROVENANCE_SCHEMA = "s22.runtime_provenance.v1"

MODEL_SOURCES = frozenset(
    {
        "provider_reported",
        "harness_reported",
        "caller_declared",
        "transport_inferred",
        "unavailable",
    }
)
HARNESS_SOURCES = frozenset(
    {
        "harness_reported",
        "caller_declared",
        "transport_user_agent",
        "unavailable",
    }
)
ADAPTER_SOURCES = frozenset(
    {
        "harness_reported",
        "caller_declared",
        "server_configured",
        "unavailable",
    }
)
EXACT_MODEL_SOURCES = frozenset({"provider_reported", "harness_reported"})
EXACT_HARNESS_SOURCES = frozenset(
    {"harness_reported", "transport_user_agent"}
)
MODEL_REPORTING_CHANNELS = frozenset(
    {
        "none",
        "transport_header",
        "request_context",
        "legacy_flat_argument",
        "handler_fallback",
        "user_agent",
        "identity_argument",
        "persisted_envelope",
        "host_hook_payload",
    }
)

_MODEL_MAX = 160
_PROVIDER_MAX = 80
_HARNESS_MAX = 80
_VERSION_MAX = 80
_ADAPTER_MAX = 80
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@-]*$")
_SENSITIVE_RE = re.compile(
    r"(?i)(?:\bbearer\s+|\bapi[_-]?key\b|\bpassword\b|\bpasswd\b|"
    r"\bsecret\b|\btoken\s*[:=]|\bsk-[A-Za-z0-9_-]{8,})"
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _safe_identifier(value: Any, *, maximum: int) -> tuple[Optional[str], str]:
    """Return a compact identifier or an explicit non-attribution reason.

    Values are rejected rather than truncated: truncating a model identifier
    would silently turn one model into another cohort key.  URLs and common
    credential shapes are also rejected so descriptive telemetry cannot become
    a secret exfiltration surface.
    """
    if value is None:
        return None, "not_exposed"
    if not isinstance(value, str):
        return None, "invalid_type"
    text = value.strip()
    if not text:
        return None, "not_exposed"
    if len(text) > maximum:
        return None, "value_too_long"
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        return None, "control_character_rejected"
    if "://" in text or _SENSITIVE_RE.search(text):
        return None, "redacted_sensitive_value"
    if not _SAFE_IDENTIFIER_RE.fullmatch(text):
        return None, "invalid_identifier_format"
    return text, "available"


def _source(value: Any, *, allowed: frozenset[str], default: str) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in allowed:
            return normalized
    return default


def _verification(source: str) -> str:
    return {
        "provider_reported": "provider_reported_unverified",
        "harness_reported": "harness_reported_unverified",
        "caller_declared": "unverified",
        "transport_inferred": "inferred_family_only",
        "transport_user_agent": "transport_observed",
        "server_configured": "server_configured",
        "unavailable": "unavailable",
    }.get(source, "unverified")


def _reporting_channel(value: Any, *, model_available: bool) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in MODEL_REPORTING_CHANNELS:
            return normalized
    return "request_context" if model_available else "none"


def _coerce_exact(value: Any, *, source: str) -> bool:
    if source not in EXACT_MODEL_SOURCES:
        return False
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _infer_model_family(user_agent: Optional[str]) -> Optional[str]:
    """Return a coarse family only; never manufacture an exact model id."""
    ua = (user_agent or "").lower()
    if not ua:
        return None
    if any(marker in ua for marker in ("codex", "chatgpt", "openai", "gpt")):
        return "gpt-family"
    if any(marker in ua for marker in ("claude", "anthropic")):
        return "claude-family"
    if any(marker in ua for marker in ("gemini", "google")):
        return "gemini-family"
    if "llama" in ua:
        return "llama-family"
    return None


def infer_harness_version(
    user_agent: Optional[str], harness_type: Optional[str]
) -> Optional[str]:
    """Extract a product version only when the UA names the known harness.

    A generic first ``product/version`` token is intentionally not accepted;
    it could be an HTTP library or proxy rather than the agent harness.
    """
    ua = user_agent or ""
    harness = (harness_type or "").strip().lower().replace("_", "-")
    aliases: tuple[str, ...]
    if "codex" in harness:
        aliases = ("codex", "codex-cli", "codex-cli-rs", "codex_cli_rs")
    elif "claude" in harness:
        aliases = ("claude", "claude-code", "claude_code")
    elif "cursor" in harness:
        aliases = ("cursor",)
    elif "chatgpt" in harness:
        aliases = ("chatgpt",)
    else:
        return None

    names = "|".join(re.escape(alias) for alias in aliases)
    match = re.search(
        rf"(?i)(?:^|[\s;(])(?:{names})[/\s_-]+v?"
        r"([0-9][0-9A-Za-z.+-]{0,79})",
        ua,
    )
    return match.group(1) if match else None


def runtime_signal_fields_from_headers(headers: Mapping[str, str]) -> dict[str, Any]:
    """Extract the small model/harness header contract without storing raw UA."""
    lowered = {str(key).lower(): value for key, value in headers.items()}

    def get(*names: str) -> Optional[str]:
        for name in names:
            value = lowered.get(name.lower())
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    model = get("x-unitares-model")
    model_provider = get("x-unitares-model-provider")
    harness_type = get("x-unitares-harness-type")
    harness_version = get("x-unitares-harness-version")
    return {
        "reported_model": model,
        "model_provider": model_provider,
        "model_provenance_source": (
            get("x-unitares-model-source")
            or ("harness_reported" if model or model_provider else None)
        ),
        "reported_harness_type": harness_type,
        "harness_version": harness_version,
        "harness_provenance_source": (
            get("x-unitares-harness-source")
            or (
                "harness_reported"
                if harness_type or harness_version
                else None
            )
        ),
        "adapter_type": get("x-unitares-adapter-type"),
        "adapter_version": get("x-unitares-adapter-version"),
    }


def build_runtime_provenance_from_values(
    *,
    model_identifier: Any = None,
    model_provider: Any = None,
    model_source: Any = None,
    model_exact: Any = None,
    model_channel: str = "none",
    provider_source: Any = None,
    harness_type: Any = None,
    harness_type_source: Any = None,
    harness_version: Any = None,
    harness_version_source: Any = None,
    adapter_type: Any = None,
    adapter_version: Any = None,
    adapter_source: Any = None,
    record_status: str = "captured",
) -> dict[str, Any]:
    """Build and sanitize one complete prospective provenance envelope."""
    model_id, model_reason = _safe_identifier(
        model_identifier, maximum=_MODEL_MAX
    )
    provider, provider_reason = _safe_identifier(
        model_provider, maximum=_PROVIDER_MAX
    )
    model_source_value = _source(
        model_source,
        allowed=MODEL_SOURCES,
        default="caller_declared" if model_identifier is not None else "unavailable",
    )
    if model_id is None:
        model_source_value = "unavailable"
    exact = bool(model_id) and _coerce_exact(model_exact, source=model_source_value)
    reporting_channel = _reporting_channel(
        model_channel, model_available=model_id is not None
    )

    harness, harness_reason = _safe_identifier(
        harness_type, maximum=_HARNESS_MAX
    )
    harness_type_source_value = _source(
        harness_type_source,
        allowed=HARNESS_SOURCES,
        default="caller_declared" if harness_type is not None else "unavailable",
    )
    if harness is None:
        harness_type_source_value = "unavailable"

    version, version_reason = _safe_identifier(
        harness_version, maximum=_VERSION_MAX
    )
    harness_version_source_value = _source(
        harness_version_source,
        allowed=HARNESS_SOURCES,
        default=(
            harness_type_source_value
            if harness_version is not None
            else "unavailable"
        ),
    )
    if version is None:
        harness_version_source_value = "unavailable"

    adapter, adapter_reason = _safe_identifier(
        adapter_type, maximum=_ADAPTER_MAX
    )
    adapter_ver, adapter_version_reason = _safe_identifier(
        adapter_version, maximum=_VERSION_MAX
    )
    adapter_source_value = _source(
        adapter_source,
        allowed=ADAPTER_SOURCES,
        default="caller_declared" if adapter_type is not None else "unavailable",
    )
    if adapter is None:
        adapter_source_value = "unavailable"

    provider_source_value = _source(
        provider_source,
        allowed=MODEL_SOURCES,
        default=model_source_value if provider is not None else "unavailable",
    )
    if provider is None:
        provider_source_value = "unavailable"

    return {
        "schema": RUNTIME_PROVENANCE_SCHEMA,
        "record_status": record_status,
        "model": {
            "identifier": model_id,
            "provider": provider,
            "source": model_source_value,
            "provider_source": provider_source_value,
            "reporting_channel": reporting_channel,
            "exact": exact,
            "verification": _verification(model_source_value),
            "missing_reason": None if model_id else model_reason,
            "provider_missing_reason": None if provider else provider_reason,
        },
        "harness": {
            "type": harness,
            "version": version,
            "type_source": harness_type_source_value,
            "version_source": harness_version_source_value,
            "type_verification": _verification(harness_type_source_value),
            "version_verification": _verification(harness_version_source_value),
            "missing_reason": None if harness else harness_reason,
            "version_missing_reason": None if version else version_reason,
        },
        "adapter": {
            "type": adapter,
            "version": adapter_ver,
            "source": adapter_source_value,
            "verification": _verification(adapter_source_value),
            "missing_reason": None if adapter else adapter_reason,
            "version_missing_reason": (
                None if adapter_ver else adapter_version_reason
            ),
        },
        "authority": {
            "role": "descriptive_context",
            "is_identity_proof": False,
            "is_verdict_authority": False,
            "is_policy_dispatch_key": False,
        },
    }


def build_runtime_provenance(
    arguments: Mapping[str, Any],
    *,
    signals: Any = None,
    fallback_model_identifier: Optional[str] = None,
    fallback_model_source: Optional[str] = None,
    fallback_harness_type: Optional[str] = None,
) -> dict[str, Any]:
    """Normalize request, transport, and legacy hints into one envelope.

    Transport headers win over request-body claims.  A legacy flat
    ``model``/``model_type`` remains caller-declared and non-exact.  User-Agent
    fallback yields only a coarse family and can therefore never enter an exact
    model cohort.
    """
    args = _mapping(arguments)
    public = _mapping(args.get("provenance_context"))
    nested = _mapping(
        args.get("runtime_provenance")
        or public.get("runtime_provenance")
        or args.get("model_provenance")
        or public.get("model_provenance")
    )
    nested_model = _mapping(nested.get("model"))
    nested_harness = _mapping(nested.get("harness"))
    nested_adapter = _mapping(nested.get("adapter"))

    signal_model = getattr(signals, "reported_model", None) if signals else None
    nested_model_id = _first_present(nested_model, "identifier", "id", "model")
    flat_model = _first_present(args, "model", "model_type")
    if flat_model is None:
        flat_model = _first_present(public, "model", "model_type")

    if signal_model is not None:
        model_identifier = signal_model
        model_source = getattr(signals, "model_provenance_source", None)
        model_exact = True
        model_channel = "transport_header"
    elif nested_model_id is not None:
        model_identifier = nested_model_id
        model_source = nested_model.get("source")
        model_exact = nested_model.get("exact")
        model_channel = str(
            nested_model.get("reporting_channel") or "request_context"
        )
    elif flat_model is not None:
        model_identifier = flat_model
        model_source = _first_present(args, "model_source") or _first_present(
            public, "model_source"
        )
        model_source = model_source or "caller_declared"
        model_exact = False
        model_channel = "legacy_flat_argument"
    elif fallback_model_identifier is not None:
        model_identifier = fallback_model_identifier
        model_source = fallback_model_source or "transport_inferred"
        model_exact = False
        model_channel = "handler_fallback"
    else:
        family = _infer_model_family(
            getattr(signals, "user_agent", None) if signals else None
        )
        model_identifier = family
        model_source = "transport_inferred" if family else "unavailable"
        model_exact = False
        model_channel = "user_agent" if family else "none"

    signal_provider = getattr(signals, "model_provider", None) if signals else None
    model_provider = (
        signal_provider
        if signal_provider is not None
        else _first_present(nested_model, "provider")
    )
    if model_provider is None:
        model_provider = _first_present(args, "model_provider") or _first_present(
            public, "model_provider"
        )
    provider_source = (
        getattr(signals, "model_provenance_source", None)
        if signal_provider is not None
        else nested_model.get("provider_source")
    )

    signal_harness = (
        getattr(signals, "reported_harness_type", None) if signals else None
    )
    nested_harness_type = _first_present(nested_harness, "type", "harness_type")
    explicit_harness = _first_present(args, "harness_type", "client_hint")
    if explicit_harness is None:
        explicit_harness = _first_present(public, "harness_type", "harness")
    signal_detected_harness = getattr(signals, "client_hint", None) if signals else None

    if signal_harness is not None:
        harness_type = signal_harness
        harness_type_source = getattr(signals, "harness_provenance_source", None)
    elif nested_harness_type is not None:
        harness_type = nested_harness_type
        harness_type_source = nested_harness.get("type_source") or nested_harness.get(
            "source"
        )
    elif explicit_harness is not None:
        harness_type = explicit_harness
        harness_type_source = "caller_declared"
    elif signal_detected_harness is not None:
        harness_type = signal_detected_harness
        harness_type_source = "transport_user_agent"
    else:
        harness_type = fallback_harness_type
        harness_type_source = (
            "transport_user_agent" if fallback_harness_type else "unavailable"
        )

    signal_version = getattr(signals, "harness_version", None) if signals else None
    nested_version = _first_present(nested_harness, "version", "harness_version")
    flat_version = _first_present(args, "harness_version") or _first_present(
        public, "harness_version"
    )
    if signal_version is not None:
        harness_version = signal_version
        harness_version_source = getattr(
            signals, "harness_provenance_source", None
        )
    elif nested_version is not None:
        harness_version = nested_version
        harness_version_source = nested_harness.get(
            "version_source"
        ) or nested_harness.get("source")
    elif flat_version is not None:
        harness_version = flat_version
        harness_version_source = "caller_declared"
    else:
        harness_version = infer_harness_version(
            getattr(signals, "user_agent", None) if signals else None,
            str(harness_type) if harness_type is not None else None,
        )
        harness_version_source = (
            "transport_user_agent" if harness_version else "unavailable"
        )

    signal_adapter_type = getattr(signals, "adapter_type", None) if signals else None
    signal_adapter_version = (
        getattr(signals, "adapter_version", None) if signals else None
    )
    adapter_type = signal_adapter_type or _first_present(nested_adapter, "type")
    adapter_version = signal_adapter_version or _first_present(
        nested_adapter, "version"
    )
    adapter_source = (
        "harness_reported"
        if signal_adapter_type or signal_adapter_version
        else nested_adapter.get("source")
    )

    return build_runtime_provenance_from_values(
        model_identifier=model_identifier,
        model_provider=model_provider,
        model_source=model_source,
        model_exact=model_exact,
        model_channel=model_channel,
        provider_source=provider_source,
        harness_type=harness_type,
        harness_type_source=harness_type_source,
        harness_version=harness_version,
        harness_version_source=harness_version_source,
        adapter_type=adapter_type,
        adapter_version=adapter_version,
        adapter_source=adapter_source,
    )


def normalize_persisted_runtime_provenance(value: Any) -> dict[str, Any]:
    """Read a persisted envelope without upgrading legacy or malformed rows."""
    raw = _mapping(value)
    schema = raw.get("schema")
    if schema != RUNTIME_PROVENANCE_SCHEMA:
        status = "legacy_unversioned" if not schema else "unsupported_schema"
        return build_runtime_provenance_from_values(record_status=status)

    model = _mapping(raw.get("model"))
    harness = _mapping(raw.get("harness"))
    adapter = _mapping(raw.get("adapter"))
    return build_runtime_provenance_from_values(
        model_identifier=model.get("identifier"),
        model_provider=model.get("provider"),
        model_source=model.get("source"),
        model_exact=model.get("exact"),
        model_channel=str(model.get("reporting_channel") or "persisted_envelope"),
        provider_source=model.get("provider_source"),
        harness_type=harness.get("type"),
        harness_type_source=harness.get("type_source"),
        harness_version=harness.get("version"),
        harness_version_source=harness.get("version_source"),
        adapter_type=adapter.get("type"),
        adapter_version=adapter.get("version"),
        adapter_source=adapter.get("source"),
        record_status=str(raw.get("record_status") or "captured"),
    )


def exact_model_attribution_status(envelope: Mapping[str, Any]) -> str:
    """Classify whether a row is eligible for exact-model cohorting."""
    normalized = normalize_persisted_runtime_provenance(envelope)
    if normalized["record_status"] != "captured":
        return normalized["record_status"]
    model = normalized["model"]
    harness = normalized["harness"]
    if not model["identifier"]:
        return "model_unavailable"
    if model["source"] == "transport_inferred":
        return "inferred_family_only"
    if not model["exact"] or model["source"] not in EXACT_MODEL_SOURCES:
        return "model_unverified"
    if not harness["type"]:
        return "harness_type_unavailable"
    if harness["type_source"] not in EXACT_HARNESS_SOURCES:
        return "harness_unverified"
    if (
        harness["version"]
        and harness["version_source"] not in EXACT_HARNESS_SOURCES
    ):
        return "harness_version_unverified"
    return "eligible_exact"
