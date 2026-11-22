# trunk-ignore-file bandit/B101
# trunk-ignore-file bandit/B104
# trunk-ignore-file bandit/B105
"""Dockerless contract pins for honcho resource identity, from upstream source.

Verified against upstream honcho at the two pins this suite runs between:

- v3.3.0 (``ad3839db4133e282da20ded84125c93056b88a40``, the v3.3.0-aio.2
  previous image) and v3.6.2 (``e212feef9bb0e085b2a660a0181d13234dbf858b``,
  current main): every ``Workspace``/``Peer``/``Session`` response serializes
  the client-chosen name as ``id`` (pydantic ``serialization_alias`` of the
  internal ``name`` field; the JSON carries no ``name`` key — this is exactly
  what the first live run tripped over). Peer/Session additionally serialize
  ``workspace_name`` as ``workspace_id``; ``Message`` serializes ``public_id``
  as ``id`` plus ``peer_id``/``session_id``/``workspace_id``.
- v3.6.x adds ``uid``: a server-generated immutable identifier, distinct from
  the client-chosen name.
- Create-request models are identical at both pins: the field is ``name``
  with ``Field(alias="id")`` and ``populate_by_name=True``, so both versions
  accept ``{"id": <label>}`` and ``{"name": <label>}``. The suite always
  sends the alias form ``{"id": ...}``: it mirrors the serialized response
  (which carries no ``name`` key) and is the form the live runs verified on
  both versions.

The fixtures below encode exactly those shapes. Nothing loosens identity.
"""

from __future__ import annotations

import pytest

from tests.release.conftest import (
    assert_message_chain,
    assert_resource_identity,
)

pytestmark = pytest.mark.integration

REQUESTED_WS = "honcho-rq-fixture-ws"
REQUESTED_PEER = "honcho-rq-fixture-peer"
REQUESTED_SESS = "honcho-rq-fixture-sess"

# v3.3.0 response shapes (no name key, no uid).
OLD_WS_RESPONSE = {
    "id": REQUESTED_WS,
    "metadata": {},
    "configuration": {},
    "created_at": "2026-11-21T00:00:00Z",
}
OLD_PEER_RESPONSE = {
    "id": REQUESTED_PEER,
    "workspace_id": REQUESTED_WS,
    "created_at": "2026-11-21T00:00:00Z",
    "metadata": {},
    "configuration": {},
}
OLD_SESSION_RESPONSE = {
    "id": REQUESTED_SESS,
    "is_active": True,
    "workspace_id": REQUESTED_WS,
    "created_at": "2026-11-21T00:00:00Z",
    "metadata": {},
    "configuration": {},
}
# v3.6.2 adds the server-generated uid; identity fields otherwise equal.
NEW_WS_RESPONSE = {**OLD_WS_RESPONSE, "uid": "wsp_server_gen_uid_01"}
NEW_PEER_RESPONSE = {**OLD_PEER_RESPONSE, "uid": "per_server_gen_uid_02"}
NEW_SESSION_RESPONSE = {**OLD_SESSION_RESPONSE, "uid": "ses_server_gen_uid_03"}

OLD_MESSAGE_RESPONSE = {
    "id": "msg_fixture_public_id",
    "content": "fixture content",
    "peer_id": REQUESTED_PEER,
    "session_id": REQUESTED_SESS,
    "workspace_id": REQUESTED_WS,
    "metadata": {},
    "token_count": 3,  # nosec B105
    "created_at": "2026-11-21T00:00:00Z",
}


def test_workspace_identity_accepts_serialization_alias_shape() -> None:
    """Old and new images both report the chosen name under `id`."""
    for response in (OLD_WS_RESPONSE, NEW_WS_RESPONSE):
        uid = assert_resource_identity(response, REQUESTED_WS, kind="workspace")
        assert uid == response.get("uid")  # nosec B101


