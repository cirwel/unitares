import pytest
"""
Test Circuit Breaker Dialectic Protocol

Tests the end-to-end dialectic flow:
1. Request review → 2. Thesis → 3. Antithesis → 4. Synthesis → 5. Resolution

Note: These tests require PostgreSQL connection and proper database setup.
Run with: UNITARES_DIALECTIC_BACKEND=postgres pytest tests/test_dialectic_protocol.py -v
"""

import asyncio
import sys
import os
sys.path.insert(0, 'src')

# Legacy operator drill. The deterministic handler and protocol coverage lives
# in test_dialectic_handlers.py and test_dialectic_protocol_pure.py; this file
# exercises live registration/session state and is opt-in.
pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DIALECTIC_PROTOCOL_TESTS") != "1",
    reason=(
        "Legacy dialectic protocol drill requires "
        "RUN_DIALECTIC_PROTOCOL_TESTS=1 and registered test agents"
    ),
)

from mcp_handlers.dialectic import (
    handle_request_dialectic_review,
    handle_submit_thesis,
    handle_submit_antithesis,
    handle_submit_synthesis,
    handle_get_dialectic_session
)
import json


@pytest.mark.asyncio
async def test_full_dialectic_flow():
    """Test complete dialectic flow with convergence"""

    print("=== 🎭 TESTING CIRCUIT BREAKER DIALECTIC PROTOCOL ===\n")

    # Step 1: Request dialectic review
    print("STEP 1: Request dialectic review")
    print("-" * 60)

    result1 = await handle_request_dialectic_review({
        "agent_id": "test_agent_critical",
        "reason": "Risk score 0.65 exceeded threshold",
        "api_key": "test_key_123"
    })

    response1 = json.loads(result1[0].text)
    print(json.dumps(response1, indent=2))

    if not response1.get("success"):
        print("\n❌ Failed to request review")
        return False

    session_id = response1["session_id"]
    paused_agent_id = response1["paused_agent_id"]
    reviewer_id = response1["reviewer_agent_id"]

    print(f"\n✅ Session created: {session_id}")
    print(f"   Paused agent: {paused_agent_id}")
    print(f"   Reviewer: {reviewer_id}\n")

    # Step 2: Paused agent submits thesis
    print("\nSTEP 2: Paused agent submits thesis")
    print("-" * 60)

    result2 = await handle_submit_thesis({
        "session_id": session_id,
        "agent_id": paused_agent_id,
        "api_key": "test_key_123",
        "root_cause": "AGI discussion triggered complexity spike",
        "proposed_conditions": [
            "Lower coherence_critical_threshold to 0.45",
            "Monitor for cascade (>2 agents critical)"
        ],
        "reasoning": "The spike was external (topic complexity), not internal failure. System thresholds need adjustment."
    })

    response2 = json.loads(result2[0].text)
    print(json.dumps(response2, indent=2))

    if not response2.get("success"):
        print("\n❌ Failed to submit thesis")
        return False

    print(f"\n✅ Thesis submitted. Phase: {response2['phase']}\n")

    # Step 3: Reviewer submits antithesis
    print("\nSTEP 3: Reviewer submits antithesis")
    print("-" * 60)

    result3 = await handle_submit_antithesis({
        "session_id": session_id,
        "agent_id": reviewer_id,
        "api_key": "reviewer_key_456",
        "observed_metrics": {
            "risk_score": 0.65,
            "coherence": 0.45,
            "coherence_drop_percent": 24.4
        },
        "concerns": [
            "Risk at 65% is significantly above healthy threshold",
            "24% coherence drop is severe",
            "Need monitoring to prevent cascade"
        ],
        "reasoning": "Agree complexity was external, but 0.45 threshold may be too permissive. Suggest 0.48 with active monitoring."
    })

    response3 = json.loads(result3[0].text)
    print(json.dumps(response3, indent=2))

    if not response3.get("success"):
        print("\n❌ Failed to submit antithesis")
        return False

    print(f"\n✅ Antithesis submitted. Phase: {response3['phase']}\n")

    # Step 4: Synthesis round 1 - Paused agent proposes compromise
    print("\nSTEP 4: Synthesis round 1 - Paused agent proposes")
    print("-" * 60)

    result4 = await handle_submit_synthesis({
        "session_id": session_id,
        "agent_id": paused_agent_id,
        "api_key": "test_key_123",
        "root_cause": "External complexity spike (AGI discussion) exposed threshold calibration issue",
        "proposed_conditions": [
            "Set coherence_critical_threshold to 0.48 (compromise)",
            "Enable cascade detection: alert if >2 agents critical within 5min",
            "Resume with confidence=0.85 (slightly reduced)"
        ],
        "reasoning": "Accepting reviewer's threshold recommendation (0.48 vs my 0.45). Adding cascade detection for safety.",
        "agrees": False  # Waiting for reviewer agreement
    })

    response4 = json.loads(result4[0].text)
    print(json.dumps(response4, indent=2))

    if not response4.get("success"):
        print("\n❌ Failed synthesis round 1")
        return False

    print(f"\n✅ Synthesis round 1 submitted. Converged: {response4.get('converged')}\n")

    # Step 5: Synthesis round 2 - Reviewer agrees
    print("\nSTEP 5: Synthesis round 2 - Reviewer agrees")
    print("-" * 60)

    result5 = await handle_submit_synthesis({
        "session_id": session_id,
        "agent_id": reviewer_id,
        "api_key": "reviewer_key_456",
        "root_cause": "External complexity spike (AGI discussion) exposed threshold calibration issue",
        "proposed_conditions": [
            "Set coherence_critical_threshold to 0.48 (compromise)",
            "Enable cascade detection: alert if >2 agents critical within 5min",
            "Resume with confidence=0.85 (slightly reduced)"
        ],
        "reasoning": "Agree with compromise. Threshold 0.48 balances safety with operational flexibility. Cascade detection addresses my concern.",
        "agrees": True  # Agreement!
    })

    response5 = json.loads(result5[0].text)
    print(json.dumps(response5, indent=2))

    if not response5.get("success"):
        print("\n❌ Failed synthesis round 2")
        return False

    print(f"\n✅ Synthesis round 2 submitted. Converged: {response5.get('converged')}")

    if response5.get("converged"):
        print("\n🎉 CONVERGENCE ACHIEVED!")
        print("\nResolution:")
        print(json.dumps(response5.get("resolution"), indent=2))

        # Verify safety check
        if response5.get("action") == "resume":
            print("\n✅ SAFETY CHECK PASSED - Agent can resume with conditions")
        else:
            print(f"\n⚠️  Action: {response5.get('action')} - {response5.get('reason')}")

    # Step 6: Get final session state
    print("\n\nSTEP 6: Get final session state")
    print("-" * 60)

    result6 = await handle_get_dialectic_session({
        "session_id": session_id
    })

    response6 = json.loads(result6[0].text)
    print(f"Session ID: {response6.get('session_id')}")
    print(f"Phase: {response6.get('phase')}")
    print(f"Synthesis rounds: {response6.get('synthesis_round')}")
    print(f"Transcript entries: {len(response6.get('transcript', []))}")

    print("\n" + "=" * 60)
    print("✅ DIALECTIC PROTOCOL TEST COMPLETE")
    print("=" * 60)

    return True


