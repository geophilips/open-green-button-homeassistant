"""Tests for BillingSummary.total_cost — the per-period charge derivation.

Regression coverage for the Burlington Hydro feed, whose UsageSummary detail list carries
running-balance bookkeeping ("Balance Forward" / "Payments Received") and subtotal lines
("New Charges This Period" / "Total Amount Due") alongside the real charges. Two failure modes
seen in real data, both fixed by summing only the itemized charge lines:

  - Naively summing *every* detail triple-counts the bill (charges + New-Charges subtotal +
    Total-Amount-Due subtotal) — the first ~3× inflation we saw.
  - On roughly every other bill the "New Charges This Period" / "Total Amount Due" subtotals
    are themselves *corrupt* (stamped well above the itemized charges), so trusting them is
    also wrong — the second inflation. Only the itemized lines are reliable.
"""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.greenbutton.api import BillingSummary, CostDetail


def _detail(note: str, dollars: float) -> CostDetail:
    return CostDetail(
        amount_raw=round(dollars * 100_000),
        note=note,
        item_kind=None,
        unit_cost_raw=0,
    )


def _summary(details: list[CostDetail], bill_last_period_raw: int = 0) -> BillingSummary:
    return BillingSummary(
        billing_period_start=datetime(2026, 4, 2, 4, tzinfo=UTC),
        billing_period_duration_seconds=32 * 86400,
        bill_last_period_raw=bill_last_period_raw,
        cost_additional_last_period_raw=0,
        cost_details=details,
        currency_numeric_code=124,
    )


# The real Apr 2 – May 4 2026 Burlington Hydro bill (a "clean" month, where the subtotal
# happens to match): itemized charges sum to $182.08, the carried balance of $241.84 cancels
# against an equal payment, and both subtotal lines also read $182.08. Summing every line
# yields $546.24 (~3×).
_BURLINGTON_APR_2026 = [
    _detail("Off Peak-Charge", 70.39),
    _detail("Mid Peak-Charge", 26.79),
    _detail("On Peak-Charge", 32.90),
    _detail("Delivery Charge", 67.29),
    _detail("Regulatory charge", 6.06),
    _detail("H.S.T.", 26.45),
    _detail("Ontario Electricity Rebate", -47.80),
    _detail("Balance Forward", 241.84),
    _detail("Payments Received", -241.84),
    _detail("New Charges This Period", 182.08),
    _detail("Total Amount Due", 182.08),
]

# The real May 4 – Jun 2 2026 bill (a "corrupt" month): the itemized charges sum to $187.73
# and the H.S.T. of $27.27 is 13% of the pre-rebate subtotal — internally consistent — but
# the "New Charges This Period" / "Total Amount Due" subtotals are stamped $502.29, with no
# line item for the $314.56 difference. total_cost must resolve to the itemized $187.73.
_BURLINGTON_MAY_2026 = [
    _detail("Off Peak-Charge", 75.04),
    _detail("Mid Peak-Charge", 24.45),
    _detail("On Peak-Charge", 35.43),
    _detail("Delivery Charge", 68.52),
    _detail("Regulatory charge", 6.31),
    _detail("H.S.T.", 27.27),
    _detail("Ontario Electricity Rebate", -49.29),
    _detail("Balance Forward", 182.08),
    _detail("Payments Received", -182.08),
    _detail("New Charges This Period", 502.29),
    _detail("Total Amount Due", 502.29),
]


def test_total_cost_sums_itemized_charges_not_the_triple_counted_sum() -> None:
    """A clean Burlington bill resolves to its itemized period charges, not the ~3× sum."""
    summary = _summary(_BURLINGTON_APR_2026)
    assert round(summary.total_cost, 2) == 182.08
    # And crucially NOT the naive sum-of-everything the very first version produced.
    assert round(sum(d.amount for d in summary.cost_details), 2) == 546.24


def test_total_cost_ignores_corrupt_new_charges_subtotal() -> None:
    """When the feed's subtotal is inflated, total_cost trusts the itemized lines instead.

    This is the second inflation: "New Charges This Period" reads $502.29 but the real,
    itemized period charges are $187.73. Trusting the subtotal (the earlier fix) gave ~3.5×
    the true daily cost in the dashboard.
    """
    summary = _summary(_BURLINGTON_MAY_2026)
    assert round(summary.total_cost, 2) == 187.73
    assert summary.total_cost != 502.29


def test_total_cost_prefers_bill_last_period_when_present() -> None:
    """A populated billLastPeriod is the grand total and wins over the detail lines."""
    summary = _summary(_BURLINGTON_APR_2026, bill_last_period_raw=13008000)  # $130.08
    assert summary.total_cost == 130.08


def test_total_cost_excludes_uncancelled_running_balance() -> None:
    """Balance Forward / Payments Received / subtotals never leak into the period cost.

    Even when a carried balance does NOT cancel (a partial payment), only the itemized
    charges count — the running balance is prior-invoice money, not this period's usage.
    """
    details = [
        _detail("Off Peak-Charge", 70.39),
        _detail("Delivery Charge", 67.29),
        _detail("H.S.T.", 26.45),
        _detail("Balance Forward", 300.00),
        _detail("Payments Received", -100.00),  # deliberately does NOT cancel
        _detail("New Charges This Period", 164.13),
        _detail("Total Amount Due", 364.13),
    ]
    summary = _summary(details)
    assert round(summary.total_cost, 2) == 70.39 + 67.29 + 26.45


def test_is_period_charge_classifies_line_items() -> None:
    """Charges are summable; balance bookkeeping and subtotals are not."""
    assert _detail("Off Peak-Charge", 1).is_period_charge
    assert _detail("Delivery Charge", 1).is_period_charge
    assert _detail("Ontario Electricity Rebate", -1).is_period_charge
    assert not _detail("Balance Forward", 1).is_period_charge
    assert not _detail("Payments Received", -1).is_period_charge
    assert not _detail("New Charges This Period", 1).is_period_charge
    assert not _detail("Total Amount Due", 1).is_period_charge
    # Matching is case- and whitespace-insensitive.
    assert not _detail("  TOTAL  AMOUNT   DUE ", 1).is_period_charge
