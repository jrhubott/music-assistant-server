"""Tests for waking the AirPlay announcements of a receiver."""

from __future__ import annotations

import asyncio
import stat
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.providers.airplay import wake
from music_assistant.providers.airplay.provider import AirPlayProvider
from music_assistant.providers.airplay.wake import WakeOutcome, is_same_device, wake_receiver

if TYPE_CHECKING:
    from pathlib import Path

HOST = "192.0.2.10"
MAC = "F0:F6:C1:93:E5:DD"
PROTOCOL_ID = "apf0f6c193e5dd"
PIPE_NAME = f"ma-airplay-wake-{PROTOCOL_ID}"

FAKE_CLIENT = """#!/bin/sh
printf '%s\\n' "$@" > "{args_file}"
while [ "$#" -gt 0 ]; do
  [ "$1" = "--cmdpipe" ] && pipe="$2"
  shift
done
{before_pipe}
exec 3<"$pipe"
echo "[STATUS] connected"
echo "[STATUS] audio buffered_ms=92"
while read -r line <&3; do
  case "$line" in
    ACTION=START) echo "[STATUS] started at_unix_ms=1" ;;
    ACTION=STOP) echo "[STATUS] stopped"; exit 0 ;;
  esac
done
"""


def _not_blocked() -> bool:
    """Report a device that is free."""
    return False


def _provider(*player_lookups: object) -> MagicMock:
    """
    Return an AirPlay provider stand-in.

    :param player_lookups: What each call of get_player returns, the last one repeating.
    """
    provider = MagicMock()
    provider.wake_tasks = {}
    provider.dacp_id = "1A2B3D4EA1B2C3D4"
    lookups = list(player_lookups) or [None]

    def get_player(_protocol_id: str) -> object:
        return lookups.pop(0) if len(lookups) > 1 else lookups[0]

    provider.get_player.side_effect = get_player
    return provider


def _client(tmp_path: Path, before_pipe: str = "") -> tuple[Path, Path]:
    """
    Write a fake AirPlay client.

    :param tmp_path: Directory to write it to.
    :param before_pipe: Shell commands it runs before it opens its command pipe.
    :return: The client and the file it records its arguments in.
    """
    args_file = tmp_path / "args"
    client = tmp_path / "client"
    client.write_text(FAKE_CLIENT.format(args_file=args_file, before_pipe=before_pipe))
    client.chmod(client.stat().st_mode | stat.S_IEXEC)
    return client, args_file


@pytest.mark.parametrize(
    ("device_id", "expected"),
    [
        (MAC, True),
        (MAC.lower(), True),
        ("F0-F6-C1-93-E5-DD", True),
        ("00:11:22:33:44:55", False),
        (None, False),
    ],
)
def test_is_same_device(device_id: object, expected: bool) -> None:
    """Only the device id of this MAC address matches, however it is written."""
    assert is_same_device(device_id, MAC) is expected


async def test_wake_skips_a_receiver_that_announces() -> None:
    """A receiver that is already set up gets no session."""
    provider = _provider(MagicMock())
    with patch.object(wake, "_run_silent_session", AsyncMock()) as session:
        outcome = await wake_receiver(provider, HOST, MAC, _not_blocked)
    assert outcome == WakeOutcome.ALREADY_ANNOUNCED
    session.assert_not_awaited()


async def test_wake_runs_once_at_a_time() -> None:
    """A second press while a wake runs starts no session."""
    provider = _provider()
    running = MagicMock()
    provider.wake_tasks[PROTOCOL_ID] = running
    with patch.object(wake, "_run_silent_session", AsyncMock()) as session:
        outcome = await wake_receiver(provider, HOST, MAC, _not_blocked)
    assert outcome == WakeOutcome.IN_PROGRESS
    session.assert_not_awaited()
    assert provider.wake_tasks[PROTOCOL_ID] is running


