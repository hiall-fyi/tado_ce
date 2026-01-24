# Tado CE - Credits & Acknowledgments

**All Versions** - Community contributions that made this integration possible

---

## v1.5.3 (2026-01-24) - AC Fix & Resume All Schedules

### Bug Reports & Issue Reporters

**[@neonsp](https://github.com/neonsp)** - [Issue #31](https://github.com/hiall-fyi/tado_ce/issues/31)
- Reported AC control 422 errors
- Provided critical API payload from app.tado.com showing correct field names
- Helped identify `fanSpeed` → `fanLevel`, `swing` → `verticalSwing`/`horizontalSwing` fix

### Feature Contributors

**[@hapklaar](https://github.com/hapklaar)** - [Discussion #39](https://github.com/hiall-fyi/tado_ce/discussions/39)
- Requested Resume All Schedules button
- Shared automation use case for resetting manual overrides

### What Was Fixed/Added

- ✅ **Issue #31**: AC control 422 error - fixed API field names
- ✅ **Discussion #39**: Resume All Schedules button on Hub device
- ✅ Blocking I/O warning in config_flow.py
- ✅ Comprehensive upgrade logging at INFO level

---

## v1.5.2 (2026-01-24) - Data Storage Fix

### Bug Reports & Issue Reporters

**[@jeverley](https://github.com/jeverley)** - [Issue #34](https://github.com/hiall-fyi/tado_ce/issues/34)
- Reported token loss after HACS upgrade
- Identified that data directory was being overwritten

**[@hapklaar](https://github.com/hapklaar)** - [Issue #34](https://github.com/hiall-fyi/tado_ce/issues/34)
- Confirmed token loss issue
- Helped validate the problem

**[@wrowlands3](https://github.com/wrowlands3)** - [Issue #34](https://github.com/hiall-fyi/tado_ce/issues/34)
- Additional confirmation of the upgrade issue

### What Was Fixed

- ✅ **Issue #34**: Token loss on HACS upgrade - data directory moved to `/config/.storage/tado_ce/`

---

## v1.5.1 (2026-01-24) - OAuth & Re-auth Fix

### Bug Reports & Issue Reporters

**[@mkruiver](https://github.com/mkruiver)** - [Issue #36](https://github.com/hiall-fyi/tado_ce/issues/36)
- Reported OAuth flow "invalid flow specified" error for new users

**[@jeverley](https://github.com/jeverley)** - [Issue #34](https://github.com/hiall-fyi/tado_ce/issues/34)
- Requested re-authenticate option in UI

**[@hapklaar](https://github.com/hapklaar)** - [Issue #34](https://github.com/hiall-fyi/tado_ce/issues/34)
- Supported re-authenticate feature request

**[@harryvandervossen](https://github.com/harryvandervossen)** - [Discussion #35](https://github.com/hiall-fyi/tado_ce/discussions/35)
- Provided detailed OAuth flow feedback

### What Was Fixed

- ✅ **Issue #36**: OAuth flow error - simplified to manual check approach
- ✅ **Issue #34**: Re-authenticate option added via reconfigure flow

---

## v1.5.0 (2026-01-24) - Async & Stability Release

### Bug Reports & Issue Reporters

**[@hapklaar](https://github.com/hapklaar)** - [Issue #26](https://github.com/hiall-fyi/tado_ce/issues/26)
- Reported null value crash in water_heater entities
- Helped identify the `temperature: null` API response issue for HOT_WATER zones

**[@neonsp](https://github.com/neonsp)** - [Issue #31](https://github.com/hiall-fyi/tado_ce/issues/31)
- Reported AC zone missing DRY/FAN modes, fan levels, and swing options
- Provided detailed API response data showing AC capabilities endpoint
- Helped identify that AC capabilities require separate API endpoint

### Feature Contributors

**[@pisolofin](https://github.com/pisolofin)** - [Issue #24](https://github.com/hiall-fyi/tado_ce/issues/24)
- Requested `get_temperature_offset` service for automations

**[@ohipe](https://github.com/ohipe)** - [Issue #25](https://github.com/hiall-fyi/tado_ce/issues/25)
- Requested optional `offset_celsius` attribute on climate entities
- Identified HVAC mode behavior difference from official integration

**[@beltrao](https://github.com/beltrao)** - [Issue #28](https://github.com/hiall-fyi/tado_ce/issues/28)
- Requested frequent mobile device sync option for presence automations

### What Was Fixed/Added

- ✅ **Issue #24**: `tado_ce.get_temperature_offset` service
- ✅ **Issue #25**: Optional `offset_celsius` attribute, HVAC mode logic fix
- ✅ **Issue #26**: Null value crash fix for water_heater and climate entities
- ✅ **Issue #27**: Async migration, blocking I/O warning fix
- ✅ **Issue #28**: Frequent mobile device sync option
- ✅ **Issue #31**: Full AC capabilities support (DRY/FAN modes, fan levels, swing)

---

## v1.4.1 (2026-01-23) - Hotfix Release

### Bug Reports & Issue Reporters

**[@hapklaar](https://github.com/hapklaar)** - [Issue #26](https://github.com/hiall-fyi/tado_ce/issues/26)
- First to report authentication broken after v1.2.1 → v1.4.0 upgrade
- Quick bug report that caught this critical issue early

**[@mjsarfatti](https://github.com/mjsarfatti)** - [Issue #26](https://github.com/hiall-fyi/tado_ce/issues/26)
- Additional confirmation of the upgrade issue
- Helped validate the problem

### What Was Fixed

- ✅ **Issue #26**: Authentication broken after upgrade - missing migration path from VERSION 2/3 to VERSION 4

---

## v1.4.0 (2026-01-23) - Setup Simplification Release

### Feature Contributors

**Setup Flow Redesign**
- New device authorization flow - no more SSH required
- Home selection for multi-home accounts

### Bug Reports & Issue Reporters

**[@ChrisMarriott38](https://github.com/ChrisMarriott38)** - [Issue #15](https://github.com/hiall-fyi/tado_ce/issues/15), [Issue #16](https://github.com/hiall-fyi/tado_ce/issues/16), [Issue #17](https://github.com/hiall-fyi/tado_ce/issues/17)
- Reported Boiler Flow Temperature sensor issues
- Identified API Reset time confusion after re-authentication
- Reported Options UI issues (checkboxes, values not saving)
- Suggested uniform polling mode

**[@jeverley](https://github.com/jeverley)** - [Issue #22](https://github.com/hiall-fyi/tado_ce/issues/22)
- Reported climate preset mode stuck on Away
- Helped identify mobile device location vs home state issue

**[@hapklaar](https://github.com/hapklaar)**
- Volunteered for OpenTherm testing

### What Was Fixed

- ✅ **Issue #15**: Boiler Flow Temperature sensor - auto-detect OpenTherm, moved to Hub device
- ✅ **Issue #16**: API Reset time now uses actual Tado API reset time
- ✅ **Issue #17**: Options UI fixes, uniform polling mode support
- ✅ **Issue #22**: Climate preset mode now uses home state instead of mobile device location

---

## v1.2.1 (2026-01-22) - Hotfix Release

### Bug Reports & Issue Reporters

**[@marcovn](https://github.com/marcovn)** - [Issue #10](https://github.com/hiall-fyi/tado_ce/issues/10), [Issue #11](https://github.com/hiall-fyi/tado_ce/issues/11)
- Reported duplicate hub issue after v1.1.0 → v1.2.0 upgrade
- Reported confusing entity names for multi-device zones
- Provided valuable feedback and testing
- Helped identify both critical issues in v1.2.1

**[@ChrisMarriott38](https://github.com/ChrisMarriott38)** - [Issue #10](https://github.com/hiall-fyi/tado_ce/issues/10)
- Reported duplicate hub cleanup issue
- Provided detailed testing feedback
- Helped validate the fix

**[@hapklaar](https://github.com/hapklaar)** - [Issue #10](https://github.com/hiall-fyi/tado_ce/issues/10)
- Reported duplicate hub issue
- Contributed to testing and validation

### What Was Fixed

- ✅ **Issue #10**: Duplicate hub cleanup race condition - automatic cleanup on upgrade
- ✅ **Issue #11**: Multi-device zone entity naming - clear device type + index suffixes

---

## v1.2.0 (2026-01-21) - Major Stability Release

### Feature Contributors

**[@wrowlands3](https://github.com/wrowlands3)** - [Issue #4](https://github.com/hiall-fyi/tado_ce/issues/4)
- Requested zone-based device organization
- Suggested improved entity naming (remove "Tado CE" prefix)
- Highlighted difficulty in searching and identifying zones in UI
- Helped shape the device organization structure

**[@ChrisMarriott38](https://github.com/ChrisMarriott38)** - [Issue #4](https://github.com/hiall-fyi/tado_ce/issues/4)
- Requested boiler flow temperature sensor integration
- Suggested optional weather sensors toggle to save API calls
- Requested API call tracking with detailed history and diagnostic codes
- Suggested enhanced reset time display (human-readable timestamp)
- Proposed customizable day/night polling intervals for shift workers
- Requested test mode with enforced API limits (100 calls) for testing
- Suggested pre-release testing mechanism for community feedback
- Provided extensive testing feedback and bug reports
- Identified API polling patterns and optimization opportunities
- Shared advanced use cases: mold risk calculations, air quality index, weather compensation for heat pumps
- Helped validate temperature and humidity sensor functionality
- Provided detailed feedback on API optimization and usage patterns

**[@donnie-darko](https://github.com/donnie-darko)** - [Issue #4](https://github.com/hiall-fyi/tado_ce/issues/4)
- Requested `set_water_heater_timer` service with temperature parameter
- Proposed service compatibility with official Tado integration
- Shared solar water heater use case and automation requirements
- Suggested parameter naming alignment (`time_period` vs `duration`)
- Helped shape service design for seamless migration from official integration
- Provided NODE-RED workflow requirements for solar thermal systems

**[@marcovn](https://github.com/marcovn)** - [Issue #4](https://github.com/hiall-fyi/tado_ce/issues/4)
- Participated in Issue #4 discussions
- Contributed to community feedback

**[@StreborStrebor](https://github.com/StreborStrebor)** - [Issue #4](https://github.com/hiall-fyi/tado_ce/issues/4)
- Requested immediate refresh after user actions
- Highlighted the 2-minute delay issue
- Reported device card not updating after temperature/mode changes
- Requested AC fan mode controls (Auto, Low, Medium, High)
- Requested AC swing mode controls (Off, Vertical, Horizontal, Both)
- Suggested disabling weather polling to free up API calls for more frequent updates
- Helped prioritize UX improvements

### Bug Reports & Issue Reporters

**[@LorDHarA](https://github.com/LorDHarA)** - [Issue #1](https://github.com/hiall-fyi/tado_ce/issues/1) *(Fixed in v1.0.1)*
- Identified 403 authentication error for new users
- Led to auto-fetch home ID feature
- Helped improve initial setup experience

**[@hapklaar](https://github.com/hapklaar)** - [Issue #2](https://github.com/hiall-fyi/tado_ce/issues/2), [Issue #5](https://github.com/hiall-fyi/tado_ce/issues/5)
- Suggested adding humidity attribute to climate entities *(Implemented in v1.1.0)*
- Suggested adding preset mode support (Home/Away) *(Implemented in v1.1.0)*
- Reported away mode switch toggling back issue
- Reported 2-minute delay for temperature/mode changes
- Both issues fixed with Immediate Refresh feature
- Generous Buy Me a Coffee supporter! ☕

**[@MJWMJW2](https://github.com/MJWMJW2)** - [Issue #3](https://github.com/hiall-fyi/tado_ce/issues/3) *(Implemented in v1.1.0)*
- Requested Away Mode switch for manual Home/Away toggle
- Improved geofencing control

**[@ctcampbell](https://github.com/ctcampbell)** - [Issue #6](https://github.com/hiall-fyi/tado_ce/issues/6)
- Requested proper AUTO/HEAT/OFF operation modes for hot water
- Highlighted limitations of ON/OFF only modes (water runs forever or schedule never runs)
- Requested timer-based HEAT mode support
- Requested AUTO mode to return to schedule
- Led to comprehensive hot water operation modes implementation in v1.2.0

**[@greavous1138](https://github.com/greavous1138)** - [Issue #7](https://github.com/hiall-fyi/tado_ce/issues/7)
- Reported `duration` parameter not working in `climate.set_temperature` service
- Identified YAML configuration error with timer duration
- Requested boost button feature
- Helped identify service parameter issues
- Contributed to climate timer service improvements

**[@thefern69](https://github.com/thefern69)** - [Issue #9](https://github.com/hiall-fyi/tado_ce/issues/9)
- Provided Docker installation instructions
- Helped improve README documentation for Docker users

### Community Quotes

> "This integration saves me from rate limit headaches!" - Community feedback

> "The immediate refresh feature is exactly what I needed!" - @hapklaar

> "Zone-based devices make organization so much better!" - @wrowlands3

> "API call tracking gives me peace of mind about my quota." - @ChrisMarriott38

> "What an incredible reply! So detailed thanks! I'm ultra novice at code things, just a dabble here and there, but have done Extensive testing for other tools/plugins/web/developers over the years for work." - @ChrisMarriott38

> "Boiler Flow would be my No1 To be included in the API pull. So i dont have to have that as a separate rest API call if possible. to save the Limit." - @ChrisMarriott38

> "This integration turns 'on' which turns the water on until I manually cancel the state (i.e. forever), or 'off' which means my schedule never runs." - @ctcampbell (Issue #6 - Fixed in v1.2.0)

> "First of all, thanks for making this integration, this morning I was actually able to use my Tado system via HA when I woke up!" - @greavous1138

---

## 🌟 Special Thanks

**Hardware Verification:**

**[@wyx087](https://github.com/wyx087)** - [Discussion #21](https://github.com/hiall-fyi/tado_ce/discussions/21)
- Verified Tado V2 hardware compatibility
- Confirmed integration works perfectly with V2 devices

**Community Testers & Feedback Providers:**
- Users who shared their setup configurations
- Community members who provided detailed use cases
- All supporters on Buy Me a Coffee

**Technical Contributions:**
- Bug reports that helped identify edge cases
- Feature requests that shaped the roadmap
- Detailed feedback on API usage patterns
- Real-world testing across different Tado setups
- Advanced automation examples (mold risk, air quality, weather compensation)
- Documentation improvements and setup guides

---

## 📊 Overall Impact

**Total Issues Addressed:** 22+ issues across all versions
**Features Implemented:** 25+ new features
**Bug Fixes:** 15+ critical/high-priority fixes
**API Optimization:** 60-70% reduction in API calls
**Community Engagement:** Active discussions and continuous feedback

---

## 🎯 Looking Forward

The community continues to shape Tado CE's future! Current discussions:

**Planned for v1.5.0:**
- Async architecture (migrate from urllib to aiohttp)
- Centralize all API URLs in const.py

**Requested Features:**
- AC fan mode controls (Auto, Low, Medium, High) - @StreborStrebor
- AC swing mode controls (Off, Vertical, Horizontal, Both) - @StreborStrebor
- Boost button entity - @greavous1138
- Air Comfort sensors (humidity comfort level)
- Multiple homes support (simultaneous)
- Max Flow Temperature control (requires OpenTherm) - @ChrisMarriott38
- Combi boiler mode - @ChrisMarriott38
- Auto-assign devices to Areas during setup

**Want to contribute?** Open an issue or join the discussion on [GitHub](https://github.com/hiall-fyi/tado_ce/issues)!

---

**Made with ❤️ by the Tado CE community**

*Special thanks to everyone who uses, tests, reports issues, and supports this project. You make this integration better every day!*
