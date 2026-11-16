"""Persistent HTTP Stream Player provider implementation."""

from __future__ import annotations

import asyncio
from contextlib import aclosing, suppress
from typing import TYPE_CHECKING

from aiohttp import HttpVersion11, web
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, ContentType, PlaybackState
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import (
    CONF_ENABLE_ICY_METADATA,
    CONF_ENTRY_HTTP_PROFILE,
    CONF_HTTP_PROFILE,
    DEFAULT_STREAM_HEADERS,
    ICY_HEADERS,
)
from music_assistant.helpers.audio import calculate_content_length
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    CONF_ENTRY_ENABLE_ICY_METADATA_BASIC,
    CONF_PLAYERS,
    ICY_INTERVAL_BASIC,
    ICY_INTERVAL_FULL,
    PLAYER_ID_PREFIX,
    URL_PATH_PREFIX,
)
from .helpers import format_icy_metadata_frame, slugify_name
from .player import HTTPStreamPlayer

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.config_entries import ProviderConfig

_SUBSCRIBER_QUEUE_SIZE = 128
# Let ffmpeg detect the codec MA sends; the player requests FLAC.
_SOURCE_FORMAT = AudioFormat()
# Fixed PCM format between the decoder of each source and the persistent encoder.
_PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16, channels=2
)
_OUTPUT_FORMAT = AudioFormat(
    content_type=ContentType.MP3, sample_rate=44100, bit_depth=16, channels=2, bit_rate=128
)
# 100 ms of silence in _PCM_FORMAT.
_SILENCE_CHUNK = bytes(44100 * 2 * 2 // 10)
_ICY_NO_CHANGE_FRAME = b"\x00"
# One of the sizes the imageproxy accepts: 0, 80, 160, 256, 512, 1024.
_ICY_IMAGE_SIZE = 512


class _BroadcastHub:
    """Fan one MA source stream out to all listeners without closing on stop/EOF."""

    def __init__(
        self,
        provider: HTTPStreamPlayerProvider,
        player: HTTPStreamPlayer,
    ) -> None:
        self.provider = provider
        self.player = player
        self.subscribers: set[asyncio.Queue[bytes | None]] = set()
        self.task: asyncio.Task[None] | None = None
        # Revision of the last source whose decoder was started.
        self.started_revision: int | None = None
        self._closed = False

    def subscribe(self) -> asyncio.Queue[bytes | None]:
        """Add a listener and start the shared producer on first use."""
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
        self.subscribers.add(queue)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(
                self._run(), name=f"http-stream-{self.player.player_id}"
            )
            if (
                self.player.resolved_stream_url
                and self.started_revision == self.player.stream_revision
                and self.player.playback_state == PlaybackState.PLAYING
            ):
                # The last listener left while the queue kept playing, and the
                # source cannot be fetched again; resume the queue where it is now.
                self.provider.mass.create_task(
                    self.provider.mass.player_queues.resume(self.player.player_id)
                )
        return queue

    def unsubscribe(self, queue: asyncio.Queue[bytes | None]) -> None:
        """Remove a listener; the producer exits once the last listener is gone."""
        self.subscribers.discard(queue)

    async def close(self) -> None:
        """Stop the shared producer."""
        self._closed = True
        self.player.stream_changed.set()
        if self.task and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        self.task = None
        for queue in tuple(self.subscribers):
            # Tell each listener's serving loop to end its response.
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)
        self.subscribers.clear()

    def _broadcast(self, chunk: bytes) -> None:
        """Fan out a chunk without allowing slow clients to stall the producer."""
        for queue in tuple(self.subscribers):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with suppress(asyncio.QueueFull):
                queue.put_nowait(chunk)

    async def _run(self) -> None:
        """Keep one MP3 encoder alive for as long as listeners are connected."""
        encoder = get_ffmpeg_stream(
            audio_input=self._pcm_source(),
            input_format=_PCM_FORMAT,
            output_format=_OUTPUT_FORMAT,
            # get_ffmpeg_stream encodes MP3 at its own default bitrate; the last -b:a wins.
            extra_output_args=[
                "-b:a",
                f"{_OUTPUT_FORMAT.bit_rate}k",
                "-reservoir",
                "0",
                "-flush_packets",
                "1",
            ],
        )
        try:
            async with aclosing(encoder):
                async for chunk in encoder:
                    self._broadcast(chunk)
        except AudioError as err:
            self.provider.logger.warning(
                "HTTP stream encoder of %s stopped: %s", self.player.player_id, err
            )
        finally:
            self.task = None

    async def _pcm_source(self) -> AsyncGenerator[bytes]:
        """Yield the PCM of the current MA source, and real-time silence when there is none."""
        source_revision: int | None = None
        source_finished = False
        if self.started_revision == self.player.stream_revision:
            # A previous run already consumed this source; never replay it.
            source_revision = self.started_revision
            source_finished = True

        while self.subscribers and not self._closed:
            revision = self.player.stream_revision
            # A new play_media/stop invalidates any previous source.
            if source_revision != revision:
                source_revision = revision
                source_finished = False

            url = self.player.resolved_stream_url
            if url and not source_finished:
                async for pcm in self._decode_source(url, revision):
                    yield pcm
                # Ended or replaced; a replacement is picked up by the revision check.
                # Never restart an exhausted URL; after its end only silence follows.
                source_finished = True
                continue

            # No source, or it reached its end: clock real silence into the same encoder.
            self.player.stream_changed.clear()
            yield _SILENCE_CHUNK
            with suppress(TimeoutError):
                await asyncio.wait_for(self.player.stream_changed.wait(), timeout=0.1)

    async def _decode_source(self, url: str, revision: int) -> AsyncGenerator[bytes]:
        """Yield one MA source as PCM until it ends or is replaced."""
        self.started_revision = revision
        decoder = get_ffmpeg_stream(
            audio_input=url,
            input_format=_SOURCE_FORMAT,
            output_format=_PCM_FORMAT,
        )
        first_chunk = True
        try:
            async with aclosing(decoder):
                async for pcm in decoder:
                    if (
                        revision != self.player.stream_revision
                        or not self.subscribers
                        or self._closed
                    ):
                        return
                    if first_chunk:
                        # The queue measures its position against this source's audio.
                        self.player.restart_elapsed_time()
                        first_chunk = False
                    yield pcm
        except AudioError as err:
            self.provider.logger.warning(
                "HTTP stream source of %s failed: %s", self.player.player_id, err
            )
        # The source played to its end (or failed) without being replaced.
        self.player.source_ended(revision)

