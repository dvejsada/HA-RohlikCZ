"""Shared fixtures for the Rohlik.cz integration tests."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of the custom integration in every test."""
    yield


@pytest.fixture(autouse=True)
def no_real_announcement_requests():
    """Never let the automatic ETA refresh reach the network in tests."""
    from unittest.mock import AsyncMock, patch

    with patch(
        "rohlik_api.services.delivery.DeliveryService.get_announcements",
        new=AsyncMock(return_value=None),
    ) as mock:
        yield mock
