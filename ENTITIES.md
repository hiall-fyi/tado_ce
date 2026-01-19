# Tado CE Entities Reference

Complete list of all entities created by Tado CE integration.

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

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.tado_ce_outside_temperature` | Temperature | Outside temperature at your location |
| `sensor.tado_ce_solar_intensity` | Percentage | Solar intensity (0-100%) |
| `sensor.tado_ce_weather_state` | State | Current weather condition |

## Home/Away

| Entity | Type | Description | API Calls |
|--------|------|-------------|-----------|
| `binary_sensor.tado_ce_home` | Binary Sensor | Home/Away status (read-only, from geofencing) | 0 |
| `switch.tado_ce_away_mode` | Switch | Toggle Home/Away manually | 1 per toggle |

## Per Zone - Climate

For each heating zone (e.g. "Lounge"), you get:

| Entity | Type | Description | API Calls |
|--------|------|-------------|-----------|
| `climate.tado_ce_{zone}` | Climate | Full climate control | 1 per action |

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

### Climate Preset Modes

| Preset | Description | API Calls |
|--------|-------------|-----------|
| `home` | Set presence to Home | 1 |
| `away` | Set presence to Away | 1 |

## Per Zone - Sensors

For each zone, you get these sensors:

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.tado_ce_{zone}_temperature` | Temperature | Current temperature |
| `sensor.tado_ce_{zone}_humidity` | Percentage | Current humidity |
| `sensor.tado_ce_{zone}_heating` | Percentage | Heating power (0-100%) |
| `sensor.tado_ce_{zone}_target` | Temperature | Target temperature |
| `sensor.tado_ce_{zone}_mode` | State | Mode (Manual/Schedule/Off) |
| `sensor.tado_ce_{zone}_battery` | State | Battery status (NORMAL/LOW) |
| `sensor.tado_ce_{zone}_connection` | State | Connection (Online/Offline) |

## Per Zone - Binary Sensors

| Entity | Type | Description |
|--------|------|-------------|
| `binary_sensor.tado_ce_{zone}_open_window` | Binary Sensor | Open window detected |

## Per Zone - Switches

| Entity | Type | Description | API Calls |
|--------|------|-------------|-----------|
| `switch.tado_ce_{zone}_early_start` | Switch | Smart pre-heating | 1 per toggle |
| `switch.tado_ce_{zone}_child_lock` | Switch | Child lock on device | 1 per toggle |

## Hot Water

If you have hot water control:

| Entity | Type | Description | API Calls |
|--------|------|-------------|-----------|
| `water_heater.tado_ce_{zone}` | Water Heater | Hot water control | 1 per action |

## Device Trackers

For each mobile device with geo tracking enabled:

| Entity | Type | Description |
|--------|------|-------------|
| `device_tracker.tado_ce_{device}` | Device Tracker | Presence (home/not_home) |

## AC Zones

For air conditioning zones, climate entities support additional features:

| Feature | Description |
|---------|-------------|
| `hvac_modes` | off/auto/cool/heat/dry/fan_only |
| `fan_mode` | auto/low/medium/high |
| `swing_mode` | on/off |

---

## API Usage Summary

| Action | API Calls |
|--------|-----------|
| Regular polling (day) | 2 per sync |
| Regular polling (night) | 2 per sync |
| Full sync (every 6h) | 4 per sync |
| Set temperature | 1 |
| Change HVAC mode | 1 |
| Toggle Away Mode | 1 |
| Change Preset | 1 |
| Toggle Early Start | 1 |
| Toggle Child Lock | 1 |
| Set Hot Water | 1 |

All read operations use cached data from the last sync - no additional API calls.
