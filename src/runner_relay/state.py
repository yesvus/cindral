"""Runner state loading and serialization."""
import json
from pathlib import Path
from typing import Any

from .models import Runner


def load_runners(path: str | Path) -> tuple[Runner, ...]:
    with Path(path).open() as stream:
        value: Any = json.load(stream)
    return tuple(Runner.from_dict(item) for item in value.get("runners", []))
