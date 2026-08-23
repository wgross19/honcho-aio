# trunk-ignore-file bandit/B101
# trunk-ignore-file bandit/B104
# trunk-ignore-file bandit/B105
from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess  # nosec B404 - test helpers shell out only to local tooling
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# trunk-ignore-file bandit/B101
# trunk-ignore-file bandit/B104
# trunk-ignore-file bandit/B105
from tests.conftest import REPO_ROOT


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 - tests execute trusted local commands only
        command,
        cwd=cwd or REPO_ROOT,
        env=env,
        check=check,
        text=True,
        capture_output=capture_output,
    )


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False

    result = run_command(["docker", "info"], check=False)
    return result.returncode == 0


def docker_image_exists(image_tag: str) -> bool:
    result = run_command(["docker", "image", "inspect", image_tag], check=False)
    return result.returncode == 0


def ensure_pytest_image(image_tag: str) -> None:
    if os.environ.get("AIO_PYTEST_USE_PREBUILT_IMAGE") == "true":
        if not docker_image_exists(image_tag):
            raise AssertionError(
                f"Expected prebuilt pytest image {image_tag} to be loaded before the test run."
            )
        return

    run_command(["docker", "build", "--platform", "linux/amd64", "-t", image_tag, "."])


def reserve_host_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return sock.getsockname()[1]


def create_docker_volume(prefix: str) -> str:
    volume_name = f"{prefix}-{uuid.uuid4().hex[:10]}"
    run_command(["docker", "volume", "create", volume_name])
    return volume_name


def remove_docker_volume(volume_name: str) -> None:
    run_command(["docker", "volume", "rm", "-f", volume_name], check=False)


@contextmanager
def docker_volume(prefix: str) -> Iterator[str]:
    volume_name = create_docker_volume(prefix)
    try:
        yield volume_name
    finally:
        remove_docker_volume(volume_name)


def docker_exec(
    container_name: str, command: str, *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return run_command(
        ["docker", "exec", container_name, "sh", "-lc", command], check=check
    )


def container_path_exists(container_name: str, path: str) -> bool:
    return (
        docker_exec(
            container_name, f"test -e {shlex.quote(path)}", check=False
        ).returncode
        == 0
    )


def read_container_file(container_name: str, path: str) -> str:
    return docker_exec(container_name, f"cat {shlex.quote(path)}").stdout


def container_file_size(container_name: str, path: str) -> int:
    return int(
        docker_exec(container_name, f"wc -c < {shlex.quote(path)}").stdout.strip()
    )


class HonchoDockerRuntime:
    """Docker runtime for honcho-aio integration tests.

    Mounts three named volumes matching the Dockerfile VOLUME declarations:
      /data/postgres, /data/redis, /var/lib/honcho.

    Uses docker exec to run HTTP health checks inside the container
    (port mapping is unreliable in Docker-in-Docker environments).

    Uses the pytest image tag from .aio-fleet.yml (honcho-aio:pytest).
    """

    IMAGE_TAG = "honcho-aio:pytest"

    def __init__(self) -> None:
        self.image_tag = self.IMAGE_TAG

    def build(self) -> None:
        ensure_pytest_image(self.image_tag)

    def inspect_state(self, name: str, field: str) -> str:
        result = run_command(
            ["docker", "inspect", "-f", f"{{{{.{field}}}}}", name],
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def logs(self, name: str) -> str:
        result = run_command(["docker", "logs", name], check=False)
        return result.stdout + result.stderr

    def remove(self, name: str) -> None:
        run_command(["docker", "rm", "-f", name], check=False)

    @contextmanager
    def container(
        self,
        *,
        env_overrides: dict[str, str] | None = None,
    ) -> Iterator["ContainerHandle"]:
        suffix = uuid.uuid4().hex[:10]
        name = f"honcho-aio-pytest-{suffix}"
        pg_volume = create_docker_volume(f"{name}-pg")
        redis_volume = create_docker_volume(f"{name}-redis")
        honcho_volume = create_docker_volume(f"{name}-honcho")
        try:
            command = [
                "docker",
                "run",
                "-d",
                "--platform",
                "linux/amd64",
                "--name",
                name,
                # No port mapping — health checks use docker exec inside the
                # container for reliable operation in DinD environments.
                "-v",
                f"{pg_volume}:/data/postgres",
                "-v",
                f"{redis_volume}:/data/redis",
                "-v",
                f"{honcho_volume}:/var/lib/honcho",
            ]

            if env_overrides:
                for key, value in env_overrides.items():
                    command.extend(["-e", f"{key}={value}"])

            command.append(self.image_tag)
            run_command(command)
            handle = ContainerHandle(
                runtime=self,
                name=name,
                pg_volume=pg_volume,
                redis_volume=redis_volume,
                honcho_volume=honcho_volume,
            )
            try:
                yield handle
            finally:
                self.remove(name)
        finally:
            remove_docker_volume(pg_volume)
            remove_docker_volume(redis_volume)
            remove_docker_volume(honcho_volume)


class ContainerHandle:
    def __init__(
        self,
        *,
        runtime: HonchoDockerRuntime,
        name: str,
        pg_volume: str,
        redis_volume: str,
        honcho_volume: str,
    ) -> None:
        self.runtime = runtime
        self.name = name
        self.pg_volume = pg_volume
        self.redis_volume = redis_volume
        self.honcho_volume = honcho_volume

    def logs(self) -> str:
        return self.runtime.logs(self.name)

    def exec(
        self, command: str, *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return docker_exec(self.name, command, check=check)

    def restart(self) -> None:
        run_command(["docker", "restart", self.name])

    def stop(self) -> None:
        run_command(["docker", "stop", self.name])

    def start(self) -> None:
        run_command(["docker", "start", self.name])

    def is_running(self) -> bool:
        return self.runtime.inspect_state(self.name, "State.Status") == "running"

    def path_exists(self, path: str) -> bool:
        return container_path_exists(self.name, path)

    def read_text(self, path: str) -> str:
        return read_container_file(self.name, path)

    def file_size(self, path: str) -> int:
        return container_file_size(self.name, path)

    def wait_for_http(self, *, path: str = "/health", timeout: int = 300) -> None:
        """Wait for HTTP 200 by curling inside the container.

        Uses `docker exec` to run curl against the container's own loopback,
        which avoids port-mapping issues in Docker-in-Docker environments.
        """
        deadline = time.time() + timeout

        while time.time() < deadline:
            if not self.is_running():
                raise AssertionError(
                    f"{self.name} stopped before HTTP became healthy.\nLogs:\n{self.logs()}"
                )

            result = self.exec(
                f"curl -fsS http://127.0.0.1:8000{path}",
                check=False,
            )
            if result.returncode == 0:
                return
            time.sleep(5)

        raise AssertionError(
            f"{self.name} did not become healthy.\nLogs:\n{self.logs()}"
        )

    def http_get(
        self, path: str, *, timeout: int = 30
    ) -> subprocess.CompletedProcess[str]:
        """Run GET request via docker exec inside the container."""
        return self.exec(
            f"curl -fsS http://127.0.0.1:8000{path}",
            check=False,
        )

    def http_post(
        self,
        path: str,
        data: str,
        *,
        content_type: str = "application/json",
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        """Run POST request via docker exec inside the container."""
        quoted_data = shlex.quote(data)
        return self.exec(
            f"curl -sS -X POST -H 'Content-Type: {content_type}' -d {quoted_data} http://127.0.0.1:8000{path}",
            check=False,
        )


def pytest_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env
