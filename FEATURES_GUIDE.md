# Tado CE Features Guide

Complete guide to all Tado CE exclusive features, configurations, and usage scenarios.

> **Entity ID note:** Examples on this page use the older entity_id style (e.g. `sensor.tado_ce_api_usage`, `sensor.lounge_thermal_inertia`), because HA preserves entity_ids across upgrades, so these slugs stay stable for longstanding installs. On a fresh install, **hub-level** entities pick up a device-prefixed shape (`sensor.tado_ce_hub_api_usage`); **zone-level** entities use the same `sensor.<zone>_<feature>` shape either way. [ENTITIES.md](ENTITIES.md) lists every entity with its older and fresh-install entity_id side by side. Check there before copying examples into your dashboard or automations.

## Table of Contents

1. [Glossary](#glossary)
   - [Quota & API Limits](#quota--api-limits)
   - [HomeKit Local Control](#homekit-terms)
   - [Smart Valve Control](#smart-valve-control-terms)
   - [Heating Circuit](#heating-circuit)
   - [Other Key Terms](#other-key-terms)
2. [Multi-Home Support](#multi-home-support)
3. [API Management](#api-management)
4. [Smart Polling](#smart-polling)
5. [API Write Optimization](#api-write-optimization)
6. [Thermal Analytics](#thermal-analytics)
7. [Smart Comfort Analytics](#smart-comfort-analytics)
8. [Enhanced Mold Risk Assessment](#enhanced-mold-risk-assessment)
9. [Heating Cycle Detection](#heating-cycle-detection)
10. [Enhanced Controls](#enhanced-controls)
11. [Bridge API Integration](#bridge-api-integration)
12. [HomeKit Local Control](#homekit-local-control)
13. [Weather Compensation](#weather-compensation)
14. [Smart Valve Control](#smart-valve-control)
15. [Optional Features](#optional-features)
16. [Automation Events](#automation-events)
17. [Multi-TRV Zones](#multi-trv-zones)
18. [Per-Zone Configuration](#per-zone-configuration)
19. [Per-Zone Entity Types](#per-zone-entity-types)
20. [Reset to Defaults](#reset-to-defaults)
21. [Configuration Scenarios](#configuration-scenarios)
22. [Actionable Insights](#actionable-insights)
23. [Settings Configured in the Tado App, Not Tado CE](#settings-configured-in-the-tado-app-not-tado-ce)
24. [Troubleshooting](#troubleshooting)
25. [What's New in 4.x](#whats-new-in-4x)

---

## Glossary

**Upgrading from v4.0.x?** See [ROADMAP - Upgrade Paths](ROADMAP.md#upgrade-paths) for version compatibility and migration steps.

### Quota & API Limits

**Definition:** Tado enforces daily API call limits (100–20,000 depending on plan). The built-in HA integration polls cloud every 15–20 minutes, consuming ~300+ calls/day on multi-zone homes.

**Why it matters:** Once you hit the limit, all API calls (including cloud reads) fail until the next day's reset.

**How Tado CE helps:**
- Exposes real-time quota usage (`sensor.tado_ce_api_usage`) so you see when approaching the limit
- Smart Polling adapts refresh intervals based on quota (slower polling = fewer calls)
- HomeKit local reads (if available) bypass quota entirely, returning temperature/humidity/state in ~2 seconds without API calls

### HomeKit terms

**Definition:** Pairing your Internet Bridge V3 or V3+ via HomeKit (in the Home app, not Tado app) lets Tado CE read temperature, humidity, and heating/cooling state directly from the bridge over LAN. These reads don't count against the Tado API quota.

**Prerequisites:**
- Internet Bridge V3 or V3+
- HomeKit enabled on the bridge (see [Tado's HomeKit article](https://support.tado.com/en/articles/3387324-how-do-i-set-up-tado-with-apple-homekit))
- Bridge not paired with Apple Home or another HomeKit controller (unpair it there first if it is). Tado CE does the pairing
- Tado CE configured to read HomeKit (automatic on setup; configurable per-zone in Settings → Configure)

**Why it matters:** On a nine-zone test home, daily API usage dropped from about 394 calls to under 80 once HomeKit local control was active. Temperature updates arrive in ~2 seconds vs 15–20 minutes via cloud polling.

**Limitations:**
- HomeKit is LAN-only (must be on same network as bridge or have a home hub)
- Bridge V2 doesn't support HomeKit mode (V3 or V3+ only)
- Heating/cooling state from HomeKit; target-temperature and HVAC-mode writes on heating zones also go over HomeKit (the cloud handles a zone that is off, and overlay modes other than Manual; see the HomeKit local control section of the README)

### Smart Valve Control terms

**Definition:** Three strategies to compensate when a radiator TRV reads warmer than the room, so the valve closes before the room reaches target temperature:

1. **No compensation** (default): Tado uses only the TRV sensor temperature
2. **Offset Sync** (v4.1.0+): Write a temperature offset to Tado's override, so Tado's algorithm sees (e.g.) "room is 20°C" instead of "TRV reads 22°C". Tado handles the compensation.
3. **Valve Target** (v4.1.0+): Override the setpoint directly, bypassing Tado's algorithm. Tado CE writes the setpoint it needs for the valve to open or close (say 25°C instead of "reach 20°C"), based on your external sensor. Full control, but you handle the logic.

**When to use:**
- **No compensation:** If your TRV reads accurately or you don't have an external sensor
- **Offset Sync:** If you have an external sensor and trust Tado's setpoint logic
- **Valve Target:** If you want full control over valve position

### Heating Circuit

**Definition:** A boiler can heat multiple zones via separate circuits (e.g., "downstairs" circuit, "upstairs" circuit). A zone's heating circuit select tells the boiler which circuit to activate. Tado allows multiple zones per circuit or "No heating circuit" for residual-heat coasting (zone relies on passive warm-up from adjacent heated zones).

**Why it matters:**
- Zoning efficiency: only heat the zones you need
- Residual heat: close a zone's circuit but leave it occupied, it warms passively
- Boiler diagnostics: Tado CE exposes which circuits are active, correlates to energy use

**Related entity:** `select.<zone>_heating_circuit` (per zone, auto-created)

### Other Key Terms

- **Thermal Inertia:** Lag between heating activation and room temperature change (minutes). Tado CE calculates this to predict preheat start times.
- **Mold Risk Indicator:** Percentage (0–100%) representing mold risk based on humidity, room and outdoor temperature, and window type. Triggered by prolonged high humidity.
- **Dew Point:** Temperature at which air becomes saturated (humidity 100%). If room temp drops below dew point, condensation forms. Tado CE tracks this alongside absolute humidity.
- **Absolute Humidity:** Mass of water per volume (g/m³). Unlike relative humidity, it doesn't change with temperature, so it's more useful for mold risk assessment.
- **Weather Compensation (Boiler Flow Temp):** For OpenTherm boilers, set the maximum flow temperature your boiler may use from the outdoor temperature (colder outside → higher limit), in Tado's own Maximum flow temperature setting. Needs the Internet Bridge and an outdoor temperature, from your own sensor, or from Tado's weather data when Weather Data is turned on.
- **Actionable Insights:** Automated recommendations (e.g., "ventilate now, outside air is drier") based on current conditions.
- **Multi-Home Isolation:** Each Tado account/home in HA gets its own Tado CE integration instance, with separate data files, entity IDs, and polling. No cross-home data leakage.

---

## What's New in 4.x

New user-facing features and settings added across the 4.x line. Each links to its section below; bug fixes are in [CHANGELOG.md](CHANGELOG.md).

| Version | What you got | Where |
|---------|-------------|-------|
| v5.0.0-beta.2 | `tado_ce.set_climate_active_timetable` switches a room between its three Tado timetables, and heating and AC climates show the active one | [Switch the Active Timetable](#switch-the-active-timetable) |
| v5.0.0 | A Cloud Data Freshness diagnostic sensor shows how old the zone data from Tado is and why the last poll skipped a fetch | [API Management](#api-management) |
| v5.0.0 | A room heated by radiator valves, alone or with a wall thermostat, follows an off switched in the Tado app straight away, and the heating action follows a HomeKit target change at once | [HomeKit Local Control](#homekit-local-control) |
| v4.6.0 | Per-zone Send changes via (Automatic or Tado cloud) for heating zones, so a wall thermostat's display can follow changes made from Home Assistant | [Per-Zone Configuration](#per-zone-configuration) |
| v4.6.0 | The dropdown options in Zone Configuration and the hub Override duration select are translated; the hub select's state is now `manual`, `tado_mode` or `timer` | [Override duration](#override-duration) |
| v4.6.0 | Optional Share write timing setting (off by default), counting how long Tado takes to show a change | [Share write timing](#share-write-timing-optional) |
| v4.5.1 | HomeKit pairing logs a line once every zone is mapped, and the pairing code field is marked as a one-time code | [HomeKit Local Control](#homekit-local-control) |
| v4.5.0 | Absolute Humidity sensor, per zone (g/m³, alongside the existing Dew Point sensor) | [Enhanced Mold Risk Assessment](#enhanced-mold-risk-assessment) |
| v4.5.0 | Bind a third-party boiler flow-temperature sensor for homes without an Internet Bridge | [Boiler Flow Temperature](#boiler-flow-temperature) |
| v4.5.0 | Ventilation Opportunity insight — flags when a room's air is unusually wetter than outside | [Actionable Insights](#actionable-insights) |
| v4.4.0 | `tado_ce.refresh` gains a third option, Zone states, to force an immediate temperature/target/mode fetch past the polling floor | [Force a Refresh](#11-force-a-refresh-v430-zone-states-added-in-v440) |
| v4.3.3 | `tado_ce.set_climate_timer` shows the timer's temperature on the card with the write, rather than after Tado's next refresh | [Climate Timer Service](#3-climate-timer-service) |
| v4.3.0 | `tado_ce.refresh` service — force an immediate presence fetch past the polling floor | [Force a Refresh](#11-force-a-refresh-v430-zone-states-added-in-v440) |
| v4.2.0 | Heating circuit control — per-zone boiler-circuit select, "No heating circuit" for residual-heat coasting | [Heating Circuit Control](#heating-circuit-control) |
| v4.2.0 | Heat and cold alerts for vulnerable occupants (elderly / infant / unwell) | [Smart Comfort Analytics](#smart-comfort-analytics) |
| v4.1.0 | Per-zone Temperature source (Automatic / HomeKit / Cloud) | [HomeKit Local Control](#homekit-local-control) |
| v4.1.0 | Per-device Identify button (flashes the TRV / thermostat LED) | [Per-Zone Entity Types](#per-zone-entity-types) |
| v4.1.0 | `tado_ce.set_climate_schedule_temperature` — set a target from automations without tripping Smart Valve Control | [Smart Valve Control](#smart-valve-control) |
| v4.1.0 | Configurable refresh intervals for Home Presence, Weather, and Mobile Device Tracking | [Optional Features](#optional-features) |
| v4.1.0 | Override duration relabelled to match the Tado app (3 options, Timer cap up to 12 hours) | [Per-Zone Configuration](#per-zone-configuration) |
| v4.1.4 | Boiler diagnostic sensors renamed "Bridge …" → "Boiler …" (display names only) | [Bridge API Integration](#bridge-api-integration) |
| v4.0.1 | AC swing split into independent vertical and horizontal axes | [Enhanced Controls](#enhanced-controls) |
| v4.0.1 | Mold Risk % sensor renamed to Mold Risk Indicator | [Enhanced Mold Risk Assessment](#enhanced-mold-risk-assessment) |
| v4.0.0 | HomeKit local control, Weather Compensation, Smart Valve Control, and more | across this guide |

---

## Multi-Home Support

**Requirement:** Multiple Tado homes/accounts | **Automatic**

Run multiple Tado accounts or homes in a single Home Assistant instance with full data isolation.

### Overview

Each config entry is completely isolated — its own coordinator, API client, data loader, and cleanup. All per-entry state uses `ConfigEntry.runtime_data` instead of shared global state. A line-by-line audit across all 85 source files confirmed zero data isolation issues.

### How It Works

1. Add a second Tado CE integration via **Settings → Devices & Services → Add Integration → Tado CE**
2. Authenticate with a different Tado account (or same account, different home)
3. Each home gets its own set of entities, data files, and polling schedule

### Data Isolation

All data files include `{home_id}` suffix:
- `zones_{home_id}.json`, `ratelimit_{home_id}.json`, `config_{home_id}.json`, etc.
- Entity unique IDs include `{home_id}` prefix for collision avoidance
- Each home has independent API quota tracking and adaptive polling

---

## API Management

**Available:** v1.0.0+ | **Requirement:** None | **Always Enabled**

Real-time tracking of your Tado API usage, helping you avoid rate limiting and understand consumption patterns.

### Overview

Tado enforces API rate limits (100–20,000 calls/day depending on your plan). The built-in HA integration doesn't expose usage data. Tado CE solves this by:

- Reading rate limit data from Tado API response headers
- Auto-detecting your daily limit (100/1000/20000)
- Tracking reset time, call history, and per-endpoint breakdown

**Computed by:** Both — Tado server supplies raw quota figures (calls used, limit, reset time) via API response headers; Tado CE reads those headers, derives `api_status` from threshold logic (>80% → warning, 0 remaining → rate_limited), tracks call history and per-endpoint breakdown locally, and exposes all of it as sensors.

### Sensors

| Sensor | Friendly Name | Unit | Description |
|--------|--------------|------|-------------|
| `sensor.tado_ce_api_usage` | API Usage | calls | API calls used today |
| `sensor.tado_ce_api_limit` | API Limit | calls | Daily API call limit |
| `sensor.tado_ce_api_reset` | API Reset | timestamp | When your limit resets |
| `sensor.tado_ce_api_status` | API Status | text | API connection status |
| `sensor.tado_ce_token_status` | Token Status | text | Auth token health |
| `sensor.tado_ce_next_sync` | Next Sync | timestamp | Next scheduled API sync |
| `sensor.tado_ce_polling_interval` | Polling Interval | minutes | Current polling interval |
| `sensor.tado_ce_call_history` | Call History | count | API call history with statistics |
| `sensor.tado_ce_api_call_breakdown` | API Breakdown | text | Call breakdown by endpoint type |
| `sensor.tado_ce_hub_cloud_data_freshness` | Cloud Data Freshness (v5.0.0) | minutes | How old the zone data from Tado is. Attributes show when the zone states and the last full refresh were fetched, why the last poll skipped a fetch, and the calls made today by type |

### API Status States

| State | Meaning | When |
|-------|---------|------|
| `ok` | All good | Quota usage < 80% |
| `warning` | High usage | Quota usage > 80% |
| `rate_limited` | Quota exhausted | Remaining = 0 |
| `error` | Connection issue | Failed to read rate limit data |
| `unavailable` | Sensor not ready | During HA restart/reload |

### Configuration

**API History Retention:**
1. Go to Settings → Devices & Services → Tado CE → Configure
2. Set "API History Retention" (0–365 days, default: 14)
3. Set to 0 for unlimited retention

### Usage Scenarios

#### Scenario 1: Monitor API Usage to Avoid Rate Limiting

```yaml
automation:
  - alias: "Alert: API Usage High"
    trigger:
      - platform: state
        entity_id: sensor.tado_ce_api_status
        to: "warning"
    action:
      - service: notify.mobile_app
        data:
          title: "Tado API Usage High"
          message: >
            API status: {{ states('sensor.tado_ce_api_status') }}.
            Usage: {{ states('sensor.tado_ce_api_usage') }} / {{ states('sensor.tado_ce_api_limit') }}.
            Resets at {{ states('sensor.tado_ce_api_reset') }}.
```

#### Scenario 2: Dashboard Card

```yaml
type: entities
entities:
  - entity: sensor.tado_ce_api_usage
    name: "Calls Used Today"
  - entity: sensor.tado_ce_api_limit
    name: "Daily Limit"
  - entity: sensor.tado_ce_api_status
    name: "API Status"
  - entity: sensor.tado_ce_api_reset
    name: "Resets At"
```

---

## Smart Polling

**Available:** v1.0.0+ | **Requirement:** None | **Always Enabled**

Automatically adjusts API polling frequency based on time of day, remaining quota, and configuration.

### Overview

Smart Polling includes multiple strategies:

- **Day/Night Polling** — more frequent during day, less at night
- **Adaptive Polling** — auto-adjusts based on remaining quota, with a flat 5 min floor and a 120 min ceiling. The floor is the same whatever your daily call limit; a bigger quota doesn't buy a faster automatic cadence because zone temperature doesn't change any faster. Want faster than 5 min? Set a custom interval.
- **Per-Type Refresh** — slow-changing data (weather, presence, mobile locations) refreshes on its own schedule instead of riding the zone-state cadence, so a fast poll doesn't burn quota re-reading data that hasn't changed
- **HomeKit Defer** — when HomeKit local control is connected, scheduled cloud polls come no closer together than your Cloud Data Refresh setting, and further apart when the quota maths asks for it (120 minutes at night by default), so the setting works as a minimum. Fetches set off by an action, such as the check about 90 seconds after a HomeKit write, happen sooner, and a custom interval replaces the schedule in its own hours. Temperature and target from HomeKit are not slowed down by this
- **Quota Reserve Protection**: pauses polling before a poll would dip into the last 5 calls (or 5% of your daily limit if that is more), and resumes once the expected quota reset time has passed
- **Bootstrap Reserve**: with Quota Reserve Protection on, the last 3 API calls are held back for auto-recovery after the reset
- **Custom Intervals** — override with fixed intervals (1–1440 min)

**Computed by:** Tado CE — the adaptive interval formula, day/night schedule, quota reserve pausing, bootstrap reserve, and per-type refresh floors are all calculated locally by the integration. Quota headroom figures come from Tado server response headers, but every scheduling decision is made by Tado CE.

### How often each data type refreshes

Different kinds of data change at different speeds, so Tado CE refreshes them at different rates. A fast zone-state cadence no longer drags the slow-changing data along with it.

| Data | Refreshes | Why |
|------|-----------|-----|
| Zone temperature / mode | Your polling interval (adaptive or custom) | Changes minute-to-minute |
| Weather | Default every 30 min (configurable 15–120 min) | Outdoor weather moves slowly |
| Home presence (Home/Away) | Default every 5 min (configurable 1–60 min) | Changes only when you cross the geofence |
| Mobile device locations | Default every 5 min (configurable 1–60 min, when Frequent Mobile Sync is on) | Changes only when a phone crosses the geofence |
| Device firmware / battery / connection | Normally the first poll at least an hour after the last refresh of zone and device details (4 hours on the 100-call plan), later if the quota is nearly spent; sooner at startup, when a room is added or removed, and when a room with its external sensor in place is switched to Valve Target | Rarely changes |

When HomeKit local control is connected, these floors widen to your **Cloud Data Refresh** dial (Settings → Tado CE → Configure → Advanced Settings → HomeKit) if that's slower.

#### Home presence vs mobile device locations

These two sound like the same thing and people often mix them up, so it's worth being clear about which is which. They're two separate Tado endpoints that answer two different questions:

- **Home presence** (Home State Sync) answers *"is the home as a whole Home or Away right now?"* Tado works this out from everyone's phones together and gives you one Home/Away answer for the household. This is what drives the **Presence Mode** entity and Away Mode automations.
- **Mobile device locations** (Mobile Device Tracking) answers *"which individual phones are home right now?"* You get a `device_tracker` entity per phone, so you can tell who specifically is in or out, not just whether anyone is.

Think of the mobile locations as the raw input and home presence as the verdict Tado derives from them. They're separate toggles because you might want one without the other: just the household Home/Away verdict for heating automations (Home State Sync alone), or per-person trackers as well (add Mobile Device Tracking). Each is an independent cloud call, which is why they're listed separately above, and each only earns its keep if you've turned it on.

### What changed across versions, and what it saves you

The per-type floors are new in v4.0.2. Here's what a polling cycle actually costs in cloud calls, traced through each version, for a cloud-only setup with weather, home presence, and Frequent Mobile Sync all enabled.

If you're on the **default 30-minute day interval**, your call count barely moves between versions, because the slow-changing data was already only fetched every 30 minutes at that cadence. The change matters when your zones poll **faster** than 30 minutes, either because you set a fast custom interval or, on v4.0.1 / v4.1.0-beta.2 with a large quota, because the adaptive logic dropped to 1-minute polling on its own.

At a **1-minute zone cadence**, the difference is large, because before v4.0.2 the weather, presence, and mobile-location calls all rode that same 1-minute cadence:

| Data type | pre-v4.0.2 (rode the zone cadence) | v4.0.2 (own floor) |
|-----------|-----------------------------------|--------------------|
| Weather | ~960 calls/day | ~36 calls/day (30-min floor) |
| Home presence | ~960 calls/day | ~196 calls/day (5-min floor) |
| Mobile locations | ~960 calls/day | ~196 calls/day (5-min floor) |
| **Total auxiliary calls** | **~2,900 calls/day** | **~430 calls/day** |

That's the change [#289](https://github.com/hiall-fyi/tado_ce/issues/289) was about: a 1,000-call-tier home with HomeKit connected, where the zone temperatures came over HomeKit for free but the auxiliary calls kept firing every minute and drained the daily quota. Two things fixed it together. The auxiliary data now refreshes on its own slower floor regardless of how fast your zones poll, and the adaptive floor itself went back to a flat 5 minutes, so a healthy quota no longer pushes the cadence down to 1 minute in the first place. Zone temperatures, separately, have arrived locally over HomeKit since v4.0.0 if you've paired the bridge, which takes the single biggest call off the cycle entirely.

(The ~960 figures assume the fast cadence held all day; in practice on v4.0.1 the adaptive logic would widen the interval itself as quota ran low, so a real day landed somewhere below that. The point is the shape, not a guaranteed daily total.)

### Configuration

**Day/Night Schedule:**

| Option | Default | Description |
|--------|---------|-------------|
| Day Start Hour | 7 | When "day" period starts (0–23) |
| Night Start Hour | 23 | When "night" period starts (0–23) |
| Custom Day Interval | Empty | Fixed interval during day (1–1440 min). It applies in the day window only: with HomeKit connected and no Custom Night Interval, the night still uses the adaptive night interval (120 minutes by default), not Cloud Data Refresh |
| Custom Night Interval | Empty | Fixed interval during night (1–1440 min). It applies in the night window only, so the day keeps the automatic schedule |

**Optional Sensors (affect API usage):**

| Option | Default | API Cost |
|--------|---------|----------|
| Weather Data | Off | ~1 call per refresh (default every 30 min, configurable in Advanced Settings → Polling, Delays & Defaults) |
| Mobile Device Tracking | Off | 1 call with each refresh of zone and device details (at startup, then normally on the first poll at least an hour after the last one, or 4 hours after on the 100-call plan); with Frequent Mobile Sync on, ~1 call per refresh instead (default every 5 min, configurable in Advanced Settings → Polling, Delays & Defaults) and that larger refresh doesn't repeat it |
| Home Presence (Home/Away) | Off | Needed for the Presence Mode select (the Home sensor, the climate card, the Away and Home insights, Adaptive Preheat and the Preheat Advisor follow Tado's Home or Away answer without it); ~1 call per refresh (default every 5 min, configurable in Advanced Settings → Polling, Delays & Defaults) |

### How It Works

**Adaptive Polling:** Tado CE takes your remaining calls, keeps 10% back for manual calls and unexpected usage, takes off the 5-call reserve and the calls the larger refresh of zone and device details will need before the reset (including 2 for each room Tado reports that Tado CE has no details for yet), and divides what is left by the calls one poll costs. It spreads those polls over the day, with the night's polls set aside, and clamps the result between a 5-minute floor and a 120-minute ceiling.

The adaptive floor is a flat 5 minutes whatever your daily call limit. The maths already widens the interval on its own when quota is tight, so the floor is just the fast end: the point past which a faster cadence buys you nothing because zone temperature doesn't change that quickly. A bigger quota doesn't lower the floor, so if you genuinely want faster than 5 minutes, set a custom interval and it's honoured as long as your quota can sustain it. Presence, weather, and mobile device minimum waits are configurable separately in Advanced Settings → Polling, Delays & Defaults (v4.1.0, each behind its own toggle; defaults unchanged).

**Day/Night Aware:**
- Night period: Fixed 120 min interval to conserve quota (with HomeKit connected, never shorter than Cloud Data Refresh)
- Day period: Adaptive based on remaining quota after reserving night calls
- If Day Start == Night Start: always uses adaptive polling (24/7 mode), so there is no 120 minute night stretch, with or without HomeKit

**Quota Reserve:**
- Pauses polling when the next poll would leave you with no more than the reserve, which is the larger of 5 calls and 5% of your daily limit. Tado CE works out what that poll costs first, so polling stops one poll early instead of spending into the reserve
- A due refresh of zone and device details (normally due an hour after the last one, 4 hours on the 100-call plan) that wouldn't fit becomes a regular poll, and the refresh runs once the quota allows, at the latest after the reset. Battery and connection updates wait with it. The very first refresh on a new install runs regardless, because its cost isn't known yet
- The remaining-calls count it checks includes calls made since the last poll, so a change you make by hand while polling is paused counts against the reserve straight away
- The refresh's price includes 2 calls for each room Tado reports that Tado CE has no details for yet. A room added while Home Assistant was down is still priced 2 calls short on the first refresh after the restart, because the zone data saved before the restart doesn't show it
- Smart Valve Control, Offset Sync and Adaptive Preheat hold their automatic cloud writes once your remaining calls are down to the reserve plus what the write costs (1 call for a valve target or a preheat start; 1 per device in the zone plus 1 for the readback for an Offset Sync write), until the quota is expected to reset. Handing a zone back to Tado is held only at the last 3 calls (the Bootstrap Reserve), and is tried again after the expected reset. The log notes each hold once per quota day: one line for the three features, one for hand-backs. Both holds need Quota Reserve on
- Reserves quota for manual operations (set temperature, change mode)
- Auto-resumes when API reset time passes
- With HomeKit connected, your entities stay live on HomeKit during the pause, including when Home Assistant restarts during it (Tado CE starts on the data it saved before the restart). A change that goes over HomeKit is not a cloud call, so the last-3-calls reserve below does not stop it
- Without HomeKit, your entities stay available during the pause and keep the last values Tado CE fetched, so a change you make by hand still goes through until only 3 calls are left, and it stays on the card until polling resumes. Those values can be hours old, and mode, target and power are not hidden as they age (only the temperature and humidity of a device that was already offline are), so check the **Cloud Data Freshness** sensor to see how old they are. A change made by a service or button during the pause (Turn off all, Resume All Schedules, Restore) reaches Tado straight away but shows on the cards only when polling resumes. If Home Assistant restarts during the pause there is nothing to show yet, so the entities are unavailable until the quota resets
- Toggle: `switch.tado_ce_hub_quota_reserve` (default on). When disabled, polling and manual actions continue until the quota is used up, and the Bootstrap Reserve below is off too. Only disable Quota Reserve if you've sized your custom intervals against your tier's daily limit and don't want preemptive pausing.

**Bootstrap Reserve:**
- With Quota Reserve Protection on, the last 3 cloud API calls are held back from manual actions until the expected reset time has passed, as long as Tado CE knows your remaining calls and that reset time
- A change sent over HomeKit is not a cloud call, so this reserve does not stop it. A change that has to fall back to the cloud at that point is refused, and the card goes back to what Tado holds
- A held change that is owed to Tado after an outage (see [Writes while Tado's cloud is down](#writes-while-tados-cloud-is-down)) meets the same reserve as those automatic writers. Once your remaining calls are down to the reserve plus the 1 call it costs, it stays held and is tried again later, within its 24 hours
- When triggered: persistent notification "API limit reached. Use the Tado app for emergency changes."
- Auto-dismisses when API reset detected

**Reset time detection:**

Tado's `X-RateLimit-Reset` header often points at midnight UTC, which isn't when your quota actually rolls over, so the integration doesn't trust it on its own. It works out the real reset time from whatever evidence it has, in order of preference: it watches for the moment your remaining-calls count jumps back up and records that as the reset (a jump of more than 20 calls, or 5% of the limit on bigger plans, counts at any time, and from two hours before the expected reset time onwards (longer if the last reset was only spotted late) a rise of even one call counts, so a quiet day is caught too, at the price that a small wobble in the count just before the reset can be taken for it); failing that it reads your own sensor history for the usage drop, and uses it only when it shows a later reset than the one already recorded; then it extrapolates from your current usage rate and call history; and as a last resort it falls back to the time of your first call each day. The result is what `sensor.tado_ce_api_reset` shows, and it's what Quota Reserve waits for before resuming.

**Offset Sync drift refresh (v4.0.0):**

When Device Temperature Offsets is on, the integration runs a periodic drift refresh that pulls the stored device offset back from Tado, so the displayed `offset_celsius` stays close to the value Tado's adaptive calibration writes behind your back rather than freezing at the boot reading. It runs as part of a poll and never more than once every 30 minutes, or once per Cloud Data Refresh when HomeKit is connected and that setting is longer (see HomeKit Local Control). Without HomeKit it runs on the first poll 30 minutes or more after the last refresh, so it waits for the first poll at or after the 30 minutes. With HomeKit connected and the default settings it runs every 30 to 60 minutes by day and about every 120 minutes at night, and every 60 to 120 minutes by day if you widen Cloud Data Refresh to 60 minutes. A room on Valve Target (with its external sensor in place) keeps the night wait at 30 minutes, or Cloud Data Refresh if that is longer, unless you have set a custom night interval, which then applies.

From v4.1.0-beta.4 the refresh visits **one zone per cycle** rather than every zone at once. Each zone still gets refreshed, just spread out: with N zones the cycle rotates through them, so any one zone is re-read every N cycles. That turns the old all-at-once burst into a steady **1 cloud call per cycle**, whatever your zone count.

Example daily totals at the default 30 minute cadence (at most 48 cycles a day) on a 1,000-call or larger plan (the 100-call free plan is covered below):

- 4 zones: 48 calls/day, each zone refreshed every 2 hours
- 8 zones: 48 calls/day, each zone refreshed every 4 hours

Before v4.1.0-beta.4 the same 8-zone home cost 384 calls/day (8 every cycle). The per-zone interval is longer now, but that's fine: Tado's adaptive calibration drifts over hours to days, so a few hours between re-reads still catches it well within the window, and every write is still readback-checked the moment it happens. Widening Cloud Data Refresh to 60 minutes stretches both the cycle cadence and the per-zone interval further. With Quota Reserve on, the drift refresh is paused automatically when the remaining quota drops below 10% of your daily limit (never less than 100 calls), so it never eats the budget you keep for manual changes; with Quota Reserve off it keeps going below that line. On the 100-call free plan it never runs, whatever the Quota Reserve setting. Your device offsets are still re-read by the refresh of zone and device details, which runs at least 4 hours apart on that plan, and every write is still readback-checked.

The refresh log line in `homeassistant.log` names the zone it refreshed and the call count, so you can audit it directly:

```
Offset Sync: drift refresh complete — zone 5 reconciled with Tado, 1 cloud call(s) used this cycle
```

### Usage Scenarios

#### Scenario 1: Low Quota (100 calls/day) Day/Night Setup

```
Day Start: 7, Night Start: 23
Custom Day Interval: 30 min, Custom Night Interval: 120 min
Weather: Off, Mobile Tracking: Off, Home State Sync: Off

Expected: Day 32 polls × 1 call = 32, Night 4 polls × 1 call = 4, zone and device refresh (at least 4 h apart on this plan) at most 6 × 2 extra calls = 12 → up to ~48 calls/day
```

#### Scenario 2: High Quota (1000+) Adaptive

Leave custom intervals empty. Adaptive polling reads your remaining quota from Tado's response headers and picks an interval that fits the budget, down to the flat 5-minute floor when quota is healthy. The floor is the same on every plan: a bigger quota doesn't poll faster on its own, because your room temperature doesn't change any faster. Enable all optional sensors. Adaptive will widen the interval automatically if quota drops critically low, and if you genuinely want faster than 5 minutes, set a custom interval, which is honoured as long as your quota can sustain it. On a 20,000-call plan, you may also want to lower the presence and mobile minimum waits in Advanced Settings → Polling, Delays & Defaults for tighter geofencing latency.

#### Scenario 3: Disable Optional Sensors to Save Calls

- Weather off: saves ~144 calls/day (at 10 min intervals)
- Mobile tracking off: saves ~4 calls/day
- Home State Sync off: saves 1 call/sync

---

## API Write Optimization

**Requirement:** None | **All Enabled by Default**

Reduces unnecessary API calls when you interact with climate controls — temperature changes, mode switches, device toggles, and schedule resumes are all optimized automatically.

### Overview

Every time you adjust a temperature slider, toggle a switch, or resume a schedule, Tado CE sends API calls to the Tado cloud. Without optimization, rapid interactions (dragging a slider, toggling multiple devices) can waste dozens of calls on intermediate or redundant values. API Write Optimization tackles this with five complementary strategies that work together transparently.

**Computed by:** Tado CE — all five optimisation layers (debounce timer, action guard deduplication, device sync queue, refresh delay, resume guard) run entirely within the integration before any call leaves for Tado's servers. The `scheduled_target_temperature` attribute is read from Tado's schedule data and cached locally by Tado CE.

These layers cut calls. Heating and AC changes to one room are also sent to Tado one at a time in the order you made them, so a Resume Schedule can't overtake a temperature change that is still on its way. A heating change made while Tado's cloud is down is a separate case, see [Writes while Tado's cloud is down](#writes-while-tados-cloud-is-down).

![What happens before a change reaches Tado. A temperature, mode, fan, swing or preset change first meets the Action Guard, which skips it with no call if the zone is already in that state in zone data less than 1.5 times your Cloud Data Refresh setting old (older data is not trusted and the call is sent; a repeated preset is skipped whatever the age). A temperature change then waits in a 3 second debounce that restarts on each new change, so only the final value is sent. Child lock, early start and Offset Sync writes go through the Device Sync Queue, one at a time and 1 second apart by default, with up to 20 waiting; a switch you flip goes ahead of queued offset writes, and a full queue, or Tado being unreachable, refuses a new one. A resume schedule request is skipped with no call if the zone already follows its schedule in data that recent. After a change, a 15 second Refresh Delay restarts on each write, so quick changes give one refresh, and it is skipped only if that poll would pause for the quota or Tado rate limits you. On hot water, a repeated temperature or Off that changes nothing is skipped too. Smart Valve Control and Offset Sync keep their own fixed 3 second window.](docs/images/tado-ce-fewer-api-calls.png)

### How Much Does It Save?

The savings depend on how you use your system:

| Scenario | Without Optimization | With Optimization | Savings |
|----------|---------------------|-------------------|---------|
| Drag temperature slider from 18°C to 22°C | Up to ~8 API calls if every 0.5°C step were sent | 1 API call (final value only) | Up to ~87% |
| Set 22°C when already at 22°C | 1 API call | 0 API calls (skipped) | 100% |
| Toggle child lock + early start quickly | 2 calls sent at the same moment | 2 sequential calls with delay | Same number of calls, one at a time |
| Change temp + mode + fan in quick succession | 3 refreshes | 1 refresh after the delay | ~66% fewer refreshes |
| Resume schedule when already on schedule | 1 API call | 0 API calls (skipped) | 100% |

How much this saves depends on how many of your writes are duplicates or part of a rapid burst, so it varies by household.

### Components

#### 1. Smart Actions Debounce

When you drag a temperature slider, each position fires a `set_temperature` call. Smart Actions waits for you to stop adjusting before sending the final value.

| Setting | Value |
|---------|-------|
| Default | 3 seconds |
| Range | 0–10 seconds |
| Disable | Set to 0 |

**Where to configure:** Settings → Tado CE → Configure → Advanced Settings → Polling, Delays & Defaults

**How it works:** Each slider movement resets a per-zone timer. Only when the timer expires (no new movement for N seconds) does the API call fire. If you drag from 18°C to 22°C over 2 seconds, only one call is made for 22°C.

This covers temperature changes on heating and AC zones. Smart Valve Control and Offset Sync have their own fixed 3-second window, which this setting doesn't change.

#### 2. Action Guard

Skips API calls when the requested state already matches the current state. If your zone is already at 22°C and you (or an automation) sets it to 22°C again, the call is silently skipped.

| Setting | Value |
|---------|-------|
| Behaviour | Always active |
| Covers | Temperature, HVAC mode, fan mode, vertical swing, horizontal swing, preset mode, hot water temperature and Off |

**Checked states:** temperature, HVAC mode, fan mode, vertical swing, horizontal swing, and preset mode (fan and swing on AC zones, preset on heating zones). Each is compared against the state the climate entity is showing, so the check needs no API call.

A call is skipped only while the zone data is recent: less than 1.5 times your **Cloud Data Refresh** setting old (45 minutes at the default 30). This age rule covers every state above except preset mode, where a repeat is skipped whatever the age of the data. Older data, or no data yet, is not trusted and the call is sent, so repeating a value after a change made in the Tado app puts it back. The call then refreshes the zone, and the next identical request is skipped again. An automation that repeats the same value every minute overnight sends roughly 10 calls a night per zone at the default, and roughly 60 with Cloud Data Refresh at 5. Homes without HomeKit use the same window, so they send until the next refresh lands.

Two cases are never skipped: a temperature or mode change made while an earlier change of yours is still waiting for Tado to confirm it, and switching to Auto on a zone that has an override, since that is how you hand the zone back to its schedule.

**Hot water** is guarded more narrowly, because its shown target is the schedule's while the zone follows its schedule, and setting the same temperature then turns it into a manual hold. A hot water temperature is skipped only when the zone is already on a manual hold at that temperature, and Off only when the zone is already off on a manual hold. A temperature set while the zone follows its schedule, or while a timer or Tado mode hold is running, is sent, and so is an Off during a timed Off. Heat and the timer are never skipped, because they start a new hold.

While polling is paused for the Quota Reserve, no guard skips anything, and Resume Schedule and Auto are always sent. The values Tado CE holds during a pause can be hours old, so a skip could drop a change you made elsewhere. Each such call costs one API call, and Quota Reserve still stops manual actions at the last 3 calls.

#### 3. Device Sync Queue

Device-level operations (child lock, early start, and the offset writes Offset Sync makes) are queued and executed one at a time, with a configurable delay between each. The ones you start by hand (the child lock and early start switches) run before the offset writes Offset Sync queued, so a switch waits only for the operation already running, and the order is first in first out within each group. The first operation in a batch goes straight away. If the queue already holds 20 operations, a new one is refused with a warning in the log and a switch goes back to its previous position.

| Setting | Value |
|---------|-------|
| Default delay | 1 second |
| Range | 0.5–5 seconds |
| Max queue depth | 20 operations |

**Where to configure:** Settings → Tado CE → Configure → Advanced Settings → Polling, Delays & Defaults

**Why it matters:** Toggling several device settings at once would otherwise send their calls to Tado at the same moment. The queue makes each operation finish before the next one starts, and one failing doesn't stop the rest.

**While Tado can't be reached:** when Tado is rate limiting you, blocking requests or unreachable, the queue refuses an operation at once instead of sending it. A Child Lock or Early Start switch goes back to its previous position straight away (it used to wait up to about 90 seconds for three timed-out tries), and the log gets a warning. No error message shows on screen. A sign-in problem isn't included, those operations are still tried. Unloading Tado CE releases every operation still waiting in the queue, so no switch is left waiting for one that never starts.

#### 4. Refresh Delay After a Change

When multiple state changes happen in quick succession (e.g. changing temperature, mode, and fan within a few seconds), each change would normally trigger its own coordinator refresh. Tado CE instead waits for the **Refresh Delay After a Change** (15 seconds by default, 1 to 60 under Configure → Advanced Settings → Polling, Delays & Defaults) and then fetches fresh data once.

It only merges the refreshes. Each write still goes to Tado as its own call, so a mode change and a temperature change sent back to back from an automation are two overlay writes. To send one, use a single `climate.set_temperature` call with `hvac_mode` set.

Each new write restarts the countdown, and the refresh only fires once it runs out with no new writes. So 3 rapid changes = 1 refresh instead of 3.

Only changes to climate, switch, water heater and select entities trigger a refresh. It is skipped, and the next normal poll picks the change up, when the refresh would take your remaining calls into the quota reserve (with Quota Reserve on), or while Tado is rate limiting you, blocking requests or unreachable. Two refreshes are also kept at least 2 seconds apart, and that gap doubles after each failed refresh, up to 5 minutes.

#### 5. Resume Guard

Skips the resume API call (`resume_climate_schedule` or `resume_water_heater_schedule`) if the zone is already following its schedule (no active overlay) in overlay data that is recent: less than 1.5 times your **Cloud Data Refresh** setting old (45 minutes at the default 30). Older data, or no data yet, is not trusted and the call is sent, so an override made in the Tado app overnight is still cleared. It reads the overlay state Tado CE already holds, so the check costs no API call. It applies to the Resume Schedule services and buttons and to hot water, not to switching a climate entity to Auto.

| Setting | Value |
|---------|-------|
| Behaviour | Always active |

Useful when automations call `resume_climate_schedule` or `resume_water_heater_schedule` as a safety measure: if the zone is on schedule and the data is recent, the call is free. When the data is older, the first call for a room goes out, and the refresh it triggers makes the next call free again.

The call is never skipped while a change to that zone is still being sent or Tado has not confirmed it (a change that timed out, for example), because what Tado CE holds for the zone may be older than that change, so the resume goes out.

### Schedule Preview

Heating and AC climate entities now include a `scheduled_target_temperature` attribute showing the current schedule target. This lets you see what temperature the zone would be at without an overlay, useful for dashboard cards and automations.

| Attribute | Description |
|-----------|-------------|
| `scheduled_target_temperature` | Target temperature from the active schedule block (°C), or `None` if heating is OFF in the current block |

**Example automation — alert when overlay differs significantly from schedule:**

```yaml
automation:
  - alias: "Alert: Large Schedule Override"
    trigger:
      - platform: template
        value_template: >
          {% set sched = state_attr('climate.living_room', 'scheduled_target_temperature') %}
          {% set current = state_attr('climate.living_room', 'temperature') %}
          {{ sched is not none and current is not none and (current - sched) | abs > 3 }}
    action:
      - service: notify.mobile_app
        data:
          message: >
            Living room is set to {{ state_attr('climate.living_room', 'temperature') }}°C
            but the schedule says {{ state_attr('climate.living_room', 'scheduled_target_temperature') }}°C
```

### Switch the Active Timetable

Every Tado room has three timetables, each with its own time blocks, and one of them is active. The Tado app calls them Mon-Sun, Mon-Fri / Sat / Sun, and each day separately. `tado_ce.set_climate_active_timetable` switches the active one from Home Assistant, for one or several heating or AC rooms in one call.

| `timetable` | Tado app name |
|-------------|---------------|
| `one_day` | Mon-Sun |
| `three_day` | Mon-Fri, Sat, Sun |
| `seven_day` | Each day separately |

Heating and AC climate entities show the active one in an `active_timetable` attribute (`one_day`, `three_day` or `seven_day`), once Tado CE has loaded that room's schedule.

If the room's schedule was already loaded, Tado CE reloads it from Tado after the switch, so `scheduled_target_temperature` and the next scheduled change come from the new timetable. If Tado doesn't confirm the switch, the room keeps its current timetable and the call fails with an error that names the room.

The switch costs one cloud call per room, or 3, 5 or 9 calls (for `one_day`, `three_day` or `seven_day`) when the room's schedule was already loaded, because Tado CE then reloads it. With Quota Reserve on, the call is refused with the quota error when it would leave fewer than 3 calls for the day.

**Example automation: use a spare timetable on public holidays:**

```yaml
automation:
  - alias: "Holiday timetable on"
    trigger:
      - platform: time
        at: "00:05:00"
    condition:
      - condition: state
        entity_id: calendar.public_holidays
        state: "on"
    action:
      - service: tado_ce.set_climate_active_timetable
        target:
          entity_id:
            - climate.living_room
            - climate.bedroom
        data:
          timetable: one_day
```

Switch back the next day with a second automation that sets the timetable you normally use.

### Configuration Summary

All settings are under **Settings → Tado CE → Configure → Advanced Settings → Polling, Delays & Defaults**:

| Setting | Default | Range | Notes |
|---------|---------|-------|-------|
| Smart Actions debounce window | 3s | 0–10s | Set to 0 to disable |
| Action Guard | Always on | — | No configuration needed |
| Device Sync delay | 1s | 0.5–5s | Lower = faster but riskier, higher = safer |
| Refresh Delay After a Change | 15s | 1–60s | How long to wait before fetching fresh data after a change |
| Resume Guard | Always on | — | No configuration needed |

---

## Thermal Analytics

**Requirement:** Zones with heatingPower data (TRV or Smart Thermostat) | **Toggle:** off by default on new installs, see Configuration below

Real-time analysis of heating system thermal performance based on complete heating cycles.

### Sensors

| Sensor | Friendly Name | Unit | Description |
|--------|--------------|------|-------------|
| `sensor.{zone}_thermal_inertia` | Thermal Inertia | minutes | Time constant for temperature changes |
| `sensor.{zone}_avg_heating_rate` | Heating Rate | °C/h | Average heating rate when heating ON |
| `sensor.{zone}_preheat_time` | Preheat Time | minutes | Estimated time to reach target |
| `sensor.{zone}_analysis_confidence` | Confidence | % | Confidence score for analysis |
| `sensor.{zone}_heating_acceleration` | Heat Accel | °C/h² | Rate of change of heating rate |
| `sensor.{zone}_approach_factor` | Approach Factor | %/hour | How quickly zone approaches target |

### Sensor Interpretation

**Thermal Inertia:**
- Low (10–30 min): Room heats/cools quickly — may indicate poor insulation or small room
- Medium (30–60 min): Typical for most rooms
- High (60+ min): Good insulation or large thermal mass

**Heating Rate:**
- Slow (<0.6°C/h): Possible radiator, boiler, or insulation issues
- Normal (0.6–1.8°C/h): Typical
- Fast (>1.8°C/h): Small room or oversized radiator

**Analysis Confidence:**
- Below 30%: Low, it needs more complete heating cycles
- 30–70%: Moderate, more cycles will improve preheat accuracy
- 70% and above: Adequate

**Approach Factor:**
- Below 100%: Heating slows down near the setpoint (normal, controlled approach)
- Around 100%: Constant rate, so the room may overshoot
- Above 100%: Heating speeds up near the setpoint (unusual, may overshoot)

### Configuration

**Global Toggle:** Options → General Settings → Smart Automations → Thermal Analytics

**Per-Zone Control:** Options → Advanced Settings → Thermal Analytics → Thermal Analytics Zones
- Default: All zones with heatingPower data enabled
- Deselect zones that never call for heat (passive heating) to keep UI clean

**Supported Devices:** TRV (VA01, VA02, RU01, RU02), Smart Thermostat (SU02)

**Analysis Tuning:** Options → Advanced Settings → Thermal Analytics (same collapsed section as Thermal Analytics Zones)

| Setting | Default | Range | Description |
|---------|---------|-------|--------------|
| Heating Cycle History (days) | 7 | 1–30 | How many days of completed heating cycles feed the rolling analysis |
| Minimum Heating Cycles | 3 | 1–10 | Completed cycles required before Thermal Inertia / Heating Rate are considered reliable |
| Inertia Threshold (°C) | 0.1 | 0.05–0.5 | Temperature rise that counts as the start of a heating response, for Thermal Inertia timing |

### Usage Scenarios

#### Scenario 1: Optimize Preheat Timing

```yaml
automation:
  - alias: "Bedroom Preheat"
    trigger:
      - platform: time
        at: "17:30:00"
    condition:
      - condition: numeric_state
        entity_id: sensor.bedroom_preheat_time
        above: 20
    action:
      - service: climate.set_temperature
        target:
          entity_id: climate.bedroom
        data:
          temperature: 21
```

Wait for `sensor.{zone}_confidence` to reach 70% or more before trusting preheat estimates.

**Computed by:** Tado CE — derived from complete heating cycles detected locally; Tado server supplies the raw heatingPower and temperature readings.

#### Scenario 2: Detect Insulation Issues

**Indicators:**
- Low thermal inertia (<20 min) — heat escapes quickly
- Low heating rate (<0.6°C/h) — struggling to heat
- Approach factor above 100%: heating speeds up near the setpoint, so the room may overshoot

**Action:** Check for drafts, verify radiator valve, check boiler flow temperature.

#### Scenario 3: Compare Room Performance

| Room | Thermal Inertia | Heating Rate | Confidence | Status |
|------|----------------|--------------|------------|--------|
| Living Room | 45 min | 1.2°C/h | 95% | Normal |
| Bedroom | 35 min | 1.5°C/h | 90% | Good |
| Bathroom | 15 min | 0.6°C/h | 85% | Poor insulation |
| Kitchen | 60 min | 0.8°C/h | 80% | High thermal mass |

#### Scenario 4: Detect Radiator/Boiler Problems

Watch for sudden drops in heating rate or increases in preheat time. Check radiator valve, bleed radiators, check boiler flow temperature, verify TRV battery.

---

## Smart Comfort Analytics

**Available:** v1.9.0+ | **Requirement:** None | **Opt-in Configuration**

Learns from heating patterns and provides predictive insights.

**Computed by:** Tado CE — schedule data and temperature history come from the Tado server; deviation, preheat timing, and comfort targets are all calculated locally by this integration.

### Sensors

| Sensor | Friendly Name | Unit | Description |
|--------|--------------|------|-------------|
| `sensor.{zone}_historical_deviation` | Schedule Deviation | °C | Difference from 7-day average at same time |
| `sensor.{zone}_next_schedule_time` | Next Schedule | timestamp | When next schedule change occurs |
| `sensor.{zone}_next_schedule_temp` | Next Sched Temp | °C | Target for next schedule block |
| `sensor.{zone}_preheat_advisor` | Preheat Advisor | minutes | Recommended preheat start time |
| `sensor.{zone}_smart_comfort_target` | Comfort Target | °C | AI-recommended target temperature |

### Preheat Cooling Rate Prediction

The Preheat Advisor now considers cooling trends when the room is above the target temperature. Previously, if `current_temp >= target_temp`, it simply showed "Ready" — now it estimates when the temperature will drop below target and calculates a proactive preheat start time.

**How it works:**
1. `estimate_cooling_crossover()` calculates hours until temperature crosses below target using the current cooling rate
2. If a crossover is predicted, the advisor calculates when to start preheating to prevent undershoot
3. Heating rate data (from thermal analytics) is used to determine how long preheat needs

**Preheat Advisor attributes (when cooling prediction active):**

| Attribute | Description |
|-----------|-------------|
| `cooling_rate` | Current cooling rate in °C/h (negative value) |
| `predicted_crossover_time` | Estimated time when temp will drop below target |
| `is_cooling_prediction` | `true` when cooling prediction is active |
| `summary` | Human-readable explanation of the prediction |

### Adaptive Preheat Passive Mode

Preheat now has three modes per zone, configured via **Options Flow → Zone Configuration**:

| Mode | Behaviour |
|------|----------|
| Off | Preheat disabled for this zone |
| Active | Always triggers preheat before the next schedule change, except on a zone you've switched off in the Tado app |
| Passive | Only triggers when the zone is following its schedule — skips preheat if you've set a manual override from HomeKit, the Tado app, etc. |

A zone you've switched off in the Tado app is left off in both modes: preheat starts nothing on it until you switch it on again. Existing users with preheat enabled are automatically migrated to Active mode. Passive mode is useful if you frequently override temperatures manually and don't want preheat fighting your changes.

**When Tado refuses the start:** Adaptive Preheat tries at most 3 times per preheat window, each retry after Tado's next zone update, then logs that the preheat is skipped. It sends one start request per zone at a time, a timer countdown on the zone's overlay doesn't cancel a start that is waiting, and a start held back by the quota reserve or a Tado Cloud backoff doesn't use up a try. A rate limit or sign-in error on the start request starts the same backoff or sign-in repair as Tado CE's other background writes.

### Configuration

1. Settings → Devices & Services → Tado CE → Configure → **General Settings → Smart Automations**
2. Enable **Smart Comfort**
3. Set **Temperature History days** (1–30) under **Advanced Settings → Smart Comfort**
4. Optionally bind an **Outdoor Temperature Entity** under **Advanced Settings → External Data Sources**

**Heat and cold alerts for vulnerable occupants** (v4.2.0) live under Advanced Settings, in their own Comfort & Safety section (you don't need Smart Comfort on to use them). For homes with elderly, infant, or unwell occupants, the option raises both heat and cold warnings one level earlier, since the same temperature carries more risk. Cold alerts step up when a room with the heating off drops into a health-risk range (high below 16°C, critical below 12°C). Off by default.

### Usage Scenarios

#### Scenario 1: Detect Unusual Patterns

```yaml
automation:
  - alias: "Alert: Room Colder Than Usual"
    trigger:
      - platform: numeric_state
        entity_id: sensor.bedroom_historical_deviation
        below: -2.0
    action:
      - service: notify.mobile_app
        data:
          message: "Bedroom is 2°C colder than usual — check for open windows"
```

#### Scenario 2: Automatic Preheat

```yaml
automation:
  - alias: "Smart Preheat Before Schedule"
    trigger:
      - platform: template
        value_template: >
          {{ now().timestamp() >= 
             (state_attr('sensor.bedroom_next_schedule_time', 'timestamp') - 
              (states('sensor.bedroom_preheat_advisor') | int * 60)) }}
    action:
      - service: climate.set_temperature
        target:
          entity_id: climate.bedroom
        data:
          temperature: "{{ states('sensor.bedroom_next_schedule_temp') }}"
```

#### Scenario 3: Energy Optimization

```yaml
automation:
  - alias: "Reduce Heating When Warmer Than Usual"
    trigger:
      - platform: numeric_state
        entity_id: sensor.living_room_historical_deviation
        above: 1.0
    condition:
      - condition: state
        entity_id: climate.living_room
        state: "heat"
    action:
      - service: climate.set_temperature
        target:
          entity_id: climate.living_room
        data:
          temperature: >
            {{ state_attr('climate.living_room', 'temperature') - 0.5 }}
```

---

## Enhanced Mold Risk Assessment

**Requirement:** Outdoor temperature sensor | **Always Enabled**

Uses surface temperature calculation to accurately detect cold spots where mold can grow.

**Computed by:** Tado CE — room temperature and humidity come from the Tado server (and outdoor temperature from a separate HA entity); surface temperature, dew point, mold risk percentage, heat index, and comfort level are all calculated locally by this integration.

### Sensors

| Sensor | Friendly Name | Description |
|--------|--------------|-------------|
| `sensor.{zone}_mold_risk` | Mold Risk | Risk level: Low / Medium / High / Critical |
| `sensor.{zone}_mold_risk_pct` | Mold Risk Indicator | Numeric risk percentage (surface relative humidity) |
| `sensor.{zone}_condensation_risk` | Condensation | Condensation risk (AC zones) |
| `sensor.{zone}_surface_temperature` | Surface Temp | Calculated surface temperature |
| `sensor.{zone}_dew_point` | Dew Point | Dew point temperature |
| `sensor.{zone}_absolute_humidity` | Absolute Humidity | Water vapour mass per air volume (g/m³) |
| `sensor.{zone}_comfort_level` | Comfort Level | Overall comfort assessment |

### Heat Index & Heat Risk

The Comfort Level sensor now includes "feels like" temperature when it's warm (above 26.7°C), factoring in humidity using the NOAA/NWS Rothfusz regression. Risk levels appear in the sensor attributes and in comfort recommendations.

**Comfort Level extra attributes (when heat index is active):**

| Attribute | Description |
|-----------|-------------|
| `heat_index` | Calculated "feels like" temperature (°C) |
| `heat_risk_level` | NOAA risk level (see table below) |

**Heat Risk Levels:**

| Risk Level | Heat Index | Guidance |
|------------|-----------|----------|
| None | Below 26.7°C | No heat risk |
| Caution | 26.7–32°C | Fatigue possible with prolonged exposure |
| Extreme Caution | 32–39°C | Heat cramps and exhaustion possible |
| Danger | 39–51°C | Heat cramps/exhaustion likely, heatstroke possible |
| Extreme Danger | Above 51°C | Heatstroke highly likely |

When heat index is active, the comfort level calculation uses the "feels like" temperature instead of the raw air temperature for deviation and recommendation logic.

### Window Type Settings

| Window Type | U-Value (W/m²K) | Mold Risk |
|-------------|-----------------|-----------|
| Single Pane | 5.0 | High |
| Double Pane | 2.7 | Medium |
| Triple Pane | 1.0 | Low |
| Passive House | 0.8 | Very Low |

### Mold Risk Thresholds

| Risk Level | Surface RH | Action |
|------------|-----------|--------|
| Low | <60% | None — safe |
| Medium | 60–70% | Monitor, increase ventilation |
| High | 70–80% | Action needed, increase heating |
| Critical | >80% | Urgent — mold growth likely |

### Configuration

1. Settings → Devices & Services → Tado CE → Configure → **Advanced Settings → Smart Comfort**
2. Set the default **Window Type** (applies to any zone that hasn't picked its own)
3. Set the **Outdoor Temperature Entity** (e.g., `weather.home`) under **Advanced Settings → External Data Sources**

**Per-Zone Window Type:** For per-zone overrides, enable **General Settings → Advanced → Per-Zone Configuration**, then open **Zone Configuration → (zone) → Comfort → Window Type**.

### Usage Scenarios

#### Scenario 1: Prevent Mold Growth

```yaml
automation:
  - alias: "Alert: High Mold Risk"
    trigger:
      - platform: numeric_state
        entity_id: sensor.bedroom_mold_risk_percentage
        above: 70
        for:
          minutes: 30
    action:
      - service: notify.mobile_app
        data:
          title: "High Mold Risk in Bedroom"
          message: >
            Mold risk: {{ states('sensor.bedroom_mold_risk_percentage') }}%
            Surface temp: {{ state_attr('sensor.bedroom_mold_risk', 'surface_temperature') }}°C
            Action: Increase heating or ventilate
```

#### Scenario 2: Window Upgrade ROI

| Scenario | Indoor | Outdoor | Single Pane | Double Pane | Triple Pane |
|----------|--------|---------|-------------|-------------|-------------|
| Winter | 20°C | 0°C | 12.0°C (85% RH) | 15.4°C (72% RH) | 17.8°C (58% RH) |
| Cold Day | 20°C | 5°C | 14.5°C (78% RH) | 16.7°C (65% RH) | 18.5°C (55% RH) |
| Mild Day | 20°C | 10°C | 16.5°C (68% RH) | 17.8°C (58% RH) | 19.0°C (52% RH) |

---

## Heating Cycle Detection

**Requirement:** Zones with heatingPower data | **Always Enabled**

Identifies complete heating cycles (heating ON → target reached → heating OFF) for accurate thermal analysis.

- Automatically detects complete heating cycles
- A cycle that runs longer than 6 hours is marked interrupted
- No configuration needed

**Monitor cycle health** via `sensor.{zone}_thermal_inertia` attributes:
- `cycle_count`: Total cycles analyzed
- `completed_count`: Cycles used in the analysis

**Computed by:** Tado CE — heatingPower state is polled from the Tado server; cycle boundaries (ON → target reached → OFF) are identified and stored locally by this integration.

---

## Enhanced Controls

**Available:** v1.0.0+ | **Requirement:** None | **Always Enabled**

Improved responsiveness and convenience features for climate control.

**Computed by:** Tado CE — all actions in this section are write operations or integration-local state (Smart Boost duration, saved overlay snapshots, group expansion) executed by this integration against the Tado cloud API.

### Features

#### 1. Immediate Refresh

Dashboard updates immediately after climate control actions. Configurable debounce delay (default: 15s, range: 1–60s).

#### 2. Boost & Smart Boost

| Button | Friendly Name | Description |
|--------|--------------|-------------|
| `button.{zone}_boost` | Boost | Quick boost: 25°C for 30 min (replicates Tado app feature) |
| `button.{zone}_smart_boost` | Smart Boost | Intelligent duration based on thermal analytics |

> Boost replicates the Tado app's boost feature. The built-in HA integration doesn't expose this.
> Smart Boost is CE exclusive — calculates optimal duration from thermal data.

#### 3. Climate Timer Service

```yaml
# Timer mode — boost for specific duration
service: tado_ce.set_climate_timer
target:
  entity_id: climate.living_room
data:
  temperature: 22
  time_period: "01:00:00"

# Overlay mode — until next schedule change
service: tado_ce.set_climate_timer
target:
  entity_id: climate.living_room
data:
  temperature: 22
  overlay: next_time_block

# Overlay mode — indefinite
service: tado_ce.set_climate_timer
target:
  entity_id: climate.living_room
data:
  temperature: 22
  overlay: manual
```

> `time_period` is optional when `overlay` is specified. Supported: `next_time_block`, `manual`. Both Heating and AC zones.

#### 4. Enhanced Hot Water Timer

Control hot water with AUTO/HEAT/OFF modes. Timer duration configurable (1–720 min, default: 60).

```yaml
service: tado_ce.set_water_heater_timer
target:
  entity_id: water_heater.hot_water
data:
  time_period: "01:00:00"
```

On a temperature-controlled tank (solar or cylinder systems, where you set a target rather than a plain on/off), switching to Heat comes back at the temperature you last set, say 52°C, rather than jumping to the tank's maximum. That target is kept even after the tank has been off overnight and across a Home Assistant restart. The temperature control also shows on the entity as soon as the tank is known to support it. On/off-only combi tanks don't take a target and are unaffected.

#### 5. Temperature Offset

Calibrate device temperature readings (range: -10°C to +10°C).

```yaml
service: tado_ce.set_climate_temperature_offset
target:
  entity_id: climate.bedroom
data:
  offset: -0.5
```

The service takes a zone, a list of zones, or an area, device, floor or label like the other zone services, and writes the offset to each zone. The zones are written side by side, and if one fails the others keep the new offset and the call reports the first failure. Each zone costs one call per TRV plus one to read the value back, so an area of 8 zones with 2 TRVs each costs 24 calls, and the Quota Reserve is checked for each zone, so a zone is refused with a quota error once the reserve runs out. `tado_ce.set_climate_away_configuration` works the same way, with one call per zone.

`tado_ce.get_climate_temperature_offset` shows the same target picker and answers for every zone it reaches, with the offset under the entity ID, for example `climate.living_room: {offset: 0.5}`. Read it in a script with `response_variable`.

Enable "Temperature Offset" in Configure to add the `offset_celsius` attribute to heating climate entities. AC rooms don't have it, and Tado CE doesn't read their offset.

#### 6. Climate Group Support

Target climate groups with Tado CE custom services. Groups defined in `configuration.yaml` are automatically expanded.

```yaml
# Define group
group:
  tado_group:
    name: Tado TVR
    entities:
      - climate.bedroom
      - climate.living_room

# Use with Tado CE services
service: tado_ce.set_climate_timer
data:
  entity_id: group.tado_group
  temperature: 22
  time_period: "01:30:00"

# Resume schedule for all heating and AC rooms in the group
service: tado_ce.resume_climate_schedule
data:
  entity_id: group.tado_group
```

Supported services: `set_climate_timer`, `set_climate_schedule_temperature`, `set_water_heater_timer`, `resume_climate_schedule`, `resume_water_heater_schedule`, `set_climate_active_timetable`, `activate_climate_open_window`, `deactivate_climate_open_window`, `set_climate_open_window_mode`, `restore_climate_previous_state` and `restore_water_heater_previous_state`.

These services also take an area, a device, a floor or a label as the target, the same way Home Assistant's own services do:

```yaml
service: tado_ce.resume_climate_schedule
target:
  area_id: living_room
```

Only Tado CE zones of the type the service works on are used, so a climate entity from another integration in the same area is left alone, and `set_water_heater_timer` ignores the heating zones in the area. Entities you have hidden in Home Assistant are skipped when they are reached through an area, device, floor or label, and still used when you list them by `entity_id`.

A target that is not a Tado CE zone of the right type, or a zone that is unavailable, disabled or still loading, is skipped with a line in the Home Assistant log, and the other zones in the call are still written. A call that reaches no zone at all writes nothing and logs the same line. `entity_id: all` reaches every loaded zone of that type in every home. Members of a group that belong to another type (a light in a group of heaters) are left out.

The zones in one call are written side by side, so the order is not defined, and the Quota Reserve is checked for each zone. If several zones fail, Home Assistant reports the first failure and the zones that worked keep their change.

##### Three kinds of Tado CE service

- **Zone services** work on one kind of zone and are named `<verb>_<kind of zone>_<thing>`: `climate` for heating and AC rooms, `water_heater` for hot water. Examples are `set_climate_timer`, `set_water_heater_timer`, `resume_climate_schedule` and `resume_water_heater_schedule`. They take a target, so everything above applies. Home Assistant lets one service name serve one kind of entity, which is why resume and restore each exist once for climate and once for hot water.
- **`turn_off_all_climate_zones`** takes no target. It turns off every heating and AC room of one home, so with more than one home set up you get an error asking you to pick one.
- **Home and device services** are `add_meter_reading`, `identify_device`, `get_flow_temperature_settings` and `refresh`. They work on the home or on a device, not on a zone, so they keep their plain names and have no zone target.

#### 7. Open Window Services

Three services for open window management, each serving a different purpose:

| Service | Purpose | When to Use |
|---------|---------|-------------|
| `tado_ce.activate_climate_open_window` | Confirm Tado's own detection | Auto-Assist replacement — Tado has already detected an open window |
| `tado_ce.deactivate_climate_open_window` | Cancel Tado's *detected* open-window state | After `activate_climate_open_window`, or to clear an open window Tado detected itself. Does **not** clear `set_climate_open_window_mode` (see below) |
| `tado_ce.set_climate_open_window_mode` | Trigger from external sensors | Zigbee/Z-Wave contact sensors — no Tado detection needed. Clear it with `resume_climate_schedule` or `restore_climate_previous_state`, not `deactivate_climate_open_window` |

**Set Open Window Mode** is the most useful for automations. It sets the zone to frost protection (5°C) with a timer:

```yaml
# Basic — uses zone's Open Window Detection timeout, or 15 min default
service: tado_ce.set_climate_open_window_mode
target:
  entity_id: climate.bedroom

# Custom time period (hours:minutes:seconds, 1 minute to 1 hour)
service: tado_ce.set_climate_open_window_mode
target:
  entity_id: climate.bedroom
data:
  time_period: "00:30:00"

# Indefinite, stays until manually resumed
service: tado_ce.set_climate_open_window_mode
target:
  entity_id: climate.bedroom
data:
  time_period: "00:00:00"
```

Write `time_period` as text in quotes, the way the two timer services take it. A bare number such as `1800` or `90` is refused with an error, because it is easy to mistake for minutes.

**Time period priority:** user-provided `time_period` > zone's `openWindowDetection.timeoutInSeconds` > 15 min default.

**Automation example — contact sensor triggers open window mode:**

```yaml
automation:
  - alias: "Open Window — Bedroom"
    trigger:
      - platform: state
        entity_id: binary_sensor.bedroom_window_contact
        to: "on"
    action:
      - service: tado_ce.set_climate_open_window_mode
        target:
          entity_id: climate.bedroom
        data:
          time_period: "00:00:00"  # indefinite until window closes

  - alias: "Close Window — Bedroom"
    trigger:
      - platform: state
        entity_id: binary_sensor.bedroom_window_contact
        to: "off"
    action:
      - service: tado_ce.resume_climate_schedule
        target:
          entity_id: climate.bedroom
```

`set_climate_open_window_mode` works by writing a frost-protection overlay, so you clear it the same way you clear any overlay: `resume_climate_schedule` (back to the schedule) or `restore_climate_previous_state` (back to whatever the zone was doing before). `deactivate_climate_open_window` is the wrong partner here — it only cancels Tado's own detected open-window state (what `activate_climate_open_window` confirms), and it has nothing to act on after `set_climate_open_window_mode`.

#### 8. Restore Previous State

Puts a zone back to whatever it was doing before a change, or before the first of a run of changes. Works with heating, AC, and hot water.

State is saved automatically the moment an overlay action lands successfully (timers, temperature changes, open window mode, preheat, and `turn_off_all_climate_zones` for each heating and AC room). If the cloud rejects or times out, nothing is saved, so a failed write doesn't leave a stale snapshot to replay later. If the zone already has a saved state, that one is kept, so a run of changes restores to the state before the first of them. If no state was ever saved for the zone, restore falls back to resuming the schedule. Saved state survives HA restarts and clears when you arrive home.

```yaml
# Restore a single zone
service: tado_ce.restore_climate_previous_state
target:
  entity_id: climate.bedroom

# Restore multiple zones
service: tado_ce.restore_climate_previous_state
target:
  entity_id:
    - climate.bedroom
    - climate.living_room
```

**Pairs well with open window mode:**

```yaml
automation:
  - alias: "Close Window — Restore State"
    trigger:
      - platform: state
        entity_id: binary_sensor.bedroom_window_contact
        to: "off"
    action:
      - service: tado_ce.restore_climate_previous_state
        target:
          entity_id: climate.bedroom
```

> **Community Blueprint:** [@jeverley](https://github.com/jeverley) built a full [Window Mode Blueprint](https://raw.githubusercontent.com/jeverley/home-assistant-blueprints/refs/heads/main/blueprints/automation/tado_ce_window_mode_sensors.yaml) that handles multiple window/door sensors per zone with separate delays for nearby openings. [Import it directly](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fjeverley%2Fhome-assistant-blueprints%2Frefs%2Fheads%2Fmain%2Fblueprints%2Fautomation%2Ftado_ce_window_mode_sensors.yaml) or use the examples above as a starting point for your own automation. The blueprint was written for the service names before 5.0.0-beta.2: it calls `set_open_window_mode` and `restore_previous_state` and passes `duration`, so from 5.0.0-beta.2 it fails with "action not found" until its author updates it. The examples above already use the new names.

#### 9. Submit Energy Meter Reading

Submit a gas or electricity meter reading to Tado's Energy IQ feature. Tado uses these readings to calculate consumption and report savings in the Tado app and via the cloud API.

```yaml
# Submit today's reading
service: tado_ce.add_meter_reading
data:
  reading: 12345

# Backdate a reading (ISO date)
service: tado_ce.add_meter_reading
data:
  reading: 12345
  date: "2026-01-17"
```

The `reading` is the meter value in your provider's billing units (m³ for gas, kWh for electricity); Tado stores it as a plain integer and applies the unit configured in the Tado app. Omitting `date` records the reading against today. The service writes once and does not retry — if the cloud rejects the submission (typically a duplicate for the same date), check the log for `add_meter_reading failed` and resubmit with a corrected date.

**Automation example — monthly reading from a smart meter:**

```yaml
automation:
  - alias: "Tado — Submit Monthly Gas Reading"
    trigger:
      - platform: time
        at: "08:00:00"
    condition:
      - condition: template
        value_template: "{{ now().day == 1 }}"
    action:
      - service: tado_ce.add_meter_reading
        data:
          reading: "{{ states('sensor.gas_meter_total_m3') | int }}"
```

#### 10. Identify Device (Flash LED)

Flash the LED on a TRV, thermostat, or sensor so you can identify which physical device a serial number belongs to. Useful when a zone has multiple TRVs and you need to find the one that's offline or low on battery.

From v4.1.0-beta.3 onwards, each TRV and thermostat has its own **Identify** button on its zone card, so the easiest way is to just press it. The button flashes the LED locally over HomeKit when the bridge is connected (no cloud call) and falls back to the cloud otherwise. Multiple TRVs in a zone each get their own button, so there's no need to copy a serial anywhere.

The `identify_device` service does the same thing if you'd rather script it, and now resolves TRVs and thermostats too, not just the bridge:

```yaml
service: tado_ce.identify_device
data:
  device_serial: VA1234567890
```

The serial is the short serial visible on the back of the device (and in `binary_sensor.{zone}_connection`'s `device_serial` attribute). The service also flashes locally over HomeKit when the bridge is connected, falling back to the cloud, in which case it takes a few seconds to start. If nothing happens after about 30 seconds, the device is offline or the serial is wrong — check the log for `identify_device failed`.

#### 11. Force a Refresh (v4.3.0+, Zone states added in v4.4.0)

Presence data (mobile-device geofencing, home/away state) and zone data (temperature, target, mode) each refresh on their own minimum interval so nothing can drain your daily API quota. That floor also limits how quickly an automation can pull anything fresh, even one that calls for an update every few seconds. The `tado_ce.refresh` service forces an immediate fetch of one of the three, bypassing the floor for that one call, for anyone who'd rather drive their own refresh cadence than rely on the automatic schedule. Zone states is also the way to force a catch-up on a card lagging behind a change made outside Home Assistant (the Tado app, or the thermostat itself), when HomeKit's own reading is being trusted over the cloud poll to save API calls; before v4.4.0 the only way to force that was a full integration reload.

```yaml
# Force a mobile-device presence fetch now
service: tado_ce.refresh
data:
  data: mobile_devices   # or: home_state, or: zone_states
```

**Automation example — pull presence every 30 seconds while someone's expected home:**

```yaml
automation:
  - alias: "Tado — Fast presence while arriving"
    trigger:
      - platform: time_pattern
        seconds: "/30"
    action:
      - service: tado_ce.refresh
        data:
          data: mobile_devices
```

The forced type is only fetched if that feature is turned on (mobile-device sync for `mobile_devices`, presence sync for `home_state`); if it's off, the call stops with an error that names the feature. `zone_states` has no such toggle, it's always available. If the forced fetch lands on a cycle that was already due a full sync, that data is fetched twice that cycle (once for the scheduled sync, once forced), so keep the trigger interval sensible rather than every second. It's a single-home service; on a multi-home setup it raises an error listing the homes so you can route explicitly.

#### 12. Hub-level buttons

Three buttons live on the Tado CE Hub device (Settings → Devices & Services → Tado CE → the Hub entry). These are integration-level actions that affect the whole home, not a single zone.

| Button | What it does | When to press |
|--------|--------------|---------------|
| **Resume All Schedules** (`button.tado_ce_hub_resume_all_schedules`) | Clears the manual overlay on every zone in one press, returning all rooms to their Tado schedule. Equivalent to calling `tado_ce.resume_climate_schedule` per heating or AC zone and `tado_ce.resume_water_heater_schedule` for hot water. | After a holiday-mode or wide automation override, when you want everything back on schedule at once. |
| **Refresh Zone Capabilities** (`button.tado_ce_hub_refresh_zone_capabilities`) | Re-fetches an AC or hot-water zone's supported modes and every heating room's Early Start setting from Tado's cloud. For AC and hot-water capabilities the integration does this for you automatically: it picks up an added or removed AC or hot-water zone, and it spots a controller re-pair or hardware swap by watching each zone's device serial and firmware version, refreshing the capabilities when either changes. The button is a manual override for the rare re-pair that keeps the same serial and firmware, so the change isn't visible to the integration. Early Start is read once and then kept, so the button is also the way to pick up an Early Start change made in the Tado app. Costs one cloud call per AC or hot-water zone plus one per heating room. Appears when the home has at least one heating, AC or hot-water zone. | After a re-pair or hardware swap that kept the same serial and firmware, when AC or hot-water capabilities look wrong in HA (e.g. HVAC modes showing `[OFF]` only, or a temperature-capable tank showing as on/off only), or when an Early Start change made in the Tado app does not show on the switch. |
| **Refresh Schedule** (`button.lounge_refresh_schedule`, per heating and AC zone, substitute your own zone name) | Re-fetches a single zone's schedule from Tado and writes it through the cache. Appears for every heating and AC zone, regardless of whether Schedule Calendar is enabled. Costs 2–8 cloud calls per zone, depending on your Tado schedule type. | After editing a schedule in the Tado app. A heating zone's schedule also fetches automatically once Schedule Calendar is enabled, or once that zone uses Smart Valve Control's Valve Target mode; an AC zone has no automatic fetch and relies on this button alone. Outside of those cases, nothing else re-fetches it: the calendar entity, the `scheduled_target_temperature` attribute and the target shown for a zone that is off can all keep showing an old schedule until you press this. |

These buttons are visible in the device page's Controls section. Their entity_ids stay stable across reloads, so they can be wired into automations or dashboards if you want quick-access tiles.

#### 13. Child Lock and Early Start switches

Two TRV behaviour switches that surface settings from the Tado app directly in HA.

**Child Lock** — per-device switch. Each TRV in a zone has its own switch, e.g. `switch.lounge_child_lock` (substitute your own zone name; the device serial identifies the TRV internally but doesn't appear in the entity_id). Locks the physical buttons on the device so accidental presses (typically by children) don't override the schedule. You can lock some TRVs in a zone and leave others unlocked — useful when one TRV is in a child's room but another is on a hallway.

**Early Start** — per-zone switch, e.g. `switch.lounge_early_start` (substitute your own zone name). When enabled, Tado's controller starts heating the zone before the next schedule block's start time so the room reaches target temperature *at* the block boundary rather than starting from cold at that moment. Tado's controller manages the actual lead time based on the zone's heating profile. One switch per zone applies to every TRV in the zone (Early Start is a Tado server-side scheduler feature, not a per-device thing). Tado CE reads each heating room's setting from Tado once and keeps it across restarts, so the switch shows the value Tado holds right after a restart, and shows unknown for a room it has not read yet. A change you make with the switch updates it at once; a change made in the Tado app shows here after you press **Refresh Zone Capabilities**, which reads Early Start again. The first sync after updating costs one extra call for each heating room, and a newly added heating room costs one extra call.

Both surface as standard HA switches. Each toggle is an immediate cloud write — the Tado app reflects the change straight away. The integration confirms the new state on the next poll, so the HA entity may briefly show the previous value if the next poll is more than a few seconds out.

#### 14. Event listeners for automation builders

Tado CE fires several HA events that automations can subscribe to. Most fire on edge-triggered transitions (state change), so a naive listener that writes back to Tado on every event can trap on transient signals during quota-reset windows or partial poll responses.

| Event | Fires when | Defensive listener pattern |
|-------|-----------|---------------------------|
| `tado_ce_ready` | First API sync complete after setup or reload, deferred until Home Assistant has finished its overall startup so boot-time automation triggers are listening | Safe — once-per-session edge. No defence needed |
| `tado_ce_state_restoration_available` | A saved pre-overlay state becomes restorable (overlay timer expired or cleared) | **Edge-triggered.** v4.0.1 onwards: the integration requires 2 consecutive polls confirming the overlay is gone before firing. v4.1.0 onwards: the saved state itself only persists when the original cloud write was confirmed, so a failed write can never feed this event later. A defensive listener pattern (delay + re-check) is still recommended for any automation that writes back to Tado |
| `tado_ce_schedule_updated` | A zone's schedule re-fetched from Tado, or a zone's cached schedule cleared after Tado reuses its zone ID for a different room | Safe on the re-fetch case. The reassignment case has no fetch behind it, and the payload's `zone_name` is blank, so use `zone_id` if your automation needs to identify the zone |

**Defensive listener pattern (recommended for `tado_ce_state_restoration_available` and any future edge-triggered event that writes back to Tado):**

The event payload carries `zone_id`, `zone_name`, `entity_type`, `captured_temperature`, `captured_hvac_mode`, and `source`. Use `zone_name` to derive the climate entity ID, and check `overlay_type` on that entity — if it's still set to a value like `MANUAL`, the overlay is in place and no restore is needed.

```yaml
automation:
  - alias: "Restore previous state — defensive"
    trigger:
      - platform: event
        event_type: tado_ce_state_restoration_available
    action:
      # Wait 30s before re-checking — gives the overlay-cleared signal
      # time to either hold steady or revert if it was transient
      - delay:
          seconds: 30
      # Confirm the overlay is genuinely gone (overlay_type is None or
      # empty) before issuing the restore write. If overlay_type is
      # still set, the original overlay is in place and we skip.
      - condition: template
        value_template: >
          {% set zone = trigger.event.data.zone_name | slugify %}
          {{ state_attr('climate.' ~ zone, 'overlay_type') in [None, ''] }}
      - service: tado_ce.restore_climate_previous_state
        data:
          entity_id: "climate.{{ trigger.event.data.zone_name | slugify }}"
```

The 30s delay catches the case where Tado's API briefly returns `overlay=null` during a quota-reset window or partial poll response (the actual overlay still being in place server-side). The `overlay_type` re-check filters out the transient before any write reaches Tado. Without both guards, a naive listener risks firing on the transient and reverting the user's actual zone state.

#### 15. Turn Off All Rooms (v4.0.1+)

Mirrors the Tado app's "Turn OFF all rooms" button. Calls `tado_ce.turn_off_all_climate_zones` with no parameters; every climate zone in the home (heating + AC) goes into a manual OFF overlay in one call. Hot water is out of scope — Tado's own button targets climate zones only, and Hot Water has its own UI surface in both apps.

Schedules stay suppressed until you manually resume each zone. To restart all zones at once, use the **Resume All Schedules** hub button; for a single zone, call `tado_ce.resume_climate_schedule`.

```yaml
service: tado_ce.turn_off_all_climate_zones
```

Designed for "outside temperature above X — turn off everything" automations where the next schedule block would otherwise override the off state. Example:

```yaml
automation:
  - alias: "Turn off heating when warm outside"
    trigger:
      - platform: numeric_state
        entity_id: sensor.tado_ce_hub_outside_temp
        above: 15
        for:
          minutes: 30
    action:
      - service: tado_ce.turn_off_all_climate_zones
```

**Quota cost**: one cloud call per climate zone (heating + AC). For a 4-zone heating-only home, that's 4 calls; for an 8-zone mixed home, 8 calls. Bootstrap reserve still protects the last 3 calls — if quota is critically low when you call this service, the entire batch aborts before any zone is touched, and you get a `Quota critically low` error toast.

**Multi-home installs**: call once per home explicitly. Without an `entity_id` to anchor the call to a specific config entry, the service raises a `multiple_entries` error so you can pick which home you mean.

#### 16. Schedule Temperature (automation-friendly overrides, v4.1.0+)

Set a zone's target from an automation without Smart Valve Control treating it as a manual override. A normal `climate.set_temperature` looks identical to you grabbing the slider, so on a Smart Valve Control zone it trips the controller into back-off. `set_climate_schedule_temperature` writes the same overlay but marks it as a programmatic change, so Smart Valve Control keeps compensating towards the new target and hands back to your schedule at the next Tado block. On a zone without Smart Valve Control it behaves as a plain overlay write.

```yaml
service: tado_ce.set_climate_schedule_temperature
target:
  entity_id: climate.living_room
data:
  temperature: 21
  overlay: next_time_block   # or "manual" to hold until you resume the schedule
  force_override: false      # default — see below
```

`force_override` is off by default, so a manual override you set by hand on the slider is left alone. Turn it on when the automation should take priority even over a manual change, like a holiday schedule that must win. The main use case (a holiday or bridge-day automation) and a full example are in the [Smart Valve Control section](#smart-valve-control) under "Holiday or Bridge-Day Automations".

#### 17. AC swing — separate vertical and horizontal axes (v4.0.1+)

AC zones expose a `Swing (vertical)` and a `Swing (horizontal)` dropdown, each populated from the cloud-reported capability set for your specific unit. Pick a fixed louver position on either axis to stop oscillation there, useful in bedrooms where a constantly moving louver is disruptive. Simple `On / Off` units keep a two-value dropdown; units that report fine-grained positions (`UP`, `MID_DOWN`, `LEFT`, `MID_RIGHT`) expose them directly, translated across all seven locales.

Each axis is controlled separately: use `climate.set_swing_mode` for the vertical axis and `climate.set_swing_horizontal_mode` for the horizontal axis. The v4.0 combined values (`vertical`, `horizontal`, `both`) were rejected by Home Assistant at the service layer from v4.0.1 onwards, so per-axis control has been the only working form throughout the v4.x line.

#### 18. Read the Flow Temperature Setting (OpenTherm, v4.6.0+)

For a home with an OpenTherm heat source, Tado stores a maximum flow temperature setting in its cloud, and the Tado app reads and changes it there. `tado_ce.get_flow_temperature_settings` asks for that setting once each time you run it and returns what Tado answers. It changes nothing, uses one API call, and there is no entity and no polling behind it.

Run it from **Developer Tools → Actions** (YAML mode), or from a script that keeps the answer with `response_variable`:

```yaml
action: tado_ce.get_flow_temperature_settings
response_variable: flow
```

The response has a `status`, a `message` and the `settings` as Tado sent them. Every serial number in `settings` is cut to its first six characters. The action does not take a target, so it works when one Tado home is set up. With two or more homes it stops with an error that lists their ids, because it has no way to pick one.

| `status` | Meaning |
|----------|---------|
| `ok` | Tado returned a flow temperature setting, shown in `settings` |
| `not_applicable` | Tado has no such setting for this home, which is normal without OpenTherm |
| `access_denied` | Tado refused the request for this home (HTTP 403); nothing was changed |
| `unexpected_response` | Tado answered with a body Tado CE doesn't recognise, returned as it came |
| `failed` | The request didn't get through, the Home Assistant log has the reason |

This action only reads the setting Tado keeps in its cloud. To change the flow temperature, use the Max Flow Temperature number described under Bridge API Integration.

---

## Bridge API Integration

**Requirement:** Tado Internet Bridge with serial + auth key | **Opt-in Configuration**

Direct communication with your Tado Internet Bridge for boiler flow temperature monitoring and control. Independent from the cloud API: bridge errors never fail the cloud poll or change its data.

### Overview

The Bridge API uses the serial number and auth key printed on the back of your Internet Bridge to authenticate directly with `my.tado.com/api/v2/homeByBridge/{serial}/`. This is separate from the OAuth-based cloud API used for zone data.

**Computed by:** Both — boiler output temperature, wiring state, and max output temperature are raw values from the Bridge API (`my.tado.com/api/v2/homeByBridge/{serial}/`); bridge connectivity status (binary sensor) is derived by Tado CE from consecutive-failure counting (marked disconnected after 3 consecutive failures).

### Entities

| Entity | Friendly Name | Type | Description |
|--------|--------------|------|-------------|
| `binary_sensor.tado_ce_hub_bridge` | Bridge Connected | Binary Sensor | Whether the Internet Bridge is online (on / off), from the bridge's own reported status |
| `sensor.tado_ce_hub_boiler_wiring_state` | Boiler Wiring State | Sensor | Bridge installation status (Ready / Installing / Failed) |
| `sensor.tado_ce_hub_boiler_output_temperature` | Boiler Output Temperature | Sensor | Real-time boiler output temperature (°C) |
| `number.tado_ce_hub_max_flow_temperature` | Max Flow Temperature | Number | Control max flow temperature (30–80°C, 0.5°C step). Created when your bridge reports a maximum output temperature, either in its wiring state or, on bridges such as the BR02, only on its own `boilerMaxOutputTemperature` endpoint, which Tado CE reads as well |

### Bridge Connected Sensor

The Bridge Connected binary sensor shows whether your Internet Bridge is online, read from the bridge's own status in Tado's response. Its on/off state tracks the bridge: switch the bridge off and the sensor goes off. Availability is separate. If Tado's cloud can't be reached at all (a network drop, or the cloud rejecting the call), the sensor shows unavailable rather than a stale reading, and it tolerates brief hiccups: the cloud is only treated as unreachable after 3 consecutive failures, so a single timeout won't flip it. The `failure_count` and `last_successful_connection` attributes show what's happening on the cloud side when it does.

**Attributes:**

| Attribute | Description |
|-----------|-------------|
| `response_time` | Last successful response time |
| `failure_count` | Consecutive failure count |
| `last_successful_connection` | Timestamp of last successful connection |

### Wiring State Attributes

The Boiler Wiring State sensor includes extra state attributes from the Bridge API response:

| Attribute | Description |
|-----------|-------------|
| `bridge_connected` | Whether the bridge is online |
| `hot_water_zone_present` | Whether hot water zone is detected |
| `device_type` | Device wired to boiler (e.g. RU02) |
| `device_serial` | Serial number of the wired device |
| `therm_interface_type` | Connection type (OPENTHERM, eBUS, relay) |
| `device_connected` | Whether the wired device is connected |

### Configuration

1. Go to **Settings → Devices & Services → Tado CE → Configure → General Settings → Hardware Connections**
2. Enable the **Internet Bridge** toggle
3. Enter your **Bridge Serial Number** (starts with `IB`, printed on the back of your Internet Bridge)
4. Enter your **Bridge Auth Key** (also printed on the back)
5. Save. The credentials are checked against the Bridge API when you first enable the toggle; if you change them later in Advanced Settings, they are saved without a check.

Turning off the Internet Bridge toggle automatically cleans up all bridge-related entities. V2 bridges (`GW` serial) aren't supported by the Bridge API.

### How It Works

- Bridge data is fetched during each coordinator update cycle alongside cloud API data
- Bridge API errors are isolated — they never affect cloud data or trigger reauth
- The `boiler.outputTemperature.celsius` field in the wiring state response provides real-time boiler output temperature
- Max output temperature control PUTs to `boilerMaxOutputTemperature` endpoint
- When the wiring state carries no maximum output temperature but reports an output temperature, Tado CE reads the current value with a GET on the same `boilerMaxOutputTemperature` endpoint, once per bridge poll

### Bridge API vs Cloud API Boiler Sensors

| Sensor | Source | Requires |
|--------|--------|----------|
| Boiler Flow Temp | Cloud API (`activityDataPoints.boilerFlowTemperature`), or a bound third-party boiler-gateway sensor without a bridge | Tado reporting a flow temperature for your home (whatever the boiler connection), or Advanced Settings → External Data Sources → Boiler Flow Temperature Entity |
| Boiler Output Temperature | Bridge API (`boiler.outputTemperature.celsius`) | Bridge credentials |
| Boiler Max Output Temperature | Bridge API (`boilerMaxOutputTemperature`) | Bridge credentials |

Both can coexist — they read from different data sources.

### Heating Circuit Control

This lets you stop a room from firing the boiler on its own while still letting it soak up heat that's already going. Set a zone to **No heating circuit** and its radiator valves still open when the boiler is running for another room, so the room stays warm on spare heat, but it never calls for heat itself. Pair that with an automation (by schedule, occupancy, or anything else) and it's a real energy lever: a room can coast on residual heat through the day and only claim its own boiler run when you decide it should.

That works whatever your setup, single boiler circuit or several. If you do have more than one circuit, the same per-zone select also moves a zone from one circuit to another.

**Turning it on** (v4.2.0)**:** it's off by default. Enable **Heating Circuit Control** under Settings → Devices & Services → Tado CE → Configure. Each heating zone then gets a `select.<zone>_heating_circuit` entity. Single-circuit homes get it too, so the "No heating circuit" option is available there as well.

**Cost:** the circuit list and each zone's current circuit are fetched only when you enable the option, and only on the normal sync cycle — no extra background polling. Changing a zone's circuit is one API call.

**Two things worth knowing:**
- The selection reflects a change made in the Tado app after the next sync, not instantly (the same refresh cadence as the rest of the integration). Changes you make from the select apply immediately.
- The option labels use each circuit's driver serial. If you re-pair a circuit's driver, Tado issues a new serial, so an automation that stored the old serial needs updating. The assignment itself is keyed on the stable circuit number internally, so only automations that hard-coded a serial are affected.

### Usage Scenarios

#### Scenario 1: Monitor Boiler Output Temperature

```yaml
type: entities
entities:
  - entity: sensor.tado_ce_hub_boiler_output_temperature
    name: "Boiler Output Temp"
  - entity: sensor.tado_ce_hub_boiler_wiring_state
    name: "Bridge Status"
  - entity: number.tado_ce_hub_max_flow_temperature
    name: "Max Flow Temp"
```

#### Scenario 2: Weather-Compensated Flow Temperature

```yaml
automation:
  - alias: "Lower Flow Temp When Mild"
    trigger:
      - platform: numeric_state
        entity_id: sensor.tado_ce_hub_outside_temp
        above: 12
        for:
          hours: 2
    action:
      - service: number.set_value
        target:
          entity_id: number.tado_ce_hub_max_flow_temperature
        data:
          value: 45

  - alias: "Raise Flow Temp When Cold"
    trigger:
      - platform: numeric_state
        entity_id: sensor.tado_ce_hub_outside_temp
        below: 2
        for:
          hours: 1
    action:
      - service: number.set_value
        target:
          entity_id: number.tado_ce_hub_max_flow_temperature
        data:
          value: 65
```

---

## HomeKit Local Control

**Available:** v4.0.0+ | **Requirement:** Tado Internet Bridge v3 or V3+ | **Opt-in Configuration**

Pair your Tado bridge via HomeKit to control heating zones directly on your local network. Smart AC Control V3+ stays on the cloud API, since it pairs with HomeKit separately from the bridge. Temperature updates arrive in real time instead of waiting for the next cloud poll (humidity still comes from the cloud, see "HomeKit humidity" below), and the local command itself doesn't count against your API quota (each change is followed by one cloud refresh to confirm it, see [Write Path](#write-path-climate-entities-and-smart-valve-control)).

**Computed by:** Tado CE — the integration manages the HomeKit pairing, routes temperature reads (HomeKit-first with cloud fallback), tracks write attempts/successes/latency, and counts API calls saved. Raw temperature values come from the HomeKit accessory (local) or Tado cloud; all routing, fallback decisions, and counter arithmetic are done by Tado CE.

### What You Get

| Benefit | Description |
|---------|-------------|
| Faster controls | Temperature and mode changes go through your LAN (about 1 second once sent; a temperature change first waits out the Temperature Slider Delay, 3 seconds by default) instead of the cloud |
| Real-time temperature | Temperature pushes instantly via HomeKit events; humidity comes from the cloud unless it is missing |
| Fewer API calls | Cloud polling is reduced when HomeKit is connected — the integration tracks savings |
| Automatic fallback | If HomeKit becomes unavailable, the integration switches to cloud without interruption |
| Zero-config reconnect | If the bridge connection drops — or is unreachable when HA starts — the integration retries in the background and picks up local control as soon as the bridge is reachable again, without a reload |

### How mode and temperature are sourced

Tado CE talks to your Tado two ways at once, and each reading follows the side that's right for it:

- **Room temperature follows HomeKit** — instant, the moment the TRV reports a change. A new per-zone **Reading source (HomeKit or cloud)** setting (v4.1.0; Configure → Zone Configuration → Temperature) lets you override this per zone: Automatic prefers the fast HomeKit reading and falls back to the cloud, HomeKit always prefers local, Cloud always shows Tado's. An external temperature sensor, if you've set one, still takes precedence. This only governs what the dashboard shows; Smart Valve Control always calibrates against its own reading regardless.
- **HVAC mode follows the cloud** — the only side that knows you're following a schedule. A mode change you make in the Apple Home or Tado app shows up in Home Assistant on the next cloud poll (up to your polling interval). The one exception is switching a room off in the Tado app: for a room heated only by VA02 radiator valves, HomeKit shows that straight away, while switching it back on still waits for the next poll. The cloud-first rule is deliberate, not a shortfall: Home Assistant's built-in HomeKit Controller integration has no concept of a schedule at all (its "auto" is just heat/cool switching), and the built-in Tado integration has no fast local temperature. Tado CE gives you the fast temperature and still reflects an app-side mode change on the next poll.

### Where a write goes

A temperature or mode change on a heating zone goes over HomeKit only when all four checks pass. Otherwise it goes through the Tado cloud and costs one API call.

![Where a write from Home Assistant goes. It goes over HomeKit, with no API call for the write itself, when the bridge is connected and reachable, the room's recent HomeKit writes have been healthy, Tado's cloud is not down, the room was on (turning off skips this check) and the overlay mode is Manual. Otherwise it goes through the Tado cloud. If Tado's cloud is down (an outage, rate limit, block or sign-in problem) and the bridge and the room's HomeKit writes are fine, a heating write goes to the room over HomeKit and is held until Tado answers, tried up to three times 10 minutes apart and dropped after 24 hours; a newer change from the Tado app wins. Resume schedule and timers always use the cloud. A HomeKit write is checked against the cloud with one refresh about 90 seconds after the last write; if Tado has not confirmed it by then, the room is checked first and the change is sent once through the cloud, unless the check came too late or another change reached the room first.](docs/images/tado-ce-where-writes-go.png)

The picture shows the usual route while Tado's cloud is answering. A heating zone takes a different route while it isn't, see [Writes while Tado's cloud is down](#writes-while-tados-cloud-is-down).

### Writes while Tado's cloud is down

When Tado's cloud isn't answering, a temperature, heat or off change on a heating zone is no longer refused. If the room has a working HomeKit link, Tado CE sends the change to the room over HomeKit and holds on to it until Tado is back. "Not answering" means any state other than OK in the `cloud_health` attribute of the API Usage sensor: an outage, a rate limit, a block or a sign-in problem. It applies whatever the zone's Send changes via setting says.

**What you see**
- The card shows the new setting. The Overlay sensor, the Target sensor and the comfort input read a held switch-on as on, even while Tado still reports the room as off. After a restart or an outage a switch-on that is still waiting to reach Tado stays on the card and the Target sensor on every heating room with a working HomeKit link, including rooms with a wireless sensor or a Reading source set to Cloud.
- The climate entity has `write_held_locally: true` and `write_held_since` (the time the change was held) until the change is settled. Neither attribute is there otherwise.
- The change is saved, so it survives a Home Assistant restart. After a restart it is judged again once Tado is back. The one exception is a Home Assistant that starts during the outage with a clock set to a time before the change was held: it drops the change.

**When Tado answers again**, Tado CE looks at the room before it writes anything:
- If Tado already shows the held setting with the same ending, nothing more is sent.
- If someone changed the room in the Tado app, on the thermostat or in Apple Home after the held change, it is dropped and the newer change stands. One case is sent instead: a held switch-on while Tado still shows the room off but the thermostat reads the switch-on, because the bridge may not have told Tado yet. A switch-off made in the Tado app at that moment could be overwritten, and that case still needs confirming on a real home. A Heat tap after Tado answers, while a switch-on is still waiting, sends the target the room holds and keeps a newer target set in the Tado app or on the valve. A change is also dropped when the thermostat reads a different power or temperature than the held change, and when Tado shows the schedule but the thermostat can't be read, because Tado CE writes nothing on an unknown.
- Otherwise a change on the "Until next automatic change" ending is sent to Tado with that ending, or the schedule is resumed if the next schedule change has already passed. A change on the Manual ending needs nothing sent, because the room already holds it.
- If Tado doesn't take the change, it is tried again after 10 minutes, up to 3 tries in all, and then dropped. A refusal for the request limit, a block or a sign-in problem isn't counted as a try: the change is kept and tried again when Tado answers, no sooner than 10 minutes after the last try, until it is 24 hours old. A send held back because your remaining calls are down to the Quota Reserve keeps the change for a later try.
- Rooms that recover together are sent one after another, so they can't spend into the reserve of calls.

**Cost:** one extra zone fetch for all rooms, one call per held room for each try, and one more zone fetch after each change that is applied. If Tado sends no zone update within 2 minutes of coming back, Tado CE asks for the zones again and then waits 10 minutes before the next round, for up to 3 rounds. After that it stops asking and judges the change at the next zone update. While Tado is unreachable (not for a rate limit or a block), Tado CE also asks for the zones again after 10 minutes, 30 minutes and an hour, then every 30 minutes, so it notices when Tado is back without waiting for the next poll.

**What ends a held change early:** pressing Auto or Resume Schedule on the room, Resume All Schedules, or any newer change you make to the room, whether or not Tado accepts it, including an Auto that Action Guard skips because the room looks to be on its schedule already. Pressing Auto still needs Tado to be reachable to reach the room. A held change that is still undecided after 24 hours is dropped.

**Limits**
- Heating zones only. AC and hot water change as before, and fail while Tado is unreachable.
- A timer with a set duration is refused while Tado is unreachable, and so is every change on a zone whose overlay mode is Timer, because HomeKit can't carry an end time.
- There is no timer that ends a held change by itself. The room stays at the held setting until Tado is back, even past the end of a schedule block, and then the schedule is resumed if that block has changed.
- The first change after the internet drops without warning still fails and the card goes back, because Tado CE only learns Tado is unreachable from a failed call. The next change is held.
- If the bridge didn't confirm a change (the request timed out), the change is still held and shown as held. If the thermostat did not take it, it is dropped when Tado is back and is not sent again.
- The daily quota pause is not an outage. With the quota down to the reserve, changes go the way they did before.
- A room with no working HomeKit link (the bridge is not connected, the zone has no HomeKit accessory, or HomeKit writes to it are paused after repeated failures) has nothing to hold the change with, so the change fails as before.

### Known Limitations

| Limitation | Detail |
|------------|--------|
| HomeKit humidity (fallback only) | Humidity defaults to cloud data (0.1% precision, updates every poll cycle). HomeKit humidity is only used when cloud is unavailable — it provides 1% resolution with infrequent updates due to bridge firmware behaviour. Temperature uses HomeKit first (accurate, real-time). This also applies when a zone's main device has been offline for hours and Tado's cloud humidity has stopped updating. |
| A dead sensor's last reading is hidden after four hours | When the device that supplies a zone's room temperature and humidity has been offline for four hours or more, Tado CE hides Tado's last reading instead of showing it as live, and uses the valve's HomeKit reading if there is one (a zone whose Reading source (HomeKit or cloud) is set to Cloud shows no temperature on its climate card or its temperature sensor instead). Tado CE finds out a device has gone offline when it next refreshes the device list, which is every hour (every 4 hours on the free 100-call plan) at the earliest, and later if the quota is nearly spent. It keeps applying the four-hour rule while polling is paused, and it trusts a returning device again as soon as Tado sends a reading stamped more than two hours after the device went offline. If you remove the dead device in the Tado app while Tado still holds its last reading, that reading shows as live again. |
| Cloud-only data | Heating power, battery status, schedules, hot water, and geofencing are only available from Tado's cloud. HomeKit provides the room temperature and target temperature locally on heating zones, plus an off switched in the Tado app for rooms heated only by radiator valves of the VA02 type. Mode and everything else comes from the cloud. |
| Smart AC Control V3+ | Smart AC Control units are standalone WiFi accessories with their own HomeKit pairing code, separate from the Internet Bridge. Tado CE only handles the bridge's HomeKit pairing today, so Smart AC Control units fall through to the cloud path for every operation regardless of whether the bridge is paired locally. Adding standalone-unit pairing would need parallel multi-pairing management and HAP HeaterCooler service handling on top of the existing Thermostat path; not on the roadmap until that's been scoped against real hardware. Thanks to @MacrosorcH (Discussion #271) for the correction. |
| A TRV that goes quiet can hold a stale reading briefly | The bridge answers HomeKit on each TRV's behalf. If a TRV loses radio contact but the bridge keeps returning its last reading as if current, the room temperature can sit on that stale value for a few minutes before the freshness check gives up on it and falls back to cloud. A genuine bridge disconnect falls back to cloud right away; this only applies to the bridge staying connected while one TRV stops reporting. HomeKit exposes no per-TRV "last heard from" signal for us to catch it sooner. |
| Wireless Temp Sensors | Standalone temperature sensors (ST01) don't appear as HomeKit accessories — their data always comes from the cloud. |
| Single pairing | The bridge can only be paired with one HomeKit controller at a time. |
| External sensors don't control TRV valve | Per-zone external sensors change the displayed room temperature, but the TRV still uses its own sensor to control the valve. Enable **Smart Valve Control** (Offset Sync or Valve Target) in zone settings to automatically compensate — see [Smart Valve Control](#smart-valve-control). |
| No TRV LED feedback on local writes | When a temperature change goes through HomeKit, the TRV updates silently. There's no LED flash showing the new target the way there is when you change it in the Tado app, because the HomeKit Accessory Protocol doesn't carry a feedback channel for this. If you want a flash to confirm you're at the right device, press its Identify button or call `tado_ce.identify_device` after the write. See the workaround note below. |

### General Limitations

| Limitation | Detail |
|------------|--------|
| No GPS tracking | Device trackers only show home/not_home status from Tado's geofencing — no GPS coordinates. |
| Token expiry | If your Tado token expires, HA will prompt you to re-authenticate. |
| No schedule management | Use the Tado app to create or edit heating schedules. Tado CE can read schedules (calendar entity) but not modify them. |
| No historical data fetch | Tado CE doesn't pull historical data from Tado's servers — this would consume too many API calls. Use HA's built-in recorder for history. |

### Setup

1. Go to **Settings → Tado CE → Configure → General Settings → Hardware Connections**
2. Enable **Local Control (HomeKit)**
3. Follow the pairing flow. You'll need the 8-digit HomeKit setup code from your bridge, in the format `123-45-678` (this is the setup code, not the 4-digit auth code used for the cloud bridge connection). The code is checked before pairing, so a wrong format is caught straight away rather than failing mid-handshake.
4. Once paired, the integration connects automatically on every HA restart

> **Note:** Your bridge can only be paired with one HomeKit controller at a time. If you're using Apple Home, you'll need to unpair it first. You can re-expose climate entities to Apple Home via the HA HomeKit Bridge integration.

**If you factory-reset your bridge:** a reset issues a new HomeKit identity, so the stored pairing no longer matches. Tado CE detects this, stops retrying, and raises a Home Assistant Repairs notification telling you to re-pair. Go to **Settings → Tado CE → Configure → Advanced Settings → HomeKit** and tick **Pair again**, which reopens the setup code prompt directly. Local control comes back on its own once the new code is accepted. (If a pairing attempt was interrupted and left HomeKit switched on but not connected, the same **Pair again** option gets you back to the code prompt without disabling and re-enabling.)

### Settings

| Setting | Location | Default | Description |
|---------|----------|---------|-------------|
| HomeKit Local Control | General Settings → Hardware Connections | Off | Enable/disable HomeKit pairing |
| Cloud Data Refresh | Advanced Settings → HomeKit | 30 min | The minimum time between scheduled cloud polls (humidity, heating power, overlays) while HomeKit is connected. Tado CE waits longer when the quota calls for it, and by default waits 120 minutes at night (30 minutes, or this setting if that is longer, when a room uses Smart Valve Control in Valve Target mode with its external sensor in place). Fetches set off by an action, such as the check after a HomeKit write, happen sooner. Temperature uses HomeKit locally. Zones with no HomeKit link, such as Smart AC Control units, and heating zones whose TRV could not be matched to a HomeKit accessory get all their data from the cloud on the same schedule, night included, so lower the setting for faster daytime updates if you rely on them. Each refresh of zone data costs one API call. **Note:** If you set a custom polling interval in Polling, Delays & Defaults, all data refreshes at your custom rate instead during the hours it covers; this setting applies the rest of the time. |
| Unpair | Advanced Settings → HomeKit | — | Remove the HomeKit pairing without removing the integration |

### Entities

| Entity | Type | Description |
|--------|------|-------------|
| HomeKit | `binary_sensor` | Shows whether the HomeKit connection is active. Attributes include uptime, reconnect count, mapped/unmapped zones, and current write performance metrics. |
| HomeKit Reads Saved | `sensor` (disabled by default) | How many cloud read calls HomeKit local control has saved you since the last API quota reset. |
| HomeKit Writes Saved | `sensor` (disabled by default) | How many cloud write calls HomeKit local control has saved you since the last API quota reset. |

Reads Saved and Writes Saved are diagnostic sensors disabled by default — enable them under **Settings → Devices → Tado CE Hub → "X entities not shown"** if you want history charts of HomeKit's API savings. The counters survive HA restarts and reset when your Tado API quota resets (typically once per day).

The HomeKit sensor's own attributes are connection state plus current-session write performance metrics:

**Connection state:**

| Attribute | Description |
|-----------|-------------|
| `last_connected` | When the bridge last connected (or `null` if never) |
| `last_disconnected` | When the bridge last disconnected (or `"Never"` if it hasn't since HA started) |
| `reconnect_count` | How many times the bridge has reconnected since the integration loaded |
| `uptime` | Time elapsed since the current connection established |
| `status` | `connected`, `disconnected`, or `not_configured` |
| `mapped_zones` | Climate zones (heating and AC) matched to a HomeKit accessory. A matched zone can still send a write through the cloud, see [Write Path](#write-path-climate-entities-and-smart-valve-control) |
| `unmapped_zones` | Climate zones with no matching HomeKit accessory, which use the cloud (Smart AC Control zones are counted here) |

**Write performance metrics:**

| Attribute | Description |
|-----------|-------------|
| `write_attempts` | HomeKit write attempts since last restart |
| `write_successes` | Successful HomeKit writes |
| `write_fallbacks` | Writes that failed locally and fell back to the cloud |
| `write_avg_latency_ms` | Average round-trip time for HomeKit writes (milliseconds) |

These start at zero after every HA restart, after an API quota reset, and after a HomeKit reconnect. This is intentional — performance metrics need to reflect current conditions, not yesterday's network. If your bridge moved to a different spot or your WiFi changed, stale latency numbers would be misleading. They also update on every temperature or mode change, so persisting them would mean a disk write every time you touch a slider.

> **Seeing all zeros?** That just means no one (and no automation) has changed a temperature or mode since the last restart. The counters only increment when a write actually happens.

### How Data Sources Work

When HomeKit is connected, climate entities show where their readings come from:

| Attribute | Values | Meaning |
|-----------|--------|---------|
| `temperature_source` | `cloud`, `homekit`, `external` | Where the current temperature reading comes from |
| `humidity_source` | `cloud`, `homekit`, `external` | Where the current humidity reading comes from |
| `last_write_source` | `cloud`, `homekit` | Whether the last temperature/mode change went through HomeKit or the cloud |
| `power_source` | `cloud`, `homekit` | Where the room's on or off state comes from: `homekit` when the bridge reported it after the last cloud refresh (or while Tado CE's own turn-on is still waiting for the cloud to show it), otherwise `cloud` |
| `temperature_updated_at` | a time, or empty | When the temperature last changed on its source: the bridge's last change for `homekit`, the last cloud refresh for `cloud`, empty for `external` |
| `humidity_updated_at` | a time, or empty | The same for the humidity reading |
| `homekit_target` | a number, or empty | The target the bridge holds when it differs from the card's target. Empty when the two agree, when HomeKit is not connected, when the room is off, or when the display source is set to Cloud |

Priority: external sensor override > HomeKit (if fresh) > cloud.

This priority applies to all entities that read temperature or humidity — climate cards, sensor entities (temperature, humidity, mold risk, dew point, comfort level, etc.), insights, and preheat decisions. Climate entities expose the source via attributes; other entities use the same logic silently.

### What Stays Cloud-Only

Even with HomeKit connected, some data only comes from Tado's servers:

- Heating power percentage
- Battery status
- Schedules and overlays
- Hot water control
- Geofencing / presence detection
- Device firmware info
- Everything Smart AC Control V3+ does (those units are standalone HomeKit accessories Tado CE doesn't pair with — see Smart AC Control row above)

The integration handles this automatically; it fetches cloud-only data on the cloud schedule (at least your Cloud Data Refresh, and up to 120 minutes at night by default) while using HomeKit for temperature and target temperature on heating zones. Humidity comes from the cloud first, so at night it can be up to 120 minutes old; HomeKit humidity is only the fallback. A zone with no HomeKit link, such as a Smart AC Control unit, gets everything from the cloud on that same schedule, and so does a heating zone whose TRV Tado CE could not match to a HomeKit accessory.

### Workaround: TRV LED feedback after a local write

If you'd like the LED on the TRV to flash after a HomeKit-routed write (the way it does when you change the temperature in the Tado app), press the device's Identify button or call the `tado_ce.identify_device` service straight after the write:

```yaml
service: tado_ce.identify_device
data:
  device_serial: "VA1234567890"  # short serial of the TRV, visible in the device's diagnostic attributes
```

When the bridge is connected this flashes locally over HomeKit at no quota cost. If the bridge isn't reachable it falls back to the cloud, which costs one call per flash, marginal on the 1,000-call tier and negligible on the 20,000-call legacy tier, but worth bearing in mind on the 100-call free tier if you trigger it from every write. The temperature change itself lands through HomeKit either way.

---

## Weather Compensation

**Requirement:** Internet Bridge enabled, and an outdoor temperature (your own sensor, or Tado's weather data) | **Opt-in Configuration**

Automatically adjusts your boiler's flow temperature based on outdoor temperature, so your heating runs more efficiently in mild weather and ramps up when it's cold.

### Overview

Weather compensation uses a heating curve to work out, from the outdoor temperature, the highest flow temperature your boiler may use, and writes it to Tado's own Maximum flow temperature setting (the one the Tado app shows under Flow Temperature Optimization). When it's mild outside that limit is lower, so the boiler can't run as hot and uses less energy. When it's cold the limit rises. It is a ceiling and not a request: Tado and your boiler still decide how much heat to ask for below it, so the boiler can run cooler than the number, for example when no room is calling for heat.

The engine runs every coordinator update cycle. A 10-minute hold between adjustments prevents oscillation, and outdoor temperature is smoothed (EMA or rolling average, or not at all) to avoid reacting to brief fluctuations. The outdoor reading comes from the outdoor temperature sensor you bound under Advanced Settings → External Data Sources, or from Tado's weather data if you haven't bound one.

**Computed by:** Tado CE using Tado server data — outdoor temperature is fetched from Tado's cloud API, then Tado CE applies the heating curve formula (plus optional room-compensation offset and EMA/rolling-average smoothing) to derive the maximum flow temperature it writes to Tado. The raw reading is Tado server data; the sensor value is a Tado CE calculation.

### Heating System Presets

Radiators (Standard), Radiators (Low Temperature) and Underfloor Heating all behave the same. Each works out the slope of the curve from your own Maximum Flow Temperature, Minimum Flow Temperature, Design Outdoor Temp and Shutoff Temp, and picking one doesn't fill those fields in for you. Custom is the only choice that changes anything: it uses the Slope you type in instead, or the Curved heating curve below if you pick that shape.

So set Maximum Flow Temperature to what your own system is designed for, whichever preset you pick.

### Configuration

1. Go to **Settings → Tado CE → Configure → General Settings**
2. Under **Hardware Connections**, enable the **Internet Bridge** toggle (required for real-time flow temperature data)
3. Under **Smart Automations**, enable **Weather Compensation**. Without the Internet Bridge on, Tado CE asks you to tick **Also enable Internet Bridge**; if you don't, nothing is saved and General Settings opens again with a message
4. Save, then open **Advanced Settings → Weather Compensation** to pick a **Heating System Preset** (or choose Custom for full control)
5. Save again — the engine starts adjusting on the next update cycle

**Parameters** (Slope, Heating Curve Shape and Curve Bend only apply when the preset is Custom, the rest apply to every preset):

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| Slope | 1.5 | 0.3–3.0 | Heating curve steepness (Custom preset only, ignored when the shape is Curved) |
| Heating Curve Shape | Straight line | Straight line / Curved | Curved gives a little more flow in mild weather (Custom preset only, see [Curved heating curve](#curved-heating-curve-expert-option)) |
| Curve Bend | 0.02 | 0.005–0.10 | How much the Curved shape bends (Custom preset only) |
| Design Outdoor Temp | -5°C | -30 to 10°C | Coldest expected outdoor temperature |
| Max Flow Temp | 65°C | 30–80°C | Maximum boiler flow temperature |
| Min Flow Temp | 30°C | 30–60°C | Lowest flow temperature, also the value used once the outdoor temperature reaches the shutoff |
| Shutoff Temp | 18°C | 5–30°C | At or above this outdoor temperature the flow temperature is set to Min Flow Temp |
| Smoothing Method | EMA | None / EMA / Rolling Average | How outdoor temperature is smoothed |
| Smoothing Window | 60 min | 15–1440 min | Smoothing time window |
| Room Compensation | Off | On/Off | Adjust flow temp based on room temperature |
| Room Compensation Factor | 3.0 | 1.0–5.0 | How strongly room temp affects flow temp |
| Step Size | 1.0°C | 0.5–2.0°C | The flow temperature is rounded to this step |
| Hysteresis | 1.0°C | 0.5–3.0°C | A new value is only sent if it differs from the last one sent by more than this |

### Sensors

| Entity | Name | Description |
|--------|------|-------------|
| `sensor.tado_ce_hub_ce_wc_target_flow_temp` | WC Target Flow Temp | Calculated target flow temperature |
| `sensor.tado_ce_hub_ce_wc_status` | WC Status | Engine status: active / paused / disabled |

**WC Target Flow Temp attributes:**

| Attribute | Description |
|-----------|-------------|
| `outdoor_temperature` | Smoothed outdoor temperature |
| `outdoor_temperature_raw` | Raw outdoor temperature reading |
| `heating_system_preset` | Active preset name |
| `room_compensation_offset` | Room feedback adjustment (°C) |
| `smoothing_method` | none, ema or rolling_average |
| `smoothing_window` | Smoothing window in minutes |
| `curve_shape` | The shape in use: `linear` or `exponential` (Curved). Always `linear` unless the preset is Custom |
| `curve_k` | The Curve Bend value in the settings |

### How the Heating Curve Works

![With the default settings, the target flow temperature falls in a straight line as the smoothed outdoor temperature rises: 65°C at the design temperature of -5°C, about 49.8°C at 5°C, about 42.2°C at 10°C, and 30°C at the shutoff temperature of 18°C, staying at 30°C for anything warmer. Each cycle smooths the outdoor reading, reads the flow temperature off the curve, adds an optional room offset, rounds to the step and keeps it within the minimum and maximum, then sends it to the Internet Bridge only if at least 10 minutes have passed since the last send and it is more than the hysteresis away from the last value sent. The three radiator and underfloor presets all use the same curve; only Custom changes it.](docs/images/tado-ce-heating-curve.png)

```
Target Flow Temp = Max Flow Temp - Slope × (Outdoor Temp - Design Outdoor Temp)
```

For every preset except Custom, the slope is calculated for you:
```
Auto Slope = (Max Flow - Min Flow) / (Shutoff Temp - Design Outdoor Temp)
```

This ensures the curve reaches exactly `min_flow_temp` at the shutoff temperature — no flat zones where outdoor changes have no effect.

Example with the default values (max 65°C, min 30°C, design -5°C, shutoff 18°C → auto-slope 1.52):
- Outdoor -5°C → Flow 65°C (full power)
- Outdoor 5°C → Flow 49.8°C
- Outdoor 10°C → Flow 42.2°C
- Outdoor 18°C or above → Flow 30°C (your Min Flow Temp, the value used from the shutoff upwards)

Each cycle then goes through the same steps:

1. The outdoor reading is smoothed. If it goes missing, the last smoothed value is used for up to 30 minutes, and the status shows paused after that.
2. The curve gives the flow temperature. With Room Compensation on, the room offset is added (see below).
3. The result is rounded to the Step Size and kept between Min and Max Flow Temp (and within the 30 to 80°C range Tado reports for this setting). A Min or Max Flow Temp saved below 30 by an earlier version is raised to 30.
4. It is only sent to the bridge if at least 10 minutes have passed since the last one, and it differs from the last one sent by more than the Hysteresis. The sensor shows the latest target either way.
5. If the bridge call fails, the next cycle sends it again.

**Room offset:** Factor × (average target − average room temperature), taken over the heating zones that are heating right now and have both readings. Positive when rooms are cold, negative when they are warm.

### Curved heating curve (Expert option)

The straight line is the default and what every preset uses. With the Custom preset you can pick **Curved** under Heating Curve Shape instead. A real radiator gives off a little more heat in mild weather than a straight line allows for, so the curved version runs a slightly higher flow temperature there. It still reaches your Maximum Flow Temperature at the Design Outdoor Temp and your Minimum Flow Temperature at the Shutoff Temp, so those two settings keep meaning what they say. Slope is ignored while the shape is Curved.

Curve Bend sets how much it bends. The default 0.02 suits radiators and convectors. Underfloor heating hardly bends at all (the straight line is within about 0.5°C of the model at most), so Straight line is just as good there. A higher number gives more flow in mild weather, and above about 0.03 the bend goes further than the model supports, so compare it with Straight line before you keep it.

Flow temperature in °C with max 65°C, min 30°C, design -5°C and shutoff 18°C:

| Curve Bend | -5°C | 0°C | 5°C | 10°C | 15°C | 18°C |
|------------|------|-----|-----|------|------|------|
| Straight line | 65.0 | 57.4 | 49.8 | 42.2 | 34.6 | 30.0 |
| 0.01 | 65.0 | 58.1 | 50.8 | 43.1 | 35.0 | 30.0 |
| 0.02 | 65.0 | 58.7 | 51.7 | 44.0 | 35.5 | 30.0 |
| 0.05 | 65.0 | 60.4 | 54.5 | 46.9 | 37.1 | 30.0 |
| 0.10 | 65.0 | 62.5 | 58.3 | 51.4 | 40.1 | 30.0 |

The default and the range come from a model of how radiators, convectors and underfloor heating give off heat, checked against published heating-curve tables. They haven't been measured on a real building yet, so treat Curved as new and compare it with the straight line in your own home.

### Usage Scenarios

#### Scenario 1: Standard Radiator Setup

Keep the default "Radiators (Standard)" preset and the default flow temperatures (65°C max, 30°C min). Monitor `wc_target_flow_temp` for a few days to verify the curve matches your comfort needs.

#### Scenario 2: Underfloor Heating

Set Maximum Flow Temperature to what your underfloor system is designed for (ask your installer), and the curve's slope follows from it. The preset you pick makes no difference here. If your floors feel cold in very cold weather, switch to Custom and raise the slope slightly.

#### Scenario 3: Fine-Tuning with Room Compensation

Enable Room Compensation to let the engine adjust flow temperature based on actual room temperatures, using the zones that are heating at the moment. If those rooms are too warm, the engine reduces flow temp. If they are cold, it increases. The compensation factor controls how aggressively it responds. If Home Assistant restarts inside a quota pause with Room Compensation on, the status shows paused and no flow temperature is written until the first real fetch from Tado, because the saved data has no live room readings. With Room Compensation off nothing changes.

#### Scenario 4: Dashboard Card

```yaml
type: entities
entities:
  - entity: sensor.tado_ce_hub_ce_wc_target_flow_temp
    name: "Target Flow Temp"
  - entity: sensor.tado_ce_hub_ce_wc_status
    name: "WC Status"
  - entity: number.tado_ce_hub_max_flow_temperature
    name: "Current Max Flow Temp"
```

---

## Smart Valve Control

![Smart Valve Control compared with no compensation. Without it, the TRV on the radiator reads warm, so Tado thinks the room is already warm, closes the valve early and the room stays cooler than your target. Offset Sync, recommended, writes an offset to the TRV so Tado sees the real temperature from an external sensor and modulates the valve as normal. Valve Target, advanced, writes the setpoint it needs so the valve stays open until the room reaches your target; use it when Offset Sync is not enough. Valve Target steps aside for manual changes, carries on at the next schedule block and writes over HomeKit when available; Offset Sync writes its offset through the Tado cloud.](docs/images/tado-ce-smart-valve-control.png)

**Available:** v4.0.0+ | **Requirement:** Heating zone + external temperature sensor + HomeKit (recommended for Valve Target) | **Per-Zone Opt-in**

Automatically uses your external sensor to make the TRV heat your room correctly — instead of relying on the TRV's inaccurate built-in sensor.

### The Problem

Tado TRVs have a built-in temperature sensor that sits right on the radiator. It reads significantly higher than the actual room temperature (e.g. TRV reads 22°C while the room is only 17°C). The TRV thinks the room is warm and closes the valve, but you're still cold.

External sensors in Tado CE fix what you *see* in HA, but the TRV still uses its own sensor to control the valve.

### Two Modes — Pick One Per Zone

Smart Valve Control offers two approaches to solve this problem. You choose one per zone:

Valve Target needs to know the zone's Tado schedule to work out what target to hold. The integration fetches this for you automatically; you can also pull a fresh copy any time with that zone's Refresh Schedule button (see [Hub-level buttons](#12-hub-level-buttons)). Offset Sync doesn't read the schedule at all.

| | Offset Sync (recommended) | Valve Target (advanced) |
|---|---|---|
| **What it does** | Corrects the TRV's temperature reading so Tado sees the right number | Overrides the TRV's target temperature to force the valve open/closed |
| **How it works** | Writes a device offset → Tado app and Tado's own algorithm both use the corrected temperature | Calculates a boosted target and writes it directly to the TRV, bypassing Tado's logic |
| **Tado app shows** | Your external sensor's temperature (accurate) | An inflated target (e.g. 25°C when you want 20°C) |
| **Home Assistant shows** | Your external sensor's temperature | The same inflated target, once Tado's data catches up. `desired_target` carries yours |
| **Who decides when to heat** | Tado's built-in algorithm (using corrected data) | The integration (actively monitors and adjusts) |
| **Best for** | Most setups — TRV reads 1-5°C off, Tado's algorithm works fine with correct data | Difficult rooms where Tado still undershoots even with correct temperature data |
| **API cost** | 1 call per adjustment per TRV (device offset write) | 0 (HomeKit) or 1 (cloud overlay write) |
| **Complexity** | Simple — just a number correction, no state machine | More complex — active state machine (idle/active/backed-off) |

**Which should you choose?**

- **Start with Offset Sync** if you just want things to work correctly. It feeds Tado accurate temperature data and lets Tado's own modulation algorithm handle the rest. The Tado app stays accurate, and you don't need to understand what the integration is doing behind the scenes.

- **Use Valve Target** if you've tried Offset Sync and the room still doesn't reach temperature — for example, a large room with poor insulation, a TRV in an awkward position, or a zone where Tado's PID controller consistently undershoots. Valve Target takes full control and forces the valve open until your external sensor confirms the room is warm.

> **Important:** Don't run both modes on the same zone. They're mutually exclusive — the mode selector only lets you pick one.

### Setup

1. Configure an **external temperature sensor** for the zone (any HA temperature sensor — Zigbee, Aqara, etc.)
2. Go to **Settings → Tado CE → Configure → pick a zone → External Sensors and Valve Control section**
3. Set **Smart Valve Control Mode** to **Offset Sync (recommended)** or **Valve Target (advanced)**

The mode selector only appears for heating zones that have an external temperature sensor configured.

---

### Offset Sync Mode

Offset Sync writes a device temperature offset to your TRV so that the Tado API (and app) displays your external sensor's reading. With accurate temperature data, Tado's own modulation algorithm works correctly without needing external compensation.

**Computed by:** Both — Tado server supplies the TRV's reported temperature and stored device offset via `/api/v2`; Tado CE calculates the corrected offset (`external_sensor − (TRV_reported − current_offset)`) and writes it back.

**How it works:**
```
desired_offset = external_sensor − (TRV_reported_temp − current_offset)
```

**Example:** Your external sensor reads 19.4°C, the TRV reports 19.8°C with a current offset of 0. The integration writes an offset of −0.4°C to the TRV. Now Tado sees 19.4°C and makes heating decisions based on the real room temperature.

**Key behaviours:**
- Updates automatically whenever your external sensor changes (debounced to avoid spamming)
- Rate-limited to one write per 5 minutes per device (Tado's API limit for device offsets)
- Offset is clamped to -9.9 to +10°C (Tado's hardware limit) — see [When the clamp fires](#when-the-clamp-fires) below
- Only writes when the change exceeds your configured sensitivity threshold (default 0.5°C, adjustable 0.5–3.0°C per zone)
- Readback verification: after each successful write, the integration reads the offset back from Tado and only updates the local cache when the confirmed value matches. A failed write (rate limit, server error) leaves the cache unchanged rather than poisoning it with a value the TRV never received (v4.0.0+)
- Periodic drift refresh: the integration re-reads each TRV's stored offset from Tado and reconciles the local cache. It runs as part of a poll and never more than once every 30 minutes, or once per **Cloud Data Refresh** when HomeKit is connected and that setting is longer; it never runs on the 100-call plan. This catches the case where Tado's own adaptive calibration (or a manual edit in the Tado app) changes the stored offset behind the integration's back, which would otherwise feed a wrong baseline into the next correction and could drift the cache to the ±10°C limit (v4.0.0+)
- If your external sensor goes offline, the last offset is preserved (no sudden jump)
- With Quota Reserve on, a write waits once your remaining calls are down to the reserve plus its cost (1 call for each device in the zone plus 1 to read the offset back), until the quota is expected to reset; the offset stays as it is until the next sensor change
- Keeps correcting the device offset even while a zone is off, or your whole system is off for the season. It only recalibrates what temperature the TRV reports to Tado, so the app stays accurate for whenever heating resumes. The write goes to the TRV's temperature-offset setting, so it doesn't turn heating on or change your schedule, but a TRV can still react to a new offset with a short valve-motor movement, which is why corrections are held overnight (see below). A TRV updating its display during an off season is this working as intended (v4.4.0+)
- Each correction depends on the TRV itself reporting a fresh reading. If a zone's been off long enough that the TRV stops reporting anything new, corrections pause rather than compute against a stale number. Resumes automatically the moment the TRV reports something different, which doesn't require heating to restart first (v4.5.0+)
- Holds off a due correction outside your day window, so it can't move a valve overnight. Uses the same day/night hours **Smart Polling** already follows (Advanced Settings → Polling), not a separate setting; set day and night to the same hour to turn this off. This applies to every held correction regardless of size, small or large, so an offset that's drifted a long way (after a battery swap or re-pairing, for example) waits for your day window just like a routine nudge would. Only affects Offset Sync's own device-offset write, never Smart Valve Control's temperature control (v4.5.0+)

**Offset Sync + the Tado app:**
The Tado app will show your external sensor's temperature as the room temperature. Schedules, geofencing, and Tado's own heating logic all use this corrected reading. You don't need to change anything in the Tado app.

> **Note:** If you previously had a manual offset set in the Tado app or via an automation, Offset Sync will overwrite it. That's intentional — it keeps the offset in sync with your external sensor continuously.

> **Important:** If you switch from Offset Sync to Off or Valve Target, the last written offset remains on the device. Reset it manually in the Tado app if you want to return to the TRV's uncorrected reading.

#### When the clamp fires

Tado stores device offsets in a single signed byte, capped between -9.9°C and +10°C. When your external sensor and the TRV differ by more than that, Offset Sync writes the boundary value — but the physical gap remains uncorrected beyond that point.

The climate entity exposes two attributes so you can tell when this is happening:

- `offset_clamped` — `true` when the last write was clamped, `false` otherwise
- `offset_clamp_direction` — `"hit_max"` (required correction was larger than +10°C), `"hit_min"` (smaller than −9.9°C), or `"none"` (in range)

A warning line also appears in the log on each clamp event, naming the zone and the direction.

**What to do when you see `offset_clamped: true`:**

A gap that needs >10°C of correction almost always points to an environmental issue rather than a Tado fault. Common causes, in order of likelihood:

1. **Draught on the TRV** — an air current from a window, a door, or a nearby ventilation grille is cooling the TRV's temperature sensor below room temperature.
2. **Cold external wall behind the radiator** — the TRV is mounted on a wall that sits several degrees below the room air temperature (common with poorly insulated external walls in older homes).
3. **External sensor placed in a warmer pocket** — the sensor sits near a heat source (sun-exposed windowsill, above a fridge, adjacent to another heated zone) or high up in the room where warm air collects.
4. **Radiator cycling very hot** — the TRV body itself heats up when the valve opens, reading 2–3°C above the room for short periods. This usually resolves on its own within a heating cycle.

If none of those apply, open a [GitHub Discussion](https://github.com/hiall-fyi/tado_ce/discussions) with the offset chart and your sensor placements — clamps that persist for hours without an obvious environmental cause are rare enough to be worth looking at.

**Automations on `offset_clamped`:**

You can build an automation that notifies you when a zone has been saturated for a while — useful during commissioning to catch sensor-placement issues early:

```yaml
alias: Notify when Offset Sync clamps for 30 minutes
trigger:
  - platform: state
    entity_id: climate.office_tado
    attribute: offset_clamped
    to: true
    for:
      minutes: 30
action:
  - service: notify.persistent_notification
    data:
      title: "Offset Sync saturated in Office"
      message: "Required correction exceeds ±10°C — check TRV position or external sensor placement."
```

---

### Valve Target Mode

Valve Target calculates a proportional offset and writes an adjusted target directly to the TRV:

**Computed by:** Both — Tado server supplies the TRV's current reading and overlay state via `/api/v2`; Tado CE calculates the valve target (`TRV_reading + (desired_target − external_sensor)`) and writes it via HomeKit or cloud.

```
valve_target = min(TRV_reading + (desired_target − external_sensor), 30°C)
```

**Example:** Schedule says 20°C, external sensor reads 17°C, TRV reads 22°C → controller writes `min(22 + (20 − 17), 30) = 25°C` to the TRV. The valve stays open until the room reaches 20°C.

The controller uses a hysteresis band (±0.3°C) around the target to prevent oscillation, and a minimum change guard (0.5°C) to avoid unnecessary writes.

### Setup (Valve Target)

> **Important:** If your TRV has a non-zero temperature offset (from a previous automation or manual setting in the Tado app), reset it to 0 before enabling Valve Target. The controller reads the offset-adjusted temperature from the TRV, so a fixed offset would cause double compensation and overshoot. The controller warns you on startup if it detects a non-zero offset.

> **The zone's schedule.** Valve Target reads the zone's Tado schedule to know
> what target to hold. The integration fetches it for any zone using Valve
> Target, whether or not Schedule Calendar is enabled, and then keeps what it
> fetched instead of polling for changes. If you edit a schedule in the Tado app,
> press that zone's Refresh Schedule button to bring the change in (see
> [Hub-level buttons](#12-hub-level-buttons)).
> Offset Sync doesn't read the schedule and is unaffected.

### Write Path (climate entities and Smart Valve Control)

- **HomeKit first:** temperature and heat-mode changes go through the local bridge with no API call for the write itself, on zones that are on and use the default Manual overlay mode. One cloud refresh follows to confirm the change, and several quick changes share it
- **Cloud for off zones:** a temperature or heat-mode change to a zone that is off goes through the cloud, because a HomeKit setpoint only stores the number without turning the thermostat on. Turning a zone off still goes over HomeKit on Manual
- **Cloud for other overlay modes:** On heating zones, Until next automatic change and Timer always use the cloud, since HomeKit writes don't carry their termination. This includes zones that follow the hub's Override duration. A heating zone whose Send changes via is Tado cloud always uses the cloud, except for Smart Valve Control valve-target writes (below), which do not check that setting
- **Smart Valve Control** valve-target writes try HomeKit first on any zone where it is active, then fall back to the cloud, limited to one cloud write per zone per 5 minutes
- **Cloud fallback** — if HomeKit is unavailable, writes go through the Tado cloud API (Smart Valve Control writes are limited to 1 per zone per 5 minutes to protect your quota)
- **Cloud down:** while Tado's cloud isn't answering, a temperature, heat or off change on a heating zone with a working HomeKit link goes over HomeKit and is held until Tado is back, whatever the overlay mode or Send changes via setting, apart from timers (see [Writes while Tado's cloud is down](#writes-while-tados-cloud-is-down))
- Smart Valve Control writes are debounced (3-second window) to batch rapid sensor updates into a single write

### Climate Entity Attributes

When Smart Valve Control is active, the climate entity exposes additional attributes:

| Attribute | Value | Description |
|-----------|-------|-------------|
| `valve_control_active` | `true` / `false` | Whether the controller is actively adjusting the TRV |
| `valve_target` | e.g. `24.8` | The actual temperature written to the TRV (only when active) |
| `desired_target` | e.g. `21.0` | Your desired temperature captured when the controller activated (only when active) |
| `valve_control_backed_off` | `true` | Shown when the controller has backed off due to manual override |

> **Note:** While Smart Valve Control is active, the climate card's target temperature shows the **valve target** (the inflated value written to the TRV), not your desired temperature. This is because Tado's API reports the overlay temperature, and the climate entity displays what the API returns. Your actual desired temperature is available in the `desired_target` attribute. If you want to display the desired temperature on your dashboard, create a template sensor:
>
> ```yaml
> template:
>   - sensor:
>       - name: "Living Room Desired Temperature"
>         unit_of_measurement: "°C"
>         state: >
>           {% set desired = state_attr('climate.living_room', 'desired_target') %}
>           {% if desired is not none %}
>             {{ desired }}
>           {% else %}
>             {{ state_attr('climate.living_room', 'temperature') }}
>           {% endif %}
> ```

### How the Offset Calculation Works

Smart Valve Control uses **target manipulation**, not calibration offset. It doesn't change what the TRV reads — it changes what the TRV is aiming for.

**The formula:**
```
valve_target = TRV_reading + (desired_target − external_sensor)
```

The `(desired_target − external_sensor)` part is the gap between where you want the room and where it actually is. Adding that gap to the TRV's own reading gives a target that keeps the valve open until the room reaches your desired temperature.

**Worked example — cold room, heating up:**

| Time | External Sensor | TRV Reading | Desired | Offset | Valve Target | TRV Action |
|------|----------------|-------------|---------|--------|-------------|------------|
| 08:00 | 17.0°C | 22.0°C | 20.0°C | +3.0 | 25.0°C | Valve open (22 < 25) |
| 08:30 | 18.5°C | 22.5°C | 20.0°C | +1.5 | 24.0°C | Valve open (22.5 < 24) |
| 09:00 | 19.5°C | 22.8°C | 20.0°C | +0.5 | 23.3°C | Valve open (22.8 < 23.3) |
| 09:15 | 20.0°C | 23.0°C | 20.0°C | 0.0 | 23.0°C | Valve closing (23 = 23) |
| 09:30 | 20.3°C | 23.0°C | 20.0°C | — | — | Controller deactivates (20.3 ≥ 20.3) |

As the room warms up, the offset shrinks and the valve target drops toward the TRV's own reading. When the external sensor reaches `desired + 0.3°C` (hysteresis), the controller deactivates and resumes the Tado schedule.

**What happens without Smart Valve Control:**

At 08:00, the TRV reads 22°C and the schedule target is 20°C. The TRV thinks the room is already 2°C above target and closes the valve. The room stays at 17°C.

**Comparison with offset calibration (e.g. `set_climate_temperature_offset`):**

| | Smart Valve Control | Offset Calibration |
|---|---|---|
| What changes | TRV's target temperature | TRV's internal temperature reading |
| TRV internal reading | Unchanged (still reads 22°C) | Corrected (reads 17°C after offset) |
| Climate card shows | Valve target (e.g. 25°C) | Your desired target (e.g. 20°C) |
| API cost per adjustment | 0 (HomeKit) or 1 (cloud) | 1 per TRV + 1 readback |
| Adjustment speed | Instant (target change takes effect immediately) | Depends on TRV firmware applying the offset |
| Works with HomeKit | Yes — zero API cost | No — offset is a device-level API call |

Both approaches keep the valve open until the room reaches your desired temperature. Smart Valve Control trades a less intuitive climate card display for zero API cost and instant adjustments.

### Usage Scenarios

#### Scenario 1: Single Room with External Sensor

You have a living room with one TRV and an Aqara temperature sensor on the bookshelf. The TRV reads 3–4°C higher than the room.

1. Add the Aqara sensor as the zone's external temperature sensor
2. Set **Smart Valve Control Mode** to **Valve Target** for the zone
3. The controller automatically compensates — no automations needed

Your climate card will show a higher target temperature while the controller is active (e.g. 24°C instead of 20°C). The `desired_target` attribute shows your actual target (20°C). When the room reaches temperature, the controller deactivates and the climate card returns to showing the schedule target.

#### Scenario 2: Large Room with Multiple TRVs

You have a living room with 3 radiators (3 TRVs in one Tado zone) and one external sensor. Smart Valve Control writes a single zone overlay — all 3 TRVs receive the same adjusted target. The TRV reading used for the offset calculation is Tado's zone-level `insideTemperature`, which comes from the zone's measuring device (typically the first TRV assigned to the zone).

No special configuration needed — multi-TRV zones work the same as single-TRV zones.

#### Scenario 3: Running Offset Automations Alongside SVC

If you have existing automations that call `set_climate_temperature_offset`, you should **disable them for zones where Valve Target mode is enabled**. Running both creates double compensation — Valve Target pushes the target up while the offset pulls the TRV reading down, causing the room to overshoot significantly.

> **Note:** If you want to keep your offset automations, use **Offset Sync** instead — it replaces your automation with a built-in equivalent that stays in sync automatically.

Pick one approach per zone:
- **Smart Valve Control** — zero API cost (HomeKit), automatic, but both the Tado app and the climate card show the inflated target, because that is genuinely what the valve is set to. Your own target stays available in `desired_target`.
- **Offset automations** — costs API calls per TRV, manual automation, but climate card shows your real target

#### Scenario 4: Monitoring SVC Behaviour on Your Dashboard

Add a Markdown card to see what the controller is doing:

```yaml
type: markdown
content: >
  {% set c = 'climate.living_room' %}
  {% if state_attr(c, 'valve_control_active') %}
    SVC Active — valve target {{ state_attr(c, 'valve_target') }}°C,
    desired {{ state_attr(c, 'desired_target') }}°C
  {% elif state_attr(c, 'valve_control_backed_off') %}
    SVC Backed Off (manual override detected)
  {% else %}
    SVC Idle — room at temperature
  {% endif %}
```

#### Scenario 5: Night Mode with Reduced Heating

Your schedule drops to 16°C at night. The external sensor reads 18°C (room is still warm from the evening). SVC sees `external (18) > desired (16) + hysteresis (0.3)` and stays idle — no valve adjustment needed. The TRV follows the schedule normally.

SVC only activates when the room is **colder** than your desired temperature. It never fights against the schedule to cool a room down.

#### Scenario 6: Holiday or Bridge-Day Automations

Tado's schedule is one weekly programme per zone, with no holiday support. The usual workaround is an automation that raises the target on a one-off day, say following a Sunday profile on a public holiday. The catch: with SVC on, a normal `climate.set_temperature` from that automation looks exactly like you grabbing the slider, so the controller backs off and stops compensating for the rest of the morning. The room stays cold on the one day you wanted it warm.

The `tado_ce.set_climate_schedule_temperature` service fixes this. It sets the target the same way, but SVC recognises it as a scheduled change rather than a manual override, keeps compensating towards it, and resumes your normal schedule at the next block change.

```yaml
automation:
  - alias: "Holiday heating"
    trigger:
      - platform: time
        at: "07:00:00"
    condition:
      - condition: state
        entity_id: binary_sensor.workday_sensor
        state: "off"
    action:
      - service: tado_ce.set_climate_schedule_temperature
        target:
          entity_id: climate.living_room
        data:
          temperature: 21
          overlay: next_time_block
```

Leave `force_override` off (the default) and an active manual override you set by hand on the slider is respected, so the automation won't fight a change you made deliberately. Turn it on when the automation should take priority even over a manual change, for example a holiday schedule that must win regardless.

### Safety Features

| Scenario | Behaviour |
|----------|-----------|
| You manually change the temperature | Controller backs off until the next schedule block change or overlay change |
| You switch the zone off in the Tado app | Controller leaves the zone alone and writes nothing until you switch it on again |
| External sensor goes offline while active | Hands the zone back to Tado (removes the override Smart Valve Control set), transitions to idle |
| TRV reading unavailable | Bang-bang fallback — sets TRV to max_temp to keep heating |
| Both sensors unavailable | Resumes schedule, stays idle |
| Valve target exceeds min/max bounds | Clamped to zone's configured min_temp / max_temp, then hard-capped at 30°C |
| HomeKit write followed by cloud check | After each write it waits 60 seconds, and for zone data fetched after the write, before checking for manual overrides. That prevents false back-offs while HomeKit catches up with the cloud |
| HA crashes while controller is active | At the next startup, once the first fresh zone update from Tado has arrived, the controller hands the zone back, unless the zone now shows a setting that isn't its own. Whether it owns an override is saved the moment that changes |
| Tado refuses or times out when the controller hands the zone back | Retries after 5 minutes, then waits twice as long after each further failure (10, 20, 40 minutes and so on), up to 6 hours. Each retry waits for the next zone update from Tado (with HomeKit connected, that is the cloud interval: at least your Cloud Data Refresh, and at night no longer than 30 minutes, or your setting if that is longer, while a room uses Valve Target with its external sensor in place). It never retries while Tado Cloud is backing off, and never deletes a setting that isn't the controller's. At the last 3 calls of the day (with Quota Reserve on) a hand-back waits for the expected quota reset instead, without counting as a failed try |
| Your remaining calls are down to the quota reserve | The controller's automatic valve-target writes wait until the quota is expected to reset (Offset Sync and Adaptive Preheat hold their cloud writes the same way). The log notes it once per quota day |
| A controller fails while starting or running | One warning per zone for each run of failures, the other zones carry on, and a controller that didn't start is tried again on the next poll. Offset Sync is built for a zone even while a Smart Valve Control hand-back is still being retried there |
| You switch Smart Valve Control off, or clear its sensor, while Tado is refusing the hand-back | Keeps retrying on the same schedule, also after a restart, until the zone is back on its schedule or something else has set it. If you had already changed the zone yourself, nothing is touched |
| A write times out | Treated as possibly applied, so the controller hands the zone back as if it had landed |
| TRV has a non-zero temperature offset | Warning logged on startup — reset offset to 0 to avoid double compensation |
| All Tado schedule blocks are OFF | Controller recovers from backed-off state when overlay changes (e.g. HA automation sets a new temperature) |

### State Persistence

Controller state (active/idle/backed-off, last valve target, desired target, overlay ownership) persists across HA restarts. On restart, the controller recalculates before writing and hands back any override it still owed from a previous session, leaving alone a setting that isn't its own. Offset Sync waits for that first update too before it writes an offset, and until it arrives the climate entity's `valve_control_active` attribute reads false. Smart Valve Control waits for the first fresh zone update from Tado before it hands anything back, retries, reads the zone's target or checks whether you changed the zone, because the zone data saved before the restart can't show what is on the zone now. If Tado can't be reached or polling is paused for the quota reserve after the restart, it waits until that has passed. Whether it owns an override is saved the moment it changes; the other fields are saved on a short delay. If the controller was active but the desired target wasn't saved (e.g. upgrading from an older version), it resets to idle and re-captures a fresh desired target on the next heating cycle.

### Limitations

| Limitation | Detail |
|------------|--------|
| Heating zones only | AC zones are not supported in this release |
| Schedule resume is cloud-only | Deleting overlays requires the Tado cloud API (no HomeKit equivalent) |
| TRV precision | HomeKit rounds to 0.1°C; cloud API accepts 0.01°C |

---

## Optional Features

**Available:** Various versions | **Requirement:** Varies | **Opt-in Configuration**

### Schedule Calendar

Shows heating schedules as calendar events. Enable in Configure → "Schedule Calendar".

Pressing HEAT on a zone that is off under a manual override uses the zone's current schedule target when Tado CE has that schedule cached, otherwise 20°C. The cache fills from Schedule Calendar, from Smart Valve Control's Valve Target mode when the zone has an external temperature sensor, or from the zone's Refresh Schedule button.

| Entity | Friendly Name |
|--------|--------------|
| `calendar.heating_schedule_{zone}` | Heating Schedule {zone} |

Each room's calendar takes the room's name, for example "Heating Schedule Dining". A new install gets entity IDs like `calendar.heating_schedule_dining`. An install that already had the calendars keeps the IDs it has (`calendar.heating_schedule_schedule`, `calendar.heating_schedule_schedule_2` and so on); only the friendly names change.

**Computed by:** Both — Tado server provides the weekly schedule blocks via `/api/v2`; Tado CE converts them into HA calendar events.

### Boiler Flow Temperature

Shows the boiler flow temperature. The sensor appears when Tado reports a flow temperature for your home, whatever the boiler connection. Tado CE doesn't check the connection type, so whether yours reports one is up to Tado. Homes without a Tado Internet Bridge can bind a third-party boiler-gateway sensor instead. See [Configuration](#boiler-flow-temperature-configuration) below.

| Entity | Friendly Name |
|--------|--------------|
| `sensor.tado_ce_boiler_flow_temperature` | Boiler Flow Temp |

**Computed by:** Both — Tado server provides the raw boiler flow temperature directly from the Tado device via `/api/v2` when a bridge is present; without one, Tado CE reads the value from a third-party sensor you bind yourself.

#### Boiler Flow Temperature Configuration

For homes without an Internet Bridge:

1. Settings → Devices & Services → Tado CE → Configure → **Advanced Settings → External Data Sources**
2. Set **Boiler Flow Temperature Entity** to your boiler gateway's flow-temperature sensor

Tested against ebusd, OpenTherm Gateway (OTGW), and myVaillant. All three tag their flow-temperature sensor with Home Assistant's `temperature` device class, which this field filters to. Live Tado cloud data always takes priority when a bridge is present; this binding is a fallback for homes without one.

### Device Tracking

Tracks mobile device presence (home/away). Enable "Mobile Device Tracking" in Configure.

| Entity | Friendly Name |
|--------|--------------|
| `device_tracker.tado_ce_{device_name}` | {device_name} |

API usage: 1 call with each refresh of zone and device details (at startup, then normally on the first poll at least an hour after the last one, or 4 hours after on the 100-call plan). Enable "Sync Mobile Frequently" if you want device-tracker updates on every poll instead, which is useful if you build presence-driven automations. That costs about 1 call per refresh (default every 5 minutes) and the larger refresh doesn't repeat it.

**Computed by:** Tado server — mobile device presence state (home/away) fetched from the Tado cloud API.

### Home State Sync & Presence Mode

Syncs home/away presence state. Enable "Home/Away State Sync" in Configure.

| Entity | Friendly Name | Description |
|--------|--------------|-------------|
| `select.tado_ce_presence_mode` | Presence Mode | Control: auto / home / away |
| `binary_sensor.tado_ce_home` | Home | Read-only home/away status |

**Computed by:** Tado server — home/away state is read from and written to Tado's cloud API; the `presence_mode` select entity pushes locks back to Tado.

### Override duration

Controls how long a temperature change made from Home Assistant lasts before the schedule resumes. It applies to every zone that has no Override behaviour of its own; a zone you set under [Per-Zone Configuration](#per-zone-configuration) keeps its own setting.

| Entity | Friendly Name |
|--------|--------------|
| `select.tado_ce_hub_overlay_mode` | Override duration |
| `select.tado_ce_hub_overlay_timer` | Override timer |

**Options** (verbatim from the Tado app):

| Option | Description |
|--------|-------------|
| Until you resume schedule | Stays until you resume the schedule (the default) |
| Until next automatic change | Reverts at the next automatic change in your Tado schedule |
| Timer | Reverts after the timer duration (1 minute to 12 hours) |

The entity's state is `manual`, `tado_mode` or `timer` (shown as the labels above). `select.select_option` accepts only those; Home Assistant rejects the older label text.

**Computed by:** Tado CE — the chosen option is saved in Tado CE with no cloud call, and is sent to Tado as the duration of each override you make from Home Assistant; Tado enforces it.

### Understanding Geofencing vs Presence Mode

Geofencing is a Tado account-level setting configured in the Tado app, not in this integration.

| Scenario | "Auto" Mode Behaviour |
|----------|---------------------|
| Geofencing **enabled** | Tado auto-switches Home/Away based on mobile locations |
| Geofencing **disabled** | Stays in current state — no automatic switching |

**How Presence Lock Works:**
- **"home" or "away"**: Creates a presence lock that overrides geofencing
- **"auto"**: Deletes the presence lock. If geofencing enabled, Tado resumes automatic control. If disabled, state stays as-is.

### Multi-Language

Config flow and options UI available in 7 languages:

| Language | Code |
|----------|------|
| English | `en` |
| German | `de` |
| Spanish | `es` |
| French | `fr` |
| Italian | `it` |
| Dutch | `nl` |
| Portuguese | `pt` |

HA automatically selects the language based on your system locale. No configuration needed.

**Common Misconception (geofencing OFF):**
```
Home → Away → Auto = Back to Home?  WRONG
Home → Away → Auto = Stays Away     CORRECT
```

When geofencing is disabled, "Auto" just removes the lock — it doesn't change state. Use "Home"/"Away" directly, or use HA automations with other presence detection.

---

## Automation Events

**Available:** Various versions | **Requirement:** None | **Automatic**

Tado CE fires HA bus events at key moments — you can use these as automation triggers without polling entity states.

**Computed by:** Tado CE — the integration monitors API data and internal state, then fires these HA bus events based on its own logic (load completion, overlay changes, schedule refreshes). No Tado server push; CE derives the trigger.

### Startup Ready Event

Fires once after the integration finishes loading and all entities have real data from the Tado API. On a cold Home Assistant start the event is held until Home Assistant itself has finished starting, so an automation triggered by `tado_ce_ready` is guaranteed to be listening when the event lands. Use this instead of `homeassistant.start` with delays or `wait_template` chains when your boot automations need to act on Tado CE entities.

| Event | When | Payload |
|-------|------|---------|
| `tado_ce_ready` | First API sync complete, all entities populated | `home_id`, `entry_id`, `zone_count` |

```yaml
# Example: align TRV states to boiler switch after boot
automation:
  - alias: "Tado CE Boot Sync"
    trigger:
      - platform: event
        event_type: tado_ce_ready
    condition:
      - condition: state
        entity_id: switch.boiler
        state: "off"
    action:
      - service: climate.set_hvac_mode
        target:
          entity_id: all
        data:
          hvac_mode: "off"
```

The event also fires after an integration reload, so your automations work correctly if you reconfigure Tado CE without restarting HA.

### State Restoration Event

Fires when a zone's previous state becomes available for restoration after an overlay change. Used by the Restore Previous State service.

| Event | When | Payload |
|-------|------|---------|
| `tado_ce_state_restoration_available` | Previous state captured and ready to restore | `zone_id`, `zone_name`, `entity_type`, `captured_temperature`, `captured_hvac_mode` |

### Schedule Updated Event

Fires when a zone's schedule is refreshed from the Tado API (e.g. after pressing the Refresh Schedule button), or when Tado reuses a zone's ID for a different room and the integration clears that zone's stale cached schedule. The reassignment case has no API fetch behind it, and its payload's `zone_name` is blank.

| Event | When | Payload |
|-------|------|---------|
| `tado_ce_schedule_updated` | Schedule data refreshed, or cleared after a zone ID reassignment | `zone_id`, `zone_name` |

---

## Multi-TRV Zones

**Available:** All versions | **Requirement:** Zone with 2+ TRVs | **Automatic**

If you have a room with multiple radiators, Tado lets you assign multiple TRVs to the same zone. Tado CE handles this correctly — here's how each feature behaves.

**Computed by:** Both — Tado's servers distribute zone overlays to all TRVs and provide per-device temperature readings; Tado CE manages API routing (zone-level vs per-device calls), offset write fan-out, and HomeKit event aggregation.

### What Works Automatically

| Feature | How It Works | Details |
|---------|-------------|---------|
| **Temperature & mode control** | Zone-level | Setting a temperature or HVAC mode applies to all TRVs in the zone via a single API call. Tado's servers distribute the target to every device. |
| **Smart Valve Control** | Zone-level | SVC writes a single zone overlay — all TRVs in the zone receive the same valve target. The TRV reading used for offset calculation comes from Tado's zone-level `insideTemperature`, which is the measuring device's reading. |
| **HomeKit writes** | Zone-level | Writing a temperature via HomeKit targets one TRV, but the Tado bridge syncs the zone overlay to all TRVs automatically. |
| **HomeKit event subscription** | All devices | Temperature and humidity events from every TRV in the zone are received and processed. |
| **Temperature offset** | Per-device | The `set_climate_temperature_offset` service writes the same offset value to every TRV in the zone individually. A zone with 2 TRVs uses 2 API calls (plus 1 readback). |
| **Child lock** | Per-device | Each TRV gets its own child lock switch — you can lock some TRVs and leave others unlocked. |
| **Early start** | Zone-level | One switch per zone, applies to all TRVs. |

### Things to Know

**Temperature offset readback reads from one device.** After writing an offset to all TRVs, the integration reads back the actual value from the first device to verify it was applied. The `offset_celsius` attribute on your climate entity reflects that one device's value. In practice all TRVs in a zone accept the same offset, so this matches. But if you manually set different offsets per TRV in the Tado app, only the first device's offset is shown.

**HomeKit temperature shows the last reporting device.** When HomeKit is connected, temperature events from all TRVs in a zone feed one room value. If two TRVs report slightly different temperatures (common — they're on different radiators), the displayed value is whichever TRV reported most recently, not an average. The difference is typically small (< 0.5°C) since they're in the same room. The target works differently: each device is tracked on its own, so a target change on any one device shows as the room's target within one refresh. If two devices change to different targets in the same refresh, the room shows the Tado Cloud target instead.

**API cost scales with device count for offsets only.** Most operations (set temperature, set mode, resume schedule) are zone-level and cost 1 API call regardless of TRV count. The exception is `set_climate_temperature_offset`, which makes 1 call per TRV plus 1 readback. For a zone with 3 TRVs, that's 4 API calls per offset write.

### API Cost Per Operation

| Operation | API Calls (1 TRV) | API Calls (2 TRVs) | API Calls (3 TRVs) |
|-----------|-------------------|--------------------|--------------------|
| Set temperature | 1 | 1 | 1 |
| Set HVAC mode | 1 | 1 | 1 |
| Set temperature offset | 2 (1 write + 1 readback) | 3 (2 writes + 1 readback) | 4 (3 writes + 1 readback) |
| Resume schedule | 1 | 1 | 1 |
| Smart Valve Control write | 0 (HomeKit) or 1 (cloud) | 0 or 1 | 0 or 1 |

---

## Per-Zone Configuration

**Requirement:** None | **Options Flow Configuration**

Customize settings for each individual zone via **Settings → Tado CE → Configure → Zone Configuration**.

> All per-zone settings live in the centralised Options Flow menu. No config entities are created.

### Adding or removing a room

Tado CE updates its room list on the next full refresh after Tado adds or removes a room, and with a nearly used quota that can wait until the quota resets. Until then Zone Configuration, Turn Off All and the device services work from the last known room list. A room added since then can't be configured until that refresh. A removed room drops out of Zone Configuration and Turn Off All as soon as Tado stops listing it in the zone data, unless that data is empty, in which case every stored room is kept. Its entities stay on the entity page until two refreshes in a row confirm the room is gone (see below). A Smart Valve Control change for a room that isn't in the stored list is refused with a warning.

When Tado adds a heating, air conditioning or hot water room, Tado CE reloads itself once, after the refresh that first lists the new room, so the room gets its entities. Every Tado CE entity goes unavailable for a short time while it reloads, so an automation that triggers on a change to or from `unavailable` will fire. The reload happens at most once per new room until Home Assistant restarts. If the room still has no entities afterwards, reload Tado CE from its entry on the Devices & services page, or restart Home Assistant.

When a room is removed in Tado, Tado CE waits until two refreshes in a row no longer list it, then clears the data it held for that room, stops its Smart Valve Control and Offset Sync, and removes the room's entities and its device from Home Assistant. Your Zone Configuration settings and Smart Comfort history for the room are kept. If Tado lists the room again, Tado CE reloads once to build its entities again and starts its controllers again shortly after, and the data it cleared (schedules, offsets, heating cycles, State Restore captures) builds up again from fresh data.

When Tado re-uses a room's id for a different type of room, for example a heating room that becomes an air conditioning room or the other way round, Tado CE removes the old type's entities in the same refresh and reloads once, so the new entities take the freed entity IDs instead of numbered copies such as `climate.lounge_2`. Anything you set on an entity that only the old type had (area, name, icon) goes with it, and the new climate entity keeps the old entity ID but starts without those settings.

### Available Settings

Organised in the order they appear in the Options Flow: temperature limits first, heating hardware next, sensors that augment Tado, comfort settings, and runtime override behaviour last.

**Computed by:** Tado CE — all settings in this table are stored in the integration's Options Flow config entry and applied by Tado CE at runtime. None of these values come from or are pushed to the Tado cloud.

| Setting | Description | Applies To |
|---------|-------------|------------|
| **Temperature** | | |
| Minimum Temperature | Minimum allowed temperature (5–25°C) | All zones |
| Maximum Temperature | Maximum allowed temperature (15–30°C) | All zones |
| Surface Temp Offset | Fine-tune mold risk surface temperature estimate (-5 to +5°C) | All zones |
| Reading source (HomeKit or cloud) | Where this zone's room temperature, target and on/off state come from when HomeKit is connected (Automatic, HomeKit, Cloud) | All zones |
| **Heating** | | |
| Heat Emitter Type | Radiator or Underfloor Heating | Heating zones |
| UFH Buffer | Extra preheat buffer for underfloor heating (0–60 min) | UFH zones |
| Adaptive Preheat Mode | Off, Active, or Passive | Heating zones |
| **External Sensors and Valve Control** | | |
| External Temperature Sensor | Use any HA sensor instead of Tado's built-in | All zones |
| External Humidity Sensor | Use any HA sensor instead of Tado's built-in | All zones |
| Smart Valve Control Mode | Off / Offset Sync (recommended) / Valve Target (advanced) | Heating zones (with external temp sensor) |
| Offset Sync Minimum Change | How much the offset must change before writing (0.5–3.0°C) | Offset Sync mode only |
| **Comfort** | | |
| Window Type | Window insulation type for mold risk calculation | All zones |
| **Manual Temperature Override** | | |
| Override behaviour | How long a temperature change made from Home Assistant lasts (Until you resume schedule, Until next automatic change, Timer). Clear it to follow the Override duration select on the hub | All zones |
| Timer Length (minutes) | Length of the timer when Override behaviour is Timer (15 minutes to 12 hours). Clear it to follow the Override timer select on the hub | All zones |
| Send changes via | Automatic (HomeKit when possible) or Tado cloud | Heating zones |

> **v4.5.0:** for AC zones, the Min/Max Temperature fields now default to your AC's real hardware floor/ceiling instead of a fixed 5°C/25°C. Any override you set is clamped to that hardware limit, so you can't type in a value your unit doesn't actually support. Heating zones are unaffected, since Tado's own 5–25°C range doesn't vary by hardware.

### Override duration options

| Option | Behaviour |
|--------|----------|
| Until you resume schedule | Stays until you resume the schedule (the default for new zones) |
| Until next automatic change | Reverts at the next automatic change in your Tado schedule |
| Timer | Reverts after the timer duration (1 minute to 12 hours) |

A zone with no setting of its own follows the Override duration and Override timer selects on the Tado CE hub, which start on "Until you resume schedule". A setting you make here wins for that zone, and clearing the field makes the zone follow the hub again. Zones you'd already configured keep whatever setting they had. (The `next_time_block` value still works as a service argument for anyone using it in automations; it maps to "until next automatic change".)

**Send changes via (heating zones).** A target written over HomeKit may not show on a wall thermostat's display (reported on a Smart Thermostat RU02), while a write through the cloud does. Set the zone's **Send changes via** to **Tado cloud** and every temperature and mode change on that zone goes through the cloud, so it uses API calls, while readings still arrive over HomeKit. The default, **Automatic**, sends changes over HomeKit whenever the zone's Override duration is "Until you resume schedule", the zone is on and the bridge is connected. Send changes via is independent of Override duration: Tado cloud with "Until you resume schedule" keeps a change until you resume the schedule, and a zone set to "Until next automatic change" or Timer always uses the cloud.

> **v4.1.0:** the three labels above ("Until you resume schedule" / "Until next automatic change" / "Timer") replaced the older four-option wording to match the Tado app, and the Timer cap rose from 3 hours to 12. Existing zones keep their setting; only the labels and the cap changed.

---

### Share write timing (optional)

**Share write timing** is under **General Settings → Advanced** and is off by default. It exists so that people who choose to can help tune the timing windows Tado CE uses after a change, which are currently set from very little data.

When it is on, each time Tado reports on a zone after you changed it from Home Assistant, Tado CE adds one to a count: by how long after your change that report started, and whether Tado already showed your change, showed something different, or the wait had run out. The counts are split by how the change was sent (HomeKit or the Tado cloud) and by zone type (heating, air conditioning or hot water). They are saved as a fixed table of 162 counts in a file of their own, `/config/.storage/tado_ce/write_timing_<home_id>`. The file doesn't exist when you switch the setting on: it appears shortly after Tado CE counts the first change you make from Home Assistant, so change a zone once to start it.

What this does and does not hold:

- It holds no zone names, ids, temperatures or times of individual changes. It also holds the day counting started. The file name carries your Tado home ID, so post what is in the file and leave the path out. From v5.0.0 the one line inside the file that used to carry the home ID reads `write_timing_REDACTED`; a v4.6.0 file still has it there, so replace it before you post. Say which Tado CE and Home Assistant versions you run when you post it.
- Nothing leaves your system by itself. You decide whether to post the file, so read it before you share it.
- The counts are of Tado's reports, not of your changes: a change that several reports look at is counted once for each report, and "different" is rare on the default settings.
- The counts are kept in a small file in Home Assistant's storage so they survive a restart. Turning the setting off deletes them, and so does removing the integration.

---

## Per-Zone Entity Types

Each zone creates several entity types depending on the zone's capabilities and the features you've enabled. The integration creates them automatically — most are always on, one is toggleable via the Options Flow:

**Computed by:** Both — Zone Diagnostics (battery, connection, firmware) read from Tado's device-info endpoint; Environment Sensors (mold risk, comfort, condensation) and Thermal Analytics (inertia, heating rate, preheat time) are derived by Tado CE from that raw data.

| Entity group | Entities created | Controlled by |
|--------------|------------------|---------------|
| Zone Diagnostics | Battery, connection, heating power sensors | Always on |
| Device Controls | Child lock, early start switches | Always on |
| Boost Buttons | Boost, Smart Boost buttons | Always on |
| Environment Sensors | Mold risk, comfort level, condensation risk | Always on |
| Thermal Analytics | Thermal inertia, heating rate, preheat time sensors | **General Settings → Smart Automations → Thermal Analytics** toggle |

> To hide entities you don't use, **disable the entity in HA's entity registry** (Settings → Devices & Services → Tado CE → entity → Disable). The integration continues to compute and publish them, but they won't appear on dashboards or in automations.

**Device-state refresh cadence (Battery, Connection, Firmware version):** these sensors read from Tado's device-info endpoint, which the integration refreshes on a periodic schedule and whenever a Tado-side topology change is detected. The cadence depends on your daily API quota. Paid Auto-Assist accounts refresh about every hour, so a controller dropping its cloud uplink (E04 errors, mains blip, Wi-Fi flap) shows up within roughly the hour. Free-tier accounts refresh about every four hours to keep the periodic cost under roughly six per cent of the daily quota. A refresh runs on the first poll at least that long after the previous one, so with a slower polling interval it can land later: at a 30-minute interval, an hourly refresh can be up to about 90 minutes apart. A re-pair or hardware swap triggers an immediate refresh on top of the periodic schedule.

**Low battery notice:** when any device in a room reports a low or critical battery, Tado CE adds one notice for that room under Settings → System → Repairs. It names the kind of device (radiator valve, thermostat) and says how soon to replace the batteries, and it goes away once no device in the room reports a low battery. It uses the same device-info refresh as the Battery sensor, so it can appear or clear up to one refresh after the reading changes. It is on for everyone, with no setting, and a restart brings it back on the first poll that has the device list. A room whose valve has a low battery may not heat as it should, so replace the batteries rather than waiting for the notice to clear.

---

## Reset to Defaults

Located at **Settings → Tado CE → Configure → Reset to Defaults**. Lets you revert tuning parameters without re-entering your Tado account or re-pairing hardware.

**Per-category reset** — resets only the tuning fields for that feature. The feature itself stays enabled so the entities don't disappear:

| Scope | What resets |
|-------|-------------|
| Smart Comfort | Feels-like toggle, window type default, history days |
| External Data Sources | Outdoor temperature entity, boiler flow temperature entity |
| Comfort & Safety | Heat and cold alerts for vulnerable occupants (back to off) |
| Thermal Analytics | Zones selection, history days, minimum cycles, inertia threshold |
| Weather Compensation | Preset, slope, heating curve shape and bend, design/max/min/shutoff temps, smoothing method + window, room compensation + factor, step size, hysteresis |
| Internet Bridge | Bridge serial, bridge auth key; also turns the bridge off and turns Weather Compensation off (re-enter the credentials and turn both back on to use them again) |
| HomeKit | Cloud Data Refresh (pairing credentials are preserved) |
| Polling, Delays & Defaults | Day/night start hours, custom intervals, presence/weather/mobile minimum waits, refresh delay, API history retention, smart actions debounce, device sync delay, frequent mobile sync, hot water timer default |

**Reset Everything** — turns every feature toggle OFF except the Internet Bridge (which stays on while its serial and auth key are kept), then applies all default tuning values. **Preserves**:

- Your Tado account (refresh token, home ID)
- Internet Bridge credentials (if previously entered)
- HomeKit pairing credentials (if previously paired)

After resetting everything, you'll need to re-enable the features you want from General Settings — but you don't need to re-authenticate or re-pair.

### What's NOT covered

**Per-zone configuration** (external sensors, Smart Valve Control, overlay mode, temperature limits) is **not** included in the Reset to Defaults scope. Per-zone settings are stored separately, and resetting them in bulk would wipe potentially-expensive-to-rebuild per-zone state. To reset a zone, open **Zone Configuration → (zone)** and change the values back manually.

### Safety behaviour

- Confirmation step — the final "Confirm Reset" screen describes exactly what will be wiped and what's preserved
- No toggle flipping during per-category reset — enabling/disabling features is a separate user action
- Repair notifications, HomeKit pairing credentials, and your per-zone configuration are all preserved

---

## Configuration Scenarios

### Scenario 1: Small Apartment (1–2 rooms, 100 calls/day)

```yaml
Adaptive Polling: Enabled (default)
Smart Comfort: Disabled (save quota)
Weather Sensors: Disabled
Mold Risk: Enabled (important for small spaces)
Window Type: Double Pane

Expected: roughly 63 calls/day on the default settings (no HomeKit, nothing optional on), polling about every 14 to 33 minutes by day
```

### Scenario 2: Large House (5+ rooms, 1000+ calls/day)

```yaml
Adaptive Polling: Enabled
Smart Comfort: Enabled
Weather Sensors: Enabled
Mold Risk: Enabled
Window Type: Per-zone (mix of double/triple)
Outdoor Temp: Weather integration

Expected: ~300–800 calls/day, polling ~5–10 min
```

### Scenario 3: Energy Optimization Focus

```yaml
Smart Comfort: Enabled (for preheat advisor)
Thermal Analytics: Monitor closely
Key sensors: _preheat_time, _historical_deviation, _avg_heating_rate
Automations: Preheat before schedule, reduce when warmer than usual
```

### Scenario 4: Mold Prevention Focus

```yaml
Mold Risk: Enabled
Window Type: Accurate setting per zone
Outdoor Temp: Required
Key sensors: _mold_risk, surface_temperature (attribute), _comfort_level
Automations: Alert when mold risk >70%, auto-ventilation
```

### Scenario 5: Low Quota (100 calls/day) Detailed

```yaml
Day Start: 7, Night Start: 23
Custom Day Interval: 30 min
Custom Night Interval: 120 min
Weather: Off, Mobile Tracking: Off, Home State Sync: Off
Smart Comfort: Off, Schedule Calendar: Off

Day (16h): 32 polls × 1 call = 32 calls
Night (8h): 4 polls × 1 call = 4 calls
Zone and device refresh (at least 4h apart): at most 6 × 2 extra calls = 12 calls
Total: ~48 calls/day (about half the quota left for manual changes)
```

### Scenario 6: High Quota (1000+) All Features

```yaml
Custom Intervals: Empty (use adaptive)
Weather: On, Mobile Tracking: On, Sync Mobile Frequently: On, Home State Sync: On
Device Offsets: On, 5 heating rooms
History Days: 30

Each poll: 4 calls (zone states, weather, home state, mobile devices)
Zone and device refresh (at most hourly): up to 24 × 7 calls = 168 (zones, devices, one offset call per room)

Expected: ~950 calls/day, polling every 5 to 6 minutes by day on the 1000-call plan
```

On the 1000-call plan this uses about 95% of the quota, which leaves little room for more. Turn Heating Circuit Control on as well (6 more calls per refresh with 5 heating rooms) and the 1000-call plan stretches the interval to 5 to 8 minutes to stay inside the same quota.

### Scenario 7: Mixed Zone Types (Heating + AC)

| Feature | Heating Zones | AC Zones |
|---------|---------------|----------|
| Thermal Analytics | Available | No heatingPower data |
| Smart Comfort | Heating patterns | Cooling patterns |
| Condensation Risk | N/A | AC-specific |
| Weather Impact | Moderate | High (solar gain) |

### Scenario 8: OpenTherm Boiler

Auto-detected via `sensor.tado_ce_boiler_flow_temperature`. Monitor flow temp alongside zone heating rates to detect boiler issues.

```yaml
automation:
  - alias: "Alert: Low Boiler Flow Temperature"
    trigger:
      - platform: numeric_state
        entity_id: sensor.tado_ce_boiler_flow_temperature
        below: 45
        for:
          minutes: 30
    condition:
      - condition: template
        value_template: "{{ states('climate.living_room') == 'heat' }}"
    action:
      - service: notify.mobile_app
        data:
          title: "Low Boiler Flow Temperature"
          message: "Flow temp: {{ states('sensor.tado_ce_boiler_flow_temperature') }}°C"
```

---

## Actionable Insights

**Requirement:** None | **Always Enabled**

Intelligent, context-aware recommendations across all zones for comfort, mold prevention, and energy optimization.

### Insight Enhancements

#### Smarter Summary
Home insights sensor now produces action-based summaries instead of generic counts:
- Before: `"3 actions needed across 2 zones"`
- After: `"Replace batteries: Guest, Lounge — Mold risk: Bedroom"`

Top-priority insight drives summary text. Actions grouped by type across zones.

#### Correlation / Deduplication
Related insights within a zone are automatically merged into a single action:
- Mold risk + humidity trend + condensation → single "humidity problem" action
- Configurable correlation groups in `CORRELATION_GROUPS`
- Cross-zone insights excluded from correlation

#### History & Trending
Persistent tracking of insight appearance/disappearance (survives HA restarts):
- Stored in `.storage/tado_ce/insight_history_{home_id}.json`
- Duration-aware messages appended to recommendations ("persisting for 3 days")
- Weekly digest attribute with most frequent insight types and 7-day rolling window
- Auto-pruning of entries older than 30 days

#### Priority Escalation
Auto-escalation rules based on persistence duration:
- Battery low > 7 days → high, > 14 days → critical
- Mold risk > 3 days → high, > 7 days → critical
- Monotonic escalation (never downgrades). Capped at CRITICAL.
- Configurable via `ESCALATION_RULES`

#### Health Score
Numeric 0-100 score reflecting overall home health:
- Based on active insight count and severity
- Exposed as `insight_health_score` attribute on Home Insights sensor
- 100 = no active insights, lower = more/worse issues

### Home Insights Sensor

`sensor.tado_ce_home_insights` — hub-level aggregation:

- **State**: Total number of active insights (integer)
- **Attributes**: `critical_count`, `high_count`, `medium_count`, `low_count`, `top_priority`, `top_recommendation`, `zones_with_issues`, `cross_zone_insights`

### Zone Insights Sensor

`sensor.{zone}_insights` — per-zone insights:

- **State**: Number of active insights for this zone
- **Attributes**: `top_priority`, `top_recommendation`, `insight_types`, `recommendations`
- **Dynamic icon**: Changes based on highest priority

### Insight Types

**Zone-level insights:**

| Insight | Priority | Trigger |
|---------|----------|---------|
| Mold Risk | Critical/High/Medium | Dew point margin < 7°C |
| Comfort Level | Critical/High/Medium | Room below 18°C or above 26°C (the hot side fires even with heating off). Heat priority follows the feels-like heat-index band: Danger = High, Extreme Danger = Critical, milder heat = Medium |
| Battery Low | Critical/Low | Device battery LOW/CRITICAL |
| Device Offline | High | Connection lost |
| Preheat Timing | Medium | Preheat time exceeds schedule gap |
| Heating Anomaly | High | Power ≥80% but temp change <0.5°C for 60+ min |
| Condensation Risk | Medium/High/Critical | AC zone condensation detected |
| Heating Off Cold Room | High | Heating off but room below comfort |
| Poor Thermal Efficiency | Medium/High | Below expected threshold |
| Schedule Gap | Medium | Large gap leaving zone unheated |
| Humidity Trend | Medium | Sustained rising humidity |

**Home-level insights (in `sensor.tado_ce_home_insights` only):**

| Insight | Priority | Trigger |
|---------|----------|---------|
| Cross-Zone Mold | High | 3+ zones with Medium+ mold risk |
| Cross-Zone Condensation | High | Multiple zones with condensation |
| Cross-Zone Efficiency | Medium | Significant efficiency variation |
| Temperature Imbalance | Medium | Large temp difference between zones |
| Humidity Imbalance | Medium | Large humidity difference |
| Away Heating Active | High | Away mode but heating still active |
| Home All Off | Low | Everyone home but all heating off |
| Frost Risk | Critical | Outdoor temp near freezing |
| Geofencing Offline | High | Mobile device for geofencing offline |
| API Usage Spike | Medium/High | Unusual API call rate spike |
| API Quota Planning | Medium/High | Projected exhaustion <6h before reset |
| Weather Impact | Medium | Outdoor temp >5°C below 7-day average |
| Boiler Flow Anomaly | Medium/High | Boiler flow temp above 60°C while no zone asks for more than 20% heat (Medium, skipped while hot water is on, since a cylinder reheat looks the same), or below 30°C while a zone asks for more than 80% (High). Uses Tado's reading when a bridge reports it, otherwise a bound external entity |
| Ventilation Opportunity | Low | Wettest zone's absolute humidity notably above outside, and above this home's own recent average gap (requires an Outdoor Temperature Entity bound under Advanced Settings → External Data Sources) |

### Recommendation Attributes

These sensors include a `recommendation` attribute with actionable guidance:
- `sensor.{zone}_mold_risk` — specific humidity/temperature changes needed
- `sensor.{zone}_comfort_level` — context-aware (considers if HVAC is active)
- `sensor.{zone}_condensation_risk` — AC-specific prevention
- `sensor.{zone}_battery` — battery replacement reminders
- `binary_sensor.{zone}_connection` — device troubleshooting (also exposes `last_seen` and `offline_minutes` for offline-duration automations)
- `sensor.tado_ce_api_status` — quota management suggestions

Empty string when no action needed.

### Usage Scenarios

#### Scenario 1: Dashboard Overview

```yaml
type: entities
entities:
  - entity: sensor.tado_ce_home_insights
    name: "Active Insights"
  - type: attribute
    entity: sensor.tado_ce_home_insights
    attribute: top_priority
    name: "Top Priority"
  - type: attribute
    entity: sensor.tado_ce_home_insights
    attribute: top_recommendation
    name: "Top Action"
```

#### Scenario 2: Alert on Critical Insights

```yaml
automation:
  - alias: "Alert: Critical Home Insight"
    trigger:
      - platform: state
        entity_id: sensor.tado_ce_home_insights
        attribute: top_priority
        to: "critical"
    action:
      - service: notify.mobile_app
        data:
          title: "Critical Home Issue"
          message: >
            {{ state_attr('sensor.tado_ce_home_insights', 'top_recommendation') }}
```

---

## Settings Configured in the Tado App, Not Tado CE

Some settings that affect your heating system can only be changed in the Tado app. They are listed here because users commonly look for them in Home Assistant and cannot find them. Tado's API does not expose these as writable endpoints. Tado CE can read some of these values and use them internally, but cannot write them back.

| Setting | Where in the Tado app | What it controls |
|---|---|---|
| **Smart Schedule** (time blocks, setpoints, modes) | Rooms & Devices → [Room] → Schedule | Zone heating schedules. Tado CE can read and refresh schedule data but cannot create or edit time blocks or setpoints. |
| **Away temperature per zone** | Rooms & Devices → [Room] → Settings → Away temperature | The target temperature applied when the home switches to Away mode. Tado CE reads the resulting state but does not expose this as a writable field. |
| **Open Window Detection timeout** | Rooms & Devices → [Room] → Open Window Detection → Duration | Per-zone timeout (in seconds) used when Tado's own open-window detection triggers. Tado CE's `set_climate_open_window_mode` service uses this as a fallback duration when no explicit `time_period` is supplied. The value itself is not writable via the integration. |
| **Open Window Detection sensitivity** | Rooms & Devices → [Room] → Open Window Detection → Sensitivity | How sensitive Tado's own open-window algorithm is. |
| **Hysteresis / Acceptance Range** | Rooms & Devices → [Room] → Settings → Acceptance range | How close to the target temperature the TRV must get before it stops heating. Tado manages this per zone; it is not readable or writable via the API. |
| **Minimum On/Off Time** | Rooms & Devices → [Room] → Settings → Minimum run time | The minimum duration a heating cycle must run (or stay off) before switching state. Tado manages this server-side; it is not exposed to the integration. |
| **Early Start lead time** | Rooms & Devices → [Room] → Settings → Early Start | Early Start can be toggled on/off in the Tado app. The actual lead time (how many minutes ahead Tado begins heating) is computed by Tado's algorithm from the zone's heating profile and is not configurable or readable via Tado CE. |
| **Smart Schedule preheating level** | Rooms & Devices → [Room] → Schedule → [Time block] → Preheat | How aggressively Tado preheats before a scheduled block. Set per schedule block in the Tado app; not exposed via the API. |
| **Geofencing (auto home/away switching)** | Settings → Presence → Geofencing | Whether Tado switches the home between Home and Away based on registered device locations, and which devices participate. Tado CE can read the resulting presence state and lock/unlock it via the Presence Mode selector, but cannot configure geofencing itself. |
| **Device offset (TRV temperature calibration)** | Rooms & Devices → [Room] → [Device] → Offset | A fixed temperature offset stored on the TRV hardware (-9.9 to +10 °C). Offset Sync writes this automatically from Tado CE. If you want to clear or inspect the raw value before enabling Valve Target Mode, use the Tado app. Note that Tado's own adaptive calibration can also update this value independently. |
| **Energy IQ meter unit** | Energy IQ → Energy Settings → Meter unit | The billing unit (m³ for gas, kWh for electricity) applied to readings submitted via `tado_ce.add_meter_reading`. Tado applies this unit server-side; it can only be set in the Tado app. |
| **Zone name** | Rooms & Devices → [Room] → Settings → Room name | The display name of a zone. Tado CE reads this and uses it as the entity name; it cannot be changed from within Tado CE or HA. |
| **Room and device assignments** | Rooms & Devices | Which TRVs, sensors, and extension kits belong to which room. Zone membership is set during Tado device setup and cannot be changed via the integration. |

---

## Troubleshooting

### Thermal Analytics Shows "Unknown"

**Causes:** Zone doesn't report heatingPower, not enough cycles (need 3–5), heating always on (no complete cycles).

**Solution:** Check `sensor.{zone}_heating` exists, wait 2–3 days, verify `cycle_count` attribute > 0, ensure heating turns on/off regularly.

### Inaccurate Thermal Analytics Values

**Causes:** Low confidence (<50%), recent room changes, external heat sources.

**Solution:** Wait for confidence >80% (5–10 heating cycles). Avoid room changes during data collection.

### Heating Efficiency >200%

**Normal!** Means free heat from solar gain, cooking, appliances, or people. No action needed — consider reducing target to save energy.

### Mold Risk Always Shows "Room Temperature"

**Causes:** Outdoor temperature entity not configured, sensor unavailable, window type not set.

**Solution:** Configure outdoor temp entity, verify sensor works, set window type. Check `temperature_source` attribute — should show "surface_estimation".

### Adaptive Polling Too Slow

**Causes:** Low remaining quota, custom interval too high, many zones consuming quota.

**Solution:** Check `sensor.tado_ce_api_usage`, disable custom interval for pure adaptive, disable optional features.

### Smart Comfort Sensors Not Appearing

**Causes:** Not enabled, integration not restarted, not enough historical data.

**Solution:** Enable "Smart Comfort Analytics" in Configure, restart HA, wait 24–48 hours.

### API Rate Limit Exceeded

**Causes:** Too many manual actions, polling too short, too many optional features, multiple HA instances, or Tado's servers temporarily limiting requests during setup.

**Solution:** Increase polling intervals, disable optional features (Weather, Mobile Tracking), wait for reset (`sensor.tado_ce_api_reset`), ensure single HA instance. If you see "Tado's servers are temporarily limiting requests" during setup, wait a few minutes and try again — the integration retries automatically with backoff.

### Schedule Calendar Not Showing Events

**Causes:** Not enabled, no schedules in Tado app, calendar integration not loaded.

**Solution:** Enable "Schedule Calendar", restart HA, verify schedules exist in Tado app.

### Boiler Flow Temperature Not Detected

**Causes:** Tado isn't reporting a flow temperature for your boiler connection, or there is no Internet Bridge to relay it to Tado's cloud.

**Solution:** Check the Tado app for flow temperature data. Without a bridge, bind your boiler gateway's own flow-temperature sensor instead: Advanced Settings → External Data Sources → Boiler Flow Temperature Entity.

### Bridge API Sensors Showing "Unknown"

**Causes:** Bridge credentials invalid, bridge offline.

**Solution:** Verify credentials in Configure → Bridge Configuration, check bridge is online. Enable debug logging:

```yaml
logger:
  default: info
  logs:
    custom_components.tado_ce.bridge_api: debug
    custom_components.tado_ce.sensor_bridge: debug
    custom_components.tado_ce.coordinator: debug
```

Look for `Bridge API full response` in logs to verify the API is returning data.

### Device Tracking Not Working

**Causes:** Not enabled, geo-tracking not enabled in Tado app, no mobile devices registered.

**Solution:** Enable "Mobile Device Tracking", enable geo-tracking in Tado app, wait for the next refresh of zone and device details (normally an hour or more after the last one, 4 hours or more on the 100-call plan).

### Temperature Offset Not Applying

**Causes:** Offset attribute not enabled, value out of range, service call failed.

**Solution:** Enable "Temperature Offset" in Configure, verify offset within range (-9.9 to +10°C), check HA logs.

### Preheat Advisor Shows 0 Minutes

**Causes:** Already at target, heating rate unknown, next schedule temp same as current.

**Solution:** Check current vs next schedule temp, wait for thermal analytics data (3–5 cycles), verify `_avg_heating_rate` has valid value.

---

## Related Documentation

- [ENTITIES.md](ENTITIES.md) — Complete entity reference
- [README.md](README.md) — Installation and setup
- [ROADMAP.md](ROADMAP.md) — Planned features and ideas

