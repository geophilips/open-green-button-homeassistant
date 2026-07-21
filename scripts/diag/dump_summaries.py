#!/usr/bin/env python3
"""Dump every ESPI UsageSummary in a feed: publish time, billing period, and real total cost.

Burlington (and other Ontario utilities on this platform) deliver cost as monthly UsageSummary
entries, not per-interval <cost>. The bill total is usually NOT in <billLastPeriod> (which reads 0);
it's the sum of the itemized <costAdditionalDetailLastPeriod> charge lines, excluding running
balance bookkeeping ("Balance Forward"/"Payments Received") and subtotals ("New Charges This
Period"/"Total Amount Due"). This mirrors BillingSummary.total_cost in the integration.

It also shows how each summary's Atom <published> time relates to the period it covers, and prints a
grand total (which should ~match the max cumulative value of your cost statistic).

    python3 scripts/diag/dump_summaries.py <feed.xml>

Get a cost-bearing feed by running the integration's rebuild action (with debug logging on) and
downloading <config>/.storage/greenbutton/<entry_id>.xml. Stdlib-only.
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Namespace-tolerant: tags may carry a prefix (atom:published, espi:billLastPeriod).
_ENTRY = re.compile(r"<entry[ >].*?</entry>", re.DOTALL)
_PUBLISHED = re.compile(r"<(?:\w+:)?published>(.*?)</(?:\w+:)?published>")
_BILLING = re.compile(r"<(?:\w+:)?billingPeriod>(.*?)</(?:\w+:)?billingPeriod>", re.DOTALL)
_START = re.compile(r"<(?:\w+:)?start>\s*(\d+)\s*</(?:\w+:)?start>")
_DURATION = re.compile(r"<(?:\w+:)?duration>\s*(\d+)\s*</(?:\w+:)?duration>")
_BILL = re.compile(r"<(?:\w+:)?billLastPeriod>\s*(-?\d+)\s*</(?:\w+:)?billLastPeriod>")
_CURRENCY = re.compile(r"<(?:\w+:)?currency>\s*(\d+)\s*</(?:\w+:)?currency>")
_DETAIL = re.compile(
    r"<(?:\w+:)?costAdditionalDetailLastPeriod>(.*?)</(?:\w+:)?costAdditionalDetailLastPeriod>",
    re.DOTALL,
)
_AMOUNT = re.compile(r"<(?:\w+:)?amount>\s*(-?\d+)\s*</(?:\w+:)?amount>")
_NOTE = re.compile(r"<(?:\w+:)?note>(.*?)</(?:\w+:)?note>", re.DOTALL)

# ESPI monetary amounts are in 1/100000 of the currency unit.
_COST_SCALE = 100_000.0
_ISO_4217 = {124: "CAD", 840: "USD", 978: "EUR", 826: "GBP", 36: "AUD", 554: "NZD"}
# Line-item notes that are bookkeeping / subtotals, not charges (see api.BillingSummary.total_cost).
_NON_CHARGE_NOTES = {
    "balance forward",
    "payments received",
    "new charges this period",
    "total amount due",
}


def _parse_iso(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _dt_from_epoch(match: re.Match[str] | None) -> datetime | None:
    return datetime.fromtimestamp(int(match.group(1)), tz=UTC) if match else None


def _total_cost(entry: str) -> float:
    """Replicate BillingSummary.total_cost: billLastPeriod if >0, else sum of charge line items."""
    bill = _BILL.search(entry)
    if bill and int(bill.group(1)) > 0:
        return int(bill.group(1)) / _COST_SCALE
    total = 0.0
    for block in _DETAIL.findall(entry):
        note = _NOTE.search(block)
        normalized = " ".join(note.group(1).lower().split()) if note else ""
        if normalized in _NON_CHARGE_NOTES:
            continue
        amount = _AMOUNT.search(block)
        total += (int(amount.group(1)) if amount else 0) / _COST_SCALE
    return total


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: dump_summaries.py <feed.xml>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"no such file: {path}", file=sys.stderr)
        return 2

    xml = path.read_text(errors="replace")
    rows = []
    for entry in _ENTRY.findall(xml):
        if "UsageSummary" not in entry:
            continue
        billing = _BILLING.search(entry)
        period_start = _dt_from_epoch(_START.search(billing.group(1))) if billing else None
        dur_match = _DURATION.search(billing.group(1)) if billing else None
        duration = int(dur_match.group(1)) if dur_match else None
        period_end = None
        if period_start and duration:
            period_end = period_start + timedelta(seconds=duration)
        pub = _PUBLISHED.search(entry)
        cur = _CURRENCY.search(entry)
        rows.append(
            {
                "published": pub.group(1) if pub else "?",
                "pub_dt": _parse_iso(pub.group(1)) if pub else None,
                "period_start": period_start,
                "period_end": period_end,
                "duration_days": round(duration / 86400) if duration else None,
                "cost": _total_cost(entry),
                "currency": _ISO_4217.get(int(cur.group(1)), cur.group(1)) if cur else "?",
                "details": len(_DETAIL.findall(entry)),
            }
        )

    print(f"feed         : {path}")
    print(f"UsageSummary : {len(rows)}")
    rows.sort(key=lambda r: r["period_start"] or datetime.min.replace(tzinfo=UTC))
    grand_total = 0.0
    for r in rows:
        ps = r["period_start"].date().isoformat() if r["period_start"] else "?"
        pe = r["period_end"].date().isoformat() if r["period_end"] else "?"
        days = f"{r['duration_days']}d" if r["duration_days"] is not None else "?"
        grand_total += r["cost"]
        if r["pub_dt"] and r["period_end"]:
            lag = f"{(r['pub_dt'] - r['period_end']).days:+d}d"
        else:
            lag = "?"
        print(
            f"  period={ps}..{pe} ({days})  cost={r['cost']:.2f} {r['currency']}  "
            f"lines={r['details']}  published={r['published']}  publish_lag={lag}"
        )
    print(f"\ngrand total cost across all summaries: {grand_total:.2f}")
    print("(should ~match the max cumulative value of your *_cost statistic)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
