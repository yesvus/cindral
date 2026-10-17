"""Installing the toolchain a setup action names, in the job container.

One module owns the command each setup action becomes, so the translator asks
this for a step's install and never spells out a download itself. Every command
here was run in the image a `runs-on` label maps to before it was written down.

The `ubuntu:24.04` image Cindral runs carries `apt`, `tar` and `gzip` and
nothing else, so each install brings its own fetcher. A version the action
pins resolves the way the action resolves it: an exact version is downloaded,
and a bare major or minor takes the newest patch of that line from the
distributor's own index, so a workflow pinning `node-version: 24` gets a 24
release rather than a version that does not exist.
"""
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .discovery import WorkflowUnsupported, _inside

__all__ = ["Toolchain", "action_name", "is_setup_action", "toolchain_for"]

SETUP_ACTIONS = frozenset(
    {"actions/setup-go", "actions/setup-node", "actions/setup-python", "pnpm/action-setup"}
)
# The input each action pins its version with. An action with no entry here
# pins nothing, because there is no version of the tool it names.
VERSION_INPUTS = {
    "actions/setup-go": ("go-version", "go-version-file"),
    "actions/setup-node": ("node-version",),
    "actions/setup-python": ("python-version",),
    "pnpm/action-setup": ("version",),
}
# Inputs that ask for a cache keyed on a lockfile. Cindral restores and writes
# those caches itself, from the same lockfile, so accepting the input says the
# same thing the action would have. Anything else is refused by name, because
# dropping an input silently runs something other than what the step asked for.
#
# Scoped to the actions that declare the input, because pnpm/action-setup has no
# cache input at all: honouring one there would accept a step GitHub itself
# rejects, and the run would differ from the workflow for no stated reason.
CACHE_INPUTS = frozenset({"cache", "cache-dependency-path"})
CACHE_ACTIONS = frozenset(
    {"actions/setup-go", "actions/setup-node", "actions/setup-python"}
)
# pnpm/action-setup with no version reads the repository's `packageManager`
# field, which corepack writes as `pnpm@<version>+<integrity>`. The hash names
# no release, so the version ends at the `+` and a prerelease's `-` is kept.
PACKAGE_MANAGER = re.compile(r"^pnpm@(v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)")
# A version input can be an expression, which the run's context resolves before
# the version is read. It is resolved here rather than left in the command,
# because the command names the release to download.
EXPRESSION = re.compile(r"\$\{\{")
# The version specifiers each action accepts, matched against the value the
# workflow pins. A value outside them is refused by name rather than guessed at.
EXACT_VERSION = re.compile(r"^v?\d+(\.\d+)*$")
# A node version is a major, a major.minor, or a major.minor.patch, which is
# what the action accepts and what names either a release line or one release.
RELEASE_LINE = re.compile(r"^v?\d+(\.\d+){0,2}$")
PYTHON_LINE = re.compile(r"^3\.\d+$")
PYTHON_VERSION = re.compile(r"^3\.\d+(\.\d+)?$")
PNPM_VERSION = re.compile(r"^\d+\.\d+\.\d+([-+][0-9A-Za-z.+-]+)?$")
# a `go.mod` or `go.work` names its version in a directive; `.go-version` and
# `go.env` hold the version alone
GO_DIRECTIVE = re.compile(r"^go\s+(v?\d+\.\d+(\.\d+)?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Toolchain:
    """What one setup step installs, and the commands that install it."""

    action: str
    tool: str
    version: str
    commands: tuple[str, ...]


def action_name(step: dict) -> str | None:
    """The action a step names, without its ref, or None when it names none."""
    uses = step.get("uses")
    return str(uses).split("@")[0].strip() if uses is not None else None


def is_setup_action(step: dict) -> bool:
    name = action_name(step)
    return name is not None and name in SETUP_ACTIONS


# Resolves an expression in a version input, naming the input it came from.
Expand = Callable[[str, str], str]


def toolchain_for(
    step: dict, repo: Path | None = None, expand: Expand | None = None
) -> Toolchain | None:
    """The install a setup step asks for, or None when the step names no setup action.

    `repo` is the checkout, which a `go-version-file` and a repository's
    `packageManager` field are read from. `expand` resolves an expression in a
    version input, because the command has to name the release to download.
    """
    name = action_name(step)
    if name is None or name not in SETUP_ACTIONS:
        return None
    inputs = step.get("with") if isinstance(step.get("with"), dict) else {}
    for key in inputs:
        if key not in VERSION_INPUTS[name] and not (
            key in CACHE_INPUTS and name in CACHE_ACTIONS
        ):
            raise WorkflowUnsupported(f"{name} sets input '{key}', which Cindral does not apply")
    if name == "actions/setup-go":
        return _go(inputs, repo, expand)
    if name == "actions/setup-node":
        return _node(inputs, expand)
    if name == "actions/setup-python":
        return _python(inputs, expand)
    return _pnpm(inputs, repo)


def _input(inputs: dict, expand: Expand | None, *names: str) -> str | None:
    for name in names:
        value = inputs.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        if EXPRESSION.search(text):
            if expand is None:
                raise WorkflowUnsupported(f"input '{name}' reads an expression this run cannot resolve")
            text = expand(text, name).strip()
        return text
    return None


def _go(inputs: dict, repo: Path | None, expand: Expand | None) -> Toolchain:
    version = _input(inputs, expand, "go-version")
    if version is None:
        version = _go_version_file(_input(inputs, expand, "go-version-file"), repo)
    if version is None:
        raise WorkflowUnsupported(
            "actions/setup-go sets no go-version, and no go-version-file names one either"
        )
    if not EXACT_VERSION.fullmatch(version):
        raise WorkflowUnsupported(
            f"actions/setup-go pins go-version '{version}', which names no release"
        )
    return Toolchain("actions/setup-go", "go", version.lstrip("v"), _go_commands(version.lstrip("v")))


def _go_version_file(path: str | None, repo: Path | None) -> str | None:
    if path is None:
        return None
    if repo is None:
        raise WorkflowUnsupported(
            f"actions/setup-go reads go-version-file '{path}', which this translation cannot read"
        )
    # The name comes from the workflow, so a repository can point this at any
    # file the broker can read. A `uses:` reaching outside the repository is
    # refused by the same rule, and a version file is no more entitled to it.
    #
    # Resolving before the check is what catches `../`, since the comparison is
    # lexical. A symlink loop raises RuntimeError rather than OSError, and letting
    # that out would replace a clean refusal with an executor crash, costing the
    # committed-contract fallback this refusal exists to reach.
    try:
        target = (repo / path).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkflowUnsupported(
            f"actions/setup-go reads go-version-file '{path}', which could not be resolved: {exc}"
        ) from exc
    if not _inside(target, repo):
        raise WorkflowUnsupported(
            f"actions/setup-go reads go-version-file '{path}', which reaches outside the repository"
        )
    try:
        text = target.read_text(encoding="utf-8")
    # A non-UTF-8 file raises UnicodeDecodeError, which is a ValueError and not
    # an OSError, so it escapes every handler above this one and the job dies as
    # an unexpected executor failure instead of falling back to the committed
    # contract this refusal exists to reach.
    except (OSError, UnicodeError) as exc:
        raise WorkflowUnsupported(
            f"actions/setup-go reads go-version-file '{path}', which could not be read: {exc}"
        ) from exc
    match = GO_DIRECTIVE.search(text)
    if match is not None:
        return match.group(1)
    for line in text.splitlines():
        stripped = line.split("//")[0].strip()
        if stripped and EXACT_VERSION.fullmatch(stripped):
            return stripped
    raise WorkflowUnsupported(
        f"actions/setup-go reads go-version-file '{path}', which names no Go version"
    )


def _node(inputs: dict, expand: Expand | None) -> Toolchain:
    version = _input(inputs, expand, "node-version")
    if version is None:
        raise WorkflowUnsupported("actions/setup-node sets no node-version")
    if not RELEASE_LINE.fullmatch(version):
        raise WorkflowUnsupported(
            f"actions/setup-node pins node-version '{version}', which names no release line"
        )
    return Toolchain("actions/setup-node", "node", version.lstrip("v"), _node_commands(version.lstrip("v")))


def _python(inputs: dict, expand: Expand | None) -> Toolchain:
    version = _input(inputs, expand, "python-version")
    if version is None:
        raise WorkflowUnsupported("actions/setup-python sets no python-version")
    if not PYTHON_VERSION.fullmatch(version):
        raise WorkflowUnsupported(
            f"actions/setup-python pins python-version '{version}', which names no release line"
        )
    if not PYTHON_LINE.fullmatch(version):
        raise WorkflowUnsupported(
            f"actions/setup-python pins python-version '{version}', which names a patch release"
        )
    return Toolchain("actions/setup-python", "python", version, _python_commands(version))


def _pnpm(inputs: dict, repo: Path | None) -> Toolchain:
    version = _input(inputs, None, "version") or _package_manager(repo)
    if version is None:
        raise WorkflowUnsupported(
            "pnpm/action-setup sets no version, and no packageManager field names one either"
        )
    if not PNPM_VERSION.fullmatch(version):
        raise WorkflowUnsupported(f"pnpm/action-setup pins version '{version}', which names no release")
    return Toolchain("pnpm/action-setup", "pnpm", version, _pnpm_commands(version))


def _package_manager(repo: Path | None) -> str | None:
    """The pnpm version a repository declares, which is what the action falls back to."""
    if repo is None:
        return None
    manifest = repo / "package.json"
    try:
        declared = json.loads(manifest.read_text(encoding="utf-8")).get("packageManager")
    except (OSError, ValueError, AttributeError):
        return None
    match = PACKAGE_MANAGER.match(str(declared or ""))
    return match.group(1) if match is not None else None


# Every install runs as root in the job container and writes to the prefix every
# image already has on PATH, because each step is its own subshell and an export
# in one does not reach the next. The architecture is read from the container
# rather than the broker, because the device decides it.
_ARCH = (
    'a=$(uname -m) && case "$a" in x86_64) g=amd64; n=x64; p=amd64 ;; aarch64|arm64) g=arm64; n=arm64; p=arm64 ;; '
    '*) echo "cindral: no release for architecture $a" >&2; exit 1 ;; esac'
)
_FETCH = "apt-get update -qq && apt-get install -y -qq --no-install-recommends ca-certificates curl"
_DEADSNAKES_KEY = "0xF23C5A6CF475977595C89F51BA6932366A755776"
_DEADSNAKES_ARCHIVE = "https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu/"


def _go_commands(version: str) -> tuple[str, ...]:
    # A bare `go 1.27` names the newest patch of that line, which is the release
    # a `go` directive in go.mod asks for; the toolchain publishes one archive
    # per release, so the line is resolved from its own index rather than guessed.
    resolved = version.count(".") != 2
    if not resolved:
        release = f"go{version}"
    else:
        line = re.escape(version)
        release = (
            "$(curl -fsSL 'https://go.dev/dl/?mode=json&include=all' "
            f"| grep -oE '\"version\": \"go{line}\\.[0-9]+\"' | head -1 | cut -d'\"' -f4)"
        )
    commands = [
        _FETCH,
        f'{_ARCH} && curl -fsSL "https://go.dev/dl/{release}.linux-$g.tar.gz" | tar -xz -C /usr/local '
        "&& ln -sf /usr/local/go/bin/go /usr/local/bin/go "
        "&& ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt "
        "&& go version",
    ]
    if resolved:
        # A lookup that matched nothing leaves the URL with no version in it, and
        # the 404 that follows names a missing file rather than the line that was
        # asked for, so the empty result is named here instead.
        # The braces are load-bearing: `a || b; exit 1` parses as `(a || b)` then
        # an unconditional `exit 1`, which would fail the step on a line that
        # resolved perfectly well.
        commands.insert(1, f'{_ARCH} && [ -n "{release}" ] || '
                          '{ echo "cindral: no Go release published for line '
                          f'{version}" >&2; exit 1; }}')
    return tuple(commands)


def _node_commands(version: str) -> tuple[str, ...]:
    # The checksum file beside a release names that release's archive, so the
    # directory a version names also names the file to fetch.
    #
    # Only an exact version has a directory of its own. The distributor publishes
    # `latest-vMAJOR.x`, which holds the newest release of that major and nothing
    # else, so it settles a bare major but fails a `major.minor` whose newest
    # patch is no longer current. That line is only written down in the release
    # index, so it is read from there, the way Go's is. Guessing the directory
    # instead fetched a 404 for a line that has releases.
    if version.count(".") == 2:
        return _node_install(f"v{version}", f"version {version}")
    if "." in version:
        return _node_install_from_index(version)
    return _node_install(f"latest-v{version}.x", f"line {version}")


def _node_install_from_index(version: str) -> tuple[str, ...]:
    """Fetch a `major.minor` line, which only the release index writes down.

    One step, not two, because each step runs in its own subshell: a release
    resolved by one step is not in scope for the next, and the download would
    silently fetch a URL with an empty version in it.
    """
    # The node index writes `"version":"v22.14.0"` with no space after the colon,
    # unlike the go index, so the pattern matches this file's shape and not the
    # other one: a pattern copied across fetches nothing and names the line as
    # unpublished.
    pattern = "v" + r"\.".join(version.split(".")) + r"\.[0-9]+"
    return (
        _FETCH,
        f"{_ARCH} && release=$(curl -fsSL https://nodejs.org/dist/index.json "
        f"| grep -oE '\"version\":\"{pattern}\"' | head -1 | cut -d'\"' -f4) "
        f'&& [ -n "$release" ] || '
        '{ echo "cindral: no Node release published for line ' + version + '" >&2; exit 1; }'
        f' && dir="https://nodejs.org/dist/$release" '
        f'&& file=$(curl -fsSL "$dir/SHASUMS256.txt" '
        "| awk -v a=\"linux-$n.tar.gz\" '$2 ~ a\"$\" {print $2; exit}') "
        f'&& [ -n "$file" ] && curl -fsSL "$dir/$file" '
        "| tar -xz -C /usr/local --strip-components=1 && node --version",
    )


def _node_install(directory: str, label: str) -> tuple[str, ...]:
    """Fetch the release a directory names, or name the {label} that has none.

    One step, and the fetch is its own check: a directory with no release answers
    a 404 whose path names the missing checksum file rather than the version that
    was asked for, which is the same hole the Go lookup closes. A separate guard
    step could not close it, because the release is a literal here and a literal
    is never empty.
    """
    return (
        _FETCH,
        f'{_ARCH} && sums=$(curl -fsSL "https://nodejs.org/dist/{directory}/SHASUMS256.txt") '
        f'|| {{ echo "cindral: no Node release published for {label}" >&2; exit 1; }}; '
        'file=$(printf \'%s\\n\' "$sums" '
        "| awk -v a=\"linux-$n.tar.gz\" '$2 ~ a\"$\" {print $2; exit}') "
        f'&& [ -n "$file" ] && curl -fsSL "https://nodejs.org/dist/{directory}/$file" '
        "| tar -xz -C /usr/local --strip-components=1 && node --version",
    )


def _python_commands(version: str) -> tuple[str, ...]:
    # Ubuntu ships one Python of its own and no other, so a version it does not
    # carry comes from the archive that builds every release for every Ubuntu.
    # The key is pinned by fingerprint, so the archive is trusted on that alone.
    return (
        f"{_FETCH} && apt-get install -y -qq --no-install-recommends gnupg",
        "mkdir -p /etc/apt/keyrings && "
        f"curl -fsSL 'https://keyserver.ubuntu.com/pks/lookup?op=get&search={_DEADSNAKES_KEY}' "
        "-o /etc/apt/keyrings/deadsnakes.asc && "
        '. /etc/os-release && printf "deb [signed-by=/etc/apt/keyrings/deadsnakes.asc] '
        f'{_DEADSNAKES_ARCHIVE} $UBUNTU_CODENAME main\\n" '
        "> /etc/apt/sources.list.d/cindral-deadsnakes.list && apt-get update -qq",
        # Ubuntu marks its own Python externally managed, so pip refuses to
        # install into it without the flag. The job container is the one
        # disposable, and a repository's `python -m pip install` needs pip on
        # the interpreter itself rather than inside a virtual environment.
        f"apt-get install -y -qq --no-install-recommends python{version} python{version}-venv "
        f"&& curl -fsSL https://bootstrap.pypa.io/get-pip.py | python{version} - --break-system-packages "
        # `python3` points at the requested version, deliberately and the way
        # `actions/setup-python` does it. A repository that pins a version and
        # then calls `python3` is asking for the version it named, and the two
        # toolchains above already put their own binaries ahead of the image's
        # for the same reason. The alternative is a `python3` that silently is
        # not the version the workflow declared, which is the failure this
        # translation exists to prevent.
        f"&& ln -sf /usr/bin/python{version} /usr/local/bin/python3 "
        f"&& ln -sf /usr/bin/python{version} /usr/local/bin/python "
        "&& python --version",
    )


def _pnpm_commands(version: str) -> tuple[str, ...]:
    return (
        _FETCH,
        f'{_ARCH} && curl -fsSL "https://github.com/pnpm/pnpm/releases/download/v{version}/pnpm-linux-$p.tar.gz" '
        "| tar -xz -C /usr/local/bin && pnpm --version",
    )
