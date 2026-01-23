# Changelog

All notable changes to Tado CE will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
