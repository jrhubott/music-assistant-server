"""
Party Plugin Provider for Music Assistant.

Provides guest access with a shareable URL, allowing guests
to add songs to the queue with configurable rate limiting.
"""

from __future__ import annotations

import asyncio
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from mashumaro import DataClassDictMixin
from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ProviderConfig,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    MediaType,
    PlaybackState,
    ProviderFeature,
)
from music_assistant_models.errors import (
    InsufficientPermissions,
    InvalidDataError,
    SetupFailedError,
)

from music_assistant.constants import CONF_ENABLED
from music_assistant.controllers.player_queues.helpers import build_queue_item, committed_index
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_token,
    get_current_user,
    has_player_access,
)
from music_assistant.helpers import guest_access
from music_assistant.helpers.config_entries import PLAYBACK_TARGET_TYPES
from music_assistant.helpers.shared_playback import SharedPlaybackMode, SharedPlaybackSession
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# Configuration keys
CONF_ENABLE_GUEST_ACCESS = "enable_guest_access"
CONF_PARTY_MODE = "mode"
CONF_PARTY_PLAYER = "player"
CONF_PARTY_PLAYER_AUTO = "__auto__"
CONF_ENABLE_RATE_LIMITING = "enable_rate_limiting"
# Boost song feature
CONF_ENABLE_BOOST = "enable_boost"
CONF_PARTY_BOOST_LIMIT = "boost_limit"
CONF_PARTY_BOOST_REFILL_MINUTES = "boost_refill_minutes"
# Add song to Queue feature
CONF_ENABLE_ADD_QUEUE = "enable_add_queue"
CONF_PARTY_ADD_QUEUE_LIMIT = "add_queue_limit"
CONF_PARTY_ADD_QUEUE_REFILL_MINUTES = "add_queue_refill_minutes"
# Skip Song feature
CONF_ENABLE_SKIP_SONG = "enable_skip_song"
CONF_PARTY_SKIP_SONG_LIMIT = "skip_song_limit"
CONF_PARTY_SKIP_SONG_REFILL_MINUTES = "skip_song_refill_minutes"
CONF_PARTY_SKIP_VOTES_REQUIRED = "skip_votes_required"
# Badge color configuration
CONF_REQUEST_BADGE_COLOR = "request_badge_color"
CONF_BOOST_BADGE_COLOR = "boost_badge_color"
# Lyrics / Karaoke
CONF_PARTY_KARAOKE_MODE = "karaoke_mode"
CONF_PARTY_HIGHLIGHT_AHEAD = "highlight_ahead"
CONF_PARTY_LYRICS_DELAY = "lyrics_delay"
# Anti burn-in
CONF_ANTI_BURN_IN = "anti_burn_in"
# Custom party settings
CONF_PARTY_NAME = "party_name"
CONF_PARTY_QR_TEXT = "qr_text"
CONF_PARTY_DURATION = "party_duration"
CONF_HIDE_BACK_BUTTON = "hide_back_button"
CONF_SHOW_PROGRESS_BAR = "show_progress_bar"
CONF_PREVENT_DUPLICATE_TRACKS = "prevent_duplicate_tracks"
# Guest identity and interaction
CONF_ENABLE_GUEST_NAMES = "enable_guest_names"
CONF_ENABLE_SHOUT_OUTS = "enable_shout_outs"
CONF_ENABLE_REACTIONS = "enable_reactions"
# Party screen extras
CONF_SHOW_LEADERBOARD = "show_leaderboard"
CONF_SHOW_SKIP_METER = "show_skip_meter"
CONF_SHOW_REQUEST_ANIMATIONS = "show_request_animations"
CONF_SHOW_TIPS = "show_tips"

# Color options for badges (name, hex value)
# Green and Orange are listed first as they are the defaults
BADGE_COLOR_OPTIONS = [
    ("Green", "#2D6A4F"),
    ("Orange", "#B55522"),
    ("Magenta", "#E91E63"),
    ("Pink", "#F06292"),
    ("Purple", "#9C27B0"),
    ("Deep Purple", "#673AB7"),
    ("Indigo", "#3F51B5"),
    ("Cyan", "#00BCD4"),
    ("Teal", "#009688"),
    ("Green", "#4CAF50"),
    ("Lime", "#8BC34A"),
    ("Deep Orange", "#E64A19"),
    ("Amber", "#FFC107"),
    ("Yellow", "#FFEB3B"),
]

# Guest user configuration
PARTY_GUEST_USER = "party_guest"
PARTY_GUEST_DISPLAY_NAME = "Party Guest"

# Extra attribute keys for tracking guest items in the queue
ATTR_PARTY_GUEST = "party_guest"
ATTR_PARTY_BOOSTED = "party_boosted"
ATTR_PARTY_SKIP_VOTES = "party_skip_votes"
# The voters' ids, comma joined, as extra_attributes only hold scalar values
ATTR_PARTY_SKIP_VOTERS = "party_skip_voters"
ATTR_PARTY_GUEST_NAME = "party_guest_name"
ATTR_PARTY_MESSAGE = "party_message"

MAX_GUEST_NAME_LENGTH = 24
MAX_SHOUT_OUT_LENGTH = 80
LEADERBOARD_SIZE = 5
# Reactions a guest can send to the party screen
REACTIONS = ("🔥", "❤️", "😂", "🙌", "🎉", "💃")
# Minimum seconds between two reactions of the same guest
REACTION_MIN_INTERVAL = 0.5

SUPPORTED_FEATURES: set[ProviderFeature] = set()


@dataclass
class GuestStats:
    """What one guest did at the party, for the leaderboard."""

    name: str
    requests: int = 0
    boosts: int = 0


@dataclass
class LeaderboardEntry(DataClassDictMixin):
    """One guest on the party leaderboard."""

    name: str
    requests: int
    boosts: int
    score: int


