# trunk-ignore-file bandit/B101
# trunk-ignore-file bandit/B104
# trunk-ignore-file bandit/B105
"""Release upgrade probe for honcho-aio.

Acceptance: a real API message written through Honcho on the previous image
survives a container replacement with a distinct candidate on the same
appdata volumes, is read back through the real API on the candidate, and the
candidate's write path stays live after a restart. Proven in stages:

1. previous image boots; a message is created through the real API
   (workspace -> peer -> session -> messages) and lands in Postgres on disk.
2. the container is replaced with the candidate image on the SAME volumes;
   the seeded message reads back through the candidate's real API.
3. a *distinct new message* is written through the candidate's API (readback
   alone proves persistence; only a new write proves the write path).
4. the candidate container is restarted; the seeded message and the first
   new message both read back again.
5. a *second distinct new message* is written after the restart.

Disk message counts are asserted at every stage transition. The probe always
uses real Honcho API calls and never a model: the boot config points both
providers at offline dummy endpoints so the deriver has nothing to call.

Two scenarios, distinguished by image digest:

- ``test_same_digest_smoke`` — the replacement image is the same digest that
  was seeded. Harness sanity: the whole choreography must be lossless when
  nothing changed.
- ``test_two_version_upgrade`` — candidate digest differs from the previous
  digest. The real release upgrade: migrations run on existing data, the
  message comes back through the new version's API, and fresh writes land.

Docker access is restricted to the approved isolated runtimes; see
``tests/release/conftest.py``. Every container/volume/network is named per-run
and removed by this test only. Receipts (stage evidence, no secrets) are
written under ``AIO_RELEASE_RECEIPTS_DIR``.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.helpers import run_command
from tests.release.conftest import (
    SOCKET_DIND_TLS,
    SOCKET_HOST_DEFAULT,
    DockerSession,
    ReleaseBlocker,
    ReleaseInputs,
    assert_message_chain,
    assert_resource_identity,
    docker_release_env,
    docker_session,
    release_inputs,
)

pytestmark = pytest.mark.integration

# First boot initializes postgres, runs migrations, and downloads the one
# tokenizer blob tiktoken needs to import (openaipublic blob). 420s covers it
# on the DinD host; the health poll also asserts the container stayed running.
HEALTH_TIMEOUT_SECONDS = 420

# Offline dummy-model boot config: provider 'local' with an unreachable
# loopback base URL. The embedding probe is best-effort and skips (exit 2) on
# the connection error; no model is ever used. LOCAL_API_KEY is deliberately
# unset — the image substitutes a placeholder.
BOOT_ENV = {
    "POSTGRES_PASSWORD": "release-quality-test-password",  # nosec B105
    "EMBEDDING_MODEL": "local/dummy-embedding",
    "CHAT_MODEL": "local/dummy-chat",
    "LOCAL_BASE_URL": "http://127.0.0.1:9",
    "EMBEDDING_VECTOR_DIMENSIONS": "768",
}

API_BASE = "http://127.0.0.1:8000"


def _docker(env: dict[str, str], *args: str, check: bool = True):
    return run_command(["docker", *args], env=env, check=check)


def _container_logs(env: dict[str, str], name: str) -> str:
    result = _docker(env, "logs", name, check=False)
    return (result.stdout + result.stderr)[-6000:]


def _inspect(env: dict[str, str], name: str, go_template: str) -> str | None:
    result = _docker(env, "inspect", "-f", go_template, name, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def _assert_running(env: dict[str, str], name: str) -> None:
    status = _inspect(env, name, "{{.State.Status}}")
    if status != "running":
        raise AssertionError(
            f"Container {name} is {status!r}, expected running.\n"
            f"Logs:\n{_container_logs(env, name)}"
        )


def api_call(
    env: dict[str, str],
    container: str,
    method: str,
    path: str,
    body: dict | None = None,
    expected_statuses: tuple[int, ...] = (200, 201),
) -> tuple[int, dict]:
    """One real API call via curl inside the container; returns (status, parsed).

    Uses ``curl -sS -w '\\n%{http_code}'`` so HTTP error bodies stay visible in
    assertion messages instead of being swallowed by an exit code alone.
    """
    if body is not None:
        result = _docker(
            env, "exec", container, "curl", "-sS", "--max-time", "30",
            "-X", method, "-H", "Content-Type: application/json",
            "-d", json.dumps(body),
            "-w", r"\n%{http_code}", f"{API_BASE}{path}", check=False,
        )
    else:
        result = _docker(
            env, "exec", container, "curl", "-sS", "--max-time", "30",
            "-X", method, "-w", r"\n%{http_code}", f"{API_BASE}{path}", check=False,
        )
    if result.returncode != 0:
        raise AssertionError(
            f"API transport failed on {container} ({method} {path}): "
            f"{(result.stdout + result.stderr)[-1500:]}\nLogs:\n{_container_logs(env, container)}"
        )
    lines = result.stdout.rstrip("\n").rsplit("\n", 1)
    if len(lines) != 2 or not lines[-1].isdigit():
        raise AssertionError(
            f"API response on {container} ({method} {path}) was not status-tagged: "
            f"{result.stdout[:500]!r}"
        )
    status = int(lines[-1])
    if status not in expected_statuses:
        raise AssertionError(
            f"{method} {path} on {container} returned {status}, expected "
            f"{expected_statuses}: body={lines[0][-800:]!r}\n"
            f"Logs:\n{_container_logs(env, container)}"
        )
    try:
        return status, (json.loads(lines[0]) if lines[0].strip() else {})
    except ValueError as exc:
        raise AssertionError(
            f"{method} {path} on {container} returned non-JSON body: "
            f"{lines[0][:500]!r}"
        ) from exc


def ensure_image(env: dict[str, str], ref: str, arch: str) -> dict:
    """Make the digest-pinned image available locally; enforce arch; return identity.

    The ref itself carries the digest, so inspecting it resolves exactly that
    manifest — there is no mutable tag to race.
    """
    result = _docker(env, "image", "inspect", ref, check=False)
    if result.returncode != 0:
        pull_result = _docker(env, "pull", ref, check=False)
        if pull_result.returncode != 0:
            raise ReleaseBlocker(
                f"Image {ref} is absent and pull failed: "
                f"{(pull_result.stdout + pull_result.stderr)[-2000:]}"
            )
        result = _docker(env, "image", "inspect", ref, check=False)
        if result.returncode != 0:
            raise ReleaseBlocker(f"Image {ref} still not inspectable after pull.")

    image = json.loads(result.stdout)[0]
    if image.get("Architecture") != arch:
        raise ReleaseBlocker(
            f"Image {ref} is linux/{image.get('Architecture')} but "
            f"AIO_RELEASE_ARCH={arch}; refusing to run against the wrong arch."
        )
    image_id = image.get("Id") or ""
    assert image_id, f"image inspect returned no Id for {ref}: {image!r}"
    return {
        "ref": ref,
        "id": image_id,
        "arch": f"linux/{image.get('Architecture')}",
    }


@contextmanager
def release_stack(env: dict[str, str], scenario: str, arch: str) -> Iterator[dict]:
    """Create uniquely named per-run resources and clean exactly those up.

    Names embed the scenario and a per-run uuid, so nothing outside this run
    is ever touched by the cleanup.
    """
    base = f"honcho-rq-{scenario.replace('_', '-')}-{uuid.uuid4().hex[:10]}"
    names = {
        "base": base,
        "network": f"{base}-net",
        "pg_volume": f"{base}-pg",
        "redis_volume": f"{base}-redis",
        "honcho_volume": f"{base}-honcho",
        "prev_container": f"{base}-prev",
        "cand_container": f"{base}-cand",
    }

    _docker(env, "network", "create", names["network"])
    for key in ("pg_volume", "redis_volume", "honcho_volume"):
        _docker(env, "volume", "create", names[key])
    try:
        yield names
    finally:
        # Own-only cleanup: every name below was created by this context.
        for container_key in ("prev_container", "cand_container"):
            _docker(env, "rm", "-f", names[container_key], check=False)
        for volume_key in ("pg_volume", "redis_volume", "honcho_volume"):
            _docker(env, "volume", "rm", "-f", names[volume_key], check=False)
        _docker(env, "network", "rm", names["network"], check=False)


def _boot_container(
    env: dict[str, str],
    name: str,
    image_ref: str,
    network: str,
    volumes: list[tuple[str, str]],
    arch: str,
) -> None:
    command = [
        "docker", "run", "-d",
        "--platform", f"linux/{arch}",
        "--name", name,
        "--network", network,
        # No port publishing: the API is only reached via docker exec inside
        # the container, so nothing of this stack is reachable from the host.
    ]
    for volume, mount in volumes:
        command.extend(["-v", f"{volume}:{mount}"])
    for key, value in BOOT_ENV.items():
        command.extend(["-e", f"{key}={value}"])
    command.append(image_ref)
    run_command(command, env=env)


def _assert_isolated(env: dict[str, str], name: str, network: str) -> None:
    """No published ports, and attached only to the per-run network."""
    bindings = _inspect(env, name, "{{json .HostConfig.PortBindings}}") or ""
    # "{}" and "null" are both representations of no published ports.
    assert bindings.strip() in ("{}", "null"), (
        f"{name} published ports to the host: {bindings}"
    )
    nets = _inspect(env, name, "{{json .NetworkSettings.Networks}}") or ""
    attached = json.loads(nets) if nets.strip() else {}
    assert list(attached.keys()) == [network], (
        f"{name} attached to networks {list(attached.keys())}, expected only {network}"
    )


def _assert_mounts(env: dict[str, str], name: str, volumes: list[tuple[str, str]]) -> None:
    """The container must carry exactly the intended appdata volume mounts."""
    raw = _inspect(env, name, "{{json .Mounts}}") or ""
    mounts = json.loads(raw)
    seen = {
        (m.get("Name"), m.get("Destination"))
        for m in mounts
        if m.get("Type") == "volume"
    }
    expected = set(volumes)
    assert seen == expected, (
        f"{name} mounts {sorted(seen)}, expected exactly {sorted(expected)}"
    )


def _disk_message_count(env: dict[str, str], name: str) -> int:
    """Count message rows on disk through the bundled Postgres.

    Count the app's messages, not its separate message_embeddings records.
    A missing messages table is a hard SQL error, not an empty result.
    """
    count_result = _docker(
        env, "exec", name, "gosu", "postgres", "psql", "-XAt", "-v", "ON_ERROR_STOP=1",
        "-d", "honcho", "-c", "select count(*) from public.messages", check=False,
    )
    if count_result.returncode != 0:
        raise AssertionError(
            f"psql count failed on {name}: {(count_result.stdout + count_result.stderr)[-1000:]}"
        )
    raw = count_result.stdout.strip()
    if not raw.isdigit():
        raise AssertionError(
            f"psql count on {name} did not return an integer: {raw!r}"
        )
    return int(raw)


def wait_healthy(env: dict[str, str], name: str, timeout: int = HEALTH_TIMEOUT_SECONDS) -> None:
    """Poll /health inside the container until 200 or deadline."""
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        _assert_running(env, name)
        result = _docker(
            env, "exec", name,
            "curl", "-fsS", "--max-time", "5", "http://127.0.0.1:8000/health",
            check=False,
        )
        if result.returncode == 0:
            body = result.stdout.strip()
            assert json.loads(body).get("status") == "ok", f"/health body: {body}"
            return
        last_error = (result.stdout + result.stderr).strip()
        time.sleep(4)
    raise AssertionError(
        f"{name} never reported a healthy API within {timeout}s; last probe: "
        f"{last_error}\nLogs:\n{_container_logs(env, name)}"
    )


def seed_message(
    env: dict[str, str], container: str, ws: str, peer: str, sess: str, content: str
) -> str:
    """Create workspace/peer/session and write one real message; returns id.

    Request bodies use the schema alias ``{"id": <label>}``, which BOTH tested
    upstream versions accept (v3.3.0 via populate_by_name aliasing; v3.6.2
    dropped populate_by_name, so a literal ``{"name": ...}`` would 422 on
    session create while upgrade tests run on old images like v3.3.0).
    Responses assert the source-verified identity shapes; see
    ``assert_resource_identity``.
    """
    _, created_ws = api_call(env, container, "POST", "/v3/workspaces", {"id": ws})
    assert_resource_identity(created_ws, ws, kind="workspace create")
    _, created_peer = api_call(
        env, container, "POST", f"/v3/workspaces/{ws}/peers", {"id": peer}
    )
    assert_resource_identity(created_peer, peer, workspace=ws, kind="peer create")
    _, created_sess = api_call(
        env, container, "POST", f"/v3/workspaces/{ws}/sessions", {"id": sess}
    )
    assert_resource_identity(created_sess, sess, workspace=ws, kind="session create")
    return _post_message(env, container, ws, sess, peer, content)


def _post_message(
    env: dict[str, str], container: str, ws: str, sess: str, peer: str, content: str
) -> str:
    """Write one real message into an existing session; returns its id."""
    _, payload = api_call(
        env, container, "POST",
        f"/v3/workspaces/{ws}/sessions/{sess}/messages",
        {"messages": [{"peer_id": peer, "content": content}]},
    )
    messages = payload if isinstance(payload, list) else payload.get("messages")
    assert isinstance(messages, list) and messages, f"unexpected create response: {payload!r}"
    message = messages[0]
    assert_message_chain(
        message, content=content, peer=peer, session=sess, workspace=ws,
        context="created message",
    )
    message_id = message.get("id")
    assert isinstance(message_id, str) and message_id, (
        f"created message carries no id: {message!r}"
    )
    return message_id


def write_new_message(
    env: dict[str, str], container: str, ws: str, sess: str, peer: str, content: str
) -> str:
    """A distinct new write through the real API, verified by read-back."""
    return _post_message(env, container, ws, sess, peer, content)


def read_message(env: dict[str, str], container: str, ws: str, sess: str, message_id: str) -> dict:
    body = api_call(
        env, container, "GET",
        f"/v3/workspaces/{ws}/sessions/{sess}/messages/{message_id}",
        expected_statuses=(200,),
    )[1]
    assert body.get("id") == message_id, (
        f"GET message returned id {body.get('id')!r}, expected {message_id!r}: {body!r}"
    )
    return body


def _replace_container(
    env: dict[str, str],
    names: dict[str, str],
    image_ref: str,
    volumes: list[tuple[str, str]],
    arch: str,
) -> dict:
    """Stop and remove the previous container; volumes stay for the candidate."""
    prev = names["prev_container"]
    stop = _docker(env, "stop", "--time", "30", prev, check=False)
    if stop.returncode != 0:
        raise AssertionError(
            f"docker stop {prev} failed: {stop.stderr}\nLogs:\n{_container_logs(env, prev)}"
        )
    assert _inspect(env, prev, "{{.State.Status}}") != "running"
    rm = _docker(env, "rm", prev, check=False)
    assert rm.returncode == 0, f"docker rm {prev} failed: {rm.stderr}"
    # The volumes survive the removal; that is the appdata handoff.
    for volume, _mount in volumes:
        inspect = _docker(env, "volume", "inspect", volume, check=False)
        assert inspect.returncode == 0, f"Volume {volume} vanished after replace"

    _boot_container(env, names["cand_container"], image_ref, names["network"], volumes, arch)
    image_id = _inspect(env, names["cand_container"], "{{.Image}}")
    assert image_id, f"candidate container {names['cand_container']} has no Image id"
    return {"container_image_id": image_id}


def _restart_container(env: dict[str, str], name: str) -> None:
    result = _docker(env, "restart", "--time", "30", name, check=False)
    assert result.returncode == 0, (
        f"docker restart {name} failed: {(result.stdout + result.stderr)[-1000:]}"
    )
    assert _inspect(env, name, "{{.State.Status}}") == "running"


def _write_receipt(scenario: str, receipt: dict) -> Path:
    base = os.environ.get("AIO_RELEASE_RECEIPTS_DIR") or (
        Path(tempfile.gettempdir()) / "aio-release-receipts"
    )
    receipts = Path(base)
    receipts.mkdir(parents=True, exist_ok=True)
    path = receipts / f"honcho-aio-{scenario}-{receipt.get('run_token', 'run')}.json"
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return path


def _run_release_probe(
    env: dict[str, str],
    inputs,  # ReleaseInputs from tests.release.conftest
    *,
    candidate_ref: str,
    previous_ref: str,
    scenario: str,
    endpoint_mode: str | None = None,
) -> dict:
    """Boot previous -> seed via real API -> replace on same appdata -> verify.

    After replacement: readback, a distinct new write, a restart, readback of
    both messages again, and a second distinct new write. Disk counts gate
    every stage transition.
    """
    prev_image = ensure_image(env, previous_ref, inputs.arch)
    cand_image = (
        prev_image
        if candidate_ref == previous_ref
        else ensure_image(env, candidate_ref, inputs.arch)
    )
    expected_arch = f"linux/{inputs.arch}"
    if prev_image["arch"] != expected_arch or cand_image["arch"] != expected_arch:
        raise ReleaseBlocker(
            f"image arch mismatch: previous={prev_image['arch']}, "
            f"candidate={cand_image['arch']}, AIO_RELEASE_ARCH={inputs.arch}"
        )
    if candidate_ref != previous_ref and prev_image["id"] == cand_image["id"]:
        raise ReleaseBlocker(
            "distinct digest refs resolved to the same image id; a two-version "
            f"upgrade cannot be proven against identical bytes "
            f"({prev_image['id']})"
        )

    run_token = uuid.uuid4().hex[:12]
    content_seed = f"release-quality-{scenario}-seed-{run_token}"
    content_after_replace = f"release-quality-{scenario}-replace-{run_token}"
    content_after_restart = f"release-quality-{scenario}-restart-{run_token}"
    assert len({content_seed, content_after_replace, content_after_restart}) == 3, (
        "seed and verification writes must be distinct message contents"
    )

    with release_stack(env, scenario, inputs.arch) as names:
        volumes = [
            (names["pg_volume"], "/data/postgres"),
            (names["redis_volume"], "/data/redis"),
            (names["honcho_volume"], "/var/lib/honcho"),
        ]

        _boot_container(env, names["prev_container"], previous_ref, names["network"], volumes, inputs.arch)
        wait_healthy(env, names["prev_container"])
        _assert_isolated(env, names["prev_container"], names["network"])
        _assert_mounts(env, names["prev_container"], volumes)

        ws = f"{names['base']}-ws"
        peer = f"{names['base']}-peer"
        sess = f"{names['base']}-sess"

        messages = {
            "seed_container": _inspect(env, names["prev_container"], "{{.Image}}"),
            "seed_image_id": prev_image["id"],
            "disk_before_seed": _disk_message_count(env, names["prev_container"]),
        }
        assert messages["disk_before_seed"] == 0, "fresh appdata must start empty"
        message_id = seed_message(env, names["prev_container"], ws, peer, sess, content_seed)
        messages["disk_after_seed"] = _disk_message_count(env, names["prev_container"])
        assert messages["disk_after_seed"] == messages["disk_before_seed"] + 1, (
            f"seed write did not reach disk: {messages}"
        )

        replace_receipt = _replace_container(env, names, candidate_ref, volumes, inputs.arch)
        wait_healthy(env, names["cand_container"])
        _assert_isolated(env, names["cand_container"], names["network"])
        _assert_mounts(env, names["cand_container"], volumes)

        seeded_read = read_message(env, names["cand_container"], ws, sess, message_id)
        assert_message_chain(
            seeded_read, content=content_seed, peer=peer, session=sess, workspace=ws,
            context=(
                f"[{scenario}] message written on previous image {previous_ref} did "
                f"not survive replacement with {candidate_ref}"
            ),
        )
        messages["disk_after_replace"] = _disk_message_count(env, names["cand_container"])
        assert messages["disk_after_replace"] == messages["disk_after_seed"], (
            f"disk message count changed across replacement: {messages}"
        )

        write_replace_id = write_new_message(
            env, names["cand_container"], ws, sess, peer, content_after_replace
        )
        assert write_replace_id != message_id, "new write reused the seeded message id"
        replace_read = read_message(env, names["cand_container"], ws, sess, write_replace_id)
        assert_message_chain(
            replace_read, content=content_after_replace, peer=peer, session=sess,
            workspace=ws, context=f"[{scenario}] new write read-back",
        )
        messages["disk_after_write_on_candidate"] = _disk_message_count(env, names["cand_container"])
        assert messages["disk_after_write_on_candidate"] == messages["disk_after_replace"] + 1, (
            f"post-replacement write did not reach disk: {messages}"
        )

        _restart_container(env, names["cand_container"])
        wait_healthy(env, names["cand_container"])
        _assert_isolated(env, names["cand_container"], names["network"])
        _assert_mounts(env, names["cand_container"], volumes)

        seeded_after_restart = read_message(env, names["cand_container"], ws, sess, message_id)
        assert_message_chain(
            seeded_after_restart, content=content_seed, peer=peer, session=sess,
            workspace=ws, context=f"[{scenario}] seeded message across restart",
        )
        write_after_restart = read_message(
            env, names["cand_container"], ws, sess, write_replace_id
        )
        assert_message_chain(
            write_after_restart, content=content_after_replace, peer=peer,
            session=sess, workspace=ws,
            context=f"[{scenario}] post-replacement write across restart",
        )
        messages["disk_after_restart"] = _disk_message_count(env, names["cand_container"])
        assert messages["disk_after_restart"] == messages["disk_after_write_on_candidate"], (
            f"disk message count changed across restart: {messages}"
        )

        write_restart_id = write_new_message(
            env, names["cand_container"], ws, sess, peer, content_after_restart
        )
        assert write_restart_id not in (message_id, write_replace_id), (
            "post-restart write reused a previous message id"
        )
        restart_read = read_message(env, names["cand_container"], ws, sess, write_restart_id)
        assert_message_chain(
            restart_read, content=content_after_restart, peer=peer, session=sess,
            workspace=ws, context=f"[{scenario}] post-restart write read-back",
        )
        messages["disk_after_post_restart_write"] = _disk_message_count(env, names["cand_container"])
        assert messages["disk_after_post_restart_write"] == messages["disk_after_restart"] + 1, (
            f"post-restart write did not reach disk: {messages}"
        )

    # Fixture cleanup must be complete and own-only: after the stack closes,
    # nothing this run created may remain on the daemon.
    leftovers: list[str] = []
    for container_name in (names["prev_container"], names["cand_container"]):
        if _docker(env, "inspect", "-f", "{{.Id}}", container_name, check=False).returncode == 0:
            leftovers.append(f"container:{container_name}")
    for volume_name in (names["pg_volume"], names["redis_volume"], names["honcho_volume"]):
        if _docker(env, "volume", "inspect", volume_name, check=False).returncode == 0:
            leftovers.append(f"volume:{volume_name}")
    if _docker(env, "network", "inspect", names["network"], check=False).returncode == 0:
        leftovers.append(f"network:{names['network']}")
    assert not leftovers, (
        f"release stack cleanup left resources of run {names['base']} behind: {leftovers}"
    )

    evidence = {
        "scenario": scenario,
        "docker_endpoint_mode": endpoint_mode,
        "classification": (
            "harness-sanity-same-digest"
            if candidate_ref == previous_ref
            else "two-version-upgrade"
        ),
        "run_token": run_token,
        "previous_ref": previous_ref,
        "candidate_ref": candidate_ref,
        "previous_image_id": prev_image["id"],
        "candidate_image_id": cand_image["id"],
        "arch": f"{prev_image['arch']}/{cand_image['arch']}",
        "message_id": message_id,
        "write_after_replace_id": write_replace_id,
        "write_after_restart_id": write_restart_id,
        "workspace": ws,
        "contents": {
            "seed": content_seed,
            "after_replace": content_after_replace,
            "after_restart": content_after_restart,
        },
        "disk_message_counts": {
            key: value
            for key, value in messages.items()
            if key.startswith("disk_")
        },
        "candidate_container_image_id": replace_receipt["container_image_id"],
    }
    receipt_path = _write_receipt(scenario, evidence)
    evidence["receipt_path"] = str(receipt_path)
    return evidence


@pytest.fixture(scope="session")
def docker_runtime() -> DockerSession:
    """Contract-checked docker endpoint for this run — fail hard, not skip.

    Local mode keeps the strict DinD identity gate (name, containerized OS,
    arch); the genuine hosted ARM64 mode pins the runner engine by
    ARCHITECTURE instead of by name, per the ephemeral-runner contract.
    """
    session = docker_session()
    env = session.env
    info = _docker(env, "info", check=False)
    if info.returncode != 0:
        raise ReleaseBlocker(
            "The approved docker endpoint is not reachable — release lane needs "
            f"the verified isolated DinD or the hosted ARM64 opt-in.\n{info.stderr[-1500:]}"
        )
    identity = _docker(
        env, "info", "--format", "{{.Name}}|{{.OperatingSystem}}|{{.Architecture}}",
        check=False,
    )
    assert identity.returncode == 0 and identity.stdout.strip(), (
        f"docker info identity failed: {(identity.stdout + identity.stderr)[-500:]}"
    )
    name, operating_system, info_arch = identity.stdout.strip().split("|")
    assert name.strip(), "docker info returned an empty engine name"
    inputs = release_inputs()
    arch_map = {"amd64": "x86_64", "arm64": "aarch64"}

    if session.mode == SOCKET_DIND_TLS:
        assert name == "Docker-in-Docker-DinD", (
            f"unexpected daemon identity: {name!r} ({operating_system!r})"
        )
    else:
        assert session.mode == SOCKET_HOST_DEFAULT
        # Native hosted ARM64: engine is trusted by construction (ephemeral,
        # runner-scoped); pin its architecture, never its DinD-style name.
        assert "DOCKER_HOST" not in env, (
            "the hosted mode must use the runner's default socket; the session "
            "env still carries a DOCKER_HOST override"
        )
        assert env.get("AIO_RELEASE_NATIVE_DOCKER") == "1"
        assert inputs.arch == "arm64", (
            f"hosted mode is ARM64-only; AIO_RELEASE_ARCH={inputs.arch!r}"
        )
    assert info_arch == arch_map[inputs.arch], (
        f"daemon architecture mismatch: info reported {info_arch!r}, "
        f"expected {arch_map[inputs.arch]!r} for AIO_RELEASE_ARCH={inputs.arch}"
    )
    server_arch = _docker(env, "version", "-f", "{{.Server.Arch}}", check=False)
    assert server_arch.returncode == 0, f"docker version failed: {server_arch.stderr}"
    assert server_arch.stdout.strip() == inputs.arch, (
        f"docker version reports {{.Server.Arch}}={server_arch.stdout.strip()!r}, "
        f"expected {inputs.arch!r}"
    )
    print(
        f"\n[release] docker engine reachable ({session.mode}): {name!r} on "
        f"{operating_system!r}, arch {info_arch}/{server_arch.stdout.strip()}, "
        f"server {_docker(env, 'version', '-f', '{{.Server.Version}}').stdout.strip()}"
    )
    return session


# ── Contract negatives: enforcement itself must hold (fast, no daemon) ────


def test_release_contract_rejects_missing_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty AIO_* inputs fail the run — skipping would hide an unverified release."""
    for var in ("AIO_CANDIDATE_IMAGE", "AIO_PREVIOUS_IMAGE", "AIO_RELEASE_ARCH"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ReleaseBlocker, match="Missing required release inputs"):
        release_inputs()


