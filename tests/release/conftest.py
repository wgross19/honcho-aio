# trunk-ignore-file bandit/B101
# trunk-ignore-file bandit/B104
# trunk-ignore-file bandit/B105
"""Release-lane contract for honcho-aio upgrade probes.

The aio-fleet candidate worker sets these inputs for every release run:

- ``AIO_CANDIDATE_IMAGE`` / ``AIO_PREVIOUS_IMAGE`` — immutable, digest-pinned
  image refs (``<name>@sha256:<64 hex>``)
- ``AIO_RELEASE_ARCH`` — ``amd64`` or ``arm64``

Missing or malformed inputs FAIL, they never skip. Docker access is restricted
to pre-approved, explicitly configured runtimes (today: the local verified
isolated DinD; future: an ephemeral GitHub native runner). The host default
socket and any other endpoint are refused.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

DIGEST_REF_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")

ALLOWED_ARCHES = frozenset({"amd64", "arm64"})

# The only explicitly approved containerized runtime for this lane: the local
# isolated DinD service, reached over TLS with its pinned cert directory.
ALLOWED_DIND_HOST = "tcp://Docker-in-Docker-DinD:2376"
DIND_CERT_PATH = "/certs/dind"

# Opt-in for a future ephemeral GitHub native runner (DOCKER_HOST unset there).
NATIVE_RUNNER_FLAG = "AIO_RELEASE_NATIVE_DOCKER"

# The genuine GitHub hosted-runner markers. The native opt-in is only honored
# when BOTH are present with exactly these values; anything else (self-hosted
# runners, spoofed variants, a bare flag) stays refused.
HOSTED_ACTIONS_VAR = "GITHUB_ACTIONS"
HOSTED_ACTIONS_VALUE = "true"
RUNNER_ENVIRONMENT_VAR = "RUNNER_ENVIRONMENT"
HOSTED_RUNNER_VALUE = "github-hosted"


class ReleaseBlocker(AssertionError):
    """A release-lane precondition that must fail the run, never skip it."""


def assert_resource_identity(
    response: dict,
    expected_name: str,
    *,
    workspace: str | None = None,
    kind: str = "resource",
) -> str | None:
    """The canonical identity contract for workspace/peer/session responses.

    Verified against upstream honcho at the two pins this suite runs between
    (v3.3.0 ``ad3839d…``, v3.6.2 ``e212fee…``): every such response serializes
    the client-chosen name as ``id`` (pydantic serialization_alias of the
    internal ``name``/``public_id``; the JSON carries no ``name`` key), and
    v3.6.x additionally exposes ``uid`` — a server-generated immutable id that
    never replaces ``id``. Nothing here loosens identity: ``id`` must equal
    the label the caller addressed, a contradictory ``name``/``uid`` is
    refused, and when a workspace binding is present in the schema
    (peer/session ``workspace_id``) it must match the addressed workspace.
    Returns the server-generated ``uid`` when one is present, else ``None``.
    """
    identity = response.get("id")
    assert identity == expected_name, (
        f"{kind} identity mismatch: id={identity!r} != requested {expected_name!r}; "
        f"response: {response!r}"
    )
    name_key = response.get("name")
    assert name_key in (None, expected_name), (
        f"{kind} carries a contradictory name key: {name_key!r} != {expected_name!r}"
    )
    uid = response.get("uid")
    if uid is not None:
        assert uid != expected_name, (
            f"{kind} uid must be server-generated, not a copy of the requested "
            f"name: {uid!r}"
        )
    if workspace is not None:
        bound = response.get("workspace_id")
        assert bound == workspace, (
            f"{kind} workspace binding mismatch: workspace_id={bound!r} != "
            f"{workspace!r}; response: {response!r}"
        )
    return uid if isinstance(uid, str) else None


def assert_message_chain(
    message: dict,
    *,
    content: str,
    peer: str,
    session: str,
    workspace: str,
    context: str = "message",
) -> None:
    """The full identity chain of an honcho API message (Message schema).

    Serialization aliases on both tested versions: ``public_id`` → ``id``,
    ``peer_name`` → ``peer_id``, ``session_name`` → ``session_id``,
    ``workspace_name`` → ``workspace_id``. Every link must match what the
    caller addressed; content equality is part of the chain.
    """
    assert message.get("content") == content, (
        f"{context} content mismatch: {message.get('content')!r} != {content!r}; "
        f"message: {message!r}"
    )
    assert message.get("peer_id") == peer, (
        f"{context} peer_id mismatch: {message.get('peer_id')!r} != {peer!r}; "
        f"message: {message!r}"
    )
    assert message.get("session_id") == session, (
        f"{context} session_id mismatch: {message.get('session_id')!r} != "
        f"{session!r}; message: {message!r}"
    )
    assert message.get("workspace_id") == workspace, (
        f"{context} workspace_id mismatch: {message.get('workspace_id')!r} != "
        f"{workspace!r}; message: {message!r}"
    )


# Resolved docker-endpoint modes (recorded in receipts, used by the suite).
# The strict local path is, and stays, the verified TLS DinD wire-up; the
# hosted default socket exists ONLY for the explicitly opted-in, genuine
# GitHub-hosted ARM64 qualification (ephemeral runner, runner-scoped daemon).
SOCKET_DIND_TLS = "dind-tls"
SOCKET_HOST_DEFAULT = "host-default"


@dataclass(frozen=True)
class DockerSession:
    """The resolved, contract-checked docker endpoint for a release run."""

    env: dict[str, str]
    mode: str  # SOCKET_DIND_TLS | SOCKET_HOST_DEFAULT
    hosted: bool


@dataclass(frozen=True)
class ReleaseInputs:
    candidate_ref: str
    previous_ref: str
    arch: str

    @staticmethod
    def digest(ref: str) -> str:
        return ref.rsplit("@", 1)[1]


def release_inputs() -> ReleaseInputs:
    """Read and validate the worker-provided release inputs.

    Every violation raises :class:`ReleaseBlocker` — hard failures, per the
    worker interface contract ("Missing required inputs fail, not skip").
    """
    candidate = os.environ.get("AIO_CANDIDATE_IMAGE")
    previous = os.environ.get("AIO_PREVIOUS_IMAGE")
    arch = os.environ.get("AIO_RELEASE_ARCH")

    missing = [
        name
        for name, value in (
            ("AIO_CANDIDATE_IMAGE", candidate),
            ("AIO_PREVIOUS_IMAGE", previous),
            ("AIO_RELEASE_ARCH", arch),
        )
        if not value
    ]
    if missing:
        raise ReleaseBlocker(
            "Missing required release inputs: " + ", ".join(missing)
            + ". The fleet candidate worker must set them; skipping would hide "
            "an unverified release."
        )

    for name, ref in (("AIO_CANDIDATE_IMAGE", candidate), ("AIO_PREVIOUS_IMAGE", previous)):
        assert ref is not None  # narrowed above; for the type checker
        if not DIGEST_REF_RE.match(ref):
            raise ReleaseBlocker(
                f"{name} must be a digest-pinned ref (<name>@sha256:<64 hex>), got {ref!r}. "
                "Mutable tags are not accepted in the release lane."
            )

    if arch is None or arch not in ALLOWED_ARCHES:
        raise ReleaseBlocker(
            f"AIO_RELEASE_ARCH must be one of {sorted(ALLOWED_ARCHES)}, got {arch!r}."
        )

    assert candidate is not None and previous is not None  # narrowed above
    return ReleaseInputs(candidate_ref=candidate, previous_ref=previous, arch=arch)


def docker_session() -> DockerSession:
    """Resolve and check the docker endpoint for this run, or raise ReleaseBlocker.

    Allowed configurations, and nothing else:

    1. The verified isolated DinD endpoint ``tcp://Docker-in-Docker-DinD:2376``
       with ``DOCKER_TLS_VERIFY=1`` and ``DOCKER_CERT_PATH=/certs/dind``
       (ca.pem/cert.pem/key.pem must exist). This strict local path is
       unchanged; it qualifies either arch on the shared local daemon, where
       the daemon identity is pinned downstream.
    2. An ephemeral GitHub-hosted ARM64 runner's own default docker socket,
       ONLY when the run explicitly opts in with ``AIO_RELEASE_NATIVE_DOCKER=1``
       AND the genuine hosted-runner markers (``GITHUB_ACTIONS=true`` AND
       ``RUNNER_ENVIRONMENT=github-hosted``) are present AND
       ``AIO_RELEASE_ARCH=arm64``. Self-hosted runners and amd64 runs are
       refused: amd64 evidence belongs on the verified local DinD.

    A non-empty ``DOCKER_CONTEXT`` is refused in every mode: a docker context
    silently redirects the CLI to that context's endpoint regardless of the
    pinned ``DOCKER_HOST``. The returned environment never carries
    ``DOCKER_CONTEXT``.

    Any other DOCKER_HOST — including unset-plus-missing opt-in — is refused
    so the lane can never fall back onto the host docker or an unverified
    remote.
    """
    docker_context = os.environ.get("DOCKER_CONTEXT")
    if docker_context:
        raise ReleaseBlocker(
            f"DOCKER_CONTEXT={docker_context!r} is set; a docker context "
            "silently redirects the CLI away from the approved endpoint "
            "regardless of DOCKER_HOST. Unset DOCKER_CONTEXT and configure the "
            f"approved endpoint ({ALLOWED_DIND_HOST}) explicitly."
        )
    host = os.environ.get("DOCKER_HOST")

    if host is None:
        if os.environ.get(NATIVE_RUNNER_FLAG) != "1":
            raise ReleaseBlocker(
                "DOCKER_HOST is unset and " + NATIVE_RUNNER_FLAG + "=1 is not set; "
                "refusing to guess a docker endpoint. Use the approved DinD "
                f"({ALLOWED_DIND_HOST}) or the native-runner opt-in."
            )
        hosted_markers = (
            os.environ.get(HOSTED_ACTIONS_VAR) == HOSTED_ACTIONS_VALUE
            and os.environ.get(RUNNER_ENVIRONMENT_VAR) == HOSTED_RUNNER_VALUE
        )
        if not hosted_markers:
            raise ReleaseBlocker(
                f"{NATIVE_RUNNER_FLAG}=1 requires the genuine GitHub "
                "hosted-runner markers "
                f"({HOSTED_ACTIONS_VAR}={HOSTED_ACTIONS_VALUE} AND "
                f"{RUNNER_ENVIRONMENT_VAR}={HOSTED_RUNNER_VALUE}); a bare opt-in "
                "without them is not an approved endpoint and the lane refuses "
                "to guess."
            )
        arch = os.environ.get("AIO_RELEASE_ARCH")
        if arch != "arm64":
            raise ReleaseBlocker(
                "The hosted-runner opt-in is reserved for the native ARM64 "
                f"qualification: AIO_RELEASE_ARCH is {arch!r}, expected 'arm64'. "
                "amd64 evidence belongs on the verified local DinD endpoint "
                f"({ALLOWED_DIND_HOST})."
            )
        env = dict(os.environ)
        env.pop("DOCKER_CONTEXT", None)
        return DockerSession(env=env, mode=SOCKET_HOST_DEFAULT, hosted=True)

    if host != ALLOWED_DIND_HOST:
        raise ReleaseBlocker(
            f"DOCKER_HOST {host!r} is not the approved isolated DinD endpoint "
            f"{ALLOWED_DIND_HOST!r}; this lane never talks to other daemons."
        )
    if os.environ.get("DOCKER_TLS_VERIFY") != "1":
        raise ReleaseBlocker("DOCKER_TLS_VERIFY=1 is required for the DinD endpoint.")
    cert_path = os.environ.get("DOCKER_CERT_PATH")
    if cert_path != DIND_CERT_PATH:
        raise ReleaseBlocker(
            f"DOCKER_CERT_PATH must be {DIND_CERT_PATH!r}, got {cert_path!r}."
        )
    for cert_name in ("ca.pem", "cert.pem", "key.pem"):
        if not (Path(cert_path) / cert_name).exists():
            raise ReleaseBlocker(f"Missing DinD TLS material: {cert_path}/{cert_name}.")

    env = dict(os.environ)
    env.pop("DOCKER_CONTEXT", None)
    env.update(
        DOCKER_HOST=host,
        DOCKER_TLS_VERIFY="1",
        DOCKER_CERT_PATH=cert_path,
    )
    return DockerSession(env=env, mode=SOCKET_DIND_TLS, hosted=False)


def docker_release_env() -> dict[str, str]:
    """Backward-compatible view of :func:`docker_session` (env only)."""
    return docker_session().env