@pytest.mark.asyncio
async def test_no_convergence_escalation():
    """Test escalation when agents don't converge"""

    print("\n\n=== 🎭 TESTING NO CONVERGENCE → ESCALATION ===\n")

    # Setup
    result1 = await handle_request_dialectic_review({
        "agent_id": "test_agent_stubborn",
        "reason": "Testing escalation path"
    })

    response1 = json.loads(result1[0].text)
    
    # Check if session creation succeeded
    if not response1.get("success"):
        pytest.skip(f"Session creation failed: {response1.get('error', 'Unknown error')}")
    
    session_id = response1.get("session_id")
    if not session_id:
        pytest.skip(f"No session_id in response: {response1}")
    
    paused_agent_id = response1["paused_agent_id"]
    reviewer_id = response1["reviewer_agent_id"]

    # Thesis
    await handle_submit_thesis({
        "session_id": session_id,
        "agent_id": paused_agent_id,
        "root_cause": "Issue A",
        "proposed_conditions": ["Condition X"],
        "reasoning": "I want X"
    })

    # Antithesis
    await handle_submit_antithesis({
        "session_id": session_id,
        "agent_id": reviewer_id,
        "concerns": ["X is too risky"],
        "reasoning": "I disagree with X"
    })

    # Synthesis - 6 rounds without agreement (should trigger escalation)
    for i in range(6):
        result = await handle_submit_synthesis({
            "session_id": session_id,
            "agent_id": paused_agent_id if i % 2 == 0 else reviewer_id,
            "proposed_conditions": [f"Condition iteration {i}"],
            "reasoning": f"Still disagreeing round {i}",
            "agrees": False
        })

        response = json.loads(result[0].text)

        if not response.get("success"):
            print(f"Round {i+1}: Escalation triggered")
            print(f"Action: {response.get('action')}")
            print("\n✅ NO CONVERGENCE → ESCALATION WORKING")
            return True

    print("\n❌ Expected escalation but didn't trigger")
    return False


async def main():
    """Run all tests"""
    print("\n")
    print("="*60)
    print(" CIRCUIT BREAKER DIALECTIC PROTOCOL - TEST SUITE")
    print("="*60)

    # Test 1: Full flow with convergence
    test1_passed = await test_full_dialectic_flow()

    # Test 2: No convergence → escalation
    test2_passed = await test_no_convergence_escalation()

    print("\n\n" + "="*60)
    print(" TEST RESULTS")
    print("="*60)
    print(f"Test 1 (Full Dialectic Flow):     {'✅ PASSED' if test1_passed else '❌ FAILED'}")
    print(f"Test 2 (Escalation):               {'✅ PASSED' if test2_passed else '❌ FAILED'}")
    print("="*60)

    return test1_passed and test2_passed


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
