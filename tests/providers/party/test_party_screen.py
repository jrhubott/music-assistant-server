"""Tests for the party screen extras: nicknames, shout-outs, reactions and the leaderboard."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.party import (
    ATTR_PARTY_BOOSTED,
    ATTR_PARTY_GUEST,
    ATTR_PARTY_GUEST_NAME,
    ATTR_PARTY_MESSAGE,
    CONF_ENABLE_ADD_QUEUE,
    CONF_ENABLE_BOOST,
    CONF_ENABLE_GUEST_ACCESS,
    CONF_ENABLE_GUEST_NAMES,
    CONF_ENABLE_REACTIONS,
    CONF_ENABLE_SHOUT_OUTS,
    CONF_ENABLE_SKIP_SONG,
    CONF_PREVENT_DUPLICATE_TRACKS,
    CONF_SHOW_COPY_LINK,
    CONF_SHOW_LEADERBOARD,
    CONF_SHOW_REQUEST_ANIMATIONS,
    CONF_SHOW_SKIP_METER,
    CONF_SHOW_TIPS,
    LEADERBOARD_SIZE,
    MAX_GUEST_NAME_LENGTH,
    MAX_SHOUT_OUT_LENGTH,
    PartyPlugin,
)


def _create_plugin(**overrides: Any) -> PartyPlugin:
    """Create a party plugin with a playing queue and all party screen extras on."""
    plugin = PartyPlugin.__new__(PartyPlugin)
    plugin.mass = MagicMock()
    plugin.mass.player_queues = MagicMock()
    plugin.mass.player_queues.load = AsyncMock()
    plugin.mass.player_queues.items.return_value = []
    plugin.mass.player_queues.get.return_value = MagicMock(
        state=PlaybackState.PLAYING, current_index=0, index_in_buffer=0
    )
    media_item = MagicMock(media_type=MediaType.TRACK)
    plugin.mass.music.get_item_by_uri = AsyncMock(return_value=media_item)
    plugin.logger = MagicMock()
    plugin.config = MagicMock()
    plugin._queue_lock = asyncio.Lock()
    plugin._session = None
    plugin._session_lock = asyncio.Lock()
    plugin._guest_stats = {}
    plugin._last_reaction = {}
    plugin.get_party_player = AsyncMock(return_value="party_queue")  # type: ignore[method-assign]
    plugin._get_voter_id = AsyncMock(return_value="token:a")  # type: ignore[method-assign]
    config_values: dict[str, Any] = {
        CONF_ENABLE_GUEST_ACCESS: True,
        CONF_ENABLE_ADD_QUEUE: True,
        CONF_ENABLE_BOOST: True,
        CONF_ENABLE_SKIP_SONG: True,
        CONF_PREVENT_DUPLICATE_TRACKS: False,
        CONF_ENABLE_GUEST_NAMES: True,
        CONF_ENABLE_SHOUT_OUTS: True,
        CONF_ENABLE_REACTIONS: True,
        CONF_SHOW_LEADERBOARD: True,
        CONF_SHOW_SKIP_METER: True,
        CONF_SHOW_REQUEST_ANIMATIONS: True,
        CONF_SHOW_TIPS: True,
        CONF_SHOW_COPY_LINK: False,
        **overrides,
    }
    cast("MagicMock", plugin.config.get_value).side_effect = config_values.get
    return plugin


async def _add(plugin: PartyPlugin, **kwargs: Any) -> dict[str, Any]:
    """Add a track as a guest and return the extra attributes of the queued item."""
    queue_item = MagicMock(extra_attributes={})
    with patch("music_assistant.providers.party.build_queue_item", return_value=queue_item):
        await plugin.add_to_queue("spotify://track/1", **kwargs)
    return cast("dict[str, Any]", queue_item.extra_attributes)


def _events(plugin: PartyPlugin) -> list[dict[str, Any]]:
    """Return the payloads of the provider events the plugin signalled."""
    signal = cast("MagicMock", plugin.mass.signal_event)
    return [call.kwargs["data"] for call in signal.call_args_list]


@pytest.mark.asyncio
async def test_request_carries_guest_name_and_shout_out() -> None:
    """A guest's nickname and shout-out are stored on the queued item."""
    plugin = _create_plugin()

    attrs = await _add(plugin, guest_name="Sam", message="for Mike's birthday")

    assert attrs[ATTR_PARTY_GUEST] is True
    assert attrs[ATTR_PARTY_GUEST_NAME] == "Sam"
    assert attrs[ATTR_PARTY_MESSAGE] == "for Mike's birthday"


