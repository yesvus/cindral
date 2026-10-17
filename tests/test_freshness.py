"""Whether a device names itself, and whether it is behind the broker.

These test the comparison on its own terms, in a checkout built for the purpose.
The two tests that matter most are the ones about *unknown*: a guard that refuses
on missing information takes every device offline the moment the broker stops
naming itself, which is a worse outage than the stale agent it prevents.
"""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
from functools import partial

from cindral.agent import Agent
from cindral.build_info import BuildInfo, checkout_build
from test_agent import FakeClient

from cindral.freshness import (
    NOT_AN_ANCESTOR,
    Throttled,
    _output_text,
    behind_reason,
    checkout_root,
    fetch_commit,
)


def git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)


#: git's own switch for assuming a repository belongs to somebody else. It is
#: the refusal the fix exists to answer, from git rather than from a stand-in.
ASSUME_OTHER_OWNER = "GIT_TEST_ASSUME_DIFFERENT_OWNER"


def git_is_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def build_checkout(test: unittest.TestCase, version: str = "0.12.0") -> tuple[str, str]:
    """A checkout built for the purpose, and the commit it names.

    The point of building one is that the answer no longer depends on where the
    suite happens to be running. A test that reads the ambient checkout needs
    that checkout to have a resolvable commit, which is a property of the
    environment: it held on a laptop and not in a job container, so the same
    commit passed in one place and failed in the other.
    """
    root = tempfile.mkdtemp(prefix="cindral-build-")
    test.addCleanup(shutil.rmtree, root, True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "device@example.invalid")
    git(root, "config", "user.name", "device")
    (pathlib.Path(root) / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "build")
    return root, git(root, "rev-parse", "HEAD").stdout.strip()


def refuse_unowned_repositories(test: unittest.TestCase) -> None:
    """Make git refuse every repository the process reads, as it would for another.

    `GIT_TEST_ASSUME_DIFFERENT_OWNER` is git's own switch for this, so the rule
    under test is git's rather than a wrapper's: a fake that reproduces the
    refusal for one subcommand cannot see a call site that forgot the directory,
    because the fake lets the others through. It applies to every repository
    operation, which is the point.

    It also needs no root and touches no PATH, so it cannot leak into other tests
    the way a shim on PATH does.
    """
    if shutil.which("git") is None:
        test.skipTest("no git to ask")
    root = tempfile.mkdtemp(prefix="cindral-canary-")
    test.addCleanup(shutil.rmtree, root, True)
    # The canary: the switch is git's own test knob, not a supported interface,
    # so a future git may rename it, and a machine may already carry
    # `safe.directory = *` globally, which answers the same question for
    # everything. Either way the tests that depend on this would pass while
    # checking nothing, so the switch is confirmed to bite before they run.
    subprocess.run(["git", "init", "-q", "-b", "main", root], check=True, capture_output=True)
    previous = os.environ.get(ASSUME_OTHER_OWNER)
    os.environ[ASSUME_OTHER_OWNER] = "1"
    test.addCleanup(
        os.environ.__setitem__ if previous is not None else os.environ.pop,
        ASSUME_OTHER_OWNER,
        previous,
    )
    if _git_refuses(root):
        return
    test.skipTest(
        f"{ASSUME_OTHER_OWNER} does not make git refuse here, so the refusal is not being tested"
    )


def _git_refuses(root: str) -> bool:
    """Whether git refuses to read `root` under the switch that is set."""
    done = subprocess.run(
        ["git", "log", "-1", "--format=%H"], cwd=root, capture_output=True, check=False
    )
    return done.returncode != 0


