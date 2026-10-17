"""HTTP service for routing decisions."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable
from urllib.parse import quote, urlsplit

from .artifactapi import attach_artifacts, discard_artifact
from .cache import CACHE_REPOSITORY_QUOTA, CACHE_STORAGE_QUOTA, CACHE_TTL_SECONDS, MAX_CACHE_BLOB_BYTES, CacheStore
from .check_runs import publish_check_run_quietly
from .dispatch_capacity import choose_webhook_lane, overlapping_reservations
from . import device_auth
from .device_auth import DeviceAuthMixin
from .deliveries import DeliveryStore
from .metering import MeteredLedger
from .app_auth import AppAuth, probe_block, startup_report
from .github import GitHubAPIError, GitHubClient, GitHubDispatchError, RepositoryRunner, is_repository_slug
from .jobapi import AGENT_HEARTBEAT_PATH, JobApiMixin
from .jobrecord import head_sha_for
from .jobs import JobStore
from .models import RouteDecision, RouteRequest, Runner
from .observability import METRICS_PATH, POOL_PATH, ObservabilityMixin, health_payload
from .dispatch_ref import checked_dispatch_ref
from .policy import DispatchNotPermitted, Policy, RouteUnavailable
from . import refusal
from .budget import DispatchBudget, attach_budget, release_reservation
from .quota import QuotaSource, configured_source
from .scheduling import positive_repository_limit
from .state import load_runners
from .startup import warn_on_missing_configuration
from .webhook import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    WebhookError,
    parse_pull_request_event,
    parse_push_event,
    verify_signature,
)
from .pull_request_dispatch import PullRequestDispatchMixin
from .lease_reclaim import RECLAIM_INTERVAL_SECONDS, check_run_publisher, reclaim_loop, status_publisher
from .webhook_dispatch import (
    DispatchSendingMixin,
    WebhookDispatchMixin,
    delivery_fallback,
    replay_loop,
)

WEBHOOK_PATH = "/cindral/dispatch"
MAX_BODY = 1 << 20
JOB_ID_PLACEHOLDER = "{job_id}"


def _note(message: str) -> None:
    print(message, flush=True)


def attach_quota_source(server: CindralServer, github: GitHubClient | None) -> QuotaSource | None:
    """Attach the billing-backed quota source the environment names, if any."""
    source = configured_source(github, os.environ.get, _note)
    if source is not None:
        server.policy = replace(server.policy, quota_source=source)
    return source


def parse_status_target_url(value: str) -> str:
    """Validate the template a commit status links to."""
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("CINDRAL_STATUS_TARGET_URL must be an http or https URL")
    if parsed.username or parsed.password:
        raise ValueError("CINDRAL_STATUS_TARGET_URL must not embed credentials")
    if JOB_ID_PLACEHOLDER not in value:
        raise ValueError(f"CINDRAL_STATUS_TARGET_URL must contain {JOB_ID_PLACEHOLDER}")
    return value


class CindralServer(ThreadingHTTPServer):
    #: the App installation's health, set by serve(). None means no App is
    #: configured, which is a valid deployment, so /healthz stays plain.
    app: dict[str, Any] | None = None

    def handle_error(self, request: object, client_address: object) -> None:
        """A client hanging up mid-request is routine, so no traceback for it."""
        kind = sys.exc_info()[0]
        if kind is not None and issubclass(kind, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


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
    deliveries: DeliveryStore | None = None
    metered: MeteredLedger | None = None
    cache: CacheStore | None = None
    agent_token: str | None = None
    #: device name -> that device's own token. Absent means the shared
    #: CINDRAL_AGENT_TOKEN still stands in, which is the rollout path.
    agent_tokens: Mapping[str, str] | None = None
    # read-only credential for /v1/pool, so a control panel can inspect the
    # queue without holding the agent token that can claim and report jobs
    pool_token: str | None = None
    lease_seconds: int = 300
    job_timeout: int = 3600
    max_repository_running: int = 2
    status_context: str = "cindral/ci"
    status_target_url: str = ""

    def job_target_url(self, job_id: str) -> str:
        if not self.status_target_url:
            return ""
        return self.status_target_url.replace(JOB_ID_PLACEHOLDER, quote(job_id, safe=""))


class CindralHandler(
    WebhookDispatchMixin,
    DispatchSendingMixin,
    PullRequestDispatchMixin,
    JobApiMixin,
    DeviceAuthMixin,
    ObservabilityMixin,
    BaseHTTPRequestHandler,
):
    server: CindralServer

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, health_payload(self.server.app, self.server.policy.fingerprint, self.server.policy))
            return
        if self.path == METRICS_PATH:
            self._metrics()
            return
        if self.path.split("?", 1)[0] == POOL_PATH:
            self._pool()
            return
        if self.path.split("?", 1)[0].startswith("/v1/jobs/"):
            self._jobs()
            return
        if self.path.split("?", 1)[0].startswith("/v1/cache/"):
            self._cache()
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == WEBHOOK_PATH:
            self._webhook()
            return
        if self.path.split("?", 1)[0].startswith("/v1/jobs"):
            self._jobs()
            return
        if self.path.split("?", 1)[0] == AGENT_HEARTBEAT_PATH:
            self._agents()
            return
        if self.path.split("?", 1)[0].startswith("/v1/cache/"):
            self._cache()
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
            # an exact match against a configured name, which subsumes the slug check
            self.server.policy.check_dispatch(repository, workflow, ref)
            checked_dispatch_ref(self.server.policy, repository, workflow, inputs)
        except KeyError as exc:
            self._send(400, {"error": f"missing dispatch field: {exc.args[0]}"})
            return
        except DispatchNotPermitted as exc:
            self._send(403, {"error": str(exc)})
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

    def do_PUT(self) -> None:
        if self.path.split("?", 1)[0].startswith("/v1/cache/"):
            self._cache()
            return
        self._send(404, {"error": "not found"})

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
        if not self._dispatch_to_github(repository, workflow, ref, inputs, reservation, decision):
            return
        self._send(200, {**decision.as_dict(), "dispatched": True})

    def _dispatch_to_github(
        self,
        repository: str,
        workflow: str,
        ref: str,
        inputs: dict[str, str],
        reservation: tuple[str, int] | None,
        decision: RouteDecision,
    ) -> bool:
        try:
            self._send_dispatch(repository, workflow, ref, inputs)
        except GitHubDispatchError as exc:
            err = str(exc)
            # A 4xx and an unset status are both definite: GitHub refused, or the
            # request never reached it. A 5xx may already have started a run, and
            # a run that exists has to be counted.
            code = exc.status_code
            definite = code is None or code < 500
        except Exception as exc:
            # a connection that failed part way through is not an answer, and the
            # run it started may exist. An unreleased hold makes the ceiling one
            # dispatch tight; an erased one books a run nobody paid for.
            self.log_error("unexpected GitHub dispatch failure: %s", exc)
            err = "GitHub dispatch failed"
            definite = False
        else:
            self._name_hold(decision, repository, workflow, ref)
            return True

        try:
            if definite:
                release_reservation(self.server, decision.reservation)
        finally:
            self._release_reservation(reservation)
            self._send(502, {"error": err})
        return False

    def _dispatch_decision(
        self,
        request: RouteRequest,
        repository: str,
        allow_busy: bool = False,
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
            decision = choose_webhook_lane(self.server.policy, request, local_runners, allow_busy)
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
            reserved = overlapping_reservations(
                repository_runners,
                required_labels,
                reservations.values(),
            )
            available = lane_idle - reserved
            if decision.lane != "hosted" and available <= 0 and (not allow_busy or lane_idle > 0):
                raise RouteUnavailable("all eligible repository runners are busy or reserved")
            snapshot = {
                "source": "github_repository_runners",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "registered": len(repository_runners),
                "online_idle": online_idle,
                "lane_idle": lane_idle,
                "reserved": reserved,
                "available": max(available, 0),
                "queued_by_github": decision.lane != "hosted" and available <= 0,
                "runner_candidate": decision.runner,
            }
            decision = replace(decision, capacity=snapshot)
            reservation = None
            if decision.lane != "hosted" and decision.runner and available > 0:
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
        return self._token_authorized(self.server.dispatch_token)


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
        publish_check_run_quietly(self.server.github, job, target_url=self.server.job_target_url(job.id))
        posted = False
        if self.server.github is not None:
            try:
                self.server.github.post_status(
                    repository,
                    head_sha_for(sha, status_sha),
                    "pending",
                    "queued on the device pool",
                    context=self.server.status_context,
                    target_url=self.server.job_target_url(job.id),
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
        if not branch and event.ref.startswith("refs/tags/"):
            self._send(422, refusal.tag_push(event.ref))
            return
        if not branch or branch != event.default_branch:
            self._send(202, refusal.non_default_branch(branch, event.ref))
            return
        lane = self.server.policy.push_lane(event.repository) if self.server.jobs else None
        if lane == "device":
            self._enqueue_direct(event.repository, event.after, branch)
            return
        fallback = None if lane is None else (
            lambda: self._enqueue_direct(event.repository, event.after, branch)
        )
        self._dispatch_push(event, branch, body, fallback)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: int, payload: dict[str, Any] | None = None, cors: bool = False) -> None:
        if payload is None:
            self._respond(status, cors=cors)
            return
        self._respond(status, "application/json", json.dumps(payload).encode(), cors=cors)

    def _send_text(self, status: int, text: str, content_type: str = "text/plain; version=0.0.4; charset=utf-8") -> None:
        self._respond(status, content_type, text.encode("utf-8"))

    def _respond(self, status: int, content_type: str = "", body: bytes = b"", *, cors: bool = False) -> None:
        """Status line, the two headers every response carries, then the body.

        One place, because the alternative is each caller repeating the ordering
        and a header that goes missing on whichever path nobody tested.
        """
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


def build_app_auth(getenv: Callable[[str], str | None] = os.environ.get) -> AppAuth | None:
    """The App installation the broker authenticates as, or None when unset.

    An App is what makes account-wide discovery safe: a user token sees every
    repository its owner can see, including ones they merely collaborate on,
    while an installation sees only what it covers. With neither variable set
    the broker falls back to the user token and behaves exactly as before.
    """
    client_id = (getenv("CINDRAL_APP_CLIENT_ID") or "").strip()
    key_path = (getenv("CINDRAL_APP_PRIVATE_KEY") or "").strip()
    if not client_id and not key_path:
        return None
    if not client_id or not key_path:
        missing = "CINDRAL_APP_CLIENT_ID" if not client_id else "CINDRAL_APP_PRIVATE_KEY"
        raise ValueError(f"{missing} is unset; set both or neither, or the App cannot authenticate")
    try:
        private_key = Path(key_path).read_bytes()
    except OSError as exc:
        # the path and the reason, never the file's contents
        raise ValueError(
            f"the App private key named by CINDRAL_APP_PRIVATE_KEY could not be read: {exc.strerror}"
        ) from None
    return AppAuth(client_id, private_key, api_url=getenv("CINDRAL_APP_API_URL") or "https://api.github.com")


def serve(policy_path: str | Path, state_path: str | Path, host: str, port: int, github_token: str | None = None) -> None:
    server = CindralServer((host, port), CindralHandler)
    server.policy = Policy.load(policy_path)
    server.runners = load_runners(state_path)
    app_auth = build_app_auth()
    if app_auth is not None:
        # Refusing here is the point. An App that is uninstalled, suspended, or
        # scoped to nothing would leave every push undelivered with nothing in
        # the logs to say why, and a pod that stayed up would look healthy.
        server.app = probe_block(app_auth)
        # Both facts come off the block rather than being resolved a second time:
        # probe_block already asked, and asking again costs an API call and can
        # disagree with itself. And the block's shape is not fixed -- health() returns
        # ok: False with a reason when the installation cannot be read -- so the
        # degraded case is printed as itself rather than indexing a key it may not
        # have. A broker that cannot authenticate says so at startup, which is the
        # one moment somebody is reading.
        print(startup_report(server.app), flush=True)
    server.github = (
        GitHubClient(app_auth.token)
        if app_auth is not None
        else GitHubClient(github_token) if github_token else None
    )
    # App auth can read the owner's Actions billing, so a quota source reading
    # as unavailable under it is a credential gap, not a configuration choice
    server.quota_source = attach_quota_source(server, server.github)
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
    server.deliveries = DeliveryStore(jobs_db) if jobs_db else None
    # its own table rather than the delivery store's, because a metered dispatch
    # is a spend and not a delivery: it is written from three different paths and
    # is never replayed onto a device
    server.metered = MeteredLedger(jobs_db) if jobs_db else None
    attach_budget(server, server.metered)
    server.agent_token = os.environ.get("CINDRAL_AGENT_TOKEN") or None
    server.agent_tokens = device_auth.parse(os.environ.get("CINDRAL_AGENT_TOKENS"))
    server.pool_token = os.environ.get("CINDRAL_POOL_TOKEN") or None
    if server.pool_token and server.pool_token in {server.agent_token, server.dispatch_token}:
        # sharing a value would hand the read-only consumer the write capability
        raise ValueError("CINDRAL_POOL_TOKEN must differ from CINDRAL_AGENT_TOKEN and CINDRAL_DISPATCH_TOKEN")
    if jobs_db:
        server.cache = CacheStore(
            os.environ.get("CINDRAL_CACHE_DIR") or str(Path(jobs_db).parent / "cache"),
            ttl_seconds=int(os.environ.get("CINDRAL_CACHE_TTL_SECONDS", str(CACHE_TTL_SECONDS))),
            repository_quota=int(os.environ.get("CINDRAL_CACHE_REPOSITORY_QUOTA", str(CACHE_REPOSITORY_QUOTA))),
            storage_quota=int(os.environ.get("CINDRAL_CACHE_STORAGE_QUOTA", str(CACHE_STORAGE_QUOTA))),
            max_blob_bytes=int(os.environ.get("CINDRAL_CACHE_MAX_BLOB_BYTES", str(MAX_CACHE_BLOB_BYTES))),
        )
        # The artifact store is built here for the same reason the cache is: a
        # mistyped CINDRAL_ARTIFACT_* value should stop the broker at boot, in a
        # log, where a wrong TTL is obvious. Built per request it surfaced as a
        # 400 on every job read while /healthz stayed green and the pool carried
        # on, so the misconfiguration looked like a client error.
        attach_artifacts(server)
    server.lease_seconds = int(os.environ.get("CINDRAL_JOB_LEASE_SECONDS", "300"))
    server.job_timeout = int(os.environ.get("CINDRAL_JOB_TIMEOUT", "3600"))
    server.max_repository_running = positive_repository_limit(os.environ.get("CINDRAL_MAX_REPOSITORY_RUNNING", "2"))
    server.status_context = os.environ.get("CINDRAL_STATUS_CONTEXT") or "cindral/ci"
    server.status_target_url = parse_status_target_url(
        os.environ.get("CINDRAL_STATUS_TARGET_URL") or ""
    )
    warn_on_missing_configuration(server)
    quota_source = server.quota_source
    reclaim_stop = threading.Event()
    replay_stop = threading.Event()
    replay_thread = None
    if server.deliveries is not None:
        uncertain = server.deliveries.recover()
        if uncertain:
            print(f"warning: {uncertain} interrupted GitHub dispatches need reconciliation", flush=True)
        replay_thread = threading.Thread(
            target=replay_loop,
            args=(server.deliveries, server.github, server.policy, replay_stop, 60),
            # the device lane a refused delivery is handed to, so a push the
            # broker answered and then lost is not a push that ran nowhere.
            # Keyword arguments, because replay_loop's last two are keyword-only
            # and a positional seventh would raise inside the thread on every
            # broker start, which is what a signature change once did.
            kwargs={"fallback": delivery_fallback(server), "ledger": server.metered},
            daemon=True,
        )
        replay_thread.start()
    # The reclaim timer, so a lapsed lease is settled on its own rather than when
    # a device happens to ask for work. Without it the queue only moves on
    # traffic, and a parked run's verdict waits for a device to come back.
    reclaim_thread = None
    if server.jobs is not None:
        reclaim_thread = threading.Thread(
            target=reclaim_loop,
            args=(
                server.jobs,
                status_publisher(
                    server.github, server.status_context, server.job_target_url
                ),
                reclaim_stop,
                RECLAIM_INTERVAL_SECONDS,
                check_run_publisher(server.github, server.job_target_url),
            ),
            # keyword-only, and load-bearing: a release on the timer that does not
            # drop the archive the lost attempt left behind is a reader served one
            # run's bytes under another's verdict, and this timer is the path that
            # runs when no device is asking for work.
            kwargs={"discard": lambda job_id: discard_artifact(server, job_id)},
            daemon=True,
        )
        reclaim_thread.start()
    try:
        server.serve_forever()
    finally:
        reclaim_stop.set()
        replay_stop.set()
        if reclaim_thread is not None:
            reclaim_thread.join(5)
        if replay_thread is not None:
            replay_thread.join(timeout=2)
        if quota_source is not None:
            quota_source.stop()
        server.server_close()
