# Tado CE Features Guide

Complete guide to all Tado CE exclusive features, configurations, and usage scenarios.

> **Entity ID note:** Examples on this page use **v2.3.1-style entity_ids** (e.g. `sensor.tado_ce_api_usage`, `sensor.lounge_thermal_inertia`) because HA preserves entity_ids across upgrades, so these slugs stay stable for anyone who installed Tado CE before v3.0.0. If you installed fresh on v3.0.0 or later, **hub-level** entities pick up a device-prefixed shape (`sensor.tado_ce_hub_api_usage`); **zone-level** entities use the same `sensor.<zone>_<feature>` shape across both classes. [ENTITIES.md](ENTITIES.md) lists every entity with the v2.3.1, v3.0+ fresh, and v4.0.1+ fresh entity_id side by side — check there before copying examples into your dashboard or automations.
>
> **v5.0.0 upgrade path note (forward-looking):** v5.0.0 will drop the in-place migration code that upgrades v3.x option keys, entity IDs, and storage layouts. v3.x installs should upgrade to v4.x first (any release; settings carry over automatically), then to v5.0.0 once it ships. v3.x to v5.0.0 in a single jump won't be supported. See [ROADMAP.md](ROADMAP.md#v500--legacy-cleanup) for the full removals list. No timeline yet.

## 📑 Table of Contents

