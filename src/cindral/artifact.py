"""Build artifacts: the archive one run produces, and where the broker keeps it.

A cache entry is content-keyed and disposable: two runs producing the same bytes
share one, and losing it costs a rebuild. An artifact belongs to the run that
produced it, is written once, and is read by whoever the run was for, so it is
stored under its own job and never deduplicated, which is why this is a separate
store rather than a cache key.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import BinaryIO

from .artifact_staging import Staging, stage, sweep_staging
from .sqlite_store import open_database

# A run's output is read soon after the run. The cache TTL is longer because a
# cache entry is worth keeping until something uses it, while an artifact past
# its week is a disk cost nothing is reading. The quotas keep a chatty
# repository evicting its own oldest runs rather than everyone else's, and the
# largest single archive is one the device refuses to pack rather than build.
ARTIFACT_TTL_SECONDS = 7 * 24 * 60 * 60
ARTIFACT_REPOSITORY_QUOTA = 256 * 1024 * 1024
ARTIFACT_STORAGE_QUOTA = 768 * 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_ARTIFACT_FILES = 20_000
# Where a step writes what it wants kept, inside the checkout it runs in. The
# root is the one name every archive member carries, so nothing a step wrote
# names a path outside it. The suffix is what the download is called.
ARTIFACT_DIRECTORY = ".cindral/artifacts"
#: the route the archive is served and received on, in one place. It was a
#: constant with no caller while three modules spelled the path out by hand, so
#: a change to the route would have moved two of the three.
ARTIFACT_PATH = "/v1/jobs/{job_id}/artifacts"
ARTIFACT_ROOT = "artifacts"
ARTIFACT_SUFFIX = ".tar"
# A job id is a uuid, and the download path is the only place a caller supplies
# one, so the character set is what makes it a name rather than a path
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    job_id TEXT PRIMARY KEY, repository TEXT NOT NULL, name TEXT NOT NULL,
    size INTEGER NOT NULL, digest TEXT NOT NULL,
    created_at REAL NOT NULL, expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS artifacts_repository ON artifacts(repository, created_at);
CREATE INDEX IF NOT EXISTS artifacts_expiry ON artifacts(expires_at);
"""


class ArtifactError(ValueError):
    pass


@dataclass(frozen=True)
class Artifact:
    job_id: str
    repository: str
    name: str
    size: int
    digest: str
    created_at: float
    expires_at: float

    def as_dict(self) -> dict[str, str | int | float]:
        # the url is broker-relative, so a panel reads its base URL once
        return {
            "name": self.name, "size": self.size, "digest": self.digest,
            "created_at": self.created_at, "expires_at": self.expires_at,
            "url": ARTIFACT_PATH.format(job_id=self.job_id),
        }


def _unlink_all(paths: list[Path]) -> None:
    """Remove files whose rows are already gone, once the commit has returned.

    A delete that has not committed is a delete that may not happen, and the file
    it named has to outlive the possibility. Unlinking inside the transaction is
    how a failed commit somewhere else destroyed a stored artifact that had
    nothing to do with it.
    """
    for path in paths:
        path.unlink(missing_ok=True)


