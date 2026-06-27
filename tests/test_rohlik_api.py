"""Tests for the aiohttp-based RohlikCZAPI client (HTTP layer mocked)."""
from __future__ import annotations

import re

import aiohttp
import pytest
from aioresponses import aioresponses

from custom_components.rohlikcz.errors import (
    APIRequestFailedError,
    InvalidCredentialsError,
    RohlikczError,
)
from custom_components.rohlikcz.rohlik_api import BASE_URL, RohlikCZAPI

LOGIN_URL = f"{BASE_URL}/services/frontend-service/login"
LOGOUT_URL = f"{BASE_URL}/services/frontend-service/logout"

LOGIN_OK = {"status": 200, "data": {"user": {"id": 123, "name": "Test User"}}}
LOGOUT_OK = {"status": 200}


def _api() -> RohlikCZAPI:
    return RohlikCZAPI("user@example.com", "secret")


async def test_login_success_sets_user_id() -> None:
    with aioresponses() as m:
        m.post(LOGIN_URL, payload=LOGIN_OK)
        api = _api()
        session = api._new_session()
        try:
            reply = await api.login(session)
        finally:
            await session.close()
    assert reply["data"]["user"]["id"] == 123
    assert api._user_id == 123
    assert api._address_id is None


async def test_login_invalid_credentials() -> None:
    with aioresponses() as m:
        m.post(LOGIN_URL, payload={"status": 401, "messages": [{"content": "Bad creds"}]})
        api = _api()
        session = api._new_session()
        try:
            with pytest.raises(InvalidCredentialsError, match="Bad creds"):
                await api.login(session)
        finally:
            await session.close()


async def test_login_other_error_raises_rohlikcz() -> None:
    with aioresponses() as m:
        m.post(LOGIN_URL, payload={"status": 500, "messages": []})
        api = _api()
        session = api._new_session()
        try:
            with pytest.raises(RohlikczError, match="status code 500"):
                await api.login(session)
        finally:
            await session.close()


async def test_login_network_error() -> None:
    with aioresponses() as m:
        m.post(LOGIN_URL, exception=aiohttp.ClientConnectionError("boom"))
        api = _api()
        session = api._new_session()
        try:
            with pytest.raises(APIRequestFailedError):
                await api.login(session)
        finally:
            await session.close()


async def test_get_data_aggregates_endpoints() -> None:
    cart_payload = {
        "data": {
            "totalPrice": 100,
            "submitConditionPassed": True,
            "items": {
                "111": {
                    "orderFieldId": "f1",
                    "productName": "Milk",
                    "quantity": 2,
                    "price": 40,
                    "primaryCategoryName": "Dairy",
                    "brand": "BrandX",
                }
            },
        }
    }
    with aioresponses() as m:
        m.post(LOGIN_URL, payload=LOGIN_OK)
        m.get(f"{BASE_URL}/services/frontend-service/first-delivery?reasonableDeliveryTime=true", payload={"data": {}})
        m.get(f"{BASE_URL}/api/v3/orders/upcoming", payload=[])
        m.get(f"{BASE_URL}/services/frontend-service/announcements/top", payload={"data": {"announcements": []}})
        m.get(f"{BASE_URL}/api/v1/reusable-bags/user-info", payload={"data": {}})
        m.get(f"{BASE_URL}/services/frontend-service/v1/timeslot-reservation", payload=None)
        m.get(f"{BASE_URL}/api/v3/orders/delivered?offset=0&limit=1", payload=[])
        m.get(f"{BASE_URL}/services/frontend-service/premium/profile", payload={"data": {}})
        m.get(f"{BASE_URL}/services/frontend-service/announcements/delivery", payload={"data": {"announcements": []}})
        m.get(f"{BASE_URL}/api/v3/orders/delivered?offset=0&limit=50", payload=[])
        m.get(f"{BASE_URL}/services/frontend-service/v2/cart", payload=cart_payload)
        m.post(LOGOUT_URL, payload=LOGOUT_OK)

        result = await _api().get_data()

    assert result["login"]["data"]["user"]["id"] == 123
    # No address in login -> delivery slot endpoint skipped.
    assert result["next_delivery_slot"] is None
    assert result["cart"]["total_items"] == 1
    assert result["cart"]["products"][0]["name"] == "Milk"
    assert result["cart"]["can_make_order"] is True


