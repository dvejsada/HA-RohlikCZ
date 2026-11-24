# HA-RohlikCZ: use rohlik-api 0.4.0 delivery tracking — Implementation Plan

**Revision 2.** Incorporates the independent review of revision 1: blockers B1–B2, important points 1–10
and the minors. Open questions 1–5 have proposed answers below, pending the owner's confirmation.

**Goal:** make the delivery entities reliable and locale-independent by reading the courier's live
tracking (status, ETA, stops before your order) from `rohlik-api` 0.4.0, and give automations a safe
"the courier is here" signal (discussion #105).

**Inputs:**
- `docs/plans/2026-11-24-delivery-tracking-observations.md` (one complete live delivery, early delivery
  requested, so the slot cap was never observed live)
- `rohlik-api` 0.4.0: `orders.get_delivery_tracking(order_id, raise_on_error=True)`,
  `UpcomingOrder.list_from_api()`, `NextDelivery.resolve()`, `TrackingStatus`

**Prerequisite:** `rohlik-api` 0.4.0 published on PyPI. CI installs it via `requirements_test.txt`.

**Scope of 1.3.0:**
- tracking-based ETA
- Delivery Status and Stops Before Delivery sensors
- a latched Courier at the Door binary sensor
- a latched Last Mile
- privacy fixes

**Cut, possible later:**
- the courier `device_tracker` (map)
- the courier name
- an `eta_capped` entity attribute (diagnostics only)

---

## Why (from the observations)

- **ETA:** tracking had a timestamped courier ETA 3 h 13 min before delivery, within 3.5 min of the
  actual arrival. The announcement showed the slot start until early delivery was accepted, and is Czech text.
- **Lifetime:** the announcement disappears about 45 s after arrival. Tracking stays readable through `DELIVERED`.
- **Status:** tracking has the status (`IN_QUEUE → ON_THE_WAY → ARRIVED → HANDING_OVER → DELIVERED`) and the
  stops before your order. `ARRIVED` matched the app's "Přijeli jsme".
- **Short phases:** `ARRIVED` lasted about 2 min and `HANDING_OVER` about 2 min. Polling must be at most 60 s near the end to see them.
- **ETA jitter:** the ETA changed in 55 of 185 queued polls, but crossed a whole minute only twice. While the
  courier was on the way it jumped 12:43 → 12:46. That makes a plain "ETA − lead" window flap: with the 5-min
  default, Last Mile went on at 12:38, off at 12:40 and on again at 12:41.

---

## Design

### 1. Order selection and IDs (one place)

- `hub.soonest_upcoming(data) -> UpcomingOrder | None` uses `UpcomingOrder.list_from_api(data["next_order"])[0]`.
- `eta.py` is switched from `get_earliest_order` to this result, so the tracker and the hub always agree
  on which order is soonest.
- Order IDs are compared as `int` everywhere. The tracker's stored `last_live_order_id` (string) is
  converted on load.
- Behaviour change: orders without a slot now sort last instead of being skipped. This affects the ETA
  only when every order lacks a slot, which was never observed.

### 2. Fetching (`hub.py`)

State on the hub, **outside `self.data`**:

```python
@dataclass
class TrackedOrder:
    order_id: int
    tracking: DeliveryTracking
    fetched_at: datetime         # last successful fetch
    not_upcoming_since: datetime | None   # set when the order left the upcoming list
```

The hub holds `self._tracked: TrackedOrder | None` and `self._no_tracking: set[int]`, the order IDs that
answered 404.

`async _async_refresh_tracking(data: dict) -> bool` receives the **fresh** `data` (B1) and returns
"changed" (defined in §2.4).

1. **Upcoming failed.** If `data.get("next_order") is None`, leave everything unchanged and return False.
2. **Pick the order.** `soonest = soonest_upcoming(data)`, which may be `None` (no `orders[0]` on an empty list).
3. **Tracked order left the list.** Set `not_upcoming_since` once.
   - If the last status was not `DELIVERED`, fetch that ID again, at most **3 attempts**, one per refresh.
   - Expire when **any** of these holds:
     - `DELIVERED` held for `DELIVERED_HOLD` (30 min)
     - 30 min passed since it left the list, whatever the status (cancelled, final fetch failed, stuck in `ARRIVED`)
     - another order is within the lookahead
4. **Gate.** Fetch for `soonest` only if `soonest.slot_since − now ≤ TRACKING_LOOKAHEAD` (4 h) or the slot is
   running or overdue, and `soonest.id not in _no_tracking`.
5. **Fetch.** `get_delivery_tracking(id, raise_on_error=True)`:
   - **Tracking returned:** store it and update `fetched_at`.
   - **`None` (404):** add the ID to `_no_tracking`, so a shop without tracking is asked once per order
     rather than every minute. Clear any tracked state for that order.
   - **`RohlikAPIError` (including `APIRequestFailedError`):** keep the previous tracking for the same order
     and log at debug. The coordinator update does not fail.
6. **Maximum age.** Kept tracking older than `TRACKING_MAX_AGE` (15 min) without a successful fetch counts
   as unknown: status `None`, and the ETA falls back to the announcement or slot.
7. **Delivered.** On a transition to `DELIVERED`, call `self.async_request_refresh()` (debounced by HA), so
   the upcoming list and the ETA catch up straight away instead of up to 10 min later.

**Call sites:**
- **`_async_update_data`:** calls `await self._async_refresh_tracking(data)` with the local `data` before returning it.
- **`refresh_delivery_times()`** (the poll and the service):
  - Fetch tracking; if a tracked ETA exists, **skip the announcement request** (keeps the request budget roughly unchanged).
  - Otherwise fetch announcements as today.
  - If both are needed they run concurrently with `asyncio.gather`.
  - Notify once, only if something changed.

**Change detection (§2.4)** compares:
- status
- ETA truncated to the **minute**
- `stops_before`
- `early_delivery_requested`
- `is_delivered`

It never compares `generated_at` or seconds-level ETA jitter.

**Poll window (`_schedule_eta_poll`)**, applied when auto-refresh is on:
- **Anchor:** the window is based on the **uncapped** courier estimate when there is one, otherwise on the
  resolved ETA, so a capped ETA doesn't delay polling.
- **Window:** ETA−30 min … ETA+10 min, as today.
- **Late stage:** also poll while `stops_before ≤ 1` or the status is `ON_THE_WAY` / `ARRIVED` /
  `HANDING_OVER`, at `min(configured interval, 60 s)` so the 2-minute phases are seen. This is capped at
  the estimate + `MAX_OVERDUE` (1 h) and also at `TRACKING_MAX_AGE` of failed fetches, so polling never runs
  forever.
- **Stop:** at `DELIVERED`, or when the tracked order expires.

**Auto-refresh off:** status changes are seen only on the 10-minute refresh or the `update_delivery_times`
service. The README says so.

### 3. ETA resolution (`eta.py`)

- `EtaSource = Literal["tracking", "announcement", "slot", "last_known"]`.
- `update(data, tracked)` receives the soonest order (from §1) and the hub's tracking for it.
- **Priority 1:** tracking for the soonest order with an estimate that is **not** `DELIVERED` and not past
  max age gives `NextDelivery.resolve(order, tracking)`. That handles the cap (slot or early-delivery start
  while queued). Recorded as `last_live_value` / `last_live_order_id` with `last_live_source="tracking"`.
- **Priorities 2–4:** unchanged (announcement → slot → last_known). They cover no tracking, no estimate yet,
  404 shops and failures.
- **`resolve` raising `ValueError`** (the tracking belongs to another order) is treated as no tracking; this
  never happens with the shared selection from §1.
- `is_live` becomes `source in ("tracking", "announcement")`.
- Persistence: `last_live_source` is added, and old stores default to `"announcement"`. The store version stays 1.
- The stale timer, `seed_legacy` and restore are unchanged. Tracking itself is not persisted: after an HA
  restart mid-delivery the kept live ETA bridges the time until the first refresh. This is documented.
- `eta_capped`, `tracking_status` and `stops_before` live on the hub (`hub.tracking_state`), not on the tracker.

### 4. Entities

**Availability.** Every entity below stays available while the hub holds valid tracking (not expired),
using the same override pattern as Last Mile (`binary_sensor.py:63-66`). So a failed refresh, a reload or a
restart doesn't go through `unavailable` and re-fire triggers.

**Latching.** A per-order latch on the hub records "the last mile started" and "the courier arrived". It is
reset only by `DELIVERED`, by the order expiring (§2.3) or by `MAX_OVERDUE` after the estimate. It is also
persisted in the ETA store, so a restart keeps it.

| Entity | Type | State | Notes |
| --- | --- | --- | --- |
| **Delivery Status** (new) | `sensor`, `device_class=ENUM`, `_attr_options` = the 5 states | `in_queue`, `on_the_way`, `arrived`, `handing_over`, `delivered`; `None` without valid tracking | Attributes: `order_id`, `updated` (`generated_at`). An unknown API value gives `None` and a single debug log per value. State names are translated (`entity.sensor.delivery_status.state.*`) |
| **Stops Before Delivery** (new) | `sensor`, integer, no unit, no state class | `stops_before` | `0` means you are next |
| **Courier at the Door** (new) | `binary_sensor`, `device_class=PRESENCE` | **On** from the first `ARRIVED`/`HANDING_OVER` until `DELIVERED`, expiry or the hard timeout (latched) | The recommended #105 trigger, availability-guarded |
| **Delivery Last Mile** (changed) | `binary_sensor` | Turns on as today (estimate − lead, live ETA only) **or** at Courier at the Door. **Latched:** once on, it stays on until `DELIVERED`, expiry or the existing hard timeout | Fixes the observed flapping. Uses the uncapped estimate when on the way |
| **Delivery Time / Time Remaining** | unchanged | `source` may be `tracking` | No new attributes |

**Shipped automation example** `automations/courier_arrived.yaml` triggers on Courier at the Door `to: "on"`
with `not_from: ["unavailable", "unknown"]`.

### 5. Privacy

- **Delivery Information:** its **Additional Content** attribute has phone numbers and `tel:` links stripped
  (`+?\d[\d\s]{7,}` and the anchor) before publishing, so they never reach the recorder. Example: "{courier} už
  Vám veze celkem 8 tašek. A kdyby něco, je na čísle …" keeps the bag count but loses the number. The courier
  name in that text is left as the shop sends it; the plan documents this.
- **Diagnostics `TO_REDACT`** additions: `latitude`, `longitude`, `displayName`, `phoneNumber`, `fullAddress`,
  `display`, `postalCode`, `additionalContent`.
- **Diagnostics `delivery_tracking` block:** order ID, status, the estimate (raw and resolved), `eta_capped`,
  `stops_before`, `generated_at`, `fetched_at`, `not_upcoming_since`, early-delivery flags, `_no_tracking` size.
  No courier name or position.
- No new entity exposes the courier name or position.

### 6. Options

No new option. The auto-refresh description is reworded: it also polls while the courier is on the way and
until delivery, at most every 60 s in the final stage.

### 7. Translations (en, cs, de)

- names and states for `delivery_status`
- names for `stops_before` and `courier_at_door`
- the reworded auto-refresh description

### 8. Dependencies, version, docs

- `manifest.json` and `requirements_test.txt`: `rohlik-api==0.4.0`. Version **1.3.0**.
- **Readme:**
  - the new entities and the automation example
  - the updated Delivery Time and Last Mile descriptions, including the latch
  - polling behaviour, and that auto-refresh off means 10-min granularity
  - tracking is unofficial and verified on Rohlík.cz only; other shops fall back to announcements
  - tracking isn't persisted across restarts

---

## Tests

- **`conftest.py`:** an autouse patch for `OrderService.get_delivery_tracking` returning `None`.
- **Fixtures:** `DeliveryTracking.from_api(...)` built from the library's placeholder values, in `fixtures_data.py`.
- **Hub:**
  - the first refresh (with `self.data` still `None`) uses the fresh data
  - empty `next_order` and `next_order` None (unchanged state)
  - lookahead gating
  - a 404 is cached per order and not retried
  - a failure keeps the previous value, and kept tracking expires after the max age
  - a `RohlikAPIError` subclass and a `ValueError` from `resolve` are both handled
  - int vs string IDs
  - the soonest order agrees between the hub and the tracker
- **Order leaves the list:** a final fetch returning `DELIVERED`, `ARRIVED`, a failure and a 404 (each
  expires within 30 min); the `DELIVERED_HOLD` expiry; a second order entering the lookahead ends the hold.
- **Delivered:** the transition triggers `async_request_refresh`.
- **Poll:**
  - the anchor uses the uncapped estimate
  - `≤ 60 s` once `stops_before ≤ 1`
  - polling continues past ETA+10 min while on the way or arrived
  - the cap at estimate + `MAX_OVERDUE`
  - polling stops at `DELIVERED`
  - the announcement request is skipped while a tracked ETA exists
  - the two requests run concurrently otherwise
- **Change detection:** second-level ETA jitter and `generated_at` produce **no** listener call; a minute
  change does. Count the listener calls per poll.
- **ETA:** tracking beats the announcement; the cap applies while queued and not early; no cap on the way;
  `DELIVERED` tracking is ignored; fallbacks work; a restored store without `last_live_source`.
- **Entities:**
  - Delivery Status mapping, unknown value and translated options
  - Stops sensor values
  - **replay of the observed ETA sequence:** Last Mile turns on once and stays on (no flap)
  - Courier at the Door latches
  - availability stays on through a failed refresh while `arrived`
  - a restart mid-delivery keeps the latch
  - unload during an in-flight tracking fetch
- **Privacy:** the Delivery Information phone strip; diagnostics redaction (no name, position, phone or address).

---

## Steps

1. Wait for `rohlik-api` 0.4.0 on PyPI and bump the pins. The existing suite must still pass.
2. Shared order selection and ID normalisation, plus tests.
3. Hub fetching, gating, 404 cache, expiry and refresh-on-delivered, plus tests.
4. Poll window and the announcement skip, plus tests.
5. `eta.py` tracking source, plus tests.
6. Latches, Delivery Status, Stops, Courier at the Door, the Last Mile change, translations, plus tests.
7. Privacy: the phone strip and diagnostics, plus tests.
8. Readme, the automation example, the version bump; then a review, a PR, CI, and a live check during a real
   delivery, ideally **without** early delivery so the cap is exercised.

## Proposed answers to the open questions (pending confirmation)

| # | Question | Proposal |
| --- | --- | --- |
| 1 | `DELIVERED_HOLD` | 30 min, ended early when another order enters the lookahead |
| 2 | Separate courier binary sensor | **Yes**, "Courier at the Door", latched and availability-guarded |
| 3 | Courier name attribute | **No** |
| 4 | Courier map in 1.3.0 | **Deferred**. If added later, as a disabled-by-default entity rather than an option |
| 5 | Lookahead | 4 h at the 10-min cadence |
