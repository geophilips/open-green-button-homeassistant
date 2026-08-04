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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import TYPE_CHECKING

from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_list_statistic_ids,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant

from .const import CONF_TIER_COST_ESTIMATES, DOMAIN
from .tou import cost_detail_tou_bucket, ontario_tou_bucket

if TYPE_CHECKING:
    from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
    from homeassistant.config_entries import ConfigEntry

    from .api import (
        BillingSummary,
        MeterReadingSeries,
        NormalizedReadingType,
        UsagePoint,
        UsageResponse,
    )

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


@dataclass(frozen=True, slots=True)
class _TieredEstimateProfile:
    """Rates inferred from the latest completed Ontario Tiered bill."""

    tier_one_rate: float
    tier_two_rate: float
    tier_one_kwh_per_day: float
    residual_rate: float


@dataclass(frozen=True, slots=True)
class _TieredEstimateState:
    """Persisted inputs needed to cost an incremental open-period response."""

    profile: _TieredEstimateProfile
    active_period_start: datetime
    predicted_days: float
    currency_alpha: str


def _load_tiered_estimate_state(
    entry: ConfigEntry,
    usage_point_id: str,
) -> _TieredEstimateState | None:
    """Load a validated per-usage-point estimator state from config-entry data."""
    all_states = entry.data.get(CONF_TIER_COST_ESTIMATES)
    if not isinstance(all_states, dict):
        return None
    raw = all_states.get(usage_point_id)
    if not isinstance(raw, dict):
        return None
    try:
        active_period_start = datetime.fromisoformat(raw["active_period_start"])
        if active_period_start.tzinfo is None:
            active_period_start = active_period_start.replace(tzinfo=UTC)
        profile = _TieredEstimateProfile(
            tier_one_rate=float(raw["tier_one_rate"]),
            tier_two_rate=float(raw["tier_two_rate"]),
            tier_one_kwh_per_day=float(raw["tier_one_kwh_per_day"]),
            residual_rate=float(raw["residual_rate"]),
        )
        state = _TieredEstimateState(
            profile=profile,
            active_period_start=active_period_start,
            predicted_days=float(raw["predicted_days"]),
            currency_alpha=str(raw["currency_alpha"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not (
        0 < state.profile.tier_one_rate < 1
        and 0 < state.profile.tier_two_rate < 1
        and state.profile.tier_one_kwh_per_day > 0
        and -1 < state.profile.residual_rate < 1
        and state.predicted_days > 0
        and state.currency_alpha in _ISO_4217_ALPHA.values()
    ):
        return None
    return state


def _store_tiered_estimate_state(
    hass: HomeAssistant,
    entry: ConfigEntry,
    usage_point_id: str,
    state: _TieredEstimateState,
) -> None:
    """Persist estimator state so summary-free incremental polls can keep costing usage."""
    raw_states = entry.data.get(CONF_TIER_COST_ESTIMATES)
    all_states = dict(raw_states) if isinstance(raw_states, dict) else {}
    payload = {
        "active_period_start": state.active_period_start.isoformat(),
        "predicted_days": state.predicted_days,
        "currency_alpha": state.currency_alpha,
        "tier_one_rate": state.profile.tier_one_rate,
        "tier_two_rate": state.profile.tier_two_rate,
        "tier_one_kwh_per_day": state.profile.tier_one_kwh_per_day,
        "residual_rate": state.profile.residual_rate,
    }
    if all_states.get(usage_point_id) == payload:
        return
    all_states[usage_point_id] = payload
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_TIER_COST_ESTIMATES: all_states},
    )


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


async def async_clear_statistics_for_entry(hass: HomeAssistant, entry_id: str) -> list[str]:
    """Delete every long-term statistic owned by one config entry; return the ids cleared.

    Shared by ``async_remove_entry`` (teardown) and the ``rebuild_statistics`` service
    (purge-before-reimport), so the "which ids belong to this entry" rule lives in one place
    next to [statistic_id_for_series] / [statistic_id_prefix_for_entry].

    ``async_list_statistic_ids`` is ``async`` (not a ``@callback``) and takes no source
    filter, so we list everything the recorder knows and filter to our source + this entry's
    prefix. The source check is the load-bearing one; the prefix keeps us from touching a
    sibling entry's rows. ``Recorder.async_clear_statistics`` is a ``@callback`` that queues
    the delete on the recorder's worker thread — call it from the event loop, never wrap it in
    an executor job (that would bypass the recorder queue and run a callback off-loop).
    """
    prefix = statistic_id_prefix_for_entry(entry_id)
    all_ids = await async_list_statistic_ids(hass)
    owned = [
        item["statistic_id"]
        for item in all_ids
        if item.get("source") == DOMAIN and item["statistic_id"].startswith(prefix)
    ]
    if owned:
        get_instance(hass).async_clear_statistics(owned)
    return owned


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
    *,
    fresh: bool = False,
) -> None:
    """Push interval-delta series in [response] into HA long-term statistics.

    Idempotent on (statistic_id, hour) — re-importing a previously-imported hour is a no-op,
    so the coordinator can pull overlapping windows on every poll without worrying about
    duplicates.

    ``fresh=True`` means "the store was just cleared; import from a zero baseline". It skips
    the per-series resume-point read entirely. That read (``get_last_statistics``) is what a
    rebuild raced against: if it observed the pre-clear cursor, every reading in the re-fetched
    full-history feed looked "already imported" and got skipped, importing nothing. On a
    rebuild there is by definition no prior data to resume from, so reading it is both
    unnecessary and the source of the race — bypass it.
    """
    for up in response.usage_points:
        for series in up.series:
            if not _is_interval_consumption_series(series):
                _LOGGER.debug(
                    "Skipping meter reading %s: accumulation behaviour %s is not "
                    "interval consumption",
                    series.meter_reading_id,
                    series.reading_type.accumulation_behaviour,
                )
                continue
            await _import_series(hass, entry, up, series, utility_display_name, fresh=fresh)

    # Cost is written after usage, in a second pass. A monthly UsageSummary arrives long after its
    # billing period (Burlington publishes it ~2-3 weeks later), so the period's usage is NOT in
    # this response — it's already in the recorder. [_import_cost_summaries] reads it back to
    # distribute the bill, so the usage writes above must be committed first. Block once here; on a
    # fresh rebuild the period's usage was written moments ago and would otherwise not be visible.
    if any(
        not _has_interval_cost(up)
        and (up.summaries or _load_tiered_estimate_state(entry, up.usage_point_id) is not None)
        for up in response.usage_points
    ):
        await get_instance(hass).async_block_till_done()

    for up in response.usage_points:
        # Prefer genuine per-interval <cost>, which is more accurate and self-contained on the
        # reading. Fall back to distributing a monthly UsageSummary total over the period's
        # recorded usage when interval costs are absent or only zero-valued placeholders exist.
        if _has_interval_cost(up):
            await _import_cost_from_readings(hass, entry, up, utility_display_name, fresh=fresh)
        else:
            await _import_cost_summaries(hass, entry, up, utility_display_name, fresh=fresh)


