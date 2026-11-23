"""Wake the AirPlay announcements of a receiver that stopped making them."""

from __future__ import annotations

import asyncio
import os
import plistlib
import tempfile
from contextlib import suppress
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from aiohttp import ClientError, ClientTimeout

from music_assistant.helpers.named_pipe import AsyncNamedPipeWriter
from music_assistant.helpers.util import format_ip_for_url

from .constants import AIRPLAY_DEFAULT_PORT
from .helpers import get_cli_binary, get_protocol_id

if TYPE_CHECKING:
    from collections.abc import Callable

    from .provider import AirPlayProvider

_INFO_TIMEOUT: Final[float] = 3.0
# The client opens its command pipe only once the receiver accepted the connection, so
# this covers pairing and setup as well.
_CONNECT_TIMEOUT: Final[float] = 15.0
_SILENCE_SECONDS: Final[float] = 3.0
_ANNOUNCE_TIMEOUT: Final[float] = 15.0
_EXIT_TIMEOUT: Final[float] = 5.0
_SAMPLE_RATE: Final[int] = 44100
_FRAME_BYTES: Final[int] = 4
_CHUNK_SECONDS: Final[float] = 0.1
_PREBUFFER_SECONDS: Final[float] = 1.0


class WakeOutcome(StrEnum):
    """What a wake of an AirPlay receiver came to."""

    ALREADY_ANNOUNCED = "already_announced"
    BUSY = "busy"
    IN_PROGRESS = "in_progress"
    NO_RECEIVER = "no_receiver"
    SESSION_FAILED = "session_failed"
    NOT_ANNOUNCED = "not_announced"
    WOKEN = "woken"


async def wake_receiver(
    provider: AirPlayProvider, host: str, mac_address: str, is_blocked: Callable[[], bool]
) -> WakeOutcome:
    """
    Open a short silent session on a receiver and wait for its announcements to return.

    :param provider: The AirPlay provider, which cancels the wake when it unloads.
    :param host: Address of the receiver.
    :param mac_address: MAC address of the device the receiver has to belong to.
    :param is_blocked: Tells whether the device is in use, asked right before the session.
    """
    protocol_id = get_protocol_id(mac_address)
    if provider.get_player(protocol_id) is not None:
        return WakeOutcome.ALREADY_ANNOUNCED
    if protocol_id in provider.wake_tasks:
        return WakeOutcome.IN_PROGRESS
    task = asyncio.create_task(_wake(provider, host, mac_address, protocol_id, is_blocked))
    provider.wake_tasks[protocol_id] = task
    try:
        return await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if task.cancelled() and current is not None and not current.cancelling():
            # the provider unloaded, the button that started this is still waiting
            return WakeOutcome.SESSION_FAILED
        raise
    finally:
        provider.wake_tasks.pop(protocol_id, None)


def is_same_device(device_id: Any, mac_address: str) -> bool:
    """
    Return whether the device id of a receiver is this MAC address.

    :param device_id: The deviceID the receiver reported.
    :param mac_address: The MAC address to compare with.
    """
    return isinstance(device_id, str) and _normalize_mac(device_id) == _normalize_mac(mac_address)


async def _wake(
    provider: AirPlayProvider,
    host: str,
    mac_address: str,
    protocol_id: str,
    is_blocked: Callable[[], bool],
) -> WakeOutcome:
    """
    Run the wake of a receiver.

    :param provider: The AirPlay provider.
    :param host: Address of the receiver.
    :param mac_address: MAC address of the device the receiver has to belong to.
    :param protocol_id: Protocol id of the receiver.
    :param is_blocked: Tells whether the device is in use.
    """
    info = await _fetch_info(provider, host)
    if info is None or not is_same_device(info.get("deviceID"), mac_address):
        return WakeOutcome.NO_RECEIVER
    if provider.get_player(protocol_id) is not None:
        return WakeOutcome.ALREADY_ANNOUNCED
    if is_blocked():
        return WakeOutcome.BUSY
    if not await _run_silent_session(provider, host, protocol_id):
        return WakeOutcome.SESSION_FAILED
    try:
        async with asyncio.timeout(_ANNOUNCE_TIMEOUT):
            while provider.get_player(protocol_id) is None:
                await asyncio.sleep(0.5)
    except TimeoutError:
        return WakeOutcome.NOT_ANNOUNCED
    return WakeOutcome.WOKEN


async def _fetch_info(provider: AirPlayProvider, host: str) -> dict[str, Any] | None:
    """
    Return the /info response of a receiver, None when it gives none.

    :param provider: The AirPlay provider.
    :param host: Address of the receiver.
    """
    url = f"http://{format_ip_for_url(host)}:{AIRPLAY_DEFAULT_PORT}/info"
    try:
        async with provider.mass.http_session.get(
            url, timeout=ClientTimeout(total=_INFO_TIMEOUT)
        ) as resp:
            if resp.status != 200:
                return None
            info = plistlib.loads(await resp.read())
    except ClientError, TimeoutError, plistlib.InvalidFileException, ValueError:
        return None
    return info if isinstance(info, dict) else None