1. [Multi-Home Support](#-multi-home-support)
2. [API Management](#-api-management)
3. [Smart Polling](#-smart-polling)
4. [API Write Optimization](#-api-write-optimization)
5. [Thermal Analytics](#-thermal-analytics)
6. [Smart Comfort Analytics](#-smart-comfort-analytics)
7. [Enhanced Mold Risk Assessment](#-enhanced-mold-risk-assessment)
8. [Heating Cycle Detection](#-heating-cycle-detection)
9. [Enhanced Controls](#-enhanced-controls)
10. [Bridge API Integration](#-bridge-api-integration)
11. [HomeKit Local Control](#-homekit-local-control)
12. [Weather Compensation](#-weather-compensation)
13. [Smart Valve Control](#-smart-valve-control)
14. [Optional Features](#-optional-features)
15. [Automation Events](#-automation-events)
16. [Multi-TRV Zones](#-multi-trv-zones)
17. [Per-Zone Configuration](#-per-zone-configuration)
18. [Per-Zone Entity Types](#-per-zone-entity-types)
19. [Reset to Defaults](#-reset-to-defaults)
20. [Configuration Scenarios](#-configuration-scenarios)
21. [Actionable Insights](#-actionable-insights)
22. [Settings Configured in the Tado App, Not Tado CE](#️-settings-configured-in-the-tado-app-not-tado-ce)
23. [Troubleshooting](#-troubleshooting)
24. [What's New in 4.x](#-whats-new-in-4x)

---

## 🆕 What's New in 4.x

New user-facing features and settings added across the 4.x line. Each links to its section below; bug fixes are in [CHANGELOG.md](CHANGELOG.md).

| Version | What you got | Where |
|---------|-------------|-------|
| v4.4.0 | `tado_ce.refresh` gains a third option, Zone states, to force an immediate temperature/target/mode fetch past the polling floor | [Force a Refresh](#11-force-a-refresh-v430-zone-states-added-in-v440) |
| v4.3.3 | `tado_ce.set_climate_timer` shows the timer's temperature on the card with the write, rather than after Tado's next refresh | [Climate Timer Service](#3-climate-timer-service) |
| v4.3.0 | `tado_ce.refresh` service — force an immediate presence fetch past the polling floor | [Force a Refresh](#11-force-a-refresh-v430-zone-states-added-in-v440) |
| v4.2.0 | Heating circuit control — per-zone boiler-circuit select, "No heating circuit" for residual-heat coasting | [Heating Circuit Control](#heating-circuit-control) |
| v4.2.0 | Heat and cold alerts for vulnerable occupants (elderly / infant / unwell) | [Smart Comfort Analytics](#-smart-comfort-analytics) |
| v4.1.0 | Per-zone Temperature source (Automatic / HomeKit / Cloud) | [HomeKit Local Control](#-homekit-local-control) |
| v4.1.0 | Per-device Identify button (flashes the TRV / thermostat LED) | [Per-Zone Entity Types](#️-per-zone-entity-types) |
| v4.1.0 | `tado_ce.set_schedule_temperature` — set a target from automations without tripping Smart Valve Control | [Smart Valve Control](#-smart-valve-control) |
| v4.1.0 | Configurable refresh intervals for Home Presence, Weather, and Mobile Device Tracking | [Optional Features](#-optional-features) |
| v4.1.0 | Override duration relabelled to match the Tado app (3 options, Timer cap up to 12 hours) | [Per-Zone Configuration](#-per-zone-configuration) |
| v4.1.4 | Boiler diagnostic sensors renamed "Bridge …" → "Boiler …" (display names only) | [Bridge API Integration](#-bridge-api-integration) |
| v4.0.1 | AC swing split into independent vertical and horizontal axes | [Enhanced Controls](#-enhanced-controls) |
| v4.0.1 | Mold Risk % sensor renamed to Mold Risk Indicator | [Enhanced Mold Risk Assessment](#-enhanced-mold-risk-assessment) |
| v4.0.0 | HomeKit local control, Weather Compensation, Smart Valve Control, and more | across this guide |

---

## 🏠 Multi-Home Support

**Available:** v3.0.0 | **Requirement:** Multiple Tado homes/accounts | **Automatic**

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

### Migration from Single-Home

If upgrading from v2.3.1 (single home), migration runs automatically:
- Entity unique IDs updated to include `{home_id}` prefix (idempotent)
- Refresh token copied from `config.json` to `entry.data`
- Existing automations continue to work unchanged

---

## 📊 API Management

**Available:** v1.0.0+ | **Requirement:** None | **Always Enabled**

Real-time tracking of your Tado API usage, helping you avoid rate limiting and understand consumption patterns.

### Overview

Tado enforces API rate limits (100–20,000 calls/day depending on your plan). The official HA integration doesn't expose usage data. Tado CE solves this by:

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
          title: "⚠️ Tado API Usage High"
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

## 🔄 Smart Polling

**Available:** v1.0.0+ | **Requirement:** None | **Always Enabled**

Automatically adjusts API polling frequency based on time of day, remaining quota, and configuration.

### Overview

Smart Polling includes multiple strategies:

- **Day/Night Polling** — more frequent during day, less at night
- **Adaptive Polling** — auto-adjusts based on remaining quota, with a flat 5 min floor and a 120 min ceiling. The floor is the same whatever your daily call limit; a bigger quota doesn't buy a faster automatic cadence because zone temperature doesn't change any faster. Want faster than 5 min? Set a custom interval.
- **Per-Type Refresh** — slow-changing data (weather, presence, mobile locations) refreshes on its own schedule instead of riding the zone-state cadence, so a fast poll doesn't burn quota re-reading data that hasn't changed
- **HomeKit Defer** — when HomeKit local control is connected, the cloud cadence follows your Cloud Data Refresh dial instead of the adaptive quota maths
- **Quota Reserve Protection** — pauses polling when quota critically low (≤5%), auto-resumes after reset
- **Bootstrap Reserve** — hard limit of 3 API calls never used, reserved for auto-recovery after reset
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
| Device firmware / battery / connection | Hourly (paid tier) / every 4 h (free tier) | Rarely changes |

When HomeKit local control is connected, these floors widen to your **Cloud Data Refresh** dial (Settings → Tado CE → Configure → HomeKit) if that's slower.

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
| Custom Day Interval | Empty | Fixed interval during day (1–1440 min) |
| Custom Night Interval | Empty | Fixed interval during night (1–1440 min) |

**Optional Sensors (affect API usage):**

| Option | Default | API Cost |
|--------|---------|----------|
| Enable Weather Sensors | Off | ~1 call per refresh (default every 30 min, configurable in Advanced Settings → Polling & API) |
| Enable Mobile Device Tracking | Off | 1 call at startup; with Frequent Sync on, ~1 call per refresh (default every 5 min, configurable in Advanced Settings → Polling & API) |
| Enable Home State Sync | Off | Required for Away Mode; ~1 call per refresh (default every 5 min, configurable in Advanced Settings → Polling & API) |

### How It Works

**Adaptive Polling Formula:**
```
Interval = (Time Until Reset / Remaining Calls) / 0.90
Clamped to: 5 min (floor) – 120 min (ceiling)
```

The adaptive floor is a flat 5 minutes whatever your daily call limit. The maths already widens the interval on its own when quota is tight, so the floor is just the fast end: the point past which a faster cadence buys you nothing because zone temperature doesn't change that quickly. A bigger quota doesn't lower the floor, so if you genuinely want faster than 5 minutes, set a custom interval and it's honoured as long as your quota can sustain it. Presence, weather, and mobile device refresh intervals are configurable separately in Advanced Settings → Polling & API (v4.1.0, each behind its own toggle; defaults unchanged).

**Day/Night Aware (v2.0.1):**
- Night period: Fixed 120 min interval to conserve quota
- Day period: Adaptive based on remaining quota after reserving night calls
- If Day Start == Night Start: always uses adaptive polling (24/7 mode)

**Quota Reserve (v2.0.0):**
- Pauses polling when remaining ≤5% or ≤5 calls
- Reserves quota for manual operations (set temperature, change mode)
- Auto-resumes when API reset time passes
- Toggle: `switch.tado_ce_{home_id}_quota_reserve_enabled` (default on). When disabled, polling continues until quota truly runs low; the separate Bootstrap Reserve below still protects the last 3 calls so manual actions remain possible. Only disable Quota Reserve if you've sized your custom intervals against your tier's daily limit and don't want preemptive pausing.

**Bootstrap Reserve (v2.0.1):**
- Hard limit of 3 API calls never used, even for manual actions
- When triggered: persistent notification "API limit reached. Use the Tado app for emergency changes."
- Auto-dismisses when API reset detected

**Reset time detection:**

Tado's `X-RateLimit-Reset` header often points at midnight UTC, which isn't when your quota actually rolls over, so the integration doesn't trust it on its own. It works out the real reset time from whatever evidence it has, in order of preference: it watches for the moment your remaining-calls count jumps back up and records that as the reset; failing that it reads your own sensor history for the usage drop; then it extrapolates from your current usage rate and call history; and as a last resort it falls back to the time of your first call each day. The result is what `sensor.tado_ce_api_reset` shows, and it's what Quota Reserve waits for before resuming.

**Offset Sync drift refresh (v4.0.0):**

When Device Temperature Offsets is on, the integration runs a periodic drift refresh that pulls the stored device offset back from Tado, so the displayed `offset_celsius` stays close to the value Tado's adaptive calibration writes behind your back rather than freezing at the boot reading. The cadence is the larger of 30 minutes or your configured Cloud Sync Interval (the HomeKit-aware floor, see HomeKit Local Control), so on the default Cloud Sync it fires every 30 minutes, and if you've widened Cloud Sync to 60 minutes it fires every 60 minutes.

From v4.1.0-beta.4 the refresh visits **one zone per cycle** rather than every zone at once. Each zone still gets refreshed, just spread out: with N zones the cycle rotates through them, so any one zone is re-read every N cycles. That turns the old all-at-once burst into a steady **1 cloud call per cycle**, whatever your zone count.

Example daily totals at the default 30 minute cadence (48 cycles a day):

- 4 zones: 48 calls/day, each zone refreshed every 2 hours
- 8 zones: 48 calls/day, each zone refreshed every 4 hours

Before v4.1.0-beta.4 the same 8-zone home cost 384 calls/day (8 every cycle). The per-zone interval is longer now, but that's fine: Tado's adaptive calibration drifts over hours to days, so a few hours between re-reads still catches it well within the window, and every write is still readback-checked the moment it happens. Widening Cloud Sync to 60 minutes stretches both the cycle cadence and the per-zone interval further. The drift refresh is paused automatically when remaining quota runs low, so even on the 100-call free tier you don't lose your manual-action budget to it.

The refresh log line in `homeassistant.log` names the zone it refreshed and the call count, so you can audit it directly:

```
Offset Sync: drift refresh complete — zone 5 reconciled with Tado, 1 cloud call(s) used this cycle
```

### Usage Scenarios

#### Scenario 1: Low Quota (100 calls/day) Day/Night Setup

```
Day Start: 7, Night Start: 23
Custom Day Interval: 30 min, Custom Night Interval: 120 min
Weather: Off, Mobile Tracking: Off

Expected: Day 32 syncs × 2 = 64, Night 4 × 2 = 8, Full sync 4 × 2 = 8 → ~80 calls/day
```

#### Scenario 2: High Quota (1000+) Adaptive

Leave custom intervals empty. Adaptive polling reads your remaining quota from Tado's response headers and picks an interval that fits the budget, down to the flat 5-minute floor when quota is healthy. The floor is the same on every plan: a bigger quota doesn't poll faster on its own, because your room temperature doesn't change any faster. Enable all optional sensors. Adaptive will widen the interval automatically if quota drops critically low, and if you genuinely want faster than 5 minutes, set a custom interval, which is honoured as long as your quota can sustain it. On a 20,000-call plan, you may also want to lower the presence and mobile refresh intervals in Advanced Settings → Polling & API for tighter geofencing latency.

#### Scenario 3: Disable Optional Sensors to Save Calls

- Weather off: saves ~144 calls/day (at 10 min intervals)
- Mobile tracking off: saves ~4 calls/day
- Home State Sync off: saves 1 call/sync

---

## ⚡ API Write Optimization

**Available:** v3.4.0+ | **Requirement:** None | **All Enabled by Default**

Reduces unnecessary API calls when you interact with climate controls — temperature changes, mode switches, device toggles, and schedule resumes are all optimized automatically.

### Overview

Every time you adjust a temperature slider, toggle a switch, or resume a schedule, Tado CE sends API calls to the Tado cloud. Without optimization, rapid interactions (dragging a slider, toggling multiple devices) can waste dozens of calls on intermediate or redundant values. API Write Optimization tackles this with five complementary strategies that work together transparently.

**Computed by:** Tado CE — all five optimisation layers (debounce timer, action guard deduplication, device sync queue, write coalescing window, resume guard) run entirely within the integration before any call leaves for Tado's servers. The `scheduled_target_temperature` attribute is read from Tado's schedule data and cached locally by Tado CE.

### How Much Does It Save?

The savings depend on how you use your system:

| Scenario | Without Optimization | With Optimization | Savings |
|----------|---------------------|-------------------|---------|
| Drag temperature slider from 18°C to 22°C | ~8 API calls (one per 0.5°C step) | 1 API call (final value only) | ~87% |
| Set 22°C when already at 22°C | 1 API call | 0 API calls (skipped) | 100% |
| Toggle child lock + early start quickly | 2 simultaneous calls (race condition risk) | 2 sequential calls with delay | Safer |
| Change temp + mode + fan in quick succession | 3 refreshes | 1 coalesced refresh | ~66% fewer refreshes |
| Resume schedule when already on schedule | 1 API call | 0 API calls (skipped) | 100% |

For a typical household making 10–20 manual adjustments per day, you can expect roughly 30–50% fewer write-related API calls.

### Components

#### 1. Smart Actions Debounce

When you drag a temperature slider, each position fires a `set_temperature` call. Smart Actions waits for you to stop adjusting before sending the final value.

| Setting | Value |
|---------|-------|
| Default | 3 seconds |
| Range | 0–10 seconds |
| Disable | Set to 0 |

**Where to configure:** Settings → Tado CE → Configure → Advanced Settings → Polling & API

**How it works:** Each slider movement resets a per-zone timer. Only when the timer expires (no new movement for N seconds) does the API call fire. If you drag from 18°C to 22°C over 2 seconds, only one call is made for 22°C.

#### 2. Action Guard

Skips API calls when the requested state already matches the current state. If your zone is already at 22°C and you (or an automation) sets it to 22°C again, the call is silently skipped.

| Setting | Value |
|---------|-------|
| Behaviour | Always active |
| Covers | Temperature, HVAC mode, fan mode, vertical swing, horizontal swing, preset mode |

**Checked states:** temperature, HVAC mode, fan mode, vertical swing, horizontal swing, and preset mode. Each is compared against the coordinator's cached state — no extra API call needed for the check.

#### 3. Device Sync Queue

Device-level operations (child lock, early start) are queued and executed one at a time with a configurable delay between each. This prevents race conditions when you toggle multiple device settings in quick succession.

| Setting | Value |
|---------|-------|
| Default delay | 1 second |
| Range | 0.5–5 seconds |
| Max queue depth | 20 operations |

**Where to configure:** Settings → Tado CE → Configure → Advanced Settings → Polling & API

**Why it matters:** Without queuing, toggling child lock and early start simultaneously can cause the Tado API to return stale data or reject one of the calls. The queue ensures each operation completes before the next starts.

#### 4. Write Coalescing

When multiple state changes happen in quick succession (e.g. changing temperature, mode, and fan within a few seconds), each change would normally trigger its own coordinator refresh. Write Coalescing batches these into a single refresh after a 2-second window.

| Setting | Value |
|---------|-------|
| Window | 2 seconds (fixed) |
| Behaviour | Always active |

Each new write resets the 2-second timer. The refresh only fires once the timer expires with no new writes. This means 3 rapid changes = 1 refresh instead of 3.

#### 5. Resume Guard

Skips the `resume_schedule` API call if the zone is already following its schedule (no active overlay). Uses the coordinator's cached overlay state — no extra API call needed.

| Setting | Value |
|---------|-------|
| Behaviour | Always active |

Useful when automations call `resume_schedule` as a safety measure — if the zone is already on schedule, the call is free.

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

### Configuration Summary

All settings are under **Settings → Tado CE → Configure → Advanced Settings → Polling & API**:

| Setting | Default | Range | Notes |
|---------|---------|-------|-------|
| Smart Actions debounce window | 3s | 0–10s | Set to 0 to disable |
| Action Guard | Always on | — | No configuration needed |
| Device Sync delay | 1s | 0.5–5s | Lower = faster but riskier, higher = safer |
| Write Coalescing | Always on | — | 2s fixed window, not configurable |
| Resume Guard | Always on | — | No configuration needed |

---

## 🔥 Thermal Analytics

**Available:** v2.0.0+ | **Requirement:** Zones with heatingPower data (TRV or Smart Thermostat) | **Always Enabled**

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
- <50%: Not enough data yet
- 50–80%: Reasonable confidence
- >80%: High confidence, reliable estimates

**Approach Factor:**
- <50%/h: Slow approach, multiple hours to target
- 50–100%/h: Normal, 1–2 hours
- >100%/h: Fast approach, less than 1 hour

### Configuration

**Global Toggle:** Options → General Settings → Smart Automations → Thermal Analytics

**Per-Zone Control (v2.1.0+):** Options → Advanced Settings → Thermal Analytics → Thermal Analytics Zones
- Default: All zones with heatingPower data enabled
- Deselect zones that never call for heat (passive heating) to keep UI clean

**Supported Devices (v2.0.1+):** TRV (VA01, VA02, RU01, RU02), Smart Thermostat (SU02)

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

Wait for `_analysis_confidence` > 80% before trusting preheat estimates.

**Computed by:** Tado CE — derived from complete heating cycles detected locally; Tado server supplies the raw heatingPower and temperature readings.

#### Scenario 2: Detect Insulation Issues

**Indicators:**
- Low thermal inertia (<20 min) — heat escapes quickly
- Low heating rate (<0.6°C/h) — struggling to heat
- High approach factor (>150%/h) — temperature fluctuates rapidly

**Action:** Check for drafts, verify radiator valve, check boiler flow temperature.

#### Scenario 3: Compare Room Performance

| Room | Thermal Inertia | Heating Rate | Confidence | Status |
|------|----------------|--------------|------------|--------|
| Living Room | 45 min | 1.2°C/h | 95% | ✅ Normal |
| Bedroom | 35 min | 1.5°C/h | 90% | ✅ Good |
| Bathroom | 15 min | 0.6°C/h | 85% | ⚠️ Poor insulation |
| Kitchen | 60 min | 0.8°C/h | 80% | ✅ High thermal mass |

#### Scenario 4: Detect Radiator/Boiler Problems

Watch for sudden drops in heating rate or increases in preheat time. Check radiator valve, bleed radiators, check boiler flow temperature, verify TRV battery.

---

## 🧠 Smart Comfort Analytics

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

### Preheat Cooling Rate Prediction (v3.0.0)

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

### Adaptive Preheat Passive Mode (v3.3.0+)

Preheat now has three modes per zone, configured via **Options Flow → Zone Configuration**:

| Mode | Behaviour |
|------|----------|
| Off | Preheat disabled for this zone |
| Active | Always triggers preheat before the next schedule change |
| Passive | Only triggers when the zone is following its schedule — skips preheat if you've set a manual override from HomeKit, the Tado app, etc. |

Existing users with preheat enabled are automatically migrated to Active mode. Passive mode is useful if you frequently override temperatures manually and don't want preheat fighting your changes.

### Configuration

1. Settings → Devices & Services → Tado CE → Configure → **General Settings → Smart Automations**
2. Enable **Smart Comfort**
3. Open **Advanced Settings → Smart Comfort** and pick a **Smart Comfort Mode**: None / Light (recommended when first enabling) / Moderate / Aggressive
4. Set **Temperature History days** (1–30)
5. Optionally select an **Outdoor Temperature Entity** for weather-adjusted comfort targets

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

## 💧 Enhanced Mold Risk Assessment

**Available:** v2.0.0+ | **Requirement:** Outdoor temperature sensor | **Always Enabled**

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
| `sensor.{zone}_comfort_level` | Comfort Level | Overall comfort assessment |

### Heat Index & Heat Risk (v3.3.0+)

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
| Single Pane | 5.0 | ⚠️ High |
| Double Pane | 2.7 | ⚠️ Medium |
| Triple Pane | 1.0 | ✅ Low |
| Passive House | 0.8 | ✅ Very Low |

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
3. Set the **Outdoor Temperature Entity** (e.g., `weather.home`)

**Per-Zone Window Type:** For per-zone overrides, enable **General Settings → Advanced → Per-Zone Configuration**, then open **Zone Configuration → (zone) → Smart Features → Window Type**.

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
          title: "⚠️ High Mold Risk in Bedroom"
          message: >
            Mold risk: {{ states('sensor.bedroom_mold_risk_percentage') }}%
            Surface temp: {{ state_attr('sensor.bedroom_mold_risk', 'surface_temperature') }}°C
            Action: Increase heating or ventilate
```

#### Scenario 2: Window Upgrade ROI

| Scenario | Indoor | Outdoor | Single Pane | Double Pane | Triple Pane |
|----------|--------|---------|-------------|-------------|-------------|
| Winter | 20°C | 0°C | 12.0°C (⚠️ 85% RH) | 15.4°C (⚠️ 72% RH) | 17.8°C (✅ 58% RH) |
| Cold Day | 20°C | 5°C | 14.5°C (⚠️ 78% RH) | 16.7°C (⚠️ 65% RH) | 18.5°C (✅ 55% RH) |
| Mild Day | 20°C | 10°C | 16.5°C (⚠️ 68% RH) | 17.8°C (✅ 58% RH) | 19.0°C (✅ 52% RH) |

### Passive Window Detection (v3.3.0+)

Detects open windows even when your heating or AC is off. Combines temperature drop speed, humidity changes, and indoor-outdoor temperature difference to tell the difference between a natural cooldown and an open window.

**How it differs from active detection:**

| Mode | When It Works | How It Detects |
|------|---------------|----------------|
| Active | Only when heating/cooling is running | Rapid temperature drop while HVAC is active |
| Passive | Anytime, even when HVAC is off | Temperature drop rate + humidity + outdoor differential |

**A known limit:** passive detection reads a room drifting towards the outside temperature as a possible open window. On a cold day, a room that has just stopped heating drifts the same way for a while as it settles, so a "check windows" reading shortly after the heating goes off can be the room cooling down rather than a window actually open. The predicted-window sensor drives the display and insights only and never a heating decision, so a false reading here doesn't affect your heating. It's slated for retirement in a future major version rather than further tuning.

### Per-Zone Sensitivity (v3.3.0+)

Configured via **Options Flow → Zone Configuration**. The integration runs both detection modes automatically — active when the zone is heating or cooling, passive when it's idle — so there's no per-zone mode selector to choose from. What you can configure is sensitivity:

| Sensitivity | Min Readings | Temp Rate Threshold | Best For |
|-------------|-------------|---------------------|----------|
| Low | 4 readings | 0.4°C/min | Reducing false alarms in drafty rooms |
| Medium | 3 readings | 0.25°C/min | Most rooms (default) |
| High | 2 readings | 0.15°C/min | Rooms where you want fast detection |

### Window Detection Events (v3.3.0+)

HA events fire when a window is detected or cleared — useful for building your own automations:

| Event | When |
|-------|------|
| `tado_ce_window_predicted` | Window open detected |
| `tado_ce_window_predicted_cleared` | Detection cleared |

The Window Predicted sensor also tracks when the last detection happened, how many times today, and which detection mode was used. Daily count resets at midnight.

### Smoother Detection (v3.3.0+)

The Window Predicted sensor no longer flickers on/off rapidly. It waits for several stable readings before clearing a detection (3 readings on Low sensitivity, 2 on Medium, 1 on High).

---

## 🔄 Heating Cycle Detection

**Available:** v2.0.0+ | **Requirement:** Zones with heatingPower data | **Always Enabled**

Identifies complete heating cycles (heating ON → target reached → heating OFF) for accurate thermal analysis.

- Automatically detects complete heating cycles
- Minimum cycle: 10 minutes, maximum: 4 hours
- No configuration needed

**Monitor cycle health** via `sensor.{zone}_thermal_inertia` attributes:
- `cycle_count`: Total cycles analyzed
- `timeout_count`: Cycles that timed out
- Success rate >90% = heating system working well

**Computed by:** Tado CE — heatingPower state is polled from the Tado server; cycle boundaries (ON → target reached → OFF) are identified and stored locally by this integration.

---

## ⚡ Enhanced Controls

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

> ⬆ Boost replicates the Tado app's boost feature. HA official doesn't expose this.
> Smart Boost is CE exclusive — calculates optimal duration from thermal data.

#### 3. Climate Timer Service

```yaml
# Timer mode — boost for specific duration
service: tado_ce.set_climate_timer
target:
  entity_id: climate.living_room
data:
  temperature: 22
  time_period: 60

# Overlay mode (v2.3.0+) — until next schedule change
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

> v2.3.0: `time_period` is optional when `overlay` is specified. Supported: `next_time_block`, `manual`. Both Heating and AC zones.

#### 4. Enhanced Hot Water Timer

Control hot water with AUTO/HEAT/OFF modes. Timer duration configurable (1–1440 min, default: 60).

```yaml
service: tado_ce.set_water_heater_timer
target:
  entity_id: water_heater.hot_water
data:
  time_period: 60
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

Enable "Temperature Offset" in Configure to add `offset_celsius` attribute to climate entities.

#### 6. Climate Group Support (v2.2.3+)

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

# Resume schedule for all zones
service: tado_ce.resume_schedule
data:
  entity_id: group.tado_group
```

Supported services: `set_climate_timer`, `set_water_heater_timer`, `resume_schedule`.

#### 7. Open Window Services (v3.1.0+)

Three services for open window management, each serving a different purpose:

| Service | Purpose | When to Use |
|---------|---------|-------------|
| `tado_ce.activate_open_window` | Confirm Tado's own detection | Auto-Assist replacement — Tado has already detected an open window |
| `tado_ce.deactivate_open_window` | Cancel Tado's *detected* open-window state | After `activate_open_window`, or to clear an open window Tado detected itself. Does **not** clear `set_open_window_mode` (see below) |
| `tado_ce.set_open_window_mode` | Trigger from external sensors | Zigbee/Z-Wave contact sensors — no Tado detection needed. Clear it with `resume_schedule` or `restore_previous_state`, not `deactivate_open_window` |

**Set Open Window Mode** is the most useful for automations. It sets the zone to frost protection (5°C) with a timer:

```yaml
# Basic — uses zone's Open Window Detection timeout, or 15 min default
service: tado_ce.set_open_window_mode
target:
  entity_id: climate.bedroom

# Custom duration (seconds)
service: tado_ce.set_open_window_mode
target:
  entity_id: climate.bedroom
data:
  duration: 1800  # 30 minutes

# Indefinite — stays until manually resumed (v3.2.1+)
service: tado_ce.set_open_window_mode
target:
  entity_id: climate.bedroom
data:
  duration: 0
```

**Duration priority:** user-provided `duration` > zone's `openWindowDetection.timeoutInSeconds` > 15 min default.

**Automation example — contact sensor triggers open window mode:**

```yaml
automation:
  - alias: "Open Window — Bedroom"
    trigger:
      - platform: state
        entity_id: binary_sensor.bedroom_window_contact
        to: "on"
    action:
      - service: tado_ce.set_open_window_mode
        target:
          entity_id: climate.bedroom
        data:
          duration: 0  # indefinite until window closes

  - alias: "Close Window — Bedroom"
    trigger:
      - platform: state
        entity_id: binary_sensor.bedroom_window_contact
        to: "off"
    action:
      - service: tado_ce.resume_schedule
        target:
          entity_id: climate.bedroom
```

`set_open_window_mode` works by writing a frost-protection overlay, so you clear it the same way you clear any overlay: `resume_schedule` (back to the schedule) or `restore_previous_state` (back to whatever the zone was doing before). `deactivate_open_window` is the wrong partner here — it only cancels Tado's own detected open-window state (what `activate_open_window` confirms), and it has nothing to act on after `set_open_window_mode`.

#### 8. Restore Previous State (v3.3.0+)

Puts a zone back to whatever it was doing before the last change. Works with heating, AC, and hot water.

State is saved automatically the moment an overlay action lands successfully (timers, temperature changes, open window mode, preheat). If the cloud rejects or times out, nothing is saved, so a failed write can never leave a stale snapshot to replay later. If no state was ever saved for the zone, restore falls back to resuming the schedule. Saved state survives HA restarts and clears when you arrive home.

```yaml
# Restore a single zone
service: tado_ce.restore_previous_state
target:
  entity_id: climate.bedroom

# Restore multiple zones
service: tado_ce.restore_previous_state
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
      - service: tado_ce.restore_previous_state
        target:
          entity_id: climate.bedroom
```

> **Community Blueprint:** [@jeverley](https://github.com/jeverley) built a comprehensive [Window Mode Blueprint](https://raw.githubusercontent.com/jeverley/home-assistant-blueprints/refs/heads/main/blueprints/automation/tado_ce_window_mode_sensors.yaml) that handles multiple window/door sensors per zone with separate delays for nearby openings. [Import it directly](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fjeverley%2Fhome-assistant-blueprints%2Frefs%2Fheads%2Fmain%2Fblueprints%2Fautomation%2Ftado_ce_window_mode_sensors.yaml) or use the examples above as a starting point for your own automation.

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

The forced type is only fetched if that feature is turned on (mobile-device sync for `mobile_devices`, presence sync for `home_state`); if it's off, the call is ignored and logged. `zone_states` has no such toggle, it's always available. If the forced fetch lands on a cycle that was already due a full sync, that data is fetched twice that cycle (once for the scheduled sync, once forced), so keep the trigger interval sensible rather than every second. It's a single-home service; on a multi-home setup it raises an error listing the homes so you can route explicitly.

#### 12. Hub-level buttons

Three buttons live on the Tado CE Hub device (Settings → Devices & Services → Tado CE → the Hub entry). These are integration-level actions that affect the whole home, not a single zone.

| Button | What it does | When to press |
|--------|--------------|---------------|
| **Resume All Schedules** (`button.tado_ce_{home_id}_resume_all`) | Clears the manual overlay on every zone in one press, returning all rooms to their Tado schedule. Equivalent to calling `tado_ce.resume_schedule` per zone. | After a holiday-mode or wide automation override, when you want everything back on schedule at once. |
| **Refresh AC Capabilities** (`button.tado_ce_{home_id}_refresh_ac`) | Re-fetches each AC zone's supported modes from Tado's cloud. The integration watches for a re-pair or hardware swap on both AC and hot-water zones, by their device serial and firmware version, and refreshes the affected zone's capabilities on its own when either changes. The button itself stays AC-only for now: a manual override for the rare re-pair that keeps the same serial and firmware, so the change isn't visible to the integration. Costs one cloud call per AC zone (typically 1–3 calls). Only appears when the home has at least one AC zone. | After a re-pair that kept the same hardware, or whenever AC capabilities look wrong in HA (e.g. HVAC modes showing `[OFF]` only). |
| **Refresh Schedule** (`button.tado_ce_{home_id}_zone_{zone_id}_refresh_schedule`, per heating and AC zone) | Re-fetches a single zone's schedule from Tado and writes it through the cache. Appears for every heating and AC zone, regardless of whether Schedule Calendar is enabled. Costs 2–8 cloud calls per zone, depending on your Tado schedule type. | After editing a schedule in the Tado app. A heating zone's schedule also fetches automatically once Schedule Calendar is enabled, or once that zone uses Smart Valve Control's Valve Target mode; an AC zone has no automatic fetch and relies on this button alone. Outside of those cases, nothing else re-fetches it: the calendar entity, the `scheduled_target_temperature` attribute and the target shown for a zone that is off can all keep showing an old schedule until you press this. |

These buttons are visible in the device page's Controls section. Their entity_ids stay stable across reloads, so they can be wired into automations or dashboards if you want quick-access tiles.

#### 12. Child Lock and Early Start switches

Two TRV behaviour switches that surface settings from the Tado app directly in HA.

**Child Lock** — per-device switch. Each TRV in a zone has its own `switch.tado_ce_{home_id}_device_{serial}_child_lock`. Locks the physical buttons on the device so accidental presses (typically by children) don't override the schedule. You can lock some TRVs in a zone and leave others unlocked — useful when one TRV is in a child's room but another is on a hallway.

**Early Start** — per-zone switch. `switch.tado_ce_{home_id}_zone_{zone_id}_early_start`. When enabled, Tado's controller starts heating the zone before the next schedule block's start time so the room reaches target temperature *at* the block boundary rather than starting from cold at that moment. Tado's controller manages the actual lead time based on the zone's heating profile. One switch per zone applies to every TRV in the zone (Early Start is a Tado server-side scheduler feature, not a per-device thing).

Both surface as standard HA switches. Each toggle is an immediate cloud write — the Tado app reflects the change straight away. The integration confirms the new state on the next poll, so the HA entity may briefly show the previous value if the next poll is more than a few seconds out.

#### 13. Event listeners for automation builders

Tado CE fires several HA events that automations can subscribe to. Most fire on edge-triggered transitions (state change), so a naive listener that writes back to Tado on every event can trap on transient signals during quota-reset windows or partial poll responses.

| Event | Fires when | Defensive listener pattern |
|-------|-----------|---------------------------|
| `tado_ce_ready` | First API sync complete after setup or reload, deferred until Home Assistant has finished its overall startup so boot-time automation triggers are listening | Safe — once-per-session edge. No defence needed |
| `tado_ce_window_predicted` | Window-open prediction crosses the detection threshold | Smoothed by 3-reading window on Low sensitivity. Listener can act immediately |
| `tado_ce_window_predicted_cleared` | Detection clears | Same smoothing. Safe to act immediately |
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
      - service: tado_ce.restore_previous_state
        data:
          entity_id: "climate.{{ trigger.event.data.zone_name | slugify }}"
```

The 30s delay catches the case where Tado's API briefly returns `overlay=null` during a quota-reset window or partial poll response (the actual overlay still being in place server-side). The `overlay_type` re-check filters out the transient before any write reaches Tado. Without both guards, a naive listener risks firing on the transient and reverting the user's actual zone state.

#### 14. Turn Off All Rooms (v4.0.1+)

Mirrors the Tado app's "Turn OFF all rooms" button. Calls `tado_ce.turn_off_all_zones` with no parameters; every climate zone in the home (heating + AC) goes into a manual OFF overlay in one call. Hot water is out of scope — Tado's own button targets climate zones only, and Hot Water has its own UI surface in both apps.

Schedules stay suppressed until you manually resume each zone. To restart all zones at once, use the **Resume All Schedules** hub button; for a single zone, call `tado_ce.resume_schedule`.

```yaml
service: tado_ce.turn_off_all_zones
```

Designed for "outside temperature above X — turn off everything" automations where the next schedule block would otherwise override the off state. Example:

```yaml
automation:
  - alias: "Turn off heating when warm outside"
    trigger:
      - platform: numeric_state
        entity_id: sensor.tado_ce_{home_id}_outside_temperature
        above: 15
        for:
          minutes: 30
    action:
      - service: tado_ce.turn_off_all_zones
```

**Quota cost**: one cloud call per climate zone (heating + AC). For a 4-zone heating-only home, that's 4 calls; for an 8-zone mixed home, 8 calls. Bootstrap reserve still protects the last 3 calls — if quota is critically low when you call this service, the entire batch aborts before any zone is touched, and you get a `Quota critically low` error toast.

**Multi-home installs**: call once per home explicitly. Without an `entity_id` to anchor the call to a specific config entry, the service raises a `multiple_entries` error so you can pick which home you mean.

#### 15. Schedule Temperature (automation-friendly overrides, v4.1.0+)

Set a zone's target from an automation without Smart Valve Control treating it as a manual override. A normal `climate.set_temperature` looks identical to you grabbing the slider, so on a Smart Valve Control zone it trips the controller into back-off. `set_schedule_temperature` writes the same overlay but marks it as a programmatic change, so Smart Valve Control keeps compensating towards the new target and hands back to your schedule at the next Tado block. On a zone without Smart Valve Control it behaves as a plain overlay write.

```yaml
service: tado_ce.set_schedule_temperature
target:
  entity_id: climate.living_room
data:
  temperature: 21
  overlay: next_time_block   # or "manual" to hold until you resume the schedule
  force_override: false      # default — see below
```

`force_override` is off by default, so a manual override you set by hand on the slider is left alone. Turn it on when the automation should take priority even over a manual change, like a holiday schedule that must win. The main use case (a holiday or bridge-day automation) and a full example are in the [Smart Valve Control section](#-smart-valve-control) under "Holiday or Bridge-Day Automations".

#### 16. AC swing — separate vertical and horizontal axes (v4.0.1+)

AC zones expose a `Swing (vertical)` and a `Swing (horizontal)` dropdown, each populated from the cloud-reported capability set for your specific unit. Pick a fixed louver position on either axis to stop oscillation there, useful in bedrooms where a constantly moving louver is disruptive. Simple `On / Off` units keep a two-value dropdown; units that report fine-grained positions (`UP`, `MID_DOWN`, `LEFT`, `MID_RIGHT`) expose them directly, translated across all seven locales.

Automations calling the old unified `climate.set_swing_mode` (`off` / `vertical` / `horizontal` / `both`) still work with a deprecation warning, but the per-axis services `climate.set_swing_horizontal_mode` and `climate.set_swing_vertical_mode` are the current shape. The compatibility shim is removed in v5.0.0.

---

## 🌉 Bridge API Integration

**Available:** v3.2.0+ | **Requirement:** Tado Internet Bridge with serial + auth key | **Opt-in Configuration**

Direct communication with your Tado Internet Bridge for boiler flow temperature monitoring and control. Independent from the cloud API — errors never affect main polling.

### Overview

The Bridge API uses the serial number and auth key printed on the bottom of your Internet Bridge to authenticate directly with `my.tado.com/api/v2/homeByBridge/{serial}/`. This is separate from the OAuth-based cloud API used for zone data.

**Computed by:** Both — boiler output temperature, wiring state, and max output temperature are raw values from the Bridge API (`my.tado.com/api/v2/homeByBridge/{serial}/`); bridge connectivity status (binary sensor) is derived by Tado CE from consecutive-failure counting (marked disconnected after 3 consecutive failures).

### Entities

| Entity | Friendly Name | Type | Description |
|--------|--------------|------|-------------|
| `binary_sensor.tado_ce_{home_id}_bridge_connected` | Bridge Connected | Binary Sensor | Whether the Internet Bridge is online (on / off), from the bridge's own reported status |
| `sensor.tado_ce_{home_id}_boiler_wiring_state` | Boiler Wiring State | Sensor | Bridge installation status (Ready / Installing / Failed) |
| `sensor.tado_ce_{home_id}_boiler_output_temperature` | Boiler Output Temperature | Sensor | Real-time boiler output temperature (°C) |
| `number.tado_ce_{home_id}_boiler_max_output_temperature` | Boiler Max Output Temperature | Number | Control max flow temperature (25–80°C, 0.5°C step) |

### Bridge Connected Sensor (v3.3.0+)

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
3. Enter your **Bridge Serial Number** (starts with `IB`, printed on the bottom of your Internet Bridge)
4. Enter your **Bridge Auth Key** (also printed on the bottom)
5. Save — credentials are validated automatically against the Bridge API

Turning off the Internet Bridge toggle automatically cleans up all bridge-related entities. V2 bridges (`GW` serial) aren't supported by the Bridge API.

### How It Works

- Bridge data is fetched during each coordinator update cycle alongside cloud API data
- Bridge API errors are isolated — they never affect cloud data or trigger reauth
- The `boiler.outputTemperature.celsius` field in the wiring state response provides real-time boiler output temperature
- Max output temperature control PUTs to `boilerMaxOutputTemperature` endpoint

### Bridge API vs Cloud API Boiler Sensors

| Sensor | Source | Requires |
|--------|--------|----------|
| Boiler Flow Temp | Cloud API (`activityDataPoints.boilerFlowTemperature`) | OpenTherm-connected boiler |
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
  - entity: sensor.tado_ce_{home_id}_boiler_output_temperature
    name: "Boiler Output Temp"
  - entity: sensor.tado_ce_{home_id}_boiler_wiring_state
    name: "Bridge Status"
  - entity: number.tado_ce_{home_id}_boiler_max_output_temperature
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
          entity_id: number.tado_ce_{home_id}_boiler_max_output_temperature
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
          entity_id: number.tado_ce_{home_id}_boiler_max_output_temperature
        data:
          value: 65
```

---

## 🏠 HomeKit Local Control

**Available:** v4.0.0+ | **Requirement:** Tado Internet Bridge v3+ | **Opt-in Configuration**

Pair your Tado bridge via HomeKit to control heating and AC directly on your local network. Temperature and humidity updates arrive in real time instead of waiting for the next cloud poll, and local commands don't count against your API quota.

**Computed by:** Tado CE — the integration manages the HomeKit pairing, routes temperature reads (HomeKit-first with cloud fallback), tracks write attempts/successes/latency, and counts API calls saved. Raw temperature values come from the HomeKit accessory (local) or Tado cloud; all routing, fallback decisions, and counter arithmetic are done by Tado CE.

### What You Get

| Benefit | Description |
|---------|-------------|
| Faster controls | Temperature and mode changes go through your LAN (~1 second) instead of the cloud |
| Real-time sensor data | Temperature and humidity push instantly via HomeKit events |
| Fewer API calls | Cloud polling is reduced when HomeKit is connected — the integration tracks savings |
| Automatic fallback | If HomeKit becomes unavailable, the integration switches to cloud without interruption |
| Zero-config reconnect | If the bridge connection drops — or is unreachable when HA starts — the integration retries in the background and picks up local control as soon as the bridge is reachable again, without a reload |

### How mode and temperature are sourced

Tado CE talks to your Tado two ways at once, and each reading follows the side that's right for it:

- **Room temperature follows HomeKit** — instant, the moment the TRV reports a change. A new per-zone **Temperature source** setting (v4.1.0; Configure → Zone Configuration → Temperature) lets you override this per zone: Automatic prefers the fast HomeKit reading and falls back to the cloud, HomeKit always prefers local, Cloud always shows Tado's. An external temperature sensor, if you've set one, still takes precedence. This only governs what the dashboard shows; Smart Valve Control always calibrates against its own reading regardless.
- **HVAC mode follows the cloud** — the only side that knows you're following a schedule. A mode change you make in the Apple Home or Tado app shows up in Home Assistant on the next cloud poll (up to your polling interval). That's deliberate, not a shortfall: the official HomeKit Controller integration has no concept of a schedule at all (its "auto" is just heat/cool switching), and the official Tado integration has no fast local temperature. Tado CE gives you the fast temperature and still reflects an app-side mode change on the next poll.

### Known Limitations

| Limitation | Detail |
|------------|--------|
| HomeKit humidity (fallback only) | Humidity defaults to cloud data (0.1% precision, updates every poll cycle). HomeKit humidity is only used when cloud is unavailable — it provides 1% resolution with infrequent updates due to bridge firmware behaviour. Temperature uses HomeKit first (accurate, real-time). |
| Cloud-only data | Heating power, battery status, schedules, hot water, and geofencing are only available from Tado's cloud. HomeKit provides temperature and HVAC mode locally on heating zones. |
| Smart AC Control V3+ | Smart AC Control units are standalone WiFi accessories with their own HomeKit pairing code, separate from the Internet Bridge. Tado CE only handles the bridge's HomeKit pairing today, so Smart AC Control units fall through to the cloud path for every operation regardless of whether the bridge is paired locally. Adding standalone-unit pairing would need parallel multi-pairing management and HAP HeaterCooler service handling on top of the existing Thermostat path; not on the roadmap until that's been scoped against real hardware. Thanks to @MacrosorcH (Discussion #271) for the correction. |
| A TRV that goes quiet can hold a stale reading briefly | The bridge answers HomeKit on each TRV's behalf. If a TRV loses radio contact but the bridge keeps returning its last reading as if current, the room temperature can sit on that stale value for a few minutes before the freshness check gives up on it and falls back to cloud. A genuine bridge disconnect falls back to cloud right away; this only applies to the bridge staying connected while one TRV stops reporting. HomeKit exposes no per-TRV "last heard from" signal for us to catch it sooner. |
| Wireless Temp Sensors | Standalone temperature sensors (ST01) don't appear as HomeKit accessories — their data always comes from the cloud. |
| Single pairing | The bridge can only be paired with one HomeKit controller at a time. |
| External sensors don't control TRV valve | Per-zone external sensors change the displayed room temperature, but the TRV still uses its own sensor to control the valve. Enable **Smart Valve Control** (Offset Sync or Valve Target) in zone settings to automatically compensate — see [Smart Valve Control](#-smart-valve-control). |
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
| Cloud Sync Interval | Advanced Settings → HomeKit | 30 min | How often to fetch cloud data (humidity, heating power, overlays) when HomeKit is connected. Temperature uses HomeKit locally. **Note:** If you set a custom polling interval in Polling & API, all data refreshes at your custom rate instead — this setting only applies when using automatic polling. |
| Unpair | Advanced Settings → HomeKit | — | Remove the HomeKit pairing without removing the integration |

### Entities

| Entity | Type | Description |
|--------|------|-------------|
| HomeKit Connected | `binary_sensor` | Shows whether the HomeKit connection is active. Attributes include uptime, reconnect count, mapped/unmapped zones, and current write performance metrics. |
| HomeKit Reads Saved | `sensor` (disabled by default) | How many cloud read calls HomeKit local control has saved you since the last API quota reset. |
| HomeKit Writes Saved | `sensor` (disabled by default) | How many cloud write calls HomeKit local control has saved you since the last API quota reset. |

Reads Saved and Writes Saved are diagnostic sensors disabled by default — enable them under **Settings → Devices → Tado CE Hub → "X entities not shown"** if you want history charts of HomeKit's API savings. The counters survive HA restarts and reset when your Tado API quota resets (typically once per day).

The HomeKit Connected sensor's own attributes are connection state plus current-session write performance metrics:

**Connection state:**

| Attribute | Description |
|-----------|-------------|
| `last_connected` | When the bridge last connected (or `null` if never) |
| `last_disconnected` | When the bridge last disconnected (or `"Never"` if it hasn't since HA started) |
| `reconnect_count` | How many times the bridge has reconnected since the integration loaded |
| `uptime` | Time elapsed since the current connection established |
| `status` | `connected`, `disconnected`, or `not_configured` |
| `mapped_zones` | Heating zones currently routed via HomeKit |
| `unmapped_zones` | Heating zones still falling back to cloud (e.g. no matching HomeKit accessory) |

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

Priority: external sensor override > HomeKit (if fresh) > cloud.

This priority applies to all entities that read temperature or humidity — climate cards, sensor entities (temperature, humidity, mold risk, dew point, comfort level, etc.), window detection, insights, and preheat decisions. Climate entities expose the source via attributes; other entities use the same logic silently.

### What Stays Cloud-Only

Even with HomeKit connected, some data only comes from Tado's servers:

- Heating power percentage
- Battery status
- Schedules and overlays
- Hot water control
- Geofencing / presence detection
- Device firmware info
- Everything Smart AC Control V3+ does (those units are standalone HomeKit accessories Tado CE doesn't pair with — see Smart AC Control row above)

The integration handles this automatically; it fetches cloud-only data at the configured Cloud Sync Interval while using HomeKit for temperature, humidity, target temperature, and HVAC mode on heating zones.

### Workaround: TRV LED feedback after a local write

If you'd like the LED on the TRV to flash after a HomeKit-routed write (the way it does when you change the temperature in the Tado app), press the device's Identify button or call the `tado_ce.identify_device` service straight after the write:

```yaml
service: tado_ce.identify_device
data:
  device_serial: "VA1234567890"  # short serial of the TRV, visible in the device's diagnostic attributes
```

When the bridge is connected this flashes locally over HomeKit at no quota cost. If the bridge isn't reachable it falls back to the cloud, which costs one call per flash, marginal on the 1,000-call tier and negligible on the 20,000-call legacy tier, but worth bearing in mind on the 100-call free tier if you trigger it from every write. The temperature change itself lands through HomeKit either way.

---

## 🌡️ Weather Compensation

**Available:** v3.3.0+ | **Requirement:** Bridge API configured OR cloud outdoor temperature | **Opt-in Configuration**

Automatically adjusts your boiler's flow temperature based on outdoor temperature, so your heating runs more efficiently in mild weather and ramps up when it's cold.

### Overview

Weather compensation uses a heating curve to map outdoor temperature to a target boiler flow temperature. When it's mild outside, the boiler runs at a lower flow temperature (saving energy). When it's cold, the flow temperature increases to maintain comfort.

The engine runs every coordinator update cycle. A 10-minute hold between adjustments prevents oscillation, and outdoor temperature is smoothed (EMA or rolling average) to avoid reacting to brief fluctuations.

**Computed by:** Tado CE using Tado server data — outdoor temperature is fetched from Tado's cloud API, then Tado CE applies the heating curve formula (plus optional room-compensation offset and EMA/rolling-average smoothing) to derive the target boiler flow temperature. The raw reading is Tado server data; the sensor value is a Tado CE calculation.

### Heating System Presets

| Preset | Max Flow Temp | Description |
|--------|---------------|-------------|
| Radiators Standard | 65°C | Traditional radiators |
| Radiators Low Temp | 55°C | Modern low-temperature radiators |
| Underfloor | 45°C | Underfloor heating systems |
| Custom | User-defined | Full control over all parameters |

Presets automatically calculate the slope from your min/max flow and design/shutoff temperatures, so the heating curve spans the full outdoor range without flat zones. Custom preset gives you full manual control over the slope.

### Configuration

1. Go to **Settings → Tado CE → Configure → General Settings**
2. Under **Hardware Connections**, enable the **Internet Bridge** toggle (required for real-time flow temperature data)
3. Under **Smart Automations**, enable **Weather Compensation**
4. Save, then open **Advanced Settings → Weather Compensation** to pick a **Heating System Preset** (or choose Custom for full control)
5. Save again — the engine starts adjusting on the next update cycle

**Custom parameters (when preset = Custom):**

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| Slope | 1.5 | 0.1–3.0 | Heating curve steepness |
| Design Outdoor Temp | -5°C | -30 to 10°C | Coldest expected outdoor temperature |
| Max Flow Temp | 65°C | 25–80°C | Maximum boiler flow temperature |
| Min Flow Temp | 25°C | 20–50°C | Minimum boiler flow temperature |
| Shutoff Temp | 18°C | 5–25°C | Outdoor temp above which heating stops |
| Smoothing Method | EMA | EMA / Rolling Avg | How outdoor temperature is smoothed |
| Smoothing Window | 60 min | 15–240 min | Smoothing time window |
| Room Compensation | Off | On/Off | Adjust flow temp based on room temperature |
| Room Compensation Factor | 3.0 | 1.0–5.0 | How strongly room temp affects flow temp |
| Step Size | 1.0°C | 0.5–5.0°C | Minimum change between adjustments |
| Hysteresis | 1.0°C | 0.5–5.0°C | Deadband to prevent oscillation |

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
| `smoothing_method` | EMA or rolling_avg |
| `smoothing_window` | Smoothing window in minutes |

### How the Heating Curve Works

```
Target Flow Temp = Max Flow Temp - Slope × (Outdoor Temp - Design Outdoor Temp)
```

For presets, the slope is automatically calculated:
```
Auto Slope = (Max Flow - Min Flow) / (Shutoff Temp - Design Outdoor Temp)
```

This ensures the curve reaches exactly `min_flow_temp` at the shutoff temperature — no flat zones where outdoor changes have no effect.

Example with Radiators Standard (max 65°C, min 25°C, design -5°C, shutoff 18°C → auto-slope 1.74):
- Outdoor -5°C → Flow 65°C (full power)
- Outdoor 5°C → Flow 47.6°C
- Outdoor 10°C → Flow 38.9°C
- Outdoor 18°C+ → Heating off (shutoff)

### Usage Scenarios

#### Scenario 1: Standard Radiator Setup

Select "Radiators Standard" preset. The default curve works well for most homes with traditional radiators. Monitor `wc_target_flow_temp` for a few days to verify the curve matches your comfort needs.

#### Scenario 2: Underfloor Heating

Select "Underfloor" preset. The gentler slope (0.8) and lower max flow temp (45°C) protect UFH systems from overheating. If your floors feel cold in very cold weather, increase the slope slightly using Custom mode.

#### Scenario 3: Fine-Tuning with Room Compensation

Enable Room Compensation to let the engine adjust flow temperature based on actual room temperatures. If rooms are consistently too warm, the engine reduces flow temp. If rooms are cold, it increases. The compensation factor controls how aggressively it responds.

#### Scenario 4: Dashboard Card

```yaml
type: entities
entities:
  - entity: sensor.tado_ce_hub_ce_wc_target_flow_temp
    name: "Target Flow Temp"
  - entity: sensor.tado_ce_hub_ce_wc_status
    name: "WC Status"
  - entity: number.tado_ce_{home_id}_boiler_max_output_temperature
    name: "Current Max Flow Temp"
```

---

## 🎯 Smart Valve Control

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
| **Tado app shows** | ✅ Your external sensor's temperature (accurate) | ⚠️ An inflated target (e.g. 25°C when you want 20°C) |
| **Home Assistant shows** | ✅ Your external sensor's temperature | ⚠️ The same inflated target, once Tado's data catches up. `desired_target` carries yours |
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
2. Go to **Settings → Tado CE → Configure → pick a zone → External Sensors section**
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
- Offset is clamped to ±10°C (Tado's hardware limit) — see [When the clamp fires](#when-the-clamp-fires) below
- Only writes when the change exceeds your configured sensitivity threshold (default 0.5°C, adjustable 0.5–3.0°C per zone)
- Readback verification: after each successful write, the integration reads the offset back from Tado and only updates the local cache when the confirmed value matches. A failed write (rate limit, server error) leaves the cache unchanged rather than poisoning it with a value the TRV never received (v4.0.0+)
- Periodic drift refresh: the integration re-reads each TRV's stored offset from Tado and reconciles the local cache. With HomeKit connected, the refresh follows your **HomeKit Cloud Refresh** setting (matching the rest of cloud sync); HomeKit-off installs run it every 30 minutes. This catches the case where Tado's own adaptive calibration (or a manual edit in the Tado app) changes the stored offset behind the integration's back, which would otherwise feed a wrong baseline into the next correction and could drift the cache to the ±10°C limit (v4.0.0+)
- If your external sensor goes offline, the last offset is preserved (no sudden jump)

**Offset Sync + the Tado app:**
The Tado app will show your external sensor's temperature as the room temperature. Schedules, geofencing, and Tado's own heating logic all use this corrected reading. You don't need to change anything in the Tado app.

> **Note:** If you previously had a manual offset set in the Tado app or via an automation, Offset Sync will overwrite it. That's intentional — it keeps the offset in sync with your external sensor continuously.

> **Important:** If you switch from Offset Sync to Off or Valve Target, the last written offset remains on the device. Reset it manually in the Tado app if you want to return to the TRV's uncorrected reading.

#### When the clamp fires

Tado stores device offsets in a single signed byte, capped at ±10°C. When your external sensor and the TRV differ by more than that, Offset Sync writes the ±10°C boundary — but the physical gap remains uncorrected beyond that point.

The climate entity exposes two attributes so you can tell when this is happening:

- `offset_clamped` — `true` when the last write was clamped, `false` otherwise
- `offset_clamp_direction` — `"hit_max"` (required correction was larger than +10°C), `"hit_min"` (smaller than −10°C), or `"none"` (in range)

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
> what target to hold. The integration fetches this automatically for any zone
> using Valve Target, on the ordinary polling cycle, whether or not Schedule
> Calendar is enabled. If you've just edited a schedule in the Tado app and want
> the change reflected straight away rather than waiting for the next poll,
> press that zone's Refresh Schedule button (see [Hub-level buttons](#12-hub-level-buttons)).
> Offset Sync doesn't read the schedule and is unaffected.

### Write Path

- **HomeKit first** — adjustments go through the local bridge with zero API cost
- **Cloud fallback** — if HomeKit is unavailable, writes go through the Tado cloud API (rate-limited to 1 write per zone per 5 minutes to protect your quota)
- Writes are debounced (3-second window) to batch rapid sensor updates into a single write

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
    🔥 SVC Active — valve target {{ state_attr(c, 'valve_target') }}°C,
    desired {{ state_attr(c, 'desired_target') }}°C
  {% elif state_attr(c, 'valve_control_backed_off') %}
    ⏸️ SVC Backed Off (manual override detected)
  {% else %}
    ✅ SVC Idle — room at temperature
  {% endif %}
```

#### Scenario 5: Night Mode with Reduced Heating

Your schedule drops to 16°C at night. The external sensor reads 18°C (room is still warm from the evening). SVC sees `external (18) > desired (16) + hysteresis (0.3)` and stays idle — no valve adjustment needed. The TRV follows the schedule normally.

SVC only activates when the room is **colder** than your desired temperature. It never fights against the schedule to cool a room down.

#### Scenario 6: Holiday or Bridge-Day Automations

Tado's schedule is one weekly programme per zone, with no holiday support. The usual workaround is an automation that raises the target on a one-off day, say following a Sunday profile on a public holiday. The catch: with SVC on, a normal `climate.set_temperature` from that automation looks exactly like you grabbing the slider, so the controller backs off and stops compensating for the rest of the morning. The room stays cold on the one day you wanted it warm.

The `tado_ce.set_schedule_temperature` service fixes this. It sets the target the same way, but SVC recognises it as a scheduled change rather than a manual override, keeps compensating towards it, and resumes your normal schedule at the next block change.

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
      - service: tado_ce.set_schedule_temperature
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
| External sensor goes offline while active | Resumes Tado schedule (deletes overlay), transitions to idle |
| TRV reading unavailable | Bang-bang fallback — sets TRV to max_temp to keep heating |
| Both sensors unavailable | Resumes schedule, stays idle |
| Valve target exceeds min/max bounds | Clamped to zone's configured min_temp / max_temp, then hard-capped at 30°C |
| HomeKit write followed by cloud check | 60-second grace period after each write before checking for manual overrides — prevents false back-offs during HomeKit-to-cloud sync |
| HA crashes while controller is active | Stale overlay is cleaned up automatically on next startup |
| TRV has a non-zero temperature offset | Warning logged on startup — reset offset to 0 to avoid double compensation |
| All Tado schedule blocks are OFF | Controller recovers from backed-off state when overlay changes (e.g. HA automation sets a new temperature) |

### State Persistence

Controller state (active/idle/backed-off, last valve target, desired target, overlay ownership) persists across HA restarts. On restart, the controller recalculates before writing and cleans up any stale overlays from a previous session. If the controller was active but the desired target wasn't saved (e.g. upgrading from an older version), it resets to idle and re-captures a fresh desired target on the next heating cycle.

### Limitations

| Limitation | Detail |
|------------|--------|
| Heating zones only | AC zones are not supported in this release |
| Schedule resume is cloud-only | Deleting overlays requires the Tado cloud API (no HomeKit equivalent) |
| TRV precision | HomeKit rounds to 0.1°C; cloud API accepts 0.01°C |

---

## 🔧 Optional Features

**Available:** Various versions | **Requirement:** Varies | **Opt-in Configuration**

### Schedule Calendar

Shows heating schedules as calendar events. Enable in Configure → "Schedule Calendar".

| Entity | Friendly Name |
|--------|--------------|
| `calendar.{zone}` | Schedule |

**Computed by:** Both — Tado server provides the weekly schedule blocks via `/api/v2`; Tado CE converts them into HA calendar events.

### Boiler Flow Temperature

Monitors OpenTherm boiler flow temperature. Auto-detected if available.

| Entity | Friendly Name |
|--------|--------------|
| `sensor.tado_ce_boiler_flow_temperature` | Boiler Flow Temp |

**Computed by:** Tado server — raw OpenTherm boiler flow temperature reported directly from the Tado device via `/api/v2`.

### Device Tracking

Tracks mobile device presence (home/away). Enable "Mobile Device Tracking" in Configure.

| Entity | Friendly Name |
|--------|--------------|
| `device_tracker.tado_ce_{device_name}` | {device_name} |

API usage: 1 call per full sync (full sync runs at HA startup). Enable "Sync Mobile Frequently" if you want device-tracker updates on every poll instead — useful if you build presence-driven automations.

**Computed by:** Tado server — mobile device presence state (home/away) fetched from the Tado cloud API.

### Home State Sync & Presence Mode

Syncs home/away presence state. Enable "Home/Away State Sync" in Configure.

| Entity | Friendly Name | Description |
|--------|--------------|-------------|
| `select.tado_ce_presence_mode` | Presence Mode | Control: auto / home / away |
| `binary_sensor.tado_ce_home` | Home | Read-only home/away status |

**Computed by:** Tado server — home/away state is read from and written to Tado's cloud API; the `presence_mode` select entity pushes locks back to Tado.

### Override duration

Controls how long a temperature change made from Home Assistant lasts before the schedule resumes.

| Entity | Friendly Name |
|--------|--------------|
| `select.tado_ce_overlay_mode` | Override duration |
| `select.tado_ce_overlay_timer_duration` | Overlay Timer |

**Options** (verbatim from the Tado app):

| Option | Description |
|--------|-------------|
| Until you resume schedule | Stays until you resume the schedule (the default) |
| Until next automatic change | Reverts at the next automatic change in your Tado schedule |
| Timer | Reverts after the timer duration (15 minutes to 12 hours) |

**Computed by:** Tado server — the current overlay termination mode is read from and written to Tado's cloud API; Tado enforces the chosen duration when a temperature override is set.

### Understanding Geofencing vs Presence Mode

Geofencing is a Tado account-level setting configured in the Tado app, not in this integration.

| Scenario | "Auto" Mode Behaviour |
|----------|---------------------|
| Geofencing **enabled** | Tado auto-switches Home/Away based on mobile locations |
| Geofencing **disabled** | Stays in current state — no automatic switching |

**How Presence Lock Works:**
- **"home" or "away"**: Creates a presence lock that overrides geofencing
- **"auto"**: Deletes the presence lock. If geofencing enabled, Tado resumes automatic control. If disabled, state stays as-is.

### Multi-Language (v3.0.0)

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
Home → Away → Auto = Back to Home?  ❌ WRONG
Home → Away → Auto = Stays Away     ✅ CORRECT
```

When geofencing is disabled, "Auto" just removes the lock — it doesn't change state. Use "Home"/"Away" directly, or use HA automations with other presence detection.

---

## 📡 Automation Events

**Available:** Various versions | **Requirement:** None | **Automatic**

Tado CE fires HA bus events at key moments — you can use these as automation triggers without polling entity states.

**Computed by:** Tado CE — the integration monitors API data and internal state, then fires these HA bus events based on its own logic (load completion, passive window algorithm, overlay changes, schedule refreshes). No Tado server push; CE derives the trigger.

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

### Window Detection Events

Fires when the passive window detection algorithm detects a temperature drop consistent with an open window, or when the detection clears. See [Window Detection Events](#window-detection-events-v330) for details.

| Event | When | Payload |
|-------|------|---------|
| `tado_ce_window_predicted` | Window open detected | `zone_id`, `zone_name`, `confidence`, `temp_drop`, `detection_mode`, `recommendation` |
| `tado_ce_window_predicted_cleared` | Detection cleared | `zone_id`, `zone_name` |

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

## 🔗 Multi-TRV Zones

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

**HomeKit temperature shows the last reporting device.** When HomeKit is connected, temperature events from all TRVs in a zone update the same cache entry. If two TRVs report slightly different temperatures (common — they're on different radiators), the displayed value is whichever TRV reported most recently, not an average. The difference is typically small (< 0.5°C) since they're in the same room.

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

## 🏠 Per-Zone Configuration

**Available:** v3.1.0+ | **Requirement:** None | **Options Flow Configuration**

Customize settings for each individual zone via **Settings → Tado CE → Configure → Zone Configuration**.

> **v3.1.0 change:** All per-zone settings moved from individual HA entities to the centralised Options Flow menu. No config entities are created — settings live in the Options Flow.

### Available Settings

Organised in the order they appear in the Options Flow — fundamental limits first, hardware next, sensors that augment Tado, smart features that depend on those sensors, and runtime override behaviour last.

**Computed by:** Tado CE — all settings in this table are stored in the integration's Options Flow config entry and applied by Tado CE at runtime. None of these values come from or are pushed to the Tado cloud.

| Setting | Description | Applies To |
|---------|-------------|------------|
| **Temperature Limits** | | |
| Min Temperature | Minimum allowed temperature (5–25°C) | All zones |
| Max Temperature | Maximum allowed temperature (15–30°C) | All zones |
| Surface Temp Offset | Fine-tune mold risk surface temperature estimate (-5 to +5°C) | All zones |
| **Heating System** | | |
| Heating Type | Radiator or Underfloor Heating | Heating zones |
| UFH Buffer | Extra preheat buffer for underfloor heating (0–60 min) | UFH zones |
| Adaptive Preheat Mode | Off, Active, or Passive | Heating zones |
| **External Sensors** | | |
| External Temperature Sensor | Use any HA sensor instead of Tado's built-in | All zones |
| External Humidity Sensor | Use any HA sensor instead of Tado's built-in | All zones |
| Smart Valve Control Mode | Off / Offset Sync (recommended) / Valve Target (advanced) | Heating zones (with external temp sensor) |
| Offset Sync Sensitivity | How much the offset must change before writing (0.5–3.0°C) | Offset Sync mode only |
| **Smart Features** | | |
| Smart Comfort Mode | Adjust target temperature based on outdoor conditions | Heating zones (when Smart Comfort is enabled globally) |
| Window Type | Window insulation type for mold risk calculation | All zones |
| Window Predicted Sensitivity | Low, Medium, or High | All zones |
| **Manual Temperature Override** | | |
| Override duration | How long a temperature change made from Home Assistant lasts (Until you resume schedule, Until next automatic change, Timer) | All zones |
| Override Timer | Timer duration when override duration is Timer (up to 12 hours) | All zones |
| Temperature source | Which reading the dashboard shows for this zone (Automatic, HomeKit, Cloud) | All zones |

### Override duration options

| Option | Behaviour |
|--------|----------|
| Until you resume schedule | Stays until you resume the schedule (the default for new zones) |
| Until next automatic change | Reverts at the next automatic change in your Tado schedule |
| Timer | Reverts after the timer duration (15 minutes to 12 hours) |

The starting default for a new zone is "Until you resume schedule". Zones you'd already configured before this release keep whatever setting they had. If you want a different default per zone, set it here under Override duration. (The `next_time_block` value still works as a service argument for anyone using it in automations; it maps to "until next automatic change".)

> **v4.1.0:** the three labels above ("Until you resume schedule" / "Until next automatic change" / "Timer") replaced the older four-option wording to match the Tado app, and the Timer cap rose from 3 hours to 12. Existing zones keep their setting; only the labels and the cap changed.

---

## 🎛️ Per-Zone Entity Types

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

**Device-state refresh cadence (Battery, Connection, Firmware version):** these sensors read from Tado's device-info endpoint, which the integration refreshes on a periodic schedule and whenever a Tado-side topology change is detected. The cadence depends on your daily API quota. Paid Auto-Assist accounts refresh hourly, so a controller dropping its cloud uplink (E04 errors, mains blip, Wi-Fi flap) shows up within the hour. Free-tier accounts refresh every four hours to keep the periodic cost under roughly six per cent of the daily quota. A re-pair or hardware swap triggers an immediate refresh on top of the periodic schedule.

---

## 🔁 Reset to Defaults

Located at **Settings → Tado CE → Configure → Reset to Defaults**. Lets you revert tuning parameters without re-entering your Tado account or re-pairing hardware.

**Per-category reset** — resets only the tuning fields for that feature. The feature itself stays enabled so the entities don't disappear:

| Scope | What resets |
|-------|-------------|
| Smart Comfort | Mode, feels-like toggle, window type default, history days, outdoor temperature entity |
| Thermal Analytics | Zones selection, history days, minimum cycles, inertia threshold |
| Weather Compensation | Preset, slope, design/max/min/shutoff temps, smoothing method + window, room compensation + factor, step size, hysteresis |
| Internet Bridge | Bridge serial, bridge auth key (re-entry required to keep Bridge API / Weather Compensation working) |
| HomeKit | Cloud sync interval (pairing credentials are preserved) |
| Polling & API | Day/night start hours, custom intervals, presence/weather/mobile refresh intervals, refresh delay, API history retention, smart actions debounce, device sync delay, frequent mobile sync, hot water timer default |

**Reset Everything** — turns every feature toggle OFF, then applies all default tuning values. **Preserves**:

- Your Tado account (refresh token, home ID)
- Internet Bridge credentials (if previously entered)
- HomeKit pairing credentials (if previously paired)

After resetting everything, you'll need to re-enable the features you want from General Settings — but you don't need to re-authenticate or re-pair.

### What's NOT covered

**Per-zone configuration** (external sensors, Smart Valve Control, overlay mode, temperature limits, window detection) is **not** included in the Reset to Defaults scope. Per-zone settings are stored separately, and resetting them in bulk would wipe potentially-expensive-to-rebuild per-zone state. To reset a zone, open **Zone Configuration → (zone)** and change the values back manually.

### Safety behaviour

- Confirmation step — the final "Confirm Reset" screen describes exactly what will be wiped and what's preserved
- No toggle flipping during per-category reset — enabling/disabling features is a separate user action
- Repair notifications, HomeKit pairing files, and zone_config.json are all preserved

---

## 🎯 Configuration Scenarios

### Scenario 1: Small Apartment (1–2 rooms, 100 calls/day)

```yaml
Adaptive Polling: Enabled (default)
Smart Comfort: Disabled (save quota)
Weather Sensors: Disabled
Mold Risk: Enabled (important for small spaces)
Window Type: Double Pane

Expected: ~80–120 calls/day, polling ~15–20 min
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

Day (16h): 32 syncs × 2 = 64 calls
Night (8h): 4 syncs × 2 = 8 calls
Full sync (6h): 4 × 2 = 8 calls
Total: ~80 calls/day (20% buffer)
```

### Scenario 6: High Quota (1000+) All Features

```yaml
Custom Intervals: Empty (use adaptive)
All features: On
Sync Mobile Frequently: On
Smart Comfort Mode: Moderate
History Days: 30

Expected: ~576 calls/day at ~5 min intervals
```

### Scenario 7: Mixed Zone Types (Heating + AC)

| Feature | Heating Zones | AC Zones |
|---------|---------------|----------|
| Thermal Analytics | ✅ Available | ❌ No heatingPower data |
| Smart Comfort | ✅ Heating patterns | ✅ Cooling patterns |
| Condensation Risk | ❌ N/A | ✅ AC-specific |
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
          title: "⚠️ Low Boiler Flow Temperature"
          message: "Flow temp: {{ states('sensor.tado_ce_boiler_flow_temperature') }}°C"
```

---

## 💡 Actionable Insights

**Available:** v2.2.0+ | **Requirement:** None | **Always Enabled**

Intelligent, context-aware recommendations across all zones for comfort, mold prevention, and energy optimization.

### v3.0.0 Insight Enhancements

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
| Window Predicted | High | Rapid temperature drop |
| Battery Low | Critical/Low | Device battery LOW/CRITICAL |
| Device Offline | High | Connection lost |
| Preheat Timing | Medium | Preheat time exceeds schedule gap |
| Schedule Deviation | Medium | Consistent deviation from schedule |
| Heating Anomaly | High | Power ≥80% but temp change <0.5°C for 60+ min |
| Condensation Risk | Medium/High/Critical | AC zone condensation detected |
| Overlay Duration | Medium/High | Manual override active too long |
| Frequent Override | Medium | Multiple manual overrides recently |
| Heating Off Cold Room | High | Heating off but room below comfort |
| Early Start Disabled | Low | Preheat feature not enabled |
| Poor Thermal Efficiency | Medium/High | Below expected threshold |
| Schedule Gap | Medium | Large gap leaving zone unheated |
| Boiler Flow Anomaly | High | Flow temp outside expected range |
| Humidity Trend | Medium | Sustained rising humidity |
| Device Limitation | Low | Hardware limitations affecting features |

**Home-level insights (in `sensor.tado_ce_home_insights` only):**

| Insight | Priority | Trigger |
|---------|----------|---------|
| Cross-Zone Mold | High | 3+ zones with Medium+ mold risk |
| Cross-Zone Windows | High | 2+ zones with window predicted open |
| Cross-Zone Condensation | High | Multiple zones with condensation |
| Cross-Zone Efficiency | Medium | Significant efficiency variation |
| Temperature Imbalance | Medium | Large temp difference between zones |
| Humidity Imbalance | Medium | Large humidity difference |
| Away Heating Active | High | Away mode but heating still active |
| Home All Off | Low | Everyone home but all heating off |
| Solar Gain | Low | Solar gain detected |
| Solar AC Load | Medium | Strong solar increasing AC load |
| Frost Risk | Critical | Outdoor temp near freezing |
| Heating Season Advisory | Low | Seasonal guidance |
| Geofencing Offline | High | Mobile device for geofencing offline |
| API Usage Spike | Medium/High | Unusual API call rate spike |
| API Quota Planning | Medium/High | Projected exhaustion <6h before reset |
| Weather Impact | Medium | Outdoor temp >5°C below 7-day average |

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
          title: "🚨 Critical Home Issue"
          message: >
            {{ state_attr('sensor.tado_ce_home_insights', 'top_recommendation') }}
```

---

## ⚙️ Settings Configured in the Tado App, Not Tado CE

Some settings that affect your heating system can only be changed in the Tado app. They are listed here because users commonly look for them in Home Assistant and cannot find them. Tado's API does not expose these as writable endpoints. Tado CE can read some of these values and use them internally, but cannot write them back.

| Setting | Where in the Tado app | What it controls |
|---|---|---|
| **Smart Schedule** (time blocks, setpoints, modes) | Rooms & Devices → [Room] → Schedule | Zone heating schedules. Tado CE can read and refresh schedule data but cannot create or edit time blocks or setpoints. |
| **Away temperature per zone** | Rooms & Devices → [Room] → Settings → Away temperature | The target temperature applied when the home switches to Away mode. Tado CE reads the resulting state but does not expose this as a writable field. |
| **Open Window Detection timeout** | Rooms & Devices → [Room] → Open Window Detection → Duration | Per-zone timeout (in seconds) used when Tado's own open-window detection triggers. Tado CE's `set_open_window_mode` service uses this as a fallback duration when no explicit `duration` is supplied. The value itself is not writable via the integration. |
| **Open Window Detection sensitivity** | Rooms & Devices → [Room] → Open Window Detection → Sensitivity | How sensitive Tado's own open-window algorithm is. This is separate from Tado CE's passive open-window predictor, which is configured entirely within HA. |
| **Hysteresis / Acceptance Range** | Rooms & Devices → [Room] → Settings → Acceptance range | How close to the target temperature the TRV must get before it stops heating. Tado manages this per zone; it is not readable or writable via the API. |
| **Minimum On/Off Time** | Rooms & Devices → [Room] → Settings → Minimum run time | The minimum duration a heating cycle must run (or stay off) before switching state. Tado manages this server-side; it is not exposed to the integration. |
| **Early Start lead time** | Rooms & Devices → [Room] → Settings → Early Start | Early Start can be toggled on/off in the Tado app. The actual lead time (how many minutes ahead Tado begins heating) is computed by Tado's algorithm from the zone's heating profile and is not configurable or readable via Tado CE. |
| **Smart Schedule preheating level** | Rooms & Devices → [Room] → Schedule → [Time block] → Preheat | How aggressively Tado preheats before a scheduled block. Set per schedule block in the Tado app; not exposed via the API. |
| **Geofencing (auto home/away switching)** | Settings → Presence → Geofencing | Whether Tado switches the home between Home and Away based on registered device locations, and which devices participate. Tado CE can read the resulting presence state and lock/unlock it via the Presence Mode selector, but cannot configure geofencing itself. |
| **Device offset (TRV temperature calibration)** | Rooms & Devices → [Room] → [Device] → Offset | A fixed temperature offset stored on the TRV hardware (±10 °C). Offset Sync writes this automatically from Tado CE. If you want to clear or inspect the raw value before enabling Valve Target Mode, use the Tado app. Note that Tado's own adaptive calibration can also update this value independently. |
| **Energy IQ meter unit** | Energy IQ → Energy Settings → Meter unit | The billing unit (m³ for gas, kWh for electricity) applied to readings submitted via `tado_ce.add_meter_reading`. Tado applies this unit server-side; it can only be set in the Tado app. |
| **Zone name** | Rooms & Devices → [Room] → Settings → Room name | The display name of a zone. Tado CE reads this and uses it as the entity name; it cannot be changed from within Tado CE or HA. |
| **Room and device assignments** | Rooms & Devices | Which TRVs, sensors, and extension kits belong to which room. Zone membership is set during Tado device setup and cannot be changed via the integration. |

---

## 🔧 Troubleshooting

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

**Causes:** Boiler not OpenTherm-compatible, Tado system doesn't support OpenTherm.

**Solution:** Verify boiler supports OpenTherm, check Tado app for flow temp data.

### Bridge API Sensors Showing "Unknown"

**Causes:** Wrong data path (fixed in v3.2.2), bridge credentials invalid, bridge offline.

**Solution:** Update to v3.2.2+, verify credentials in Configure → Bridge Configuration, check bridge is online. Enable debug logging:

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

**Solution:** Enable "Mobile Device Tracking", enable geo-tracking in Tado app, wait for next full sync (6h).

### Temperature Offset Not Applying

**Causes:** Offset attribute not enabled, value out of range, service call failed.

**Solution:** Enable "Temperature Offset" in Configure, verify offset within range (-10 to +10°C), check HA logs.

### Preheat Advisor Shows 0 Minutes

**Causes:** Already at target, heating rate unknown, next schedule temp same as current.

**Solution:** Check current vs next schedule temp, wait for thermal analytics data (3–5 cycles), verify `_avg_heating_rate` has valid value.

---

## 📚 Related Documentation

- [ENTITIES.md](ENTITIES.md) — Complete entity reference (87 entities)
- [README.md](README.md) — Installation and setup
- [ROADMAP.md](ROADMAP.md) — Planned features and ideas

---

**Last Updated:** v4.0.2 (2026-06-07)
