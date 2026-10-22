"""Public, non-secret-bearing errors returned at the runtime boundary."""


class RuntimeViolation(Exception):
    code = "runtime_violation"


class PermissionDenied(RuntimeViolation):
    code = "permission_denied"


class BudgetExceeded(RuntimeViolation):
    code = "budget_exceeded"


class SandboxUnavailable(RuntimeViolation):
    code = "sandbox_unavailable"


class SandboxCleanupFailed(SandboxUnavailable):
    code = "sandbox_cleanup_failed"


class InvalidSubmission(RuntimeViolation):
    code = "invalid_submission"


class RunBusy(RuntimeViolation):
    code = "run_busy"
