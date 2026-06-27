"""Sample API payloads for tests."""
from __future__ import annotations

import copy

from rohlik_api import Cart


def sample_api_data() -> dict:
    """A representative payload as returned by RohlikAPI.get_data().

    Mirrors the real client: every endpoint is a raw JSON dict/list except
    ``cart``, which is a typed :class:`~rohlik_api.Cart` model.
    """
    data = copy.deepcopy(
        {
            "login": {
                "status": 200,
                "data": {
                    "user": {
                        "id": 123456,
                        "name": "Test User",
                        "email": "test@example.com",
                        "phone": "+420123456789",
                        "reusablePackaging": True,
                        "parentsClub": False,
                        "premium": {"active": False},
                    }
                },
            },
            "delivery": {"data": {}},
            "next_order": [],
            "announcements": {"data": {"announcements": []}},
            "bags": {"current": 0, "max": 0},
            "timeslot": None,
            "last_order": [
                {
                    "id": 9001,
                    "orderTime": "2026-05-01T10:00:00.000+02:00",
                    "itemsCount": 5,
                    "priceComposition": {"total": {"amount": 750.0}},
                }
            ],
            "premium_profile": {"data": {}},
            "next_delivery_slot": None,
            "delivery_announcements": {"data": {"announcements": []}},
            "delivered_orders": [
                {
                    "id": 9001,
                    "orderTime": "2026-05-01T10:00:00.000+02:00",
                    "priceComposition": {"total": {"amount": 750.0}},
                }
            ],
        }
    )
    data["cart"] = Cart(total_price=0, total_items=0, can_make_order=False, products=[])
    return data
