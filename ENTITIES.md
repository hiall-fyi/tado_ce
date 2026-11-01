# Tado CE — Entity Reference (v4.6.0)

A catalogue of every entity Tado CE creates, with the exact `entity_id` you'll see in your install.

The exact count depends on which features you've enabled and what your hardware exposes. Bridge API entities are dynamically discovered, AC zones expose different entities than heating zones, and several entities are disabled by default.

## How `entity_id` works in Tado CE

Three concepts that get conflated, but aren't the same thing:

- **`unique_id`** — internal stable identifier, hardcoded in the integration. Never changes after install. You don't see this; HA uses it to match entities to the registry across restarts.
- **`entity_id`** — the slug you use in automations and YAML (`climate.lounge`, `sensor.tado_ce_hub_api_usage`). Set when the entity is **first created**. Once written to the registry, HA never auto-renames it, even if the friendly name later changes. You can rename it yourself in **Settings → Devices**.
- **Friendly name** — the label shown in the UI. Code-driven via `strings.json`, so a single release can change it for everyone.

This document focuses on `entity_id`, because that's the one that ends up in your YAML.

### Why your slugs may differ

Tado CE has had three eras of entity naming, and `entity_id` is sticky:

- **v1.x – v2.x (legacy):** entities used a `Tado CE ` prefix (v1.x) or no device-grouping prefix at all (v2.x). Entity names were set explicitly and slugified directly.
- **v3.0.0 onward (device-grouped):** entities adopted the modern HA pattern (`_attr_has_entity_name = True`, `device_info`, and translation keys). HA derived `entity_id` from `slugify(device_name + friendly_name)` (see the Home Assistant 2026.6 note below for how this changed).
- **v4.0.0+ (multi-home):** the underlying `unique_id` gained a `home_id` segment for multi-home support, but `entity_id` shapes stayed the same.

Anyone who installed during an older era kept their old `entity_id` slugs through every upgrade. Settings → Devices in HA is the authoritative source for any individual install.

**A note on Home Assistant 2026.6+.** From HA 2026.6, Home Assistant prefixes the **area name** to a newly-created entity's slug, on top of the device name. When a zone's device sits in an area of the same name (the usual Tado layout, e.g. a "Lounge" device in a "Lounge" area), the room name ends up twice: `sensor.lounge_lounge_temperature` instead of `sensor.lounge_temperature`. This is a Home Assistant change, not a Tado CE one, and it only affects entities created **fresh** on 2026.6 or later. Existing installs are unaffected, since `entity_id` is fixed the day an entity is first created. The friendly name stays correct either way; only the slug reads oddly. You can rename any slug under Settings → Devices. The slugs in the tables below assume the pre-2026.6 behaviour.

The tables below show the slug a **fresh installer** got at each major-version snapshot. Migrated installs keep whichever slug the entity got the day it was first created, even if the friendly name has since changed.

## Conventions in this document

- **Zone examples** use zone name `Lounge`. Substitute your own zone name.
- **Device serial** placeholder `SU1234`. Substitute your TRV / zone serial.
- **Mobile device** placeholder `{device_name}`. Substitute the device name in your Tado app.
- **CE column markers:**
  - `✓` — CE-exclusive, not in HA's built-in Tado integration.
  - `⬆` — enhanced version of a feature the built-in HA integration already has.
  - blank — present in both integrations.

---

## Hub Sensors

Diagnostic sensors attached to the Tado CE Hub device. Four are disabled by default (Home ID, Token Status, Polling Interval, Call History), plus the two HomeKit counters. Enable the ones you want under **Settings → Devices → Tado CE Hub**. The three weather sensors exist only when the weather option is on, Boiler Flow Temp only when Tado reports a flow temperature (OpenTherm boiler) or you set your own flow-temperature sensor, and the HomeKit counters only when HomeKit local control is on. The weather sensors belong to no device, so their `entity_id` has no `tado_ce_hub_` prefix.

