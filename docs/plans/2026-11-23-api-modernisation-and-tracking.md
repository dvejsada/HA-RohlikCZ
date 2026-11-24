# rohlik-api modernisation, new endpoints and delivery tracking — Plan

**Inputs:** PR #113 (`docs/rohlik-ios-api.md`, iOS capture), live probes on 2026-11-23 against
rohlik.cz using the web-login session of `rohlik-api` 0.3.0 (appendix).

**Scope:** `rohlik_api_python` is the product. HA-RohlikCZ is one consumer among others
(rohlik-mcp, scripts, third parties). Library changes are designed for all of them, and HA work
only follows from what the library offers.

---

## Principles

1. **Stable public contract.** Every existing public method keeps its name, signature, return type
   and response shape. Changes may only **add** data (new keys or model fields), never remove or rename.
   Raw-dict methods keep their `{status, messages, data}` envelope.
2. **Prefer the new endpoints.** The old `/services/frontend-service/*` paths may be retired
   without notice. Where a new `/api/...` endpoint carries the same information, the method
   reads it and an **adapter** rebuilds the legacy shape.
3. **Legacy fallback for one release cycle.** A migrated method tries the new endpoint first. If it
   returns 404/410/5xx, or the adapter can't parse the response, it calls the legacy endpoint. This guards
   against the new endpoint changing as well. The fallback is removed once the new path has been stable.
4. **New capabilities get new methods** with typed models (`@dataclass(slots=True)` + `from_api`,
   as existing models do), whether or not HA uses them.
5. **No new requests in `get_data()`.** The aggregate keeps its keys and costs. Consumers call new
   methods explicitly.
6. **Observable drift.** The client records which path served each migrated call, so consumers can
   surface it (e.g. HA diagnostics) and we learn early when something breaks.

---

## Phase 1 — buildable and verifiable now (no active order needed)

### 1.1 Library infrastructure

| Item | Detail |
| --- | --- |
| `BaseService._fetch_preferred(new, adapt, legacy, ctx)` | Implements principles 2–3. Returns the adapted dict. On fallback it logs once per method at INFO and increments a counter |
| `RohlikAPI.endpoint_report()` | `{method: {"served_by": "new"/"legacy", "fallbacks": n, "last_error": str}}` for diagnostics |
| Site timezone in services | Some new endpoints return local times without an offset (`"2026-11-23 22:00"`). Resolve the tz from `base_url` via `SITES` (default `Europe/Prague`) and pass it into the adapters |
| Error envelope | New endpoints return `{status: int, messages: [{messageCode, content}]}` on 4xx, and `status` is sometimes the string `"success"` on 200. Add one helper to read both |
| Fixtures | Store redacted copies of the probe responses in `tests/fixtures/*.json`, for adapter tests in both directions (new → legacy shape equals real legacy response, ignoring documented gaps) |
| Live contract tests | `tests/live/` is skipped unless `ROHLIK_USERNAME`/`ROHLIK_PASSWORD` are set. It is read-only and checks that each endpoint answers 200 with the keys our adapters need. Run it manually or in a scheduled job to catch deprecations early |

### 1.2 Migrations of existing methods