def test_release_contract_rejects_mutable_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-digest-pinned refs are refused; only immutable refs are allowed."""
    monkeypatch.setenv("AIO_CANDIDATE_IMAGE", "dub19/honcho-aio:latest")
    monkeypatch.setenv("AIO_PREVIOUS_IMAGE", "dub19/honcho-aio@sha256:" + "0" * 64)
    monkeypatch.setenv("AIO_RELEASE_ARCH", "amd64")
    with pytest.raises(ReleaseBlocker, match="digest-pinned"):
        release_inputs()


def test_release_contract_rejects_unknown_arch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIO_CANDIDATE_IMAGE", "x@sha256:" + "0" * 64)
    monkeypatch.setenv("AIO_PREVIOUS_IMAGE", "y@sha256:" + "0" * 64)
    monkeypatch.setenv("AIO_RELEASE_ARCH", "riscv64")
    with pytest.raises(ReleaseBlocker, match="AIO_RELEASE_ARCH"):
        release_inputs()


def test_docker_env_rejects_unapproved_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any DOCKER_HOST other than the approved DinD endpoint is refused."""
    monkeypatch.setenv("DOCKER_HOST", "tcp://something-else:2376")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", "/certs/dind")
    with pytest.raises(ReleaseBlocker, match="not the approved isolated DinD"):
        docker_release_env()