def _is_interval_consumption_series(series: MeterReadingSeries) -> bool:
    """True for readings that represent consumption during each interval.

    ``BULK_QUANTITY`` readings are cumulative register snapshots. Adding those values to the
    running statistic alongside hourly ``DELTA_DATA`` produces enormous false consumption spikes,
    as seen in Milton Hydro feeds that publish both series for the same meter.
    """
    return series.reading_type.accumulation_behaviour == "DELTA_DATA"


def _has_interval_cost(up: UsagePoint) -> bool:
    """True when interval consumption carries at least one non-zero cost.

    Some utilities attach ``cost=0`` to a cumulative ``BULK_QUANTITY`` register reading while
    reporting the actual bill in ``UsageSummary``. A zero placeholder must not select the
    per-interval path and suppress that summary. Legitimate zero-cost hours remain included once
    another interval establishes that the series genuinely itemizes costs.
    """
    return any(
        r.cost is not None and r.cost != 0
        for s in up.series
        if s.reading_type.flow_direction == "FORWARD" and _is_interval_consumption_series(s)
        for r in s.readings
    )


async def _import_series(
    hass: HomeAssistant,
    entry: ConfigEntry,
    up: UsagePoint,
    series: MeterReadingSeries,
    utility_display_name: str,
    *,
    fresh: bool = False,
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
        "unit_class": _ha_unit_class_for(series.reading_type),
    }
    # New typed field added in HA core ≥ 2025.6; mean_type replaces has_mean. We keep
    # has_mean for compatibility with HA installs older than that. StatisticMeanType.NONE
    # is the correct value for energy/volume statistics (we only carry `sum`, no mean).
    if _MEAN_TYPE_NONE is not None:
        metadata["mean_type"] = _MEAN_TYPE_NONE

    resume_from_sum, resume_after_epoch = (
        (0.0, None) if fresh else await _resume_point(hass, statistic_id)
    )

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


