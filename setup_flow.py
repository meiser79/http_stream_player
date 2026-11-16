"""Setup flow for the HTTP Stream Player provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from .constants import CONF_PLAYERS
from .helpers import slugify_name

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession
    from music_assistant_models.config_entries import ConfigValueType


async def run_setup(session: SetupSession) -> None:
    """Collect virtual player names and finish provider setup."""
    setup_data = dict(session.context.setup_data)
    errors: dict[str, str] | None = None

    while True:
        current_players = cast("list[str]", setup_data.get(CONF_PLAYERS, []) or [])
        submitted = await session.form(
            [
                ConfigEntry(
                    key=CONF_PLAYERS,
                    type=ConfigEntryType.STRING,
                    value=current_players,
                    default_value=[],
                    required=True,
                    multi_value=True,
                )
            ],
            step_id="players",
            errors=errors,
            last_step=True,
        )

        players = [
            str(name).strip() for name in submitted.get(CONF_PLAYERS, []) if str(name).strip()
        ]
        slugs = [slugify_name(name) for name in players]
        if not players:
            errors = {CONF_PLAYERS: "no_players"}
            continue
        if any(not slug for slug in slugs):
            errors = {CONF_PLAYERS: "invalid_player_name"}
            continue
        if len(slugs) != len(set(slugs)):
            errors = {CONF_PLAYERS: "duplicate_player_url"}
            continue

        setup_data[CONF_PLAYERS] = players
        await session.finish(setup_data)
        return