| Function | CE? | Friendly Name | v2.3.1 entity_id | v3.0+ fresh | v4.0+ fresh |
|---|---|---|---|---|---|
| Outdoor temperature | | Outside Temp | `sensor.outside_temperature` | `sensor.outside_temp` | `sensor.outside_temp` |
| Solar radiation | | Solar Intensity | `sensor.solar_intensity` | `sensor.solar_intensity` | `sensor.solar_intensity` |
| Weather condition | | Weather | `sensor.weather` | `sensor.weather` | `sensor.weather` |
| Boiler flow temperature | ✓ | Boiler Flow Temp | `sensor.boiler_flow_temperature` | `sensor.tado_ce_hub_boiler_flow_temp` | `sensor.tado_ce_hub_boiler_flow_temp` |
| Home identifier | ✓ | Home ID | `sensor.home_id` | `sensor.tado_ce_hub_home_id` | `sensor.tado_ce_hub_home_id` |
| API calls used | ✓ | API Usage | `sensor.api_usage` | `sensor.tado_ce_hub_api_usage` | `sensor.tado_ce_hub_api_usage` |
| API reset countdown | ✓ | API Reset | `sensor.api_reset` | `sensor.tado_ce_hub_api_reset` | `sensor.tado_ce_hub_api_reset` |
| Daily API limit | ✓ | API Limit | `sensor.api_limit` | `sensor.tado_ce_hub_api_limit` | `sensor.tado_ce_hub_api_limit` |
| API health | ✓ | API Status | `sensor.api_status` | `sensor.tado_ce_hub_api_status` | `sensor.tado_ce_hub_api_status` |
| Auth token health | ✓ | Token Status | `sensor.token_status` | `sensor.tado_ce_hub_token_status` | `sensor.tado_ce_hub_token_status` |
| Zone count | ✓ | Zone Count | `sensor.zone_count` | `sensor.tado_ce_hub_zone_count` | `sensor.tado_ce_hub_zone_count` |
| Last sync time | ✓ | Last Sync | `sensor.last_sync` | `sensor.tado_ce_hub_last_sync` | `sensor.tado_ce_hub_last_sync` |
| Next sync time | ✓ | Next Sync | `sensor.next_sync` | `sensor.tado_ce_hub_next_sync` | `sensor.tado_ce_hub_next_sync` |
| Polling interval | ✓ | Polling Interval | `sensor.polling_interval` | `sensor.tado_ce_hub_polling_interval` | `sensor.tado_ce_hub_polling_interval` |
| API call history | ✓ | Call History | `sensor.call_history` | `sensor.tado_ce_hub_call_history` | `sensor.tado_ce_hub_call_history` |
| API call breakdown | ✓ | API Breakdown | `sensor.api_call_breakdown` | `sensor.tado_ce_hub_api_breakdown` | `sensor.tado_ce_hub_api_breakdown` |
| Home-wide insights | ✓ | Home Insights | `sensor.home_insights` | `sensor.tado_ce_hub_home_insights` | `sensor.tado_ce_hub_home_insights` |
| HomeKit reads saved | ✓ | HomeKit Reads Saved | — | — | `sensor.tado_ce_hub_homekit_reads_saved` |
| HomeKit writes saved | ✓ | HomeKit Writes Saved | — | — | `sensor.tado_ce_hub_homekit_writes_saved` |

The HomeKit savings counters were added in v4.0.0 alongside HomeKit local control. They reset when your API quota resets.

---

## Hub Binary Sensors

| Function | CE? | Friendly Name | v2.3.1 entity_id | v3.0+ fresh | v4.0+ fresh |
|---|---|---|---|---|---|
| Geofencing (home/away) | ✓ | Geofencing (was "Home" pre-v3.5.0) | `binary_sensor.home` | `binary_sensor.tado_ce_hub_geofencing` | `binary_sensor.tado_ce_hub_geofencing` |
| HomeKit local control status | ✓ | HomeKit | — | — | `binary_sensor.tado_ce_hub_homekit` |
| Internet Bridge connectivity | ✓ | Connection | — | — | `binary_sensor.internet_bridge_connection` |

The Geofencing sensor was renamed from "Home" in v3.5.0. Your `entity_id` slug stayed the same if you installed before that. HomeKit and Bridge sensors are v4.0.0+ only. The Bridge sensor sits on the Internet Bridge device and is named Connection; installs created on v4.0.0 to v4.4.x have it as `binary_sensor.tado_ce_hub_bridge`. It exists only when a bridge is paired or Tado reports one. The HomeKit sensor exists only when HomeKit local control is on.

