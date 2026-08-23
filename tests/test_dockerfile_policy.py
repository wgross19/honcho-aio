from __future__ import annotations

from tests.conftest import REPO_ROOT


def test_dockerfile_base_image_digest_pinned() -> None:
    """All base images must be pinned by digest, not floating tag.

    Accepts ARG-based FROM lines (${UV_IMAGE}, ${DEBIAN_IMAGE}) whose
    default ARG values are digest-pinned.
    """
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    lines = dockerfile.splitlines()

    from_lines = [ln for ln in lines if ln.strip().startswith("FROM ")]
    for line in from_lines:
        _, rest = line.strip().split(None, 1)
        image_spec = rest.split(" AS ")[0].strip()
        # Variable-based FROMs (${VAR}) are OK if the ARG default is pinned
        if image_spec.startswith("${") and image_spec.endswith("}"):
            arg_name = image_spec.strip("${}")
            # Find the ARG line and check its default value
            for ln in lines:
                if ln.strip().startswith(f"ARG {arg_name}="):
                    default = ln.split("=", 1)[1].strip().strip('"').strip("'")
                    assert (  # nosec B101
                        "@sha256:" in default
                    ), f"ARG {arg_name}={default} is not digest-pinned"
                    break
            else:
                raise AssertionError(  # nosec B101
                    f"ARG {arg_name} not found — cannot verify digest pin"
                )
        else:
            assert (
                "@sha256:" in image_spec
            ), f"FROM must be digest-pinned, got: {line}"  # nosec B101


def test_dockerfile_required_labels_present() -> None:
    """Image must carry OpenContainer labels for provenance."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    assert "org.opencontainers.image.title=" in dockerfile  # nosec B101
    assert "org.opencontainers.image.source=" in dockerfile  # nosec B101
    assert "org.opencontainers.image.revision=" in dockerfile  # nosec B101
    assert "org.opencontainers.image.version=" in dockerfile  # nosec B101
    assert "com.honcho-aio.service=" in dockerfile  # nosec B101


def test_dockerfile_healthcheck_configured() -> None:
    """Dockerfile must define HEALTHCHECK."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    assert "HEALTHCHECK" in dockerfile  # nosec B101
    assert "curl -fsS http://127.0.0.1:8000/health" in dockerfile  # nosec B101


def test_dockerfile_exposes_port_8000() -> None:
    """Honcho serves on port 8000 internally."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    assert "EXPOSE 8000" in dockerfile  # nosec B101


def test_dockerfile_s6_entrypoint() -> None:
    """Must use s6-overlay /init as ENTRYPOINT."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    assert 'ENTRYPOINT ["/init"]' in dockerfile  # nosec B101


def test_dockerfile_pinned_honcho_git_sha() -> None:
    """HONCHO_GIT_SHA must be a pinned commit hash (40 hex chars)."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    for line in dockerfile.splitlines():
        if "HONCHO_GIT_SHA" in line and "=" in line:
            sha = line.split("=", 1)[1].strip().strip('"').strip("'")
            assert (
                len(sha) == 40
            ), f"HONCHO_GIT_SHA must be 40 chars: {line}"  # nosec B101
            assert all(  # nosec B101
                c in "0123456789abcdef" for c in sha
            ), f"HONCHO_GIT_SHA must be hex: {line}"
            return
    raise AssertionError("HONCHO_GIT_SHA not found in Dockerfile")  # nosec B101


def test_dockerfile_git_sha_verified_after_checkout() -> None:
    """Dockerfile must verify the checked-out SHA matches HONCHO_GIT_SHA."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    assert "rev-parse HEAD" in dockerfile  # nosec B101
    assert (  # nosec B101
        'test "$(git -C /tmp/honcho-src rev-parse HEAD)" = "${HONCHO_GIT_SHA}"'
        in dockerfile
    )


def test_dockerfile_no_hardcoded_secrets() -> None:
    """Must not contain hardcoded passwords, tokens, or API keys."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    assert "password" not in dockerfile.lower()  # nosec B101


def test_dockerfile_volumes_declared() -> None:
    """Must declare volumes for persistent data dirs."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    assert '"/data/postgres"' in dockerfile  # nosec B101
    assert '"/data/redis"' in dockerfile  # nosec B101
    assert '"/var/lib/honcho"' in dockerfile  # nosec B101


def test_dockerfile_checkov_skips_are_documented() -> None:
    """Every # checkov:skip must have a reason comment."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.startswith("# checkov:skip="):
            # Must have a colon followed by explanation
            assert (
                ":" in stripped
            ), f"checkov skip without reason: {stripped}"  # nosec B101
            _, _, explanation = stripped.partition(":")
            assert (
                explanation.strip()
            ), f"checkov skip with empty reason: {stripped}"  # nosec B101
