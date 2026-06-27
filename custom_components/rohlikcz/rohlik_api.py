"""
RohlikCZ API Client

This module provides an asynchronous API client for interacting with Rohlik.cz, a Czech online grocery delivery service. It allows logging in, retrieving account data, searching products, managing shopping carts, and accessing shopping lists.

Networking uses aiohttp directly (no blocking calls in the event loop). Each
public operation uses its own ClientSession with an isolated cookie jar, so the
login/logout cycle of one operation never interferes with another running
concurrently (e.g. a background enrichment overlapping a regular refresh).

Example:
    from rohlik_api import RohlikCZAPI

    async def example():
        client = RohlikCZAPI('username@example.com', 'password')
        data = await client.get_data()
        print(data)
"""

import asyncio
import json
import logging

import aiohttp

from typing import TypedDict, Dict
from .const import HTTP_TIMEOUT
from .errors import InvalidCredentialsError, RohlikczError, APIRequestFailedError

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://www.rohlik.cz"

# Errors raised by aiohttp for connection/timeout problems.
_NETWORK_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError)


def mask_data(input_dict):
    """ Takes a dictionary and replaces all non-null values with "XXXXXXX". Null values (None) remain unchanged."""
    if not isinstance(input_dict, dict):
        return input_dict

    result = {}
    for key, value in input_dict.items():
        if value is None:
            result[key] = None
        elif isinstance(value, dict):
            # Recursively mask nested dictionaries
            result[key] = mask_data(value)
        elif isinstance(value, list):
            # Handle lists by masking each element if needed
            result[key] = [mask_data(item) if isinstance(item, dict)
                           else "XXXXXXX" if item is not None else None
                           for item in value]
        else:
            result[key] = "XXXXXXX"

    return result


class Product(TypedDict):

    product_id: int
    quantity: int