| Method | Legacy path | New path | Adapter fidelity | Decision |
| --- | --- | --- | --- | --- |
| `delivery.get_next_slots()` | `frontend-service/timeslots-api/0?userId&addressId` | `GET /api/v3/timeslots/address?addressId&reasonableDeliveryTime=true` | High. Same tree. Differences: `price` int → `{amount,currency}` (adapter flattens, and adds `currency`); no `interval{since,till}` ISO (adapter builds it from `since`/`till` + site tz); missing `deliveryAreaName`, `selectedPriceLevel`, `restrictionMessageSummary` (set to `None`). New adds `timeSlotReservationDTO{enabled,duration}`. No `userId` needed | **Migrate** |
| `delivery.get_addresses()` / `get_active_address_id()` | `frontend-service/delivery-address/list` | `GET /api/destinations/v1` | Medium. Maps `display→fullAddress`, `stores[0]→store`, `flatDetails` 1:1. **`isDeliveredTo` is absent**, so `get_active_address_id` falls back to the first destination (all three were `true` in the probe) | **Migrate**, with legacy fallback. New typed `get_destinations()` too |
| `account.get_premium_profile()` | `frontend-service/premium/profile` | `GET /api/v1/premium/profile` (found by probing, not in the capture) | High. Renames `premiumTrialsAdvantages→premiumTrials`, `deals→premiumDeals`. Missing: `minimalPrice` (available from `/api/v6/config` `minimalPrice.order`, or leave it out), `stats.savedTotal/savedTitle/averageSavings/possibleSavings` (`savedTotal` ≈ `savings.total`; others `None`) | **Migrate**; list the gaps in the changelog |
| `account.get_shopping_list(id)` | `/api/v1/shopping-lists/id/{id}` | `/api/v2/shopping-lists/id/{id}` | High. Superset (`sharingInfo`). `productActions` are shorter but the `ShoppingList` model doesn't use them | **Migrate** |
| `orders.get_detail(id)` | `/api/v3/orders/{id}` | `/api/v4/orders/{id}` | High for what consumers read (HA uses item `id,name,amount,priceComposition,textualAmount`, all present). v4 adds `state`, full `priceComposition` (goods/delivery/tip/credits/fines/fees), `documents`, `deliveryType`. Per-item `categories` need verifying (not visible at top level); HA gets categories from `products/{id}/categories` anyway | **Migrate**; adapter keeps any v3-only keys we find |
| `delivery.get_info()` (first delivery) | `frontend-service/first-delivery` | `/api/v1/timeslots/first-delivery` | Low. New: `{type, firstDeliveryAvailableSinceMessage:"Dnes 20:45"}`. Legacy: `deliveryType`, `firstDeliveryText.default:"Do 90 min"`, `earlierDelivery`, `deliveryLocationText` (address) | **Keep legacy primary.** The fallback is *composed*: text from new v1, location from destinations, type from v3 slots. Add typed `get_first_delivery()` on the v1 path |
| `delivery.get_timeslot_reservation()` | `frontend-service/v1/timeslot-reservation` → `{active, reservationDetail}` | none found (`/api/v1/timeslot-reservation` 404). Candidates: checkout `timeslotSection.slotId`, v3 slots `timeSlotReservationDTO` | Unknown | **Keep legacy.** Can be verified now **if a slot reservation is allowed** (no order needed) |
| `delivery.get_announcements()` | `frontend-service/announcements/delivery` (`data.announcements[]`) | `/api/v1/announcements/delivery` (`data{available, announcement}`, singular) | Can't be compared without an announcement | **Phase 2** |
| `cart.get_content()` | `frontend-service/v2/cart` | `/api/v2/cart` | Low. Items only carry `productId, amount, unitPrice`: no name, brand, category, **no `orderFieldId`** (needed by `delete_item`), no totals/minimum order/`submitConditionPassed` | **Keep legacy.** Add typed `get_cart_summary()` on v2 plus product cards |
| `cart.add_items()` | `POST frontend-service/v2/cart` | `POST /api/v1/shopping-lists/cart/all` `{"products":[{"productId","quantity"}]}`: **verified**, adds to the existing quantity, returns the same envelope as the legacy cart | Medium (the path is meant for lists, so its semantics could change) | **Keep legacy primary**, new path as fallback |
| `cart.delete_item()` | `DELETE frontend-service/v2/cart?orderFieldId` | `PATCH /api/v6/cart-review` `{"items":[{productId, amount, actionType}]}`: `actionType` enum unknown | — | **Keep legacy.** Ask the PR author for one captured request that changes a quantity (guessing triggers server-side errors) |
| `products.search()` | `GET frontend-service/search-metadata` | `POST /api/v3/search/autocomplete` → groups `PRODUCT`/`PHARMACY`/`RECIPE`, products as IDs + `promotionType` | Medium. Needs `products.get_cards()` for names/prices (2 requests); sponsored filter via `promotionType`; favourite flag from cards or user-data, to verify | **Migrate if card parity holds** for every `ProductSearchResult` field; otherwise fallback. Add typed `search_all()` that returns all groups (incl. recipes) |
| `account.get_announcements()` (top) | `frontend-service/announcements/top` | `/api/v1/announcements/top` → 400 (needs a parameter?) | — | Keep legacy; check the 400 message |
| `recipes.*` | `frontend-service/recipe/*`, `…/chef/ingredients/products` | `/api/v1/chef/{id}/ingredients/selection`, `chef-recipe` components; search not captured | Low | Keep legacy; Phase 3 |
| `login` / `logout` | `frontend-service/login`/`logout` (cookie) | App: `POST /api/v5/login/access_token` (token) | — | **Keep.** All new endpoints accept the cookie session. Biggest single deprecation risk, so track it in the live tests |

