# Changelog

All notable changes to Tado CE will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-01-19

### Added
- **Away Mode switch**: New `switch.tado_ce_away_mode` to manually toggle Home/Away status (1 API call per toggle)
- **Preset mode support**: Climate entities now support Home/Away presets (1 API call per change)
- **Humidity on climate**: Climate entities now show `current_humidity` attribute (no extra API calls - uses existing data)

### Changed
- **Device organization**: All entities (climate, switches, sensors) are now linked to the Tado CE Hub device for better organization in Home Assistant
- Updated all entity `device_info` to reference the Hub device

### API Usage Notes
- Away Mode switch: 1 API call per toggle
- Preset mode change: 1 API call per change
- Humidity attribute: No additional API calls (uses existing zone data)

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