class CheckoutTest(unittest.TestCase):
    def test_the_root_is_found_from_a_file_inside_the_checkout(self) -> None:
        # an agent is started from `/` by a service manager, so the working
        # directory is not evidence of anything
        root = checkout_root()
        self.assertIsNotNone(root)
        self.assertTrue((pathlib.Path(root) / ".git").exists())
        self.assertEqual(
            checkout_root(pathlib.Path(__file__).resolve()), root, "the package anchor moved"
        )

    def test_no_checkout_is_reported_when_there_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as outside:
            self.assertIsNone(checkout_root(outside))

    def test_a_checkout_names_its_own_commit_and_version(self) -> None:
        # the two halves of the comparison: this device's commit, and the
        # version that goes with it because a bare sha says nothing to a human
        root, commit = build_checkout(self)
        build = checkout_build(root)
        self.assertEqual(build.commit, commit)
        self.assertEqual(build.version, "0.12.0")
        self.assertNotEqual(build.built_at, "unknown")

    def test_the_checkout_the_suite_runs_in_names_its_own_commit(self) -> None:
        """The ambient checkout, which is what a device actually runs from.

        This is the coverage that matters for the product, so it is kept rather
        than folded into the test above. It is skipped, with the reason, where
        there is no git to ask: the job container the suite runs in has not
        always carried one, and a skip says that plainly where a failure would
        say the guard is broken when it is the environment that is.
        """
        if not git_is_available():
            self.skipTest("no git in this environment, so no checkout can name itself")
        root = checkout_root()
        if root is None:
            self.skipTest("the suite is not running from a checkout, so there is nothing to read")
        build = checkout_build(root)
        self.assertEqual(len(build.commit), 40, build)

    def test_a_checkout_the_process_does_not_own_still_names_its_commit(self) -> None:
        """The case that made the staleness guard inert, and it was silent.

        A device agent's checkout is routinely owned by whoever cloned it while
        the agent runs as another user, and git refuses a repository it does not
        own. The refusal is not an error here: the build came back `unknown`, the
        broker had nothing to compare against its own commit, and a genuinely
        stale device kept claiming work. Nothing said so at any point.
        """
        # built first: the switch refuses every repository operation, so the
        # checkout has to exist before the product is asked to read it
        root, commit = build_checkout(self)
        refuse_unowned_repositories(self)
        build = checkout_build(root)
        self.assertEqual(build.commit, commit)
        self.assertNotEqual(build.built_at, "unknown")

    def test_a_directory_that_is_not_a_checkout_names_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as outside:
            build = checkout_build(outside)
            self.assertEqual(build.commit, "unknown")
            self.assertEqual(build.version, "unknown")


