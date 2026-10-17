"""Which build is running, from metadata fixed at image build time.

The values come from the environment the image was built with, not from a file
in the image. A source checkout and a rebuilt image on the same tag would
otherwise report whatever the file said, which is the one reading that cannot
settle "is the running broker the change I just merged".
"""
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess

# unset in a source checkout, where there is no build to describe
UNKNOWN = "unknown"


def git_prefix(root: str | Path) -> list[str]:
    """The arguments that let a git call read a checkout the process does not own.

    Git refuses a repository owned by another user, and an agent's checkout is
    routinely owned by whoever cloned it while the agent runs as another. The
    refusal is a non-zero status and nothing else, so every caller would read it
    as "the answer is unavailable" and conclude the worst: that the broker's
    commit is missing, which stops a healthy device from claiming work and tells
    the operator to run a `git fetch` that is refused the same way.

    Naming the directory is scoped to one invocation, so nothing is written to a
    config file, and it names this repository alone rather than every one. It
    goes before the subcommand, which is the only position git accepts.

    An unresolvable path yields nothing rather than a guess: there is no
    checkout to name, and a wrong name is a refusal with extra steps.
    """
    try:
        return ["-c", f"safe.directory={Path(root).resolve()}"]
    except (OSError, RuntimeError, ValueError):
        return []


@dataclass(frozen=True)
class BuildInfo:
    version: str = UNKNOWN
    commit: str = UNKNOWN
    built_at: str = UNKNOWN

    def as_dict(self) -> dict[str, str]:
        return {"version": self.version, "commit": self.commit, "built_at": self.built_at}


def current_build() -> BuildInfo:
    return BuildInfo(
        version=os.environ.get("CINDRAL_VERSION") or UNKNOWN,
        commit=os.environ.get("CINDRAL_COMMIT") or UNKNOWN,
        built_at=os.environ.get("CINDRAL_BUILT_AT") or UNKNOWN,
    )


def checkout_build(root: str | Path = ".") -> BuildInfo:
    """The build a source checkout is, for an agent that has no image metadata.

    A device agent runs from a checkout on the machine, so `CINDRAL_COMMIT` is
    unset and `current_build()` answers `unknown` for everything. That is not a
    cosmetic gap: a device behind the broker claims a job, runs it, and reports a
    verdict produced by code that predates the feature the verdict depends on. The
    broker cannot see that, and neither can the operator. Naming the commit is
    what makes it visible, and the version falls back to the file the image would
    have read, so a checkout reports the same version string as a build of it.

    Unresolvable is `unknown`, not an error: a device with no git, or a checkout
    with no commits, still has to be able to announce itself.
    """
    version = UNKNOWN
    version_file = Path(root) / "VERSION"
    try:
        if version_file.is_file():
            version = version_file.read_text(encoding="utf-8").strip() or UNKNOWN
    except OSError:
        pass
    commit = UNKNOWN
    built_at = UNKNOWN
    try:
        # one call, because a shallow or detached checkout may have no ref to
        # name and the commit is the fact that matters
        shown = subprocess.run(
            # `git_prefix` names this checkout for the call, because git refuses
            # a repository the process does not own and the refusal is silent
            # here: the build reports `unknown` and the staleness check this
            # feeds is inert for exactly the deployments that need it.
            ["git", *git_prefix(root), "log", "-1", "--format=%H%n%cI"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if shown.returncode == 0:
            head, _, when = shown.stdout.strip().partition("\n")
            if head:
                commit = head.strip() or UNKNOWN
            built_at = when.strip() or UNKNOWN
    except (OSError, subprocess.SubprocessError):
        pass
    return BuildInfo(version=version, commit=commit, built_at=built_at)


def build_labels(info: BuildInfo | None = None) -> str:
    """Prometheus labels naming the build, for grouping and dashboards."""
    build = info or current_build()
    return f'version="{escape(build.version)}",commit="{escape(build.commit)}"'


def escape(value: str) -> str:
    """Make an arbitrary value safe inside a Prometheus label.

    Build metadata is set by the build, but a checkout that exports a value by
    hand should not be able to inject a quote or a newline into the exposition.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

