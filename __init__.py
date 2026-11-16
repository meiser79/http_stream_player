"""HTTP Stream Player provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from .provider import HTTPStreamPlayerProvider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

SUPPORTED_FEATURES = {ProviderFeature.REMOVE_PLAYER}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize the provider instance."""
    return HTTPStreamPlayerProvider(mass, manifest, config, SUPPORTED_FEATURES)
