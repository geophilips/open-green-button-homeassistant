"""Parser regression tests for custodian-specific ESPI feed shapes."""

from custom_components.greenbutton.espi import _flow_direction, parse_usage_feed


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
