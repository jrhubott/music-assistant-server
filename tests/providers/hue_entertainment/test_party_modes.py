"""Tests for the Hue Lights Sync party modes."""

from __future__ import annotations

import random

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
