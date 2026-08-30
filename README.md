# Tado CE - Community-maintained Tado integration for Home Assistant

<div align="center">

<!-- Platform -->
![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.11%2B-blue?style=for-the-badge&logo=home-assistant) ![Python](https://img.shields.io/badge/Python-3.13%2B-blue?style=for-the-badge&logo=python&logoColor=white) ![Tado](https://img.shields.io/badge/Tado-V2%2FV3%2FV3%2B-1E3A8A?style=for-the-badge) ![HACS](https://img.shields.io/badge/HACS-Default-41BDF5?style=for-the-badge)

<!-- Status -->
![Stable](https://img.shields.io/badge/Stable-4.4.0-brightgreen?style=for-the-badge) ![License](https://img.shields.io/badge/License-AGPL--3.0-lightgrey?style=for-the-badge) ![Coverage](https://img.shields.io/badge/Coverage-94%25-green?style=for-the-badge)

<!-- Community -->
![GitHub stars](https://img.shields.io/github/stars/hiall-fyi/tado_ce?style=for-the-badge&logo=github) ![GitHub issues](https://img.shields.io/github/issues/hiall-fyi/tado_ce?style=for-the-badge&logo=github) ![GitHub Release Date](https://img.shields.io/github/release-date/hiall-fyi/tado_ce?style=for-the-badge&logo=github)

<!-- Support -->
[![Buy Me A Coffee](https://img.shields.io/badge/Support-Buy%20Me%20A%20Coffee-FFDD00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/hiallfyi)

**The Tado HA community's wish list, shipped. Local HomeKit control, multi-home, smart valve compensation, weather compensation, and a polling system that respects Tado's API quota.**

[Quick Start](#quick-start) • [Features](#features) • [Configuration](#configuration) • [FAQ](#faq) • [Discussions](https://github.com/hiall-fyi/tado_ce/discussions)

</div>

---

## About

Tado CE is an alternative Home Assistant integration for Tado, built around requests the official integration hasn't picked up. Almost every feature here started as a GitHub issue or a Discussions thread — users with low API quotas asking for HomeKit local reads, OpenTherm boiler owners asking for flow-temperature modulation, multi-home accounts asking for proper isolation, radiator-TRV owners asking for external-sensor compensation. The 4.0 release is the first stable cut that pulls all of those together.

Developed independently of Tado. Targets Tado V2, V3, and V3+ hardware.

---

## Why use Tado CE

Three situations where it's worth considering:

1. **You've hit the Tado API quota.** From 2025, Tado limits most households to 100 API calls per day. The official integration is cloud-polling only, so dashboards lag 15–20 minutes once the quota tightens. Pair your Internet Bridge V3+ via HomeKit and Tado CE reads temperature, humidity, and heating state locally — updates arrive in around 2 seconds and don't count against the quota. On a nine-zone test home, daily API usage dropped from ~394 calls to under 80 once HomeKit local control was active.
2. **Your TRV reads run hot, your room never reaches target.** Radiator TRVs sit on the radiator and read warmer than the room, so Tado closes the valve before the room is up to temperature. Smart Valve Control compensates with an external sensor — either by writing a temperature offset Tado's own algorithm sees (Offset Sync) or by directly overriding the setpoint (Valve Target). Came from [@Si-Hill's Discussion #231](https://github.com/hiall-fyi/tado_ce/discussions/231) and three months of debugging across the beta cycle.
3. **You need a feature the official integration doesn't have.** Multi-home support, OpenTherm-aware Weather Compensation, Actionable Insights, mould-risk monitoring, thermal analytics, schedule-as-calendar entity, hot-water timer service, per-zone external sensor override — see the table below.

If none of those apply, the official Tado integration is fine — Tado CE isn't positioned as a replacement for healthy installs, but as the option when a specific need arises.

---

## Feature comparison

| Feature                                         | Official Tado | Tado CE  |
| ----------------------------------------------- | :-----------: | :------: |
| Climate, AC, hot water control                  |       ✅      |    ✅    |
| Presence / geofencing                           |       ✅      |    ✅    |
| Weather data                                    |       ✅      |    ✅    |
| **HomeKit local control** (LAN, quota-free)     |       ❌      |    ✅    |
| **Multi-home support**                          |       ❌      |    ✅    |
| **Smart Valve Control** (Offset Sync / Valve Target) |   ❌      |    ✅    |
| **Weather Compensation** (OpenTherm flow temp)  |       ❌      |    ✅    |
| **Actionable Insights**                         |       ❌      |    ✅    |
| **Thermal analytics / preheat advisor**         |       ❌      |    ✅    |
| **Mould-risk monitoring**                       |       ❌      |    ✅    |
| **Schedule calendar entity**                    |       ❌      |    ✅    |
| **Adaptive polling** (API-quota aware)          |       ❌      |    ✅    |
| **Hot water timer** (down to 1 minute)          |       ❌      |    ✅    |
| Tado X series (Matter / Thread)                 |       ✅      |    ❌    |

Tado X is intentionally out of scope — those devices are Matter-over-Thread and handled best by Home Assistant's native Matter integration. A short migration note is in the [FAQ](#faq).

---

## Quick start

### Prerequisites

- Home Assistant 2025.11 or newer
- A Tado account
- Tado V2, V3, or V3+ hardware (see [Supported devices](#supported-devices))
- Optional but recommended: an Internet Bridge V3+ for HomeKit local control

> **Heads up if you're on v4.0.x or older.** v5.0.0 drops the code that reads the pre-v4.1 data format, so it needs you to be on **v4.1.0 or later** first. If you're on v3.x or v4.0.x, install v4.3.x before moving to v5.0.0 (your settings carry over automatically). Going straight to v5.0.0 from older than v4.1.0 won't work: it refuses to load and tells you what to do rather than failing quietly. Anyone already on v4.1.0 or later has nothing to do.

### 1. Install via HACS

Tado CE is in the HACS default store, so no custom-repository setup is needed.

1. Open HACS, search for **Tado CE**, and click **Download**.
2. Restart Home Assistant.

[![Open your Home Assistant instance and open a repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=hiall-fyi&repository=tado_ce&category=integration)

The button above takes you straight to the Tado CE page in HACS.

<details>
<summary>Manual installation</summary>

Copy the `custom_components/tado_ce/` directory from this repository into your Home Assistant `config/custom_components/` directory and restart Home Assistant.

</details>

### 2. Add and authenticate

1. **Settings → Devices & Services → Add Integration → Tado CE**
2. Open the authorisation link shown (or visit the URL displayed and enter the code), authorise Home Assistant in your Tado account, and return.
3. If your Tado account covers more than one home, pick which one to add. Each home becomes its own Home Assistant config entry.

No SSH, no manual token extraction, no YAML.

### 3. Verify

Check **Settings → System → Logs** for:

```
Tado CE: Integration loading...
Tado CE: Polling interval set to 30m (day)
Tado CE full sync SUCCESS
Tado CE: Integration loaded successfully
```

### 4. Enable HomeKit local control (recommended)

If you have an Internet Bridge V3 or V3+, pair it directly with Tado CE:

1. Enable HomeKit on your Tado Internet Bridge — follow Tado's own walkthrough at [support.tado.com — How do I set up tado° with Apple HomeKit?](https://support.tado.com/en/articles/3387324-how-do-i-set-up-tado-with-apple-homekit) Note the 8-digit code (printed on the side of the bridge, also shown in the Tado app once HomeKit is enabled).
2. In Home Assistant, open Tado CE's **Configure → General Settings → Hardware Connections → HomeKit**, enable it, and enter the code when prompted.

Tado CE drives the pairing itself, so don't add the bridge through Home Assistant's standard **HomeKit Device** integration first. The Tado bridge only allows one HomeKit controller at a time, and a pre-existing pairing will block Tado CE. If you've already paired the bridge with Apple Home or HomeKit Device, unpair there before enabling local control here.

Temperature and humidity entities will then show `data_source: homekit` when values arrive locally. See [FEATURES_GUIDE.md](FEATURES_GUIDE.md#homekit-local-control) for details.

### 5. Configure

Open the gear icon on the Tado CE integration card. Settings take effect immediately — no restart needed. Start with **General Settings** to enable the features you want; **Advanced Settings** only exposes tuning parameters for features you've enabled.

---

## Features

### Core climate, AC, and hot water

Full Home Assistant `climate.*`, `water_heater.*`, `sensor.*`, and `binary_sensor.*` entity coverage for every Tado zone and device type. Preset modes (Home / Away), timer overlays (manual / next-time-block / timer), and geofencing are mapped onto Home Assistant-native controls — no custom cards required.

### HomeKit local control

With an Internet Bridge V3+ paired, Tado CE uses HomeKit for temperature reads, humidity reads, target-temperature writes, and HVAC-mode writes on heating zones. Updates arrive in around 2 seconds over your LAN; cloud-only mode is bound by your polling interval, typically 5–30 minutes. The cloud API is used for features HomeKit doesn't expose (schedules, geofencing, the Tado app's calibration engine) and for everything Smart AC Control V3+ does — those units are standalone WiFi accessories with their own HomeKit pairing, separate from the bridge, and Tado CE doesn't currently handle that pairing flow.

When the bridge is unreachable — including at HA startup — everything falls back to cloud automatically and local control resumes as soon as the bridge is reachable again, with no reload needed. If the bridge is factory-reset (which issues a new HomeKit identity), Tado CE raises a Home Assistant Repairs notification and stops retrying until you re-pair from Settings → Tado CE → Configure. A `data_source` attribute on each temperature sensor reports whether the current value came from HomeKit or cloud, and the HomeKit Connected sensor tracks how many API calls local control has saved you. One edge case worth knowing: if a single TRV drops off the bridge's radio while the bridge itself stays connected, its room temperature can sit on the last reading for a few minutes before falling back to cloud (see [FEATURES_GUIDE.md](FEATURES_GUIDE.md#-homekit-local-control) for why).

### Smart Valve Control

For rooms where the TRV reading differs from the actual room temperature (common with radiator TRVs — draughts, cold external walls, sensor placement), Tado CE can use an external temperature sensor to correct Tado's algorithm. Two modes:

- **Offset Sync (recommended)** — writes a device temperature offset so Tado's own heating algorithm sees your external sensor's reading. Tado then modulates as normal.
- **Valve Target (advanced)** — overrides the TRV setpoint directly. Use when Offset Sync's ±10 °C range isn't enough.

Both modes yield to manual changes (Tado app, physical TRV dial), resume on the next schedule block, and prefer HomeKit for the write when available (zero API cost). Valve Target reads the zone's Tado schedule to know what target to hold; that schedule is fetched automatically as needed and refreshed on demand via the Refresh Schedule button, rather than polled continuously, keeping the cloud-call budget for state that actually changes.

### Weather Compensation

For homes with an OpenTherm-compatible boiler, Tado CE can adjust the boiler's flow temperature based on outdoor temperature. Presets for radiators (aggressive / balanced / gentle) and underfloor heating are included; custom slopes are supported. Boiler flow temperature is read directly from the bridge every 60 seconds — independent of your cloud polling interval, since the bridge API doesn't count against your Tado quota.

### Smart Polling

The whole point of the polling system is to spend as few of your daily Tado API calls as possible without you noticing any loss of freshness. It is not built to use up your quota; it is built to leave as much of it as it can for the things that actually matter, like setting a temperature or running an automation. Tado started limiting most households to 100 API calls a day in 2025, so every call the integration doesn't make is one you keep.

Tado's per-home quota varies (100 / 1,000 / 20,000 calls per day depending on tier). Tado CE detects it, tracks remaining calls, and adapts its cadence automatically: quick polls during the day when activity matters, longer intervals overnight. The fastest it will poll on its own is every 5 minutes, the same on every plan. A bigger quota doesn't make it poll faster, because your room temperature doesn't change any faster just because Tado granted you more calls; polling faster would only spend calls re-reading the same numbers. If you genuinely want faster than 5 minutes, set a custom interval under **Configure → Advanced Settings → Polling & API** and it's honoured as long as your quota can sustain it.

Different kinds of data also refresh at their own pace rather than all riding the same cycle. Your zone temperatures update on the polling interval, but weather, presence, and mobile device locations refresh on their own slower schedule, because those barely change in between. The default floors are 30 minutes for weather and 5 minutes for presence and mobile, all configurable in **Configure → Advanced Settings → Polling & API**. So a fast polling interval no longer drags slow-moving data along with it and burns calls re-fetching values that haven't moved.

With HomeKit connected, cloud polling drops further since temperatures arrive locally. Token refreshes also happen ~50% less often than in earlier versions, since the integration now reads the actual expiry from Tado's response instead of refreshing on a fixed 5-minute timer.

That trade-off runs one way, worth knowing going in: a change you make from Home Assistant reaches Tado in seconds, but a change made in the Tado app doesn't come back the same way, it waits for the next cloud sync, so it can take up to the **Cloud Data Refresh** interval under **Configure → Advanced Settings → Polling & API** (30 minutes by default) to show up here, even with local HomeKit control active. A change made by hand at the thermostat is different: the bridge relays that locally, so it still arrives in real time.

### Actionable Insights

Per-zone and home-wide notifications that tell you *what's wrong and what to do about it*, rather than just surfacing raw metrics. Examples: open-window detection (with confidence scoring), zones heating outside scheduled times, low-quota warnings with a recommended configuration change, and cold-room alerts when a zone can't reach its setpoint.

Insights correlate and deduplicate across the home — a single underperforming boiler doesn't produce six near-identical notifications, it produces one home-level "heating system looks under-sized" insight.

### Thermal analytics

Per-zone heating-rate and cooling-rate tracking, preheat-time advisor, and thermal-inertia estimation. Uses a rolling window so the values adapt when you rearrange furniture, change windows, or upgrade insulation — no manual retraining.

### Multi-home support

If your Tado account covers more than one home, each becomes its own Home Assistant config entry with full data isolation. Switching between homes in the Tado app updates the correct HA entry automatically.

### Full feature reference

This page covers the features most users will notice first. See **[FEATURES_GUIDE.md](FEATURES_GUIDE.md)** for the complete reference including Smart Comfort, Adaptive Preheat, Hot Water Timer, Mould Risk monitoring, Environment Monitoring, Zone Features Toggles, and per-feature configuration.

---

## Configuration

All settings live under **Settings → Devices & Services → Tado CE → gear icon**. Changes apply immediately without restart.

| Section | Purpose |
| --- | --- |
| **General Settings** | Enable/disable features. Organised by origin: Tado Features (Home Presence, Weather Data, Mobile Tracking, Schedule Calendar, Device Offsets), Hardware Connections (Internet Bridge, HomeKit), Smart Automations (Smart Comfort, Thermal Analytics, Adaptive Preheat, Weather Compensation), Advanced (Per-Zone Configuration). |
| **Advanced Settings** | Tuning parameters for enabled features. Polling intervals, HomeKit cloud-sync frequency, Smart Comfort preheat ceiling, Weather Compensation curves, and similar. |
| **Zone Configuration** | Per-zone: manual-override behaviour, temperature limits, heating type, external sensors, window detection, preheat mode, Smart Valve Control mode. |
| **Reset to Defaults** | Reset per feature or everything at once, without affecting your Tado account or bridge pairing. |

For usage scenarios (low-quota setup, high-quota setup, mixed zones, OpenTherm boiler), see [FEATURES_GUIDE.md](FEATURES_GUIDE.md#configuration-scenarios).

---

## Entities

Tado CE creates entities covering hub-level diagnostics, per-zone climate and analytics, environment monitoring, hot water control, actionable insights, and weather compensation. See **[ENTITIES.md](ENTITIES.md)** for the complete list of entity types and their attributes.

Highlights:

- **Hub** — API usage/reset/sync, weather, home-wide insights, presence mode, overlay mode, resume-all button.
- **Per zone** — Climate control, temperature, humidity, heating power, overlay status, battery, connection.
- **Environment** — Mould risk, comfort level, surface temperature, dew point, condensation risk (AC).
- **Insights** — Per-zone and home-aggregated actionable notifications.
- **Smart Comfort** — Heating/cooling rates, time-to-target, preheat advisor, schedule sensors.
- **Thermal analytics** — Thermal inertia, heating rate, preheat time, confidence scoring.
- **Hot water** — Water heater entity with timer buttons.
- **Weather Compensation** — Target flow temperature, compensation status.
- **Switches** — Child lock, early start per zone.

---

## Services

Tado CE exposes services for climate control, hot water timers, open-window mode, temperature offsets, restoring previous state, and more. All services appear under **Developer Tools → Services** with parameter documentation. See [FEATURES_GUIDE.md](FEATURES_GUIDE.md#services) for full details and examples.

Automations that need to act on Tado CE entities right after Home Assistant starts can listen for the `tado_ce_ready` event instead of guessing timing with delays. The event fires once all climate entities have real data and Home Assistant itself has finished starting, so boot-time automation triggers are guaranteed to be listening when it lands. Payload carries `home_id`, `entry_id`, and `zone_count`.

---

## Supported devices

| Device | Type | Support | HomeKit local control |
| --- | --- | --- | --- |
| Smart Thermostat V2 | HEATING | Full (community-verified) | ❌ (V2 bridge) |
| Smart Thermostat V3 / V3+ | HEATING | Full | ✅ |
| Smart Radiator Thermostat (SRT / VA02) | HEATING | Full | ✅ |
| Smart AC Control V3 / V3+ | AIR_CONDITIONING | Full (cloud) | ❌ (standalone HomeKit pairing not handled — see note below) |
| Wireless Temperature Sensor | HEATING | Full | ❌ (not a HomeKit accessory) |
| Internet Bridge V3+ | Infrastructure | Required for local control | — |
| Tado X series | Matter / Thread | Not supported | — |

Tado X uses Matter over Thread. For those devices, use Home Assistant's [native Matter integration](https://community.home-assistant.io/t/using-tado-smart-thermostat-x-through-matter/736576).

---

## Known Limitations

These are how the integration or the underlying protocol works today, not bugs. Check here before opening a report; each links to the fuller explanation.

| Limitation | Detail |
|---|---|
| HomeKit humidity is fallback-only | Cloud data drives it by default (finer resolution, updates every poll). HomeKit's own humidity reading only takes over if cloud is unavailable. Temperature uses HomeKit first. See [HomeKit Local Control](FEATURES_GUIDE.md#homekit-local-control). |
| Some data is cloud-only | Heating power, battery status, schedules, hot water, and geofencing don't have a HomeKit equivalent, so they always come from Tado's cloud regardless of local control. |
| A change made outside Home Assistant can take a while to show up | Writes from Home Assistant reach Tado in seconds either way. A change made in the Tado app or on the thermostat itself is only picked up on the next Cloud Sync Interval (default 30 minutes, adjustable 5–120 under Advanced Settings → HomeKit), even with HomeKit connected, since Tado CE trusts HomeKit's own reading for temperature rather than polling the cloud that often. See [HomeKit Local Control](FEATURES_GUIDE.md#homekit-local-control). |
| Single HomeKit pairing | The bridge pairs with one HomeKit controller at a time. Pairing with Tado CE means unpairing it from Apple Home or anywhere else first. |
| A quiet TRV can hold a stale reading briefly | If a TRV drops off the bridge's radio while the bridge itself stays connected, its temperature can sit on the last reading for a few minutes before falling back to cloud. HomeKit has no "last heard from" signal that would let this integration catch it sooner. |
| Wireless Temp Sensors (ST01) | Not exposed over HomeKit at all; their readings always come from the cloud. |
| External sensors don't drive the valve | Binding an external sensor changes what the dashboard shows, not what the TRV heats to. Enable Smart Valve Control on that zone to have the TRV actually compensate. |
| No TRV LED feedback on local writes | A HomeKit-routed temperature change updates the TRV silently, unlike a change made in the Tado app. Press the device's Identify button, or call `tado_ce.identify_device`, for a flash to confirm you're at the right one. |
| Smart Valve Control is heating-only | AC zones aren't supported. Schedule resume through it is cloud-only, and it inherits HomeKit's 0.1°C rounding where the cloud API would accept 0.01°C. See [Smart Valve Control](FEATURES_GUIDE.md#smart-valve-control). |
| Smart AC Control V3+ has no standalone local pairing | These units pair with HomeKit separately from the Internet Bridge, and this integration only handles the bridge's pairing today, so AC zones use the cloud path regardless. See [HomeKit Local Control](FEATURES_GUIDE.md#homekit-local-control). |
| V2 Internet Bridges (`GW` serial) | Not supported by the Bridge API. See [Bridge API Integration](FEATURES_GUIDE.md#bridge-api-integration). |

---

## FAQ

<details>
<summary><strong>Can I run Tado CE alongside the official Tado integration?</strong></summary>

Yes, though there's usually no reason to. Both integrations read from the same Tado account and add to your API call count. If you want to compare, enable both, then pick one. Disabling the official integration is recommended once Tado CE is working.

</details>

<details>
<summary><strong>Does HomeKit pairing interfere with other HomeKit apps (Apple Home, etc.)?</strong></summary>

The Tado bridge only allows one HomeKit controller at a time, so Tado CE and Apple Home (or Home Assistant's standard HomeKit Device integration) can't both pair with it directly. If you want to keep your Tado zones in Apple Home, unpair the bridge there first and then pair with Tado CE. You can re-expose the resulting `climate.*` entities to Apple Home through Home Assistant's **HomeKit Bridge** integration, which is a separate component that publishes HA entities back out as a HomeKit accessory.

</details>

<details>
<summary><strong>What happens if my Tado API quota hits zero?</strong></summary>

With HomeKit connected, you'll barely notice — temperature, humidity, target, and mode keep updating in real time. Cloud-only features (schedule changes from outside Home Assistant, geofencing updates, presence detection) pause until the quota resets. Without HomeKit, polling pauses entirely and you'll see a repair notice; again, everything resumes once the quota resets.

</details>

<details>
<summary><strong>Why is my temperature reading different from the Tado app?</strong></summary>

Three common causes:

1. **Tado's app shows a calibrated reading**, not the raw TRV value. If your `sensor.*_temperature` is set to read the HomeKit / bridge value directly, it reports the TRV's uncorrected measurement — which runs hot during heating cycles.
2. **External sensor with no Offset Sync configured.** Tado's algorithm still uses the TRV's own reading to modulate. If your external sensor shows a different value, enable Smart Valve Control (Offset Sync mode) under **Configure → Zone Configuration**.
3. **Offset Sync saturation.** If the gap between external sensor and TRV needs a correction larger than ±10 °C (Tado's device limit), the clamp fires. The climate entity reports this via `offset_clamped: true` + `offset_clamp_direction`.

</details>

<details>
<summary><strong>My authentication failed / I need to re-authenticate.</strong></summary>

Open **Settings → Devices & Services → Tado CE**. If a re-authentication prompt is shown, follow it. Otherwise click **Configure** and re-run the device-authorisation flow.

</details>

<details>
<summary><strong>Device trackers are missing.</strong></summary>

Device trackers are only created for mobile devices that have geo-tracking enabled inside the Tado app. Enable it in the Tado app first, then reload the Tado CE integration.

</details>

<details>
<summary><strong>How do I migrate from Tado X hardware?</strong></summary>

Tado X runs on Matter / Thread and isn't supported here. Use Home Assistant's [native Matter integration](https://community.home-assistant.io/t/using-tado-smart-thermostat-x-through-matter/736576). You'll lose Tado-specific features (schedules, geofencing, Tado CE's insights) — that's an upstream Matter limitation, not something this integration can work around.

</details>

<details>
<summary><strong>Enable debug logging</strong></summary>

Add to `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.tado_ce: debug
```

Restart Home Assistant, reproduce the issue, then check **Settings → System → Logs**. Filter by `tado_ce` to focus on the relevant entries.

</details>

For issues not covered here, check **Settings → System → Logs** (filter by `tado_ce`), search the existing [GitHub Issues](https://github.com/hiall-fyi/tado_ce/issues), and if it's new, [open a bug report](https://github.com/hiall-fyi/tado_ce/issues/new/choose). The bug-report form walks you through what to include (your versions, whether HomeKit is on, and a debug log), which is what turns a triage round-trip into a same-day fix.

---

## Uninstall

1. **Settings → Devices & Services → Tado CE → ⋮ → Delete**
2. Restart Home Assistant.
3. In HACS: find Tado CE, **⋮ → Remove**. Or for manual installs, delete the `config/custom_components/tado_ce/` directory.
4. Restart Home Assistant again.

---

## Documentation

| Document | Purpose |
| --- | --- |
| [FEATURES_GUIDE.md](FEATURES_GUIDE.md) | Full feature reference, configuration scenarios, service examples |
| [ENTITIES.md](ENTITIES.md) | Complete entity type list with attributes |
| [ROADMAP.md](ROADMAP.md) | Planned features, no fixed release yet |
| [CREDITS.md](CREDITS.md) | Contributor credits per release |
| [CHANGELOG.md](CHANGELOG.md) | Version history |

### External resources

- [Tado API rate-limit announcement (HA community)](https://community.home-assistant.io/t/tado-rate-limiting-api-calls/928751)
- [Official Tado integration](https://www.home-assistant.io/integrations/tado/)
- [Tado API documentation (community project)](https://github.com/kritsel/tado-openapispec-v2)

---

## Contributing

Tado CE's feature set is almost entirely driven by GitHub Issues and Discussions. Specific user reports, data, and testing turn into features or fixes. If you've hit a problem, [open a bug report](https://github.com/hiall-fyi/tado_ce/issues/new/choose); if you want something changed or just want to ask, [start a Discussion](https://github.com/hiall-fyi/tado_ce/discussions). Logs and screenshots are what make the difference between a triage round-trip and a same-day fix.

Code contributions are also welcome: fork, branch, commit, and open a PR. The [Discussions](https://github.com/hiall-fyi/tado_ce/discussions) tab is the best place to float an idea before writing code.

---

## License

Released under the **GNU Affero General Public License v3.0 (AGPL-3.0)**. Modifications must be released as open source under the same licence with attribution. See [LICENSE](LICENSE) for the full text.

**Original author:** Joe Yiu ([@hiall-fyi](https://github.com/hiall-fyi))

---

<details>
<summary><strong>Disclaimer</strong></summary>

This project is not affiliated with, endorsed by, or connected to tado GmbH or Home Assistant. *tado* and the tado logo are registered trademarks of tado GmbH. *Home Assistant* is a trademark of Nabu Casa, Inc.

This integration is provided "as is" without warranty. Use at your own risk.

</details>