def _select_billing_summaries(summaries: list[BillingSummary]) -> list[BillingSummary]:
    """Pick a non-overlapping, deduplicated set of summaries, in billing-period order.

    A single ESPI feed frequently carries more than one ``UsageSummary`` covering the same
    hours — exact duplicates repeated across the paginated feed, and/or rollup summaries at a
    coarser granularity (e.g. a 12-month total alongside the per-bill totals). The cost
    importer distributes each summary's *full* period total across its hours and **adds** the
    result into one cumulative statistic, so any overlap multiplies the cost the Energy
    dashboard shows for those hours (a single duplicated bill doubles it; a handful of
    overlapping rollups can inflate it several-fold) while leaving energy untouched.

    We defend against that here by choosing a maximal non-overlapping subset:

      - Shortest duration first, so the summary most specific to a single bill wins over a
        rollup that spans it.
      - Higher total cost breaks ties, so a real bill beats a $0 placeholder for the same
        period (test-lab feeds emit those).
      - A summary is dropped when its ``[start, end)`` window overlaps one already kept.

    Consecutive real billing periods share only their boundary instant, which is half-open
    here, so they never collide — only genuine duplicates and coarser rollups get dropped.
    """
    # Shortest-first, then earliest, then most-expensive — see docstring for the rationale.
    ordered = sorted(
        summaries,
        key=lambda s: (
            s.billing_period_duration_seconds,
            s.billing_period_start,
            -s.total_cost,
        ),
    )
    accepted: list[BillingSummary] = []
    accepted_intervals: list[tuple[datetime, datetime]] = []
    for summary in ordered:
        start = summary.billing_period_start
        end = start + timedelta(seconds=summary.billing_period_duration_seconds)
        if any(start < a_end and a_start < end for a_start, a_end in accepted_intervals):
            _LOGGER.debug(
                "Dropping overlapping billing summary %s (+%ds, total_cost=%.2f) — its hours "
                "are already costed by a more specific summary",
                start.isoformat(),
                summary.billing_period_duration_seconds,
                summary.total_cost,
            )
            continue
        accepted.append(summary)
        accepted_intervals.append((start, end))

    accepted.sort(key=lambda s: s.billing_period_start)
    return accepted


async def _recorded_forward_hours(
    hass: HomeAssistant,
    entry: ConfigEntry,
    up: UsagePoint,
    period_start: datetime,
    period_end: datetime,
) -> list[tuple[datetime, float]]:
    """Per-hour FORWARD consumption ``(hour, kWh)`` for ``[period_start, period_end)``.

    Read from the recorder, not the response: a UsageSummary distributed here arrives long after
    its period, whose readings are already imported into the FORWARD usage statistic. We recover
    each hour's kWh as the delta of that statistic's cumulative ``sum`` — querying one hour before
    ``period_start`` so the first in-period hour has a predecessor to diff against.

    Hours with no forward movement (a gap, or a duplicate) are dropped; the result feeds only the
    proportional cost distribution, so approximate weights across a small gap are harmless.
    """
    stat_id = statistic_id_for_series(entry.entry_id, up.usage_point_id, "FORWARD")
    by_id = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        period_start - timedelta(hours=1),
        period_end,
        {stat_id},
        "hour",
        None,
        {"sum"},
    )
    hours: list[tuple[datetime, float]] = []
    prev_sum: float | None = None
    for row in by_id.get(stat_id, []):
        total = row.get("sum")
        if total is None:
            continue
        start = row["start"]
        hour = start if isinstance(start, datetime) else datetime.fromtimestamp(start, tz=UTC)
        if prev_sum is not None and period_start <= hour < period_end and total > prev_sum:
            hours.append((hour, total - prev_sum))
        prev_sum = total
    return hours


