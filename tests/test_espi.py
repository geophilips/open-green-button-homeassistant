"""Parser regression tests for custodian-specific ESPI feed shapes."""

from custom_components.greenbutton.espi import (
    _flow_direction,
    parse_customer_feed,
    parse_usage_feed,
)


def test_flow_direction_codes_match_espi_xsd() -> None:
    """NAESB ESPI FlowDirectionKind (espi.xsd): 1=forward, 4=net, 19=reverse, 20=total.

    Guards against the earlier bug where 4/net and 20/total were swapped (net showed as "Total").
    """
    assert _flow_direction(1) == "FORWARD"
    assert _flow_direction(4) == "NET"
    assert _flow_direction(19) == "REVERSE"
    assert _flow_direction(20) == "TOTAL"


# savagedata-style feed: resources are nested in the URL path
# (.../UsagePoint/{up}/MeterReading/{mr}/IntervalBlock/{ib}) and the MeterReading has NO flat
# rel="related" espi-entry/UsagePoint link — only its hierarchical self URL. The parser must derive
# the parent UsagePoint from that path, or interval readings never attach (the "0 readings" bug).
_SAVAGEDATA_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:f</id>
  <updated>2026-07-08T00:00:00Z</updated>
  <entry xmlns:espi="http://naesb.org/espi">
    <link rel="self" href="https://sd/Subscription/s/UsagePoint/UP1"/>
    <content><espi:UsagePoint><espi:ServiceCategory><espi:kind>0</espi:kind></espi:ServiceCategory></espi:UsagePoint></content>
  </entry>
  <entry xmlns:espi="http://naesb.org/espi">
    <link rel="self" href="https://sd/Subscription/s/UsagePoint/UP1/MeterReading/MR1"/>
    <link rel="related" type="espi-entry/ReadingType" href="https://sd/ReadingType/RT1"/>
    <content><espi:MeterReading/></content>
  </entry>
  <entry xmlns:espi="http://naesb.org/espi">
    <link rel="up" type="espi-feed/IntervalBlock" href="https://sd/Subscription/s/UsagePoint/UP1/MeterReading/MR1/IntervalBlock"/>
    <link rel="self" href="https://sd/Subscription/s/UsagePoint/UP1/MeterReading/MR1/IntervalBlock/IB1"/>
    <content>
      <espi:IntervalBlock>
        <espi:IntervalReading>
          <espi:cost>8700</espi:cost>
          <espi:timePeriod><espi:duration>3600</espi:duration><espi:start>1720432800</espi:start></espi:timePeriod>
          <espi:value>795000</espi:value>
          <espi:tou>3</espi:tou>
        </espi:IntervalReading>
        <espi:IntervalReading>
          <espi:timePeriod><espi:duration>3600</espi:duration><espi:start>1720436400</espi:start></espi:timePeriod>
          <espi:value>5271000</espi:value>
        </espi:IntervalReading>
      </espi:IntervalBlock>
    </content>
  </entry>
  <entry xmlns:espi="http://naesb.org/espi">
    <link rel="self" href="https://sd/ReadingType/RT1"/>
    <content><espi:ReadingType><espi:powerOfTenMultiplier>0</espi:powerOfTenMultiplier><espi:uom>72</espi:uom><espi:commodity>1</espi:commodity><espi:flowDirection>1</espi:flowDirection></espi:ReadingType></content>
  </entry>
