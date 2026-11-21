"""Helpers for the Sonos (S2) Provider."""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

from music_assistant_models.enums import ChannelLayout
from zeroconf import IPVersion

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo

# (front channels, surrounds, subwoofer) of a set
_CHANNEL_LAYOUTS = {
    (1, False, True): ChannelLayout.LAYOUT_1_1,
    (2, False, False): ChannelLayout.STEREO,
    (2, False, True): ChannelLayout.LAYOUT_2_1,
    (3, False, True): ChannelLayout.LAYOUT_3_1,
    (2, True, False): ChannelLayout.LAYOUT_4_0,
    (2, True, True): ChannelLayout.LAYOUT_4_1,
    (3, True, False): ChannelLayout.LAYOUT_5_0,
    (3, True, True): ChannelLayout.LAYOUT_5_1,
}


def parse_boot_seq(zone_group_state: str, player_id: str) -> int | None:
    """
    Return the boot counter of a speaker from a ZoneGroupState document.

    :param zone_group_state: The ZoneGroupState value, escaped or not.
    :param player_id: The RINCON id of the speaker.
    """
    document = html.unescape(zone_group_state)
    for tag in re.findall(r"<ZoneGroupMember\b[^>]*>", document):
        if f'UUID="{player_id}"' not in tag:
            continue
        match = re.search(r'\bBootSeq="(\d+)"', tag)
        return int(match.group(1)) if match else None
    return None


def get_primary_ip_address(discovery_info: AsyncServiceInfo) -> str | None:
    """Get primary IP address from zeroconf discovery info."""
    for address in discovery_info.ip_addresses_by_version(IPVersion.V4Only):
        if address.is_loopback or address.is_link_local or address.is_unspecified:
            continue
        return str(address)
    # fall back to IPv6 addresses if no usable IPv4 address found
    for address in discovery_info.ip_addresses_by_version(IPVersion.V6Only):
        if address.is_loopback or address.is_link_local or address.is_unspecified:
            continue
        return str(address)
    return None


def get_channel_layout(
    front_channels: int, paired: bool, surrounds: bool, subwoofer: bool
) -> ChannelLayout | None:
    """
    Return the channel layout of a set of Sonos speakers, None when the speakers are no set.

    :param front_channels: The channels of the front: 1 for a speaker, 2 for a stereo pair
        or an Amp, 3 for a soundbar.
    :param paired: Whether the front is a stereo pair of two speakers.
    :param surrounds: Whether surround speakers are bonded to the front.
    :param subwoofer: Whether a subwoofer is bonded to the front.
    """
    if not (paired or surrounds or subwoofer):
        return None
    return _CHANNEL_LAYOUTS.get((front_channels, surrounds, subwoofer), ChannelLayout.UNKNOWN)
