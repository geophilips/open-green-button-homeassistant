"""Bridge from normalized proxy usage data → HA long-term external statistics.

Two hard requirements baked in here, documented in [__init__.py] and the README:

1. **Statistic IDs are scoped per config entry.** Two entries (a sandbox/test account and a
   real account on the same utility, say) MUST NOT collide in the Energy dashboard.
2. **Removal must purge.** ``async_remove_entry`` in __init__.py reads the same id format and
   calls ``recorder.async_clear_statistics`` so deleting the integration leaves no orphans.

Both requirements depend on ``statistic_id_for_series`` being the single source of truth for
the id format — never construct one ad-hoc elsewhere.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
    from homeassistant.config_entries import ConfigEntry

    from .api import MeterReadingSeries, NormalizedReadingType, UsagePoint, UsageResponse

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData

_LOGGER = logging.getLogger(__name__)


def statistic_id_for_series(
    entry_id: str,
    usage_point_id: str,
    flow_direction: str,
) -> str:
    """Return the canonical statistic_id for one (entry, usage_point, flow_direction) triple.

    Format: ``greenbutton:<entry_id>_<usage_point_id>_<flow_lower>``.

    The entry_id prefix is what scopes a test entry's stats apart from a real entry's stats
    on the same utility. Lower-casing flow_direction keeps the id stable if a future server
    version emits a different enum casing.
    """
    return f"{DOMAIN}:{entry_id}_{usage_point_id}_{flow_direction.lower()}"


def statistic_id_prefix_for_entry(entry_id: str) -> str:
    """Return the ``startswith`` prefix that matches every statistic owned by an entry.

    Used by ``async_remove_entry`` to find all of an entry's stats for purging — pairs with
    [statistic_id_for_series] so the format only lives in one place.
    """
    return f"{DOMAIN}:{entry_id}_"


async def import_usage_statistics(
    hass: HomeAssistant,
    entry: ConfigEntry,
    response: UsageResponse,
    utility_display_name: str,
) -> None:
    """Push every series in [response] into HA long-term statistics.

    Idempotent on (statistic_id, hour) — re-importing a previously-imported hour is a no-op,
    so the coordinator can pull overlapping windows on every poll without worrying about
    duplicates.
    """
    for up in response.usage_points:
        for series in up.series:
            await _import_series(hass, entry, up, series, utility_display_name)


async def _import_series(
    hass: HomeAssistant,
    entry: ConfigEntry,
    up: UsagePoint,
    series: MeterReadingSeries,
    utility_display_name: str,
) -> None:
    if not series.readings:
        return  # Nothing to write; keeps logs quiet on the test-lab empty-account case.

    statistic_id = statistic_id_for_series(
        entry.entry_id,
        up.usage_point_id,
        series.reading_type.flow_direction,
    )
    unit = _ha_unit_for(series.reading_type)
    if unit is None:
        _LOGGER.debug(
            "Skipping series %s: no HA unit mapping for %s/%s",
            statistic_id,
            series.reading_type.commodity,
            series.reading_type.unit_of_measure,
        )
        return

    metadata: StatisticMetaData = {
        "has_mean": False,
        "has_sum": True,
        "name": _stat_display_name(utility_display_name, up, series),
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": unit,
    }

    resume_from_sum, resume_after_epoch = await _resume_point(hass, statistic_id)

    stats: list[StatisticData] = []
    running = resume_from_sum
    for reading in series.readings:
        # Stale-window guard — HA's statistics machinery already deduplicates on
        # (statistic_id, start), but skipping locally avoids resetting `running` from
        # readings already accounted for in the stored cumulative sum.
        if resume_after_epoch is not None and reading.start.timestamp() <= resume_after_epoch:
            continue
        converted = _to_ha_units(reading.value, series.reading_type)
        running += converted
        stats.append(
            StatisticData(
                start=_align_to_hour(reading.start),
                state=running,
                sum=running,
            )
        )

    if not stats:
        return

    _LOGGER.info(
        "Importing %d statistic rows for %s (resume_from_sum=%.3f)",
        len(stats),
        statistic_id,
        resume_from_sum,
    )
    async_add_external_statistics(hass, metadata, stats)


def _stat_display_name(
    utility_display_name: str,
    up: UsagePoint,
    series: MeterReadingSeries,
) -> str:
    """Friendly label shown in the Energy dashboard picker. UsagePoint UUIDs are unhelpful
    raw; we truncate to 8 chars so users can disambiguate multi-meter setups by suffix."""
    short_id = up.usage_point_id[:8]
    flow = series.reading_type.flow_direction.title()
    return f"{utility_display_name} · {up.service_kind.title()} {flow} ({short_id})"


def _ha_unit_for(reading_type: NormalizedReadingType) -> str | None:
    """Map the server's normalized unit name to the HA constant the Energy dashboard expects.

    Returns None for units we don't yet have a domain mapping for — the caller skips writing
    rather than guessing and confusing the dashboard.
    """
    if reading_type.unit_of_measure == "WATT_HOURS":
        return UnitOfEnergy.KILO_WATT_HOUR  # We convert Wh → kWh below.
    if reading_type.unit_of_measure == "CUBIC_METERS":
        return UnitOfVolume.CUBIC_METERS
    return None


def _to_ha_units(value: float, reading_type: NormalizedReadingType) -> float:
    """Apply the unit conversion implied by [_ha_unit_for]. The server already applied the
    ESPI ``powerOfTenMultiplier`` on its end, so ``value`` is in the base unit."""
    if reading_type.unit_of_measure == "WATT_HOURS":
        return value / 1000.0
    return value


def _align_to_hour(start: datetime) -> datetime:
    """HA external statistics require hour-aligned UTC timestamps. ESPI hourly readings
    already align in practice, but defensively zero out sub-hour fields so a buggy utility
    can't poison the statistics store with off-boundary rows."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    return start.replace(minute=0, second=0, microsecond=0).astimezone(UTC)


async def _resume_point(hass: HomeAssistant, statistic_id: str) -> tuple[float, float | None]:
    """Return (last_sum, last_start_epoch) for this statistic_id, or (0.0, None) if no prior."""
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics,
        hass,
        1,
        statistic_id,
        True,
        {"sum", "start"},
    )
    if not last or statistic_id not in last or not last[statistic_id]:
        return 0.0, None
    row = last[statistic_id][0]
    return float(row.get("sum") or 0.0), row.get("start")
