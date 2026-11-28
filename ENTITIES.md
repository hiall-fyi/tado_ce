# Tado CE — Entity Reference (v5.0.0-beta.2)

A catalogue of every entity Tado CE creates, with the exact `entity_id` you'll see in your install.

The exact count depends on which features you've enabled and what your hardware exposes. Bridge API entities are dynamically discovered, AC zones expose different entities than heating zones, and several entities are disabled by default.

## How `entity_id` works in Tado CE

Three concepts that get conflated, but aren't the same thing:

- **`unique_id`** — internal stable identifier, hardcoded in the integration. Never changes after install. You don't see this; HA uses it to match entities to the registry across restarts.
- **`entity_id`** — the slug you use in automations and YAML (`climate.lounge`, `sensor.tado_ce_hub_api_usage`). Set when the entity is **first created**. Once written to the registry, HA never auto-renames it, even if the friendly name later changes. You can rename it yourself in **Settings → Devices**.
- **Friendly name** — the label shown in the UI. Code-driven via `strings.json`, so a single release can change it for everyone.

This document focuses on `entity_id`, because that's the one that ends up in your YAML.

### Why your slugs may differ

Tado CE's entity naming has two eras that still matter, and `entity_id` is sticky:

- **v4.x (device-grouped, multi-home):** entities use the modern HA pattern (`_attr_has_entity_name = True`, `device_info`, and translation keys). HA derives `entity_id` from `slugify(device_name + friendly_name)` (see the Home Assistant 2026.6 note below for how this changed), and the underlying `unique_id` carries a `home_id` segment for multi-home support.
- **v5.0.0:** keeps the v4.x shapes. Where a name changed, the tables below say so, and existing installs keep their `entity_id`.

Anyone who installed earlier kept their original `entity_id` slugs through every upgrade. Settings → Devices in HA is the authoritative source for any individual install.

**A note on Home Assistant 2026.6+.** From HA 2026.6, Home Assistant prefixes the **area name** to a newly-created entity's slug, on top of the device name. When a zone's device sits in an area of the same name (the usual Tado layout, e.g. a "Lounge" device in a "Lounge" area), the room name ends up twice: `sensor.lounge_lounge_temperature` instead of `sensor.lounge_temperature`. This is a Home Assistant change, not a Tado CE one, and it only affects entities created **fresh** on 2026.6 or later. Existing installs are unaffected, since `entity_id` is fixed the day an entity is first created. The friendly name stays correct either way; only the slug reads oddly. You can rename any slug under Settings → Devices. The slugs in the tables below assume the pre-2026.6 behaviour.

The tables below show the slug a **fresh installer** gets on v4.0 or later. Existing installs keep whichever slug the entity got the day it was first created, even if the friendly name has since changed.

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

Diagnostic sensors attached to the Tado CE Hub device. Most are disabled by default; enable the ones you want under **Settings → Devices → Tado CE Hub**. Boiler Flow Temp exists only when Tado reports a flow temperature for your home, whatever the boiler connection, or when you set your own flow-temperature sensor.

