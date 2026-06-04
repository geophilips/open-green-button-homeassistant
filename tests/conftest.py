"""Shared pytest fixtures for the Open Green Button integration tests."""

from __future__ import annotations

from collections.abc import Generator

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_mock: object,  # noqa: ARG001  # MUST come before enable_custom_integrations
    enable_custom_integrations: None,  # noqa: ARG001  # fixture from pytest-homeassistant-custom-component
) -> Generator[None]:
    """Enable loading of custom_components/* during every test.

    ``recorder_mock`` is listed *first* so its underlying ``recorder_db_url`` fixture (which
    asserts ``not hass_fixture_setup``) runs before ``hass`` is built via the
    ``enable_custom_integrations`` chain. Without this ordering, our manifest's declared
    ``recorder`` dependency makes every test fail at integration load.
    """
    yield
