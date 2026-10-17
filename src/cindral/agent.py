"""Pull runner agent: claim a job, execute it, report the result.

Execution is injected as a callable so the loop can be tested without Docker
and so the on-device executor can be wired in separately.
"""
import http.client
import json
import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
import urllib.error
import urllib.request

from .artifact_client import CindralArtifactClient
from .artifact_upload import discard_archive, upload_archive
from .build_info import BuildInfo, current_build
from .errors import CindralError
from .freshness import Throttled, behind_reason, fetch_commit
from .heartbeats import AgentHeartbeat, LeaseHeartbeat
from .models import ExecutionResult
from .retry import MAX_ERROR_CHARS

# the broker drops a device from the pool once it has been quiet for a minute,
# so an interval at or beyond that would let a healthy device read as gone
ONLINE_WINDOW_SECONDS = 60.0
MAX_HEARTBEAT_SECONDS = ONLINE_WINDOW_SECONDS / 2
DEFAULT_HEARTBEAT_SECONDS = 15.0
# a device that advertised more slots than this is a configuration mistake, and
# the broker refuses the heartbeat, leaving the device invisible
MAX_CONCURRENCY = 1024
# an idle wait this short turns an empty queue into a claim request per slot per
# wait, which loads the broker and burns the device's own cpu
MIN_IDLE_SECONDS = 0.1

# a device error travels as one line on a commit status, so it is flattened and
# bounded by MAX_ERROR_CHARS, which retry.py owns because the broker bounds the
# same value on the stored error. One limit, so a reported error and a stored
# one can never disagree about how much of it survives.


@dataclass(frozen=True)
class JobSpec:
    id: str
    repository: str
    sha: str
    ref: str
    command: tuple[str, ...]
    timeout: int

    @classmethod
    def from_dict(cls, value: dict) -> "JobSpec":
        return cls(
            id=str(value["id"]),
            repository=str(value["repository"]),
            sha=str(value["sha"]),
            ref=str(value["ref"]),
            command=tuple(str(part) for part in value.get("command", [])),
            timeout=int(value.get("timeout", 3600)),
        )


class CindralClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: int = 30,
        build: BuildInfo | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        #: what this client is running, sent on every heartbeat. Defaults to the
        #: build metadata in the environment, which is the container's case; a
        #: checkout-based agent passes its own, because it has no image to read.
        self.build = build or current_build()
        #: what the broker last said it is running, learned from the heartbeat
        #: reply. A device that is behind the broker is a device whose code
        #: predates the job, and the broker cannot order the two commits on its
        #: own because it holds no history.
        self.broker_build: dict[str, str] = {}

    def claim(
        self,
        device: str,
        labels: Iterable[str] = (),
        lease_seconds: int = 300,
    ) -> JobSpec | None:
        _, payload = self._request(
            "POST",
            "/v1/jobs/claim",
            {"device": device, "labels": list(labels), "lease_seconds": lease_seconds},
        )
        job = payload.get("job")
        if not job:
            return None
        try:
            return JobSpec.from_dict(job)
        except (KeyError, TypeError, ValueError) as exc:
            raise CindralError("relay returned a malformed job") from exc

    def renew(self, job_id: str, device: str, lease_seconds: int = 300) -> None:
        self._request(
            "POST",
            f"/v1/jobs/{job_id}/renew",
            {"device": device, "lease_seconds": lease_seconds},
        )

    def heartbeat(self, device: str, labels: Iterable[str] = (), max_concurrency: int = 1) -> dict:
        """Tells the broker the device is alive, what it can hold, and what it is.

        The build travels with the heartbeat because a device agent runs from a
        checkout on the machine, so it has no image to be asked about. Without it
        the broker cannot tell a device that is current from one that predates the
        code the job needs, and neither can an operator reading the pool.
        """
        _, payload = self._request(
            "POST",
            "/v1/agents/heartbeat",
            {
                "device": device,
                "labels": list(labels),
                "max_concurrency": int(max_concurrency),
                "build": self.build.as_dict(),
            },
        )
        # the broker answers with its own build, which is the only side of the
        # comparison that cannot be worked out locally
        broker = payload.get("broker_build")
        if isinstance(broker, dict) and all(isinstance(v, str) for v in broker.values()):
            self.broker_build = broker
        return payload

    def report(self, job_id: str, device: str, exit_code: int, log: str = "", steps: list[dict] | None = None) -> dict:
        body: dict[str, object] = {"device": device, "exit_code": exit_code}
        if log:
            body["log"] = log
        if steps:
            body["steps"] = steps
        _, payload = self._request("POST", f"/v1/jobs/{job_id}/report", body)
        return payload

    def report_lost(self, job_id: str, device: str, error: str, log: str = "", steps: list[dict] | None = None) -> dict:
        """Report a run the device could not finish, so the broker can retry it.

        A nonzero exit code here would reach the commit as a failed test run,
        which is the one reading a device that died must never produce.
        """
        body: dict[str, object] = {"device": device, "error": error[:MAX_ERROR_CHARS]}
        if log:
            body["log"] = log
        if steps:
            body["steps"] = steps
        _, payload = self._request("POST", f"/v1/jobs/{job_id}/report", body)
        return payload

    def upload_artifacts(self, job_id: str, device: str, archive: str) -> dict:
        """Send a run's artifact archive, which the report body cannot carry."""
        return CindralArtifactClient(self).upload(job_id, device, archive)

    def _request(self, method: str, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise CindralError(
                f"relay request failed with HTTP {exc.code}: {detail}", status=exc.code
            ) from exc
        except urllib.error.URLError as exc:
            raise CindralError(f"relay request failed: {exc.reason}") from exc
        except (OSError, http.client.HTTPException) as exc:
            # a reply that stalls or ends mid-body raises out of the open context
            # rather than returning, and a heartbeat or slot thread that lets one
            # through is gone for the rest of the process
            raise CindralError(f"relay request failed: {exc}") from exc
        if not body:
            return status, {}
        try:
            value = json.loads(body)
        except json.JSONDecodeError as exc:
            raise CindralError("relay returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise CindralError("relay returned a non-object response")
        return status, value


Executor = Callable[[JobSpec, threading.Event], "int | ExecutionResult"]


class Agent:
    def __init__(
        self,
        client: CindralClient,
        device: str,
        executor: Executor,
        labels: Iterable[str] = (),
        lease_seconds: int = 300,
        idle_seconds: float = 15.0,
        max_concurrency: int = 1,
        heartbeat_seconds: float = 15.0,
        preflight: Callable[[], bool] | None = None,
        log: Callable[[str], None] | None = None,
        source_root: str | Path | None = None,
    ) -> None:
        if not 1 <= max_concurrency <= MAX_CONCURRENCY:
            raise ValueError(f"max_concurrency must be between 1 and {MAX_CONCURRENCY}")
        if not 0 < heartbeat_seconds <= MAX_HEARTBEAT_SECONDS:
            raise ValueError(f"heartbeat_seconds must be between 0 and {MAX_HEARTBEAT_SECONDS}")
        # a wait at or below zero returns at once, so an idle device would spin
        # the claim endpoint for as long as the queue stays empty
        if not math.isfinite(idle_seconds) or idle_seconds < MIN_IDLE_SECONDS:
            raise ValueError(f"idle_seconds must be a finite number of at least {MIN_IDLE_SECONDS}")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be greater than zero")
        self.client = client
        self.device = device
        self.executor = executor
        self.labels = tuple(labels)
        self.lease_seconds = lease_seconds
        self.idle_seconds = idle_seconds
        self.max_concurrency = max_concurrency
        self.heartbeat_seconds = heartbeat_seconds
        self.preflight = preflight
        self.log = log
        #: the checkout this device runs from, when it is a checkout. It is the
        #: only side of a version comparison that has the history to answer it.
        self.source_root = str(source_root) if source_root is not None else None
        #: what this device is running. Taken from the client, which is the side
        #: that talks to the broker and therefore the one that knows it.
        self.build = getattr(client, "build", None) or BuildInfo()
        #: The fetch that closes the gap between two commits, rate limited
        #: because a device that cannot reach its origin would otherwise spend
        #: every poll waiting on one instead of taking or refusing work.
        self._fetch = (
            Throttled(lambda commit: fetch_commit(self.source_root, commit))
            if source_root
            else None
        )
        self._stopping = threading.Event()
        self._behind_note: str | None = None

    def note(self, message: str) -> None:
        if self.log is not None:
            self.log(message)

    def announce(self) -> None:
        self.client.heartbeat(self.device, self.labels, self.max_concurrency)

    def start_heartbeat(self) -> "AgentHeartbeat":
        heartbeat = AgentHeartbeat(self, self.heartbeat_seconds)
        heartbeat.start()
        return heartbeat

    def stop(self) -> None:
        """Asks every slot to stop claiming; a job in flight still runs and reports."""
        self._stopping.set()

    def behind_broker(self) -> str | None:
        """Why this device is behind the broker, or None when it is not.

        The comparison lives in `freshness` because it needs the history between
        the two commits, and the only side that has it is this checkout.
        """
        return behind_reason(
            self.build.commit,
            getattr(self.client, "broker_build", {}).get("commit", ""),
            self.source_root,
            self.build.as_dict(),
            fetch=self._fetch,
        )

    def run_once(self) -> bool:
        # a device that cannot run the work its executor needs leaves the queue
        # alone: a claim it cannot finish costs a lease, a commit status and a
        # broker retry, and the broker reads the failed run as a red test
        if self.preflight is not None and not self.preflight():
            return False
        behind = self.behind_broker()
        if behind is not None:
            # said once per streak rather than once per poll, so the log says why
            # the device is idle instead of filling with the same line
            if self._behind_note != behind:
                self._behind_note = behind
                self.note(f"cindral: not claiming work: {behind}. Update this checkout and restart the agent.")
            return False
        self._behind_note = None
        job = self.client.claim(self.device, self.labels, self.lease_seconds)
        if job is None:
            return False
        cancel = threading.Event()
        heartbeat = LeaseHeartbeat(
            self.client, job.id, self.device, self.lease_seconds, cancel, self
        )
        heartbeat.start()
        log = ""
        error = ""
        steps: list[dict] = []
        archive = ""
        exit_code = 0
        try:
            try:
                result = self.executor(job, cancel)
                if isinstance(result, ExecutionResult):
                    exit_code, log, steps, archive = (
                        result.exit_code, result.log, result.steps, result.artifacts
                    )
                else:
                    exit_code = int(result)
            except Exception as exc:  # noqa: BLE001 - the device failed, the run did not
                # a raised executor never produced an exit code, so reporting one
                # would turn a dead device into a red commit for a good change
                error = " ".join(f"{type(exc).__name__}: {exc}".split())[:MAX_ERROR_CHARS]
            if heartbeat.lost.is_set():
                # the lease moved on; the server will reject a report anyway, and
                # another device is already running this commit
                if archive:
                    discard_archive(archive)
                return True
            if archive:
                upload_archive(self.client, self.device, job.id, archive, self.note)
                # the upload is a network round trip that can outlast the lease,
                # and it is the last thing before the report, which the server
                # checks the lease for
                if heartbeat.lost.is_set():
                    return True
            if error:
                self.note(f"cindral: job {job.id} could not run: {error}")
                self.client.report_lost(job.id, self.device, error, log, steps)
            else:
                self.client.report(job.id, self.device, exit_code, log, steps)
            return True
        finally:
            # The heartbeat runs until the run is *reported*, not until it stops
            # executing. Stopping it at the end of the executor left the upload
            # and the report to share only the tail of a lease, and a lease that
            # lapses there is reclaimed: the run is handed to another device, the
            # work runs twice, and both devices were doing the right thing.
            heartbeat.stop()

    def serve_forever(self) -> None:
        heartbeat = self.start_heartbeat()
        try:
            if self.max_concurrency == 1:
                # one slot runs in the caller's thread, so the default agent adds
                # no slot thread and Ctrl-C reaches the loop that claims
                self._run_slot()
                return
            workers = [
                threading.Thread(target=self._run_slot, name=f"cindral-slot-{index}", daemon=True)
                for index in range(self.max_concurrency)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
        finally:
            heartbeat.stop()

    def _run_slot(self) -> None:
        """Claims until asked to stop, restarting a slot that raised.

        A slot thread that returned is capacity the heartbeat keeps advertising,
        so an unexpected failure is reported and the slot is claimed again.
        """
        while not self._stopping.is_set():
            try:
                self._poll_forever()
            except Exception as exc:  # noqa: BLE001 - a lost slot is invisible capacity
                self.note(f"cindral: slot restarted after an unexpected failure: {exc!r}")
                self._stopping.wait(self.idle_seconds)

    def _poll_forever(self) -> None:
        while not self._stopping.is_set():
            try:
                claimed = self.run_once()
            except CindralError:
                claimed = False
            if not claimed:
                self._stopping.wait(self.idle_seconds)
