"""Agent-facing job API for claiming, renewing, and reporting leased work."""
import json
from urllib.parse import urlsplit
from typing import Any

from .cache import CacheError, CacheStore, MAX_CACHE_BLOB_BYTES
from .github import GitHubAPIError
from .jobs import JobStore


class JobApiMixin:
    """Handlers for ``/v1/jobs``.

    Claim, renew, and report mutate the queue and take the agent token. Reading
    one job's status also takes the read-only pool token, so a control panel can
    inspect a run without holding the credential that can lease work.
    """

    server: Any

    def _read_authorized(self) -> bool:
        return self._agent_authorized() or self._pool_authorized()  # type: ignore[attr-defined]

    def _jobs(self) -> None:
        store = self.server.jobs
        if store is None:
            self._send(503, {"error": "job queue is not configured"})  # type: ignore[attr-defined]
            return
        path = self.path.split("?", 1)[0].rstrip("/")  # type: ignore[attr-defined]
        # only the status lookup is read-only; everything else mutates the queue
        read_only = not path.endswith(("/claim", "/renew", "/report"))
        authorized = (  # type: ignore[attr-defined]
            self._read_authorized() if read_only else self._agent_authorized()
        )
        if not authorized:
            self._send(  # type: ignore[attr-defined]
                401, {"error": "job requests require a bearer token"}
            )
            return
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

    def _cache(self) -> None:
        store: CacheStore | None = self.server.cache
        if store is None:
            self._send(503, {"error": "cache is not configured"})  # type: ignore[attr-defined]
            return
        if not self._agent_authorized():  # type: ignore[attr-defined]
            self._send(401, {"error": "cache requests require a bearer token"})  # type: ignore[attr-defined]
            return
        path = urlsplit(self.path).path  # type: ignore[attr-defined]
        try:
            if path == "/v1/cache/lookup" and self.command == "POST":  # type: ignore[attr-defined]
                self._cache_lookup(store)
                return
            if path == "/v1/cache/entries" and self.command == "PUT":  # type: ignore[attr-defined]
                self._cache_put(store)
                return
            if path.startswith("/v1/cache/blobs/") and self.command == "GET":  # type: ignore[attr-defined]
                self._cache_blob(store, path.removeprefix("/v1/cache/blobs/"))
                return
            self._send(404, {"error": "not found"})  # type: ignore[attr-defined]
        except (CacheError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})  # type: ignore[attr-defined]

    def _cache_lookup(self, store: CacheStore) -> None:
        payload = self._read_json()  # type: ignore[attr-defined]
        fields = ("repository", "branch", "architecture", "key")
        if any(not isinstance(payload.get(field), str) for field in fields):
            raise ValueError("repository, branch, architecture, and key must be strings")
        restore_keys = payload.get("restore_keys", [])
        if not isinstance(restore_keys, list) or not all(isinstance(key, str) for key in restore_keys):
            raise ValueError("restore_keys must be an array of strings")
        entry = store.lookup(
            payload["repository"],
            payload["branch"],
            payload["architecture"],
            payload["key"],
            tuple(restore_keys),
        )
        self._send(200, {"hit": entry is not None, "entry": entry.as_dict() if entry else None})  # type: ignore[attr-defined]

    def _cache_put(self, store: CacheStore) -> None:
        try:
            length = int(self.headers.get("Content-Length", "-1"))  # type: ignore[attr-defined]
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > min(store.max_blob_bytes, MAX_CACHE_BLOB_BYTES):
            self._send(413, {"error": "cache blob exceeds the configured size limit"})  # type: ignore[attr-defined]
            return
        entry, created = store.put(
            self.headers.get("X-Cindral-Repository", ""),  # type: ignore[attr-defined]
            self.headers.get("X-Cindral-Branch", ""),  # type: ignore[attr-defined]
            self.headers.get("X-Cindral-Architecture", ""),  # type: ignore[attr-defined]
            self.headers.get("X-Cindral-Key", ""),  # type: ignore[attr-defined]
            self.rfile,  # type: ignore[attr-defined]
            length,
        )
        self._send(201 if created else 200, {"created": created, "entry": entry.as_dict()})  # type: ignore[attr-defined]

    def _cache_blob(self, store: CacheStore, digest: str) -> None:
        try:
            blob, size = store.open_blob(digest)
        except FileNotFoundError:
            self._send(404, {"error": "unknown cache blob"})  # type: ignore[attr-defined]
            return
        self.send_response(200)  # type: ignore[attr-defined]
        self.send_header("Content-Type", "application/octet-stream")  # type: ignore[attr-defined]
        self.send_header("Content-Length", str(size))  # type: ignore[attr-defined]
        self.send_header("ETag", f'"sha256:{digest}"')  # type: ignore[attr-defined]
        self.end_headers()  # type: ignore[attr-defined]
        with blob:
            while block := blob.read(1024 * 1024):
                self.wfile.write(block)  # type: ignore[attr-defined]

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