class HTTPStreamPlayerProvider(PlayerProvider):
    """Create virtual players and expose each one as a persistent HTTP MP3 stream."""

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._hubs: dict[str, _BroadcastHub] = {}

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the config entries of the provider, holding its list of players."""
        return (
            ConfigEntry(
                key=CONF_PLAYERS,
                type=ConfigEntryType.STRING,
                default_value=[],
                required=False,
                multi_value=True,
            ),
        )

    async def loaded_in_mass(self) -> None:
        """Register the stable HTTP route and the configured players."""
        # On the plain-HTTP stream server, which simple web radios can reach without TLS.
        self.mass.streams.register_dynamic_route(
            f"{URL_PATH_PREFIX}*", self._handle_stream_request, "*"
        )
        await self._sync_players()

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """Add and remove players in place when only the list of players changed."""
        if changed_keys - {f"values/{CONF_PLAYERS}"}:
            await super().update_config(config, changed_keys)
            return
        # Skip the reload, so listeners of the other players stay connected.
        self.config = config
        await self._sync_players()

    async def unload(self, is_removed: bool = False) -> None:
        """Unregister routes, producers and players."""
        del is_removed
        self.mass.streams.unregister_dynamic_route(f"{URL_PATH_PREFIX}*", "*")
        for hub in list(self._hubs.values()):
            await hub.close()
        self._hubs.clear()
        for player in list(self.players):
            await self.mass.players.unregister(player.player_id)

    async def discover_players(self) -> None:
        """Create configured virtual players."""
        await self._sync_players()

    async def remove_player(self, player_id: str) -> None:
        """Remove a player and drop its name from the provider config."""
        hub = self._hubs.get(player_id)
        if hub is None:
            await self.mass.players.unregister(player_id, True)
            return
        names = self._player_names()
        new_names = [name for name in names if slugify_name(name) != hub.player.slug]
        if new_names != names:
            self._update_config_value(CONF_PLAYERS, new_names)
        await self._remove_hub(player_id, permanent=True)

    def _player_names(self) -> list[str]:
        """Return the configured player names."""
        value = self.config.get_value(CONF_PLAYERS)
        if not isinstance(value, list):
            return []
        return [str(name).strip() for name in value if str(name).strip()]

    def _migrate_setup_data(self) -> None:
        """Move the names collected by the setup flow into the editable provider config."""
        if not (setup_names := self.config.setup_data.get(CONF_PLAYERS)):
            return
        if not self._player_names():
            self._update_config_value(CONF_PLAYERS, list(setup_names))
        self._update_setup_data(CONF_PLAYERS, [])

    def _rename_players_sharing_a_url(self) -> None:
        """Give a player whose name yields the URL of an earlier one a numbered name."""
        raw_names = self._player_names()
        names: list[str] = []
        taken: set[str] = set()
        for raw_name in raw_names:
            name, number = raw_name, 1
            while (slug := slugify_name(name)) in taken:
                number += 1
                name = f"{raw_name} ({number})"
            if slug:
                taken.add(slug)
            names.append(name)
        if names != raw_names:
            # Stored, so the name in the settings, the player and the URL all agree.
            self._update_config_value(CONF_PLAYERS, names)

    async def _sync_players(self) -> None:
        """Register configured players and unregister those no longer configured."""
        self._migrate_setup_data()
        self._rename_players_sharing_a_url()
        configured: dict[str, str] = {}
        for name in self._player_names():
            if not (slug := slugify_name(name)):
                self.logger.warning("Ignoring invalid HTTP stream player name: %r", name)
                continue
            configured[f"{PLAYER_ID_PREFIX}{slug}"] = name

        for player_id in list(self._hubs):
            if player_id not in configured:
                await self._remove_hub(player_id)

        for player_id, name in configured.items():
            if hub := self._hubs.get(player_id):
                if hub.player.name != name:
                    # Same URL, different spelling: only the shown name changes.
                    hub.player.set_name(name)
                continue
            slug = player_id.removeprefix(PLAYER_ID_PREFIX)
            player = HTTPStreamPlayer(self, player_id, name, slug)
            await self.mass.players.register(player)
            self._hubs[player_id] = _BroadcastHub(self, player)
            self.logger.info(
                "Persistent HTTP stream player '%s' available at %s",
                name,
                player.stream_url,
            )

    async def _remove_hub(self, player_id: str, permanent: bool = False) -> None:
        """Disconnect a player's listeners and unregister the player."""
        if hub := self._hubs.pop(player_id, None):
            await hub.close()
        await self.mass.players.unregister(player_id, permanent)

    def _icy_metadata_frame(self, player: HTTPStreamPlayer, icy_preference: str) -> bytes:
        """Return the ICY metadata frame describing what the player broadcasts now."""
        queue = self.mass.player_queues.get(player.player_id)
        current_item = queue.current_item if queue else None
        if player.playback_state != PlaybackState.PLAYING or current_item is None:
            return format_icy_metadata_frame(player.display_name)
        title = current_item.name or player.display_name
        if current_item.streamdetails and current_item.streamdetails.stream_title:
            title = current_item.streamdetails.stream_title
        image_url = None
        if icy_preference == "full" and current_item.image is not None:
            # Proxied through the plain-HTTP stream server, so local artwork works too
            # and simple devices get a small image without needing TLS.
            image_url = self.mass.metadata.get_image_url(
                current_item.image,
                size=_ICY_IMAGE_SIZE,
                prefer_proxy=True,
                prefer_stream_server=True,
            )
        return format_icy_metadata_frame(title, image_url)

    async def _handle_stream_request(self, request: web.Request) -> web.StreamResponse:
        """Keep the client connection open across track changes, stop and queue end."""
        tail = request.path[len(URL_PATH_PREFIX) :]
        slug = tail.removesuffix(".mp3").strip("/")
        if not slug or "/" in slug:
            raise web.HTTPNotFound(text="Unknown HTTP stream player")

        player_id = f"{PLAYER_ID_PREFIX}{slug}"
        player = self.mass.players.get_player(player_id)
        hub = self._hubs.get(player_id)
        if not isinstance(player, HTTPStreamPlayer) or hub is None:
            raise web.HTTPNotFound(text="Unknown HTTP stream player")

        station_name = " ".join(player.display_name.split())
        headers = {
            **DEFAULT_STREAM_HEADERS,
            **ICY_HEADERS,
            "Content-Type": "audio/mpeg",
            "Cache-Control": "no-cache, no-store",
            "icy-name": station_name,
            "icy-description": f"Music Assistant stream: {station_name}",
            "icy-br": str(_OUTPUT_FORMAT.bit_rate),
            "icy-pub": "0",
        }
        if request.method == "HEAD":
            # Some radios probe the URL before they tune in.
            return web.Response(status=200, headers=headers)

        icy_preference = str(
            self.mass.config.get_raw_player_config_value(
                player_id,
                CONF_ENABLE_ICY_METADATA,
                CONF_ENTRY_ENABLE_ICY_METADATA_BASIC.default_value,
            )
        )
        enable_icy = request.headers.get("Icy-MetaData", "") == "1" and (
            icy_preference != "disabled"
        )
        icy_interval = ICY_INTERVAL_FULL if icy_preference == "full" else ICY_INTERVAL_BASIC
        if enable_icy:
            headers["icy-metaint"] = str(icy_interval)

        response = web.StreamResponse(status=200, headers=headers)
        # Without a Content-Length an HTTP/1.1 listener gets chunked transfer encoding
        # and an HTTP/1.0 one a plain stream until the connection closes. The HTTP
        # profile can force a (very high) Content-Length for clients that need one.
        http_profile = self.mass.config.get_raw_player_config_value(
            player_id, CONF_HTTP_PROFILE, CONF_ENTRY_HTTP_PROFILE.default_value
        )
        if http_profile == "forced_content_length":
            response.content_length = calculate_content_length(_OUTPUT_FORMAT, 12 * 3600)
        elif http_profile == "chunked" and request.version >= HttpVersion11:
            # Chunked transfer encoding does not exist in HTTP/1.0.
            response.enable_chunked_encoding()
        response.force_close()
        # A forced Content-Length runs out at some point on an endless stream.
        bytes_left = response.content_length
        await response.prepare(request)

        subscriber = hub.subscribe()
        self.logger.info("HTTP listener connected to %s (icy=%s)", player_id, enable_icy)
        # Audio bytes left until the next ICY metadata frame is due.
        icy_bytes_left = icy_interval
        last_metadata = b""
        try:
            while True:
                data = await subscriber.get()
                if data is None:
                    # The player was removed or the provider unloaded.
                    break
                while data:
                    chunk = data[:icy_bytes_left] if enable_icy else data
                    data = data[len(chunk) :]
                    frame = b""
                    if enable_icy:
                        icy_bytes_left -= len(chunk)
                        if icy_bytes_left == 0:
                            icy_bytes_left = icy_interval
                            metadata = self._icy_metadata_frame(player, icy_preference)
                            frame = _ICY_NO_CHANGE_FRAME
                            if metadata != last_metadata:
                                frame = last_metadata = metadata
                    out = chunk + frame
                    if bytes_left is not None:
                        out = out[:bytes_left]
                        bytes_left -= len(out)
                    await response.write(out)
                    if bytes_left is not None and bytes_left <= 0:
                        # End the response, so the listener reconnects for a new one.
                        return response
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            hub.unsubscribe(subscriber)
            self.logger.info("HTTP listener disconnected from %s", player_id)

        return response