async def _import_cost_summaries(
    hass: HomeAssistant,
    entry: ConfigEntry,
    up: UsagePoint,
    utility_display_name: str,
    *,
    fresh: bool = False,
) -> None:
    """Write a cumulative-cost statistic from this UsagePoint's BillingSummary entries.

    HA's Energy dashboard pairs an energy stat with a cost stat at config time, and reads
    them at the *same* time-bucket granularity as the energy stat (hourly). A single-value
    cost stat at billing_period_start would show non-zero for one hour and zero for every
    other hour — which is exactly the "shows as zero" symptom users see.

    Instead, we distribute each billing-period total across the hours within that period
    in proportion to that hour's consumption:

        cost_at_hour_h = period_total_cost × (kwh_h / total_kwh_in_period)

    This gives a per-hour cost that the dashboard aggregates into daily/monthly views
    correctly, and matches the way a utility actually bills (you pay for the energy you
    used, and a higher-consumption hour incurs more of the period's total cost). Real-world
    accuracy depends on whether the utility's pricing is flat or TOU; the test lab is
    flat, while production TOU pricing would want richer cost-detail handling
    (future work).

    The period's usage comes from the **recorder**, not this response: a UsageSummary is
    published weeks after its period closes, so an incremental poll that carries a freshly-
    published summary does NOT carry that period's readings (they were imported long ago). We
    read them back from the usage statistic ([_recorded_forward_hours]) to distribute over.
    This is why a plain published-min poll is enough to keep cost current — no reach-back needed.

    When an incremental poll has no summary, a previously inferred Tiered profile is loaded
    from the config entry so the open-period estimate keeps advancing across polls/restarts.
    Meter-only profiles without either summaries or saved estimate state are skipped.
    """
    saved_state = _load_tiered_estimate_state(entry, up.usage_point_id)
    if not up.summaries and saved_state is None:
        return
    currency_alpha = (
        _iso_4217_alpha(up.summaries[0].currency_numeric_code)
        if up.summaries
        else saved_state.currency_alpha
    )
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
        # HA's recorder validates `unit_class` against the registered `BaseUnitConverter`
        # families in `util.unit_conversion`. There's no MonetaryConverter (you can't
        # convert CAD ↔ USD via a fixed-ratio table), so anything besides None throws
        # `Unsupported unit_class: '<value>'` at metadata validation. None is the
        # well-formed answer for currency stats — the 2026.11 deprecation warning still
        # fires for it, but the warning isn't a hard error and HA hasn't introduced a
        # monetary class to migrate to yet. Revisit when HA adds one.
        "unit_class": None,
    }
    if _MEAN_TYPE_NONE is not None:
        metadata["mean_type"] = _MEAN_TYPE_NONE

    resume_from_sum, resume_after_epoch = (
        (0.0, None) if fresh else await _resume_point(hass, statistic_id)
    )

    if not up.summaries:
        latest_forward_hour = _latest_forward_hour(up)
        if latest_forward_hour is None:
            return
        open_hours = await _recorded_forward_hours(
            hass,
            entry,
            up,
            saved_state.active_period_start,
            latest_forward_hour + timedelta(hours=1),
        )
        cost_at_hour = _tiered_estimated_costs(
            open_hours,
            saved_state.profile,
            saved_state.predicted_days,
        )
        running = resume_from_sum
        stats: list[StatisticData] = []
        for hour_start, _kwh in open_hours:
            if resume_after_epoch is not None and hour_start.timestamp() <= resume_after_epoch:
                continue
            running += cost_at_hour[hour_start]
            stats.append(StatisticData(start=hour_start, state=running, sum=running))
        if stats:
            _LOGGER.info(
                "Appending %d provisional tiered-cost rows for %s",
                len(stats),
                statistic_id,
            )
            async_add_external_statistics(hass, metadata, stats)
        return

    selected = _select_billing_summaries(up.summaries)
    if not selected:
        return

    # The most recent completed bill may replace provisional hourly costs written while that
    # period was still open. Rewrite that bill plus the new open period on every normal refresh,
    # using the last cumulative value before it as the baseline. A fresh/first import still writes
    # every bill from zero.
    if fresh or resume_after_epoch is None:
        summaries_to_write = selected
        running = 0.0
    else:
        summaries_to_write = [selected[-1]]
        running = await _cost_sum_before(
            hass,
            statistic_id,
            selected[-1].billing_period_start,
        )

    stats: list[StatisticData] = []
    latest_summary_hours: list[tuple[datetime, float]] = []
    for summary in summaries_to_write:
        period_cost = summary.total_cost
        if period_cost == 0:
            # Test-lab fixtures often have $0 placeholders — skip rather than emit a
            # cumulative-flat row across an entire month.
            continue
        period_start = summary.billing_period_start
        period_end = period_start + timedelta(seconds=summary.billing_period_duration_seconds)
        # FORWARD consumption for the period, read back from the recorder (see the docstring).
        # REVERSE flow (solar export) isn't part of consumption cost, so the usage stat we read
        # here — the FORWARD series — is the right basis.
        in_period = await _recorded_forward_hours(hass, entry, up, period_start, period_end)
        total_period_kwh = sum(k for (_, k) in in_period)
        if total_period_kwh <= 0:
            # No recorded usage for this period yet (e.g. a bill whose period predates our usage
            # backfill) — nothing to distribute the cost over; skip until/if the usage exists.
            continue

        # Per-hour cost = per-kWh TOU rate (zero if not a TOU bucket) + per-kWh non-TOU rate
        # for everything else (Delivery, Global Adjustment, rebates, etc.). Sum of all hourly
        # costs across the period equals the period's total bill — verified by construction.
        cost_at_hour = _cost_distribution_for_period(summary, in_period, total_period_kwh)
        for hour_start, _kwh in in_period:
            running += cost_at_hour[hour_start]
            stats.append(StatisticData(start=hour_start, state=running, sum=running))
        if summary is selected[-1]:
            latest_summary_hours = in_period

    # A UsageSummary is only published after the bill closes. Until then, infer the Ontario
    # Tiered rates from the latest completed bill and append provisional hourly costs for the
    # currently-open period. The next summary refresh rewrites this tail with the exact bill.
    latest = selected[-1]
    latest_end = latest.billing_period_start + timedelta(
        seconds=latest.billing_period_duration_seconds
    )
    latest_forward_hour = _latest_forward_hour(up)
    if latest_summary_hours:
        profile = _tiered_estimate_profile(latest, latest_summary_hours)
        if profile is not None:
            predicted_days = _predicted_billing_days(selected, latest_end)
            estimate_state = _TieredEstimateState(
                profile=profile,
                active_period_start=latest_end,
                predicted_days=predicted_days,
                currency_alpha=currency_alpha,
            )
            _store_tiered_estimate_state(hass, entry, up.usage_point_id, estimate_state)
        else:
            estimate_state = None
        if (
            estimate_state is not None
            and latest_forward_hour is not None
            and latest_forward_hour >= latest_end
        ):
            open_hours = await _recorded_forward_hours(
                hass,
                entry,
                up,
                latest_end,
                latest_forward_hour + timedelta(hours=1),
            )
            cost_at_hour = _tiered_estimated_costs(
                open_hours,
                estimate_state.profile,
                estimate_state.predicted_days,
            )
            for hour_start, _kwh in open_hours:
                running += cost_at_hour[hour_start]
                stats.append(StatisticData(start=hour_start, state=running, sum=running))
            if open_hours:
                _LOGGER.info(
                    "Added %d provisional tiered-cost rows from %s using a %.0f-day "
                    "billing-period estimate",
                    len(open_hours),
                    latest_end.isoformat(),
                    estimate_state.predicted_days,
                )

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


