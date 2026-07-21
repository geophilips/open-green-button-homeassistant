#!/usr/bin/env python3
"""Report the gap between each ESPI entry's <published> time and its interval (data) dates.

Answers the question behind "why does a narrow historical probe return no <cost>?": does the Data
Custodian filter /proxy/usage by *publication* time or by *interval (data) date*?

For every Atom <entry> that carries a non-zero per-interval <cost>, this prints the entry's
<published>/<updated> timestamps, the date range of the readings inside it, and the lag between
them. If <published> sits well *after* the interval dates (e.g. a billing cycle later), the
custodian publishes cost late and the API filters by publication time — so a `published-min`/
`published-max` window that ends near the interval date cannot contain that cost, and a probe
anchored to the interval-date frontier will never see it.

    mise exec -- python scripts/diag/cost_publish_lag.py <feed.xml>

The integration persists the raw feed to `<config>/.storage/greenbutton/<entry_id>.xml` when debug
logging is enabled for `custom_components.greenbutton`; a rebuild (or first setup) writes a wide,
cost-bearing feed there. Download that file off the HA instance and pass its path here (a routine
incremental feed has no cost to inspect). Stdlib-only, so plain `python3 <path>` works too.
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path

# Namespace-tolerant: tags may carry a prefix (atom:published, espi:cost) or a default namespace.
_ENTRY = re.compile(r"<entry[ >].*?</entry>", re.DOTALL)
_PUBLISHED = re.compile(r"<(?:\w+:)?published>(.*?)</(?:\w+:)?published>")
_UPDATED = re.compile(r"<(?:\w+:)?updated>(.*?)</(?:\w+:)?updated>")
_START = re.compile(r"<(?:\w+:)?start>\s*(\d+)\s*</(?:\w+:)?start>")
_COST = re.compile(r"<(?:\w+:)?cost>\s*(-?\d+)\s*</(?:\w+:)?cost>")

_MAX_ROWS = 12


def _parse_iso(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: cost_publish_lag.py <feed.xml>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"no such file: {path}", file=sys.stderr)
        return 2

    xml = path.read_text(errors="replace")
    entries = _ENTRY.findall(xml)
    print(f"feed        : {path}")
    print(f"entries     : {len(entries)}")

    shown = 0
    for entry in entries:
        if "IntervalReading" not in entry:
            continue
        costs = [int(c) for c in _COST.findall(entry)]
        if not any(c != 0 for c in costs):  # skip uncosted / all-zero-placeholder blocks
            continue
        starts = [int(s) for s in _START.findall(entry)]
        if not starts:
            continue

        first_dt = datetime.fromtimestamp(min(starts), tz=UTC)
        last_dt = datetime.fromtimestamp(max(starts), tz=UTC)
        pub = _PUBLISHED.search(entry)
        upd = _UPDATED.search(entry)
        pub_dt = _parse_iso(pub.group(1)) if pub else None
        lag = f"{(pub_dt - last_dt).days:+d}d" if pub_dt else "?"

        print(
            f"  published={pub.group(1) if pub else '?'}  "
            f"updated={upd.group(1) if upd else '?'}  "
            f"interval_dates={first_dt.date()}..{last_dt.date()}  "
            f"costed_rows={sum(1 for c in costs if c != 0)}  "
            f"publish_lag_vs_last_interval={lag}"
        )
        shown += 1
        if shown >= _MAX_ROWS:
            break

    if shown == 0:
        print("  (no non-zero per-interval <cost> found — is this a wide/cost-bearing feed?)")
    else:
        print(
            "\nRead: publish_lag ~ a billing cycle => cost is published late and the API filters\n"
            "by publication time, so a window ending near the interval date can't return it."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
