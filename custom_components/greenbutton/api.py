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


@dataclass(frozen=True, slots=True)
class UsageReading:
    """One hourly (or sub-hourly) consumption point."""

    start: datetime
    duration_seconds: int
    value: float


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
        """Best-effort total cost in currency units.

        Some utilities populate `billLastPeriod` with the grand total; others leave it 0
        and only fill in the detail items. We prefer `billLastPeriod` when present and
        positive, falling back to the sum of detail amounts.
        """
        bill = (self.bill_last_period_raw or 0) / 100_000.0
        if bill > 0:
            return bill
        return sum(d.amount for d in self.cost_details)


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


class OpenGbApiError(Exception):
    """Catch-all proxy-server error."""


class OpenGbClaimNotFoundError(OpenGbApiError):
    """The claim code was unknown, already used, or expired (HTTP 410)."""


class OpenGbAuthExpiredError(OpenGbApiError):
    """The utility rejected our refresh token — caller must trigger reauth.

    Distinct from generic ``OpenGbApiError`` so the coordinator can map this to
    ``ConfigEntryAuthFailed`` instead of treating it as a transient ``UpdateFailed``.
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
                Burlington Hydro's test-lab harness requires).
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
            if resp.status == 401:
                text = await resp.text()
                error_code = _safe_json_error(text)
                if error_code == "utility_auth_expired":
                    raise OpenGbAuthExpiredError(
                        "Utility refresh token expired; user must re-authorize",
                    )
                raise OpenGbApiError(f"POST /proxy/usage returned 401 ({error_code}): {text[:200]}")
            if resp.status != 200:
                text = await resp.text()
                raise OpenGbApiError(f"POST /proxy/usage returned {resp.status}: {text[:200]}")

            xml_bytes = await resp.read()
            new_credentials = _new_credentials_from_headers(resp.headers)

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


def _safe_json_error(text: str) -> str | None:
    """Pull the `error` field out of a JSON body, if present, without raising on malformed input.

    The proxy emits ``{"error": "...", "message": "..."}`` on 4xx/5xx, but we don't want to
    explode on an empty body or a different shape.
    """
    import json

    try:
        return json.loads(text).get("error")
    except (ValueError, AttributeError):
        return None


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
