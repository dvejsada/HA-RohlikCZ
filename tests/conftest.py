"""Shared fixtures for the Rohlik.cz integration tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of the custom integration in every test."""
    yield


@pytest.fixture(autouse=True)
def no_real_announcement_requests():
    """Never let the automatic ETA refresh reach the network in tests."""
    with patch(
        "rohlik_api.services.delivery.DeliveryService.get_announcements",
        new=AsyncMock(return_value=None),
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def no_real_tracking_requests():
    """Never let delivery tracking reach the network in tests (no tracking: 404)."""
    with patch(
        "rohlik_api.services.orders.OrderService.get_delivery_tracking",
        new=AsyncMock(return_value=None),
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def no_order_item_requests():
    """Stub the upcoming-order item and product-detail lookups (and the favourites) so no test
    reaches the network.

    Tests that check them patch ``OrderService.get_items`` or
    ``ProductService.get_detail`` themselves.
    """
    from unittest.mock import AsyncMock, patch

    with (
        patch(
            "rohlik_api.services.orders.OrderService.get_items",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "rohlik_api.services.products.ProductService.get_detail",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "rohlik_api.services.products.ProductService.get_favourites",
            new=AsyncMock(return_value=[]),
        ),
    ):
        yield
