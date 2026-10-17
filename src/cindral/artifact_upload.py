"""Handing a finished run's archive to the broker, and clearing the local copy.

Its own module because it is the one piece of the artifact feature that runs on
the device rather than in the broker, and it sits at the end of the agent's run
where the lease is the thing most likely to have run out.
"""
import os
from typing import Callable

from .artifact_client import ArtifactClientError
from .errors import CindralError

__all__ = ["discard_archive", "upload_archive"]


def discard_archive(archive: str) -> None:
    """Remove a packed archive the run will never upload.

    The packer writes beside the run's workspace rather than inside it, so the
    workspace cleanup cannot reach it and this is the only thing that does. A
    lease lost between the pack and the upload skips the send entirely, so
    without this every such run leaves up to the cap on the device for ever, and
    nothing sweeps them. The reclaim also drops the broker's copy, since that
    run is about to execute again elsewhere.
    """
    try:
        os.unlink(archive)
    except OSError:
        pass


def upload_archive(
    client: object,
    device: str,
    job_id: str,
    archive: str,
    note: Callable[[str], None],
) -> None:
    """Send the run's archive, then remove the local copy whatever happened.

    The archive goes before the report, so a reader who opens the run as soon as
    it finishes finds it rather than finding it a moment later.

    A failure is noted and dropped. The run's verdict is its exit code, and
    losing an upload must not turn a green run into a lost one.

    The local copy is removed either way, and that matters more than it looks: a
    lease lost between the pack and the upload skips the send entirely, so
    without this the archive of every such run stays on the device for ever, at
    up to the cap each, and nothing sweeps them.
    """
    try:
        client.upload_artifacts(job_id, device, archive)  # type: ignore[attr-defined]
    except (ArtifactClientError, CindralError, OSError) as exc:
        note(f"cindral: job {job_id} artifacts were not uploaded: {exc}")
    finally:
        discard_archive(archive)
