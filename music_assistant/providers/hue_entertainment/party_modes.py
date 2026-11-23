"""
Dance floor and party effects for Hue Lights Sync.

These modes share a bar-aware beat clock, per-band hit detection, a song section tracker
that reads the audio features queued ahead of the playhead, and a limiter that keeps any
single light at or below three flashes per second.
"""

from __future__ import annotations

import itertools
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
_FULL_WHITE: Final[RGB] = (1.0, 1.0, 1.0)
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
# A drop is a sudden step: the audio just after a moment is this much louder than just before it.
_DROP_STEP: Final[float] = 1.25
_DROP_BEFORE_US: Final[int] = 500_000
_DROP_AFTER_US: Final[int] = 1_000_000
_DROP_BEFORE_MIN_FRAMES: Final[int] = 3
_DROP_EDGE_FRAMES: Final[int] = 8
# The new level must also clear what has been playing, so a short stop that returns is no drop.
_DROP_OVER_SHORT: Final[float] = 1.15
_DROP_OVER_LONG: Final[float] = 1.05
_DROP_HORIZON_US: Final[int] = 6_000_000
_DROP_SCAN_US: Final[int] = 250_000
_DROP_BEATS: Final[int] = 16
_DIP_MAX_US: Final[int] = 600_000
_LOOK_FADE_US: Final[int] = 500_000
_LOOK_BARS: Final[int] = 8
_FUTURE_FRAMES: Final[int] = 600
_BREAKDOWN_RATIO: Final[float] = 0.6
_BREAKDOWN_SWEEP_S: Final[float] = 6.0
# The room holds the drop color this long, then eases into the shockwave.
_DROP_HOLD_BEATS: Final[float] = 8.0
_DROP_EASE_BEATS: Final[float] = 2.0
_DROP_ROLL_BEATS: Final[int] = 4
_ROLL_MIN_INTERVAL_US: Final[int] = 300_000
_BLINDER_BARS: Final[int] = 16
_BLINDER_DECAY_US: Final[float] = 120_000.0
_BLINDER_AMOUNT: Final[float] = 0.8
_MAX_COLOR_LEVEL: Final[float] = 0.85
# A jump in energy around a track change is the new song starting, not a drop.
_TRACK_SETTLE_BEFORE_US: Final[int] = 1_000_000
_TRACK_SETTLE_AFTER_US: Final[int] = 3_000_000
_DROP_CONFIRM_FROM_US: Final[int] = 500_000
_DROP_CONFIRM_MIN_FRAMES: Final[int] = 5

_FLASH_DELTA: Final[float] = 0.1
_FLASHES_PER_SECOND: Final[int] = 3
_FLASH_WINDOW_US: Final[int] = 1_000_000


@dataclass(frozen=True, slots=True)
class BeatEvent:
    """The latest scheduled beat at the render time."""

    timestamp_us: int
    is_downbeat: bool
    interval_us: int


@dataclass(frozen=True, slots=True)
class _LightProfile:
    """Brightness levels of Party Auto's sections, as a fraction of full color."""

    breakdown: float
    calm: float
    groove: float
    peak: float
    build: float
    dip: float
    hold: float
    # scales groove and peak looks, up to full color
    gain: float


_LIGHT_PROFILES: Final[dict[str, _LightProfile]] = {
    "moody": _LightProfile(
        breakdown=0.04, calm=0.6, groove=0.14, peak=0.3, build=0.25, dip=0.05, hold=0.75, gain=0.7
    ),
    "balanced": _LightProfile(
        breakdown=0.06, calm=1.0, groove=0.25, peak=0.5, build=0.4, dip=0.1, hold=0.9, gain=1.0
    ),
    "bright": _LightProfile(
        breakdown=0.1, calm=1.2, groove=0.4, peak=0.65, build=0.55, dip=0.2, hold=1.0, gain=1.25
    ),
}


