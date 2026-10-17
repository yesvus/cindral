"""The toolchain a setup action installs, read from the workflows that use it.

The workflow bodies are the ones the fleet repositories commit, taken from the
files rather than retyped, because the versions they pin and the order they
pin them in are the whole question here.
"""
import json
import platform
import subprocess
import tempfile
import unittest
from pathlib import Path

from cindral.actions import WorkflowUnsupported, resolve_workflow_contract
from cindral.agent import JobSpec
from cindral.discovery import parse_workflow
from cindral.toolchain import _go_commands, _node_commands, toolchain_for


class ToolchainTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.workflows = self.repo / ".github" / "workflows"
        self.workflows.mkdir(parents=True)

    def write(self, name: str, body: str) -> Path:
        path = self.workflows / name
        path.write_text(body, encoding="utf-8")
        return path

    def _require_a_supported_architecture(self) -> None:
        """These tests exec the installer, which reads the real `uname -m`.

        On an architecture the installer refuses, the command exits 1 before it
        reaches the guard under test, so a contributor on such a host would see a
        red suite that is the platform and not the change. Skipped with the
        reason stated rather than passed on a technicality.
        """
        machine = platform.machine()
        if machine not in {"x86_64", "aarch64", "arm64"}:
            self.skipTest(f"the installer supports no release for {machine}")

    def contract_for(self, body: str, *, job_id: str = "ci", ref: str = "refs/heads/main", target: str = ""):
        self.write("ci.yml", body)
        spec = JobSpec("job1", "org/repo", "a" * 40, ref, (target,) if target else (), 3600)
        return resolve_workflow_contract(self.repo, spec)


