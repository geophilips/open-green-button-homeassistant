"""Integration-level tests — the `rebuild_statistics` service dispatch.

The service handler resolves targets from ``hass.data[DOMAIN]`` at call time, so these tests
register the service directly and stub the coordinators, keeping the focus on target
selection and validation (the rebuild mechanics themselves live in test_coordinator.py).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.greenbutton import _async_register_services
from custom_components.greenbutton.const import (
    ATTR_CONFIG_ENTRY_ID,
    DOMAIN,
    SERVICE_REBUILD_STATISTICS,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


def _stub_coordinator() -> MagicMock:
    coord = MagicMock()
    coord.async_rebuild_statistics = AsyncMock()
    return coord


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
