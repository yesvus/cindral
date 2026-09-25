"""HTTP service for routing decisions."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
from typing import Any

from .github import GitHubAPIError, GitHubClient, GitHubDispatchError, is_repository_slug
from .jobs import SUCCESS, JobStore
from .models import RouteRequest
from .policy import Policy, RouteUnavailable
from .state import load_runners
from .webhook import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    WebhookError,
    parse_push_event,
    verify_signature,
)

WEBHOOK_PATH = "/relay/dispatch"
MAX_BODY = 1 << 20


class RelayServer(ThreadingHTTPServer):
    policy: Policy
    runners: tuple
    github: GitHubClient | None = None
    webhook_secret: str | None = None
    dispatch_token: str | None = None
    workflow_file: str = "relay-dispatch.yml"
    jobs: JobStore | None = None
    agent_token: str | None = None
    lease_seconds: int = 300
    job_timeout: int = 3600
    direct_repositories: tuple[str, ...] = ()
    status_context: str = "relay/ci"


class RelayHandler(BaseHTTPRequestHandler):
    server: RelayServer

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, {"status": "ok"})
            return
        if self.path.split("?", 1)[0].startswith("/v1/jobs/"):
            self._jobs()
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == WEBHOOK_PATH:
            self._webhook()
            return
        if self.path.split("?", 1)[0].startswith("/v1/jobs"):
            self._jobs()
            return
        if self.path not in {"/v1/route", "/v1/dispatch"}:
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_BODY:
            self._send(413, {"error": "payload too large"})
            return
        body = self.rfile.read(length)
        if self.path == "/v1/dispatch" and not self._dispatch_authorized():
            self._send(401, {"error": "dispatch requires a bearer token"})
            return
        try:
            payload: dict[str, Any] = json.loads(body)
            request = RouteRequest.from_dict(payload)
            decision = self.server.policy.choose(request, self.server.runners)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})
            return
        except RouteUnavailable as exc:
            self._send(409, {"error": str(exc)})
            return
        if self.path == "/v1/route":
            self._send(200, decision.as_dict())
            return
        self._dispatch(payload, request, decision)

    def _dispatch(self, payload: dict[str, Any], request: RouteRequest, decision: Any) -> None:
        if self.server.github is None:
            self._send(503, {"error": "GitHub dispatch token is not configured"})
            return
        try:
            repository = str(payload["repository"])
            workflow = str(payload["workflow"])
            ref = str(payload["ref"])
        except KeyError as exc:
            self._send(400, {"error": f"missing dispatch field: {exc.args[0]}"})
            return
        inputs = dict(payload.get("inputs", {}))
        inputs.update(
            {
                "relay_lane": decision.lane,
                "relay_target": request.target or "",
                "relay_reason": decision.reason,
            }
        )
        try:
            self.server.github.dispatch(repository, workflow, ref, inputs)
        except GitHubDispatchError as exc:
            self._send(502, {"error": str(exc)})
            return
        self._send(200, {**decision.as_dict(), "dispatched": True})

    def _dispatch_authorized(self) -> bool:
        expected = self.server.dispatch_token
        if not expected:
            # no token configured means dispatch is refused rather than open,
            # so an unset secret cannot silently expose the endpoint
            return False
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix):].strip(), expected)

    def _agent_authorized(self) -> bool:
        expected = self.server.agent_token
        if not expected:
            return False
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix):].strip(), expected)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_BODY:
            raise ValueError("payload too large")
        body = self.rfile.read(length)
        if not body:
            return {}
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("body is not a JSON object")
        return value

    def _jobs(self) -> None:
        store = self.server.jobs
        if store is None:
            self._send(503, {"error": "job queue is not configured"})
            return
        if not self._agent_authorized():
            self._send(401, {"error": "job requests require a bearer token"})
            return
        path = self.path.split("?", 1)[0].rstrip("/")
        try:
            if path == "/v1/jobs/claim":
                self._claim(store)
                return
            if path.startswith("/v1/jobs/"):
                job_id, _, action = path[len("/v1/jobs/"):].partition("/")
                if not job_id:
                    self._send(404, {"error": "not found"})
                    return
                if action == "renew":
                    self._renew(store, job_id)
                    return
                if action == "report":
                    self._report(store, job_id)
                    return
                if action == "":
                    self._job_status(store, job_id)
                    return
            self._send(404, {"error": "not found"})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})

    def _claim(self, store: JobStore) -> None:
        payload = self._read_json()
        device = str(payload["device"])
        labels = [str(label) for label in payload.get("labels", [])]
        lease = int(payload.get("lease_seconds", self.server.lease_seconds))
        job = store.claim(device, labels, lease_seconds=lease)
        if job is None:
            self._send(204)
            return
        self._send(200, {"job": job.as_dict()})

    def _renew(self, store: JobStore, job_id: str) -> None:
        payload = self._read_json()
        device = str(payload["device"])
        lease = int(payload.get("lease_seconds", self.server.lease_seconds))
        if not store.renew(job_id, device, lease_seconds=lease):
            self._send(409, {"error": "job is not leased to this device"})
            return
        self._send(200, {"renewed": True, "id": job_id})

    def _report(self, store: JobStore, job_id: str) -> None:
        if store.get(job_id) is None:
            self._send(404, {"error": "unknown job"})
            return
        payload = self._read_json()
        device = str(payload["device"])
        exit_code = int(payload["exit_code"])
        try:
            job = store.report(job_id, device, exit_code)
        except ValueError as exc:
            self._send(409, {"error": str(exc)})
            return
        self._send(200, {**job.as_dict(), "status_posted": self._post_job_status(job)})

    def _job_status(self, store: JobStore, job_id: str) -> None:
        job = store.get(job_id)
        if job is None:
            self._send(404, {"error": "unknown job"})
            return
        self._send(200, job.as_dict())

    def _post_job_status(self, job: Any) -> bool:
        github = self.server.github
        if github is None:
            return False
        if job.status == SUCCESS:
            state, description = "success", "device pool run passed"
        else:
            state, description = "failure", f"device pool run failed (exit {job.exit_code})"
        try:
            github.post_status(
                job.repository, job.sha, state, description, context=self.server.status_context
            )
            return True
        except GitHubAPIError:
            return False

    def _enqueue_direct(self, repository: str, sha: str, branch: str) -> None:
        jobs = self.server.jobs
        assert jobs is not None
        job = jobs.enqueue(repository, sha, branch, timeout=self.server.job_timeout)
        posted = False
        if self.server.github is not None:
            try:
                self.server.github.post_status(
                    repository,
                    sha,
                    "pending",
                    "queued on the device pool",
                    context=self.server.status_context,
                )
                posted = True
            except GitHubAPIError:
                posted = False
        self._send(
            202,
            {
                "queued": True,
                "job": job.id,
                "repository": repository,
                "sha": sha,
                "status_posted": posted,
            },
        )

    def _webhook(self) -> None:
        secret = self.server.webhook_secret
        if not secret:
            self._send(503, {"error": "webhook secret is not configured"})
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_BODY:
            self._send(413, {"error": "payload too large"})
            return
        body = self.rfile.read(length)
        if not verify_signature(secret, body, self.headers.get(SIGNATURE_HEADER)):
            self._send(401, {"error": "signature verification failed"})
            return
        if self.headers.get(EVENT_HEADER) != "push":
            self._send(202, {"status": "ignored", "reason": "event is not push"})
            return
        try:
            event = parse_push_event(body)
        except WebhookError as exc:
            self._send(400, {"error": str(exc)})
            return
        if not is_repository_slug(event.repository):
            self._send(400, {"error": "invalid repository name"})
            return
        branch = event.branch
        if not branch or branch != event.default_branch:
            self._send(202, {"status": "ignored", "reason": f"branch is not the default branch: {branch or event.ref}"})
            return
        if self.server.jobs is not None and event.repository in self.server.direct_repositories:
            self._enqueue_direct(event.repository, event.after, branch)
            return
        if self.server.github is None:
            self._send(503, {"error": "GitHub dispatch token is not configured"})
            return
        try:
            if not self.server.github.workflow_exists(event.repository, self.server.workflow_file):
                self._send(202, {"status": "ignored", "reason": "repository has not opted in with the relay workflow"})
                return
        except GitHubAPIError as exc:
            self._send(502, {"error": str(exc)})
            return
        # quota is deliberately unknown: only the caller knows the remaining
        # minutes, and the policy default for unknown quota is the local lane.
        # visibility comes from the payload, so public repositories still route
        # to hosted runners.
        request = RouteRequest(
            repository_visibility="private" if event.private else "public",
            quota_status="unknown",
        )
        try:
            decision = self.server.policy.choose(request, self.server.runners)
        except RouteUnavailable as exc:
            self._send(409, {"error": str(exc)})
            return
        inputs = {
            "relay_lane": decision.lane,
            "relay_target": request.target or "",
            "relay_reason": decision.reason,
        }
        try:
            self.server.github.dispatch(event.repository, self.server.workflow_file, branch, inputs)
        except GitHubDispatchError as exc:
            self._send(502, {"error": str(exc)})
            return
        self._send(
            200,
            {
                **decision.as_dict(),
                "dispatched": True,
                "repository": event.repository,
                "branch": branch,
                "delivery": self.headers.get(DELIVERY_HEADER, ""),
            },
        )

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: int, payload: dict[str, Any] | None = None) -> None:
        if payload is None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(policy_path: str | Path, state_path: str | Path, host: str, port: int, github_token: str | None = None) -> None:
    server = RelayServer((host, port), RelayHandler)
    server.policy = Policy.load(policy_path)
    server.runners = load_runners(state_path)
    server.github = GitHubClient(github_token) if github_token else None
    server.webhook_secret = os.environ.get("RELAY_WEBHOOK_SECRET") or None
    server.dispatch_token = os.environ.get("RELAY_DISPATCH_TOKEN") or None
    server.workflow_file = os.environ.get("RELAY_WORKFLOW_FILE") or "relay-dispatch.yml"
    jobs_db = os.environ.get("RELAY_JOBS_DB")
    server.jobs = JobStore(jobs_db) if jobs_db else None
    server.agent_token = os.environ.get("RELAY_AGENT_TOKEN") or None
    server.lease_seconds = int(os.environ.get("RELAY_JOB_LEASE_SECONDS", "300"))
    server.job_timeout = int(os.environ.get("RELAY_JOB_TIMEOUT", "3600"))
    server.direct_repositories = tuple(
        repository.strip()
        for repository in os.environ.get("RELAY_DIRECT_REPOSITORIES", "").split(",")
        if repository.strip()
    )
    server.status_context = os.environ.get("RELAY_STATUS_CONTEXT") or "relay/ci"
    if not server.webhook_secret:
        # refuse loudly at startup rather than serving a path that 503s later
        print("warning: RELAY_WEBHOOK_SECRET is unset, /relay/dispatch will refuse every delivery")
    if not server.dispatch_token:
        print("warning: RELAY_DISPATCH_TOKEN is unset, /v1/dispatch will refuse every request")
    if server.jobs is not None and not server.agent_token:
        print("warning: RELAY_JOBS_DB is set but RELAY_AGENT_TOKEN is unset, /v1/jobs will refuse every request")
    try:
        server.serve_forever()
    finally:
        server.server_close()