@pytest.mark.asyncio
async def test_name_and_shout_out_are_ignored_when_disabled() -> None:
    """Turning the options off keeps names and messages off the party screen."""
    plugin = _create_plugin(**{CONF_ENABLE_GUEST_NAMES: False, CONF_ENABLE_SHOUT_OUTS: False})

    attrs = await _add(plugin, guest_name="Sam", message="hello")

    assert ATTR_PARTY_GUEST_NAME not in attrs
    assert ATTR_PARTY_MESSAGE not in attrs
    assert plugin._guest_stats == {}


@pytest.mark.asyncio
async def test_guest_text_is_cleaned_and_shortened() -> None:
    """Control characters and runs of whitespace are removed and long text is cut."""
    plugin = _create_plugin()

    attrs = await _add(
        plugin,
        guest_name="  Sam\n\u202e" + "x" * 50,
        message="line one\r\n\tline two " + "y" * 200,
    )

    assert attrs[ATTR_PARTY_GUEST_NAME].startswith("Sam x")
    assert len(attrs[ATTR_PARTY_GUEST_NAME]) <= MAX_GUEST_NAME_LENGTH
    assert attrs[ATTR_PARTY_MESSAGE].startswith("line one line two y")
    assert len(attrs[ATTR_PARTY_MESSAGE]) <= MAX_SHOUT_OUT_LENGTH


@pytest.mark.asyncio
async def test_blank_name_is_not_stored() -> None:
    """A name of only whitespace counts as no name."""
    plugin = _create_plugin()

    attrs = await _add(plugin, guest_name=" \n ")

    assert ATTR_PARTY_GUEST_NAME not in attrs


@pytest.mark.asyncio
async def test_requests_and_boosts_build_the_leaderboard() -> None:
    """Each guest is counted by device and ranked by requests plus boosts."""
    plugin = _create_plugin()
    await _add(plugin, guest_name="Sam")
    await _add(plugin, guest_name="Sam", boost=True)
    cast("AsyncMock", plugin._get_voter_id).return_value = "token:b"
    await _add(plugin, guest_name="Alex")

    leaderboard = await plugin.get_leaderboard()

    assert [(e.name, e.requests, e.boosts, e.score) for e in leaderboard] == [
        ("Sam", 2, 1, 3),
        ("Alex", 1, 0, 1),
    ]
    assert _events(plugin)[-1]["event"] == "leaderboard_updated"


@pytest.mark.asyncio
async def test_leaderboard_uses_the_latest_nickname_and_is_capped() -> None:
    """A guest that renames shows up once, under the new name, and only the top guests show."""
    plugin = _create_plugin()
    await _add(plugin, guest_name="Sam")
    await _add(plugin, guest_name="DJ Sam")
    for index in range(LEADERBOARD_SIZE + 2):
        cast("AsyncMock", plugin._get_voter_id).return_value = f"token:{index}"
        await _add(plugin, guest_name=f"Guest {index}")

    leaderboard = await plugin.get_leaderboard()

    assert len(leaderboard) == LEADERBOARD_SIZE
    assert leaderboard[0].name == "DJ Sam"


@pytest.mark.asyncio
async def test_leaderboard_is_empty_when_guest_names_are_off() -> None:
    """Without nicknames there is no leaderboard."""
    plugin = _create_plugin()
    await _add(plugin, guest_name="Sam")
    cast("MagicMock", plugin.config.get_value).side_effect = {
        CONF_ENABLE_GUEST_ACCESS: True,
        CONF_ENABLE_GUEST_NAMES: False,
    }.get

    assert await plugin.get_leaderboard() == []


