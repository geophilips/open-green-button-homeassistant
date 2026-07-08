"""ESPI Atom feed parser for Open Green Button.

Stream-parses NAESB ESPI 1.1 Atom usage feeds into the domain dataclasses defined in
[api]. Uses ``xml.etree.ElementTree.iterparse`` so the resident memory cost is O(one entry)
rather than O(whole feed) — important on memory-constrained HA hosts when a utility returns
multi-year hourly backfill on first sync.

The shape mirrors the Kotlin reference parser in
[open-green-button-kotlin-client](https://github.com/rocketraman/open-green-button-kotlin-client):
the same ESPI integer codes (ServiceCategory.kind, ReadingType.commodity / flowDirection /
accumulationBehaviour / uom) map to the same string enum names that the rest of the
integration consumes.

What's parsed:
  - UsagePoint (ServiceCategory.kind → service_kind)
  - MeterReading (empty content; the link graph carries the relationship)
  - IntervalBlock (interval + repeated IntervalReading children)
  - ReadingType (commodity / flowDirection / accumulationBehaviour / intervalLength /
                 uom / powerOfTenMultiplier / currency)
  - UsageSummary (billing-period total + per-line-item cost breakdown for the Energy
                  dashboard's Cost column — see [statistics._import_cost_summaries])

What's deliberately ignored (this round):
  - LocalTimeParameters (HA uses its own configured TZ)
  - Customer-namespace payloads (not needed for the Energy dashboard)
"""

from __future__ import annotations

import io
import logging
import xml.etree.ElementTree as ET  # Element/types only — parsing goes through defusedxml.
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import IO

# Defuse XML attacks (XXE, billion laughs) — the bytes here flow from the utility through
# our proxy, so we treat them as untrusted. defusedxml ships with HA already.
from defusedxml.ElementTree import iterparse as _safe_iterparse

from .api import (
    BillingSummary,
    CostDetail,
    MeterReadingSeries,
    NormalizedReadingType,
    UsagePoint,
    UsageReading,
)

_LOGGER = logging.getLogger(__name__)

_ATOM_NS = "http://www.w3.org/2005/Atom"
_ESPI_NS = "http://naesb.org/espi"

_TAG_FEED = f"{{{_ATOM_NS}}}feed"
_TAG_ENTRY = f"{{{_ATOM_NS}}}entry"
_TAG_LINK = f"{{{_ATOM_NS}}}link"
_TAG_UPDATED = f"{{{_ATOM_NS}}}updated"

_TAG_USAGE_POINT = f"{{{_ESPI_NS}}}UsagePoint"
_TAG_METER_READING = f"{{{_ESPI_NS}}}MeterReading"
_TAG_INTERVAL_BLOCK = f"{{{_ESPI_NS}}}IntervalBlock"
_TAG_READING_TYPE = f"{{{_ESPI_NS}}}ReadingType"
_TAG_USAGE_SUMMARY = f"{{{_ESPI_NS}}}UsageSummary"

_TAG_SERVICE_CATEGORY = f"{{{_ESPI_NS}}}ServiceCategory"
_TAG_KIND = f"{{{_ESPI_NS}}}kind"
_TAG_INTERVAL = f"{{{_ESPI_NS}}}interval"
_TAG_TIME_PERIOD = f"{{{_ESPI_NS}}}timePeriod"
_TAG_INTERVAL_READING = f"{{{_ESPI_NS}}}IntervalReading"
_TAG_DURATION = f"{{{_ESPI_NS}}}duration"
_TAG_START = f"{{{_ESPI_NS}}}start"
_TAG_VALUE = f"{{{_ESPI_NS}}}value"
_TAG_COST = f"{{{_ESPI_NS}}}cost"

