"""Tests for product images, upcoming-order items and the cart name.

All payloads are made up; they use the field names the integration already
reads. No real account data.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from rohlik_api import Cart, CartItem, OrderItem, ProductSearchResult, SearchResults

from custom_components.rohlikcz.const import DOMAIN

from fixtures_data import sample_api_data

ENTRY_DATA = {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"}
IMG_A = "https://cdn.example.test/images/grocery/products/2001/2001-1.jpg"
IMG_B = "https://cdn.example.test/images/grocery/products/2002/2002-1.jpg"


def _entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, unique_id="123456", data=ENTRY_DATA, options={})


def _upcoming_order(items_count: int = 2, total: float = 5.5) -> dict:
    since = (dt_util.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000%z")
    till = (dt_util.now() + timedelta(days=1, hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000%z")
    return {
        "id": 7001,
        "itemsCount": items_count,
        "priceComposition": {"total": {"amount": total}},
        "deliverySlot": {"since": since, "till": till},
        "itemImages": [
            {"productId": 2001, "image": "https://cdn.example.test/other-a.jpg"},
            {"productId": 2002, "image": IMG_B},
        ],
    }


ORDER_ITEMS = [
    # Has its own picture in the order detail.
    OrderItem(id=2001, name="Testbrot", quantity=1, price=2.5, unit_price=2.5,
              textual_amount="500 g", currency="EUR", image=IMG_A),
    # No picture in the detail: filled from itemImages.
    OrderItem(id=2002, name="Testmilch", quantity=2, price=3.0, unit_price=1.5,
              textual_amount="1 l", currency="EUR", image=None),
]


async def _setup(hass: HomeAssistant, data: dict, get_items: AsyncMock) -> MockConfigEntry:
    entry = _entry()
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.rohlikcz.hub.RohlikAPI.get_data",
            new=AsyncMock(return_value=data),
        ),
        patch("rohlik_api.services.orders.OrderService.get_items", new=get_items),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _ordered_state(hass: HomeAssistant):
    entity_id = er.async_get(hass).async_get_entity_id("binary_sensor", DOMAIN, "123456_is_ordered")
    return hass.states.get(entity_id)


async def test_upcoming_order_items_attribute(hass: HomeAssistant) -> None:
    data = sample_api_data()
    data["next_order"] = [_upcoming_order()]
    get_items = AsyncMock(return_value=ORDER_ITEMS)

    await _setup(hass, data, get_items)

    get_items.assert_awaited_once_with(7001)
    state = _ordered_state(hass)
    assert state.state == "on"
    assert state.attributes["items"] == [
        {"id": 2001, "name": "Testbrot", "quantity": 1, "price": 2.5,
         "textual_amount": "500 g", "image": IMG_A},
        {"id": 2002, "name": "Testmilch", "quantity": 2, "price": 3.0,
         "textual_amount": "1 l", "image": IMG_B},
    ]
    assert "order_data" in state.attributes


async def test_items_are_fetched_only_when_the_order_changes(hass: HomeAssistant) -> None:
    data = sample_api_data()
    data["next_order"] = [_upcoming_order()]
    get_items = AsyncMock(return_value=ORDER_ITEMS)
    entry = await _setup(hass, data, get_items)
    account = entry.runtime_data

    with patch("rohlik_api.services.orders.OrderService.get_items", new=get_items):
        # Same order, same count and total: no extra request.
        await account._update_next_order_items(data)
        assert get_items.await_count == 1

        # The order was edited (one more item): fetch again.
        changed = sample_api_data()
        changed["next_order"] = [_upcoming_order(items_count=3, total=7.0)]
        await account._update_next_order_items(changed)
        assert get_items.await_count == 2

        # No upcoming order any more: list is cleared without a request.
        await account._update_next_order_items(sample_api_data())
        assert get_items.await_count == 2
        assert account.next_order_items == []


async def test_failed_item_fetch_keeps_sensor_working(hass: HomeAssistant) -> None:
    data = sample_api_data()
    data["next_order"] = [_upcoming_order()]
    get_items = AsyncMock(return_value=None)  # request failed

    entry = await _setup(hass, data, get_items)

    state = _ordered_state(hass)
    assert state.state == "on"
    assert state.attributes["items"] == []
    # Not marked as fetched, so the next refresh tries again.
    assert entry.runtime_data._next_order_items_key is None


async def test_no_upcoming_order_has_no_attributes(hass: HomeAssistant) -> None:
    get_items = AsyncMock(return_value=ORDER_ITEMS)
    await _setup(hass, sample_api_data(), get_items)

    get_items.assert_not_awaited()
    state = _ordered_state(hass)
    assert state.state == "off"
    assert "items" not in state.attributes


async def test_items_attribute_is_not_recorded(hass: HomeAssistant) -> None:
    from custom_components.rohlikcz.binary_sensor import IsOrderedSensor

    assert "items" in IsOrderedSensor._unrecorded_attributes


def _cart_with_image() -> Cart:
    return Cart(
        total_price=2.5,
        total_items=1,
        can_make_order=False,
        products=[
            CartItem(id="2001", cart_item_id="55", name="Testbrot", quantity=1, price=2.5,
                     category_name="Brot", brand="Test", currency="EUR", image=IMG_A),
        ],
    )


async def test_todo_description_has_image_and_cart_is_translated(hass: HomeAssistant) -> None:
    hass.config.language = "de"
    data = sample_api_data()
    data["cart"] = _cart_with_image()
    await _setup(hass, data, AsyncMock(return_value=[]))

    entity_id = er.async_get(hass).async_get_entity_id("todo", DOMAIN, "123456-cart")
    state = hass.states.get(entity_id)
    # The translation is used now, not the hard-coded "Rohlik Shopping Cart".
    assert state.attributes["friendly_name"].endswith("Warenkorb")

    result = await hass.services.async_call(
        "todo", "get_items", {"entity_id": entity_id}, blocking=True, return_response=True
    )
    item = result[entity_id]["items"][0]
    assert item["summary"].startswith("Testbrot (1)")
    assert item["description"].endswith(f"Image: {IMG_A}")


async def test_todo_description_without_image(hass: HomeAssistant) -> None:
    data = sample_api_data()
    cart = _cart_with_image()
    cart.products[0].image = None
    data["cart"] = cart
    await _setup(hass, data, AsyncMock(return_value=[]))

    entity_id = er.async_get(hass).async_get_entity_id("todo", DOMAIN, "123456-cart")
    result = await hass.services.async_call(
        "todo", "get_items", {"entity_id": entity_id}, blocking=True, return_response=True
    )
    assert "Image:" not in result[entity_id]["items"][0]["description"]


async def test_search_product_service_returns_image(hass: HomeAssistant) -> None:
    entry = await _setup(hass, sample_api_data(), AsyncMock(return_value=[]))
    results = SearchResults(results=[
        ProductSearchResult(id=2001, name="Testbrot", price="2.5 €", brand="Test",
                            amount="500 g", image=IMG_A),
    ])
    with patch(
        "rohlik_api.services.products.ProductService.search",
        new=AsyncMock(return_value=results),
    ):
        response = await hass.services.async_call(
            DOMAIN, "search_product",
            {"config_entry_id": entry.entry_id, "product_name": "brot"},
            blocking=True, return_response=True,
        )
    assert response["search_results"][0]["image"] == IMG_A


async def test_get_cart_content_service_returns_image(hass: HomeAssistant) -> None:
    entry = await _setup(hass, sample_api_data(), AsyncMock(return_value=[]))
    with patch(
        "rohlik_api.services.cart.CartService.get_content",
        new=AsyncMock(return_value=_cart_with_image()),
    ):
        response = await hass.services.async_call(
            DOMAIN, "get_cart_content", {"config_entry_id": entry.entry_id},
            blocking=True, return_response=True,
        )
    assert response["products"][0]["image"] == IMG_A


async def test_unexpected_error_does_not_fail_the_refresh(hass: HomeAssistant) -> None:
    data = sample_api_data()
    data["next_order"] = [_upcoming_order()]
    entry = await _setup(hass, data, AsyncMock(side_effect=RuntimeError("boom")))

    assert entry.runtime_data.last_update_success is True
    assert _ordered_state(hass).attributes["items"] == []


async def test_get_product_service(hass: HomeAssistant) -> None:
    """get_product combines the product card with the detail's picture."""
    from rohlik_api import ProductCard

    entry = await _setup(hass, sample_api_data(), AsyncMock(return_value=[]))
    card = ProductCard(id=2001, name="Testbrot", brand=None, amount="500 g", unit="ks",
                       price=2.5, original_price=2.5, unit_price=5.0, currency="CZK")
    detail = {"productId": 2001, "imgPath": "/images/grocery/products/2001/2001-1.jpg"}
    with (
        patch("rohlik_api.services.products.ProductService.get_cards",
              new=AsyncMock(return_value=[card])),
        patch("rohlik_api.services.products.ProductService.get_detail",
              new=AsyncMock(return_value=detail)),
    ):
        response = await hass.services.async_call(
            DOMAIN, "get_product", {"config_entry_id": entry.entry_id, "product_id": 2001},
            blocking=True, return_response=True,
        )
    assert response["found"] is True
    assert response["name"] == "Testbrot"
    assert response["price"] == 2.5
    assert response["in_stock"] is True
    # Relative imgPath is completed with the shop's CDN (Rohlík.cz for this entry).
    assert response["image"] == "https://cdn.rohlik.cz/images/grocery/products/2001/2001-1.jpg"
    assert response["detail"] == detail


