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

import voluptuous as vol
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .api import OpenGbApi
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    CONF_SERVER_BASE_URL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SERVER_BASE_URL,
    DOMAIN,
    SERVICE_REBUILD_STATISTICS,
)
from .coordinator import GreenButtonCoordinator
from .diagnostics import async_remove_xml_cache
from .statistics import async_clear_statistics_for_entry

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, ServiceCall

_REBUILD_STATISTICS_SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string})

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

    # This integration owns no entities, so nothing subscribes to the coordinator. HA's
    # DataUpdateCoordinator only arms its internal poll timer when it has ≥1 listener AND the
    # entry's `pref_disable_polling` is off (update_coordinator._schedule_refresh). Relying on
    # that gated scheduler is fragile for a poll-only integration — a stray "disable polling"
    # system-option silently stops all data updates. So we drive the periodic refresh
    # ourselves with an unconditional time interval that HA can't turn off. The first fetch
    # already ran above via async_config_entry_first_refresh; this covers every fetch after.
    async def _async_poll(now) -> None:
        _LOGGER.debug(
            "Periodic poll firing for entry %s (scheduled tick %s)", entry.entry_id, now
        )
        await coordinator.async_refresh()

    entry.async_on_unload(async_track_time_interval(hass, _async_poll, DEFAULT_SCAN_INTERVAL))
    # Emitted once, immediately, on every successful setup. If you do NOT see this line in the
    # log right after an HA (full) restart, the running code is stale — the deployed files or the
    # loaded module predate the timer. A config-entry *reload* is not enough; Python caches the
    # module, so only a full HA restart re-imports this file.
    _LOGGER.info(
        "Armed periodic poll for entry %s: every %s", entry.entry_id, DEFAULT_SCAN_INTERVAL
    )

    # NOTE: deliberately NO `add_update_listener(...reload...)` here. The coordinator writes
    # bookkeeping (CONF_LAST_FETCHED_AT, rotated credentials) into entry.data on every poll;
    # a blanket reload-on-update listener would tear the entry down and re-set it up on each
    # of those writes. Reauth reloads itself via the config flow's
    # `async_update_reload_and_abort`, and there is no options flow, so nothing else needs a
    # reload on data change.
    _async_register_services(hass)
    _LOGGER.info(
        "Set up Open Green Button entry %s for utility %s",
        entry.entry_id,
        entry.data.get("utility_id"),
    )
    return True


def _async_register_services(hass: HomeAssistant) -> None:
    """Register integration-wide services once (idempotent across multiple entries).

    Services are global, not per-entry, so the first entry to set up registers them and later
    entries no-op. We deliberately don't deregister on unload: HA services normally persist
    for the integration's lifetime, and the handler already validates targets at call time.
    """
    if hass.services.has_service(DOMAIN, SERVICE_REBUILD_STATISTICS):
        return

    async def _handle_rebuild_statistics(call: ServiceCall) -> None:
        entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
        coordinators: dict[str, GreenButtonCoordinator] = hass.data.get(DOMAIN, {})
        if entry_id is not None:
            coordinator = coordinators.get(entry_id)
            if coordinator is None:
                raise ServiceValidationError(
                    f"No loaded Open Green Button account with config entry id {entry_id!r}"
                )
            targets = [coordinator]
        else:
            targets = list(coordinators.values())
            if not targets:
                raise ServiceValidationError("No loaded Open Green Button accounts to rebuild")
        for coordinator in targets:
            await coordinator.async_rebuild_statistics()

    hass.services.async_register(
        DOMAIN,
        SERVICE_REBUILD_STATISTICS,
        _handle_rebuild_statistics,
        schema=_REBUILD_STATISTICS_SCHEMA,
    )


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
    # Drop any cached debug XML for this entry regardless of whether the entry has stats
    # to clear — happens before the stats purge so a stats-purge exception doesn't strand
    # the file on disk.
    await async_remove_xml_cache(hass, entry.entry_id)

    # Drop any background-load repair issue this entry raised — HA doesn't auto-clear custom
    # integration issues on entry removal, so it would otherwise linger in Settings → Repairs.
    from homeassistant.helpers import issue_registry as ir

    ir.async_delete_issue(hass, DOMAIN, f"background_load_{entry.entry_id}")

    owned = await async_clear_statistics_for_entry(hass, entry.entry_id)
    if owned:
        _LOGGER.info(
            "Purged %d statistic(s) for removed entry %s: %s",
            len(owned),
            entry.entry_id,
            owned,
        )