class ArtifactStore:
    """One immutable archive per job, kept until its TTL or a quota evicts it."""

    def __init__(
        self, directory: str | Path, ttl_seconds: int = ARTIFACT_TTL_SECONDS,
        repository_quota: int = ARTIFACT_REPOSITORY_QUOTA,
        storage_quota: int = ARTIFACT_STORAGE_QUOTA, max_bytes: int = MAX_ARTIFACT_BYTES,
    ) -> None:
        if min(ttl_seconds, repository_quota, storage_quota, max_bytes) < 1:
            raise ValueError("artifact limits must be positive")
        self.directory = Path(directory)
        self.archives = self.directory / "archives"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.archives.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / "artifacts.db"
        self.ttl_seconds = ttl_seconds
        self.repository_quota = repository_quota
        self.storage_quota = storage_quota
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        return open_database(self.database, timeout=30)

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _init(self) -> None:
        with self._connection() as connection:
            connection.executescript(SCHEMA)

    def archive_path(self, job_id: str) -> Path:
        """Where one job's archive lives, refusing an id that is not one.

        The id arrives in a URL, so it is the one name here a caller picks. A
        validated id cannot spell a parent or a sibling, and the resolved path
        is checked against the archive root too, so neither this call nor a
        later relaxation of the pattern reaches a file outside the directory.
        """
        if not _JOB_ID.fullmatch(job_id):
            raise ArtifactError("invalid job id")
        path = self.archives / f"{job_id}{ARTIFACT_SUFFIX}"
        if not path.resolve().is_relative_to(self.archives.resolve()):
            raise ArtifactError("artifact path escapes the archive directory")
        return path

    def put(
        self, job_id: str, repository: str, source: BinaryIO, content_length: int, now: float | None = None
    ) -> tuple[Artifact, bool]:
        """Store one run's archive, or answer with the one already stored.

        A run's artifact is immutable while the run is on it, so a second upload
        is not an overwrite: the first archive stands, which is what a device that
        re-sends after a lost reply needs. The reclaim is the one thing that
        breaks that, and it is deliberate, because a requeued run is a new
        execution rather than a second look at the last one's output.
        """
        path = self.archive_path(job_id)
        if content_length < 0 or content_length > self.max_bytes:
            raise ArtifactError("artifact archive exceeds the configured size limit")
        with Staging(self.archives) as staged:
            published = False
            try:
                # The bytes are read with no lock held. That read is a socket and
                # its duration is the sender's to choose, so holding the store's
                # lock across it stops every artifact read in the broker for as
                # long as one upload takes: one device trickling a 64 MiB body
                # stalls the panel's job page for the whole broker. The staging
                # name is unique per upload, so nothing needs the lock to write
                # them; only the transaction and the rename do.
                written, digest = stage(staged, source, content_length)
                unlink: list[Path] = []
                with self._lock:
                    # Sampled here, under the lock, rather than where the request
                    # arrived. The read above is a socket and its duration is the
                    # sender's to choose, so a time taken before it does not order
                    # anything: two puts can begin in one order and publish in the
                    # other, and then the earlier value is on the later row. Taken
                    # under the lock it is ordered by publication, which is what the
                    # discard's compare-and-delete needs from it. A side effect is
                    # that the TTL runs from publication rather than from arrival,
                    # which is the more correct of the two.
                    current = time.time() if now is None else now
                    record = Artifact(
                        job_id, repository, path.name, written, digest, current, current + self.ttl_seconds
                    )
                    with self._connection() as connection:
                        connection.execute("BEGIN IMMEDIATE")
                        self._expire(connection, current, unlink)
                        existing = connection.execute("SELECT * FROM artifacts WHERE job_id=?", (job_id,)).fetchone()
                        if existing is None:
                            connection.execute(
                                "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (job_id, repository, record.name, record.size, record.digest, current, record.expires_at),
                            )
                            # before the commit, so a reader can never see the
                            # row without the file, and the lock means no other
                            # write renamed over this path in between
                            os.replace(staged, path)
                            published = True
                            self._enforce_quota(connection, repository, unlink)
                            connection.execute("COMMIT")
                            _unlink_all(unlink)
                            return record, True
                        connection.execute("COMMIT")
                        _unlink_all(unlink)
                    return self._record(existing), False
            except BaseException:
                # Only ever an archive this call published, and only while no
                # row names it: the rename is reached solely where the run had
                # none, so a stored run returns the record it has. The TTL and
                # both quotas are counted from the row, so what a rolled-back
                # commit leaves is disk nothing accounts for.
                if published:
                    # Under the lock, because the check and the unlink are one
                    # decision and every other file operation in this store takes
                    # it. Without it a second put for the same run can publish
                    # between the check and the unlink, and this unlink then takes
                    # *its* file: a device answered 201 for an archive that no row
                    # names and no reader will find.
                    with self._lock:
                        if not self._claimed(job_id):
                            path.unlink(missing_ok=True)
                raise

    def get(self, job_id: str, now: float | None = None) -> Artifact | None:
        """One job's artifact, or None when it was never stored or has expired."""
        path = self.archive_path(job_id)
        current = time.time() if now is None else now
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            unlink: list[Path] = []
            self._expire(connection, current, unlink)
            row = connection.execute("SELECT * FROM artifacts WHERE job_id=?", (job_id,)).fetchone()
            connection.execute("COMMIT")
            _unlink_all(unlink)
        if row is None or not path.is_file():
            # a row with no archive is a row nothing can serve, so it goes. Scoped
            # to the row this call read, because the file check is outside the lock
            # and the row can be replaced in between: a reclaim and a second
            # device's upload landing in that window leaves the standing row
            # belonging to the attempt now running the commit, which was already
            # answered 201 and will never send its archive again.
            if row is not None:
                self.discard(job_id, str(row["digest"]), float(row["created_at"]))
            return None
        return self._record(row)

    def open(self, job_id: str, now: float | None = None) -> tuple[BinaryIO, int, Artifact]:
        record = self.get(job_id, now)
        if record is None:
            raise FileNotFoundError(job_id)
        blob = self.archive_path(job_id).open("rb")
        return blob, os.fstat(blob.fileno()).st_size, record

    def discard(self, job_id: str, digest: str | None = None, created_at: float | None = None) -> None:
        """Drop a run's archive, or only the one whose digest is named.

        A digest and a publication time make the drop a compare-and-delete, so it
        removes this request's row and nothing else. Both are needed, and the
        digest alone is not enough: the packer builds each tar by hand with no
        timestamps, owners or ordering that varies, so two attempts of the same
        commit on the same device produce byte-identical archives. Identical bytes
        are the expected case across a retry, not a corner one, and scoping on the
        digest alone deletes the second attempt's archive along with the first's.

        The time is a discriminator, not an identity, and it is worth being precise
        about that. A double at epoch scale has an ULP of about 238 nanoseconds, and
        two unsynchronised reads agreeing is an ordinary event on a loaded box, so
        this is improbable rather than impossible. What makes it improbable enough is
        that it also needs the reclaim's unconditional drop *and* a second
        publication inside the sub-millisecond span between this put's commit and its
        compare-and-delete, which is two transactions wide. `put` samples the time
        under the store lock so that value orders by publication, which is what
        makes "the second publisher's time is later" true rather than assumed.

        Without a digest the drop is unconditional, which is what the reclaim
        wants: the run is about to execute again, so whatever is stored belongs to
        the attempt that lost its lease.

        A caller must not pass the digest of a row it merely read. An upload that
        is refused because its lease went is answered with whatever row is
        standing, which after a reclaim belongs to the attempt now running the
        commit, and that attempt was already told its upload succeeded. Deleting it
        is not a case of serving the wrong bytes; the harm is that the artifact is
        absent while a device believes it stored one and will never send it again.
        So the upload path drops only when it published, and passes its own digest
        and its own publication time.
        """
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            removed = self._drop(connection, job_id, digest, created_at)
            connection.execute("COMMIT")
            if removed is not None:
                removed.unlink(missing_ok=True)

    def _claimed(self, job_id: str) -> bool:
        """Whether a committed row names this run's archive, read fresh.

        A commit can land and still raise on its way out, so a flag this call
        set is not the answer to whether the row survived. Reading the row is.
        """
        with self._connection() as connection:
            return connection.execute("SELECT 1 FROM artifacts WHERE job_id=?", (job_id,)).fetchone() is not None

    def _drop(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        digest: str | None = None,
        created_at: float | None = None,
    ) -> Path | None:
        """Remove a row and the file behind it in the same transaction.

        Deleting the row alone would leave the archive on disk with nothing
        naming it, and a store that never reclaims those files fills whatever
        the TTL and quotas said it would not.

        The file is removed only when a row was actually removed. The archive is
        named by job id, so an unlink under a delete that matched nothing takes
        whatever attempt is currently standing, and the row that named it is
        exactly what a digest-scoped delete declined to touch.

        It returns the path and does **not** unlink, because the delete is not
        committed yet when this runs. Unlinking here means a later commit failure
        restores the row and not the file, and a row naming a file that is not there
        is discarded by the next reader, taking a stored artifact with it because
        some other upload failed. The caller unlinks after its own COMMIT returns.
        """
        if digest is None:
            removed = connection.execute("DELETE FROM artifacts WHERE job_id=?", (job_id,))
        elif created_at is None:
            removed = connection.execute(
                "DELETE FROM artifacts WHERE job_id=? AND digest=?", (job_id, digest)
            )
        else:
            removed = connection.execute(
                "DELETE FROM artifacts WHERE job_id=? AND digest=? AND created_at=?",
                (job_id, digest, created_at),
            )
        if not removed.rowcount:
            return None
        return self.archives / f"{job_id}{ARTIFACT_SUFFIX}"

    def _expire(self, connection: sqlite3.Connection, now: float, unlink: list[Path]) -> None:
        for row in connection.execute("SELECT job_id FROM artifacts WHERE expires_at <= ?", (now,)).fetchall():
            removed = self._drop(connection, str(row["job_id"]))
            if removed is not None:
                unlink.append(removed)
        sweep_staging(self.archives)

    def _enforce_quota(self, connection: sqlite3.Connection, repository: str, unlink: list[Path]) -> None:
        for scope, quota in ((repository, self.repository_quota), (None, self.storage_quota)):
            self._evict_to_quota(connection, scope, quota, unlink)

    def _evict_to_quota(
        self, connection: sqlite3.Connection, repository: str | None, quota: int, unlink: list[Path]
    ) -> None:
        """Drop the oldest artifacts until the scope fits.

        An artifact is read once by whoever the run was for, so age is what
        order to evict in; a cache entry is reused, which is why it evicts on
        access instead.
        """
        where, parameters = (" WHERE repository=?", (repository,)) if repository is not None else ("", ())
        while True:
            total = connection.execute(
                f"SELECT coalesce(sum(size), 0) AS bytes FROM artifacts{where}", parameters
            ).fetchone()["bytes"]
            if int(total) <= quota:
                return
            oldest = connection.execute(
                f"SELECT job_id FROM artifacts{where} ORDER BY created_at, job_id LIMIT 1", parameters
            ).fetchone()
            if oldest is None:
                return
            removed = self._drop(connection, str(oldest["job_id"]))
            if removed is not None:
                unlink.append(removed)

    @staticmethod
    def _record(row: sqlite3.Row) -> Artifact:
        return Artifact(
            job_id=row["job_id"], repository=row["repository"], name=row["name"],
            size=int(row["size"]), digest=row["digest"],
            created_at=float(row["created_at"]), expires_at=float(row["expires_at"]),
        )
