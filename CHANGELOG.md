# Changelog

All notable changes to Tado CE will be documented in this file.

## [1.8.1] - 2026-01-26

**Hotfix: AC Optimistic Updates** - Fixed optimistic state updates for AC zones.

- **Fixed AC optimistic updates not working** - AC zones now have the same optimistic update protection as heating zones ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @neonsp)
- AC state changes (temperature, mode, fan, swing) now update immediately without bouncing back

## [1.8.0] - 2026-01-26

**Multi-Home Data Migration + Schedule Calendar** - Per-home data files and heating schedule visualization.

- **NEW: Schedule Calendar** - Per-zone calendar entities showing heating schedules from Tado app (opt-in in Options)
- **NEW: Per-zone Refresh Schedule button** - Refresh individual zone schedules on demand
- **NEW: API Reset sensor attributes** - Added `reset_at` and `last_reset` attributes showing actual times ([#54](https://github.com/hiall-fyi/tado_ce/issues/54) - @ChrisMarriott38)
- **Multi-home prep: Per-home data files** - Data files now use `{filename}_{home_id}.json` format
- **Auto-migration** - Existing files automatically renamed with home_id suffix
- **Schedules cached locally** - Fetched once on startup, stored in `schedules.json`
- **Changed Home State Sync default to OFF** - Consistent with Weather/Mobile Devices defaults to save API calls ([#55](https://github.com/hiall-fyi/tado_ce/issues/55) - @ChrisMarriott38)

## [1.7.0] - 2026-01-26

**Multi-Home Preparation** - Foundation for future multi-home support with UX improvements.

- **NEW: Optimistic state updates** - Immediate UI feedback when changing modes/temperature, with rollback on API failure
- **NEW: Optional homeState sync** - Disable home/away state sync to save 1 API call per quick sync (for users not using Tado geofencing)
- **Multi-home prep: unique_id migration** - Integration unique_id changed from `tado_ce_integration` to `tado_ce_{home_id}`
- **Auto-migration** - Existing entries automatically updated, no user action needed
- **Fixed options float validation** - HA NumberSelector returns float, config_manager now converts to int properly

## [1.6.3] - 2026-01-25

**Accurate API Reset Time Detection** - Uses Home Assistant sensor history for precise reset time.

- **NEW: HA History Detection** - Detects API reset time from `sensor.tado_ce_api_usage` history by finding when usage drops (e.g., 406 → 2)
- **More accurate reset time** - No longer relies on extrapolation or Tado's incorrect `t=` header
- **Works after HA reboots** - Uses recorded sensor history, not just call tracking

## [1.6.2] - 2026-01-25

**Timezone Fixes & API Call Tracking** - Comprehensive timezone handling and async-safe file I/O.

- **Fixed API call history not recording** - `async_api.py` was missing call tracking (v1.6.0 regression)
- **Fixed `recent_calls` not showing local timezone** - API Limit sensor now converts timestamps correctly
- **Fixed `call_history` timezone** - API Usage sensor timestamps now display in local timezone
- **Fixed API call recording** - All timestamps now stored in UTC consistently
- **Fixed meter reading date** - Now uses Home Assistant's configured timezone
- **Fixed 24h call count calculation** - Now uses UTC for accurate counting
- **Fixed blocking I/O warnings** - `api_call_tracker.py` now uses async file I/O via `run_in_executor`
- **Fixed `get_call_history` bug** - Naive vs aware datetime comparison was silently failing
- **Fixed rate limit file read** - `save_ratelimit()` now loads previous data asynchronously
- **Fixed thread leak on integration reload** - Added `cleanup_executor()` to properly shutdown ThreadPoolExecutor

## [1.6.1] - 2026-01-25

**Hotfix Release** - Fixes critical v1.6.0 regression affecting all users.

- **Fixed API Usage/Reset sensors showing 0** - Rate limit header parsing was case-sensitive, now fixed
- Fixed timezone awareness for Day/Night polling hours
- Added configurable refresh debounce delay (1-60 seconds, default 15)
- Improved Options UI with collapsible sections (Features, Polling Schedule, Advanced)

## [1.6.0] - 2026-01-25

- Deprecated `tado_api.py` - sync now uses native async API (faster, no subprocess overhead)
- Removed subprocess dependency for polling (cleaner architecture)
- Fixed cumulative migration bug - users upgrading across multiple versions now run ALL migrations correctly
- Fixed blocking I/O warning in `get_polling_interval` (async-safe ratelimit loading)
- Fixed `climate.set_temperature` ignoring `hvac_mode` parameter (Node-RED/automation compatibility)
- Fixed climate entities not updating consistently when changing multiple zones
- Fixed Resume All Schedules button not refreshing dashboard
- Added debounced refresh mechanism for batch updates (multiple zone changes = 1 API call)

## [1.5.5] - 2026-01-24

- Fixed AC Auto mode turning off AC (removed confusing AUTO option, use Heat/Cool instead)
- Reduced API calls per state change from 3 to 2 (optimized immediate refresh)

## [1.5.4] - 2026-01-24

- Fixed all AC control issues (modes, fan, swing, status display)
- Added unified swing dropdown (off/vertical/horizontal/both)
- Fixed AC Power sensor showing 0%
- Improved entity ID stability when renaming zones

## [1.5.3] - 2026-01-24

- Added Resume All Schedules button
- Fixed AC control 422 errors
- Fixed blocking I/O warning in config flow

## [1.5.2] - 2026-01-24

- Fixed token loss on HACS upgrade (moved data to safe location)

## [1.5.1] - 2026-01-24

- Fixed OAuth flow errors for new users
- Added re-authenticate option in UI (no SSH needed)

## [1.5.0] - 2026-01-24

Major code quality release with async architecture rewrite.

- Migrated to async API calls (faster, no blocking)
- Added temperature offset service and attribute
- Added frequent mobile device sync option
- Fixed null value crashes
- Full AC mode/fan/swing support
- Hot water temperature control

## [1.4.1] - 2026-01-23

- Fixed authentication broken after upgrade from v1.2.x

## [1.4.0] - 2026-01-23

- New in-app OAuth setup (no SSH required)
- Home selection for multi-home accounts
- Weather/mobile tracking off by default (saves API calls)
- Fixed various options UI issues

## [1.2.1] - 2026-01-22

- Fixed duplicate hub cleanup race condition
- Fixed multi-device zone entity naming

## [1.2.0] - 2026-01-21

Major stability release with zone-based device organization.

- Each zone now appears as separate device
- Centralized auth manager (fixes token race conditions)
- Optional weather sensors
- Customizable polling intervals
- 60-70% reduction in API calls

## [1.1.0] - 2026-01-19

- Added Away Mode switch
- Added preset mode support (Home/Away)
- Added humidity attribute to climate entities

## [1.0.1] - 2026-01-18

- Fixed auto-fetch home ID (no more 403 errors)

## [1.0.0] - 2026-01-17

- Initial release
