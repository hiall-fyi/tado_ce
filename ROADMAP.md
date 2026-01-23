# Roadmap

Feature requests and planned improvements for Tado CE.

## Planned for v1.5.0

- [ ] Async architecture (migrate from urllib to aiohttp)
- [ ] Centralize all API URLs in const.py (remove hardcoded URLs from switch.py, __init__.py, tado_api.py, config_flow.py)
- [ ] `get_temperature_offset` service - on-demand fetch of current offset ([#24](https://github.com/hiall-fyi/tado_ce/issues/24) - @pisolofin)
- [ ] Optional `offset_celsius` attribute on climate entities - for template-based automations ([#25](https://github.com/hiall-fyi/tado_ce/issues/25) - @ohipe)

---

## Considering (Need More Feedback)

- Air Comfort sensors (humidity comfort level)
- Boost button entity
- Auto-assign devices to Areas during setup ([#14](https://github.com/hiall-fyi/tado_ce/issues/14))
- Apply for HACS default repository inclusion
- Max Flow Temperature control (requires OpenTherm, [#15](https://github.com/hiall-fyi/tado_ce/issues/15))
- Combi boiler mode - hide timers/schedules for on-demand hot water ([#15](https://github.com/hiall-fyi/tado_ce/issues/15))

### Multiple Homes (Simultaneous)

v1.4.0 supports selecting a home during setup, but only one home per integration entry. To support multiple homes simultaneously (add integration multiple times), the following changes would be needed:

1. **Unique ID**: Change from `tado_ce_integration` to `tado_ce_{home_id}`
2. **Data files**: Per-home files (`config_{home_id}.json`, `zones_{home_id}.json`)
3. **Hub device identifier**: Change from `tado_ce_hub` to `tado_ce_hub_{home_id}`
4. **Migration**: Existing users would need migration to new identifiers

**Note**: Entity IDs should remain stable if entity `unique_id` is unchanged. Low priority as multi-home use cases are rare.

---

## Completed

### v1.4.1 (2026-01-23)

**Hotfix Release:**
- [x] Fixed authentication broken after upgrade from v1.2.x (Issue #26) - missing migration path from VERSION 2/3 to VERSION 4

### v1.4.0 (2026-01-23)

**Setup Simplification Release:**
- [x] New Device Authorization setup flow (no more SSH required - setup entirely in HA UI)
- [x] Home selection during setup (supports accounts with multiple homes)
- [x] Change weather sensors default to OFF (saves 1 API call per sync)
- [x] Change mobile device tracking default to OFF (saves 1 API call per sync)
- [x] API Reset sensor now uses Tado API's actual reset time (not calculated from history)
- [x] Added `next_poll` and `current_interval_minutes` attributes to API Reset sensor
- [x] Cleaned up API Usage sensor (removed redundant reset attributes)
- [x] Improve initial reset time estimation
- [x] Logging levels cleanup (setup messages from `warning` to `debug`/`info`)
- [x] Fix options not saving properly (weather/mobile checkboxes reverting)
- [x] Fix Day/Night Start Hour options showing confusing checkboxes (Issue #17)
- [x] Uniform polling mode: set Day Start Hour = Night Start Hour for 24/7 consistent polling (Issue #17)
- [x] Boiler Flow Temperature sensor: auto-detect OpenTherm data, only create sensor if available (Issue #15)
- [x] Move Boiler Flow Temperature sensor to Hub device with `source_zone` attribute (Issue #15)
- [x] Fix climate preset mode stuck on Away (was using mobile device location instead of home state) (Issue #22)

### v1.2.1 (2026-01-22)

**Hotfix Release:**
- [x] Fixed duplicate hub cleanup race condition (Issue #10)
- [x] Fixed confusing entity names for multi-device zones (Issue #11)
- [x] Improved migration handling (missing zones_info.json)

### v1.2.0 (2026-01-21)

**Note**: v1.2.0 combines all planned features from both v1.2.0 and v1.3.0 into a single release.

**New Features:**
- [x] Zone-based device organization (each zone as separate device)
- [x] Improved entity naming (removed "Tado CE" prefix from zone entities)
- [x] Optional weather sensors (disable to save 1 API call per sync)
- [x] Optional mobile device tracking (disable to save 1 API call per full sync)
- [x] API call history tracking with call type codes
- [x] Test Mode switch (enforce 100-call limit for testing)
- [x] Reset time as actual timestamp attribute
- [x] Configurable API history retention (0-365 days)
- [x] Hot water operation modes (AUTO/HEAT/OFF) with proper timer support
- [x] Hot water timer preset buttons (30/60/90 minutes quick access)
- [x] Custom water heater timer service (`tado_ce.set_water_heater_timer`)
- [x] Boiler flow temperature sensor (for hot water zones)
- [x] Configurable hot water timer duration (5-1440 minutes, default 60)
- [x] Customizable day/night hours for smart polling
- [x] Manual polling interval override (custom day/night intervals)
- [x] Advanced API management configuration UI
- [x] Config flow migration system (VERSION 2)
- [x] Immediate refresh after user actions

**Bug Fixes & Improvements:**
- [x] Fixed token refresh race condition (centralized AuthManager)
- [x] Fixed home state API calls not being tracked
- [x] Fixed immediate refresh quota checking (prevents quota exhaustion)
- [x] Fixed rate limit reset time calculation (removed buggy modulo logic)
- [x] Fixed thread safety issue in immediate refresh (async_create_task from sync context)
- [x] Fixed database performance (API sensor attributes optimized, 53% size reduction)
- [x] Improved immediate refresh with exponential backoff (10s → 20s → 40s → 80s → 160s → 300s)
- [x] Improved rate limit calculation (three-strategy approach for accuracy)
- [x] Improved logging levels (INFO for normal operations, DEBUG for troubleshooting)
- [x] Added strategic DEBUG logging for token operations, API calls, and rate limits

**Documentation:**
- [x] Updated README with new screenshots (zone devices, hot water controls, config UI)
- [x] Added dashboard card examples for hot water controls
- [x] Added debug logging guide in troubleshooting section
- [x] Service compatibility documentation

### v1.1.0

- [x] Link climate entities to Tado CE Hub device
- [x] Add Away Mode switch to manually toggle Home/Away status (1 API call per toggle) (Issue #3)
- [x] Add `current_humidity` attribute to climate entities (no extra API calls) (Issue #2)
- [x] Add preset mode support (Home/Away) to climate entities (1 API call per change) (Issue #2)

### v1.0.1

- [x] Auto-fetch home ID from account (fixes 403 error for new users) (Issue #1)

### v1.0.0

- [x] Initial release with full climate control, sensors, and API rate limit tracking
