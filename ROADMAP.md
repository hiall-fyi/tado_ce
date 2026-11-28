# Tado CE Roadmap

Planned features and improvements for Tado CE.

For completed features, see [CHANGELOG.md](CHANGELOG.md).

---

## Version Overview

### Current Release

- Stable: v4.6.0 (2026-11-01)
- Headliners:
  - Heating zones can send their changes through the Tado cloud, and a zone with no Override behaviour of its own now follows the hub
  - The dropdown options in Zone Configuration and the hub's Override duration follow your language
  - A new action shows the flow temperature setting Tado keeps for an OpenTherm system
  - Tado CE sets up again on Home Assistant before 2026.7, and timers shorter than 15 minutes go out as you set them
- Beta: v5.0.0-beta.2 (2026-11-28), a pre-release you opt into. It needs Home Assistant 2026.11 or newer, and the [release notes](https://github.com/hiall-fyi/tado_ce/releases/tag/v5.0.0-beta.2) say what changed and how to install it
- Supported versions: v4.1.0 and later

### Next Release

- Next: v5.0.0, which reworks how Tado CE talks to Tado and to HomeKit and clears out the old compatibility code. The first beta is out and there is no date for the stable release
- After v4.6.0 there is no further v4.x release, except for a serious security problem or a problem that stops heating from working
- Help wanted: if you have HomeKit connected, counts of how long Tado takes to show a change would help set the timings v5.0.0 depends on, and please post your file before you update to the beta because the counts start again at v5.0.0, see [Discussion #391](https://github.com/hiall-fyi/tado_ce/discussions/391)
- Breaking changes: see [Breaking Changes](#breaking-changes-v500) below

### End of Life

- v4.6.0 is the last planned v4.x release, and until v5.0.0 launches it only gets fixes for a serious security problem or a problem that stops heating from working
- v5.0 launch will trigger v4.x EOL

## Upgrade Paths

| From | To | Intermediate Step | Notes |
|------|----|--------------------|-------|
| v3.x or v4.0.x | v4.x (any, including this release) | None | Direct upgrade; the migration code that reads your old settings is still in place |
| v3.x or v4.0.x | v5.0.0+ | Install any v4.1.0+ release first | v5.0.0 only reads the settings format v4.1.0 introduced; an older install needs one v4.1.0+ release to update to it first |
| v4.1.0+ | v5.0.0+ | None | Direct upgrade |

### v3.x or v4.0.x to v4.x

Direct upgrade to any v4.x release, including this one. No intermediate step required. Settings and entity IDs carry over automatically.

### v3.x or v4.0.x to v5.0.0+

v5.0.0 requires **v4.1.0 or later** to load your saved state. If you're not there yet, install any v4.1.0+ release first (this one included), verify your setup works (run a fresh restart, check entities in Developer Tools), then install v5.0.0+ whenever it's out.

### v4.1.0+ to v5.0.0+

Direct upgrade. No intermediate steps required.

v5.0.0 removes migration code for versions older than v4.1.0, so it will refuse to load anything below that and tell you which version to upgrade to first.

## Breaking Changes (v5.0.0)

### Requirement: Home Assistant 2026.11 or newer

**What breaks:** v5.0.0 uses the new names Home Assistant 2026.11 gave the target temperature, current temperature and temperature unit of a climate entity (the old names stop working in 2027.11), and older versions don't know them. It also asks for version 4.0.0 or later of the HomeKit library (`aiohomekit`) that it shares with Home Assistant's own HomeKit Controller, and Home Assistant 2026.7 and older ship 3.2.20.

**User impact:** None on Home Assistant 2026.11 or newer. HACS may still offer the download on an older Home Assistant (see v4.3.1), so don't count on it to stop you.

**When:** v5.0.0 release

**Migration:** Update Home Assistant before you update Tado CE, or stay on v4.5.x. The reasoning and the Home Assistant version table are in [Discussion #367](https://github.com/hiall-fyi/tado_ce/discussions/367).

### Removal: Pre-v4.1.0 Data Format Migration

**What breaks:** v5.0.0 will refuse to load state from v3.x or v4.0.x installations. Existing installs on older versions will be blocked with a clear error message.

**Why:** Post-v4.0 migration code is maintenance debt. Removing it simplifies version floor logic and allows aggressive refactoring downstream.

**When:** v5.0.0 release

**Migration:** Upgrade to **v4.1.0 or later before moving to v5.0.0**. See [Upgrade Paths](#upgrade-paths) for step-by-step instructions. Any v4.1.0+ release, including v4.5.0, auto-migrates v3.x and v4.0.x state automatically; simply install one, verify the setup works, then install v5.0.0.

### Rename: Refresh AC Capabilities → Refresh Zone Capabilities

**What changes:** The hub button that re-fetches a zone's supported modes is renamed from "Refresh AC Capabilities" to "Refresh Zone Capabilities", reflecting what it actually covers since v4.1.4 (AC zones and hot-water zones).

**User impact:** Minimal. On upgrade, the button keeps its history and customisation; the friendly name and icon update in the UI. The `entity_id` doesn't change for existing installs because Home Assistant only sets it once, at creation. New installs from v5.0.0 onward get the updated `entity_id` (`button.tado_ce_hub_refresh_zone_capabilities`).

**When:** v5.0.0 release

### Rename: the zone services

**What changes:** Each zone service now says which kind of zone it works on, for example `resume_schedule` becomes `resume_climate_schedule` and `resume_water_heater_schedule`, and `set_schedule_temperature` becomes `set_climate_schedule_temperature`. The services that covered heating, AC and hot water are split in two, so a call that targeted a heating room together with the hot water zone needs two calls. The full old and new list is in the [CHANGELOG](CHANGELOG.md) under 5.0.0-beta.2.

**User impact:** Automations and scripts that call an old name fail with "action not found" until you change the name. There is no deprecation window. The zone services also follow Home Assistant's standard target rules (an area, device, floor or label works, an entity that is not a matching Tado CE zone is skipped), and `set_climate_open_window_mode` takes `time_period` as text like `"00:30:00"` instead of `duration` in seconds. A bare number is refused on it and on the two timer services.

**When:** v5.0.0 release (first in 5.0.0-beta.2)

---

*Additional breaking changes to be documented as v5.0 development progresses. Check back before upgrading.*

## Future Features

### Up Next

**On API-call optimisation as a through-line.** A design principle that runs through the whole v4.x line: spend as few of your daily Tado API calls as possible without losing freshness. The pieces already shipped (the 5-minute polling floor, per-type refresh floors you can tune, deferring to HomeKit's dial when local control is connected, taking the offset refresh off its all-at-once burst, and the `tado_ce.refresh` service that lets you force a single presence fetch past the floor when you drive your own cadence) are in the changelog. The next step on this front is the API resilience pass under **Later** below, which hardens how the integration degrades when any one Tado endpoint is unavailable. The goal stays the same: your quota goes further and local control stays solid.

AC mode, swing, fan and timer changes all go through the cloud. A plain temperature change on a running AC zone can go over HomeKit when the bridge is connected, the same as a heating zone. See the AC entry below for why fuller HomeKit local control on Smart AC Control V3+ isn't on the active roadmap.

### Later — feature additions, no fixed release

Wanted and well-specified, and the foundation it builds on (persistence, polling rework, HomeKit reliability) is already in place. No release target yet.

- **API resilience pass** — a review of every Tado endpoint the integration relies on, hardening how it degrades when any one of them is unavailable so a single Tado-side hiccup doesn't take entities down with it. Also settles a long-standing request: per-zone hysteresis / Acceptance Range and Minimum On/Off Time can't be exposed as entities because Tado serves them from a newer API the integration doesn't reach. They become a documentation note rather than a sensor.

### Future Consideration

#### AC

- **Smart AC Control V3+ standalone HomeKit pairing** ([Discussion #271](https://github.com/hiall-fyi/tado_ce/discussions/271) - @MacrosorcH) — Smart AC Control units are autonomous WiFi devices, each with its own 8-digit HomeKit code, paired separately from the Internet Bridge. Tado CE only handles the bridge pairing today, so AC zones use the cloud path for every operation regardless of HomeKit configuration. Checked what owning that pairing would actually add: a paired unit's HomeKit characteristics cap out at temperature and on/off, the same ceiling Home Assistant's own HomeKit Device integration already gets, so native support wouldn't add fan, swing or mode control either. You can get local temperature and humidity readings today without it: pair the unit to Home Assistant's HomeKit Device integration, then bind its temperature and humidity entities under the AC zone's Options Flow, the same external-sensor binding a heating zone uses. Native per-unit pairing stays off the active roadmap, since it would save a setup step rather than add a capability, and it's still hardware I don't own to build or test against.
- **AC cloud-path fixes still land from field reports.** The hardware gap above blocks *local* HomeKit control, but the cloud path AC uses today is maintained, and a user with the unit can drive a fix without me owning one. In v4.1.0, [#305](https://github.com/hiall-fyi/tado_ce/issues/305) (@stefanzweig1979) was exactly that: fan changes never reached older units because the integration sent the fan setting under the wrong field name (the plural `fanSpeeds` from the capability list, where the write payload wants the singular `fanSpeed`). A debug log plus a captured Tado web-app request confirmed the correct field, and the fix shipped. So a cloud-side AC issue with a clear repro is actionable; only the standalone local-control feature is hardware-blocked.


#### Infrastructure

- **Local Only Mode** — A toggle that stops all cloud polling after initial setup, running purely off HomeKit bridge data. Technically feasible — the coordinator already skips cloud calls when HomeKit provides live data. Tradeoff: cloud-only data (schedules, battery, heating power, geofencing) would go stale. Could include a daily cloud check for diagnostics.
- **Bridge wiring-state poll cadence** ([#289](https://github.com/hiall-fyi/tado_ce/issues/289) - @driagi) — the boiler wiring sensor reads the bridge every 60 seconds on its own loop. These calls hit the bridge directly with its local auth key, so they don't count against your daily Tado cloud quota, but a minute-by-minute read of an almost-static value is heavier on the bridge than it needs to be. The wiring state only changes when you rewire the boiler, so a much slower cadence (or an event-driven refresh) covers it. Surfaced while diagnosing #289's call volume.
- **Smart polling pause/resume rework** — the polling cadence has grown a handful of separate decisions (when to slow down on low quota, when to pause entirely, when to resume after the quota resets) that were each added on their own and never designed as one piece. The plan is to pull them into a single clear state machine so the edges line up. The tier-aware quota reserve that the rework was originally going to fix already landed in v4.0.2, so this was parked pending a concrete sign that the pause/resume path itself, not just the reserve threshold, was where a bug lived. Two such signs turned up during testing: a reload landing while quota was already at the reserve floor could leave entities unavailable instead of serving cached data, and a repair notification tied to the same pause logic could linger after the underlying problem had already cleared. Both get their own fixes first, but finding two independent gaps in one sitting is exactly the "grown on its own, never designed as one piece" pattern this rework exists to fix.
- **Smart Valve and Offset Sync read different temperatures** — the two compensation modes don't read the room the same way. On a zone with a HomeKit bridge connected, Smart Valve Control reads the live HomeKit-merged temperature, while Offset Sync reads the cloud reading. Only one mode runs per zone, so they never fight inside a single zone, but two zones set up the same way can compensate off slightly different numbers depending on which mode they're in. It's a small difference in practice and there's no bug today, but the inconsistency is worth tidying so both modes share one source. Folds in naturally with the temperature-source work the integration already exposes per zone.
  - **Pick the source direction deliberately, it's not a free change.** Moving Offset Sync onto the live HomeKit reading only helps zones that have a bridge connected, so it trades the current mode-to-mode inconsistency for a new connected-vs-cloud-only one. The cleaner direction is usually the other way (both modes read the cloud reading), but that gives up Smart Valve's real-time response. Decide this on its own before building, not as a rider on another change.
  - **It ties into the overnight offset-swing fix.** That fix waits for the cloud reading to catch up to the last write before correcting again, keyed on the cloud poll. Offset Sync reads the cloud reading today, so that timing lines up. If Offset Sync is ever switched to the live HomeKit reading, the wait has to follow the HomeKit reading instead, or it'll hold off corrections longer than it should. Whoever does this must re-check the swing fix's timing in the same pass.

#### Long-Term Exploration

- **Fully Local Control** ([Discussion #29](https://github.com/hiall-fyi/tado_ce/discussions/29)) — Control via the 868MHz protocol between Bridge and TRVs, bypassing both cloud and HomeKit. Requires specialized hardware and community help.

## Support Timeline

v4.6.0 is the last planned v4.x release. Until v5.0.0 launches it is supported for a serious security problem or a problem that stops heating from working, and nothing else. EOL dates will be announced closer to the v5.0.0 release.

---

## v5.0.0 — Legacy cleanup

A spring-clean release that drops backward-compat code accumulated through the v3.x and v4.x cycles. The cleanup is laser-focused on dead surface area, not behaviour change.

**You need v4.1.0 or later installed before you upgrade to v5.0.0, and if you are older than that, go through any current v4.x release first.** v4.1.0 is the line v5.0.0 refuses to load below, because that is where the saved-settings format it reads begins. Any 4.1.0+ release updates your settings to that format, so there is no reason to pick an older one. Coming from v4.1.0 or later, there is nothing to do. v5.0.0 does not auto-migrate the v3.x option keys, entity unique_ids or storage layouts, because the code that handled those upgrades is removed in this release. The path through the current v4.x release stays open, so there is no need to do it in one jump.

Skipping that step now fails loudly rather than quietly. An entry too old to upgrade refuses to load and raises a repair notice telling you to install a v4.1.0+ release first, instead of loading with your settings silently reset to their defaults. Nothing is deleted or rewritten when it refuses, so going back to that release recovers the install. Note that v4.0.x counts as too old here even though it is a v4 release: its saved settings are one format behind v4.1.0's.

Planned removals:

- **Entity-naming polish** — a couple of internal naming inconsistencies get a clean baseline. These change entity IDs, so they ride the breaking-change boundary with a migration step rather than stranding your existing entities.
- **Hand-pinned entity IDs retired** — a couple of entities (the heating-circuit select, the Identify button) set their `entity_id` by hand to sidestep a Home Assistant 2026.6 quirk that doubles the slug when a zone device shares its area name (`select.lounge_lounge_heating_circuit`). Home Assistant made the doubling configurable in 2026.6 rather than fixing it outright, and the default still doubles, so the pins are still doing real work today. This stays on the list for whenever that changes, or for a decision to drop them anyway and accept the doubled slug on a fresh install.

No timeline yet.
