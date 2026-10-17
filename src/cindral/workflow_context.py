"""The GitHub Actions context one direct run resolves, and the names it refuses.

A direct job is queued rather than dispatched from a GitHub run, so the values a
step reads are synthesized here from the queue record. A field the record does not
carry is refused by name before the run starts, rather than arriving in the step as
an empty string that reads like an answer.
"""
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .act import CONTAINER_REPO
from .agent import JobSpec
from .contract import ContractError, Service

EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
# a bare context path: the only shape a translated step can resolve
PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*")
# the same shape read loosely, so a path is still recognized where the engine,
# not this module, evaluates the function call or comparison around it
TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_-]+)*")
# a quoted literal compares as text, so its contents are not a context read
QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
# an index names a field the dotted path may not, and the engine resolves one it
# cannot find to an empty string, so an index is read as the path it stands for
INDEX = re.compile(r"\[\s*['\"]?(?P<key>[A-Za-z0-9_.-]+)['\"]?\s*\]")
PULL_NUMBER = re.compile(r"(\d+)/")
BRANCH_PREFIX = "refs/heads/"
PULL_PREFIX = "refs/pull/"
# the contexts the engine evaluates from the workflow file and the container it
# creates, neither of which a translated run-only step has
ENGINE_ROOTS = frozenset({"job", "runner", "steps", "strategy"})
# the contexts no direct run can supply: the record carries no secret, no matrix
# combination, and no other job's outputs
UNSUPPLIED_ROOTS = frozenset({"inputs", "matrix", "needs", "secrets", "vars"})


class ContextError(ContractError):
    """Raised when a step reads context the run does not carry."""


@dataclass(frozen=True)
class RunContext:
    """What one queued run resolves, from its record and the event it was queued for."""

    event_name: str
    event: dict[str, Any]
    env: dict[str, str] = field(default_factory=dict)
    workflow: str = ""
    job_id: str = ""

    @classmethod
    def from_job(
        cls,
        job: JobSpec,
        event_name: str,
        workflow: str = "",
        job_id: str = "",
        env: Mapping[str, str] | None = None,
    ) -> "RunContext":
        return cls(
            event_name=event_name,
            event=create_event_payload(job, event_name),
            env=dict(env or {}),
            workflow=workflow,
            job_id=job_id,
        )

    def value(self, path: str) -> str | None:
        """The value a context path resolves to, or None when the run has none."""
        if path.startswith("github.event."):
            return _scalar(_lookup(self.event, path.removeprefix("github.event.")))
        if path.startswith("github."):
            return self._github(path)
        if path.startswith("env."):
            return self.env.get(path.removeprefix("env.")) or None
        return None

    def expand(self, text: str, label: str) -> str:
        """The step text with the context it reads substituted into it.

        A value is inserted raw, as GitHub inserts one, so a command written
        against `github.ref` behaves here as it does there. The values come from
        the queue record, whose ref the broker takes from a signed webhook and
        whose trust gate already admitted the repository, so substituting one adds
        no reach the same commit's own run steps did not already have.
        """
        def substitute(match: re.Match[str]) -> str:
            path = match.group(1).strip()
            if not PATH.fullmatch(path):
                raise ContextError(f"{label} reads expression '{path}', which this run cannot evaluate")
            value = self.value(path)
            if value is None:
                raise ContextError(f"{label} reads context '{path}' that this run does not carry")
            return value

        return EXPRESSION.sub(substitute, text)

    def expand_env(self, env: Mapping[str, str], label: str) -> dict[str, str]:
        """The environment the job declares, with the context it reads substituted."""
        return {name: self.expand(value, f"{label} env '{name}'") for name, value in env.items()}

    def require_resolvable(self, text: str, label: str) -> None:
        """Refuse context the engine would resolve to nothing, before the run starts.

        The engine evaluates the expression itself, so a function call or a
        comparison is left to it. A context path is decided here instead: the
        payload is the only source the engine has.
        """
        for expression in EXPRESSION.findall(text):
            self.require_expression(expression, label)

    def require_expression(self, expression: str, label: str) -> None:
        """The same refusal for a value that is already an expression, a step `if:`."""
        readable = readable_expression(expression)
        if "[" in readable:
            raise ContextError(f"{label} reads context by index, which this run does not carry")
        for token in TOKEN.findall(readable):
            root, _, rest = token.partition(".")
            if root in UNSUPPLIED_ROOTS:
                raise ContextError(f"{label} reads context '{token}' that this run does not carry")
            if not rest or root in ENGINE_ROOTS:
                continue
            if self.value(token) is None:
                raise ContextError(f"{label} reads context '{token}' that this run does not carry")

    def _github(self, path: str) -> str | None:
        repository = self.event.get("repository") or {}
        ref = str(self.event.get("ref", ""))
        ref_name, ref_type = _ref_parts(ref)
        fields = {
            "github.event_name": self.event_name,
            "github.job": self.job_id,
            "github.ref": ref,
            "github.ref_name": ref_name,
            "github.ref_type": ref_type,
            # the executor mounts the checkout where the engine works, so both
            # paths read the same workspace
            "github.workspace": CONTAINER_REPO,
            "github.repository": str(repository.get("full_name", "")),
            "github.repository_owner": str((repository.get("owner") or {}).get("login", "")),
            "github.sha": str(self.event.get("sha", "")),
            "github.workflow": self.workflow,
        }
        # an absent value is not an empty one: the run does not carry the field
        return fields.get(path) or None


