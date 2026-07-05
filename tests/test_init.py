"""Integration-level tests — the `rebuild_statistics` service dispatch.

The service handler resolves targets from ``hass.data[DOMAIN]`` at call time, so these tests
register the service directly and stub the coordinators, keeping the focus on target
selection and validation (the rebuild mechanics themselves live in test_coordinator.py).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.greenbutton import _async_register_services
from custom_components.greenbutton.api import UsageResponse
from custom_components.greenbutton.const import (
    ATTR_CONFIG_ENTRY_ID,
    CONF_ENCRYPTED_REFRESH_BLOB,
    CONF_PROXY_TOKEN,
    CONF_UTILITY_ID,
    CONF_UTILITY_NAME,
    DOMAIN,
    SERVICE_REBUILD_STATISTICS,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


def _stub_coordinator() -> MagicMock:
    coord = MagicMock()
    coord.async_rebuild_statistics = AsyncMock()
    return coord


async def test_setup_arms_periodic_polling(hass: HomeAssistant) -> None:
    """Regression: an entity-less coordinator must keep a listener so it re-schedules polls.

    HA's DataUpdateCoordinator only re-arms its periodic refresh when it has ≥1 listener.
    This integration owns no entities, so setup must register a keep-alive listener —
    otherwise the coordinator fetches once at setup and never polls again.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_UTILITY_ID: "example_utility",
            CONF_UTILITY_NAME: "Example Utility",
            CONF_ENCRYPTED_REFRESH_BLOB: "blob",
            CONF_PROXY_TOKEN: "token",
        },
    )
    entry.add_to_hass(hass)

    empty = UsageResponse(updated=None, usage_points=[], new_credentials=None)
    with (
        patch(
            "custom_components.greenbutton.OpenGbApi.fetch_usage",
            new=AsyncMock(return_value=empty),
        ),
        patch(
            "custom_components.greenbutton.coordinator.import_usage_statistics",
            new=AsyncMock(),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    # A live listener is what keeps _schedule_refresh firing at DEFAULT_SCAN_INTERVAL.
    assert coordinator._listeners, "coordinator has no listener; periodic polling won't re-arm"
    assert coordinator._unsub_refresh is not None, "next refresh is not scheduled"


async def test_service_is_registered(hass: HomeAssistant) -> None:
    """_async_register_services exposes greenbutton.rebuild_statistics."""
    _async_register_services(hass)
    assert hass.services.has_service(DOMAIN, SERVICE_REBUILD_STATISTICS)


async def test_service_targets_a_single_entry(hass: HomeAssistant) -> None:
    """A config_entry_id rebuilds only that account."""
    coord_a, coord_b = _stub_coordinator(), _stub_coordinator()
    hass.data[DOMAIN] = {"entry_a": coord_a, "entry_b": coord_b}
    _async_register_services(hass)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_REBUILD_STATISTICS,
        {ATTR_CONFIG_ENTRY_ID: "entry_a"},
        blocking=True,
    )

    coord_a.async_rebuild_statistics.assert_awaited_once()
    coord_b.async_rebuild_statistics.assert_not_awaited()


async def test_service_without_target_rebuilds_all_entries(hass: HomeAssistant) -> None:
    """Omitting the target rebuilds every loaded account."""
    coord_a, coord_b = _stub_coordinator(), _stub_coordinator()
    hass.data[DOMAIN] = {"entry_a": coord_a, "entry_b": coord_b}
    _async_register_services(hass)

    await hass.services.async_call(DOMAIN, SERVICE_REBUILD_STATISTICS, {}, blocking=True)

    coord_a.async_rebuild_statistics.assert_awaited_once()
    coord_b.async_rebuild_statistics.assert_awaited_once()


async def test_service_rejects_unknown_entry(hass: HomeAssistant) -> None:
    """An unknown config_entry_id is a user error, not a silent no-op."""
    hass.data[DOMAIN] = {"entry_a": _stub_coordinator()}
    _async_register_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REBUILD_STATISTICS,
            {ATTR_CONFIG_ENTRY_ID: "does_not_exist"},
            blocking=True,
        )


async def test_service_errors_when_no_entries_loaded(hass: HomeAssistant) -> None:
    """With nothing configured, an untargeted call reports there's nothing to do."""
    hass.data[DOMAIN] = {}
    _async_register_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, SERVICE_REBUILD_STATISTICS, {}, blocking=True)
