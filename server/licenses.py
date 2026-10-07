"""Licence sidecar files shown in the viewer's status bar.

An image — or a mosaic page — may ship a small ``.LICENSE`` text file beside
it; the viewer shows its contents under the file size in the status bar. Only
the text is read here; escaping and turning URLs into links happen in the
client (so no markup from the file is ever trusted).
"""
from __future__ import annotations

from pathlib import Path

#: Max bytes read from a licence file. The text is short; a larger file is
#: truncated rather than read whole on every info request.
LICENSE_MAX_BYTES = 8192


def read_license_text(path: Path) -> str | None:
    """Return the stripped UTF-8 text of a licence file, or ``None``.

    Undecodable bytes are replaced and the read is capped at
    :data:`LICENSE_MAX_BYTES`. An absent, empty, or unreadable file yields
    ``None`` so callers simply report no licence.

    Args:
        path: The licence file to read.

    Returns:
        The stripped licence text, or ``None``.
    """
    try:
        if not path.is_file():
            return None
        text = path.read_bytes()[:LICENSE_MAX_BYTES].decode("utf-8", "replace").strip()
    except OSError:
        return None
    return text or None


def image_license(image_path: Path) -> str | None:
    """Return an archive image's licence: ``<filename>.LICENSE`` then ``<stem>.LICENSE``.

    Args:
        image_path: The full-size image file.

    Returns:
        The licence text, or ``None`` when neither sidecar exists.
    """
    for candidate in (
        image_path.with_name(image_path.name + ".LICENSE"),
        image_path.with_suffix(".LICENSE"),
    ):
        text = read_license_text(candidate)
        if text:
            return text
    return None