class GoSetupTest(ToolchainTestCase):
    """`example-org/service`, whose go.mod names the version the setup step installs."""

    WORKFLOW = """
name: ci
on:
  pull_request:
  push:
    branches: [main]
jobs:
  check:
    # The example repository targets a self-hosted arm64 runner and its own image.
    runs-on: [self-hosted, Linux, ARM64, fluid]
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4
      - uses: actions/setup-go@40f1582b2485089dde7abd97c1529aa768e1baff # v5
        with:
          go-version-file: go.mod
      - run: make check
"""
    GO_MOD = "module github.com/example-org/service\n\ngo 1.27\n\nrequire golang.org/x/sys v0.48.0\n"

    def test_the_version_is_read_out_of_the_file_the_step_names(self) -> None:
        (self.repo / "go.mod").write_text(self.GO_MOD, encoding="utf-8")
        workflow = parse_workflow(self.write("ci.yml", self.WORKFLOW), self.repo)
        install = toolchain_for(workflow.jobs["check"].steps[1], self.repo)
        self.assertEqual(install.tool, "go")
        self.assertEqual(install.version, "1.27")

    def test_the_install_names_the_line_the_file_declares(self) -> None:
        # a bare `go 1.27` names no archive of its own, so the command resolves
        # the newest release of that line rather than fetching a version that
        # was never published
        (self.repo / "go.mod").write_text(self.GO_MOD, encoding="utf-8")
        workflow = parse_workflow(self.write("ci.yml", self.WORKFLOW), self.repo)
        install = toolchain_for(workflow.jobs["check"].steps[1], self.repo)
        self.assertIn("go1\\.27\\.[0-9]+", " ".join(install.commands))
        self.assertIn("https://go.dev/dl/", " ".join(install.commands))

    def test_a_line_that_names_no_release_is_named_rather_than_fetched(self) -> None:
        """The empty-lookup case, which the URL hides.

        A two-component `1.27` resolves its patch from the release index. When
        the index names no such release the substitution is empty, and the
        download URL becomes one with no version in it, whose 404 names a file
        rather than the line that was asked for. The line is named instead.
        """
        (self.repo / "go.mod").write_text(self.GO_MOD, encoding="utf-8")
        workflow = parse_workflow(self.write("ci.yml", self.WORKFLOW), self.repo)
        commands = " ".join(toolchain_for(workflow.jobs["check"].steps[1], self.repo).commands)
        self.assertIn("no Go release published for line 1.27", commands)
        # and the two-component version never builds the URL itself, which is
        # the defect the review named: an archive URL needs a patch release
        self.assertNotIn("dl/go1.27.linux", commands)

    def test_an_exact_version_still_names_its_own_archive(self) -> None:
        (self.repo / ".go-version").write_text("1.26.3\n", encoding="utf-8")
        workflow = parse_workflow(self.write(
            "ci.yml", self.WORKFLOW.replace("go-version-file: go.mod", "go-version-file: .go-version")
        ), self.repo)
        commands = " ".join(toolchain_for(workflow.jobs["check"].steps[1], self.repo).commands)
        self.assertIn("https://go.dev/dl/go1.26.3.linux-$g.tar.gz", commands)
        self.assertNotIn("no Go release published", commands)

    def _run_guard(self, index_body: str) -> tuple[int, str]:
        """Run the generated guard for real, against a stubbed release index.

        Asserting on the command text cannot see a shell-precedence mistake, and
        the mistake that matters here is one: `a || b; exit 1` parses as
        `(a || b)` then an unconditional `exit 1`, so a line that resolved
        perfectly well still failed the step. Only running it tells the two
        cases apart.
        """
        self._require_a_supported_architecture()
        stub = self.repo / "stub"
        stub.mkdir(exist_ok=True)
        (stub / "curl").write_text(
            "#!/bin/sh\n" + "cat <<'JSON'\n" + index_body + "\nJSON\n", encoding="utf-8"
        )
        (stub / "curl").chmod(0o755)
        guard = _go_commands("1.27")[1]
        script = self.repo / "guard.sh"
        script.write_text(guard, encoding="utf-8")
        result = subprocess.run(
            ["/bin/sh", str(script)],
            capture_output=True, text=True, timeout=30,
            env={"PATH": f"{stub}:/usr/bin:/bin", "HOME": str(self.repo)},
        )
        return result.returncode, result.stdout + result.stderr

    def test_a_line_the_index_answers_for_installs(self) -> None:
        code, output = self._run_guard('[{"version": "go1.27.5"}]')
        self.assertEqual(code, 0, f"a resolved line failed the step: {output}")

    def test_a_line_no_release_answers_for_names_the_line(self) -> None:
        code, output = self._run_guard('[{"version": "go1.26.4"}]')
        self.assertNotEqual(code, 0, "an unresolved line was reported as installed")
        self.assertIn("no Go release published for line 1.27", output)

    def test_the_release_lookup_uses_an_extended_pattern(self) -> None:
        # in a basic pattern `{1,2}` is a literal brace, so the lookup matched
        # nothing and the download URL lost its version
        (self.repo / "go.mod").write_text(self.GO_MOD, encoding="utf-8")
        workflow = parse_workflow(self.write("ci.yml", self.WORKFLOW), self.repo)
        install = toolchain_for(workflow.jobs["check"].steps[1], self.repo)
        self.assertIn("grep -oE", " ".join(install.commands))

    def test_a_go_version_file_that_is_not_a_module_is_read_as_a_version(self) -> None:
        # `.go-version` and `go.env` hold the version alone rather than a
        # directive, and the action reads both
        (self.repo / ".go-version").write_text("1.26.3\n", encoding="utf-8")
        workflow = parse_workflow(self.write(
            "ci.yml", self.WORKFLOW.replace("go-version-file: go.mod", "go-version-file: .go-version")
        ), self.repo)
        install = toolchain_for(workflow.jobs["check"].steps[1], self.repo)
        self.assertEqual(install.version, "1.26.3")
        # an exact version names its own archive, so nothing has to be resolved
        self.assertIn("https://go.dev/dl/go1.26.3.linux-$g.tar.gz", " ".join(install.commands))

    def test_a_cache_input_on_an_action_that_declares_none_is_refused(self) -> None:
        # pnpm/action-setup has no cache input, so honouring one would accept a
        # step GitHub itself rejects
        workflow = parse_workflow(self.write("ci.yml", """
name: ci
on: [push]
jobs:
  check:
    runs-on: ubuntu-24.04
    steps:
      - uses: pnpm/action-setup@v6
        with:
          cache: pnpm
"""), self.repo)
        with self.assertRaises(WorkflowUnsupported) as cm:
            toolchain_for(workflow.jobs["check"].steps[0], self.repo)
        self.assertIn("cache", str(cm.exception))

    def test_a_version_file_outside_the_repository_is_refused_by_name(self) -> None:
        # the name comes from the workflow, so without this a repository could
        # point the read at any file the broker can open. A `uses:` reaching
        # outside the repository is refused by the same rule.
        (self.repo / "go.mod").write_text(self.GO_MOD, encoding="utf-8")
        workflow = parse_workflow(self.write(
            "ci.yml", self.WORKFLOW.replace("go-version-file: go.mod", "go-version-file: /etc/hostname")
        ), self.repo)
        with self.assertRaises(WorkflowUnsupported) as cm:
            toolchain_for(workflow.jobs["check"].steps[1], self.repo)
        self.assertIn("outside the repository", str(cm.exception))

    def test_a_version_file_reaching_out_by_traversal_is_refused_by_name(self) -> None:
        (self.repo / "go.mod").write_text(self.GO_MOD, encoding="utf-8")
        workflow = parse_workflow(self.write(
            "ci.yml", self.WORKFLOW.replace("go-version-file: go.mod", "go-version-file: ../go.mod")
        ), self.repo)
        with self.assertRaises(WorkflowUnsupported) as cm:
            toolchain_for(workflow.jobs["check"].steps[1], self.repo)
        self.assertIn("outside the repository", str(cm.exception))

    def test_a_version_file_that_is_not_utf8_is_named_not_crashed(self) -> None:
        """A `UnicodeDecodeError` here costs the same fallback as the loop above.

        It is a `ValueError` and not an `OSError`, so it escaped the handler on
        the very statement after the one the last fix hardened, and the job died
        as an unexpected executor failure instead of reaching its contract.
        """
        (self.repo / "go.mod").write_bytes(b"go 1.27\n\xff\xfe latin-1\n")
        workflow = parse_workflow(self.write("ci.yml", self.WORKFLOW), self.repo)
        with self.assertRaises(WorkflowUnsupported) as cm:
            toolchain_for(workflow.jobs["check"].steps[1], self.repo)
        self.assertIn("go.mod", str(cm.exception))

    def test_a_version_file_that_is_a_symlink_loop_is_named_not_crashed(self) -> None:
        """A `RuntimeError` here costs the fallback this refusal exists to reach.

        Resolving the path is what catches `../`, and a symlink loop raises
        RuntimeError rather than OSError. Letting it out replaced a clean
        refusal, which falls back to the committed contract, with an executor
        crash that never reaches the fallback. Git stores a self-referencing
        symlink happily, so this is a thing a repository can contain.
        """
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "loop.mod").symlink_to("loop.mod")
        workflow = parse_workflow(self.write(
            "ci.yml", self.WORKFLOW.replace("go-version-file: go.mod", "go-version-file: sub/loop.mod")
        ), self.repo)
        with self.assertRaises(WorkflowUnsupported) as cm:
            toolchain_for(workflow.jobs["check"].steps[1], self.repo)
        self.assertIn("sub/loop.mod", str(cm.exception))

    def test_a_missing_version_file_is_named_rather_than_defaulted(self) -> None:
        workflow = parse_workflow(self.write("ci.yml", self.WORKFLOW), self.repo)
        with self.assertRaises(WorkflowUnsupported) as cm:
            toolchain_for(workflow.jobs["check"].steps[1], self.repo)
        self.assertIn("go.mod", str(cm.exception))

    def test_a_version_file_naming_no_version_is_refused_by_name(self) -> None:
        (self.repo / "go.mod").write_text("module github.com/example-org/service\n", encoding="utf-8")
        workflow = parse_workflow(self.write("ci.yml", self.WORKFLOW), self.repo)
        with self.assertRaises(WorkflowUnsupported) as cm:
            toolchain_for(workflow.jobs["check"].steps[1], self.repo)
        self.assertIn("names no Go version", str(cm.exception))


