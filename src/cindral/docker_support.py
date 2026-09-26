"""Docker daemon and CLI mounts required by trusted job contracts."""
import shutil
from pathlib import Path

DOCKER_SOCKET = "/var/run/docker.sock"
DOCKER_PLUGIN_DIRS = ("/usr/libexec/docker/cli-plugins", "/usr/local/lib/docker/cli-plugins")


def docker_cli_mounts(docker: str, socket_path: str | None = None) -> list[str]:
    socket_path = socket_path or DOCKER_SOCKET
    socket = Path(socket_path)
    if not socket.exists():
        raise ValueError(f"contract requests docker but {socket_path} is missing")
    binary = shutil.which(docker)
    if binary is None:
        raise ValueError(f"contract requests docker but {docker} is not on PATH")
    resolved = str(Path(binary).resolve())
    mounts = [
        "-v",
        f"{socket}:{socket}",
        "--group-add",
        str(socket.stat().st_gid),
        "-v",
        f"{resolved}:{resolved}:ro",
    ]
    for directory in DOCKER_PLUGIN_DIRS:
        path = Path(directory)
        if path.is_dir():
            mounts += ["-v", f"{path}:{path}:ro"]
    return mounts
