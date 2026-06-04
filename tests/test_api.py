"""Tests for the proxy-server HTTP client — fetch_usage paths."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.greenbutton.api import (
    OpenGbApi,
    OpenGbApiError,
    OpenGbAuthExpiredError,
)

from .const import (
    MOCK_USAGE_RESPONSE_OK,
    MOCK_USAGE_RESPONSE_WITH_ROTATION,
    PROXY_USAGE_URL,
    SERVER_BASE_URL,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )


def _api(hass: HomeAssistant) -> OpenGbApi:
    return OpenGbApi(session=async_get_clientsession(hass), server_base_url=SERVER_BASE_URL)


async def test_fetch_usage_returns_normalized_dataclasses(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Happy path → camelCase JSON maps onto the snake_case dataclass tree."""
    aioclient_mock.post(PROXY_USAGE_URL, json=MOCK_USAGE_RESPONSE_OK)

    response = await _api(hass).fetch_usage("blob_value", "token_value")

    assert response.updated == datetime(2026, 6, 3, 14, 0, 0, tzinfo=UTC)
    assert response.new_credentials is None
    assert len(response.usage_points) == 1

    up = response.usage_points[0]
    assert up.usage_point_id == "e082e9a9-390b-58fb-8ca5-4ee707c95652"
    assert up.service_kind == "ELECTRICITY"
    assert len(up.series) == 1

    series = up.series[0]
    assert series.meter_reading_id == "022e1a41-a279-5af7-889e-3b46e67d9a01"
    assert series.reading_type.commodity == "ELECTRICITY_SECONDARY_METERED"
    assert series.reading_type.flow_direction == "FORWARD"
    assert series.reading_type.unit_of_measure == "WATT_HOURS"
    assert series.reading_type.interval_length_seconds == 3600
    assert series.reading_type.currency_numeric_code == 124

    assert len(series.readings) == 2
    assert series.readings[0].start == datetime(2026, 2, 24, 5, 0, 0, tzinfo=UTC)
    assert series.readings[0].duration_seconds == 3600
    assert series.readings[0].value == 1000.0
    assert series.readings[1].start == datetime(2026, 2, 24, 6, 0, 0, tzinfo=UTC)
    assert series.readings[1].value == 1500.0


async def test_fetch_usage_surfaces_new_credentials(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Refresh-token rotation surfaces verbatim in `response.new_credentials`."""
    aioclient_mock.post(PROXY_USAGE_URL, json=MOCK_USAGE_RESPONSE_WITH_ROTATION)

    response = await _api(hass).fetch_usage("blob_value", "token_value")

    assert response.new_credentials is not None
    assert response.new_credentials.encrypted_refresh_blob == "rotated_blob_value"
    assert response.new_credentials.proxy_token == "rotated_proxy_token"  # noqa: S105


async def test_fetch_usage_passes_published_window(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """publishedMin / publishedMax appear in the request body when supplied."""
    aioclient_mock.post(PROXY_USAGE_URL, json=MOCK_USAGE_RESPONSE_OK)

    await _api(hass).fetch_usage(
        "blob_value",
        "token_value",
        published_min=1771900000,
        published_max=1772000000,
    )

    last = aioclient_mock.mock_calls[-1]
    body = last[2]
    assert body["encryptedRefreshBlob"] == "blob_value"
    assert body["publishedMin"] == 1771900000
    assert body["publishedMax"] == 1772000000


async def test_fetch_usage_translates_utility_auth_expired_to_distinct_exception(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """401 with error=utility_auth_expired → OpenGbAuthExpiredError (reauth signal)."""
    aioclient_mock.post(
        PROXY_USAGE_URL,
        status=401,
        json={"error": "utility_auth_expired", "message": "refresh expired"},
    )

    with pytest.raises(OpenGbAuthExpiredError):
        await _api(hass).fetch_usage("blob_value", "token_value")


async def test_fetch_usage_treats_other_401_as_generic_error(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """401 invalid_credentials is *our* bug (proxy token mismatch), not a reauth trigger."""
    aioclient_mock.post(
        PROXY_USAGE_URL,
        status=401,
        json={"error": "invalid_credentials"},
    )

    with pytest.raises(OpenGbApiError) as exc_info:
        await _api(hass).fetch_usage("blob_value", "token_value")
    assert not isinstance(exc_info.value, OpenGbAuthExpiredError)


async def test_fetch_usage_treats_5xx_as_generic_error(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """5xx upstream errors surface as OpenGbApiError (UpdateFailed, not reauth)."""
    aioclient_mock.post(PROXY_USAGE_URL, status=502, json={"error": "utility_upstream_error"})

    with pytest.raises(OpenGbApiError) as exc_info:
        await _api(hass).fetch_usage("blob_value", "token_value")
    assert not isinstance(exc_info.value, OpenGbAuthExpiredError)
