"""Agent-facing job API for claiming, renewing, and reporting leased work."""
import json
from typing import Any

from .github import GitHubAPIError
from .jobs import JobStore


class JobApiMixin:
    """Handlers for ``/v1/jobs``, gated behind the agent bearer token."""

    server: Any

    def _jobs(self) -> None:
        store = self.server.jobs
        if store is None:
            self._send(503, {"error": "job queue is not configured"})  # type: ignore[attr-defined]
            return
        if not self._agent_authorized():  # type: ignore[attr-defined]
            self._send(401, {"error": "job requests require a bearer token"})  # type: ignore[attr-defined]
            return
        path = self.path.split("?", 1)[0].rstrip("/")  # type: ignore[attr-defined]
        method = self.command  # type: ignore[attr-defined]
        try:
            if path == "/v1/jobs/claim":
                self._require(method, "POST", "claim")
                if method == "POST":
                    self._claim(store)
                return
            if path.startswith("/v1/jobs/"):
                job_id, _, action = path[len("/v1/jobs/"):].partition("/")
                if not job_id:
                    self._send(404, {"error": "not found"})  # type: ignore[attr-defined]
                    return
                if action in {"renew", "report"}:
                    self._require(method, "POST", action)
                    if method != "POST":
                        return
                    if action == "renew":
                        self._renew(store, job_id)
                    else:
                        self._report(store, job_id)
                    return
                if action == "":
                    self._require(method, "GET", "job status")
                    if method == "GET":
                        self._job_status(store, job_id)
                    return
            self._send(404, {"error": "not found"})  # type: ignore[attr-defined]
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})  # type: ignore[attr-defined]

    def _require(self, method: str, expected: str, action: str) -> None:
        if method != expected:
            self._send(405, {"error": f"{action} requires {expected}"})  # type: ignore[attr-defined]

    def _claim(self, store: JobStore) -> None:
        payload = self._read_json()  # type: ignore[attr-defined]
        device = str(payload["device"])
        labels = [str(label) for label in payload.get("labels", [])]
        lease = int(payload.get("lease_seconds", self.server.lease_seconds))
        job = store.claim(device, labels, lease_seconds=lease)
        if job is None:
            self._send(204)  # type: ignore[attr-defined]
            return
        self._send(200, {"job": job.as_dict()})  # type: ignore[attr-defined]

    def _renew(self, store: JobStore, job_id: str) -> None:
        payload = self._read_json()  # type: ignore[attr-defined]
        device = str(payload["device"])
        lease = int(payload.get("lease_seconds", self.server.lease_seconds))
        if not store.renew(job_id, device, lease_seconds=lease):
            self._send(409, {"error": "job is not leased to this device"})  # type: ignore[attr-defined]
            return
        self._send(200, {"renewed": True, "id": job_id})  # type: ignore[attr-defined]

    def _report(self, store: JobStore, job_id: str) -> None:
        job = store.get(job_id)
        if job is None:
            self._send(404, {"error": "unknown job"})  # type: ignore[attr-defined]
            return
        payload = self._read_json()  # type: ignore[attr-defined]
        device = str(payload["device"])
        exit_code = int(payload["exit_code"])
        log = str(payload.get("log", "") or "")
        if not store.holds_lease(job_id, device):
            self._send(409, {"error": "job is not leased to this device"})  # type: ignore[attr-defined]
            return
        if self.server.github is not None:
            if exit_code == 0:
                state, description = "success", "device pool run passed"
            else:
                state, description = "failure", f"device pool run failed (exit {exit_code})"
            try:
                self.server.github.post_status(
                    job.repository,
                    job.status_sha or job.sha,
                    state,
                    description,
                    context=self.server.status_context,
                )
            except GitHubAPIError as exc:
                # keep the job leased so the agent can retry the report; a
                # terminal job with a lost status could never be repaired
                self._send(502, {"error": f"commit status update failed: {exc}"})  # type: ignore[attr-defined]
                return
        try:
            job = store.report(job_id, device, exit_code, log=log)
        except ValueError as exc:
            self._send(409, {"error": str(exc)})  # type: ignore[attr-defined]
            return
        posted = self.server.github is not None
        self._send(200, {**job.as_dict(), "status_posted": posted})  # type: ignore[attr-defined]

    def _job_status(self, store: JobStore, job_id: str) -> None:
        job = store.get(job_id)
        if job is None:
            self._send(404, {"error": "unknown job"})  # type: ignore[attr-defined]
            return
        self._send(200, job.as_dict())  # type: ignore[attr-defined]
