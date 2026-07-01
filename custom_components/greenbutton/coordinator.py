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
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    OpenGbApiError,
    OpenGbAuthExpiredError,
    OpenGbDataPendingError,
    UsageResponse,
)
from .const import (
    BACKGROUND_LOAD_ISSUE_URL,
    CONF_ENCRYPTED_REFRESH_BLOB,
    CONF_INITIAL_HISTORY_SECONDS,
    CONF_LAST_FETCHED_AT,
    CONF_PROXY_TOKEN,
    CONF_UTILITY_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    INITIAL_FETCH_LOOKBACK,
    LAST_FETCHED_OVERLAP,
    PUBLISHED_MAX_LOOKAHEAD,
)
from .statistics import import_usage_statistics
from .storage import xml_cache_path

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .api import OpenGbApi

_LOGGER = logging.getLogger(__package__)


def _write_xml_sync(path: str, data: bytes) -> None:
    """Synchronous file write executed off the event loop via `async_add_executor_job`."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Write to a temp file + atomic rename so a half-written cache from a crash mid-write
    # doesn't fool the diagnostics handler into reading garbage.
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


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
        now = datetime.now(UTC)
        published_min = self._published_min(now)
        published_max = now + PUBLISHED_MAX_LOOKAHEAD

        _LOGGER.info(
            "Fetching usage for entry %s with published-min=%s published-max=%s",
            self.entry.entry_id,
            published_min.isoformat(),
            published_max.isoformat(),
        )

        # Persist the raw upstream XML to disk only when debug logging is enabled on our
        # domain — keeps the integration's normal memory + disk footprint negligible while
        # giving operators a one-toggle path to capture the bytes for diagnostics. The sink
        # is invoked between read and parse inside `fetch_usage`, then the bytes drop out
        # of scope: no in-memory cache.
        raw_xml_sink = self._make_raw_xml_sink_if_debug()

        try:
            response = await self.api.fetch_usage(
                encrypted_refresh_blob=self.entry.data[CONF_ENCRYPTED_REFRESH_BLOB],
                proxy_token=self.entry.data[CONF_PROXY_TOKEN],
                published_min=published_min,
                published_max=published_max,
                raw_xml_sink=raw_xml_sink,
            )
        except OpenGbAuthExpiredError as err:
            # HA turns ConfigEntryAuthFailed into a persistent notification + reauth flow;
            # the user re-authorizes through the existing config flow, which updates the
            # blob/token via _update_existing_entry().
            raise ConfigEntryAuthFailed(str(err)) from err
        except OpenGbDataPendingError as err:
            # The utility is assembling a large dataset out-of-band (ESPI async batch). We
            # don't implement the Notification/BatchList retrieval flow yet, so raise a repair
            # issue linking to the tracking GitHub issue and ask the (rare) affected user to
            # comment — then fail this refresh so the entry shows as failed. Must be caught
            # BEFORE OpenGbApiError, of which it is a subclass.
            self._async_create_background_load_issue()
            raise UpdateFailed(str(err)) from err
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
            _LOGGER.info(
                "Persisted rotated credentials for entry %s",
                self.entry.entry_id,
            )

        total_readings = sum(len(s.readings) for up in response.usage_points for s in up.series)
        _LOGGER.info(
            "Fetched %d usage point(s) with %d total reading(s) for entry %s",
            len(response.usage_points),
            total_readings,
            self.entry.entry_id,
        )

        await import_usage_statistics(
            self.hass,
            self.entry,
            response,
            utility_display_name=self.entry.data.get(CONF_UTILITY_NAME, "Open Green Button"),
        )

        # A successful fetch means we're no longer blocked on an async background load — clear
        # any background-load repair issue raised by a previous poll (no-op if none exists).
        self._async_clear_background_load_issue()

        # Record the success cutoff — read on the next refresh to scope `published-min`.
        # Done LAST so a partial failure (stats write throwing) doesn't advance the cursor
        # and leave a gap; better to re-fetch a window we've already imported (idempotent)
        # than to skip a window we never wrote.
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, CONF_LAST_FETCHED_AT: datetime.now(UTC).isoformat()},
        )
        return response

    @property
    def _background_load_issue_id(self) -> str:
        """Stable repair-issue id for this entry's async background-load condition."""
        return f"background_load_{self.entry.entry_id}"

    def _async_create_background_load_issue(self) -> None:
        """Raise a (non-fixable) repair issue pointing at the background-load tracking issue.

        The issue carries `learn_more_url` so HA renders a "Learn more" link straight to the
        GitHub issue, and the description asks the user to comment with their utility/details.
        We don't implement the async batch flow yet, so this is the user-facing surface for it.
        """
        from homeassistant.helpers import issue_registry as ir

        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._background_load_issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="background_load_unsupported",
            translation_placeholders={
                "utility": self.entry.data.get(CONF_UTILITY_NAME, "your utility"),
            },
            learn_more_url=BACKGROUND_LOAD_ISSUE_URL,
        )

    def _async_clear_background_load_issue(self) -> None:
        """Delete the background-load repair issue for this entry (no-op if absent)."""
        from homeassistant.helpers import issue_registry as ir

        ir.async_delete_issue(self.hass, DOMAIN, self._background_load_issue_id)

    def _make_raw_xml_sink_if_debug(self):
        """Return a sink that writes the raw upstream XML to disk, or None.

        Gating is `_LOGGER.isEnabledFor(DEBUG)` for `custom_components.greenbutton` — the
        same toggle that flips when the user enables debug logging on the integration in
        the UI (Settings → Devices & Services → ⋮ → Enable debug logging). When debug is
        off we return None so [api.fetch_usage] skips the IO entirely.
        """
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return None
        path = xml_cache_path(self.hass, self.entry.entry_id)

        async def sink(data: bytes) -> None:
            await self.hass.async_add_executor_job(_write_xml_sync, path, data)
            _LOGGER.debug("Persisted %d bytes of raw upstream XML to %s", len(data), path)

        return sink

    def _published_min(self, now: datetime) -> datetime:
        """Return the `published_min` value to send on the next /proxy/usage call.

        Always returns a concrete instant — never None. This is a deliberate workaround for
        a quirk in the Green Button Alliance test-lab harness: when `published-min` and
        `published-max` are both absent, it returns the usage feed without any IntervalBlock
        entries (metadata only). Sending an explicit window forces the data path on.

        On the first refresh (no recorded `last_fetched_at`) we look back by the per-utility
        initial-history window the server supplied in the claim response — a bounded initial
        import that keeps the utility's data-collection job and our statistics write
        manageable. Subsequent refreshes ask for the slice published since the last successful
        fetch, minus a small overlap that absorbs clock skew and any late-arriving corrections.
        """
        raw = self.entry.data.get(CONF_LAST_FETCHED_AT)
        if raw is None:
            return now - self._initial_lookback()
        try:
            last_fetched = datetime.fromisoformat(raw)
        except ValueError:
            # Stored value is corrupt — drop back to a full refetch rather than silently
            # losing the historical window.
            _LOGGER.warning(
                "%s in entry data is unparseable (%r); fetching full history",
                CONF_LAST_FETCHED_AT,
                raw,
            )
            return now - self._initial_lookback()
        return last_fetched - LAST_FETCHED_OVERLAP

    def _initial_lookback(self) -> timedelta:
        """How far back to backfill on the first fetch.

        Prefers the per-utility window the server supplied in the claim response (stored as
        `CONF_INITIAL_HISTORY_SECONDS`); falls back to `INITIAL_FETCH_LOOKBACK` only when that
        value is missing or non-positive (entries created before the server exposed it, or a
        self-hosted server that doesn't). The server is the single source of truth.
        """
        secs = self.entry.data.get(CONF_INITIAL_HISTORY_SECONDS)
        if isinstance(secs, (int, float)) and secs > 0:
            return timedelta(seconds=secs)
        return INITIAL_FETCH_LOOKBACK