async def test_get_product_unknown(hass: HomeAssistant) -> None:
    entry = await _setup(hass, sample_api_data(), AsyncMock(return_value=[]))
    with (
        patch("rohlik_api.services.products.ProductService.get_cards",
              new=AsyncMock(return_value=[])),
        patch("rohlik_api.services.products.ProductService.get_detail",
              new=AsyncMock(return_value=None)),
    ):
        response = await hass.services.async_call(
            DOMAIN, "get_product", {"config_entry_id": entry.entry_id, "product_id": 9},
            blocking=True, return_response=True,
        )
    assert response["found"] is False
    assert response["image"] is None
    assert response["name"] is None


# Shape seen live on Gurkerl.at: the product detail lists full picture URLs.
DETAIL_IMG = "https://cdn.example.test/images/grocery/products/3001/3001-1.jpg"


async def test_get_product_reads_images_list(hass: HomeAssistant) -> None:
    entry = await _setup(hass, sample_api_data(), AsyncMock(return_value=[]))
    detail = {"productId": 3001, "images": [None, DETAIL_IMG, "https://cdn.example.test/b.png"]}
    with (
        patch("rohlik_api.services.products.ProductService.get_cards",
              new=AsyncMock(return_value=[])),
        patch("rohlik_api.services.products.ProductService.get_detail",
              new=AsyncMock(return_value=detail)),
    ):
        response = await hass.services.async_call(
            DOMAIN, "get_product", {"config_entry_id": entry.entry_id, "product_id": 3001},
            blocking=True, return_response=True,
        )
    assert response["image"] == DETAIL_IMG