The HomeKit sensor exposes attributes for connection times (`last_connected`, `last_disconnected`), `uptime`, `reconnect_count`, `status`, zone mapping (`mapped_zones`, `unmapped_zones`) and write performance metrics (`write_attempts`, `write_successes`, `write_fallbacks`, `write_avg_latency_ms`). Those metrics reset on every HA restart, API quota reset, and HomeKit reconnect, since they reflect current network conditions rather than historical data.

---

## Hub Controls

| Function | CE? | Friendly Name | v2.3.1 entity_id | v3.0+ fresh | v4.0.1+ fresh |
|---|---|---|---|---|---|
| Resume all schedules | ✓ | Resume All Schedules | `button.resume_all_schedules` | `button.tado_ce_hub_resume_all` | `button.tado_ce_hub_resume_all_schedules` |
| Refresh AC capabilities (only with an AC zone) | ✓ | Refresh AC Capabilities | `button.refresh_ac_capabilities` | `button.tado_ce_hub_refresh_ac` | `button.tado_ce_hub_refresh_ac_capabilities` |
| Presence mode | ✓ | Presence Mode | `select.presence_mode` | `select.tado_ce_hub_presence_mode` | `select.tado_ce_hub_presence_mode` |
| Overlay mode | ✓ | Override duration | `select.overlay_mode` | `select.tado_ce_hub_overlay_mode` | `select.tado_ce_hub_override_duration` (`select.tado_ce_hub_overlay_mode` on v4.0.1 to v4.1.0-beta.2) |
| Overlay timer duration | ✓ | Overlay Timer | `select.overlay_timer_duration` | `select.tado_ce_hub_overlay_timer` | `select.tado_ce_hub_overlay_timer` |

The two button friendly names were lengthened in v4.0.1 (`Resume All` → `Resume All Schedules`, `Refresh AC` → `Refresh AC Capabilities`). Existing installs kept the old slug; only fresh v4.0.1+ installers picked up the longer name.

The "Overlay Mode" select was renamed to "Override duration" in v4.1.0-beta.3 to match the Tado app. The `unique_id` is unchanged, so existing installs keep their `select.tado_ce_hub_overlay_mode` slug. Fresh installs from that version on get `select.tado_ce_hub_override_duration`.

---

## Hub Config Switches

| Function | CE? | Friendly Name | v3.0+ fresh |
|---|---|---|---|
| Quota Reserve toggle | ✓ | Quota Reserve | `switch.tado_ce_hub_quota_reserve` |

The Quota Reserve switch arrived after v2.3.1, so there's no legacy entity_id for migrated users.

---

## Bridge API — Dynamic Discovery

When the Internet Bridge is configured, Tado CE discovers entities from the bridge's response. Which entities appear depends on your wiring type (OpenTherm, eBUS, Relay).

### Default Enabled

| Function | CE? | Friendly Name | Platform | Wiring |
|---|---|---|---|---|
| Boiler wiring state | ✓ | Boiler Wiring State | `sensor` | All |
| Boiler output temperature | ✓ | Boiler Output Temperature | `sensor` | OpenTherm |
| Boiler flow temperature | ✓ | Boiler Flow Temperature | `sensor` | eBUS (also OpenTherm when the bridge reports it) |
| Max output temperature control | ✓ | Max Flow Temperature | `number` | OpenTherm |

The Max Flow Temperature number and the read-only Boiler Max Output Temperature sensor below are only created when the bridge's response contains a maximum output temperature. None of the bridge responses reported in [#187](https://github.com/hiall-fyi/tado_ce/issues/187) do, so on most OpenTherm homes neither appears. The `tado_ce.get_flow_temperature_settings` action reads the setting Tado keeps in its cloud instead.

### Default Disabled

