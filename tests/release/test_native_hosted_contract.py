# trunk-ignore-file bandit/B101
# trunk-ignore-file bandit/B104
# trunk-ignore-file bandit/B105
"""Dockerless regressions for the resolved docker-endpoint modes.

The strict local path stays the verified TLS DinD wire-up; the hosted default
socket opens ONLY for the explicitly opted-in, genuine GitHub-hosted ARM64
case (ephemeral runner, runner-scoped daemon). These tests exercise
``docker_session``'s mode classification and every refusal with no docker CLI
call: cert presence is faked in a tmp dir by monkeypatching the module
constant, which the implementation reads at call time.
"""

from __future__ import annotations

import pathlib

import pytest

from tests.release import conftest
from tests.release.conftest import (
    ALLOWED_DIND_HOST,
    SOCKET_DIND_TLS,
    SOCKET_HOST_DEFAULT,
    DockerSession,
    NATIVE_RUNNER_FLAG,
    ReleaseBlocker,
    docker_session,
)

pytestmark = pytest.mark.integration

HOSTED_ENV = {
    "GITHUB_ACTIONS": "true",
    "RUNNER_ENVIRONMENT": "github-hosted",
}


def _fake_certs(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    cert_dir = tmp_path / "certs"
    cert_dir.mkdir()
    for name in ("ca.pem", "cert.pem", "key.pem"):
        (cert_dir / name).write_bytes(b"fixture-cert\n")
    monkeypatch.setattr(conftest, "DIND_CERT_PATH", str(cert_dir))
    return cert_dir


# ── DinD path: strictness preserved, mode classified ──────────────────────


def test_dind_path_classified_with_fake_certs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    cert_dir = _fake_certs(monkeypatch, tmp_path)
    monkeypatch.setenv("DOCKER_HOST", ALLOWED_DIND_HOST)
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", str(cert_dir))
    session = docker_session()
    assert isinstance(session, DockerSession)
    assert session.mode == SOCKET_DIND_TLS
    assert session.hosted is False
    assert session.env["DOCKER_HOST"] == ALLOWED_DIND_HOST
    assert session.env["DOCKER_TLS_VERIFY"] == "1"
    assert "DOCKER_CONTEXT" not in session.env


def test_dind_path_refuses_missing_cert_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    cert_dir = tmp_path / "partial-certs"
    cert_dir.mkdir()
    (cert_dir / "ca.pem").write_bytes(b"ca\n")  # cert.pem/key.pem deliberately missing
    monkeypatch.setattr(conftest, "DIND_CERT_PATH", str(cert_dir))
    monkeypatch.setenv("DOCKER_HOST", ALLOWED_DIND_HOST)
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", str(cert_dir))
    with pytest.raises(ReleaseBlocker, match="TLS material"):
        docker_session()


# ── Hosted ARM64 path: the only native case, and only for arm64 ───────────


def _opt_in_hosted(
    monkeypatch: pytest.MonkeyPatch, arch: str, markers: bool = True
) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv(NATIVE_RUNNER_FLAG, "1")
    monkeypatch.setenv("AIO_RELEASE_ARCH", arch)
    if markers:
        for key, value in HOSTED_ENV.items():
            monkeypatch.setenv(key, value)
    else:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.delenv("RUNNER_ENVIRONMENT", raising=False)


def test_hosted_arm64_opt_in_classified_host_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _opt_in_hosted(monkeypatch, "arm64")
    session = docker_session()
    assert session.mode == SOCKET_HOST_DEFAULT
    assert session.hosted is True
    assert "DOCKER_HOST" not in session.env
    assert "DOCKER_CONTEXT" not in session.env
    assert session.env["AIO_RELEASE_NATIVE_DOCKER"] == "1"
    assert session.env["GITHUB_ACTIONS"] == "true"
    assert session.env["RUNNER_ENVIRONMENT"] == "github-hosted"


@pytest.mark.parametrize("arch", ["amd64", "aarch64", None])
def test_hosted_opt_in_reserved_for_arm64(
    monkeypatch: pytest.MonkeyPatch, arch: str | None
) -> None:
    """amd64 evidence belongs on the verified local DinD, never the hosted lane."""
    _opt_in_hosted(monkeypatch, arch if arch is not None else "")  # type: ignore[arg-type]
    if arch is None:
        monkeypatch.delenv("AIO_RELEASE_ARCH", raising=False)
    with pytest.raises(ReleaseBlocker, match="reserved for the native ARM64"):
        docker_session()


# ── Context redirect: refused in both modes ───────────────────────────────


def test_context_redirect_refused_on_hosted_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _opt_in_hosted(monkeypatch, "arm64")
    monkeypatch.setenv("DOCKER_CONTEXT", "runner-context")
    with pytest.raises(ReleaseBlocker, match="DOCKER_CONTEXT"):
        docker_session()


def test_hosted_markers_still_required_before_arch_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marker refusal precedes the arch gate: the message names the markers."""
    _opt_in_hosted(monkeypatch, "arm64", markers=False)
    with pytest.raises(ReleaseBlocker, match="hosted-runner markers"):
        docker_session()