class NodeSetupTest(ToolchainTestCase):
    def test_a_bare_major_installs_that_release_line(self) -> None:
        # `example-org/web-app` pins 24 with no patch, which names no archive
        install = toolchain_for({"uses": "actions/setup-node@v4", "with": {"node-version": 24}})
        self.assertEqual(install.version, "24")
        self.assertIn("https://nodejs.org/dist/latest-v24.x/", " ".join(install.commands))

    def test_an_exact_version_downloads_that_archive(self) -> None:
        install = toolchain_for({"uses": "actions/setup-node@v4", "with": {"node-version": "22.14.0"}})
        self.assertIn("https://nodejs.org/dist/v22.14.0/", " ".join(install.commands))

    def test_a_version_that_names_no_line_is_refused_by_name(self) -> None:
        with self.assertRaises(WorkflowUnsupported) as cm:
            toolchain_for({"uses": "actions/setup-node@v4", "with": {"node-version": "lts/*"}})
        self.assertIn("lts/*", str(cm.exception))

    def test_a_setup_node_with_no_version_is_refused_by_name(self) -> None:
        with self.assertRaises(WorkflowUnsupported) as cm:
            toolchain_for({"uses": "actions/setup-node@v4"})
        self.assertIn("no node-version", str(cm.exception))

    def test_a_version_written_as_an_expression_is_resolved_first(self) -> None:
        # `example-org/store` reads its version from the workflow environment, and
        # the command has to name the release rather than the expression
        install = toolchain_for(
            {"uses": "actions/setup-node@v7", "with": {"node-version": "${{ env.NODE_VERSION }}"}},
            expand=lambda text, name: "22",
        )
        self.assertEqual(install.version, "22")
        self.assertNotIn("${{", " ".join(install.commands))


