"""The follow session: lights fed from the buffered audio of a player that plays natively."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest
from music_assistant_models.enums import ContentType, PlaybackState
from music_assistant_models.media_items import AudioFormat

from music_assistant.providers.hue_entertainment.follow import FollowSession, pcm_to_s16

PLAYER_ID = "patio"
QUEUE_ID = "patio_queue"


class _World:
    """A followed player, its queue items and the Sendspin clock, all driven by the test."""

    def __init__(self) -> None:
        """Create a player that plays nothing yet, at clock time 0."""
        self.now_us = 0
        self.loop_time = 0.0
        self.items: dict[str, Any] = {}
        self.player = SimpleNamespace(
            playback_state=PlaybackState.IDLE,
            current_media=None,
            corrected_elapsed_time=None,
        )
        mass = MagicMock()
        mass.players.get_player = MagicMock(
            side_effect=lambda player_id: self.player if player_id == PLAYER_ID else None
        )
        mass.player_queues.get_item = MagicMock(
            side_effect=lambda _queue_id, item_id: self.items.get(item_id)
        )
        mass.loop.time = MagicMock(side_effect=lambda: self.loop_time)

        def _create_task(coro: Any) -> MagicMock:
            coro.close()
            return MagicMock()

        mass.create_task = MagicMock(side_effect=_create_task)
        self.bridge = MagicMock()
        self.bridge.mass = mass
        self.bridge.target.name = "Patio Music"
        self.bridge.sendspin_server.clock.now_us = MagicMock(side_effect=lambda: self.now_us)
        self.session = FollowSession(self.bridge, PLAYER_ID, MagicMock())
        self.starts: list[tuple[str, float, int]] = []
        real_start = self.session._start_feed

        def _start_feed(queue_item: Any, position_s: float, anchor_us: int) -> None:
            self.starts.append((queue_item.queue_item_id, position_s, anchor_us))
            real_start(queue_item, position_s, anchor_us)

        self.session._start_feed = _start_feed  # type: ignore[method-assign]

    def play(self, item_id: str, position_s: float) -> None:
        """Let the player report it plays an item at a position."""
        self.items.setdefault(
            item_id,
            SimpleNamespace(queue_item_id=item_id, name=item_id, streamdetails=MagicMock()),
        )
        self.player.playback_state = PlaybackState.PLAYING
        self.player.current_media = SimpleNamespace(
            source_id=QUEUE_ID,
            queue_item_id=item_id,
            corrected_elapsed_time=position_s,
            palette=None,
        )

    def tick(self, seconds: float = 0.25) -> None:
        """Advance the clock and the reported position by a tick, then let the session look."""
        self.now_us += int(seconds * 1_000_000)
        media = self.player.current_media
        if media is not None and self.player.playback_state == PlaybackState.PLAYING:
            media.corrected_elapsed_time += seconds
        self.session._tick()

    def jump(self, position_s: float) -> None:
        """Let the reported position of the playing item jump."""
        self.player.current_media.corrected_elapsed_time = position_s


def test_the_feed_starts_at_the_reported_position() -> None:
    """The first tick starts the feed of the playing item, anchored to its position."""
    world = _World()
    world.now_us = 10_000_000
    world.play("a", 30.0)
    world.session._tick()

    assert world.starts == [("a", 30.0, 10_000_000 - 30_000_000)]
    world.bridge.follow_stream_start.assert_called_once()


def test_steady_playback_does_not_restart_the_feed() -> None:
    """A position that moves with the clock keeps the feed running."""
    world = _World()
    world.play("a", 30.0)
    world.session._tick()
    for _ in range(20):
        world.tick()

    assert len(world.starts) == 1


def test_a_seek_reanchors_once_it_holds() -> None:
    """A jump within the item re-anchors only after it held for two ticks."""
    world = _World()
    world.play("a", 30.0)
    world.session._tick()

    world.jump(90.0)
    world.session._tick()
    assert len(world.starts) == 1
    world.tick()
    assert world.starts[-1][0] == "a"
    assert world.starts[-1][1] == pytest.approx(90.25)


def test_a_jump_to_the_start_waits_for_the_next_item() -> None:
    """
    At a track change the position resets before the player names the next item.

    The old item is not re-anchored at its start; the next item starts once it is named.
    """
    world = _World()
    world.play("a", 180.0)
    world.session._tick()

    world.jump(0.0)
    for _ in range(4):
        world.tick()
    world.play("b", 1.0)
    world.tick()

    assert [start[0] for start in world.starts] == ["a", "b"]


def test_a_restart_of_the_same_item_reanchors_after_a_while() -> None:
    """A jump to the start that holds (a restart of the item) re-anchors in the end."""
    world = _World()
    world.play("a", 120.0)
    world.session._tick()

    world.jump(0.0)
    for _ in range(8):
        world.tick()

    assert [start[0] for start in world.starts] == ["a", "a"]
    assert world.starts[-1][1] < 3.0


def test_the_feed_ends_when_playback_stops() -> None:
    """A player that stops playing ends the feed; the lights are let go."""
    world = _World()
    world.play("a", 30.0)
    world.session._tick()

    world.player.playback_state = PlaybackState.PAUSED
    world.tick()

    world.bridge.follow_stream_end.assert_called_once()


def test_an_item_without_stream_details_is_not_followed() -> None:
    """An item whose audio is not resolved yet starts no feed."""
    world = _World()
    world.play("a", 30.0)
    world.items["a"].streamdetails = None
    world.session._tick()

    assert world.starts == []


def test_a_missing_buffer_is_retried_after_a_while() -> None:
    """A feed that found no buffered audio starts again from the position at that time."""
    world = _World()
    world.play("a", 30.0)
    world.session._tick()
    # the feed found nothing buffered and asked to try again later
    world.session._feed_task = None
    world.session._retry_at = 2.0

    world.tick()
    assert len(world.starts) == 1
    world.loop_time = 2.0
    world.tick()
    assert len(world.starts) == 2


def test_the_delay_moves_the_anchor_later() -> None:
    """A follow delay anchors the feed that much later than the reported position."""
    world = _World()
    world.session.set_delay(300_000)
    world.now_us = 10_000_000
    world.play("a", 30.0)
    world.session._tick()

    assert world.starts == [("a", 30.0, 10_000_000 - 30_000_000 + 300_000)]


def test_a_changed_delay_restarts_the_feed_on_the_next_tick() -> None:
    """A delay change re-anchors at once, even when it is smaller than a jump."""
    world = _World()
    world.play("a", 30.0)
    world.session._tick()

    world.session.set_delay(-100_000)
    world.tick()

    assert len(world.starts) == 2
    _, position_s, anchor_us = world.starts[1]
    assert anchor_us == world.now_us - int(position_s * 1_000_000) - 100_000


def test_a_live_source_is_logged_once(caplog: pytest.LogCaptureFixture) -> None:
    """A player that plays something no queue holds is logged once, and starts no feed."""
    world = _World()
    world.player.name = "Kitchen"
    world.player.active_source = "spotify_connect"
    world.player.playback_state = PlaybackState.PLAYING
    world.player.current_media = SimpleNamespace(
        source_id="spotify_connect", queue_item_id=None, palette=None
    )
    with caplog.at_level(logging.INFO):
        world.session._tick()
        world.session._tick()

    assert world.starts == []
    assert len([r for r in caplog.records if "no Music Assistant queue" in r.message]) == 1


def _format(content_type: ContentType, bit_depth: int) -> AudioFormat:
    """Return a stereo 44.1 kHz PCM format."""
    return AudioFormat(
        content_type=content_type, sample_rate=44100, bit_depth=bit_depth, channels=2
    )


def test_s16_audio_passes_unchanged() -> None:
    """Signed 16-bit audio is what the extractor takes."""
    pcm = np.array([1, -2, 300], dtype="<i2").tobytes()
    assert pcm_to_s16(pcm, _format(ContentType.PCM_S16LE, 16)) == pcm


@pytest.mark.parametrize(
    ("content_type", "dtype"),
    [(ContentType.PCM_F32LE, "<f4"), (ContentType.PCM_F64LE, "<f8")],
)
def test_float_audio_is_scaled_and_clipped(content_type: ContentType, dtype: str) -> None:
    """Float samples scale to 16 bits, out-of-range ones clip."""
    pcm = np.array([0.0, 0.5, -1.0, 2.0], dtype=dtype).tobytes()
    converted = np.frombuffer(pcm_to_s16(pcm, _format(content_type, 32)), dtype="<i2")
    assert converted.tolist() == [0, 16383, -32767, 32767]


def test_24_bit_audio_keeps_its_top_two_bytes() -> None:
    """Packed 24-bit samples drop their lowest byte."""
    samples = [0x123456, -0x123456 & 0xFFFFFF]
    pcm = b"".join(sample.to_bytes(3, "little") for sample in samples)
    converted = np.frombuffer(pcm_to_s16(pcm, _format(ContentType.PCM_S24LE, 24)), dtype="<i2")
    assert converted.tolist() == [0x1234, np.int16(np.uint16(0xEDCB))]


def test_32_bit_audio_keeps_its_top_half() -> None:
    """Signed 32-bit samples shift down to 16 bits."""
    pcm = np.array([0x12345678, -0x12345678], dtype="<i4").tobytes()
    converted = np.frombuffer(pcm_to_s16(pcm, _format(ContentType.PCM_S32LE, 32)), dtype="<i2")
    assert converted.tolist() == [0x1234, -0x1235]
