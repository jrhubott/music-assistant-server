"""Tests for the Hue Lights Sync party modes."""

from __future__ import annotations

import random
from collections.abc import Callable
from unittest.mock import MagicMock

import pytest
from aiosendspin.models.visualizer import BeatTiming
from hue_entertainment import LightChannel

from music_assistant.providers.hue_entertainment.analyzer import _MODES, HueAudioAnalyzer
from music_assistant.providers.hue_entertainment.constants import COLOR_MODES, PARTY_MODES
from music_assistant.providers.hue_entertainment.party_modes import (
    BeatEvent,
    PartyEngine,
    _FlashLimiter,
    _Section,
)

_RED = (0.85, 0.0, 0.0)
_BLUE = (0.0, 0.0, 0.85)
_PALETTE = [_RED, _BLUE]
_TICK_US = 33_333


def _row(count: int) -> list[LightChannel]:
    """Create lights in a left-to-right row."""
    return [
        LightChannel(
            channel_id=i,
            service_id=f"svc_{i}",
            name=f"Light {i}",
            position=(i / max(1, count - 1) * 2 - 1, 0.0, 0.0),
        )
        for i in range(count)
    ]


def _beats(count: int, interval_us: int = 500_000) -> list[BeatTiming]:
    """Create a 4/4 beat schedule."""
    return [BeatTiming(timestamp_us=i * interval_us, is_downbeat=i % 4 == 0) for i in range(count)]


def _level(color: tuple[float, float, float]) -> float:
    """Return a color's brightness."""
    return max(color)


def _flash_count(levels: list[float]) -> int:
    """Count dark-to-bright rises of at least the limiter's flash size."""
    flashes = 0
    low_point = levels[0]
    rising = False
    peak = levels[0]
    for level in levels:
        if rising:
            peak = max(peak, level)
            if level <= peak - 0.1:
                rising = False
                low_point = level
        else:
            low_point = min(low_point, level)
            if level >= low_point + 0.1:
                flashes += 1
                rising = True
                peak = level
    return flashes


def test_existing_modes_are_kept_first() -> None:
    """The original modes stay available, with Smooth as the default."""
    assert COLOR_MODES[:4] == ("smooth", "ambient", "flashing", "energetic")
    assert set(COLOR_MODES[:4]) == set(_MODES)
    assert set(PARTY_MODES).isdisjoint(_MODES)


@pytest.mark.parametrize("mode", ["smooth", "ambient", "flashing", "energetic"])
def test_existing_modes_ignore_the_party_engine(mode: str) -> None:
    """An original mode never renders through the party engine."""
    analyzer = HueAudioAnalyzer(_row(3), color_mode=mode)
    analyzer.push_beats(_beats(8))
    analyzer._party.render = None  # type: ignore[assignment,method-assign]
    assert len(analyzer.render(750_000)) == 3


@pytest.mark.parametrize("mode", PARTY_MODES)
@pytest.mark.parametrize("count", [1, 2, 5])
def test_party_modes_render_every_light(mode: str, count: int) -> None:
    """Every mode gives valid colors with and without a beat schedule."""
    for with_beats in (True, False):
        analyzer = HueAudioAnalyzer(_row(count), color_mode=mode)
        if with_beats:
            analyzer.push_beats(_beats(40))
        else:
            analyzer.apply_peak(255, 100_000)
        for tick in range(300):
            now = tick * _TICK_US
            analyzer.apply_spectrum([20000 + tick * 40] * 17, now)
            commands = analyzer.render(now)
            assert len(commands) == count
            assert all(0 <= c.red <= 65535 for c in commands)


@pytest.mark.parametrize("mode", PARTY_MODES)
def test_party_modes_respect_brightness_setting(mode: str) -> None:
    """The brightness setting scales party output."""
    analyzer = HueAudioAnalyzer(_row(3), color_mode=mode, brightness=0)
    analyzer.push_beats(_beats(8))
    commands = analyzer.render(500_000)
    assert all(c.red == c.green == c.blue == 0 for c in commands)


