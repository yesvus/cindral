"""Tar the artifact directory a run filled, on the device that ran it.

Split out of the broker's artifact store because the two answer to different
sides of the same run: this is what the device produces at the end of an
execution, and `artifact.py` is what the broker keeps afterwards. They are
adjacent in time and opposite in process.
"""
import contextlib
import os
from pathlib import Path
import stat
import tarfile
from typing import Callable, TextIO
from .artifact import (
    ARTIFACT_DIRECTORY,
    ARTIFACT_ROOT,
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACT_FILES,
    ArtifactError,
)

__all__ = ["pack_artifacts"]



def pack_artifacts(
    repository: Path, workspace: Path, log: TextIO,
    make_readable: Callable[[Path], None] | None = None,
) -> str:
    """Tar the artifact directory a run filled, returning the archive's path.

    The archive is written beside the run's workspace rather than inside it, so
    it survives the cleanup that removes the workspace once the run ends. A run
    that wrote nothing, wrote an empty directory, or wrote more than the broker
    could store produces no archive and says so, because a reader otherwise
    cannot tell a run that uploaded nothing from a device that never looked.
    Every failure is a log line and an empty result: a run's verdict is its exit
    code, and output the broker will not take is a missing artifact.
    """
    directory = repository / ARTIFACT_DIRECTORY
    destination = workspace.parent / f"{workspace.name}.tar"
    if not _inside_checkout(repository, directory) or not directory.is_dir():
        log.write(f"cindral: artifacts: no {ARTIFACT_DIRECTORY} directory\n")
        return ""
    try:
        try:
            written = _write_archive(directory, destination)
        except PermissionError:
            # the archive the failed attempt opened is the one the repair
            # reuses, so it goes whichever way this branch leaves
            destination.unlink(missing_ok=True)
            if make_readable is None:
                log.write(f"cindral: artifacts: {ARTIFACT_DIRECTORY} could not be read\n")
                return ""
            make_readable(directory)
            written = _write_archive(directory, destination)
    except Exception as exc:  # noqa: BLE001 - a pack must never decide a run's verdict
        # a pack that stopped part way leaves a truncated archive that looks
        # like a real one, and a device that fills its disk or loses the device
        # mid-pack would leave one behind every run. Anything at all can raise
        # here: a run's verdict is its exit code, and the artifact goes missing.
        destination.unlink(missing_ok=True)
        log.write(f"cindral: artifacts skipped: {exc}\n")
        return ""
    if not written:
        destination.unlink(missing_ok=True)
        log.write(f"cindral: artifacts: {ARTIFACT_DIRECTORY} is empty\n")
        return ""
    log.write(f"cindral: artifacts: {written} file(s), {destination.stat().st_size} bytes\n")
    return str(destination)


def _inside_checkout(repository: Path, directory: Path) -> bool:
    """Whether the artifact directory really sits in the checkout it names.

    The name is two parts and a step wrote both, so either can be a symlink: a
    checkout whose `.cindral` points elsewhere makes the join resolve outside
    it, and the walk would pack what is there.
    """
    with contextlib.suppress(OSError):
        return directory.resolve().is_relative_to(repository.resolve())
    return False


def _write_archive(directory: Path, destination: Path) -> int:
    """Write the directory as one tar, returning how many files it holds.

    Every member is named under a fixed root and from a path relative to the
    directory, and anything that is not a plain file is left out: a step's own
    bytes, including a symlink it wrote, decide what the archive names, so a
    reader extracting what it is given must not be walked out by doing so.
    """
    files: list[tuple[Path, os.stat_result]] = []
    directories: list[Path] = []
    total = 0

    def fail(error: OSError) -> None:
        raise error

    for current, children, names in os.walk(directory, followlinks=False, onerror=fail):
        here = Path(current)
        children[:] = [name for name in children if not (here / name).is_symlink()]
        directories.extend(here / name for name in children)
        for name in names:
            path = here / name
            file_stat = path.lstat()
            if stat.S_ISREG(file_stat.st_mode):
                total += file_stat.st_size
                files.append((path, file_stat))
                _refuse(len(files) + len(directories), total)
    _refuse(len(files) + len(directories), total)
    # every member is named under the fixed root, from a path relative to the
    # directory, so the archive holds no absolute path and no parent reference
    def named(path: Path | None) -> str:
        return ARTIFACT_ROOT if path is None else f"{ARTIFACT_ROOT}/{path.relative_to(directory).as_posix()}"


    with tarfile.open(destination, "w") as archive:
        # the directories are members in their own right rather than implied by
        # the files under them, so an extractor that does not create a parent
        # for every entry it is given still gets the shape the run wrote
        for path in (None, *directories):
            member = tarfile.TarInfo(named(path))
            member.type, member.mode = tarfile.DIRTYPE, 0o755
            archive.addfile(member)
        for path, file_stat in files:
            member = tarfile.TarInfo(named(path))
            member.size = file_stat.st_size
            member.mode = 0o755 if file_stat.st_mode & 0o111 else 0o644
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as source:
                archive.addfile(member, source)
    return len(files)


def _refuse(count: int, total: int) -> None:
    """Refuse an artifact the broker could not store, before writing any of it.

    The broker caps an archive and the store caps what it keeps, so a step that
    fills a disk is refused here rather than packed and refused again.
    """
    if count > MAX_ARTIFACT_FILES:
        raise ArtifactError("artifact directory exceeds the file-count limit")
    if total + 1024 * count > MAX_ARTIFACT_BYTES:
        raise ArtifactError("artifact directory exceeds the size limit")
