# trunk-ignore-file bandit/B101
# trunk-ignore-file bandit/B104
# trunk-ignore-file bandit/B105
"""Dockerless regressions for the release endpoint contract (tests.release.conftest).

These tests touch only environment variables and the contract functions — no
docker CLI call is ever made. The Docker-context redirect refusal and the
hosted-marker requirement for the native opt-in must both hold in every mode,
including the modes that exist so the lane can never silently re-point itself
at an unapproved daemon.
"""

from __future__ import annotations

import pytest

from tests.release.conftest import (
    ALLOWED_DIND_HOST,
    DIND_CERT_PATH,
    HOSTED_ACTIONS_VAR,
    HOSTED_RUNNER_VALUE,
    NATIVE_RUNNER_FLAG,
    RUNNER_ENVIRONMENT_VAR,
    ReleaseBlocker,
    docker_release_env,
)

pytestmark = pytest.mark.integration

HOSTED_ENV = {
    HOSTED_ACTIONS_VAR: "true",
    RUNNER_ENVIRONMENT_VAR: HOSTED_RUNNER_VALUE,
}


def _approve_dind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCKER_HOST", ALLOWED_DIND_HOST)
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", DIND_CERT_PATH)


# ── DOCKER_CONTEXT redirects ───────────────────────────────────────────────


def test_docker_env_rejects_docker_context_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-empty DOCKER_CONTEXT redirects the CLI regardless of DOCKER_HOST."""
    _approve_dind(monkeypatch)
    monkeypatch.setenv("DOCKER_CONTEXT", "remote-prod")
    with pytest.raises(ReleaseBlocker, match="DOCKER_CONTEXT"):
        docker_release_env()


def test_docker_context_redirect_refused_even_for_native_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hosted-runner opt-in does not launder a set docker context."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv(NATIVE_RUNNER_FLAG, "1")
    for key, value in HOSTED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DOCKER_CONTEXT", "runner-something")
    with pytest.raises(ReleaseBlocker, match="DOCKER_CONTEXT"):
        docker_release_env()


def test_docker_env_strips_docker_context_from_returned_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty DOCKER_CONTEXT is tolerated but never carried through."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv(NATIVE_RUNNER_FLAG, "1")
    for key, value in HOSTED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DOCKER_CONTEXT", "")
    monkeypatch.setenv("AIO_RELEASE_ARCH", "arm64")  # hosted mode is ARM64-only
    env = docker_release_env()
    assert "DOCKER_CONTEXT" not in env
    assert env[NATIVE_RUNNER_FLAG] == "1"


# ── Native opt-in requires the genuine hosted markers ─────────────────────


def test_native_opt_in_requires_genuine_hosted_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare AIO_RELEASE_NATIVE_DOCKER=1 without markers is not an endpoint."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv(NATIVE_RUNNER_FLAG, "1")
    monkeypatch.delenv(HOSTED_ACTIONS_VAR, raising=False)
    monkeypatch.delenv(RUNNER_ENVIRONMENT_VAR, raising=False)
    with pytest.raises(ReleaseBlocker, match="hosted-runner markers"):
        docker_release_env()


@pytest.mark.parametrize(
    ("actions_value", "runner_value"),
    [
        ("1", HOSTED_RUNNER_VALUE),           # wrong truthy spelling
        ("true", "self-hosted"),              # actions right, runner wrong
        ("TRUE", HOSTED_RUNNER_VALUE),        # case-sensitive values
        ("true", "GitHub-hosted"),            # case-sensitive runner value
        ("", HOSTED_RUNNER_VALUE),            # empty marker is not genuine
    ],
)
def test_native_opt_in_rejects_marker_value_variants(
    monkeypatch: pytest.MonkeyPatch, actions_value: str, runner_value: str
) -> None:
    """Only the exact genuine marker pair enables the hosted default socket."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv(NATIVE_RUNNER_FLAG, "1")
    monkeypatch.setenv(HOSTED_ACTIONS_VAR, actions_value)
    monkeypatch.setenv(RUNNER_ENVIRONMENT_VAR, runner_value)
    with pytest.raises(ReleaseBlocker, match="hosted-runner markers"):
        docker_release_env()


def test_native_opt_in_accepts_genuine_hosted_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact marker pair plus the opt-in plus arm64 is the one native case."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv(NATIVE_RUNNER_FLAG, "1")
    monkeypatch.setenv("AIO_RELEASE_ARCH", "arm64")  # hosted mode is ARM64-only
    for key, value in HOSTED_ENV.items():
        monkeypatch.setenv(key, value)
    env = docker_release_env()
    assert env[NATIVE_RUNNER_FLAG] == "1"
    assert env[HOSTED_ACTIONS_VAR] == "true"
    assert env[RUNNER_ENVIRONMENT_VAR] == HOSTED_RUNNER_VALUE


# ── Prior negatives still hold under the hardened contract ────────────────


def test_docker_env_rejects_unapproved_daemon_with_context_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unapproved DOCKER_HOST is refused whether or not a context is set."""
    monkeypatch.setenv("DOCKER_HOST", "tcp://something-else:2376")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", DIND_CERT_PATH)
    with pytest.raises(ReleaseBlocker, match="not the approved isolated DinD"):
        docker_release_env()


def test_native_flag_without_markers_beats_missing_flag_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag set but no DOCKER_HOST and no markers: refuse with marker guidance."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv(NATIVE_RUNNER_FLAG, "1")
    monkeypatch.setenv(HOSTED_ACTIONS_VAR, "true")
    monkeypatch.delenv(RUNNER_ENVIRONMENT_VAR, raising=False)
    with pytest.raises(ReleaseBlocker, match=RUNNER_ENVIRONMENT_VAR):
        docker_release_env()