# ESPI reports monetary amounts (IntervalReading/cost, UsageSummary line items) in 1/100,000 of the
# currency unit.
_ESPI_COST_SCALE = 100_000.0
_TAG_ACCUM = f"{{{_ESPI_NS}}}accumulationBehaviour"
_TAG_COMMODITY = f"{{{_ESPI_NS}}}commodity"
_TAG_CURRENCY = f"{{{_ESPI_NS}}}currency"
_TAG_FLOW = f"{{{_ESPI_NS}}}flowDirection"
_TAG_INTERVAL_LEN = f"{{{_ESPI_NS}}}intervalLength"
_TAG_POW10 = f"{{{_ESPI_NS}}}powerOfTenMultiplier"
_TAG_UOM = f"{{{_ESPI_NS}}}uom"

_TAG_BILLING_PERIOD = f"{{{_ESPI_NS}}}billingPeriod"
_TAG_BILL_LAST_PERIOD = f"{{{_ESPI_NS}}}billLastPeriod"
_TAG_COST_ADDITIONAL_LAST_PERIOD = f"{{{_ESPI_NS}}}costAdditionalLastPeriod"
_TAG_COST_ADDITIONAL_DETAIL_LAST_PERIOD = f"{{{_ESPI_NS}}}costAdditionalDetailLastPeriod"
_TAG_AMOUNT = f"{{{_ESPI_NS}}}amount"
_TAG_NOTE = f"{{{_ESPI_NS}}}note"
_TAG_ITEM_KIND = f"{{{_ESPI_NS}}}itemKind"
_TAG_UNIT_COST = f"{{{_ESPI_NS}}}unitCost"


# Intermediate types — accumulated during iterparse and resolved into the public api.py
# dataclasses after the whole feed has been walked. Kept module-private.


@dataclass(slots=True)
class _RawUsagePoint:
    usage_point_id: str
    kind: int | None = None


@dataclass(slots=True)
class _RawMeterReading:
    meter_reading_id: str
    usage_point_id: str | None = None
    reading_type_id: str | None = None


@dataclass(slots=True)
class _RawReadingType:
    reading_type_id: str
    accumulation_behaviour: int | None = None
    commodity: int | None = None
    currency: int | None = None
    flow_direction: int | None = None
    interval_length_seconds: int = 0
    power_of_ten_multiplier: int = 0
    uom: int | None = None


@dataclass(slots=True)
class _RawIntervalBlock:
    meter_reading_id: str | None
    readings: list[UsageReading] = field(default_factory=list)


@dataclass(slots=True)
class _RawUsageSummary:
    """A UsageSummary entry pinned to its parent UsagePoint via the `related` link."""

    usage_point_id: str | None
    summary: BillingSummary


def parse_usage_feed(source: IO[bytes] | bytes) -> tuple[datetime | None, list[UsagePoint]]:
    """Parse an ESPI usage Atom feed.

    Accepts either a file-like object opened in binary mode or a raw `bytes` buffer (as
    returned from `aiohttp.ClientResponse.read()`). Returns ``(updated_at, usage_points)``
    where ``updated_at`` is the feed-level ``<updated>`` instant and ``usage_points`` is the
    assembled domain tree. Empty ``usage_points`` is a valid result — it means the feed had
    no UsagePoint entries (the test-lab default before they seed your account).
    """
    # iterparse wants a stream, not a bytes literal — wrap if needed.
    stream = io.BytesIO(source) if isinstance(source, bytes) else source

    feed_updated: datetime | None = None
    raw_usage_points: dict[str, _RawUsagePoint] = {}
    raw_meter_readings: dict[str, _RawMeterReading] = {}
    raw_reading_types: dict[str, _RawReadingType] = {}
    raw_interval_blocks: list[_RawIntervalBlock] = []
    raw_summaries: list[_RawUsageSummary] = []

    seen_first_entry = False

    for _event, elem in _safe_iterparse(stream, events=("end",)):
        if elem.tag == _TAG_UPDATED and not seen_first_entry:
            # Feed-level <updated> appears in document order before any <entry>, so this
            # captures it without ambiguity with per-entry <updated> elements.
            feed_updated = _parse_iso(elem.text)
            elem.clear()
        elif elem.tag == _TAG_ENTRY:
            seen_first_entry = True
            _classify_entry(
                elem,
                raw_usage_points,
                raw_meter_readings,
                raw_reading_types,
                raw_interval_blocks,
                raw_summaries,
            )
            elem.clear()

    return feed_updated, _assemble(
        raw_usage_points,
        raw_meter_readings,
        raw_reading_types,
        raw_interval_blocks,
        raw_summaries,
    )