@pytest.mark.asyncio
async def test_boost_names_an_item_without_requester() -> None:
    """Boosting a song nobody requested puts the booster's name on it, but not over a requester."""
    plugin = _create_plugin()
    playing = MagicMock(queue_item_id="playing", extra_attributes={})
    unnamed = MagicMock(queue_item_id="unnamed", extra_attributes={})
    named = MagicMock(queue_item_id="named", extra_attributes={ATTR_PARTY_GUEST_NAME: "Alex"})
    cast("MagicMock", plugin.mass.player_queues.items).return_value = [playing, unnamed, named]

    with patch("music_assistant.providers.party.committed_index", return_value=0):
        await plugin.boost_queue_item("unnamed", guest_name="Sam")
        await plugin.boost_queue_item("named", guest_name="Sam")

    assert unnamed.extra_attributes[ATTR_PARTY_GUEST_NAME] == "Sam"
    assert unnamed.extra_attributes[ATTR_PARTY_BOOSTED] is True
    assert named.extra_attributes[ATTR_PARTY_GUEST_NAME] == "Alex"
    assert plugin._guest_stats["token:a"].boosts == 2


@pytest.mark.asyncio
async def test_request_is_announced_to_the_party_screen() -> None:
    """The party screen hears about each request, with the guest's nickname."""
    plugin = _create_plugin()
    queue_item = MagicMock(extra_attributes={}, queue_item_id="qi1")
    queue_item.name = "Artist - Song"

    with patch("music_assistant.providers.party.build_queue_item", return_value=queue_item):
        await plugin.add_to_queue("spotify://track/1", boost=True, guest_name="Sam")

    assert _events(plugin)[0] == {
        "event": "guest_request",
        "queue_item_id": "qi1",
        "title": "Artist - Song",
        "name": "Sam",
        "boosted": True,
    }


@pytest.mark.asyncio
async def test_reaction_is_sent_to_the_party_screen() -> None:
    """A known reaction is signalled as a provider event."""
    plugin = _create_plugin()

    result = await plugin.react("🔥")

    assert result == {"success": True, "shown": True}
    assert _events(plugin) == [{"event": "reaction", "emoji": "🔥"}]


@pytest.mark.asyncio
async def test_reactions_faster_than_a_tap_are_dropped() -> None:
    """A guest flooding reactions only gets one through per interval."""
    plugin = _create_plugin()

    with patch("music_assistant.providers.party.time.monotonic", side_effect=[100.0, 100.1, 101]):
        results = [await plugin.react("🔥") for _ in range(3)]

    assert [result["shown"] for result in results] == [True, False, True]
    assert len(_events(plugin)) == 2


@pytest.mark.asyncio
async def test_unknown_or_disabled_reactions_are_refused() -> None:
    """Only the listed emoji can be sent, and only while reactions are on."""
    plugin = _create_plugin()
    with pytest.raises(InvalidDataError, match="Unknown reaction"):
        await plugin.react("<script>")

    plugin = _create_plugin(**{CONF_ENABLE_REACTIONS: False})
    with pytest.raises(InvalidDataError, match="disabled"):
        await plugin.react("🔥")


@pytest.mark.asyncio
async def test_party_config_exposes_the_screen_options() -> None:
    """The party config tells the frontend which extras to show."""
    plugin = _create_plugin()

    config = await plugin.get_party_config()

    assert config.enable_guest_names is True
    assert config.enable_shout_outs is True
    assert config.enable_reactions is True
    assert "🔥" in config.reactions
    assert config.show_leaderboard is True
    assert config.show_skip_meter is True
    assert config.show_request_animations is True
    assert config.show_tips is True
    assert config.show_copy_link is False


@pytest.mark.asyncio
async def test_party_config_hides_extras_whose_feature_is_off() -> None:
    """The leaderboard needs nicknames and the skip meter needs skipping."""
    plugin = _create_plugin(**{CONF_ENABLE_GUEST_NAMES: False, CONF_ENABLE_SKIP_SONG: False})

    config = await plugin.get_party_config()

    assert config.show_leaderboard is False
    assert config.show_skip_meter is False