class NodeReleaseTest(ToolchainTestCase):
    """The generated installer, run against a stubbed distributor.

    A `major.minor` pin is the shape that was broken: `latest-vMAJOR.x` holds only
    the newest release of the major, so a minor that is no longer current 404s
    for a line that has releases. Resolving it needs the release index, and the
    node index writes its keys without a space after the colon, unlike the Go
    one, so a pattern carried across from there resolves nothing.
    """

    def _checksums(self, version: str) -> str:
        """A shell command printing a checksum line for every architecture.

        One line, because the stub is a `case` arm. Offering every architecture is
        what lets the same test say the same thing on an x86_64 host and this one.
        """
        rows = "".join(
            f"aaaa  node-{version}-linux-{arch}.tar.gz" + chr(92) + "n"
            for arch in ("x64", "arm64", "armv7l", "ppc64le", "s390x")
        )
        return "printf '" + rows + "'"

    def _run(self, command: str, published: bool) -> tuple[int, str]:
        """Run the whole generated step, with the distributor and the host stubbed.

        Every architecture is offered by the checksum stub, so the test says the
        same thing on every machine, and `tar` and `node` are stubbed because the
        step ends in a real extraction otherwise.
        """
        self._require_a_supported_architecture()
        stub = self.repo / "stub"
        stub.mkdir(exist_ok=True)
        checksums = "".join(
            f"aaaa  node-v22.0.0-linux-{arch}.tar.gz\n"
            for arch in ("x64", "arm64", "armv7l", "ppc64le", "s390x")
        )
        (stub / "curl").write_text(
            "#!/bin/sh\n"
            + (f'printf "%s" "{checksums}"\n' if published else "exit 22\n"),
            encoding="utf-8",
        )
        (stub / "curl").chmod(0o755)
        for tool in ("tar", "node"):
            shim = stub / tool
            shim.write_text(f"#!/bin/sh\necho {tool}-stub $2\nexit 0\n", encoding="utf-8")
            shim.chmod(0o755)
        script = self.repo / "node.sh"
        script.write_text(command, encoding="utf-8")
        result = subprocess.run(
            ["/bin/sh", str(script)], capture_output=True, text=True, timeout=30,
            env={"PATH": f"{stub}:/usr/bin:/bin", "HOME": str(self.repo)},
        )
        return result.returncode, result.stdout + result.stderr

    def test_a_node_line_that_has_releases_installs(self) -> None:
        code, output = self._run(_node_commands("22")[1], True)
        # reaching the extraction and then the version check is what "installed"
        # means here, and the stub names each so the output says which ran
        self.assertEqual(code, 0, f"a published line failed the step: {output}")
        self.assertIn("tar-stub -C", output)
        self.assertIn("node-stub", output)

    def test_a_node_line_with_no_release_names_the_line(self) -> None:
        code, output = self._run(_node_commands("99")[1], False)
        self.assertNotEqual(code, 0, "an unpublished line was reported as installed")
        self.assertIn("no Node release published for line 99", output)

    def test_an_exact_version_names_its_own_archive(self) -> None:
        code, output = self._run(_node_commands("22.0.0")[1], True)
        self.assertEqual(code, 0, f"an exact version failed the step: {output}")
        self.assertIn("nodejs.org/dist/v22.0.0/SHASUMS256.txt", _node_commands("22.0.0")[1])

    def test_a_major_minor_pin_resolves_through_the_release_index(self) -> None:
        # `latest-v22.14.x` does not exist, so the directory is not the answer
        command = _node_commands("22.14")[1]
        self.assertIn("index.json", command)
        self.assertNotIn("latest-v22.14.x", command)
        # and the index it reads writes its keys with no space after the colon,
        # which is the whole difference between resolving and not
        self.assertIn('\"version\":\"v22\\.14\\.[0-9]+\"', command)

    def _run_against(self, script_name: str, served: dict[str, str]) -> tuple[int, str, str]:
        """Run a step where the distributor serves only the URLs it is given.

        The stub answers nothing it was not asked to answer, because the real
        distributor does not either: a stub that answers every URL serves
        `latest-v22.14.x`, which does not exist, exactly as well as the index, so
        a test written against it passes with the bug restored.
        """
        self._require_a_supported_architecture()
        stub = self.repo / script_name
        stub.mkdir(exist_ok=True)
        # every request is logged, because serving a URL proves nothing unless
        # a test asserts the URL was asked for. Without that, a step that
        # skipped the download entirely passed.
        asked = self.repo / f"{script_name}.asked"
        lines = ["#!/bin/sh", 'echo "$*" >> ' + str(asked), 'case "$*" in']
        for fragment, body in served.items():
            lines.append(f"*{fragment}*) {body} ;;")
        lines.append("*) echo 404 >&2; exit 22 ;;")
        lines.append("esac")
        (stub / "curl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (stub / "curl").chmod(0o755)
        for tool in ("tar", "node"):
            shim = stub / tool
            shim.write_text(f"#!/bin/sh\necho {tool}-ran\nexit 0\n", encoding="utf-8")
            shim.chmod(0o755)
        script = self.repo / f"{script_name}.sh"
        script.write_text(_node_commands("22.14")[1], encoding="utf-8")
        result = subprocess.run(
            ["/bin/sh", str(script)], capture_output=True, text=True, timeout=30,
            env={"PATH": f"{stub}:/usr/bin:/bin", "HOME": str(self.repo)},
        )
        requests = asked.read_text() if asked.exists() else ""
        return result.returncode, result.stdout + result.stderr, requests

    def test_a_major_minor_line_the_index_answers_for_installs(self) -> None:
        # only the index and the release it names are served, so reaching the
        # download proves the index was read rather than a directory guessed
        # the archive itself is served too, and only for the version the index
        # named, so a download that skipped it fails rather than passing
        code, output, asked = self._run_against("minor", {
            "index.json": """echo '{"version":"v22.14.0"}'""",
            "/v22.14.0/SHASUMS256.txt": self._checksums("v22.14.0"),
        })
        self.assertEqual(code, 0, output)
        self.assertIn("tar-ran", output)
        # and the archive the checksums named is what was fetched, which is the
        # step a download that was dropped entirely would not have taken
        self.assertIn("/v22.14.0/node-v22.14.0-linux-", asked)

    def test_the_stub_refuses_a_url_it_was_not_given(self) -> None:
        """The harness the other tests lean on, checked against itself.

        A stub that answers every URL serves `latest-v22.14.x`, which the real
        distributor 404s, exactly as well as it serves the index. A test written
        against that stub passes with the bug restored, so the stub's refusal is
        what gives the other tests their teeth, and that is worth asserting.
        """
        stub = self.repo / "probe"
        stub.mkdir(exist_ok=True)
        (stub / "curl").write_text(
            '#!/bin/sh\ncase "$*" in '
            '*index.json) echo \'{"version":"v22.14.0"}\' ;; '
            '*/v22.14.0/*) echo served ;; '
            '*) echo 404 >&2; exit 22 ;; esac\n',
            encoding="utf-8",
        )
        (stub / "curl").chmod(0o755)
        for url in ("https://nodejs.org/dist/latest-v22.14.x/SHASUMS256.txt",
                    "https://nodejs.org/dist/v22.14.0/SHASUMS256.txt"):
            result = subprocess.run(
                ["/bin/sh", str(stub / "curl"), "-fsSL", url],
                capture_output=True, text=True, timeout=30,
            )
            if "latest-v22.14" in url:
                self.assertNotEqual(result.returncode, 0, "the guessed directory is served")
            else:
                self.assertEqual(result.returncode, 0, "the real release is refused")

    def test_a_major_minor_line_the_index_does_not_answer_for_names_the_line(self) -> None:
        code, output, _ = self._run_against("minor4", {"index.json": "exit 22"})
        self.assertNotEqual(code, 0)
        self.assertIn("no Node release published for line 22.14", output)

    def test_the_index_resolution_and_the_download_are_one_step(self) -> None:
        # each step runs in its own subshell, so a release resolved by one step
        # is not in scope for the next and the download would fetch an empty URL
        self.assertEqual(len(_node_commands("22.14")), 2)


class PythonSetupTest(ToolchainTestCase):
    def test_the_pinned_minor_is_the_one_installed(self) -> None:
        install = toolchain_for({"uses": "actions/setup-python@v6", "with": {"python-version": "3.12"}})
        self.assertEqual(install.version, "3.12")
        commands = " ".join(install.commands)
        self.assertIn("python3.12 python3.12-venv", commands)
        self.assertIn("/usr/local/bin/python", commands)
        # `python3` points at the version the workflow declared, the way
        # `actions/setup-python` does it and the way node and pnpm already put
        # their own binaries ahead of the image's here. A repository that pins a
        # version and then calls `python3` is asking for the version it named,
        # and silently running a different interpreter is the failure this
        # translation exists to prevent. Pinned so it stays a decision.
        self.assertIn("ln -sf /usr/bin/python3.12 /usr/local/bin/python3", commands)

    def test_a_patch_release_is_refused_rather_than_installed_as_its_minor(self) -> None:
        # apt carries no patch-specific package, so installing 3.12.3 from a
        # `python-version: 3.12.3` would quietly run a different interpreter
        with self.assertRaises(WorkflowUnsupported) as cm:
            toolchain_for({"uses": "actions/setup-python@v6", "with": {"python-version": "3.12.3"}})
        self.assertIn("patch release", str(cm.exception))

    def test_a_version_that_names_no_line_is_refused_by_name(self) -> None:
        with self.assertRaises(WorkflowUnsupported) as cm:
            toolchain_for({"uses": "actions/setup-python@v6", "with": {"python-version": "pypy3"}})
        self.assertIn("pypy3", str(cm.exception))


class PnpmSetupTest(ToolchainTestCase):
    def test_a_pinned_version_is_the_one_installed(self) -> None:
        # `example-org/panel-app` states its version on the step
        install = toolchain_for({"uses": "pnpm/action-setup@v4", "with": {"version": "12.4.1"}})
        self.assertEqual(install.version, "12.4.1")
        self.assertIn("download/v12.4.1/pnpm-linux-$p.tar.gz", " ".join(install.commands))

    def test_a_step_with_no_version_reads_the_repository_field(self) -> None:
        # `example-org/web-app` states it only in package.json, and the field
        # carries an integrity hash that names no release
        (self.repo / "package.json").write_text(
            json.dumps({"name": "leotron-example-org", "packageManager": "pnpm@12.3.4+sha512.961aa41fb077"}),
            encoding="utf-8",
        )
        install = toolchain_for({"uses": "pnpm/action-setup@v4"}, self.repo)
        self.assertEqual(install.version, "12.3.4")
        self.assertIn("download/v12.3.4/", " ".join(install.commands))

    def test_a_step_with_no_version_and_no_field_is_refused_by_name(self) -> None:
        (self.repo / "package.json").write_text(json.dumps({"name": "app"}), encoding="utf-8")
        with self.assertRaises(WorkflowUnsupported) as cm:
            toolchain_for({"uses": "pnpm/action-setup@v4"}, self.repo)
        self.assertIn("no packageManager field", str(cm.exception))

    def test_a_version_that_names_no_release_is_refused_by_name(self) -> None:
        with self.assertRaises(WorkflowUnsupported) as cm:
            toolchain_for({"uses": "pnpm/action-setup@v4", "with": {"version": "latest"}})
        self.assertIn("latest", str(cm.exception))


class SetupInputTest(ToolchainTestCase):
    def test_an_input_cindral_does_not_apply_is_refused_by_name(self) -> None:
        # `architecture` would install a build for a machine the job does not
        # run on, so it is a construct rather than a no-op
        with self.assertRaises(WorkflowUnsupported) as cm:
            toolchain_for({
                "uses": "actions/setup-node@v4",
                "with": {"node-version": 22, "architecture": "x86"},
            })
        self.assertIn("architecture", str(cm.exception))

    def test_a_cache_input_is_accepted_because_cindral_does_that_itself(self) -> None:
        # `cache: pnpm` asks for a store keyed on the lockfile, which the agent
        # restores from that same lockfile
        install = toolchain_for({
            "uses": "actions/setup-node@v4",
            "with": {"node-version": 22, "cache": "pnpm", "cache-dependency-path": "web/pnpm-lock.yaml"},
        })
        self.assertEqual(install.version, "22")


class ToolchainInTheContractTest(ToolchainTestCase):
    """The install is observable in the contract, in the order the steps wrote."""

    WORKFLOW = """
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
jobs:
  quality:
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
      - uses: pnpm/action-setup@v4
        with:
          version: 12.4.1
      - uses: actions/setup-node@v4
        with:
          node-version: 22
          cache: pnpm
      - run: pnpm install --frozen-lockfile
      - run: pnpm run ci
"""

    def test_the_install_lands_in_the_contract_before_the_run_steps(self) -> None:
        contract = self.contract_for(self.WORKFLOW, job_id="quality")
        self.assertEqual(contract.image, "ubuntu:24.04")
        self.assertFalse(contract.docker)
        # one container, so the install is a step of the same run rather than a
        # second execution, and the repository's own steps keep their order
        self.assertEqual(contract.steps[-2:], ("pnpm install --frozen-lockfile", "pnpm run ci"))
        installs = " ".join(contract.steps[:-2])
        self.assertIn("download/v12.4.1/pnpm-linux-$p.tar.gz", installs)
        self.assertIn("https://nodejs.org/dist/latest-v22.x/", installs)
        self.assertLess(
            contract.steps.index("pnpm install --frozen-lockfile"),
            len(contract.steps) - 1,
        )

    def test_each_install_runs_before_the_step_that_uses_the_tool(self) -> None:
        contract = self.contract_for(self.WORKFLOW, job_id="quality")
        pnpm = next(i for i, s in enumerate(contract.steps) if "download/v12.4.1" in s)
        node = next(i for i, s in enumerate(contract.steps) if "nodejs.org" in s)
        install = contract.steps.index("pnpm install --frozen-lockfile")
        self.assertLess(pnpm, install)
        self.assertLess(node, install)

    def test_a_setup_step_no_longer_sends_the_job_to_the_act_engine(self) -> None:
        contract = self.contract_for(self.WORKFLOW, job_id="quality")
        self.assertFalse(contract.docker)
        self.assertNotIn("act", " ".join(contract.steps))


class GoSetupInTheContractTest(ToolchainTestCase):
    def test_a_go_step_is_translated_rather_than_refused(self) -> None:
        (self.repo / "go.mod").write_text("module m\n\ngo 1.27\n", encoding="utf-8")
        contract = self.contract_for("""
name: ci
on: [push]
jobs:
  check:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with:
          go-version-file: go.mod
      - run: make check
""", job_id="check")
        self.assertEqual(contract.steps[-1], "make check")
        self.assertIn("go1\\.27\\.[0-9]+", " ".join(contract.steps))


if __name__ == "__main__":
    unittest.main()
