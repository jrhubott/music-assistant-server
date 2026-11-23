"""Tests for the party setting that only plays songs with lyrics."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import PlaybackState, ProviderFeature
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Track

from music_assistant.providers.party import (
    CONF_ENABLE_ADD_QUEUE,
    CONF_ENABLE_BOOST,
    CONF_ENABLE_GUEST_ACCESS,
    CONF_REQUIRE_LYRICS,
    LYRICS_MAX_CONSECUTIVE_SKIPS,
    LYRICS_MAX_REMOVED_AT_ONCE,
    REASON_NO_LYRICS,
    PartyPlugin,
)


def _track(name: str, lyrics: str | None = None) -> MagicMock:
    """Create a track with or without stored lyrics."""
    track = MagicMock(spec=Track)
    track.name = name
    track.uri = f"test://track/{name}"
    track.metadata = MagicMock(lyrics=lyrics, lrc_lyrics=None)
    return track


def _queue_item(item_id: str, track: Any) -> MagicMock:
    """Create a queue item for the given track."""
    item = MagicMock(queue_item_id=item_id, media_item=track, extra_attributes={})
    item.name = item_id
    return item


def _plugin(items: list[MagicMock] | None = None, required: bool = True) -> PartyPlugin:
    """Create a party plugin with a lyrics provider and a playing queue."""
    plugin = PartyPlugin.__new__(PartyPlugin)
    plugin.mass = MagicMock()
    plugin.mass.providers = [MagicMock(supported_features={ProviderFeature.LYRICS})]
    plugin.mass.metadata.get_track_lyrics = AsyncMock(return_value=(None, None))
    plugin.mass.music.get_item_by_uri = AsyncMock()
    plugin.mass.player_queues.next = AsyncMock()
    plugin.mass.player_queues.load = AsyncMock()
    queue_items = items or []
    plugin.mass.player_queues.items.return_value = queue_items
    plugin.mass.player_queues.delete_item.side_effect = lambda _queue, item_id: queue_items.remove(
        next(item for item in queue_items if item.queue_item_id == item_id)
    )
    plugin.mass.player_queues.get.return_value = MagicMock(
        state=PlaybackState.PLAYING,
        current_index=0,
        index_in_buffer=0,
        current_item=queue_items[0] if queue_items else None,
    )
    plugin.logger = MagicMock()
    plugin.config = MagicMock()
    values = {
        CONF_ENABLE_GUEST_ACCESS: True,
        CONF_ENABLE_ADD_QUEUE: True,
        CONF_ENABLE_BOOST: True,
        CONF_REQUIRE_LYRICS: required,
    }
    plugin.config.get_value.side_effect = values.get
    plugin._queue_lock = asyncio.Lock()
    plugin._guest_key_salt = "salt"
    plugin._blocked_guests = set()
    plugin._lyrics_cache = {}
    plugin._lyrics_checked = {}
    plugin._lyrics_skips = 0
    plugin._lyrics_lock = asyncio.Lock()
    plugin.get_party_player = AsyncMock(return_value="party_queue")  # type: ignore[method-assign]
    plugin._get_voter_id = AsyncMock(return_value="token:a")  # type: ignore[method-assign]
    return plugin


def _mass(plugin: PartyPlugin) -> MagicMock:
    """Return the mocked server of the given plugin."""
    return cast("MagicMock", plugin.mass)


async def test_request_without_lyrics_is_refused() -> None:
    """A song without lyrics never reaches the queue."""
    plugin = _plugin()
    _mass(plugin).music.get_item_by_uri.return_value = _track("silent")
    result = await plugin.add_to_queue("test://track/silent")
    assert result == {"success": False, "reason": REASON_NO_LYRICS}
    _mass(plugin).player_queues.load.assert_not_awaited()


async def test_setting_off_does_no_lookup() -> None:
    """With the setting off nothing is looked up."""
    plugin = _plugin(required=False)
    assert await plugin._uri_has_lyrics("test://track/silent")
    _mass(plugin).music.get_item_by_uri.assert_not_awaited()
    _mass(plugin).metadata.get_track_lyrics.assert_not_awaited()


async def test_stored_lyrics_need_no_lookup() -> None:
    """Lyrics already on the track are enough."""
    plugin = _plugin()
    assert await plugin._item_has_lyrics(_track("song", "la la"))
    _mass(plugin).metadata.get_track_lyrics.assert_not_awaited()


async def test_lookup_result_is_cached() -> None:
    """The same song is only looked up once."""
    plugin = _plugin()
    track = _track("silent")
    assert not await plugin._item_has_lyrics(track)
    assert not await plugin._item_has_lyrics(track)
    _mass(plugin).metadata.get_track_lyrics.assert_awaited_once()


async def test_non_track_is_allowed() -> None:
    """Radio and other items have no lyrics to require."""
    plugin = _plugin()
    assert await plugin._item_has_lyrics(MagicMock())


async def test_no_lyrics_provider_allows_everything() -> None:
    """Without a lyrics provider the setting cannot refuse songs."""
    plugin = _plugin()
    _mass(plugin).providers = [MagicMock(supported_features=set())]
    assert await plugin._item_has_lyrics(_track("song"))


async def test_failed_lookup_allows_the_song_and_is_not_cached() -> None:
    """A lookup error is not treated as a song without lyrics."""
    plugin = _plugin()
    _mass(plugin).metadata.get_track_lyrics.side_effect = MediaNotFoundError("down")
    track = _track("song")
    assert await plugin._item_has_lyrics(track)
    assert track.uri not in plugin._lyrics_cache


async def test_boost_without_lyrics_is_refused() -> None:
    """A queued song without lyrics cannot be boosted."""
    items = [_queue_item("now", _track("now", "la")), _queue_item("next", _track("silent"))]
    plugin = _plugin(items)
    result = await plugin.boost_queue_item("next")
    assert result == {"success": False, "reason": REASON_NO_LYRICS}
    _mass(plugin).player_queues.update_items.assert_not_called()


async def test_playing_song_without_lyrics_is_skipped() -> None:
    """A song without lyrics is skipped when it starts."""
    plugin = _plugin([_queue_item("now", _track("silent"))])
    await plugin._on_queue_updated(MagicMock(object_id="party_queue"))
    _mass(plugin).player_queues.next.assert_awaited_once_with("party_queue")


async def test_song_is_checked_once() -> None:
    """Repeated queue updates for one song do not skip again."""
    plugin = _plugin([_queue_item("now", _track("silent"))])
    event = MagicMock(object_id="party_queue")
    await plugin._on_queue_updated(event)
    await plugin._on_queue_updated(event)
    _mass(plugin).player_queues.next.assert_awaited_once()


async def test_other_queues_are_left_alone() -> None:
    """Only the party queue is enforced."""
    plugin = _plugin([_queue_item("now", _track("silent"))])
    await plugin._on_queue_updated(MagicMock(object_id="kitchen"))
    _mass(plugin).player_queues.next.assert_not_awaited()


async def test_skips_stop_after_the_limit() -> None:
    """After repeated skips the song plays rather than racing through the queue."""
    plugin = _plugin([_queue_item("now", _track("silent"))])
    plugin._lyrics_skips = LYRICS_MAX_CONSECUTIVE_SKIPS
    await plugin._enforce_lyrics("party_queue", "now")
    _mass(plugin).player_queues.next.assert_not_awaited()


async def test_song_with_lyrics_resets_skips_and_clears_next() -> None:
    """Songs without lyrics are removed up to the first one with lyrics."""
    items = [
        _queue_item("now", _track("now", "la")),
        _queue_item("bad", _track("silent")),
        _queue_item("good", _track("good", "la")),
        _queue_item("later", _track("later")),
    ]
    plugin = _plugin(items)
    plugin._lyrics_skips = 2
    await plugin._enforce_lyrics("party_queue", "now")
    assert plugin._lyrics_skips == 0
    assert [item.queue_item_id for item in items] == ["now", "good", "later"]


async def test_removal_is_capped() -> None:
    """One song change cannot empty the queue."""
    items = [_queue_item("now", _track("now", "la"))] + [
        _queue_item(f"bad{number}", _track(f"bad{number}"))
        for number in range(LYRICS_MAX_REMOVED_AT_ONCE + 2)
    ]
    plugin = _plugin(items)
    await plugin._enforce_lyrics("party_queue", "now")
    assert _mass(plugin).player_queues.delete_item.call_count == LYRICS_MAX_REMOVED_AT_ONCE
