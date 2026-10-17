"""The device-side release path has to hold the properties the deploy depends on.

A shell script cannot be mutation tested by the Python suite, so these assert the
things that would silently break a release: a single-architecture build, a token
written to disk, a digest that is guessed rather than read back, and a build that
does not carry the version and commit the image reports at runtime.
"""
import pathlib
import re
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "publish-image.sh"
WORKFLOW = SCRIPT.parent.parent / ".github" / "workflows" / "publish-broker.yml"


class PublishScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_the_script_exists_and_is_executable(self) -> None:
        self.assertTrue(SCRIPT.is_file())
        self.assertTrue(SCRIPT.stat().st_mode & 0o111, "publish-image.sh is not executable")

    def test_binfmt_registers_the_foreign_architecture_not_the_native_one(self) -> None:
        # A host that is already arm64 emulates nothing. Installing arm64 there
        # registers the emulator this machine does not need and leaves amd64
        # unregistered, so the build fails on its last step, after the native
        # half is built and the cache written. The architecture has to be
        # derived from the native one or the script only works on one host.
        self.assertIn("NATIVE=", self.source)
        self.assertIn('if [ "$NATIVE" = "amd64" ]', self.source)
        self.assertIn('FOREIGN="arm64"', self.source)
        self.assertIn('binfmt --install "$FOREIGN"', self.source)
        self.assertNotIn("binfmt --install arm64", self.source)
        self.assertNotIn("binfmt --install amd64", self.source)

    def test_it_says_which_half_is_emulated(self) -> None:
        # a build that is about to spend twenty minutes under emulation should
        # say so, and the log is the only place that will be read afterwards
        self.assertIn("emulating ${FOREIGN}", self.source)

    def test_it_builds_both_architectures(self) -> None:
        # the cluster is arm64 and a hosted runner is amd64, so a single-arch
        # image resolves to a digest half the fleet cannot pull, which takes the
        # broker down on rollout rather than failing the build
        self.assertIn("--platform linux/amd64,linux/arm64", self.source)
        self.assertIn("tonistiigi/binfmt", self.source)

    def test_it_refuses_to_run_without_a_registry_credential(self) -> None:
        # a push with no token fails deep inside buildx, after the clone and the
        # build, which is an hour of work for an error that is knowable up front
        self.assertIn('if [ -z "${GHCR_TOKEN:-}" ]', self.source)

    def test_it_requires_an_explicit_image_repository(self) -> None:
        self.assertIn('REPO="${CINDRAL_REPO:-}"', self.source)
        self.assertIn("set CINDRAL_REPO to the GitHub owner/name", self.source)
        self.assertNotIn("yesvus/cindral", self.source)

    def test_the_token_is_only_ever_stdin(self) -> None:
        # a token in a file, or echoed, outlives the command in a shell history
        # and in whatever reads the working directory afterwards
        self.assertIn("--password-stdin", self.source)
        for leak in (">$", ">>", "echo $GHCR_TOKEN", "echo \"$GHCR_TOKEN", "printenv"):
            self.assertNotIn(leak, self.source, f"the token could reach a file via {leak!r}")

    def test_it_reads_the_digest_back_rather_than_assuming_one(self) -> None:
        # ops pins a digest, so a guessed or hardcoded one is a production pin
        self.assertIn("imagetools inspect", self.source)
        self.assertIn("exit 1", self.source)
        self.assertNotRegex(self.source, r"sha256:[0-9a-f]{64}")

    def test_it_builds_the_commit_being_pushed(self) -> None:
        # a build that reads main at a different moment than it reports is an
        # image whose version string names a commit it does not contain
        self.assertIn('git -C "$REPO" fetch --depth 1 origin main', self.source)
        self.assertIn('SHA="$(git -C "$REPO" rev-parse HEAD)"', self.source)
        for arg in ("CINDRAL_VERSION", "CINDRAL_COMMIT", "CINDRAL_BUILT_AT"):
            self.assertIn(f'--build-arg "{arg}=', self.source)

    def test_it_fails_fast_on_a_missing_prerequisite(self) -> None:
        for tool in ("command -v docker", "docker buildx version"):
            self.assertIn(tool, self.source)
        self.assertRegex(self.source, r"exit 1")

    def test_it_does_not_silently_continue_past_a_failed_build(self) -> None:
        # set -euo pipefail is the only thing between a failed build and a pinned
        # digest for an image that was never pushed
        self.assertIn("set -euo pipefail", self.source)
        self.assertEqual(self.source.count("set -euo pipefail"), 1)


