"""Tests for the statistics helper — id format invariants + unit conversion.

The full async_add_external_statistics path is exercised by the coordinator-level tests
where a recorder is wired up; these focus on the pure-function bits that don't need HA.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.greenbutton.api import (
    BillingSummary,
    MeterReadingSeries,
    NormalizedReadingType,
    UsagePoint,
    UsageReading,
    UsageResponse,
)
from custom_components.greenbutton.const import DOMAIN
from custom_components.greenbutton.statistics import (
    _recorded_forward_hours,
    _select_billing_summaries,
    import_usage_statistics,
    statistic_id_for_series,
    statistic_id_prefix_for_entry,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_DAY = 86400


async def test_recorded_forward_hours_reconstructs_hourly_kwh(hass: HomeAssistant) -> None:
    """`_recorded_forward_hours` reads the FORWARD usage stat back and diffs it into per-hour kWh.

    Round-trips a real recorder (not mocked) to validate the statistics_during_period call shape
    and the cumulative-sum → per-hour delta reconstruction the summary-cost path relies on.
    """
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    up = UsagePoint(usage_point_id="up1", service_kind="electricity", series=[])
    stat_id = statistic_id_for_series("01TESTENTRY", "up1", "FORWARD")
    base = datetime(2026, 4, 3, 4, 0, tzinfo=UTC)
    metadata = {
        "has_mean": False,
        "has_sum": True,
        "name": "test usage",
        "source": DOMAIN,
        "statistic_id": stat_id,
        "unit_of_measurement": "kWh",
        "unit_class": "energy",
        "mean_type": StatisticMeanType.NONE,
    }
    # Cumulative sums 0, 2, 5, 6 → per-hour deltas 2, 3, 1 for the three hours after `base`.
    async_add_external_statistics(
        hass,
        metadata,
        [
            {"start": base + timedelta(hours=i), "state": s, "sum": s}
            for i, s in enumerate((0.0, 2.0, 5.0, 6.0))
        ],
    )
    await async_wait_recording_done(hass)

    hours = await _recorded_forward_hours(
        hass, entry, up, base + timedelta(hours=1), base + timedelta(hours=4)
    )
    assert [(h.hour, round(k, 1)) for h, k in hours] == [(5, 2.0), (6, 3.0), (7, 1.0)]


def _summary(start: datetime, duration_days: int, total_dollars: float) -> BillingSummary:
    """Build a BillingSummary whose total_cost is `total_dollars` (via billLastPeriod)."""
    return BillingSummary(
        billing_period_start=start,
        billing_period_duration_seconds=duration_days * _DAY,
        bill_last_period_raw=round(total_dollars * 100_000),
        cost_additional_last_period_raw=0,
        cost_details=[],
        currency_numeric_code=124,
    )


_APR2 = datetime(2026, 4, 2, tzinfo=UTC)
_MAY4 = datetime(2026, 5, 4, tzinfo=UTC)


def _one_reading_response() -> UsageResponse:
    """A response with a single FORWARD electricity reading, enough to drive an import."""
    reading_type = NormalizedReadingType(
        commodity="ELECTRICITY_SECONDARY_METERED",
        flow_direction="FORWARD",
        accumulation_behaviour="DELTA_DATA",
        interval_length_seconds=3600,
        unit_of_measure="WATT_HOURS",
        unit_of_measure_symbol="Wh",
        power_of_ten_multiplier=0,
        currency_numeric_code=124,
    )
    series = MeterReadingSeries(
        meter_reading_id="mr1",
        reading_type=reading_type,
        readings=[
            UsageReading(
                start=datetime(2026, 7, 5, 5, tzinfo=UTC), duration_seconds=3600, value=1000.0
            )
        ],
    )
    up = UsagePoint(usage_point_id="up1", service_kind="electricity", series=[series])
    return UsageResponse(updated=None, usage_points=[up], new_credentials=None)


async def test_fresh_import_bypasses_resume_point(hass: HomeAssistant) -> None:
    """`fresh=True` must NOT read the resume point — that read is the rebuild race.

    Regression guard: a rebuild clears the store then re-imports the full-history feed. If the
    resume-point read observed the pre-clear cursor, every reading looked "already imported"
    and got skipped, importing nothing. `fresh=True` skips the read and imports from zero.
    """
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    with (
        patch(
            "custom_components.greenbutton.statistics._resume_point", new=AsyncMock()
        ) as resume_mock,
        patch("custom_components.greenbutton.statistics.async_add_external_statistics") as add_mock,
    ):
        await import_usage_statistics(
            hass, entry, _one_reading_response(), utility_display_name="X", fresh=True
        )

    resume_mock.assert_not_awaited()  # the racy read is skipped entirely
    add_mock.assert_called_once()  # and the reading is still written
    _metadata, stats = add_mock.call_args.args[1], add_mock.call_args.args[2]
    assert len(stats) == 1


async def test_incremental_import_reads_resume_point(hass: HomeAssistant) -> None:
    """The normal (non-fresh) poll path still resumes from stored totals."""
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    with (
        patch(
            "custom_components.greenbutton.statistics._resume_point",
            new=AsyncMock(return_value=(0.0, None)),
        ) as resume_mock,
        patch("custom_components.greenbutton.statistics.async_add_external_statistics"),
    ):
        await import_usage_statistics(
            hass, entry, _one_reading_response(), utility_display_name="X"
        )

    resume_mock.assert_awaited()  # incremental imports must still read the resume point


def _sub_hourly_response(hours: range, interval_seconds: int = 900) -> UsageResponse:
    """A FORWARD electricity response on a sub-hourly `intervalLength`.

    Each hour in [hours] is split into `3600 // interval_seconds` readings of 250 Wh, so every
    hour totals exactly 1 kWh no matter the interval length.
    """
    per_hour = 3600 // interval_seconds
    reading_type = NormalizedReadingType(
        commodity="ELECTRICITY_SECONDARY_METERED",
        flow_direction="FORWARD",
        accumulation_behaviour="DELTA_DATA",
        interval_length_seconds=interval_seconds,
        unit_of_measure="WATT_HOURS",
        unit_of_measure_symbol="Wh",
        power_of_ten_multiplier=0,
        currency_numeric_code=124,
    )
    series = MeterReadingSeries(
        meter_reading_id="mr1",
        reading_type=reading_type,
        readings=[
            UsageReading(
                start=datetime(2026, 7, 5, h, tzinfo=UTC) + timedelta(seconds=i * interval_seconds),
                duration_seconds=interval_seconds,
                value=1000.0 / per_hour,
            )
            for h in hours
            for i in range(per_hour)
        ],
    )
    up = UsagePoint(usage_point_id="up1", service_kind="electricity", series=[series])
    return UsageResponse(updated=None, usage_points=[up], new_credentials=None)


async def _recorded_sums(hass: HomeAssistant, stat_id: str) -> list[tuple[int, float]]:
    """Read a usage statistic back out of the recorder as ``[(hour, cumulative_sum), ...]``."""
    by_id = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 7, 5, tzinfo=UTC),
        datetime(2026, 7, 6, tzinfo=UTC),
        {stat_id},
        "hour",
        None,
        {"sum"},
    )
    return [
        (datetime.fromtimestamp(row["start"], tz=UTC).hour, round(row["sum"], 3))
        for row in by_id.get(stat_id, [])
    ]


async def test_sub_hourly_series_is_not_double_counted_across_polls(hass: HomeAssistant) -> None:
    """A 15-minute-interval feed imported twice must not inflate the cumulative sum.

    Regression guard for a latent double-count: `_align_to_hour` floors all four readings in an
    hour to the same `start`, so one row per reading collides on (statistic_id, start) and only
    the last survives HA's upsert — which looks right on a single import, but leaves the stored
    row's start at the hour boundary. `_resume_point` returns that boundary, so a stale-window
    guard comparing the *raw* reading start would wave the :15/:30/:45 readings of an
    already-imported hour straight through on the next poll and add them on top of the resumed
    sum, inflating that hour and every hour after it.

    Two polls over overlapping windows, against a real recorder, must leave 1 kWh per hour.
    """
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    stat_id = statistic_id_for_series(entry.entry_id, "up1", "FORWARD")

    # Poll 1: hours 05 and 06 (1 kWh each, as four 250 Wh quarter-hour readings).
    await import_usage_statistics(
        hass, entry, _sub_hourly_response(range(5, 7)), utility_display_name="X"
    )
    await async_wait_recording_done(hass)
    assert await _recorded_sums(hass, stat_id) == [(5, 1.0), (6, 2.0)]

    # Poll 2: the same two hours again (the fetch window overlaps by design) plus hour 07.
    await import_usage_statistics(
        hass, entry, _sub_hourly_response(range(5, 8)), utility_display_name="X"
    )
    await async_wait_recording_done(hass)
    assert await _recorded_sums(hass, stat_id) == [(5, 1.0), (6, 2.0), (7, 3.0)]


async def test_sub_hourly_readings_are_summed_into_one_row_per_hour(hass: HomeAssistant) -> None:
    """Four quarter-hour readings become a single StatisticData row carrying the whole hour."""
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    with (
        patch(
            "custom_components.greenbutton.statistics._resume_point",
            new=AsyncMock(return_value=(0.0, None)),
        ),
        patch("custom_components.greenbutton.statistics.async_add_external_statistics") as add_mock,
    ):
        await import_usage_statistics(
            hass, entry, _sub_hourly_response(range(5, 7)), utility_display_name="X"
        )

    stats = add_mock.call_args.args[2]
    assert [(s["start"].hour, round(s["sum"], 3)) for s in stats] == [(5, 1.0), (6, 2.0)]


async def test_partial_trailing_hour_is_deferred_then_imported_whole(hass: HomeAssistant) -> None:
    """A poll landing mid-hour holds that hour back rather than freezing it at half a total.

    The resume point is a single (sum, start) pair, so an hour can't be revised once written —
    importing a half-covered hour would permanently under-count it. Defer it instead; the next
    poll carries the full hour and imports it whole.
    """
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    stat_id = statistic_id_for_series(entry.entry_id, "up1", "FORWARD")

    # Poll 1 lands mid-hour: hour 05 complete, hour 06 only half published.
    partial = _sub_hourly_response(range(5, 7))
    series = partial.usage_points[0].series[0]
    truncated = MeterReadingSeries(
        meter_reading_id=series.meter_reading_id,
        reading_type=series.reading_type,
        readings=series.readings[:6],  # 4 readings for hour 05, 2 for hour 06
    )
    up = UsagePoint(usage_point_id="up1", service_kind="electricity", series=[truncated])
    await import_usage_statistics(
        hass,
        entry,
        UsageResponse(updated=None, usage_points=[up], new_credentials=None),
        utility_display_name="X",
    )
    await async_wait_recording_done(hass)
    assert await _recorded_sums(hass, stat_id) == [(5, 1.0)]  # hour 06 held back, not halved

    # Poll 2 carries hour 06 complete — it lands at its full 1 kWh.
    await import_usage_statistics(
        hass, entry, _sub_hourly_response(range(5, 8)), utility_display_name="X"
    )
    await async_wait_recording_done(hass)
    assert await _recorded_sums(hass, stat_id) == [(5, 1.0), (6, 2.0), (7, 3.0)]


def _per_interval_cost_response() -> UsageResponse:
    """A FORWARD electricity response where each reading itemizes a per-interval cost (Milton)."""
    reading_type = NormalizedReadingType(
        commodity="ELECTRICITY_SECONDARY_METERED",
        flow_direction="FORWARD",
        accumulation_behaviour="DELTA_DATA",
        interval_length_seconds=3600,
        unit_of_measure="WATT_HOURS",
        unit_of_measure_symbol="Wh",
        power_of_ten_multiplier=0,
        currency_numeric_code=124,
    )
    series = MeterReadingSeries(
        meter_reading_id="mr1",
        reading_type=reading_type,
        readings=[
            UsageReading(datetime(2026, 7, 5, 5, tzinfo=UTC), 3600, 1000.0, cost=0.087),
            UsageReading(datetime(2026, 7, 5, 6, tzinfo=UTC), 3600, 1500.0, cost=0.122),
        ],
    )
    up = UsagePoint(usage_point_id="up1", service_kind="electricity", series=[series])
    return UsageResponse(updated=None, usage_points=[up], new_credentials=None)


async def test_per_interval_cost_writes_cumulative_cost_stat(hass: HomeAssistant) -> None:
    """Readings with per-interval <cost> drive a cumulative cost stat directly (no summary)."""
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    with (
        patch(
            "custom_components.greenbutton.statistics._resume_point",
            new=AsyncMock(return_value=(0.0, None)),
        ),
        patch("custom_components.greenbutton.statistics.async_add_external_statistics") as add_mock,
    ):
        await import_usage_statistics(
            hass, entry, _per_interval_cost_response(), utility_display_name="X"
        )

    cost_calls = [c for c in add_mock.call_args_list if c.args[1]["statistic_id"].endswith("_cost")]
    assert len(cost_calls) == 1
    metadata, stats = cost_calls[0].args[1], cost_calls[0].args[2]
    assert metadata["unit_of_measurement"] == "CAD"
    assert [round(s["sum"], 3) for s in stats] == [0.087, 0.209]  # cumulative


def _summary_only_response() -> UsageResponse:
    """A monthly UsageSummary with NO per-interval <cost> and NO readings in the response.

    This is the Burlington shape *as an incremental poll sees it*: the summary is published weeks
    after its period, so the period's readings aren't here — they're already in the recorder, and
    the importer reads them back to distribute the bill.
    """
    up = UsagePoint(
        usage_point_id="up1",
        service_kind="electricity",
        series=[],
        summaries=[_summary(datetime(2026, 4, 1, tzinfo=UTC), 30, total_dollars=40.0)],
    )
    return UsageResponse(updated=None, usage_points=[up], new_credentials=None)


async def test_summary_cost_distributes_over_recorded_usage(hass: HomeAssistant) -> None:
    """Burlington: a UsageSummary total is spread across the period's *recorded* usage.

    The period's readings are not in this response (a bill publishes weeks late); the importer
    recovers them from the recorder. $40 over 1 kWh + 3 kWh → $10 then $30 → cumulative 10, 40.
    """
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    recorded = [
        (datetime(2026, 4, 3, 5, tzinfo=UTC), 1.0),
        (datetime(2026, 4, 3, 6, tzinfo=UTC), 3.0),
    ]
    with (
        patch(
            "custom_components.greenbutton.statistics._resume_point",
            new=AsyncMock(return_value=(0.0, None)),
        ),
        patch(
            "custom_components.greenbutton.statistics._recorded_forward_hours",
            new=AsyncMock(return_value=recorded),
        ),
        patch("custom_components.greenbutton.statistics.async_add_external_statistics") as add_mock,
    ):
        await import_usage_statistics(
            hass, entry, _summary_only_response(), utility_display_name="X"
        )

    cost_calls = [c for c in add_mock.call_args_list if c.args[1]["statistic_id"].endswith("_cost")]
    assert len(cost_calls) == 1  # summary path writes a cost stat from recorded usage
    stats = cost_calls[0].args[2]
    assert [round(s["sum"], 2) for s in stats] == [10.0, 40.0]


async def test_summary_cost_skipped_when_no_recorded_usage(hass: HomeAssistant) -> None:
    """A bill whose period has no recorded usage yet (e.g. predates the backfill) writes nothing."""
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    with (
        patch(
            "custom_components.greenbutton.statistics._resume_point",
            new=AsyncMock(return_value=(0.0, None)),
        ),
        patch(
            "custom_components.greenbutton.statistics._recorded_forward_hours",
            new=AsyncMock(return_value=[]),
        ),
        patch("custom_components.greenbutton.statistics.async_add_external_statistics") as add_mock,
    ):
        await import_usage_statistics(
            hass, entry, _summary_only_response(), utility_display_name="X"
        )

    cost_calls = [c for c in add_mock.call_args_list if c.args[1]["statistic_id"].endswith("_cost")]
    assert not cost_calls


async def test_summary_cost_deferred_until_hass_started(hass: HomeAssistant) -> None:
    """Before HA has started, the cost pass must NOT block on the recorder — it defers.

    Regression guard for the startup deadlock: the recorder thread doesn't drain its queue until
    EVENT_HOMEASSISTANT_STARTED, and HA doesn't fire STARTED until config-entry setup returns, so
    awaiting `async_block_till_done()` inside `async_config_entry_first_refresh()` hangs until HA's
    300s setup timeout cancels the entry ("Setup of config entry ... cancelled"). The usage import
    must still complete inline; only the recorder-dependent cost pass waits for start.
    """
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    recorded = [
        (datetime(2026, 4, 3, 5, tzinfo=UTC), 1.0),
        (datetime(2026, 4, 3, 6, tzinfo=UTC), 3.0),
    ]
    hass.set_state(CoreState.starting)
    with (
        patch(
            "custom_components.greenbutton.statistics._resume_point",
            new=AsyncMock(return_value=(0.0, None)),
        ),
        patch(
            "custom_components.greenbutton.statistics._recorded_forward_hours",
            new=AsyncMock(return_value=recorded),
        ),
        patch(
            "custom_components.greenbutton.statistics.get_instance",
        ) as get_instance_mock,
        patch("custom_components.greenbutton.statistics.async_add_external_statistics") as add_mock,
    ):
        get_instance_mock.return_value.async_block_till_done = AsyncMock()

        await import_usage_statistics(
            hass, entry, _summary_only_response(), utility_display_name="X"
        )

        # Nothing recorder-blocking may happen while HA is still starting.
        get_instance_mock.return_value.async_block_till_done.assert_not_awaited()
        assert not [
            c for c in add_mock.call_args_list if c.args[1]["statistic_id"].endswith("_cost")
        ]

        # ...and the deferred pass runs (and blocks safely) once HA is up.
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()

        get_instance_mock.return_value.async_block_till_done.assert_awaited_once()
        cost_calls = [
            c for c in add_mock.call_args_list if c.args[1]["statistic_id"].endswith("_cost")
        ]

    assert len(cost_calls) == 1
    assert [round(s["sum"], 2) for s in cost_calls[0].args[2]] == [10.0, 40.0]


def test_statistic_id_is_scoped_per_entry() -> None:
    """Two entries on the same utility point must produce distinct statistic IDs.

    This is the invariant that lets a tester swap a sandbox account for a real one without
    bleeding data between them in the Energy dashboard — see README Roadmap.
    """
    id_a = statistic_id_for_series("entry_a", "up_123", "FORWARD")
    id_b = statistic_id_for_series("entry_b", "up_123", "FORWARD")
    assert id_a != id_b
    assert id_a.startswith("greenbutton:entry_a_")
    assert id_b.startswith("greenbutton:entry_b_")


def test_statistic_id_differentiates_flow_directions() -> None:
    """Same usage point, opposite flow direction → different statistic IDs.

    Solar PV would emit both FORWARD (consumption) and REVERSE (export) — they must be
    separate series in HA so the Energy dashboard can graph them separately.
    """
    forward = statistic_id_for_series("entry_a", "up_123", "FORWARD")
    reverse = statistic_id_for_series("entry_a", "up_123", "REVERSE")
    assert forward != reverse


def test_statistic_id_lowercases_flow_direction() -> None:
    """Flow casing is normalized so a future server enum-rename doesn't shift the id."""
    upper = statistic_id_for_series("entry_a", "up_123", "FORWARD")
    lower = statistic_id_for_series("entry_a", "up_123", "forward")
    assert upper == lower


