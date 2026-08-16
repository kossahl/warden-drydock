from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence

from . import MINIMUM_DOCKER_COMPOSE, MINIMUM_DOCKER_ENGINE


def parse_version(value: str) -> tuple[int, int, int]:
    match = re.search(r"(?:v)?(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise ValueError("unrecognized_version")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def require_minimum(actual: str, minimum: Sequence[int], component: str) -> None:
    if parse_version(actual) < tuple(minimum):
        required = ".".join(str(part) for part in minimum)
        raise RuntimeError(f"unsupported_{component}_version: requires >= {required}")


def check_host_runtime() -> None:
    engine = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    compose = subprocess.run(
        ["docker", "compose", "version", "--short"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    require_minimum(engine, MINIMUM_DOCKER_ENGINE, "docker_engine")
    require_minimum(compose, MINIMUM_DOCKER_COMPOSE, "docker_compose")


def main() -> None:
    # The container cannot safely infer the host daemon version. Operators and
    # the backup/restore commands run check_host_runtime before Compose use.
    return None


if __name__ == "__main__":
    main()
