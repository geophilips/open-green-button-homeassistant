"""Shared pytest fixtures for the Open Green Button integration tests."""

from __future__ import annotations

from collections.abc import Generator

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,  # noqa: ARG001  # fixture from pytest-homeassistant-custom-component
) -> Generator[None]:
    """Enable loading of custom_components/* during every test."""
    yield