| Function | CE? | Friendly Name | Fresh install entity_id |
|---|---|---|---|
| Outdoor temperature |  | Outside Temp | `sensor.tado_ce_hub_outside_temp` |
| Solar radiation |  | Solar Intensity | `sensor.tado_ce_hub_solar_intensity` |
| Weather condition |  | Weather | `sensor.tado_ce_hub_weather` |
| Boiler flow temperature | ✓ | Boiler Flow Temp | `sensor.tado_ce_hub_boiler_flow_temp` |
| Home identifier | ✓ | Home ID | `sensor.tado_ce_hub_home_id` |
| API calls used | ✓ | API Usage | `sensor.tado_ce_hub_api_usage` |
| API reset countdown | ✓ | API Reset | `sensor.tado_ce_hub_api_reset` |
| Daily API limit | ✓ | API Limit | `sensor.tado_ce_hub_api_limit` |
| API health | ✓ | API Status | `sensor.tado_ce_hub_api_status` |
| Auth token health | ✓ | Token Status | `sensor.tado_ce_hub_token_status` |
| Zone count | ✓ | Zone Count | `sensor.tado_ce_hub_zone_count` |
| Last sync time | ✓ | Last Sync | `sensor.tado_ce_hub_last_sync` |
| Next sync time | ✓ | Next Sync | `sensor.tado_ce_hub_next_sync` |
| Polling interval | ✓ | Polling Interval | `sensor.tado_ce_hub_polling_interval` |
| API call history | ✓ | Call History | `sensor.tado_ce_hub_call_history` |
| API call breakdown | ✓ | API Breakdown | `sensor.tado_ce_hub_api_breakdown` |
| Home-wide insights | ✓ | Home Insights | `sensor.tado_ce_hub_home_insights` |
| Age of the cloud zone data | ✓ | Cloud Data Freshness | `sensor.tado_ce_hub_cloud_data_freshness` |
| HomeKit reads saved | ✓ | HomeKit Reads Saved | `sensor.tado_ce_hub_homekit_reads_saved` |
| HomeKit writes saved | ✓ | HomeKit Writes Saved | `sensor.tado_ce_hub_homekit_writes_saved` |

The HomeKit savings counters were added in v4.0.0 alongside HomeKit local control. They reset when your API quota resets.

---

## Hub Binary Sensors

| Function | CE? | Friendly Name | Fresh install entity_id |
|---|---|---|---|
| Geofencing (home/away) | ✓ | Geofencing | `binary_sensor.tado_ce_hub_geofencing` |
| HomeKit local control status | ✓ | HomeKit | `binary_sensor.tado_ce_hub_homekit` |
| Internet Bridge connectivity | ✓ | Bridge | `binary_sensor.tado_ce_hub_bridge` |

HomeKit and Bridge sensors are v4.0.0+ only.

The HomeKit sensor exposes attributes for uptime, reconnect count, and write performance metrics (`write_attempts`, `write_successes`, `write_fallbacks`, `write_avg_latency_ms`). Those metrics reset on every HA restart, API quota reset, and HomeKit reconnect, since they reflect current network conditions rather than historical data.

---

## Hub Controls

| Function | CE? | Friendly Name | Fresh install entity_id |
|---|---|---|---|
| Resume all schedules | ✓ | Resume All Schedules | `button.tado_ce_hub_resume_all_schedules` |
| Refresh zone capabilities | ✓ | Refresh Zone Capabilities | `button.tado_ce_hub_refresh_ac_capabilities` |
| Presence mode | ✓ | Presence Mode | `select.tado_ce_hub_presence_mode` |
| Overlay mode | ✓ | Override duration | `select.tado_ce_hub_overlay_mode` (a fresh English v5.0.0 install gets `select.tado_ce_hub_override_duration`) |
| Overlay timer duration | ✓ | Override timer (named Overlay Timer before v5.0.0) | `select.tado_ce_hub_overlay_timer` (a fresh English v5.0.0 install gets `select.tado_ce_hub_override_timer`) |

The two button friendly names were lengthened in v4.0.1 (`Resume All` → `Resume All Schedules`, `Refresh AC` → `Refresh AC Capabilities`). Existing installs kept the old slug; only fresh v4.0.1+ installers picked up the longer name.

**v5.0.0 renames the Refresh AC Capabilities button to Refresh Zone Capabilities, and this one works differently from the v4.0.1 rename above.** The v4.0.1 change only touched the friendly name; this one changes the underlying `unique_id` too, because the button covers hot-water zones as well as AC now and the old name no longer describes what it does. A friendly-name-only change is invisible to the entity registry, but a `unique_id` change normally isn't: without help, HA would treat it as a brand new entity and leave the old one behind as an orphan. v5.0.0 avoids that by re-keying the existing registry entry to the new `unique_id` on upgrade, so the button keeps its history and any customisation you've made rather than being replaced. Because HA only assigns an `entity_id` once, when an entity is first created, your `entity_id` doesn't change either way, whichever slug in the table above it happens to be. Only a fresh v5.0.0 install gets a new default slug, `button.tado_ce_hub_refresh_zone_capabilities`, from day one. Automations and dashboards referencing the button don't need any changes.