async def test_get_data_endpoint_failure_is_isolated() -> None:
    with aioresponses() as m:
        m.post(LOGIN_URL, payload=LOGIN_OK)
        m.get(f"{BASE_URL}/services/frontend-service/first-delivery?reasonableDeliveryTime=true", status=500)
        m.get(f"{BASE_URL}/api/v3/orders/upcoming", payload=[1, 2])
        m.get(f"{BASE_URL}/services/frontend-service/announcements/top", payload={"data": {"announcements": []}})
        m.get(f"{BASE_URL}/api/v1/reusable-bags/user-info", payload={"data": {}})
        m.get(f"{BASE_URL}/services/frontend-service/v1/timeslot-reservation", payload=None)
        m.get(f"{BASE_URL}/api/v3/orders/delivered?offset=0&limit=1", payload=[])
        m.get(f"{BASE_URL}/services/frontend-service/premium/profile", payload={"data": {}})
        m.get(f"{BASE_URL}/services/frontend-service/announcements/delivery", payload={"data": {"announcements": []}})
        m.get(f"{BASE_URL}/api/v3/orders/delivered?offset=0&limit=50", payload=[])
        m.get(f"{BASE_URL}/services/frontend-service/v2/cart", payload={"data": {}})
        m.post(LOGOUT_URL, payload=LOGOUT_OK)

        result = await _api().get_data()

    # The failing endpoint becomes None; the others still populate.
    assert result["delivery"] is None
    assert result["next_order"] == [1, 2]


async def test_add_to_cart_returns_added_products() -> None:
    with aioresponses() as m:
        m.post(LOGIN_URL, payload=LOGIN_OK)
        m.post(f"{BASE_URL}/services/frontend-service/v2/cart", payload={"ok": True})
        m.post(LOGOUT_URL, payload=LOGOUT_OK)

        result = await _api().add_to_cart([{"product_id": 555, "quantity": 2}])

    assert result == {"added_products": [555]}


async def test_search_product_filters_promoted() -> None:
    search_payload = {
        "data": {
            "productList": [
                {
                    "productId": 1,
                    "productName": "Coffee",
                    "price": {"full": 99, "currency": "CZK"},
                    "brand": "Tchibo",
                    "textualAmount": "250 g",
                    "badge": [],
                },
                {
                    "productId": 2,
                    "productName": "Sponsored Coffee",
                    "price": {"full": 120, "currency": "CZK"},
                    "brand": "Ad",
                    "textualAmount": "250 g",
                    "badge": [{"slug": "promoted"}],
                },
            ]
        }
    }
    with aioresponses() as m:
        m.post(LOGIN_URL, payload=LOGIN_OK)
        # Search builds query params, so match the path regardless of them.
        m.get(
            re.compile(r"^https://www\.rohlik\.cz/services/frontend-service/search-metadata"),
            payload=search_payload,
        )
        m.post(LOGOUT_URL, payload=LOGOUT_OK)

        result = await _api().search_product("coffee", limit=10)

    ids = [r["id"] for r in result["search_results"]]
    assert ids == [1]  # promoted item filtered out
    assert result["search_results"][0]["price"] == "99 CZK"


async def test_fetch_all_delivered_orders_survives_logout_failure() -> None:
    """A failing logout in the finally must not lose the fetched orders."""
    with aioresponses() as m:
        m.post(LOGIN_URL, payload=LOGIN_OK)
        # Single short page -> pagination stops after one request.
        m.get(
            f"{BASE_URL}/api/v3/orders/delivered?offset=0&limit=50",
            payload=[{"id": 1}, {"id": 2}],
        )
        m.post(LOGOUT_URL, exception=aiohttp.ClientConnectionError("logout down"))

        orders = await _api().fetch_all_delivered_orders()

    assert orders == [{"id": 1}, {"id": 2}]


async def test_get_cart_content_standalone_login_failure() -> None:
    """Standalone get_cart_content surfaces a login error (and closes its session)."""
    with aioresponses() as m:
        m.post(LOGIN_URL, payload={"status": 401, "messages": [{"content": "Bad creds"}]})
        with pytest.raises(InvalidCredentialsError):
            await _api().get_cart_content()


async def test_delete_from_cart_returns_json() -> None:
    with aioresponses() as m:
        m.post(LOGIN_URL, payload=LOGIN_OK)
        m.delete(
            f"{BASE_URL}/services/frontend-service/v2/cart?orderFieldId=f1",
            payload={"status": 200},
        )
        m.post(LOGOUT_URL, payload=LOGOUT_OK)

        result = await _api().delete_from_cart("f1")

    assert result == {"status": 200}
