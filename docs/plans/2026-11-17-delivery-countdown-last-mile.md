# Delivery countdown + "Last mile" entities — plan (rev. 2, after review)

Source: GitHub discussion request (@xdanik's AWTRIX setup) asking for a built-in
remaining-minutes sensor and an ETA-based "Last mile" binary sensor.

## Decision: integration only, no API change

`rohlik_api_python` only returns the raw announcement envelope
(`services/delivery.py:get_announcements`). All ETA derivation — parsing
(`utils.extract_delivery_datetime`), slot placement, order matching, preserving
the last live ETA, restore — lives in the integration. Countdown/last-mile are
derivations of that ETA plus the clock. The cheap poll already exists
(`hub.refresh_delivery_times`). No library change.

Note: `get_announcements()` returns `None` on HTTP error and `get_data` stores it,
so `data["delivery_announcements"]` can be `None` — all new code (and the moved
code) must read it defensively, as `hub._track_announcement_arrivals` does.

## Step 1 — coordinator-owned `DeliveryEtaTracker` (refactor, no behaviour change)

- New `eta.py`: `DeliveryEtaTracker`, owned by `RohlikAccount` (`account.eta`).
  Holds `last_value`, `last_live_value/order_id/slot_since` and the logic now in
  `DeliveryTime.native_value`.
- `resolve(now)` is the **only mutator**; `eta`, `is_live` (announcement-derived
  incl. preserved value), `order_id`, `source` are plain cached reads.
- Called from the existing `RohlikAccount.async_update_listeners` override, after
  `_track_announcement_arrivals()` — the single choke point that also covers
  `async_set_updated_data` (tests) and the in-place mutations in
  `refresh_delivery_times` / `refresh_slots`. Not from `_async_update_data`
  (`self.data` not yet set) and no identity-based caching (data is mutated in
  place and the result is time-dependent).
- Time dependence: the tracker schedules its own re-resolve + notify at
  `last_live_value + LIVE_ETA_STALE_AFTER` (and for overdue relative ETAs), so
  all entities drop a stale ETA together without waiting for a refresh. Timer
  cancelled on unload (`entry.async_on_unload`).
- **Persistence moves to the tracker**: `helpers.storage.Store` (same pattern as
  `OrderStore`), loaded in `async_setup_entry` *before*
  `async_config_entry_first_refresh`, so the first resolve already has restored
  state → no slot-fallback flap after restart. Also persist
  `_announcement_arrivals` (so an unchanged "za 2 minuty" doesn't restart its
  clock on reboot). One-time migration: if Store is empty, `DeliveryTime` seeds
  it from its old `ExtraStoredData` and triggers a re-resolve.
- Keep parity quirks for now (only `announcements[0]` considered). Follow-up:
  pick the announcement by id match with the soonest order.
- DST: use timestamps / `dt_util.utcnow()` for all deltas and the stale check
  (fixes latent same-tzinfo issue at current `sensor.py` stale check).
- `DeliveryTime` becomes a thin reader. Keep `dt_util` import in `sensor.py`
  (tests patch `custom_components.rohlikcz.sensor.dt_util.now`). Existing
  `tests/test_delivery_time.py` must pass, incl. reload/restore tests.
- Add tracker state to `diagnostics.py`.

## Step 2 — `sensor.*_delivery_remaining` ("Delivery Time Remaining")

- `device_class=DURATION`, unit minutes, no `state_class`, precision 0.
- Value: `ceil((eta − now)/60 s)`, clamped at 0; `None` with no order, or once
  more than `MAX_OVERDUE` (60 min) past the ETA. Attribute: `source`
  (`announcement` / `slot`) only.
- One `async_track_point_in_time` handle: next minute boundary inside the window
  (ETA ≤ 90 min ahead), otherwise window start. Re-armed in
  `_handle_coordinator_update` and in the `@callback` timer; cancel via
  `async_on_remove`. Write state only on change.

## Step 3 — `binary_sensor.*_last_mile` ("Delivery Last Mile")

- On when: upcoming order AND `tracker.is_live` AND
  `eta − 5 min ≤ now ≤ eta + 60 min`. Slot fallback never turns it on (it is not
  a courier position; door-opening must not trigger on it).
- Off when the order disappears / soonest order changes (derived from tracker).
- One point-in-time timer: next transition (`eta − lead`, then `eta + max`).
- Lead is a constant 5 min (users wanting another lead use
  `remaining below: N`). Configurable later via a `number` entity if requested.
- Availability: stays available while the tracker holds a live ETA, so one failed
  10-min refresh doesn't cause on → unavailable → on (re-firing door automations).

## Step 4 — built-in ETA polling near delivery (in scope)

With the 10-min coordinator interval the ETA can be 10 min old, which makes a
5-min last-mile unreliable alone. Coordinator-owned poller: while an order has an
ETA, call `refresh_delivery_times()` every 60 s from ETA−30 to ETA+10 (~40 light
requests per delivery). Errors logged, not raised; cancelled on unload.
Options-flow toggle; default **TBD by maintainer** (proposal: on). Keep the
example automation for users who disable it.

## Step 5 — translations, docs, tests

- `translations/en.json`, `cs.json`, `de.json` (test enforces en/de parity).
- Icons via `const.py` constants + `icon` property (existing convention).
- README: tables, doorbell/countdown example, note that "in N minutes"
  announcements are parsed only in Czech (lower precision elsewhere).
- Tests (`async_fire_time_changed` / freezer):
  - existing DeliveryTime suite unchanged;
  - countdown ticks without API calls; unknown with no order; clamp 0 → None
    after max overdue; re-anchors on new ETA; no per-minute writes when far away;
  - last-mile on at ETA−5, follows ETA moving later, never on from slot fallback,
    off when order disappears / after max overdue, soonest of concurrent orders;
  - restart inside last-mile window → no flap; in-place
    `refresh_delivery_times` updates new entities; stale transition without
    coordinator update; DST-boundary countdown; `delivery_announcements=None`;
    timers/poller cancelled on unload.

## Resolved questions

1. Clamp at 0, then `None` after max overdue (no negative values).
2. Constant 5-min lead (no options field).
3. Built-in polling in scope (Step 4); default to be decided.
4. Tracker owned by coordinator, with its own persistence.
