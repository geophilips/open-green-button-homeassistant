"""Filesystem paths for integration-private cache files.

Today: just the raw-XML debug cache written by the coordinator (gated on debug logging)
and read back by [diagnostics]. Anything else we cache in the future goes here too so
the rest of the codebase doesn't have to think about HA's storage conventions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.storage import STORAGE_DIR

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


def xml_cache_path(hass: HomeAssistant, entry_id: str) -> str:
    """Path of the per-entry raw-XML cache file under HA's ``.storage`` tree.

    Lives in ``.storage`` rather than the visible config directory because users shouldn't
    have to navigate file-share addons to grab it — the path is read back into the
    diagnostics download via ``Settings → Devices & Services → Open Green Button → ⋮ →
    Download Diagnostics``. The file may be absent if debug logging has never been on for
    this entry; callers must tolerate that.
    """
    return hass.config.path(STORAGE_DIR, DOMAIN, f"{entry_id}.xml")
