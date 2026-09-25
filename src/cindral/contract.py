"""Per-repository direct-execution contract: .cindral/ci.toml."""
import tomllib
from dataclasses import dataclass, field
from pathlib import PurePosixPath

CONTRACT_PATH = ".cindral/ci.toml"


class ContractError(ValueError):
    """Raised when a .cindral/ci.toml is missing required structure."""


@dataclass(frozen=True)
class Service:
    name: str
    image: str
    env: dict[str, str] = field(default_factory=dict)
    command: tuple[str, ...] = ()
    health_cmd: str | None = None

    def as_dict(self) -> dict:
        return {"name": self.name, "image": self.image, "env": dict(self.env)}


@dataclass(frozen=True)
class Contract:
    steps: tuple[str, ...]
    image: str | None = None
    dockerfile: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    services: tuple[Service, ...] = ()
    timeout_minutes: int | None = None
    docker: bool = False

    @classmethod
    def parse(cls, text: str) -> "Contract":
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ContractError(f"{CONTRACT_PATH} is not valid TOML: {exc}") from exc
        if not isinstance(data, dict):
            raise ContractError(f"{CONTRACT_PATH} must be a TOML table")

        image_table = data.get("image", {})
        if not isinstance(image_table, dict):
            raise ContractError("[image] must be a table")
        image = _optional_str(image_table, "ref", "[image]")
        dockerfile = _optional_str(image_table, "dockerfile", "[image]")
        if bool(image) == bool(dockerfile):
            raise ContractError("[image] must set exactly one of ref or dockerfile")
        if dockerfile:
            dockerfile = _relative_path(dockerfile, "[image].dockerfile")

        raw_steps = data.get("steps", [])
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ContractError("define at least one [[steps]] entry")
        steps: list[str] = []
        for index, entry in enumerate(raw_steps):
            if not isinstance(entry, dict) or not isinstance(entry.get("run"), str):
                raise ContractError(f"steps[{index}] must be a table with a string run")
            command = entry["run"].strip()
            if not command:
                raise ContractError(f"steps[{index}].run must not be empty")
            steps.append(command)

        env = _string_map(data.get("env", {}), "[env]")

        runner = data.get("runner", {})
        if not isinstance(runner, dict):
            raise ContractError("[runner] must be a table")
        docker = runner.get("docker", False)
        if not isinstance(docker, bool):
            raise ContractError("[runner].docker must be a boolean")

        services: list[Service] = []
        raw_services = data.get("services", [])
        if not isinstance(raw_services, list):
            raise ContractError("[[services]] must be an array of tables")
        for index, entry in enumerate(raw_services):
            if not isinstance(entry, dict):
                raise ContractError(f"services[{index}] must be a table")
            name = entry.get("name")
            service_image = entry.get("image")
            if not isinstance(name, str) or not _is_alias(name):
                raise ContractError(f"services[{index}].name must be a lowercase docker alias")
            if not isinstance(service_image, str) or not service_image.strip():
                raise ContractError(f"services[{index}].image must be a non-empty string")
            service_env = _string_map(entry.get("env", {}), f"services[{index}].env")
            raw_command = entry.get("command", [])
            if not isinstance(raw_command, list) or not all(isinstance(part, str) for part in raw_command):
                raise ContractError(f"services[{index}].command must be a list of strings")
            health_cmd = entry.get("health_cmd")
            if health_cmd is not None and (not isinstance(health_cmd, str) or not health_cmd.strip()):
                raise ContractError(f"services[{index}].health_cmd must be a non-empty string")
            services.append(
                Service(
                    name=name,
                    image=service_image.strip(),
                    env=service_env,
                    command=tuple(raw_command),
                    health_cmd=health_cmd.strip() if isinstance(health_cmd, str) else None,
                )
            )

        timeout = data.get("timeout_minutes")
        if timeout is not None and (not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0):
            raise ContractError("timeout_minutes must be a positive integer")

        return cls(
            steps=tuple(steps),
            image=image,
            dockerfile=dockerfile,
            env=env,
            services=tuple(services),
            timeout_minutes=timeout,
            docker=docker,
        )


def _optional_str(table: dict, key: str, label: str) -> str | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label}.{key} must be a non-empty string")
    return value.strip()


def _relative_path(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ContractError(f"{label} must be a relative path inside the repository")
    return str(path)


def _is_alias(name: str) -> bool:
    if not name or len(name) > 63:
        return False
    return all(c.isalnum() or c in "-_." for c in name) and name[0].isalnum()


def _string_map(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be a table of strings")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(item, str):
            raise ContractError(f"{label}.{key} must be a string")
        result[str(key)] = item
    return result