# ---------------------------------------------------------------------------------------
# Entry classification — figure out which ESPI payload type this entry is and stash the
# raw shape. Cross-entry link resolution happens later in _assemble().
# ---------------------------------------------------------------------------------------


def _classify_entry(
    entry: ET.Element,
    usage_points: dict[str, _RawUsagePoint],
    meter_readings: dict[str, _RawMeterReading],
    reading_types: dict[str, _RawReadingType],
    interval_blocks: list[_RawIntervalBlock],
    summaries: list[_RawUsageSummary],
) -> None:
    # Atom content wrapper → first child tells us the payload type.
    content = entry.find(f"{{{_ATOM_NS}}}content")
    if content is None:
        return
    payload = next(iter(content), None)
    if payload is None:
        return
    self_link = _link(entry, rel="self")

    if payload.tag == _TAG_USAGE_POINT:
        up_id = _id_from(self_link, "UsagePoint")
        if up_id is None:
            return
        usage_points[up_id] = _RawUsagePoint(
            usage_point_id=up_id,
            kind=_child_int(payload, _TAG_SERVICE_CATEGORY, _TAG_KIND),
        )
    elif payload.tag == _TAG_METER_READING:
        mr_id = _id_from(self_link, "MeterReading")
        if mr_id is None:
            return
        meter_readings[mr_id] = _RawMeterReading(
            meter_reading_id=mr_id,
            # savagedata nests resources in the URL path
            # (.../UsagePoint/{up}/MeterReading/{mr}) and omits the flat rel="related"
            # espi-entry/UsagePoint link, so derive the parent UsagePoint from the self href first;
            # fall back to the related link for custodians (Burlington) using flat resources.
            usage_point_id=(
                _id_from(self_link, "UsagePoint")
                or _related_id(entry, "espi-entry/UsagePoint", "UsagePoint")
            ),
            reading_type_id=_related_id(entry, "espi-entry/ReadingType", "ReadingType"),
        )
    elif payload.tag == _TAG_INTERVAL_BLOCK:
        mr_id_from_up = _id_from(_link(entry, rel="up"), "MeterReading")
        interval_blocks.append(
            _RawIntervalBlock(
                meter_reading_id=mr_id_from_up,
                readings=list(_parse_interval_readings(payload)),
            )
        )
    elif payload.tag == _TAG_READING_TYPE:
        rt_id = _id_from(self_link, "ReadingType")
        if rt_id is None:
            return
        interval_len = _int_text(payload.find(_TAG_INTERVAL_LEN)) or 0
        reading_types[rt_id] = _RawReadingType(
            reading_type_id=rt_id,
            accumulation_behaviour=_int_text(payload.find(_TAG_ACCUM)),
            commodity=_int_text(payload.find(_TAG_COMMODITY)),
            currency=_int_text(payload.find(_TAG_CURRENCY)),
            flow_direction=_int_text(payload.find(_TAG_FLOW)),
            interval_length_seconds=interval_len,
            power_of_ten_multiplier=_int_text(payload.find(_TAG_POW10)) or 0,
            uom=_int_text(payload.find(_TAG_UOM)),
        )
    elif payload.tag == _TAG_USAGE_SUMMARY:
        # Pin to parent via "related" link, since UsageSummary doesn't appear in the
        # UsagePoint's own link tree.
        up_id_from_related = _related_id(entry, "espi-entry/UsagePoint", "UsagePoint")
        summary = _parse_usage_summary(payload)
        if summary is not None:
            summaries.append(_RawUsageSummary(usage_point_id=up_id_from_related, summary=summary))