async def _cost_sum_before(
    hass: HomeAssistant,
    statistic_id: str,
    before: datetime,
) -> float:
    """Return the cumulative cost immediately before ``before``."""
    by_id = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        before - timedelta(hours=2),
        before,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    rows = by_id.get(statistic_id, [])
    candidates: list[tuple[float, float]] = []
    for row in rows:
        total = row.get("sum")
        start = row.get("start")
        if total is None or start is None:
            continue
        epoch = start.timestamp() if isinstance(start, datetime) else float(start)
        if epoch < before.timestamp():
            candidates.append((epoch, float(total)))
    return max(candidates, default=(0.0, 0.0))[1]


def _latest_forward_hour(up: UsagePoint) -> datetime | None:
    """Newest hour-aligned FORWARD interval-consumption reading in this response."""
    hours = [
        _align_to_hour(reading.start)
        for series in up.series
        if series.reading_type.flow_direction == "FORWARD"
        and _is_interval_consumption_series(series)
        for reading in series.readings
    ]
    return max(hours, default=None)


def _tiered_estimate_profile(
    summary: BillingSummary,
    in_period: list[tuple[datetime, float]],
) -> _TieredEstimateProfile | None:
    """Infer Ontario Tiered rates and non-energy cost from a completed bill.

    Milton-style UsageSummary details label the commodity charges as Block/Tier 1 and 2 but
    omit their per-kWh rates. Ontario's residential lower-tier allowance is 600 kWh per 30
    summer days and 1,000 kWh per 30 winter days; utilities prorate that allowance to the
    actual number of billed days. Those quantities let us recover both rates exactly from a
    completed bill, while the remaining bill total becomes an effective per-kWh estimate for
    delivery, regulatory charges, HST, and rebates during the open period.
    """
    tier_cost = {1: 0.0, 2: 0.0}
    seasons: set[str] = set()
    for detail in summary.cost_details:
        note = detail.normalized_note.replace("-", " ").replace("'", "")
        tier: int | None = None
        if "block 1" in note or "tier 1" in note:
            tier = 1
        elif "block 2" in note or "tier 2" in note:
            tier = 2
        if tier is None or detail.amount <= 0:
            continue
        tier_cost[tier] += detail.amount
        if "summer" in note or "smr" in note:
            seasons.add("summer")
        if "winter" in note or "win" in note:
            seasons.add("winter")

    if tier_cost[1] <= 0 or tier_cost[2] <= 0 or len(seasons) != 1:
        return None

    # Billing thresholds are prorated by calendar days. A period spanning a DST boundary can
    # contain 23/25-hour days, so round the elapsed seconds instead of treating the fractional
    # UTC duration as a fractional billing day.
    days = round(summary.billing_period_duration_seconds / 86400)
    tier_one_kwh_per_day = 20.0 if "summer" in seasons else 1000.0 / 30.0
    tier_one_kwh = tier_one_kwh_per_day * days
    total_kwh = sum(kwh for _hour, kwh in in_period)
    tier_two_kwh = total_kwh - tier_one_kwh
    if days <= 0 or tier_two_kwh <= 0 or total_kwh <= 0:
        return None

    tier_one_rate = tier_cost[1] / tier_one_kwh
    tier_two_rate = tier_cost[2] / tier_two_kwh
    residual_rate = (summary.total_cost - tier_cost[1] - tier_cost[2]) / total_kwh
    # Fail closed for malformed/non-Ontario line items instead of publishing implausible costs.
    if not (0 < tier_one_rate < 1 and 0 < tier_two_rate < 1 and -1 < residual_rate < 1):
        return None
    return _TieredEstimateProfile(
        tier_one_rate=tier_one_rate,
        tier_two_rate=tier_two_rate,
        tier_one_kwh_per_day=tier_one_kwh_per_day,
        residual_rate=residual_rate,
    )


