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
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.core import CoreState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval

from .api import OpenGbApi
from .const import (
    ATTR_ACTIVE_PERIOD_START,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_CURRENCY_ALPHA,
    ATTR_PREDICTED_DAYS,
    ATTR_RESIDUAL_RATE,
    ATTR_TIER_ONE_KWH_PER_DAY,
    ATTR_TIER_ONE_RATE,
    ATTR_TIER_TWO_RATE,
    ATTR_USAGE_POINT_ID,
    CONF_DAILY_POLL_TIME,
    CONF_DAILY_POLL_TIME_ENABLED,
    CONF_LAST_FETCHED_AT,
    CONF_SERVER_BASE_URL,
    CONF_UTILITY_NAME,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SERVER_BASE_URL,
    DOMAIN,
    SERVICE_REBUILD_STATISTICS,
    SERVICE_SET_TIER_COST_ESTIMATE,
)
from .coordinator import GreenButtonCoordinator
from .diagnostics import async_remove_xml_cache
from .statistics import async_clear_statistics_for_entry, async_seed_tiered_estimate

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, ServiceCall

_REBUILD_STATISTICS_SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string})
_SET_TIER_COST_ESTIMATE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_USAGE_POINT_ID): cv.string,
        vol.Required(ATTR_ACTIVE_PERIOD_START): cv.string,
        vol.Required(ATTR_PREDICTED_DAYS): vol.All(vol.Coerce(float), vol.Range(min=1, max=62)),
        vol.Required(ATTR_CURRENCY_ALPHA): cv.string,
        vol.Required(ATTR_TIER_ONE_RATE): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
        vol.Required(ATTR_TIER_TWO_RATE): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
        vol.Required(ATTR_TIER_ONE_KWH_PER_DAY): vol.All(
            vol.Coerce(float), vol.Range(min=0.1, max=100)
        ),
        vol.Required(ATTR_RESIDUAL_RATE): vol.All(vol.Coerce(float), vol.Range(min=-1, max=1)),
    }
)

_LOGGER = logging.getLogger(__name__)