async def test_get_product_uppercase_extension(hass: HomeAssistant) -> None:
    entry = await _setup(hass, sample_api_data(), AsyncMock(return_value=[]))
    url = "https://cdn.example.test/images/grocery/products/3002/3002-1.JPEG"
    with (
        patch("rohlik_api.services.products.ProductService.get_cards",
              new=AsyncMock(return_value=[])),
        patch("rohlik_api.services.products.ProductService.get_detail",
              new=AsyncMock(return_value={"images": [url]})),
    ):
        response = await hass.services.async_call(
            DOMAIN, "get_product", {"config_entry_id": entry.entry_id, "product_id": 3002},
            blocking=True, return_response=True,
        )
    assert response["image"] == url


def _order_items_without_pictures() -> list[OrderItem]:
    return [
        OrderItem(id=2001, name="Testbrot", quantity=1, price=2.5, unit_price=2.5,
                  textual_amount="500 g", currency="EUR", image=IMG_A),
        OrderItem(id=2002, name="Testmilch", quantity=2, price=3.0, unit_price=1.5,
                  textual_amount="1 l", currency="EUR", image=None),  # from itemImages
        OrderItem(id=3001, name="Testkäse", quantity=1, price=4.0, unit_price=4.0,
                  textual_amount="200 g", currency="EUR", image=None),  # from detail
    ]


async def test_order_items_missing_pictures_come_from_product_detail(hass: HomeAssistant) -> None:
    data = sample_api_data()
    data["next_order"] = [_upcoming_order(items_count=3)]
    get_detail = AsyncMock(return_value={"images": [DETAIL_IMG]})
    with patch("rohlik_api.services.products.ProductService.get_detail", new=get_detail):
        entry = await _setup(hass, data, AsyncMock(return_value=_order_items_without_pictures()))

    images = {row["id"]: row["image"] for row in _ordered_state(hass).attributes["items"]}
    assert images == {2001: IMG_A, 2002: IMG_B, 3001: DETAIL_IMG}
    # Only the product without any picture was looked up.
    get_detail.assert_awaited_once_with(3001)

    # The order changes: the picture of 3001 comes from the cache, no new request.
    account = entry.runtime_data
    changed = sample_api_data()
    changed["next_order"] = [_upcoming_order(items_count=3, total=9.9)]
    with (
        patch("rohlik_api.services.orders.OrderService.get_items",
              new=AsyncMock(return_value=_order_items_without_pictures())),
        patch("rohlik_api.services.products.ProductService.get_detail", new=get_detail),
    ):
        await account._update_next_order_items(changed)
    assert get_detail.await_count == 1
    assert account.next_order_items[2]["image"] == DETAIL_IMG


async def test_failed_picture_lookup_leaves_image_empty(hass: HomeAssistant) -> None:
    data = sample_api_data()
    data["next_order"] = [_upcoming_order(items_count=3)]
    get_detail = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("rohlik_api.services.products.ProductService.get_detail", new=get_detail):
        entry = await _setup(hass, data, AsyncMock(return_value=_order_items_without_pictures()))

    assert entry.runtime_data.last_update_success is True
    rows = _ordered_state(hass).attributes["items"]
    assert [r["image"] for r in rows] == [IMG_A, IMG_B, None]
    # A failure is not cached, so the next order change tries again.
    assert "3001" not in entry.runtime_data._product_images