def test_resource_identity_refuses_wrong_id() -> None:
    """The response id must equal the label the caller addressed."""
    with pytest.raises(AssertionError, match="identity mismatch"):
        assert_resource_identity(
            {**OLD_WS_RESPONSE, "id": "someone-else"}, REQUESTED_WS
        )
    with pytest.raises(AssertionError, match="identity mismatch"):
        assert_resource_identity({"metadata": {}}, REQUESTED_WS)  # id absent


def test_resource_identity_refuses_contradictory_name_key() -> None:
    """A `name` key, when a version emits one, must agree with `id`."""
    with pytest.raises(AssertionError, match="contradictory name key"):
        assert_resource_identity(
            {**NEW_WS_RESPONSE, "name": "different-label"}, REQUESTED_WS
        )


def test_resource_identity_refuses_uid_copied_from_name() -> None:
    """v3.6.x `uid` is server-generated; it must never echo the chosen name."""
    with pytest.raises(AssertionError, match="uid must be server-generated"):
        assert_resource_identity({**NEW_WS_RESPONSE, "uid": REQUESTED_WS}, REQUESTED_WS)


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (OLD_PEER_RESPONSE, "peer"),
        (NEW_PEER_RESPONSE, "peer"),
        (OLD_SESSION_RESPONSE, "session"),
        (NEW_SESSION_RESPONSE, "session"),
    ],
)
def test_peer_and_session_identity_with_workspace_binding(
    response: dict, kind: str
) -> None:
    """Peer/session identity checks include the workspace binding."""
    label = REQUESTED_PEER if kind == "peer" else REQUESTED_SESS
    assert_resource_identity(response, label, workspace=REQUESTED_WS, kind=kind)


def test_session_id_mismatch_is_refused() -> None:
    """A session response that answers a different id must fail the probe."""
    with pytest.raises(AssertionError, match="identity mismatch"):
        assert_resource_identity(
            {**OLD_SESSION_RESPONSE, "id": REQUESTED_PEER},
            REQUESTED_SESS,
            workspace=REQUESTED_WS,
            kind="session",
        )


def test_session_workspace_binding_mismatch_is_refused() -> None:
    """A session bound to the wrong workspace must fail the probe."""
    with pytest.raises(AssertionError, match="workspace binding mismatch"):
        assert_resource_identity(
            {**OLD_SESSION_RESPONSE, "workspace_id": REQUESTED_SESS},
            REQUESTED_SESS,
            workspace=REQUESTED_WS,
            kind="session",
        )


def test_missing_workspace_binding_is_refused() -> None:
    """A response missing the required workspace_id link must fail, not pass."""
    unbound = {k: v for k, v in OLD_PEER_RESPONSE.items() if k != "workspace_id"}
    with pytest.raises(AssertionError, match="workspace binding mismatch"):
        assert_resource_identity(
            unbound,
            REQUESTED_PEER,
            workspace=REQUESTED_WS,
            kind="peer",
        )


def test_message_chain_accepts_canonical_shape() -> None:
    assert_message_chain(
        OLD_MESSAGE_RESPONSE,
        content="fixture content",
        peer=REQUESTED_PEER,
        session=REQUESTED_SESS,
        workspace=REQUESTED_WS,
        context="fixture",
    )


@pytest.mark.parametrize(
    "override",
    [
        {"content": "tampered"},
        {"peer_id": "another-peer"},
        {"session_id": "another-session"},
        {"workspace_id": "another-workspace"},
        {"peer_id": None},  # missing link in the chain must fail too
    ],
)
def test_message_chain_refuses_every_broken_link(override: dict) -> None:
    broken = {**OLD_MESSAGE_RESPONSE, **override}
    with pytest.raises(AssertionError, match="mismatch"):
        assert_message_chain(
            broken,
            content="fixture content",
            peer=REQUESTED_PEER,
            session=REQUESTED_SESS,
            workspace=REQUESTED_WS,
            context="fixture",
        )
