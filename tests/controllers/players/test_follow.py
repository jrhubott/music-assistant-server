"""The follower registry: a light that follows a leader instead of joining its group."""

from __future__ import annotations

from types import MethodType
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import PlayerType
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.controllers.players import PlayerController
from music_assistant.models.player import Player
from tests.common import MockProtocol, MockProvider, attach_mock


def _player(player_id: str, *, can_follow: bool = True) -> MagicMock:
    """Return a player whose own protocol can follow, its calls recorded."""
    player = MagicMock(player_id=player_id)
    player.own_protocol.can_follow = can_follow
    player.own_protocol.follow = AsyncMock()
    player.own_protocol.unfollow = AsyncMock()
    return player


def _controller(*players: MagicMock) -> MagicMock:
    """Return a players controller holding the given players, with its real follow methods."""
    controller = MagicMock()
    controller._follows = {}
    by_id = {player.player_id: player for player in players}
    controller.get_player = MagicMock(side_effect=by_id.get)
    for name in (
        "_follow",
        "_unfollow",
        "_update_follow_states",
        "followed_player",
        "followers_of",
    ):
        setattr(controller, name, MethodType(getattr(PlayerController, name), controller))
    return controller


async def test_follow_registers_and_starts_the_follower() -> None:
    """Following calls the follower's protocol and publishes both players."""
    leader, light = MagicMock(player_id="leader"), _player("light")
    controller = _controller(leader, light)

    await controller._follow("leader", "light")

    light.own_protocol.follow.assert_awaited_once_with("leader")
    assert controller.followed_player("light") == "leader"
    assert controller.followers_of("leader") == ["light"]
    leader.update_state.assert_called_once()
    light.update_state.assert_called_once()


async def test_following_the_same_leader_again_does_nothing() -> None:
    """A repeated follow of the same leader makes no second call."""
    light = _player("light")
    controller = _controller(MagicMock(player_id="leader"), light)

    await controller._follow("leader", "light")
    await controller._follow("leader", "light")

    light.own_protocol.follow.assert_awaited_once()


async def test_following_another_leader_leaves_the_first() -> None:
    """A follower moving to another leader stops following the first one."""
    light = _player("light")
    controller = _controller(MagicMock(player_id="a"), MagicMock(player_id="b"), light)

    await controller._follow("a", "light")
    await controller._follow("b", "light")

    light.own_protocol.unfollow.assert_awaited_once()
    assert controller.followers_of("a") == []
    assert controller.followers_of("b") == ["light"]


@pytest.mark.parametrize("known", [True, False], ids=["can-not-follow", "unknown"])
async def test_a_player_that_can_not_follow_is_refused(known: bool) -> None:
    """Following is refused for a player whose protocol can not follow, or that is gone."""
    players = [_player("light", can_follow=False)] if known else []
    controller = _controller(*players)

    with pytest.raises(PlayerCommandFailed):
        await controller._follow("leader", "light")
    assert controller._follows == {}


async def test_unfollow_stops_the_follower() -> None:
    """Unfollowing calls the protocol and drops the follower from the registry."""
    leader, light = MagicMock(player_id="leader"), _player("light")
    controller = _controller(leader, light)
    await controller._follow("leader", "light")

    await controller._unfollow("light")

    light.own_protocol.unfollow.assert_awaited_once()
    assert controller.followed_player("light") is None
    assert controller.followers_of("leader") == []


async def test_a_failing_unfollow_still_leaves_the_registry() -> None:
    """A protocol that fails to stop does not keep the follower in the leader's group."""
    light = _player("light")
    light.own_protocol.unfollow.side_effect = PlayerCommandFailed("gone")
    controller = _controller(MagicMock(player_id="leader"), light)
    await controller._follow("leader", "light")

    with pytest.raises(PlayerCommandFailed):
        await controller._unfollow("light")

    assert controller.followers_of("leader") == []


