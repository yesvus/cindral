"""Pull runner agent: claim a job, execute it, report the result.

Execution is injected as a callable so the loop can be tested without Docker
and so the on-device executor can be wired in separately.
"""
import json
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable
import urllib.error
import urllib.request


class CindralError(RuntimeError):
    """Raised when a Relay request fails."""


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
    def __init__(self, base_url: str, token: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

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

    def report(self, job_id: str, device: str, exit_code: int) -> dict:
        _, payload = self._request(
            "POST",
            f"/v1/jobs/{job_id}/report",
            {"device": device, "exit_code": exit_code},
        )
        return payload

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
            raise CindralError(f"relay request failed with HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise CindralError(f"relay request failed: {exc.reason}") from exc
        if not body:
            return status, {}
        try:
            value = json.loads(body)
        except json.JSONDecodeError as exc:
            raise CindralError("relay returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise CindralError("relay returned a non-object response")
        return status, value


class _LeaseHeartbeat(threading.Thread):
    """Renews the lease until the job finishes or the lease is lost."""

    def __init__(
        self,
        client: CindralClient,
        job_id: str,
        device: str,
        lease_seconds: int,
        cancel: threading.Event,
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

    def stop(self) -> None:
        self._stopped.set()

    def run(self) -> None:
        while not self._stopped.wait(self._interval):
            try:
                self._client.renew(self._job_id, self._device, self._lease_seconds)
            except CindralError:
                # a lost lease may already be owned by another device; stop
                # renewing and signal the executor rather than racing it
                self.lost.set()
                self._cancel.set()
                return


Executor = Callable[[JobSpec, threading.Event], int]


class Agent:
    def __init__(
        self,
        client: CindralClient,
        device: str,
        executor: Executor,
        labels: Iterable[str] = (),
        lease_seconds: int = 300,
        idle_seconds: float = 15.0,
    ) -> None:
        self.client = client
        self.device = device
        self.executor = executor
        self.labels = tuple(labels)
        self.lease_seconds = lease_seconds
        self.idle_seconds = idle_seconds

    def run_once(self) -> bool:
        job = self.client.claim(self.device, self.labels, self.lease_seconds)
        if job is None:
            return False
        cancel = threading.Event()
        heartbeat = _LeaseHeartbeat(self.client, job.id, self.device, self.lease_seconds, cancel)
        heartbeat.start()
        try:
            exit_code = int(self.executor(job, cancel))
        except Exception:
            exit_code = 1
        finally:
            heartbeat.stop()
        if heartbeat.lost.is_set():
            # the lease moved on; the server will reject a report anyway
            return True
        self.client.report(job.id, self.device, exit_code)
        return True

    def serve_forever(self) -> None:
        while True:
            try:
                claimed = self.run_once()
            except CindralError:
                claimed = False
            if not claimed:
                time.sleep(self.idle_seconds)