def _parse_usage_summary(payload: ET.Element) -> BillingSummary | None:
    """Build a BillingSummary from the inside of a `<content><espi:UsageSummary>` block."""
    billing_period_elem = payload.find(_TAG_BILLING_PERIOD)
    if billing_period_elem is None:
        return None
    start_text = billing_period_elem.findtext(_TAG_START)
    duration_text = billing_period_elem.findtext(_TAG_DURATION)
    if start_text is None or duration_text is None:
        return None
    try:
        start_epoch = int(start_text)
        duration_seconds = int(duration_text)
    except ValueError:
        return None

    # Currency is occasionally surfaced on UsageSummary directly; otherwise the consumer
    # falls back to whatever the linked ReadingType carries.
    currency = _int_text(payload.find(_TAG_CURRENCY))

    return BillingSummary(
        billing_period_start=datetime.fromtimestamp(start_epoch, tz=UTC),
        billing_period_duration_seconds=duration_seconds,
        bill_last_period_raw=_int_text(payload.find(_TAG_BILL_LAST_PERIOD)),
        cost_additional_last_period_raw=_int_text(payload.find(_TAG_COST_ADDITIONAL_LAST_PERIOD)),
        cost_details=[
            _parse_cost_detail(d) for d in payload.findall(_TAG_COST_ADDITIONAL_DETAIL_LAST_PERIOD)
        ],
        currency_numeric_code=currency,
    )


def _parse_cost_detail(elem: ET.Element) -> CostDetail:
    """One <espi:costAdditionalDetailLastPeriod> → CostDetail."""
    return CostDetail(
        amount_raw=_int_text(elem.find(_TAG_AMOUNT)) or 0,
        note=elem.findtext(_TAG_NOTE),
        item_kind=_int_text(elem.find(_TAG_ITEM_KIND)),
        unit_cost_raw=_int_text(elem.find(_TAG_UNIT_COST)),
    )


def _parse_interval_readings(block: ET.Element):
    """Yield UsageReading objects from an IntervalBlock element."""
    for r in block.findall(_TAG_INTERVAL_READING):
        time_period = r.find(_TAG_TIME_PERIOD)
        if time_period is None:
            continue
        start_text = time_period.findtext(_TAG_START)
        duration_text = time_period.findtext(_TAG_DURATION)
        value_text = r.findtext(_TAG_VALUE)
        if start_text is None or duration_text is None or value_text is None:
            continue
        try:
            start_epoch = int(start_text)
            duration_seconds = int(duration_text)
            value = float(value_text)
        except (TypeError, ValueError):
            continue
        yield UsageReading(
            start=datetime.fromtimestamp(start_epoch, tz=UTC),
            duration_seconds=duration_seconds,
            value=value,
            cost=_cost_amount(r.findtext(_TAG_COST)),
        )


def _cost_amount(text: str | None) -> float | None:
    """Per-interval `<cost>` (ESPI 1/100,000 currency units) → float currency amount, or None."""
    if text is None:
        return None
    try:
        return int(text) / _ESPI_COST_SCALE
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------------------
# Cross-entry assembly — collapse the link graph into UsagePoint → MeterReadingSeries trees.
# ---------------------------------------------------------------------------------------


