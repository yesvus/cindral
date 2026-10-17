"""Agent-facing API: job claims, leases and reports, cache transfer, presence."""
from dataclasses import dataclass, field
import json
from typing import Any

from .agent import MAX_CONCURRENCY
from .artifactapi import ArtifactApiMixin
from .build_info import current_build
from .cache_api import CacheApiMixin
from .check_runs import publish_check_run_quietly
from .github import GitHubAPIError
from .jobrecord import Job, head_sha_for
from .lease_reclaim import POST_TIMEOUT_SECONDS, drain_owed, release_expired
from .jobs import JobStore
from .retry import LEASE_LOST
from .status_text import failure_description, lost_description
from .steps import encode_steps

AGENT_HEARTBEAT_PATH = "/v1/agents/heartbeat"
# a device name and a label are operator-chosen strings, so the bounds only
# exist to keep one agent from filling the snapshot with megabytes of payload
MAX_DEVICE_CHARS = 200
MAX_LABELS = 64
MAX_LABEL_CHARS = 200


@dataclass(frozen=True)
class Heartbeat:
    device: str
    labels: tuple[str, ...]
    max_concurrency: int
    build: dict[str, str] = field(default_factory=dict)


def parse_heartbeat(payload: dict[str, Any]) -> Heartbeat:
    """Validate an agent's announcement of itself and its own capacity.

    The reported limit is what the device says it can run, not proof of it: the
    snapshot counts active work from leases, so a device claiming more slots
    than it holds only misreports itself.
    """
    device = payload.get("device")
    if not isinstance(device, str) or not device.strip():
        raise ValueError("device must be a non-empty string")
    if len(device) > MAX_DEVICE_CHARS:
        raise ValueError(f"device must be at most {MAX_DEVICE_CHARS} characters")
    build = payload.get("build", {})
    if not isinstance(build, dict) or not all(
        isinstance(value, str) for value in build.values()
    ):
        raise ValueError("build must be an object of strings")
    # the three fields are named rather than trusted wholesale, so a device
    # cannot put an arbitrary blob in a column the panel renders
    build = {key: build[key] for key in ("version", "commit", "built_at") if key in build}
    labels = payload.get("labels")
    if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
        raise ValueError("labels must be an array of strings")
    if len(labels) > MAX_LABELS:
        raise ValueError(f"labels must hold at most {MAX_LABELS} entries")
    if any(not label.strip() or len(label) > MAX_LABEL_CHARS for label in labels):
        raise ValueError(f"each label must be 1 to {MAX_LABEL_CHARS} characters")
    concurrency = payload.get("max_concurrency")
    # bool is an int, and a float or a digit string is not the positive integer
    # the contract promises
    if isinstance(concurrency, bool) or not isinstance(concurrency, int):
        raise ValueError("max_concurrency must be an integer")
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"max_concurrency must be between 1 and {MAX_CONCURRENCY}")
    return Heartbeat(
        device=device, labels=tuple(labels), max_concurrency=concurrency, build=build
    )


