# Changelog

All notable changes to Tado CE will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.2] - 2026-01-24

### Fixed
- **CRITICAL: Token loss on HACS upgrade** (Issue #34 - @jeverley, @hapklaar, @wrowlands3): Moved data directory from `custom_components/tado_ce/data/` to `/config/.storage/tado_ce/`. HACS upgrades now preserve your credentials and data files. Automatic migration from old location on first startup.

### Changed
- Data files (config.json, zones.json, etc.) now stored in `/config/.storage/tado_ce/` instead of inside the integration folder
- Config entry version updated to 5

### Notes
- **Automatic migration**: Existing data files will be automatically copied to the new location on upgrade
- **No re-authentication required**: Your existing credentials will be preserved
- **Legacy cleanup**: After confirming the integration works, you can optionally delete the old `custom_components/tado_ce/data/` folder

## [1.5.1] - 2026-01-24

### Fixed
- **CRITICAL: OAuth flow "invalid flow specified" error** (Issue #36 - @mkruiver, Discussion #35 - @harryvandervossen): Fixed config flow failing for new users during setup. The `async_show_progress` polling mechanism had race conditions causing authorization to fail even after successful Tado login. Replaced with simpler manual check approach - users now click Submit after authorizing on Tado's website.

### Added
- **Re-authenticate option in UI** (Issue #34 - @jeverley, @hapklaar): Added reconfigure flow to re-authenticate without SSH. Go to Settings → Devices & Services → Tado CE → three dots menu → Reconfigure. No more need to SSH into Home Assistant to fix authentication issues after upgrades.

### Changed
- **Simplified OAuth flow**: Removed automatic polling spinner. Users now:
  1. Click link to authorize on Tado website
  2. Complete authorization (see "Successfully connected")
  3. Return to HA and click Submit
  4. If not yet authorized, shows helpful error message to try again

### Community Credits
- **Issue #34**: @jeverley, @hapklaar (re-authenticate UI request)
- **Issue #36**: @mkruiver (OAuth flow bug report)
- **Discussion #35**: @harryvandervossen (detailed OAuth flow feedback)

## [1.5.0] - 2026-01-24

> **🚀 Major Code Quality Release**: This release represents a near-complete rewrite of the codebase with async architecture, comprehensive null-safe patterns, and centralized data loading. The focus is on stability, maintainability, and future-proofing.

### Highlights

- **Async Architecture**: Complete migration from blocking urllib to async aiohttp for all entity control methods
- **Null-Safe Patterns**: Comprehensive audit and fix of all API data parsing to handle null values gracefully
- **Centralized Data Loading**: New `data_loader.py` module eliminates code duplication across entity files
- **235 Tests Passing**: Full test coverage for all new features and fixes

### Added
- **`tado_ce.get_temperature_offset` service**: On-demand fetch of current temperature offset for automations. Returns offset value via service response for use in templates (Issue #24 - @pisolofin).
- **Optional `offset_celsius` attribute**: Enable in integration options to add temperature offset attribute to climate entities. Synced during full sync (every 6 hours). Uses 1 API call per device (Issue #25 - @ohipe).
- **Frequent mobile device sync option**: New toggle "Sync Mobile Devices Frequently" - when enabled, mobile devices sync every quick sync instead of every 6 hours. Useful for presence-based automations (Issue #28 - @beltrao).
- **New `data_loader.py` module**: Centralized file loading helpers used by `sensor.py`, `climate.py`, `water_heater.py`. Eliminates duplicate code and ensures consistent error handling.

### Changed
- **HVAC mode logic**: Now matches official Tado integration behavior. Shows `auto` when following schedule (even if scheduled OFF). This helps distinguish "OFF by schedule" vs "OFF by manual override" (Issue #25 - @ohipe).
- **Async API architecture**: Migrated entity control methods from blocking urllib to async aiohttp. Climate, water heater, and switch entities now use non-blocking API calls, improving Home Assistant responsiveness and eliminating potential event loop blocking warnings (Issue #27).
- **Improved unload cleanup**: `async_unload_entry()` now properly cleans up polling timer, async client, immediate refresh handler, and hass.data to prevent memory leaks on reload.

### Fixed
- **Blocking I/O warning for manifest.json**: Version is now loaded at module import time instead of during entity creation, eliminating the blocking I/O warning in Home Assistant logs (Issue #27).
- **Memory leak on integration reload**: Fixed global `_async_clients` dictionary not being cleaned up when integration is reloaded or removed.
- **Token refresh race condition**: Moved all token validity checks inside the async lock to prevent multiple coroutines from triggering duplicate token refreshes simultaneously.
- **Sync/async deadlock risk in water_heater**: Removed dangerous sync wrapper methods that could block the event loop.
- **Subprocess zombie process on timeout**: Fixed `_execute_quick_sync()` to properly consume stdout/stderr after killing timed-out process.
- **Hardcoded paths**: Replaced all hardcoded `/config/custom_components/...` paths with dynamic paths using `Path(__file__).parent` and environment variable support (`TADO_CE_CONFIG_DIR`).
- **HOT_WATER zone sensors showing "unknown"**: Temperature and humidity sensors for zones without sensor data (e.g., combi boiler hot water zones) now correctly show "unavailable" instead of "unknown". Existing entities will show unavailable; new installations won't create these sensors.
- **Null value crash in water_heater and climate entities** (Issue #26 - @hapklaar): Fixed `'NoneType' object has no attribute 'get'` error when API returns `temperature: null` (e.g., HOT_WATER zones with power OFF). Now safely handles null values in `setting.temperature`, `bearingFromHome`, and `connectionState` fields.

### Added (AC Capabilities)
- **Full AC mode support** (Issue #31 - @neonsp): AC zones now properly show DRY and FAN modes when supported by your AC unit. Previously only showed Cool/Heat/Auto.
- **AC fan level support**: Fan modes now correctly map Tado's SILENT/LEVEL1-5/AUTO to Home Assistant's Low/Medium/High/Auto.
- **AC swing support**: Swing modes (vertical/horizontal) now available when supported by your AC unit.
- **AC temperature range from API**: Min/max temperature and step size now read from actual AC capabilities instead of hardcoded values.

### Added (Hot Water)
- **Hot water temperature control** (Discussion #21 - @wyx087): Hot water zones with temperature support (e.g., hot water tanks) now show target temperature and allow temperature adjustment. Auto-detected based on API response.
- **Hot water power sensor**: New sensor showing ON/OFF status for hot water zones.

### Code Quality (Internal)

This release includes a comprehensive engineering audit with focus on stability and maintainability:

- **Null-Safe Patterns**: All `.get('key', {})` patterns replaced with `(data.get('key') or {})` pattern across 7 files to handle API null values correctly
- **Centralized Data Loading**: New `data_loader.py` module with `load_zones_file()`, `load_zones_info_file()`, `load_config_file()`, `load_weather_file()`, `get_zone_names()`, `get_zone_types()`, `get_zone_data()` helpers
- **Consistent Error Handling**: All file loading operations now use centralized helpers with proper exception handling
- **Files Refactored**: `sensor.py`, `climate.py`, `water_heater.py`, `binary_sensor.py`, `switch.py`, `device_tracker.py`, `data_loader.py`

### Technical
- New `async_api.py` module with `TadoAsyncClient` class using aiohttp
- Climate entities: `async_set_temperature`, `async_set_hvac_mode`, `async_set_preset_mode`, `async_set_timer`
- Water heater entities: `async_set_operation_mode`, `async_set_timer`
- Switch entities: `async_turn_on`, `async_turn_off` for Away Mode, Early Start, Child Lock
- Automatic token refresh with caching (5-minute cache duration)
- Rate limit header parsing for future quota management
- New `cleanup_async_client()` function in `async_api.py`
- New `cleanup_handler()` function in `immediate_refresh_handler.py`
- `const.py` now supports `TADO_CE_CONFIG_DIR` environment variable for testing/development
- Added explicit `aiohttp>=3.8.0` requirement in manifest.json

### Community Credits
- **Issue #24**: @pisolofin (get_temperature_offset service request)
- **Issue #25**: @ohipe (offset attribute and HVAC mode behavior)
- **Issue #26**: @hapklaar (null value crash in water_heater)
- **Issue #27**: (blocking I/O warning fix, async migration)
- **Issue #28**: @beltrao (frequent mobile device sync)
- **Issue #31**: @neonsp (AC modes, fan levels, swing support)
- **Discussion #21**: @wyx087 (hot water temperature control discovery)

## [1.4.1] - 2026-01-23

### Fixed
- **CRITICAL: Authentication broken after upgrade from v1.2.x** (Issue #26): Fixed missing migration path from config entry VERSION 2/3 to VERSION 4. Users upgrading from v1.2.x had entities become unavailable because the migration code only handled VERSION 1→2. Now properly migrates all versions and preserves existing refresh tokens.

### Community Credits
- **Issue #26**: @hapklaar, @mjsarfatti (authentication issue after upgrade)

## [1.4.0] - 2026-01-23

### Added
- **New Device Authorization setup flow**: Setup now happens entirely in Home Assistant UI. No more SSH required to run `tado_api.py auth` manually. Users authorize via Tado's OAuth2 device flow with a link and code.
- **Home selection during setup**: Accounts with multiple homes can now select which home to configure.

### Changed
- **Weather sensors default to OFF**: New installations will have weather sensors disabled by default (saves 1 API call per sync). Existing users' settings are preserved.
- **Mobile device tracking default to OFF**: New installations will have mobile device tracking disabled by default (saves 1 API call per full sync). Existing users' settings are preserved.

### Fixed
- **Logging levels cleanup**: Changed setup messages from `warning` to `debug`/`info` for cleaner logs. Normal operations no longer flood logs with warnings.
- **Options not saving properly**: Fixed inconsistent default values between config_flow.py and config_manager.py causing weather/mobile checkboxes to revert after saving.
- **Day/Night Start Hour checkboxes**: Changed from `vol.Optional` to `vol.Required` to remove confusing enable/disable checkboxes that caused values to not save (Issue #17).
- **Uniform polling mode**: Setting Day Start Hour = Night Start Hour now enables uniform 24/7 polling using day interval (Issue #17).
- **Boiler Flow Temperature sensor**: Now auto-detects if your system has boiler flow temperature data. Sensor only appears if you have OpenTherm connection between Tado and boiler. Moved to Hub device with `source_zone` attribute (Issue #15).
- **Climate preset mode stuck on Away**: Fixed bug where preset mode used mobile device location data instead of actual Tado home state. Now correctly reflects Home/Away regardless of geo-tracking settings (Issue #22).

### Enhanced
- **API Reset sensor improvements**: 
  - Now uses Tado API's actual reset time from response headers instead of calculating from call history
  - Added `next_poll` and `current_interval_minutes` attributes to show when the next API poll will occur
  - Improved initial reset time estimation
- **API Usage sensor cleanup**: Removed `reset_human` and `reset_at` attributes (now in API Reset sensor where they belong)

### Community Credits
- **Issue #16**: @ChrisMarriott38 (API Reset time confusion after re-authentication)
- **Issue #17**: @ChrisMarriott38 (Options UI fixes, uniform polling mode suggestion)
- **Issue #15**: @ChrisMarriott38 (Boiler Flow Temperature sensor fixes)
- **Issue #22**: @jeverley (Climate preset mode stuck on Away)

## [1.2.1] - 2026-01-22

### Fixed
- **CRITICAL: Duplicate hub cleanup race condition** (Issue #10): Fixed race condition where duplicate entries from v1.1.0 upgrade couldn't be deleted. Changed from non-blocking (`async_create_task`) to blocking removal (`await`) to ensure old entries are fully removed before new one continues setup.
- **Multi-device zone entity naming** (Issue #11): Fixed confusing entity names for zones with multiple devices (e.g., 1 sensor + 2 valves). Entity names now include device type + index suffix when needed (e.g., "Living Room VA02 (1) Battery", "Living Room VA02 (2) Battery", "Living Room RU01 Battery").
- **Migration handling**: Enhanced `async_migrate_entry` to gracefully handle missing `zones_info.json` file during upgrade.
- **Duplicate prevention**: Added unique ID check in config flow to prevent duplicate integration entries.

### Changed
- **Improved duplicate cleanup**: Now uses blocking removal to prevent race conditions during upgrade.
- **Smart entity naming**: Single-device zones keep simple names, multi-device zones get device type + index suffixes for clarity.
- **README updates**: Added comprehensive troubleshooting section for upgrade issues.

### Notes
- **Automatic cleanup**: Upgrading from v1.1.0 will automatically remove duplicate entries - no manual action needed.
- **Entity IDs preserved**: Entity IDs remain unchanged - automations will continue to work.
- **Multi-device zones**: Battery, connection, and child lock entities now have clearer names with device type + index.

### Community Credits
- **Issue #10**: @marcovn, @ChrisMarriott38, @hapklaar (duplicate hub issue)
- **Issue #11**: @marcovn (multi-device naming feedback and testing)


## [1.2.0] - 2026-01-21

> **Development Note**: This release underwent comprehensive engineering audit. All 7 critical fixes verified.

### Breaking Changes
- **Zone-based device organization**: Each Tado zone now appears as a separate device. Zone entities are assigned to their respective zone devices, while hub entities remain on the Tado CE Hub device. Entity IDs are preserved - automations will continue to work.
- **Improved entity naming**: Zone entities no longer have "Tado CE" prefix (e.g., "Living Room" instead of "Tado CE Living Room"). Hub entities retain "Tado CE" prefix for clarity.

### Added
- **Centralized authentication manager** (`auth_manager.py`): Thread-safe token management with 300s caching, eliminates duplicate refresh calls
- **Centralized error handler** (`error_handler.py`): Consistent error handling with automatic retry, entity availability management, and graceful degradation
- **API call tracking for home state**: Added `CALL_TYPE_HOME_STATE = 7` to track `/api/v2/homes/{id}/state` calls
- **Optional weather sensors**: Toggle weather sensors on/off in integration options (disabled by default for new installations, saves 1 API call per sync)
- **Customizable polling intervals**: Configure custom day/night hours and polling intervals in integration options
- **Enhanced immediate refresh**: Quota-aware triggering with exponential backoff (10s → 20s → 40s → 80s → 160s → 300s)
- **Device manager** (`device_manager.py`): Manages zone-based device organization
- **Config manager** (`config_manager.py`): Centralized configuration management with validation

### Fixed
- **CRITICAL: Token refresh race condition**: Multiple concurrent API calls could trigger duplicate token refreshes. Fixed with centralized `AuthManager` using thread-safe locking and token caching.
- **CRITICAL: Thread safety in immediate refresh**: Fixed `async_create_task` being called from synchronous methods in `climate.py`, `water_heater.py`, and `switch.py`. Now uses `call_soon_threadsafe` for proper async task scheduling.
- **HIGH: Incomplete API call tracking**: Home state API calls were not being tracked. Now all API calls are properly tracked for accurate quota monitoring.
- **HIGH: Immediate refresh without rate limiting**: Rapid user actions could exhaust API quota. Fixed with quota checking, increased minimum interval (5s → 10s), and exponential backoff.
- **MEDIUM: Rate limit reset time calculation**: Buggy modulo logic caused incorrect reset time display. Implemented three-strategy approach (API-provided → calculated from last reset → estimated from history).
- **MEDIUM: Database performance**: API limit sensor stored 20KB of data exceeding Home Assistant's 16KB limit. Optimized to store only last 100 calls (~10KB, 53% reduction).

### Changed
- **API usage optimization**: Normal polling now uses 1-2 API calls (quick sync) instead of 4. Full sync only every 6 hours. Estimated 60-70% reduction in API calls.
- **Weather sensors default**: Disabled by default for new installations to save API calls. Existing users' settings preserved.
- **Immediate refresh interval**: Increased minimum interval from 5s to 10s to prevent quota exhaustion.
- **Error handling**: All API calls now use centralized error handler with automatic retry and entity availability management.

### Contributors
- **[@wrowlands3](https://github.com/wrowlands3)**: Zone-based devices, improved entity naming
- **[@ChrisMarriott38](https://github.com/ChrisMarriott38)**: Optional weather sensors, API call tracking, customizable polling, boiler flow temperature sensor
- **[@StreborStrebor](https://github.com/StreborStrebor)**: Immediate refresh feature, AC controls feedback
- **[@donnie-darko](https://github.com/donnie-darko)**: Hot water service compatibility, timer features

## [1.1.0] - 2026-01-19

### Added
- **Away Mode switch**: New `switch.tado_ce_away_mode` to manually toggle Home/Away status (1 API call per toggle) (Issue #3)
- **Preset mode support**: Climate entities now support Home/Away presets (1 API call per change)
- **Humidity on climate**: Climate entities now show `current_humidity` attribute (no extra API calls - uses existing data)

### Changed
- **Device organization**: All entities (climate, switches, sensors) are now linked to the Tado CE Hub device for better organization in Home Assistant
- Updated all entity `device_info` to reference the Hub device

### API Usage Notes
- Away Mode switch: 1 API call per toggle
- Preset mode change: 1 API call per change
- Humidity attribute: No additional API calls (uses existing zone data)

### Community Credits
- **Issue #2**: @hapklaar (Humidity attribute and preset mode suggestion)
- **Issue #3**: @MJWMJW2 (Away Mode switch request)

## [1.0.1] - 2026-01-18

### Fixed
- **Auto-fetch home ID**: The integration now automatically fetches your home ID from your Tado account using the `/me` endpoint instead of using a hardcoded value
- **403 "user is not a resident" error**: New users no longer encounter this error during setup

### Changed
- Removed hardcoded `DEFAULT_HOME_ID` constant
- Home ID is now automatically discovered and saved to config on first API call

### Notes
- If upgrading from 1.0.0, delete `/config/custom_components/tado_ce/data/config.json` and re-authenticate

## [1.0.0] - 2026-01-17

### Added
- Initial release
- Climate control for heating zones (Heat/Off/Auto modes)
- AC control with full mode support (Cool/Heat/Dry/Fan, fan speed, swing)
- Hot water control with timer support
- Real-time API rate limit tracking from Tado response headers
- Dynamic limit detection (100/5000/20000 calls)
- Rolling 24h reset time tracking
- Smart day/night polling intervals
- Open window detection
- Home/Away geofencing support
- Mobile device presence tracking
- Weather data (outside temperature, solar intensity, conditions)
- Child lock switch
- Early start switch
- Temperature offset calibration
- Device identify (LED flash)
- Energy IQ meter readings
- Device connection state monitoring
- OAuth2 device authorization flow
- Rotating refresh token handling
- Persistent token storage
