"""Docker daemon and CLI mounts required by trusted job contracts."""
import os
import shutil
import threading
from pathlib import Path
from typing import TextIO

DOCKER_SOCKET = "/var/run/docker.sock"
DOCKER_PLUGIN_DIRS = (
    "/usr/lib/docker/cli-plugins",
    "/usr/libexec/docker/cli-plugins",
    "/usr/local/lib/docker/cli-plugins",
)


def docker_cli_mounts(docker: str, socket_path: str | None = None) -> list[str]:
    socket_path = socket_path or DOCKER_SOCKET
    socket = Path(socket_path)
    if not socket.exists():
        raise ValueError(f"contract requests docker but {socket_path} is missing")
    binary = shutil.which(docker)
    if binary is None:
        raise ValueError(f"contract requests docker but {docker} is not on PATH")
    mounts = [
        "-v",
        f"{socket}:{socket}",
        "--group-add",
        str(socket.stat().st_gid),
        "-v",
        f"{binary}:{binary}:ro",
    ]
    for directory in DOCKER_PLUGIN_DIRS:
        path = Path(directory)
        if path.is_dir():
            mounts += ["-v", f"{path}:{path}:ro"]
    return mounts


def make_cache_readable(
    runner,
    docker: str,
    cache_path: Path,
    image: str,
    log: TextIO,
    cancel: threading.Event,
    shell: str = "sh",
) -> None:
    uid = os.getuid() if hasattr(os, "getuid") else 0
    gid = os.getgid() if hasattr(os, "getgid") else 0
    try:
        runner.check(
            [
                docker,
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "0:0",
                "-v",
                f"{cache_path}:/cindral-cache",
                "--entrypoint",
                shell,
                image,
                "-lc",
                f"chown -R {uid}:{gid} /cindral-cache 2>/dev/null || chmod -R a+rX /cindral-cache",
            ],
            log,
            cancel=cancel,
        )
    except RuntimeError as exc:
        raise OSError(f"cache permissions could not be prepared: {exc}") from exc