| Function | CE? | Friendly Name | Platform | Wiring |
|---|---|---|---|---|
| Output temp timestamp | ✓ | Boiler Output Temperature Time | `sensor` | OpenTherm |
| Flow temp timestamp | ✓ | Boiler Flow Temperature Time | `sensor` | eBUS (also OpenTherm when the bridge reports it) |
| Max output temperature (read-only) | ✓ | Boiler Max Output Temperature | `sensor` | OpenTherm |
| Hot water zone present | ✓ | Hot Water Present | `sensor` | All |
| Boiler device type | ✓ | Boiler Device Type | `sensor` | All |
| Boiler device serial | ✓ | Boiler Device Serial | `sensor` | All |
| Therm interface type | ✓ | Boiler Interface Type | `sensor` | All |
| Boiler device connected | ✓ | Boiler Device Connected | `sensor` | All |
| Bridge capabilities summary | ✓ | Bridge Capabilities | `sensor` | All |
| Bridge schema version | ✓ | Bridge Schema Version | `sensor` | All |

Bridge API entities arrived in v3.3.0, so there's no v2.x equivalent. The bridge sensors belong to the Internet Bridge device, so a fresh install gets slugs such as `sensor.internet_bridge_boiler_wiring_state` and `sensor.internet_bridge_boiler_output_temperature`. Installs from before v4.5.0 keep the hub slugs they were created with, for example `sensor.tado_ce_hub_boiler_wiring_state`. The Max Flow Temperature number belongs to the Tado CE Hub device (`number.tado_ce_hub_max_flow_temperature`). Any other field the bridge reports gets a diagnostic sensor, disabled by default, named from the field path.

The Bridge connectivity status is a binary sensor on the Internet Bridge device, `binary_sensor.internet_bridge_connection` (name Connection, diagnostic, enabled by default). It is also created when the bridge only shows up in Tado's cloud and no local pairing is set up.

---

## Zone Sensors — Core

Per-zone temperature, humidity, and target tracking. Heating and AC zones get Temperature, Humidity, Target and Overlay, plus Heating on heating zones and AC on AC zones. Hot water zones get Overlay and, if the zone reports a temperature, Temperature.

| Function | CE? | Friendly Name | v2.3.1 entity_id | v3.0+ fresh | v4.0+ fresh |
|---|---|---|---|---|---|
| Temperature | | Temperature (was "Temp" pre-v4.0) | `sensor.lounge_temperature` | `sensor.lounge_temp` | `sensor.lounge_temperature` |
| Humidity | | Humidity | `sensor.lounge_humidity` | `sensor.lounge_humidity` | `sensor.lounge_humidity` |
| Heating power % | | Heating | `sensor.lounge_heating_power` | `sensor.lounge_heating` | `sensor.lounge_heating` |
| AC power % | | AC | `sensor.lounge_ac_power` | `sensor.lounge_ac` | `sensor.lounge_ac` |
| Target temperature | ✓ | Target | `sensor.lounge_target` | `sensor.lounge_target` | `sensor.lounge_target` |
| Overlay status | ✓ | Overlay (was "Mode" in v2.3.1) | `sensor.lounge_mode` | `sensor.lounge_overlay` | `sensor.lounge_overlay` |

Temperature was shortened to "Temp" in v3.0 then restored to "Temperature" in v4.0. Fresh installs from each era got the slug matching the name in force at the time.

Hot water power was a `sensor` in v2.x; it migrated to a `binary_sensor` in v4.0.0 and now lives under Zone Binary Sensors.

---

## Zone Sensors — Smart Comfort

Five diagnostic sensors per heating or AC zone, all CE-exclusive. They are created only when Smart Comfort is on (off by default).

| Function | Friendly Name | v2.3.1 entity_id | v3.0+ fresh |
|---|---|---|---|
| Schedule deviation | Schedule Deviation (was "Historical Deviation" in v2.3.1) | `sensor.lounge_historical_deviation` | `sensor.lounge_schedule_deviation` |
| Next schedule time | Next Schedule | `sensor.lounge_next_schedule_time` | `sensor.lounge_next_schedule` |
| Next schedule temp | Next Schedule Temperature (was "Next Schedule Temp" then "Next Sched Temp") | `sensor.lounge_next_schedule_temp` | `sensor.lounge_next_sched_temp` (v3.5.x) → `sensor.lounge_next_schedule_temperature` (v4.0+) |
| Preheat advisor | Preheat Advisor | `sensor.lounge_preheat_advisor` | `sensor.lounge_preheat_advisor` |
| Comfort target | Comfort Target (was "Smart Comfort Target" in v2.3.1) | `sensor.lounge_smart_comfort_target` | `sensor.lounge_comfort_target` |

