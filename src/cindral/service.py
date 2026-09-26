"""HTTP service for routing decisions."""
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

from .github import GitHubAPIError, GitHubClient, GitHubDispatchError, RepositoryRunner, is_repository_slug
from .jobs import JobStore
from .models import RouteDecision, RouteRequest, Runner
from .policy import Policy, RouteUnavailable
from .state import load_runners
from .webhook import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    WebhookError,
    parse_pull_request_event,
    parse_push_event,
    verify_signature,
)

WEBHOOK_PATH = "/cindral/dispatch"
MAX_BODY = 1 << 20


class CindralServer(ThreadingHTTPServer):
    policy: Policy
    runners: tuple
    github: GitHubClient | None
    webhook_secret: str | None
    dispatch_token: str | None
    workflow_file: str
    capacity_lock: threading.Lock
    runner_reservations: dict[str, dict[int, tuple[float, tuple[str, ...]]]]
    reservation_seconds: int
    jobs: JobStore | None = None
    agent_token: str | None = None
    lease_seconds: int = 300
    job_timeout: int = 3600
    direct_repositories: tuple[str, ...] = ()
    status_context: str = "cindral/ci"


class CindralHandler(BaseHTTPRequestHandler):
    server: CindralServer

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
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})
            return
        inputs = payload.get("inputs", {})
        if not isinstance(inputs, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in inputs.items()
        ):
            self._send(400, {"error": "inputs must be a string-to-string object"})
            return
        if self.path == "/v1/route":
            try:
                decision = self.server.policy.choose(request, self.server.runners)
            except RouteUnavailable as exc:
                self._send(409, {"error": str(exc)})
                return
            self._send(200, decision.as_dict())
            return
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
        if not is_repository_slug(repository):
            self._send(400, {"error": "invalid repository name"})
            return
        try:
            decision, reservation = self._dispatch_decision(request, repository)
        except GitHubAPIError as exc:
            self._send(502, {"error": str(exc)})
            return
        except RouteUnavailable as exc:
            self._send(409, {"error": str(exc)})
            return
        self._dispatch(repository, workflow, ref, inputs, request, decision, reservation)

    def _dispatch(
        self,
        repository: str,
        workflow: str,
        ref: str,
        inputs: dict[str, str],
        request: RouteRequest,
        decision: RouteDecision,
        reservation: tuple[str, int] | None,
    ) -> None:
        inputs = dict(inputs)
        inputs.update(
            {
                "cindral_lane": decision.lane,
                "cindral_target": request.target or "",
                "cindral_reason": decision.reason,
            }
        )
        if not self._dispatch_to_github(repository, workflow, ref, inputs, reservation):
            return
        self._send(200, {**decision.as_dict(), "dispatched": True})

    def _dispatch_to_github(
        self,
        repository: str,
        workflow: str,
        ref: str,
        inputs: dict[str, str],
        reservation: tuple[str, int] | None,
    ) -> bool:
        try:
            self.server.github.dispatch(repository, workflow, ref, inputs)
            return True
        except GitHubDispatchError as exc:
            self._release_reservation(reservation)
            self._send(502, {"error": str(exc)})
            return False
        except Exception as exc:
            self._release_reservation(reservation)
            self.log_error("unexpected GitHub dispatch failure: %s", exc)
            self._send(502, {"error": "GitHub dispatch failed"})
            return False

    def _dispatch_decision(
        self,
        request: RouteRequest,
        repository: str,
    ) -> tuple[RouteDecision, tuple[str, int] | None]:
        hosted_decision: RouteDecision | None = None
        try:
            hosted_decision = self.server.policy.choose(request, ())
        except RouteUnavailable:
            pass
        if hosted_decision is not None:
            return hosted_decision, None
        if self.server.github is None:
            raise GitHubAPIError("GitHub runner lookup is not configured")

        repository_runners = self.server.github.list_runners(repository)
        now = time.monotonic()
        with self.server.capacity_lock:
            self._expire_reservations(repository, now)

            local_runners = tuple(
                Runner(
                    name=runner.name,
                    status=runner.status,
                    busy=runner.busy,
                    labels=runner.labels,
                    healthy=runner.status == "online",
                )
                for runner in repository_runners
            )
            decision = self.server.policy.choose(request, local_runners)
            online_idle = sum(runner.status == "online" and not runner.busy for runner in repository_runners)
            label_key = tuple(sorted(decision.runs_on))
            required_labels = set(label_key)
            lane_idle = sum(
                runner.status == "online"
                and not runner.busy
                and required_labels.issubset(set(runner.labels))
                for runner in repository_runners
            )
            reservations = self.server.runner_reservations.get(repository, {})
            reserved = self._overlapping_reservations(
                repository_runners,
                required_labels,
                reservations.values(),
            )
            available = lane_idle - reserved
            if decision.lane != "hosted" and available <= 0:
                raise RouteUnavailable("all eligible repository runners are busy or reserved")
            snapshot = {
                "source": "github_repository_runners",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "registered": len(repository_runners),
                "online_idle": online_idle,
                "lane_idle": lane_idle,
                "reserved": reserved,
                "available": max(available, 0),
                "runner_candidate": decision.runner,
            }
            decision = replace(decision, capacity=snapshot)
            reservation = None
            if decision.lane != "hosted" and decision.runner:
                reservation_id = time.monotonic_ns()
                reservation = (repository, reservation_id)
                self.server.runner_reservations.setdefault(repository, {})[reservation_id] = (
                    now + self.server.reservation_seconds,
                    label_key,
                )
            return decision, reservation

    def _expire_reservations(self, repository: str, now: float) -> None:
        reservations = self.server.runner_reservations.get(repository, {})
        active = {
            reservation_id: reservation
            for reservation_id, reservation in reservations.items()
            if reservation[0] > now
        }
        if active:
            self.server.runner_reservations[repository] = active
        else:
            self.server.runner_reservations.pop(repository, None)

    def _overlapping_reservations(
        self,
        repository_runners: tuple[RepositoryRunner, ...],
        required_labels: set[str],
        reservations: Iterable[tuple[float, tuple[str, ...]]],
    ) -> int:
        idle_labels = [
            set(runner.labels)
            for runner in repository_runners
            if runner.status == "online" and not runner.busy
        ]
        return sum(
            any(set(reservation_labels).union(required_labels).issubset(labels) for labels in idle_labels)
            for _, reservation_labels in reservations
        )

    def _release_reservation(self, reservation: tuple[str, int] | None) -> None:
        if reservation is None:
            return
        with self.server.capacity_lock:
            repository, reservation_id = reservation
            reservations = self.server.runner_reservations.get(repository, {})
            reservations.pop(reservation_id, None)
            if not reservations:
                self.server.runner_reservations.pop(repository, None)

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
        method = self.command
        try:
            if path == "/v1/jobs/claim":
                if method != "POST":
                    self._send(405, {"error": "claim requires POST"})
                    return
                self._claim(store)
                return
            if path.startswith("/v1/jobs/"):
                job_id, _, action = path[len("/v1/jobs/"):].partition("/")
                if not job_id:
                    self._send(404, {"error": "not found"})
                    return
                if action in {"renew", "report"}:
                    if method != "POST":
                        self._send(405, {"error": f"{action} requires POST"})
                        return
                    if action == "renew":
                        self._renew(store, job_id)
                    else:
                        self._report(store, job_id)
                    return
                if action == "":
                    if method != "GET":
                        self._send(405, {"error": "job status requires GET"})
                        return
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
        job = store.get(job_id)
        if job is None:
            self._send(404, {"error": "unknown job"})
            return
        payload = self._read_json()
        device = str(payload["device"])
        exit_code = int(payload["exit_code"])
        log = str(payload.get("log", "") or "")
        if not store.holds_lease(job_id, device):
            self._send(409, {"error": "job is not leased to this device"})
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
                self._send(502, {"error": f"commit status update failed: {exc}"})
                return
        try:
            job = store.report(job_id, device, exit_code, log=log)
        except ValueError as exc:
            self._send(409, {"error": str(exc)})
            return
        self._send(200, {**job.as_dict(), "status_posted": self.server.github is not None})

    def _job_status(self, store: JobStore, job_id: str) -> None:
        job = store.get(job_id)
        if job is None:
            self._send(404, {"error": "unknown job"})
            return
        self._send(200, job.as_dict())

    def _enqueue_direct(
        self,
        repository: str,
        sha: str,
        ref: str,
        status_sha: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not self.server.agent_token:
            self._send(503, {"error": "direct execution requires CINDRAL_AGENT_TOKEN"})
            return
        jobs = self.server.jobs
        assert jobs is not None
        delivery = self.headers.get(DELIVERY_HEADER) or None
        job = jobs.enqueue(
            repository,
            sha,
            ref,
            timeout=self.server.job_timeout,
            delivery=delivery,
            status_sha=status_sha,
        )
        posted = False
        if self.server.github is not None:
            try:
                self.server.github.post_status(
                    repository,
                    status_sha or sha,
                    "pending",
                    "queued on the device pool",
                    context=self.server.status_context,
                )
                posted = True
            except GitHubAPIError:
                posted = False
        payload: dict[str, Any] = {
            "queued": True,
            "job": job.id,
            "repository": repository,
            "sha": sha,
            "status_posted": posted,
        }
        if extra:
            payload.update(extra)
        self._send(202, payload)

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
        event_name = self.headers.get(EVENT_HEADER)
        if event_name not in {"push", "pull_request"}:
            self._send(202, {"status": "ignored", "reason": "event is not supported"})
            return
        if event_name == "pull_request":
            self._pull_request_webhook(body)
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
                self._send(202, {"status": "ignored", "reason": "repository has not opted in with the cindral workflow"})
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
            decision, reservation = self._dispatch_decision(request, event.repository)
        except GitHubAPIError as exc:
            self._send(502, {"error": str(exc)})
            return
        except RouteUnavailable as exc:
            self._send(409, {"error": str(exc)})
            return
        inputs = {
            "cindral_lane": decision.lane,
            "cindral_target": request.target or "",
            "cindral_reason": decision.reason,
        }
        if not self._dispatch_to_github(
            event.repository,
            self.server.workflow_file,
            branch,
            inputs,
            reservation,
        ):
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

    def _pull_request_webhook(self, body: bytes) -> None:
        try:
            event = parse_pull_request_event(body)
        except WebhookError as exc:
            self._send(400, {"error": str(exc)})
            return
        if not is_repository_slug(event.repository):
            self._send(400, {"error": "invalid repository name"})
            return
        if not event.should_route:
            self._send(202, {"status": "ignored", "reason": f"pull request action is not routed: {event.action}"})
            return
        # direct repositories run trusted, same-repository pull requests on the
        # device pool; fork and untrusted pull requests stay on the hosted lane
        if (
            self.server.jobs is not None
            and event.repository in self.server.direct_repositories
            and event.direct_eligible
        ):
            self._enqueue_direct(
                event.repository,
                event.run_sha,
                event.ref,
                status_sha=event.status_sha,
                extra={"pull_request": event.number, "trusted": True},
            )
            return
        if self.server.github is None:
            self._send(503, {"error": "GitHub dispatch token is not configured"})
            return
        try:
            if not self.server.github.workflow_exists(event.repository, self.server.workflow_file):
                self._send(202, {"status": "ignored", "reason": "repository has not opted in with the cindral workflow"})
                return
        except GitHubAPIError as exc:
            self._send(502, {"error": str(exc)})
            return
        request = RouteRequest(
            requested_lane="auto" if event.trusted else "hosted",
            repository_visibility="private" if event.private else "public",
            quota_status="unknown",
        )
        try:
            decision = self.server.policy.choose(request, self.server.runners)
        except RouteUnavailable as exc:
            self._send(409, {"error": str(exc)})
            return
        inputs = {
            "cindral_lane": decision.lane,
            "cindral_target": request.target or "",
            "cindral_reason": (
                decision.reason if event.trusted else "untrusted pull request requires hosted execution"
            ),
            "cindral_ref": event.ref,
        }
        try:
            self.server.github.dispatch(event.repository, self.server.workflow_file, event.default_branch, inputs)
        except GitHubDispatchError as exc:
            self._send(502, {"error": str(exc)})
            return
        self._send(
            200,
            {
                **decision.as_dict(),
                "reason": inputs["cindral_reason"],
                "dispatched": True,
                "repository": event.repository,
                "pull_request": event.number,
                "ref": event.ref,
                "trusted": event.trusted,
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
    server = CindralServer((host, port), CindralHandler)
    server.policy = Policy.load(policy_path)
    server.runners = load_runners(state_path)
    server.github = GitHubClient(github_token) if github_token else None
    server.webhook_secret = os.environ.get("CINDRAL_WEBHOOK_SECRET") or None
    server.dispatch_token = os.environ.get("CINDRAL_DISPATCH_TOKEN") or None
    server.workflow_file = os.environ.get("CINDRAL_WORKFLOW_FILE") or "cindral-dispatch.yml"
    server.capacity_lock = threading.Lock()
    server.runner_reservations = {}
    server.reservation_seconds = int(os.environ.get("CINDRAL_RUNNER_RESERVATION_SECONDS", "10"))
    if server.reservation_seconds < 1:
        raise ValueError("CINDRAL_RUNNER_RESERVATION_SECONDS must be positive")
    jobs_db = os.environ.get("CINDRAL_JOBS_DB")
    server.jobs = JobStore(jobs_db) if jobs_db else None
    server.agent_token = os.environ.get("CINDRAL_AGENT_TOKEN") or None
    server.lease_seconds = int(os.environ.get("CINDRAL_JOB_LEASE_SECONDS", "300"))
    server.job_timeout = int(os.environ.get("CINDRAL_JOB_TIMEOUT", "3600"))
    server.direct_repositories = tuple(
        repository.strip()
        for repository in os.environ.get("CINDRAL_DIRECT_REPOSITORIES", "").split(",")
        if repository.strip()
    )
    server.status_context = os.environ.get("CINDRAL_STATUS_CONTEXT") or "cindral/ci"
    if not server.webhook_secret:
        # refuse loudly at startup rather than serving a path that 503s later
        print("warning: CINDRAL_WEBHOOK_SECRET is unset, /cindral/dispatch will refuse every delivery")
    if not server.dispatch_token:
        print("warning: CINDRAL_DISPATCH_TOKEN is unset, /v1/dispatch will refuse every request")
    if server.jobs is not None and not server.agent_token:
        print("warning: CINDRAL_JOBS_DB is set but CINDRAL_AGENT_TOKEN is unset, /v1/jobs will refuse every request")
    try:
        server.serve_forever()
    finally:
        server.server_close()