The "Overlay Mode" select was renamed to "Override duration" in v4.1.0-beta.3 to match the Tado app. The friendly name changed only; the `entity_id` and `unique_id` are unchanged, so existing installs keep the same slug.

---

## Hub Config Switches

| Function | CE? | Friendly Name | Fresh install entity_id |
|---|---|---|---|
| Quota Reserve toggle | ✓ | Quota Reserve | `switch.tado_ce_hub_quota_reserve` |

---

## Bridge API — Dynamic Discovery

When the Internet Bridge is configured, Tado CE discovers entities from the bridge's response. Which entities appear depends on your wiring type (OpenTherm, eBUS, Relay). Tado CE works the wiring type out from the fields the bridge reports (a flow temperature without an output temperature shows as eBUS, an output temperature as OpenTherm), so it is a label for what the bridge sends, not a statement about the boiler.

### Default Enabled

| Function | CE? | Friendly Name | Platform | Wiring |
|---|---|---|---|---|
| Boiler wiring state | ✓ | Boiler Wiring State | `sensor` | All |
| Boiler output temperature | ✓ | Boiler Output Temperature | `sensor` | OpenTherm |
| Boiler flow temperature | ✓ | Boiler Flow Temperature | `sensor` | eBUS |
| Max output temperature control | ✓ | Max Flow Temperature | `number` | OpenTherm |

The Max Flow Temperature number and the read-only Boiler Max Output Temperature sensor below are created when the bridge reports a maximum output temperature, either in its wiring state or, on bridges such as the BR02, only on its own `boilerMaxOutputTemperature` endpoint, which Tado CE reads as well. The `tado_ce.get_flow_temperature_settings` action reads the setting Tado keeps in its cloud.

### Default Disabled

| Function | CE? | Friendly Name | Platform | Wiring |
|---|---|---|---|---|
| Output temp timestamp | ✓ | Boiler Output Temperature Time | `sensor` | OpenTherm |
| Flow temp timestamp | ✓ | Boiler Flow Temperature Time | `sensor` | eBUS |
| Max output temperature (read-only) | ✓ | Boiler Max Output Temperature | `sensor` | OpenTherm |
| Hot water zone present | ✓ | Hot Water Present | `sensor` | All |
| Boiler device type | ✓ | Boiler Device Type | `sensor` | All |
| Boiler device serial | ✓ | Boiler Device Serial | `sensor` | All |
| Therm interface type | ✓ | Boiler Interface Type | `sensor` | All |
| Boiler device connected | ✓ | Boiler Device Connected | `sensor` | All |
| Bridge capabilities summary | ✓ | Capabilities | `sensor` | All |
| Bridge schema version | ✓ | Schema Version | `sensor` | All |

The slugs follow the standard hub-attached pattern (`sensor.tado_ce_hub_<key>`).

The Bridge connectivity status itself is documented above as a hub binary sensor (`binary_sensor.tado_ce_hub_bridge`).

---

## Zone Sensors — Core

Per-zone temperature, humidity, and target tracking.

| Function | CE? | Friendly Name | Fresh install entity_id |
|---|---|---|---|
| Temperature |  | Temperature (was "Temp" pre-v4.0) | `sensor.lounge_temperature` |
| Humidity |  | Humidity | `sensor.lounge_humidity` |
| Heating power % |  | Heating | `sensor.lounge_heating` |
| AC power % |  | AC | `sensor.lounge_ac` |
| Target temperature | ✓ | Target | `sensor.lounge_target` |
| Overlay status | ✓ | Overlay | `sensor.lounge_overlay` |

Hot water power has been a `binary_sensor` since v4.0.0 and lives under Zone Binary Sensors.

---

## Zone Sensors — Smart Comfort

Five diagnostic sensors per heating zone, all CE-exclusive.

