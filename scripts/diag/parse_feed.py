#!/usr/bin/env python3
"""Run a downloaded /proxy/usage ESPI feed through the real parser and summarise it.

Streams the XML through `espi.parse_usage_feed` (memory-safe via iterparse, so a multi-MB / 2-year
feed is fine) and prints usage points, series, total interval readings, and a sample reading.
Useful for validating a new Data Custodian's feed shape or debugging a "0 readings" import without
standing up Home Assistant.

    mise exec -- python scripts/diag/parse_feed.py <feed.xml>

The integration persists the raw feed to `<config>/.storage/greenbutton/<entry_id>.xml` when debug
logging is enabled for `custom_components.greenbutton`, so that file is the usual input here.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a plain script from any cwd by putting the repo root (which holds the
# custom_components package) on the import path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from custom_components.greenbutton.espi import parse_usage_feed  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: parse_feed.py <feed.xml>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"no such file: {path}", file=sys.stderr)
        return 2

    with path.open("rb") as f:
        updated, usage_points = parse_usage_feed(f)

    print(f"feed updated : {updated}")
    print(f"usage points : {len(usage_points)}")
    grand_total = 0
    grand_cost = 0.0
    for up in usage_points:
        total = sum(len(s.readings) for s in up.series)
        n_cost = sum(1 for s in up.series for r in s.readings if r.cost is not None)
        cost_sum = sum(r.cost for s in up.series for r in s.readings if r.cost is not None)
        grand_total += total
        grand_cost += cost_sum
        print(
            f"  {up.usage_point_id}  kind={up.service_kind}  series={len(up.series)}  "
            f"readings={total}  summaries={len(up.summaries)}  "
            f"readings_with_cost={n_cost}  cost_sum={cost_sum:.2f}"
        )
        for series in up.series[:3]:
            first = series.readings[0] if series.readings else None
            print(f"    {series.meter_reading_id}: {len(series.readings)} readings; first={first}")
    print(f"TOTAL readings: {grand_total}  TOTAL per-interval cost: {grand_cost:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
