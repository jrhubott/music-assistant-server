"""
TV link for the party dashboard: a sign-in link that does not run out.

The link signs a browser in as the dashboard viewer, the same account a cast dashboard
uses, through a join code that has no use limit and does not expire for ten years. The
viewer token the code is exchanged for still lasts one day; the frontend remembers the
link and exchanges the code again when that token ends, so a TV keeps showing the
dashboard without anyone signing in. Anyone holding the link can view the dashboard
until it is revoked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlencode

from music_assistant.controllers.dashboard.controller import (
    DASHBOARD_VIEWER_DISPLAY_NAME,
    DASHBOARD_VIEWER_USERNAME,
)
from music_assistant.controllers.webserver.helpers.auth_providers import normalize_username
from music_assistant.helpers.datetime import utc
from music_assistant.helpers.guest_access import get_or_create_guest_user

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

KIOSK_DEVICE_NAME = "Party TV Link"
KIOSK_CODE_EXPIRY_HOURS = 24 * 365 * 10
KIOSK_ROUTE = "/party"


async def get_kiosk_code(mass: MusicAssistant) -> str | None:
    """
    Return the active TV link code, if one exists.

    :param mass: MusicAssistant instance.
    """
    auth = mass.webserver.auth
    if not (user := await auth.get_user_by_username(DASHBOARD_VIEWER_USERNAME)):
        return None
    cursor = await auth.database.execute(
        """
        SELECT code FROM join_codes
        WHERE user_id = :user_id
        AND device_name = :device_name
        AND expires_at > :now
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {
            "user_id": user.user_id,
            "device_name": KIOSK_DEVICE_NAME,
            "now": utc().isoformat(),
        },
    )
    row = await cursor.fetchone()
    return str(row["code"]) if row else None


async def create_kiosk_code(mass: MusicAssistant) -> str:
    """
    Replace any existing TV link with a new one and return its code.

    :param mass: MusicAssistant instance.
    """
    await revoke_kiosk_access(mass)
    user = await get_or_create_guest_user(
        mass, DASHBOARD_VIEWER_USERNAME, DASHBOARD_VIEWER_DISPLAY_NAME
    )
    code, _expires_at = await mass.webserver.auth.generate_join_code(
        user,
        expires_in_hours=KIOSK_CODE_EXPIRY_HOURS,
        max_uses=0,
        device_name=KIOSK_DEVICE_NAME,
    )
    return code


async def revoke_kiosk_access(mass: MusicAssistant) -> bool:
    """
    Revoke the TV link and sign out the browsers that use it.

    The dashboard viewer account is shared with cast dashboards, so revoking its tokens
    also ends any dashboard cast at that moment; casting again starts a new one.

    :param mass: MusicAssistant instance.
    :return: True when a TV link existed and was revoked.
    """
    auth = mass.webserver.auth
    # read the row directly: on stable the user lookups return None for a disabled account
    user_row = await auth.database.get_row(
        "users", {"username": normalize_username(DASHBOARD_VIEWER_USERNAME)}
    )
    if not user_row:
        return False
    user_id = user_row["user_id"]
    cursor = await auth.database.execute(
        "DELETE FROM join_codes WHERE user_id = :user_id AND device_name = :device_name",
        {"user_id": user_id, "device_name": KIOSK_DEVICE_NAME},
    )
    await auth.database.commit()
    if not cursor.rowcount:
        return False
    if user := await auth.get_user(user_id):
        await auth.revoke_tokens_for_user(user)
    else:
        await auth.database.execute(
            "DELETE FROM auth_tokens WHERE user_id = :user_id", {"user_id": user_id}
        )
        await auth.database.commit()
    return True


def build_kiosk_url(mass: MusicAssistant, code: str) -> str:
    """
    Build the URL a browser on the local network opens to show the party dashboard.

    :param mass: MusicAssistant instance.
    :param code: The TV link code.
    """
    query = {"dashboard": code, "path": KIOSK_ROUTE, "kiosk": "1"}
    return f"{mass.webserver.base_url}?{urlencode(query)}"