def test_docker_env_rejects_host_default_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No DOCKER_HOST and no native-runner opt-in: refuse, never grab the host."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("AIO_RELEASE_NATIVE_DOCKER", raising=False)
    with pytest.raises(ReleaseBlocker, match="refusing to guess"):
        docker_release_env()


def test_docker_engine_verified(docker_runtime: DockerSession) -> None:
    """The approved engine is actually reachable before the probes run."""
    if docker_runtime.mode == SOCKET_DIND_TLS:
        identity = _docker(docker_runtime.env, "info", "--format", "{{.Name}}|{{.Architecture}}")
        name, arch = identity.stdout.strip().split("|")
        assert name == "Docker-in-Docker-DinD", f"unexpected daemon name: {name!r}"
        assert arch == {"amd64": "x86_64", "arm64": "aarch64"}[release_inputs().arch], (
            f"daemon arch {arch!r} does not match AIO_RELEASE_ARCH={release_inputs().arch}"
        )  # nosec B101
    else:
        # Hosted ARM64: markers + opt-in re-checked where they will be used.
        assert release_inputs().arch == "arm64"
        assert "DOCKER_HOST" not in docker_runtime.env  # nosec B101 - hosted truth
        assert docker_runtime.env["AIO_RELEASE_NATIVE_DOCKER"] == "1"
        assert docker_runtime.env["GITHUB_ACTIONS"] == "true"  # nosec B105 - marker
        assert docker_runtime.env["RUNNER_ENVIRONMENT"] == "github-hosted"  # nosec B105 - marker


