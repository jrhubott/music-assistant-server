"""Tests for the party dashboard TV link."""

from __future__ import annotations

import asyncio
import logging
import pathlib
import threading
from collections.abc import AsyncGenerator
from unittest.mock import MagicMock

import pytest

from music_assistant.controllers.config import ConfigController
from music_assistant.controllers.dashboard.controller import DASHBOARD_VIEWER_USERNAME
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.helpers.guest_access import get_or_create_guest_user
from music_assistant.mass import MusicAssistant
from music_assistant.providers.party import kiosk


@pytest.fixture
async def mass_auth(tmp_path: pathlib.Path) -> AsyncGenerator[MusicAssistant]:
    """
    Create a minimal Music Assistant instance with a real auth manager.

    :param tmp_path: Temporary directory for test data.
    """
    storage_path = tmp_path / "data"
    cache_path = tmp_path / "cache"
    storage_path.mkdir(parents=True)
    cache_path.mkdir(parents=True)
    logging.getLogger("aiosqlite").level = logging.INFO

    mass_instance = MusicAssistant(str(storage_path), str(cache_path))
    mass_instance.loop = asyncio.get_running_loop()
    mass_instance.loop_thread_id = threading.get_ident()
    mass_instance.config = ConfigController(mass_instance)
    await mass_instance.config.setup()
    webserver = WebserverController(mass_instance)
    mass_instance.webserver = webserver
    webserver.config = await mass_instance.config.get_core_config("webserver")
    await webserver.auth.setup()
    try:
        yield mass_instance
    finally:
        await webserver.auth.close()
        await mass_instance.config.close()


async def _exchange(mass: MusicAssistant, code: str) -> str | None:
    return await mass.webserver.auth._exchange_join_code(code)


async def test_no_kiosk_code_before_one_is_created(mass_auth: MusicAssistant) -> None:
    """Without a TV link there is no code, and revoking is a no-op."""
    assert await kiosk.get_kiosk_code(mass_auth) is None
    assert await kiosk.revoke_kiosk_access(mass_auth) is False


async def test_kiosk_code_can_be_exchanged_repeatedly(mass_auth: MusicAssistant) -> None:
    """The TV link code signs in again and again as the dashboard viewer."""
    code = await kiosk.create_kiosk_code(mass_auth)

    assert await kiosk.get_kiosk_code(mass_auth) == code
    for _ in range(3):
        assert await _exchange(mass_auth, code)


async def test_replacing_kiosk_code_invalidates_the_old_one(mass_auth: MusicAssistant) -> None:
    """A new TV link replaces the old one, which no longer signs in."""
    old_code = await kiosk.create_kiosk_code(mass_auth)
    new_code = await kiosk.create_kiosk_code(mass_auth)

    assert new_code != old_code
    assert await kiosk.get_kiosk_code(mass_auth) == new_code
    assert await _exchange(mass_auth, old_code) is None
    assert await _exchange(mass_auth, new_code)


async def test_revoking_kiosk_code_signs_out_its_screens(mass_auth: MusicAssistant) -> None:
    """Revoking the TV link removes the code and the viewer tokens it handed out."""
    auth = mass_auth.webserver.auth
    code = await kiosk.create_kiosk_code(mass_auth)
    token = await _exchange(mass_auth, code)
    assert token
    assert await auth.authenticate_with_token(token)

    assert await kiosk.revoke_kiosk_access(mass_auth) is True

    assert await kiosk.get_kiosk_code(mass_auth) is None
    assert await _exchange(mass_auth, code) is None
    assert await auth.authenticate_with_token(token) is None


async def test_revoking_kiosk_code_keeps_other_viewer_codes(mass_auth: MusicAssistant) -> None:
    """A cast dashboard code of the same viewer account is not the TV link."""
    viewer = await get_or_create_guest_user(mass_auth, DASHBOARD_VIEWER_USERNAME, "Viewer")
    cast_code, _ = await mass_auth.webserver.auth.generate_join_code(viewer, max_uses=1)

    assert await kiosk.get_kiosk_code(mass_auth) is None
    assert await kiosk.revoke_kiosk_access(mass_auth) is False
    assert await _exchange(mass_auth, cast_code)


def test_build_kiosk_url() -> None:
    """The URL opens the party dashboard on the server's own address."""
    mass = MagicMock()
    mass.webserver.base_url = "http://192.168.1.10:8095"

    url = kiosk.build_kiosk_url(mass, "ABCD1234")

    assert url == "http://192.168.1.10:8095?dashboard=ABCD1234&path=%2Fparty&kiosk=1"
