"""Tests for the broker store that keeps a run's artifact."""
import io
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from cindral.artifact_staging import STAGING_SUFFIX, Staging as ArtifactStaging, StagingError
from cindral.artifact import (
    ARTIFACT_SUFFIX,
    ArtifactError,
    ArtifactStore,
)

JOB = "a" * 32
OTHER = "b" * 32


class CountingBytes(io.BytesIO):
    """A body that reports how much of itself the broker actually read."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.read_bytes = 0

    def read(self, size: int = -1) -> bytes:
        block = super().read(size)
        self.read_bytes += len(block)
        return block


class GreedyBytes(io.BytesIO):
    """A body that hands over more than it was asked for, as a hostile one can."""

    def read(self, size: int = -1) -> bytes:
        return super().read(size * 4)


class TricklingBytes(io.BytesIO):
    """A body that stops mid-transfer until it is let go, as a slow one can.

    Nothing is wrong with the bytes. The point is the duration: how long an
    upload takes is the sender's choice, not the store's, so the store must not
    be holding anything a reader needs while it waits.

    `mid_body` is set on the read that blocks, so a reader can be timed from the
    moment the uploader is provably inside the body rather than from whenever it
    happened to be scheduled. Timing from an unproven starting point is what made
    this test pass with the lock held across the read most of the time.
    """

    def __init__(self, payload: bytes, release: threading.Event, mid_body: threading.Event):
        super().__init__(payload)
        self._release = release
        self._mid_body = mid_body
        self.reads = 0

    def read(self, size: int = -1) -> bytes:
        self.reads += 1
        if self.reads > 1:
            self._mid_body.set()
            self._release.wait(10)
        return super().read(size)


class ArtifactStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ArtifactStore(self.root / "artifacts", ttl_seconds=100)

    def put(self, job_id=JOB, value=b"archive-bytes", repository="owner/repo", now=10.0, store=None):
        return (store or self.store).put(job_id, repository, io.BytesIO(value), len(value), now=now)

    def test_a_discarded_run_takes_the_archive_of_the_attempt_after_it(self) -> None:
        """A reclaim is a new execution, so the first archive has to stop standing.

        A run's archive is immutable: a second upload of the same run is answered
        with the one already stored, which is what makes a device that re-sends
        after a lost reply safe. That immutability is exactly wrong across a
        reclaim, because the row keeps its id and the run is executing again on
        another device. Without the discard the second device is told its upload
        was accepted and a reader is handed the first attempt's bytes under the
        second attempt's verdict.
        """
        first, created = self.put(value=b"from attempt one")
        self.assertTrue(created)
        again, created = self.put(value=b"ignored")
        self.assertFalse(created, "the first upload was not the stored one")
        self.assertEqual(again.digest, first.digest)

        self.store.discard(JOB)

        second, created = self.put(value=b"from attempt two")
        self.assertTrue(created, "the second attempt's upload was refused as a duplicate")
        self.assertEqual(self.store.get(JOB, now=11.0).digest, second.digest)
        self.assertNotEqual(second.digest, first.digest)

    def test_a_slow_upload_does_not_hold_up_a_read(self) -> None:
        """One device trickling a body must not stall the broker's reads.

        The bytes are read from a socket, and how long that takes is the sender's
        to decide. With the store's lock held across it, a read that every job
        page and every report response performs waits for the slowest upload in
        the system: one device on a bad connection froze the panel for the whole
        broker. The staging name is unique per upload, so nothing needs the lock
        to write the bytes, and only the transaction needs it afterwards.

        The read is timed from the uploader's own signal that it is inside the
        body. Started at an unproven moment instead, the race is won by the
        reader most of the time and the test passes with the lock held across the
        read, which is the regression it exists to catch.
        """
        release = threading.Event()
        self.addCleanup(release.set)
        mid_body = threading.Event()
        payload = b"x" * (1024 * 1024 + 8)  # two reads, so the second one blocks
        source = TricklingBytes(payload, release, mid_body)

        def upload() -> None:
            try:
                self.store.put("a" * 32, "owner/repo", source, len(payload), now=10.0)
            except Exception:  # noqa: BLE001 - the assertion is about the read
                pass

        uploading = threading.Thread(target=upload, daemon=True)
        uploading.start()
        try:
            self.assertTrue(mid_body.wait(10), "the upload never reached the body")
            # a read of a different run, which is what a job page does, and it
            # has to answer while the upload is still mid-body. The elapsed time
            # is the assertion: a read that waits for the upload is the bug, and
            # only a measurement can tell the two apart.
            started = time.monotonic()
            self.assertIsNone(self.store.get("b" * 32, now=11.0))
            elapsed = time.monotonic() - started
        finally:
            release.set()
            uploading.join(timeout=10)
        self.assertLess(elapsed, 2.0, f"a read waited {elapsed:.1f}s for an upload")
        self.assertFalse(uploading.is_alive(), "the upload never finished")

    def test_a_rollback_does_not_take_a_concurrent_publishs_file(self) -> None:
        """A rolled-back publish must not delete the file a second one just wrote.

        The rollback's check and its unlink are one decision, so they belong under
        the store lock like every other file operation here. Without it a second
        put for the same run can publish in between, and the rollback then removes
        *its* file: a device answered 201 for an archive that no row names and no
        reader will find, which is the same harm as a lease-lost upload taking a
        later attempt's archive.

        Two seams arrange it, both internal to the store, and the first put is made
        to fail after its rename so the rollback runs at all. The pause is only long
        enough for a concurrent publish, and with the lock taken no amount of waiting
        lets one through, so this is not a timing coin-flip: the unfixed code has the
        window and the fix turns the second put into a waiter.

        I previously wrote that no test was possible here because the ordering stops
        being arrangeable. That was wrong, and it was the kind of wrong that would
        have left a real hole untested while sounding principled.
        """
        paused = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        real_claimed = self.store._claimed

        def pausing_claimed(job_id: str) -> bool:
            answer = real_claimed(job_id)
            paused.set()
            release.wait(10)
            return answer

        def failing_put() -> None:
            try:
                with unittest.mock.patch.object(
                    self.store, "_enforce_quota", side_effect=RuntimeError("quota")
                ), unittest.mock.patch.object(self.store, "_claimed", pausing_claimed):
                    self.put(value=b"the archive that fails to commit")
            except RuntimeError:
                pass

        first = threading.Thread(target=failing_put, daemon=True)
        first.start()
        self.assertTrue(paused.wait(10), "the rollback never reached its check")

        def second_put() -> None:
            self.put(value=b"the archive that succeeds")

        other = threading.Thread(target=second_put, daemon=True)
        other.start()
        # every chance to publish inside the pause, which the lock forbids
        time.sleep(0.2)
        release.set()
        first.join(timeout=10)
        other.join(timeout=10)

        self.assertFalse(other.is_alive(), "the second publish never completed")
        self.assertTrue(
            (self.store.archives / f"{JOB}.tar").exists(),
            "the rollback took the file a concurrent publish had just stored",
        )
        self.assertIsNotNone(self.store.get(JOB, now=30.0))

    def test_a_failed_commit_elsewhere_leaves_a_stored_archive_alone(self) -> None:
        """Quota eviction must not unlink a file whose delete is not committed.

        `_enforce_quota` evicts by deleting a row and unlinking the file behind it,
        and it runs inside `put`'s own transaction. A commit that then fails
        restores the row and not the file, so a row naming a file that is not there
        is discarded by the next reader: an upload of a *different* job destroyed a
        stored artifact, with no error on either job.
        """
        other = "c" * 32
        store = ArtifactStore(
            self.root / "quota", ttl_seconds=1000, repository_quota=10, storage_quota=1000
        )
        store.put(JOB, "owner/repo", io.BytesIO(b"FIRST-7"), 7, now=10)
        self.assertIsNotNone(store.get(JOB, now=11))

        class RefusesCommit:
            """Raises on COMMIT before it runs, so the transaction rolls back."""

            def __init__(self, connection):
                self._connection = connection

            def execute(self, statement, *args):
                if statement == "COMMIT":
                    raise sqlite3.OperationalError("disk I/O error before commit")
                return self._connection.execute(statement, *args)

            def __getattr__(self, name):
                return getattr(self._connection, name)

            def close(self):
                self._connection.close()

        real_connect = store._connect
        with unittest.mock.patch.object(
            store, "_connect", lambda: RefusesCommit(real_connect())
        ):
            with self.assertRaises(sqlite3.OperationalError):
                # 8 bytes on top of 7 exceeds the 10 byte repository quota, so this
                # evicts JOB and then fails to commit
                store.put(other, "owner/repo", io.BytesIO(b"SECOND-8"), 8, now=20)

        self.assertIsNotNone(
            store.get(JOB, now=21),
            "an unrelated failed upload destroyed a stored artifact",
        )

    def test_get_does_not_discard_the_archive_that_replaced_its_row(self) -> None:
        """`get` discards a row whose file is gone, and must name the row it read.

        The file check is outside the store lock, so a reclaim and a second
        device's upload can land between reading the row and discarding it, leaving
        the standing row belonging to the attempt already answered 201. The window
        is arranged on the check itself, so the interleaving is deterministic
        rather than something a thread has to win.
        """
        self.put(value=b"the first attempt")
        replaced: dict[str, object] = {}
        real_is_file = Path.is_file

        def replaced_before_the_check(self_path):
            if not replaced:
                self.store.discard(JOB)
                replaced["record"], replaced["created"] = self.put(
                    value=b"the second attempt"
                )
                return False
            return real_is_file(self_path)

        with unittest.mock.patch.object(Path, "is_file", replaced_before_the_check):
            self.assertIsNone(self.store.get(JOB, now=11.0))

        self.assertTrue(replaced["created"], "the replacement was not a fresh publication")
        self.assertIsNotNone(
            self.store.get(JOB, now=21.0),
            "get's discard destroyed the archive that had replaced its row",
        )

    def test_stores_one_archive_per_job_and_serves_it_back(self) -> None:
        record, created = self.put()
        self.assertTrue(created)
        self.assertEqual(record.size, len(b"archive-bytes"))
        self.assertEqual(record.name, f"{JOB}.tar")
        self.assertEqual(record.as_dict()["url"], f"/v1/jobs/{JOB}/artifacts")
        blob, size, served = self.store.open(JOB, now=11)
        with blob:
            self.assertEqual(blob.read(), b"archive-bytes")
        self.assertEqual((size, served.digest), (len(b"archive-bytes"), record.digest))

    def test_a_second_upload_for_the_same_run_keeps_the_first(self) -> None:
        # a run's artifact is immutable, and a retried attempt of the same run
        # must read the archive that was already stored rather than replace it
        self.put(value=b"first")
        record, created = self.put(value=b"second")
        self.assertFalse(created)
        blob, _, _ = self.store.open(JOB, now=11)
        with blob:
            self.assertEqual(blob.read(), b"first")
        self.assertEqual(record.size, len(b"first"))

    def test_two_jobs_keep_separate_archives(self) -> None:
        self.put(value=b"one")
        self.put(job_id=OTHER, value=b"two")
        for job_id, expected in ((JOB, b"one"), (OTHER, b"two")):
            blob, _, _ = self.store.open(job_id, now=11)
            with blob:
                self.assertEqual(blob.read(), expected)

    def test_a_run_that_uploaded_nothing_reads_as_no_artifact(self) -> None:
        self.assertIsNone(self.store.get(JOB, now=11))
        with self.assertRaises(FileNotFoundError):
            self.store.open(JOB, now=11)

    def test_an_archive_expires_at_its_ttl(self) -> None:
        self.put(now=1.0)
        self.assertIsNotNone(self.store.get(JOB, now=50))
        self.assertIsNone(self.store.get(JOB, now=150))
        # the file goes with the row, or an expired run's bytes stay on disk
        # with nothing naming them
        self.assertFalse(self.store.archive_path(JOB).exists())

    def test_repository_quota_evicts_that_repository_oldest_first(self) -> None:
        store = ArtifactStore(self.root / "quota", ttl_seconds=100, repository_quota=12, storage_quota=64)
        self.put(job_id=JOB, value=b"12345678", now=1, store=store)
        self.put(job_id=OTHER, value=b"abcdefgh", now=2, store=store)
        self.assertIsNone(store.get(JOB, now=3))
        self.assertIsNotNone(store.get(OTHER, now=3))

    def test_storage_quota_spares_the_newest_across_repositories(self) -> None:
        store = ArtifactStore(self.root / "global", ttl_seconds=100, repository_quota=64, storage_quota=12)
        self.put(job_id=JOB, value=b"12345678", repository="owner/one", now=1, store=store)
        self.put(job_id=OTHER, value=b"abcdefgh", repository="owner/two", now=2, store=store)
        self.assertIsNone(store.get(JOB, now=3))
        self.assertIsNotNone(store.get(OTHER, now=3))

    def test_refuses_an_upload_that_declares_more_than_the_cap(self) -> None:
        # the declared length is refused before a byte is read, so a device
        # cannot put a large body on the broker's disk by claiming a size the
        # cap turns down and then sending it anyway
        store = ArtifactStore(self.root / "capped", max_bytes=16)
        source = CountingBytes(b"x" * 64)
        with self.assertRaises(ArtifactError):
            store.put(JOB, "owner/repo", source, 64, now=10)
        self.assertEqual(source.read_bytes, 0)
        self.assertEqual(list(store.archives.iterdir()), [])
        self.assertIsNone(store.get(JOB, now=11))

    def test_an_upload_that_under_declares_writes_only_what_it_declared(self) -> None:
        # the other half: a device that claims less than it sends has the rest
        # left unread, so what lands on disk is what it said it was sending
        store = ArtifactStore(self.root / "under", max_bytes=1024)
        source = CountingBytes(b"x" * 64)
        record, created = store.put(JOB, "owner/repo", source, 16, now=10)
        self.assertTrue(created)
        self.assertEqual(record.size, 16)
        self.assertEqual(source.read_bytes, 16)
        blob, size, _ = store.open(JOB, now=11)
        with blob:
            self.assertEqual(blob.read(), b"x" * 16)
        self.assertEqual(size, 16)

    def test_refuses_a_body_that_hands_over_more_than_it_declared(self) -> None:
        # a read is trusted to return at most what it was asked for, and a body
        # that does not disagrees with the length it declared. The extra bytes
        # are exactly what the cap bounds, so the upload is refused rather than
        # stored at a size the caller never declared.
        store = ArtifactStore(self.root / "greedy", max_bytes=1024)
        with self.assertRaises(StagingError) as refused:
            store.put(JOB, "owner/repo", GreedyBytes(b"x" * 64), 16, now=10)
        self.assertIn("Content-Length", str(refused.exception))
        self.assertEqual(list(store.archives.iterdir()), [])
        self.assertIsNone(store.get(JOB, now=11))

    def test_refuses_an_archive_over_the_size_cap(self) -> None:
        store = ArtifactStore(self.root / "small", ttl_seconds=100, max_bytes=8)
        with self.assertRaises(ArtifactError):
            self.put(value=b"x" * 9, store=store)
        self.assertIsNone(store.get(JOB, now=11))

    def test_refuses_an_upload_that_ends_early(self) -> None:
        # a body that stops before the length it declared lost the connection
        # mid-upload, and half an archive is not a run's output
        with self.assertRaises(StagingError):
            self.store.put(JOB, "owner/repo", io.BytesIO(b"x"), 2, now=10)
        self.assertIsNone(self.store.get(JOB, now=11))

    def test_leaves_no_temporary_file_behind(self) -> None:
        self.put()
        with self.assertRaises(ArtifactError):
            self.put(value=b"x" * 9, store=ArtifactStore(self.root / "small", max_bytes=4))
        self.assertEqual(list(self.store.directory.glob("upload-*")), [])

    def test_a_missing_archive_file_reads_as_no_artifact(self) -> None:
        self.put()
        self.store.archive_path(JOB).unlink()
        self.assertIsNone(self.store.get(JOB, now=11))

    def test_a_failed_commit_leaves_nothing_a_reader_can_reach(self) -> None:
        # a transaction that did not commit leaves no row, so the archive it
        # published names nothing: a reader gets no artifact rather than one
        # whose row was discarded out from under it
        with unittest.mock.patch.object(
            self.store, "_enforce_quota", side_effect=sqlite3.OperationalError("disk I/O error")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.put()
        self.assertIsNone(self.store.get(JOB, now=11))
        with self.assertRaises(FileNotFoundError):
            self.store.open(JOB, now=11)

    def test_a_later_upload_stores_cleanly_after_a_failed_one(self) -> None:
        with unittest.mock.patch.object(
            self.store, "_enforce_quota", side_effect=sqlite3.OperationalError("disk I/O error")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.put(value=b"first")
        record, created = self.put(value=b"second")
        self.assertTrue(created)
        self.assertEqual(record.size, len(b"second"))
        blob, _, _ = self.store.open(JOB, now=11)
        with blob:
            self.assertEqual(blob.read(), b"second")

    def test_a_failed_commit_leaves_no_archive_outside_the_quota(self) -> None:
        # the archive is published before the commit so a reader cannot see the
        # row without it, and the TTL and both quotas are counted from the
        # row, so a rolled-back commit has to take the published archive with
        # it or the store holds disk nothing accounts for
        with unittest.mock.patch.object(
            self.store, "_enforce_quota", side_effect=sqlite3.OperationalError("disk I/O error")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.put()
        self.assertIsNone(self.store.get(JOB, now=11))
        self.assertEqual([path.name for path in self.store.archives.iterdir()], [])

    def test_a_failure_after_the_commit_keeps_the_committed_archive(self) -> None:
        # the commit is what makes the row claim the file. Anything that raises
        # after it rolled nothing back, so taking the archive away would leave a
        # committed row with nothing behind it. The failure is raised at the
        # commit itself, which is the first instruction after it.
        real_commit = sqlite3.Connection.execute
        state = {"committed": False}

        def fail_after_commit(connection, sql, *args):
            result = real_commit(connection, sql, *args)
            if sql == "COMMIT":
                state["committed"] = True
                raise RuntimeError("raised after the commit")
            return result

        original = self.store._connect

        class Watching:
            def __init__(self, connection):
                self._connection = connection

            def execute(self, sql, *args):
                if state["committed"] and sql == "COMMIT":
                    raise RuntimeError("raised after the commit")
                return fail_after_commit(self._connection, sql, *args)

            def __getattr__(self, name):
                return getattr(self._connection, name)

        with unittest.mock.patch.object(self.store, "_connect", lambda: Watching(original())):
            with self.assertRaises(RuntimeError):
                self.put(value=b"stored")
        self.assertTrue(state["committed"])
        self.assertTrue(self.store.archive_path(JOB).exists())
        blob, _, _ = self.store.open(JOB, now=11)
        with blob:
            self.assertEqual(blob.read(), b"stored")

    def test_a_failure_before_the_commit_removes_the_orphan(self) -> None:
        # the other side of that: nothing names the file, so it is an orphan
        with unittest.mock.patch.object(
            self.store, "_enforce_quota", side_effect=sqlite3.OperationalError("disk I/O error")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.put(value=b"output")
        self.assertFalse(self.store.archive_path(JOB).exists())
        self.assertIsNone(self.store.get(JOB, now=11))

    def test_a_failed_commit_never_deletes_a_stored_run_s_archive(self) -> None:
        # the rename is reached only where no row names the run, so what a
        # rollback removes is always the archive this call published
        self.put(value=b"stored")
        with unittest.mock.patch(
            "cindral.artifact.os.replace", side_effect=OSError("no space left on device")
        ):
            record, created = self.put(value=b"retry")
        self.assertFalse(created)
        self.assertEqual(record.size, len(b"stored"))
        self.assertTrue(self.store.archive_path(JOB).is_file())
        blob, _, _ = self.store.open(JOB, now=11)
        with blob:
            self.assertEqual(blob.read(), b"stored")

    def test_a_failed_upload_leaves_a_stored_run_readable(self) -> None:
        # the row short-circuits before the rename, so the bytes a reader
        # already holds are never on the path a failure could touch
        self.put(value=b"stored")
        with unittest.mock.patch(
            "cindral.artifact.os.replace", side_effect=OSError("no space left on device")
        ):
            record, created = self.put(value=b"retry")
        self.assertFalse(created)
        self.assertEqual(record.size, len(b"stored"))
        blob, _, _ = self.store.open(JOB, now=11)
        with blob:
            self.assertEqual(blob.read(), b"stored")

    def test_a_stored_run_is_never_overwritten_by_a_later_upload(self) -> None:
        # the row short-circuits the write, so a device that re-uploads after a
        # lost reply cannot replace what a reader is already downloading
        self.put(value=b"stored")
        record, created = self.put(value=b"retry")
        self.assertFalse(created)
        self.assertEqual(record.size, len(b"stored"))
        blob, _, _ = self.store.open(JOB, now=11)
        with blob:
            self.assertEqual(blob.read(), b"stored")

    def test_a_read_never_sees_a_row_without_the_archive_it_names(self) -> None:
        # a reader that found the row and no file would discard the row, and
        # the upload publishing it would leave a file no row names. The rename
        # happens before the commit, so the archive is in place the instant the
        # row becomes visible to another connection, and the row survives the
        # read that follows.
        with self.store._connection() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM artifacts").fetchone())
        self.put(value=b"stored")
        self.assertIsNotNone(self.store.get(JOB, now=11))
        self.assertTrue(self.store.archive_path(JOB).is_file())

    def test_the_archive_is_in_place_before_the_row_becomes_visible(self) -> None:
        # the order is the whole guarantee, so it is observed rather than
        # inferred: a commit that pauses mid-flight must find the archive
        # already published, or a reader racing it would see the row first
        seen: list[bool] = []
        real_connect = self.store._connect
        store = self.store

        class WatchingConnection:
            def __init__(self, connection):
                self._connection = connection

            def execute(self, sql, *args):
                result = self._connection.execute(sql, *args)
                if sql == "COMMIT":
                    seen.append(store.archive_path(JOB).is_file())
                return result

            def __getattr__(self, name):
                return getattr(self._connection, name)

        def connect():
            return WatchingConnection(real_connect())

        with unittest.mock.patch.object(self.store, "_connect", connect):
            self.put(value=b"stored")
        self.assertEqual(seen, [True])

    def test_a_staging_file_is_never_left_beside_the_run_it_belongs_to(self) -> None:
        # staging lives under the run's own name, so no read and no sweep of
        # another run can reach the bytes an upload is still writing, and the
        # file is gone by the time the call returns either way
        self.put(value=b"stored")
        self.assertEqual([path.name for path in self.store.archives.iterdir()], [f"{JOB}{ARTIFACT_SUFFIX}"])

    def test_a_failed_upload_leaves_no_staging_file(self) -> None:
        with unittest.mock.patch.object(
            self.store, "_enforce_quota", side_effect=sqlite3.OperationalError("disk I/O error")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.put(value=b"first")
        self.assertEqual([path.name for path in self.store.archives.iterdir()], [])

    def test_each_upload_stages_under_a_name_of_its_own(self) -> None:
        # a fixed name would let a second broker over the same directory
        # truncate the bytes this one is writing
        names: list[str] = []
        with ArtifactStaging(self.store.archives) as first, ArtifactStaging(self.store.archives) as second:
            names = [first.name, second.name]
        self.assertEqual(len(set(names)), 2)
        self.assertTrue(all(name.endswith(STAGING_SUFFIX) for name in names), names)

    def test_a_staging_file_a_killed_upload_left_is_collected_by_age(self) -> None:
        # the process that removes one in a finally is the one that died, so a
        # broker that restarts mid-upload finds it on disk
        orphan = self.store.archives / f"{'f' * 32}{STAGING_SUFFIX}"
        orphan.write_bytes(b"half an archive")
        self.put()
        self.assertTrue(orphan.exists(), "a fresh staging file is an upload in flight")
        os.utime(orphan, (0, 0))
        self.assertIsNotNone(self.store.get(JOB, now=11))
        self.assertFalse(orphan.exists())

    def test_repeated_failed_uploads_of_one_run_leave_nothing_behind(self) -> None:
        # a device retrying a run whose broker keeps failing must not fill the
        # directory with one orphan per attempt
        with unittest.mock.patch.object(
            self.store, "_enforce_quota", side_effect=sqlite3.OperationalError("disk I/O error")
        ):
            for _ in range(5):
                with self.assertRaises(sqlite3.OperationalError):
                    self.put(value=b"output")
        self.assertEqual([path.name for path in self.store.archives.iterdir()], [])


class ArtifactPathTest(unittest.TestCase):
    """A job id arrives in a URL, so it is the one name a caller picks here."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ArtifactStore(self.root / "artifacts")

    def test_refuses_an_id_that_is_not_a_job_id(self) -> None:
        for job_id in ("..", "../" + "a" * 32, "a" * 31, "A" * 32, "a" * 33, "abc.def", "%2e%2e", ""):
            with self.subTest(job_id=job_id), self.assertRaises(ArtifactError):
                self.store.archive_path(job_id)

    def test_refuses_an_id_that_names_a_file_outside_the_archives(self) -> None:
        secret = self.root / "secret.txt"
        secret.write_text("broker secret")
        for job_id in ("../../secret.txt", "..%2fsecret.txt", "/etc/passwd"):
            with self.subTest(job_id=job_id), self.assertRaises(ArtifactError):
                self.store.open(job_id)

    def test_an_accepted_id_resolves_inside_the_archive_directory(self) -> None:
        path = self.store.archive_path(JOB)
        self.assertTrue(path.resolve().is_relative_to(self.store.archives.resolve()))