def _predicted_billing_days(
    summaries: list[BillingSummary],
    open_period_start: datetime,
) -> float:
    """Predict the open period length from the same cycle in the previous year.

    Utilities use customer-specific read cycles rather than a fixed weekday. The closest
    same-month start from an earlier year is the best available forecast; otherwise use the
    median of recent completed periods. Calendar-day rounding removes one-hour DST artifacts.
    """
    prior_same_month = [
        summary
        for summary in summaries
        if summary.billing_period_start.year < open_period_start.year
        and summary.billing_period_start.month == open_period_start.month
    ]
    if prior_same_month:
        closest = min(
            prior_same_month,
            key=lambda summary: (
                open_period_start.year - summary.billing_period_start.year,
                abs(open_period_start.day - summary.billing_period_start.day),
            ),
        )
        return float(round(closest.billing_period_duration_seconds / 86400))

    recent_days = [
        round(summary.billing_period_duration_seconds / 86400) for summary in summaries[-12:]
    ]
    return float(round(median(recent_days))) if recent_days else 30.0


def _tiered_estimated_costs(
    hours: list[tuple[datetime, float]],
    profile: _TieredEstimateProfile,
    predicted_days: float,
) -> dict[datetime, float]:
    """Price open-period hours, splitting the hour that crosses the tier threshold."""
    threshold = profile.tier_one_kwh_per_day * predicted_days
    consumed = 0.0
    out: dict[datetime, float] = {}
    for hour_start, kwh in hours:
        tier_one_kwh = max(0.0, min(kwh, threshold - consumed))
        tier_two_kwh = kwh - tier_one_kwh
        out[hour_start] = (
            tier_one_kwh * profile.tier_one_rate
            + tier_two_kwh * profile.tier_two_rate
            + kwh * profile.residual_rate
        )
        consumed += kwh
    return out


