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
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.start import async_at_started

from .const import CONF_TIER_COST_ESTIMATES, CONF_UTILITY_ID, DOMAIN
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

_MILTON_UTILITY_ID = "milton_hydro"
_TIERED_ESTIMATE_GRACE_DAYS = 14


@dataclass(frozen=True, slots=True)
class _TieredEstimateProfile:
    """Rates inferred from a completed Milton Hydro Ontario Tiered bill."""

    tier_one_rate: float
    tier_two_rate: float
    tier_one_kwh_per_day: float
    residual_rate: float


@dataclass(frozen=True, slots=True)
class _TieredEstimateState:
    """Persisted inputs for one bounded, provisional billing-period estimate."""

    profile: _TieredEstimateProfile
    active_period_start: datetime
    predicted_days: float
    currency_alpha: str
    baseline_sum: float | None

    @property
    def period_end(self) -> datetime:
        """Predicted exclusive end of the active billing period."""
        return self.active_period_start + timedelta(days=self.predicted_days)

    @property
    def grace_end(self) -> datetime:
        """Exclusive safety cutoff while an exact UsageSummary is delayed."""
        return self.period_end + timedelta(days=_TIERED_ESTIMATE_GRACE_DAYS)


def _tiered_estimates_supported(entry: ConfigEntry) -> bool:
    """True only for the verified Milton Hydro feed shape.

    Ontario thresholds and Milton's Block/Tier summary labels are utility-specific. Keeping the
    gate here prevents a coincidentally similar line item from enabling estimates for Burlington,
    Elexicon, or a future non-Ontario utility.
    """
    return entry.data.get(CONF_UTILITY_ID) == _MILTON_UTILITY_ID


def _load_tiered_estimate_state(
    entry: ConfigEntry,
    usage_point_id: str,
) -> _TieredEstimateState | None:
    """Load a validated Milton estimate; accept legacy state without a saved baseline."""
    if not _tiered_estimates_supported(entry):
        return None
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
        raw_baseline = raw.get("baseline_sum")
        state = _TieredEstimateState(
            profile=profile,
            active_period_start=active_period_start,
            predicted_days=float(raw["predicted_days"]),
            currency_alpha=str(raw["currency_alpha"]),
            baseline_sum=None if raw_baseline is None else float(raw_baseline),
        )
    except (KeyError, TypeError, ValueError):
        return None
    return state if _is_valid_tiered_estimate_state(state) else None


def _is_valid_tiered_estimate_state(state: _TieredEstimateState) -> bool:
    """Return whether persisted or manually supplied estimator inputs are safe."""
    return (
        0 < state.profile.tier_one_rate < 1
        and 0 < state.profile.tier_two_rate < 1
        and state.profile.tier_one_kwh_per_day > 0
        and -1 < state.profile.residual_rate < 1
        and 0 < state.predicted_days <= 62
        and state.currency_alpha in _ISO_4217_ALPHA.values()
        and (state.baseline_sum is None or state.baseline_sum >= 0)
    )


def _store_tiered_estimate_state(
    hass: HomeAssistant,
    entry: ConfigEntry,
    usage_point_id: str,
    state: _TieredEstimateState,
) -> None:
    """Persist estimate state without triggering a config-entry reload.

    The integration intentionally has no broad update listener: the coordinator already writes
    cursors and rotated credentials into entry.data on every poll. This state write relies on the
    same invariant and is kept idempotent to avoid needless registry updates.
    """
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
        "baseline_sum": state.baseline_sum,
    }
    if all_states.get(usage_point_id) == payload:
        return
    all_states[usage_point_id] = payload
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_TIER_COST_ESTIMATES: all_states},
    )


def _clear_tiered_estimate_state(
    hass: HomeAssistant,
    entry: ConfigEntry,
    usage_point_id: str,
) -> None:
    """Drop a stale profile when a new exact bill cannot safely renew it."""
    raw_states = entry.data.get(CONF_TIER_COST_ESTIMATES)
    if not isinstance(raw_states, dict) or usage_point_id not in raw_states:
        return
    all_states = dict(raw_states)
    del all_states[usage_point_id]
    data = {**entry.data, CONF_TIER_COST_ESTIMATES: all_states}
    hass.config_entries.async_update_entry(entry, data=data)