def _assemble(
    raw_usage_points: dict[str, _RawUsagePoint],
    raw_meter_readings: dict[str, _RawMeterReading],
    raw_reading_types: dict[str, _RawReadingType],
    raw_interval_blocks: list[_RawIntervalBlock],
    raw_summaries: list[_RawUsageSummary],
) -> list[UsagePoint]:
    # Group IntervalBlocks by their parent MeterReading id.
    blocks_by_mr: dict[str, list[_RawIntervalBlock]] = {}
    for block in raw_interval_blocks:
        if block.meter_reading_id is None:
            continue
        blocks_by_mr.setdefault(block.meter_reading_id, []).append(block)

    # Group billing summaries by their parent UsagePoint id (resolved from the "related" link).
    summaries_by_up: dict[str, list[BillingSummary]] = {}
    for raw_summary in raw_summaries:
        if raw_summary.usage_point_id is None:
            continue
        summaries_by_up.setdefault(raw_summary.usage_point_id, []).append(raw_summary.summary)

    # The currency on a UsageSummary occasionally falls back to the ReadingType's currency
    # — pre-compute the first non-null currency we see per UsagePoint so we can fill it in.
    currency_by_up: dict[str, int | None] = {}
    for raw_mr in raw_meter_readings.values():
        up_id = raw_mr.usage_point_id
        if up_id is None or up_id in currency_by_up:
            continue
        rt = raw_reading_types.get(raw_mr.reading_type_id or "")
        if rt and rt.currency is not None:
            currency_by_up[up_id] = rt.currency

    out: list[UsagePoint] = []
    for up_id, raw_up in raw_usage_points.items():
        series: list[MeterReadingSeries] = []
        for mr_id, raw_mr in raw_meter_readings.items():
            if raw_mr.usage_point_id != up_id:
                continue
            rt = raw_reading_types.get(raw_mr.reading_type_id or "")
            scale = 10 ** (rt.power_of_ten_multiplier if rt else 0)
            readings = [
                UsageReading(
                    start=r.start,
                    duration_seconds=r.duration_seconds,
                    value=r.value * scale,
                    cost=r.cost,
                )
                for block in blocks_by_mr.get(mr_id, [])
                for r in block.readings
            ]
            readings.sort(key=lambda r: r.start)
            series.append(
                MeterReadingSeries(
                    meter_reading_id=mr_id,
                    reading_type=_normalize_reading_type(rt),
                    readings=readings,
                )
            )
        # Fill in the parent UsagePoint's currency on each summary that didn't carry one of
        # its own.
        fallback_currency = currency_by_up.get(up_id)
        summaries = [
            _with_currency_default(s, fallback_currency) for s in summaries_by_up.get(up_id, [])
        ]
        summaries.sort(key=lambda s: s.billing_period_start)
        out.append(
            UsagePoint(
                usage_point_id=up_id,
                service_kind=_service_kind(raw_up.kind),
                series=series,
                summaries=summaries,
            )
        )
    return out


def _with_currency_default(s: BillingSummary, fallback: int | None) -> BillingSummary:
    if s.currency_numeric_code is not None or fallback is None:
        return s
    return BillingSummary(
        billing_period_start=s.billing_period_start,
        billing_period_duration_seconds=s.billing_period_duration_seconds,
        bill_last_period_raw=s.bill_last_period_raw,
        cost_additional_last_period_raw=s.cost_additional_last_period_raw,
        cost_details=s.cost_details,
        currency_numeric_code=fallback,
    )


def _normalize_reading_type(rt: _RawReadingType | None) -> NormalizedReadingType:
    """ESPI integer codes → string enum names. Mirrors the Kotlin reference parser's mapping.

    Defaults preserve the shape when a MeterReading has no linked ReadingType (which
    happens with mocked / partial feeds) so the downstream stats writer can still skip
    cleanly via its `unit_of_measure` lookup.
    """
    if rt is None:
        return NormalizedReadingType(
            commodity="OTHER",
            flow_direction="OTHER",
            accumulation_behaviour="OTHER",
            interval_length_seconds=0,
            unit_of_measure="OTHER",
            unit_of_measure_symbol="?",
            power_of_ten_multiplier=0,
            currency_numeric_code=None,
        )
    uom_name, uom_symbol = _uom(rt.uom)
    return NormalizedReadingType(
        commodity=_commodity(rt.commodity),
        flow_direction=_flow_direction(rt.flow_direction),
        accumulation_behaviour=_accumulation(rt.accumulation_behaviour),
        interval_length_seconds=rt.interval_length_seconds,
        unit_of_measure=uom_name,
        unit_of_measure_symbol=uom_symbol,
        power_of_ten_multiplier=rt.power_of_ten_multiplier,
        currency_numeric_code=rt.currency,
    )


