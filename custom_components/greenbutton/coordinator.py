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
    NewCredentials,
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
from .statistics import async_clear_statistics_for_entry, import_usage_statistics
from .storage import xml_cache_path

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .api import OpenGbApi

_LOGGER = logging.getLogger(__package__)


def _newest_reading_start(response: UsageResponse) -> datetime | None:
    """Return the latest reading ``start`` across every series, or None if there are none.

    Used to anchor the incremental cursor to the real data frontier (the newest interval we
    actually imported) rather than wall-clock ``now`` — see
    [GreenButtonCoordinator._advance_cursor].
    """
    newest: datetime | None = None
    for up in response.usage_points:
        for series in up.series:
            for reading in series.readings:
                if newest is None or reading.start > newest:
                    newest = reading.start
    return newest


def _parse_iso_or_none(raw: object) -> datetime | None:
    """Parse a stored ISO 8601 timestamp, tolerating None / non-str / corrupt values."""
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


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
        # One-shot flag set by [async_rebuild_statistics] so the next fetch backfills the full
        # initial-history window instead of the incremental slice since `last_fetched_at`.
        # Consumed in [_published_min]; cleared after a successful import.
        self._force_full_history = False

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

        response = await self._fetch(published_min, published_max)

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

        # Advance the incremental cursor. Done LAST so a partial failure (stats write throwing)
        # doesn't move it and leave a gap.
        self._advance_cursor(response)
        # A full-history rebuild has now landed; revert to incremental polling.
        self._force_full_history = False
        return response

    async def _fetch(self, published_min: datetime, published_max: datetime) -> UsageResponse:
        """Call /proxy/usage for a window and persist any rotated credentials.

        Shared by the normal poll ([_async_update_data]) and [async_rebuild_statistics] so
        the upstream-error → HA-outcome mapping and the credential-rotation write live in one
        place. Raises ``ConfigEntryAuthFailed`` (→ reauth) or ``UpdateFailed`` (→ transient)
        on failure; the stats write is the caller's responsibility.
        """
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
            # The refresh itself was rejected, so there's normally nothing to rotate — but
            # persist defensively in case the proxy did surface a new blob. HA turns
            # ConfigEntryAuthFailed into a persistent notification + reauth flow; the user
            # re-authorizes through the existing config flow (updates blob/token).
            self._persist_rotated_credentials(err.new_credentials)
            raise ConfigEntryAuthFailed(str(err)) from err
        except OpenGbDataPendingError as err:
            # The utility is assembling a large dataset out-of-band (ESPI async batch). We
            # don't implement the Notification/BatchList retrieval flow yet, so raise a repair
            # issue linking to the tracking GitHub issue and ask the (rare) affected user to
            # comment — then fail this refresh so the entry shows as failed. Must be caught
            # BEFORE OpenGbApiError, of which it is a subclass.
            self._persist_rotated_credentials(err.new_credentials)
            self._async_create_background_load_issue()
            raise UpdateFailed(str(err)) from err
        except OpenGbApiError as err:
            # Crucial for one-time refresh tokens (savagedata/OpenIddict): the proxy may have
            # refreshed — rotating and burning our stored refresh token — before the upstream
            # fetch failed. Persist the rotated blob so the next poll retries with the fresh
            # token instead of the dead one, which would otherwise force a spurious reauth.
            self._persist_rotated_credentials(err.new_credentials)
            raise UpdateFailed(str(err)) from err
        except (TimeoutError, ConnectionError) as err:
            raise UpdateFailed(f"network error talking to the proxy: {err}") from err

        self._persist_rotated_credentials(response.new_credentials)
        return response

    def _persist_rotated_credentials(self, new_credentials: NewCredentials | None) -> None:
        """Write rotated credentials into the config entry, if the proxy returned any.

        Persisted on BOTH the success and error paths, and BEFORE anything else that can fail:
        a one-time refresh token (e.g. savagedata's OpenIddict) is redeemed during the proxy's
        token refresh, so dropping the rotated blob leaves the stored token dead and the next
        poll cascades into a spurious reauth. No-op when nothing was rotated.
        """
        if new_credentials is None:
            return
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={
                **self.entry.data,
                CONF_ENCRYPTED_REFRESH_BLOB: new_credentials.encrypted_refresh_blob,
                CONF_PROXY_TOKEN: new_credentials.proxy_token,
            },
        )
        _LOGGER.info("Persisted rotated credentials for entry %s", self.entry.entry_id)

    def _advance_cursor(self, response: UsageResponse) -> None:
        """Persist the incremental cursor as the newest reading start we've imported.

        The cursor only moves *forward*, and only when the response actually carried
        readings. On an empty response it is left untouched — critical for a utility that
        publishes on a multi-day lag: anchoring the cursor to the real data frontier (rather
        than wall-clock ``now``) stops `published-min` from marching past the not-yet-
        published data and starving every later poll. See [_published_min].
        """
        newest = _newest_reading_start(response)
        if newest is None:
            return  # Empty response — keep the window reaching back to the last real data.
        prior_raw = self.entry.data.get(CONF_LAST_FETCHED_AT)
        prior = _parse_iso_or_none(prior_raw)
        cursor = newest if prior is None else max(prior, newest)
        cursor_iso = cursor.isoformat()
        if cursor_iso == prior_raw:
            return  # No forward movement — avoid a no-op entry write (and its churn).
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, CONF_LAST_FETCHED_AT: cursor_iso},
        )

    async def async_rebuild_statistics(self) -> None:
        """Rebuild this entry's statistics from a full re-fetch — non-destructively.

        The supported recovery path when a calculation change (e.g. the cost fix) means the
        already-stored rows are wrong — without making the user remove the entry and redo the
        OAuth authorization. Steps, in order:

          1. Re-fetch the full initial-history window. **This happens FIRST**, so a failed
             fetch can never leave the store emptied.
          2. Only once we hold the replacement data: clear this entry's energy + cost stats.
          3. Block until the recorder has committed the delete.
          4. Import the freshly-fetched response into the now-empty store with ``fresh=True``,
             which imports from a zero baseline and skips the per-series resume-point read.
             That read ([statistics._resume_point], on a DB executor thread) is what a rebuild
             raced against: if it observed the pre-clear cursor, every reading in the
             full-history feed looked "already imported" and got skipped — importing nothing
             and leaving the store empty. ``fresh=True`` removes the race entirely.

        The earlier implementation cleared *before* re-fetching, so a failed fetch (the
        utility's resource server is intermittently flaky) wiped the user's history — and the
        incremental poll could not repopulate it, because its `published-min` window sits
        ahead of the utility's lagged data. Fetching first makes a failed rebuild a no-op.

        Raises:
            HomeAssistantError: the rebuild fetch failed (network / upstream / reauth).
                Nothing was purged; the existing statistics remain intact.
        """
        from homeassistant.components.recorder import get_instance
        from homeassistant.exceptions import HomeAssistantError

        now = datetime.now(UTC)
        self._force_full_history = True
        try:
            published_min = self._published_min(now)
            published_max = now + PUBLISHED_MAX_LOOKAHEAD
            _LOGGER.info(
                "Rebuild for entry %s: re-fetching full history (published-min=%s) before purge",
                self.entry.entry_id,
                published_min.isoformat(),
            )
            try:
                response = await self._fetch(published_min, published_max)
            except (UpdateFailed, ConfigEntryAuthFailed) as err:
                raise HomeAssistantError(
                    f"Rebuild fetch failed for entry {self.entry.entry_id}: {err}. "
                    "Existing statistics were left untouched."
                ) from err
        finally:
            self._force_full_history = False

        total_readings = sum(len(s.readings) for up in response.usage_points for s in up.series)
        _LOGGER.info(
            "Rebuild for entry %s: fetched %d usage point(s) with %d total reading(s)",
            self.entry.entry_id,
            len(response.usage_points),
            total_readings,
        )

        # The fetch succeeded — now it's safe to purge and re-import from a clean slate.
        cleared = await async_clear_statistics_for_entry(self.hass, self.entry.entry_id)
        await get_instance(self.hass).async_block_till_done()
        _LOGGER.info(
            "Rebuild for entry %s: cleared %d statistic(s); importing fresh",
            self.entry.entry_id,
            len(cleared),
        )
        # fresh=True: we just cleared the store, so import from a zero baseline and skip the
        # per-series resume-point read. That read is what a rebuild raced against — if it saw
        # the pre-clear cursor, every reading in the re-fetched full-history feed looked
        # "already imported" and got skipped, importing nothing.
        await import_usage_statistics(
            self.hass,
            self.entry,
            response,
            utility_display_name=self.entry.data.get(CONF_UTILITY_NAME, "Open Green Button"),
            fresh=True,
        )
        self._async_clear_background_load_issue()
        self._advance_cursor(response)
        # Publish the fresh response into the coordinator (updates .data, marks the update
        # successful, notifies listeners, and re-arms the 6h poll) without a second fetch.
        self.async_set_updated_data(response)
        _LOGGER.info("Rebuild complete for entry %s", self.entry.entry_id)

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

        On the first refresh (no recorded `last_fetched_at`) — or a rebuild, which sets
        `_force_full_history` — we look back by the per-utility initial-history window the
        server supplied in the claim response: a bounded initial import that keeps the
        utility's data-collection job and our statistics write manageable. Subsequent refreshes
        ask for the slice since the cursor, minus a small overlap that absorbs clock skew and
        any late-arriving corrections.

        The cursor (`CONF_LAST_FETCHED_AT`) is the newest *reading* we've imported, NOT the
        wall-clock time of the last poll (see [_advance_cursor]). Anchoring it to the data
        frontier is load-bearing: utilities publish on a multi-day lag, so a wall-clock cursor
        would push `published-min` past the not-yet-published data and every later poll would
        come back empty.
        """
        if self._force_full_history:
            return now - self._initial_lookback()
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