def run_context(job: JobSpec, workflow: str, job_id: str, env: Mapping[str, str]) -> "RunContext":
    """The context one workflow job resolves, with its environment values resolved.

    An environment value is resolved before a step reads it, so a step reads the
    value the run declares rather than the expression behind it.
    """
    declared = RunContext.from_job(job, event_name_for(job), workflow=workflow, job_id=job_id)
    return replace(declared, env=declared.expand_env(env, f"job '{job_id}'"))


def resolve_services(context: RunContext, services: tuple[Service, ...], native: bool) -> tuple[Service, ...]:
    resolved = []
    for service in services:
        label = f"service '{service.name}'"
        if native:
            image = context.expand(service.image, f"{label} image")
            env = context.expand_env(service.env, label)
        else:
            context.require_resolvable(service.image, f"{label} image")
            for name, value in service.env.items():
                context.require_resolvable(value, f"{label} env '{name}'")
            image, env = service.image, service.env
        resolved.append(replace(service, image=image, env=env))
    return tuple(resolved)


def event_name_for(job: JobSpec) -> str:
    """The Actions event a direct job is treated as, from the ref it was queued for."""
    return "pull_request" if job.ref.startswith(("refs/pull/", "pull/")) else "push"


def create_event_payload(job: JobSpec, event_name: str) -> dict[str, Any]:
    """The push or pull_request event a direct run is read as.

    Only what the queue record carries is here: the repository, the ref, and the
    commit the job runs.
    """
    owner, _, name = job.repository.partition("/")
    payload: dict[str, Any] = {
        "repository": {
            "full_name": job.repository,
            "name": name or job.repository,
            "owner": {"login": owner},
        },
        "ref": _qualified_ref(job.ref),
        "sha": job.sha,
    }
    if event_name == "pull_request" or job.ref.startswith(("refs/pull/", "pull/")):
        number = _pull_number(job.ref)
        # the engine reads the number at the top level to name the merge ref
        payload["number"] = number
        payload["pull_request"] = {"number": number, "head": {"sha": job.sha}}
    else:
        # the push event carries the commit it moved to, which is where a reader
        # of the event looks for the sha
        payload["after"] = job.sha
    return payload


def _qualified_ref(ref: str) -> str:
    """The full git ref, which the agent accepts written either way and a step does not."""
    if ref.startswith("refs/"):
        return ref
    if ref.startswith("pull/"):
        return f"{PULL_PREFIX}{ref.removeprefix('pull/')}"
    return f"{BRANCH_PREFIX}{ref}"


def _ref_parts(ref: str) -> tuple[str, str]:
    """The short name and the kind of a ref, as a step reads them.

    A merge ref is a branch as far as a step is concerned, and its short name is
    the pull request number with the word merge.
    """
    for prefix, kind in (("refs/tags/", "tag"), (PULL_PREFIX, "branch"), (BRANCH_PREFIX, "branch")):
        if ref.startswith(prefix):
            return ref.removeprefix(prefix), kind
    return ref.removeprefix("refs/"), "branch"


def _pull_number(ref: str) -> int:
    found = PULL_NUMBER.match(_qualified_ref(ref).removeprefix(PULL_PREFIX))
    if found is None:
        raise ContextError(f"ref '{ref}' carries no pull request number to read")
    return int(found.group(1))


def readable_expression(expression: str) -> str:
    """The paths an expression names, as the engine reads them.

    An index stands for the field it names, because the engine resolves one it
    cannot find to an empty string. A quoted literal is text, so its contents
    are not a context read.
    """
    return QUOTED.sub("", INDEX.sub(r".\g<key>", expression))


def _lookup(event: dict[str, Any], path: str) -> Any:
    value: Any = event
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _scalar(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    return None
