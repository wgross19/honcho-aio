"""Regression coverage for the shipped embedding probe and its boot mapping.

The template's starter HTTP demo (and the test that drove it) is gone; the
only Python entrypoint this repo still ships is
``rootfs/usr/local/bin/honcho-probe-embedding``. These tests pin its
misconfiguration contract: invalid embedding env exits 3 *before* any
provider client is imported, and ``01-bootstrap.sh`` turns that exit 3 into a
fatal boot error (exit 64) rather than a warning.

Provider calls are deliberately not exercised. The probe imports ``openai`` /
``google.genai`` only after validation succeeds, and this lane runs off-image
where those packages do not exist.

Assumes ``/var/lib/honcho/runtime.env`` is absent, as it is off-image; that
file is written during a container boot, and unit tests never run there.
"""

from __future__ import annotations

import subprocess  # nosec B404 - runs a local, in-repo script only
import sys

import pytest

from tests.conftest import REPO_ROOT

PROBE = REPO_ROOT / "rootfs/usr/local/bin/honcho-probe-embedding"
BOOTSTRAP = REPO_ROOT / "rootfs/etc/cont-init.d/01-bootstrap.sh"

# A configuration that passes every validation gate, so each case below can
# invalidate exactly one input.
VALID_ENV = {
    "EMBEDDING_VECTOR_DIMENSIONS": "1536",
    "EMBEDDING_MODEL_CONFIG__model": "fixture-embedding",
    "EMBEDDING_MODEL_CONFIG__transport": "openai",
}


def _run_probe(**overrides: str | None) -> subprocess.CompletedProcess[str]:
    env = {**VALID_ENV, **overrides}
    return subprocess.run(  # nosec B603 - sys.executable + an in-repo script
        [sys.executable, "-I", str(PROBE)],
        env={key: value for key, value in env.items() if value is not None},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"EMBEDDING_VECTOR_DIMENSIONS": None},
            "EMBEDDING_VECTOR_DIMENSIONS not set or invalid",
        ),
        (
            {"EMBEDDING_VECTOR_DIMENSIONS": ""},
            "EMBEDDING_VECTOR_DIMENSIONS not set or invalid",
        ),
        (
            {"EMBEDDING_VECTOR_DIMENSIONS": "large"},
            "EMBEDDING_VECTOR_DIMENSIONS not set or invalid",
        ),
        (
            {"EMBEDDING_MODEL_CONFIG__model": ""},
            "EMBEDDING_MODEL_CONFIG__model not set",
        ),
        (
            {"EMBEDDING_MODEL_CONFIG__transport": ""},
            "EMBEDDING_MODEL_CONFIG__transport not set",
        ),
    ],
)
def test_misconfiguration_exits_three_before_provider_import(
    overrides: dict[str, str | None], message: str
) -> None:
    """Bad env is a misconfiguration (3), never a crash or a provider call."""
    result = _run_probe(**overrides)

    assert result.returncode == 3, result.stderr  # nosec B101
    assert message in result.stderr  # nosec B101
    # A traceback would mean validation ran after an import and the bootstrap
    # would see exit 1 ("dimension mismatch") instead of exit 3.
    assert "Traceback" not in result.stderr  # nosec B101
    assert result.stdout == ""  # nosec B101


def test_probe_ships_as_executable_container_python() -> None:
    """cont-init runs it through PATH, so the file must stay executable."""
    assert PROBE.is_file()  # nosec B101
    assert (  # nosec B101
        PROBE.stat().st_mode & 0o111
    ), "honcho-probe-embedding must be executable"
    assert (  # nosec B101
        PROBE.read_text().splitlines()[0] == "#!/opt/honcho/.venv/bin/python"
    )


def _probe_dispatch_arms() -> dict[str, str]:
    """Return the ``case "${_p}" in`` arm bodies that handle probe exits."""
    lines = BOOTSTRAP.read_text().splitlines()
    case_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip() == 'case "${_p}" in'
        ),
        None,
    )
    assert (
        case_index is not None
    ), f"no probe exit-code dispatch found in {BOOTSTRAP}"  # nosec B101

    arms: dict[str, str] = {}
    label: str | None = None
    for line in lines[case_index + 1 :]:
        stripped = line.strip()
        if stripped == "esac":
            break
        if stripped.endswith(")") and not stripped.startswith(";;"):
            label = stripped[:-1].strip().strip('"')
            arms[label] = ""
        elif label is not None:
            arms[label] += stripped + " "
    return arms


def test_bootstrap_treats_probe_misconfiguration_as_fatal() -> None:
    """Exit 3 must block boot (64); only the skip path may stay a warning."""
    arms = _probe_dispatch_arms()

    assert "3" in arms, f"bootstrap has no exit-3 arm: {sorted(arms)}"  # nosec B101
    assert (  # nosec B101
        "exit 64" in arms["3"]
    ), "bootstrap must fail the boot on probe misconfiguration"
    assert (
        "exit 1" in arms["1"]
    ), "bootstrap must fail the boot on a dimension mismatch"  # nosec B101
    assert (
        "warning:" in arms["*"]
    ), "only the skip path may degrade to a warning"  # nosec B101
