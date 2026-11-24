# HA-RohlikCZ: use rohlik-api 0.4.0 delivery tracking — Implementation Plan

**Goal:** make the delivery entities reliable and locale-independent by reading the courier's live
tracking (status, ETA, stops before your order, position) from `rohlik-api` 0.4.0, and give automations
a real "the courier is here" signal (discussion #105).

**Inputs:**
- `docs/plans/2026-11-24-delivery-tracking-observations.md` (one complete live delivery)
- `rohlik_api_python` PR #13 / v0.4.0: `orders.get_delivery_tracking(order_id, raise_on_error=True)`,
  `UpcomingOrder.list_from_api()`, `NextDelivery.resolve()`, `TrackingStatus`

**Prerequisite:** `rohlik-api` 0.4.0 must be published on PyPI before this can ship. CI installs it via
`requirements_test.txt`.

---

## Why (what the observations showed)

| Today (announcement text) | With tracking |
| --- | --- |
| ETA available ~3 h 15 min before delivery? **No.** The text shows the slot start until early delivery is accepted | ETA from 3 h 13 min before delivery; it was within 3.5 min of the actual arrival |
| ETA is Czech text. "za 8 minut" countdowns are parsed only in Czech, other shops get clock times only | A UTC timestamp, the same in every shop |
| The announcement **disappears ~45 s after arrival**, so `eta.py` keeps the last value | Tracking stays readable through `DELIVERED` |
| No status, no stops before your order | `IN_QUEUE → ON_THE_WAY → ARRIVED → HANDING_OVER → DELIVERED`, plus stops before your order (11 → 0) |
| Last Mile is time-based only | `ARRIVED` matched the app's "Přijeli jsme" exactly |
| `additionalContent` attribute carries the courier's phone number near the end | Tracking model has no phone |

---

## Design

### 1. Fetching (`hub.py`)

New coordinator state: `self._tracking: DeliveryTracking | None`, `self._tracking_order_id: int | None`
and `self._tracking_fetched_at`. Tracking is kept **outside** `self.data`, so diagnostics and other data
consumers don't get a typed model mixed into raw JSON.

`async _async_refresh_tracking() -> bool` returns True if anything relevant changed:
1. `raw = self.data.get("next_order")`. If it is **None** (the upcoming request failed), keep the
   previous state and return.
2. `orders = UpcomingOrder.list_from_api(raw)`.
3. **Order left the list.** If the tracked order is no longer in `orders` and its last status was active,
   do **one** final fetch for that ID to capture `DELIVERED` (the order drops out of the upcoming list
   within about 1 s of delivery). Keep the `DELIVERED` tracking for `DELIVERED_HOLD` (proposed 30 min),
   then clear it.
4. **Gate:** with `soonest = orders[0]`, fetch only if `soonest.slot_since − now ≤ TRACKING_LOOKAHEAD`
   (proposed **4 h**; the queue started ~4 h before the slot and the ETA ~3 h before), or if the slot is
   already running or overdue. Orders days ahead cost nothing.
5. Call `get_delivery_tracking(soonest.id, raise_on_error=True)`:
   - `APIRequestFailedError`: keep the previous tracking **for the same order**, log at debug, and don't
     fail the coordinator update.
   - `None` (404, the shop has no tracking): clear the tracking. Entities fall back to the announcement path.
   - A tracking object: store it.

Call sites:
- end of `_async_update_data` (every 10 min)
- `refresh_delivery_times()`, used by the auto-ETA poll and the `update_delivery_times` service. It
  fetches tracking **and** announcements and notifies once if either changed. Tracking changes are
  compared on `(status, estimated_arrival, stops_before, courier_position, early_delivery_requested)`,
  not on `generated_at`.

**Poll window (`_schedule_eta_poll`).**
- Unchanged: ETA−30 min … ETA+10 min.
- New: also poll at the configured interval while the status is `ON_THE_WAY`, `ARRIVED` or
  `HANDING_OVER`, even past ETA+10 min, because an ETA in the past is not an arrival. Stop at `DELIVERED`.
- No change while `IN_QUEUE`: stops are picked up by the 10-min refresh.

**Request budget.** For the observed delivery, a 4 h lookahead × 1 tracking request every 10 min is about
24 extra requests. The poll window (about 40 min at 60 s) is about 40 tracking requests on top of today's 40
announcement requests. Orders not due within 4 h make no extra requests.

### 2. ETA resolution (`eta.py`)

- `EtaSource = Literal["tracking", "announcement", "slot", "last_known"]`.
- `DeliveryEtaTracker.update(data, tracking)` takes the hub's tracking.
- **Priority 1:** if there is tracking for the soonest order with an ETA, use
  `NextDelivery.resolve(order, tracking)`. The library applies the "not before the slot / early-delivery
  start while queued" rule. Record it as `last_live_value` and `last_live_order_id`, with source `"tracking"`.
- **Priorities 2–4:** the existing announcement, slot and last_known paths, unchanged. They cover shops
  without tracking, the time before an ETA exists, and failures.
- `is_live` becomes `source in ("tracking", "announcement")`. Persistence is unchanged, plus
  `last_live_source` (old stores default to `"announcement"`). Store version stays 1, since the change is additive.
- New properties for entities: `tracking_status`, `stops_before`, `has_arrived`, `is_delivered`
  (from the hub's tracking for the soonest or just-delivered order).
- Diagnostics: add `tracking_status`, `tracking_generated_at`, `eta_capped`.

**Effect:** Delivery Time, Delivery Time Remaining and Delivery Last Mile get a locale-independent ETA.
The `source` attribute shows `tracking`.

### 3. Entities

| Entity | Type | State / attributes | Notes |
| --- | --- | --- | --- |
| **Delivery Status** (new) | `sensor`, `device_class=ENUM` | `in_queue`, `on_the_way`, `arrived`, `handing_over`, `delivered`; `None` without tracking. Attrs: `order_id`, `updated` (`generated_at`), `courier` (first name) | The #105 trigger: `to: arrived`. An unknown API status gives `None` plus one debug log. `delivered` is held for `DELIVERED_HOLD` |
| **Stops Before Delivery** (new) | `sensor`, integer, no unit, no state class | `stops_before` | `0` = you are next; `None` once delivered or without tracking |
| **Delivery Last Mile** (changed) | `binary_sensor` | Additionally **on** while `has_arrived`; **off** immediately at `DELIVERED`. The time window is unchanged otherwise | Keeps the "available while live" rule, so automations don't fire twice |
| **Delivery Time**, **Delivery Time Remaining** | `sensor` | Unchanged entities; `source` can be `tracking`. Add attribute `eta_capped` | No breaking change |
| **Courier** (new, opt-in) | `device_tracker` (`TrackerEntity`, GPS) | lat/lon from `courier_position`. Attrs: `recorded_at`, `stale` | Created only with option `track_courier`. Unavailable without a position. Adds `device_tracker` to `PLATFORMS` |

All new entities use `_attr_should_poll = False`, a `translation_key`, and the existing unique-ID scheme
`{account}_{translation_key}`.

### 4. Options (`const.py`, `config_flow.py`)

- `CONF_TRACK_COURIER` ("Show the courier on the map"), default **off**. Changing it reloads the entry.
- The auto-refresh description is reworded: it also refreshes the courier's status while the courier is on the way.

### 5. Privacy and diagnostics

- **`TO_REDACT` additions:** `latitude`, `longitude`, `displayName`, `phoneNumber`, `fullAddress`,
  `display`, `postalCode`, `additionalContent` (the latter carries the courier's name and phone number near
  the end of a delivery).
- **Diagnostics `delivery_tracking` block:** `order_id`, `status`, `estimated_arrival`, `stops_before`,
  `generated_at`, early-delivery flags and `fetched_at`. **No** courier name or position.
- **Courier position** is exposed only with the opt-in option. The courier name appears only as a Delivery
  Status attribute; the same name is already visible in the announcement text.

### 6. Translations (en, cs, de)

- Entity names: `delivery_status`, `stops_before`, `courier`.
- ENUM state names for `delivery_status`.
- `track_courier` option label and description.
- The reworded auto-refresh description.

`tests/test_translations.py` checks that the key sets match.

### 7. Dependencies, version, docs

- `manifest.json`: `rohlik-api==0.4.0`, version **1.3.0**. `requirements_test.txt`: `rohlik-api==0.4.0`.
- **Readme:**
  - new entities and the option
  - an "arrived" automation example (`automations/courier_arrived.yaml`)
  - updated descriptions of Delivery Time and Last Mile
  - a note that tracking is unofficial and verified on Rohlík.cz only; other shops fall back to announcements

---

## Tests (`pytest-homeassistant-custom-component`)

- **`conftest.py`:** an autouse patch for `OrderService.get_delivery_tracking` (`None`), like the existing
  announcements patch, so no test reaches the network.
- **Fixtures:** reuse the library's hand-written values (placeholder IDs, coordinates and name), built as
  `DeliveryTracking.from_api(...)` objects in `fixtures_data.py`.
- **`test_delivery_eta.py` additions:**
  - tracking beats the announcement
  - capped while queued and not early (ETA = slot start, `eta_capped`)
  - not capped once on the way
  - tracking without an ETA falls back to the announcement, then the slot
  - tracking for another order is ignored
  - a restored store without `last_live_source`
- **New `test_delivery_tracking.py`:**
  - no request for an order due more than 4 h out
  - a request within the lookahead
  - a failure keeps the previous value for the same order
  - a 404 clears it
  - a final fetch after the order leaves the upcoming list captures `delivered`, which is held for `DELIVERED_HOLD` and then cleared
  - `next_order` None (upcoming failed) keeps state
  - change detection ignores `generated_at`
  - the poll continues while `ON_THE_WAY`/`ARRIVED` past ETA+10 min and stops at `DELIVERED`
  - the Delivery Status enum mapping and the unknown-status case
  - Stops sensor values
  - Last Mile on for `ARRIVED` and off for `DELIVERED`
  - the Courier tracker exists only with the option and is unavailable without a position
- **`test_diagnostics.py`:** none of the redacted keys or the courier name leak.
- The existing suites keep passing unchanged (announcement-only behaviour when tracking is `None`).

## Steps

1. Wait for `rohlik-api` 0.4.0 on PyPI, then bump the pins. Run the existing suite to confirm nothing changed.
2. Hub fetching, gating and the final-fetch / hold logic, plus tests.
3. `eta.py` tracking source, plus tests.
4. Delivery Status and Stops sensors, and the Last Mile change, plus translations and tests.
5. The Courier `device_tracker` and the option, plus tests.
6. Diagnostics redaction, plus tests.
7. Readme, the automation example, the version bump; then a review, a PR, CI, and a live check during a
   real delivery before release.

## Open questions

1. **`DELIVERED_HOLD`:** how long should Delivery Status show `delivered` after the order leaves the list?
   30 min, 1 h (matching `MAX_OVERDUE`), or until the next order is placed?
2. **Courier binary sensor:** add a separate binary sensor "Courier at the door" (on for
   `ARRIVED`/`HANDING_OVER`), or are the Delivery Status enum plus Last Mile enough?
3. **Courier name:** expose the first name as an attribute, or leave it out?
4. **Courier map:** ship the `device_tracker` in 1.3.0, or leave it for later?
5. **Lookahead:** is 4 h acceptable as the request budget, or should tracking be fetched only inside the
   ETA poll window (cheaper, but the ETA appears later)?