class JobApiMixin(ArtifactApiMixin, CacheApiMixin):
    """Handlers for the agent-facing ``/v1`` paths: jobs, cache, and presence.

    Claim, renew, and report mutate the queue and take the agent token. Reading
    one job's status, log, or artifact also takes the read-only pool token, so
    the panel a commit status links to can inspect a run without holding the
    credential that can lease work.
    """

    server: Any

    def _read_authorized(self) -> bool:
        return self._agent_authorized() or self._pool_authorized()  # type: ignore[attr-defined]

    def _agents(self) -> None:
        """Handle ``POST /v1/agents/heartbeat``: an agent announcing itself.

        The agent token alone: presence is a claim about a device, so a consumer
        that only reads the pool must not be able to make one.
        """
        store = self.server.jobs
        if store is None:
            self._send(503, {"error": "job queue is not configured"})  # type: ignore[attr-defined]
            return
        if not self._agent_authorized():  # type: ignore[attr-defined]
            self._send(401, {"error": "agent requests require a bearer token"})  # type: ignore[attr-defined]
            return
        try:
            heartbeat = parse_heartbeat(self._read_json())  # type: ignore[attr-defined]
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})  # type: ignore[attr-defined]
            return
        if not self._agent_authorized_as(heartbeat.device):  # type: ignore[attr-defined]
            # a heartbeat declares labels and concurrency, so a forged one can put
            # a device into the pool with capacity it does not have
            self._send(401, {"error": "this token does not speak for that device"})  # type: ignore[attr-defined]
            return
        last_seen = store.record_heartbeat(
            heartbeat.device, heartbeat.labels, heartbeat.max_concurrency, build=heartbeat.build
        )
        # answered with the broker's own build, so an agent can see from its own
        # log how far behind it is without asking a human to compare two commits
        self._send(  # type: ignore[attr-defined]
            200,
            {
                "recorded": True,
                "device": heartbeat.device,
                "last_seen": last_seen,
                "broker_build": current_build().as_dict(),
            },
        )

    def _jobs(self) -> None:
        store = self.server.jobs
        if store is None:
            self._send(503, {"error": "job queue is not configured"})  # type: ignore[attr-defined]
            return
        path = self.path.split("?", 1)[0].rstrip("/")  # type: ignore[attr-defined]
        # only the status lookup is read-only; everything else mutates the queue
        # or the run's stored bytes, so uploading an artifact sits with the
        # writes even though the path is the read one
        mutating = path.endswith(("/claim", "/renew", "/report")) or (
            path.endswith("/artifacts") and self.command != "GET"  # type: ignore[attr-defined]
        )
        authorized = (  # type: ignore[attr-defined]
            self._read_authorized() if not mutating else self._agent_authorized()
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
                if action == "log":
                    self._require(method, "GET", "job log")
                    if method == "GET":
                        self._job_log(store, job_id)
                    return
                if action == "artifacts":
                    if method == "GET":
                        self._artifact_download(store, job_id)
                    elif method == "POST":
                        self._artifact_upload(store, job_id)
                    else:
                        self._send(405, {"error": "job artifacts requires GET or POST"})  # type: ignore[attr-defined]
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
        # a lapsed lease is how the broker learns a device is gone, and the only
        # moment it can still tell the commit which run was lost. The text the
        # release owes is written with the release, so a status that fails to
        # publish here is still owed and the drain below retries it.
        # the error this release records is the lapsed lease itself, which the
        # row cannot show yet: it is written after this text is computed
        # The release and the artifact it invalidates go together, and both
        # callers go through `release_expired` so neither can do one without the
        # other: the reclaim timer runs every minute in every broker, and doing it
        # only here left the previous attempt's archive in place on that path.
        release_expired(
            store,
            publish=lambda job: json.dumps(lost_description(job, LEASE_LOST)),
            discard=self._discard_artifact,
        )
        self._drain_statuses(store)
        if not self._agent_authorized_as(device):  # type: ignore[attr-defined]
            # A claim is how a device *takes* work, so a forged one is not
            # impersonation alone: it takes capacity away from the pool. Checked
            # after the name is read, because the name is what the credential has
            # to be checked against.
            self._send(401, {"error": "this token does not speak for that device"})  # type: ignore[attr-defined]
            return
        job = store.claim(device, labels, lease_seconds=lease, max_repository_running=self.server.max_repository_running)
        if job is None:
            self._send(204)  # type: ignore[attr-defined]
            return
        self._send(200, {"job": job.as_dict()})  # type: ignore[attr-defined]

    def _drain_statuses(self, store: JobStore) -> None:
        """Publish every verdict the queue is still holding for the commit.

        The same path the reclaim timer uses, so two drains cannot contradict each
        other. It used to read the row, post, and then clear the row, which let a
        verdict written during the post be erased by a drain that never sent it,
        and let a post land after a newer one for the same run.

        A run released in an earlier request whose status could not be published
        is retried here, so a commit that never resolved does not depend on
        another lease lapsing.
        """
        drain_owed(store, self._post_verdict, self._close_check)

    def _close_check(self, job: Job) -> None:
        """Close the check a drained verdict belongs to, outside the lock.

        A check run is two requests with a 30 second timeout, and the lock it
        would otherwise be inside is the one every claim and every report waits
        for. The commit status is the record; the check is the view of it.
        """
        published = publish_check_run_quietly(
            self.server.github, job, target_url=self.server.job_target_url(job.id)
        )
        if not published and self.server.github is not None:
            raise GitHubAPIError("the check run was refused")

    def _post_verdict(self, job: Job, state: str, description: str) -> None:
        """Send one owed verdict to the commit."""
        failure = self._post_status(job, (state, description))
        # The check is published from the verdict that was drained, not from the
        # one being retired, so a run that was reclaimed and re-run shows its real
        # outcome rather than the loss the queue was still holding.
        #
        # Either failure raises, which is what puts the verdict back in the row
        # for the next drain. Retiring the row regardless would make a refused
        # check permanent, and the check is the feature; leaving it regardless
        # would wedge the claim endpoint for every device, since the drain runs
        # before store.claim. So one duplicate verdict beats one check that never
        # appears.
        if failure is not None:
            raise failure

    def _post_status(self, job: Job, status: tuple[str, str]) -> GitHubAPIError | None:
        """Post one run's commit status, returning the failure instead of raising.

        A claim that cannot post still has to answer the agent, so the caller
        that needs the failure to stop reads it back, and the one that does not
        ignores it: the loss is already recorded in the queue either way.
        """
        if self.server.github is None:
            return None
        state, description = status
        try:
            self.server.github.post_status(
                job.repository,
                head_sha_for(job.sha, job.status_sha),
                state,
                description,
                context=self.server.status_context,
                target_url=self.server.job_target_url(job.id),
                # the same ceiling the reclaim timer uses, because this runs
                # inside the lock that guards the ordering and a device is
                # waiting on the claim that has not been answered yet
                timeout=POST_TIMEOUT_SECONDS,
            )
        except GitHubAPIError as exc:
            return exc
        return None

    def _renew(self, store: JobStore, job_id: str) -> None:
        payload = self._read_json()  # type: ignore[attr-defined]
        device = str(payload["device"])
        lease = int(payload.get("lease_seconds", self.server.lease_seconds))
        if not self._agent_authorized_as(device):  # type: ignore[attr-defined]
            self._send(401, {"error": "this token does not speak for that device"})  # type: ignore[attr-defined]
            return
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
        error = str(payload.get("error", "") or "").strip()
        # read the value rather than testing for the key, so an explicit null is
        # refused with the same message instead of reaching int(None)
        reported = payload.get("exit_code")
        if not error and reported is None:
            # a report with neither is a device that stopped mid-handshake, and
            # guessing a verdict for it is the red commit this path exists to stop
            raise ValueError("a report needs an exit code or a device error")
        exit_code = None if error else int(reported)
        log = str(payload.get("log", "") or "")
        # validated once, so the status and the stored record agree on what
        # the device actually reported
        steps = encode_steps(payload.get("steps"))
        if not self._agent_authorized_as(device):  # type: ignore[attr-defined]
            # the forged-verdict path: naming the device that holds the lease was
            # enough to post its exit code, which writes the commit status
            self._send(401, {"error": "this token does not speak for that device"})  # type: ignore[attr-defined]
            return
        if not store.holds_lease(job_id, device):
            self._send(409, {"error": "job is not leased to this device"})  # type: ignore[attr-defined]
            return
        if error:
            status = lost_description(job, error)
        elif exit_code == 0:
            status = ("success", "device pool run passed")
        else:
            status = ("failure", failure_description(exit_code, log, steps))
        # The row settles first and the post goes out after it, both inside the
        # lock that guards the ordering. Posted first, the reclaim could release
        # the run in between: the report would be refused as a lapsed lease, the
        # request would answer 409, and the work would run again on a commit the
        # status had already called passed. Settling first also supersedes
        # anything the queue still owes for the run, so the drain does not
        # republish the loss of the earlier lease afterwards.
        with store.status_lock:
            try:
                job = store.report(job_id, device, exit_code, log=log, error=error, steps=steps)
            except ValueError as exc:
                self._send(409, {"error": str(exc)})  # type: ignore[attr-defined]
                return
            failure = self._post_status(job, status) if self.server.github is not None else None
            if failure is not None:
                # The verdict is recorded, so the run is not lost; the status is
                # owed rather than absent, and the next drain sends it. The job
                # is terminal, so a device cannot retry the report.
                store.owe_status(job_id, json.dumps(list(status)))
        posted = self.server.github is not None
        # after the row settled, so the check closes on the recorded verdict
        # rather than on the pre-record job, which still reads as in progress
        publish_check_run_quietly(self.server.github, job, target_url=self.server.job_target_url(job.id))
        # the device uploaded its archive before this report, so the payload the
        # verdict returns already names what a reader can download
        self._send(200, {**self._with_artifacts(job), "status_posted": posted})  # type: ignore[attr-defined]

    def _job_status(self, store: JobStore, job_id: str) -> None:
        job = store.get(job_id)
        if job is None:
            self._send(404, {"error": "unknown job"})  # type: ignore[attr-defined]
            return
        self._send(200, self._with_artifacts(job))  # type: ignore[attr-defined]

    def _job_log(self, store: JobStore, job_id: str) -> None:
        """Serve the log tail the agent reported, for the view a status links to.

        The stored log is already bounded by the report limit, and the response
        carries no CORS header, so a page a browser reaches cannot read a run.
        """
        job = store.get(job_id)
        if job is None:
            self._send(404, {"error": "unknown job"})  # type: ignore[attr-defined]
            return
        log_text = job.log if job.log is not None else ""
        self._send_text(200, log_text, content_type="text/plain; charset=utf-8")  # type: ignore[attr-defined]