def test_statistic_id_prefix_matches_all_ids_for_an_entry() -> None:
    """The remove-entry purge filter must catch every id produced by `statistic_id_for_series`.

    The prefix is the load-bearing surface for async_remove_entry — if these drift the
    purge silently leaks orphan rows.
    """
    prefix = statistic_id_prefix_for_entry("entry_a")
    assert statistic_id_for_series("entry_a", "up_1", "FORWARD").startswith(prefix)
    assert statistic_id_for_series("entry_a", "up_2", "REVERSE").startswith(prefix)
    # …and crucially, *doesn't* catch a different entry's ids
    assert not statistic_id_for_series("entry_b", "up_1", "FORWARD").startswith(prefix)


def test_statistic_id_slugifies_real_world_ulid_and_uuid_inputs() -> None:
    """Real production inputs (HA ULID entry_id, ESPI UUID usage_point_id) must produce a
    valid slug after the `:` — HA's external-statistics machinery rejects mixed case or
    hyphens with "Invalid statistic_id", which kills async_setup_entry on first refresh.
    """
    sid = statistic_id_for_series(
        "01KT5B7TVYNVZY86P0PH0EPTAB",  # ULID — uppercase + digits
        "e082e9a9-390b-58fb-8ca5-4ee707c95652",  # UUID — hex + hyphens
        "FORWARD",
    )
    # No uppercase letters, no hyphens, only [a-z0-9_:] anywhere in the id.
    after_colon = sid.split(":", 1)[1]
    assert all(c.isalnum() or c == "_" for c in after_colon), sid
    assert after_colon.islower() or not any(c.isalpha() for c in after_colon), sid
    # And the same entry id still produces the same prefix used by async_remove_entry.
    assert sid.startswith(statistic_id_prefix_for_entry("01KT5B7TVYNVZY86P0PH0EPTAB"))


