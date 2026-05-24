"""The Open Green Button integration.

Phase 3.0/3.1 scope: config-flow only. async_setup_entry just persists the entry data into
hass.data; the DataUpdateCoordinator and statistics writer arrive in Phase 3.3.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an Open Green Button config entry."""
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"entry": entry}
    # Reload the entry when options change (no options flow yet, but the listener is cheap
    # and avoids forgetting to wire it later).
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))
    _LOGGER.debug(
        "Set up entry %s for utility %s",
        entry.entry_id,
        entry.data.get("utility_id"),
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an Open Green Button config entry."""
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True


async def _async_reload_on_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its data changes (e.g. after a reauth or options update)."""
    await hass.config_entries.async_reload(entry.entry_id)
