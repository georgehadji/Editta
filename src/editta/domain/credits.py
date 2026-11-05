"""Credit policy (pure). Credits are the unit customers buy; one job costs a fixed amount.

The balance is always derived from the append-only ledger (PLAN.md §3); this module only
decides whether a balance can pay for a job.
"""

from editta.domain.errors import InsufficientCredits

DEFAULT_JOB_COST_CREDITS = 1


def require_credits(balance: int, required: int = DEFAULT_JOB_COST_CREDITS) -> None:
    if required < 1:
        raise ValueError("a job must cost at least one credit")
    if balance < required:
        raise InsufficientCredits(balance, required)


def reservation_key(job_id: object) -> str:
    """Idempotency key of a job's credit reservation. Shared by the upload (which
    reserves) and the job runner (which refunds against it, and reserves only for jobs
    created before upload-time reservation existed)."""
    return f"job:{job_id}:reserve"


def refund_key(job_id: object) -> str:
    return f"job:{job_id}:refund"