@pytest.mark.parametrize("info", [None, {"deviceID": "00:11:22:33:44:55"}, {}])
async def test_wake_refuses_another_device(info: dict[str, str] | None) -> None:
    """No session starts unless /info names the MAC address of this speaker."""
    provider = _provider()
    with (
        patch.object(wake, "_fetch_info", AsyncMock(return_value=info)),
        patch.object(wake, "_run_silent_session", AsyncMock()) as session,
    ):
        assert await wake_receiver(provider, HOST, MAC, _not_blocked) == WakeOutcome.NO_RECEIVER
    session.assert_not_awaited()
    assert not provider.wake_tasks


async def test_wake_rechecks_the_device_right_before_the_session() -> None:
    """Playback that started during the /info request is not interrupted."""
    provider = _provider()
    with (
        patch.object(wake, "_fetch_info", AsyncMock(return_value={"deviceID": MAC})),
        patch.object(wake, "_run_silent_session", AsyncMock()) as session,
    ):
        assert await wake_receiver(provider, HOST, MAC, lambda: True) == WakeOutcome.BUSY
    session.assert_not_awaited()


async def test_wake_stops_when_the_announcement_returned_meanwhile() -> None:
    """A receiver that came back during the /info request gets no session."""
    provider = _provider(None, MagicMock())
    with (
        patch.object(wake, "_fetch_info", AsyncMock(return_value={"deviceID": MAC})),
        patch.object(wake, "_run_silent_session", AsyncMock()) as session,
    ):
        outcome = await wake_receiver(provider, HOST, MAC, _not_blocked)
    assert outcome == WakeOutcome.ALREADY_ANNOUNCED
    session.assert_not_awaited()


async def test_wake_reports_a_refused_session() -> None:
    """A receiver that does not start the session is reported."""
    provider = _provider()
    with (
        patch.object(wake, "_fetch_info", AsyncMock(return_value={"deviceID": MAC})),
        patch.object(wake, "_run_silent_session", AsyncMock(return_value=False)),
    ):
        outcome = await wake_receiver(provider, HOST, MAC, _not_blocked)
    assert outcome == WakeOutcome.SESSION_FAILED
    assert not provider.wake_tasks


async def test_wake_succeeds_when_the_announcement_returns() -> None:
    """The wake is done once mDNS set the receiver up."""
    provider = _provider(None, None, None, MagicMock())
    with (
        patch.object(wake, "_fetch_info", AsyncMock(return_value={"deviceID": MAC})),
        patch.object(wake, "_run_silent_session", AsyncMock(return_value=True)),
    ):
        assert await wake_receiver(provider, HOST, MAC, _not_blocked) == WakeOutcome.WOKEN
    assert not provider.wake_tasks


async def test_wake_reports_a_receiver_that_stays_silent() -> None:
    """A session that brings no announcement is reported."""
    provider = _provider()
    with (
        patch.object(wake, "_fetch_info", AsyncMock(return_value={"deviceID": MAC})),
        patch.object(wake, "_run_silent_session", AsyncMock(return_value=True)),
        patch.object(wake, "_ANNOUNCE_TIMEOUT", 0.01),
    ):
        outcome = await wake_receiver(provider, HOST, MAC, _not_blocked)
    assert outcome == WakeOutcome.NOT_ANNOUNCED
    assert not provider.wake_tasks


async def test_silent_session_never_sets_a_volume(tmp_path: Path) -> None:
    """The session runs start to stop against the client and passes it no volume."""
    client, args_file = _client(tmp_path)
    provider = _provider()
    with (
        patch.object(wake, "get_cli_binary", AsyncMock(return_value=str(client))),
        patch.object(wake, "_SILENCE_SECONDS", 0.05),
        patch("tempfile.gettempdir", return_value=str(tmp_path)),
    ):
        assert await wake._run_silent_session(provider, HOST, PROTOCOL_ID) is True
    args = args_file.read_text().splitlines()
    assert "--volume" not in args
    assert args[-1] == HOST
    assert not (tmp_path / PIPE_NAME).exists()