class DockerfileBuildArgTest(unittest.TestCase):
    """The build args only matter if the image turns them into what it reports."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dockerfile = (SCRIPT.parent.parent / "Dockerfile").read_text(encoding="utf-8")

    def test_every_arg_the_publish_script_passes_is_declared(self) -> None:
        declared = set(re.findall(r"^ARG (\w+)", self.dockerfile, re.MULTILINE))
        for arg in ("CINDRAL_VERSION", "CINDRAL_COMMIT", "CINDRAL_BUILT_AT"):
            self.assertIn(arg, declared, f"{arg} is passed at build time but the Dockerfile ignores it")

    def test_each_arg_becomes_the_environment_the_broker_reads(self) -> None:
        # an ARG that is not ENV'd is accepted and ignored, so the image reports
        # the image reports "unknown" again
        envs = set(re.findall(r"^ENV (\w+)=\$(\w+)$", self.dockerfile, re.MULTILINE))
        for arg in ("CINDRAL_VERSION", "CINDRAL_COMMIT", "CINDRAL_BUILT_AT"):
            self.assertIn(
                (arg, arg), envs, f"{arg} is declared as an ARG but never becomes the ENV the broker reads"
            )


if __name__ == "__main__":
    unittest.main()


class ImmutableTagTest(unittest.TestCase):
    """Every published name must identify one build, on both publish paths.

    A bare version tag repoints on every publish, so it looks like a release and
    answers "what is 0.12.0" with "whatever ran last". Two paths push the broker
    image -- the workflow and the device script -- and a rule that holds on only
    one of them is a rule nobody can rely on, so both are asserted here.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.script = SCRIPT.read_text(encoding="utf-8")
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_the_device_script_publishes_no_bare_version_tag(self) -> None:
        tags = re.findall(r'--tag "\$\{IMAGE\}:([^"\\]+)"', self.script)
        self.assertTrue(tags, "the publish script declares no tags at all")
        for tag in tags:
            # both names are derived from a value that carries the commit, so no
            # name written here can be rewritten by a later publish
            self.assertIn(
                tag, ("${TAG}", "${SHA}"),
                f"tag {tag!r} is neither the shared $TAG nor the commit",
            )

    def test_the_workflow_publishes_no_bare_version_tag(self) -> None:
        version = re.search(r'echo "tag=([^"]+)"', self.workflow)
        self.assertIsNotNone(version, "the workflow computes no tag")
        self.assertIn(
            "GITHUB_SHA", version.group(1),
            f"the workflow publishes {version.group(1)!r}, which carries no commit",
        )

    def test_the_two_paths_produce_the_same_readable_name(self) -> None:
        """One rule, not two that happen to agree today.

        The workflow builds the name from the version and the first eight of the
        sha; the script does the same with a substring. If one gains a prefix or a
        separator the readable name stops meaning the same thing on the two paths,
        and an operator comparing a tag in a log to a tag in the registry would be
        comparing two different names.
        """
        workflow_tag = re.search(r'echo "tag=\$\{version\}-([^"]+)\"', self.workflow)
        self.assertIsNotNone(workflow_tag, "the workflow does not build a version-sha name")
        self.assertIn("GITHUB_SHA::8", workflow_tag.group(1))
        # the script states the same name once and uses it everywhere, so the two
        # paths cannot drift by one of them re-deriving it
        self.assertIn('TAG="${VERSION}-${SHA:0:8}"', self.script)

    def test_the_digest_is_read_back_from_the_name_that_was_pushed(self) -> None:
        """The record must name the image this run pushed, not another one.

        The script pushes the version-sha name and then reads the digest back to
        print the authoritative record a deployment pins. When the push moved to an
        immutable name and the read-back kept inspecting the bare version tag, the
        two diverged: the record would have carried the digest of whatever the
        moving tag last held, which is the failure this file exists to prevent,
        arrived at from the other direction.
        """
        pushed = re.findall(r'--tag "\$\{IMAGE\}:([^"\\]+)"', self.script)
        inspected = re.findall(r'imagetools inspect "\$\{IMAGE\}:([^"\s]+)"', self.script)
        self.assertTrue(inspected, "the script never reads a pushed digest back")
        for name in inspected:
            self.assertIn(
                "${TAG}", name,
                f"the read-back inspects {name!r}, which is not the name built once as $TAG",
            )
        # and every reference is the single TAG, never a name derived a second time
        self.assertIn('TAG="${VERSION}-${SHA:0:8}"', self.script)
        for name in pushed + inspected:
            self.assertNotIn("${VERSION}", name.replace("${VERSION}-", "", 1),
                             f"{name!r} rebuilds the readable name instead of using $TAG")

    def test_the_record_names_the_tag_it_pushed(self) -> None:
        """The printed line is what a human copies into a deployment change.

        It carried the version and the commit but not the tag, so reconstructing
        which published name produced the digest meant re-deriving it by hand,
        which is the step that goes wrong.
        """
        record = [l for l in self.script.splitlines() if l.startswith('echo "PUSHED ')]
        self.assertEqual(len(record), 1, "the script prints no single authoritative record")
        self.assertIn("tag=${TAG}", record[0])

    def test_the_workflow_checks_the_image_under_the_name_it_pushed(self) -> None:
        """The architecture check and the import smoke test read the pushed name.

        Both read `steps.version.outputs.tag`, so making that name immutable also
        makes them verify the name an operator would pull. A second name used only
        inside the job would leave the published name unverified while the job
        reported success.
        """
        # once to push it and twice to check it afterwards: the architecture check
        # and the import smoke test. The second pushed name is the bare sha, which
        # carries its own commit and needs no separate assertion.
        self.assertEqual(
            self.workflow.count("steps.version.outputs.tag"),
            3,
            "the pushed tag is not read by both post-push checks",
        )
