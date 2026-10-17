"""Which repositories the broker may dispatch to, and which refs of each.

Split out of the policy object because this is the part that answers "is this
request even allowed", and it is answered by parsing a file rather than by
choosing a lane. A dispatch that is not permitted never reaches a decision, so
these rules have no opinion about budgets or runners.
"""
import re
from typing import Any, Mapping

from .github import is_repository_slug
from .models import DispatchTarget

BRANCH_REF_ROOT = "refs/heads/"
# the paths a listed repository may be served on. Both remain independently
# gated: a mode selects which check applies, it does not replace one
SERVED_MODES = ("dispatch", "device", "both")
PUSH_LANES = ("auto", "device", "hosted")
# git rejects these in a ref name, so a ref carrying one is never the branch a
# caller means. Refusing it also stops a branch prefix from matching past itself.
INVALID_REF_FRAGMENTS = ("..", "//", "@{", "\\", " ", "~", "^", ":", "?", "[", "*")


def load_dispatch_targets(data: Mapping[str, Any]) -> tuple[DispatchTarget, ...]:
    """Read the repositories the broker may dispatch to.

    A missing or empty section yields no targets, so an unconfigured policy
    refuses every dispatch instead of allowing every one.
    """
    dispatch = data.get("dispatch", {})
    if not isinstance(dispatch, dict):
        raise ValueError("[dispatch] must be a table")
    entries = dispatch.get("repositories", [])
    if not isinstance(entries, list):
        raise ValueError("[[dispatch.repositories]] must be an array of tables")
    targets = tuple(_parse_target(entry) for entry in entries)
    # compared the way check_dispatch matches, so two entries that differ only
    # in case are a duplicate rather than a second entry that can never match
    seen = {target.repository.lower() for target in targets}
    if len(seen) != len(targets):
        raise ValueError("[[dispatch.repositories]] names a repository more than once")
    return targets


def _parse_target(value: Any) -> DispatchTarget:
    if not isinstance(value, dict):
        raise ValueError("[[dispatch.repositories]] entries must be tables")
    mode = value.get("mode")
    if mode not in SERVED_MODES:
        # no default: a repository that omits its mode is a repository whose
        # reach was never decided, and guessing here grants both paths
        raise ValueError(
            f"dispatch repository must set mode to one of {', '.join(SERVED_MODES)}: {mode!r}"
        )
    repository = value.get("repository")
    # an owner default, written owner/*, so an account's repositories are one
    # line rather than one entry each, and the credential rather than this file
    # is what actually bounds which of them exist
    if isinstance(repository, str) and repository.endswith("/*"):
        owner = repository[:-2]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner or ""):
            raise ValueError(f"dispatch repository default must be owner/*: {repository!r}")
    elif not isinstance(repository, str) or not is_repository_slug(repository):
        raise ValueError(f"dispatch repository must be owner/name or owner/*: {repository!r}")
    workflows = value.get("workflows", [])
    if not isinstance(workflows, list):
        raise ValueError(f"{repository} dispatch workflows must be an array of strings")
    # an absent workflow list would leave every workflow in the repository
    # dispatchable, which is the case the allowlist exists to prevent. A
    # device-only repository cannot dispatch, so naming one would be noise
    if mode in {"dispatch", "both"} and not workflows:
        raise ValueError(f"{repository} must name at least one dispatch workflow")
    for workflow in workflows:
        if not isinstance(workflow, str) or not _is_workflow_name(workflow):
            raise ValueError(f"{repository} dispatch workflow must be a workflow file name: {workflow!r}")
    refs = value.get("refs", [])
    if not isinstance(refs, list):
        raise ValueError(f"{repository} dispatch refs must be an array of strings")
    for pattern in refs:
        _check_ref_pattern(repository, pattern)
    # Omitted rather than defaulted here, so DispatchTarget holds the only
    # definition of what an unstated push_lane means. Passing a literal default
    # as well left two, and changing the field's default changed nothing.
    push_lane = value.get("push_lane")
    if push_lane is not None and push_lane not in PUSH_LANES:
        raise ValueError(
            f"{repository} push_lane must be one of {', '.join(PUSH_LANES)}: {push_lane!r}"
        )
    return DispatchTarget(
        mode=str(mode), repository=repository, workflows=tuple(workflows), refs=tuple(refs),
        **({"push_lane": str(push_lane)} if push_lane is not None else {}),
    )


def _is_workflow_name(workflow: str) -> bool:
    """GitHub dispatches a workflow by its file name, never by a path."""
    return workflow.endswith((".yml", ".yaml")) and "/" not in workflow and not workflow.startswith(".")


def _check_ref_pattern(repository: str, pattern: Any) -> None:
    if not isinstance(pattern, str) or not pattern.startswith(BRANCH_REF_ROOT):
        raise ValueError(f"{repository} dispatch refs must be branches under {BRANCH_REF_ROOT}: {pattern!r}")
    if "*" in pattern and not (pattern.endswith("*") and pattern.count("*") == 1):
        raise ValueError(f"{repository} dispatch ref takes one trailing *: {pattern!r}")
    if pattern.endswith("*") and not pattern.endswith("/*"):
        # a wildcard only ever follows a slash, so the matched prefix always
        # ends at a path boundary: without this, refs/heads/feature* would also
        # authorize refs/heads/featureXYZ, which is not a branch under feature/
        raise ValueError(
            f"{repository} dispatch ref wildcard must follow a slash, as in {pattern[:-1]}/*: {pattern!r}"
        )
    prefix = pattern[:-1] if pattern.endswith("*") else pattern
    if any(fragment in prefix for fragment in INVALID_REF_FRAGMENTS):
        raise ValueError(f"{repository} dispatch ref is not a branch name: {pattern!r}")


def _ref_permitted(candidate: str, patterns: tuple[str, ...]) -> bool:
    if not candidate.startswith(BRANCH_REF_ROOT) or candidate == BRANCH_REF_ROOT:
        return False
    if any(fragment in candidate for fragment in INVALID_REF_FRAGMENTS):
        return False
    for pattern in patterns:
        if pattern.endswith("*"):
            if candidate.startswith(pattern[:-1]):
                return True
        elif candidate == pattern:
            return True
    return False
