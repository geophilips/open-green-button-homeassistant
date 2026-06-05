"""Tests for the Ontario IESO TOU classifier + ESPI cost-detail note mapping."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.greenbutton.tou import (
    MID_PEAK,
    OFF_PEAK,
    ON_PEAK,
    cost_detail_tou_bucket,
    ontario_tou_bucket,
)


def test_summer_weekday_buckets() -> None:
    """11:00-17:00 on-peak; 7:00-11:00 + 17:00-19:00 mid-peak; else off-peak.

    Uses 2024-07-08 (a Monday in July) for the weekday + summer combination. Hours are local
    America/Toronto so we feed instants computed by hand (UTC = local + 4h DST offset).
    """
    # 03:00 UTC = 23:00 EDT Sunday-night-into-Monday — still weekend off-peak
    # 11:00 UTC = 07:00 EDT Mon — mid-peak starts
    # 15:00 UTC = 11:00 EDT Mon — on-peak starts
    # 21:00 UTC = 17:00 EDT Mon — mid-peak resumes
    # 23:00 UTC = 19:00 EDT Mon — off-peak resumes
    assert ontario_tou_bucket(datetime(2024, 7, 8, 6, 0, tzinfo=UTC)) == OFF_PEAK  # 02:00 EDT
    assert ontario_tou_bucket(datetime(2024, 7, 8, 11, 0, tzinfo=UTC)) == MID_PEAK  # 07:00 EDT
    assert ontario_tou_bucket(datetime(2024, 7, 8, 14, 59, tzinfo=UTC)) == MID_PEAK  # 10:59 EDT
    assert ontario_tou_bucket(datetime(2024, 7, 8, 15, 0, tzinfo=UTC)) == ON_PEAK  # 11:00 EDT
    assert ontario_tou_bucket(datetime(2024, 7, 8, 20, 59, tzinfo=UTC)) == ON_PEAK  # 16:59 EDT
    assert ontario_tou_bucket(datetime(2024, 7, 8, 21, 0, tzinfo=UTC)) == MID_PEAK  # 17:00 EDT
    assert ontario_tou_bucket(datetime(2024, 7, 8, 23, 0, tzinfo=UTC)) == OFF_PEAK  # 19:00 EDT


def test_winter_weekday_buckets() -> None:
    """Winter inverts on/mid: 7-11 + 17-19 are on-peak, 11-17 is mid-peak.

    Uses 2024-01-15 (a Monday in January). UTC = local + 5h EST offset.
    """
    assert ontario_tou_bucket(datetime(2024, 1, 15, 11, 0, tzinfo=UTC)) == OFF_PEAK  # 06:00 EST
    assert ontario_tou_bucket(datetime(2024, 1, 15, 12, 0, tzinfo=UTC)) == ON_PEAK  # 07:00 EST
    assert ontario_tou_bucket(datetime(2024, 1, 15, 16, 0, tzinfo=UTC)) == MID_PEAK  # 11:00 EST
    assert ontario_tou_bucket(datetime(2024, 1, 15, 22, 0, tzinfo=UTC)) == ON_PEAK  # 17:00 EST
    assert ontario_tou_bucket(datetime(2024, 1, 16, 0, 0, tzinfo=UTC)) == OFF_PEAK  # 19:00 EST Mon


def test_weekends_always_off_peak() -> None:
    """Every hour of a Sat/Sun is off-peak regardless of season."""
    # 2024-07-13 = Saturday, 15:00 UTC = 11:00 EDT (would be on-peak on a weekday)
    assert ontario_tou_bucket(datetime(2024, 7, 13, 15, 0, tzinfo=UTC)) == OFF_PEAK
    # 2024-01-14 = Sunday, 12:00 UTC = 07:00 EST (would be on-peak in winter on a weekday)
    assert ontario_tou_bucket(datetime(2024, 1, 14, 12, 0, tzinfo=UTC)) == OFF_PEAK


def test_cost_detail_note_to_bucket() -> None:
    """ESPI line-item notes map to canonical bucket names; non-TOU items → None."""
    # Burlington Hydro's actual labels seen in the test-lab feed.
    assert cost_detail_tou_bucket("Off Peak-Summer") == OFF_PEAK
    assert cost_detail_tou_bucket("Mid Peak-Summer") == MID_PEAK
    assert cost_detail_tou_bucket("On Peak-Summer") == ON_PEAK
    # Variants we might see from other utilities or different seasons.
    assert cost_detail_tou_bucket("Off-Peak Winter") == OFF_PEAK
    assert cost_detail_tou_bucket("ON PEAK") == ON_PEAK
    assert cost_detail_tou_bucket("on peak winter") == ON_PEAK
    # Non-TOU items return None — caller distributes those flat per kWh.
    assert cost_detail_tou_bucket("Delivery") is None
    assert cost_detail_tou_bucket("Regulatory Charges") is None
    assert cost_detail_tou_bucket("Global Adjustment") is None
    assert cost_detail_tou_bucket("Ontario Electricity Rebate") is None
    assert cost_detail_tou_bucket(None) is None
