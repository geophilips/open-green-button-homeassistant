"""The Open Green Button integration.

Phase 3.3: config entry setup spins up a [GreenButtonCoordinator] that polls the proxy and
appends to HA long-term statistics. The integration owns no sensor entities — its only
output is statistics surfaced via the Energy dashboard.

Two hard invariants enforced here, both about cleanly supporting multiple config entries on
the same utility (sandbox / test account beside a real account, or multi-meter homes):

1. **Statistic IDs are scoped per config entry.** See [statistics.statistic_id_for_series].
   A single user can legitimately have two entries against the same utility, and unscoped
   IDs would alias them in the Energy dashboard.
2. **``async_remove_entry`` purges statistics owned by the removed entry.** HA does NOT
   auto-purge external statistics on config-entry deletion — without this hook, uninstalling
   or re-adding the integration leaves orphaned rows that can only be cleared manually via
   DevTools → Statistics. The test → real account swap relies on this working.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.recorder.statistics import async_list_statistic_ids
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import OpenGbApi
from .const import CONF_SERVER_BASE_URL, DEFAULT_SERVER_BASE_URL, DOMAIN
from .coordinator import GreenButtonCoordinator
from .statistics import statistic_id_prefix_for_entry

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an Open Green Button config entry.

    Creates a coordinator, kicks off a first refresh (which will trigger reauth if the
    persisted refresh token has been revoked while HA was down), and stashes the coordinator
    in ``hass.data`` for diagnostics.
    """
    api = OpenGbApi(
        session=async_get_clientsession(hass),
        server_base_url=entry.data.get(CONF_SERVER_BASE_URL, DEFAULT_SERVER_BASE_URL),
    )
    coordinator = GreenButtonCoordinator(hass, api, entry)

    # First refresh raises ConfigEntryAuthFailed → HA opens a reauth notification. Any other
    # failure becomes ConfigEntryNotReady → HA retries with backoff.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))
    _LOGGER.info(
        "Set up Open Green Button entry %s for utility %s",
        entry.entry_id,
        entry.data.get("utility_id"),
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an Open Green Button config entry."""
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Purge every long-term statistic owned by this entry.

    Without this, uninstalling the integration (or removing one entry of several) leaves
    the rows in ``statistics`` / ``statistics_meta`` indefinitely — they're invisible to
    the integration UI but show up in DevTools → Statistics and pollute the dashboard.
    """
    prefix = statistic_id_prefix_for_entry(entry.entry_id)
    # ``async_list_statistic_ids`` is a ``@callback`` (sync, called from async context) and
    # takes no `statistic_source` kwarg — we list everything the recorder knows about and
    # filter to the stats we own by (a) source == DOMAIN and (b) id starts with our prefix.
    # The source check is the load-bearing one; the prefix check guards against future
    # additions where we might emit multiple sources from one integration.
    all_ids = async_list_statistic_ids(hass)
    owned = [
        item["statistic_id"]
        for item in all_ids
        if item.get("source") == DOMAIN and item["statistic_id"].startswith(prefix)
    ]
    if not owned:
        return

    # async_clear_statistics is recorder-internal; import lazily so the recorder import
    # cost lands only when removing an entry.
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import async_clear_statistics

    _LOGGER.info(
        "Purging %d statistic(s) for removed entry %s: %s",
        len(owned),
        entry.entry_id,
        owned,
    )
    await get_instance(hass).async_add_executor_job(async_clear_statistics, hass, owned)


async def _async_reload_on_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its data changes (e.g. after a reauth or options update)."""
    await hass.config_entries.async_reload(entry.entry_id)
