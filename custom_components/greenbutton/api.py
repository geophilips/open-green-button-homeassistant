"""Thin async HTTP client for the Open Green Button proxy server.

Endpoints used:
  - GET /utilities          — discovery for the utility picker
  - POST /claim/{code}      — single-use claim-code redemption
  - POST /proxy/usage       — pulls a window of usage data; returns the utility's raw ESPI
                              Atom XML which we parse locally via [espi.parse_usage_feed]

The proxy server is stateless and a pure pass-through for the data fetch: every
`/proxy/usage` call carries the encrypted refresh blob and the proof-of-possession proxy
token. The proxy decrypts the blob, refreshes the access token at the utility, GETs the
subscription URI, and streams the response body back verbatim. Refresh-token rotation
(RFC 6749 §6) surfaces via response **headers** (`OpenGB-New-Encrypted-Refresh-Blob` +
`OpenGB-New-Proxy-Token`) so the body stays pure ESPI; the coordinator persists rotated
credentials back into the config entry when present.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import aiohttp

from .const import API_VERSION

# An async callable that receives raw upstream XML bytes and persists them somewhere.
# The bytes are released as soon as the call returns — no caller holds a reference.
RawXmlSink = Callable[[bytes], Awaitable[None]]

_LOGGER = logging.getLogger(__name__)

# How much of a proxy error body to keep in raised-exception messages. The proxy embeds the
# utility's own error (status + body snippet) inside its JSON `message`, after a ~150-char URL —
# so a tight cap hides the actual upstream reason (e.g. why a Data Custodian 400s a data request).
_MAX_ERROR_CHARS = 1200


@dataclass(frozen=True, slots=True)
class UtilitySummary:
    """A utility entry as returned by GET /utilities."""

    id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class ClaimResponse:
    """A successful POST /claim/{code} response."""

    utility_id: str
    encrypted_refresh_blob: str
    proxy_token: str
    subscription_uri: str | None
    scope: str | None
    current_api_version: str
    # Per-utility initial-backfill window in seconds. Older servers omit it → None, and the
    # coordinator falls back to its local default.
    initial_history_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class UsageReading:
    """One hourly (or sub-hourly) consumption point."""

    start: datetime
    duration_seconds: int
    value: float
    # Per-interval cost in the ReadingType's currency (float currency units, already divided out of
    # ESPI's 1/100,000 raw). Present when the utility itemizes cost on each IntervalReading (e.g.
    # savagedata/Milton); None when it only bills via a monthly UsageSummary (e.g. Burlington).
    cost: float | None = None


@dataclass(frozen=True, slots=True)
class NormalizedReadingType:
    """ReadingType metadata flattened onto each series — the server already maps ESPI integer
    codes to enum names, so these strings are stable across utility implementations."""

    commodity: str
    flow_direction: str
    accumulation_behaviour: str
    interval_length_seconds: int
    unit_of_measure: str
    unit_of_measure_symbol: str
    power_of_ten_multiplier: int
    currency_numeric_code: int | None


@dataclass(frozen=True, slots=True)
class MeterReadingSeries:
    """All readings for one (UsagePoint, MeterReading, ReadingType) tuple."""

    meter_reading_id: str
    reading_type: NormalizedReadingType
    readings: list[UsageReading]


# Normalized (lowercased, whitespace-collapsed) notes for UsageSummary detail lines that are
# NOT this period's charges: running-balance bookkeeping and subtotals that already aggregate
# the real charge lines. Summing these back in is what triples a bill. See
# [CostDetail.is_period_charge].
_NON_CHARGE_NOTES = frozenset(
    {
        "balance forward",
        "payments received",
        "new charges this period",
        "total amount due",
    }
)


@dataclass(frozen=True, slots=True)
class CostDetail:
    """One line item in a UsageSummary's cost breakdown.

    ESPI reports cost amounts in **1/100,000 of the parent's currency unit** — see
    [`amount`][] for the float currency value. The raw integer is preserved for callers
    that want to inspect or round differently.
    """

    amount_raw: int
    note: str | None
    item_kind: int | None
    unit_cost_raw: int | None

    @property
    def amount(self) -> float:
        """Amount in currency units (e.g. dollars). Raw value divided by 100,000."""
        return self.amount_raw / 100_000.0

    @property
    def normalized_note(self) -> str:
        """Lowercased, whitespace-collapsed note for label matching (``""`` when absent)."""
        return " ".join(self.note.lower().split()) if self.note else ""

    @property
    def is_period_charge(self) -> bool:
        """True when this line item is an actual charge for the period.

        A UsageSummary's detail list mixes three kinds of line item and only the first is a
        charge to be summed:

          - **Real charges** — Off/Mid/On-Peak energy, Delivery, Regulatory, tax, rebates.
          - **Running-balance bookkeeping** — "Balance Forward" / "Payments Received". These
            are prior-invoice carry-over, not this period's consumption.
          - **Subtotals** — "New Charges This Period" / "Total Amount Due", which *already*
            aggregate the real charge lines.

        Summing everything indiscriminately is what inflates a bill ~3× (the charges, plus
        the New-Charges subtotal that equals them, plus the Total-Amount-Due subtotal that
        equals them again) — see the Burlington Hydro feed. Excluding the bookkeeping and
        subtotal notes leaves exactly the period's charges.
        """
        return self.normalized_note not in _NON_CHARGE_NOTES


@dataclass(frozen=True, slots=True)
class BillingSummary:
    """One periodic billing summary parsed from an ESPI UsageSummary entry.

    Typically one summary per billing period (monthly) per UsagePoint. Carries the period
    total, an "additional charges" subtotal, and detailed line items (Delivery, Off-Peak,
    On-Peak, Mid-Peak, etc. in Ontario's case).
    """

    billing_period_start: datetime
    billing_period_duration_seconds: int
    bill_last_period_raw: int | None
    cost_additional_last_period_raw: int | None
    cost_details: list[CostDetail]
    currency_numeric_code: int | None

    @property
    def total_cost(self) -> float:
        """Best-effort total cost for *this billing period* in currency units.

        Order of preference:

        1. ``billLastPeriod`` when present and positive — some utilities put the grand total
           here directly.
        2. The sum of the genuine charge line items — every detail except running-balance
           bookkeeping and subtotals (see [`CostDetail.is_period_charge`]).

        We deliberately do NOT trust the feed's own "New Charges This Period" / "Total Amount
        Due" subtotal lines. Burlington Hydro stamps a corrupt value there on roughly every
        other bill: a period whose itemized charges sum to $187.73 is reported as "New Charges
        This Period = $502.29", with no line item for the $314.56 difference — and the bill's
        own H.S.T. line (13% of the pre-rebate subtotal) only reconciles against the itemized
        $187.73, confirming the subtotal is the wrong number. Summing the itemized charges is
        correct there and identical to the subtotal on well-formed bills, so it's the single
        rule for every feed. (A utility that under-itemizes — putting a real total only in the
        subtotal — would need revisiting, but none in scope does.)
        """
        bill = (self.bill_last_period_raw or 0) / 100_000.0
        if bill > 0:
            return bill
        return sum(d.amount for d in self.cost_details if d.is_period_charge)


@dataclass(frozen=True, slots=True)
class UsagePoint:
    """A logical metering point (e.g. the electric meter at a service address)."""

    usage_point_id: str
    service_kind: str
    series: list[MeterReadingSeries]
    summaries: list[BillingSummary] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class NewCredentials:
    """Rotated refresh blob + proxy token to persist into the config entry."""

    encrypted_refresh_blob: str
    proxy_token: str


@dataclass(frozen=True, slots=True)
class UsageResponse:
    """A successful POST /proxy/usage response."""

    updated: datetime | None
    usage_points: list[UsagePoint]
    new_credentials: NewCredentials | None


@dataclass(frozen=True, slots=True)
class CustomerInfo:
    """Identifying customer details parsed from an ESPI RetailCustomer (customer-data) feed.

    Used to give otherwise-identical config entries a human-distinguishable label. Two accounts
    at the same utility (e.g. Milton Hydro's two sandbox test customers) show up as identical
    entries until we surface the service address / account number.
    """

    account_id: str | None
    service_address: str | None
    customer_name: str | None

    @property
    def label(self) -> str | None:
        """Best short one-line distinguisher for this customer, or None if nothing usable.

        Prefers the service address (most human-recognizable), then the account id, then the
        customer/organisation name.
        """
        return self.service_address or self.account_id or self.customer_name


@dataclass(frozen=True, slots=True)
class CustomerResponse:
    """A successful POST /proxy/customer response.

    ``customer`` is None when the feed carried no recognizable customer payload (the parse
    yielded nothing) — distinct from a fetch failure, which raises.
    """

    customer: CustomerInfo | None
    new_credentials: NewCredentials | None


class OpenGbApiError(Exception):
    """Catch-all proxy-server error.

    Carries any rotated credentials the proxy returned on the *error* response (the
    ``OpenGB-New-*`` headers). The proxy refreshes the access token — and the utility may
    redeem a one-time refresh token (e.g. savagedata's OpenIddict) — *before* the resource
    fetch, so an upstream failure can still come with a fresh blob. The caller MUST persist
    ``new_credentials`` before treating the error as retryable; otherwise the next attempt
    reuses a burned refresh token and cascades into a spurious reauth.
    """

    def __init__(self, *args: object, new_credentials: NewCredentials | None = None) -> None:
        """Standard exception args plus optional rotated credentials from the response headers."""
        super().__init__(*args)
        self.new_credentials = new_credentials


class OpenGbClaimNotFoundError(OpenGbApiError):
    """The claim code was unknown, already used, or expired (HTTP 410)."""


class OpenGbAuthExpiredError(OpenGbApiError):
    """The utility rejected our refresh token — caller must trigger reauth.

    Distinct from generic ``OpenGbApiError`` so the coordinator can map this to
    ``ConfigEntryAuthFailed`` instead of treating it as a transient ``UpdateFailed``.
    """


class OpenGbDataPendingError(OpenGbApiError):
    """The utility is preparing the data asynchronously (ESPI async batch — HTTP 202).

    The proxy surfaces this as ``utility_data_pending`` (HTTP 202) when the utility answers
    the batch request with "data is being collected, available later" — which only happens
    for very large datasets. We don't implement the Notification/BatchList retrieval flow
    yet, so the coordinator maps this to a repair issue (with a link to the tracking GitHub
    issue) instead of treating it as a transient ``UpdateFailed``.
    """


class OpenGbPermanentError(OpenGbApiError):
    """The request failed with a permanent (4xx) status — retrying will not help.

    The proxy propagates the resource server's own HTTP status (it no longer collapses every
    upstream failure to 502). A 4xx therefore means the utility gave a definitive client-error
    answer for this request/scope/credential — e.g. Burlington's ``403 access_denied`` for a
    customer-data resource our OAuth scope doesn't cover — as opposed to a transient 5xx. The
    caller should stop retrying rather than loop forever. (``408 Request Timeout`` and ``429
    Too Many Requests`` are excluded — those 4xx *are* retryable — see [fetch_customer].)
    """


class OpenGbApi:
    """Async client for the Open Green Button proxy server."""

    def __init__(self, session: aiohttp.ClientSession, server_base_url: str) -> None:
        """Hold an aiohttp session (typically HA's shared one) and the proxy base URL."""
        self._session = session
        self._base = server_base_url.rstrip("/")

    @property
    def headers(self) -> dict[str, str]:
        """Common request headers. Stripe-style API version on every call."""
        return {"OpenGB-Api-Version": API_VERSION}

    async def list_utilities(self) -> list[UtilitySummary]:
        """Fetch the configured utility list. Returns [] if the server has none configured."""
        url = f"{self._base}/utilities"
        async with self._session.get(url, headers=self.headers) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise OpenGbApiError(f"GET /utilities returned {resp.status}: {body[:200]}")
            payload: list[dict[str, Any]] = await resp.json()
        return [UtilitySummary(id=item["id"], display_name=item["displayName"]) for item in payload]

    async def redeem_claim(self, claim_code: str) -> ClaimResponse:
        """Atomically redeem a one-time claim code generated by the OAuth callback.

        Raises:
            OpenGbClaimNotFoundError: claim code unknown / expired / already used (HTTP 410).
            OpenGbApiError: any other failure (network, 4xx other than 410, 5xx).
        """
        url = f"{self._base}/claim/{claim_code}"
        async with self._session.post(url, headers=self.headers) as resp:
            if resp.status == 410:
                raise OpenGbClaimNotFoundError("Claim code is unknown, expired, or already used")
            if resp.status != 200:
                body = await resp.text()
                raise OpenGbApiError(f"POST /claim returned {resp.status}: {body[:200]}")
            payload: dict[str, Any] = await resp.json()
        return ClaimResponse(
            utility_id=payload["utilityId"],
            encrypted_refresh_blob=payload["encryptedRefreshBlob"],
            proxy_token=payload["proxyToken"],
            subscription_uri=payload.get("subscriptionUri"),
            scope=payload.get("scope"),
            current_api_version=payload["currentApiVersion"],
            initial_history_seconds=payload.get("initialHistorySeconds"),
        )

    async def fetch_usage(
        self,
        encrypted_refresh_blob: str,
        proxy_token: str,
        published_min: datetime | None = None,
        published_max: datetime | None = None,
        raw_xml_sink: RawXmlSink | None = None,
    ) -> UsageResponse:
        """Pull a window of usage data from the proxy.

        The proxy returns the utility's raw ESPI Atom XML; we parse it locally via
        [espi.parse_usage_feed]. Rotated credentials, when present, arrive as response
        headers rather than in the body.

        Args:
            encrypted_refresh_blob: persisted blob from the config entry.
            proxy_token: persisted bearer token from the config entry.
            published_min: optional ESPI `published-min` filter — must be tz-aware.
                Serialized to ISO 8601 with `Z` suffix on the wire (the format
                the Green Button test-lab harness requires).
            published_max: optional ESPI `published-max` filter — same constraints.
            raw_xml_sink: optional async callback invoked with the raw response body
                bytes between the read and the parse. Used by the coordinator to persist
                the body to disk for diagnostics — passed in (instead of returned on
                [UsageResponse]) so the bytes can be garbage-collected as soon as parsing
                finishes. No memory residency beyond a single request.

        Raises:
            OpenGbAuthExpiredError: utility refused our refresh token (HTTP 401 with
                `error=utility_auth_expired`). Caller should trigger the reauth flow.
            OpenGbApiError: any other failure (network, 401 with different error code,
                4xx body issue, 5xx).
        """
        # Local import to avoid an import cycle (espi.py imports the dataclasses defined
        # above in this module).
        from .espi import parse_usage_feed

        url = f"{self._base}/proxy/usage"
        body: dict[str, Any] = {"encryptedRefreshBlob": encrypted_refresh_blob}
        if published_min is not None:
            body["publishedMin"] = _to_iso_z(published_min)
        if published_max is not None:
            body["publishedMax"] = _to_iso_z(published_max)

        headers = {**self.headers, "Authorization": f"Bearer {proxy_token}"}
        async with self._session.post(url, headers=headers, json=body) as resp:
            # Rotated credentials can accompany ANY response, not just 200. The proxy refreshes
            # the access token (and the utility may redeem a one-time refresh token) BEFORE the
            # resource fetch, so a subsequent upstream failure still carries a new blob we must
            # not drop. Read the headers up front and attach them to every error so the
            # coordinator can persist them before retrying.
            new_credentials = _new_credentials_from_headers(resp.headers)
            if resp.status == 401:
                text = await resp.text()
                error_code = _safe_json_field(text, "error")
                if error_code == "utility_auth_expired":
                    # Surface the utility's own reason (the token endpoint's error_description,
                    # forwarded by the proxy in `message`) so HA shows *why* re-auth is needed
                    # — e.g. "refresh token expired" vs "already been redeemed".
                    detail = _safe_json_field(text, "message")
                    reason = f" ({detail})" if detail else ""
                    raise OpenGbAuthExpiredError(
                        f"Utility rejected the refresh token; re-authorization required{reason}",
                        new_credentials=new_credentials,
                    )
                raise OpenGbApiError(
                    f"POST /proxy/usage returned 401 ({error_code}): {text[:_MAX_ERROR_CHARS]}",
                    new_credentials=new_credentials,
                )
            if resp.status == 202:
                # The proxy passes the utility's 202 Accepted through as `utility_data_pending`:
                # the dataset is large enough that the utility is assembling it out-of-band
                # (ESPI async batch). We don't implement the Notification/BatchList retrieval
                # flow yet, so raise a distinct error the coordinator turns into a repair issue.
                text = await resp.text()
                error_code = _safe_json_field(text, "error")
                raise OpenGbDataPendingError(
                    "Utility is preparing data asynchronously (HTTP 202, "
                    f"{error_code or 'utility_data_pending'}); background data loads are not "
                    "yet supported",
                    new_credentials=new_credentials,
                )
            if resp.status != 200:
                text = await resp.text()
                raise OpenGbApiError(
                    f"POST /proxy/usage returned {resp.status}: {text[:_MAX_ERROR_CHARS]}",
                    new_credentials=new_credentials,
                )

            xml_bytes = await resp.read()

        if raw_xml_sink is not None:
            # Persist before parsing so a parse-failure path still leaves a debuggable
            # artifact on disk (which is useful for figuring out *why* it failed).
            await raw_xml_sink(xml_bytes)

        updated, usage_points = parse_usage_feed(xml_bytes)
        # xml_bytes goes out of scope here; the only persistent copy is whatever the sink
        # decided to do with it (default: nothing).
        return UsageResponse(
            updated=updated,
            usage_points=usage_points,
            new_credentials=new_credentials,
        )

    async def fetch_customer(
        self,
        encrypted_refresh_blob: str,
        proxy_token: str,
    ) -> CustomerResponse:
        """Pull the customer-data (ESPI RetailCustomer) feed from the proxy.

        The proxy locates the customer resource from the ESPI Authorization resource's
        ``customerResourceURI`` and streams back the raw customer Atom XML, which we parse
        locally via [espi.parse_customer_feed]. Used to give an otherwise-identical config
        entry a human-distinguishable label (service address / account number).

        Rotated credentials arrive the same way as [fetch_usage] — via ``OpenGB-New-*``
        response headers — and are attached to any raised error so the caller can persist them.

        Raises:
            OpenGbAuthExpiredError: utility refused our refresh token (HTTP 401 with
                `error=utility_auth_expired`).
            OpenGbApiError: any other failure (network, other 4xx/5xx). Notably the proxy
                returns 400 `no_customer_uri` when the custodian advertises no customer
                resource — a permanent condition the caller should not keep retrying.
        """
        # Local import to avoid an import cycle (espi.py imports this module's dataclasses).
        from .espi import parse_customer_feed

        url = f"{self._base}/proxy/customer"
        body: dict[str, Any] = {"encryptedRefreshBlob": encrypted_refresh_blob}
        headers = {**self.headers, "Authorization": f"Bearer {proxy_token}"}
        async with self._session.post(url, headers=headers, json=body) as resp:
            new_credentials = _new_credentials_from_headers(resp.headers)
            if resp.status == 401:
                text = await resp.text()
                error_code = _safe_json_field(text, "error")
                if error_code == "utility_auth_expired":
                    detail = _safe_json_field(text, "message")
                    reason = f" ({detail})" if detail else ""
                    raise OpenGbAuthExpiredError(
                        f"Utility rejected the refresh token; re-authorization required{reason}",
                        new_credentials=new_credentials,
                    )
                raise OpenGbApiError(
                    f"POST /proxy/customer returned 401 ({error_code}): {text[:_MAX_ERROR_CHARS]}",
                    new_credentials=new_credentials,
                )
            if resp.status != 200:
                text = await resp.text()
                # The proxy propagates the resource server's own status. A 4xx is permanent for
                # this scope/credential (e.g. 403 access_denied on a customer resource the utility
                # won't grant us) — raise a distinct error so the caller stops retrying, rather
                # than a generic (retryable) OpenGbApiError. 408/429 are the retryable 4xx, so
                # they stay transient. 5xx stays transient too.
                if 400 <= resp.status < 500 and resp.status not in (408, 429):
                    raise OpenGbPermanentError(
                        f"POST /proxy/customer returned {resp.status} (permanent): "
                        f"{text[:_MAX_ERROR_CHARS]}",
                        new_credentials=new_credentials,
                    )
                raise OpenGbApiError(
                    f"POST /proxy/customer returned {resp.status}: {text[:_MAX_ERROR_CHARS]}",
                    new_credentials=new_credentials,
                )
            xml_bytes = await resp.read()

        customer = parse_customer_feed(xml_bytes)
        return CustomerResponse(customer=customer, new_credentials=new_credentials)


def _safe_json_field(text: str, field: str) -> str | None:
    """Pull a top-level string field out of a JSON body, without raising on malformed input.

    The proxy emits ``{"error": "...", "message": "..."}`` on 4xx/5xx (the `message` carries the
    utility's own error, e.g. the token endpoint's `error_description`), but we don't want to
    explode on an empty body or a different shape.
    """
    import json

    try:
        value = json.loads(text).get(field)
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, str) else None


_HEADER_NEW_ENCRYPTED_REFRESH_BLOB = "OpenGB-New-Encrypted-Refresh-Blob"
_HEADER_NEW_PROXY_TOKEN = "OpenGB-New-Proxy-Token"  # noqa: S105 — header name, not a token


def _new_credentials_from_headers(headers: Any) -> NewCredentials | None:
    """Extract rotated credentials from the OpenGB-New-* response headers, if both present.

    The proxy emits these only when the utility actually rotated the refresh token; absence
    means "your stored credentials are still valid, don't update the config entry."
    """
    blob = headers.get(_HEADER_NEW_ENCRYPTED_REFRESH_BLOB)
    token = headers.get(_HEADER_NEW_PROXY_TOKEN)
    if not blob or not token:
        return None
    return NewCredentials(encrypted_refresh_blob=blob, proxy_token=token)


def _to_iso_z(dt: datetime) -> str:
    """Serialize a tz-aware datetime to ISO 8601 with `Z` suffix at second precision.

    Two non-obvious normalizations:
      - Python's stdlib emits ``+00:00`` for UTC; the ESPI harness wants ``Z``. Convert.
      - The Green Button Alliance test-lab harness rejects timestamps with subsecond
        precision (it returns HTTP 400 with an empty body). ``datetime.now(UTC)`` carries
        microseconds, which leak through ``.isoformat()`` into the URL — strip them. ESPI
        readings are hourly, so we never need sub-second resolution on the wire anyway.
    """
    if dt.tzinfo is None:
        raise ValueError("published_min/max must be timezone-aware")
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