class BehindReasonTest(unittest.TestCase):
    def _checkout(self) -> tuple[str, str, str]:
        """A checkout at `old`, and the commit `new` that the broker names."""
        root = tempfile.mkdtemp(prefix="cindral-fresh-")
        self.addCleanup(shutil.rmtree, root, True)
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "device@example.invalid")
        git(root, "config", "user.name", "device")
        (pathlib.Path(root) / "VERSION").write_text("0.12.0\n", encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "old")
        old = git(root, "rev-parse", "HEAD").stdout.strip()
        (pathlib.Path(root) / "VERSION").write_text("0.13.0\n", encoding="utf-8")
        git(root, "commit", "-qam", "new")
        return root, old, git(root, "rev-parse", "HEAD").stdout.strip()

    def test_a_device_whose_commit_is_in_the_brokers_history_is_behind(self) -> None:
        root, old, new = self._checkout()
        reason = behind_reason(old, new, root, {"version": "0.12.0"})
        self.assertIsNotNone(reason)
        self.assertIn(old[:8], reason)
        self.assertIn(new[:8], reason)
        self.assertIn("0.12.0", reason)
        self.assertIn("1 commit(s) behind", reason)

    def test_the_distance_counts_the_commits_between_them(self) -> None:
        root, old, new = self._checkout()
        (pathlib.Path(root) / "more").write_text("x\n", encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "third")
        third = git(root, "rev-parse", "HEAD").stdout.strip()
        self.assertIn("2 commit(s) behind", behind_reason(old, third, root))
        self.assertIn("1 commit(s) behind", behind_reason(new, third, root))

    def test_a_device_at_the_brokers_commit_is_not_behind(self) -> None:
        root, _old, new = self._checkout()
        self.assertIsNone(behind_reason(new, new, root))

    def test_a_device_ahead_of_the_broker_is_not_behind(self) -> None:
        # a device updated before the broker is deployed is not stale, and
        # refusing it would take a working device offline
        root, _old, new = self._checkout()
        (pathlib.Path(root) / "deployed-later").write_text("x\n", encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "ahead")
        ahead = git(root, "rev-parse", "HEAD").stdout.strip()
        self.assertIsNone(behind_reason(ahead, new, root))

    def _clone_behind(self) -> tuple[str, str, str]:
        """A device clone whose origin has moved on without it.

        The shape a device is actually in: a normal clone that has not pulled
        since the broker was deployed. This is the case where `git merge-base`
        answers about a commit it has never seen, and the answer is
        indistinguishable from "not an ancestor".
        """
        origin = self._checkout()[0]
        device = tempfile.mkdtemp(prefix="cindral-device-")
        self.addCleanup(shutil.rmtree, device, True)
        subprocess.run(["git", "clone", "-q", origin, device], capture_output=True, check=True)
        git(origin, "commit", "-qam", "deployed", "--allow-empty")
        newer = git(origin, "rev-parse", "HEAD").stdout.strip()
        return device, git(device, "rev-parse", "HEAD").stdout.strip(), newer

    def test_a_device_that_has_not_fetched_the_brokers_commit_is_behind(self) -> None:
        # the whole guard is a no-op here without this: git answers "not an
        # ancestor" for a commit it has never seen, and that is the device this
        # exists to stop
        device, own, newer = self._clone_behind()
        # with the fetch removed, the device has the newer commit nowhere
        reason = behind_reason(own, newer, device, fetch=lambda commit: None)
        self.assertIsNotNone(reason, "a device that never fetched the broker's commit was let through")
        self.assertIn("does not have", reason)
        self.assertIn(newer[:8], reason)
        self.assertIn(own[:8], reason)

    def test_a_device_that_can_fetch_is_measured_rather_than_merely_stopped(self) -> None:
        # the distance is the part an operator acts on, so a fetch is attempted
        # once and, when it works, the reason says how far behind the device is
        #
        # The refusal is not set here, and that is deliberate rather than an
        # omission. This is the only test where a fetch runs for real, and its
        # origin is a local clone, so under the switch git refuses *that*
        # repository too while reading the remote. A device in production fetches
        # from a URL and never reads a second repository, so enabling the switch
        # here would assert something about the fixture rather than about the
        # product. The fetch call site is covered by the shared helper with the
        # other three, not by a test of its own.
        device, own, newer = self._clone_behind()
        reason = behind_reason(own, newer, device, fetch=partial(fetch_commit, device))
        self.assertIsNotNone(reason)
        self.assertIn("1 commit(s) behind", reason)
        self.assertIn(own[:8], reason)
        # the fetch must not damage the checkout it was run in. A shallow fetch
        # gets the commit here and leaves every later comparison answering from a
        # hole, so the note about a shallow history is the signal it happened
        self.assertNotIn("shallow", reason)
        self.assertEqual(
            _output_text(device, "rev-parse", "--is-shallow-repository").strip(), "false"
        )

    def test_a_shallow_checkout_is_stopped_rather_than_trusted(self) -> None:
        # a shallow clone answers "not an ancestor" for a commit past the cut,
        # which is the same false answer as never having fetched. The distance
        # still counts, so the device is stopped with a lower bound and a note
        root, old, new = self._checkout()
        git(root, "commit", "-qam", "third", "--allow-empty")
        third = git(root, "rev-parse", "HEAD").stdout.strip()
        (pathlib.Path(root) / ".git/shallow").write_text(third + "\n", encoding="utf-8")
        reason = behind_reason(old, third, root)
        self.assertIsNotNone(reason, "a shallow checkout was trusted to answer the question")
        self.assertIn("shallow", reason)
        self.assertIn("commit(s) behind", reason)

    def test_a_device_with_no_remote_is_still_reported_as_behind(self) -> None:
        # the fetch is a courtesy for the message. A device that cannot reach
        # the remote is not made better by a fetch that did not happen, and the
        # fact that the commit is absent is enough to stop it
        device, own, newer = self._clone_behind()
        git(device, "remote", "remove", "origin")
        reason = behind_reason(own, newer, device, {"version": "0.12.0"})
        self.assertIsNotNone(reason)
        self.assertIn("does not have", reason)
        self.assertIn(own[:8], reason)

    def test_a_commit_that_does_not_exist_anywhere_still_stops_the_device(self) -> None:
        # a broker naming a commit no device can ever have would otherwise wedge
        # the pool, and the reason has to say that rather than look like a diff
        root, _old, _new = self._checkout()
        reason = behind_reason("a" * 40, "b" * 40, root, fetch=lambda commit: None)
        self.assertIsNotNone(reason)
        self.assertIn("does not have", reason)

    def test_a_staleness_check_still_works_on_a_checkout_it_does_not_own(self) -> None:
        """The consumer side, which is where a partial fix does its damage.

        Naming the directory for the one call that reads the commit leaves every
        other call refusing, and a refusal is a non-zero status that reads as
        "unavailable". The ancestry test then concludes the broker's commit is
        missing, and a device that is *ahead* and perfectly healthy stops
        claiming work, with a remedy that cannot work: the `git fetch` it tells
        the operator to run is refused the same way.

        A device behind the broker must still be told so, which is the whole
        point, and each direction is checked separately because each reads
        through a different call. One failure hides the others: a checkout equal
        to the broker's commit is answered before any git runs, and a stop on the
        missing commit hides whether the distance was ever measured. So: ahead,
        which is the shallow probe; behind with a distance, which is the count;
        and behind with a fetch, which is the fetch itself.
        """
        root, old, new = self._checkout()
        git(root, "commit", "-qam", "third", "--allow-empty")
        third = git(root, "rev-parse", "HEAD").stdout.strip()
        # every commit is made before the switch is set, because it refuses
        # repository operations and building the checkout is one of those
        refuse_unowned_repositories(self)
        # a device ahead of the broker is not behind. The shallow probe is the
        # call that has to answer for it: refused, it reports every checkout as
        # shallow, and a healthy device is stopped for a history it has.
        self.assertIsNone(
            behind_reason(third, new, root),
            "a healthy device ahead of the broker was stopped",
        )
        # a device behind is stopped with a distance, which is a different call:
        # the count is what turns a diff into "2 commit(s) behind"
        reason = behind_reason(old, third, root)
        self.assertIsNotNone(reason)
        self.assertIn("2 commit(s) behind", reason)
        self.assertNotIn("shallow", reason)
        # and the fetch still runs, which is a fourth: refused, the device is
        # told to fetch something it was never able to fetch
        fetched: list[str] = []
        reason = behind_reason(old, "a" * 40, root, fetch=fetched.append)
        self.assertIsNotNone(reason)
        self.assertEqual(fetched, ["a" * 40], "the fetch was skipped on an unowned checkout")

    def test_a_git_failure_is_not_read_as_an_answer(self) -> None:
        # exit 1 is git saying "not an ancestor" and exit 0 is the opposite.
        # Anything else is git failing, which is not a claim about the device,
        # and a device is stopped on a claim. The job it goes on to take fails in
        # the executor with the same broken git and is reported, so proceeding
        # makes the fault visible where stopping would only make a device quiet.
        root, old, new = self._checkout()
        self.assertIsNone(self._behind_with(root, old, new, compare_status=128))
        # and a confirmed non-ancestor is still not behind
        self.assertIsNone(self._behind_with(root, new, old, compare_status=1))
        # while a confirmed ancestor still is
        self.assertIsNotNone(self._behind_with(root, old, new, compare_status=0))

    def _behind_with(self, root, own, broker, compare_status):
        """`behind_reason` with git answering the ancestry test one way only."""
        answered = {
            ("cat-file",): (0, ""),
            ("rev-parse",): (0, "false\n"),
            ("merge-base",): (compare_status, ""),
            ("rev-list",): (0, "3\n"),
        }

        def fake(args, **kwargs):
            # every call now names the directory first, so the subcommand is the
            # argument after that pair rather than the second one
            named = args.index("-c") + 2 if "-c" in args else 1
            for key, (status, out) in answered.items():
                if key[0] in args[named:named + 1]:
                    return subprocess.CompletedProcess(args, status, out, "")
            raise AssertionError(f"unexpected git call: {args}")

        with unittest.mock.patch("cindral.freshness.subprocess.run", side_effect=fake):
            return behind_reason(own, broker, root)

    def test_a_missing_commit_says_what_to_do_about_it(self) -> None:
        # the stop is self-healing once the checkout is updated, but only if the
        # operator reading the log knows that updating it is the fix
        root, old, new = self._checkout()
        reason = behind_reason(old, "f" * 40, root, fetch=lambda commit: None)
        self.assertIn("git fetch", reason)

    def test_a_fetch_that_is_not_due_does_not_run(self) -> None:
        # a device that cannot reach its origin would otherwise spend every poll
        # waiting on a fetch, which is slower than the work it is refusing and
        # hammers the remote besides
        moment = [1000.0]
        runs = []
        throttled = Throttled(runs.append, interval=300.0, now=lambda: moment[0])
        for _ in range(5):
            throttled("commit")
        self.assertEqual(runs, ["commit"], "the fetch ran on every poll")
        moment[0] += 299.0
        throttled("commit")
        self.assertEqual(len(runs), 1, "the fetch ran before its interval elapsed")
        moment[0] += 2.0
        throttled("commit")
        self.assertEqual(len(runs), 2, "the fetch never ran again")

    def test_the_device_throttles_its_own_fetch_across_polls(self) -> None:
        # the throttle has to live with the polling loop. Built per comparison it
        # would be new every time, and the first poll would fetch every time
        root, old, new = self._checkout()
        client = FakeClient()
        client.build = BuildInfo(version="0.12.0", commit=old)
        client.broker_build = {"commit": "f" * 40}
        agent = Agent(client, "server", executor=lambda spec, cancel: 0, source_root=root)
        # the agent's own throttle and its own fetch, not a stand-in: an agent
        # that never fetches is an agent whose checkout never learns about the
        # broker, which is the state this whole change exists to end
        fetched = []
        with unittest.mock.patch(
            "cindral.agent.fetch_commit", side_effect=lambda root, commit: fetched.append((root, commit))
        ):
            for _ in range(3):
                self.assertIsNotNone(agent.behind_broker())
        self.assertEqual(len(fetched), 1, fetched)
        self.assertEqual(fetched[0], (root, "f" * 40), "the wrong commit was fetched")

    def test_a_distance_git_cannot_count_does_not_hide_the_staleness(self) -> None:
        # the guard must still fire when the number is unavailable, or a device
        # that is behind is let through because a cosmetic detail failed
        root, old, new = self._checkout()
        with unittest.mock.patch("cindral.freshness._count", return_value=None):
            reason = behind_reason(old, new, root)
        self.assertIsNotNone(reason)
        self.assertIn("behind", reason)

    def test_unknown_is_not_evidence_of_anything(self) -> None:
        root, old, _new = self._checkout()
        for own, broker in (
            (old, ""),
            (old, "unknown"),
            (old, "   "),
            ("unknown", "b" * 40),
            ("", "b" * 40),
        ):
            with self.subTest(own=own, broker=broker):
                self.assertIsNone(behind_reason(own, broker, root))

    def test_unknown_never_consults_git(self) -> None:
        # a placeholder substituted for a missing commit puts a device's fate
        # in the hands of whatever git does with a name it cannot resolve. The
        # answer would be the same, so only the absence of the call shows it
        root, old, _new = self._checkout()
        with unittest.mock.patch("cindral.freshness.subprocess.run") as runner:
            self.assertIsNone(behind_reason(old, "unknown", root))
        runner.assert_not_called()

    def test_a_device_with_no_checkout_cannot_be_ordered_and_is_left_alone(self) -> None:
        # with no .git there is no history, so the answer is unknown rather than
        # a guess, and the device keeps working
        _root, old, new = self._checkout()
        for where in (None, "/nonexistent-checkout", tempfile.gettempdir()):
            with self.subTest(where=where):
                self.assertIsNone(behind_reason(old, new, where))

    def test_git_failing_is_not_evidence_of_being_behind(self) -> None:
        # a missing binary, a timeout, or a repository that cannot be read
        # answers nothing, and answering nothing must not remove a device
        with unittest.mock.patch(
            "cindral.freshness.subprocess.run", side_effect=OSError("no git here")
        ):
            self.assertIsNone(behind_reason("a" * 40, "b" * 40, "/tmp"))

    def test_a_padded_or_cased_unknown_is_still_unknown(self) -> None:
        # the value is a string from the network, so "Unknown" must not be read
        # as a commit and asked about git
        root, old, _new = self._checkout()
        with unittest.mock.patch("cindral.freshness.subprocess.run") as runner:
            self.assertIsNone(behind_reason(old, " unknown ", root))
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