class _Section(StrEnum):
    """Song section tracked by Party Auto."""

    BREAKDOWN = "breakdown"
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

    def __init__(
        self,
        channels: list[LightChannel],
        rng: random.Random | None = None,
        light_range: str = "balanced",
        full_white: bool = False,
    ) -> None:
        """
        Initialize the engine.

        :param channels: The lights to render, with their positions.
        :param rng: Random source for Confetti; a new one when omitted.
        :param light_range: How bright Party Auto runs: moody, balanced or bright.
        :param full_white: Run Party Auto's white flashes and blinders at full power.
        """
        self._count = len(channels)
        self._ranks, self._radial = _layout(channels)
        self._rng = rng or random.Random()  # noqa: S311
        self._profile = _LIGHT_PROFILES["balanced"]
        self.set_light_range(light_range)
        self._hit_white = _FULL_WHITE if full_white else _WHITE
        self._effects: dict[str, Effect] = {
            "breakdown": self._breakdown,
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
        self._section = _Section.CALM
        self.clear_schedule()

    def clear_schedule(self) -> None:
        """Forget the beat position and any expected drop, keeping the mood across a track."""
        if self._section in (_Section.BUILD, _Section.DIP, _Section.DROP):
            self._steady = _Section.PEAK
            self._steady_since = None
        self._section = self._steady
        self._interval_us = _DEFAULT_INTERVAL_US
        self._beat_us = -1
        self._beat_index = -1
        self._bar_beat = 3
        self._bar = -1
        self._drop_at_us: int | None = None
        self._drop_minimum = 0.0
        self._drop_color: RGB | None = None
        self._drop_start_us = 0
        self._drop_until_us = 0
        self._build_start_us = 0
        self._track_start_us: int | None = None

    def set_light_range(self, light_range: str) -> None:
        """
        Set how bright Party Auto runs.

        :param light_range: moody, balanced or bright; anything else means balanced.
        """
        self._profile = _LIGHT_PROFILES.get(light_range, _LIGHT_PROFILES["balanced"])

    def set_full_white(self, enabled: bool) -> None:
        """
        Set whether Party Auto's white flashes and blinders run at full power.

        :param enabled: True for full power, False to stay at the color limit.
        """
        self._hit_white = _FULL_WHITE if enabled else _WHITE

    def mark_track_start(self, timestamp_us: int) -> None:
        """
        Record where a new track starts, so its opening is not mistaken for a drop.

        :param timestamp_us: Playback time of the track's first beat.
        """
        self._track_start_us = timestamp_us

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

    def _chase(
        self, now_us: int, palette: list[RGB], *, subdivision: int | None = None
    ) -> list[RGB]:
        """Bounce a lit head across the room, stepping this many times per beat."""
        color = _pick(palette, self._bar)
        if self._count == 1:
            return [_scale(color, _FLOOR + (1.0 - _FLOOR) * self._hit(now_us, 220_000.0))]
        steps = subdivision or (2 if self._interval_us >= 400_000 else 1)
        phase = self._beat_phase(now_us)
        step = self._beat_index * steps + int(phase * steps)
        step_us = self._interval_us / steps
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

    def _breakdown(self, now_us: int, palette: list[RGB]) -> list[RGB]:
        """Keep the room dark with one slow accent of color moving through it."""
        color = _pick(palette, self._bar)
        floor = self._profile.breakdown
        seconds = now_us / 1_000_000.0
        sweep = 0.5 + 0.5 * math.sin(seconds * math.tau / _BREAKDOWN_SWEEP_S)
        if self._count == 1:
            return [_scale(color, floor + (0.5 - floor) * sweep)]
        head = sweep * (self._count - 1)
        return [
            _scale(color, max(floor, 0.55 * math.exp(-(((rank - head) / 1.2) ** 2))))
            for rank in self._ranks
        ]

    def _party_auto(self, now_us: int, palette: list[RGB]) -> list[RGB]:
        """Pick the look that fits the current song section."""
        if self._section is _Section.BUILD:
            self._set_look("chase", now_us, fade=False)
            return self._build_look(now_us, palette)
        if self._section is _Section.DIP:
            self._set_look("chase", now_us, fade=False)
            return [_scale(_pick(palette, self._bar), self._profile.dip)] * self._count
        if self._section is _Section.DROP:
            self._set_look("shockwave", now_us, fade=False)
            return self._drop_look(now_us, palette)
        self._drop_color = None
        self._set_look(self._steady_look(), now_us, fade=True)
        colors = self._effects[self._look](now_us, palette)
        elapsed = now_us - self._look_since
        if self._previous_look is not None and elapsed < _LOOK_FADE_US:
            previous = self._effects[self._previous_look](now_us, palette)
            blend = elapsed / _LOOK_FADE_US
            colors = [_lerp(old, new, blend) for old, new in zip(previous, colors, strict=True)]
        else:
            self._previous_look = None
        return self._apply_steady_levels(now_us, colors)

    def _build_look(self, now_us: int, palette: list[RGB]) -> list[RGB]:
        """Ramp a chase up toward the drop, rolling faster through the last bar."""
        progress = self._build_progress(now_us)
        steps: int | None = 2 if progress >= 0.5 else None
        if (
            self._drop_at_us is not None
            and self._drop_at_us - now_us <= _DROP_ROLL_BEATS * self._interval_us
        ):
            steps = 4 if self._interval_us >= _ROLL_MIN_INTERVAL_US else 2
        low = self._profile.build
        colors = self._chase(now_us, palette, subdivision=steps)
        level = low + (1.0 - low) * progress
        floor = max(_FLOOR, low * progress)
        return [_lift(_scale(color, level), floor) for color in colors]

    def _drop_look(self, now_us: int, palette: list[RGB]) -> list[RGB]:
        """Flash white, hold the room in a contrasting color, then ease into the shockwave."""
        if self._drop_color is None:
            current = _pick(palette, self._bar)
            self._drop_color = max(palette, key=lambda color: math.dist(color, current))
        elapsed = max(0, now_us - self._drop_start_us)
        beats = elapsed / self._interval_us
        hold = max(0.0, min(1.0, (_DROP_HOLD_BEATS - beats) / _DROP_EASE_BEATS))
        held = _scale(
            self._drop_color, self._profile.hold * (0.88 + 0.12 * self._hit(now_us, 200_000.0))
        )
        flash = math.exp(-elapsed / 180_000.0)
        return [
            _lerp(_lerp(_lift(color, self._profile.peak), held, hold), self._hit_white, flash)
            for color in self._shockwave(now_us, palette)
        ]

    def _apply_steady_levels(self, now_us: int, colors: list[RGB]) -> list[RGB]:
        """Set the brightness of a calm, groove or peak section, with phrase blinders."""
        if self._section is _Section.BREAKDOWN:
            return colors
        if self._section is _Section.CALM:
            return [_scale(color, self._profile.calm) for color in colors]
        floor = self._profile.peak
        if self._section is _Section.GROOVE:
            dark_phrase = (max(self._bar, 0) // _LOOK_BARS) % 2 == 1
            floor = self._profile.groove / 2 if dark_phrase else self._profile.groove
        lifted = [_lift(_apply_gain(color, self._profile.gain), floor) for color in colors]
        if self._bar <= 0 or self._bar % _BLINDER_BARS or self._bar_beat:
            return lifted
        blinder = _BLINDER_AMOUNT * self._hit(now_us, _BLINDER_DECAY_US)
        return [_lerp(color, self._hit_white, blinder) for color in lifted]

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
        """Schedule a drop when the queued audio holds a sudden step up in energy."""
        if self._drop_at_us is not None:
            self._confirm_drop()
            return
        if now_us < self._drop_until_us or self._energy_us is None or self._short_energy < _SILENCE:
            return
        minimum = max(self._short_energy * _DROP_OVER_SHORT, self._long_energy * _DROP_OVER_LONG)
        step_us = _find_energy_step(list(self._future), minimum, now_us + _DROP_HORIZON_US)
        if step_us is None or self._is_track_opening(step_us):
            return
        self._drop_at_us = self._snap_to_beat(step_us, now_us)
        self._drop_minimum = minimum
        self._build_start_us = now_us

    def _confirm_drop(self) -> None:
        """Cancel the expected drop once the queued audio shows the jump does not last."""
        if self._drop_at_us is None:
            return
        start = self._drop_at_us + _DROP_CONFIRM_FROM_US
        end = self._drop_at_us + _DROP_AFTER_US
        values = [energy for timestamp, energy in self._future if start <= timestamp < end]
        if len(values) >= _DROP_CONFIRM_MIN_FRAMES and (
            sum(values) / len(values) < self._drop_minimum
        ):
            self._drop_at_us = None

    def _is_track_opening(self, timestamp_us: int) -> bool:
        """Return whether a moment falls in the opening of the current track."""
        if self._track_start_us is None:
            return False
        return (
            self._track_start_us - _TRACK_SETTLE_BEFORE_US
            <= timestamp_us
            <= self._track_start_us + _TRACK_SETTLE_AFTER_US
        )

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
        if self._short_energy < _SILENCE:
            candidate = _Section.CALM
        elif ratio < _BREAKDOWN_RATIO:
            candidate = _Section.BREAKDOWN
        elif ratio < _CALM_RATIO:
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
            # odd phrases are the dark ones, see _apply_steady_levels
            return ("color_slam", "chase", "two_tone", "rhythm_section")[rotation % 4]
        if self._section is _Section.BREAKDOWN:
            return "breakdown"
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


def _find_energy_step(frames: list[tuple[int, float]], minimum: float, last_us: int) -> int | None:
    """
    Return the time of the first sudden step up in energy, or None.

    :param frames: Queued ``(timestamp, energy)`` frames in time order.
    :param minimum: The level the audio after the step must reach.
    :param last_us: The latest time a step may start.
    """
    totals = [0.0, *itertools.accumulate(energy for _, energy in frames)]
    start = end = 0
    best_ratio = 0.0
    best_index = last_index = -1
    for index, (timestamp, _) in enumerate(frames):
        edge_end = index + _DROP_EDGE_FRAMES
        if timestamp > last_us or edge_end > len(frames):
            break
        last_index = index
        while frames[start][0] < timestamp - _DROP_BEFORE_US:
            start += 1
        while end < len(frames) and frames[end][0] < timestamp + _DROP_AFTER_US:
            end += 1
        end = max(end, edge_end)
        before_count = index - start
        if before_count < _DROP_BEFORE_MIN_FRAMES:
            continue
        before = (totals[index] - totals[start]) / before_count
        edge = (totals[edge_end] - totals[index]) / _DROP_EDGE_FRAMES
        sustained = (totals[end] - totals[index]) / (end - index)
        ratio = edge / max(before, 1e-6)
        if min(edge, sustained) >= minimum and ratio >= _DROP_STEP:
            if ratio > best_ratio:
                best_ratio = ratio
                best_index = index
        elif best_index >= 0:
            return frames[best_index][0]
    # A ratio still climbing at the last visible frame may not have reached the step yet.
    return frames[best_index][0] if 0 <= best_index < last_index else None


def _clamp_interval(interval_us: int) -> int:
    """Keep a beat interval inside the range the effects are built for."""
    return max(_MIN_INTERVAL_US, min(_MAX_INTERVAL_US, interval_us))


def _apply_gain(color: RGB, gain: float) -> RGB:
    """Scale a color's brightness without pushing it past full color."""
    top = max(color)
    if top <= 0.0:
        return color
    return _scale(color, min(gain, max(1.0, _MAX_COLOR_LEVEL / top)))


def _lift(color: RGB, floor: float) -> RGB:
    """Raise a color to at least ``floor`` of full color, keeping its hue."""
    top = max(color)
    target = floor * _MAX_COLOR_LEVEL
    if top <= 0.0 or top >= target:
        return color
    return _scale(color, target / top)


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
