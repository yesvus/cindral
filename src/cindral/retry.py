"""Policy for a run a device lost before it could report a verdict.

A device that dies, loses the network, or crashes its executor ends a run
without an exit code. That failure belongs to the device, not to the commit, so
the run waits out a growing backoff and is claimed again; the attempt cap is
what stops a device that never works from cycling the same job forever.
"""
MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 30.0
MAX_BACKOFF_SECONDS = 300.0
# a lost run is recorded as one sentence a commit status can carry, not as the
# traceback the device logged
MAX_ERROR_CHARS = 200
LEASE_LOST = "the device lease expired before the run finished"


def backoff_seconds(attempts: int) -> float:
    """Wait before the next attempt, doubling per attempt and capped."""
    return min(BASE_BACKOFF_SECONDS * 2 ** max(0, int(attempts) - 1), MAX_BACKOFF_SECONDS)


def parked(attempts: int) -> bool:
    """Whether a run that has already taken this many attempts is out of retries."""
    return int(attempts) >= MAX_ATTEMPTS
