"""Tests for BillingSummary.total_cost — the per-period charge derivation.

Regression coverage for the Burlington Hydro feed, whose UsageSummary detail list carries
running-balance bookkeeping ("Balance Forward" / "Payments Received") and subtotal lines
("New Charges This Period" / "Total Amount Due") alongside the real charges. Naively summing
every detail triple-counts the bill (charges + New-Charges subtotal + Total-Amount-Due
subtotal), which is exactly what inflated the Energy dashboard's cost ~3×.
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


# The real Apr 2 – May 4 2026 Burlington Hydro bill: energy (Off/Mid/On) = $130.08, full
# period charges = $182.08, and the feed's carried balance of $241.84 cancels against an
# equal payment. Summing every line yields $546.24 (~3×). The one authoritative number is
# "New Charges This Period" = $182.08.
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


def test_total_cost_uses_new_charges_not_the_triple_counted_sum() -> None:
    """The whole point: a real Burlington bill resolves to its period charges, not ~3×."""
    summary = _summary(_BURLINGTON_APR_2026)
    assert round(summary.total_cost, 2) == 182.08
    # And crucially NOT the naive sum-of-everything the old code produced.
    assert round(sum(d.amount for d in summary.cost_details), 2) == 546.24


def test_total_cost_prefers_bill_last_period_when_present() -> None:
    """A populated billLastPeriod is the grand total and wins over the detail lines."""
    summary = _summary(_BURLINGTON_APR_2026, bill_last_period_raw=13008000)  # $130.08
    assert summary.total_cost == 130.08


def test_total_cost_falls_back_to_charge_sum_without_a_new_charges_line() -> None:
    """No "New Charges This Period" line → sum only genuine charges, dropping bookkeeping.

    Balance Forward / Payments Received / Total Amount Due must be excluded so a carried
    balance that doesn't cancel can't leak into the period cost.
    """
    details = [
        _detail("Off Peak-Charge", 70.39),
        _detail("Delivery Charge", 67.29),
        _detail("H.S.T.", 26.45),
        _detail("Balance Forward", 300.00),
        _detail("Payments Received", -100.00),  # deliberately does NOT cancel
        _detail("Total Amount Due", 364.13),
    ]
    summary = _summary(details)
    assert round(summary.total_cost, 2) == 70.39 + 67.29 + 26.45


def test_total_cost_ignores_zero_valued_new_charges_placeholder() -> None:
    """A $0 "New Charges This Period" placeholder shouldn't zero out a period with charges."""
    details = [
        _detail("Off Peak-Charge", 40.00),
        _detail("Delivery Charge", 20.00),
        _detail("New Charges This Period", 0.0),
    ]
    summary = _summary(details)
    assert round(summary.total_cost, 2) == 60.00


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
