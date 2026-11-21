"""Tests for the party fair queue and PIN host controls."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import InsufficientPermissions, InvalidDataError

from music_assistant.providers.party import (
    ATTR_PARTY_GUEST,
    ATTR_PARTY_GUEST_ID,
    ATTR_PARTY_MESSAGE,
    CONF_ENABLE_GUEST_ACCESS,
    CONF_ENABLE_HOST_CONTROLS,
    CONF_PARTY_HOST_PIN,
    HOST_PIN_LOCKOUT_SECONDS,
    HOST_PIN_MAX_FAILURES,
    HOST_PIN_MAX_TOTAL_FAILURES,
    PartyPlugin,
)

PIN = "482915"


def _item(item_id: str, guest: str | None = None, **attrs: Any) -> MagicMock:
    """Create a queue item requested by the given guest key."""
    extra: dict[str, Any] = {ATTR_PARTY_GUEST: True, **attrs}
    if guest:
        extra[ATTR_PARTY_GUEST_ID] = guest
    return MagicMock(queue_item_id=item_id, extra_attributes=extra)


def _plugin(items: list[MagicMock] | None = None, voter: str = "token:host") -> PartyPlugin:
    """Create a party plugin with host controls on and the given queue."""
    plugin = PartyPlugin.__new__(PartyPlugin)
    plugin.mass = MagicMock()
    plugin.mass.player_queues.items.return_value = items or []
    plugin.mass.player_queues.get.return_value = MagicMock(
        state=PlaybackState.PLAYING, current_index=0, index_in_buffer=0
    )
    plugin.logger = MagicMock()
    plugin.config = MagicMock()
    values = {
        CONF_ENABLE_GUEST_ACCESS: True,
        CONF_ENABLE_HOST_CONTROLS: True,
        CONF_PARTY_HOST_PIN: PIN,
    }
    plugin.config.get_value.side_effect = values.get
    plugin._queue_lock = asyncio.Lock()
    plugin._guest_key_salt = "salt"
    plugin._host_voters = set()
    plugin._blocked_guests = set()
    plugin._host_failures = {}
    plugin.get_party_player = AsyncMock(return_value="party_queue")  # type: ignore[method-assign]
    plugin._get_voter_id = AsyncMock(return_value=voter)  # type: ignore[method-assign]
    plugin.signal_provider_event = MagicMock()  # type: ignore[misc,method-assign]
    return plugin


def _mass(plugin: PartyPlugin) -> MagicMock:
    """Return the mocked server of the given plugin."""
    return cast("MagicMock", plugin.mass)


def _order(items: list[MagicMock], guest: str, name: str) -> None:
    """Insert a request where the fair queue puts it."""
    index = PartyPlugin._fair_insert_index(cast("list[Any]", items), 0, len(items), guest)
    items.insert(index, _item(name, guest))


def test_fair_queue_lets_a_new_guest_in_after_first_round() -> None:
    """A new guest plays before the second song of a guest with several requests."""
    items = [_item("a1", "a"), _item("a2", "a"), _item("a3", "a")]
    _order(items, "b", "b1")
    _order(items, "c", "c1")
    assert [i.queue_item_id for i in items] == ["a1", "b1", "c1", "a2", "a3"]


def test_fair_queue_rotates_guests() -> None:
    """Requests from two guests alternate whatever order they arrive in."""
    items: list[MagicMock] = []
    for guest, name in (("a", "a1"), ("a", "a2"), ("b", "b1"), ("b", "b2"), ("a", "a3")):
        _order(items, guest, name)
    assert [i.queue_item_id for i in items] == ["a1", "b1", "a2", "b2", "a3"]


def test_fair_queue_respects_section_start() -> None:
    """Items before the regular guest section never move."""
    items = [_item("now"), _item("boost", "a"), _item("a1", "a"), _item("a2", "a")]
    assert PartyPlugin._fair_insert_index(cast("list[Any]", items), 2, 4, "b") == 3


async def test_host_tools_need_the_pin() -> None:
    """A device that did not unlock cannot use a host command."""
    plugin = _plugin([_item("now"), _item("next", "a")])
    with pytest.raises(InsufficientPermissions):
        await plugin.host_remove_item("next")
    _mass(plugin).player_queues.delete_item.assert_not_called()


async def test_unlock_is_per_device() -> None:
    """The right PIN unlocks only the device that entered it."""
    plugin = _plugin()
    assert (await plugin.host_unlock(PIN))["success"]
    assert (await plugin.host_status())["is_host"]
    plugin._get_voter_id = AsyncMock(return_value="token:guest")  # type: ignore[method-assign]
    assert not (await plugin.host_status())["is_host"]


async def test_shared_user_identity_cannot_unlock() -> None:
    """A caller without a device token would unlock every guest, so it is refused."""
    plugin = _plugin(voter="user:party_guest")
    with pytest.raises(InsufficientPermissions):
        await plugin.host_unlock(PIN)


async def test_host_controls_off_without_valid_pin() -> None:
    """A PIN that is too short leaves host controls off."""
    plugin = _plugin()
    cast("MagicMock", plugin.config.get_value).side_effect = {
        CONF_ENABLE_GUEST_ACCESS: True,
        CONF_ENABLE_HOST_CONTROLS: True,
        CONF_PARTY_HOST_PIN: "123",
    }.get
    assert not (await plugin.host_status())["enabled"]
    with pytest.raises(InvalidDataError):
        await plugin.host_unlock("123")


async def test_device_lockout_blocks_even_the_right_pin() -> None:
    """After too many failures the correct PIN is refused until the lockout ends."""
    plugin = _plugin()
    for _ in range(HOST_PIN_MAX_FAILURES):
        assert not (await plugin.host_unlock("wrong-pin"))["success"]
    result = await plugin.host_unlock(PIN)
    assert not result["success"]
    assert 0 < result["retry_after"] <= HOST_PIN_LOCKOUT_SECONDS + 1


async def test_total_lockout_stops_rescanning_the_qr() -> None:
    """New device tokens cannot be used to keep guessing."""
    plugin = _plugin()
    for number in range(HOST_PIN_MAX_TOTAL_FAILURES):
        plugin._get_voter_id = AsyncMock(  # type: ignore[method-assign]
            return_value=f"token:{number}"
        )
        await plugin.host_unlock("wrong-pin")
    plugin._get_voter_id = AsyncMock(return_value="token:new")  # type: ignore[method-assign]
    assert not (await plugin.host_unlock(PIN))["success"]


async def test_remove_refuses_the_playing_song() -> None:
    """The host can only remove songs the player does not hold yet."""
    plugin = _plugin([_item("now"), _item("next", "a")])
    await plugin.host_unlock(PIN)
    with pytest.raises(InvalidDataError):
        await plugin.host_remove_item("now")
    await plugin.host_remove_item("next")
    _mass(plugin).player_queues.delete_item.assert_called_once_with("party_queue", "next")


async def test_mute_clears_shout_out() -> None:
    """Muting removes the message and tells the screen."""
    item = _item("now", "a", **{ATTR_PARTY_MESSAGE: "rude"})
    plugin = _plugin([item])
    await plugin.host_unlock(PIN)
    await plugin.host_mute_shout_out("now")
    assert ATTR_PARTY_MESSAGE not in item.extra_attributes
    plugin.signal_provider_event.assert_called_once_with(  # type: ignore[attr-defined]
        {"event": "shout_out_muted", "queue_item_id": "now"}
    )


async def test_block_removes_upcoming_and_stops_the_guest() -> None:
    """Blocking removes only upcoming songs of that guest and refuses their actions."""
    plugin = _plugin()
    guest_key = plugin._guest_key("token:guest")
    _mass(plugin).player_queues.items.return_value = [
        _item("now", guest_key),
        _item("theirs", guest_key),
        _item("other", "someone"),
    ]
    await plugin.host_unlock(PIN)
    assert (await plugin.host_block_guest("theirs"))["removed"] == 1
    _mass(plugin).player_queues.delete_item.assert_called_once_with("party_queue", "theirs")
    with pytest.raises(InsufficientPermissions):
        plugin._raise_if_blocked("token:guest")
    await plugin.host_unblock_all()
    plugin._raise_if_blocked("token:guest")


async def test_host_cannot_be_blocked() -> None:
    """A host cannot lock themselves or another host out."""
    plugin = _plugin()
    _mass(plugin).player_queues.items.return_value = [
        _item("now"),
        _item("mine", plugin._guest_key("token:host")),
    ]
    await plugin.host_unlock(PIN)
    with pytest.raises(InvalidDataError):
        await plugin.host_block_guest("mine")
