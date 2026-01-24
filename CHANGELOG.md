# Changelog

All notable changes to Tado CE will be documented in this file.

For detailed technical changes, see [DEV/CHANGELOG_DETAILED.md](DEV/CHANGELOG_DETAILED.md).

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
