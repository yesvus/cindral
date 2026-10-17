"""Tests for the directory a run tars and the archive it becomes."""
import hashlib
import os
import tarfile
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from cindral import artifact_pack as pack_module
from cindral.artifact import (
    ARTIFACT_DIRECTORY,
    ARTIFACT_ROOT,
    MAX_ARTIFACT_BYTES,
    ArtifactStore,
)
from cindral.artifact_pack import pack_artifacts

JOB = "a" * 32


class Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, text: str) -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "".join(self.lines)


class PackArtifactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.workspace = self.root / "work" / JOB
        self.workspace.mkdir(parents=True)
        (self.repo / ARTIFACT_DIRECTORY).mkdir(parents=True)
        self.log = Log()

    def write(self, name: str, text: str) -> Path:
        path = self.repo / ARTIFACT_DIRECTORY / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def members(self, archive: str) -> list[str]:
        with tarfile.open(archive) as opened:
            return opened.getnames()

    def test_no_member_carries_a_wall_clock_time(self) -> None:
        """The packer is deterministic, and the store's dedup depends on it.

        The broker's artifact identity is a digest of the uploaded bytes, and a
        retry of the same commit on the same device is expected to produce the same
        bytes. That is what makes a digest unusable as an identity on its own, and
        why the store scopes a compare-and-delete by publication time as well.

        Nothing pinned it. `_write_archive` builds every TarInfo by hand and sets
        only type, mode and size, so mtime, uid and gid default to zero. One
        `member.mtime = time.time()` added for tidiness would make two packs of one
        tree differ, and the store would go on treating them as the same archive
        while the premise underneath it quietly stopped holding.

        Asserted on the members rather than by hashing two packs, because the hash
        version passes by coincidence: a mtime at one-second resolution is the same
        for two packs made in the same second, so the test would sit there green
        while the determinism it exists to protect was already gone. I made that
        mistake writing it and the mutation is what caught it.
        """
        self.write("report.txt", "all checks passed")
        self.write("nested/junit.xml", "<testsuite/>")
        archive = pack_artifacts(self.repo, self.workspace, self.log)
        with tarfile.open(archive) as opened:
            members = opened.getmembers()
        self.assertTrue(members)
        for member in members:
            with self.subTest(member=member.name):
                self.assertEqual(member.mtime, 0, "a member carries a wall-clock time")
                self.assertEqual(member.uid, 0)
                self.assertEqual(member.gid, 0)

    def test_tars_a_directory_a_run_filled(self) -> None:
        self.write("report.txt", "all checks passed")
        self.write("nested/junit.xml", "<testsuite/>")
        archive = pack_artifacts(self.repo, self.workspace, self.log)
        self.assertTrue(archive)
        self.assertEqual(
            sorted(self.members(archive)),
            [ARTIFACT_ROOT, f"{ARTIFACT_ROOT}/nested", f"{ARTIFACT_ROOT}/nested/junit.xml", f"{ARTIFACT_ROOT}/report.txt"],
        )
        with tarfile.open(archive) as opened:
            self.assertEqual(opened.extractfile(f"{ARTIFACT_ROOT}/report.txt").read(), b"all checks passed")

    def test_the_archive_survives_the_workspace_it_was_packed_from(self) -> None:
        # the executor removes the workspace once a run ends, so an archive
        # written inside it would be gone before the device could upload it
        self.write("report.txt", "output")
        archive = Path(pack_artifacts(self.repo, self.workspace, self.log))
        self.assertFalse(archive.is_relative_to(self.workspace))
        self.assertTrue(archive.is_file())

    def test_says_so_and_writes_nothing_when_the_directory_is_absent(self) -> None:
        (self.repo / ARTIFACT_DIRECTORY).rmdir()
        self.assertEqual(pack_artifacts(self.repo, self.workspace, self.log), "")
        self.assertIn("no .cindral/artifacts directory", self.log.text)
        self.assertEqual(list(self.workspace.parent.glob("*.tar")), [])

    def test_says_so_and_writes_nothing_when_the_directory_is_empty(self) -> None:
        self.assertEqual(pack_artifacts(self.repo, self.workspace, self.log), "")
        self.assertIn("is empty", self.log.text)
        self.assertEqual(list(self.workspace.parent.glob("*.tar")), [])

    def test_every_member_is_named_under_the_fixed_root(self) -> None:
        # a name a step chose decides where the archive puts its bytes, so a
        # reader that extracts what it is given must not be walked out of the
        # directory it was handed by doing so
        self.write("report.txt", "output")
        self.write("../escaped.txt", "should not be reachable")
        self.write("nested/deep/../../climbing.txt", "should not be reachable")
        archive = pack_artifacts(self.repo, self.workspace, self.log)
        for member in self.members(archive):
            self.assertTrue(member.startswith(f"{ARTIFACT_ROOT}/") or member == ARTIFACT_ROOT, member)
            self.assertNotIn("..", member)
        self.assertNotIn("escaped.txt", " ".join(self.members(archive)))

    def test_leaves_out_a_symlink_a_step_wrote(self) -> None:
        secret = self.root / "secret.txt"
        secret.write_text("device secret")
        self.write("report.txt", "output")
        (self.repo / ARTIFACT_DIRECTORY / "escape").symlink_to(secret)
        archive = pack_artifacts(self.repo, self.workspace, self.log)
        self.assertNotIn(f"{ARTIFACT_ROOT}/escape", self.members(archive))

    def test_refuses_a_directory_bigger_than_the_broker_would_store(self) -> None:
        self.write("big.bin", "x" * 1024)
        with unittest.mock.patch("cindral.artifact_pack.MAX_ARTIFACT_BYTES", 512):
            self.assertEqual(pack_artifacts(self.repo, self.workspace, self.log), "")
        self.assertIn("artifacts skipped", self.log.text)
        self.assertEqual(list(self.workspace.parent.glob("*.tar")), [])

    def test_refuses_a_directory_with_more_files_than_the_broker_would_store(self) -> None:
        for index in range(4):
            self.write(f"file-{index}.txt", "x")
        with unittest.mock.patch("cindral.artifact_pack.MAX_ARTIFACT_FILES", 2):
            self.assertEqual(pack_artifacts(self.repo, self.workspace, self.log), "")
        self.assertIn("file-count limit", self.log.text)

    def test_an_unexpected_pack_error_still_leaves_no_archive(self) -> None:
        # a pack can raise something nobody anticipated, and a truncated file
        # is worse than no file: the device must not leave one behind
        self.write("report.txt", "output")
        with unittest.mock.patch(
            "tarfile.TarFile.addfile", side_effect=RuntimeError("something nobody planned for")
        ):
            self.assertEqual(pack_artifacts(self.repo, self.workspace, self.log), "")
        self.assertIn("artifacts skipped", self.log.text)
        self.assertEqual(list(self.workspace.parent.glob("*.tar")), [])

    def test_a_name_the_tar_format_cannot_encode_is_a_skipped_artifact(self) -> None:
        # a member the tar format refuses would raise out of the pack and
        # reach the executor, where a passing run becomes a failed one
        self.write("report.txt", "output")
        with unittest.mock.patch("tarfile.TarFile.addfile", side_effect=ValueError("name is too long")):
            self.assertEqual(pack_artifacts(self.repo, self.workspace, self.log), "")
        self.assertIn("artifacts skipped", self.log.text)
        self.assertEqual(list(self.workspace.parent.glob("*.tar")), [])

    def test_a_directory_the_device_cannot_read_is_repaired_and_retried(self) -> None:
        # a job container leaves root-owned files behind, the way the cache
        # directory's repair does, so the archive is written again after the
        # ownership is handed back
        self.write("report.txt", "output")
        repaired: list[Path] = []
        real = pack_module._write_archive
        calls = {"n": 0}

        def flaky(directory: Path, destination: Path) -> int:
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError("root-owned files")
            return real(directory, destination)

        def repair(directory: Path) -> None:
            repaired.append(directory)

        with unittest.mock.patch("cindral.artifact_pack._write_archive", side_effect=flaky):
            archive = pack_artifacts(self.repo, self.workspace, self.log, repair)
        self.assertTrue(archive)
        self.assertEqual(repaired, [self.repo / ARTIFACT_DIRECTORY])
        self.assertIn(f"{ARTIFACT_ROOT}/report.txt", self.members(archive))

    def test_an_unreadable_directory_with_no_repair_leaves_no_archive(self) -> None:
        # the archive the failed attempt opened is the one the repair reuses,
        # so with no repair to run the partial file goes rather than being left
        # for the next run to find
        self.write("report.txt", "output")

        def denied(directory: Path, target: Path) -> int:
            target.write_bytes(b"truncated")
            raise PermissionError("root-owned files")

        with unittest.mock.patch("cindral.artifact_pack._write_archive", side_effect=denied):
            self.assertEqual(pack_artifacts(self.repo, self.workspace, self.log), "")
        self.assertIn("could not be read", self.log.text)
        self.assertEqual(list(self.workspace.parent.glob("*.tar")), [])

    def test_a_repair_that_fails_is_a_skipped_artifact(self) -> None:
        # the helper that hands a root-owned directory back runs a container,
        # and that container can fail to start. It is the same rule as every
        # other pack failure: a log line and no archive, never a failed run.
        self.write("report.txt", "output")

        def repair(_directory: Path) -> None:
            raise OSError("cache permissions could not be prepared: helper container failed")

        def denied(directory: Path, target: Path) -> int:
            raise PermissionError("root-owned files")

        with unittest.mock.patch("cindral.artifact_pack._write_archive", side_effect=denied):
            self.assertEqual(pack_artifacts(self.repo, self.workspace, self.log, repair), "")
        self.assertIn("helper container failed", self.log.text)
        self.assertEqual(list(self.workspace.parent.glob("*.tar")), [])

    def test_never_raises_when_the_directory_cannot_be_repaired(self) -> None:
        # a run's verdict is its exit code, so a missing artifact is a log line
        self.write("report.txt", "output")

        def repair(_directory: Path) -> None:
            raise OSError("chown failed")

        with unittest.mock.patch("cindral.artifact_pack._write_archive", side_effect=PermissionError):
            self.assertEqual(pack_artifacts(self.repo, self.workspace, self.log, repair), "")
        self.assertIn("artifacts skipped", self.log.text)
        self.assertEqual(list(self.workspace.parent.glob("*.tar")), [])

    def truncating(self, directory: Path, target: Path) -> int:
        """A pack that opens the archive and then stops part way."""
        target.write_bytes(b"truncated")
        raise OSError("no space left on device")

    def test_a_pack_that_stops_part_way_leaves_no_archive(self) -> None:
        # a truncated archive looks like a real one to everything downstream,
        # and a device whose disk filled mid-pack would leave one behind on
        # every run it tried
        self.write("report.txt", "output")
        destination = self.workspace.parent / f"{self.workspace.name}.tar"
        with unittest.mock.patch("cindral.artifact_pack._write_archive", side_effect=self.truncating):
            self.assertEqual(pack_artifacts(self.repo, self.workspace, self.log), "")
        self.assertFalse(destination.exists())
        self.assertIn("artifacts skipped", self.log.text)

    def test_refuses_a_directory_that_escapes_the_checkout(self) -> None:
        # the directory is a two-part name and a step wrote both parts, so
        # either can be a symlink. A checkout whose .cindral points elsewhere
        # makes the join resolve outside it, and packing that would hand a
        # reader files the run never produced.
        elsewhere = self.root / "elsewhere"
        (elsewhere / "artifacts").mkdir(parents=True)
        (elsewhere / "artifacts" / "leaked.txt").write_text("a device file")
        cindral = self.repo / ".cindral"
        cindral.rename(self.root / "cindral-holding-the-contract")
        cindral.symlink_to(elsewhere, target_is_directory=True)
        self.assertEqual(pack_artifacts(self.repo, self.workspace, self.log), "")
        self.assertIn("no .cindral/artifacts directory", self.log.text)
        self.assertEqual(list(self.workspace.parent.glob("*.tar")), [])

    def test_refuses_a_directory_reached_through_a_symlinked_parent(self) -> None:
        # the walk follows the join, so the guard has to be on what the join
        # resolves to rather than on the last part being a link
        target = self.root / "target"
        (target / "artifacts").mkdir(parents=True)
        (target / "artifacts" / "leaked.txt").write_text("a device file")
        artifacts = self.repo / ARTIFACT_DIRECTORY
        artifacts.rmdir()
        artifacts.symlink_to(target / "artifacts", target_is_directory=True)
        self.assertEqual(pack_artifacts(self.repo, self.workspace, self.log), "")
        self.assertEqual(list(self.workspace.parent.glob("*.tar")), [])

    def test_a_directory_that_is_a_symlink_out_of_the_checkout_is_not_packed(self) -> None:
        secret = self.root / "secret.txt"
        secret.write_text("device secret")
        (self.repo / ARTIFACT_DIRECTORY).rmdir()
        (self.repo / ARTIFACT_DIRECTORY).symlink_to(secret)
        self.assertEqual(pack_artifacts(self.repo, self.workspace, self.log), "")

    def test_a_retry_after_a_failed_pack_stores_the_whole_archive(self) -> None:
        self.write("report.txt", "output")
        (self.workspace.parent / f"{self.workspace.name}.tar").write_bytes(b"a half-written archive")
        with unittest.mock.patch("cindral.artifact_pack._write_archive", side_effect=self.truncating):
            self.assertEqual(pack_artifacts(self.repo, self.workspace, self.log), "")
        archive = pack_artifacts(self.repo, self.workspace, self.log)
        with tarfile.open(archive) as opened:
            self.assertEqual(opened.getnames(), [ARTIFACT_ROOT, f"{ARTIFACT_ROOT}/report.txt"])

    def test_the_size_cap_is_the_same_one_the_broker_refuses(self) -> None:
        self.assertEqual(MAX_ARTIFACT_BYTES, ArtifactStore(self.root / "n").max_bytes)


if __name__ == "__main__":
    unittest.main()
