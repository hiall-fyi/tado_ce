# Roadmap

Feature requests and planned improvements for Tado CE.

## Planned for v1.6.0

- [ ] **Better upgrade path testing** - Release beta versions for community testing before major releases
- [ ] **Deprecate tado_api.py** - Move sync functions to `__init__.py` or new `sync.py`, remove legacy CLI authentication tool (replaced by in-app OAuth flow since v1.4.0)

---

## Considering (Need More Feedback)

- Air Comfort sensors (humidity comfort level)
- Boost button entity
- Auto-assign devices to Areas during setup ([#14](https://github.com/hiall-fyi/tado_ce/issues/14))
- Apply for HACS default repository inclusion
- Max Flow Temperature control (requires OpenTherm, [#15](https://github.com/hiall-fyi/tado_ce/issues/15))
- Combi boiler mode - hide timers/schedules for on-demand hot water ([#15](https://github.com/hiall-fyi/tado_ce/issues/15))

### Local API Support ([Discussion #29](https://github.com/hiall-fyi/tado_ce/discussions/29))

Investigating local API to reduce cloud dependency and API call usage.

- **TadoLocal project**: https://github.com/AmpScm/TadoLocal (early stage)
- **Goal**: Local-first, cloud-fallback approach
- **Benefits**: Works without subscription (100 calls/day limit), faster response, works when cloud is down
- **Status**: Gathering community feedback - react/comment on Discussion #29 if interested!

### Multiple Homes (Simultaneous)

v1.4.0 supports selecting a home during setup, but only one home per integration entry. To support multiple homes simultaneously (add integration multiple times), the following changes would be needed:

1. **Unique ID**: Change from `tado_ce_integration` to `tado_ce_{home_id}`
2. **Data files**: Per-home files (`config_{home_id}.json`, `zones_{home_id}.json`)
3. **Hub device identifier**: Change from `tado_ce_hub` to `tado_ce_hub_{home_id}`
4. **Migration**: Existing users would need migration to new identifiers

**Note**: Entity IDs should remain stable if entity `unique_id` is unchanged. Low priority as multi-home use cases are rare.

---

## Completed

### v1.5.5 (2026-01-24)

- [x] **AC Auto mode fix** - Removed confusing `AUTO` option from AC zones (use `Heat/Cool` instead, which correctly maps to Tado's AUTO mode) ([#31](https://github.com/hiall-fyi/tado_ce/issues/31) - @neonsp)
- [x] **Reduced API calls** - Immediate refresh now uses 1 API call instead of 2-3 (only fetches zoneStates) ([#31](https://github.com/hiall-fyi/tado_ce/issues/31) - @neonsp)

### v1.5.4 (2026-01-24)

- [x] **AC fan/swing state fix** - Read correct field names (`fanLevel`, `verticalSwing`/`horizontalSwing`) from API response ([#31](https://github.com/hiall-fyi/tado_ce/issues/31) - @neonsp)
- [x] **AC hvac_action fix** - Use `acPower.value` for correct cooling/heating/drying/fan status ([#31](https://github.com/hiall-fyi/tado_ce/issues/31) - @neonsp)
- [x] **AC mode-specific payload** - Only send fields supported by each mode (DRY doesn't support fanLevel) ([#31](https://github.com/hiall-fyi/tado_ce/issues/31) - @neonsp)
- [x] **AC unified swing dropdown** - Single dropdown with off/vertical/horizontal/both options ([#31](https://github.com/hiall-fyi/tado_ce/issues/31) - @neonsp)
- [x] **Keep temperature when AC off** - Preserve last temperature setting for reference ([#31](https://github.com/hiall-fyi/tado_ce/issues/31) - @neonsp)
- [x] **Swing state persistence fix** - Preserve swing setting when changing temperature/fan/mode ([#31](https://github.com/hiall-fyi/tado_ce/issues/31) - @neonsp)
- [x] **AC Power sensor fix** - Handle newer API format (`acPower.value` instead of `percentage`) ([#31](https://github.com/hiall-fyi/tado_ce/issues/31) - @neonsp)
- [x] **Entity unique_id stability** - Changed `TadoOpenWindowSensor` and `TadoEarlyStartSwitch` to use `zone_id` instead of `zone_name`

### v1.5.3 (2026-01-24)

- [x] **Resume All Schedules button** - New `button.tado_ce_resume_all_schedules` on Hub device to reset all manual overrides ([Discussion #39](https://github.com/hiall-fyi/tado_ce/discussions/39) - @hapklaar)
- [x] **Fix AC control 422 error** - Fixed wrong API field names (`fanSpeed` → `fanLevel`, `swing` → `verticalSwing`/`horizontalSwing`) ([#31](https://github.com/hiall-fyi/tado_ce/issues/31) - @neonsp)
- [x] **Fix blocking I/O warning in config_flow.py** - Use `async_add_executor_job()` for file writes
- [x] **Comprehensive upgrade logging** - Detailed logs during migration/setup at INFO level (no debug enable needed)

### v1.5.2 (2026-01-24)

- [x] **Fix token loss on HACS upgrade** - Move data directory from `custom_components/tado_ce/data/` to `/config/.storage/tado_ce/` so HACS upgrades don't overwrite credentials ([#34](https://github.com/hiall-fyi/tado_ce/issues/34) - @jeverley, @hapklaar, @wrowlands3)

### v1.5.1 (2026-01-24)

- [x] **Fix OAuth flow "invalid flow specified" error** - New users unable to complete setup ([#36](https://github.com/hiall-fyi/tado_ce/issues/36) - @mkruiver)
- [x] **Re-authenticate option in UI** - Add reconfigure flow to re-authenticate without SSH ([#34](https://github.com/hiall-fyi/tado_ce/issues/34) - @jeverley, @hapklaar)

### v1.5.0 (2026-01-24)

**Major Code Quality Release** - Near-complete rewrite with async architecture, comprehensive null-safe patterns, and centralized data loading.

- [x] Async architecture (migrate from urllib to aiohttp)
- [x] `tado_ce.get_temperature_offset` service ([#24](https://github.com/hiall-fyi/tado_ce/issues/24) - @pisolofin)
- [x] Optional `offset_celsius` attribute on climate entities ([#25](https://github.com/hiall-fyi/tado_ce/issues/25) - @ohipe)
- [x] HVAC mode logic fix - show `auto` when following schedule ([#25](https://github.com/hiall-fyi/tado_ce/issues/25) - @ohipe)
- [x] Frequent mobile device sync option ([#28](https://github.com/hiall-fyi/tado_ce/issues/28) - @beltrao)
- [x] Fix blocking I/O warning ([#27](https://github.com/hiall-fyi/tado_ce/issues/27))
- [x] Fix null value crash in water_heater/climate ([#26](https://github.com/hiall-fyi/tado_ce/issues/26) - @hapklaar)
- [x] AC zone capabilities: DRY/FAN modes, fan levels, swing ([#31](https://github.com/hiall-fyi/tado_ce/issues/31) - @neonsp)
- [x] New `data_loader.py` module for centralized file loading
- [x] Comprehensive null-safe patterns across 7 files
- [x] Memory leak fixes, token refresh race condition fix
- [x] 235 tests passing

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
