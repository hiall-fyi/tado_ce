# Tado CE Entities Reference

Complete list of all entities created by Tado CE integration.

## 📋 v1.5.0 Changes

### Climate Entity Enhancements
- **Optional `offset_celsius` attribute**: Enable in options to show temperature offset on climate entities
- **Full AC support**: DRY/FAN modes, fan levels (Low/Medium/High/Auto), swing modes now properly supported

### Hot Water Temperature Control
- **Auto-detected**: If your hot water zone supports temperature (e.g., hot water tanks), you can now see and set target temperature
- **Works with V2 systems**: Verified working with Tado V2 thermostats

### New Sensors
- **Power sensor** (Hot water zones): Shows ON/OFF status

### New Service
- **`tado_ce.get_temperature_offset`**: Fetch current offset for use in automations

### Mobile Device Sync
- **Frequent sync option**: Enable to sync mobile devices every quick sync (for presence automations)

---

## 📋 v1.4.0 Changes

### Boiler Flow Temperature Sensor
- **Auto-detection**: Sensor only created if OpenTherm/eBUS data is available
- **Moved to Hub device**: Now a Hub-level sensor (was incorrectly zone-level)
- **New attribute**: `source_zone` shows which zone the data comes from
- **No more "unavailable"**: Users without OpenTherm won't see this sensor

### Climate Preset Mode Fix
- Preset mode now correctly reflects Tado's actual home/away state
- Works regardless of mobile device geo-tracking settings

---

## 📋 v1.2.0 Changes

### Device Organization
- **Zone-based devices**: Each zone now appears as a separate device
- **Zone entities**: Assigned to their respective zone devices
- **Hub entities**: Remain on the Tado CE Hub device
- **Entity IDs**: Preserved - automations continue to work

### Entity Naming
- **Zone entities**: No "Tado CE" prefix (e.g., "Living Room" instead of "Tado CE Living Room")
- **Hub entities**: Retain "Tado CE" prefix for clarity

---

## Hub Buttons (v1.5.3)

| Entity | Type | Description | API Calls |
|--------|------|-------------|-----------|
| `button.tado_ce_resume_all_schedules` | Button | Delete all zone overlays and return to schedules | 1 per zone |

---

## Hub Sensors

