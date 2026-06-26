"""Standalone test for OrderStore logic — run without HA dependencies."""
import json
import os
import tempfile


def test_order_store():
    """Test order processing, deduplication, yearly/alltime aggregation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store_path = os.path.join(tmpdir, "test_orders.json")

        # Simulate order data as returned by Rohlik API
        orders = [
            {"id": 1001, "orderTime": "2026-01-15T10:00:00.000+01:00",
             "priceComposition": {"total": {"amount": 500.0}}},
            {"id": 1002, "orderTime": "2026-02-20T10:00:00.000+01:00",
             "priceComposition": {"total": {"amount": 1200.0}}},
            {"id": 1003, "orderTime": "2025-12-01T10:00:00.000+01:00",
             "priceComposition": {"total": {"amount": 800.0}}},
            # Duplicate — should be skipped
            {"id": 1003, "orderTime": "2025-12-01T10:00:00.000+01:00",
             "priceComposition": {"total": {"amount": 800.0}}},
            # No amount — should be skipped
            {"id": 1004, "orderTime": "2025-06-01T10:00:00.000+01:00",
             "priceComposition": {"total": None}},
            # Missing priceComposition — should be skipped
            {"id": 1005, "orderTime": "2025-05-01T10:00:00.000+01:00"},
            # Empty id — should be skipped
            {"id": "", "orderTime": "2025-04-01T10:00:00.000+01:00",
             "priceComposition": {"total": {"amount": 100.0}}},
        ]

        # Replicate OrderStore.process_orders logic
        data = {"version": 1, "user_id": "test", "tracking_since": None, "orders": {}}
        new_count = 0
        for order in orders:
            order_id = str(order.get("id", ""))
            if not order_id or order_id in data["orders"]:
                continue
            price_comp = order.get("priceComposition", {})
            total = price_comp.get("total", {})
            if not isinstance(total, dict):
                continue
            amount = total.get("amount")
            if amount is None:
                continue
            try:
                amount = float(amount)
            except (ValueError, TypeError):
                continue
            order_time = order.get("orderTime", "")
            date_str = order_time[:10] if order_time else ""
            data["orders"][order_id] = {"date": date_str, "amount": amount}
            new_count += 1

        # Test: correct number of new orders (3 valid, skip duplicate + null + missing + empty)
        assert new_count == 3, f"Expected 3 new orders, got {new_count}"
        assert len(data["orders"]) == 3, f"Expected 3 in store, got {len(data['orders'])}"

        # Test: yearly totals
        yearly_2026 = sum(o["amount"] for o in data["orders"].values() if o["date"].startswith("2026"))
        assert yearly_2026 == 1700.0, f"Expected 2026 total 1700.0, got {yearly_2026}"

        yearly_2025 = sum(o["amount"] for o in data["orders"].values() if o["date"].startswith("2025"))
        assert yearly_2025 == 800.0, f"Expected 2025 total 800.0, got {yearly_2025}"

        # Test: alltime total
        alltime = sum(o["amount"] for o in data["orders"].values())
        assert alltime == 2500.0, f"Expected alltime 2500.0, got {alltime}"

        # Test: per-year breakdown (mirrors OrderStore.yearly_breakdown)
        breakdown = {}
        for o in data["orders"].values():
            year = (o.get("date") or "")[:4]
            if len(year) != 4:
                continue
            entry = breakdown.setdefault(year, {"total": 0.0, "order_count": 0})
            entry["total"] += o["amount"]
            entry["order_count"] += 1
        for entry in breakdown.values():
            entry["total"] = round(entry["total"], 2)
        breakdown = {year: breakdown[year] for year in sorted(breakdown, reverse=True)}
        assert list(breakdown.keys()) == ["2026", "2025"], f"Expected years sorted desc, got {list(breakdown.keys())}"
        assert breakdown["2026"] == {"total": 1700.0, "order_count": 2}, f"Bad 2026 breakdown: {breakdown['2026']}"
        assert breakdown["2025"] == {"total": 800.0, "order_count": 1}, f"Bad 2025 breakdown: {breakdown['2025']}"

        # Test: first order date
        dates = [o["date"] for o in data["orders"].values() if o["date"]]
        first = min(dates)
        assert first == "2025-12-01", f"Expected first date 2025-12-01, got {first}"

        # Test: save and reload persistence
        with open(store_path, "w") as f:
            json.dump(data, f)
        with open(store_path, "r") as f:
            reloaded = json.load(f)
        assert len(reloaded["orders"]) == 3, "Persistence failed"

        # Test: processing same orders again adds nothing (deduplication)
        second_count = 0
        for order in orders[:3]:
            order_id = str(order.get("id", ""))
            if not order_id or order_id in data["orders"]:
                continue
            second_count += 1
        assert second_count == 0, f"Dedup failed: got {second_count} new on re-process"

    print("All tests passed!")


if __name__ == "__main__":
    test_order_store()
