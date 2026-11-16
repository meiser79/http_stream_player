"""Helpers for the HTTP Stream Player provider."""

import re
import unicodedata


def slugify_name(value: str) -> str:
    """Return a conservative URL/player-id safe slug without extra dependencies."""
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value


def format_icy_metadata_frame(title: str, image_url: str | None = None) -> bytes:
    """Build a Shoutcast/ICY in-band metadata frame."""
    metadata = f"StreamTitle='{title}';".encode()
    if image_url:
        metadata += f"StreamURL='{image_url}';".encode()
    # the leading length byte counts 16-byte blocks, so a frame holds at most 255 of them
    metadata = metadata[: 255 * 16]
    metadata += b"\x00" * (-len(metadata) % 16)
    return bytes((len(metadata) // 16,)) + metadata