Already on modern paths (no change): `orders.get_next/get_last/get_delivered`, `account.get_bags_info`,
all `products.*` detail/price/cards/categories, `get_week_sales`.

### 1.3 New library methods (all verified live, read-only unless marked)

| Service.method | Endpoint | Model | Notes |
| --- | --- | --- | --- |
| `account.get_shopping_lists()` | `GET /api/v1/shopping-lists/preview` | `ShoppingListPreview(id, name, product_count, description, status, read_only)` | List discovery |
| `cart.add_shopping_list(list_id, only_available=True)` **(write)** | v2 list → `POST /api/v1/shopping-lists/cart/all` | returns `Cart` | Adds the list's amounts on top of the cart |
| `products.get_favorites(page, size, sort, on_sale=False)` | `GET /api/v1/components/product-section/favorites[-in-sale]` | `ProductIdPage(product_ids, page, size, total?)` | Ad cards are dropped. Convenience `get_favorite_cards()` adds `get_cards` |
| `products.get_stock(id)` | `GET /api/v1/products/{id}/stock` | `ProductStock(in_stock, max_basket_amount, unavailability_reason, expected_replenishment, preorder_enabled, warehouse_id)` | `warehouse_id` is also needed for v3 search |
| `products.get_user_data(id)` | `GET /api/v1/products/{id}/user-data` | `ProductUserData(favorite, shopping_list_ids, shopping_routine, user_rating, watchdog_active)` | |
| `account.get_notifications(page, size)` | `GET /api/v1/notifications` | `Notification(id, title, text, link, created_label, clicked)` | `createdAt` is a localized label (`"22.11."`), so it's kept as text and **not** parsed |
| `account.get_notification_stats()` | `GET /api/v1/notifications/stats` | `NotificationStats(unseen_count, enabled)` | |
| `account.get_credits()` | `GET /api/v3/user/credits` | `Credits(total, description, items: list[dict])` | Item shape unknown (empty in probe), so items stay raw |
| `delivery.get_slots(address_id=None)` | `/api/v3/timeslots/address` | `DeliverySlots(express, first, eco, days, usual_express_time, …)` with `Slot(since, till (aware), type, price, currency, capacity_percent, capacity_message, eco, premium)` | Typed version of the migrated raw method |
| `delivery.get_destinations()` | `/api/destinations/v1` | `Destination(id, name, type, address_display, latitude, longitude, store_id)` | |
| `orders.get_order(id)` | `/api/v4/orders/{id}` | `OrderDetail(id, state, delivery_type, slot, price_breakdown, documents, items, packaging, available_actions)` | Typed |
| `orders.get_canceled(limit, offset)` | `/api/v3/orders/canceled` | raw list (same as delivered) | |
| `cart.get_review()` | `GET /api/v6/cart-review` | `CartReview(valid, total, currency, section_types)` | |
| `cart.get_checkout_status()` | `GET /api/v3/checkout` | `CheckoutStatus(all_valid, sections: {address, contact, courier, packaging, timeslot, payment: bool})` | Read only. Never calls submit |
| `orders.get_delivery_tracking(id)` | `/api/v2/orders/{id}/delivery-tracking` | `DeliveryTracking` (per PR #113 model: status, ETA, stops, courier name, position, slot, early delivery, **destination lat/lon**) | Verified: 200 for a past order (`progress: null`), 404 for an unknown ID. `progress` parsing is based on PR samples only, so it's marked **experimental** until Phase 2 |
| `shop.get_config()` (optional) | `/api/v6/config` | `ShopConfig(minimal_order_price, customer_care, last_supported_app_versions)` | Low priority |

Release as **0.4.0**: README API reference, a "Migration notes" section listing the shape gaps
above, version bump, PUBLISHING.md.

### 1.4 HA-RohlikCZ on 0.4.0 (Phase 1 features)

Bumping alone changes no entity (contract principle). On top of it, everything below works
without an active order:

| Feature | Source | Cost |
| --- | --- | --- |
| Diagnostics: `endpoint_report()` | lib | none |
| **Shopping list picker** in options / todo setup instead of typing an ID | `get_shopping_lists()` | on demand |
| Service `rohlikcz.add_shopping_list_to_cart` | `cart.add_shopping_list()` | on demand |
| Sensor **Favourites on Sale** (count, attrs: name/price/discount top N) | favorites-in-sale + cards | 2 req, own 1 h cadence |
| Sensor **Unread Notifications** (count, attrs: latest titles/links) | stats (+ list when count > 0) | 1–2 req per 10 min, or slower |
| **Express Available** / slot sensors: confirm the v3 slots fill `expressSlot` (the legacy call returned `null` in a probe, possibly because of the probe's dummy `userId`; to check) | migrated `get_next_slots` | none |
| Cart sensor attr **Checkout Ready** (`all_valid` + per-section) next to `Can Order` | `get_checkout_status()` | 1 req per 10 min, only when cart is non-empty |
| Last order: price breakdown + documents (receipt link) as attributes | `get_order(last_id)` once per new last order | ~0 |
| Next Order attrs `items_count` / `items_total_quantity` | existing `next_order` data | none |

Translations en/cs/de and readme for each. Version **1.3.0**.

---

## Phase 2 — needs an active order (verify during a real delivery)

1. Live check of `delivery-tracking` `progress` against the PR samples; finalise the model and drop "experimental".
2. Migrate `delivery.get_announcements()` to `/api/v1/announcements/delivery` (singular →
   wrap into `announcements[]`), after comparing both responses for the same announcement.
3. HA delivery tracking (from the earlier plan, unchanged in substance):
   - ETA source `tracking` takes priority over the announcement, then the slot. Keep polling while the status is active; stop at `DELIVERED`.
   - New **Delivery Status** enum sensor (`arrived` trigger for #105) and **Stops Before Delivery** sensor.
   - **Last Mile** turns on for `ARRIVED`/`HANDING_OVER` and off at `DELIVERED`.
   - Opt-in **Courier** `device_tracker`, plus distance to the destination computed from `destination` + position.
   - Redact courier name, phone, photo, coordinates and polylines in diagnostics.
4. Upcoming order: confirm `itemsTotalQuantity` and v4 `state: UPCOMING` before relying on them.

## Phase 3 — needs extra captures or a decision

- `PATCH /api/v6/cart-review` `actionType` values: one captured request from the app → migrate
  `delete_item` and add `set_quantity()`.
- Timeslot reservation on new endpoints: needs permission to reserve a slot for the test.
- SSE `/services/customer-events`: library `async for event in client.events.stream()`; HA optional.
- Token login (`/api/v5/login/access_token`) as an alternative auth strategy, if the cookie login ever goes away.
- Recipes on `chef-recipe` endpoints; `/api/v1/announcements/top` parameter.

---

## Appendix — live probe results (2026-11-23)

Raw responses are kept outside the repos (they contain personal data). Only redacted fixtures may be committed.

| Endpoint | Status | Shape notes |
| --- | --- | --- |
| `GET /api/v2/orders/{past id}/delivery-tracking` | 200 | `orderId, deliverySlot{since,till,type}, destination{latitude,longitude}, courier{displayName,phoneNumber,photoUrl}, earlyDelivery{available,requested,allowedFrom}, progress: null` |
| same, unknown ID | 404 | `{status, messages[{messageCode,…}]}` |
| `GET /api/v4/orders/{id}` | 200 | `state, deliveryType, deliverySlot, priceComposition{total,goods,delivery,creditsUsed,courierTip,fines,reusableBagsDeposit,paymentFee,packagingFee}, documents[], items[{id,name,amount,textualAmount,priceComposition,orderFieldId,compensated}], selectedPackaging, availableActions` |
| `GET /api/v3/orders/delivered`, `/canceled` | 200 | `itemsCount` and `itemsTotalQuantity`, `availableActions` |
| `GET /api/v1/shopping-lists/preview` | 200 | `[{id,name,numberOfProducts,status,readOnly,description,…}]` |
| `GET /api/v2/shopping-lists/id/{id}` | 200 | v1 + `sharingInfo` |
| `POST /api/v1/shopping-lists/cart/all` | 200 | Body `{"products":[{"productId","quantity"}]}` (`amount` → 400). Adds to the existing quantity. Returns the legacy cart envelope |
| `PATCH /api/v6/cart-review` | 400 | Needs item-level `actionType` (enum unknown; 8 guesses failed) |
| `GET /api/v1/components/product-section/favorites[-in-sale]` | 200 | `productIds[]`, `pageable`, ad cards |
| `GET /api/v1/notifications`, `/stats` | 200 | localized `createdAt`; `{unseenCount, enabled}` |
| `GET /api/v3/user/credits` | 200 | `{totalCreditsAmount, description, creditItems[]}` |
| `GET /api/v1/timeslots/first-delivery` | 200 | `{status, type, firstDeliveryAvailableSinceMessage}` |
| `GET /api/v3/timeslots/address` | 200 | as legacy timeslots minus 3 keys; `price` object; no `interval` |
| `GET /api/destinations/v1` | 200 | `destinations[{id,name,type,address{display,…,latitude,longitude,stores,flatDetails}}]` |
| `GET /api/v1/premium/profile` | 200 | legacy minus `minimalPrice`, 4 stats keys; 2 renames |
| `GET /api/v1/announcements/delivery` | 200 | `data{available, announcement}` |
| `GET /api/v1/announcements/top` | 400 | — |
| `GET /api/v1/timeslot-reservation` | 404 | — |
| `GET /api/v2/cart` | 200 | thin items, `destination.address` (coordinates → redact) |
| `GET /api/v6/cart-review` | 200 | `{valid, totalPrice, sections[type]}` |
| `GET /api/v3/checkout` | 200 | `formSections.*.data.valid`, `allSectionsValid` |
| `POST /api/v3/search/autocomplete` | 200 | Body `{search,type:"ALL",sort,limit,offset,filters:[],warehouseId}`. Groups `PRODUCT`, `PHARMACY`, `RECIPE` |
| `GET /api/v1/products/{id}/stock`, `/user-data` | 200 | see 1.3 |
| `GET /api/v6/config` | 200 | `minimalPrice, customerCare, lastSupportedVersion, features, modules…` |
| All legacy `frontend-service` endpoints in use | 200 | still alive today |