def test_chase_moves_across_the_row() -> None:
    """The brightest light walks along the row and back."""
    engine = PartyEngine(_row(3))
    heads = []
    for index in range(4):
        timestamp = index * 1_000_000
        beat = BeatEvent(timestamp, index == 0, 1_000_000)
        colors = engine.render("chase", timestamp, _PALETTE, [], beat, None)
        heads.append(max(range(3), key=lambda i: _level(colors[i])))
    assert heads == [0, 2, 0, 2]


def test_shockwave_starts_in_the_center() -> None:
    """On the beat the middle light is brighter than the edges."""
    engine = PartyEngine(_row(3))
    colors = engine.render("shockwave", 0, _PALETTE, [], BeatEvent(0, True, 500_000), None)
    assert _level(colors[1]) > _level(colors[0])
    assert _level(colors[1]) > _level(colors[2])


def test_color_slam_changes_color_on_the_downbeat() -> None:
    """Color holds through a bar and changes on the next downbeat."""
    engine = PartyEngine(_row(2))
    seen = []
    for index in range(5):
        timestamp = index * 500_000
        beat = BeatEvent(timestamp, index % 4 == 0, 500_000)
        colors = engine.render("color_slam", timestamp, _PALETTE, [], beat, None)
        seen.append(colors[0][0] > colors[0][2])
    assert seen == [True, True, True, True, False]


def test_two_tone_swaps_no_faster_than_three_per_second() -> None:
    """At a very fast tempo the swap skips beats."""
    engine = PartyEngine(_row(2))
    swaps = 0
    last = None
    for index in range(4):
        timestamp = index * 200_000
        beat = BeatEvent(timestamp, False, 200_000)
        colors = engine.render("two_tone", timestamp, _PALETTE, [], beat, None)
        red_first = colors[0][0] > colors[0][2]
        swaps += last is not None and red_first != last
        last = red_first
    assert swaps <= 2


def test_confetti_is_repeatable_with_a_seed() -> None:
    """A seeded engine picks the same lights."""
    runs = []
    for _ in range(2):
        engine = PartyEngine(_row(5), rng=random.Random(7))  # noqa: S311
        runs.append(engine.render("confetti", 0, _PALETTE, [], BeatEvent(0, True, 500_000), None))
    assert runs[0] == runs[1]


def test_flash_limiter_caps_rises_at_three_per_second() -> None:
    """A 10 Hz square wave comes out with at most three flashes in a second."""
    limiter = _FlashLimiter()
    levels = []
    for tick in range(30):
        wanted = 0.85 if tick % 3 == 0 else 0.05
        levels.append(_level(limiter.apply(tick * _TICK_US, (wanted, wanted, wanted))))
    assert _flash_count(levels) <= 3


def test_flash_limiter_passes_a_slow_ramp() -> None:
    """A steady build is one rise, so the limiter leaves it alone."""
    limiter = _FlashLimiter()
    for tick in range(60):
        wanted = tick / 60
        assert limiter.apply(tick * _TICK_US, (wanted, 0.0, 0.0)) == (wanted, 0.0, 0.0)


@pytest.mark.parametrize("mode", PARTY_MODES)
def test_party_modes_stay_within_the_flash_limit(mode: str) -> None:
    """At 240 BPM no light flashes more than three times in any second."""
    engine = PartyEngine(_row(3), rng=random.Random(1))  # noqa: S311
    interval = 250_000
    levels: list[list[float]] = [[], [], []]
    for tick in range(90):
        now = tick * _TICK_US
        index = now // interval
        beat = BeatEvent(index * interval, index % 4 == 0, interval)
        for light, color in enumerate(engine.render(mode, now, _PALETTE, [], beat, None)):
            levels[light].append(_level(color))
    for series in levels:
        for start in range(len(series) - 30):
            assert _flash_count(series[start : start + 30]) <= 3


def test_party_auto_finds_a_drop_in_the_queued_audio() -> None:
    """Quiet audio followed by loud audio gives a build, a dip and a drop, in order."""
    engine = PartyEngine(_row(3))
    quiet, loud = 1500, 40000
    drop_us = 30_000_000
    for frame in range(800):
        timestamp = frame * 50_000
        if timestamp <= drop_us + 3_000_000:
            engine.observe(timestamp, [loud if timestamp >= drop_us else quiet] * 17)
    sections: list[_Section] = []
    for tick in range(int((drop_us + 1_000_000) / _TICK_US)):
        now = tick * _TICK_US
        index = now // 500_000
        engine.render(
            "party_auto", now, _PALETTE, [], BeatEvent(index * 500_000, False, 500_000), None
        )
        if not sections or sections[-1] is not engine._section:
            sections.append(engine._section)
    assert _Section.DROP in sections
    drop = sections.index(_Section.DROP)
    assert _Section.DIP in sections[:drop]
    assert _Section.BUILD in sections[:drop]


