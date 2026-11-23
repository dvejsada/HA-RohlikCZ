# Rohlík.cz iOS API reference

**Observation date** 23 November 2026 · **Market** Czech Republic · **iOS app build** `2026111949` · **API origin** `https://www.rohlik.cz`

## At a glance

`GET /api/v2/orders/{orderId}/delivery-tracking` returns the ETA in `progress.estimatedArrival` and the delivery phase in `progress.deliveryStatus`. On the observed order, the iPhone showed “Přijeli jsme” (“We have arrived”) when the API reported `ARRIVED`. It reported `DELIVERED` after handover. This distinction matters for the arrival trigger discussed in [HA-RohlikCZ discussion #105](https://github.com/dvejsada/HA-RohlikCZ/discussions/105).

| Information | Where to find it |
| --- | --- |
| ETA, stops ahead, courier name and map coordinates | The [tracking response](#tracking-response) documents `progress.stopsBefore`, `courier.displayName`, `progress.courierPosition.latitude` and `progress.courierPosition.longitude`, with sample JSON and observations from queueing through delivery. |
| Changing courier position and ETA | [Live updates](#live-updates-via-server-sent-events) describes the event stream, payload fields and observed position update intervals. |
| Home-screen messages, item quantity and bags | [Home-screen card and announcements](#home-screen-card-and-announcements) identifies the source of the banner text. [Counts shown on the iPhone](#counts-shown-on-the-iphone) distinguishes order quantities from the bag count in the announcement. |
| Existing integration and Python client | [Home Assistant compatibility](#home-assistant-compatibility) identifies the existing ETA source and the remaining client authentication check. [Python client capabilities](#existing-python-client-capabilities) lists the published library features. |
| Other mobile API calls | The [endpoint inventory](#observed-mobile-endpoints) groups 105 observed method/path combinations by feature, including cart, checkout, slots, search, products, lists, recipes and account services. [Evidence and coverage](#evidence-and-coverage) describes the capture scope. |

## Delivery tracking

Opening the tracking screen issued **`GET /api/v2/orders/{orderId}/delivery-tracking`**. Opening the order detail immediately afterwards issued **`GET /api/v4/orders/{orderId}`**. Both returned HTTP 200. The tracking request had no query string or body. Opening the same tracking URL for the same order in a signed-in desktop Chrome session also returned the matching order and progress data, without supplying a mobile access token or mobile-specific headers. Authentication through the Python client remains untested.

The observed sequence was **IN_QUEUE → ON_THE_WAY → ARRIVED → HANDING_OVER → DELIVERED**. The ETA became available while queued and retained its previous value after delivery.

### Tracking response

The first excerpt shows the queued order before an ETA was available. The server returned additional top-level objects named `deliverySlot`, `destination`, `courier` and `earlyDelivery`, together with `orderId`.

```json
{
  "progress": {
    "generatedAt": "2026-11-23T16:26:13.115788612Z",
    "deliveryStatus": "IN_QUEUE",
    "stopsBefore": 2,
    "estimatedArrival": null
  }
}
```

At 17:48 CET, the same endpoint returned an ETA of **18:32:16 CET** while the order was still queued with two stops ahead. The user could already see the courier's first name on the tracking screen. The map had not appeared. At 17:53 CET a direct request from signed-in desktop Chrome returned a fresh snapshot with a revised ETA of **18:35:39 CET**, the same status and the same stop count. This independently confirmed that an ordinary authenticated GET can retrieve a populated ETA.

```json
{
  "progress": {
    "generatedAt": "2026-11-23T16:47:41.118275652Z",
    "deliveryStatus": "IN_QUEUE",
    "stopsBefore": 2,
    "estimatedArrival": "2026-11-23T17:32:16Z",
    "courierPosition": null,
    "courierMovement": null,
    "routeToStop": null
  }
}
```

At 18:13 CET a desktop tracking GET first showed **one stop ahead**. The following excerpt shows that the stop count can decrease while the order remains queued.

```json
{
  "progress": {
    "generatedAt": "2026-11-23T17:13:33.390715496Z",
    "deliveryStatus": "IN_QUEUE",
    "stopsBefore": 1,
    "estimatedArrival": "2026-11-23T17:28:31Z"
  }
}
```

When the customer reported that the map had appeared, the mobile GET and an `ORDER_DELIVERY_PROGRESS_CHANGED` event both showed `ON_THE_WAY`, zero stops ahead and a populated position and route. Both snapshots were generated at 18:21:57 CET. The following excerpt is from the GET.

```json
{
  "progress": {
    "generatedAt": "2026-11-23T17:21:57.71324352Z",
    "deliveryStatus": "ON_THE_WAY",
    "stopsBefore": 0,
    "estimatedArrival": "2026-11-23T17:28:17Z"
  }
}
```

The coordinate fields are `progress.courierPosition.latitude` and `progress.courierPosition.longitude` (JSON numbers). The timestamp is `progress.courierPosition.recordedAt` (string). `progress.routeToStop` contained strings `encodedPolyline` and `routeCalculationId`. `progress.courierMovement` was still null. The map's appearance and these populated fields were observed together on one order. The app's exact display rule is unknown.

During this phase the customer saw the distance on the iPhone change from **1.5 km** to **1.1 km**, then **734 m** and **552 m**. A desktop tracking GET still reported `ON_THE_WAY` with zero stops ahead at the 552 m observation. The exact unit-switch threshold was not observed. The inspected tracking responses, mobile progress events and home-screen card did not expose an explicit remaining-distance value. A later progress event included a populated `courierMovement` object with `encodedPolyline`, `fromRecordedAt`, `toRecordedAt` and `snapped`. The presence of position and geometry fields does not establish whether the displayed distance is measured along the route, calculated in a straight line, or obtained elsewhere.

At 18:28:23 CET the server still reported `ON_THE_WAY` with an estimated arrival of 18:28:07 CET. An elapsed ETA is therefore insufficient to determine arrival.

The customer then reported the iPhone message **“Přijeli jsme”** (“We have arrived”). The desktop tracking GET returned the excerpt below. The mobile progress event also reported `ARRIVED` at `2026-11-23T17:28:57.304984877Z`, with the same stop count and ETA. In that event the courier position was populated, while movement and route were null.

```json
{
  "progress": {
    "generatedAt": "2026-11-23T17:28:57.306475773Z",
    "deliveryStatus": "ARRIVED",
    "stopsBefore": 0,
    "estimatedArrival": "2026-11-23T17:28:07Z"
  }
}
```

The next observed status was `HANDING_OVER`. The desktop GET and mobile event returned the same `generatedAt` value, `2026-11-23T17:30:05.329783539Z`, with zero stops ahead and the existing ETA. At that check, the order detail (`GET /api/v4/orders/{orderId}`) still reported `state: "UPCOMING"`. The customer then saw the vehicle icon replaced by a person. A fresh tracking snapshot generated at 18:31:29 CET still reported `HANDING_OVER`. This observation associates the person icon with the handover phase. It does not identify the exact instant or condition for the icon change.

At **18:34:18 CET** the per-order tracking GET reported `DELIVERED`, and the separate order-detail GET also returned `state: "DELIVERED"` for the same order. The mobile capture did not retain a matching terminal event. The two GET responses confirm that the service recorded completion. The exact moment the customer physically received the bags was not independently timed. The tracking endpoint remained readable in this immediate post-delivery check.

```json
{
  "progress": {
    "generatedAt": "2026-11-23T17:34:18.746120179Z",
    "deliveryStatus": "DELIVERED",
    "stopsBefore": null,
    "estimatedArrival": "2026-11-23T17:28:07Z"
  }
}
```

| Field | Observation | Implication for a consumer |
| --- | --- | --- |
| `progress.generatedAt` | UTC timestamp string with variable fractional-second precision | Identifies when the progress snapshot was generated. Consecutive reads can return the same snapshot. |
| `progress.deliveryStatus` | `IN_QUEUE` → `ON_THE_WAY` → `ARRIVED` → `HANDING_OVER` → `DELIVERED` | Use `ARRIVED` for reported courier arrival and `DELIVERED` for completion. This sequence describes one observed delivery. |
| `progress.stopsBefore` | Integers `2`, `1` and `0`, then `null` once delivered. The initial value matched the two stops shown on the iPhone. | Preserve the difference between zero remaining stops and an absent count. |
| `progress.estimatedArrival` | Initially `null`, then a UTC timestamp string with second precision | Parse a populated value as a timezone-aware instant. The field can become available before the status changes or a courier position appears. |
| `courier.displayName` | A non-empty string containing the courier's first name only in this sample. The customer confirmed the screen displayed the first name. | Treat it as an optional display label. This observation establishes neither a fixed name format across orders nor a stable courier identifier. |
| `courier.phoneNumber`, `courier.photoUrl` | A phone string was populated and the photo URL was null in this sample. | Neither value is needed to read the display name or ETA. |
| Other progress fields | Position, movement and route were null while queued. Position and route were populated in the on-the-way sample. `courierLocationStale` was a boolean. | Handle each nullable field independently. |
| `deliverySlot` | `since`, `till`, `type` | Describes the booked interval. |
| `earlyDelivery` | Boolean `available` and `requested`, plus a UTC `allowedFrom` timestamp | Describes early-delivery eligibility and the customer's request. It has a separate meaning from estimated arrival. |

### Home-screen card and announcements

The home-screen card had an estimate even while the tracking response's `progress.estimatedArrival` was null. At the same point in the iOS capture, `GET /api/v2/homepage/components/announcement/` returned an empty JSON array. The card was delivered by **`GET /api/v3/homepage/components/order-tracking/`**. Its observed response included this excerpt. The strings are preserved in the original locale.

```json
{
  "label": "Doručení dnes",
  "headline": "19:00 → 18:39",
  "statusLine": {
    "text": "Doobjednat už nejde.",
    "indicator": { "type": "DOT_PULSING", "style": "POSITIVE" }
  }
}
```

Later, the customer reported that the iPhone had replaced the stop count with a message that the courier was nearby. At approximately 18:22 CET an authenticated read of the home-screen card for the same order returned this excerpt:

```json
{
  "statusLine": {
    "text": "Kurýr je blízko"
  }
}
```

The tracking GET at that check still returned `IN_QUEUE`, `stopsBefore: 1` and an ETA of 18:28:06 CET. Its `generatedAt` was 18:17:20 CET, several minutes before the card was read. The “nearby” message alone cannot establish zero remaining stops, courier arrival, or the rule iOS uses to switch labels.

| Surface | Source | Reading for this order |
| --- | --- | --- |
| iOS tracking screen | `/api/v2/orders/{orderId}/delivery-tracking` → `progress.estimatedArrival` | Changed from null to a UTC timestamp while the status remained `IN_QUEUE`. |
| iOS home-screen card | `/api/v3/homepage/components/order-tracking/` → `headline` | Localized display string containing the revised time. No standalone timestamp was present at the top level of this card. |
| Web delivery announcement | `/services/frontend-service/announcements/delivery` → `data.announcements[0].content` | Web-session check found the visible banner text including `<span>18:39</span>`. A separate web-session request to `/api/v1/announcements/delivery` returned the same text in `data.announcement.content`. |
| Upcoming orders and order detail | `/api/v3/orders/upcoming` → `expressDeliveryPromise`; `/api/v4/orders/{orderId}` → `expressDeliveryMessage` | Both fields were null in the web-session check for this booked order. |

### Live updates via server-sent events

The iOS app opened **`GET /services/customer-events`** with `Accept: text/event-stream`. A successful HTTP 200 response used that media type and carried both `PING` and `ORDER_DELIVERY_PROGRESS_CHANGED` events. The latter contained the order ID and progress values directly in the JSON `eventData` object. The following excerpt shows the ETA changing to **18:33:13 CET** while the status and stop count stayed the same.

```json
{
  "eventData": {
    "generatedAt": "2026-11-23T16:48:08.094538638Z",
    "deliveryStatus": "IN_QUEUE",
    "stopsBefore": 2,
    "estimatedArrival": "2026-11-23T17:33:13Z"
  }
}
```

The event also included `expectedUpdateIntervalSeconds` (integer), `courierLocationStale` (boolean), and nullable position, movement and route fields. The stream's envelope contained `type`, `timestamp` and `eventData`. A consumer can retrieve the current ETA and courier display name with the per-order GET. A stream consumer must associate each progress event with its `eventData.orderId`.

A later mobile event with `generatedAt: "2026-11-23T17:12:09.535965274Z"` reported one stop ahead while the order remained `IN_QUEUE`. Its ETA of `2026-11-23T17:28:31Z` matched the desktop GET sampled at 18:13 CET. A subsequent mobile GET and desktop GET returned the identical `generatedAt` value `2026-11-23T17:17:20.787386039Z`, one stop ahead and an ETA of `2026-11-23T17:28:06Z`.

While the iPhone displayed a moving vehicle icon, the mobile event stream carried 11 distinct `courierPosition.recordedAt` timestamps. The coordinate pair changed between every consecutive pair of readings. The last six observed sampling intervals ranged from 20 to 30 seconds. Some events included a movement polyline with a start and end timestamp and a boolean `snapped` field. These payloads supply geometry for a client to animate a marker. The capture establishes neither the app's interpolation algorithm nor the physical accuracy of the reported coordinates.

### Counts shown on the iPhone

The customer reported seven items, five bags and two courier stops ahead for the same order. The API exposed the item and stop counts as integers. The delivery announcement supplied the bag count in localized text. The notification's own update mechanism remains unverified.

| Displayed value | Evidence for this order |
| --- | --- |
| 7 items | The matching entry in `GET /api/v3/orders/upcoming` had `itemsTotalQuantity: 7` and `itemsCount: 6`. In `GET /api/v4/orders/{orderId}`, the six `items[]` rows included one with `amount: 2`. Their amounts summed to seven. |
| 5 bags | An authenticated `GET /services/frontend-service/announcements/delivery` returned the Czech phrase “5 tašek” after HTML and whitespace normalization in `data.announcements[0].additionalContent`. The sibling `content` field contained no bag count in this response. The inspected order and tracking responses exposed no dedicated integer for bags in this delivery. |
| 2 stops ahead | The tracking response contained `progress.stopsBefore: 2`. The `ORDER_DELIVERY_PROGRESS_CHANGED` event also carried `eventData.stopsBefore: 2`. An integration can read the stop count and ETA together in one tracking request. |

HA-RohlikCZ v1.2.0 exposes the HTML-stripped `additionalContent` as the **Additional Content** attribute of its Delivery Information sensor. The live attribute contained “5 tašek” for this order. A Czech-language consumer can extract the number from that attribute without an extra API request. Treat a missing or unrecognized phrase as unknown. The integration can retain the previous attributes while an order is active, so check the associated order ID when reusing them. Other locales and Czech plural forms require separate verification. [Delivery Information sensor implementation](https://github.com/dvejsada/HA-RohlikCZ/blob/v1.2.0/custom_components/rohlikcz/sensor.py#L108-L146)

### Home Assistant compatibility

HA-RohlikCZ v1.2.0 pins `rohlik-api==0.3.0`. Its targeted delivery refresh currently calls `client.delivery.get_announcements()`. The ETA tracker associates the announcement with the soonest upcoming order and handles a missing announcement, a previously observed estimate and the booked slot. The Python client's endpoint list at that tag has no dedicated delivery-tracking method. [Integration dependency](https://github.com/dvejsada/HA-RohlikCZ/blob/v1.2.0/custom_components/rohlikcz/manifest.json) · [Targeted refresh](https://github.com/dvejsada/HA-RohlikCZ/blob/v1.2.0/custom_components/rohlikcz/hub.py#L884) · [ETA tracker](https://github.com/dvejsada/HA-RohlikCZ/blob/v1.2.0/custom_components/rohlikcz/eta.py) · [Client endpoints](https://github.com/dvejsada/rohlik_api_python/blob/v0.3.0/rohlik_api/endpoints.py)

The smallest integration change worth evaluating is a read for the current order ID in the existing Python client. The same response supplies the ETA, stop count, arrival status and optional courier display name. Before changing ETA source priority, verify the existing client session can read the endpoint and compare a populated tracking ETA with the announcement for the same order.

## Existing Python client capabilities

| Area | Coverage at `rohlik-api` v0.3.0 |
| --- | --- |
| Catalog and cart | Product search, details, prices, composition, categories, cards, deals and AI summary. Cart reads and item changes. |
| Orders | Upcoming order, last order, paginated delivered orders and individual order details. |
| Delivery | Delivery announcements, first available delivery, the current slot reservation and upcoming slots for an address. |
| Lists, recipes and account | Saved shopping list by ID, recipe search, recipe detail and ingredients, account announcements, premium profile and bags. |

These are capabilities of the released library. The iOS inventory below identifies the particular paths and versions the app called. Equivalent user-facing features can use different endpoints. [Tagged Python client reference](https://github.com/dvejsada/rohlik_api_python/blob/v0.3.0/README.md#api-reference) · [Tagged Home Assistant integration](https://github.com/dvejsada/HA-RohlikCZ/tree/v1.2.0)

## Evidence and coverage

The primary evidence is an HTTPS capture from a signed-in customer using the Czech iOS app on their own account. The build identifier was present in `X-App-Version`. The inventory covers the observed host and screens during one session. Product availability, account features, market and app build can change which requests the app makes. The JSON excerpts preserve values from the captured responses and event payload. Field lists below are samples, and their presence in one response does not establish that they are required.

This document contains no session material, raw traffic archive, customer or order identifiers, addresses, courier details or location history. Rohlík separately documents a public MCP interface for functions such as shopping and order history. The mobile paths in this reference have no vendor compatibility guarantee identified in that documentation. [Rohlík MCP documentation](https://www.rohlik.cz/en-CZ/mcp-docs)

### Search, lists and delivery slots

| Workflow | How the observed resources fit together |
| --- | --- |
| Product search | The search screen fetched recommendations and trending searches. A `POST /api/v3/search/autocomplete` returned a `results` array of groups. Observed group keys included `type`, `totalHits`, `offset`, `items`, `categories` and `suggestions`. In one request, the app sent `type: "ALL"`, `sort: "recommended"`, `limit: 30` and `offset: 0`, alongside the search text, filters and warehouse ID. These values describe the sample rather than server defaults. |
| Category browsing | The paged category listing exposed `productIds` and `productsWithType`. `GET /api/v1/products/card` accepted a `typedProducts` query parameter and returned an array containing prices and stock. |
| Saved shopping lists | `GET /api/v1/shopping-lists/preview` returned list IDs, names and product counts. `GET /api/v2/shopping-lists/id/{id}` returned the selected list, including `products`, `productSections` and `readOnly`. List discovery complements the Python client's existing lookup by ID. |
| Slot selection | The `data` object from `GET /api/v3/timeslots/address` included `availabilityDays`, `expressSlot`, `preselectedSlots`, `priceLevels` and `usualExpressTime`. The associated request included an address ID. The response describes the slots available in that shopping context. |
| Recipe ingredients | The preparation component returned instructions and an ingredient summary. A separate `GET /api/v1/chef/{id}/ingredients/selection` returned `ingredientGroups`, `metadata` and `presets`. |

The inventory describes calls made during browsing, cart review, slot display, order inspection and recipe viewing. Purchase submission, payment authorization, delivery changes, post-delivery claims and account changes were outside the walkthrough. Calls initiated by the app were observed passively.

## Observed mobile endpoints

The inventory contains **105 distinct method/path combinations** on `www.rohlik.cz` after replacing record identifiers with `{id}`. Paths retain observed API versions and trailing slashes. Named selectors such as `price-hits` are shown as captured. Input columns list observed query or JSON keys. A dash indicates that no keys are documented. Status codes apply to the individual observations.

### Orders, tracking and announcements

Upcoming, delivered and canceled orders are list resources. The detail and tracking paths below take an order ID. The spelling `canceled` is part of the URL.

| Method and path | Observed inputs | HTTP | Response shape or selected fields |
| --- | --- | --- | --- |
| `GET /api/v2/homepage/components/announcement/` | — | 200 | Empty array `[]` |
| `GET /api/v2/orders/{id}/delivery-tracking` | — | 200 | Object · `progress`, `deliverySlot`, `earlyDelivery`, `orderId` |
| `GET /api/v3/homepage/components/order-rating/` | — | 200 | Object · `orderId`, `announcementText`, `claimActive`, `orderHash` |
| `GET /api/v3/homepage/components/order-tracking/` | — | 200 | Object · `orderId`, `headline`, `statusLine`, `buttons` |
| `GET /api/v3/orders/canceled` | Query `limit`, `offset` | 200 | Array · item fields `deliverySlot`, `expressDeliveryPromise`, `itemsCount`, `address` |
| `GET /api/v3/orders/delivered` | Query `limit`, `offset` | 200 | Array · item fields `deliverySlot`, `expressDeliveryPromise`, `itemsCount`, `address` |
| `GET /api/v3/orders/upcoming` | — | 200 | Array · item fields `deliverySlot`, `expressDeliveryPromise`, `itemsCount`, `itemsTotalQuantity` |
| `GET /api/v4/orders/{id}` | — | 200 | Object · `deliverySlot`, `state`, `expressDeliveryMessage`, `items` |
| `GET /services/customer-events` | `Accept: text/event-stream` | 200 | Server-sent events · `PING`, `ORDER_DELIVERY_PROGRESS_CHANGED` |

### Cart, checkout and delivery slots

The cart, its review screen, checkout information and slot availability have separate responses. The slot lookup included `addressId` and `reasonableDeliveryTime`. Opening a checkout screen does not establish a purchase or reservation contract.

| Method and path | Observed inputs | HTTP | Response shape or selected fields |
| --- | --- | --- | --- |
| `GET /api/destinations/v1` | — | 200 | Object · `status`, `destinations` |
| `GET /api/v1/cart-review/items/upgrades` | — | 200 | Empty array `[]` |
| `GET /api/v1/timeslots/first-delivery` | Query `reasonableDeliveryTime` | 200 | Object · `status`, `firstDeliveryAvailableSinceMessage`, `type` |
| `GET /api/v2/cart` | — | 200 | Object · `cartId`, `items`, `totalPrice`, `status` |
| `GET /api/v3/checkout` | — | 200 | Object · `checkout`, `errorResponse` |
| `GET /api/v3/timeslots/address` | Query `addressId`, `reasonableDeliveryTime` | 200 | Object · `data`, `status` |
| `GET /api/v3/upsell/LOWER_BELT` | — | 200 | Object · `categories`, `title`, `upsellShowType` |
| `GET /api/v3/upsell/UPPER_BELT` | — | 200 | Object · `categories`, `title`, `upsellShowType` |
| `GET /api/v6/cart-review` | — | 200 | Object · `cartId`, `totalPrice`, `valid`, `sections` |
| `PATCH /api/v6/cart-review` | JSON `items` | 200 | Object · `cartId`, `totalPrice`, `valid`, `sections` |
| `POST /api/v6/cart-review` | — | 200 | Object · `cartId`, `totalPrice`, `valid`, `sections` |
| `GET /api/v6/cart-review/ad-banners` | — | 200 | Object · `status`, `banners`, `version` |
| `GET /api/v6/cart-review/banners` | Query `version` | 200, 204 | Object · `status`, `banners`, `version` |

### Categories and search

The category product listing returns identifiers and pagination metadata. Product cards are fetched separately. Category filter options were fetched using POST with a JSON body.

| Method and path | Observed inputs | HTTP | Response shape or selected fields |
| --- | --- | --- | --- |
| `GET /api/v1/categories` | Query `categories`, `type` | 200 | Array · item fields `categoryId`, `categoryType`, `description`, `hasSubcategories` |
| `GET /api/v1/categories/normal/{id}/` | — | 200 | Object · `categoryId`, `categoryType`, `description`, `hasSubcategories` |
| `GET /api/v1/categories/normal/{id}/components/banners` | — | 200, 404 | Object · `categoryId`, `banners`, `categoryType`, `componentType` |
| `GET /api/v1/categories/normal/{id}/components/recommended-products` | — | 200 | Object · `products`, `categoryId`, `categoryType`, `componentType` |
| `GET /api/v1/categories/normal/{id}/products` | Query `filter`, `page`, `size`, `sort` | 200 | Object · `productIds`, `productsWithType`, `categoryId`, `pageable` |
| `GET /api/v1/categories/normal/{id}/subcategories` | Query `filter` | 200 | Object · `categoryIds`, `categoryId`, `categoryType` |
| `GET /api/v1/categories/{id}/components/shoppable-display` | — | 200 | Object · `categoryId`, `categoryType`, `componentType`, `shoppableDisplay` |
| `POST /api/v1/filters/category` | JSON `categoryId`, `categoryType`, `filters` | 200 | Object · `itemsCount`, `filterGroups`, `activeFilters`, `productsCount` |
| `GET /api/v1/filters/filter-lock` | — | 200 | Object · `lockedFilters` |
| `GET /api/v1/search/placeholders` | — | 204 | No content |
| `GET /api/v1/search/recommendation` | — | 200 | Object · `products`, `categories`, `mediumRectangleBanners` |
| `GET /api/v2/categories/normal/{id}/components/sorting` | — | 200 | Object · `categoryId`, `availableSorts`, `defaultSort`, `categoryType` |
| `GET /api/v2/categories/normal/{id}/structure` | — | 200 | Object · `components`, `categoryId`, `categoryType` |
| `POST /api/v3/search/autocomplete` | JSON `filters`, `limit`, `offset`, `search`, `sort`, `type`, `warehouseId` | 200 | Object · `categoryCount`, `fuzzy`, `hybridSearchUsed`, `mediumRectangleBanners` |
| `GET /api/v3/search/trending` | — | 200 | Object · `trendingSearches` |

### Product data

The app used both a composite product-detail resource and narrower reads for prices, stock, content, ratings and customer-specific product state.

| Method and path | Observed inputs | HTTP | Response shape or selected fields |
| --- | --- | --- | --- |
| `GET /api/v1/products/card` | Query `typedProducts` | 200 | Array · item fields `productId`, `prices`, `stock`, `archived` |
| `GET /api/v1/products/{id}` | — | 200 | Object · `filters`, `adviceForSafeUse`, `archived`, `attachments` |
| `GET /api/v1/products/{id}/ai-summary` | — | 200 | Object · `productId`, `content`, `icon`, `rating` |
| `GET /api/v1/products/{id}/belts` | — | 200 | Array · item fields `itemIds`, `source`, `title`, `type` |
| `GET /api/v1/products/{id}/categories` | — | 200 | Object · `productId`, `categories` |
| `GET /api/v1/products/{id}/detail` | Query `categoryType` | 200 | Object · `productId`, `prices`, `availability`, `filters` |
| `GET /api/v1/products/{id}/detail/content` | — | 200 | Object · `productId`, `adviceForSafeUse`, `attachments`, `composition` |
| `GET /api/v1/products/{id}/price-comparison` | — | 204 | No content |
| `GET /api/v1/products/{id}/prices` | — | 200 | Object · `productId`, `price`, `lastMinuteTitle`, `pricePerUnit` |
| `GET /api/v1/products/{id}/ratings` | — | 200 | Object · `productId`, `ratings` |
| `GET /api/v1/products/{id}/stock` | — | 200 | Object · `productId`, `inStock`, `availabilityDimension`, `billablePackaging` |
| `GET /api/v1/products/{id}/user-data` | — | 200 | Object · `shoppingListIds`, `productId`, `canBeUserRated`, `favorite` |

### Favorites and shopping lists

Favorite-product sections are paginated. The list preview supplies list identifiers and product counts. The per-list response contains products, sections and list settings.

| Method and path | Observed inputs | HTTP | Response shape or selected fields |
| --- | --- | --- | --- |
| `GET /api/v1/components/menu/favorites` | — | 200 | Object · `items`, `componentType`, `subtype` |
| `GET /api/v1/components/product-section/favorites` | Query `page`, `size`, `sort` | 200 | Object · `productIds`, `subCategories`, `pageable`, `categoryType` |
| `GET /api/v1/components/product-section/favorites-in-sale` | Query `page`, `size`, `sort` | 200 | Object · `productIds`, `subCategories`, `pageable`, `categoryType` |
| `GET /api/v1/components/shoppable-display/favorite` | — | 200 | Object · `categoryId`, `categoryType`, `componentType`, `shoppableDisplay` |
| `GET /api/v1/components/sorting/favorites` | — | 200 | Object · `availableSorts`, `defaultSort`, `componentType`, `subtype` |
| `GET /api/v1/favorites/structure` | — | 200 | Object · `components` |
| `POST /api/v1/shopping-lists/cart/all` | JSON `products` | 200 | Object · `data`, `status`, `messages` |
| `GET /api/v1/shopping-lists/from-cart` | — | 200 | Object · `enabled`, `cartChanged`, `shoppingListId` |
| `GET /api/v1/shopping-lists/preview` | — | 200 | Array · item fields `firstThreeProductsIds`, `numberOfProducts`, `status`, `badge` |
| `GET /api/v2/shopping-lists/id/{id}` | — | 200 | Object · `products`, `productSections`, `actions`, `id` |

### Recipes

Recipe cards, screen composition, preparation instructions and ingredient selection were separate reads.

| Method and path | Observed inputs | HTTP | Response shape or selected fields |
| --- | --- | --- | --- |
| `GET /api/v1/chef-recipe/structure/{id}` | — | 200 | Object · `components` |
| `GET /api/v1/chef/{id}/ingredients/selection` | — | 200 | Object · `ingredientGroups`, `metadata`, `presets` |
| `GET /api/v1/components/author-tile/chef-recipe/{id}` | — | 200 | Object · `componentType`, `id`, `image`, `link` |
| `GET /api/v1/components/banners/chef-recipe/{id}` | — | 200 | Object · `banners` |
| `GET /api/v1/components/recipe-header/chef-recipe/{id}` | — | 200 | Object · `author`, `averageRating`, `badges`, `componentType` |
| `GET /api/v1/components/recipe-nutritions/chef-recipe/{id}` | — | 200 | Object · `componentType`, `id`, `portion`, `subtype` |
| `GET /api/v1/components/recipe-preparation/chef-recipe/{id}` | — | 200 | Object · `directions`, `ingredientsSummary`, `componentType`, `id` |
| `GET /api/v1/components/recipes/chef-recipe/{id}` | — | 200 | Object · `componentType`, `id`, `recipesIds`, `showMoreLabel` |
| `GET /api/v1/recipes/card` | Query `recipes` | 200 | Array · item fields `recipeId`, `authorImage`, `authorName`, `averageRating` |
| `GET /api/v2/components/recipe-category-navigator/chef-recipe/` | — | 200 | Object · `categories`, `componentType`, `subtype`, `title` |

### Account and customer services

| Method and path | Observed inputs | HTTP | Response shape or selected fields |
| --- | --- | --- | --- |
| `GET /api/v1/assistant/info` | — | 200 | Object · `features`, `language`, `termsAndConditionsUrl` |
| `GET /api/v1/notifications` | Query `page`, `size` | 200 | Array · item fields `createdAt`, `title`, `text`, `clicked` |
| `GET /api/v1/notifications/stats` | — | 200 | Object · `enabled`, `unseenCount` |
| `GET /api/v1/referrer/profile` | — | 200 | Object · `buttonLabel`, `content`, `title` |
| `GET /api/v1/referrer/thank-you/structure` | — | 200 | Object · `badge`, `buttonText`, `buttonUri`, `description` |
| `GET /api/v1/reusable-bags/user-info` | — | 200 | Object · `progress`, `current`, `deposit`, `editable` |
| `GET /api/v1/user/custom-attributes` | — | 200 | Object · `customAttributes` |
| `GET /api/v2/user/payment-cards` | — | 200 | Array · item fields `brand`, `brandLogo`, `expiry`, `id` |
| `GET /api/v3/user/credits` | — | 200 | Object · `totalCreditsAmount`, `creditItems`, `description` |

### Home, content and presentation

| Method and path | Observed inputs | HTTP | Response shape or selected fields |
| --- | --- | --- | --- |
| `GET /api/v1/bottom-sheet-message` | — | 200 | Object · `data`, `status`, `messages` |
| `GET /api/v1/ftu/banner` | — | 204 | No content |
| `GET /api/v2/tutorial/should-show/mobile-tooltip-sl-cart` | — | 200 | Object · `show`, `tutorial` |
| `GET /api/v2/tutorial/should-show/shopping-list-collaboration-tooltip` | — | 200 | Object · `show`, `tutorial` |
| `GET /api/v2/tutorial/should-show/shopping-lists` | — | 200 | Object · `show`, `tutorial` |
| `GET /api/v3/homepage/components/banner-carousel` | — | 200 | Object · `banners`, `listName`, `priceComparisonBanner` |
| `GET /api/v3/homepage/components/navigation-tabs` | — | 200 | Object · `tabs` |
| `GET /api/v3/homepage/components/navigation-tabs/brands` | — | 200 | Object · `items` |
| `GET /api/v3/homepage/components/navigation-tabs/inspiration` | — | 200 | Object · `items` |
| `GET /api/v3/homepage/components/navigation-tabs/recipes` | — | 200 | Object · `items`, `title` |
| `GET /api/v3/homepage/components/products-slider/local-brands` | — | 200 | Object · `products`, `appUrl`, `categories`, `categoryType` |
| `GET /api/v3/homepage/components/products-slider/price-hits` | — | 200 | Object · `products`, `appUrl`, `categories`, `categoryType` |
| `GET /api/v3/homepage/components/products-slider/{id}` | — | 200 | Object · `products`, `categoryId`, `appUrl`, `categories` |
| `GET /api/v3/homepage/components/trade-banners/` | — | 200 | Object · `mediumRectangleBanners` |
| `GET /api/v3/placeholders` | — | 200 | Object · `addressBenefits`, `cart`, `orderReview`, `reusablePackaging` |
| `GET /api/v4/homepage/components/navigation-tabs/categories` | — | 200 | Object · `items`, `hotLinks` |
| `GET /api/v4/navigation/components/navigation-tabs` | — | 200 | Object · `tabs` |
| `GET /api/v4/placeholders` | — | 200 | Object · `addressBenefits`, `cart`, `orderReview`, `reusablePackaging` |
| `GET /api/v5/navigation/components/navigation-tabs/categories` | — | 200 | Object · `items`, `hotLinks`, `title` |
| `GET /api/v8/homepage` | — | 200 | Array · item fields `id`, `type` |

### Other observed services

| Method and path | Observed inputs | HTTP | Response shape or selected fields |
| --- | --- | --- | --- |
| `GET /api/v6/config` | — | 200 | Object · `data`, `status` |

### Session and app services

These calls appeared alongside the customer-facing screens. No credentials, identifiers or event payloads are reproduced.

| Method and path | Observed inputs | HTTP | Response shape or selected fields |
| --- | --- | --- | --- |
| `POST /api/galileo/v1` | — | 200 | Metadata only |
| `POST /api/v1/affiliate` | — | 204 | No content |
| `POST /api/v1/push/token` | — | 200 | Metadata only |
| `POST /api/v5/login/access_token` | — | 200 | Metadata only |
| `POST /services/frontend-service/metric/click/batch` | — | 200 | Metadata only |
| `POST /services/frontend-service/metric/show/batch` | — | 200 | Metadata only |
