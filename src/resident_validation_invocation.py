"""Compatibility shim for the resident-validation invocation API.

Canonical imports live in :mod:`src.evaluation.resident_validation.invocation`.
"""

from src.evaluation.resident_validation.invocation import (
    DEFAULT_ALLOWED_OUTPUTS as DEFAULT_ALLOWED_OUTPUTS,
    FORBIDDEN_OUTPUTS as FORBIDDEN_OUTPUTS,
    INVOCATION_EVENT_TYPE as INVOCATION_EVENT_TYPE,
    LOCAL_OUTPUTS as LOCAL_OUTPUTS,
    InvocationLockHeld as InvocationLockHeld,
    SupervisedInvocationPlan as SupervisedInvocationPlan,
    acquire_invocation_lock as acquire_invocation_lock,
    release_invocation_lock as release_invocation_lock,
    run_supervised_canary_invocation as run_supervised_canary_invocation,
)

__all__ = (
    "DEFAULT_ALLOWED_OUTPUTS",
    "FORBIDDEN_OUTPUTS",
    "INVOCATION_EVENT_TYPE",
    "LOCAL_OUTPUTS",
    "InvocationLockHeld",
    "SupervisedInvocationPlan",
    "acquire_invocation_lock",
    "release_invocation_lock",
    "run_supervised_canary_invocation",
)
