"""Constants for the HTTP Stream Player provider."""

from typing import Final

from music_assistant_models.config_entries import ConfigEntry

from music_assistant.constants import CONF_ENTRY_ENABLE_ICY_METADATA

CONF_PLAYERS: Final = "players"
PLAYER_ID_PREFIX: Final = "httpstream_"
URL_PATH_PREFIX: Final = "/stream/"
# Per-player config key of the read-only stream URL shown in the player settings.
CONF_STREAM_URL_LABEL: Final = "http_stream_url"

# Default to "basic": the stream exists to be tuned into by web radios that show the title.
# The core entry depends on the flow mode setting, which this always-flow player does not show.
CONF_ENTRY_ENABLE_ICY_METADATA_BASIC: Final = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_ENABLE_ICY_METADATA.to_dict(),
        "default_value": "basic",
        "depends_on": None,
        "depends_on_value_not": None,
    }
)
ICY_INTERVAL_BASIC: Final = 16384
ICY_INTERVAL_FULL: Final = 256000