</feed>"""


def test_hierarchical_urls_without_related_usagepoint_link_still_attach_readings() -> None:
    """savagedata omits the flat rel=related UsagePoint link on MeterReadings; deriving the
    UsagePoint from the hierarchical self URL is what keeps interval readings attached."""
    _updated, usage_points = parse_usage_feed(_SAVAGEDATA_FEED)
    assert len(usage_points) == 1
    readings = [r for series in usage_points[0].series for r in series.readings]
    assert len(readings) == 2, f"expected 2 readings, got {len(readings)}"
    assert [r.value for r in readings] == [795000.0, 5271000.0]


def test_per_interval_cost_parsed_when_present_else_none() -> None:
    """<cost> on an IntervalReading → UsageReading.cost (ESPI 1/100,000 → currency units);
    absent → None (so utilities without per-interval cost, e.g. Burlington, keep cost=None)."""
    _updated, usage_points = parse_usage_feed(_SAVAGEDATA_FEED)
    readings = usage_points[0].series[0].readings
    assert readings[0].cost == 0.087  # <cost>8700</cost>
    assert readings[1].cost is None  # second reading has no <cost>


# A RetailCustomer (customer-data) feed in the ESPI customer namespace — mirrors the real shape
# (CustomerAccount/accountId + ServiceLocation/mainAddress) used to distinguish two accounts at
# the same utility. Structure taken from an anonymized Green Button "Download My Data" feed.
_CUSTOMER_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:cust="http://naesb.org/espi/customer">
  <entry>
    <content>
      <cust:Customer>
        <cust:Organisation>
          <cust:organisationName>Jane Doe</cust:organisationName>
        </cust:Organisation>
      </cust:Customer>
    </content>
  </entry>
  <entry>
    <content>
      <cust:CustomerAccount>
        <cust:accountId>100001-0000001</cust:accountId>
      </cust:CustomerAccount>
    </content>
  </entry>
  <entry>
    <content>
      <cust:ServiceLocation>
        <cust:mainAddress>
          <cust:streetDetail>
            <cust:number>123</cust:number>
            <cust:name>EXAMPLE ST</cust:name>
            <cust:suiteNumber></cust:suiteNumber>
          </cust:streetDetail>
          <cust:townDetail>
            <cust:name>MILTON</cust:name>
            <cust:stateOrProvince>ON</cust:stateOrProvince>
            <cust:country>CA</cust:country>
          </cust:townDetail>
          <cust:postalCode>L0L 0L0</cust:postalCode>
        </cust:mainAddress>
      </cust:ServiceLocation>
    </content>
  </entry>
</feed>"""


def test_parse_customer_feed_extracts_account_address_and_name() -> None:
    """Account id, formatted service address, and organisation name all round-trip."""
    info = parse_customer_feed(_CUSTOMER_FEED)
    assert info is not None
    assert info.account_id == "100001-0000001"
    assert info.service_address == "123 EXAMPLE ST, MILTON ON, L0L 0L0"
    assert info.customer_name == "Jane Doe"
    # label prefers the service address (most human-recognizable).
    assert info.label == "123 EXAMPLE ST, MILTON ON, L0L 0L0"


def test_parse_customer_feed_falls_back_to_address_general() -> None:
    """A streetDetail with only <addressGeneral> (no number/name) still yields a street line."""
    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:cust="http://naesb.org/espi/customer">
  <entry>
    <content>
      <cust:ServiceLocation>
        <cust:mainAddress>
          <cust:streetDetail>
            <cust:addressGeneral>456 GENERAL AVE</cust:addressGeneral>
          </cust:streetDetail>
          <cust:townDetail>
            <cust:name>MILTON</cust:name>
            <cust:stateOrProvince>ON</cust:stateOrProvince>
          </cust:townDetail>
        </cust:mainAddress>
      </cust:ServiceLocation>
    </content>
  </entry>
</feed>"""
    info = parse_customer_feed(feed)
    assert info is not None
    assert info.service_address == "456 GENERAL AVE, MILTON ON"
    # No account id / name → label falls back to the address.
    assert info.label == "456 GENERAL AVE, MILTON ON"


def test_parse_customer_feed_label_prefers_account_when_no_address() -> None:
    """With no ServiceLocation, the label falls back to the account id."""
    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:cust="http://naesb.org/espi/customer">
  <entry>
    <content><cust:CustomerAccount><cust:accountId>ACC-42</cust:accountId></cust:CustomerAccount></content>
  </entry>
</feed>"""
    info = parse_customer_feed(feed)
    assert info is not None
    assert info.service_address is None
    assert info.label == "ACC-42"


def test_parse_customer_feed_returns_none_when_nothing_recognizable() -> None:
    """A feed with no customer payloads → None (nothing to label with)."""
    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:cust="http://naesb.org/espi/customer">
  <entry><content><cust:LocalTimeParameters/></content></entry>
</feed>"""
    assert parse_customer_feed(feed) is None
