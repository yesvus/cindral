"""HTTP surface for build artifacts: one run's upload, and its download.

A run's artifact is repository-written bytes, and reading it back is serving
bytes the repository chose. It is read through the same read-only credential
as the run it belongs to, so a panel that can already read a run's log can
download what the run produced and nothing else, and the reply is an
attachment with a fixed content type rather than something a browser will
render. Writing one takes the agent token, because it is a mutation, and a
device that does not hold the run's lease cannot attach bytes to it.
"""
import os
import sqlite3
from pathlib import Path
import threading
from typing import Any

from .artifact import (
    ARTIFACT_REPOSITORY_QUOTA,
    ARTIFACT_STORAGE_QUOTA,
    ARTIFACT_TTL_SECONDS,
    ArtifactError,
    ArtifactStore,
    MAX_ARTIFACT_BYTES,
)

_LOCK = threading.Lock()


def attach_artifacts(server: Any, getenv: Any = os.environ.get) -> ArtifactStore | None:
    """The store the environment names for a server that has none attached.

    Wiring it at startup reads better than building it per request, so a server
    that carries the attribute at all keeps whatever it was given, ``None``
    included. What is left is the default: artifacts live beside the jobs
    database unless ``CINDRAL_ARTIFACT_DIR`` says otherwise, and a broker with
    no jobs database has no runs to carry output for.
    """
    if hasattr(server, "artifacts"):
        return server.artifacts
    jobs_db = getenv("CINDRAL_JOBS_DB")
    if not jobs_db:
        return None
    with _LOCK:
        if not hasattr(server, "artifacts"):
            server.artifacts = ArtifactStore(
                getenv("CINDRAL_ARTIFACT_DIR") or str(Path(jobs_db).parent / "artifacts"),
                ttl_seconds=int(getenv("CINDRAL_ARTIFACT_TTL_SECONDS") or ARTIFACT_TTL_SECONDS),
                repository_quota=int(
                    getenv("CINDRAL_ARTIFACT_REPOSITORY_QUOTA") or ARTIFACT_REPOSITORY_QUOTA
                ),
                storage_quota=int(getenv("CINDRAL_ARTIFACT_STORAGE_QUOTA") or ARTIFACT_STORAGE_QUOTA),
                max_bytes=int(getenv("CINDRAL_ARTIFACT_MAX_BYTES") or MAX_ARTIFACT_BYTES),
            )
    return server.artifacts


def discard_artifact(
    server: Any, job_id: str, digest: str | None = None, created_at: float | None = None
) -> None:
    """Drop a run's archive, if it has one, because the run is about to rerun.

    A reclaimed run is a *new* execution of the same commit, not a second look at
    the first one's output. Leaving the old archive in place would serve the first
    attempt's bytes under the second attempt's verdict, and tell the second device
    its upload was accepted when it was not.

    A module function rather than a handler method because the reclaim timer runs
    it on a schedule with no request in hand, and a version reachable only from
    the claim endpoint is a version that runs when a device happens to ask for
    work rather than when a lease actually lapses.

    A reclaim names neither a digest nor a time, because whatever is stored belongs
    to the attempt that lost its lease. The upload path names both, because by the
    time it gets here the run may have been requeued and re-uploaded by a second
    device that has already been answered 201, and that archive is not this
    request's to delete. The time is as load-bearing as the digest: the packer is
    byte-deterministic, so a retry of the same commit produces identical bytes and
    the digest alone would match the second attempt's row.
    """
    store = attach_artifacts(server)
    if store is None:
        return
    try:
        store.discard(job_id, digest, created_at)
    except (ArtifactError, OSError, sqlite3.Error):
        # A reclaim must not fail because an archive could not be dropped. The
        # next attempt's upload takes the path, so a leftover is bounded by the
        # quota and the TTL rather than by anything this can promise.
        pass


