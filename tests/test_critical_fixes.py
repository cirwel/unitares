#!/usr/bin/env python3
"""
Test Script for Critical Fixes

Tests state locking, health thresholds, and process management.
Run this after implementing the critical fixes to verify they work.
"""

import sys
import json
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.state_locking import StateLockManager
from src.health_thresholds import HealthThresholds, HealthStatus
from src.process_cleanup import ProcessManager


def test_state_locking():
    """Test that state locking prevents concurrent access"""
    print("\n🔒 Testing State Locking...")
    
    lock_manager = StateLockManager()
    agent_id = "test_lock_agent"
    
    # Test acquiring lock
    try:
        with lock_manager.acquire_agent_lock(agent_id, timeout=1.0):
            print("  ✅ Lock acquired successfully")
            
            # Try to acquire same lock again (should timeout)
            try:
                with lock_manager.acquire_agent_lock(agent_id, timeout=0.5):
                    print("  ❌ ERROR: Should not acquire lock twice!")
                    return False
            except TimeoutError:
                print("  ✅ Lock correctly prevents concurrent access")
                return True
    except Exception as e:
        print(f"  ❌ ERROR: {e}")
        return False


def test_health_thresholds():
    """Test risk-based health status calculation"""
    print("\n🏥 Testing Health Thresholds...")
    
    health_checker = HealthThresholds()
    
    test_cases = [
        (0.10, None, False, HealthStatus.HEALTHY, "Low risk"),
        (0.20, None, False, HealthStatus.MODERATE, "Medium risk"),
        (0.35, None, False, HealthStatus.CRITICAL, "High risk"),
        (None, 0.90, False, HealthStatus.HEALTHY, "High coherence"),
        (None, 0.70, False, HealthStatus.MODERATE, "Moderate coherence"),
        (None, 0.50, False, HealthStatus.CRITICAL, "Low coherence"),
        (0.10, 0.90, True, HealthStatus.CRITICAL, "Void active (overrides)"),
    ]
    
    all_passed = True
    for risk, coherence, void, expected_status, description in test_cases:
        status, message = health_checker.get_health_status(
            risk_score=risk,
            coherence=coherence,
            void_active=void
        )
        if status == expected_status:
            print(f"  ✅ {description}: {status.value}")
        else:
            print(f"  ❌ {description}: Expected {expected_status.value}, got {status.value}")
            all_passed = False
    
    return all_passed


def test_process_manager():
    """Test process management and heartbeat"""
    print("\n💓 Testing Process Manager...")
    
    process_mgr = ProcessManager()

    # cleanup_zombies() terminates every mcp_server_std.py on the host whose
    # heartbeat is missing from pid_dir. The suite redirects pid_dir to a tmp
    # dir no real server writes to, so give the reaper an empty process table.
    import src.process_cleanup as process_cleanup
    from contextlib import nullcontext
    from unittest.mock import patch
    empty_process_table = (
        patch.object(process_cleanup.psutil, "process_iter", return_value=iter(()))
        if process_cleanup.PSUTIL_AVAILABLE
        else nullcontext()
    )
    
    # Test heartbeat
    try:
        process_mgr.write_heartbeat()
        print("  ✅ Heartbeat written successfully")
        
        # Check heartbeat file exists
        if process_mgr.heartbeat_file.exists():
            print("  ✅ Heartbeat file exists")
        else:
            print("  ❌ Heartbeat file not found")
            return False
        
        # Test cleanup (should not error even if no zombies)
        with empty_process_table:
            cleaned = process_mgr.cleanup_zombies()
        print(f"  ✅ Cleanup ran (cleaned {len(cleaned)} processes)")
        
        # Test getting active processes
        processes = process_mgr.get_active_processes()
        print(f"  ✅ Found {len(processes)} active processes")
        
        return True
    except Exception as e:
        print(f"  ❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_integration():
    """Test that all components work together"""
    print("\n🔗 Testing Integration...")
    
    try:
        lock_manager = StateLockManager()
        health_checker = HealthThresholds()
        process_mgr = ProcessManager()
        
        agent_id = "test_integration_agent"
        
        # Simulate update flow
        with lock_manager.acquire_agent_lock(agent_id):
            # Simulate processing
            risk_score = 0.18  # Should be DEGRADED
            coherence = 0.85
            void_active = False
            
            health_status, health_message = health_checker.get_health_status(
                risk_score=risk_score,
                coherence=coherence,
                void_active=void_active
            )
            
            process_mgr.write_heartbeat()

            assert health_status == HealthStatus.MODERATE, f"Expected MODERATE, got {health_status}"
            print(f"  ✅ Integration test passed: {health_status.value} ({health_message})")
            return True
            
    except Exception as e:
        print(f"  ❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("=" * 60)
    print("Testing Critical Fixes")
    print("=" * 60)
    
    results = {
        "State Locking": test_state_locking(),
        "Health Thresholds": test_health_thresholds(),
        "Process Manager": test_process_manager(),
        "Integration": test_integration(),
    }
    
    print("\n" + "=" * 60)
    print("Test Results Summary")
    print("=" * 60)
    
    all_passed = True
    for test_name, passed in results.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{test_name}: {status}")
        if not passed:
            all_passed = False
    
    print("=" * 60)
    if all_passed:
        print("🎉 All tests passed!")
        sys.exit(0)
    else:
        print("⚠️  Some tests failed. Review the output above.")
        sys.exit(1)

