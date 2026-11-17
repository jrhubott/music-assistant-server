"""A visualizer client whose bridge can follow a leader instead of joining its group."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.errors import PlayerCommandFailed

import music_assistant.providers.sendspin.player as player_module
from music_assistant.models.player_protocol import PlayerProtocol
from music_assistant.providers.sendspin.player import SendspinVisualizerPlayer
from music_assistant.providers.sendspin.provider import SendspinProvider

if TYPE_CHECKING:
    from music_assistant.models.player_provider import PlayerProvider

CLIENT_ID = "hue-area"


def _provider() -> SendspinProvider:
    """Return a provider with only the follow handler registry of the real one."""
    client = SimpleNamespace(info=SimpleNamespace(player_support=None))
    provider = SimpleNamespace(
        instance_id="sendspin--test",
        logger=Mock(),
        server_api=SimpleNamespace(get_client=Mock(return_value=client)),
        _bridge_follow_handlers={},
    )
    for name in ("register_bridge_follow_handler", "get_bridge_follow_handler"):
        setattr(provider, name, getattr(SendspinProvider, name).__get__(provider))
    return cast("SendspinProvider", provider)


@pytest.fixture
def visualizer(monkeypatch: pytest.MonkeyPatch) -> SendspinVisualizerPlayer:
    """Return the visualizer player of the bridge client, without a connection."""

    def _init_player(self: PlayerProtocol, provider: PlayerProvider, protocol_id: str) -> None:
        self._provider = provider
        self._protocol_id = protocol_id
        self._player = None
        self._attr_can_group_with = set()

    monkeypatch.setattr(PlayerProtocol, "__init__", _init_player)
    monkeypatch.setattr(player_module.SendspinBasePlayer, "_refresh_client_info", Mock())
    monkeypatch.setattr(SendspinVisualizerPlayer, "_subscribe_client_callbacks", Mock())
    monkeypatch.setattr(player_module, "SendspinPlaybackSession", Mock())
    monkeypatch.setattr(PlayerProtocol, "display_name", property(lambda _self: "Light"))
    return SendspinVisualizerPlayer(_provider(), CLIENT_ID)


def _handler() -> Mock:
    """Return a follow handler whose calls are recorded."""
    return Mock(follow=AsyncMock(), unfollow=AsyncMock())


async def test_a_registered_handler_lets_the_player_follow(
    visualizer: SendspinVisualizerPlayer,
) -> None:
    """The player can follow while its bridge has a handler, and hands the calls to it."""
    handler = _handler()
    cast("SendspinProvider", visualizer.provider).register_bridge_follow_handler(CLIENT_ID, handler)

    assert visualizer.can_follow
    await visualizer.follow("leader")
    await visualizer.unfollow()

    handler.follow.assert_awaited_once_with("leader")
    handler.unfollow.assert_awaited_once()


async def test_without_a_handler_the_player_can_not_follow(
    visualizer: SendspinVisualizerPlayer,
) -> None:
    """A client without a bridge that follows refuses to follow; unfollow is a no-op."""
    assert not visualizer.can_follow
    with pytest.raises(PlayerCommandFailed):
        await visualizer.follow("leader")
    await visualizer.unfollow()


async def test_an_unregistered_handler_ends_following(visualizer: SendspinVisualizerPlayer) -> None:
    """A bridge that stops removes its handler, and the player can no longer follow."""
    provider = cast("SendspinProvider", visualizer.provider)
    provider.register_bridge_follow_handler(CLIENT_ID, _handler())
    provider.register_bridge_follow_handler(CLIENT_ID, None)

    assert not visualizer.can_follow
    assert provider.get_bridge_follow_handler(CLIENT_ID) is None