class ArtifactApiMixin:
    """Handlers for ``/v1/jobs/{id}/artifacts``.

    The route is the job's own, so the run's identity and its archive are read
    and written together: the artifact is a field of the run, and the payload
    that reports it is built here rather than in the store that keeps the file.
    """

    server: Any

    def _discard_artifact(self, job_id: str) -> None:
        # no digest: whatever is stored belongs to the attempt that lost its lease
        discard_artifact(self.server, job_id)

    def _artifact_store(self) -> ArtifactStore | None:
        """The store artifacts are kept in, or None when none is configured.

        A broker that carries no store has no artifact to list, so a job
        payload reports none and both routes answer 503 rather than pretending
        a run produced nothing.
        """
        return attach_artifacts(self.server)

    def _with_artifacts(self, job: Any) -> dict:
        """One job's payload with the archive a reader can download, or null.

        ``null`` rather than an empty object, so a panel can render "this run
        produced nothing" without inspecting a shape that is otherwise the
        same as an artifact it could not read.
        """
        store = self._artifact_store()
        record = None
        if store is not None:
            try:
                record = store.get(job.id)
            except ArtifactError:
                record = None
        return {**job.as_dict(), "artifacts": record.as_dict() if record else None}

    def _artifact_upload(self, store: Any, job_id: str) -> None:
        """Store the archive a device sent for a run it is holding."""
        artifacts = self._artifact_store()
        if artifacts is None:
            self._send(503, {"error": "artifact storage is not configured"})  # type: ignore[attr-defined]
            return
        job = store.get(job_id)
        if job is None:
            self._send(404, {"error": "unknown job"})  # type: ignore[attr-defined]
            return
        device = self.headers.get("X-Cindral-Device", "")  # type: ignore[attr-defined]
        if not self._agent_authorized_as(device):  # type: ignore[attr-defined]
            self._send(401, {"error": "this token does not speak for that device"})  # type: ignore[attr-defined]
            return
        if not store.holds_lease(job_id, device):
            self._send(409, {"error": "job is not leased to this device"})  # type: ignore[attr-defined]
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))  # type: ignore[attr-defined]
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        # refused before a byte is read: the cap is what keeps an archive that
        # would fill the broker's disk from ever reaching one
        if length < 0 or length > artifacts.max_bytes:
            self._send(413, {"error": "artifact archive exceeds the configured size limit"})  # type: ignore[attr-defined]
            return
        try:
            record, created = artifacts.put(job_id, job.repository, self.rfile, length)  # type: ignore[attr-defined]
        except ArtifactError as exc:
            self._send(400, {"error": str(exc)})  # type: ignore[attr-defined]
            return
        if not store.holds_lease(job_id, device):
            # The lease is checked before the body is read as well, but reading it
            # is the slow part and it is the sender who decides how slow. A reclaim
            # landing inside that window, on this broker or the timer in another,
            # discards what the run had and requeues it, and storing the bytes
            # afterwards puts the first attempt's archive back under a verdict that
            # no longer belongs to it. The run is executing again elsewhere, so
            # these bytes are the ones that must not stand.
            #
            # Only when this call is the one that published. A run's archive is
            # immutable, so a second upload of the same run is answered with the
            # row already standing and `created` is false, and that row belongs to
            # whichever attempt stored it. In that ordering the reclaim and a whole
            # second upload can land while this body is still arriving, so the row
            # is the second device's, it has already been answered 201, and
            # dropping it here would delete an archive its device believes it
            # stored and will never send again. There is nothing of this request's
            # own to clean up in that case, because it published nothing.
            if created:
                discard_artifact(self.server, job_id, record.digest, record.created_at)
            self._send(409, {"error": "job is not leased to this device"})  # type: ignore[attr-defined]
            return
        if created and artifacts.get(job_id) is None:
            # a quota smaller than this archive evicted the row it had just
            # written, so answering stored would name an artifact no reader
            # can fetch
            self._send(507, {"error": "artifact archive is larger than the configured storage quota"})  # type: ignore[attr-defined]
            return
        self._send(  # type: ignore[attr-defined]
            201 if created else 200, {"stored": True, "artifact": record.as_dict()}
        )

    def _artifact_download(self, store: Any, job_id: str) -> None:
        """Serve one run's archive as a download.

        The reply carries no CORS header, as the run log does not, so a page a
        browser reaches cannot read a run's output through the browser. The
        name is the one the store derived from the job id rather than anything
        a step wrote, and the content type is fixed with sniffing refused, so
        the bytes a run produced are downloaded rather than rendered.
        """
        artifacts = self._artifact_store()
        if artifacts is None:
            self._send(503, {"error": "artifact storage is not configured"})  # type: ignore[attr-defined]
            return
        try:
            record = artifacts.get(job_id)
        except ArtifactError as exc:
            self._send(400, {"error": str(exc)})  # type: ignore[attr-defined]
            return
        # the archive is served only for a run that is still there and still
        # this repository's, so bytes outliving the run that produced them, or
        # an id that names a different repository's run, are not readable
        job = store.get(job_id)
        if record is None or job is None or record.repository != job.repository:
            self._send(404, {"error": "job has no artifact"})  # type: ignore[attr-defined]
            return
        try:
            blob, size, record = artifacts.open(job_id)
        except FileNotFoundError:
            self._send(404, {"error": "job has no artifact"})  # type: ignore[attr-defined]
            return
        with blob:
            self.send_response(200)  # type: ignore[attr-defined]
            self.send_header("Content-Type", "application/x-tar")  # type: ignore[attr-defined]
            self.send_header("Content-Length", str(size))  # type: ignore[attr-defined]
            self.send_header("Content-Disposition", f'attachment; filename="{record.name}"')  # type: ignore[attr-defined]
            self.send_header("X-Content-Type-Options", "nosniff")  # type: ignore[attr-defined]
            self.send_header("ETag", f'"sha256:{record.digest}"')  # type: ignore[attr-defined]
            self.end_headers()  # type: ignore[attr-defined]
            while block := blob.read(1024 * 1024):
                self.wfile.write(block)  # type: ignore[attr-defined]