async def _import_cost_from_readings(
    hass: HomeAssistant,
    entry: ConfigEntry,
    up: UsagePoint,
    utility_display_name: str,
    *,
    fresh: bool = False,
) -> None:
    """Write a cumulative-cost statistic from per-interval `<cost>` on the FORWARD readings.

    Some utilities itemize the actual cost on every IntervalReading — more accurate and
    finer-grained than distributing a monthly UsageSummary total (and it works when the summary
    only carries an "Amount Due" subtotal, which our summary path deliberately drops). Costs are
    summed per hour across the UsagePoint's FORWARD interval-delta series (multiple meters roll up
    into one bill), then accumulated into the same cost stat the Energy dashboard reads.
    """
    currency_code = next(
        (
            s.reading_type.currency_numeric_code
            for s in up.series
            if s.reading_type.flow_direction == "FORWARD"
            and _is_interval_consumption_series(s)
            and s.reading_type.currency_numeric_code is not None
        ),
        None,
    )
    currency_alpha = _iso_4217_alpha(currency_code)
    if currency_alpha is None:
        _LOGGER.debug(
            "Skipping per-interval cost for %s: currency %s has no ISO 4217 mapping",
            up.usage_point_id,
            currency_code,
        )
        return

    cost_by_hour: dict[datetime, float] = {}
    for series in up.series:
        if series.reading_type.flow_direction != "FORWARD" or not _is_interval_consumption_series(
            series
        ):
            continue
        for reading in series.readings:
            if reading.cost is None:
                continue
            hour = _align_to_hour(reading.start)
            cost_by_hour[hour] = cost_by_hour.get(hour, 0.0) + reading.cost
    if not cost_by_hour:
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
        "unit_class": None,  # no monetary converter in HA; see _import_cost_summaries.
    }
    if _MEAN_TYPE_NONE is not None:
        metadata["mean_type"] = _MEAN_TYPE_NONE

    resume_from_sum, resume_after_epoch = (
        (0.0, None) if fresh else await _resume_point(hass, statistic_id)
    )
    stats: list[StatisticData] = []
    running = resume_from_sum
    for hour in sorted(cost_by_hour):
        if resume_after_epoch is not None and hour.timestamp() <= resume_after_epoch:
            continue
        running += cost_by_hour[hour]
        stats.append(StatisticData(start=hour, state=running, sum=running))
    if not stats:
        return
    _LOGGER.info(
        "Importing %d per-interval cost rows for %s in %s",
        len(stats),
        statistic_id,
        currency_alpha,
    )
    async_add_external_statistics(hass, metadata, stats)


