"""Virtual HTTP Stream Player."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, PlaybackState, PlayerFeature
from music_assistant_models.player import DeviceInfo, PlayerSource

from music_assistant.constants import CONF_ENTRY_HTTP_PROFILE, CONF_ENTRY_OUTPUT_CODEC_ENFORCE_FLAC
from music_assistant.models.player import Player, PlayerMedia

from .constants import (
    CONF_ENTRY_ENABLE_ICY_METADATA_BASIC,
    CONF_STREAM_URL_LABEL,
    URL_PATH_PREFIX,
)

if TYPE_CHECKING:
    from .provider import HTTPStreamPlayerProvider


class HTTPStreamPlayer(Player):
    """Virtual player whose queue is exposed as a persistent HTTP MP3 stream."""

    def __init__(
        self,
        provider: HTTPStreamPlayerProvider,
        player_id: str,
        name: str,
        slug: str,
    ) -> None:
        super().__init__(provider, player_id)
        self.slug = slug
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            model="HTTP Stream Player",
            manufacturer="Music Assistant",
        )
        self._attr_supported_features = {PlayerFeature.PLAY_MEDIA}
        self._attr_powered = True
        self._attr_playback_state = PlaybackState.IDLE
        self._resolved_stream_url: str | None = None
        self._stream_revision = 0
        # Flow session of the queue that the current source belongs to.
        self._session_id: str | None = None
        self.stream_changed = asyncio.Event()

    @property
    def needs_poll(self) -> bool:
        """This virtual player is updated directly."""
        return False

    @property
    def stream_url(self) -> str:
        """Return the public URL listeners tune into."""
        base_url = self.provider.mass.streams.base_url
        return f"{base_url}{URL_PATH_PREFIX}{self.slug}.mp3"

    @property
    def source_list(self) -> list[PlayerSource]:
        """Return the Music Assistant queue as the only source of this player."""
        # Seek and pause mean nothing on a live broadcast, so the UI shows stop
        # instead. A player that is not playing can be started.
        queue = self.provider.mass.player_queues.get(self.player_id)
        queue_has_items = bool(queue and queue.items)
        return [
            PlayerSource(
                id=self.player_id,
                name="Music Assistant Queue",
                passive=False,
                can_play_pause=(
                    self._attr_playback_state != PlaybackState.PLAYING and queue_has_items
                ),
                can_seek=False,
                can_next_previous=queue_has_items and not (queue and queue.ended),
            )
        ]

    @property
    def resolved_stream_url(self) -> str | None:
        """Return the current Music Assistant source stream URL."""
        return self._resolved_stream_url

    @property
    def stream_revision(self) -> int:
        """Return a counter that changes whenever the source stream changes."""
        return self._stream_revision

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return the player-level config entries."""
        return [
            # The provider decodes the source and re-encodes it to MP3 for the listeners,
            # so request lossless audio to avoid a second lossy encode.
            CONF_ENTRY_OUTPUT_CODEC_ENFORCE_FLAC,
            ConfigEntry(
                key=CONF_STREAM_URL_LABEL,
                type=ConfigEntryType.LABEL,
                translation_params=[self.stream_url],
            ),
            CONF_ENTRY_ENABLE_ICY_METADATA_BASIC,
            CONF_ENTRY_HTTP_PROFILE,
        ]

    async def play(self) -> None:
        """Mark the virtual player as playing when a source is available."""
        if self._resolved_stream_url:
            self._attr_playback_state = PlaybackState.PLAYING
            self.update_state()

    async def stop(self) -> None:
        """Stop MA playback while keeping connected HTTP listeners alive on silence."""
        self._set_source(None)
        self._attr_active_source = None
        self.mark_idle()

    async def play_media(self, media: PlayerMedia) -> None:
        """Resolve the MA source URL and switch the persistent broadcast to it."""
        url = await self.provider.mass.streams.resolve_stream_url(self.player_id, media)
        self.provider.logger.info("HTTP stream source %s -> %s", self.player_id, url)
        queue_data = self.provider.mass.player_queues.queue_data_or_none(self.player_id)
        self._session_id = queue_data.session_id if queue_data else None
        self._set_source(url)
        self._attr_current_media = media
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    def set_name(self, name: str) -> None:
        """Change the name of the player, keeping its URL."""
        self._attr_name = name
        self.update_state()

    def restart_elapsed_time(self) -> None:
        """Restart the elapsed time, called when a source starts sending audio."""
        if self._attr_playback_state != PlaybackState.PLAYING:
            return
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    def mark_idle(self) -> None:
        """Report the player as idle, with nothing being broadcast."""
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self._attr_elapsed_time = None
        self._attr_elapsed_time_last_updated = None
        self.update_state()

    def source_ended(self, revision: int) -> None:
        """Handle the end of a source that played to its end without being replaced."""
        if revision != self._stream_revision:
            return
        # The queue ran out, or core ended the flow early (a format change or a
        # live item) and plays the next item once the player reports idle.
        exhausted = self._session_id is not None and (
            self.provider.mass.player_queues.flow_queue_exhausted(
                self.player_id, self._session_id
            )
        )
        if exhausted:
            # A listener that reconnects must not start the queue over.
            self._set_source(None)
        self.mark_idle()

    def _set_source(self, url: str | None) -> None:
        """Atomically replace the active source and wake the broadcast producer."""
        self._resolved_stream_url = url
        self._stream_revision += 1
        self.stream_changed.set()
