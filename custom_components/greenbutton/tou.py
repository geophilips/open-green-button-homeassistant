"""Time-of-use (TOU) classification for utility cost distribution.

Used by [statistics._import_cost_summaries] when an ESPI UsageSummary carries per-TOU-bucket
cost details (Ontario's IESO and some other jurisdictions break out Off-Peak, Mid-Peak,
On-Peak charges as separate `costAdditionalDetailLastPeriod` line items). For each hour we
need to know which bucket it falls in so the right rate applies — which depends on the
utility's jurisdiction. This module exposes one classifier per supported jurisdiction.

Today: only the Ontario IESO schedule is implemented because every currently supported
utility is on it. When we onboard a US or other-Canadian utility on a different schedule,
add a sibling classifier here and extend the dispatch to pick the right one per utility.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

# Canonical TOU bucket names used across the codebase. The strings match the way ESPI
# `costAdditionalDetailLastPeriod` notes are normalized (see [cost_detail_tou_bucket]) so
# we can look up rates by bucket directly.
OFF_PEAK = "off_peak"
MID_PEAK = "mid_peak"
ON_PEAK = "on_peak"

_ONTARIO_TZ = ZoneInfo("America/Toronto")

# IESO TOU schedule changes between Summer and Winter pricing periods. Summer runs
# May 1 - Oct 31 inclusive in the calendar months sense (the IESO publishes effective
# dates but they reliably line up with the month boundaries we use here).
_SUMMER_MONTHS = range(5, 11)  # May (5) through October (10)


def ontario_tou_bucket(dt: datetime) -> str:
    """Classify a tz-aware instant under Ontario's IESO TOU schedule.

    Returns one of [OFF_PEAK], [MID_PEAK], [ON_PEAK]. Holidays are off-peak per IESO rules
    but this implementation doesn't yet carry a holiday calendar — fix when a real customer
    reports a holiday-week cost discrepancy.

    Summer (May 1 - Oct 31) weekdays:
      - 11:00 - 17:00 → On-Peak
      -  7:00 - 11:00 and 17:00 - 19:00 → Mid-Peak
      - elsewhere → Off-Peak

    Winter (Nov 1 - Apr 30) weekdays:
      -  7:00 - 11:00 and 17:00 - 19:00 → On-Peak
      - 11:00 - 17:00 → Mid-Peak
      - elsewhere → Off-Peak

    Weekends are always Off-Peak.
    """
    local = dt.astimezone(_ONTARIO_TZ)
    if local.weekday() >= 5:  # 5=Sat, 6=Sun
        return OFF_PEAK
    hour = local.hour
    if local.month in _SUMMER_MONTHS:
        if 11 <= hour < 17:
            return ON_PEAK
        if 7 <= hour < 11 or 17 <= hour < 19:
            return MID_PEAK
        return OFF_PEAK
    # Winter:
    if 7 <= hour < 11 or 17 <= hour < 19:
        return ON_PEAK
    if 11 <= hour < 17:
        return MID_PEAK
    return OFF_PEAK


def cost_detail_tou_bucket(note: str | None) -> str | None:
    """Map an ESPI `costAdditionalDetailLastPeriod` note to a TOU bucket, or None.

    Ontario utility feeds label TOU line items with notes like "Off Peak-Summer",
    "Mid Peak-Winter", "On Peak". We accept any phrasing variant containing "off/mid/on"
    plus "peak", and ignore the season suffix (we re-derive season from the hour timestamp,
    not the line-item label, so the same `bucket` lookup works year-round).

    Non-TOU detail items (Delivery, Regulatory Charges, Global Adjustment, Ontario
    Electricity Rebate, etc.) return None — the caller distributes those flat per kWh.
    """
    if note is None:
        return None
    n = note.lower().replace("-", " ")
    if "off peak" in n:
        return OFF_PEAK
    if "mid peak" in n:
        return MID_PEAK
    if "on peak" in n:
        return ON_PEAK
    return None
