"""Tool-specific dependency and build cache adapters."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import tarfile
import tempfile
import threading
from typing import Callable, TextIO

from .agent import JobSpec
from .cache_client import CacheClientError, CindralCacheClient

MAX_RESTORED_CACHE_BYTES = 2 * 1024 * 1024 * 1024
MAX_CACHE_FILES = 100_000


class CacheArchiveError(ValueError):
    pass


@dataclass(frozen=True)
class CachePlan:
    name: str
    architecture: str
    key: str
    restore_prefixes: tuple[str, ...]
    path: Path
    environment: tuple[tuple[str, str], ...] = ()


def detect_cache_plans(repository: Path, cache_root: Path, architecture: str) -> tuple[CachePlan, ...]:
    plans: list[CachePlan] = []
    pnpm_lock = _repository_file(repository, "pnpm-lock.yaml")
    npm_lock = next(
        (path for name in ("package-lock.json", "npm-shrinkwrap.json") if (path := _repository_file(repository, name))),
        None,
    )
    node_lock = pnpm_lock or npm_lock
    if pnpm_lock is not None:
        plans.append(_plan("pnpm", (pnpm_lock,), architecture, cache_root / "pnpm", "npm_config_store_dir", "/cindral-cache/pnpm"))
    elif npm_lock:
        plans.append(_plan("npm", (npm_lock,), architecture, cache_root / "npm", "npm_config_cache", "/cindral-cache/npm"))

    uv_lock = _repository_file(repository, "uv.lock")
    requirements = tuple(
        path
        for candidate in sorted(repository.glob("requirements*.txt"))
        if (path := _repository_file(repository, candidate.relative_to(repository)))
    )
    if uv_lock is not None:
        plans.append(_plan("uv", (uv_lock,), architecture, cache_root / "uv", "UV_CACHE_DIR", "/cindral-cache/uv"))
    elif requirements:
        plans.append(_plan("pip", requirements, architecture, cache_root / "pip", "PIP_CACHE_DIR", "/cindral-cache/pip"))

    go_sum = _repository_file(repository, "go.sum")
    go_mod = _repository_file(repository, "go.mod")
    if go_sum is not None:
        plans.append(
            _plan(
                "go",
                tuple(path for path in (go_mod, go_sum) if path is not None),
                architecture,
                cache_root / "go",
                None,
                None,
                (("GOMODCACHE", "/cindral-cache/go/mod"), ("GOCACHE", "/cindral-cache/go/build")),
            )
        )

    package_json = _repository_file(repository, "package.json")
    if node_lock is not None and package_json is not None:
        try:
            package = json.loads(package_json.read_text())
        except (OSError, json.JSONDecodeError):
            package = {}
        dependencies = {}
        if isinstance(package, dict):
            for section in (package.get("dependencies", {}), package.get("devDependencies", {})):
                if isinstance(section, dict):
                    dependencies.update(section)
        if "next" in dependencies:
            plans.append(_plan("next", (node_lock, package_json), architecture, repository / ".next" / "cache"))
    return tuple(plans)


def _plan(
    name: str,
    lockfiles: tuple[Path, ...],
    architecture: str,
    path: Path,
    env_name: str | None = None,
    env_path: str | None = None,
    environment: tuple[tuple[str, str], ...] = (),
) -> CachePlan:
    digest = hashlib.sha256()
    for lockfile in lockfiles:
        digest.update(lockfile.name.encode())
        digest.update(b"\0")
        digest.update(lockfile.read_bytes())
    key = f"{name}-{architecture}-{digest.hexdigest()[:32]}"
    values = environment or (((env_name, env_path),) if env_name and env_path else ())
    return CachePlan(name, architecture, key, (f"{name}-{architecture}-",), path, values)


def _repository_file(repository: Path, name: str | Path) -> Path | None:
    root = repository.resolve()
    path = repository / name
    try:
        file_stat = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(file_stat.st_mode) or not path.resolve().is_relative_to(root):
        return None
    return path


def architecture_name(machine: str) -> str:
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(machine.lower(), machine.lower())


def create_archive(directory: Path, destination: Path) -> bool:
    _assert_no_symlink_ancestors(directory)
    if not directory.is_dir() or directory.is_symlink():
        return False
    with tarfile.open(destination, "w:gz") as archive:
        for current, directories, files in os.walk(directory, followlinks=False):
            current_path = Path(current)
            directories[:] = [name for name in directories if not (current_path / name).is_symlink()]
            for name in directories:
                path = current_path / name
                info = tarfile.TarInfo(path.relative_to(directory).as_posix())
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            for name in files:
                path = current_path / name
                try:
                    file_stat = path.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(file_stat.st_mode):
                    continue
                info = tarfile.TarInfo(path.relative_to(directory).as_posix())
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as source:
                    info.size = os.fstat(source.fileno()).st_size
                    info.mode = 0o755 if info.size and os.fstat(source.fileno()).st_mode & 0o111 else 0o644
                    archive.addfile(info, source)
    return destination.stat().st_size > 0


def restore_archive(archive_path: Path, destination: Path) -> None:
    _assert_no_symlink_ancestors(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".cindral-cache-restore-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "payload"
        staged.mkdir()
        _extract_archive(archive_path, staged)
        if destination.is_dir():
            shutil.rmtree(destination)
        elif destination.exists():
            destination.unlink()
        os.replace(staged, destination)


def _extract_archive(archive_path: Path, destination: Path) -> None:
    root = destination.resolve()
    total = 0
    count = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            count += 1
            if count > MAX_CACHE_FILES or member.size < 0:
                raise CacheArchiveError("cache archive exceeds extraction limits")
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise CacheArchiveError("cache archive contains an unsafe path")
            if not member.isdir() and not member.isfile():
                raise CacheArchiveError("cache archive contains an unsupported file type")
            target = destination.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(root):
                raise CacheArchiveError("cache archive escapes its destination")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            total += member.size
            if total > MAX_RESTORED_CACHE_BYTES:
                raise CacheArchiveError("cache archive expands beyond the extraction limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise CacheArchiveError("cache archive entry could not be read")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)


def restore_caches(
    client: CindralCacheClient | None,
    job: JobSpec,
    plans: tuple[CachePlan, ...],
    workspace: Path,
    log: TextIO,
) -> dict[str, str]:
    environment: dict[str, str] = {}
    for plan in plans:
        try:
            _assert_no_symlink_ancestors(plan.path)
            plan.path.mkdir(parents=True, exist_ok=True)
        except (OSError, CacheArchiveError) as exc:
            log.write(f"cindral: cache directory skipped for {plan.name}: {exc}\n")
            continue
        environment.update(plan.environment)
        if client is None:
            continue
        try:
            entry = client.lookup(job.repository, job.ref, plan.architecture, plan.key, plan.restore_prefixes)
            if entry is None:
                log.write(f"cindral: cache miss {plan.name}\n")
                continue
            with tempfile.TemporaryDirectory(dir=workspace) as temporary:
                archive = Path(temporary) / "cache.tar.gz"
                client.download(str(entry["digest"]), archive)
                restore_archive(archive, plan.path)
            log.write(f"cindral: cache hit {plan.name} ({entry['key']})\n")
        except (CacheClientError, OSError, ValueError, KeyError) as exc:
            log.write(f"cindral: cache restore skipped for {plan.name}: {exc}\n")
    return environment


def store_caches(
    client: CindralCacheClient | None,
    job: JobSpec,
    plans: tuple[CachePlan, ...],
    workspace: Path,
    log: TextIO,
) -> None:
    if client is None:
        return
    for plan in plans:
        try:
            with tempfile.TemporaryDirectory(dir=workspace) as temporary:
                archive = Path(temporary) / "cache.tar.gz"
                if not create_archive(plan.path, archive):
                    continue
                result = client.upload(job.repository, job.ref, plan.architecture, plan.key, archive)
            outcome = "stored" if result.get("created") else "already exists"
            log.write(f"cindral: cache {outcome} {plan.name}\n")
        except (CacheClientError, OSError, ValueError) as exc:
            log.write(f"cindral: cache write skipped for {plan.name}: {exc}\n")


def run_cached_job(
    job: JobSpec,
    repository: Path,
    workspace: Path,
    log: TextIO,
    cancel: threading.Event,
    client: CindralCacheClient | None,
    run_steps: Callable[[Path | None, dict[str, str]], int],
) -> int:
    cache_root = workspace / "cache"
    try:
        plans = detect_cache_plans(repository, cache_root, architecture_name(platform.machine()))
    except (OSError, ValueError) as exc:
        log.write(f"cindral: cache detection skipped: {exc}\n")
        plans = ()
    environment = restore_caches(client, job, plans, workspace, log)
    mount = cache_root if any(plan.environment for plan in plans) else None
    result = run_steps(mount, environment)
    if result == 0 and not cancel.is_set():
        store_caches(client, job, plans, workspace, log)
    return result


def _assert_no_symlink_ancestors(path: Path) -> None:
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise CacheArchiveError("cache destination contains a symlink")
