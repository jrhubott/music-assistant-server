"""The follower registry: a light that follows a leader instead of joining its group."""

from __future__ import annotations

from types import MethodType
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.controllers.players import PlayerController


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
