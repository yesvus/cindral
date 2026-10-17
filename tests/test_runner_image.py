"""Which container image a runs-on label resolves to.

A self-hosted label names hardware, and the device pool has hardware, so it takes
the default image rather than being refused. A hosted platform label names an image
GitHub picks, so one this build has no image for is refused rather than answered
with a near-enough substitute: `ubuntu-26.04` on `ubuntu:24.04` is a toolchain the
job never asked for, and it reports success.
"""
import tempfile
import unittest
from pathlib import Path

from cindral.actions import (
    DEFAULT_RUNNER_IMAGE,
    IMAGE_MAP,
    WorkflowError,
    parse_workflow,
    workflow_to_contract,
)
from cindral.agent import JobSpec
from cindral.workflow_context import RunContext


class RunnerImageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workflows_dir = Path(self.tmp.name) / ".github" / "workflows"
        self.workflows_dir.mkdir(parents=True)

    def context(self) -> RunContext:
        job = JobSpec("job1", "org/repo", "sha123", "refs/heads/main", (), 60)
        return RunContext.from_job(job, "push", workflow="CI", job_id="test")

    def job_with(self, runs_on: str):
        """The one job in a workflow whose only interesting line is `runs-on`."""
        workflow_file = self.workflows_dir / "ci.yml"
        workflow_file.write_text(
            f"""
name: ci
on: [push]
jobs:
  check:
    runs-on: {runs_on}
    steps:
      - run: echo hello
"""
        )
        return parse_workflow(workflow_file).jobs["check"]

    def image_for(self, runs_on: str) -> str:
        return workflow_to_contract(self.job_with(runs_on), self.context()).image

    def refused_for(self, runs_on: str) -> str:
        with self.assertRaises(WorkflowError) as caught:
            self.image_for(runs_on)
        return str(caught.exception)

    def test_a_self_hosted_label_runs_on_the_default_image(self) -> None:
        """`[self-hosted, Linux, ARM64, repo-label]` names hardware, not an image.

        Refusing it meant a repository whose runner is registered, online and idle
        could not use it, while the image for the case sat in the module with no
        caller anywhere in the repository.
        """
        self.assertEqual(
            self.image_for("[self-hosted, Linux, ARM64, repo-label]"), DEFAULT_RUNNER_IMAGE
        )

    def test_a_repository_s_own_label_alone_also_names_hardware(self) -> None:
        """GitHub requires `self-hosted` on a custom label, but the name alone is
        still not a platform, so it takes the default image."""
        self.assertEqual(self.image_for("repo-label"), DEFAULT_RUNNER_IMAGE)

    def test_a_custom_label_starting_with_a_platform_name_is_still_hardware(self) -> None:
        """`ubuntu-builder` is a runner someone registered, not a hosted image.

        Matching hosted labels by prefix alone refused it. The explicit
        `self-hosted` marker is what GitHub requires on a self-hosted runner, so
        where it is present the repository has already said where the job runs.
        """
        self.assertEqual(self.image_for("[self-hosted, ubuntu-builder]"), DEFAULT_RUNNER_IMAGE)

    def test_a_platform_shaped_label_with_no_self_hosted_marker_is_still_refused(self) -> None:
        """The marker is what settles it, so without it the shape decides.

        A bare `ubuntu-26.04` names a hosted image this build has no mapping for,
        and answering it with a near-enough image is the failure this guard exists
        to prevent.
        """
        self.assertIn("ubuntu-26.04", self.refused_for("ubuntu-26.04"))

    def test_a_bare_custom_label_shaped_like_a_platform_is_hardware(self) -> None:
        """`ubuntu-builder` without the marker is still a machine, not an image.

        A prefix cannot tell the two apart. The shape can: a hosted label is a
        platform then `latest` or a version, so `ubuntu-26.04` is an image and
        `ubuntu-builder` is a label a maintainer chose.
        """
        self.assertEqual(self.image_for("ubuntu-builder"), DEFAULT_RUNNER_IMAGE)
        self.assertEqual(self.image_for("macos-mini"), DEFAULT_RUNNER_IMAGE)

    def test_every_label_is_read_for_a_refusal_before_a_map_hit_is_accepted(self) -> None:
        """A list means whichever is free, so ignoring one label answers another question.

        `[ubuntu-22.04, windows-latest]` must not quietly take the mapped image while
        the Windows label sits beside it: the job offered two platforms and the run
        gets one the author may not have meant.
        """
        refusal = self.refused_for("[ubuntu-22.04, windows-latest]")
        self.assertIn("windows-latest", refusal)

    def test_a_mapped_hosted_label_still_wins_over_the_default(self) -> None:
        """The fallback is for labels with no image, not a replacement for the map."""
        self.assertEqual(self.image_for("ubuntu-22.04"), IMAGE_MAP["ubuntu-22.04"])
        self.assertNotEqual(self.image_for("ubuntu-22.04"), DEFAULT_RUNNER_IMAGE)

    def test_a_mapped_label_resolves_the_same_whatever_its_case(self) -> None:
        """The shape check is case-blind, so the lookup beside it has to be too.

        `Ubuntu-22.04` matches the hosted shape and was refused because `IMAGE_MAP`
        holds only the lowercase key. A shape check that agrees a label is one it
        knows, beside a lookup that then refuses it, is two answers to one question.
        """
        self.assertEqual(self.image_for("Ubuntu-22.04"), IMAGE_MAP["ubuntu-22.04"])
        self.assertEqual(self.image_for("UBUNTU-LATEST"), IMAGE_MAP["ubuntu-latest"])

    def test_an_unmapped_label_is_refused_the_same_whatever_its_case(self) -> None:
        """Normalising the lookup must not normalise the refusal away, or the label.

        The message quotes the label as the repository wrote it, because that is
        what someone editing the workflow will be looking for.
        """
        self.assertIn("Ubuntu-26.04", self.refused_for("Ubuntu-26.04"))

    def test_an_unmapped_hosted_ubuntu_label_is_refused_rather_than_defaulted(self) -> None:
        """`ubuntu-26.04` is not `ubuntu:24.04`, and must not be run as one.

        The fallback is for labels that name no platform. A hosted Ubuntu label this
        build has no image for names a toolchain version, and answering it with the
        default runs the job on a version it never asked for and reports success.
        """
        self.assertIn("ubuntu-26.04", self.refused_for("ubuntu-26.04"))

    def test_a_foreign_platform_is_refused_rather_than_given_a_linux_image(self) -> None:
        """Answering a Windows or macOS job with a Linux container is worse than
        refusing: the job runs, on the wrong platform, and its verdict means
        nothing."""
        for label in ("windows-latest", "macos-latest"):
            with self.subTest(label=label):
                self.assertIn(label, self.refused_for(label))