Global sensors for the Tado CE Hub device.

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.tado_ce_home_id` | Diagnostic | Your Tado home ID |
| `sensor.tado_ce_api_usage` | Sensor | API calls used (e.g. "142/5000") |
| `sensor.tado_ce_api_reset` | Sensor | Time until rate limit resets (e.g. "5h 30m") |
| `sensor.tado_ce_api_limit` | Diagnostic | Your daily API call limit |
| `sensor.tado_ce_api_status` | Diagnostic | API status (ok/warning/rate_limited) |
| `sensor.tado_ce_token_status` | Diagnostic | Token status (valid/expired) |
| `sensor.tado_ce_zones_count` | Diagnostic | Number of zones configured |
| `sensor.tado_ce_last_sync` | Diagnostic | Last successful sync timestamp |

## Weather Sensors

**Note:** Weather sensors are **disabled by default** in v1.2.0 to save API calls. Enable in integration options if needed.

| Entity | Type | Description | API Calls |
|--------|------|-------------|-----------|
| `sensor.tado_ce_outside_temperature` | Temperature | Outside temperature at your location | 1 per sync (when enabled) |
| `sensor.tado_ce_solar_intensity` | Percentage | Solar intensity (0-100%) | Included in weather call |
| `sensor.tado_ce_weather_state` | State | Current weather condition | Included in weather call |

## Home/Away

| Entity | Type | Description | API Calls |
|--------|------|-------------|-----------|
| `binary_sensor.tado_ce_home` | Binary Sensor | Home/Away status (read-only, from geofencing) | 0 |
| `switch.tado_ce_away_mode` | Switch | Toggle Home/Away manually | 1 per toggle |

## Per Zone - Climate

**Device Organization (v1.2.0):** Each zone has its own device. Zone entities are assigned to their zone device.

For each heating zone (e.g. "Lounge"), you get:

| Entity | Type | Description | API Calls |
|--------|------|-------------|-----------|
| `climate.{zone}` | Climate | Full climate control | 1 per action |

**Note:** Entity naming changed in v1.2.0 - no "tado_ce_" prefix for zone entities.

### Climate Entity Attributes

| Attribute | Description |
|-----------|-------------|
| `current_temperature` | Current room temperature |
| `current_humidity` | Current room humidity |
| `target_temperature` | Target temperature |
| `hvac_mode` | Current mode (heat/off/auto) |
| `hvac_action` | Current action (heating/idle/off) |
| `preset_mode` | Home/Away preset |
| `overlay_type` | Manual/Schedule/Timer |
| `heating_power` | Heating demand (0-100%) |
| `zone_id` | Tado zone ID |
| `offset_celsius` | Temperature offset (v1.5.0, optional - enable in options) |

### Climate Preset Modes

| Preset | Description | API Calls |
|--------|-------------|-----------|
| `home` | Set presence to Home | 1 |
| `away` | Set presence to Away | 1 |

## Per Zone - Sensors

For each zone, you get these sensors:

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.{zone}_temperature` | Temperature | Current temperature |
| `sensor.{zone}_humidity` | Percentage | Current humidity |
| `sensor.{zone}_heating` | Percentage | Heating power (0-100%) |
| `sensor.{zone}_power` | State | Power state (ON/OFF) (v1.5.0) |
| `sensor.{zone}_target` | Temperature | Target temperature |
| `sensor.{zone}_mode` | State | Mode (Manual/Schedule/Off) |
| `sensor.{zone}_battery` | State | Battery status (NORMAL/LOW) |
| `sensor.{zone}_connection` | State | Connection (Online/Offline) |

**Note:** Entity naming changed in v1.2.0 - no "tado_ce_" prefix for zone entities.

## Per Zone - Binary Sensors

| Entity | Type | Description |
|--------|------|-------------|
| `binary_sensor.{zone}_open_window` | Binary Sensor | Open window detected |

**Note:** Entity naming changed in v1.2.0 - no "tado_ce_" prefix for zone entities.

## Per Zone - Switches

| Entity | Type | Description | API Calls |
|--------|------|-------------|-----------|
| `switch.{zone}_early_start` | Switch | Smart pre-heating | 1 per toggle |
| `switch.{zone}_child_lock` | Switch | Child lock on device | 1 per toggle |

**Note:** Entity naming changed in v1.2.0 - no "tado_ce_" prefix for zone entities.

## Hot Water

If you have hot water control:

| Entity | Type | Description | API Calls |
|--------|------|-------------|-----------|
| `water_heater.{zone}` | Water Heater | Hot water control (with temperature if supported) | 1 per action |
| `sensor.{zone}_mode` | Sensor | Mode (Manual/Schedule/Off) | 0 |
| `sensor.{zone}_power` | Sensor | Power state (ON/OFF) (v1.5.0) | 0 |

**Note:** Entity naming changed in v1.2.0 - no "tado_ce_" prefix for zone entities.

### Hot Water Temperature Control (v1.5.0)

If your hot water zone supports temperature (e.g., hot water tanks with V2 thermostats), the water heater entity will show and allow setting target temperature. This is auto-detected from the API response.

### Hot Water Operation Modes (v1.2.0)

| Mode | Description |
|------|-------------|
| `auto` | Follow Tado schedule |
| `heat` | Manual override (on until cancelled or timer expires) |
| `off` | Completely off |

### Hot Water Timer Buttons (v1.2.0)

Quick-access timer buttons for hot water boost:

| Entity | Type | Description | API Calls |
|--------|------|-------------|-----------|
| `button.{zone}_timer_30min` | Button | Turn on hot water for 30 minutes | 1 per press |
| `button.{zone}_timer_60min` | Button | Turn on hot water for 60 minutes | 1 per press |
| `button.{zone}_timer_90min` | Button | Turn on hot water for 90 minutes | 1 per press |

