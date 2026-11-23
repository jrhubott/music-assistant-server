"""Readers for the Hue Lights Sync playback settings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .constants import (
    CONF_BRIGHTNESS,
    CONF_COLOR_MODE,
    CONF_HUE_LATENCY_MS,
    CONF_PARTY_FULL_WHITE,
    CONF_PARTY_LIGHT_RANGE,
    DEFAULT_BRIGHTNESS,
    DEFAULT_COLOR_MODE,
    DEFAULT_HUE_LATENCY_MS,
    DEFAULT_PARTY_FULL_WHITE,
    DEFAULT_PARTY_LIGHT_RANGE,
    PARTY_LIGHT_RANGES,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig


def get_color_mode(config: ProviderConfig) -> str:
    """
    Return the configured visualization mode.

    :param config: Provider config to read the setting from.
    """
    value = config.get_value(CONF_COLOR_MODE)
    return DEFAULT_COLOR_MODE if value is None else str(value)


def get_brightness(config: ProviderConfig) -> int:
    """
    Return the configured brightness percentage.

    :param config: Provider config to read the setting from.
    """
    value = config.get_value(CONF_BRIGHTNESS)
    # stored numbers can come back as float or str, so normalize before truncating
    return DEFAULT_BRIGHTNESS if value is None else int(float(str(value)))


def get_hue_latency_ms(config: ProviderConfig) -> int:
    """
    Return the configured latency compensation in milliseconds.

    :param config: Provider config to read the setting from.
    """
    value = config.get_value(CONF_HUE_LATENCY_MS)
    return DEFAULT_HUE_LATENCY_MS if value is None else int(float(str(value)))


def get_party_light_range(config: ProviderConfig) -> str:
    """
    Return how bright Party Auto runs.

    :param config: Provider config to read the setting from.
    """
    value = str(config.get_value(CONF_PARTY_LIGHT_RANGE))
    return value if value in PARTY_LIGHT_RANGES else DEFAULT_PARTY_LIGHT_RANGE


def get_party_full_white(config: ProviderConfig) -> bool:
    """
    Return whether Party Auto's white flashes and blinders run at full power.

    :param config: Provider config to read the setting from.
    """
    value = config.get_value(CONF_PARTY_FULL_WHITE)
    return DEFAULT_PARTY_FULL_WHITE if value is None else str(value).lower() == "true"
