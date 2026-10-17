"""Derive a CI contract from a repository with no committed CI definition."""
import json
import tomllib
from collections.abc import Callable
from pathlib import Path

from .actions import (
    WorkflowAmbiguous,
    WorkflowError,
    WorkflowNoMatch,
    WorkflowUnsupported,
    resolve_workflow_contract,
    workflow_files,
)
from .agent import JobSpec
from .contract import CONTRACT_PATH, Contract, ContractError


def resolve_contract(repo: Path, job: JobSpec, note: Callable[[str], None] | None = None) -> Contract:
    """The CI this repository runs, preferring its own workflow over a contract.

    A committed `.github/workflows/*.yml` is what a maintainer, and any agent
    asked to add CI, writes first, so it is read first. Preferring the contract
    unconditionally meant a repository that committed both ran the contract and
    its workflow was dead weight, which is how a second CI definition spread to
    repositories that never needed one.

    The contract is the escape hatch for what no single workflow job expresses
    translatably here: `defaults:` step defaults, a runner that needs a host
    daemon, service containers, and a nested toolchain reached through that
    daemon. It is consulted only when the workflow asks for one of those, which
    raises `WorkflowUnsupported`, and it is also consulted when nothing the
    repository committed handles the event at all. A workflow that is broken
    YAML, a dispatch that names a job no workflow defines, and a workflow that
    declares the event but filters out this ref are all mistakes or decisions to
    report rather than gaps to route around, and none of them is caught here.
    """
    contract_file = repo / CONTRACT_PATH
    if contract_file.is_file() and job.command and not _has_workflow(repo):
        # a dispatch naming a job on a repository that committed only a
        # contract: there is no workflow for the target to name, and reporting
        # a missing `.cindral/ci.toml` while it is right there is worse than
        # running it
        return Contract.parse(contract_file.read_text())
    if not job.command and _contract_declares_itself_ci(contract_file):
        # Checked before the workflow branch, not after it, so a repository whose
        # release workflows also trigger on the pushed event reaches the contract
        # instead of failing as ambiguous: that ambiguity is a plain WorkflowError
        # which none of the fallbacks below catch.
        #
        # Scoped to an event with no named job. A dispatch that names a job is
        # asking the broker to run a specific workflow job, and a repository that
        # keeps a workflow_dispatch adapter still needs that path; a contract
        # would silently answer for a job it never declared.
        return Contract.parse(contract_file.read_text())
    ambiguous = False
    if job.command or _has_workflow(repo):
        try:
            return resolve_workflow_contract(repo, job)
        except WorkflowNoMatch as exc:
            # Nothing the repository committed handles this event, which is not
            # a broken CI definition but an absent one. Fall through to the
            # contract or the lockfile, and say so: a run that quietly reports
            # a fraction of what the repository declares is the thing this
            # whole path exists to prevent.
            if note is not None:
                source = CONTRACT_PATH if contract_file.is_file() else "the inferred commands"
                note(f"cindral: warning: {exc}\n")
                note(f"cindral: warning: no committed workflow handles this event; running {source}\n")
        except WorkflowAmbiguous as exc:
            # More than one workflow triggers on this event, which is ordinary for a
            # repository with a release workflow beside its CI one, and a push carries
            # no job name to disambiguate with, so refusing leaves the maintainer with
            # nothing. Inference answers it instead.
            #
            # Inference and not the contract: a contract only answers when it declares
            # itself CI, which it has already had its chance to do above. Falling back
            # to a contract that never claimed itself would hand a push to a file
            # nobody said was the CI definition, which is the guard the self-declaration
            # exists to hold.
            ambiguous = True
            if note is not None:
                note(f"cindral: warning: {exc}\n")
                note("cindral: warning: running the inferred commands; name one workflow job to run instead\n")
        except WorkflowUnsupported as exc:
            # the workflow asked for something this build cannot do, and the
            # repository carries a contract that says what to run instead. A
            # parse failure is not caught here: that is a mistake to report,
            # and a contract must not answer it.
            if not contract_file.is_file():
                raise
            if note is not None:
                note(f"cindral: warning: {exc}\n")
                note(f"cindral: warning: running {CONTRACT_PATH} instead of the committed workflow\n")
            return Contract.parse(contract_file.read_text())
    if contract_file.is_file() and not ambiguous:
        return Contract.parse(contract_file.read_text())
    return infer_contract(repo)


def _has_workflow(repo: Path) -> bool:
    return bool(workflow_files(repo))


def infer_contract(repo: Path) -> Contract:
    pnpm = repo / "pnpm-lock.yaml"
    uv = repo / "uv.lock"
    if pnpm.is_file() and uv.is_file():
        raise ContractError("both pnpm-lock.yaml and uv.lock exist; commit .cindral/ci.toml to choose CI commands")
    if pnpm.is_file():
        manifest = repo / "package.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ContractError("pnpm-lock.yaml needs a readable package.json with a ci script") from exc
        if not isinstance(data, dict) or not isinstance(data.get("scripts"), dict) or not isinstance(data["scripts"].get("ci"), str) or not data["scripts"]["ci"].strip():
            raise ContractError("pnpm-lock.yaml needs a package.json ci script; commit .cindral/ci.toml for other commands")
        return Contract(image="node:22-bookworm", steps=("corepack enable && pnpm install --frozen-lockfile", "pnpm run ci"))
    if uv.is_file():
        if not (repo / "pyproject.toml").is_file():
            raise ContractError("uv.lock needs pyproject.toml; commit .cindral/ci.toml for other commands")
        if (repo / "scripts" / "check.py").is_file():
            return Contract(image="ghcr.io/astral-sh/uv:python3.12-bookworm", steps=("uv sync --frozen", "uv run python scripts/check.py"))
        raise ContractError("uv.lock needs scripts/check.py; commit .cindral/ci.toml for other commands")
    raise ContractError("no CI definition found; commit .cindral/ci.toml, a matching .github/workflows file, or a supported lockfile and CI script")


def _contract_declares_itself_ci(contract_file: Path) -> bool:
    """Whether a committed contract claims to be this repository's CI.

    Read as a table rather than by parsing the whole contract: this only needs one
    boolean, and a full parse would mean reading the file twice on the hot path
    and turning a missing required section into a False that silently sends the
    push down the workflow branch instead of reporting the broken file.

    A file that is not valid TOML answers False as well. That is deliberate: the
    paths that already know how to explain a broken contract still run, and this
    guard exists only to notice an opt-in that is present.
    """
    if not contract_file.is_file():
        return False
    try:
        data = tomllib.loads(contract_file.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        return False
    section = data.get("ci")
    return isinstance(section, dict) and section.get("contract_only") is True