## Boiler Flow Temperature (v1.4.0)

**Auto-detected** - Only appears if your system has OpenTherm/eBUS connection between Tado and boiler.

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.tado_ce_boiler_flow_temperature` | Temperature | Real-time boiler flow temperature |

**Attributes:**
- `source_zone`: The zone providing the boiler data

**Note:** This is a Hub-level sensor. If you don't have OpenTherm, this sensor won't be created.

---

## Device Trackers

For each mobile device with geo tracking enabled:

| Entity | Type | Description |
|--------|------|-------------|
| `device_tracker.tado_ce_{device}` | Device Tracker | Presence (home/not_home) |

## AC Zones (v1.5.0 Enhanced)

For air conditioning zones, climate entities support additional features:

| Feature | Description |
|---------|-------------|
| `hvac_modes` | off/auto/cool/heat/dry/fan_only |
| `fan_mode` | auto/low/medium/high (mapped from Tado's SILENT/LEVEL1-5/AUTO) |
| `swing_mode` | on/off/vertical/horizontal (when supported by AC unit) |
| `min_temp` / `max_temp` | Read from AC capabilities API |
| `target_temp_step` | Read from AC capabilities API |

**Note (v1.5.0):** AC capabilities are now fetched from Tado API instead of hardcoded values.

---

## API Usage Summary

**v1.2.0 Optimizations:**
- Normal polling: 1-2 calls (quick sync) instead of 4
- Full sync: Every 6 hours only (4 calls)
- Weather: Optional (disabled by default, saves 1 call per sync)
- Immediate refresh: Quota-aware with exponential backoff
- **Estimated savings: 60-70% reduction in API calls**

| Action | API Calls |
|--------|-----------|
| Quick sync (normal) | 1-2 per sync |
| Full sync (every 6h) | 4 per sync |
| Weather (if enabled) | 1 per sync |
| Set temperature | 1 |
| Change HVAC mode | 1 |
| Toggle Away Mode | 1 |
| Change Preset | 1 |
| Toggle Early Start | 1 |
| Toggle Child Lock | 1 |
| Set Hot Water | 1 |
| Hot Water Timer Button | 1 |

All read operations use cached data from the last sync - no additional API calls.

---

## 🆕 v1.2.0 New Features

### Hot Water Timer Buttons

Quick-access timer buttons for hot water boost:

| Entity | Type | Description | API Calls |
|--------|------|-------------|-----------|
| `button.{zone}_timer_30min` | Button | Turn on hot water for 30 minutes | 1 per press |
| `button.{zone}_timer_60min` | Button | Turn on hot water for 60 minutes | 1 per press |
| `button.{zone}_timer_90min` | Button | Turn on hot water for 90 minutes | 1 per press |

### Enhanced API Monitoring

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.tado_ce_api_usage` | Sensor | Shows detailed call history (last 100 calls) |
| `sensor.tado_ce_api_reset` | Sensor | Exact reset timestamp in local timezone |

### Customizable Polling

Configure in integration options:
- Custom day/night hours (default: 7am-11pm day, 11pm-7am night)
- Custom polling intervals
- Quota warnings when intervals would exceed limits

### Optional Weather Sensors

Toggle weather sensors on/off in integration options:
- **Default: Disabled** for new installations
- Saves 1 API call per sync when disabled
- Enable if you need weather data

---

## 📝 Entity Naming Changes (v1.2.0)

### Zone Entities (No Prefix)
- Before: `climate.tado_ce_living_room`
- After: `climate.living_room`

### Hub Entities (Keep Prefix)
- `sensor.tado_ce_api_usage`
- `sensor.tado_ce_api_reset`
- `switch.tado_ce_away_mode`
- `sensor.tado_ce_outside_temperature` (if enabled)

**Note:** Entity IDs are preserved during upgrade - automations continue to work.
