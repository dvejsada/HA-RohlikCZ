# Delivery tracking — live observations (2026-11-24)

One complete Rohlík.cz delivery (standard 15-minute slot 13:00–13:15 CET, early delivery requested
by the customer at 12:00), monitored from order placement to 20 minutes after handover. The
monitor used the `rohlik-api` 0.3.0 web-login cookie session, with no mobile token or headers.
No order, address, courier or location data is reproduced here. All times are CET.

## What was polled

| Source | Cadence |
| --- | --- |
| `GET /api/v2/orders/{id}/delivery-tracking` | 15 min, 60 s from 12:00 or once a status existed, 30 s while `ON_THE_WAY`/`ARRIVED`/`HANDING_OVER` |
| `GET /services/customer-events` (SSE) | kept open; auto-reconnect |
| `frontend-service/announcements/delivery`, `/api/v1/announcements/delivery`, `/api/v3/homepage/components/order-tracking/`, `/api/v2/homepage/components/announcement/` | 15 min, then 60 s |
| `/api/v3/orders/upcoming`, `/api/v3/orders/{id}`, `/api/v4/orders/{id}` | every 5th cycle of the above |

## Timeline

| Time | Tracking (`progress`) | Announcement | Home card |
| --- | --- | --- | --- |
| 07:01 | order placed | — | — |
| 08:06 | `progress: null`; `earlyDelivery.available: true` | "Tašky už se začínají plnit… zhruba v 13:15" | "Už na tom makáme!", "od 13:00", "Doobjednat už nejde." |
| 08:36 | null | same, now "v 13:00" | — |
| **09:04** | **`IN_QUEUE`, `stopsBefore: 11`, `estimatedArrival: null`** (first seen in SSE; poll ran every 15 min) | — | — |
| ~09:30 | — | **"Nákup už frčí k Vám!"** "za 209 minut, tedy v 13:00"; `additionalContent` names courier and **"celkem 8 tašek"** | — |
| **09:32** | **ETA 12:49:32** appears (still `IN_QUEUE`, 11 stops) | still says 13:00 (slot start) | still "od 13:00" |
| 10:27 → 12:30 | stops 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, about every 7–20 min; ETA stays 12:49 ± 40 s | 13:00 | "od 13:00" |
| **12:00:10–12:00:55** | customer requests early delivery → `earlyDelivery {requested: true, allowedFrom: slot.since − 1 h}`; ETA unchanged 12:49:37 | 13:00 → **12:50** | → "za 48 min, v 12:49", "3 zastávky před vámi" |
| 12:32–12:35 | 1 stop; ETA 12:46 → 12:48:39 | follows ETA (rounded to nearest minute) | follows ETA (truncated to the minute) |
| 12:36:27 | `IN_QUEUE`, 1 stop | **relative only**: "za 8 minut"; `additionalContent` adds **courier phone number** | — |
| **12:37:50** | **`ON_THE_WAY`**, 0 stops, ETA 12:45:22; `courierPosition` + `routeToStop` populated | — | "za 4 min", **"Kurýr je blízko"** (no clock time) |
| 12:40:20 | ETA 12:46:18; `courierMovement` populated | — | "za 5 min" … "za 1 min" (counts down every minute) |
| **12:45:51** | **`ARRIVED`** (27 s before ETA); `routeToStop` null. App showed **"Přijeli jsme"** (confirmed by customer) | — | — |
| **12:46:35** | `ARRIVED` | **all announcements empty** (`available: false` / `[]`) | **HTTP 204** |
| **12:47:29** | **`HANDING_OVER`**; `expectedUpdateIntervalSeconds: 5` | empty | 204 |
| **12:49:36** | **`DELIVERED`**; `stopsBefore: null`, position null; ETA kept (12:46:18) | empty | 204 |
| 12:49:37–38 | — | upcoming list `[]`; order v3/v4 `state: DELIVERED` | — |
| 12:49:56 | `earlyDelivery.available: false` | | |
| 12:50–13:10 | still 200; snapshot **frozen** (`generatedAt` stays 12:49:36); SSE re-sends the `DELIVERED` snapshot on every reconnect | empty | 204 |

## Findings

### Tracking endpoint (`/api/v2/orders/{id}/delivery-tracking`)

