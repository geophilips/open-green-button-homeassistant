"""Fixed test data used across config-flow tests."""

from __future__ import annotations

SERVER_BASE_URL = "https://api.opengreenbutton.org"
UTILITIES_URL = f"{SERVER_BASE_URL}/utilities"


def claim_url(code: str) -> str:
    """Return the claim-redemption URL for a specific code."""
    return f"{SERVER_BASE_URL}/claim/{code}"


MOCK_UTILITIES = [
    {"id": "burlington_hydro", "displayName": "Burlington Hydro"},
    {"id": "pge", "displayName": "Pacific Gas & Electric"},
]

VALID_CLAIM_CODE = "gb_live_abc123def456"

MOCK_CLAIM_RESPONSE = {
    "utilityId": "burlington_hydro",
    "encryptedRefreshBlob": "AQABABCDEF==",
    "proxyToken": "proxy_token_xyz",
    "subscriptionUri": "https://utility.example/Subscription/42",
    "scope": "FB=1_3_4_5;IntervalDuration=3600",
    "currentApiVersion": "2026-05-22",
}

PROXY_USAGE_URL = f"{SERVER_BASE_URL}/proxy/usage"

# Minimal but realistic /proxy/usage XML body — one UsagePoint (electricity), one
# MeterReading, one IntervalBlock with two hourly readings, one ReadingType. After parsing
# this exercises the full domain shape: ServiceCategory.kind → ELECTRICITY,
# commodity=1 → ELECTRICITY_SECONDARY_METERED, flowDirection=1 → FORWARD,
# accumulationBehaviour=4 → DELTA_DATA, uom=72 → WATT_HOURS.
#
# 1740373200 = 2025-02-24T05:00:00Z, 1740376800 = 2025-02-24T06:00:00Z — round-numbered for
# readable assertions.
MOCK_USAGE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:test-feed</id>
  <title>Mock</title>
  <updated>2026-06-03T14:00:00Z</updated>
  <entry xmlns:espi="http://naesb.org/espi">
    <id>urn:uuid:up</id>
    <link rel="self" href="https://utility.mock/UsagePoint/e082e9a9-390b-58fb-8ca5-4ee707c95652"/>
    <content>
      <espi:UsagePoint>
        <espi:ServiceCategory><espi:kind>0</espi:kind></espi:ServiceCategory>
        <espi:status>1</espi:status>
      </espi:UsagePoint>
    </content>
  </entry>
  <entry xmlns:espi="http://naesb.org/espi">
    <id>urn:uuid:mr</id>
    <link rel="self" href="https://utility.mock/UsagePoint/e082e9a9-390b-58fb-8ca5-4ee707c95652/MeterReading/022e1a41-a279-5af7-889e-3b46e67d9a01"/>
    <link rel="related" type="espi-entry/UsagePoint" href="https://utility.mock/UsagePoint/e082e9a9-390b-58fb-8ca5-4ee707c95652"/>
    <link rel="related" type="espi-entry/ReadingType" href="https://utility.mock/ReadingType/100001"/>
    <content><espi:MeterReading/></content>
  </entry>
  <entry xmlns:espi="http://naesb.org/espi">
    <id>urn:uuid:ib</id>
    <link rel="up" type="espi-feed/IntervalBlock" href="https://utility.mock/UsagePoint/e082e9a9-390b-58fb-8ca5-4ee707c95652/MeterReading/022e1a41-a279-5af7-889e-3b46e67d9a01/IntervalBlock"/>
    <link rel="self" href="https://utility.mock/UsagePoint/e082e9a9-390b-58fb-8ca5-4ee707c95652/MeterReading/022e1a41-a279-5af7-889e-3b46e67d9a01/IntervalBlock/ib1"/>
    <content>
      <espi:IntervalBlock>
        <espi:interval>
          <espi:duration>7200</espi:duration>
          <espi:start>1740373200</espi:start>
        </espi:interval>
        <espi:IntervalReading>
          <espi:timePeriod><espi:duration>3600</espi:duration><espi:start>1740373200</espi:start></espi:timePeriod>
          <espi:value>1000</espi:value>
        </espi:IntervalReading>
        <espi:IntervalReading>
          <espi:timePeriod><espi:duration>3600</espi:duration><espi:start>1740376800</espi:start></espi:timePeriod>
          <espi:value>1500</espi:value>
        </espi:IntervalReading>
      </espi:IntervalBlock>
    </content>
  </entry>
  <entry xmlns:espi="http://naesb.org/espi">
    <id>urn:uuid:rt</id>
    <link rel="self" href="https://utility.mock/ReadingType/100001"/>
    <content>
      <espi:ReadingType>
        <espi:accumulationBehaviour>4</espi:accumulationBehaviour>
        <espi:commodity>1</espi:commodity>
        <espi:currency>124</espi:currency>
        <espi:flowDirection>1</espi:flowDirection>
        <espi:intervalLength>3600</espi:intervalLength>
        <espi:powerOfTenMultiplier>0</espi:powerOfTenMultiplier>
        <espi:uom>72</espi:uom>
      </espi:ReadingType>
    </content>
  </entry>
  <entry xmlns:espi="http://naesb.org/espi">
    <id>urn:uuid:us</id>
    <link rel="self" href="https://utility.mock/UsagePoint/e082e9a9-390b-58fb-8ca5-4ee707c95652/UsageSummary/sum1"/>
    <link rel="related" type="espi-entry/UsagePoint" href="https://utility.mock/UsagePoint/e082e9a9-390b-58fb-8ca5-4ee707c95652"/>
    <content>
      <espi:UsageSummary>
        <!-- billingPeriod start = 1716129608 = 2024-05-19T13:20:08Z; duration = 30 days -->
        <espi:billingPeriod>
          <espi:duration>2592000</espi:duration>
          <espi:start>1716129608</espi:start>
        </espi:billingPeriod>
        <!-- billLastPeriod 10250000 (raw, 1/100,000) = $102.50 in currency units -->
        <espi:billLastPeriod>10250000</espi:billLastPeriod>
        <espi:costAdditionalLastPeriod>0</espi:costAdditionalLastPeriod>
        <espi:costAdditionalDetailLastPeriod>
          <espi:amount>2700</espi:amount>
          <espi:note>Regulatory Charges</espi:note>
          <espi:itemKind>0</espi:itemKind>
          <espi:unitCost>0</espi:unitCost>
        </espi:costAdditionalDetailLastPeriod>
        <espi:costAdditionalDetailLastPeriod>
          <espi:amount>61780</espi:amount>
          <espi:note>Delivery</espi:note>
          <espi:itemKind>0</espi:itemKind>
          <espi:unitCost>0</espi:unitCost>
        </espi:costAdditionalDetailLastPeriod>
      </espi:UsageSummary>
    </content>
  </entry>
</feed>"""

# Server emits these on rotation. Header NAMES must match what the server sends (and what
# api.py reads). aioclient_mock accepts headers as a dict on `headers=`.
ROTATED_HEADERS = {
    "OpenGB-New-Encrypted-Refresh-Blob": "rotated_blob_value",
    "OpenGB-New-Proxy-Token": "rotated_proxy_token",
}