async def test_silent_session_waits_through_a_slow_handshake(tmp_path: Path) -> None:
    """A client that opens its pipe only after a slow connect still gets its session."""
    client, _ = _client(tmp_path, before_pipe="sleep 0.4")
    provider = _provider()
    with (
        patch.object(wake, "get_cli_binary", AsyncMock(return_value=str(client))),
        patch.object(wake, "_SILENCE_SECONDS", 0.05),
        patch.object(wake, "_CONNECT_TIMEOUT", 5.0),
        patch("tempfile.gettempdir", return_value=str(tmp_path)),
    ):
        assert await wake._run_silent_session(provider, HOST, PROTOCOL_ID) is True


async def test_silent_session_fails_fast_when_the_client_exits(tmp_path: Path) -> None:
    """A client that gives up ends the session without waiting out the timeouts."""
    client, _ = _client(tmp_path, before_pipe="exit 1")
    provider = _provider()
    with (
        patch.object(wake, "get_cli_binary", AsyncMock(return_value=str(client))),
        patch("tempfile.gettempdir", return_value=str(tmp_path)),
    ):
        async with asyncio.timeout(3):
            assert await wake._run_silent_session(provider, HOST, PROTOCOL_ID) is False
    assert not (tmp_path / PIPE_NAME).exists()


async def test_silent_session_survives_a_failed_spawn(tmp_path: Path) -> None:
    """A client that can not start is a failed wake, and leaves no pipe behind."""
    provider = _provider()
    with (
        patch.object(wake, "get_cli_binary", AsyncMock(return_value=str(tmp_path / "missing"))),
        patch("tempfile.gettempdir", return_value=str(tmp_path)),
    ):
        assert await wake._run_silent_session(provider, HOST, PROTOCOL_ID) is False
    assert not (tmp_path / PIPE_NAME).exists()


async def test_silent_session_without_client_fails() -> None:
    """A missing AirPlay client is a failed wake, not an error."""
    provider = _provider()
    with patch.object(wake, "get_cli_binary", AsyncMock(side_effect=RuntimeError("missing"))):
        assert await wake._run_silent_session(provider, HOST, PROTOCOL_ID) is False


async def test_unload_cancels_a_running_wake(tmp_path: Path) -> None:
    """Unloading the provider ends the client, removes its pipe and answers the button."""
    client, _ = _client(tmp_path)
    provider = _provider()
    with (
        patch.object(wake, "_fetch_info", AsyncMock(return_value={"deviceID": MAC})),
        patch.object(wake, "get_cli_binary", AsyncMock(return_value=str(client))),
        patch.object(wake, "_SILENCE_SECONDS", 30),
        patch("tempfile.gettempdir", return_value=str(tmp_path)),
    ):
        press = asyncio.create_task(wake_receiver(provider, HOST, MAC, _not_blocked))
        while not (tmp_path / "args").exists():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
        running: Any = provider.wake_tasks[PROTOCOL_ID]
        unloading = MagicMock(spec=AirPlayProvider)
        unloading.wake_tasks = {PROTOCOL_ID: running}
        unloading.mass = MagicMock()
        unloading.mass.discovery.aiozc.async_unregister_service = AsyncMock()
        unloading._dacp_server = None
        unloading._dacp_info = None
        unloading._ptp_daemon = None
        unloading._ptp_daemon_ready = None
        unloading._ptp_daemon_restart_task = None
        unloading._ptp_daemon_stdout_task = None
        unloading._unsub_timeline_rate = None
        unloading.dashboards = None
        unloading._bridge_manager = None
        async with asyncio.timeout(10):
            await AirPlayProvider.unload(unloading)
            assert await press == WakeOutcome.SESSION_FAILED
    assert running.done()
    assert not provider.wake_tasks
    assert not (tmp_path / PIPE_NAME).exists()
