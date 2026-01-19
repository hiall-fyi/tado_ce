# Roadmap

Feature requests and planned improvements for Tado CE.

## Considering (Need More Feedback)

- Air Comfort sensors (humidity comfort level)
- Boost button entity
- Multiple homes support
- Feature toggles to disable unused features and save API calls

---

## Completed

### v1.1.0
- [x] Link climate entities to Tado CE Hub device
- [x] Add Away Mode switch to manually toggle Home/Away status (1 API call per toggle)
- [x] Add `current_humidity` attribute to climate entities (no extra API calls)
- [x] Add preset mode support (Home/Away) to climate entities (1 API call per change)

### v1.0.1
- [x] Auto-fetch home ID from account (fixes 403 error for new users)

### v1.0.0
- [x] Initial release with full climate control, sensors, and API rate limit tracking
