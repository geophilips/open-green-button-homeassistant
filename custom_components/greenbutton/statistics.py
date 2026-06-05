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

# `mean_type` replaces `has_mean` in HA core ≥ 2025.6; the legacy field becomes a hard
# requirement to omit at 2026.11. Import lazily so this module still loads on an HA core
# that predates the enum — if the import fails we'll keep emitting `has_mean=False` only.
try:
    from homeassistant.components.recorder.models import StatisticMeanType

    _MEAN_TYPE_NONE = StatisticMeanType.NONE
except ImportError:  # pragma: no cover — older HA core, drop-through to has_mean only
    _MEAN_TYPE_NONE = None

_LOGGER = logging.getLogger(__name__)


def statistic_id_for_series(
    entry_id: str,
    usage_point_id: str,
    flow_direction: str,
) -> str:
    """Return the canonical statistic_id for one (entry, usage_point, flow_direction) triple.

    Format: ``greenbutton:<entry_slug>_<usage_point_slug>_<flow_lower>``.

    The entry_id prefix is what scopes a test entry's stats apart from a real entry's stats
    on the same utility. Each id component is slugified — HA enforces that the part of a
    statistic_id after the `:` matches a lowercase-letters/digits/underscores slug pattern,
    and our inputs (ULID entry_id with uppercase, UUID usage_point_id with hyphens) violate
    that as-is.
    """
    return f"{DOMAIN}:{_slugify(entry_id)}_{_slugify(usage_point_id)}_{flow_direction.lower()}"


def statistic_id_for_cost(entry_id: str, usage_point_id: str) -> str:
    """Return the statistic_id for the cost series tied to a UsagePoint.

    Cost is per UsagePoint (matches one customer account's billing), not per flow direction
    — ESPI's UsageSummary is account-level. The id shares the same entry+usage-point prefix
    as the energy stats so async_remove_entry's prefix purge catches both.
    """
    return f"{DOMAIN}:{_slugify(entry_id)}_{_slugify(usage_point_id)}_cost"


def statistic_id_prefix_for_entry(entry_id: str) -> str:
    """Return the ``startswith`` prefix that matches every statistic owned by an entry.

    Used by ``async_remove_entry`` to find all of an entry's stats for purging — pairs with
    [statistic_id_for_series] so the format (and the slugification) only live in one place.
    """
    return f"{DOMAIN}:{_slugify(entry_id)}_"


def _slugify(component: str) -> str:
    """Lowercase + replace non-alphanumeric with underscore.

    HA's ``valid_statistic_id`` rejects anything outside ``[a-z0-9_]`` after the colon. Our
    inputs are ULIDs (uppercase letters + digits) and UUIDs (hex + hyphens) — both pass
    through this cleanly into a valid slug.
    """
    return "".join(c if c.isalnum() else "_" for c in component.lower())


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
        await _import_cost_summaries(hass, entry, up, utility_display_name)


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
    # New typed field added in HA core ≥ 2025.6; mean_type replaces has_mean. We keep
    # has_mean for compatibility with HA installs older than that. StatisticMeanType.NONE
    # is the correct value for energy/volume statistics (we only carry `sum`, no mean).
    if _MEAN_TYPE_NONE is not None:
        metadata["mean_type"] = _MEAN_TYPE_NONE

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


async def _import_cost_summaries(
    hass: HomeAssistant,
    entry: ConfigEntry,
    up: UsagePoint,
    utility_display_name: str,
) -> None:
    """Write a cumulative-cost statistic from this UsagePoint's BillingSummary entries.

    HA's Energy dashboard pairs an energy stat with a cost stat at config time — by emitting
    a cost stat with the same entry+usage-point prefix, the user can wire it in alongside
    the FORWARD energy stat to get the Cost column populated.

    Skipped when the UsagePoint has no summaries (most utilities only attach UsageSummary to
    accounts they bill; meter-only test profiles often won't), or when the currency code
    isn't one we have an ISO 4217 alpha mapping for.
    """
    if not up.summaries:
        return
    currency_alpha = _iso_4217_alpha(up.summaries[0].currency_numeric_code)
    if currency_alpha is None:
        _LOGGER.debug(
            "Skipping cost stat for usage point %s: currency code %s has no ISO 4217 mapping",
            up.usage_point_id,
            up.summaries[0].currency_numeric_code,
        )
        return

    statistic_id = statistic_id_for_cost(entry.entry_id, up.usage_point_id)
    metadata: StatisticMetaData = {
        "has_mean": False,
        "has_sum": True,
        "name": (
            f"{utility_display_name} · {up.service_kind.title()} Cost ({up.usage_point_id[:8]})"
        ),
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": currency_alpha,
    }
    if _MEAN_TYPE_NONE is not None:
        metadata["mean_type"] = _MEAN_TYPE_NONE

    resume_from_sum, resume_after_epoch = await _resume_point(hass, statistic_id)

    stats: list[StatisticData] = []
    running = resume_from_sum
    # Summaries are already sorted by billing_period_start (the parser does this); the
    # explicit sort here is belt-and-suspenders.
    for summary in sorted(up.summaries, key=lambda s: s.billing_period_start):
        period_start = _align_to_hour(summary.billing_period_start)
        if resume_after_epoch is not None and period_start.timestamp() <= resume_after_epoch:
            continue
        period_cost = summary.total_cost
        if period_cost == 0:
            # Test-lab fixtures often have $0 placeholders — skip rather than emit a
            # cumulative-flat row that confuses the dashboard.
            continue
        running += period_cost
        stats.append(StatisticData(start=period_start, state=running, sum=running))

    if not stats:
        return

    _LOGGER.info(
        "Importing %d cost rows for %s in %s (resume_from_sum=%.2f)",
        len(stats),
        statistic_id,
        currency_alpha,
        resume_from_sum,
    )
    async_add_external_statistics(hass, metadata, stats)


# Just the codes we expect to see from utilities currently in scope. Expand as we onboard
# more — leaving an unknown code unmapped is safe (we skip the cost stat rather than emit
# one with a unit HA can't display).
_ISO_4217_ALPHA: dict[int, str] = {
    124: "CAD",  # Canada — Burlington Hydro, all Ontario utilities
    840: "USD",  # United States
    978: "EUR",  # Eurozone
    826: "GBP",  # United Kingdom
    36: "AUD",  # Australia
    554: "NZD",  # New Zealand
}


def _iso_4217_alpha(numeric_code: int | None) -> str | None:
    """Map an ISO 4217 numeric currency code to its alpha-3 string (e.g. 124 → ``CAD``)."""
    if numeric_code is None:
        return None
    return _ISO_4217_ALPHA.get(numeric_code)


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
