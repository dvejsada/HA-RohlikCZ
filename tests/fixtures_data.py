"""Sample API payloads for tests."""
from __future__ import annotations

import copy


def sample_api_data() -> dict:
    """A representative payload as returned by RohlikCZAPI.get_data()."""
    return copy.deepcopy(
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
            "bags": {"data": {"reusableBagsCount": 0}},
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
            "cart": {
                "total_price": 0,
                "total_items": 0,
                "can_make_order": False,
                "products": [],
            },
        }
    )