| Function | Friendly Name | Fresh install entity_id |
|---|---|---|
| Schedule deviation | Schedule Deviation | `sensor.lounge_schedule_deviation` |
| Next schedule time | Next Schedule | `sensor.lounge_next_schedule` |
| Next schedule temp | Next Schedule Temperature | `sensor.lounge_next_schedule_temperature` |
| Preheat advisor | Preheat Advisor | `sensor.lounge_preheat_advisor` |
| Comfort target | Comfort Target | `sensor.lounge_comfort_target` |

---

## Zone Sensors — Environment

Six sensors per zone, all CE-exclusive, all diagnostic.

| Function | Friendly Name | Fresh install entity_id |
|---|---|---|
| Mold risk level | Mold Risk | `sensor.lounge_mold_risk` |
| Mold risk percentage | Mold Risk Indicator (was "Mold Risk %" pre-v4.0.1) | `sensor.lounge_mold_risk_pct` |
| Condensation risk | Condensation Risk | `sensor.lounge_condensation_risk` |
| Surface temperature | Surface Temp | `sensor.lounge_surface_temp` |
| Dew point | Dew Point | `sensor.lounge_dew_point` |
| Absolute humidity | Absolute Humidity (v4.5.0+) | `sensor.lounge_absolute_humidity` |
| Comfort level | Comfort Level | `sensor.lounge_comfort_level` |

**Mold Risk vs Mold Risk Indicator** — these aren't duplicates. The text sensor's state is the risk level (`Critical` / `High` / `Medium` / `Low` / `None`); the numeric sensor's state is surface relative humidity (0–100%) for HA history graphs and threshold automations. The numeric sensor was renamed from "Mold Risk %" to "Mold Risk Indicator" in v4.0.1, which also fixed an earlier slug collision (HA's slugify dropped the `%`, so a fresh install gave the second entity an `_2` suffix); its slug is now `mold_risk_pct`.

---

## Zone Sensors — Thermal Analytics

Six sensors per heating zone, all CE-exclusive, all disabled by default.

| Function | Friendly Name | Fresh install entity_id |
|---|---|---|
| Thermal inertia | Thermal Inertia | `sensor.lounge_thermal_inertia` |
| Heating rate | Heating Rate | `sensor.lounge_heating_rate` |
| Preheat time | Preheat Time | `sensor.lounge_preheat_time` |
| Analysis confidence | Confidence | `sensor.lounge_confidence` |
| Heating acceleration | Heating Acceleration | `sensor.lounge_heating_acceleration` |
| Approach factor | Approach Factor | `sensor.lounge_approach_factor` |

---

## Zone Sensors — Insights

| Function | CE? | Friendly Name | Fresh install entity_id |
|---|---|---|---|
| Zone insights | ✓ | Insights | `sensor.lounge_insights` |

---

## Zone Binary Sensors

| Function | CE? | Friendly Name | Fresh install entity_id |
|---|---|---|---|
| Open window detected |  | Window | `binary_sensor.lounge_window` |
| Preheat trigger | ✓ | Preheat Now | `binary_sensor.lounge_preheat_now` |
| Hot water power | ✓ | Power | `binary_sensor.lounge_power` |

Hot water power migrated from `sensor` to `binary_sensor` in v4.0.0 with the `POWER` device class. Migration is automatic on first startup.

---

## Device Sensors

One battery sensor and one connection sensor per Tado device (TRV / room sensor / wired thermostat).

| Function | CE? | Friendly Name | Fresh install entity_id |
|---|---|---|---|
| Battery status |  | Battery | `sensor.lounge_battery` |
| Connection status |  | Connection | `binary_sensor.lounge_connection` |

Connection migrated from `sensor` to `binary_sensor` with the `CONNECTIVITY` device class. Battery stays a `sensor` because Tado reports `NORMAL` / `LOW` / `CRITICAL` rather than a boolean.

For zones with multiple devices (e.g. a wired thermostat plus two TRVs), the friendly name picks up a device-type suffix to disambiguate (`Battery (TRV)`, `Battery (Wired)`).

---

## Climate / Water Heater

