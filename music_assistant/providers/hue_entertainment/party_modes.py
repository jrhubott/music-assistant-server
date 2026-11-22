"""
Dance floor and party effects for Hue Lights Sync.

These modes share a bar-aware beat clock, per-band hit detection, a song section tracker
that reads the audio features queued ahead of the playhead, and a limiter that keeps any
single light at or below three flashes per second.
"""

from __future__ import annotations

import math
import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from hue_entertainment import LightChannel

type RGB = tuple[float, float, float]
type Effect = Callable[[int, list[RGB]], list[RGB]]

_WHITE: Final[RGB] = (0.85, 0.85, 0.85)
# Lights stay faintly lit so a dark room never goes fully black during an effect.
_FLOOR: Final[float] = 0.06

_DEFAULT_INTERVAL_US: Final[int] = 500_000
_MIN_INTERVAL_US: Final[int] = 200_000
_MAX_INTERVAL_US: Final[int] = 2_000_000
# Onsets closer together than this are one hit when no beat schedule is available.
_MIN_PEAK_GAP_US: Final[int] = 180_000
# Whole-room color swaps stay at or below three per second.
_MIN_SWAP_US: Final[int] = 334_000

_KICK_BINS: Final[slice] = slice(0, 2)
_SNARE_BINS: Final[slice] = slice(3, 11)
_HAT_BINS: Final[slice] = slice(11, 17)
_ENERGY_BINS: Final[int] = 11
_BAND_FAST_ALPHA: Final[float] = 0.7
_BAND_SLOW_ALPHA: Final[float] = 0.03
_BAND_SCALE: Final[float] = 5.0
_BAND_DECAY_US: Final[float] = 140_000.0
_SPARK_THRESHOLD: Final[float] = 0.35
_SPARK_GAP_US: Final[int] = 90_000
_SPARK_DECAY_US: Final[float] = 180_000.0

_SHORT_ENERGY_S: Final[float] = 1.5
_LONG_ENERGY_S: Final[float] = 20.0
_SILENCE: Final[float] = 0.03
_CALM_RATIO: Final[float] = 0.75
_PEAK_RATIO: Final[float] = 1.2
_SECTION_HOLD_US: Final[int] = 4_000_000
_DROP_RISE: Final[float] = 1.35
_DROP_OVER_LONG: Final[float] = 1.05
_DROP_HORIZON_US: Final[int] = 6_000_000
_DROP_BUCKET_US: Final[int] = 500_000
_DROP_BUCKET_MIN_FRAMES: Final[int] = 3
_DROP_SCAN_US: Final[int] = 250_000
_DROP_BEATS: Final[int] = 16
_DIP_MAX_US: Final[int] = 600_000
_LOOK_FADE_US: Final[int] = 500_000
_LOOK_BARS: Final[int] = 8
_FUTURE_FRAMES: Final[int] = 600

_FLASH_DELTA: Final[float] = 0.1
_FLASHES_PER_SECOND: Final[int] = 3
_FLASH_WINDOW_US: Final[int] = 1_000_000


@dataclass(frozen=True, slots=True)
class BeatEvent:
    """The latest scheduled beat at the render time."""

    timestamp_us: int
    is_downbeat: bool
    interval_us: int


class _Section(StrEnum):
    """Song section tracked by Party Auto."""

    CALM = "calm"
    GROOVE = "groove"
    PEAK = "peak"
    BUILD = "build"
    DIP = "dip"
    DROP = "drop"