def _with_kicks(energy: float, timestamp_us: int) -> float:
    """Add a 120 BPM kick pattern to a section's energy level."""
    return energy * (1.25 if timestamp_us % 500_000 < 100_000 else 0.9)


def _rising_build(timestamp_us: int) -> float:
    """Return a verse, a build that gets louder for 10 seconds, then a drop at 30 seconds."""
    if timestamp_us < 20_000_000:
        energy = 0.35
    elif timestamp_us < 30_000_000:
        energy = 0.35 + 0.25 * (timestamp_us - 20_000_000) / 10_000_000
    else:
        energy = 0.9
    return _with_kicks(energy, timestamp_us)


def _first_drop_us(
    energy_at: Callable[[int], float], duration_us: int, lookahead_us: int = 4_000_000
) -> tuple[int | None, set[_Section]]:
    """Play a song with audio queued ahead; return the first drop and the sections before it."""
    engine = PartyEngine(_row(3))
    next_frame = 0
    before_drop: set[_Section] = set()
    for tick in range(duration_us // _TICK_US):
        now = tick * _TICK_US
        while next_frame <= min(now + lookahead_us, duration_us):
            level = min(1.0, energy_at(next_frame))
            engine.observe(next_frame, [int(level * level * 65535)] * 17)
            next_frame += 50_000
        index = now // 500_000
        beat = BeatEvent(index * 500_000, index % 4 == 0, 500_000)
        engine.render("party_auto", now, _PALETTE, [], beat, None)
        if engine._section is _Section.DROP:
            return now, before_drop
        before_drop.add(engine._section)
    return None, before_drop


@pytest.mark.parametrize("lookahead_us", [4_000_000, 1_000_000])
def test_party_auto_catches_a_drop_after_a_rising_build(lookahead_us: int) -> None:
    """A build that gets louder still leads to a drop on time, however little audio is queued."""
    drop_us, before_drop = _first_drop_us(_rising_build, 34_000_000, lookahead_us)
    assert drop_us is not None
    assert 30_000_000 <= drop_us <= 30_000_000 + 2 * _TICK_US
    assert _Section.DIP in before_drop


def test_party_auto_does_not_treat_a_steady_rise_as_a_drop() -> None:
    """Music that only gets gradually louder never fires a drop."""
    drop_us, _ = _first_drop_us(lambda ts: _with_kicks(0.3 + 0.5 * ts / 40_000_000, ts), 40_000_000)
    assert drop_us is None


def test_party_auto_ignores_kicks_in_a_loud_section() -> None:
    """A steady four-on-the-floor groove is not a drop."""
    drop_us, _ = _first_drop_us(lambda ts: _with_kicks(0.7, ts), 40_000_000)
    assert drop_us is None


def test_party_auto_ignores_a_short_gap_at_the_same_level() -> None:
    """A one-beat stop that returns to the same level is not a drop."""

    def with_gap(timestamp_us: int) -> float:
        if 20_000_000 <= timestamp_us < 20_500_000:
            return 0.05
        return _with_kicks(0.5, timestamp_us)

    drop_us, _ = _first_drop_us(with_gap, 30_000_000)
    assert drop_us is None


def test_switching_to_a_party_mode_keeps_queued_audio() -> None:
    """Frames queued before a live mode change still feed the look-ahead."""
    analyzer = HueAudioAnalyzer(_row(2))
    analyzer.apply_spectrum([1000] * 17, 1_000_000)
    analyzer.update_settings(color_mode="party_auto")
    assert len(analyzer._party._future) == 1


def test_seek_resets_party_state() -> None:
    """A seek clears the bar position and the queued audio."""
    analyzer = HueAudioAnalyzer(_row(2), color_mode="chase")
    analyzer.push_beats(_beats(8))
    analyzer.apply_spectrum([1000] * 17, 5_000_000)
    analyzer.render(1_000_000)
    analyzer.clear_beats()
    assert analyzer._party._beat_index == -1
    assert not analyzer._party._future


def _engine_at(section: _Section, light_range: str = "balanced", count: int = 5) -> PartyEngine:
    """Return an engine that holds a steady song section."""
    engine = PartyEngine(_row(count), rng=random.Random(3), light_range=light_range)  # noqa: S311
    engine._update_section = MagicMock()  # type: ignore[method-assign]
    engine._section = section
    engine._steady = section
    return engine


def _beat_at(now_us: int, interval_us: int = 500_000) -> BeatEvent:
    """Return the 4/4 beat playing at ``now_us``."""
    index = now_us // interval_us
    return BeatEvent(index * interval_us, index % 4 == 0, interval_us)


def _lowest_level(engine: PartyEngine, start_us: int, ticks: int = 60) -> float:
    """Return the dimmest any light gets while rendering Party Auto."""
    lowest = 1.0
    for tick in range(ticks):
        now = start_us + tick * _TICK_US
        colors = engine.render("party_auto", now, _PALETTE, [], _beat_at(now), None)
        lowest = min(lowest, *(_level(color) for color in colors))
    return lowest


def test_peaks_stay_brighter_than_breakdowns() -> None:
    """Loud sections keep a high floor while breakdowns can go properly dark."""
    peak = _lowest_level(_engine_at(_Section.PEAK), 1_000_000)
    breakdown = _lowest_level(_engine_at(_Section.BREAKDOWN), 1_000_000)
    assert peak >= 0.5 * 0.85 - 1e-6
    assert breakdown <= 0.1


@pytest.mark.parametrize("section", [_Section.GROOVE, _Section.PEAK])
def test_light_range_orders_brightness(section: _Section) -> None:
    """Moody runs darker than Balanced, and Balanced darker than Bright."""
    levels = [
        _lowest_level(_engine_at(section, light_range), 1_000_000)
        for light_range in ("moody", "balanced", "bright")
    ]
    assert levels[0] < levels[1] < levels[2]


def test_unknown_light_range_is_balanced() -> None:
    """A stored value from another version falls back to Balanced."""
    assert _lowest_level(_engine_at(_Section.PEAK, "other"), 1_000_000) == pytest.approx(
        _lowest_level(_engine_at(_Section.PEAK), 1_000_000)
    )


def test_groove_alternates_bright_and_dark_phrases() -> None:
    """Even eight-bar phrases hold a higher floor than odd ones."""
    bright = _engine_at(_Section.GROOVE)
    bright._bar = 0
    dark = _engine_at(_Section.GROOVE)
    dark._bar = 8
    colors = [(0.01, 0.01, 0.01)]
    assert _level(bright._apply_steady_levels(0, colors)[0]) > _level(
        dark._apply_steady_levels(0, colors)[0]
    )


def test_blinder_fires_on_the_first_beat_of_a_long_phrase() -> None:
    """Bar 16 opens with a white burst that bar 15 does not have."""
    colors = [(0.85, 0.0, 0.0)]
    engine = _engine_at(_Section.PEAK)
    engine._beat_us = 0
    engine._bar_beat = 0
    engine._bar = 15
    assert engine._apply_steady_levels(0, colors)[0][1] == 0.0
    engine._bar = 16
    assert engine._apply_steady_levels(0, colors)[0][1] > 0.5


def test_drop_holds_the_room_in_a_contrasting_color() -> None:
    """Two beats after the hit every light shows the color farthest from the old one."""
    engine = PartyEngine(_row(5))
    engine._update_section = MagicMock()  # type: ignore[method-assign]
    engine._section = _Section.DROP
    engine._bar = 0
    engine._beat_index = 0
    engine._beat_us = 1_000_000
    colors = engine._drop_look(2_000_000, _PALETTE)
    assert all(color[2] > 0.6 and color[0] < 0.05 for color in colors)
    engine._beat_us = 6_000_000
    eased = engine._drop_look(6_000_000, _PALETTE)
    assert len({round(_level(color), 3) for color in eased}) > 1


def test_build_rolls_faster_through_the_last_bar() -> None:
    """The chase takes four steps per beat in the bar before the drop."""
    engine = PartyEngine(_row(5))
    engine._beat_index = 0
    engine._beat_us = 10_000_000
    engine._build_start_us = 0
    engine._drop_at_us = 11_500_000
    heads = set()
    for offset in (0, 130_000, 260_000, 390_000):
        now = 10_000_000 + offset
        colors = engine._build_look(now, _PALETTE)
        heads.add(max(range(5), key=lambda i: _level(colors[i])))
    assert len(heads) == 4


def test_a_jump_that_does_not_last_is_canceled() -> None:
    """A half-second burst is dropped once the audio after it is visible."""

    def burst(timestamp_us: int) -> float:
        loud = 30_000_000 <= timestamp_us < 30_500_000
        return _with_kicks(0.9 if loud else 0.35, timestamp_us)

    drop_us, _ = _first_drop_us(burst, 34_000_000, lookahead_us=1_000_000)
    assert drop_us is None


def test_a_new_track_opening_is_not_a_drop() -> None:
    """The jump into a louder song at a track change does not fire a drop."""
    engine = PartyEngine(_row(3))
    engine.mark_track_start(20_000_000)
    assert engine._is_track_opening(20_000_000)
    assert engine._is_track_opening(19_500_000)
    assert not engine._is_track_opening(24_000_000)


def test_track_change_keeps_the_energy_of_a_drop() -> None:
    """A track change during a drop continues in the peak look instead of going calm."""
    engine = PartyEngine(_row(3))
    engine._section = _Section.DROP
    engine.clear_schedule()
    assert engine._section is _Section.PEAK
    assert engine._steady is _Section.PEAK


def test_first_beats_of_a_track_mark_its_start() -> None:
    """Only the first schedule segment after a clear sets the track start."""
    analyzer = HueAudioAnalyzer(_row(2), color_mode="party_auto")
    analyzer.push_beats(_beats(4))
    analyzer.push_beats([BeatTiming(timestamp_us=9_000_000, is_downbeat=True)])
    assert analyzer._party._track_start_us == 0


def test_light_range_updates_live() -> None:
    """Changing the setting reaches the engine without a restart."""
    analyzer = HueAudioAnalyzer(_row(2), color_mode="party_auto")
    analyzer.update_settings(light_range="bright")
    assert analyzer._party._profile.peak == pytest.approx(0.65)


@pytest.mark.parametrize(("full_white", "expected"), [(False, 0.85), (True, 1.0)])
def test_full_white_sets_the_level_of_the_drop_flash(full_white: bool, expected: float) -> None:
    """The white flash on a drop reaches the chosen level, in white."""
    engine = PartyEngine(_row(3), full_white=full_white)
    engine._drop_start_us = 1_000_000
    engine._beat_us = 1_000_000
    colors = engine._drop_look(1_000_000, _PALETTE)
    assert all(color == pytest.approx((expected, expected, expected)) for color in colors)


def test_full_white_reaches_the_blinder() -> None:
    """A blinder goes past the color limit only with the option on."""
    colors = [(0.85, 0.85, 0.85)]
    engine = _engine_at(_Section.PEAK)
    engine._beat_us = 0
    engine._bar_beat = 0
    engine._bar = 16
    assert _level(engine._apply_steady_levels(0, colors)[0]) == pytest.approx(0.85)
    engine.set_full_white(True)
    assert _level(engine._apply_steady_levels(0, colors)[0]) > 0.95


@pytest.mark.parametrize("mode", [mode for mode in PARTY_MODES if mode != "party_auto"])
def test_full_white_leaves_other_party_modes_alone(mode: str) -> None:
    """Only Party Auto uses the full-power white."""
    outputs = []
    for full_white in (False, True):
        engine = PartyEngine(_row(3), rng=random.Random(4), full_white=full_white)  # noqa: S311
        outputs.append(
            [
                engine.render(mode, tick * _TICK_US, _PALETTE, [], _beat_at(tick * _TICK_US), None)
                for tick in range(120)
            ]
        )
    assert outputs[0] == outputs[1]


def test_full_white_updates_live() -> None:
    """Changing the setting reaches the engine without a restart."""
    analyzer = HueAudioAnalyzer(_row(2), color_mode="party_auto")
    analyzer.update_settings(full_white=True)
    assert max(analyzer._party._hit_white) == 1.0
