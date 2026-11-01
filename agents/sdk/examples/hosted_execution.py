"""Single-execution SDK example; NOT an installed hosted-dot adapter.

The host must establish a real execution start before calling this function
ONCE. ``host_start_verified`` records the host's assertion, not verification
performed by this example. No room ID, label, timestamp or stored memory can
supply it. There is deliberately no CLI, guessed callback, scheduler or cache.
"""

from collections.abc import AsyncIterable

from unitares_sdk.client import GovernanceClient
from unitares_sdk.errors import extract_identity_refusal


async def report_known_execution(
    reports: AsyncIterable[str],
    *,
    display_name: str,
    mcp_url: str,
    host_start_verified: bool = False,
) -> str:
    """Bind one known execution and file only supplied agent-authored reports.

    The caller owns execution boundaries and must not invoke this again after
    an uncertain mint. A reconnect/compaction within that execution must retain
    the same client, not invoke this function. A fresh execution gets a fresh
    invocation only after its boundary is verified. No automatic lineage is
    claimed: a previous UUID alone does not prove a finished predecessor.
    """
    if host_start_verified is not True:
        raise RuntimeError("Host execution boundary unknown; no calls made")

    async with GovernanceClient(mcp_url=mcp_url) as client:
        await client.onboard(
            display_name,
            model_type="agent",
            client_hint="hosted_execution_example",
            force_new=True,
            retry=False,
        )
        if not client.agent_uuid or not client.client_session_id:
            raise RuntimeError("Incomplete binding; stop and reconcile, do not remint")
        # The SDK injects client_session_id, including on this verification read.
        state = await client.call_tool("check_working_state", {})
        if state.get("agent_uuid") != client.agent_uuid:
            raise RuntimeError("Binding not confirmed; stop and reconcile, do not remint")

        async for report in reports:
            if report.strip():
                # These must be authored by the agent, not generated heartbeats.
                # No invented confidence, cadence or lifecycle/end observation.
                # Use the ordinary tool path, not checkin()'s resident-only
                # substrate emission or continuity-token recovery convenience.
                result = await client.call_tool("sync_state", {
                    "response_text": report,
                    "epistemic_class": "agent_report",
                }, retry=False)
                refusal = extract_identity_refusal(result)
                if refusal is not None:
                    raise refusal
        return client.agent_uuid