One climate or water heater entity per zone.

| Function | Friendly Name | Fresh install entity_id |
|---|---|---|
| Heating climate | Lounge | `climate.lounge` |
| AC climate | Lounge | `climate.lounge` |
| Water heater | Lounge | `water_heater.lounge` |

### Smart Valve Control attributes (heating zones only)

Smart Valve Control and Offset Sync (both v4.0.0+) don't create dedicated entities. They expose state via attributes on each heating zone's climate entity. Dashboards (e.g. Pulse Card) read these directly.

| Attribute | Present when | Value |
|---|---|---|
| `valve_control_enabled` | Valve Target mode is configured | `true` |
| `valve_control_active` | SVC configured | `true` while compensating, `false` when paused after a manual write or deactivated |
| `valve_control_backed_off` | Valve Target mode + BACKED_OFF state | `true` while the controller has yielded to a manual override (cleared on the next schedule block change) |
| `valve_control_mode` | Offset Sync configured | `"offset_sync"` |
| `valve_target` | Valve Target mode + currently writing | Current TRV target temperature (°C, rounded to 0.1) |
| `desired_target` | Valve Target mode + ACTIVE state | ACTIVE transition (°C) |
| `offset_celsius` | Device offset feature enabled | Current device offset in °C, fetched from Tado when the toggle is on (no Smart Valve Control needed). With Offset Sync running, it tracks the correction the controller writes |
| `offset_clamped` | `offset_celsius` present and at least one Offset Sync write has run | `true` if the last write hit Tado's -9.9 to +10°C clamp |
| `offset_clamp_direction` | Same as `offset_clamped` | `"none"` / `"hit_max"` / `"hit_min"` |

Configure SVC per zone under **Settings → Tado CE → Configure → Zone Configuration → External Sensors and Valve Control → Smart Valve Control Mode**.

### Held change attributes (heating zones only)

