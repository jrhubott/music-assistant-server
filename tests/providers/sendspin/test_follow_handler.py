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
    from music_assistant_models.config_entries import PlayerConfig

    from music_assistant.models.player_provider import PlayerProvider

CLIENT_ID = "hue-area"


def _provider() -> SendspinProvider:
    """Return a provider with only the follow handler registry of the real one."""
    client = SimpleNamespace(info=SimpleNamespace(player_support=None))
    provider = SimpleNamespace(
        instance_id="sendspin--test",
        mass=SimpleNamespace(get_provider=Mock(return_value=None)),
        logger=Mock(),
        server_api=SimpleNamespace(get_client=Mock(return_value=client)),
        sources=Mock(),
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
        self.mass = provider.mass
        self._protocol_id = protocol_id
        self._player = None
        self._attr_can_group_with = set()
        self._config = cast(
            "PlayerConfig",
            SimpleNamespace(get_value=lambda key, default=None: follow_config.get(key, default)),
        )

    follow_config: dict[str, int] = {}

    monkeypatch.setattr(PlayerProtocol, "__init__", _init_player)
    monkeypatch.setattr(player_module.SendspinBasePlayer, "_refresh_client_info", Mock())
    monkeypatch.setattr(SendspinVisualizerPlayer, "_subscribe_client_callbacks", Mock())
    monkeypatch.setattr(player_module, "SendspinPlaybackSession", Mock())
    monkeypatch.setattr(PlayerProtocol, "display_name", property(lambda _self: "Light"))
    player = SendspinVisualizerPlayer(_provider(), CLIENT_ID)
    player.follow_config = follow_config  # type: ignore[attr-defined]
    return player


def _handler() -> Mock:
    """Return a follow handler whose calls are recorded."""
    return Mock(
        follow=AsyncMock(),
        unfollow=AsyncMock(),
        set_follow_delay=Mock(),
        follow_domains=frozenset({"sonos"}),
    )


async def test_a_registered_handler_lets_the_player_follow(
    visualizer: SendspinVisualizerPlayer,
) -> None:
    """The player can follow while its bridge has a handler, and hands the calls to it."""
    handler = _handler()
    cast("SendspinProvider", visualizer.provider).register_bridge_follow_handler(CLIENT_ID, handler)

    assert visualizer.can_follow
    assert visualizer.follow_domains == {"sonos"}
    await visualizer.follow("leader")
    await visualizer.unfollow()

    handler.follow.assert_awaited_once_with("leader")
    handler.unfollow.assert_awaited_once()


async def test_without_a_handler_the_player_can_not_follow(
    visualizer: SendspinVisualizerPlayer,
) -> None:
    """A client without a bridge that follows refuses to follow; unfollow is a no-op."""
    assert not visualizer.can_follow
    assert visualizer.follow_domains == frozenset()
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


async def test_the_follow_delay_goes_to_the_handler(visualizer: SendspinVisualizerPlayer) -> None:
    """The configured delay reaches the bridge before it follows, and again when changed."""
    handler = _handler()
    cast("SendspinProvider", visualizer.provider).register_bridge_follow_handler(CLIENT_ID, handler)
    follow_config = visualizer.follow_config  # type: ignore[attr-defined]
    follow_config[player_module.CONF_FOLLOW_DELAY_MS] = 250

    await visualizer.follow("leader")
    handler.set_follow_delay.assert_called_with(250)

    follow_config[player_module.CONF_FOLLOW_DELAY_MS] = -100
    await visualizer.on_config_updated()
    handler.set_follow_delay.assert_called_with(-100)


async def test_only_a_player_that_can_follow_offers_the_delay(
    visualizer: SendspinVisualizerPlayer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The follow delay setting is shown only while the bridge can follow."""
    monkeypatch.setattr(
        player_module.SendspinBasePlayer, "get_config_entries", AsyncMock(return_value=[])
    )
    assert not [
        entry
        for entry in await visualizer.get_config_entries()
        if entry.key == player_module.CONF_FOLLOW_DELAY_MS
    ]
    cast("SendspinProvider", visualizer.provider).register_bridge_follow_handler(
        CLIENT_ID, _handler()
    )
    assert [
        entry
        for entry in await visualizer.get_config_entries()
        if entry.key == player_module.CONF_FOLLOW_DELAY_MS
    ]
