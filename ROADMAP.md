# Roadmap

Feature requests and planned improvements for Tado CE.

For completed features, see [CHANGELOG.md](CHANGELOG.md).

---

## v1.9.0 - Hub Device Migration + Smart Heating

Hub device identifier migration plus intelligent heating features.

**Multi-Home Migration:**
- [ ] **Change hub device identifier** - From `tado_ce_hub` to `tado_ce_hub_{home_id}`
- [ ] **Device registry migration** - Existing hub device updated automatically
- [ ] **Entity IDs stable** - No entity ID changes for existing users

**Smart Heating:**
- [ ] **Room-aware Early Start** - Consider room thermal characteristics, not just outside temperature ([Discussion #33](https://github.com/hiall-fyi/tado_ce/discussions/33))
- [ ] **Predictive Heating** - Use weather forecast to optimize heating schedule
- [ ] **Heating Analytics** - Track heating patterns, efficiency metrics, cost estimates
- [ ] **Smart Boost** - One-tap boost with intelligent duration based on current vs target temperature

---

## v2.0.0 - Multiple Homes Enabled + Polish

Major release enabling full multi-home support.

**Multi-Home Support:**
- [ ] **Allow multiple integration entries** - Each entry for a different home
- [ ] **Multi-home setup guide** - Documentation for users with multiple properties

**Setup & Polish:**
- [ ] **Auto-assign Areas** - Suggest HA Areas based on zone names during setup ([#14](https://github.com/hiall-fyi/tado_ce/issues/14))
- [ ] **Setup wizard improvements** - Streamlined flow with better error messages
- [ ] **Delete tado_api.py** - File deprecated in v1.6.0, now fully removed

**Local API (Experimental):**
- [ ] **Local-first, cloud-fallback** - Use local API when available, fall back to cloud
- [ ] **Hybrid mode** - Configurable per-feature (e.g., local for reads, cloud for writes)
- [ ] **Community testing program** - Beta channel for local API testing

**Note**: Local API requires community help to test across different Tado hardware versions. See [Discussion #29](https://github.com/hiall-fyi/tado_ce/discussions/29).

---

## Considering (Need More Feedback)

- Air Comfort sensors (humidity comfort level)
- Boost button entity
- Apply for HACS default repository inclusion
- Max Flow Temperature control (requires OpenTherm, [#15](https://github.com/hiall-fyi/tado_ce/issues/15))
- Combi boiler mode - hide timers/schedules for on-demand hot water ([#15](https://github.com/hiall-fyi/tado_ce/issues/15))

---

## Migration Design

All migrations are cumulative - users can upgrade directly from any version (e.g., v1.6.0 → v2.0.0) and all intermediate migrations will be applied automatically. Each migration step is idempotent (safe to run multiple times).

Entity IDs remain stable throughout migration if entity `unique_id` is unchanged.