@dataclass
class PartyConfig(DataClassDictMixin):
    """Configuration data returned to the party guest frontend."""

    # Feature toggles
    enable_rate_limiting: bool
    enable_add_queue: bool
    enable_boost: bool
    enable_skip_song: bool
    # Add to Queue rate limiting
    add_queue_limit: int
    add_queue_refill_minutes: int
    # Boost rate limiting
    boost_limit: int
    boost_refill_minutes: int
    # Skip Song rate limiting
    skip_song_limit: int
    skip_song_refill_minutes: int
    # Number of distinct guests that must vote before the current song is skipped
    skip_votes_required: int
    # UI settings
    karaoke_mode: bool
    highlight_ahead: bool
    # Milliseconds the dashboard lyrics are held back to match speakers that play late
    lyrics_delay: int
    # Badge colors (hex values)
    request_badge_color: str
    boost_badge_color: str
    # Anti burn-in
    anti_burn_in: bool
    # Custom party settings
    party_name: str | None
    qr_text: str | None
    hide_back_button: bool
    show_progress_bar: bool
    prevent_duplicate_tracks: bool
    # Shared playback mode: "venue" (a real player plays out loud) or "remote"
    # (silent disco; every guest device is its own receiver). Drives the listen-in default.
    mode: str
    # Guest identity and interaction
    enable_guest_names: bool = False
    enable_shout_outs: bool = False
    enable_reactions: bool = False
    reactions: list[str] = field(default_factory=list)
    # Party screen extras
    show_leaderboard: bool = False
    show_skip_meter: bool = False
    show_request_animations: bool = False
    show_tips: bool = False


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return PartyPlugin(mass, manifest, config, SUPPORTED_FEATURES)