# ---------------------------------------------------------------------------------------
# ESPI code → enum-string mappers. Integer literals are the NAESB ESPI spec; suppressing
# the "magic number" lint is reasonable here because *they are* the magic.
# ---------------------------------------------------------------------------------------


# NOTE: each lookup uses `if kind is None else kind` rather than the seductive `kind or -1`
# — ESPI's electricity code IS zero (and so is uom=0 for currency-only ReadingTypes), so the
# truthy-zero trap silently routes them to "UNKNOWN" / "OTHER".


def _service_kind(kind: int | None) -> str:
    return {0: "ELECTRICITY", 1: "GAS", 2: "WATER", 4: "HEAT", 5: "COLD"}.get(
        -1 if kind is None else kind, "UNKNOWN"
    )


def _commodity(c: int | None) -> str:
    return {
        1: "ELECTRICITY_SECONDARY_METERED",
        2: "ELECTRICITY_PRIMARY_METERED",
        7: "NATURAL_GAS",
        9: "WATER",
    }.get(-1 if c is None else c, "OTHER")


def _flow_direction(f: int | None) -> str:
    # Codes per NAESB ESPI FlowDirectionKind (espi.xsd <xs:appinfo>): 1=forward, 4=net (forward −
    # reverse), 19=reverse, 20=total (forward + reverse). NOTE: 4/net and 20/total were previously
    # swapped here, so a "net" register showed as "Total" (and vice versa).
    return {1: "FORWARD", 4: "NET", 19: "REVERSE", 20: "TOTAL"}.get(-1 if f is None else f, "OTHER")


def _accumulation(a: int | None) -> str:
    return {
        1: "BULK_QUANTITY",
        3: "CUMULATIVE",
        4: "DELTA_DATA",
        12: "INSTANTANEOUS",
    }.get(-1 if a is None else a, "OTHER")


def _uom(u: int | None) -> tuple[str, str]:
    return {
        38: ("WATTS", "W"),
        72: ("WATT_HOURS", "Wh"),
        119: ("CUBIC_METERS", "m³"),
    }.get(-1 if u is None else u, ("OTHER", "?"))


# ---------------------------------------------------------------------------------------
# Atom link / element helpers.
# ---------------------------------------------------------------------------------------


def _link(entry: ET.Element, rel: str) -> str | None:
    for link in entry.findall(_TAG_LINK):
        if link.get("rel") == rel:
            return link.get("href")
    return None


def _related_id(entry: ET.Element, link_type: str, segment: str) -> str | None:
    for link in entry.findall(_TAG_LINK):
        if link.get("rel") == "related" and link.get("type") == link_type:
            return _id_from(link.get("href"), segment)
    return None


def _id_from(url: str | None, segment: str) -> str | None:
    """Extract the path component immediately following ``/<segment>/`` in ``url``.

    ESPI URLs follow ``.../resource/<Type>/<id>`` or
    ``.../resource/Parent/<pId>/<Type>/<cId>`` — pulling the trailing id by string match is
    stable across vendors because the resource hierarchy is fixed by the spec.
    """
    if url is None:
        return None
    marker = f"/{segment}/"
    idx = url.find(marker)
    if idx < 0:
        return None
    after = url[idx + len(marker) :]
    end = after.find("/")
    return after if end < 0 else after[:end]


def _child_int(parent: ET.Element, *path: str) -> int | None:
    cur: ET.Element | None = parent
    for tag in path:
        if cur is None:
            return None
        cur = cur.find(tag)
    return _int_text(cur)


def _int_text(elem: ET.Element | None) -> int | None:
    if elem is None or elem.text is None:
        return None
    try:
        return int(elem.text.strip())
    except ValueError:
        return None


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
