# Delivery countdown + "Last mile" entities — plan

Source: GitHub discussion request (@xdanik's AWTRIX setup) asking for a built-in
remaining-minutes sensor and an ETA-based "Last mile" binary sensor.

## Decision: integration only, no API change

`rohlik_api_python` only exposes `delivery.get_announcements()` returning raw
announcement JSON. Everything that turns it into an ETA — HTML/text parsing
(`utils.extract_delivery_datetime`), placing the clock time into the booked slot,
matching the announcement to the soonest order, preserving the last live ETA after
Rohlík clears the announcement, and restoring it after restart — already lives in
the integration (`sensor.DeliveryTime`). Countdown/last-mile are pure derivations
of that ETA plus wall clock, i.e. HA presentation concerns. No new endpoint or
data is needed, so the library stays untouched.

## Step 1 — extract a single ETA source of truth (refactor, no behaviour change)

Today the ETA is computed inside `DeliveryTime.native_value`, which also mutates
state (`_last_value`, `_last_live_*`). New entities must use the *same* value, not
re-derive it, or they will disagree (e.g. after the announcement disappears).

- New `DeliveryEtaTracker` (in `utils.py` or new `eta.py`), owned by
  `RohlikAccount` (`account.eta`):
  - holds `_last_value`, `_last_live_value`, `_last_live_order_id`,
    `_last_live_slot_since` and the current logic from `native_value`;
  - `resolve(data) -> datetime | None` recomputed once per coordinator update
    (call from `_async_update_data` / `refresh_delivery_times` before
    `async_update_listeners`, or lazily cached per `data` identity);
  - exposes `eta`, `order_id`, `is_live` (announcement-based vs slot fallback).
- `DeliveryTime` becomes a thin reader of `account.eta`; it keeps the
  `RestoreEntity`/`ExtraStoredData` and on `async_added_to_hass` seeds the
  tracker (`account.eta.restore(...)`). Existing tests in
  `tests/test_delivery_time.py` must pass unchanged (adapt only the fixture
  wiring).

## Step 2 — `sensor.<account>_delivery_remaining` ("Delivery Time Remaining")

- `device_class=DURATION`, `native_unit_of_measurement=MINUTES`, no state_class
  (no long-term statistics), `suggested_display_precision=0`.
- Value: `ceil((eta - now) / 60s)` while an upcoming order with an ETA exists,
  else `None` (unknown). Clamp at 0 (don't go negative); add attribute
  `overdue_minutes` when the courier is late, plus `eta` and `source`
  (`announcement` / `slot`) attributes.
- Ticking: local timer, no extra API calls. `async_track_time_change(second=0)`
  (aligned to the minute) registered only while ETA is within a window (e.g. ≤ 3 h
  ahead or overdue); otherwise schedule a single `async_track_point_in_time` for
  the window start. Re-arm on every coordinator update. Cancel on removal.
  Rationale: avoids writing a state every minute for an order days away
  (recorder churn); exclude `eta` attr from recorder via `_unrecorded_attributes`.

## Step 3 — `binary_sensor.<account>_last_mile` ("Delivery Last Mile")

- On when: an upcoming order exists AND `now >= eta - LAST_MILE_LEAD` (default
  5 min) AND `now <= eta + LAST_MILE_MAX_OVERDUE` (e.g. 60 min, matches
  `LIVE_ETA_STALE_AFTER`) — prevents getting stuck on if the order never leaves
  `next_order`.
- Off when the order disappears (delivered) or the soonest order id changes.
- Timers: `async_track_point_in_time` at `eta - lead` and at `eta + max_overdue`,
  re-scheduled whenever the tracker's ETA changes.
- Attributes: `eta`, `order_id`, `lead_minutes`.
- Optional (decide in review): make the lead time configurable in the options flow
  (`last_mile_minutes`, 1–30, default 5). Use case in the request (auto-open
  building door) depends on it, so an option is cheap and useful.

## Step 4 (optional, separate PR) — built-in ETA polling near delivery

The commenter (and our `automations/update_delivery_times.yaml`) poll
`update_delivery_times` every minute from ETA−30 to ETA+10. Could be an options
toggle "Refresh delivery time automatically near delivery" (default off) that
runs `refresh_delivery_times()` every 60 s inside that window, driven by the same
tracker. Keeps the countdown/last-mile accurate without user automations. Default
off to avoid surprising API load; keep the example automation in docs.

## Step 5 — translations, docs, tests

- `translations/*.json` (en, cs + every existing language file; `test_translations`
  enforces parity): `delivery_remaining`, `last_mile`, option labels.
- `icons.json`/const icons: `mdi:timer-outline`, `mdi:truck-fast`.
- README: add to sensor/binary sensor tables, short "doorbell / countdown"
  example; note they update locally between API refreshes and become precise only
  while announcements arrive (link the polling automation / option).
- Tests (freezegun / `async_fire_time_changed`):
  - tracker parity with old `native_value` behaviour (existing suite);
  - remaining: counts down per minute without API calls, unknown with no order,
    clamps at 0 + `overdue_minutes`, re-anchors when a new ETA arrives, no
    per-minute writes when ETA is far ahead;
  - last mile: off → on at ETA−5, follows ETA moving later (turns back off),
    turns off when order disappears and after max overdue, multiple concurrent
    orders use the soonest one, restart restores via tracker.

## Open questions for review

1. Clamp remaining at 0 vs allow negative values?
2. Last-mile lead time: constant vs options flow?
3. Should Step 4 (auto polling) be in scope now?
4. Tracker owned by coordinator vs new entities reading `DeliveryTime` state via
   `hass.states` (rejected: depends on entity id, breaks if disabled).