def _configured_daily_poll_time(
    entry: ConfigEntry,
    poll_interval: timedelta,
) -> time | None:
    """Return an enabled local poll time for an exactly-daily utility cadence.

    The utility-provided cadence remains authoritative. A wall-clock time only changes the
    phase of a one-day schedule; it must never make a six-hour or multi-day utility poll daily.
    Invalid persisted values fall back safely to interval scheduling.
    """
    if poll_interval != DEFAULT_SCAN_INTERVAL or not entry.options.get(
        CONF_DAILY_POLL_TIME_ENABLED, False
    ):
        return None

    raw = entry.options.get(CONF_DAILY_POLL_TIME)
    if not isinstance(raw, str):
        _LOGGER.warning(
            "Ignoring invalid daily poll time for entry %s: %r",
            entry.entry_id,
            raw,
        )
        return None
    try:
        configured = time.fromisoformat(raw)
    except ValueError:
        _LOGGER.warning(
            "Ignoring invalid daily poll time for entry %s: %r",
            entry.entry_id,
            raw,
        )
        return None
    if configured.tzinfo is not None:
        _LOGGER.warning(
            "Ignoring timezone-qualified daily poll time for entry %s: %r",
            entry.entry_id,
            raw,
        )
        return None
    return configured


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an Open Green Button config entry.

    Creates a coordinator and stashes it in ``hass.data`` for diagnostics. A first install or
    manual reload fetches immediately; a normal Home Assistant boot with an existing usage
    frontier uses stored statistics and waits for the configured schedule.
    """
    api = OpenGbApi(
        session=async_get_clientsession(hass),
        server_base_url=entry.data.get(CONF_SERVER_BASE_URL, DEFAULT_SERVER_BASE_URL),
    )
    coordinator = GreenButtonCoordinator(hass, api, entry)

    # When a refresh runs, ConfigEntryAuthFailed opens HA's reauth flow; other failures become
    # ConfigEntryNotReady and are retried with backoff.
    restarting_with_history = (
        hass.state is not CoreState.running and CONF_LAST_FETCHED_AT in entry.data
    )
    if restarting_with_history:
        # Utility data is already persisted in recorder statistics. Re-fetching it on every HA
        # restart is redundant and puts network/recorder work on the startup critical path.
        coordinator.async_set_updated_data(None)
        _LOGGER.info(
            "Skipping startup fetch for entry %s; next refresh follows its polling schedule",
            entry.entry_id,
        )
    else:
        # First install and manual reload remain explicit refresh actions.
        await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # This integration owns no entities, so nothing subscribes to the coordinator. HA's
    # DataUpdateCoordinator only arms its internal poll timer when it has ≥1 listener AND the
    # entry's `pref_disable_polling` is off (update_coordinator._schedule_refresh). Relying on
    # that gated scheduler is fragile for a poll-only integration — a stray "disable polling"
    # system-option silently stops all data updates. So we drive refreshes ourselves. Daily
    # utilities may be anchored to a user-selected local wall-clock time; every other cadence
    # follows the interval supplied by the Open Green Button server. The first fetch already ran
    # above via async_config_entry_first_refresh; these listeners cover every fetch after.
    async def _async_poll(now) -> None:
        _LOGGER.debug("Periodic poll firing for entry %s (scheduled tick %s)", entry.entry_id, now)
        await coordinator.async_refresh()

    poll_interval = coordinator.update_interval or DEFAULT_SCAN_INTERVAL
    daily_poll_time = _configured_daily_poll_time(entry, poll_interval)
    if daily_poll_time is not None:
        entry.async_on_unload(
            async_track_time_change(
                hass,
                _async_poll,
                hour=daily_poll_time.hour,
                minute=daily_poll_time.minute,
                second=daily_poll_time.second,
            )
        )
        schedule_description = f"daily at {daily_poll_time.isoformat()} local time"
    else:
        entry.async_on_unload(async_track_time_interval(hass, _async_poll, poll_interval))
        schedule_description = f"every {poll_interval}"
    # Emitted once, immediately, on every successful setup. If you do NOT see this line in the
    # log right after an HA (full) restart, the running code is stale — the deployed files or the
    # loaded module predate the timer. A config-entry *reload* is not enough; Python caches the
    # module, so only a full HA restart re-imports this file.
    _LOGGER.info("Armed periodic poll for entry %s: %s", entry.entry_id, schedule_description)

    # NOTE: deliberately NO `add_update_listener(...reload...)` here. The coordinator writes
    # bookkeeping (CONF_LAST_FETCHED_AT, rotated credentials) into entry.data on every poll;
    # a blanket reload-on-update listener would tear the entry down and re-set it up on each
    # of those writes. Reauth reloads itself via the config flow's
    # `async_update_reload_and_abort`. Polling options use OptionsFlowWithReload, which reloads
    # once when the user saves without installing a broad update listener here.
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

    async def _handle_set_tier_cost_estimate(call: ServiceCall) -> None:
        entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
        coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
        if coordinator is None:
            raise ServiceValidationError(
                f"No loaded Open Green Button account with config entry id {entry_id!r}"
            )
        if coordinator.data is None:
            raise ServiceValidationError("The account has no usage response to price yet")
        try:
            active_period_start = datetime.fromisoformat(call.data[ATTR_ACTIVE_PERIOD_START])
            await async_seed_tiered_estimate(
                hass,
                coordinator.entry,
                coordinator.data,
                coordinator.entry.data.get(CONF_UTILITY_NAME, "Open Green Button"),
                active_period_start=active_period_start,
                predicted_days=call.data[ATTR_PREDICTED_DAYS],
                currency_alpha=call.data[ATTR_CURRENCY_ALPHA],
                tier_one_rate=call.data[ATTR_TIER_ONE_RATE],
                tier_two_rate=call.data[ATTR_TIER_TWO_RATE],
                tier_one_kwh_per_day=call.data[ATTR_TIER_ONE_KWH_PER_DAY],
                residual_rate=call.data[ATTR_RESIDUAL_RATE],
                usage_point_id=call.data.get(ATTR_USAGE_POINT_ID),
            )
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    if not hass.services.has_service(DOMAIN, SERVICE_REBUILD_STATISTICS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REBUILD_STATISTICS,
            _handle_rebuild_statistics,
            schema=_REBUILD_STATISTICS_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_SET_TIER_COST_ESTIMATE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_TIER_COST_ESTIMATE,
            _handle_set_tier_cost_estimate,
            schema=_SET_TIER_COST_ESTIMATE_SCHEMA,
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
