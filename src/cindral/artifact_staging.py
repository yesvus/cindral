"""Where an upload's bytes go before the rename that publishes them.

An archive and the row that names it are two resources that have to agree, and
an upload that fails between them is the case that decides the order. The bytes
are written under a name of their own and renamed into place only once the
transaction is committed, so a reader never sees a row without the file it
names, and a transaction that rolls back never leaves a row behind a file
nobody wrote for it.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import time
from typing import BinaryIO, Iterator
import uuid

# where a staged upload's bytes live, and how old one has to be before it is a
# killed upload's rather than an upload in flight
STAGING_SUFFIX = ".staged"
STALE_STAGING_SECONDS = 3600


class StagingError(ValueError):
    """A body that does not match the length it declared.

    A ValueError, and ``artifact.ArtifactError`` is a sibling of it rather than a
    parent, so a caller catching ValueError catches both refusals and neither
    module has to import the other to say so.

    The upload handler catches ArtifactError only, so this one is *not* caught
    there. It is caught a frame up, by the dispatcher's ValueError clause, which
    exists for malformed JSON and bad path segments and is what turns this into a
    400. Pinning that wording matters because the obvious reading of a subclass-
    shaped exception is that the nearest handler catches it, and the nearest
    handler does not.
    """


@contextmanager
def Staging(archives: Path) -> Iterator[Path]:  # noqa: N802 - a context manager, used as one
    """Yield a path to write an upload's bytes to, and remove it on the way out.

    The name is unique per upload rather than per run, so a second broker over
    the same directory cannot truncate the bytes this one is writing. Removing
    it is what the process does in ordinary operation; a process that dies
    first leaves one behind, which is what `sweep_staging` collects.
    """
    path = archives / f"{uuid.uuid4().hex}{STAGING_SUFFIX}"
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def stage(staged: Path, source: BinaryIO, content_length: int) -> tuple[int, str]:
    """Write a body's declared length to the staging file, digesting as it goes.

    A read is trusted to return at most what it was asked for, and a body that
    does not disagrees with the length it declared. The extra bytes are exactly
    what the size cap bounds, so an upload that hands over more than it said is
    refused rather than stored at a size the caller never declared.
    """
    digest = hashlib.sha256()
    written = 0
    with staged.open("xb") as target:
        while written < content_length:
            remaining = content_length - written
            block = source.read(min(1024 * 1024, remaining))
            if not block:
                raise StagingError("artifact upload ended before Content-Length")
            if len(block) > remaining:
                raise StagingError("artifact upload exceeded its Content-Length")
            target.write(block)
            digest.update(block)
            written += len(block)
        target.flush()
        os.fsync(target.fileno())
    return written, digest.hexdigest()


def sweep_staging(archives: Path, now: float | None = None) -> None:
    """Remove staging files a killed upload left, by age rather than by name.

    Age is what tells a dead upload's file from one in flight, including one
    another broker over the same directory is writing, which a name alone would
    not. An hour is far longer than any upload takes to arrive.
    """
    cutoff = (time.time() if now is None else now) - STALE_STAGING_SECONDS
    for staged in archives.glob(f"*{STAGING_SUFFIX}"):
        try:
            if staged.stat().st_mtime <= cutoff:
                staged.unlink(missing_ok=True)
        except OSError:
            # a file another process removed between the glob and the stat is
            # already collected, which is what this wanted
            pass
