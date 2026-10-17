"""The one exception the agent's own transport raises.

It lives apart from the client that raises it so the threads that run alongside
the agent can name it without importing the client, which would make the import
order decide whether the package loads.
"""


class CindralError(RuntimeError):
    """A request to the broker failed.

    `status` is the HTTP status when the broker answered and `None` when the
    request never got one. A caller that must tell "the broker refused" from "the
    request did not arrive" cannot do it from the message: both are a RuntimeError
    with prose, and a lease heartbeat that confuses them cancels a running job over
    a network blip.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitHubAPIError(RuntimeError):
    """Raised when a GitHub API request fails.

    Here for the same reason as `CindralError`, and now for a second caller: the
    module that decides which commit a pull-request run builds needs to raise this
    without importing the client that also raises it, or the import order decides
    whether the package loads.
    """
