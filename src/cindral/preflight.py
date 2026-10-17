"""Proof that a device can run the work it is about to claim.

A device whose process cannot reach Docker still answers a claim, leases the job,
and fails on the first pull, which reaches the commit as a red test run for code
that was never tested. The check below runs the daemon query the executor needs
from the agent's own process, because a systemd user unit keeps the supplementary
groups it was started with: a login session can reach the Docker socket while the
agent's process cannot, so a probe run from anywhere else answers for credentials
the claim does not have.
"""
import shutil
import threading
import time
from typing import Callable

from .retry import MAX_ERROR_CHARS

# the answer changes when an operator changes the service, not when work arrives,
# so the check is periodic rather than per claim and a fixed device recovers
# within one interval of the fix
PREFLIGHT_INTERVAL_SECONDS = 60.0
# the executor's capture helper has no deadline of its own, so a docker CLI that
# hangs on the socket would hold every claim slot for as long as it hangs
PREFLIGHT_TIMEOUT_SECONDS = 10.0


class DockerPreflight:
    """Answers whether this process can run Docker work, re-proving it on a cadence.

    Each probe runs on its own thread under a deadline, so a check that hangs costs
    a thread rather than the claim loop, and a change is said once, so a broken
    device is not repeated into the log on every poll.
    """

    def __init__(
        self,
        docker: str,
        capture: Callable[[list[str]], tuple[int, str]],
        note: Callable[[str], None] | None = None,
        interval: float = PREFLIGHT_INTERVAL_SECONDS,
        timeout: float = PREFLIGHT_TIMEOUT_SECONDS,
    ) -> None:
        self._docker = docker
        self._capture = capture
        self._note = note
        self._interval = interval
        self._timeout = timeout
        self._unanswered = f"the {docker} check did not answer within {timeout:g}s"
        self._lock = threading.Lock()
        self._outcome: tuple[bool, str] = (False, f"the {docker} check has not run yet")
        self._reported: tuple[bool, str] | None = None
        self._ready = False
        self._checked: float | None = None
        self._probe: threading.Thread | None = None

    def ready(self) -> bool:
        """Whether the device may claim work, re-proving it once per interval."""
        with self._lock:
            if self._checked is not None and time.monotonic() - self._checked < self._interval:
                return self._ready
            if self._probe is not None and self._probe.is_alive():
                # a hung probe keeps its own thread and is left to finish alone, so
                # the claim loop never waits on it and hung probes do not pile up
                return self._announce(False, self._unanswered)
            self._checked = time.monotonic()
            self._probe = probe = threading.Thread(target=self._check, name="cindral-preflight", daemon=True)
            probe.start()
            probe.join(self._timeout)
            outcome = (False, self._unanswered) if probe.is_alive() else self._outcome
            self._ready = self._announce(*outcome)
            return self._ready

    def _check(self) -> None:
        """Runs one probe, recording the reason when the device cannot answer."""
        try:
            if shutil.which(self._docker) is None:
                self._outcome = (False, f"{self._docker} is not on PATH")
                return
            code, output = self._capture([self._docker, "version", "--format", "{.Server.Version}"])
            detail = " ".join(output.split())[:MAX_ERROR_CHARS] or "no output"
            if code == 0:
                self._outcome = (True, f"{self._docker} answers")
            else:
                self._outcome = (False, f"{self._docker} cannot reach its daemon (exit {code}): {detail}")
        except Exception as exc:  # noqa: BLE001 - a probe that raised proves nothing
            self._outcome = (False, " ".join(f"{type(exc).__name__}: {exc}".split())[:MAX_ERROR_CHARS])

    def _announce(self, ready: bool, reason: str) -> bool:
        """Says a change once, so a broken device is not repeated on every poll."""
        state = (ready, reason)
        if state != self._reported:
            self._reported = state
            if self._note is not None:
                self._note(f"cindral: {reason}, claiming {'jobs' if ready else 'no jobs'}")
        return ready