class _FlashLimiter:
    """Hold one light at or below the flash limit by capping rises past the budget."""

    def __init__(self) -> None:
        """Initialize the limiter."""
        self._rising = False
        self._peak = 0.0
        self._low = 1.0
        self._flashes: deque[int] = deque()

    def apply(self, now_us: int, color: RGB) -> RGB:
        """
        Return ``color``, dimmed when it would start a flash past the budget.

        :param now_us: Render time.
        :param color: The color the effect wants to show.
        """
        value = max(color)
        if self._rising:
            self._peak = max(self._peak, value)
            if value <= self._peak - _FLASH_DELTA:
                self._rising = False
                self._low = value
            return color
        self._low = min(self._low, value)
        if value < self._low + _FLASH_DELTA:
            return color
        while self._flashes and now_us - self._flashes[0] >= _FLASH_WINDOW_US:
            self._flashes.popleft()
        if len(self._flashes) < _FLASHES_PER_SECOND:
            self._flashes.append(now_us)
            self._rising = True
            self._peak = value
            return color
        return _scale(color, (self._low + _FLASH_DELTA * 0.9) / value)


class PartyEngine:
    """Render the party modes for a fixed set of light channels."""

    def __init__(self, channels: list[LightChannel], rng: random.Random | None = None) -> None:
        """
        Initialize the engine.

        :param channels: The lights to render, with their positions.
        :param rng: Random source for Confetti; a new one when omitted.
        """
        self._count = len(channels)
        self._ranks, self._radial = _layout(channels)
        self._rng = rng or random.Random()  # noqa: S311
        self._effects: dict[str, Effect] = {
            "party_auto": self._party_auto,
            "chase": self._chase,
            "shockwave": self._shockwave,
            "color_slam": self._color_slam,
            "two_tone": self._two_tone,
            "rhythm_section": self._rhythm_section,
            "confetti": self._confetti,
            "warm_up": self._warm_up,
        }
        self._future: deque[tuple[int, float]] = deque(maxlen=_FUTURE_FRAMES)
        self.reset()

    def reset(self) -> None:
        """Forget all playback state, for a seek or the end of a stream."""
        self._future.clear()
        self._limiters = [_FlashLimiter() for _ in range(self._count)]
        self._last_render_us: int | None = None
        self._last_peak_us: int | None = None
        self._last_spectrum: list[float] | None = None
        self._band_fast = [0.0, 0.0, 0.0]
        self._band_slow = [0.0, 0.0, 0.0]
        self._bands = [0.0, 0.0, 0.0]
        self._sparks = [0.0] * self._count
        self._spark_colors = [0] * self._count
        self._last_spark_us = -_SPARK_GAP_US
        self._energy_us: int | None = None
        self._short_energy = 0.0
        self._long_energy = 0.0
        self._steady = _Section.CALM
        self._steady_since: int | None = None
        self._next_scan_us = 0
        self._look = "warm_up"
        self._previous_look: str | None = None
        self._look_since = 0
        self.clear_schedule()

    def clear_schedule(self) -> None:
        """Forget the beat position and any expected drop, for a track change."""
        self._interval_us = _DEFAULT_INTERVAL_US
        self._beat_us = -1
        self._beat_index = -1
        self._bar_beat = 3
        self._bar = -1
        self._section = _Section.CALM
        self._drop_at_us: int | None = None
        self._drop_start_us = 0
        self._drop_until_us = 0
        self._build_start_us = 0

    def observe(self, timestamp_us: int, bins: list[int]) -> None:
        """
        Record a spectrum frame that may still be ahead of the playhead.

        :param timestamp_us: Playback time of the frame.
        :param bins: Raw 16-bit spectrum bins.
        """
        band = bins[:_ENERGY_BINS]
        if band:
            energy = sum(math.sqrt(max(0, min(65535, b)) / 65535.0) for b in band) / len(band)
            self._future.append((timestamp_us, energy))

    def render(
        self,
        mode: str,
        now_us: int,
        palette: list[RGB],
        spectrum: list[float],
        beat: BeatEvent | None,
        peak_us: int | None,
    ) -> list[RGB]:
        """
        Return one color per channel.

        :param mode: The party mode to render.
        :param now_us: Render time on the server clock.
        :param palette: The vivid palette to draw from; must not be empty.
        :param spectrum: The current normalized spectrum.
        :param beat: The latest scheduled beat, or None without a schedule.
        :param peak_us: Time of the latest onset, used without a schedule.
        """
        self._update_clock(beat, peak_us)
        self._update_bands(now_us, spectrum)
        self._update_energy(now_us)
        if mode == "party_auto":
            self._update_section(now_us)
        colors = self._effects[mode](now_us, palette)
        return [
            limiter.apply(now_us, color)
            for limiter, color in zip(self._limiters, colors, strict=True)
        ]

    # -- Effects --

    def _chase(self, now_us: int, palette: list[RGB], *, fast: bool = False) -> list[RGB]:
        """Bounce a lit head across the room on beats or eighth notes."""
        color = _pick(palette, self._bar)
        if self._count == 1:
            return [_scale(color, _FLOOR + (1.0 - _FLOOR) * self._hit(now_us, 220_000.0))]
        eighths = fast or self._interval_us >= 400_000
        phase = self._beat_phase(now_us)
        step = self._beat_index * 2 + int(phase >= 0.5) if eighths else self._beat_index
        step_us = self._interval_us / 2 if eighths else self._interval_us
        elapsed = (phase * self._interval_us) % step_us
        pulse = 0.7 + 0.3 * math.exp(-elapsed / 120_000.0)
        period = 2 * (self._count - 1)
        position = step % period
        head = position if position < self._count else period - position
        return [
            _scale(color, max(_FLOOR, pulse * math.exp(-abs(rank - head) / 0.7)))
            for rank in self._ranks
        ]

    def _shockwave(self, now_us: int, palette: list[RGB]) -> list[RGB]:
        """Send a ring of light from the center of the room outward on each beat."""
        color = _pick(palette, self._beat_index)
        base = _scale(_pick(palette, self._beat_index + 1), 0.1)
        if self._beat_us < 0:
            return [base] * self._count
        travel = max(150_000.0, min(450_000.0, self._interval_us * 0.6))
        front = (now_us - self._beat_us) / travel
        return [
            _lerp(base, color, math.exp(-(((distance - front) / 0.3) ** 2)))
            for distance in self._radial
        ]

    def _color_slam(self, now_us: int, palette: list[RGB]) -> list[RGB]:
        """Hold one bold color per bar, punching on the beats."""
        accent = 0.45 if self._bar_beat == 0 else 0.25
        level = 0.55 + accent * self._hit(now_us, 220_000.0)
        return [_scale(_pick(palette, self._bar), level)] * self._count

    def _two_tone(self, now_us: int, palette: list[RGB]) -> list[RGB]:
        """Swap two contrasting colors between alternating lights."""
        first = _pick(palette, self._bar)
        second = _pick(palette, self._bar + max(1, len(palette) // 2))
        divisor = math.ceil(_MIN_SWAP_US / self._interval_us)
        swap = max(self._beat_index, 0) // divisor
        level = 0.7 + 0.3 * self._hit(now_us, 180_000.0)
        return [_scale(first if (rank + swap) % 2 == 0 else second, level) for rank in self._ranks]

    def _rhythm_section(self, _now_us: int, palette: list[RGB]) -> list[RGB]:
        """Give kick, snare and hi-hats their own group of lights."""
        if self._count == 1:
            band = max(range(2), key=lambda index: self._bands[index])
            return [_scale(_pick(palette, band), max(_FLOOR, self._bands[band]))]
        groups = min(3, self._count)
        return [
            _scale(_pick(palette, rank % groups), max(_FLOOR, self._bands[rank % groups]))
            for rank in self._ranks
        ]

    def _confetti(self, _now_us: int, palette: list[RGB]) -> list[RGB]:
        """Sparkle random lights on hi-hats over a dim base color."""
        base = _scale(_pick(palette, self._bar), 0.18)
        return [
            _lerp(base, _pick(palette, color), spark)
            for spark, color in zip(self._sparks, self._spark_colors, strict=True)
        ]

    def _warm_up(self, now_us: int, palette: list[RGB]) -> list[RGB]:
        """Drift soft colors slowly, with no flashing."""
        seconds = now_us / 1_000_000.0
        breath = 0.48 + 0.07 * math.sin(seconds * math.tau / 8.0)
        result: list[RGB] = []
        for rank in self._ranks:
            position = seconds / 12.0 + rank / max(1, self._count)
            low = math.floor(position)
            color = _lerp(_pick(palette, low), _pick(palette, low + 1), position - low)
            result.append(_scale(_lerp(color, _WHITE, 0.25), breath))
        return result

    def _party_auto(self, now_us: int, palette: list[RGB]) -> list[RGB]:
        """Pick the look that fits the current song section."""
        if self._section is _Section.BUILD:
            self._set_look("chase", now_us, fade=False)
            progress = self._build_progress(now_us)
            colors = self._chase(now_us, palette, fast=progress >= 0.5)
            return [_scale(color, 0.4 + 0.6 * progress) for color in colors]
        if self._section is _Section.DIP:
            self._set_look("chase", now_us, fade=False)
            return [_scale(_pick(palette, self._bar), 0.1)] * self._count
        if self._section is _Section.DROP:
            self._set_look("shockwave", now_us, fade=False)
            flash = math.exp(-max(0, now_us - self._drop_start_us) / 180_000.0)
            return [_lerp(color, _WHITE, flash) for color in self._shockwave(now_us, palette)]
        self._set_look(self._steady_look(), now_us, fade=True)
        colors = self._effects[self._look](now_us, palette)
        elapsed = now_us - self._look_since
        if self._previous_look is None or elapsed >= _LOOK_FADE_US:
            self._previous_look = None
            return colors
        previous = self._effects[self._previous_look](now_us, palette)
        blend = elapsed / _LOOK_FADE_US
        return [_lerp(old, new, blend) for old, new in zip(previous, colors, strict=True)]

    # -- State --

    def _update_clock(self, beat: BeatEvent | None, peak_us: int | None) -> None:
        """Advance the bar position from the beat schedule, else from onsets."""
        if beat is not None:
            self._interval_us = _clamp_interval(beat.interval_us)
            if beat.timestamp_us != self._beat_us:
                self._advance_beat(beat.timestamp_us, is_downbeat=beat.is_downbeat)
            return
        if peak_us is None or peak_us == self._last_peak_us:
            return
        self._last_peak_us = peak_us
        gap = peak_us - self._beat_us
        if self._beat_us >= 0 and gap < _MIN_PEAK_GAP_US:
            return
        if self._beat_us >= 0 and gap <= _MAX_INTERVAL_US:
            self._interval_us = _clamp_interval(int(self._interval_us * 0.8 + gap * 0.2))
        self._advance_beat(peak_us, is_downbeat=False)

    def _advance_beat(self, timestamp_us: int, *, is_downbeat: bool) -> None:
        """Step to a new beat."""
        self._beat_us = timestamp_us
        self._beat_index += 1
        if is_downbeat or self._bar_beat >= 3:
            self._bar_beat = 0
            self._bar += 1
        else:
            self._bar_beat += 1
        self._spark(timestamp_us)

    def _update_bands(self, now_us: int, spectrum: list[float]) -> None:
        """Decay the band envelopes and sparks, then take in a new spectrum frame."""
        if self._last_render_us is not None:
            elapsed = max(0, now_us - self._last_render_us)
            band_decay = math.exp(-elapsed / _BAND_DECAY_US)
            spark_decay = math.exp(-elapsed / _SPARK_DECAY_US)
            self._bands = [value * band_decay for value in self._bands]
            self._sparks = [value * spark_decay for value in self._sparks]
        self._last_render_us = now_us
        if not spectrum or spectrum is self._last_spectrum:
            return
        self._last_spectrum = spectrum
        for index, bins in enumerate((_KICK_BINS, _SNARE_BINS, _HAT_BINS)):
            band = spectrum[bins]
            energy = sum(band) / len(band) if band else 0.0
            self._band_fast[index] += (energy - self._band_fast[index]) * _BAND_FAST_ALPHA
            self._band_slow[index] += (energy - self._band_slow[index]) * _BAND_SLOW_ALPHA
            hit = max(0.0, self._band_fast[index] - self._band_slow[index]) * _BAND_SCALE
            self._bands[index] = max(self._bands[index], min(1.0, hit))
        if self._bands[2] >= _SPARK_THRESHOLD:
            self._spark(now_us)

    def _spark(self, now_us: int) -> None:
        """Light one random channel, at most once per spark gap."""
        if self._count == 0 or now_us - self._last_spark_us < _SPARK_GAP_US:
            return
        self._last_spark_us = now_us
        index = self._rng.randrange(self._count)
        self._sparks[index] = 1.0
        self._spark_colors[index] = self._rng.randrange(1 << 16)

    def _update_energy(self, now_us: int) -> None:
        """Feed the frames that have reached the playhead into the energy averages."""
        while self._future and self._future[0][0] <= now_us:
            timestamp, energy = self._future.popleft()
            if self._energy_us is None:
                self._short_energy = self._long_energy = energy
            else:
                seconds = max(0, timestamp - self._energy_us) / 1_000_000.0
                self._short_energy += (energy - self._short_energy) * (
                    1.0 - math.exp(-seconds / _SHORT_ENERGY_S)
                )
                self._long_energy += (energy - self._long_energy) * (
                    1.0 - math.exp(-seconds / _LONG_ENERGY_S)
                )
            self._energy_us = timestamp

    def _update_section(self, now_us: int) -> None:
        """Resolve the song section, looking ahead for a coming drop."""
        if now_us >= self._next_scan_us:
            self._next_scan_us = now_us + _DROP_SCAN_US
            self._scan_for_drop(now_us)
        if self._drop_at_us is not None:
            if now_us >= self._drop_at_us:
                self._drop_start_us = self._drop_at_us
                self._drop_until_us = now_us + _DROP_BEATS * self._interval_us
                self._drop_at_us = None
                self._section = _Section.DROP
            elif self._drop_at_us - now_us <= min(self._interval_us, _DIP_MAX_US):
                self._section = _Section.DIP
            else:
                self._section = _Section.BUILD
            return
        if now_us < self._drop_until_us:
            self._section = _Section.DROP
            return
        self._section = self._steady_section(now_us)

    def _scan_for_drop(self, now_us: int) -> None:
        """Schedule a drop when the queued audio holds a jump in energy."""
        if (
            self._drop_at_us is not None
            or now_us < self._drop_until_us
            or self._energy_us is None
            or self._short_energy < _SILENCE
            or self._short_energy > self._long_energy
        ):
            return
        target = max(self._short_energy * _DROP_RISE, self._long_energy * _DROP_OVER_LONG)
        buckets: dict[int, list[float]] = {}
        for timestamp, energy in self._future:
            if timestamp > now_us + _DROP_HORIZON_US:
                break
            buckets.setdefault((timestamp - now_us) // _DROP_BUCKET_US, []).append(energy)
        for bucket in sorted(buckets):
            values = buckets[bucket]
            if len(values) >= _DROP_BUCKET_MIN_FRAMES and sum(values) / len(values) >= target:
                self._drop_at_us = self._snap_to_beat(now_us + bucket * _DROP_BUCKET_US, now_us)
                self._build_start_us = now_us
                return

    def _snap_to_beat(self, timestamp_us: int, now_us: int) -> int:
        """Return the beat on the current grid nearest to ``timestamp_us``."""
        if self._beat_us < 0:
            return timestamp_us
        beats = round((timestamp_us - self._beat_us) / self._interval_us)
        snapped = self._beat_us + beats * self._interval_us
        return snapped if snapped > now_us else timestamp_us

    def _steady_section(self, now_us: int) -> _Section:
        """Return the calm, groove or peak section, holding each for a minimum time."""
        if self._steady_since is None:
            self._steady_since = now_us
        ratio = self._short_energy / self._long_energy if self._long_energy > 0.0 else 0.0
        if self._short_energy < _SILENCE or ratio < _CALM_RATIO:
            candidate = _Section.CALM
        elif ratio > _PEAK_RATIO:
            candidate = _Section.PEAK
        else:
            candidate = _Section.GROOVE
        if candidate is not self._steady and now_us - self._steady_since >= _SECTION_HOLD_US:
            self._steady = candidate
            self._steady_since = now_us
        return self._steady

    def _steady_look(self) -> str:
        """Return the look for a calm, groove or peak section, rotating every few bars."""
        rotation = max(self._bar, 0) // _LOOK_BARS
        if self._section is _Section.PEAK:
            return ("shockwave", "confetti")[rotation % 2]
        if self._section is _Section.GROOVE:
            return ("chase", "two_tone", "rhythm_section", "color_slam")[rotation % 4]
        return "warm_up"

    def _set_look(self, look: str, now_us: int, *, fade: bool) -> None:
        """Switch to ``look``, optionally crossfading from the current one."""
        if look == self._look:
            return
        self._previous_look = self._look if fade else None
        self._look = look
        self._look_since = now_us

    def _build_progress(self, now_us: int) -> float:
        """Return how far the current build has come toward its drop, from 0 to 1."""
        if self._drop_at_us is None or self._drop_at_us <= self._build_start_us:
            return 1.0
        span = self._drop_at_us - self._build_start_us
        return max(0.0, min(1.0, (now_us - self._build_start_us) / span))

    def _hit(self, now_us: int, decay_us: float) -> float:
        """Return a beat envelope that starts at 1 on the beat and decays."""
        if self._beat_us < 0:
            return 0.0
        return math.exp(-max(0, now_us - self._beat_us) / decay_us)

    def _beat_phase(self, now_us: int) -> float:
        """Return the position inside the current beat, from 0 up to just below 1."""
        if self._beat_us < 0:
            return 0.0
        return max(0.0, min(0.999, (now_us - self._beat_us) / self._interval_us))


def _layout(channels: list[LightChannel]) -> tuple[list[int], list[float]]:
    """Return each channel's rank along the widest axis and distance from the center (0-1)."""
    count = len(channels)
    if count == 0:
        return [], []
    positions = [channel.position for channel in channels]
    spreads = [
        max(pos[axis] for pos in positions) - min(pos[axis] for pos in positions)
        for axis in range(3)
    ]
    axis = max(range(3), key=lambda index: spreads[index])
    order = (
        sorted(range(count), key=lambda index: (positions[index][axis], index))
        if spreads[axis] > 1e-3
        else list(range(count))
    )
    ranks = [0] * count
    for rank, index in enumerate(order):
        ranks[index] = rank
    center = [sum(pos[dim] for pos in positions) / count for dim in range(3)]
    distances = [math.dist(pos, center) for pos in positions]
    furthest = max(distances)
    if furthest > 1e-3:
        return ranks, [distance / furthest for distance in distances]
    if count == 1:
        return ranks, [0.0]
    return ranks, [abs(2.0 * rank / (count - 1) - 1.0) for rank in ranks]


def _clamp_interval(interval_us: int) -> int:
    """Keep a beat interval inside the range the effects are built for."""
    return max(_MIN_INTERVAL_US, min(_MAX_INTERVAL_US, interval_us))


def _pick(palette: list[RGB], index: int) -> RGB:
    """Return the palette color at ``index``, wrapping around."""
    return palette[index % len(palette)]


def _scale(color: RGB, level: float) -> RGB:
    """Scale a color's brightness."""
    return (color[0] * level, color[1] * level, color[2] * level)


def _lerp(first: RGB, second: RGB, amount: float) -> RGB:
    """Blend two colors."""
    return (
        first[0] + (second[0] - first[0]) * amount,
        first[1] + (second[1] - first[1]) * amount,
        first[2] + (second[2] - first[2]) * amount,
    )
