"""What caps a job runs under, on each of the three surfaces that take them.

The caps reach the job container, the build container, and the line printed when the
agent starts. These pin all three, because a cap honoured on one and dropped on
another is a limit that reads as configured and is not.
"""
import argparse
import pathlib
import unittest

from cindral.limits import BUILD_SUPPORTS_DISK, JobLimits
from cindral.options import byte_rate


class RunArgsTest(unittest.TestCase):
    def test_an_uncapped_device_emits_no_flags(self) -> None:
        self.assertEqual(JobLimits().run_args(), [])
        self.assertEqual(JobLimits().build_args(resource=True), [])

    def test_cpu_becomes_a_quota_inside_one_period(self) -> None:
        self.assertEqual(
            JobLimits(cpus=2).run_args(),
            ["--cpu-period", "100000", "--cpu-quota", "200000"],
        )

    def test_swap_is_pinned_so_memory_is_the_whole_budget(self) -> None:
        # memory-swap equal to memory means no swap beyond the cap, so a container
        # that exhausts its memory is killed rather than spilling to disk
        self.assertEqual(
            JobLimits(memory="4g").run_args(),
            ["--memory", "4g", "--memory-swap", "4g"],
        )

    def test_disk_caps_reach_the_job_container(self) -> None:
        args = JobLimits(disk_write=1_048_576, disk_read=2_097_152).run_args()
        self.assertIn("--device-write-bps", args)
        self.assertIn("--device-read-bps", args)
        self.assertEqual(args[args.index("--device-write-bps") + 1], "1048576")
        self.assertEqual(args[args.index("--device-read-bps") + 1], "2097152")


class BuildArgsTest(unittest.TestCase):
    def test_buildkit_takes_cpu_and_memory_as_resource_pairs(self) -> None:
        args = JobLimits(cpus=2, memory="4g").build_args(resource=True)
        self.assertIn("--resource", args)
        self.assertIn("cpu-quota=200000", args)
        self.assertIn("memory=4g", args)

    def test_the_legacy_builder_takes_the_run_flags(self) -> None:
        args = JobLimits(cpus=2, memory="4g").build_args(resource=False)
        self.assertIn("--cpu-quota", args)
        self.assertIn("--memory", args)
        self.assertNotIn("--resource", args)

    def test_a_disk_cap_never_reaches_the_build(self) -> None:
        """BuildKit refuses a block-I/O resource key rather than ignoring it.

        Emitting one would fail every build on a device that configured a disk cap,
        which is the opposite of the cap's purpose. Probed against the tool:

            $ docker buildx build --resource device-write-bps=1048576 .
            ERROR: failed to build: unknown resource "device-write-bps"

        So the build path carries cpu and memory only, and says the disk cap does
        not reach it.
        """
        self.assertFalse(BUILD_SUPPORTS_DISK)
        limits = JobLimits(cpus=2, disk_write=1_048_576)
        for resource in (True, False):
            with self.subTest(resource=resource):
                args = limits.build_args(resource=resource)
                self.assertNotIn("--device-write-bps", args)
                self.assertNotIn("--device-read-bps", args)
                self.assertNotIn("device-write-bps", " ".join(args))
                # cpu still reaches the build, spelled the way this style spells it
                joined = " ".join(args)
                self.assertIn("cpu-quota=200000" if resource else "--cpu-quota", joined)

    def test_a_disk_cap_says_it_does_not_reach_the_build(self) -> None:
        note = JobLimits(disk_write=1_048_576, disk_read=2_097_152).build_disk_note()
        self.assertIsNotNone(note)
        self.assertIn("write, read", note)
        self.assertIn("no block-I/O resource flag", note)

    def test_no_note_without_a_disk_cap(self) -> None:
        self.assertIsNone(JobLimits(cpus=2, memory="4g").build_disk_note())


class DescribeTest(unittest.TestCase):
    """The line read when the agent starts, not from a job log."""

    def test_an_uncapped_device_says_so_plainly(self) -> None:
        line = JobLimits().describe()
        self.assertIn("no job limits", line)
        self.assertIn("uncapped", line)
        # every knob named, so a reader is not left wondering which one is open
        for knob in ("cpu", "memory", "disk read", "disk write"):
            self.assertIn(knob, line)

    def test_a_fully_capped_device_names_no_gaps(self) -> None:
        line = JobLimits(cpus=2, memory="4g", disk_write=1, disk_read=2).describe()
        self.assertIn("job limits:", line)
        self.assertNotIn("uncapped", line)

    def test_a_partial_cap_names_what_is_still_open(self) -> None:
        # the case that matters: cpu capped and disk not is a device that looks
        # configured and will still fill a disk
        line = JobLimits(cpus=2).describe()
        self.assertIn("cpu=2", line)
        self.assertIn("uncapped: memory, disk-write, disk-read", line)


class ByteRateOptionTest(unittest.TestCase):
    def test_a_realistic_rate_is_accepted(self) -> None:
        self.assertEqual(byte_rate("1048576"), 1_048_576)

    def test_zero_and_negative_are_refused(self) -> None:
        # zero would read as "no limit" in every layer below, so it must not parse
        for value in ("0", "-1"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    byte_rate(value)

    def test_a_rate_is_not_bounded_by_the_concurrency_ceiling(self) -> None:
        """positive_int counts slots and is bounded; a byte rate is not a slot count.

        Sharing the parser would refuse every real value while accepting the small
        ones, so the two quantities cannot share a ceiling.
        """
        self.assertEqual(byte_rate(str(10_000_000)), 10_000_000)

    def test_nonsense_is_refused(self) -> None:
        for value in ("abc", "1.5", ""):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    byte_rate(value)


class ConstructionTest(unittest.TestCase):
    """The guards that live on the object rather than on the argument parser.

    The CLI refuses a non-positive rate through `byte_rate`, but the executor builds
    `JobLimits` itself, so the parser is not the only way in. A guard that exists
    only on the path that happens to be tested is a guard on one path.
    """

    def test_a_non_positive_rate_is_refused_here_too(self) -> None:
        for kwargs in ({"disk_write": 0}, {"disk_read": 0}, {"disk_write": -1}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    JobLimits(**kwargs)

    def test_the_startup_line_is_in_the_cli_not_only_in_a_plan(self) -> None:
        """The line an operator reads when the agent starts must be in the code.

        An uncapped device was previously visible only by finding the word inside
        the log of a build it happened to run. Putting the line in this module's
        tests is not enough: nothing would notice its removal from the CLI, which is
        where it has to be to do any good.
        """
        cli = (pathlib.Path(__file__).resolve().parent.parent / "src" / "cindral" / "cli.py")
        source = cli.read_text(encoding="utf-8")
        self.assertIn('log(f"cindral: {executor.describe_limits()}")', source)

    def test_the_disk_flags_reach_the_command_line(self) -> None:
        source = (pathlib.Path(__file__).resolve().parent.parent / "src" / "cindral" / "cli.py")
        source = source.read_text(encoding="utf-8")
        for flag in ("--job-disk-write", "--job-disk-read"):
            self.assertIn(flag, source)
        self.assertIn("disk_write=args.job_disk_write", source)
        self.assertIn("disk_read=args.job_disk_read", source)