The Next Schedule Temp friendly name went through three iterations (`Next Schedule Temp` → `Next Sched Temp` in v3.5 → `Next Schedule Temperature` in v4.0). The slug is sticky to whichever name was in force when the install was created.

---

## Zone Sensors — Environment

Seven sensors per heating or AC zone, all CE-exclusive, all diagnostic.

| Function | Friendly Name | v2.3.1 entity_id | v3.0+ fresh |
|---|---|---|---|
| Mold risk level | Mold Risk | `sensor.lounge_mold_risk` | `sensor.lounge_mold_risk` |
| Mold risk percentage | Mold Risk Indicator (was "Mold Risk %" pre-v4.0.2) | `sensor.lounge_mold_risk_percentage` | `sensor.lounge_mold_risk_indicator` |
| Condensation risk | Condensation Risk (was "Condensation" in v3.5.x) | `sensor.lounge_condensation_risk` | `sensor.lounge_condensation` (v3.5) → `sensor.lounge_condensation_risk` (v4.0+) |
| Surface temperature | Surface Temp | `sensor.lounge_surface_temperature` | `sensor.lounge_surface_temp` |
| Dew point | Dew Point | `sensor.lounge_dew_point` | `sensor.lounge_dew_point` |
| Absolute humidity | Absolute Humidity (v4.5.0+) | — (new in v4.5.0) | `sensor.lounge_absolute_humidity` |
| Comfort level | Comfort Level | `sensor.lounge_comfort_level` | `sensor.lounge_comfort_level` |

**Mold Risk vs Mold Risk Indicator** — these aren't duplicates. The text sensor's state is the risk level (`Critical` / `High` / `Medium` / `Low`); the numeric sensor's state is surface relative humidity (0–100%) for HA history graphs and threshold automations. Added together in v2.0.1 by request. The numeric sensor was renamed from "Mold Risk %" to "Mold Risk Indicator" in v4.0.2, which also fixed an earlier slug collision (HA's slugify dropped the `%`, so a fresh install gave the second entity an `_2` suffix); a fresh install now gets `mold_risk_indicator` (the translation key is still `mold_risk_pct`).

---

## Zone Sensors — Thermal Analytics

Six sensors per heating zone, all CE-exclusive, all disabled by default. They are created only when Thermal Analytics is on, the zone reports heating power, and the zone is in the selected thermal zones (all zones if none are selected).

| Function | Friendly Name | v2.3.1 entity_id | v3.0+ fresh |
|---|---|---|---|
| Thermal inertia | Thermal Inertia | `sensor.lounge_thermal_inertia` | `sensor.lounge_thermal_inertia` |
| Heating rate | Heating Rate (was "Avg Heating Rate" in v2.3.1) | `sensor.lounge_avg_heating_rate` | `sensor.lounge_heating_rate` |
| Preheat time | Preheat Time | `sensor.lounge_preheat_time` | `sensor.lounge_preheat_time` |
| Analysis confidence | Confidence (was "Analysis Confidence" in v2.3.1) | `sensor.lounge_analysis_confidence` | `sensor.lounge_confidence` |
| Heating acceleration | Heating Acceleration (was "Heat Accel" in v3.5.x) | `sensor.lounge_heating_acceleration` | `sensor.lounge_heat_accel` (v3.5) → `sensor.lounge_heating_acceleration` (v4.0+) |
| Approach factor | Approach Factor | `sensor.lounge_approach_factor` | `sensor.lounge_approach_factor` |

---

## Zone Sensors — Insights

| Function | CE? | Friendly Name | v2.3.1 entity_id | v3.0+ fresh |
|---|---|---|---|---|
| Zone insights (heating and AC zones) | ✓ | Insights | `sensor.lounge_insights` | `sensor.lounge_insights` |

---

## Zone Binary Sensors

