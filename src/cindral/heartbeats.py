"""The two threads an agent runs alongside its own work.

Both keep a fact true that the broker can otherwise lose: that a device is still
there, and that a job it is running still belongs to it. Neither is a heartbeat
in the health-check sense, so they are kept out of the agent's own file.
"""
import threading
import time
from typing import TYPE_CHECKING

from .errors import CindralError

if TYPE_CHECKING:
    from .agent import Agent, CindralClient


#: The statuses that mean the lease is gone rather than the request failing. The
#: broker answers 409 when the job is not leased to this device and 404 when it is
#: not there at all; both are answers, and a request that never got one is not.
LEASE_LOST_STATUSES = frozenset({404, 409})


class LeaseHeartbeat(threading.Thread):
    """Renews the lease until the job finishes or the lease is lost."""

    def __init__(
        self,
        client: "CindralClient",
        job_id: str,
        device: str,
        lease_seconds: int,
        cancel: threading.Event,
        agent: "Agent | None" = None,
    ) -> None:
        super().__init__(daemon=True)
        self._client = client
        self._job_id = job_id
        self._device = device
        self._lease_seconds = lease_seconds
        self._cancel = cancel
        self._interval = max(1, lease_seconds // 3)
        self._stopped = threading.Event()
        self.lost = threading.Event()
        self._noted = False
        self._agent_note = agent.note if agent is not None else (lambda message: None)

    def stop(self) -> None:
        self._stopped.set()

    def run(self) -> None:
        # A renewal that never reached the broker is not a lost lease. The lease is
        # still ours for at least another interval, and the next tick comes before
        # it expires, so treating one timeout as a loss cancelled a long build that
        # was making progress and left the job running with nobody reporting on it.
        # The broker then reclaimed it, which is a second device running the same
        # work and an attempt spent for nothing.
        #
        # Only a refusal is a lost lease: 409 says the job is not leased to this
        # device and 404 says it is gone, and in both another device may already
        # be running it.
        deadline = time.monotonic() + self._lease_seconds
        while not self._stopped.wait(self._interval):
            try:
                self._client.renew(self._job_id, self._device, self._lease_seconds)
                deadline = time.monotonic() + self._lease_seconds
                self._noted = False
            except CindralError as exc:
                if getattr(exc, "status", None) in LEASE_LOST_STATUSES:
                    self.lost.set()
                    self._cancel.set()
                    return
                if time.monotonic() >= deadline:
                    # the lease could genuinely have lapsed while nothing got
                    # through, and renewing into that race would hand the run to a
                    # second device while this one is still working on it
                    self.lost.set()
                    self._cancel.set()
                    return
                if not self._noted:
                    self._noted = True
                    self._agent_note(
                        f"cindral: lease renewal failed ({exc}), retrying before it lapses"
                    )


class AgentHeartbeat(threading.Thread):
    """Advertises the device and its slots until the agent stops.

    A broker without the endpoint answers 404 and a device that can still claim
    and report keeps working, so only the first failure and the recovery are
    reported rather than every beat.
    """

    def __init__(self, agent: "Agent", interval: float) -> None:
        super().__init__(daemon=True, name="cindral-agent-heartbeat")
        self._agent = agent
        self._interval = max(0.1, interval)
        self._stopped = threading.Event()

    def stop(self) -> None:
        self._stopped.set()

    def run(self) -> None:
        broken = False
        while True:
            try:
                self._agent.announce()
            except Exception as exc:  # noqa: BLE001 - a beat that escapes here never repeats
                if not broken:
                    broken = True
                    self._agent.note(f"cindral: heartbeat failed, claiming continues: {exc}")
            else:
                if broken:
                    broken = False
                    self._agent.note("cindral: heartbeat recovered")
            if self._stopped.wait(self._interval):
                return
