"""Tests for the artifact directory a run leaves behind for the device."""
import tarfile
import threading
import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory

from cindral.artifact import ARTIFACT_DIRECTORY, ARTIFACT_ROOT
from test_executor import FakeRunner, executor, spec


class ExecutorArtifactTest(unittest.TestCase):
    """The executor packs what a run wrote; the agent uploads and removes it."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_with_artifacts(self, files: dict[str, str], codes=None, **kwargs):
        runner = FakeRunner(files=files, codes=codes)
        result = executor(runner, str(self.root), **kwargs)(spec(), threading.Event())
        return runner, result

    def test_a_run_that_wrote_artifacts_leaves_an_archive_beside_the_workspace(self) -> None:
        _, result = self.run_with_artifacts({f"{ARTIFACT_DIRECTORY}/report.txt": "all checks passed"})
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.artifacts)
        archive = Path(result.artifacts)
        self.assertTrue(archive.is_file())
        with tarfile.open(archive) as opened:
            self.assertEqual(opened.getnames(), [ARTIFACT_ROOT, f"{ARTIFACT_ROOT}/report.txt"])
            self.assertEqual(
                opened.extractfile(f"{ARTIFACT_ROOT}/report.txt").read(), b"all checks passed"
            )

    def test_the_archive_outlives_the_workspace_it_was_packed_from(self) -> None:
        # the executor removes the workspace once the run ends, and the device
        # still has an upload to make afterwards
        _, result = self.run_with_artifacts({f"{ARTIFACT_DIRECTORY}/report.txt": "output"})
        self.assertFalse((self.root / "job-1").exists())
        self.assertTrue(Path(result.artifacts).is_file())

    def test_a_run_that_wrote_no_artifacts_says_so_and_uploads_nothing(self) -> None:
        _, result = self.run_with_artifacts({})
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.artifacts, "")
        self.assertIn(f"no {ARTIFACT_DIRECTORY} directory", result.log)

    def test_a_failed_run_still_leaves_its_artifacts(self) -> None:
        # a failing test is the run a reader most wants the output of
        _, result = self.run_with_artifacts(
            {f"{ARTIFACT_DIRECTORY}/junit.xml": "<failure/>"},
            codes={"pnpm run ci": 1},
        )
        self.assertEqual(result.exit_code, 1)
        self.assertTrue(result.artifacts)

    def test_a_run_still_passes_when_the_artifact_directory_is_a_link_out_of_the_checkout(self) -> None:
        # the directory a step writes into can be anything it likes, including
        # a link to somewhere else on the device, and a run's verdict is its
        # steps rather than what it managed to point that directory at
        secret = self.root / "elsewhere"
        secret.mkdir()
        (secret / "leaked.txt").write_text("a device file")
        runner = FakeRunner()
        kept = executor(runner, str(self.root), cleanup=False)
        self.assertEqual(kept(spec(), threading.Event()).exit_code, 0)
        directory = self.root / "job-1" / "repo" / ARTIFACT_DIRECTORY
        directory.symlink_to(secret, target_is_directory=True)
        result = kept(spec(), threading.Event())
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.artifacts, "")
        self.assertIn("artifacts", result.log)

    def test_an_artifact_directory_over_the_broker_cap_does_not_fail_the_run(self) -> None:
        # output the broker will not take is a missing artifact, not a failed
        # test: a run that passed must still report that it passed
        runner = FakeRunner(files={f"{ARTIFACT_DIRECTORY}/big.bin": "x" * 4096})
        with unittest.mock.patch("cindral.artifact_pack.MAX_ARTIFACT_BYTES", 512):
            result = executor(runner, str(self.root))(spec(), threading.Event())
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.artifacts, "")
        self.assertIn("artifacts skipped", result.log)

    def test_a_run_still_passes_when_the_tar_format_refuses_a_member(self) -> None:
        # the end of that, through the executor: a green run stays green, and
        # the run's log names the artifact that was skipped
        runner = FakeRunner(files={f"{ARTIFACT_DIRECTORY}/report.txt": "output"})
        with unittest.mock.patch("tarfile.TarFile.addfile", side_effect=ValueError("name is too long")):
            result = executor(runner, str(self.root))(spec(), threading.Event())
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.artifacts, "")
        self.assertIn("artifacts skipped", result.log)

    def test_a_run_still_passes_when_the_permission_handoff_fails(self) -> None:
        # the repair that hands a root-owned artifact directory back runs a
        # container, and it can fail to start. That is a missing artifact, not
        # a failed run: the run's verdict is its exit code.
        runner = FakeRunner(files={f"{ARTIFACT_DIRECTORY}/report.txt": "output"})
        with unittest.mock.patch("cindral.artifact_pack._write_archive", side_effect=PermissionError("root-owned")):
            with unittest.mock.patch("cindral.docker_support.make_cache_readable", side_effect=OSError("no daemon")):
                result = executor(runner, str(self.root))(spec(), threading.Event())
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.artifacts, "")
        self.assertIn("artifacts skipped", result.log)


if __name__ == "__main__":
    unittest.main()