- **Works with the library's web-login session.** It returned 200 throughout. An unknown ID returns 404 with the usual `messages` envelope.
- **Lifecycle:** `progress: null` → `IN_QUEUE` → `ON_THE_WAY` → `ARRIVED` → `HANDING_OVER` → `DELIVERED`. No other values were seen.
- **ETA:** available **3 h 13 min before delivery**, while the order was still queued with 11 stops ahead. That was 2.5 h before the announcement or card showed it. Over the 3 h it was off by **≤ 3.5 min** (first 12:49:32, actual arrival 12:45:51). It moves in both directions near the end (12:49 → 12:46 → 12:48 → 12:43 → 12:46).
- **After delivery:** the ETA is kept and never cleared (stays at the last value).
- **`stopsBefore`:** integer from the start of `IN_QUEUE`, `null` after `DELIVERED`. Ten decrements over 3 h; each was pushed by SSE within a second.
- **Courier position:** `courierPosition{latitude, longitude, recordedAt}` and `routeToStop{encodedPolyline, routeCalculationId}` from `ON_THE_WAY`. `courierMovement{encodedPolyline, fromRecordedAt, toRecordedAt, snapped}` follows ~2.5 min later. Position stays through `HANDING_OVER` and is null at `DELIVERED`. `courierLocationStale` was always `false`.
- **Position cadence:** 21 distinct positions; mostly **25–30 s** apart while driving, irregular near arrival (1–72 s).
- **Freshness:** every poll returns a freshly generated snapshot (`generatedAt` ≈ request time, sometimes up to 40 s old). Small ETA drifts appear only in polls, not in SSE (below).
- **`earlyDelivery`:** `{available, requested, allowedFrom}`. `allowedFrom` = `deliverySlot.since − 1 h` once requested. `available` turns false at delivery. Not part of the SSE payload.
- **Top level:** also carries `destination{latitude, longitude}` (the customer's address) and `courier{displayName, phoneNumber, photoUrl}` from the start. Both are personal data to redact.

### SSE stream (`/services/customer-events`)

- **Format:** `text/event-stream`; frames `event:<TYPE>` + `data:{type, timestamp, eventData}`. Types seen: `PING`, `ORDER_DELIVERY_PROGRESS_CHANGED`.
- **`eventData`:** the tracking `progress` fields plus `dataType: "ORDER_DELIVERY_PROGRESS"`, `orderId`, `expectedUpdateIntervalSeconds` (30; **5 during `HANDING_OVER`**). No `earlyDelivery`, `deliverySlot`, `courier` or `destination`.
- **Connections:** each one closed cleanly after **~90–95 s** (186 reconnects). It is unknown whether Rohlík or this environment's proxy closes it. On every (re)connect the server sends a `PING` and **the current progress snapshot** for an active order.
- **Pushes:** only when stops, status or position change, or the ETA moves materially; minor ETA drift is not pushed. It was **3–30 s ahead of the 30–60 s polls** for every status change.
- **Before the first status:** nothing is pushed while `progress` is null (08:06–09:04: only `PING`s).

### Announcements and home card

- **Before early delivery:** the announcement shows the **slot start (13:00)** even while tracking knows 12:49. After the customer accepted early delivery, it switched to the tracking ETA within a minute. The home card showed "od 13:00" until then. *Single observation: the early-delivery request, the 1-hour mark and the 4 → 3 stops change all fell within the same minute.*
- **Rounding:** the announcement rounds the ETA to the nearest minute (flips 12:49 ↔ 12:50 for an ETA of ~12:49:30). The card truncates.
- **Under ~10 min:** both switch to **relative text only** ("za 8 minut", "za 4 min", "Kurýr je blízko").
- **Phone number:** `additionalContent` adds the **courier's phone number** in the final minutes (HA currently exposes `additionalContent` as an attribute).
- **Bags:** the bag count appears only as text ("celkem 8 tašek") in `additionalContent`, from ~09:30.
- **After arrival:** **all announcement sources go empty ~45 s after `ARRIVED`**, and the card returns **204**. This is the "announcement disappears before delivery" case the HA ETA tracker currently works around.
- **Two endpoints:** `frontend-service/announcements/delivery` and `/api/v1/announcements/delivery` carried the same text throughout. v1 HTML-escapes the `<span>` tags (`&lt;span&gt;`).

### Orders

- **`/api/v3/orders/{id}` and `/api/v4/orders/{id}`:** returned **identical bodies** at every sample. `state`: `UPCOMING` → `DELIVERED` (1–2 s after tracking `DELIVERED`); no intermediate states.
- **`/api/v3/orders/upcoming`:** dropped the order **within 1 s of `DELIVERED`**.
- **`itemsCount` (17) vs `itemsTotalQuantity` (29):** both present in upcoming; the bag count (8) is not exposed as a number anywhere.

## Implications for the plan

1. **Tracking ETA is better than the announcement.** It is available hours earlier, timestamped, locale-free, and doesn't vanish at arrival. **Cap it at `deliverySlot.since` unless `earlyDelivery.requested`**, to match what the shop promises.
2. **`ARRIVED` is the arrival trigger.** It matched the app's "Přijeli jsme" and came 27 s before the ETA. `DELIVERED` follows ~4 min later in this case.
3. **Polling cadence:** 60 s polling misses nothing important. Status changes lag by at most one interval. SSE is optional and gives sub-second updates. Its value is mainly the map position (25–30 s), and it needs a reconnect loop because connections are short-lived (verify outside this environment).
4. **Redact:** `destination`, `courier.*`, `courierPosition`, the polylines, and the phone in announcement `additionalContent`.
5. **Order detail:** v3 and v4 are the same payload today, so moving `get_detail` to v4 is low-risk.
