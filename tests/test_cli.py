"""Tests for the agent capacity flags and their validated configuration."""
import argparse
import os
import pathlib
import unittest
from unittest import mock

from cindral import cli
from test_freshness import build_checkout


class AgentCapacityFlagTest(unittest.TestCase):
    def _parse(self, argv, **environment):
        environ = {"CINDRAL_AGENT_TOKEN": "tok"}
        environ.update(environment)
        with mock.patch.dict(os.environ, environ, clear=True), mock.patch(
            "sys.argv", ["cindral", "agent", *argv]
        ), mock.patch.object(cli, "Agent") as agent, mock.patch.object(cli, "DockerExecutor") as docker:
            agent.return_value.run_once.return_value = True
            cli.main()
        return agent.call_args.kwargs, docker.call_args.kwargs

    def _refused(self, variable, values):
        """Each value must stop the agent before it claims anything."""
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(SystemExit) as raised:
                    self._parse(["--once"], **{variable: value})
                self.assertEqual(raised.exception.code, 2)

    def test_one_slot_and_no_caps_by_default(self) -> None:
        agent_kwargs, docker_kwargs = self._parse(["--once"])
        self.assertEqual(agent_kwargs["max_concurrency"], 1)
        self.assertIsNone(docker_kwargs["cpus"])
        self.assertIsNone(docker_kwargs["memory"])
        self.assertEqual(agent_kwargs["heartbeat_seconds"], 15.0)

    def test_environment_sets_slots_and_caps(self) -> None:
        agent_kwargs, docker_kwargs = self._parse(
            ["--once"],
            CINDRAL_AGENT_MAX_CONCURRENCY="4",
            CINDRAL_JOB_CPUS="2",
            CINDRAL_JOB_MEMORY="12g",
        )
        self.assertEqual(agent_kwargs["max_concurrency"], 4)
        self.assertEqual(docker_kwargs["cpus"], 2.0)
        self.assertEqual(docker_kwargs["memory"], "12g")

    def test_environment_sets_a_non_default_heartbeat(self) -> None:
        agent_kwargs, _ = self._parse(["--once"], CINDRAL_AGENT_HEARTBEAT_SECONDS="20")
        self.assertEqual(agent_kwargs["heartbeat_seconds"], 20.0)

    def test_flags_win_over_the_environment(self) -> None:
        agent_kwargs, docker_kwargs = self._parse(
            ["--once", "--max-concurrency", "2", "--job-cpus", "1.5", "--job-memory", "4GiB"],
            CINDRAL_AGENT_MAX_CONCURRENCY="4",
            CINDRAL_JOB_CPUS="8",
            CINDRAL_JOB_MEMORY="64g",
        )
        self.assertEqual(agent_kwargs["max_concurrency"], 2)
        self.assertEqual(docker_kwargs["cpus"], 1.5)
        self.assertEqual(docker_kwargs["memory"], "4GiB")

    def test_a_blank_environment_value_falls_back_to_the_default(self) -> None:
        """An exported-but-empty variable is how a unit file says "no value"."""
        agent_kwargs, docker_kwargs = self._parse(
            ["--once"],
            CINDRAL_AGENT_MAX_CONCURRENCY="",
            CINDRAL_JOB_CPUS="",
            CINDRAL_JOB_MEMORY="",
            CINDRAL_AGENT_HEARTBEAT_SECONDS="",
        )
        self.assertEqual(agent_kwargs["max_concurrency"], 1)
        self.assertEqual(agent_kwargs["heartbeat_seconds"], 15.0)
        self.assertIsNone(docker_kwargs["cpus"])
        self.assertIsNone(docker_kwargs["memory"])

    def test_a_whitespace_only_environment_value_falls_back_to_the_default(self) -> None:
        agent_kwargs, docker_kwargs = self._parse(
            ["--once"],
            CINDRAL_AGENT_MAX_CONCURRENCY="   ",
            CINDRAL_JOB_CPUS="  ",
            CINDRAL_JOB_MEMORY="\t",
        )
        self.assertEqual(agent_kwargs["max_concurrency"], 1)
        self.assertIsNone(docker_kwargs["cpus"])
        self.assertIsNone(docker_kwargs["memory"])

    def test_the_agent_is_started_with_the_checkout_it_runs_from(self) -> None:
        """The staleness guard needs both halves, and only the CLI has both.

        The agent can order two commits only if it is given the tree holding the
        history between them, and it can be refused only if it is told which
        commit it is. An agent constructed without them claims whatever the
        broker offers, which is the exact failure this exists to stop.
        """
        agent_kwargs, _ = self._parse(["--once"])
        self.assertIsNotNone(agent_kwargs["source_root"], "the agent got no checkout")
        self.assertTrue(
            (pathlib.Path(agent_kwargs["source_root"]) / ".git").exists(),
            f"{agent_kwargs['source_root']} is not a git checkout",
        )

    def test_the_client_is_given_a_build_named_from_that_checkout(self) -> None:
        # a checkout built for the purpose, so the build the client is handed is
        # pinned to a commit this test made. Reading the ambient checkout needed
        # that checkout to have a resolvable commit, which is a property of the
        # environment rather than of the code, and it failed in a job container.
        root, commit = build_checkout(self, version="1.2.3")
        environ = {"CINDRAL_AGENT_TOKEN": "tok"}
        with mock.patch.dict(os.environ, environ, clear=True), mock.patch(
            "sys.argv", ["cindral", "agent", "--once"]
        ), mock.patch.object(
            # the CLI resolves its own root, so the test pins what it resolved to
            # rather than passing one in
            cli, "checkout_root", return_value=root
        ), mock.patch.object(cli, "Agent"), mock.patch.object(cli, "DockerExecutor"), mock.patch.object(
            cli, "CindralClient"
        ) as client:
            cli.main()
        build = client.call_args.kwargs["build"]
        self.assertEqual(build.commit, commit)
        self.assertEqual(build.version, "1.2.3")

    def test_a_surrounded_environment_value_is_trimmed(self) -> None:
        agent_kwargs, docker_kwargs = self._parse(
            ["--once"],
            CINDRAL_AGENT_MAX_CONCURRENCY=" 4 ",
            CINDRAL_JOB_MEMORY=" 12g ",
        )
        self.assertEqual(agent_kwargs["max_concurrency"], 4)
        self.assertEqual(docker_kwargs["memory"], "12g")

    def test_an_invalid_slot_count_is_refused(self) -> None:
        self._refused("CINDRAL_AGENT_MAX_CONCURRENCY", ("0", "-1", "two", "1.5"))

    def test_the_slot_count_is_capped_at_the_broker_limit(self) -> None:
        """Past the broker bound the heartbeat is refused and the device vanishes."""
        self.assertEqual(cli.MAX_CONCURRENCY, 1024)
        agent_kwargs, _ = self._parse(["--once"], CINDRAL_AGENT_MAX_CONCURRENCY="1024")
        self.assertEqual(agent_kwargs["max_concurrency"], 1024)
        self._refused("CINDRAL_AGENT_MAX_CONCURRENCY", ("1025", "999999"))

    def test_a_heartbeat_slower_than_the_online_window_is_refused(self) -> None:
        """A device that beats slower than the window reads as offline."""
        self.assertEqual(cli.ONLINE_WINDOW_SECONDS, 60.0)
        self.assertEqual(cli.MAX_HEARTBEAT_SECONDS, 30.0)
        agent_kwargs, _ = self._parse(["--once"], CINDRAL_AGENT_HEARTBEAT_SECONDS="30")
        self.assertEqual(agent_kwargs["heartbeat_seconds"], 30.0)
        self._refused("CINDRAL_AGENT_HEARTBEAT_SECONDS", ("0", "30.1", "60", "600", "-5"))

    def test_a_non_positive_idle_wait_is_refused(self) -> None:
        """A zero or negative wait spins the claim endpoint against an idle queue."""
        for value in ("0", "-5"):
            with self.subTest(value=value), mock.patch.dict(
                os.environ, {"CINDRAL_AGENT_TOKEN": "tok", "CINDRAL_AGENT_IDLE_SECONDS": value}, clear=True
            ), mock.patch("sys.argv", ["cindral", "agent", "--once"]):
                with self.assertRaises(SystemExit) as raised:
                    cli.main()
                self.assertEqual(raised.exception.code, 2)

    def test_an_invalid_cpu_cap_is_refused(self) -> None:
        self._refused("CINDRAL_JOB_CPUS", ("0", "-2", "half"))

    def test_a_cpu_cap_too_small_to_run_anything_is_refused(self) -> None:
        """Below 0.01 the quota rounds to no usable cpu and the container stalls."""
        self.assertEqual(cli.MIN_JOB_CPUS, 0.01)
        _, docker_kwargs = self._parse(["--once"], CINDRAL_JOB_CPUS="0.01")
        self.assertEqual(docker_kwargs["cpus"], 0.01)
        self._refused("CINDRAL_JOB_CPUS", ("0.00001", "0.005", "0.009"))

    def test_an_idle_wait_below_the_floor_is_refused(self) -> None:
        """A sub-0.1s wait polls the claim endpoint as fast as the device can."""
        self.assertEqual(cli.MIN_IDLE_SECONDS, 0.1)
        agent_kwargs, _ = self._parse(["--once"], CINDRAL_AGENT_IDLE_SECONDS="0.1")
        self.assertEqual(agent_kwargs["idle_seconds"], 0.1)
        self._refused("CINDRAL_AGENT_IDLE_SECONDS", ("0", "-1", "0.001", "0.05", "inf", "nan"))

    def test_a_non_finite_cpu_cap_is_refused(self) -> None:
        """`inf` passes a bare positivity test and reaches docker as a broken flag."""
        self._refused("CINDRAL_JOB_CPUS", ("inf", "-inf", "nan", "NaN", "Infinity", "1e400"))
        self._refused("CINDRAL_AGENT_HEARTBEAT_SECONDS", ("inf", "nan", "1e400"))

    def test_an_unreadable_memory_size_is_refused(self) -> None:
        self._refused("CINDRAL_JOB_MEMORY", ("12", "lots", "12 gigabytes", "-1g", "0g", "g12"))

    def test_a_memory_size_with_an_inner_space_is_refused(self) -> None:
        """`12 g` would reach docker verbatim, so the error must land here instead."""
        for value in ("12 g", "1.5 g", "12\tg", "12 g "):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    cli.memory_size(value)

    def test_accepted_memory_sizes(self) -> None:
        for value in ("12g", "512m", "2GiB", "1.5g", "4096k", "  8g  "):
            with self.subTest(value=value):
                self.assertEqual(cli.memory_size(value), value.strip())

    def test_serve_forever_runs_the_agent_loop(self) -> None:
        with mock.patch.dict(
            os.environ, {"CINDRAL_AGENT_TOKEN": "tok", "CINDRAL_AGENT_MAX_CONCURRENCY": "3"}, clear=True
        ), mock.patch("sys.argv", ["cindral", "agent"]), mock.patch.object(
            cli, "Agent"
        ) as agent, mock.patch.object(cli, "DockerExecutor"):
            cli.main()
        agent.return_value.serve_forever.assert_called_once_with()
        agent.return_value.run_once.assert_not_called()
        self.assertEqual(agent.call_args.kwargs["max_concurrency"], 3)

    def test_the_agent_claims_through_a_docker_capability_check(self) -> None:
        """The gate is built from the executor that runs the work, not a second guess."""
        with mock.patch.dict(os.environ, {"CINDRAL_AGENT_TOKEN": "tok"}, clear=True), mock.patch(
            "sys.argv", ["cindral", "agent", "--once"]
        ), mock.patch.object(cli, "Agent") as agent, mock.patch.object(
            cli, "DockerExecutor"
        ) as docker:
            cli.main()
        gate = agent.call_args.kwargs["preflight"]
        self.assertIsInstance(gate.__self__, cli.DockerPreflight)
        self.assertEqual(gate.__self__._docker, docker.return_value.docker)
        self.assertIs(gate.__self__._capture, docker.return_value.runner.capture)


if __name__ == "__main__":
    unittest.main()