async def async_seed_tiered_estimate(
    hass: HomeAssistant,
    entry: ConfigEntry,
    response: UsageResponse,
    utility_display_name: str,
    *,
    active_period_start: datetime,
    predicted_days: float,
    currency_alpha: str,
    tier_one_rate: float,
    tier_two_rate: float,
    tier_one_kwh_per_day: float,
    residual_rate: float,
    usage_point_id: str | None = None,
) -> str:
    """Seed a verified Milton profile and immediately price the bounded open period."""
    if not _tiered_estimates_supported(entry):
        raise ValueError("Tiered cost estimates are supported only for Milton Hydro")

    usage_points = response.usage_points
    if usage_point_id is None:
        if len(usage_points) != 1:
            raise ValueError(
                "usage_point_id is required when the response has more than one usage point"
            )
        up = usage_points[0]
    else:
        up = next(
            (candidate for candidate in usage_points if candidate.usage_point_id == usage_point_id),
            None,
        )
        if up is None:
            raise ValueError(f"Unknown usage_point_id {usage_point_id!r}")

    if active_period_start.tzinfo is None:
        active_period_start = active_period_start.replace(tzinfo=UTC)
    statistic_id = statistic_id_for_cost(entry.entry_id, up.usage_point_id)
    baseline_sum = await _cost_sum_before(hass, statistic_id, active_period_start)
    state = _TieredEstimateState(
        profile=_TieredEstimateProfile(
            tier_one_rate=tier_one_rate,
            tier_two_rate=tier_two_rate,
            tier_one_kwh_per_day=tier_one_kwh_per_day,
            residual_rate=residual_rate,
        ),
        active_period_start=active_period_start,
        predicted_days=predicted_days,
        currency_alpha=currency_alpha.upper(),
        baseline_sum=baseline_sum,
    )
    if not _is_valid_tiered_estimate_state(state):
        raise ValueError("Tiered estimate values are outside the supported ranges")

    _store_tiered_estimate_state(hass, entry, up.usage_point_id, state)
    await _import_cost_summaries_with_estimates(hass, entry, up, utility_display_name)
    return up.usage_point_id


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

    ``Recorder.async_clear_statistics`` is a ``@callback`` that queues the delete on the
    recorder's worker thread — call it from the event loop, never wrap it in an executor job
    (that would bypass the recorder queue and run a callback off-loop).
    """
    owned = await _async_statistic_ids_for_entry(hass, entry_id)
    if owned:
        get_instance(hass).async_clear_statistics(owned)
    return owned


async def async_entry_has_statistics(hass: HomeAssistant, entry_id: str) -> bool:
    """True when this entry already owns at least one long-term statistic.

    Read-only counterpart to [async_clear_statistics_for_entry]. The coordinator uses it to
    tell "this entry has imported before, under whatever logic shipped then" from "this entry
    is importing for the first time" — which decides whether a one-time rebuild is warranted
    after an import-logic change. See [coordinator.GreenButtonCoordinator._async_migrate_import].
    """
    return bool(await _async_statistic_ids_for_entry(hass, entry_id))


async def _async_statistic_ids_for_entry(hass: HomeAssistant, entry_id: str) -> list[str]:
    """Every statistic id owned by [entry_id], per the [statistic_id_for_series] format.

    ``async_list_statistic_ids`` is ``async`` (not a ``@callback``) and takes no source filter,
    so we list everything the recorder knows and filter to our source + this entry's prefix. The
    source check is the load-bearing one; the prefix keeps us off a sibling entry's rows.
    """
    prefix = statistic_id_prefix_for_entry(entry_id)
    all_ids = await async_list_statistic_ids(hass)
    return [
        item["statistic_id"]
        for item in all_ids
        if item.get("source") == DOMAIN and item["statistic_id"].startswith(prefix)
    ]


def response_has_cumulative_series(response: UsageResponse) -> bool:
    """True when [response] carries a cumulative meter register we now exclude from statistics.

    The signal that an entry's *stored* statistics may be corrupt: before the fix for issue #6,
    a register series like this was summed into the consumption statistic (and its ``cost=0``
    placeholder hijacked cost selection, issue #7). An entry whose feed contains one, and which
    imported under the old logic, needs its statistics rebuilt — see
    [coordinator.GreenButtonCoordinator._async_migrate_import].
    """
    return any(
        not _is_interval_consumption_series(s) for up in response.usage_points for s in up.series
    )


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
    """Push every interval-consumption series in [response] into HA long-term statistics.

    Cumulative meter registers are excluded — see [_is_interval_consumption_series].

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
        imported_any = False
        for series in up.series:
            if not _is_interval_consumption_series(series):
                _warn_once(
                    f"{entry.entry_id}:{up.usage_point_id}:{series.meter_reading_id}:cumulative",
                    "Skipping meter reading %s on usage point %s: accumulation behaviour %s is a "
                    "cumulative meter register, not per-interval consumption — adding its values "
                    "to the usage statistic would report the whole meter total as one interval's "
                    "consumption",
                    series.meter_reading_id,
                    up.usage_point_id,
                    series.reading_type.accumulation_behaviour,
                )
                continue
            imported_any = True
            await _import_series(hass, entry, up, series, utility_display_name, fresh=fresh)
        if up.series and not imported_any:
            # Every series was a cumulative register. Deriving deltas from a register is
            # possible in principle but isn't implemented, so this usage point contributes no
            # energy at all — loud, because the symptom is an empty Energy dashboard.
            _LOGGER.error(
                "Usage point %s has no per-interval consumption series — every one of its %d "
                "series is a cumulative meter register (%s). No energy statistics will be "
                "written for it; please report this feed at %s",
                up.usage_point_id,
                len(up.series),
                ", ".join(sorted({s.reading_type.accumulation_behaviour for s in up.series})),
                "https://github.com/rocketraman/open-green-button-homeassistant/issues",
            )

    # Cost is written after usage, in a second pass. A monthly UsageSummary arrives long after its
    # billing period (Burlington publishes it ~2-3 weeks later), so the period's usage is NOT in
    # this response — it's already in the recorder. [_import_cost_summaries] reads it back to
    # distribute the bill, so the usage writes above must be committed first. Block once here; on a
    # fresh rebuild the period's usage was written moments ago and would otherwise not be visible.
    needs_recorder_flush = any(
        not _has_interval_cost(up)
        and (up.summaries or _load_tiered_estimate_state(entry, up.usage_point_id) is not None)
        for up in response.usage_points
    )

    if needs_recorder_flush and hass.state is not CoreState.running:
        # DEADLOCK GUARD — do not await the recorder before HA has started.
        #
        # `Recorder._run()` blocks in `_wait_startup_or_shutdown()` until HOMEASSISTANT_STARTED
        # and only then enters `_run_event_loop()`, which is what drains the queue. Our
        # `async_block_till_done()` queues a SynchronizeTask and awaits it, so before start it can
        # never complete. HA in turn doesn't fire STARTED until config-entry setup returns — and
        # this runs inside `async_config_entry_first_refresh()`. That's a genuine deadlock, broken
        # only by HA's SLOW_SETUP_MAX_WAIT (300s) cancelling the setup task:
        #   "Setup of config entry '<title>' for greenbutton integration cancelled"
        # which leaves the entry in SETUP_ERROR with no retry until the next restart.
        #
        # So defer the whole cost pass to just after start instead, where the block is safe.
        # `async_at_started` fires on STARTED (or immediately if we somehow race into `running`),
        # and its unsubscribe is tied to the entry so an unload cancels a still-pending pass.
        async def _deferred_cost_pass(_hass: HomeAssistant) -> None:
            _LOGGER.debug(
                "Running deferred cost import for entry %s (HA has started)", entry.entry_id
            )
            await get_instance(hass).async_block_till_done()
            await _import_costs(hass, entry, response, utility_display_name, fresh=fresh)

        _LOGGER.debug(
            "HA is %s, not running — deferring cost import for entry %s until after startup",
            hass.state,
            entry.entry_id,
        )
        entry.async_on_unload(async_at_started(hass, _deferred_cost_pass))
        return

    if needs_recorder_flush:
        await get_instance(hass).async_block_till_done()
    await _import_costs(hass, entry, response, utility_display_name, fresh=fresh)


async def _import_costs(
    hass: HomeAssistant,
    entry: ConfigEntry,
    response: UsageResponse,
    utility_display_name: str,
    *,
    fresh: bool = False,
) -> None:
    """Second-pass cost import for every usage point in [response].

    Split out of [import_usage_statistics] so it can also run deferred, after
    EVENT_HOMEASSISTANT_STARTED — see the deadlock guard there. Assumes the usage writes it
    depends on have already been flushed to the recorder by the caller.
    """
    for up in response.usage_points:
        # Prefer per-interval <cost> — utilities like savagedata/Milton itemize actual hourly cost,
        # which is more accurate and is self-contained on the reading. Fall back to distributing a
        # monthly UsageSummary total over the period's recorded usage for utilities (Burlington)
        # that only bill via a summary.
        if _has_interval_cost(up):
            await _import_cost_from_readings(hass, entry, up, utility_display_name, fresh=fresh)
        elif _tiered_estimates_supported(entry):
            await _import_cost_summaries_with_estimates(
                hass, entry, up, utility_display_name, fresh=fresh
            )
        else:
            await _import_cost_summaries(hass, entry, up, utility_display_name, fresh=fresh)


# ESPI AccumulationKind values whose readings are a running meter register (a total since the
# meter was installed / last reset), NOT the quantity consumed during the interval. Named per
# [espi._accumulation], which maps the full NAESB enum so nothing cumulative hides in "OTHER".
#
# Deliberately a *blacklist*: an accumulation behaviour we don't recognize keeps importing as it
# always has. The whitelist alternative ("import only DELTA_DATA") silently drops any series whose
# behaviour is missing, unmapped, or merely unusual — an empty Energy dashboard with nothing above
# DEBUG to explain it. `INDICATING` and `LATCHING_QUANTITY` are arguably register-like too, but no
# feed in scope emits them and misclassifying them would drop real data; revisit with a real
# sample.
_CUMULATIVE_ACCUMULATION = frozenset(
    {
        "BULK_QUANTITY",  # ESPI 1 — the daily register snapshot Milton Hydro publishes
        "CONTINUOUS_CUMULATIVE",  # ESPI 2
        "CUMULATIVE",  # ESPI 3
    }
)

# Keys already logged at WARNING by [_warn_once], so a permanent condition doesn't repeat the
# warning on every poll. Module-level (not per-entry) and never pruned: it holds a handful of
# short strings for the lifetime of the process, and a full HA restart re-arms every warning.
_WARNED_ONCE: set[str] = set()


def _warn_once(key: str, msg: str, *args: object) -> None:
    """Log at WARNING the first time [key] is seen this run, at DEBUG every time after.

    Skipping a series is a data-loss event and has to be visible — DEBUG-only was how the
    unit-mapping skip stayed invisible. But the conditions we skip on are properties of the
    utility's feed, so they recur on every poll; warning each time would be log spam for the
    rest of the entry's life. First one loud, the rest quiet.
    """
    if key in _WARNED_ONCE:
        _LOGGER.debug(msg, *args)
        return
    _WARNED_ONCE.add(key)
    _LOGGER.warning(msg, *args)


def _is_interval_consumption_series(series: MeterReadingSeries) -> bool:
    """True when a series' readings are per-interval quantities we can sum into a statistic.

    False for cumulative meter registers. Milton Hydro publishes an hourly ``DELTA_DATA``
    consumption series *and* a daily ``BULK_QUANTITY`` register snapshot for the same meter,
    both FORWARD — so both map to one [statistic_id_for_series] and the register's
    meter-lifetime total was being added to the hourly running sum, reporting an enormous
    false spike (issue #6).
    """
    return series.reading_type.accumulation_behaviour not in _CUMULATIVE_ACCUMULATION


def _forward_interval_series(up: UsagePoint) -> list[MeterReadingSeries]:
    """The FORWARD per-interval consumption series on this UsagePoint — the basis for cost.

    Cost is about what was consumed, so REVERSE (solar export) is out, and so are cumulative
    registers: they aren't billed intervals, and Milton's carries a ``cost=0`` placeholder that
    used to masquerade as real per-interval pricing (issue #7).
    """
    return [
        s
        for s in up.series
        if s.reading_type.flow_direction == "FORWARD" and _is_interval_consumption_series(s)
    ]


def _has_interval_cost(up: UsagePoint) -> bool:
    """True when this UsagePoint's interval-consumption readings carry per-interval cost.

    Scoped to [_forward_interval_series]. Milton Hydro attaches ``cost=0`` to its daily
    ``BULK_QUANTITY`` register while billing through ``UsageSummary``; testing every FORWARD
    reading let that placeholder select the per-interval path and write an all-zero cost
    statistic, suppressing the real (non-zero) summary entirely (issue #7).

    Deliberately still ``cost is not None`` rather than ``cost != 0``: a series that genuinely
    itemizes cost may have legitimately zero hours, and because this decision is remade on every
    poll, a "must be non-zero" test would flip a trailing all-zero window onto the summary path
    and mix summary-distributed rows into a per-interval cost statistic. Restricting *which*
    series are consulted fixes #7 on its own; see the issue for the series-level persistence
    that would make the choice stable across polls.
    """
    return any(r.cost is not None for s in _forward_interval_series(up) for r in s.readings)


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
        _warn_once(
            f"{statistic_id}:{series.reading_type.unit_of_measure}:no-unit",
            "Skipping series %s: no HA unit mapping for %s/%s — its readings will not appear "
            "in the Energy dashboard",
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

    by_hour, covered_seconds = _hourly_totals(series)
    _drop_incomplete_trailing_hour(by_hour, covered_seconds, statistic_id)

    stats: list[StatisticData] = []
    running = resume_from_sum
    for hour in sorted(by_hour):
        # Stale-window guard — HA's statistics machinery already deduplicates on
        # (statistic_id, start), but skipping locally avoids resetting `running` from
        # readings already accounted for in the stored cumulative sum. Compare the *aligned*
        # hour, because that's the granularity the stored row (and hence `_resume_point`) is
        # at: a raw sub-hourly reading start at :15 is > the hour's stored start, so a raw
        # comparison would wave through readings whose hour is already in the sum and add
        # them on top of it, inflating that hour and every hour after it.
        if resume_after_epoch is not None and hour.timestamp() <= resume_after_epoch:
            continue
        running += by_hour[hour]
        stats.append(StatisticData(start=hour, state=running, sum=running))

    if not stats:
        return

    _LOGGER.info(
        "Importing %d statistic rows for %s (resume_from_sum=%.3f)",
        len(stats),
        statistic_id,
        resume_from_sum,
    )
    async_add_external_statistics(hass, metadata, stats)


def _hourly_totals(series: MeterReadingSeries) -> tuple[dict[datetime, float], dict[datetime, int]]:
    """Fold a series' readings into ``(kwh_by_hour, covered_seconds_by_hour)``.

    Aggregating to the hour *before* accumulating is load-bearing for any utility whose feed
    uses a sub-hourly ``intervalLength`` (15 or 30 minutes — none in scope today, but the ESPI
    schema allows it and nothing upstream rejects it). One StatisticData row per reading would
    emit four rows sharing the same hour-aligned ``start``; HA upserts on
    (statistic_id, start), so three of the four are silently discarded and only the last
    reading's cumulative total survives. That happens to land on the right number within a
    single import, but it leaves the stored row's ``start`` at the hour boundary, which is what
    [_resume_point] reads back — and the sub-hour readings inside that already-imported hour
    then sail past a raw stale-window comparison on the next poll and get added a second time.

    Summing per hour here makes each hour exactly one row, so the row we write and the cursor
    we later resume from describe the same unit of time.
    """
    by_hour: dict[datetime, float] = {}
    covered_seconds: dict[datetime, int] = {}
    for reading in series.readings:
        hour = _align_to_hour(reading.start)
        by_hour[hour] = by_hour.get(hour, 0.0) + _to_ha_units(reading.value, series.reading_type)
        covered_seconds[hour] = covered_seconds.get(hour, 0) + reading.duration_seconds
    return by_hour, covered_seconds


def _drop_incomplete_trailing_hour(
    by_hour: dict[datetime, float],
    covered_seconds: dict[datetime, int],
    statistic_id: str,
) -> None:
    """Remove the newest hour from [by_hour] when the feed only covers part of it.

    The cumulative-sum model can't revise an hour once written: the resume point is a single
    (sum, start) pair, so re-stating an earlier hour would mean rewriting every later row. With
    a sub-hourly feed a poll routinely lands mid-hour — writing that half-covered hour would
    freeze it at half its real consumption, since the aligned stale-window guard correctly
    refuses to add its remaining intervals on the next poll.

    So hold the partial hour back instead and let a later poll import it whole. Only the
    *trailing* hour is deferred; a mid-series hour short of 3600s is a genuine gap in the feed
    and is imported as-is. An hour with no duration information at all (0s covered) is left
    alone rather than deferred forever. Hourly feeds — every utility in scope today — cover a
    full 3600s per hour and never trip this.
    """
    if not by_hour:
        return
    last_hour = max(by_hour)
    covered = covered_seconds.get(last_hour, 0)
    if 0 < covered < 3600:
        _LOGGER.debug(
            "Deferring partial hour %s for %s (%ds of 3600 covered) until the feed completes it",
            last_hour.isoformat(),
            statistic_id,
            covered,
        )
        del by_hour[last_hour]


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


async def _forward_hours_for_cost(
    hass: HomeAssistant,
    entry: ConfigEntry,
    up: UsagePoint,
    period_start: datetime,
    period_end: datetime,
) -> list[tuple[datetime, float]]:
    """Return recorded usage plus FORWARD hours delivered by the current response.

    Cost estimation normally reads the cumulative usage statistic back from the recorder so it
    can price the entire open billing period.  The newest readings, however, are queued for
    recorder import earlier in the same coordinator update.  On a real recorder those rows can
    still be absent from a concurrent read even after the queue synchronization point, leaving
    cost one poll behind usage.  Six-hour polling hid that lag; daily polling leaves yesterday's
    cost incomplete until the next morning.

    Merge the normalized interval readings already in this response over the recorder result.
    The recorder remains the source for older hours outside the rolling response window, while
    response values make the current poll self-contained and override an overlapping stored row.
    """
    recorded = await _recorded_forward_hours(hass, entry, up, period_start, period_end)
    response_by_hour: dict[datetime, float] = {}
    stat_id = statistic_id_for_series(entry.entry_id, up.usage_point_id, "FORWARD")
    for series in _forward_interval_series(up):
        by_hour, covered_seconds = _hourly_totals(series)
        _drop_incomplete_trailing_hour(by_hour, covered_seconds, stat_id)
        for hour, kwh in by_hour.items():
            if period_start <= hour < period_end:
                response_by_hour[hour] = response_by_hour.get(hour, 0.0) + kwh

    merged = dict(recorded)
    merged.update(response_by_hour)
    return sorted(merged.items())


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

    Skipped when the UsagePoint has no summaries (most utilities only attach UsageSummary
    to accounts they bill; meter-only test profiles often won't), or when the currency code
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

    stats: list[StatisticData] = []
    running = resume_from_sum
    for summary in _select_billing_summaries(up.summaries):
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
            if resume_after_epoch is not None and hour_start.timestamp() <= resume_after_epoch:
                continue
            running += cost_at_hour[hour_start]
            stats.append(StatisticData(start=hour_start, state=running, sum=running))

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


async def _import_cost_summaries_with_estimates(
    hass: HomeAssistant,
    entry: ConfigEntry,
    up: UsagePoint,
    utility_display_name: str,
    *,
    fresh: bool = False,
) -> None:
    """Write exact bills plus a bounded, Milton-only provisional open period."""
    saved_state = _load_tiered_estimate_state(entry, up.usage_point_id)
    if not up.summaries and saved_state is None:
        return
    currency_alpha = (
        _iso_4217_alpha(up.summaries[0].currency_numeric_code)
        if up.summaries
        else saved_state.currency_alpha
    )
    if currency_alpha is None:
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
        "unit_class": None,
    }
    if _MEAN_TYPE_NONE is not None:
        metadata["mean_type"] = _MEAN_TYPE_NONE

    resume_from_sum, resume_after_epoch = (
        (0.0, None) if fresh else await _resume_point(hass, statistic_id)
    )
    selected = _select_billing_summaries(up.summaries)
    stats: list[StatisticData] = []
    running = resume_from_sum
    rewrite_exact = False
    summaries_to_write = selected

    # Replace every contiguous estimated bill that has closed since the saved boundary. This
    # handles two bills arriving between polls instead of correcting only the newest one.
    if saved_state is not None and selected and not fresh:
        contiguous: list[BillingSummary] = []
        expected_start = saved_state.active_period_start
        for summary in selected:
            if summary.billing_period_start < expected_start:
                continue
            if summary.billing_period_start != expected_start:
                break
            contiguous.append(summary)
            expected_start = summary.billing_period_start + timedelta(
                seconds=summary.billing_period_duration_seconds
            )
        if contiguous:
            summaries_to_write = contiguous
            running = (
                saved_state.baseline_sum
                if saved_state.baseline_sum is not None
                else await _cost_sum_before(hass, statistic_id, saved_state.active_period_start)
            )
            rewrite_exact = True
        else:
            summaries_to_write = []

    last_written_summary: BillingSummary | None = None
    last_written_hours: list[tuple[datetime, float]] = []
    for summary in summaries_to_write:
        if summary.total_cost == 0:
            continue
        period_start = summary.billing_period_start
        period_end = period_start + timedelta(seconds=summary.billing_period_duration_seconds)
        in_period = await _recorded_forward_hours(hass, entry, up, period_start, period_end)
        total_period_kwh = sum(kwh for _hour, kwh in in_period)
        if total_period_kwh <= 0:
            continue
        cost_at_hour = _cost_distribution_for_period(summary, in_period, total_period_kwh)
        for hour_start, _kwh in in_period:
            if (
                not rewrite_exact
                and resume_after_epoch is not None
                and hour_start.timestamp() <= resume_after_epoch
            ):
                continue
            running += cost_at_hour[hour_start]
            stats.append(StatisticData(start=hour_start, state=running, sum=running))
        last_written_summary = summary
        last_written_hours = in_period

    estimate_state = saved_state
    if last_written_summary is not None and _tiered_estimates_supported(entry):
        profile = _tiered_estimate_profile(last_written_summary, last_written_hours)
        if profile is not None:
            active_period_start = last_written_summary.billing_period_start + timedelta(
                seconds=last_written_summary.billing_period_duration_seconds
            )
            estimate_state = _TieredEstimateState(
                profile=profile,
                active_period_start=active_period_start,
                predicted_days=_predicted_billing_days(selected, active_period_start),
                currency_alpha=currency_alpha,
                baseline_sum=running,
            )
            _store_tiered_estimate_state(hass, entry, up.usage_point_id, estimate_state)
        else:
            estimate_state = None
            _clear_tiered_estimate_state(hass, entry, up.usage_point_id)

    if estimate_state is not None:
        latest_forward_hour = _latest_forward_hour(up)
        predicted_period_end = estimate_state.period_end
        estimate_end = estimate_state.grace_end
        if (
            latest_forward_hour is not None
            and latest_forward_hour >= estimate_state.active_period_start
        ):
            period_end = min(latest_forward_hour + timedelta(hours=1), estimate_end)
            open_hours = await _forward_hours_for_cost(
                hass,
                entry,
                up,
                estimate_state.active_period_start,
                period_end,
            )
            cost_at_hour = _tiered_estimated_costs_with_provisional_rollover(
                open_hours,
                estimate_state.profile,
                estimate_state.predicted_days,
                predicted_period_end,
            )
            append_after_epoch = None if rewrite_exact or fresh else resume_after_epoch
            if not rewrite_exact and not fresh:
                running = resume_from_sum
            for hour_start, _kwh in open_hours:
                if append_after_epoch is not None and hour_start.timestamp() <= append_after_epoch:
                    continue
                running += cost_at_hour[hour_start]
                stats.append(StatisticData(start=hour_start, state=running, sum=running))
            latest_forward_end = latest_forward_hour + timedelta(hours=1)
            if latest_forward_end > estimate_end:
                _warn_once(
                    f"{entry.entry_id}:{up.usage_point_id}:stale-tier-estimate",
                    "Tiered cost estimate for usage point %s stopped after the %d-day "
                    "UsageSummary grace period at %s",
                    up.usage_point_id,
                    _TIERED_ESTIMATE_GRACE_DAYS,
                    estimate_end.isoformat(),
                )
            elif latest_forward_end > predicted_period_end:
                _warn_once(
                    f"{entry.entry_id}:{up.usage_point_id}:delayed-tier-summary",
                    "UsageSummary for usage point %s is delayed past predicted billing-period "
                    "end %s; provisional next-period costs will continue through %s",
                    up.usage_point_id,
                    predicted_period_end.isoformat(),
                    estimate_end.isoformat(),
                )

    if not stats:
        return
    _LOGGER.info(
        "Importing %d exact/provisional cost rows for %s in %s",
        len(stats),
        statistic_id,
        currency_alpha,
    )
    async_add_external_statistics(hass, metadata, stats)


async def _cost_sum_before(
    hass: HomeAssistant,
    statistic_id: str,
    before: datetime,
) -> float:
    """Find the last earlier cumulative cost, progressively widening for legacy state."""
    for lookback in (
        timedelta(days=2),
        timedelta(days=45),
        timedelta(days=400),
        timedelta(days=3650),
    ):
        by_id = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            before - lookback,
            before,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        candidates: list[tuple[float, float]] = []
        for row in by_id.get(statistic_id, []):
            total = row.get("sum")
            start = row.get("start")
            if total is None or start is None:
                continue
            epoch = start.timestamp() if isinstance(start, datetime) else float(start)
            if epoch < before.timestamp():
                candidates.append((epoch, float(total)))
        if candidates:
            return max(candidates)[1]
    return 0.0


def _latest_forward_hour(up: UsagePoint) -> datetime | None:
    """Newest hour-aligned FORWARD interval-consumption reading in this response."""
    return max(
        (
            _align_to_hour(reading.start)
            for series in _forward_interval_series(up)
            for reading in series.readings
        ),
        default=None,
    )


def _tiered_estimate_profile(
    summary: BillingSummary,
    in_period: list[tuple[datetime, float]],
) -> _TieredEstimateProfile | None:
    """Infer rates from Milton's verified Ontario Block/Tier summary labels."""
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

    days = round(summary.billing_period_duration_seconds / 86400)
    if days <= 0 or tier_cost[1] <= 0 or tier_cost[2] <= 0 or len(seasons) != 1:
        return None
    tier_one_kwh_per_day = 20.0 if "summer" in seasons else 1000.0 / 30.0
    tier_one_kwh = tier_one_kwh_per_day * days
    total_kwh = sum(kwh for _hour, kwh in in_period)
    tier_two_kwh = total_kwh - tier_one_kwh
    if tier_two_kwh <= 0 or total_kwh <= 0:
        return None
    profile = _TieredEstimateProfile(
        tier_one_rate=tier_cost[1] / tier_one_kwh,
        tier_two_rate=tier_cost[2] / tier_two_kwh,
        tier_one_kwh_per_day=tier_one_kwh_per_day,
        residual_rate=(summary.total_cost - tier_cost[1] - tier_cost[2]) / total_kwh,
    )
    probe = _TieredEstimateState(
        profile=profile,
        active_period_start=summary.billing_period_start,
        predicted_days=float(days),
        currency_alpha="CAD",
        baseline_sum=0.0,
    )
    return profile if _is_valid_tiered_estimate_state(probe) else None


def _predicted_billing_days(
    summaries: list[BillingSummary],
    open_period_start: datetime,
) -> float:
    """Predict a read-cycle length from last year's matching cycle or recent median."""
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
    """Price open-period hours, splitting the tier-threshold crossing hour."""
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


def _tiered_estimated_costs_with_provisional_rollover(
    hours: list[tuple[datetime, float]],
    profile: _TieredEstimateProfile,
    predicted_days: float,
    predicted_period_end: datetime,
) -> dict[datetime, float]:
    """Price a delayed-summary grace window with one provisional tier reset.

    Milton can publish interval usage for several days after the predicted billing boundary
    before it publishes the exact UsageSummary. Treat those hours as a provisional next period
    so the Energy dashboard does not go to zero and the Tier 1 allowance resets at the predicted
    boundary. When the exact summary arrives, the normal contiguous rewrite replaces both the
    completed period and these provisional next-period rows from the actual boundary.
    """
    active_period_hours = [item for item in hours if item[0] < predicted_period_end]
    provisional_next_period_hours = [item for item in hours if item[0] >= predicted_period_end]
    return {
        **_tiered_estimated_costs(active_period_hours, profile, predicted_days),
        **_tiered_estimated_costs(provisional_next_period_hours, profile, predicted_days),
    }


async def _import_cost_from_readings(
    hass: HomeAssistant,
    entry: ConfigEntry,
    up: UsagePoint,
    utility_display_name: str,
    *,
    fresh: bool = False,
) -> None:
    """Write a cumulative-cost statistic from per-interval `<cost>` on the FORWARD readings.

    Utilities like savagedata itemize the actual cost on every IntervalReading — more accurate
    and finer-grained than distributing a monthly UsageSummary total (and it works when the
    summary only carries an "Amount Due" subtotal, which our summary path deliberately drops).
    Costs are summed per hour across the UsagePoint's FORWARD interval-consumption series
    (multiple meters roll up into one bill), then accumulated into the same cost stat the Energy
    dashboard reads. Cumulative registers are excluded — see [_forward_interval_series].
    """
    forward_series = _forward_interval_series(up)
    currency_code = next(
        (
            s.reading_type.currency_numeric_code
            for s in forward_series
            if s.reading_type.currency_numeric_code is not None
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
    for series in forward_series:
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