# ── Scenario 1: same-digest smoke — replacement image == seeded image ─────


def test_same_digest_smoke(docker_runtime: DockerSession) -> None:
    """Seed on the previous image, replace with the identical digest, read back,
    write, restart, read back, write again.

    Harness sanity: same digest in and out means the whole choreography must
    be lossless.
    """
    inputs = release_inputs()
    evidence = _run_release_probe(
        docker_runtime.env,
        inputs,
        candidate_ref=inputs.previous_ref,
        previous_ref=inputs.previous_ref,
        scenario="same_digest_smoke",
        endpoint_mode=docker_runtime.mode,
    )
    candidate_digest = ReleaseInputs.digest(evidence["candidate_ref"])
    previous_digest = ReleaseInputs.digest(evidence["previous_ref"])
    assert candidate_digest == previous_digest
    assert evidence["previous_image_id"] == evidence["candidate_image_id"]
    print(
        f"\n[release] same-digest smoke OK on digest {candidate_digest[:16]}… "
        f"(messages {evidence['message_id']}, {evidence['write_after_replace_id']}, "
        f"{evidence['write_after_restart_id']} survived/written; "
        f"disk counts {evidence['disk_message_counts']})"
    )


# ── Scenario 2: two-version upgrade — candidate digest differs ────────────