def _cost_distribution_for_period(
    summary: BillingSummary,
    in_period: list[tuple[datetime, float]],
    total_period_kwh: float,
) -> dict[datetime, float]:
    """Return per-hour cost for the readings in [in_period].

    When the summary's detail items include TOU line items (Off-Peak, Mid-Peak, On-Peak),
    each TOU portion is distributed across the hours of that bucket at its own rate, and
    everything else (Delivery, taxes, rebates, plus billLastPeriod headroom) is distributed
    flat per kWh. When no TOU line items are present, the whole period total goes flat
    per kWh — same result as the pre-TOU implementation.

    Conservation invariant: ``sum(returned.values()) == summary.total_cost`` (to within
    floating-point rounding), provided every reading lands in a non-empty bucket. If a
    bucket has no readings in this period (e.g. test data spans only weekdays so the
    weekend off-peak bucket is empty), that bucket's spend is absorbed into the flat
    component instead — avoids "lost" dollars in the dashboard.
    """
    tou_cost_by_bucket: dict[str, float] = {}
    for detail in summary.cost_details:
        bucket = cost_detail_tou_bucket(detail.note)
        if bucket is not None:
            tou_cost_by_bucket[bucket] = tou_cost_by_bucket.get(bucket, 0.0) + detail.amount

    # Per-bucket kWh in this period (drives the TOU-rate denominator).
    kwh_by_bucket: dict[str, float] = {}
    bucket_of: dict[datetime, str] = {}
    for hour_start, kwh in in_period:
        b = ontario_tou_bucket(hour_start)
        bucket_of[hour_start] = b
        kwh_by_bucket[b] = kwh_by_bucket.get(b, 0.0) + kwh

    # If a TOU line item is for a bucket the period has no readings in, fold its spend into
    # the flat-rate residual rather than dropping it.
    tou_distributed: float = 0.0
    bucket_rates: dict[str, float] = {}
    for bucket, cost in tou_cost_by_bucket.items():
        bucket_kwh = kwh_by_bucket.get(bucket, 0.0)
        if bucket_kwh > 0:
            bucket_rates[bucket] = cost / bucket_kwh
            tou_distributed += cost

    flat_residual = summary.total_cost - tou_distributed
    flat_rate = (flat_residual / total_period_kwh) if total_period_kwh > 0 else 0.0

    out: dict[datetime, float] = {}
    for hour_start, kwh in in_period:
        tou_rate = bucket_rates.get(bucket_of[hour_start], 0.0)
        out[hour_start] = kwh * (tou_rate + flat_rate)
    return out


# Just the codes we expect to see from utilities currently in scope. Expand as we onboard
# more — leaving an unknown code unmapped is safe (we skip the cost stat rather than emit
# one with a unit HA can't display).
_ISO_4217_ALPHA: dict[int, str] = {
    124: "CAD",  # Canada — Ontario and other Canadian utilities
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


def _ha_unit_class_for(reading_type: NormalizedReadingType) -> str | None:
    """Return the HA `unit_class` matching the series's normalized unit.

    HA's recorder uses unit_class to know which `BaseUnitConverter` family the statistic
    belongs to (and therefore which units it can convert between in the UI). The class names
    are the strings on each subclass's `UNIT_CLASS` attribute in `util.unit_conversion`.
    Missing the field is a deprecation that becomes a hard requirement in 2026.11.
    """
    if reading_type.unit_of_measure == "WATT_HOURS":
        return "energy"
    if reading_type.unit_of_measure == "CUBIC_METERS":
        return "volume"
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
