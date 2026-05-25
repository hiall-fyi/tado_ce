# Changelog

All notable changes to Tado CE will be documented in this file.

## [4.1.0-beta.1] - 2026-05-25

**Split AC swing into vertical and horizontal axes**

The unified `Off / Vertical / Horizontal / Both` swing dropdown is replaced with two independent dropdowns, one per axis. Units that report fine-grained louver positions (Mitsubishi, Fujitsu, and similar) can now be parked at a fixed direction such as Up, Mid, or Mid (left) instead of being forced into a sweeping motion.

### Features

- **Split AC swing into vertical and horizontal axes** ([#270](https://github.com/hiall-fyi/tado_ce/issues/270) - @Ralf84) — AC zones now expose a `Swing (vertical)` and a `Swing (horizontal)` dropdown, each populated from the cloud-reported capability set for your specific unit. Pick a fixed louver position on either dropdown to stop oscillation on that axis — useful in bedrooms or children's rooms where a constantly moving louver is disruptive. Simple `On / Off` units keep a two-value dropdown.
- **Fine-grained louver positions for capable AC units** — Units that report values like `UP`, `MID_DOWN`, `LEFT`, `MID_RIGHT` now expose those values directly in the dropdowns. Translations land in German, Spanish, French, Italian, Dutch, and Portuguese alongside English.

### Bug Fixes

- **Swing changes no longer silently drop the off-axis on `OFF`-less units** ([#270](https://github.com/hiall-fyi/tado_ce/issues/270) - @Ralf84) — Picking "Vertical" on a Mitsubishi or Fujitsu unit (which doesn't report `OFF` as a swing value) used to send only `verticalSwing=ON` and omit `horizontalSwing` from the payload, leaving the bridge to keep whatever horizontal state it last had. Each axis now writes its own value independently and the silent-drop fallback is gone.
- **Picking "On" from a swing dropdown no longer silently moves the off-axis** — `On` was incorrectly being translated as a legacy v4.0 unified value, which set the opposite axis to `Off` and logged a deprecation warning the user hadn't earned. `On` is now treated as a v4.1 raw axis value and only the axis you picked moves.

### Improvements

- **One log line per AC write failure, not two** — Temperature, HVAC mode, fan mode, and swing writes used to log the specific failure reason (timeout, exception, or "rejected by Tado") and then a generic "write failed" warning right after. The generic line now fires only when no specific reason was logged, so a timeout produces a single warning rather than two.

### ⚠️ Migration

**Service-call automations** — calls using the old unified value (`swing_mode: off / vertical / horizontal / both`) keep working with a deprecation warning logged each call. The compat shim is removed in v4.2.0. Recipe:

```yaml
# Before (v4.0)
- service: climate.set_swing_mode
  data:
    entity_id: climate.tado_ce_living_room
    swing_mode: both

# After (v4.1+)
- service: climate.set_swing_mode
  data:
    entity_id: climate.tado_ce_living_room
    swing_mode: on        # or 'auto', 'up', 'mid', etc. — your unit's capability
- service: climate.set_swing_horizontal_mode
  data:
    entity_id: climate.tado_ce_living_room
    swing_horizontal_mode: on
```

**Dashboard templates and Lovelace conditions** — anything reading `state_attr('climate.X', 'swing_mode')` and matching `'both'`, `'vertical'`, or `'horizontal'` needs updating. The compat shim covers service calls, not state reads. After v4.1 the attribute holds raw values like `'on'`, `'off'`, `'up'`, `'auto'`. Cards and templates should switch to checking `swing_mode` and `swing_horizontal_mode` separately:

```jinja
{# Before #}
{% if state_attr('climate.tado_ce_living_room', 'swing_mode') == 'both' %}

{# After #}
{% set v = state_attr('climate.tado_ce_living_room', 'swing_mode') %}
{% set h = state_attr('climate.tado_ce_living_room', 'swing_horizontal_mode') %}
{% if v not in ['off', None] and h not in ['off', None] %}
```

**HomeKit users with AC zones** — temperature and HVAC mode still update locally via HomeKit (typically within 2 seconds). Swing changes still go through Tado's cloud, so picking a swing position uses cloud quota and confirms on the next cloud poll (typically 5–30 minutes). Same as v4.0 — not a regression. A follow-up to wire HomeKit's binary `SwingMode` characteristic into the new vertical-axis dropdown for ON/OFF AC units is being scoped separately.

## [4.0.0] - 2026-05-23

**HomeKit Local Control & Smart Valve Control**

Existing v3.5.3 installs upgrade in place — data, schedules, and HomeKit pairings migrate automatically.

### ⚠️ Breaking Changes

- **Connection sensors are now binary sensors** ([#160](https://github.com/hiall-fyi/tado_ce/issues/160) - @Thilas, @jeverley) — `sensor.tado_ce_*_connection` entities migrate to `binary_sensor.tado_ce_*_connection` with the Connectivity device class. The migration is automatic on first startup; automations or dashboard cards referencing the old IDs need updating.
- **Hot water power sensor is now a binary sensor** ([#160](https://github.com/hiall-fyi/tado_ce/issues/160) - @jeverley) — `sensor.tado_ce_*_power` migrates to `binary_sensor.tado_ce_*_power` with the Power device class. Same — update any references.

### Features

- **HomeKit Local Control for Tado Internet Bridge** — Pair your Tado bridge via HomeKit and temperature / mode changes go through your local network instead of Tado's cloud, with sensor pushes arriving within ~2 seconds (cloud-only mode is bound by your polling interval, typically 5–30 minutes). The integration tracks how many API calls HomeKit saves you per day. If HomeKit becomes unavailable, the integration automatically falls back to the cloud and tries to recover in the background. Set up under **Settings → Tado CE → Configure → General Settings → enable HomeKit**.
- **Smart Valve Control — two modes for compensating inaccurate TRV readings** ([Discussion #231](https://github.com/hiall-fyi/tado_ce/discussions/231) - @Si-Hill, @wrowlands3, [#221](https://github.com/hiall-fyi/tado_ce/issues/221) - @simonotter) — TRV built-in sensors sit on the radiator and read warmer than the room, so Tado closes the valve before the room reaches target. With an external temperature sensor configured for a zone, you can now choose how to compensate:
  - **Offset Sync (recommended)** — Writes a device temperature offset so the Tado app and Tado's own modulation see your external sensor's reading.
  - **Valve Target (advanced)** — Directly overrides the TRV setpoint. Use only when Offset Sync isn't enough.

  Mutually exclusive per zone. Configure under **Settings → Tado CE → Configure → Zone Configuration → External Sensors → Smart Valve Control Mode**. Adjustments go via HomeKit when available (no API cost), with cloud as fallback. The controller backs off on manual changes and resumes on the next schedule block. See [FEATURES_GUIDE.md](FEATURES_GUIDE.md#-smart-valve-control) for details.
- **`tado_ce_ready` event for startup automations** ([#246](https://github.com/hiall-fyi/tado_ce/issues/246) - @Newreader) — Trigger startup automations on the `tado_ce_ready` event instead of guessing timing with delays or `wait_template` chains. The event fires once all climate entities have real data — temperature, offset, overlay mode, the lot. Payload includes `home_id`, `entry_id`, and `zone_count` for multi-home filtering.
- **Climate entity exposes Offset Sync clamp state** ([#262](https://github.com/hiall-fyi/tado_ce/issues/262) - @simonotter) — When the gap between your external sensor and the TRV needs a correction larger than ±10°C, Tado clamps the offset at the limit. The climate entity now exposes `offset_clamped: true` and `offset_clamp_direction: hit_max | hit_min` so dashboards and automations can react. A warning fires in the log so you know to check for draughts, a cold external wall behind the TRV, or an external sensor placed somewhere warmer than the radiator.

### Bug Fixes

- **Tado app changes now reach Home Assistant within seconds when HomeKit is connected** ([#253](https://github.com/hiall-fyi/tado_ce/issues/253) - @apilone, [#261](https://github.com/hiall-fyi/tado_ce/issues/261) - @apilone) — Changing temperature or HVAC mode in the Tado phone app used to take up to one cloud poll cycle to show in HA. With HomeKit connected, target temperature and mode now update from bridge events in real time, including when you flip a zone from OFF to HEAT or set a temperature on an OFF zone.
- **Climate card now shows OFF correctly** ([#258](https://github.com/hiall-fyi/tado_ce/issues/258) - @Newreader, @apilone, [Discussion #219](https://github.com/hiall-fyi/tado_ce/discussions/219) - @dragorex71) — When a zone was off — via schedule, Away mode, or `set_hvac_mode: off` — the card showed the previous heat target (e.g. 23°C) and HVAC mode "auto" while Tado was actually running frost protection at 5°C. The card now shows 5°C and "off" to match Tado's actual state.
- **HomeKit no longer overwrites your changes with stale cached values** ([#253](https://github.com/hiall-fyi/tado_ce/issues/253) - @apilone) — After changing temperature or mode, the bridge could push a stale value within seconds and undo your change. There's now a 3-minute write protection window after any HomeKit or cloud write — during this window, the bridge's stale target temperature and mode are ignored until the cloud confirms the actual state.
- **Heating controls no longer get stuck after a silent HomeKit failure** — If a HomeKit write completed locally but never reached Tado's servers, the integration could keep showing "heating" indefinitely and skip subsequent commands. Writes are now verified against Tado's cloud, the stale state is cleared if not confirmed, and future writes for that zone fall through to cloud until HomeKit recovers.
- **Service calls now refresh the entity immediately** ([Discussion #219](https://github.com/hiall-fyi/tado_ce/discussions/219) - @jeverley) — `set_open_window_mode`, `restore_previous_state`, `resume_schedule`, `set_climate_timer`, and `set_water_heater_timer` used to update the Tado app instantly but leave the HA entity stale until the next poll. They now trigger an immediate refresh.
- **Failed service calls now surface as errors instead of silent success** — `set_temperature_offset`, `set_climate_timer`, and `set_water_heater_timer` used to swallow per-zone failures and report overall success even when no write actually landed. They now raise an error visible in the HA UI when every zone fails, and log a warning for partial failures.
- **`restore_previous_state` and `resume_schedule` now actually restore the zone** ([#267](https://github.com/hiall-fyi/tado_ce/issues/267) - @apilone) — Calling `restore_previous_state` after changing a zone could leave the cloud unchanged while the HA log claimed "Restore executed". Two problems stacked: the captured pre-overlay state was replayed to Tado's cloud verbatim — including response-only fields the PUT endpoint rejects with HTTP 422 — and the service didn't check the API's return value, so the failure was swallowed and the captured baseline was consumed before the call even ran. The service now sanitises the termination dict before sending, checks the cloud's response, and only clears the captured state when the call lands. On failure you get a clear error in the HA UI and the captured state stays in the store so you can retry. `resume_schedule` had the same fire-and-forget pattern and is fixed alongside.
- **Early Start and Child Lock switches roll back on failed writes** — If the API rejected a switch write (e.g. during a rate-limit window), the switch used to stay in the optimistic new state until the next refresh. The switch now reverts when the underlying API call reports failure.
- **Presence Mode "Auto" no longer briefly shows "Home" when you're away** — Switching to Auto used to overwrite the cached presence with "Home" for one poll cycle. Auto now leaves the cached presence alone and lets the next poll fill in the real value from geofencing.
- **Presence labels match across the Hub select and climate cards** ([Discussion #219](https://github.com/hiall-fyi/tado_ce/discussions/219) - @dragorex71) — On non-English Home Assistant installations, the Hub presence select and the climate card preset showed different translations (e.g. "Casa" vs "In casa" in Italian). All six supported languages now match.
- **Adaptive Preheat works with non-ASCII zone names** — Zones with accented characters or special characters (e.g. "Büro", "Salle-à-manger") were silently failing to find their matching entities, breaking Adaptive Preheat, water heater resume buttons, and thermal analytics. All zone-name lookups now use Home Assistant's standard slug method.
- **Weather Compensation no longer latches into "paused" overnight** ([#249](https://github.com/hiall-fyi/tado_ce/issues/249) - @driagi) — On long night-polling intervals (≥ 60 min, including auto night polling at 120 min), the engine could latch into "paused" forever even though the outdoor temperature source was reporting fresh values. The redundant guard causing this has been removed. The engine also holds the last known outdoor temperature for up to 30 minutes during transient outages instead of pausing on a single missed reading.
- **Token rotations no longer drop in-flight writes** — When Tado's auth servers rotate a session mid-request, the API returns 401. Read requests already retried with a fresh token; writes (temperature, mode, schedule resume) now do too. A transient 401 during normal token refresh also no longer deletes your refresh token — Home Assistant's reauth flow handles real auth failures, transient glitches recover on the next refresh.
- **Mold risk and surface temperature attributes are now translated** — The `temperature_source` attribute on Mold Risk / Mold Risk % sensors and the `calculation_method` attribute on the Surface Temp sensor used to display raw English values regardless of your Home Assistant language. They now show translated labels in German, Spanish, French, Italian, Dutch, and Portuguese alongside English.
- **Integration recovers from corrupt auxiliary storage files** — If one of several auxiliary storage files (weather compensation, bridge health, HomeKit savings, window detection state, state-restore) became corrupt after a crash or SD card issue, every entity could go unavailable. The integration now logs a warning and continues with defaults, healing the file on the next successful save.
- **Daily API usage no longer spikes when HomeKit is connected** ([#268](https://github.com/hiall-fyi/tado_ce/issues/268) - @wrowlands3) — A periodic offset drift refresh added during the 4.0 cycle fired every 30 minutes regardless of your HomeKit Cloud Refresh setting, pulling one cloud call per climate zone per cycle. On a home with eight zones at a 60-minute Cloud Refresh, that meant ~16 unexpected calls every hour. With HomeKit connected, the drift refresh now follows the same dial as the rest of cloud sync — set 60 minutes and it runs every 60 minutes. HomeKit-off installs keep the 30-minute floor as a safety net.
- **Weather fetches now follow your HomeKit Cloud Refresh setting** — Same shape as the drift-refresh bug above with a smaller blast radius (one call per cycle rather than per zone): the 30-minute weather skip floor was hardcoded and ignored the Cloud Refresh dial. Both paths are now driven by a single user-facing setting.
- **Adaptive Preheat tolerates incomplete cycle metrics** — When the heating cycle store had a partially-recorded cycle with a missing inertia time or heating rate, the preheat estimator could raise an error in the log instead of skipping. It now returns no estimate and lets the next valid cycle take over.

### Improvements

- **All data now uses Home Assistant's built-in storage** — Zone states, weather, rate limits, schedules, HomeKit pairings, and per-zone state files moved from custom JSON files to HA's native Store system. The migration runs once on first startup; old files are renamed (not deleted) so you can roll back if needed. No more blocking file reads on the main thread.
- **Token refreshes ~50% less often** — The integration used to refresh access tokens every 5 minutes, but Tado issues them for roughly 10 minutes. It now reads the actual expiry from Tado's response and only refreshes when the token is about to expire.
- **Boiler flow temperature updates every 60 seconds** ([#237](https://github.com/hiall-fyi/tado_ce/issues/237) - @ChrisMarriott38) — The boiler flow sensor used to be tied to your cloud polling interval (up to 30 minutes between updates). It now polls the bridge independently every 60 seconds. The bridge API doesn't count against your Tado quota.
- **Custom polling interval applies everywhere** ([#239](https://github.com/hiall-fyi/tado_ce/issues/239) - @ChrisMarriott38) — If you set a custom day or night polling interval, all data (zone states, humidity, heating power, weather, boiler flow temperature) refreshes at that rate. Previously, HomeKit's Cloud Data Refresh setting could override your custom interval for some data.
- **Smarter polling when HomeKit is connected** — When HomeKit is providing live data, the integration skips redundant cloud fetches and stretches the polling interval. Weather data is fetched every 30 minutes instead of every poll. Cloud outages no longer make entities unavailable when HomeKit is still working.
- **Insight history writes drop ~50%** — The insight history file used to write once per polling cycle (up to 2,880 writes/day with 30-second polling). Writes are now debounced to once per minute, reducing SD card wear without losing data on shutdown.
- **API call history attributes capped to 10 entries (was 100)** — The API usage, limit, and call history sensors used to expose up to 100 entries each in their attributes, bloating the recorder database over time. Dashboards only show the most recent few, so the cap is now 10.
- **Less CPU work per poll on homes with many zones** — Zone insights are now computed once per polling cycle and cached for every zone sensor and the home sensor to read, instead of each sensor collecting independently.
- **Smarter HTTP error handling** — 404 on overlay deletion (already gone) is no longer logged as an error. 422 (Tado API rejection) is logged as a warning with the actual rejection reason. 429 reads the `Retry-After` header to know when to try again. 500 / 502 / 503 / 504 are retried automatically with backoff.
- **Settings UI reorganised** — General Settings now grouped by what each toggle does (Tado Features / Hardware Connections / Smart Automations / Advanced). Zone Configuration reordered to match how you think about a zone (Temperature Limits → Heating System → External Sensors → Smart Features → Manual Temperature Override). Internet Bridge gets its own settings section instead of living under "Weather Compensation" ([#240](https://github.com/hiall-fyi/tado_ce/issues/240) - @ChrisMarriott38). Existing entity IDs and config keys unchanged.
- **External sensor toggles renamed for clarity** — "Use External Temperature/Humidity Sensor" → "Override Tado's Temperature/Humidity Sensor". Turning the toggle off keeps your sensor selection saved, so you can pause without losing the configuration.
- **Hot Water Timer default moved to Polling & API** — Lived under Smart Comfort by accident; now lives next to the other service defaults. Config key unchanged.
- **Smart Comfort defaults to Light on first enable** — Used to default to None, which left the feature doing nothing.
- **Device serial numbers masked across logs and entity attributes** — Battery sensors, connection sensors, device tracker entities, log messages, and HomeKit mappings now show the first 6 characters followed by `…`. The bridge serial is printed on the device and the auth code is only 4 digits, so a leaked serial in a shared log could enable brute-force pairing.
- **Diagnostics dump redacts more sensitive fields** — Bridge serial, additional Tado API response fields, and other sensitive values are scrubbed from `Settings → Devices → Tado CE → Download Diagnostics`. Diagnostics also include a `data_flow_health` section with last cloud fetch timestamp, HomeKit and bridge connection status, and persistence state.
- **Climate entities show where their data comes from** — `temperature_source` and `humidity_source` attributes show `cloud`, `homekit`, or `external` instead of the legacy `tado` label. A new `last_write_source` attribute shows whether the most recent change went through HomeKit or the cloud.
- **Window Predicted, heating rate, and insights now react to real-time HomeKit data** — Window detection, heating cycle tracking, mold risk, comfort level, humidity trends, and heating anomalies were using cloud data even when HomeKit had fresher readings. They now use the same live data as the rest of the integration.
- **Plain-English log messages, with consistent prefixes and recovery hints** — Internal terms (`backed-off`, `bang-bang fallback`, `optimistic state expired`, `ROLLBACK`) replaced with plain-English equivalents. Every line now starts with a clear subsystem label (`Coordinator:`, `Climate AC:`, `HomeKit:`, `Smart Valve:`, etc.) so you can filter by feature when something goes wrong. Failures explain what the integration did about it ("rolling back optimistic state", "will retry on the next poll", "captured baseline preserved so you can retry"), so a log paste is enough to diagnose without a back-and-forth. Per-write chatter that used to flood multi-zone homes at info level is now debug; once-per-startup / shutdown summaries stay at info. Device serials and home IDs are masked everywhere so logs are safe to share publicly.

### Known Issues

- **`register_detection_callback() is deprecated` warning if HomeKit is enabled** — The warning comes from the `aiohomekit` library's internal BLE scanning code, not from Tado CE. It does not affect functionality and will disappear when `aiohomekit` releases an update. You can safely ignore it.

## [3.5.3] - 2026-04-08

### ⚠️ Prerequisites
- **Minimum Home Assistant version is now 2025.11** — Required by the smarter rate limit handling below. If you're on an older HA release, stay on v3.5.2 or upgrade HA first.

### Improvements
- **Overlay sensor now shows timer end time** ([#217](https://github.com/hiall-fyi/tado_ce/issues/217)) — When a Timer overlay is active, the `next_change` attribute now shows when the timer expires instead of the next schedule change. Two new attributes are also available: `overlay_expiry` (the exact end time) and `overlay_remaining_seconds` (countdown). Manual and Tado Mode overlays continue to show the next schedule change as before.
- **Smarter rate limit handling** — When the API quota is exhausted, the integration now tells HA exactly how long to wait before the next poll (using the known reset time) instead of using a fixed 15-minute retry. This means polling resumes faster after a quota reset.
- **Retry delay capped** — Exponential backoff delay is now capped at 30 seconds to prevent excessively long waits on repeated failures.
- **Device authorization polls less aggressively** — When signing in via device authorization, the integration now waits 5 seconds between polls instead of 2, matching the OAuth standard recommendation. You may notice the "waiting for browser authorization" step take a touch longer on fast networks, but it's less likely to hit rate limits on slow ones.

---

## [3.5.2] - 2026-04-06

### Bug Fixes
- **Fixed token refresh and API calls not retrying on DNS/network failures** ([#214](https://github.com/hiall-fyi/tado_ce/issues/214)) — If your DNS server briefly refused a query or the connection to Tado's servers timed out, the integration would give up immediately instead of retrying. This could leave all entities unavailable until the next poll cycle. Now token refresh and all API calls retry up to 3 times with exponential backoff on any transient network error (DNS failures, connection timeouts, connection resets), matching the existing 403 retry behaviour.

---

## [3.5.1] - 2026-04-06

**Reliability & Code Quality**

### Bug Fixes
- **Fixed stale insights sticking around after resolving** — The Home Insights sensor could show issues that had already resolved but were still within the reappearance grace period. For example, a mold risk warning that cleared would keep showing in the "persistent issues" list for up to an hour. Now only genuinely active issues appear.

### Improvements
- **More resilient API calls** — All API operations (temperature offsets, child lock, zone overlays, presence lock, schedules, meter readings, away configuration) now automatically retry when the Tado cloud returns a transient 403 error. Previously, only the main polling calls had retry logic — actions like changing temperature or toggling child lock would silently fail on a temporary CDN/WAF block. Now every cloud API call retries up to 3 times with exponential backoff before giving up.
- **Token refresh also retries on 403** — If the Tado login server returns a transient 403 during token refresh, the integration now retries instead of immediately failing. Real authentication errors (401, invalid_grant) are still handled instantly without retry.
- **Dangling async tasks fixed** — Background tasks in the write optimizer (action debouncer and refresh coalescer) now properly track their lifecycle and log exceptions instead of silently swallowing them. Cleanup on shutdown cancels all pending tasks.

_Performance & storage_

- **Faster polling** — Rate limit data is now read from memory instead of reading a file from disk on every poll cycle. Small wins compound on low-quota homes that poll frequently.

---

## [3.5.0] - 2026-04-02

**Redesigned Settings & Architecture Overhaul**

### Features
- **Redesigned Options Flow** — Settings are now split into four clear sections: General Settings (feature toggles), Advanced Settings (tuning parameters for enabled features only), Zone Configuration, and Reset to Defaults. You no longer need to scroll through 79 options on one page. First-time setup for Internet Bridge and Weather Compensation now guides you through credentials step by step. You can also reset settings back to defaults (per feature or everything at once) without losing your Tado account or bridge pairing.

### Bug Fixes
- **Fixed quota deadlock on clean install** ([#204](https://github.com/hiall-fyi/tado_ce/issues/204) - @Saughassy) — On a fresh install with stale rate limit data and no known reset time, the integration could get stuck permanently in "quota critically low" state. Now allows both polling and manual actions to bootstrap fresh data when no reset time is known.
- **Fixed temperature offset not updating after service call** ([#211](https://github.com/hiall-fyi/tado_ce/issues/211) - @mat01) — After calling `set_climate_temperature_offset`, the `offset_celsius` attribute kept showing the old value until the next HA restart. Automations that read-then-write offsets would oscillate. Now updates the local cache immediately so the new offset is reflected right away.

### Improvements

_Options Flow & descriptions_

- **Clearer Settings Descriptions** ([Discussion #131](https://github.com/hiall-fyi/tado_ce/discussions/131) - @Prodeguerriero) — All option descriptions in General Settings and Advanced Settings have been rewritten in plain language. Technical jargon like "rate calculation", "inertia end", and "setpoint deviation" has been replaced with descriptions that explain what each setting actually does for you. Mobile Device Tracking now clearly states that locations only update on HA restart unless you enable Frequent Sync. API cost info uses consistent "per poll" / "on restart" wording instead of the confusing "full sync" / "quick sync" distinction.
- **Removed Legacy Options Flow** — The old single-page "Global Settings" flow has been fully removed (code, strings, and all translations). If you see a stale UI after upgrading, clear your browser cache.

_Insights & comfort_

- **Smarter, Cleaner Insights** — Insights got a full overhaul. Recommendations now only appear when they're relevant to your actual settings (e.g. no geofencing alerts when you have geofencing off). The Home Insights summary focuses on the single most urgent action with a clear reason, instead of listing everything. Empty attributes no longer clutter the sensor. Persistent issues show escalated priority (a battery problem lasting 2 weeks shows as Critical, not Low). Zone-level sensors now include how long an issue has been active. The weekly digest is a simple trend comparison — new, resolved, up or down from last week.
- **More Accurate "Feels Like" Temperature** — The Heat Index calculation no longer has a small jump at the transition point (~27°C). Previously, a tiny humidity increase could briefly make the "feels like" temperature drop instead of rise. Now the transition is smooth.

_Performance & storage_

- **Faster Startup** — The insights engine loads only the modules it needs instead of pulling in the entire 3,000-line file on every restart.

_Data integrity_

- **More state now survives HA restarts** — Weather compensation settings, bridge health status, and window detection history are now persisted and restored across restarts instead of resetting to defaults or empty on startup.
- **Configuration now lives entirely in the HA config entry** — The separate `config_{home_id}.json` file is no longer written. Existing files are migrated automatically on upgrade — no action needed.
- **Config entry version bumped to v12 with automatic migration** — Upgrading from earlier v3.x releases migrates your data to the new format on first start.

---

## [3.4.1] - 2026-03-26

### Bug Fixes
- **Fixed crash on clean install** ([#204](https://github.com/hiall-fyi/tado_ce/issues/204) - @Saughassy) — On a fresh install, the integration could fail to start with a Python `TypeError` before rate limit data was fully populated — the adaptive polling calculator was dividing by fields that hadn't arrived yet. All rate limit fields are now treated as optional during first setup, so a clean install boots cleanly even before the first API response lands.

---

## [3.4.0] - 2026-03-23

### ⚠️ Prerequisites
- **Minimum supported upgrade path is now v3.0.0+** — All migration code for upgrading from v2.x has been removed. Users still on v2.x should upgrade to a v3.x release first before taking this update.

### Features
- **API Write Optimization** — All enabled by default. Three new settings under **Settings → Tado CE → Configure → Global Settings → Polling & API** to reduce unnecessary API calls:
  - **Smart Actions Debounce** — When you drag a temperature slider, only the final value is sent to the API instead of every intermediate position. Configurable window (0–10 seconds, default 3). Set to 0 to disable.
  - **Action Guard** — Skips API calls when the requested state already matches the current state (e.g. setting 22°C when it's already 22°C). Always active.
  - **Device Sync Queue** — Device-level operations (child lock, early start) are now queued and executed sequentially with a configurable delay (0.5–5 seconds, default 1), preventing race conditions from rapid toggling. Always active.
  - **Write Coalescing** — Multiple rapid state changes trigger a single coordinated refresh instead of one per change. Always active.
  - **Resume Guard** — Resuming a zone's schedule is skipped if the zone is already following its schedule. Always active.
- **Schedule Preview** — Heating and AC climate entities now show a `scheduled_target_temperature` attribute with the current schedule target, so you can see what temperature the zone would be at without an overlay.

### Bug Fixes
- **Fixed Hassfest Validation Failure** — The window detection mode selector was using title-case option keys (`Active`, `Passive`, `Auto`) which failed Home Assistant's Hassfest validation. Lowercased across all 7 languages. No user action needed — option labels in the UI are unchanged.

### Improvements
- **UFH Buffer Now Per-Zone** — Underfloor heating buffer is now configured per zone (via Zone Configuration) instead of a global setting. Zones with heating type set to Underfloor automatically get the buffer applied.
- **Atomic Writes for Zone Config & Outdoor Temp** — Zone configuration and outdoor temperature history files now use the same crash-safe tempfile-then-rename pattern as other data files, so an unexpected shutdown can't leave these files half-written.
- **Translation Sync** — Added missing Adaptive Preheat Mode selector translations across all 7 languages (German, Spanish, French, Italian, Dutch, Portuguese).

## [3.3.1] - 2026-03-21

### Improvements
- **Smarter Weather Compensation Curve** ([#187](https://github.com/hiall-fyi/tado_ce/issues/187) - @driagi) — Preset heating curves (Radiators Standard, Radiators Low Temp, Underfloor) now automatically calculate the slope from your min/max flow and design/shutoff temperatures. Previously, a fixed slope could cause the flow temperature to hit the minimum floor well before the shutoff temperature, creating a flat zone where outdoor changes had no effect. Now the curve modulates smoothly across the entire outdoor range. Custom preset still gives you full manual control.

## [3.3.0] - 2026-03-21

### Features
- **Weather Compensation** ([#187](https://github.com/hiall-fyi/tado_ce/issues/187) - @driagi) — Automatically adjusts your boiler's flow temperature based on outdoor temperature. Pick from three presets (Radiators Standard, Radiators Low Temp, Underfloor) or create a custom heating curve. Includes smoothing, room feedback, and a 10-minute hold between adjustments to prevent oscillation. Configured via **Settings → Tado CE → Configure → Global Settings → Flow Temperature Control**.
- **Enable Internet Bridge Toggle** — Simple on/off toggle at the top of Flow Temperature Control. Turn it off and all bridge-related entities are automatically removed — no need to manually clear credentials.
- **Adaptive Preheat Passive Mode** ([#171](https://github.com/hiall-fyi/tado_ce/issues/171) - @thefern69) — Preheat now has three modes per zone: Off, Active (always triggers), and Passive (only triggers when the zone is following its schedule — skips preheat if you've set a manual override from HomeKit, the Tado app, etc.). Existing users with preheat enabled are automatically migrated to Active. Configured via **Options Flow → Zone Configuration**.
- **Restore Previous State** — New `tado_ce.restore_previous_state` service that puts a zone back to whatever it was doing before the last change. Works with heating, AC, and hot water. State is saved automatically before any overlay action (timers, temperature changes, open window mode, preheat). If nothing was saved, it falls back to resuming the schedule. Saved state survives HA restarts and clears when you arrive home.
- **Passive Window Detection** — Detects open windows even when your heating or AC is off. Combines temperature drop speed, humidity changes, and indoor-outdoor temperature difference to tell the difference between a natural cooldown and an open window. Adjusts for your window type and is stricter in winter.
- **Window Detection Mode Per Zone** — Choose how each zone detects open windows: Active (only when heating/cooling is running), Passive (works anytime), or Auto (picks the best method automatically). Configured via **Options Flow → Zone Configuration**.
- **Heat Index & Heat Risk** — The Comfort Level sensor now shows "feels like" temperature when it's warm (above 26.7°C), factoring in humidity. Risk levels (Caution, Extreme Caution, Danger, Extreme Danger) appear in the sensor attributes and in comfort recommendations.
- **Bridge Connected Sensor** — New binary sensor that shows whether your Internet Bridge is reachable. Includes health info like response time, failure count, and last successful connection as attributes.
- **Bridge Health Tracking** — The bridge connection sensor tolerates brief hiccups — it only marks the bridge as disconnected after 3 consecutive failures, so a single timeout won't trigger a false alarm.

### Bug Fixes
- **Fixed Preheat Triggering During Away Mode** ([#171](https://github.com/hiall-fyi/tado_ce/issues/171) - @thefern69) — Preheat could still fire during the Home→Away transition due to a timing gap. Now properly checks presence before any heating action, including on startup.
- **Fixed Open Window Mode Duration** — The `set_open_window_mode` service was sending the duration as text instead of a number, which could cause the Tado API to reject the request. Now sends it correctly.

### Improvements

_Flow Temperature Control & Bridge_

- **Flow Temperature Control Settings** — Bridge credentials and weather compensation settings are now in one place instead of two separate menus, so there's less clicking around.
- **Fewer Bridge Entities by Default** — Only the most useful bridge entities are visible out of the box (Bridge Connected, Wiring State, Boiler Output Temperature, Boiler Flow Temperature). The rest are hidden and can be enabled manually if you need them.
- **Bridge Serial Validation** ([#187](https://github.com/hiall-fyi/tado_ce/issues/187) - @ChrisMarriott38) — The bridge serial field now checks that it starts with `IB` (v3+ bridge). V2 bridges (`GW` serial) aren't supported by the Bridge API. Weather Compensation still works without a bridge via cloud data.
- **Weather Compensation Blueprint Updated** ([#187](https://github.com/hiall-fyi/tado_ce/issues/187) - @driagi) — Blueprint tuned to reduce oscillation: larger step size (1.0°C), wider deadband (1.0°C), and 10-minute hold between adjustments.

_Window Detection_

- **Smoother Window Detection** — The Window Predicted sensor no longer flickers on/off rapidly. It now waits for several stable readings before clearing a detection (3 readings on Low sensitivity, 2 on Medium, 1 on High).
- **Window Detection Events** — HA events (`tado_ce_window_predicted` and `tado_ce_window_predicted_cleared`) now fire when a window is detected or cleared — useful for building your own automations.
- **Window Detection History** — The Window Predicted sensor now tracks when the last detection happened, how many times today, and which detection mode was used. Daily count resets at midnight.
- **Open Window Mode Saves State** — When `set_open_window_mode` activates, it now saves what the zone was doing first. After the window is closed, use `restore_previous_state` to go back to exactly where you were.

_Other_

- **Default Temperature on First Install** ([#182](https://github.com/hiall-fyi/tado_ce/issues/182) - @neonsp) — Climate entities now start with a sensible default (20°C heating, 24°C AC) instead of showing blank controls on first install.

## [3.2.2] - 2026-03-16

### Bug Fixes
- **Fixed Boiler Output Temperature Sensor Showing Wrong Value** ([#187](https://github.com/hiall-fyi/tado_ce/issues/187) - @driagi) — The boiler output temperature sensor was reading from the wrong data field, so it never showed the actual temperature. Now displays the correct real-time value. Also fixed the Wiring State sensor's extra attributes.

## [3.2.1] - 2026-03-16

### Features
- **Indefinite Open Window Mode** ([Discussion #184](https://github.com/hiall-fyi/tado_ce/discussions/184) - @jeverley) — The `set_open_window_mode` service now supports `duration: 0` to keep the window mode active until you manually resume. Great for contact sensor automations where you want full control.

### Bug Fixes
- **Fixed Bridge Sensor Showing "Unknown"** ([#187](https://github.com/hiall-fyi/tado_ce/issues/187) - @driagi) — Bridge API sensors were stuck on "Unknown" even when the bridge was connected. Now shows boiler temperature data correctly. Added better logging for troubleshooting.

## [3.2.0] - 2026-03-16

**Bridge API Integration — Flow Temperature Control**

### Features
- **Bridge API Integration** — Connect to your Tado Internet Bridge for direct boiler monitoring. Enter your bridge serial and auth key in Settings, and you'll get sensors for boiler wiring state, output temperature, and a control to set the max output temperature (25–80°C). Bridge data is fetched separately from the cloud, so bridge issues never affect your other sensors.
- **Bridge Entity Cleanup** — Remove your bridge credentials and all bridge-related entities are automatically cleaned up.
- **Set Open Window Mode Service** ([#172](https://github.com/hiall-fyi/tado_ce/issues/172), [Discussion #184](https://github.com/hiall-fyi/tado_ce/discussions/184) - @driagi) — New `set_open_window_mode` service lets you trigger open window mode from your own contact sensors (Zigbee, Z-Wave, etc.) without waiting for Tado's built-in detection. Defaults to your zone's timeout setting or 15 minutes.

### Bug Fixes
- **Fixed Climate Card Blank After HA Restart** ([#182](https://github.com/hiall-fyi/tado_ce/issues/182) - @neonsp) — The climate card no longer shows a blank temperature after restarting HA. Your last target temperature is now restored automatically, so the controls work right away.
- **Fixed External Sensor Not Updating Instantly** ([#143](https://github.com/hiall-fyi/tado_ce/issues/143) - @BirbByte) — External temperature and humidity sensors (HomeKit, Zigbee, etc.) now update the climate card immediately when the value changes, instead of waiting for the next poll cycle.

## [3.1.1] - 2026-03-15

**Manual Token Auth, Climate Card Fix & Smart AC Mode**

### Features
- **Manual Token Authentication** ([#185](https://github.com/hiall-fyi/tado_ce/issues/185)) — New fallback login method for when Tado's authorization server is down. You can now paste a refresh token from the Tado web app as an alternative way to sign in.

### Bug Fixes
- **Fixed Climate Card Unusable When Zone is OFF** ([#182](https://github.com/hiall-fyi/tado_ce/issues/182)) — When a heating zone is off, the climate card now keeps the last temperature showing so the slider and controls still work.

### Improvements
- **Updated Global Settings Description** ([Discussion #76](https://github.com/hiall-fyi/tado_ce/discussions/76)) — The setup instructions now match the current toggle-based settings layout.
- **AC Picks the Right Mode Automatically** ([#182](https://github.com/hiall-fyi/tado_ce/issues/182) - @neonsp) — When you turn on an AC zone by setting a temperature, it now picks HEAT or COOL based on whether the target is above or below the current temperature.

## [3.1.0] - 2026-03-14

**Options Flow Zone Configuration, Open Window Services, External Sensor Override & Entity Registry**

### Features
- **Open Window Services** ([#172](https://github.com/hiall-fyi/tado_ce/issues/172) - @driagi) — New `activate_open_window` and `deactivate_open_window` services let you trigger open window mode from your own window sensors (e.g., Zigbee contact sensors) instead of waiting 15+ minutes for Tado to detect it. A free Auto-Assist replacement via HA automations.
- **External Temperature & Humidity Sensors** ([#106](https://github.com/hiall-fyi/tado_ce/issues/106), [#143](https://github.com/hiall-fyi/tado_ce/issues/143) - @BirbByte) — Use any HA sensor (HomeKit, Zigbee, etc.) instead of Tado's built-in sensor for each zone. Configured in Zone Configuration.
- **Window Predicted Sensitivity** ([#135](https://github.com/hiall-fyi/tado_ce/issues/135) - @ChrisMarriott38) — Adjust how sensitive the open window prediction is per zone (Low, Medium, or High) to reduce false alarms.
- **Hub Control Switches** — New Test Mode and Quota Reserve switches on the hub device. Toggle them from your dashboard without going into Settings.
- **Options Flow Menu** — Settings are now organized into Global Settings and Zone Configuration sections for easier navigation.

### Bug Fixes
- **Fixed AC Max Temperature Capped at 25°C** ([#180](https://github.com/hiall-fyi/tado_ce/issues/180)) — AC zones that support up to 30°C were incorrectly limited to 25°C. Now uses your AC's actual temperature range unless you've set a custom override.

### Improvements
- **Zone Configuration Moved to Options Flow** — All per-zone settings (overlay mode, timer, temperature limits, offsets, heating type, etc.) are now in one place under Settings → Configure → Zone Configuration. No more config entities cluttering your dashboard.
- **Renamed "Tado Mode" → "Tado Default"** ([#176](https://github.com/hiall-fyi/tado_ce/issues/176)) — The overlay mode name now matches what Tado calls it.
- **Entity Categories Added** ([#178](https://github.com/hiall-fyi/tado_ce/issues/178)) — Configuration and diagnostic entities are now properly categorized, so they're organized correctly in the HA UI.
- **Smarter Full Sync** ([#141](https://github.com/hiall-fyi/tado_ce/issues/141) - @Xavinooo) — Full data sync now only runs on HA restart instead of every 6 hours, saving API calls.
- **Test Mode & Quota Reserve Skip Reload** — Toggling these settings no longer restarts the integration.
- **Health Score Formatting** — Home Insights health score now shows with emoji and label (e.g., "🟢 92 — Excellent") for quick reading.
- **Improved Translations** — All 6 non-English languages revised with more natural wording. Service names and descriptions now translated in all 7 languages.

## [3.0.4] - 2026-03-12

### Bug Fixes
- **Fixed API Reset Time Estimate Off by Hours** ([#173](https://github.com/hiall-fyi/tado_ce/issues/173) - @driagi) — The estimated API reset time was off by about 6 hours. Now calculates more accurately using your actual day/night schedule.
- **Fixed API Reset History Detection** ([#173](https://github.com/hiall-fyi/tado_ce/issues/173)) — History-based reset detection was silently failing. Added logging to help troubleshoot.
- **Fixed Smart Comfort Description** — The options flow description was listing the wrong sensors. Now correctly describes what's included.

### Improvements
- **Better Entity Cleanup** — Removing entities is now more accurate and won't accidentally remove the wrong ones.

## [3.0.3] - 2026-03-11

### Bug Fixes
- **Fixed Hub Sensors Stuck on Old Values** ([#173](https://github.com/hiall-fyi/tado_ce/issues/173) - @driagi) — Hub sensors (API Limit, Reset Time, Status, Polling Interval, Next Sync) were not updating after each sync. Now refreshes in real-time.

### Improvements
- **Better Feature Toggle Cleanup** — Disabling Weather or Mobile Device features now properly removes their entities and leftover devices.

## [3.0.2] - 2026-03-11

### Bug Fixes
- **Fixed Setup Hanging for Minutes** ([#170](https://github.com/hiall-fyi/tado_ce/issues/170) - @driagi, @tigro7, @mpartington) — The integration could hang during setup for up to 80 minutes if you had old API call history. Now starts up normally.
- **Fixed Preheat Firing During Away Mode** ([#171](https://github.com/hiall-fyi/tado_ce/issues/171) - @thefern69) — Preheat was still triggering even when nobody was home. Now correctly pauses when you're away.
- **Fixed Hub Sensors Showing Wrong Values** ([#173](https://github.com/hiall-fyi/tado_ce/issues/173) - @driagi) — Polling interval and reset time sensors were showing defaults instead of actual values.
- **Fixed Performance Warning During Sensor Update** — Resolved a warning caused by slow file access during sensor updates.
- **Fixed Raw Values in Insights** — Home Insights were showing internal codes instead of readable names.

### Improvements
- **Smarter Preheat for Active Heating** ([Discussion #163](https://github.com/hiall-fyi/tado_ce/discussions/163) - @thefern69) — Preheat now also considers cooling trends for the current temperature target, not just the next schedule change. Especially helpful for underfloor heating.
- **Cleaner Insights Display** — Insights now show grouped, emoji-prefixed lines for easier reading.
- **Shorter Recommendations** — Per-zone recommendations no longer repeat the zone name.

## [3.0.1] - 2026-03-10

### Bug Fixes
- **Removed `[CE]` Prefix from Entity Names** ([#167](https://github.com/hiall-fyi/tado_ce/issues/167) - @jeverley) — Entity names no longer include the `[CE]` prefix. Entity IDs are unchanged.
- **Fixed Duplicate Sensor Names** ([#167](https://github.com/hiall-fyi/tado_ce/issues/167) - @hapklaar) — Zones with multiple devices (e.g., a sensor and two valves) now include the device type in battery and connection sensor names to avoid duplicates.
- **Fixed Entities Going Unavailable Every 5 Minutes** ([#167](https://github.com/hiall-fyi/tado_ce/issues/167) - @hapklaar, @andyb2000) — Token refresh was accidentally restarting the integration every poll cycle, briefly making all entities unavailable. Now handles token updates silently.
- **Fixed Missing Translations** — 3 translation keys added to all 7 language files.

## [3.0.0] - 2026-03-10

**Multi-Home Support & Actionable Insights**

### Features
- **Multi-Home Support** ([#110](https://github.com/hiall-fyi/tado_ce/issues/110) - @robvol87, [#145](https://github.com/hiall-fyi/tado_ce/issues/145) - @Blankf) — Run multiple Tado accounts or homes in a single HA instance. Each home is fully isolated with its own data and settings.
- **Smarter Insight Summaries** — Home insights now show action-based summaries (e.g., "Replace batteries: Guest, Lounge — Mold risk: Bedroom") instead of generic counts.
- **Related Insights Merged** — Related issues in a zone are combined into a single action item (e.g., mold risk + high humidity + condensation → "humidity problem").
- **Insight History & Trending** — Insights are tracked across HA restarts with duration-aware messages and a weekly digest.
- **Insight Priority Escalation** — Issues that persist automatically escalate in priority (e.g., low battery for over 7 days becomes high priority).
- **Home Health Score** — A 0-100 score reflecting your overall home health, shown on the Home Insights sensor.
- **Preheat Cooling Rate Prediction** ([Discussion #163](https://github.com/hiall-fyi/tado_ce/discussions/163) - @thefern69) — Preheat now considers cooling trends when the room is above target, estimating when it will drop below target for proactive heating.

### Bug Fixes
- **Fixed Auth URL Showing 404** ([#104](https://github.com/hiall-fyi/tado_ce/issues/104)) — The authorization link during setup no longer leads to a broken page.
- **Fixed Window Sensor Not Working Without Auto-Assist** ([#157](https://github.com/hiall-fyi/tado_ce/issues/157) - @tanerpaca) — Window sensor now detects open windows even without an Auto-Assist subscription.
- **Fixed Preheat Triggering a Day Early** ([#164](https://github.com/hiall-fyi/tado_ce/issues/164) - @thefern69) — Preheat sensor no longer fires a day before it should.
- **Fixed Heating Rate Unit** — Heating rate was showing inconsistent values depending on which internal path computed it — some sites reported °C/min, others °C/h. Standardised on °C/h everywhere and set the sensor's unit attribute accordingly.

### Improvements
- **Timer Minimum Lowered to 1 Minute** ([#162](https://github.com/hiall-fyi/tado_ce/issues/162) - @joaomacp) — Climate and water heater timers now accept durations as short as 1 minute (was 15).
- **7-Language Support** — Config flow and options UI now available in English, German, Spanish, French, Italian, Dutch, and Portuguese.

## [2.3.1] - 2026-02-26

### Bug Fixes
- **Fixed AC Fan Speed Reverting on Some Brands** ([#142](https://github.com/hiall-fyi/tado_ce/issues/142) - @BirbByte) — Setting "High" fan speed on Mitsubishi/Fujitsu units would revert back. Fan speed options are now built from your AC's actual capabilities.
- **Fixed Startup Warning on Fresh Install** ([#127](https://github.com/hiall-fyi/tado_ce/issues/127) - @slflowfoon) — Resolved a warning that appeared on first install when the data folder didn't exist yet.

### Improvements
- **AC Capabilities Live Reload** — After pressing "Refresh AC Capabilities", AC entities update automatically without restarting HA.
- **Smarter AC Temperature Defaults** — Default temperature when switching modes now uses your AC's actual range instead of a fixed value.
- **Smoother AC Controls** — Fan and swing mode selections no longer flicker during updates.

---

## [2.3.0] - 2026-02-25

### Features
- **21 New Insight Types** — More actionable insights across zone efficiency, schedule, occupancy, weather, humidity, device health, and cross-zone analysis.

### Bug Fixes
- **Fixed Mold Risk Giving Wrong Advice** ([#147](https://github.com/hiall-fyi/tado_ce/issues/147) - @ChrisMarriott38) — Mold risk no longer suggests turning up the heating when the room is already warm enough. Now suggests ventilation or a dehumidifier instead.
- **Fixed Hot Water Overlay Showing for Combi Boilers** ([#149](https://github.com/hiall-fyi/tado_ce/issues/149) - @ChrisMarriott38) — Overlay Mode and Timer Duration entities no longer appear for combi boiler hot water zones where they don't apply.
- **Fixed Mobile Device Tracker Not Updating** ([#150](https://github.com/hiall-fyi/tado_ce/issues/150) - @driagi) — Device tracker was stuck on the state from last HA restart. Now updates every 30 seconds.

### Improvements
- **Flexible Climate Timer** ([#152](https://github.com/hiall-fyi/tado_ce/issues/152) - @mpartington) — `time_period` is now optional in the `set_climate_timer` service. Use `overlay: next_time_block` for "until next schedule change" or `overlay: manual` for indefinite.

---

## [2.2.3] - 2026-02-24

**Smart Day/Night Polling, AC Fan Fix & Climate Group Support**

### Bug Fixes
- **Fixed Polling Stuck at 120 Minutes for Low-Quota Users** ([#144](https://github.com/hiall-fyi/tado_ce/issues/144) - @mkruiver) — Users with few API calls left now get smart day/night polling instead of being stuck on very slow intervals.
- **Fixed Night Polling Using Wrong Interval** ([#141](https://github.com/hiall-fyi/tado_ce/issues/141) - @Xavinooo) — Day period quota now correctly accounts for your custom night interval setting.
- **Fixed AC Fan Speed Reverting** ([#142](https://github.com/hiall-fyi/tado_ce/issues/142) - @BirbByte) — Fan speed validation now checks against your AC's actual supported levels.

### Improvements
- **Climate Group Support** ([#139](https://github.com/hiall-fyi/tado_ce/discussions/139) - @merlinpimpim) — `set_climate_timer`, `set_water_heater_timer`, and `resume_schedule` services now work with climate groups defined in `configuration.yaml`.

---

## [2.2.2] - 2026-02-23

### Bug Fixes
- **Fixed API Polling Options Not Saving** ([#134](https://github.com/hiall-fyi/tado_ce/issues/134) - @Xavinooo) — Custom polling intervals now save correctly even when only one field is filled, and clearing a custom interval properly persists.

---

## [2.2.1] - 2026-02-23

### Bug Fixes
- **Fixed Hot Water Settings Missing for Tank Systems** ([#115](https://github.com/hiall-fyi/tado_ce/issues/115) - @jeverley) — Tank-based hot water users now correctly see Overlay Mode and Timer Duration controls.
- **Fixed Custom Polling Intervals Not Saving** ([#134](https://github.com/hiall-fyi/tado_ce/issues/134) - @ChrisMarriott38, @Xavinooo) — Custom polling intervals now save correctly after changing options.

---

## [2.2.0] - 2026-02-23

**Calibration Sensors & Actionable Insights**

### Features
- **Surface Temperature Sensor** ([#118](https://github.com/hiall-fyi/tado_ce/issues/118)) — Shows the calculated cold spot temperature in each zone, so you can calibrate mold risk with a laser thermometer.
- **Dew Point Sensor** ([#118](https://github.com/hiall-fyi/tado_ce/issues/118)) — Useful for dehumidifier automations and condensation prevention.
- **Window Predicted Sensor** ([Discussion #112](https://github.com/hiall-fyi/tado_ce/discussions/112) - @tigro7) — Detects open windows early by spotting unusual temperature drops, giving you a heads-up minutes before Tado's cloud detection kicks in.
- **Actionable Recommendations** ([Discussion #112](https://github.com/hiall-fyi/tado_ce/discussions/112) - @tigro7) — Environment, device, and hub sensors now include a `recommendation` attribute with specific advice.
- **Home Insights Sensor** ([Discussion #112](https://github.com/hiall-fyi/tado_ce/discussions/112) - @tigro7) — A hub-level sensor that aggregates insights from all zones with priority ranking.
- **Zone Insights Sensor** ([Discussion #112](https://github.com/hiall-fyi/tado_ce/discussions/112) - @tigro7) — Per-zone insights with an icon that changes based on the highest priority issue.

### Bug Fixes
- **Fixed Long Heating Cycles Never Completing** ([#125](https://github.com/hiall-fyi/tado_ce/issues/125) - @BruceRobertson) — Heating cycles longer than 50 minutes now complete correctly.
- **Fixed First-Run Error** ([#127](https://github.com/hiall-fyi/tado_ce/issues/127) - @slflowfoon, [PR #132](https://github.com/hiall-fyi/tado_ce/pull/132) - @hacker4257) — Fixed an error on first install when the storage folder didn't exist yet.
- **Fixed Hot Water Settings Missing for Tank Systems** ([#115](https://github.com/hiall-fyi/tado_ce/issues/115) - @jeverley) — Tank-based hot water systems now correctly get Overlay Mode and Timer Duration controls.
- **Fixed Custom Polling Issues** ([#126](https://github.com/hiall-fyi/tado_ce/issues/126) - @Xavinooo) — Custom night interval, day/night detection, and settings persistence all fixed.
- **Fixed AC Swing Mode for Mitsubishi Units** ([#128](https://github.com/hiall-fyi/tado_ce/issues/128) - @BirbByte) — AC units that don't support "OFF" as a swing value no longer get API errors.
- **Fixed Environment Sensor Cleanup** — All sensors now correctly removed when the feature is toggled off.
- **Fixed Heating Anomaly False Alarms** — Heating anomaly insight no longer fires on every update.

### Improvements
- **Readable Attribute Values** — Zone type, window type, and comfort model attributes now show human-readable names instead of internal codes.

---

## [2.1.1] - 2026-02-19

### Bug Fixes
- **Fixed Test Mode Using Wrong Reset Time** ([#120](https://github.com/hiall-fyi/tado_ce/issues/120), [#119](https://github.com/hiall-fyi/tado_ce/issues/119) - @ChrisMarriott38) — Test Mode polling intervals now calculate correctly.
- **Fixed Hot Water Zones Showing Wrong Entities** ([#115](https://github.com/hiall-fyi/tado_ce/issues/115) - @ChrisMarriott38) — Per-zone configuration controls (Surface Temp Offset, Min/Max Temp, etc.) no longer appear for hot water zones where they don't apply.

## [2.1.0] - 2026-02-18

**Per-Zone Configuration**

### Features
- **Per-Zone Overlay Mode** — Choose how long manual temperature changes last, separately for each zone (Tado Default, Timer, or Manual).
- **Per-Zone Timer Duration** — Set a custom timer duration per zone (15–180 minutes).
- **Per-Zone Thermal Analytics** ([#91](https://github.com/hiall-fyi/tado_ce/issues/91)) — Choose which zones have Thermal Analytics sensors. Zones that never call for heat can be turned off to keep your UI clean.
- **Per-Zone Surface Temp Offset** ([#90](https://github.com/hiall-fyi/tado_ce/issues/90)) — Calibrate mold risk calculation per zone using a laser thermometer reading.

### Bug Fixes
- **Fixed Preheat Time Showing `unknown` After Restart** — Preheat Time sensors now display correctly after HA restart.
- **Fixed Overlay Mode API Error** — Overlay termination now correctly maps to the Tado API.
- **Fixed Custom Polling Below 5 Minutes** ([#107](https://github.com/hiall-fyi/tado_ce/issues/107) - @jakeycrx) — Custom intervals of 1–4 minutes now work correctly.

### Improvements
- **Simplified Options UI** — Test Mode moved into the Tado CE Exclusive section for a cleaner layout.

## [2.0.2] - 2026-02-14

**Presence Mode Select & Configurable Overlay Mode**

### ⚠️ Breaking Changes
- **Presence Mode Select** — `switch.tado_ce_away_mode` has been replaced by `select.tado_ce_presence_mode` ([Discussion #102](https://github.com/hiall-fyi/tado_ce/discussions/102) - @wyx087). Update your automations from `switch.turn_on/turn_off` to `select.select_option`.

### Features
- **Presence Mode Select** — New 3-option select: Auto (resume geofencing), Home, or Away.
- **Configurable Overlay Mode** ([#101](https://github.com/hiall-fyi/tado_ce/issues/101) - @leoogermenia) — Choose how long manual temperature changes last: Tado Default, Next Time Block, or Manual (indefinite).

### Bug Fixes
- **Fixed Custom Polling Below 5 Minutes** ([#107](https://github.com/hiall-fyi/tado_ce/issues/107) - @jakeycrx) — Custom intervals of 1–4 minutes now work.
- **Fixed Polling Stuck at 120 Minutes** ([#99](https://github.com/hiall-fyi/tado_ce/issues/99) - @ChrisMarriott38) — Polling now calculates correctly when day and night hours are the same.

## [2.0.1] - 2026-02-12

**Mold Risk Percentage, Hot Water Fix & Bootstrap Reserve**

### Features
- **Mold Risk Percentage Sensor** ([#90](https://github.com/hiall-fyi/tado_ce/issues/90)) — New sensor for tracking mold risk over time in HA history.
- **Bootstrap Reserve Protection** ([#99](https://github.com/hiall-fyi/tado_ce/issues/99) - @ChrisMarriott38) — Reserves 3 API calls so the integration can always recover after a restart.
- **Test Mode** — Fully simulates the 100-call API tier with its own 24-hour cycle for testing.
- **Day/Night Aware Polling** — Uses fixed 120-minute intervals at night and adaptive intervals during the day based on remaining quota.

### Bug Fixes
- **Fixed Climate Entities Unavailable After Upgrade** ([#100](https://github.com/hiall-fyi/tado_ce/issues/100) - @Claeysjens) — Climate entities could stay unavailable after upgrading from an earlier v2.x release because of a state migration gap. Now restores correctly on first load after upgrade.
- **Fixed Hot Water Temperature Jumping Back** ([#98](https://github.com/hiall-fyi/tado_ce/issues/98) - @ChrisMarriott38) — Temperature changes no longer revert in the UI.
- **Fixed Quota Reserve Not Preventing API Limit** ([#99](https://github.com/hiall-fyi/tado_ce/issues/99) - @ChrisMarriott38) — The quota reserve guard previously didn't block all API-spending paths, so the integration could still exceed the Tado daily limit when reserve was active. All API entry points now honour the reserve.
- **Fixed Mold Risk Calculation** ([#90](https://github.com/hiall-fyi/tado_ce/issues/90) - @ChrisMarriott38) — Now correctly uses room temperature for dew point calculation.
- **Fixed Thermal Analytics Missing for Some Zones** ([#91](https://github.com/hiall-fyi/tado_ce/issues/91) - @ChrisMarriott38) — Sensors now appear for all zones with heating data, not just TRV zones.

## [2.0.0] - 2026-02-09

**Smart Polling, Mold Risk Enhancement & Thermal Analytics**

### Features
- **API Monitoring Sensors** ([#86](https://github.com/hiall-fyi/tado_ce/discussions/86), [#65](https://github.com/hiall-fyi/tado_ce/issues/65)) — New sensors showing next sync time, last sync, polling interval, call history, and API breakdown.
- **Thermal Analytics** — New sensors for heating zones: thermal inertia, average heating rate, preheat time, and more.
- **Adaptive Smart Polling** ([#89](https://github.com/hiall-fyi/tado_ce/issues/89) - @ChrisMarriott38) — Polling frequency automatically adjusts based on your remaining API quota.
- **Quota Reserve Protection** ([#94](https://github.com/hiall-fyi/tado_ce/issues/94) - @ChrisMarriott38) — Pauses polling when your API quota is critically low to prevent hitting the limit.
- **Enhanced Mold Risk** ([#90](https://github.com/hiall-fyi/tado_ce/issues/90) - @ChrisMarriott38) — Surface temperature calculation with configurable window type for more accurate mold risk assessment.

### Bug Fixes
- **Fixed hot water timer buttons not finding the right entity** ([#93](https://github.com/hiall-fyi/tado_ce/issues/93) - @Fred224) — Timer button entity IDs were constructed from the zone name, but HA may add suffixes when the ID conflicts with another integration. Buttons now look up the water heater entity via the entity registry by unique ID, with name-based lookup as fallback.

### Improvements
- **Removed "Tado CE" prefix from entity names** — Hub sensors were prefixed with "Tado CE " (e.g. "Tado CE API Usage") which was redundant once every entity was grouped under the Tado CE Hub device. Prefix dropped — entity IDs unchanged so existing automations and dashboards keep working.

## [1.10.0] - 2026-02-05

### Bug Fixes
- **Fixed Climate Entity Flickering** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @hapklaar, @chinezbrun, @neonsp) — Climate entities no longer flicker or revert when you make changes. Multiple layers of protection prevent stale data from overwriting your actions.

## [1.9.7] - 2026-02-04

### Bug Fixes
- **Fixed state flickering when quickly changing modes** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @chinezbrun) — Rapid mode changes (e.g. Auto → Heat → Off within a few seconds) could leave the climate card flicking between states as the optimistic UI reconciled against delayed API responses. Optimistic state tracking now correctly holds the latest user intent until the API confirms it.

## [1.9.6] - 2026-02-04

### Bug Fixes
- **Fixed heating/cooling status reverting after a mode change** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @hapklaar, @chinezbrun) — After switching modes, the `hvac_action` attribute could briefly revert to the pre-change value on the next poll, making automations that react to heating/cooling state fire twice.

## [1.9.5] - 2026-02-02

### Bug Fixes
- **Fixed heating/cooling status not updating when setting temperature** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @hapklaar, @chinezbrun) — Changing the target temperature didn't update the `hvac_action` attribute until the next poll cycle, so automations tracking heating state missed the transition.

## [1.9.4] - 2026-02-02

**Boost Buttons**

### Features
- **Boost Button** — One tap to set any zone to 25°C for 30 minutes.
- **Smart Boost Button** — Intelligent boost that calculates the right duration automatically.

### Bug Fixes
- **Fixed heating status stuck on "Heating" after switching to Auto** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @hapklaar) — The climate card kept showing "heating" after switching from a manual target back to Auto, even when the zone was actually idle.
- **Fixed AC startup warnings** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @neonsp) — Spurious warnings on HA startup about missing AC capabilities resolved.
- **Fixed slow zone sensor updates** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @chinezbrun) — Zone sensors lagged behind climate card state by up to a poll cycle; now updated in lock-step.

## [1.9.3] - 2026-02-02

### Bug Fixes
- **Fixed slow state updates for heating users** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @hapklaar, @chinezbrun) — Heating-mode state changes took longer to reach HA than expected. Optimistic state updates now push the new state immediately while the API catches up.
- **Fixed AC DRY mode error** ([#79](https://github.com/hiall-fyi/tado_ce/issues/79) - @Fred224, @neonsp) — Setting the AC to DRY mode raised an API error on some AC models. Now correctly mapped.

## [1.9.2] - 2026-02-01

### Bug Fixes
- **Fixed grey loading state on climate card** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @chinezbrun) — The climate card showed a grey "loading" overlay for several seconds after any state change. Resolved by updating optimistic state synchronously instead of waiting for the async roundtrip.

## [1.9.1] - 2026-01-31

### Bug Fixes
- **Fixed crash on startup during device migration** ([#74](https://github.com/hiall-fyi/tado_ce/issues/74)) — Upgrading from an earlier 1.x release could crash at startup when the device migration path encountered a zone with no devices registered. Migration now handles empty device lists gracefully.

## [1.9.0] - 2026-01-31

**Smart Comfort Analytics + Environment Sensors**

### Features
- **Smart Comfort Analytics** — New sensors for heating/cooling rate, time to target, and heating efficiency.
- **Smart Comfort Insights** ([#33](https://github.com/hiall-fyi/tado_ce/discussions/33)) — Historical comparison, preheat advisor, and smart comfort target recommendations.
- **Environment Sensors** ([#64](https://github.com/hiall-fyi/tado_ce/issues/64)) — Mold risk and comfort level sensors for each zone.
- **Schedule Sensors** — Shows the next scheduled time and temperature for each zone.

### Bug Fixes
- **Fixed API reset detection for the 100-call limit** ([#54](https://github.com/hiall-fyi/tado_ce/issues/54)) — Users on the free 100-call API tier saw incorrect reset time estimates. The detection logic now reads the daily reset schedule correctly for low-quota accounts.
- **Fixed temperature offset for rooms with multiple TRVs** ([#66](https://github.com/hiall-fyi/tado_ce/issues/66)) — The temperature offset service only wrote to the first TRV in a zone; rooms with multiple radiator valves now get the offset applied to every TRV.
- **Fixed sensors being assigned to the wrong device** ([#56](https://github.com/hiall-fyi/tado_ce/issues/56)) — Some zone sensors appeared under the Hub device instead of the correct zone device. Device assignment now uses the zone's unique ID for lookup.

## [1.8.3] - 2026-01-26

### Features
- **Refresh AC Capabilities button** — New button on the Hub device to reload your AC unit's supported modes, fan speeds, and swing options without restarting HA. Useful if Tado's reported capabilities change or if you want to re-sync after connecting a new AC unit.

### Bug Fixes
- **Fixed AC not responding immediately after turning on** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @neonsp) — The first command after turning on an AC zone could take several seconds to register. Optimistic state now applies the command right away while the API catches up.

### Improvements
- **AC capabilities are now cached to save API calls** ([#61](https://github.com/hiall-fyi/tado_ce/issues/61) - @neonsp) — AC unit capabilities (supported modes, fan speeds, swing options) are only fetched when you first add the integration or manually refresh, instead of on every startup.

## [1.8.2] - 2026-01-26

### Bug Fixes
- **Fixed Resume All Schedules taking too long to respond** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @hapklaar) — The Resume All Schedules button could take several seconds to update the UI after pressing. Now refreshes immediately after the API confirms.

### Improvements
- **Smoother AC controls** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @neonsp) — Selections in AC mode, fan speed, and swing dropdowns no longer flicker during updates — optimistic state holds the new value until the API confirms.

## [1.8.1] - 2026-01-26

### Bug Fixes
- **Fixed AC instant feedback not working** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @neonsp) — AC mode and temperature changes didn't update the UI immediately, leaving users unsure whether the command had registered.
- **Fixed Resume All Schedules not refreshing the UI** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @hapklaar) — After pressing Resume All Schedules, the climate cards stayed on their overlay targets until the next poll. Now refresh immediately.

## [1.8.0] - 2026-01-26

**Schedule Calendar + Multi-Home Prep**

### Features
- **Schedule Calendar** — See your heating schedules as a calendar per zone, with each scheduled block appearing as a calendar event.
- **Per-zone Refresh Schedule button** — Trigger an on-demand schedule refresh for a specific zone without waiting for the next poll cycle.
- **API Reset sensor now shows extra details** ([#54](https://github.com/hiall-fyi/tado_ce/issues/54) - @ChrisMarriott38) — The API Reset sensor attributes now include next reset time, calls used this period, and detection method.

### Improvements
- **Multi-home preparation** — Data files are now stored per home (rather than globally) in preparation for the full multi-home support that lands in v3.0.0. No user action needed; existing single-home installs continue to work unchanged.

## [1.7.0] - 2026-01-26

### Features
- **Instant UI feedback** — Temperature changes, mode switches, and preset selections now show in the UI immediately instead of waiting for the next poll cycle to confirm. Optimistic state holds the new value until the API catches up.
- **Optional home state sync to save API calls** — Fetching Tado's cloud-side home presence state on every poll is now optional via a toggle in Options Flow. With it off, presence changes made on HA propagate to Tado without also polling for server-side changes you don't use.

### Improvements
- **Multi-home preparation — unique ID migration** — Entity unique IDs now include the home ID in preparation for the full multi-home support that lands in v3.0.0. Existing single-home installs are migrated automatically on first start; no user action needed.

## [1.6.3] - 2026-01-25

### Improvements
- **Uses HA history to detect API reset time more accurately** — The API reset time sensor now queries HA's history for past rate-limit sensor values to infer the actual reset window, rather than relying on fixed assumptions that could drift from Tado's real reset time.

## [1.6.2] - 2026-01-25

### Bug Fixes
- **Fixed API call history not being recorded** — API call counts were tracked in memory but not persisted, so the API usage sensor started from zero on every HA restart. Now correctly records history across restarts.
- **Fixed timezone issues in various sensors** — Sensors that displayed times (reset time, next sync, last sync) could show wrong values for users in non-UTC timezones. All time displays now honour HA's configured timezone.

## [1.6.1] - 2026-01-25

### Features
- **Configurable delay between rapid updates** — New Options Flow setting to adjust the debounce window for rapid state changes (e.g. slider drags). Prevents API spam when tuning temperatures quickly.

### Bug Fixes
- **Fixed API Usage and Reset sensors showing 0** — A regression in 1.6.0 left the API Usage and Reset sensors stuck on 0 regardless of actual quota state. Now reads the correct values on every poll.

## [1.6.0] - 2026-01-25

### Features
- **Faster API calls — migrated to native async** — The integration no longer spawns subprocesses to run Tado API calls. All HTTP requests now use HA's native aiohttp client, reducing per-call overhead from ~100ms to <10ms and eliminating the stability issues associated with subprocess-based calls.

### Bug Fixes
- **Fixed database migration issue from older versions** — Upgrading from pre-1.5 releases could fail mid-migration, leaving entities in an inconsistent state. Migration is now idempotent and can recover from partial prior runs.
- **Fixed `climate.set_temperature` ignoring the mode you selected** — Passing `hvac_mode` along with `temperature` to the service was being silently dropped — only the temperature was applied. Now both fields are honoured in the same call.

## [1.5.5] - 2026-01-24

### Bug Fixes
- **Fixed AC Auto mode accidentally turning off the AC** — Setting an AC zone to Auto mode could be misinterpreted by Tado's API as "off". The mode now maps to the correct API value so Auto stays Auto.

### Improvements
- **Reduced API calls when changing settings** — Rapid setting changes in the Options Flow no longer trigger multiple config reloads; changes are coalesced and applied once.

## [1.5.4] - 2026-01-24

### Features
- **Unified swing dropdown for AC** — Vertical and horizontal swing options are now combined into a single dropdown with Off/Vertical/Horizontal/Both, matching the official Tado integration's layout.

### Bug Fixes
- **Fixed all AC control issues (modes, fan speed, swing)** — A set of regressions from 1.5.3 affecting AC mode changes, fan speed selections, and swing mode updates are all resolved in this release.

## [1.5.3] - 2026-01-24

### Features
- **Resume All Schedules button** — New one-tap button on the Hub device that resumes the schedule on every zone at once, instead of clicking each zone's resume button individually.

### Bug Fixes
- **Fixed AC control errors** — Several AC mode and fan speed commands were failing with API errors due to incorrect parameter mapping. All AC commands now pass the correct API payload.

## [1.5.2] - 2026-01-24

### Bug Fixes
- **Fixed losing your login after a HACS upgrade** — A HACS upgrade could wipe the stored refresh token, forcing you to re-authenticate after every update. The token storage path now survives HACS upgrades.

## [1.5.1] - 2026-01-24

### Features
- **Re-authenticate option in the UI** — New menu option in the integration's overflow menu to trigger the re-auth flow without removing and re-adding the integration.

### Bug Fixes
- **Fixed login errors for new users** — First-time setup could fail on accounts that hadn't been used with the old official integration because of a stale token format assumption. New-account onboarding now works cleanly.

## [1.5.0] - 2026-01-24

**Async Architecture Rewrite**

### Features
- **Temperature offset service** — New `set_climate_temperature_offset` service lets you calibrate per-TRV temperature offsets from automations or scripts.
- **Full AC support — all modes, fan speeds, and swing options** — Previously AC zones were limited to basic mode/temperature. All modes (Cool/Heat/Auto/Dry/Fan), all fan speeds, and all swing options are now exposed as climate card controls.
- **Hot water temperature control** — Tank-based hot water zones now expose a water heater entity with temperature and mode control, matching what the Tado app offers.

### Improvements
- **Faster API calls with async architecture** — The integration's API layer has been rewritten using async I/O, eliminating subprocess overhead and improving per-call latency.

## [1.4.1] - 2026-01-23

### Bug Fixes
- **Fixed login broken after upgrading** — Upgrading from 1.3.x to 1.4.0 could leave your login in a broken state because the token storage format changed. The upgrade path now migrates existing tokens instead of invalidating them.

## [1.4.0] - 2026-01-23

### Features
- **In-app login** — New browser-based authentication flow built into the integration setup. No more SSH or terminal access needed to grab tokens — click a link, sign in, and you're done.
- **Home selection for accounts with multiple homes** — If your Tado account manages more than one home, setup now lets you pick which home this integration entry controls. Add the integration again to add another home.

## [1.2.1] - 2026-01-22

### Bug Fixes
- **Fixed a rare startup issue with duplicate hub cleanup** — Under specific upgrade paths, two Hub devices could end up registered for the same integration. Startup cleanup now correctly deduplicates without removing the wrong entity.

## [1.2.0] - 2026-01-21

**Zone-Based Device Organization**

### Features
- **Each zone now appears as its own device in HA** — Previously every sensor and control sat under a flat "Tado CE" device. Each zone is now its own device containing its climate entity, sensors, and switches, matching how HA users expect to browse by room.
- **Optional weather sensors** — Outdoor temperature, solar intensity, and weather state sensors can now be enabled per-installation via Options Flow.
- **Customizable polling intervals** — Options Flow exposes a polling interval setting so you can balance freshness against API quota.

### Improvements
- **60–70% fewer API calls** — The polling strategy now batches zone reads and defers non-essential endpoints to a longer interval, cutting total API calls dramatically for typical multi-zone installs.

## [1.1.0] - 2026-01-19

### Features
- **Away Mode switch** — New switch on the Hub device to toggle Tado's Away mode directly from HA.
- **Home/Away preset mode support** — Climate entities now expose the Home/Away preset modes, so dashboard climate cards include a presence selector.

## [1.0.1] - 2026-01-18

### Bug Fixes
- **Fixed auto-detection of your home ID** — For accounts with multiple homes, the integration sometimes picked the wrong home ID on setup. Home ID detection now matches the first home you logged in with via the Tado app.

## [1.0.0] - 2026-01-17

### Features
- **Initial release** — First public release of Tado CE as a fork of the official Tado integration with community-driven enhancements.