def test_two_version_upgrade(docker_runtime: DockerSession) -> None:
    """Seed on previous, upgrade to a distinct candidate on the same appdata.

    Readback, a distinct write on the candidate, restart, readback of both,
    and a second distinct write after restart. Migrations run on existing
    data during the candidate's first boot.
    """
    inputs = release_inputs()
    previous_digest = ReleaseInputs.digest(inputs.previous_ref)
    candidate_digest = ReleaseInputs.digest(inputs.candidate_ref)
    if candidate_digest == previous_digest:
        pytest.fail(
            "AIO_CANDIDATE_IMAGE has the same digest as AIO_PREVIOUS_IMAGE; "
            "a two-version upgrade cannot be proven. The identical-digest case "
            "is covered by test_same_digest_smoke."
        )

    evidence = _run_release_probe(
        docker_runtime.env,
        inputs,
        candidate_ref=inputs.candidate_ref,
        previous_ref=inputs.previous_ref,
        scenario="two_version_upgrade",
        endpoint_mode=docker_runtime.mode,
    )
    assert evidence["candidate_ref"] == inputs.candidate_ref
    assert evidence["previous_image_id"] != evidence["candidate_image_id"], (
        "distinct digests must not collapse to the same image id"
    )
    counts = evidence["disk_message_counts"]
    assert counts["disk_after_replace"] == counts["disk_after_seed"], (
        f"seeded row lost across replacement: {counts}"
    )
    assert counts["disk_after_post_restart_write"] == counts["disk_before_seed"] + 3, (
        f"expected exactly seed + 2 new writes on disk: {counts}"
    )
    print(
        f"\n[release] two-version upgrade OK: {previous_digest[:16]}… → "
        f"{candidate_digest[:16]}… (seeded {evidence['message_id']} + new writes "
        f"{evidence['write_after_replace_id']}/{evidence['write_after_restart_id']} "
        f"survived restart; disk counts {counts})"
    )


__all__ = ["ensure_image", "seed_message", "read_message", "wait_healthy"]
