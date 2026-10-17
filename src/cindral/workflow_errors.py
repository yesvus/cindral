"""What a workflow can ask for that the broker cannot give, and how it says so.

Named apart from the reader because two modules raise these and one of them
reads workflows: the errors are the shared vocabulary between the two, so they
belong where both can import them without either importing the other.
"""
from .contract import ContractError


class WorkflowError(ContractError):
    """Raised when a GitHub Actions workflow cannot be parsed or matched."""


class WorkflowNoMatch(WorkflowError):
    """No committed workflow declares this event, whatever the ref.

    The dispatch adapter is the case this exists for: it is `workflow_dispatch`
    only, so a repository that has it has nothing to run for a push, and the
    lockfile or a committed contract is the right answer. A workflow that
    *declares* the event but filters this ref out is not this, and raises
    `WorkflowError` instead, because a branch filter is the repository saying
    "not here" rather than saying nothing.
    """


class WorkflowUnsupported(WorkflowError):
    """A valid workflow asking for something Cindral does not run yet.

    A subclass, so existing `WorkflowError` handlers still catch it, and a
    repository with a committed contract falls back only for this class.
    """


class WorkflowAmbiguous(WorkflowError):
    """More than one committed workflow job matches, and none is preferred.

    Not a malformed workflow and not a capability gap, so a repository that
    triggers two workflows on the same event was refused with nothing to fall
    back to. A push carries no job name, so the message's "specify an
    unambiguous target job" cannot be followed from where the push arrives.

    Its own class because a bare `WorkflowError` is also how a parse failure
    arrives, and that must keep propagating: a contract must not answer a
    workflow it could not read.
    """
