"""Tests for the Wake AirPlay button of a Sonos speaker."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiosonos.api.models import SonosCapability
from aiosonos.exceptions import FailedCommand
from music_assistant_models.enums import ConfigEntryType, IdentifierType, PlaybackState

from music_assistant.providers.airplay import wake
from music_assistant.providers.airplay.provider import AirPlayProvider
from music_assistant.providers.airplay.wake import WakeOutcome
from music_assistant.providers.sonos.const import (
    ACTION_WAKE_AIRPLAY,
    SOURCE_LINE_IN,
    SOURCE_TV,
    WAKE_AIRPLAY_MESSAGES,
)
from music_assistant.providers.sonos.helpers import parse_boot_seq
from music_assistant.providers.sonos.player import SonosPlayer

UID = "RINCON_F0F6C193E5DD01400"
MAC = "F0:F6:C1:93:E5:DD"


def _speaker(**overrides: Any) -> MagicMock:
    """
    Return a Sonos speaker stand-in that runs the real button code.

    :param overrides: Attributes to set on the speaker.
    """
    speaker = MagicMock()
    speaker.protocol_id = UID
    speaker.display_name = "Patio"
    speaker.connected = True
    speaker._attr_active_source = None
    speaker._attr_playback_state = PlaybackState.IDLE
    speaker._reported_playback_state = PlaybackState.IDLE
    speaker._stopped_by_ma = False
    speaker._attr_volume_level = 30
    speaker._attr_device_info.identifiers = {
        IdentifierType.MAC_ADDRESS: MAC,
        IdentifierType.IP_ADDRESS: "192.0.2.10",
    }
    speaker.client.player.group_members = [UID]
    speaker.volume_set = AsyncMock()
    speaker._read_boot_seq = AsyncMock(return_value=8)
    speaker._wake_result = lambda key: SonosPlayer._wake_result(speaker, key)
    speaker._wake_blocker = lambda: SonosPlayer._wake_blocker(speaker)
    speaker._restore_wake_volume = lambda volume: SonosPlayer._restore_wake_volume(speaker, volume)
    speaker.mass.players.wants_protocol.return_value = True
    airplay = MagicMock(spec=AirPlayProvider)
    airplay.get_player.return_value = None
    speaker.mass.get_provider.return_value = airplay
    for name, value in overrides.items():
        setattr(speaker, name, value)
    return speaker


async def _press(speaker: MagicMock, outcome: WakeOutcome = WakeOutcome.WOKEN) -> tuple[Any, Any]:
    """
    Press the button with a wake that ends in an outcome.

    :param speaker: The speaker.
    :param outcome: What the wake comes to.
    :return: The result of the button and the mock of the wake.
    """
    with patch.object(wake, "wake_receiver", AsyncMock(return_value=outcome)) as wake_mock:
        result = await SonosPlayer._wake_airplay(speaker)
    return result, wake_mock


def _message(key: str) -> str:
    """Return the expected message of an outcome."""
    return WAKE_AIRPLAY_MESSAGES[key].format("Patio")


async def test_wake_reports_success_and_targets_the_speaker() -> None:
    """An idle speaker is woken on its own address and MAC."""
    speaker = _speaker()
    result, wake_mock = await _press(speaker)
    assert result.message == _message("woken")
    assert result.translation_key == "wake_airplay.woken"
    assert wake_mock.await_args.args[:3] == (
        speaker.mass.get_provider.return_value,
        "192.0.2.10",
        MAC,
    )
    speaker.volume_set.assert_not_awaited()


async def test_wake_skips_a_speaker_that_announces() -> None:
    """A healthy speaker gets no session."""
    speaker = _speaker()
    speaker.mass.get_provider.return_value.get_player.return_value = MagicMock()
    result, wake_mock = await _press(speaker)
    assert result.message == _message("already_announced")
    wake_mock.assert_not_awaited()


async def test_wake_leaves_a_disabled_airplay_output_alone() -> None:
    """An output the user switched off is not a lost announcement."""
    speaker = _speaker()
    speaker.mass.players.wants_protocol.return_value = False
    result, wake_mock = await _press(speaker)
    assert result.message == _message("airplay_disabled")
    speaker.mass.players.wants_protocol.assert_called_once_with("apf0f6c193e5dd")
    wake_mock.assert_not_awaited()


@pytest.mark.parametrize("source", [SOURCE_TV, SOURCE_LINE_IN])
async def test_wake_leaves_tv_and_line_in_alone(source: str) -> None:
    """TV or line-in audio is never interrupted."""
    result, wake_mock = await _press(_speaker(_attr_active_source=source))
    assert result.message == _message("input_active")
    wake_mock.assert_not_awaited()


@pytest.mark.parametrize("state", [PlaybackState.PLAYING, PlaybackState.PAUSED])
async def test_wake_leaves_a_busy_speaker_alone(state: PlaybackState) -> None:
    """A speaker with something playing or paused is left alone."""
    result, wake_mock = await _press(_speaker(_attr_playback_state=state))
    assert result.message == _message("busy")
    wake_mock.assert_not_awaited()


async def test_wake_leaves_a_stream_paused_on_the_speaker_alone() -> None:
    """A pause from the Sonos app shows as idle in MA, but is still a session to keep."""
    speaker = _speaker(_reported_playback_state=PlaybackState.PAUSED)
    result, wake_mock = await _press(speaker)
    assert result.message == _message("busy")
    wake_mock.assert_not_awaited()


async def test_wake_runs_after_music_assistant_stopped_the_speaker() -> None:
    """Sonos also says paused after MA stopped it, which leaves nothing to interrupt."""
    speaker = _speaker(_reported_playback_state=PlaybackState.PAUSED, _stopped_by_ma=True)
    result, wake_mock = await _press(speaker)
    assert result.message == _message("woken")
    wake_mock.assert_awaited_once()


async def test_wake_leaves_a_grouped_speaker_alone() -> None:
    """An AirPlay session would break up a native group."""
    speaker = _speaker()
    speaker.client.player.group_members = [UID, "RINCON_OTHER"]
    result, wake_mock = await _press(speaker)
    assert result.message == _message("busy")
    wake_mock.assert_not_awaited()


async def test_wake_guard_is_asked_again_before_the_session() -> None:
    """The check passed to the wake follows what the speaker does in the meantime."""
    speaker = _speaker()

    async def start_tv_during_wake(
        _air: Any, _host: str, _mac: str, is_blocked: Any
    ) -> WakeOutcome:
        assert is_blocked() is False
        speaker._attr_active_source = SOURCE_TV
        assert is_blocked() is True
        return WakeOutcome.BUSY

    with patch.object(wake, "wake_receiver", start_tv_during_wake):
        result = await SonosPlayer._wake_airplay(speaker)
    assert result.message == _message("input_active")


async def test_wake_reports_a_restart_and_restores_the_volume() -> None:
    """A changed boot counter is reported, with the volume put back."""
    speaker = _speaker(_read_boot_seq=AsyncMock(side_effect=[8, 9]))

    async def change_volume(*_args: Any) -> WakeOutcome:
        speaker._attr_volume_level = 10
        return WakeOutcome.WOKEN

    with patch.object(wake, "wake_receiver", change_volume):
        result = await SonosPlayer._wake_airplay(speaker)
    assert result.message == _message("restarted")
    speaker.volume_set.assert_awaited_once_with(30)


async def test_wake_woke_a_speaker_whose_counter_can_not_be_read() -> None:
    """A UPnP failure after a successful wake is no restart."""
    speaker = _speaker(_read_boot_seq=AsyncMock(side_effect=[8, None, None, None, None]))
    with patch("music_assistant.providers.sonos.player.WAKE_BOOT_CHECK_DELAY", 0):
        result, _ = await _press(speaker)
    assert result.message == _message("woken")


async def test_wake_reports_a_speaker_that_stays_away() -> None:
    """A speaker that neither announces nor answers is reported as not responding."""
    speaker = _speaker(_read_boot_seq=AsyncMock(side_effect=[8, None, None, None, None]))
    with patch("music_assistant.providers.sonos.player.WAKE_BOOT_CHECK_DELAY", 0):
        result, _ = await _press(speaker, WakeOutcome.NOT_ANNOUNCED)
    assert result.message == _message("not_responding")


async def test_wake_restores_a_changed_volume() -> None:
    """The volume the speaker had is put back."""
    speaker = _speaker()

    async def change_volume(*_args: Any) -> WakeOutcome:
        speaker._attr_volume_level = 10
        return WakeOutcome.WOKEN

    with patch.object(wake, "wake_receiver", change_volume):
        await SonosPlayer._wake_airplay(speaker)
    speaker.volume_set.assert_awaited_once_with(30)


async def test_failed_volume_restore_keeps_the_outcome() -> None:
    """A dropped connection during the restore does not replace the result."""
    speaker = _speaker(volume_set=AsyncMock(side_effect=FailedCommand("volume", "failed")))

    async def change_volume(*_args: Any) -> WakeOutcome:
        speaker._attr_volume_level = 10
        return WakeOutcome.WOKEN

    with patch.object(wake, "wake_receiver", change_volume):
        result = await SonosPlayer._wake_airplay(speaker)
    assert result.message == _message("woken")


@pytest.mark.parametrize(
    "outcome",
    [
        WakeOutcome.SESSION_FAILED,
        WakeOutcome.NOT_ANNOUNCED,
        WakeOutcome.NO_RECEIVER,
        WakeOutcome.ALREADY_ANNOUNCED,
    ],
)
async def test_wake_reports_failures(outcome: WakeOutcome) -> None:
    """Each outcome has its own message."""
    result, _ = await _press(_speaker(), outcome)
    assert result.message == _message(outcome.value)


@pytest.mark.parametrize(
    ("protocol_id", "capabilities", "provider", "expected"),
    [
        (UID, [SonosCapability.AIRPLAY], MagicMock(), True),
        ("RINCON_F0F6C193E5DD01500", [SonosCapability.AIRPLAY], MagicMock(), False),
        (UID, [], MagicMock(), False),
        (UID, [SonosCapability.AIRPLAY], None, False),
    ],
)
async def test_button_only_on_a_main_zone_with_airplay(
    protocol_id: str, capabilities: list[SonosCapability], provider: Any, expected: bool
) -> None:
    """The button shows on the speaker itself, when it has AirPlay and the provider runs."""
    speaker = _speaker(protocol_id=protocol_id)
    speaker._attr_device_info.model = "Amp"
    speaker.discovery_info = {"device": {"capabilities": capabilities}}
    speaker.mass.get_provider.return_value = provider
    entries = await SonosPlayer.get_config_entries(speaker)
    buttons = [entry for entry in entries if entry.key == ACTION_WAKE_AIRPLAY]
    assert bool(buttons) is expected
    if buttons:
        assert buttons[0].type == ConfigEntryType.ACTION
        assert buttons[0].action == ACTION_WAKE_AIRPLAY


def test_parse_boot_seq() -> None:
    """The counter of the right speaker is read, from escaped documents as well."""
    document = (
        '<ZoneGroups><ZoneGroup><ZoneGroupMember UUID="RINCON_OTHER" BootSeq="3"/>'
        f'<ZoneGroupMember ZoneName="Patio" UUID="{UID}" BootSeq="8"/>'
        "</ZoneGroup></ZoneGroups>"
    )
    assert parse_boot_seq(document, UID) == 8
    assert parse_boot_seq(document.replace("<", "&lt;").replace(">", "&gt;"), UID) == 8
    assert parse_boot_seq(document, "RINCON_MISSING") is None
    assert parse_boot_seq("", UID) is None