def test_select_summaries_drops_exact_duplicate_period() -> None:
    """A billing period repeated across the paginated feed must be costed once, not twice.

    Two identical summaries → keeping both would double that period's cost in the Energy
    dashboard (the regression this guards against).
    """
    a = _summary(_APR2, 32, 130.08)
    b = _summary(_APR2, 32, 130.08)
    selected = _select_billing_summaries([a, b])
    assert len(selected) == 1
    assert selected[0].total_cost == 130.08


def test_select_summaries_drops_overlapping_rollup() -> None:
    """A coarse rollup that spans a per-bill period is dropped in favor of the specific one.

    This is the multi-fold inflation case: a 12-month rollup laid over the current bill's
    hours would add its whole total on top of the per-bill total.
    """
    per_bill = _summary(_APR2, 32, 130.08)
    rollup = _summary(datetime(2025, 6, 1, tzinfo=UTC), 365, 1500.0)
    selected = _select_billing_summaries([rollup, per_bill])
    assert selected == [per_bill]


def test_select_summaries_keeps_consecutive_periods() -> None:
    """Back-to-back real billing periods share only their boundary instant → both kept.

    The interval check is half-open, so May 4 belongs to the second period only and the two
    never register as overlapping.
    """
    first = _summary(_APR2, 32, 130.08)  # Apr 2 → May 4
    second = _summary(_MAY4, 30, 118.0)  # May 4 → Jun 3
    selected = _select_billing_summaries([second, first])
    assert selected == [first, second]  # returned in billing-period order


def test_select_summaries_prefers_real_bill_over_zero_placeholder() -> None:
    """When a $0 placeholder duplicates a real bill's period, keep the real one.

    Same period + same (shortest) duration → the tie is broken by higher total_cost so the
    real bill wins and its cost isn't silently dropped.
    """
    placeholder = _summary(_APR2, 32, 0.0)
    real = _summary(_APR2, 32, 130.08)
    selected = _select_billing_summaries([placeholder, real])
    assert len(selected) == 1
    assert selected[0].total_cost == 130.08