| Function | CE? | Friendly Name | v2.3.1 entity_id | v3.0+ fresh | v4.0+ fresh |
|---|---|---|---|---|---|
| Open window detected (heating zones that support it) | | Window | `binary_sensor.lounge_open_window` | `binary_sensor.lounge_window` | `binary_sensor.lounge_window` |
| Preheat trigger (heating zones, Smart Comfort on) | ✓ | Preheat Now | `binary_sensor.lounge_preheat_now` | `binary_sensor.lounge_preheat_now` | `binary_sensor.lounge_preheat_now` |
| Window predicted (heating and AC zones) | ✓ | Window Predicted | `binary_sensor.lounge_window_predicted` | `binary_sensor.lounge_window_predicted` | `binary_sensor.lounge_window_predicted` |
| Hot water power | ✓ | Power | `sensor.lounge_power` (was a sensor) | — | `binary_sensor.lounge_power` |

Hot water power migrated from `sensor` to `binary_sensor` in v4.0.0 with the `POWER` device class. Migration is automatic on first startup.

---

## Device Sensors

One battery sensor and one connection sensor per Tado device (TRV / room sensor / wired thermostat) that reports a battery or connection state. Both are diagnostic entities.

| Function | CE? | Friendly Name | v2.3.1 entity_id | v3.0+ fresh |
|---|---|---|---|---|
| Battery status | | Battery (was "{zone} Battery" in v2.3.1) | `sensor.lounge_battery` | `sensor.lounge_battery` |
| Connection status | | Connection (was "{zone} Connection" in v2.3.1) | `sensor.lounge_connection` (was a sensor) | `binary_sensor.lounge_connection` |

Connection migrated from `sensor` to `binary_sensor` with the `CONNECTIVITY` device class. Battery stays a `sensor` because its state is `Normal`, `Low` or `Critical` rather than a boolean.

For zones with multiple devices (e.g. a wired thermostat plus two TRVs), the friendly name picks up the Tado device type to disambiguate (`Battery VA02`, `Battery SU02`, or `Battery VA02 (1)` and `Battery VA02 (2)` when two devices share a type). The fresh slugs follow suit, for example `sensor.lounge_battery_va02` and `binary_sensor.lounge_connection_va02`. A v2.3.1 install put the type before the name instead, for example `sensor.lounge_va02_battery`.

---

## Climate / Water Heater

One climate or water heater entity per zone.

| Function | Friendly Name | v2.3.1 entity_id | v3.0+ fresh |
|---|---|---|---|
| Heating climate | Lounge | `climate.lounge` | `climate.lounge` |
| AC climate | Lounge | `climate.lounge` | `climate.lounge` |
| Water heater | Lounge | `water_heater.lounge` | `water_heater.lounge` |

These slugs have been stable since v1.2.0. v1.0.0 to v1.1.0 used a `Tado CE ` prefix (for example `climate.tado_ce_lounge`).

### Smart Valve Control attributes (heating zones only)

Smart Valve Control and Offset Sync (both v4.0.0+) don't create dedicated entities. They expose state via attributes on each heating zone's climate entity. Dashboards (e.g. Pulse Card) read these directly.

| Attribute | Present when | Value |
|---|---|---|
| `valve_control_enabled` | Valve Target mode is configured | `true` |
| `valve_control_active` | Every heating zone | `true` while the controller is running: Valve Target is correcting the zone, or Offset Sync is neither paused nor deactivated. `false` when Valve Target is idle or backed off, Offset Sync is paused, the controller is deactivated, no zone data has been fetched since a restart, or no controller is set up |
| `valve_control_backed_off` | Valve Target mode + BACKED_OFF state | `true` while the controller has yielded to a manual override (cleared on the next schedule block change or when the override is removed) |
| `valve_control_mode` | Offset Sync configured | `"offset_sync"` |
| `valve_target` | Valve Target mode + ACTIVE state | Last TRV target temperature the controller set (°C) |
| `desired_target` | Valve Target mode + ACTIVE state | User's intended target captured at the IDLE → ACTIVE transition (°C) |
| `offset_celsius` | Device Temperature Offsets turned on | Current device offset in °C, fetched from Tado when the toggle is on (no Smart Valve Control needed). With Offset Sync running, it tracks the correction the controller writes |
| `offset_clamped` | `offset_celsius` present and at least one Offset Sync write has run | `true` if the last write hit Tado's -9.9 to +10°C clamp |
| `offset_clamp_direction` | Same as `offset_clamped` | `"none"` / `"hit_max"` / `"hit_min"` |

