"""DataUpdateCoordinator that pulls usage from the proxy and writes HA statistics.

This integration has no sensor entities — its only job is to backfill / append to HA's
long-term statistics so the data shows up in the Energy dashboard. The coordinator wraps
that fetch + write into HA's standard refresh/retry/reauth lifecycle.

Behaviour summary:
  - Polls the proxy at ``DEFAULT_SCAN_INTERVAL`` (currently 6h).
  - On ``OpenGbAuthExpiredError`` → raises ``ConfigEntryAuthFailed``, which HA turns into
    a reauth notification + flow.
  - On any other ``OpenGbApiError`` / network error → raises ``UpdateFailed`` (transient).
  - If the proxy returns ``new_credentials``, the new blob + proxy_token are written back
    into the config entry *before* the stats import — that way a failed import doesn't lose
    the rotated token.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import OpenGbApiError, OpenGbAuthExpiredError, UsageResponse
from .const import (
    CONF_ENCRYPTED_REFRESH_BLOB,
    CONF_PROXY_TOKEN,
    CONF_UTILITY_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .statistics import import_usage_statistics

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .api import OpenGbApi

_LOGGER = logging.getLogger(__name__)


class GreenButtonCoordinator(DataUpdateCoordinator[UsageResponse]):
    """Polls /proxy/usage and writes the result to HA statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: OpenGbApi,
        entry: ConfigEntry,
    ) -> None:
        """Build the coordinator bound to an entry."""
        super().__init__(
            hass,
            _LOGGER,
            # Required since HA 2024.10 — without it, async_config_entry_first_refresh() and
            # the reauth-flow plumbing raise ConfigEntryError. See HA dev docs on
            # "passing the ConfigEntry to your DataUpdateCoordinator".
            config_entry=entry,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.api = api
        self.entry = entry

    async def _async_update_data(self) -> UsageResponse:
        """Fetch, persist rotated credentials, then write statistics."""
        try:
            response = await self.api.fetch_usage(
                encrypted_refresh_blob=self.entry.data[CONF_ENCRYPTED_REFRESH_BLOB],
                proxy_token=self.entry.data[CONF_PROXY_TOKEN],
            )
        except OpenGbAuthExpiredError as err:
            # HA turns ConfigEntryAuthFailed into a persistent notification + reauth flow;
            # the user re-authorizes through the existing config flow, which updates the
            # blob/token via _update_existing_entry().
            raise ConfigEntryAuthFailed(str(err)) from err
        except OpenGbApiError as err:
            raise UpdateFailed(str(err)) from err
        except (TimeoutError, ConnectionError) as err:
            raise UpdateFailed(f"network error talking to the proxy: {err}") from err

        # Persist rotated credentials FIRST. If the subsequent stats write fails we still
        # want HA to remember the new token; otherwise the next poll uses the old (now
        # invalid) token and we cascade into a spurious reauth.
        if response.new_credentials is not None:
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={
                    **self.entry.data,
                    CONF_ENCRYPTED_REFRESH_BLOB: response.new_credentials.encrypted_refresh_blob,
                    CONF_PROXY_TOKEN: response.new_credentials.proxy_token,
                },
            )
            _LOGGER.debug(
                "Persisted rotated credentials for entry %s",
                self.entry.entry_id,
            )

        await import_usage_statistics(
            self.hass,
            self.entry,
            response,
            utility_display_name=self.entry.data.get(CONF_UTILITY_NAME, "Open Green Button"),
        )
        return response
