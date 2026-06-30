"""Inference host registry for model delegation.

Exposes what the server can route today: the synchronous local hosts (Ollama, HF
via ``call_model``) plus the strong-heterogeneous subscription-CLI hosts (Codex,
Claude) served ASYNCHRONOUSLY via the agent-orchestrator (``host_adapter.py``).
It does not store credentials; availability is probed live (socket / CLI on PATH /
opt-in flag). The strong hosts are gated by ``UNITARES_HOST_ADAPTER_ENABLED``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import os
import socket
from typing import Any

from .host_adapter import host_adapter_available, host_adapter_enabled


@dataclass(frozen=True)
class InferenceHost:
    host_id: str
    display_name: str
    provider_kind: str
    transport: str
    configured: bool
    available: bool
    privacy_class: str
    cost_class: str
    accountability_class: str
    capabilities: list[str]
    models: list[str]
    implementation_status: str
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ollama_available() -> bool:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        result = sock.connect_ex(("localhost", 11434))
        sock.close()
        return result == 0
    except Exception:
        return False


def _hf_token_present() -> bool:
    return bool(os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_TOKEN"))


def _base_hosts() -> list[InferenceHost]:
    ollama_model = os.getenv("UNITARES_LLM_MODEL", "gemma4:latest")
    hf_configured = _hf_token_present()
    return [
        InferenceHost(
            host_id="ollama:local",
            display_name="Ollama local",
            provider_kind="ollama",
            transport="openai_compatible_http",
            configured=True,
            available=_ollama_available(),
            privacy_class="local",
            cost_class="local_free",
            accountability_class="tool_evidence",
            capabilities=["reasoning", "generation", "analysis"],
            models=[ollama_model],
            implementation_status="active",
            notes=(
                "Local Ollama OpenAI-compatible endpoint. Model names are "
                "passed through; use `ollama list` on the host for inventory."
            ),
        ),
        InferenceHost(
            host_id="hf:router",
            display_name="Hugging Face Inference Providers",
            provider_kind="hf",
            transport="openai_compatible_http",
            configured=hf_configured,
            available=hf_configured,
            privacy_class="external_cloud",
            cost_class="token_or_free_tier",
            accountability_class="tool_evidence",
            capabilities=["reasoning", "generation", "analysis"],
            models=["deepseek-ai/DeepSeek-R1:fastest", "Qwen/Qwen2.5-72B-Instruct:fastest"],
            implementation_status="active",
            notes="Requires HF_TOKEN or HUGGINGFACE_TOKEN in the server environment.",
        ),
        InferenceHost(
            host_id="codex:host-adapter",
            display_name="Codex host adapter",
            provider_kind="codex_host_adapter",
            transport="host_adapter",
            configured=host_adapter_enabled(),
            available=host_adapter_available("codex:host-adapter"),
            privacy_class="operator_authorized_external",
            cost_class="subscription_backed",
            accountability_class="tool_evidence",
            capabilities=["reasoning", "review", "summarize"],
            models=["codex"],
            implementation_status=(
                "active" if host_adapter_available("codex:host-adapter") else "opt_in"
            ),
            notes=(
                "Subscription-backed Codex (`codex exec`) served ASYNC via the "
                "agent-orchestrator (not the sync call_model path). Enable with "
                "UNITARES_HOST_ADAPTER_ENABLED=1; needs the codex CLI on PATH + "
                "AGENT_ORCHESTRATOR_BEARER_TOKEN. See support/host_adapter.py."
            ),
        ),
        InferenceHost(
            host_id="claude:host-adapter",
            display_name="Claude host adapter",
            provider_kind="claude_host_adapter",
            transport="host_adapter",
            configured=host_adapter_enabled(),
            available=host_adapter_available("claude:host-adapter"),
            privacy_class="operator_authorized_external",
            cost_class="subscription_backed",
            accountability_class="tool_evidence",
            capabilities=["reasoning", "review", "summarize"],
            models=["claude"],
            implementation_status=(
                "active" if host_adapter_available("claude:host-adapter") else "opt_in"
            ),
            notes=(
                "Subscription-backed Claude (`claude -p`) served ASYNC via the "
                "agent-orchestrator (not the sync call_model path). Enable with "
                "UNITARES_HOST_ADAPTER_ENABLED=1; needs the claude CLI on PATH + "
                "AGENT_ORCHESTRATOR_BEARER_TOKEN. See support/host_adapter.py."
            ),
        ),
    ]


def list_inference_hosts(
    *,
    include_unconfigured: bool = True,
    provider_kind: str | None = None,
) -> list[dict[str, Any]]:
    hosts = _base_hosts()
    if provider_kind:
        hosts = [host for host in hosts if host.provider_kind == provider_kind]
    if not include_unconfigured:
        hosts = [host for host in hosts if host.configured]
    return [host.to_dict() for host in hosts]


def get_inference_host(host_id: str) -> dict[str, Any] | None:
    for host in _base_hosts():
        if host.host_id == host_id:
            return host.to_dict()
    return None


def host_for_routed_provider(provider: str) -> dict[str, Any]:
    host_id = "hf:router" if provider == "hf" else "ollama:local"
    host = get_inference_host(host_id)
    if host is not None:
        return host

    # Defensive fallback for future providers not yet in the static registry.
    return replace(
        _base_hosts()[0],
        host_id=f"{provider}:direct",
        display_name=f"{provider} direct",
        provider_kind=provider,
        transport="direct",
        configured=True,
        available=True,
        privacy_class="unknown",
        cost_class="unknown",
        implementation_status="unregistered",
        notes="Provider was routed but is not registered in the inference host catalog.",
    ).to_dict()
