"""
RohlikCZ API Client

This module provides an asynchronous API client for interacting with Rohlik.cz, a Czech online grocery delivery service. It allows logging in, retrieving account data, searching products, managing shopping carts, and accessing shopping lists.

Example:
    from rohlik_api import RohlikCZAPI

    async def example():
        client = RohlikCZAPI('username@example.com', 'password')
        data = await client.get_data()
        print(data)
"""

import logging
import requests

from requests import Response
from requests.exceptions import RequestException
from typing import TypedDict, Dict
from .errors import InvalidCredentialsError, RohlikczError, APIRequestFailedError
import asyncio
import functools

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://www.rohlik.cz"


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

    def _run_in_executor(self, func, *args, **kwargs):
        """
        Run blocking requests calls in a thread executor to maintain async compatibility.

        This helper method ensures that blocking HTTP calls don't block the asyncio event loop
        by running them in a separate thread executor.

        Args:
            func (callable): The function to execute (typically a requests method)
            *args: Positional arguments to pass to the function
            **kwargs: Keyword arguments to pass to the function

        Returns:
            asyncio.Future: A future representing the execution of the function
        """
        return asyncio.get_event_loop().run_in_executor(
            None, functools.partial(func, *args, **kwargs)
        )

    async def login(self, session):
        """
        Authenticate with the Rohlik.cz service.

        Args:
            session (requests.Session): An active requests session to use for authentication

        Returns:
            dict: The JSON response containing authentication data and user information

        Raises:
            requests.exceptions.RequestException: If the login request fails
        """

        login_data = {"email": self._user, "password": self._pass, "name": ""}
        login_url = f"{BASE_URL}/services/frontend-service/login"

        try:
            login_response: Response = await self._run_in_executor(
                session.post,
                login_url,
                json=login_data
            )

            login_response: dict = login_response.json()

            if login_response["status"] != 200:
                if login_response["status"] == 401:
                    raise InvalidCredentialsError(login_response["messages"][0]["content"])
                else:
                    raise RohlikczError(f"Unknown error occurred during login: {login_response["messages"][0]["content"]}")

            if not self._user_id:
                self._user_id = login_response.get("data", {}).get("user", {}).get("id", None)

            if not self._address_id:
                try:
                    self._address_id = login_response.get("data", {}).get("address", {}).get("id", None)
                except AttributeError:
                    _LOGGER.error(f"Address cannot be retrieved from login data. No delivery time sensors will be added. Login response: {mask_data(login_response)}")

            return login_response

        except RequestException as err:
            raise APIRequestFailedError(f"Cannot connect to website! Check your internet connection and try again: {err}")

    async def logout(self, session) -> None:
        """
        Log out from the Rohlik.cz service.
        :param session:
        :return:
        """
        logout_url = f"{BASE_URL}/services/frontend-service/logout"

        try:
            logout_response: Response = await self._run_in_executor(
                session.post,
                logout_url
            )

            logout_response: dict = logout_response.json()

            if logout_response["status"] != 200:
                raise RohlikczError(f"Unknown error occurred during logout: {logout_response}")


        except RequestException as err:
            raise APIRequestFailedError(f"Cannot connect to website! Check your internet connection and try again: {err}")

    async def get_data(self):
        """
        Retrieve all account data from Rohlik.cz in a single operation.

        Returns:
            dict: A dictionary containing all data from various Rohlik endpoints,
                 including login information, delivery details, cart contents,
        """
        session = requests.Session()
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

        result["login"] = await self.login(session)

        try:
            # Step 2: Get data from all other endpoints
            for endpoint, path in self.endpoints.items():

                if endpoint == "next_delivery_slot":
                    if self._address_id:
                        path = self.endpoints["next_delivery_slot"] + f"0?userId={self._user_id}&addressId={self._address_id}&reasonableDeliveryTime=true"
                    else:
                        result[endpoint] = None
                        continue

                try:
                    url = f"{BASE_URL}{path}"
                    response = await self._run_in_executor(session.get, url)
                    response.raise_for_status()
                    result[endpoint] = response.json()
                except RequestException as err:
                    _LOGGER.error(f"Error fetching {endpoint}: {err}")
                    result[endpoint] = None

            try:
                result["cart"] = await self.get_cart_content(logged_in=True, session=session)

            except RequestException as err:
                _LOGGER.error(f"Error fetching cart: {err}")
                result["cart"] = None

            return result

        except RequestException as err:
            raise APIRequestFailedError(f"Cannot connect to website! Check your internet connection and try again: {err}")
        finally:
            # Step 3: Close the session
            await self.logout(session)
            await self._run_in_executor(session.close)

    async def add_to_cart(self, product_list: list[dict]) -> dict:
        """
        Add multiple products to the shopping cart.

        Args:
            product_list (list[dict]): A list of objects containing product_id and quantity for each product to be added to the cart
        Returns:
            list: A list of product IDs that were successfully added to the cart
        """

        session = requests.Session()
        await self.login(session)

        try:
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
                    search_response = await self._run_in_executor(
                        session.post,
                        f"{BASE_URL}{search_url}",
                        json=search_payload
                    )
                    search_response.raise_for_status()
                    added_products.append(product["product_id"])
                except RequestException as err:
                    _LOGGER.error(f"Error adding {product["product_id"]} due to {err}")

            return {"added_products":added_products}

        except RequestException as err:
            _LOGGER.error(f"Request failed: {err}")
            raise ValueError("Request failed")
        finally:
            await self.logout(session)
            await self._run_in_executor(session.close)

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

        session = requests.Session()
        await self.login(session)

        try:
            # Set request data
            search_url = "/services/frontend-service/search-metadata"
            search_payload = {
                "search": product_name,
                "offset": 0,
                "limit": limit + 5,
                "companyId" : 1,
                "filterData": {"filters": []},
                "canCorrect": True
            }

            # Login to account to return user-specific data
            await self.login(session)

            # Perform API request
            search_response = await self._run_in_executor(
                session.get,
                f"{BASE_URL}{search_url}",
                params=search_payload
            )
            search_response.raise_for_status()
            search_data:dict = search_response.json()
            found_products:list = search_data["data"]["productList"]

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
                        "price": f"{found_products[i]["price"]["full"]} {found_products[i]["price"]["currency"]}",
                        "brand": found_products[i]["brand"],
                        "amount": found_products[i]["textualAmount"]
                            })
                return search_results
            else:
                return None

        except RequestException as err:
            _LOGGER.error(f"Request failed: {err}")
            return None
        finally:
            await self.logout(session)
            await self._run_in_executor(session.close)

    async def get_shopping_list(self, shopping_list_id=None) -> dict:
        """
        Retrieve a shopping list by its ID.

        :param:
            shopping_list_id (str, optional): The ID of the shopping list to retrieve. Must be provided.

        :return:
            dict: The shopping list details
        """

        session = requests.Session()

        try:
            if not shopping_list_id:
                raise ValueError("Missing argument - shopping list id")

            shopping_list_url = f"/api/v1/shopping-lists/id/{shopping_list_id}"

            await self.login(session)
            search_response = await self._run_in_executor(
                session.get,
                f"{BASE_URL}{shopping_list_url}",
            )
            search_response.raise_for_status()
            search_data = search_response.json()
            return {"name": search_data["name"], "products_in_list": search_data["products"]}

        except RequestException as err:
            _LOGGER.error(f"Request failed: {err}")
            raise ValueError("Request failed")
        finally:
            await self.logout(session)
            await self._run_in_executor(session.close)

    async def get_cart_content(self, logged_in: bool = False, session = None) -> Dict:
        """
        Fetches the current cart contents

        :return: Dictionary with cart content
        """

        cart_url = "/services/frontend-service/v2/cart"

        if not logged_in:
            session = requests.Session()
            await self.login(session)
        try:
            cart_response = await self._run_in_executor(
                session.get,
                f"{BASE_URL}{cart_url}",
            )
            cart_response.raise_for_status()
            cart_content = cart_response.json()

        except RequestException as err:
            _LOGGER.error(f"Request failed: {err}")
            raise ValueError("Request failed")
        finally:
            if not logged_in:
                await self.logout(session)
                await self._run_in_executor(session.close)

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
        session = requests.Session()

        try:
            await self.login(session)

            delete_url = f"/services/frontend-service/v2/cart?orderFieldId={order_field_id}"

            delete_response = await self._run_in_executor(
                session.delete,
                f"{BASE_URL}{delete_url}"
            )
            delete_response.raise_for_status()

            try:
                return delete_response.json()
            except:
                # Handle case where response might not be JSON
                return {"success": True, "status_code": delete_response.status_code}

        except RequestException as err:
            _LOGGER.error(f"Error deleting item with orderFieldId {order_field_id}: {err}")
            raise APIRequestFailedError(f"Failed to delete item from cart: {err}")
        finally:
            await self.logout(session)
            await self._run_in_executor(session.close)