async def _run_silent_session(provider: AirPlayProvider, host: str, protocol_id: str) -> bool:
    """
    Stream a few seconds of silence to a receiver, leaving its volume alone.

    :param provider: The AirPlay provider.
    :param host: Address of the receiver.
    :param protocol_id: Protocol id of the receiver.
    :return: Whether the receiver started the session.
    """
    try:
        binary = await get_cli_binary()
    except RuntimeError:
        provider.logger.warning("Can not wake %s: the AirPlay client is missing", host)
        return False
    pipe = AsyncNamedPipeWriter(
        os.path.join(tempfile.gettempdir(), f"ma-airplay-wake-{protocol_id}"), protocol_id
    )
    process: asyncio.subprocess.Process | None = None
    tasks: list[asyncio.Task[Any]] = []
    stop_sent = False

    async def drive_session() -> bool:
        nonlocal stop_sent
        async with asyncio.timeout(_CONNECT_TIMEOUT):
            if not await pipe.wait_for_reader(_CONNECT_TIMEOUT):
                return False
            await buffered.wait()
            # 0 starts at the earliest instant the client can honor
            await pipe.write(b"START_UNIX_MS=0\nACTION=START\n")
            await started.wait()
        await asyncio.sleep(_SILENCE_SECONDS)
        stop_sent = await pipe.write(b"ACTION=STOP\n")
        return True

    buffered = asyncio.Event()
    started = asyncio.Event()
    try:
        await pipe.create()
        # no --volume: the client then sends the receiver no volume at all
        process = await asyncio.create_subprocess_exec(
            binary,
            "--protocol",
            "airplay2",
            "--ap2-native",
            "--timing",
            "ntp",
            "--dacp",
            provider.dacp_id,
            "--cmdpipe",
            pipe.path,
            "--samplerate",
            str(_SAMPLE_RATE),
            "--bitdepth",
            "16",
            "--port",
            str(AIRPLAY_DEFAULT_PORT),
            host,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        session = asyncio.create_task(drive_session())
        exited = asyncio.create_task(process.wait())
        tasks = [
            session,
            exited,
            asyncio.create_task(_watch_status(process, buffered, started)),
            asyncio.create_task(_feed_silence(process)),
        ]
        await asyncio.wait({session, exited}, return_when=asyncio.FIRST_COMPLETED)
        if not session.done() or session.cancelled() or session.exception() is not None:
            return False
        return session.result()
    except OSError as err:
        provider.logger.warning("Can not wake %s: %s", host, err)
        return False
    finally:
        await _shield_cleanup(process, pipe, tasks, stop_sent)


async def _shield_cleanup(
    process: asyncio.subprocess.Process | None,
    pipe: AsyncNamedPipeWriter,
    tasks: list[asyncio.Task[Any]],
    graceful: bool,
) -> None:
    """
    End the client and remove its pipe, also when the wake is being cancelled.

    :param process: The client, None when it never started.
    :param pipe: The command pipe of the client.
    :param tasks: The tasks that follow the client.
    :param graceful: The client was told to stop, so it gets time for its teardown.
    """

    async def clean_up() -> None:
        if process is not None:
            await _end_process(process, graceful)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await pipe.remove()

    cleanup = asyncio.ensure_future(clean_up())
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        await cleanup
        raise


async def _watch_status(
    process: asyncio.subprocess.Process, buffered: asyncio.Event, started: asyncio.Event
) -> None:
    """
    Follow the status lines of the AirPlay client.

    :param process: The running client.
    :param buffered: Set once the client holds audio to start from.
    :param started: Set once the receiver started playing.
    """
    assert process.stdout is not None
    async for raw_line in process.stdout:
        if b"[STATUS] audio" in raw_line:
            buffered.set()
        elif b"[STATUS] started" in raw_line:
            started.set()


async def _feed_silence(process: asyncio.subprocess.Process) -> None:
    """
    Write silence to the client at playback speed until it is cancelled or exits.

    :param process: The running client.
    """
    assert process.stdin is not None
    chunk = bytes(int(_SAMPLE_RATE * _CHUNK_SECONDS) * _FRAME_BYTES)
    with suppress(BrokenPipeError, ConnectionResetError):
        process.stdin.write(chunk * int(_PREBUFFER_SECONDS / _CHUNK_SECONDS))
        await process.stdin.drain()
        while True:
            process.stdin.write(chunk)
            await process.stdin.drain()
            await asyncio.sleep(_CHUNK_SECONDS)


async def _end_process(process: asyncio.subprocess.Process, graceful: bool) -> None:
    """
    Make sure the client is gone.

    :param process: The client.
    :param graceful: Wait for the teardown the client was told to do before ending it.
    """
    if graceful:
        with suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(process.wait()), _EXIT_TIMEOUT)
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(asyncio.shield(process.wait()), _EXIT_TIMEOUT)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()


def _normalize_mac(mac_address: str) -> str:
    """Return a MAC address as upper case hex without separators."""
    return mac_address.replace(":", "").replace("-", "").upper()