While Tado's cloud isn't answering, a heating change from Home Assistant can be sent over HomeKit and held until Tado is back (see [Writes while Tado's cloud is down](FEATURES_GUIDE.md#writes-while-tados-cloud-is-down)). The zone's climate entity shows it with two conditional attributes.

| Attribute | Present when | Value |
|---|---|---|
| `write_held_locally` | A change to this zone is held and not yet settled | `true` |
| `write_held_since` | Same as `write_held_locally`, once the time the change was held is known | ISO 8601 timestamp of when the change was held |

Neither attribute is there when nothing is held. They go away when the change is applied, confirmed, dropped or ended by Auto, Resume Schedule or a newer change.

When `offset_clamped: true` appears, the physical gap between your external sensor and the TRV exceeded Tado's -9.9 to +10°C device-offset limit. The `offset_celsius` value you see is the clamp boundary, not the full correction required. Check for draughts around the TRV, a cold external wall behind it, or an external sensor placed in a warmer pocket than the TRV itself.

---

## Zone Switches

| Function | CE? | Friendly Name | Fresh install entity_id |
|---|---|---|---|
| Early start | ⬆ | Early Start | `switch.lounge_early_start` |
| Child lock |  | Child Lock | `switch.lounge_child_lock` |

HA's built-in Tado integration exposes early start as a read-only binary sensor; CE provides a controllable switch.

---

## Zone Buttons

| Function | CE? | Friendly Name | Fresh install entity_id |
|---|---|---|---|
| Timer buttons | ✓ | `{duration}min Timer` (e.g. "30min Timer") | `button.lounge_30min_timer` |
| Refresh schedule | ✓ | Refresh Schedule | `button.lounge_refresh_schedule` |
| Boost | ⬆ | Boost | `button.lounge_boost` |
| Smart boost | ✓ | Smart Boost | `button.lounge_smart_boost` |
| Identify (v4.1.0-beta.3+) | ✓ | Identify | `button.lounge_identify` |

Boost replicates the Tado app's boost feature (25°C for 30min). HA's built-in integration doesn't expose this. Smart Boost is CE-exclusive, using thermal analytics to calculate optimal duration.

The Identify button (added v4.1.0-beta.3) lives on the zone device and uses the `button.{zone}_identify` shape (e.g. `button.lounge_identify`). A zone with more than one TRV gets one button per device, disambiguated by a device-type suffix, so the second resolves as `button.{zone}_identify_{device_type}` (e.g. `button.lounge_identify_va02`). Its slug is pinned, so unlike most entities it stays single-prefixed even on a fresh HA 2026.6+ install (see the note above). It flashes the device's LED locally over HomeKit when the bridge is connected, falling back to the cloud `identify_device` service otherwise.

---

## Calendar

| Function | CE? | Friendly Name | Fresh install entity_id |
|---|---|---|---|
| Zone schedule | ✓ | Heating Schedule {zone name} | `calendar.heating_schedule_{zone name}` |

Each calendar's name is the calendar device's name, "Heating Schedule", plus the room's name. An install that already had the calendars keeps the entity IDs it has (`calendar.heating_schedule_schedule`, then `_2`, `_3` and so on); only the friendly names changed.

---

## Zone Config

Per-zone configuration entities (heat emitter, UFH buffer, adaptive preheat, smart comfort, window type, overlay mode, overlay timer, min/max temp, temp offset, surface offset) are not created as entities. Those settings live in the Options Flow under **Settings → Tado CE → Configure → Zone Configuration**.

One per-zone config entity exists, added in v4.2.0:

| Function | Friendly Name | Fresh entity_id |
|---|---|---|
| Heating circuit assignment | Heating Circuit | `select.lounge_heating_circuit` |

CE-exclusive, config category, added in v4.2.0. Off unless the Heating Circuit Control toggle is on; needs a bridge with boiler circuits. Assigns a zone to a boiler circuit (or "No heating circuit"), so a zone's valves can open for residual heat without firing the boiler itself.

---

## Device Tracker

One device tracker per mobile device, CE-exclusive.

| Function | Friendly Name | Fresh install entity_id |
|---|---|---|
| Mobile presence | `{device_name}` | `device_tracker.tado_ce_hub_{device_name}` |

---

## Weather Compensation

| Function | CE? | Friendly Name | Fresh install entity_id |
|---|---|---|---|
| Target flow temperature | ✓ | Weather Compensation Target | `sensor.tado_ce_hub_wc_target_flow_temp` |
| Compensation status | ✓ | Weather Compensation Status | `sensor.tado_ce_hub_wc_status` |

Requires the Internet Bridge enabled and Weather Compensation turned on under **Settings → Tado CE → Configure → General Settings → Smart Automations**.

---

## Summary

| Category | Per-zone | Hub-attached | CE-exclusive | Standard |
|---|---|---|---|---|
| Hub Sensors | — | 20 | 17 | 3 |
| Hub Binary Sensors | — | 3 | 3 | 0 |
| Hub Controls | — | 5 | 5 | 0 |
| Hub Config Switches | — | 1 | 1 | 0 |
| Bridge API (dynamic) | — | up to 14 | 14 | 0 |
| Zone Sensors — Core | 6 | — | 2 | 4 |
| Zone Sensors — Smart Comfort | 5 | — | 5 | 0 |
| Zone Sensors — Environment | 6 | — | 6 | 0 |
| Zone Sensors — Thermal Analytics | 6 | — | 6 | 0 |
| Zone Sensors — Insights | 1 | — | 1 | 0 |
| Zone Binary Sensors | 2 (+1 per HOT_WATER zone) | — | 1 | 1 |
| Device Sensors | 1 sensor + 1 binary per device | — | 0 | 2 |
| Climate / Water Heater | 1 climate or 1 water_heater | — | 0 | 1 |
| Zone Switches | 2 | — | 0 | 2 |
| Zone Config — Heating Circuit | 1 | — | 1 | 0 |
| Zone Buttons | 4 | — | 3 | 1 |
| Calendar | 1 | — | 1 | 0 |
| Device Tracker | — | 1 per mobile device | 1 | 0 |
| Weather Compensation | — | 2 | 2 | 0 |