Configure SVC per zone under **Settings → Tado CE → Configure → Zone Configuration → your zone → External Sensors and Valve Control → Smart Valve Control Mode**.

When `offset_clamped: true` appears, the physical gap between your external sensor and the TRV exceeded Tado's -9.9 to +10°C device-offset limit. The `offset_celsius` value you see is the clamp boundary, not the full correction required. Check for draughts around the TRV, a cold external wall behind it, or an external sensor placed in a warmer pocket than the TRV itself.

---

## Zone Switches

| Function | CE? | Friendly Name | v2.3.1 entity_id | v3.0+ fresh |
|---|---|---|---|---|
| Early start (heating zones) | ⬆ | Early Start | `switch.lounge_early_start` | `switch.lounge_early_start` |
| Child lock (devices that support it) | | Child Lock (was "{zone} Child Lock" in v2.3.1) | `switch.lounge_child_lock` | `switch.lounge_child_lock` |

HA's built-in Tado integration exposes early start as a read-only binary sensor; CE provides a controllable switch. A zone with more than one device that supports child lock gets one switch per device, all named Child Lock, so HA adds `_2` to the later slugs (`switch.lounge_child_lock_2`). On v2.3.1 the device type was part of the name instead (`switch.lounge_va02_child_lock`).

---

## Zone Buttons

| Function | CE? | Friendly Name | v2.3.1 entity_id | v3.0+ fresh |
|---|---|---|---|---|
| Timer buttons (hot water zones, 30, 60 and 90 min) | ✓ | `{duration}min Timer` (e.g. "30min Timer") | `button.lounge_30min_timer` | `button.lounge_30min_timer` |
| Refresh schedule (heating and AC zones) | ✓ | Refresh Schedule | `button.lounge_refresh_schedule` | `button.lounge_refresh_schedule` |
| Boost (heating zones) | ⬆ | Boost | `button.lounge_boost` | `button.lounge_boost` |
| Smart boost (heating zones) | ✓ | Smart Boost | `button.lounge_smart_boost` | `button.lounge_smart_boost` |
| Identify (v4.1.0-beta.3+) | ✓ | Identify | — | `button.lounge_identify` |

Boost replicates the Tado app's boost feature (25°C for 30min). HA's built-in integration doesn't expose this. Smart Boost is CE-exclusive, using thermal analytics to calculate optimal duration.

The Identify button (added v4.1.0-beta.3) lives on the zone device and uses the `button.{zone}_identify` shape (e.g. `button.lounge_identify`). Every device in a zone (heating, AC or hot water) gets its own button. A zone with more than one device names each button with the device type, so the slug is `button.{zone}_identify_{device_type}` (e.g. `button.lounge_identify_va02`), plus a number (`button.lounge_identify_va02_1`) when two devices share a type. A device listed under two zones gets a single button. Its slug is pinned, so unlike most entities it stays single-prefixed even on a fresh HA 2026.6+ install (see the note above). It flashes the device's LED locally over HomeKit when the bridge is connected, falling back to the cloud `identify_device` service otherwise.

---

## Calendar

| Function | CE? | Friendly Name | v2.3.1 entity_id | v3.0+ fresh |
|---|---|---|---|---|
| Zone schedule (heating zones only) | ✓ | Schedule | `calendar.lounge` | `calendar.heating_schedule_schedule` for the first zone, then `calendar.heating_schedule_schedule_2`, `_3` and so on |

The doubled-segment slug (`heating_schedule_schedule`) comes from the calendar device being named "Heating Schedule" plus a translation key of `schedule`. Every heating zone's calendar sits under that one device, so the first gets the plain slug and the others get `_2`, `_3`. The calendars exist only with Schedule Calendar turned on in **Settings → Tado CE → Configure → General Settings → Tado Features**.

---

## Zone Config

Per-zone configuration entities (heat emitter, UFH buffer, adaptive preheat, smart comfort, window type, overlay mode, overlay timer, min/max temp, temp offset, surface offset) **were removed in v3.1.0**. Those settings now live in the Options Flow under **Settings → Tado CE → Configure → Zone Configuration**. They are no longer created; any left over from before v3.1.0 can be deleted from Settings → Devices & Services → Entities.