class PartyPlugin(PluginProvider):
    """Party plugin provider for Music Assistant."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        guest_access_enabled = bool(self.get_config_value(CONF_ENABLE_GUEST_ACCESS, False))

        return (
            ConfigEntry(
                key=CONF_PARTY_MODE,
                type=ConfigEntryType.STRING,
                required=True,
                default_value=SharedPlaybackMode.VENUE.value,
                options=[
                    ConfigValueOption(SharedPlaybackMode.VENUE.value),
                    ConfigValueOption(SharedPlaybackMode.REMOTE.value),
                ],
            ),
            ConfigEntry(
                key=CONF_PARTY_PLAYER,
                type=ConfigEntryType.STRING,
                required=False,
                default_value=CONF_PARTY_PLAYER_AUTO,
                depends_on=CONF_PARTY_MODE,
                depends_on_value=SharedPlaybackMode.VENUE.value,
                options=[
                    ConfigValueOption(CONF_PARTY_PLAYER_AUTO),
                    *[
                        ConfigValueOption(player.player_id, title=player.name)
                        for player in sorted(
                            self.mass.players.all_players(False, False),
                            key=lambda p: p.name.lower(),
                        )
                        if player.type in PLAYBACK_TARGET_TYPES
                    ],
                ],
            ),
            ConfigEntry(
                key=CONF_PARTY_NAME,
                type=ConfigEntryType.STRING,
                default_value="",
                required=False,
            ),
            ConfigEntry(
                key=CONF_PARTY_DURATION,
                type=ConfigEntryType.INTEGER,
                default_value=guest_access.DEFAULT_JOIN_CODE_EXPIRY_HOURS,
                range=(1, 168),
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_ENABLE_GUEST_ACCESS,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                immediate_apply=True,
            ),
            # Guest access disabled state
            ConfigEntry(
                key="guest_disabled_note",
                type=ConfigEntryType.LABEL,
                required=False,
                hidden=guest_access_enabled,
            ),
            # Guest access enabled state
            ConfigEntry(
                key="guest_enabled_note",
                type=ConfigEntryType.ALERT,
                required=False,
                hidden=not guest_access_enabled,
            ),
            ConfigEntry(
                key=CONF_PARTY_QR_TEXT,
                type=ConfigEntryType.STRING,
                default_value="",
                required=False,
                depends_on=CONF_ENABLE_GUEST_ACCESS,
            ),
            ConfigEntry(
                key=CONF_HIDE_BACK_BUTTON,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_SHOW_PROGRESS_BAR,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_PARTY_KARAOKE_MODE,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                category="karaoke",
            ),
            ConfigEntry(
                key=CONF_PARTY_HIGHLIGHT_AHEAD,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                depends_on=CONF_PARTY_KARAOKE_MODE,
                category="karaoke",
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_PARTY_LYRICS_DELAY,
                type=ConfigEntryType.INTEGER,
                default_value=0,
                range=(-5000, 5000),
                depends_on=CONF_PARTY_KARAOKE_MODE,
                category="karaoke",
            ),
            ConfigEntry(
                key=CONF_ANTI_BURN_IN,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                depends_on=CONF_ENABLE_GUEST_ACCESS,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_ENABLE_RATE_LIMITING,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                depends_on=CONF_ENABLE_GUEST_ACCESS,
                advanced=True,
                category="guest_features",
            ),
            # Add to Queue feature
            ConfigEntry(
                key=CONF_ENABLE_ADD_QUEUE,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                depends_on=CONF_ENABLE_GUEST_ACCESS,
                advanced=True,
                category="guest_features",
            ),
            ConfigEntry(
                key=CONF_PREVENT_DUPLICATE_TRACKS,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                depends_on=CONF_ENABLE_ADD_QUEUE,
                advanced=True,
                category="guest_features",
            ),
            ConfigEntry(
                key=CONF_PARTY_ADD_QUEUE_LIMIT,
                type=ConfigEntryType.INTEGER,
                default_value=10,
                depends_on=CONF_ENABLE_ADD_QUEUE,
                range=(1, 50),
                advanced=True,
                category="guest_features",
            ),
            ConfigEntry(
                key=CONF_PARTY_ADD_QUEUE_REFILL_MINUTES,
                type=ConfigEntryType.INTEGER,
                default_value=2,
                depends_on=CONF_ENABLE_ADD_QUEUE,
                range=(1, 60),
                advanced=True,
                category="guest_features",
            ),
            # Boost feature (priority queue jumping)
            ConfigEntry(
                key=CONF_ENABLE_BOOST,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                depends_on=CONF_ENABLE_GUEST_ACCESS,
                advanced=True,
                category="guest_features",
            ),
            ConfigEntry(
                key=CONF_PARTY_BOOST_LIMIT,
                type=ConfigEntryType.INTEGER,
                default_value=1,
                depends_on=CONF_ENABLE_BOOST,
                range=(1, 10),
                advanced=True,
                category="guest_features",
            ),
            ConfigEntry(
                key=CONF_PARTY_BOOST_REFILL_MINUTES,
                type=ConfigEntryType.INTEGER,
                default_value=20,
                depends_on=CONF_ENABLE_BOOST,
                range=(5, 120),
                advanced=True,
                category="guest_features",
            ),
            # Skip Song feature
            ConfigEntry(
                key=CONF_ENABLE_SKIP_SONG,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                depends_on=CONF_ENABLE_GUEST_ACCESS,
                advanced=True,
                category="guest_features",
            ),
            ConfigEntry(
                key=CONF_PARTY_SKIP_SONG_LIMIT,
                type=ConfigEntryType.INTEGER,
                default_value=1,
                depends_on=CONF_ENABLE_SKIP_SONG,
                range=(1, 5),
                advanced=True,
                category="guest_features",
            ),
            ConfigEntry(
                key=CONF_PARTY_SKIP_SONG_REFILL_MINUTES,
                type=ConfigEntryType.INTEGER,
                default_value=60,
                depends_on=CONF_ENABLE_SKIP_SONG,
                range=(15, 180),
                advanced=True,
                category="guest_features",
            ),
            ConfigEntry(
                key=CONF_PARTY_SKIP_VOTES_REQUIRED,
                type=ConfigEntryType.INTEGER,
                default_value=1,
                depends_on=CONF_ENABLE_SKIP_SONG,
                range=(1, 50),
                advanced=True,
                category="guest_features",
            ),
            # Guest identity and interaction
            ConfigEntry(
                key=CONF_ENABLE_GUEST_NAMES,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                depends_on=CONF_ENABLE_GUEST_ACCESS,
                category="guest_features",
            ),
            ConfigEntry(
                key=CONF_ENABLE_SHOUT_OUTS,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                depends_on=CONF_ENABLE_ADD_QUEUE,
                category="guest_features",
            ),
            ConfigEntry(
                key=CONF_ENABLE_REACTIONS,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                depends_on=CONF_ENABLE_GUEST_ACCESS,
                category="guest_features",
            ),
            # Party screen extras
            ConfigEntry(
                key=CONF_SHOW_LEADERBOARD,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                depends_on=CONF_ENABLE_GUEST_NAMES,
                category="party_screen",
            ),
            ConfigEntry(
                key=CONF_SHOW_SKIP_METER,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                depends_on=CONF_ENABLE_SKIP_SONG,
                category="party_screen",
            ),
            ConfigEntry(
                key=CONF_SHOW_REQUEST_ANIMATIONS,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                depends_on=CONF_ENABLE_GUEST_ACCESS,
                category="party_screen",
            ),
            ConfigEntry(
                key=CONF_SHOW_TIPS,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                depends_on=CONF_ENABLE_GUEST_ACCESS,
                category="party_screen",
            ),
            # Badge color configuration
            ConfigEntry(
                key=CONF_REQUEST_BADGE_COLOR,
                type=ConfigEntryType.STRING,
                default_value="#2D6A4F",  # Green
                depends_on=CONF_ENABLE_GUEST_ACCESS,
                options=[
                    ConfigValueOption(value, title=name) for name, value in BADGE_COLOR_OPTIONS
                ],
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_BOOST_BADGE_COLOR,
                type=ConfigEntryType.STRING,
                default_value="#B55522",  # Orange
                depends_on=CONF_ENABLE_GUEST_ACCESS,
                options=[
                    ConfigValueOption(value, title=name) for name, value in BADGE_COLOR_OPTIONS
                ],
                advanced=True,
            ),
        )

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        """Initialize the Party plugin."""
        super().__init__(mass, manifest, config, supported_features)
        self._unregister_handles: list[Callable[[], None]] = []
        self._queue_lock = asyncio.Lock()
        self._session: SharedPlaybackSession | None = None
        self._session_lock = asyncio.Lock()
        # Leaderboard stats per guest, kept for as long as the provider is loaded
        self._guest_stats: dict[str, GuestStats] = {}
        self._last_reaction: dict[str, float] = {}

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        # Register API commands and store unregister handles
        # PROVIDERS_READ (held by guests) lets a guest fetch the join URL to display/share
        # the QR; get_party_url returns None when guest access is disabled, so nothing leaks.
        self._unregister_handles.append(
            self.mass.register_api_command(
                "party/url", self.get_party_url, required_scope=Scope.PROVIDERS_READ
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "party/player", self.get_party_player, required_scope=Scope.PLAYERS_READ
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "party/config", self.get_party_config, required_scope=Scope.PROVIDERS_READ
            )
        )
        # Guest action commands - these are called by the guest frontend
        self._unregister_handles.append(
            self.mass.register_api_command(
                "party/add_to_queue", self.add_to_queue, required_scope=Scope.QUEUES_CONTROL
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "party/boost_queue_item", self.boost_queue_item, required_scope=Scope.QUEUES_CONTROL
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "party/skip", self.skip_current, required_scope=Scope.QUEUES_CONTROL
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "party/react", self.react, required_scope=Scope.QUEUES_CONTROL
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "party/leaderboard", self.get_leaderboard, required_scope=Scope.PROVIDERS_READ
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "party/listen_in", self.listen_in, required_scope=Scope.PLAYERS_CONTROL
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "party/stop_listen_in",
                self.stop_listen_in,
                required_scope=Scope.PLAYERS_CONTROL,
            )
        )
        self._unregister_handles.append(
            self.mass.register_api_command(
                "party/can_listen_in", self.can_listen_in, required_scope=Scope.PLAYERS_CONTROL
            )
        )

    async def unload(self, is_removed: bool = False) -> None:
        """
        Call when the provider is being unloaded.

        :param is_removed: Whether the provider is being removed (vs just reloaded).
        """
        self.logger.debug("Party unload called, is_removed=%s", is_removed)

        # Unregister all API commands
        for unregister in self._unregister_handles:
            unregister()
        self._unregister_handles.clear()

        # Tear down the shared playback session (detaches guest listeners and,
        # in remote mode, removes the virtual player)
        async with self._session_lock:
            if self._session is not None:
                await self._session.close()
                self._session = None

        # Revoke all guest tokens when:
        # 1. The plugin is being removed entirely (is_removed=True)
        # 2. Guest access is disabled in config (provider reload with disabled setting)
        # 3. The plugin itself is disabled (its config is saved as disabled before unload)
        # This ensures guests are immediately disconnected when access is revoked
        # Note: We read the LIVE stored values, which also covers reloads that are triggered
        # outside of a config save. The defaults must match the stored config defaults:
        # only values that differ from their default are persisted, so switching guest access
        # off drops the key entirely.
        guest_access_enabled = self.mass.config.get_raw_provider_config_value(
            self.instance_id, CONF_ENABLE_GUEST_ACCESS, default=False
        )
        if (
            is_removed
            or not guest_access_enabled
            or not self.mass.config.get_raw_provider_config_value(
                self.instance_id, CONF_ENABLED, default=True
            )
        ):
            self.logger.debug("Revoking guest tokens...")
            await self._revoke_guest_tokens()

        await super().unload(is_removed)

    # ==================== Configuration API Commands ====================

    async def get_party_url(self) -> str | None:
        """
        Get the guest access URL for party.

        When an external URL or remote access is configured, returns a URL that works from
        anywhere. Otherwise, returns a local URL that only works on the same network.

        :returns: The guest join URL, or None if guest access is disabled.
        """
        if not self.config.get_value(CONF_ENABLE_GUEST_ACCESS):
            return None

        guest_user = await guest_access.get_or_create_guest_user(
            self.mass, PARTY_GUEST_USER, PARTY_GUEST_DISPLAY_NAME
        )
        code = await guest_access.get_or_create_join_code(
            self.mass,
            guest_user,
            device_name="Party Guest",
            expires_in_hours=cast("int", self.config.get_value(CONF_PARTY_DURATION)),
        )
        return guest_access.build_join_url(self.mass, code)

    async def get_party_player(self) -> str | None:
        """
        Get the configured party player/queue ID.

        In remote mode, returns the queue of the session's virtual player.
        When configured to auto, returns the first active playing queue,
        falling back to any paused queue, then any available queue.

        :returns: The queue ID for party, or None if no player available.
        """
        if self.config.get_value(CONF_PARTY_MODE) == SharedPlaybackMode.REMOTE.value:
            # remote mode plays on the guest session's virtual player, which needs guest access
            if not self.config.get_value(CONF_ENABLE_GUEST_ACCESS):
                return None
            session = await self._get_session()
            return session.queue_id if session else None

        if player_id := self._configured_player_id():
            return player_id

        # Auto-select: prefer playing queue, then paused, then any active queue
        best_queue: str | None = None
        best_priority = -1
        for queue in self.mass.player_queues:
            if not queue.active:
                continue
            if queue.state == PlaybackState.PLAYING:
                return queue.queue_id
            if queue.state == PlaybackState.PAUSED and best_priority < 1:
                best_queue = queue.queue_id
                best_priority = 1
            elif best_priority < 0:
                best_queue = queue.queue_id
                best_priority = 0
        return best_queue

    async def get_party_config(self) -> PartyConfig:
        """
        Get the party configuration for guest rate limiting.

        :returns: PartyConfig with feature toggles, token limits, refill rates, and colors.
        """
        return PartyConfig(
            enable_rate_limiting=cast("bool", self.config.get_value(CONF_ENABLE_RATE_LIMITING)),
            enable_add_queue=cast("bool", self.config.get_value(CONF_ENABLE_ADD_QUEUE)),
            add_queue_limit=cast("int", self.config.get_value(CONF_PARTY_ADD_QUEUE_LIMIT)),
            add_queue_refill_minutes=cast(
                "int", self.config.get_value(CONF_PARTY_ADD_QUEUE_REFILL_MINUTES)
            ),
            enable_boost=cast("bool", self.config.get_value(CONF_ENABLE_BOOST)),
            boost_limit=cast("int", self.config.get_value(CONF_PARTY_BOOST_LIMIT)),
            boost_refill_minutes=cast(
                "int", self.config.get_value(CONF_PARTY_BOOST_REFILL_MINUTES)
            ),
            enable_skip_song=cast("bool", self.config.get_value(CONF_ENABLE_SKIP_SONG)),
            skip_song_limit=cast("int", self.config.get_value(CONF_PARTY_SKIP_SONG_LIMIT)),
            skip_song_refill_minutes=cast(
                "int", self.config.get_value(CONF_PARTY_SKIP_SONG_REFILL_MINUTES)
            ),
            skip_votes_required=cast("int", self.config.get_value(CONF_PARTY_SKIP_VOTES_REQUIRED)),
            karaoke_mode=cast("bool", self.config.get_value(CONF_PARTY_KARAOKE_MODE)),
            highlight_ahead=cast("bool", self.config.get_value(CONF_PARTY_HIGHLIGHT_AHEAD)),
            lyrics_delay=cast("int", self.config.get_value(CONF_PARTY_LYRICS_DELAY)),
            request_badge_color=cast("str", self.config.get_value(CONF_REQUEST_BADGE_COLOR)),
            boost_badge_color=cast("str", self.config.get_value(CONF_BOOST_BADGE_COLOR)),
            anti_burn_in=cast("bool", self.config.get_value(CONF_ANTI_BURN_IN)),
            party_name=cast("str | None", self.config.get_value(CONF_PARTY_NAME)),
            qr_text=cast("str | None", self.config.get_value(CONF_PARTY_QR_TEXT)),
            hide_back_button=cast("bool", self.config.get_value(CONF_HIDE_BACK_BUTTON)),
            show_progress_bar=cast("bool", self.config.get_value(CONF_SHOW_PROGRESS_BAR)),
            prevent_duplicate_tracks=cast(
                "bool", self.config.get_value(CONF_PREVENT_DUPLICATE_TRACKS)
            ),
            mode=cast("str", self.config.get_value(CONF_PARTY_MODE)),
            enable_guest_names=self._guest_names_enabled(),
            enable_shout_outs=self._shout_outs_enabled(),
            enable_reactions=self._reactions_enabled(),
            reactions=list(REACTIONS),
            show_leaderboard=self._guest_names_enabled()
            and bool(self.config.get_value(CONF_SHOW_LEADERBOARD)),
            show_skip_meter=bool(self.config.get_value(CONF_ENABLE_SKIP_SONG))
            and bool(self.config.get_value(CONF_SHOW_SKIP_METER)),
            show_request_animations=bool(self.config.get_value(CONF_SHOW_REQUEST_ANIMATIONS)),
            show_tips=bool(self.config.get_value(CONF_SHOW_TIPS)),
        )

    async def get_leaderboard(self) -> list[LeaderboardEntry]:
        """
        Get the guests that requested and boosted the most songs at this party.

        :returns: The top guests, best first. Empty when guest names are disabled.
        """
        if not self._guest_names_enabled():
            return []
        return self._build_leaderboard()

    # ==================== Guest Action API Commands ====================

    async def react(self, emoji: str) -> dict[str, Any]:
        """
        Send a reaction to the party screen, where it floats up for everyone to see.

        Reactions sent faster than a guest could tap are dropped.

        :param emoji: One of the reactions listed in the party config.
        :returns: Result dict with success status and whether the reaction was shown.
        """
        if not self.config.get_value(CONF_ENABLE_GUEST_ACCESS):
            raise InvalidDataError("Party guest access is disabled")
        if not self._reactions_enabled():
            raise InvalidDataError("Reactions are disabled")
        if emoji not in REACTIONS:
            raise InvalidDataError(f"Unknown reaction: {emoji}")

        voter_id = await self._get_voter_id()
        now = time.monotonic()
        if now - self._last_reaction.get(voter_id, 0.0) < REACTION_MIN_INTERVAL:
            return {"success": True, "shown": False}
        self._last_reaction[voter_id] = now
        self.signal_provider_event({"event": "reaction", "emoji": emoji})
        return {"success": True, "shown": True}

    async def add_to_queue(
        self,
        uri: str,
        boost: bool = False,
        guest_name: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        """
        Add a media item to the party queue.

        This is the primary API for guests to add songs. The provider handles all
        queue logic including priority positioning for guest items.

        :param uri: The URI of the media item to add (e.g., "spotify://track/xxx").
        :param boost: If True, insert at the front of the guest section (play next).
        :param guest_name: Nickname of the guest, shown on the party screen.
            Ignored when guest names are disabled.
        :param message: Short shout-out shown on the party screen while the song plays.
            Ignored when shout-outs are disabled.
        :returns: Result dict with success status and queue position info.
        """
        # Check if guest access is enabled
        if not self.config.get_value(CONF_ENABLE_GUEST_ACCESS):
            raise InvalidDataError("Party guest access is disabled")

        # Check feature toggles
        if boost and not self.config.get_value(CONF_ENABLE_BOOST):
            raise InvalidDataError("Boost feature is disabled")
        if not self.config.get_value(CONF_ENABLE_ADD_QUEUE):
            raise InvalidDataError("Add to queue feature is disabled")

        # Get the party queue
        queue_id = await self.get_party_player()
        if not queue_id:
            raise InvalidDataError("Could not get player queue")

        queue = self.mass.player_queues.get(queue_id)
        if not queue:
            raise InvalidDataError(f"Queue not found: {queue_id}")

        # Handle different scenarios based on queue state and boost mode
        started_playback = False
        extra_attrs: dict[str, Any] = {ATTR_PARTY_GUEST: True}
        if boost:
            extra_attrs[ATTR_PARTY_BOOSTED] = True
        if name := self._guest_name(guest_name):
            extra_attrs[ATTR_PARTY_GUEST_NAME] = name
        shout_out = self._clean_text(message, MAX_SHOUT_OUT_LENGTH)
        if shout_out and self._shout_outs_enabled():
            extra_attrs[ATTR_PARTY_MESSAGE] = shout_out

        if queue.state == PlaybackState.PLAYING:
            # Queue is actively playing — insert into the priority section
            queue_item = await self._add_to_priority_section(queue_id, uri, extra_attrs)
        else:
            # Queue is not playing — resolve the item, insert, and start playback.
            # Hold the lock so concurrent guests don't both start playback.
            async with self._queue_lock:
                if self.config.get_value(
                    CONF_PREVENT_DUPLICATE_TRACKS
                ) and self._queue_contains_uri(self.mass.player_queues.items(queue_id), uri):
                    raise InvalidDataError("This track is already in the queue")
                media_item = await self.mass.music.get_item_by_uri(uri)
                if not media_item or media_item.media_type not in (
                    MediaType.TRACK,
                    MediaType.RADIO,
                ):
                    raise InvalidDataError(f"Cannot add {uri} to queue - not a playable item")
                queue_item = build_queue_item(queue_id, media_item)  # type: ignore[arg-type]
                queue_item.extra_attributes.update(extra_attrs)
                # Insert after current position if queue has one, otherwise at the start
                insert_index = (queue.current_index + 1) if queue.current_index is not None else 0
                await self.mass.player_queues.load(
                    queue_id=queue_id,
                    queue_items=[queue_item],
                    insert_at_index=insert_index,
                    keep_remaining=True,
                    keep_played=True,
                    shuffle=False,
                )
                await self.mass.player_queues.play_index(queue_id, insert_index)
                started_playback = True

        await self._announce_guest_request(queue_item, name, boosted=boost, new_request=True)
        self.logger.info(
            "Guest added to queue: %s (boost=%s, started_playback=%s)",
            uri,
            boost,
            started_playback,
        )

        return {
            "success": True,
            "queue_id": queue_id,
            "boosted": boost,
            "started_playback": started_playback,
        }

    async def boost_queue_item(
        self, queue_item_id: str, guest_name: str | None = None
    ) -> dict[str, Any]:
        """
        Boost an existing queue item by moving it to the boosted section.

        Finds the item in the queue, marks it as boosted, and moves it to the
        end of the boosted priority section (right after the currently playing track).

        :param queue_item_id: The queue_item_id of the item to boost.
        :param guest_name: Nickname of the guest that boosts. It is shown on the party
            screen when the item has no requester yet. Ignored when guest names are disabled.
        :returns: Result dict with success status.
        """
        if not self.config.get_value(CONF_ENABLE_GUEST_ACCESS):
            raise InvalidDataError("Party guest access is disabled")
        if not self.config.get_value(CONF_ENABLE_BOOST):
            raise InvalidDataError("Boost feature is disabled")

        queue_id = await self.get_party_player()
        if not queue_id:
            raise InvalidDataError("Could not get player queue")

        queue = self.mass.player_queues.get(queue_id)
        if not queue:
            raise InvalidDataError(f"Queue not found: {queue_id}")

        started_playback = False
        name = self._guest_name(guest_name)

        async with self._queue_lock:
            queue_items = self.mass.player_queues.items(queue_id)

            # Find the item by queue_item_id
            item_index = None
            for i, item in enumerate(queue_items):
                if item.queue_item_id == queue_item_id:
                    item_index = i
                    break

            if item_index is None:
                raise InvalidDataError(f"Queue item {queue_item_id} not found")

            if queue_items[item_index].extra_attributes.get(ATTR_PARTY_BOOSTED):
                raise InvalidDataError("This item is already boosted")

            if queue.state == PlaybackState.PLAYING:
                # Use the boundary to avoid moving already-buffered items
                boundary_index = committed_index(queue)
                current_index = boundary_index if boundary_index is not None else 0

                if item_index <= current_index:
                    raise InvalidDataError(
                        "Cannot boost an already played or currently playing item"
                    )

                # Find the end of the boosted section and move item there
                insert_index = self._find_section_end(
                    queue_items, current_index, ATTR_PARTY_BOOSTED
                )

                queue_items_copy = queue_items.copy()
                moved_item = queue_items_copy.pop(item_index)
                self._mark_boosted(moved_item, name)

                # Adjust insert index if the item was before the insert position
                if item_index < insert_index:
                    insert_index -= 1

                queue_items_copy.insert(insert_index, moved_item)
                self.mass.player_queues.update_items(queue_id, queue_items_copy)
            else:
                # Queue is not playing — move item to the play position and start playback
                current_index = queue.current_index or 0
                play_index = current_index + 1 if queue.current_index is not None else 0

                queue_items_copy = queue_items.copy()
                moved_item = queue_items_copy.pop(item_index)
                self._mark_boosted(moved_item, name)

                # Adjust play_index if the item was before it
                if item_index < play_index:
                    play_index -= 1

                queue_items_copy.insert(play_index, moved_item)
                self.mass.player_queues.update_items(queue_id, queue_items_copy)
                await self.mass.player_queues.play_index(queue_id, play_index)
                started_playback = True

        await self._announce_guest_request(moved_item, name, boosted=True, new_request=False)
        self.logger.info(
            "Guest boosted queue item: %s (started_playback=%s)",
            queue_item_id,
            started_playback,
        )

        return {
            "success": True,
            "queue_id": queue_id,
            "started_playback": started_playback,
        }

    async def _add_to_priority_section(
        self, queue_id: str, uri: str, extra_attributes: dict[str, Any]
    ) -> QueueItem:
        """
        Add a media item to the end of a priority section in the queue.

        Resolves the media item, creates a QueueItem with the given extra attributes,
        finds the correct insert position, and loads it into the queue.

        :param queue_id: The queue ID to add to.
        :param uri: The URI of the media item to add.
        :param extra_attributes: Attributes to set on the queue item (e.g., guest/boosted flags).
        :returns: The queue item that was added.
        """
        # Resolve the media item from URI
        media_item = await self.mass.music.get_item_by_uri(uri)
        if not media_item or media_item.media_type not in (
            MediaType.TRACK,
            MediaType.RADIO,
        ):
            raise InvalidDataError(f"Cannot add {uri} to queue - not a playable item")

        # Create a QueueItem from the media item
        queue_item = build_queue_item(queue_id, media_item)  # type: ignore[arg-type]
        queue_item.extra_attributes.update(extra_attributes)

        # Determine the attribute to scan for when finding the section boundary.
        # Use the most specific attribute (e.g., ATTR_PARTY_BOOSTED takes
        # priority over ATTR_PARTY_GUEST) so boosted items form their own
        # sub-section at the front of the guest section.
        if ATTR_PARTY_BOOSTED in extra_attributes:
            section_attribute = ATTR_PARTY_BOOSTED
        else:
            section_attribute = ATTR_PARTY_GUEST

        # Hold the lock while reading queue state, calculating the insert index,
        # and loading the item so concurrent guest requests don't interleave.
        async with self._queue_lock:
            queue = self.mass.player_queues.get(queue_id)
            queue_items = self.mass.player_queues.items(queue_id)
            if self.config.get_value(CONF_PREVENT_DUPLICATE_TRACKS) and self._queue_contains_uri(
                queue_items, uri
            ):
                raise InvalidDataError("This track is already in the queue")

            # Use index_in_buffer when playing to avoid inserting before an already-buffered
            # track, which would cause the newly added song to be skipped
            if queue and queue.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
                boundary_index = committed_index(queue)
                current_index = boundary_index if boundary_index is not None else 0
            else:
                current_index = queue.current_index or 0 if queue else 0

            insert_index = self._find_section_end(queue_items, current_index, section_attribute)

            await self.mass.player_queues.load(
                queue_id=queue_id,
                queue_items=[queue_item],
                insert_at_index=insert_index,
                keep_remaining=True,
                keep_played=True,
                shuffle=False,
            )
        return queue_item

    @staticmethod
    def _queue_contains_uri(queue_items: list[QueueItem], uri: str) -> bool:
        """Return whether the queue already contains the given item URI."""
        return any(queue_item.uri == uri for queue_item in queue_items)

    @staticmethod
    def _find_section_end(queue_items: list[QueueItem], current_index: int, attribute: str) -> int:
        """
        Find the index where a priority section ends.

        Scans from current_index + 1 forward to find where consecutive items
        with the given attribute end. Returns the position where a new item
        should be inserted.

        :param queue_items: List of queue items.
        :param current_index: The currently playing index.
        :param attribute: The extra_attributes key that defines the section.
        :returns: The index where new items should be inserted.
        """
        search_start = current_index + 1

        if not queue_items or search_start >= len(queue_items):
            return len(queue_items)

        section_end = search_start
        for i in range(search_start, len(queue_items)):
            if queue_items[i].extra_attributes.get(attribute):
                section_end = i + 1
            else:
                break

        return section_end

    async def skip_current(self, queue_item_id: str | None = None) -> dict[str, Any]:
        """
        Vote to skip the currently playing track.

        The track is skipped once the configured number of distinct guests voted for it.
        A guest voting twice on the same track is not counted again. A vote for a track
        that is no longer playing is not counted.

        :param queue_item_id: The queue item the guest voted on, as shown to the guest.
            When left out, the vote is for the track playing when the vote arrives.
        :returns: Result dict with success status, whether the track was skipped,
            whether this vote was counted, whether the track changed before the vote
            was counted and the current vote tally.
        """
        # Check if guest access and skip are enabled
        if not self.config.get_value(CONF_ENABLE_GUEST_ACCESS):
            raise InvalidDataError("Party guest access is disabled")
        if not self.config.get_value(CONF_ENABLE_SKIP_SONG):
            raise InvalidDataError("Skip song feature is disabled")

        # Get the party queue
        queue_id = await self.get_party_player()
        if not queue_id:
            raise InvalidDataError("Could not get player queue")

        # A vote counts towards skipping, so it needs the same player access as skipping
        current_user = get_current_user()
        if current_user and not has_player_access(
            current_user, queue_id, self.mass.players.get_player(queue_id)
        ):
            msg = f"{current_user.username} does not have access to player {queue_id}"
            raise InsufficientPermissions(msg)

        votes_required = cast("int", self.config.get_value(CONF_PARTY_SKIP_VOTES_REQUIRED))
        # Resolve the voter first: from reading the votes until writing them there is
        # no await, so votes that arrive at the same time cannot interleave
        voter_id = await self._get_voter_id()

        current_item = self._get_playing_item(queue_id)
        if queue_item_id and current_item.queue_item_id != queue_item_id:
            # e.g. a vote that arrives after earlier votes already skipped the track
            self.logger.info(
                "Guest vote to skip not counted, the track changed on queue %s", queue_id
            )
            return {
                "success": True,
                "queue_id": queue_id,
                "skipped": False,
                "vote_counted": False,
                "song_changed": True,
                "votes": 0,
                "votes_required": votes_required,
            }

        # The votes live on the queue item, so they end with it
        voters = self._get_skip_voters(current_item)
        vote_counted = voter_id not in voters
        if vote_counted:
            voters.append(voter_id)
        votes = len(voters)
        skipped = votes >= votes_required

        if skipped:
            # Drop the votes before skipping, so a vote arriving meanwhile starts over
            current_item.extra_attributes.pop(ATTR_PARTY_SKIP_VOTERS, None)
            current_item.extra_attributes.pop(ATTR_PARTY_SKIP_VOTES, None)
            self.mass.player_queues.signal_update(queue_id, items_changed=True)
            await self.mass.player_queues.next(queue_id)
            self.logger.info("Guests skipped current track on queue: %s", queue_id)
        else:
            if vote_counted:
                current_item.extra_attributes[ATTR_PARTY_SKIP_VOTERS] = ",".join(voters)
                current_item.extra_attributes[ATTR_PARTY_SKIP_VOTES] = votes
                self.mass.player_queues.signal_update(queue_id, items_changed=True)
            self.logger.info(
                "Guest voted to skip current track on queue %s (%d/%d)",
                queue_id,
                votes,
                votes_required,
            )

        return {
            "success": True,
            "queue_id": queue_id,
            "skipped": skipped,
            "vote_counted": vote_counted,
            "song_changed": False,
            "votes": votes,
            "votes_required": votes_required,
        }

    async def listen_in(self, web_player_id: str) -> dict[str, Any]:
        """
        Attach a guest's web player to the party audio.

        :param web_player_id: The player_id of the guest's web player.
        :returns: Result dict with success status and the party queue ID.
        :raises InsufficientPermissions: if the current user may not use the web player.
        """
        if not self.config.get_value(CONF_ENABLE_GUEST_ACCESS):
            raise InvalidDataError("Party guest access is disabled")
        self.mass.players.check_player_access(web_player_id)

        # hold the session lock across resolving and joining the session so a
        # guest can never be attached to a session that is being torn down
        async with self._session_lock:
            session = await self._get_or_create_session_locked()
            if not session:
                raise InvalidDataError("Listen-in is not available for this party")
            await session.add_guest_listener(web_player_id)
            queue_id = session.queue_id

        self.logger.info("Guest player %s is now listening in", web_player_id)
        return {
            "success": True,
            "queue_id": queue_id,
        }

    async def stop_listen_in(self, web_player_id: str) -> dict[str, Any]:
        """
        Detach a guest's web player from the party audio.

        :param web_player_id: The player_id of the guest's web player.
        :returns: Result dict with success status.
        :raises InsufficientPermissions: if the current user may not use the web player.
        """
        self.mass.players.check_player_access(web_player_id)
        async with self._session_lock:
            if self._session is not None:
                await self._session.remove_guest_listener(web_player_id)

        self.logger.info("Guest player %s stopped listening in", web_player_id)
        return {"success": True}

    async def can_listen_in(self, web_player_id: str) -> bool:
        """
        Return whether the given guest web player can listen in on the party audio.

        :param web_player_id: The player_id of the guest's web player.
        """
        if not self.config.get_value(CONF_ENABLE_GUEST_ACCESS):
            return False

        async with self._session_lock:
            session = await self._get_or_create_session_locked()
            return session is not None and session.can_listen_in(web_player_id)

    # ==================== Helper Methods ====================

    async def _get_session(self) -> SharedPlaybackSession | None:
        """
        Get the shared playback session for this party, creating it if needed.

        In remote mode the session is backed by a Sendspin virtual player; the
        session is (re)created here when the virtual player does not exist
        (e.g. after a Sendspin provider reload). In venue mode a session only
        exists when a fixed player is configured; with auto player selection
        there is no session (and thus no listen-in support).

        :returns: The session, or None when no session is available.
        """
        async with self._session_lock:
            return await self._get_or_create_session_locked()

    async def _get_or_create_session_locked(self) -> SharedPlaybackSession | None:
        """
        Get or (re)create the shared playback session.

        The caller must hold ``_session_lock``; guest listen-in and session
        teardown share the lock so a session is never created or joined while it
        is being closed.

        :returns: The session, or None when no session is available.
        """
        # drop a stale session whose player no longer exists
        if self._session is not None and (
            self.mass.players.get_player(self._session.player_id) is None
        ):
            await self._session.close()
            self._session = None

        if self._session is not None:
            return self._session

        if self.config.get_value(CONF_PARTY_MODE) == SharedPlaybackMode.REMOTE.value:
            party_name = cast("str | None", self.config.get_value(CONF_PARTY_NAME))
            try:
                self._session = await SharedPlaybackSession.create_remote(
                    self.mass,
                    owner_instance_id=self.instance_id,
                    display_name=party_name or "Party",
                    session_id=self.instance_id,
                )
            except SetupFailedError as err:
                self.logger.warning("Unable to create remote party session: %s", err)
        elif player_id := self._configured_player_id():
            try:
                self._session = await SharedPlaybackSession.create_venue(self.mass, player_id)
            except SetupFailedError as err:
                self.logger.warning("Unable to create venue party session: %s", err)
        return self._session

    async def _get_voter_id(self) -> str:
        """
        Return an identifier for the caller that is unique per guest device.

        All guests share the same guest user, but every guest gets its own token
        when joining, so the token identifies the individual guest.
        """
        if token := get_current_token():
            if token_id := await self.mass.webserver.auth.get_token_id_from_token(token):
                return f"token:{token_id}"
        if user := get_current_user():
            return f"user:{user.user_id}"
        return "anonymous"

    def _get_playing_item(self, queue_id: str) -> QueueItem:
        """
        Return the item that is currently playing on the given queue.

        :param queue_id: The queue ID to check.
        :raises InvalidDataError: When nothing is playing on the queue.
        """
        queue = self.mass.player_queues.get(queue_id)
        if not queue:
            raise InvalidDataError(f"Queue not found: {queue_id}")
        if queue.state != PlaybackState.PLAYING or not queue.current_item:
            raise InvalidDataError("Nothing is currently playing")
        return queue.current_item

    @staticmethod
    def _get_skip_voters(queue_item: QueueItem) -> list[str]:
        """
        Return the voters that voted to skip the given queue item.

        :param queue_item: The queue item to read the votes from.
        """
        voters = queue_item.extra_attributes.get(ATTR_PARTY_SKIP_VOTERS)
        return str(voters).split(",") if voters else []

    def _guest_names_enabled(self) -> bool:
        """Return whether guests can show their nickname on the party screen."""
        return bool(self.config.get_value(CONF_ENABLE_GUEST_ACCESS)) and bool(
            self.config.get_value(CONF_ENABLE_GUEST_NAMES)
        )

    def _guest_name(self, guest_name: str | None) -> str | None:
        """
        Return the nickname to show for a guest, or None when there is none to show.

        :param guest_name: The nickname as the guest entered it.
        """
        name = self._clean_text(guest_name, MAX_GUEST_NAME_LENGTH)
        return name if name and self._guest_names_enabled() else None

    def _shout_outs_enabled(self) -> bool:
        """Return whether guests can attach a shout-out to a request."""
        return (
            bool(self.config.get_value(CONF_ENABLE_GUEST_ACCESS))
            and bool(self.config.get_value(CONF_ENABLE_ADD_QUEUE))
            and bool(self.config.get_value(CONF_ENABLE_SHOUT_OUTS))
        )

    def _reactions_enabled(self) -> bool:
        """Return whether guests can send reactions to the party screen."""
        return bool(self.config.get_value(CONF_ENABLE_GUEST_ACCESS)) and bool(
            self.config.get_value(CONF_ENABLE_REACTIONS)
        )

    @staticmethod
    def _clean_text(value: str | None, max_length: int) -> str | None:
        """
        Return guest-entered text fit for the party screen, or None when nothing is left.

        :param value: The text as the guest entered it.
        :param max_length: The maximum number of characters to keep.
        """
        if not value:
            return None
        # drop control and format characters (newlines, direction overrides) and
        # collapse whitespace, so a guest can't break the layout of the screen
        visible = "".join(
            " " if char.isspace() else char
            for char in str(value)
            if char.isspace() or unicodedata.category(char)[0] != "C"
        )
        cleaned = " ".join(visible.split())[:max_length].strip()
        return cleaned or None

    @staticmethod
    def _mark_boosted(queue_item: QueueItem, guest_name: str | None) -> None:
        """
        Mark a queue item as a boosted guest request.

        :param queue_item: The queue item that is boosted.
        :param guest_name: Nickname of the guest that boosts, used when the item has
            no requester yet.
        """
        queue_item.extra_attributes[ATTR_PARTY_GUEST] = True
        queue_item.extra_attributes[ATTR_PARTY_BOOSTED] = True
        if guest_name and not queue_item.extra_attributes.get(ATTR_PARTY_GUEST_NAME):
            queue_item.extra_attributes[ATTR_PARTY_GUEST_NAME] = guest_name

    async def _announce_guest_request(
        self, queue_item: QueueItem, guest_name: str | None, boosted: bool, new_request: bool
    ) -> None:
        """
        Tell the party screen that a guest requested or boosted a song, and count it.

        :param queue_item: The queue item that was requested or boosted.
        :param guest_name: Nickname of the guest, None when the guest has none.
        :param boosted: Whether the song was boosted.
        :param new_request: Whether the guest added the song, rather than boosting a
            song that was already in the queue.
        """
        self.signal_provider_event(
            {
                "event": "guest_request",
                "queue_item_id": queue_item.queue_item_id,
                "title": queue_item.name,
                "name": guest_name,
                "boosted": boosted,
            }
        )
        await self._record_guest_action(guest_name, requests=int(new_request), boosts=int(boosted))

    async def _record_guest_action(
        self, guest_name: str | None, requests: int = 0, boosts: int = 0
    ) -> None:
        """
        Count a request or boost towards the leaderboard and tell the party screen.

        Guests without a nickname are not on the leaderboard.

        :param guest_name: Nickname of the guest, None when the guest has none.
        :param requests: Number of songs the guest requested.
        :param boosts: Number of songs the guest boosted.
        """
        if not guest_name:
            return
        voter_id = await self._get_voter_id()
        stats = self._guest_stats.setdefault(voter_id, GuestStats(name=guest_name))
        stats.name = guest_name
        stats.requests += requests
        stats.boosts += boosts
        self.signal_provider_event(
            {
                "event": "leaderboard_updated",
                "leaderboard": [entry.to_dict() for entry in self._build_leaderboard()],
            }
        )

    def _build_leaderboard(self) -> list[LeaderboardEntry]:
        """Return the top guests, best first."""
        entries = [
            LeaderboardEntry(
                name=stats.name,
                requests=stats.requests,
                boosts=stats.boosts,
                score=stats.requests + stats.boosts,
            )
            for stats in self._guest_stats.values()
        ]
        entries.sort(key=lambda entry: (-entry.score, -entry.requests, entry.name.lower()))
        return entries[:LEADERBOARD_SIZE]

    async def _revoke_guest_tokens(self) -> None:
        """
        Revoke all guest access tokens and codes for party.

        This is called when guest access or the plugin is disabled, or the plugin is removed.
        We disconnect WebSocket connections to force the frontend to redirect to login,
        revoke tokens so they can't reconnect, and invalidate pending join codes.
        """
        codes_revoked, tokens_revoked = await guest_access.revoke_guest_access(
            self.mass, PARTY_GUEST_USER
        )
        if codes_revoked > 0:
            self.logger.info("Revoked %d pending join codes", codes_revoked)
        if tokens_revoked > 0:
            self.logger.info(
                "Revoked %d guest access tokens for user '%s'",
                tokens_revoked,
                PARTY_GUEST_USER,
            )

    def _configured_player_id(self) -> str | None:
        """Return the id of the configured party player, or None for automatic selection."""
        player_id = self.config.get_value(CONF_PARTY_PLAYER)
        if not player_id or str(player_id) == CONF_PARTY_PLAYER_AUTO:
            return None
        # a stored id can be a legacy id of a player that answers to another id now
        return self.mass.players.resolve_player_id(str(player_id))
