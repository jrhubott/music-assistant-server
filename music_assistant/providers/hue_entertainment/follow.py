"""
Follow a player that plays over its own protocol: the lights react to what it plays.

A light player normally gets its audio as a member of a Sendspin group, which takes every
speaker in that group off its own protocol. Grouped with a speaker that plays over another
protocol, the light follows it instead, and a follow session leaves that speaker alone. It reads the audio of the queue item that player plays from the buffer Music
Assistant already holds for it, as a passive reader that never affects playback, and times
the extracted features to the position the player reports.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, cast

import numpy as np
from aiosendspin.models.visualizer import BeatTiming, StreamStartVisualizer
from aiosendspin.server.roles.visualizer.features import VisualizerFeatureExtractor
from music_assistant_models.enums import ContentType, PlaybackState, ProviderType

from music_assistant.controllers.streams.audio_analysis import SMART_FADES_ANALYSIS_DOMAIN
from music_assistant.controllers.streams.audio_buffer import (
    AudioBufferDiscarded,
    AudioBufferEOF,
)

if TYPE_CHECKING:
    from aiosendspin.models.visualizer import VisualizerStatePayload
    from music_assistant_models.media_items import AudioFormat, MediaItemPalette
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.controllers.streams.audio_buffer import AudioBuffer
    from music_assistant.models.player import Player

    from .bridge import HueEntertainmentBridge

LOGGER = logging.getLogger(__name__)

# How often the followed player's state is checked.
_TICK_S = 0.25
# How far ahead of the followed player's playhead features are extracted.
_LEAD_US = 1_500_000
# A position report that moves the playhead by more than this re-anchors the feed.
_REANCHOR_US = 500_000
# Ticks a jump within one item has to last before the feed re-anchors on it: at a track
# change a player can report the next track's position before it names the next track.
_REANCHOR_CONFIRM_TICKS = 2
# A jump back to the start of the item is most likely a track change the player has not
# named yet (Sonos lags up to a second there), so it has to hold longer.
_RESTART_POSITION_S = 3.0
_RESTART_CONFIRM_TICKS = 8
# How long to wait for the followed item's audio before trying again.
_BUFFER_RETRY_S = 2.0
# Beat analysis can still be running when a track starts.
_BEAT_RETRY_S = 3.0
_BEAT_RETRY_ATTEMPTS = 30


def pcm_to_s16(pcm: bytes, pcm_format: AudioFormat) -> bytes:
    """
    Convert PCM audio to signed 16-bit, the input of the visualizer feature extractor.

    :param pcm: The audio.
    :param pcm_format: Its format.
    """
    content_type = pcm_format.content_type
    if content_type == ContentType.PCM_S16LE:
        return pcm
    if content_type == ContentType.PCM_F32LE:
        floats = np.frombuffer(pcm, dtype="<f4")
        return (np.clip(floats, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    if content_type == ContentType.PCM_F64LE:
        floats = np.frombuffer(pcm, dtype="<f8")
        return (np.clip(floats, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    if pcm_format.bit_depth == 24:
        # packed 3-byte samples: the top two bytes are the 16-bit sample
        packed = np.frombuffer(pcm[: len(pcm) - len(pcm) % 3], dtype=np.uint8).reshape(-1, 3)
        return packed[:, 1:].copy().tobytes()
    ints = np.frombuffer(pcm[: len(pcm) - len(pcm) % 4], dtype="<i4")
    return (ints >> 16).astype("<i2").tobytes()


class FollowSession:
    """Feeds a light bridge the features of what one player plays."""

    def __init__(
        self,
        bridge: HueEntertainmentBridge,
        player_id: str,
        visualizer_request: VisualizerStatePayload,
        delay_us: int = 0,
    ) -> None:
        """
        Initialize the session.

        :param bridge: The bridge the features go to.
        :param player_id: The player to follow.
        :param visualizer_request: The features the bridge asks for.
        :param delay_us: How much later the lights render than the position the player reports.
        """
        self.bridge = bridge
        self.mass = bridge.mass
        self.player_id = player_id
        self.logger = LOGGER.getChild(bridge.target.name)
        self._visualizer_request = visualizer_request
        self._task: asyncio.Task[None] | None = None
        self._feed_task: asyncio.Task[None] | None = None
        self._beat_task: asyncio.Task[None] | None = None
        # the queue item fed, and where its position zero lies on the Sendspin clock
        self._item_id: str | None = None
        self._anchor_us: int | None = None
        self._palette: MediaItemPalette | None = None
        self._retry_at = 0.0
        # consecutive ticks the playhead of the fed item reported a jump
        self._jump_ticks = 0
        self._delay_us = delay_us
        # the source last logged as one the lights cannot follow, to log it once
        self._unfollowable_source: str | None = None
        # set when the delay changed: the next tick restarts the feed on the new anchor
        self._restart = False

    def start(self) -> None:
        """Start following."""
        self._task = self.mass.create_task(self._run())

    def set_delay(self, delay_us: int) -> None:
        """
        Change how much later the lights render than the position the player reports.

        :param delay_us: The delay in microseconds; negative renders earlier.
        """
        if delay_us == self._delay_us:
            return
        self._delay_us = delay_us
        self._restart = True

    async def stop(self) -> None:
        """Stop following and let the lights go."""
        for task in (self._task, self._feed_task, self._beat_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._task = self._feed_task = self._beat_task = None
        if self._item_id is not None:
            self._item_id = None
            self.bridge.follow_stream_end()

    async def _run(self) -> None:
        """Track the followed player and (re)start the feed when its playback moves."""
        while True:
            try:
                self._tick()
            except Exception:
                self.logger.exception("Following %s failed", self.player_id)
            await asyncio.sleep(_TICK_S)

    def _tick(self) -> None:
        """Compare the followed player's playback with the feed and act on a difference."""
        player = self.mass.players.get_player(self.player_id)
        target = self._playing_item(player)
        if target is None:
            self._log_unfollowable_source(player)
            self._end_feed()
            return
        self._unfollowable_source = None
        queue_item, position_s = target
        now_us = self.bridge.sendspin_server.clock.now_us()
        anchor_us = now_us - int(position_s * 1_000_000) + self._delay_us
        self._apply_palette(player)
        if self._restart or queue_item.queue_item_id != self._item_id:
            self._restart = False
            self._start_feed(queue_item, position_s, anchor_us)
            return
        moved = self._anchor_us is not None and abs(anchor_us - self._anchor_us) > _REANCHOR_US
        if not moved:
            self._jump_ticks = 0
            if self._feed_task is None and self.mass.loop.time() >= self._retry_at:
                # the audio was not there yet: try again from where the player is now
                self._start_feed(queue_item, position_s, anchor_us)
            return
        # a seek holds, a track change soon names the next item: act on a jump only once
        # it held for a few ticks without the item changing
        self._jump_ticks += 1
        confirm_ticks = (
            _RESTART_CONFIRM_TICKS if position_s < _RESTART_POSITION_S else _REANCHOR_CONFIRM_TICKS
        )
        if self._jump_ticks < confirm_ticks:
            return
        self.logger.debug(
            "Playhead of %s jumped to %.1f s in %s, re-anchoring",
            self.player_id,
            position_s,
            queue_item.name,
        )
        self._start_feed(queue_item, position_s, anchor_us)

    def _playing_item(self, player: Player | None) -> tuple[QueueItem, float] | None:
        """
        Return the queue item the player plays and its position, None when it plays none.

        :param player: The followed player.
        """
        if player is None or player.playback_state != PlaybackState.PLAYING:
            return None
        media = player.current_media
        if media is None or not media.source_id or not media.queue_item_id:
            return None
        queue_item = self.mass.player_queues.get_item(media.source_id, media.queue_item_id)
        if queue_item is None or queue_item.streamdetails is None:
            return None
        position_s = media.corrected_elapsed_time
        if position_s is None:
            position_s = player.corrected_elapsed_time
        if position_s is None:
            return None
        return queue_item, max(0.0, position_s)

    def _start_feed(self, queue_item: QueueItem, position_s: float, anchor_us: int) -> None:
        """
        (Re)start the feed at a position of a queue item.

        :param queue_item: The item the followed player plays.
        :param position_s: Its position in seconds.
        :param anchor_us: Where its position zero lies on the Sendspin clock.
        """
        self._cancel_feed()
        self._jump_ticks = 0
        self._item_id = queue_item.queue_item_id
        self._anchor_us = anchor_us
        self.bridge.follow_stream_start()
        self._feed_task = self.mass.create_task(self._feed(queue_item, position_s, anchor_us))
        # the beats are timed to the anchor, so they follow it when it moves
        if self._beat_task is not None:
            self._beat_task.cancel()
        self._beat_task = self.mass.create_task(self._push_beats(queue_item, anchor_us))

    def _end_feed(self) -> None:
        """Stop the feed while the followed player plays nothing."""
        if self._item_id is None:
            return
        self._cancel_feed()
        if self._beat_task is not None:
            self._beat_task.cancel()
            self._beat_task = None
        self._item_id = None
        self._anchor_us = None
        self.bridge.follow_stream_end()

    def _cancel_feed(self) -> None:
        """Cancel a running feed."""
        if self._feed_task is not None:
            self._feed_task.cancel()
            self._feed_task = None

    async def _feed(self, queue_item: QueueItem, position_s: float, anchor_us: int) -> None:
        """
        Extract features from the item's buffered audio, ahead of the followed playhead.

        :param queue_item: The item the followed player plays.
        :param position_s: Where to start, in seconds.
        :param anchor_us: Where its position zero lies on the Sendspin clock.
        """
        try:
            buffer = self._buffer_for(queue_item, position_s)
            if buffer is None:
                self._feed_task = None
                self._retry_at = self.mass.loop.time() + _BUFFER_RETRY_S
                return
            pcm_format = buffer.pcm_format
            extractor = VisualizerFeatureExtractor(
                sample_rate=pcm_format.sample_rate,
                channels=pcm_format.channels,
                config=StreamStartVisualizer.from_request(self._visualizer_request),
            )
            bytes_per_frame = (pcm_format.bit_depth // 8) * pcm_format.channels
            position_ms = int(position_s * 1000)
            chunk_number = position_ms // 1000
            # the first chunk starts at a whole second: drop what lies before the playhead
            trim_bytes = (pcm_format.sample_rate * (position_ms % 1000) // 1000) * bytes_per_frame
            timestamp_us = anchor_us + position_ms * 1000
            clock = self.bridge.sendspin_server.clock
            self.logger.debug(
                "Following %s: %s from %.1f s (%s)",
                self.player_id,
                queue_item.name,
                position_s,
                pcm_format,
            )
            while True:
                # stay a short lead ahead of the playhead, like a Sendspin stream does
                while timestamp_us - clock.now_us() > _LEAD_US:
                    await asyncio.sleep(0.1)
                try:
                    chunk = await buffer.read_chunk_for_analysis(chunk_number)
                except AudioBufferEOF:
                    return
                except AudioBufferDiscarded:
                    self.logger.debug("Audio of %s is no longer buffered", queue_item.name)
                    return
                if trim_bytes:
                    chunk = chunk[trim_bytes:]
                    trim_bytes = 0
                chunk_number += 1
                pcm = pcm_to_s16(chunk, pcm_format)
                for frame in extractor.process_chunk(pcm, timestamp_us):
                    self.bridge.follow_frame(frame)
                timestamp_us += len(chunk) * 1_000_000 // pcm_format.pcm_sample_size
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Feeding the lights from %s failed", queue_item.name)

    def _buffer_for(self, queue_item: QueueItem, position_s: float) -> AudioBuffer | None:
        """
        Return the buffer holding the item's audio at a position, None when none holds it.

        :param queue_item: The item.
        :param position_s: The position in seconds.
        """
        streamdetails = queue_item.streamdetails
        buffer = cast("AudioBuffer | None", streamdetails.buffer if streamdetails else None)
        if buffer is None:
            self.logger.debug("No audio buffered yet for %s", queue_item.name)
            return None
        # is_valid would count as activity of the buffer; check what it checks, read-only
        chunk_number = int(position_s)
        if buffer.cancelled or chunk_number < buffer.first_buffered_chunk:
            self.logger.debug(
                "Audio of %s at %.1f s is not buffered (anymore)", queue_item.name, position_s
            )
            return None
        return buffer

    async def _push_beats(self, queue_item: QueueItem, anchor_us: int) -> None:
        """
        Push the item's beats, timed to the followed playhead, once its analysis is in.

        :param queue_item: The item.
        :param anchor_us: Where its position zero lies on the Sendspin clock.
        """
        self.bridge.follow_beats_clear()
        if not any(
            provider.available and provider.domain == SMART_FADES_ANALYSIS_DOMAIN
            for provider in self.mass.get_providers(ProviderType.AUDIO_ANALYSIS)
        ):
            return
        streamdetails = queue_item.streamdetails
        if streamdetails is None:
            return
        for _ in range(_BEAT_RETRY_ATTEMPTS):
            analysis = await self.mass.streams.audio_analysis.get_audio_analysis(
                streamdetails.item_id,
                streamdetails.provider,
                media_type=streamdetails.media_type,
                priority=(SMART_FADES_ANALYSIS_DOMAIN,),
            )
            if analysis is not None and analysis.beats is not None and len(analysis.beats):
                break
            await asyncio.sleep(_BEAT_RETRY_S)
        else:
            return
        downbeats = (
            {float(beat) for beat in analysis.downbeats}
            if analysis.downbeats is not None
            else set()
        )
        now_us = self.bridge.sendspin_server.clock.now_us()
        beats = [
            BeatTiming(timestamp_us=beat_us, is_downbeat=float(beat) in downbeats)
            for beat in analysis.beats
            if (beat_us := int(anchor_us + float(beat) * 1_000_000)) >= now_us
        ]
        if beats:
            self.bridge.follow_beats(beats)

    def _log_unfollowable_source(self, player: Player | None) -> None:
        """
        Log once that the followed player plays a source the lights cannot follow.

        Only a queue item's audio is buffered for the lights to read; a live source such as
        Spotify Connect, an AirPlay receiver or a line-in streams straight to the player.

        :param player: The followed player.
        """
        if player is None or player.playback_state != PlaybackState.PLAYING:
            return
        media = player.current_media
        if media is not None and media.queue_item_id:
            return
        source = player.active_source or "an external source"
        if source == self._unfollowable_source:
            return
        self._unfollowable_source = source
        self.logger.info(
            "%s plays %s, which is no Music Assistant queue: the lights only follow what a "
            "queue plays, so they stay dark",
            player.name,
            source,
        )

    def _apply_palette(self, player: Player | None) -> None:
        """
        Hand the artwork palette of what the player plays to the lights when it changes.

        :param player: The followed player.
        """
        media = player.current_media if player is not None else None
        palette = media.palette if media is not None else None
        if palette is None or palette == self._palette:
            return
        self._palette = palette
        self.bridge.follow_palette(palette)