async def test_unfollowing_a_player_that_follows_nothing_does_nothing() -> None:
    """Unfollow of a player that follows no one makes no call."""
    light = _player("light")
    controller = _controller(light)

    await controller._unfollow("light")

    light.own_protocol.unfollow.assert_not_awaited()


@pytest.fixture
def real_controller() -> PlayerController:
    """Return a players controller on a mocked server."""
    mass = MagicMock()
    mass.closing = False
    mass.loop = None
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_player_value = MagicMock(
        side_effect=lambda _player_id, _key, default=None: default
    )
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.get_providers = MagicMock(return_value=[])
    mass.player_queues.get = MagicMock(return_value=None)
    controller = PlayerController(mass)
    mass.players = controller
    return controller


def _follower_and_leader(controller: PlayerController) -> tuple[MockProtocol, str, str]:
    """
    Attach a leader and a light, and let the light follow the leader.

    :return: The leader's protocol, the leader's id and the light's id.
    """
    mass = cast("MagicMock", controller.mass)
    provider = MockProvider("mock_provider", instance_id="mock_instance", mass=mass)
    leader_player, leader = attach_mock(provider, "leader", "Patio")
    light_player, _ = attach_mock(provider, "light", "Patio Lights")
    controller._follows[light_player.player_id] = leader_player.player_id
    leader_player.refresh_state(signal_event=False)
    light_player.refresh_state(signal_event=False)
    return leader, leader_player.player_id, light_player.player_id


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("cmd_play", ()),
        ("cmd_pause", ()),
        ("cmd_stop", ()),
        ("cmd_next_track", ()),
        ("cmd_previous_track", ()),
        ("cmd_seek", (30,)),
    ],
)
async def test_playback_commands_on_a_follower_are_refused(
    real_controller: PlayerController, command: str, args: tuple[object, ...]
) -> None:
    """A command for the playback of a light that follows fails and reaches no player."""
    leader, _, light_id = _follower_and_leader(real_controller)
    with (
        patch.object(leader, "pause", AsyncMock()) as leader_pause,
        patch.object(leader, "stop", AsyncMock()) as leader_stop,
        pytest.raises(PlayerCommandFailed, match="follows Patio"),
    ):
        await getattr(real_controller, command)(light_id, *args)
    leader_pause.assert_not_awaited()
    leader_stop.assert_not_awaited()


async def test_play_media_on_a_follower_is_refused(real_controller: PlayerController) -> None:
    """Media sent to a light that follows is refused rather than played by its leader."""
    _, _, light_id = _follower_and_leader(real_controller)
    with pytest.raises(PlayerCommandFailed, match="follows Patio"):
        await real_controller.play_media(light_id, MagicMock())


def test_the_leader_of_a_follower_is_not_refused(real_controller: PlayerController) -> None:
    """Only the follower is refused: its leader is controlled as usual."""
    _, leader_id, _ = _follower_and_leader(real_controller)
    leader = real_controller.get_player(leader_id)
    assert leader is not None
    real_controller._refuse_follower(leader)


@pytest.mark.parametrize(
    ("leader_domain", "expected"),
    [("sonos", frozenset({"light"})), ("airplay", frozenset())],
    ids=["sonos-leader", "other-leader"],
)
def test_a_leader_offers_only_lights_that_can_follow_its_protocol(
    leader_domain: str, expected: frozenset[str]
) -> None:
    """A speaker lists a light it shares no protocol with only when the light follows its own."""
    light = MagicMock(player_id="light")
    light.own_protocol.follow_domains = frozenset({"sonos"})
    light.own_protocol.player = light
    leader = MagicMock(player_id="leader", reported_type=PlayerType.PLAYER)
    leader.own_protocol.provider.domain = leader_domain
    leader.mass.players.followed_player = MagicMock(return_value=None)
    leader.mass.players.followers_of = MagicMock(return_value=[])
    leader.mass.players.iter_protocols = MagicMock(return_value=[light.own_protocol])

    _, _, followable = Player._collect_follows(leader, {}, True)

    assert followable == expected