class LabelEnforcementTest(unittest.TestCase):
    """An image is not the whole of what `runs-on` asks for.

    The contract resolves a self-hosted label to an image, which is right and
    incomplete: `[self-hosted, ARM64, repo-label]` also says what machine will do.
    Dropping that let a job run on hardware satisfying none of it and report
    success, which is the hazard the image was resolving in the first place.
    """

    def contract_asking_for(self, runs_on: str):
        import tempfile
        from cindral.actions import parse_workflow, workflow_to_contract
        from cindral.agent import JobSpec
        from cindral.workflow_context import RunContext

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        workflows = Path(tmp.name) / ".github" / "workflows"
        workflows.mkdir(parents=True)
        workflow_file = workflows / "ci.yml"
        workflow_file.write_text(
            f"""
name: ci
on: [push]
jobs:
  check:
    runs-on: {runs_on}
    steps:
      - run: echo hello
"""
        )
        job = JobSpec("job1", "org/repo", "sha123", "refs/heads/main", (), 60)
        context = RunContext.from_job(job, "push", workflow="CI", job_id="test")
        return workflow_to_contract(parse_workflow(workflow_file).jobs["check"], context)

    def test_the_contract_carries_the_labels_the_job_asked_for(self) -> None:
        contract = self.contract_asking_for("[self-hosted, Linux, ARM64, repo-label]")
        self.assertEqual(sorted(contract.labels), ["ARM64", "Linux", "repo-label"])

    def test_self_itself_is_dropped_because_it_asserts_nothing(self) -> None:
        """Every device in the pool is self-hosted, so carrying it would make a
        correctly-matched device look mismatched."""
        contract = self.contract_asking_for("[self-hosted, ARM64]")
        self.assertEqual(list(contract.labels), ["ARM64"])

    def test_a_device_missing_a_label_refuses_the_job(self) -> None:
        from cindral.executor import DockerExecutor, ExecutorError

        contract = self.contract_asking_for("[self-hosted, ARM64, repo-label]")
        executor = DockerExecutor.__new__(DockerExecutor)
        executor.device_labels = {"linux", "arm64"}
        with self.assertRaises(ExecutorError) as caught:
            executor._check_labels(contract, None)  # type: ignore[arg-type]
        self.assertIn("repo-label", str(caught.exception))

    def test_loading_a_contract_for_this_device_refuses_it_rather_than_running(self) -> None:
        """The check has to be on the path that resolves a job, not only callable.

        The tests above call `_check_labels` directly, which proves the predicate and
        nothing about whether anything calls it. Mutating the call site out of
        `_load_contract` left this file green, which is the whole point: a guard that
        nothing invokes is the shape this repository keeps finding.
        """
        from cindral.agent import JobSpec
        from cindral.executor import DockerExecutor, ExecutorError

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        workflows = repo / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text(
            """
name: ci
on: [push]
jobs:
  check:
    runs-on: [self-hosted, ARM64, repo-label]
    steps:
      - run: echo hello
"""
        )
        executor = DockerExecutor.__new__(DockerExecutor)
        executor.device_labels = {"linux", "arm64"}
        job = JobSpec("job1", "org/repo", "sha123", "refs/heads/main", (), 60)
        with self.assertRaises(ExecutorError) as caught:
            executor._load_contract(repo, job)
        self.assertIn("repo-label", str(caught.exception))

    def test_a_hosted_image_label_is_not_treated_as_hardware(self) -> None:
        """`ubuntu-24.04` is an image, settled by the translator, not a device label.

        Enforcing it would refuse every hosted job on a correctly configured pool,
        because no device advertises the name of a hosted runner image.
        """
        from cindral.executor import DockerExecutor

        contract = self.contract_asking_for("ubuntu-24.04")
        self.assertEqual(list(contract.labels), ["ubuntu-24.04"])
        executor = DockerExecutor.__new__(DockerExecutor)
        executor.device_labels = {"linux", "arm64"}
        executor._check_labels(contract, None)  # type: ignore[arg-type]

    def test_a_device_with_every_label_runs_the_job(self) -> None:
        from cindral.executor import DockerExecutor

        contract = self.contract_asking_for("[self-hosted, Linux, ARM64]")
        executor = DockerExecutor.__new__(DockerExecutor)
        executor.device_labels = {"linux", "arm64", "x64"}
        executor._check_labels(contract, None)  # type: ignore[arg-type]