One per-zone config entity was reintroduced in v4.2.0:

| Function | Friendly Name | Fresh entity_id |
|---|---|---|
| Heating circuit assignment | Heating Circuit | `select.lounge_heating_circuit` |

CE-exclusive, config category, added in v4.2.0. One per heating zone, created only when Heating Circuit Control is on (**Configure → General Settings → Tado Features**) and Tado reports at least one boiler circuit. Enabled by default once created. Assigns a zone to a boiler circuit (or "No heating circuit"), so a zone's valves can open for residual heat without firing the boiler itself.

---

## Device Tracker

One device tracker per mobile device that has geo-tracking on in the Tado app, CE-exclusive. Needs Mobile Device Tracking turned on in **Configure → General Settings → Tado Features**. Diagnostic category, enabled by default.

| Function | Friendly Name | v2.3.1 entity_id | v3.0+ fresh |
|---|---|---|---|
| Mobile presence | `{device_name}` (was "Tado CE {device_name}") | `device_tracker.tado_ce_{device_name}` | `device_tracker.tado_ce_hub_{device_name}` |

The `Tado CE ` prefix was dropped from friendly names in v3.0.1; existing trackers kept the original `entity_id`.

---

## Weather Compensation

| Function | CE? | Friendly Name | v3.3.0+ fresh |
|---|---|---|---|
| Target flow temperature | ✓ | Weather Compensation Target | `sensor.tado_ce_hub_weather_compensation_target` |
| Compensation status | ✓ | Weather Compensation Status | `sensor.tado_ce_hub_weather_compensation_status` |

Requires the Internet Bridge turned on (**Settings → Tado CE → Configure → General Settings → Hardware Connections**) and Weather Compensation turned on (same page, **Smart Automations**). Both sensors are diagnostic and enabled by default. The target is in °C and carries the attributes `outdoor_temperature`, `outdoor_temperature_raw`, `heating_system_preset`, `room_compensation_offset`, `smoothing_method` and `smoothing_window`.

---

## Summary

| Category | Per-zone | Hub-attached | CE-exclusive | Standard |
|---|---|---|---|---|
| Hub Sensors | — | up to 19 | 16 | 3 |
| Hub Binary Sensors | — | up to 3 | 3 | 0 |
| Hub Controls | — | up to 5 | 5 | 0 |
| Hub Config Switches | — | 1 | 1 | 0 |
| Bridge API (dynamic) | — | up to 14 | 14 | 0 |
| Zone Sensors — Core | 5 per heating or AC zone | — | 2 | 3 |
| Zone Sensors — Smart Comfort | 5 per heating or AC zone | — | 5 | 0 |
| Zone Sensors — Environment | 7 per heating or AC zone | — | 7 | 0 |
| Zone Sensors — Thermal Analytics | 6 per heating zone | — | 6 | 0 |
| Zone Sensors — Insights | 1 per heating or AC zone | — | 1 | 0 |
| Zone Binary Sensors | up to 3 per heating zone (Window Predicted always, Open Window if the zone supports it, Preheat Now if Smart Comfort is on), 1 per AC zone (+1 per HOT_WATER zone) | — | 2 | 1 |
| Device Sensors | 1 sensor + 1 binary per device | — | 0 | 2 |
| Climate / Water Heater | 1 climate or 1 water_heater | — | 0 | 1 |
| Zone Switches | up to 1 Early Start per heating zone (if the zone supports it) + 1 Child Lock per device that reports a child lock | — | 0 | 2 |
| Zone Config — Heating Circuit | 1 per heating zone | — | 1 | 0 |
| Zone Buttons | 3 per heating zone, 1 per AC zone | — | 2 | 1 |
| Hot Water Timer Buttons | 3 per HOT_WATER zone | — | 3 | 0 |
| Identify Buttons | 1 per device | — | 1 | 0 |
| Calendar | 1 per heating zone | — | 1 | 0 |
| Device Tracker | — | 1 per mobile device | 1 | 0 |
| Weather Compensation | — | 2 | 2 | 0 |
