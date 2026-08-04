"""Tests for the statistics helper — id format invariants + unit conversion.

The full async_add_external_statistics path is exercised by the coordinator-level tests
where a recorder is wired up; these focus on the pure-function bits that don't need HA.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics
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
            UsageReading(datetime(2026, 7, 5, 7, tzinfo=UTC), 3600, 500.0, cost=0.0),
        ],
    )
    up = UsagePoint(usage_point_id="up1", service_kind="electricity", series=[series])
    return UsageResponse(updated=None, usage_points=[up], new_credentials=None)


def _milton_mixed_series_response(*, with_summary: bool = False) -> UsageResponse:
    """Milton's hourly deltas plus its daily cumulative register snapshot."""
    delta_type = NormalizedReadingType(
        commodity="ELECTRICITY_SECONDARY_METERED",
        flow_direction="FORWARD",
        accumulation_behaviour="DELTA_DATA",
        interval_length_seconds=3600,
        unit_of_measure="WATT_HOURS",
        unit_of_measure_symbol="Wh",
        power_of_ten_multiplier=0,
        currency_numeric_code=124,
    )
    bulk_type = NormalizedReadingType(
        commodity="ELECTRICITY_SECONDARY_METERED",
        flow_direction="FORWARD",
        accumulation_behaviour="BULK_QUANTITY",
        interval_length_seconds=_DAY,
        unit_of_measure="WATT_HOURS",
        unit_of_measure_symbol="Wh",
        power_of_ten_multiplier=0,
        currency_numeric_code=124,
    )
    delta_series = MeterReadingSeries(
        meter_reading_id="hourly",
        reading_type=delta_type,
        readings=[
            UsageReading(datetime(2026, 7, 5, 5, tzinfo=UTC), 3600, 1000.0),
            UsageReading(datetime(2026, 7, 5, 6, tzinfo=UTC), 3600, 1500.0),
        ],
    )
    bulk_series = MeterReadingSeries(
        meter_reading_id="register",
        reading_type=bulk_type,
        readings=[UsageReading(datetime(2026, 7, 5, tzinfo=UTC), _DAY, 9_876_543.0, cost=0.0)],
    )
    summaries = (
        [_summary(datetime(2026, 7, 1, tzinfo=UTC), 31, total_dollars=50.0)] if with_summary else []
    )
    up = UsagePoint(
        usage_point_id="up1",
        service_kind="electricity",
        series=[delta_series, bulk_series],
        summaries=summaries,
    )
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
    assert [round(s["sum"], 3) for s in stats] == [0.087, 0.209, 0.209]  # cumulative


async def test_bulk_register_series_is_not_added_to_interval_usage(hass: HomeAssistant) -> None:
    """Milton's cumulative daily register must not inflate its hourly consumption statistic."""
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
            hass, entry, _milton_mixed_series_response(), utility_display_name="Milton Hydro"
        )

    usage_calls = [
        call
        for call in add_mock.call_args_list
        if not call.args[1]["statistic_id"].endswith("_cost")
    ]
    assert len(usage_calls) == 1
    stats = usage_calls[0].args[2]
    assert [round(s["sum"], 3) for s in stats] == [1.0, 2.5]


async def test_bulk_zero_cost_falls_back_to_billing_summary(hass: HomeAssistant) -> None:
    """Milton's zero-cost register placeholder must not suppress its real billing summary."""
    entry = MagicMock()
    entry.entry_id = "01TESTENTRY"
    recorded = [
        (datetime(2026, 7, 5, 5, tzinfo=UTC), 1.0),
        (datetime(2026, 7, 5, 6, tzinfo=UTC), 1.5),
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
            hass,
            entry,
            _milton_mixed_series_response(with_summary=True),
            utility_display_name="Milton Hydro",
        )

    cost_calls = [
        call for call in add_mock.call_args_list if call.args[1]["statistic_id"].endswith("_cost")
    ]
    assert len(cost_calls) == 1
    stats = cost_calls[0].args[2]
    assert [round(s["sum"], 2) for s in stats] == [20.0, 50.0]


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