class RohlikCZAPI:
    """
    API client for interacting with Rohlik.cz services.

    This class provides methods to authenticate with Rohlik.cz and perform
    various operations such as retrieving account data, searching for products,
    adding products to cart, and accessing shopping lists.

    Attributes:
        endpoints (dict): Dictionary of available API endpoints

    """
    def __init__(self, username, password):
        """
        Initialize the Rohlik API client.

        Args:
            username (str): Email address used for Rohlik.cz login
            password (str): Password for Rohlik.cz account
        """
        self._user = username
        self._pass = password
        self._user_id = None
        self._address_id = None
        self.endpoints = {}
        # A dedicated, reusable logged-in session for cheap slot polling. It has
        # its own cookie jar (separate from the per-operation sessions), so it
        # never clashes with get_data/service calls. Access is serialized.
        self._slot_session: aiohttp.ClientSession | None = None
        self._slot_lock = asyncio.Lock()

    def _new_session(self) -> aiohttp.ClientSession:
        """Create a fresh client session with an isolated cookie jar."""
        return aiohttp.ClientSession(timeout=HTTP_TIMEOUT)

    def _timeslots_url(self) -> str | None:
        """Build the preselected-timeslots URL, or None if no address is known."""
        if not self._address_id:
            return None
        return (f"{BASE_URL}/services/frontend-service/timeslots-api/0"
                f"?userId={self._user_id}&addressId={self._address_id}&reasonableDeliveryTime=true")

    async def _ensure_slot_session(self) -> aiohttp.ClientSession:
        """Return the reusable slot session, logging in if needed."""
        if self._slot_session is None or self._slot_session.closed:
            session = self._new_session()
            await self.login(session)
            self._slot_session = session
        return self._slot_session

    async def _reset_slot_session(self) -> None:
        """Close the reusable slot session so the next call logs in fresh."""
        if self._slot_session is not None:
            await self._slot_session.close()
            self._slot_session = None

    async def get_timeslots(self) -> dict | None:
        """Fetch only the preselected delivery slots, cheaply.

        Reuses a logged-in session so a poll is a single GET rather than a full
        login/logout cycle. Re-authenticates on 401 and retries once on a
        dropped keep-alive connection. Returns None if the account has no
        delivery address (no slot URL to query).
        """
        async with self._slot_lock:
            try:
                return await self._fetch_timeslots()
            except aiohttp.ClientConnectionError:
                # Stale keep-alive connection dropped by the server between
                # polls - rebuild the session and try once more.
                await self._reset_slot_session()
                try:
                    return await self._fetch_timeslots()
                except _NETWORK_ERRORS as err:
                    raise APIRequestFailedError(f"Cannot fetch timeslots: {err}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise APIRequestFailedError(f"Cannot fetch timeslots: {err}")

    async def _fetch_timeslots(self) -> dict | None:
        session = await self._ensure_slot_session()
        url = self._timeslots_url()
        if url is None:
            return None
        async with session.get(url) as response:
            if response.status != 401:
                response.raise_for_status()
                return await response.json(content_type=None)
        # Session expired (401): the response is released as the context exits
        # above, so it's now safe to close the session, log in again and retry.
        await self._reset_slot_session()
        session = await self._ensure_slot_session()
        url = self._timeslots_url()
        if url is None:
            return None
        async with session.get(url) as retry:
            retry.raise_for_status()
            return await retry.json(content_type=None)

    async def async_close(self) -> None:
        """Close the reusable slot session (call on unload)."""
        await self._reset_slot_session()

    async def login(self, session: aiohttp.ClientSession):
        """
        Authenticate with the Rohlik.cz service.

        Args:
            session (aiohttp.ClientSession): An active session to use for authentication

        Returns:
            dict: The JSON response containing authentication data and user information

        Raises:
            APIRequestFailedError: If the login request fails at the network level
            InvalidCredentialsError / RohlikczError: If the API rejects the login
        """

        login_data = {"email": self._user, "password": self._pass, "name": ""}
        login_url = f"{BASE_URL}/services/frontend-service/login"

        try:
            async with session.post(login_url, json=login_data) as response:
                login_response: dict = await response.json(content_type=None)
        except _NETWORK_ERRORS as err:
            raise APIRequestFailedError(f"Cannot connect to website! Check your internet connection and try again: {err}")

        if login_response["status"] != 200:
            # The Rohlik API sometimes returns an empty "messages" array on
            # failure, so extract the error message defensively to avoid an
            # IndexError masking the real status code.
            messages = login_response.get("messages") or []
            fallback_detail = f"status code {login_response['status']}, no message provided"
            if messages and isinstance(messages[0], dict):
                # Fall back when content is missing or an empty string.
                error_detail = messages[0].get("content") or fallback_detail
            else:
                error_detail = fallback_detail

            if login_response["status"] == 401:
                raise InvalidCredentialsError(error_detail)
            else:
                _LOGGER.error(f"Login failed. Status: {login_response['status']}, Full response: {mask_data(login_response)}")
                raise RohlikczError(f"Unknown error occurred during login: {error_detail}")

        if not self._user_id:
            self._user_id = login_response.get("data", {}).get("user", {}).get("id", None)

        if not self._address_id:
            try:
                self._address_id = login_response.get("data", {}).get("address", {}).get("id", None)
            except AttributeError:
                _LOGGER.error(f"Address cannot be retrieved from login data. No delivery time sensors will be added. Login response: {mask_data(login_response)}")

        return login_response

    async def logout(self, session: aiohttp.ClientSession) -> None:
        """
        Log out from the Rohlik.cz service.
        :param session:
        :return:
        """
        logout_url = f"{BASE_URL}/services/frontend-service/logout"

        try:
            async with session.post(logout_url) as response:
                logout_response: dict = await response.json(content_type=None)
        except _NETWORK_ERRORS as err:
            raise APIRequestFailedError(f"Cannot connect to website! Check your internet connection and try again: {err}")

        if logout_response["status"] != 200:
            raise RohlikczError(f"Unknown error occurred during logout: {logout_response}")

    async def get_data(self):
        """
        Retrieve all account data from Rohlik.cz in a single operation.

        Returns:
            dict: A dictionary containing all data from various Rohlik endpoints,
                 including login information, delivery details, cart contents,
        """
        session = self._new_session()
        result: dict = {}
        self.endpoints = {
            "delivery": "/services/frontend-service/first-delivery?reasonableDeliveryTime=true",
            "next_order": "/api/v3/orders/upcoming",
            "announcements": "/services/frontend-service/announcements/top",
            "bags": "/api/v1/reusable-bags/user-info",
            "timeslot": "/services/frontend-service/v1/timeslot-reservation",
            "last_order": "/api/v3/orders/delivered?offset=0&limit=1",
            "premium_profile": "/services/frontend-service/premium/profile",
            "next_delivery_slot": "/services/frontend-service/timeslots-api/",
            "delivery_announcements": "/services/frontend-service/announcements/delivery",
            "delivered_orders": "/api/v3/orders/delivered?offset=0&limit=50"
        }

        try:
            # Login first; if this fails the error propagates (logout in the
            # finally is best-effort and never masks it).
            result["login"] = await self.login(session)

            # Step 2: Get data from all other endpoints
            for endpoint, path in self.endpoints.items():

                if endpoint == "next_delivery_slot":
                    # Built (and DRYed) via the shared helper; None without an address.
                    url = self._timeslots_url()
                    if url is None:
                        result[endpoint] = None
                        continue
                else:
                    url = f"{BASE_URL}{path}"

                try:
                    async with session.get(url) as response:
                        response.raise_for_status()
                        result[endpoint] = await response.json(content_type=None)
                except Exception as err:
                    _LOGGER.error(f"Error fetching {endpoint}: {err}")
                    result[endpoint] = None

            try:
                result["cart"] = await self.get_cart_content(logged_in=True, session=session)
            except Exception as err:
                _LOGGER.error(f"Error fetching cart: {err}")
                result["cart"] = None

            return result

        finally:
            # Step 3: Log out (best-effort) and always close the session.
            try:
                await self.logout(session)
            except Exception:
                pass
            await session.close()

    async def get_delivered_orders_page(self, session: aiohttp.ClientSession, offset: int = 0, limit: int = 50) -> list:
        """Fetch a page of delivered orders using an existing authenticated session."""
        url = f"{BASE_URL}/api/v3/orders/delivered?offset={offset}&limit={limit}"
        try:
            async with session.get(url) as response:
                response.raise_for_status()
                return await response.json(content_type=None)
        except _NETWORK_ERRORS as err:
            _LOGGER.error(f"Error fetching delivered orders page (offset={offset}): {err}")
            return []

    async def get_order_detail(self, session: aiohttp.ClientSession, order_id: str) -> dict | None:
        """Fetch detailed order info including items for a single order."""
        url = f"{BASE_URL}/api/v3/orders/{order_id}"
        try:
            async with session.get(url) as response:
                response.raise_for_status()
                return await response.json(content_type=None)
        except _NETWORK_ERRORS as err:
            _LOGGER.error(f"Error fetching order detail for {order_id}: {err}")
            return None

    async def get_product_categories(self, session: aiohttp.ClientSession, product_id: int) -> list | None:
        """Fetch category hierarchy for a product. Returns None for 404 (discontinued)."""
        url = f"{BASE_URL}/api/v1/products/{product_id}/categories"
        try:
            async with session.get(url) as response:
                if response.status == 404:
                    return None  # Product no longer exists
                response.raise_for_status()
                data = await response.json(content_type=None)
            return data.get("categories", [])
        except _NETWORK_ERRORS as err:
            _LOGGER.debug(f"Could not fetch categories for product {product_id}: {err}")
            return []

    async def get_product_detail(self, session: aiohttp.ClientSession, product_id: int) -> dict | None:
        """Fetch product detail including brand."""
        url = f"{BASE_URL}/api/v1/products/{product_id}"
        try:
            async with session.get(url) as response:
                response.raise_for_status()
                return await response.json(content_type=None)
        except _NETWORK_ERRORS as err:
            _LOGGER.debug(f"Could not fetch product detail for {product_id}: {err}")
            return None

    async def enrich_orders_with_items(self, order_ids: list[str]) -> dict[str, list]:
        """Fetch item details for a list of order IDs. Returns {order_id: items_list}."""
        session = self._new_session()
        results = {}
        try:
            await self.login(session)
        except Exception as err:
            _LOGGER.error(f"Login failed for order enrichment: {err}")
            await session.close()
            return results

        try:
            for i, order_id in enumerate(order_ids):
                try:
                    detail = await self.get_order_detail(session, order_id)
                    if detail and detail.get("items"):
                        items = []
                        for item in detail["items"]:
                            items.append({
                                "id": item.get("id"),
                                "name": item.get("name", "Unknown"),
                                "quantity": item.get("amount", 1),
                                "price": item.get("priceComposition", {}).get("total", {}).get("amount", 0),
                                "unit_price": item.get("priceComposition", {}).get("unit", {}).get("amount", 0),
                                "textual_amount": item.get("textualAmount", ""),
                            })
                        results[order_id] = items
                except Exception as err:
                    _LOGGER.warning(f"Failed to fetch items for order {order_id}: {err}")
                if i % 50 == 0 and i > 0:
                    _LOGGER.info(f"Fetched items for {i}/{len(order_ids)} orders")
                await asyncio.sleep(0.2)
            _LOGGER.info(f"Item fetch complete: {len(results)}/{len(order_ids)} orders")
            return results
        except Exception as err:
            _LOGGER.error(f"Error during order item fetch: {err}")
            return results
        finally:
            try:
                await self.logout(session)
            except Exception:
                pass
            await session.close()

    async def fetch_product_categories_batch(self, product_ids: list[int], progress_callback=None) -> dict[int, list]:
        """Fetch categories for a batch of product IDs. Returns {product_id: categories_list}."""
        session = self._new_session()
        results = {}
        try:
            await self.login(session)
        except Exception as err:
            _LOGGER.error(f"Login failed for category fetch: {err}")
            await session.close()
            return results

        try:
            for i, pid in enumerate(product_ids):
                try:
                    cats = await self.get_product_categories(session, pid)
                    if cats is None:
                        # Product discontinued (404) — mark with sentinel category
                        results[pid] = [{"level": 1, "name": "Discontinued"}]
                    elif cats:
                        results[pid] = cats
                except Exception as err:
                    _LOGGER.debug(f"Failed to fetch categories for product {pid}: {err}")
                if progress_callback and i % 50 == 0:
                    await progress_callback(i, len(product_ids))
                await asyncio.sleep(0.2)
            _LOGGER.info(f"Category fetch complete: {len(results)}/{len(product_ids)} products")
            return results
        except Exception as err:
            _LOGGER.error(f"Error during category fetch: {err}")
            return results
        finally:
            try:
                await self.logout(session)
            except Exception:
                pass
            await session.close()

    async def fetch_all_delivered_orders(self) -> list:
        """Fetch ALL delivered orders by paginating through the API. Returns list of all orders."""
        session = self._new_session()
        all_orders = []
        offset = 0
        limit = 50

        try:
            await self.login(session)

            while True:
                page = await self.get_delivered_orders_page(session, offset, limit)
                if not page:
                    break
                all_orders.extend(page)
                _LOGGER.info(f"Fetched {len(all_orders)} orders so far (offset={offset})")
                if len(page) < limit:
                    break
                offset += limit
                # Rate limit: 200ms between pages
                await asyncio.sleep(0.2)

            return all_orders

        finally:
            # Per-page network errors are handled in get_delivered_orders_page
            # (which returns [] and ends pagination), so no network error can
            # reach here; a login failure propagates to the caller.
            try:
                await self.logout(session)
            except Exception:
                pass
            await session.close()

    async def add_to_cart(self, product_list: list[dict]) -> dict:
        """
        Add multiple products to the shopping cart.

        Args:
            product_list (list[dict]): A list of objects containing product_id and quantity for each product to be added to the cart
        Returns:
            list: A list of product IDs that were successfully added to the cart
        """

        session = self._new_session()
        try:
            # A login network failure surfaces as APIRequestFailedError; per-product
            # failures below are caught individually.
            await self.login(session)

            search_url = "/services/frontend-service/v2/cart"
            added_products = []

            for product in product_list:
                search_payload = {
                    "actionId": None,
                    "productId": int(product["product_id"]),
                    "quantity": int(product["quantity"]),
                    "recipeId": None,
                    "source": "true:Shopping Lists"
                }
                try:
                    async with session.post(f"{BASE_URL}{search_url}", json=search_payload) as response:
                        response.raise_for_status()
                    added_products.append(product["product_id"])
                except _NETWORK_ERRORS as err:
                    _LOGGER.error(f"Error adding {product['product_id']} due to {err}")

            return {"added_products": added_products}

        finally:
            try:
                await self.logout(session)
            except Exception:
                pass
            await session.close()

    async def search_product(self, product_name: str, limit: int = 10, favourite: bool = False):
        """
        Search for products by name and return the first matching product.

        Args:
            product_name (str): The name or search term for the product
            limit (int): Number of products returned
            favourite (bool): Whether only favourite items shall be returned

        Returns:
            dict: The first matching product's details, or None if no products found
        """

        session = self._new_session()
        try:
            await self.login(session)

            # Set request data
            search_url = "/services/frontend-service/search-metadata"
            # aiohttp query params must be flat strings, so complex values are
            # JSON-encoded / stringified.
            search_payload = {
                "search": product_name,
                "offset": "0",
                "limit": str(limit + 5),
                "companyId": "1",
                "filterData": json.dumps({"filters": []}),
                "canCorrect": "true",
            }

            # Perform API request
            async with session.get(f"{BASE_URL}{search_url}", params=search_payload) as response:
                response.raise_for_status()
                search_data: dict = await response.json(content_type=None)

            found_products: list = search_data["data"]["productList"]

            # Remove sponsored content
            found_products = [p for p in found_products if
                              not any(badge.get("slug") == "promoted" for badge in p.get("badge", []))]

            # Keep only favourites if requested
            if favourite:
                found_products = [p for p in found_products if p.get("favourite", False)]

            # Keep only results up to the specified limit
            if len(found_products) > limit:
                found_products = found_products[:limit]

            if len(found_products) > 0:
                search_results = {"search_results": []}
                for i in range(len(found_products)):
                    search_results["search_results"].append({
                        "id": found_products[i]["productId"],
                        "name": found_products[i]["productName"],
                        "price": f"{found_products[i]['price']['full']} {found_products[i]['price']['currency']}",
                        "brand": found_products[i]["brand"],
                        "amount": found_products[i]["textualAmount"]
                            })
                return search_results
            else:
                return None

        except _NETWORK_ERRORS as err:
            _LOGGER.error(f"Request failed: {err}")
            return None
        finally:
            try:
                await self.logout(session)
            except Exception:
                pass
            await session.close()

    async def get_shopping_list(self, shopping_list_id=None) -> dict:
        """
        Retrieve a shopping list by its ID.

        :param:
            shopping_list_id (str, optional): The ID of the shopping list to retrieve. Must be provided.

        :return:
            dict: The shopping list details
        """

        if not shopping_list_id:
            raise ValueError("Missing argument - shopping list id")

        session = self._new_session()
        try:
            shopping_list_url = f"/api/v1/shopping-lists/id/{shopping_list_id}"

            await self.login(session)
            async with session.get(f"{BASE_URL}{shopping_list_url}") as response:
                response.raise_for_status()
                search_data = await response.json(content_type=None)
            return {"name": search_data["name"], "products_in_list": search_data["products"]}

        except _NETWORK_ERRORS as err:
            _LOGGER.error(f"Request failed: {err}")
            raise ValueError("Request failed")
        finally:
            try:
                await self.logout(session)
            except Exception:
                pass
            await session.close()

    async def get_cart_content(self, logged_in: bool = False, session: aiohttp.ClientSession = None) -> Dict:
        """
        Fetches the current cart contents

        :return: Dictionary with cart content
        """

        cart_url = "/services/frontend-service/v2/cart"
        own_session = not logged_in

        if own_session:
            session = self._new_session()
        try:
            # Login inside the try so the session is still closed if it fails.
            if own_session:
                await self.login(session)
            async with session.get(f"{BASE_URL}{cart_url}") as response:
                response.raise_for_status()
                cart_content = await response.json(content_type=None)

        except _NETWORK_ERRORS as err:
            _LOGGER.error(f"Request failed: {err}")
            raise ValueError("Request failed")
        finally:
            if own_session:
                try:
                    await self.logout(session)
                except Exception:
                    pass
                await session.close()

        data = cart_content.get("data", {})

        # Extract the main cart information
        cart_info = {
            "total_price": data.get("totalPrice", 0),
            "total_items": len(data.get("items", {})),
            "can_make_order": data.get("submitConditionPassed", False),
            "products": []
        }

        # Process each product item
        for product_id, product_data in data.get("items", {}).items():

            product_info = {
                "id": product_id,
                "cart_item_id": product_data.get("orderFieldId", ""),
                "name": product_data.get("productName", ""),
                "quantity": product_data.get("quantity", 0),
                "price": product_data.get("price", 0),
                "category_name": product_data.get("primaryCategoryName", ""),
                "brand": product_data.get("brand", "")
            }

            cart_info["products"].append(product_info)

        return cart_info

    async def delete_from_cart(self, order_field_id: str) -> dict:
        """
        Delete an item from the shopping cart using orderFieldId.

        Args:
            order_field_id (str): The orderFieldId of the item to delete

        Returns:
            dict: Response from the deletion operation
        """
        session = self._new_session()

        try:
            await self.login(session)

            delete_url = f"/services/frontend-service/v2/cart?orderFieldId={order_field_id}"

            async with session.delete(f"{BASE_URL}{delete_url}") as response:
                response.raise_for_status()
                try:
                    return await response.json(content_type=None)
                except (aiohttp.ClientError, ValueError):
                    # Handle case where response might not be JSON
                    return {"success": True, "status_code": response.status}

        except _NETWORK_ERRORS as err:
            _LOGGER.error(f"Error deleting item with orderFieldId {order_field_id}: {err}")
            raise APIRequestFailedError(f"Failed to delete item from cart: {err}")
        finally:
            try:
                await self.logout(session)
            except Exception:
                pass
            await session.close()
