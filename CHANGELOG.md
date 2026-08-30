# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).


## [4.4.0] - 2026-08-30

### Bug fixes

- **Every entity went unavailable for hours when the daily API quota ran low, even with HomeKit connected** ([#340](https://github.com/hiall-fyi/tado_ce/issues/340) - @nicohabets) — Tado CE pauses cloud polling before the quota fully runs out, keeping a few calls in reserve for manual actions like setting a temperature. That pause already kept every entity live when it was triggered by Tado rejecting a request outright, but a separate guard trips first, before the quota is actually exhausted, and always dropped every zone, hub and sensor to unavailable regardless of whether HomeKit local control was connected and could have kept them updating. It now checks the same way the other guard already did, so a connected bridge keeps entities live through the whole reserve window instead of only after Tado starts rejecting requests.
- **Upgrading to Home Assistant 2026.9 no longer breaks the integration** ([#338](https://github.com/hiall-fyi/tado_ce/issues/338) - @EDelsman) — The Home Assistant 2026.9 beta removes a device-registry field the integration used to group every zone's entities under its device. Without this fix, every thermostat, sensor and other zone entity would have gone unavailable the moment you updated to it. Tado CE now checks what your installed Home Assistant supports and adapts automatically, so this works whichever way 2026.9 ships, and on everything back to Home Assistant 2025.11, the oldest version it still supports. There's nothing for you to do.
- **A bridge that drops the connection partway through pairing, not a factory reset, could spin the CPU with no backoff at all** ([#322](https://github.com/hiall-fyi/tado_ce/issues/322) - @bobbinz) — the earlier #322 fix stopped the spin that follows a factory reset, but a bridge resetting the connection mid-handshake for any other reason hit a different gap: the underlying HomeKit library retried several times a second with zero delay, invisible to Tado CE's own reconnect logic since the connection never got far enough to report a failure. It now detects that pattern from the connection's own behaviour and interrupts it with a proper backoff, leaving the factory-reset case the first fix already covers untouched.
- **Saving a zone's Options form for any reason no longer locks in whatever Minimum/Maximum Temperature or Window Type it happened to be showing** — Opening that form to change one thing, like binding an external sensor, and hitting Save stamped every other field on the page as if you'd deliberately set it, at whatever value it was already displaying. For an AC zone that can reach 30°C but has never had its own Max Temperature set, the next unrelated save quietly capped it back to 25°C, the same ceiling issue #180 fixed. The form now only saves a field when you've actually changed it. If a zone already picked up one of these stamped values before this fix, it keeps that value; re-open the form, set the field to what you actually want, and it saves correctly from there.
- **A zone with no Window Type of its own now reads the global Window Type under Advanced Settings, instead of always assuming double-pane** ([#337](https://github.com/hiall-fyi/tado_ce/issues/337) - @davidjirovec) — Surface Temperature, Mold Risk and Condensation Risk all quietly used double-pane glazing (U=2.7) for a zone with no per-zone Window Type set, whatever the global Window Type under Smart Comfort was set to, so changing it to triple-pane never reached a zone relying on it. They now read the global value instead, and a per-zone override still wins over it as before. The Options-form stamping bug above no longer freezes it either, once you save that zone's form for an unrelated reason.
- **Removing a zone no longer resets every zone's settings** — Take a radiator off your Tado account, or delete a zone in the Tado app, and the integration tidied its saved per-zone settings to match. It tidied too much: the file was emptied rather than trimmed, so Smart Valve Control mode, the external sensor you had bound, adaptive preheat, temperature limits and every other per-zone setting went back to defaults, across all your zones and not just the one that left. Nothing looked wrong at the time, because the running integration carried on using what it already had in memory. The settings only disappeared at the next restart, which could be days later, so there was nothing to connect them to a zone you removed. Settings belonging to a zone that no longer exists are now left alone; nothing reads them, so they cost you nothing.
- **A zone that replaces a deleted one no longer inherits its old schedule, Smart Valve Control setup or HomeKit control** — Take a zone off your Tado account and add a new one, and Tado can quietly reuse the same internal ID for the zone. The integration had no way to tell the two apart, so the new zone silently picked up whatever the old one had configured: its cached schedule, its Smart Valve Control or Offset Sync setup including any external sensor you'd bound to it, and its HomeKit device mapping. It now recognises when a zone's internal ID has changed hands and clears all of that, so a replacement zone starts with a clean slate instead of someone else's settings.
- **Turning a zone back on from Off with HomeKit connected could silently do nothing** ([#335](https://github.com/hiall-fyi/tado_ce/issues/335) - @motoko-bv) — Setting a new temperature tried the HomeKit bridge first and only fell back to Tado's cloud if that failed. A HomeKit setpoint isn't a power switch though: writing it to a thermostat that's off changes the stored number without turning the zone on, so the bridge reported success while Tado never created an overlay, and the card reverted a few minutes later once nothing confirmed the change. Turning a zone on from Off, whether by setting a temperature or switching its mode to Heat, now always goes through the cloud, the only write that can both turn it on and carry Tado's overlay settings.
- **Pressing Heat on an off zone could send the wrong target** — Tado's frost-protection temperature (5°C) shows on the card while a zone sits off under a manual override, and asking to heat from there is meant to fall back to a sensible 20°C rather than send that placeholder on. The check for whether the card was showing the frost placeholder only looked at the number itself, so a zone following its schedule to a genuine 5°C target, or an off zone under Schedule or Away rather than a manual override, got silently bumped to 20°C too. It now checks the zone's actual state instead of the number, so only a real frost-protection Off gets the 20°C fallback.
- **Schedule Calendar now works on the free API tier** — Enabling it on the 100-call tier produced no calendar entities at all, and never would. Before fetching a schedule it checked what quota was left against a threshold that, on that tier, is the whole day's allowance, so it always concluded quota was too low, even first thing after a reset. The warning it logged then promised an entity once the quota reset, which could never arrive. It now works to a call budget: it spends what the day can spare, up to a quarter of the allowance in one go, and leaves any zones it doesn't reach for the next restart or for that zone's Refresh Schedule button. Paid tiers were unaffected.
- **Smart Valve Control's Valve Target mode now works without Schedule Calendar enabled** — it reads the zone's Tado schedule to know what target to hold, and Schedule Calendar was the only thing that ever fetched one. On a calendar-off install, a Valve Target zone was evaluated every poll and never activated; where it did activate (via a manual overlay), the moment that overlay cleared it lost its only source of a schedule and could never reactivate. The schedule now fetches automatically for any zone using Valve Target, on the ordinary polling cycle, independent of Schedule Calendar. Offset Sync mode doesn't read the schedule and was never affected.
- **A zone that changes from a radiator to an AC unit no longer keeps its old schedule** — the cached schedule from before the change could otherwise be read as if it belonged to the zone's new type.
- **A zone type change from radiator to AC (or back) could still slip through if Home Assistant restarted at exactly the wrong moment** — detecting it relies on remembering each zone's last-known type, and that memory only lived in the running integration, not on disk. A restart between the change happening and the next check meant the new type looked like it had always been that way, so the old schedule never got cleared. That memory is now saved as it updates, closing the gap.
- **Offset Sync keeps correcting the device offset while a zone is off** ([#266](https://github.com/hiall-fyi/tado_ce/issues/266) - @Thilas) — Turn a zone off, or leave your whole system off over summer, and Offset Sync stopped correcting the device offset until the zone came back on, so the Tado app's room temperature reading could drift away from your external sensor's in the meantime. There's no heating decision for the offset to affect while a zone is off, but the app's display still needs it, so Offset Sync now keeps it up to date whatever the zone's power state.
- **A hot-water tank that changed hardware kept showing its old capabilities.** Swapping a boiler or updating its firmware can change whether a tank accepts a target temperature, but Tado CE only watched AC zones for that kind of change. A hot-water tank that gained or lost temperature control carried on presenting the old way (an on/off-only tank still showed as on/off-only after a swap that added a temperature dial, for instance), and on a home with no AC zone there was no button to put it right either. Hot-water zones are now watched the same way AC zones are, so a hardware or firmware change is picked up and the capabilities are re-fetched automatically.
- **The Target sensor and the climate card could disagree about an off zone's target** — with a zone off under a manual override, the climate card correctly shows the frost-protection temperature (5°C), matching the Tado app, but the Target sensor for that same zone showed unknown, since it never checked why the zone was off. It now shows the frost temperature too, so a template or automation reading either surface sees the same number. A zone that's off under its schedule or Away mode is unaffected: the sensor still reports unknown there, since the card's own value in that case isn't reliable enough yet to mirror.
- **The Refresh Schedule button works without Schedule Calendar enabled, and now appears for AC zones too** — it depended on the same toggle its own fetch never needed. Every heating and AC zone now gets one.
- **HomeKit pairing now tells you what actually went wrong** ([#334](https://github.com/hiall-fyi/tado_ce/issues/334) - @BirbByte) — The two likeliest first-time failures, no bridge found on the network and a bridge that is still paired to something else, both came out as "Pairing failed, please try again". The integration knew which one it was and had a specific message for each, and the form was throwing them away. You now get the real reason, so "reset the bridge" and "check the bridge is reachable" stop looking like the same problem.
- **Add Meter Reading, Identify Device and Activate Open Window could demand a re-authenticate for no reason** — if your access token happened to expire right as one of those went out, Tado CE cleared it and immediately asked you to log in again, even though a fresh token was already on its way for the next request. Repeating those three isn't safe, so they still won't retry automatically, but an expired token doesn't need to force reauth either. They now report that the request didn't go through and ask you to try again; a genuinely bad token, not just one due for a refresh, still prompts you to re-authenticate as before.

### Improvements

- **`tado_ce.refresh` can now force an immediate zone-state fetch** — the service already let you force mobile-device presence or home presence data past their normal cache window; it now has a third option, Zone states, for the actual temperature, target and mode data your cards show. With HomeKit connected, Tado CE can skip the cloud poll for a while to save API calls if HomeKit's own reading is fresh enough, so a card can lag behind a change made outside Home Assistant (the Tado app, or the thermostat itself) for longer than you'd like. Before this the only way to force a catch-up was a full integration reload.

## [4.3.3] - 2026-08-16

### Bug fixes

- **Turning a zone off now shows 5°C straight away, as one change instead of two** ([#332](https://github.com/hiall-fyi/tado_ce/issues/332) - @davidjirovec) — Switch a heating zone off and the card said "off" while still showing the old target, then jumped to 5°C by itself a few seconds later. Both now arrive together. Beyond looking odd, that second jump was indistinguishable from someone turning the dial by hand, so an automation watching for manual changes could take the 5°C as your new preference and leave the room cold when it should have warmed back up. Local control through a bridge did the same and is fixed too.
- **Turning a zone back to Heat no longer sets it to 5°C** — Because an off zone shows the 5°C frost target, switching it back to Heat without naming a temperature took that 5°C as the new setting, so the room read as on and never warmed up. It now falls back to 20°C, the same default a fresh install starts from. A 5°C you chose yourself while in Heat is left alone.
- **A zone turned on no longer reads as off underneath** — the mode changed on the card straight away while the heating indicator beside it stayed "off", until Tado caught up, which on a quiet system is a long wait. That happened both when switching a zone to Heat and when setting a timer on it, and on an AC zone the mode itself stayed off although Tado had already been told to turn on. They now match the write.
- **An off zone no longer shows a stale radiator target after a restart** — with a bridge paired, a zone that was off could show an old target read from the radiator, 16°C say, while Tado was actually holding frost protection at 5°C, flipping between the two every couple of minutes and settling on the wrong one. It lasted about five minutes after each Home Assistant restart or integration reload. Changing a target in the Tado app still reaches the card in seconds, as before.
- **A temperature change that fails no longer leaves the failed number on the card** — When Tado rejected a temperature change or the request timed out, the card kept showing the value you had tried to set until the next refresh, rather than the one still in force.

### Improvements

- **A timer's temperature reaches the card with the write** — `tado_ce.set_climate_timer` set the new target on Tado but left the previous one on the card until Tado's next refresh, which on a quiet system is a long wait. The timer itself was working; only the number shown was stale. Heating and AC zones both update straight away now. An AC mode that carries no target of its own, Fan for one, is unchanged, since there is no temperature to show.
- **Refresh Schedule button: what the guide said about it was wrong** — The feature guide offered the button as the impatient option, "rather than waiting for the next sync". There is no next sync. A zone's schedule is read once, when its calendar entity is first set up, and cached from then on, so a schedule you edit in the Tado app reaches Home Assistant only when you press this button. The guide now says so, and names what else goes stale until you do: the calendar entity, the `scheduled_target_temperature` attribute, and the target shown for a zone that is off.
- **Resuming the schedule still updates the target one step behind** — switching a zone to Auto clears the override, and the new target still turns up a few seconds after the mode, the way Off used to. It is not fixed here, and the difference is real rather than an oversight: with Off the answer is always 5°C so it can be stated up front, while after a resume the target is whatever your schedule says at that moment and only Tado knows that. Getting it right needs the integration's copy of your schedule kept up to date first, which is its own piece of work.

## [4.3.2] - 2026-08-12

### Bug fixes

- **A temperature you set no longer springs back a few seconds later** ([#330](https://github.com/hiall-fyi/tado_ce/pull/330) - @Flavien) — With a HomeKit bridge paired, changing a zone's target temperature could show the new value, then quietly revert to the old one moments afterwards, and the change never reached the Tado app either. The bridge keeps reporting the previous target for a short while after a write, and on this one path the integration was accepting that stale reading back over the value you had just set. There is already a three-minute window that tells it to trust your own change over anything the bridge echoes; the heating path never opened that window, and the air-conditioning path only opened it for changes that went via the cloud. Both now do. If you have been nudging a radiator up half a degree at a time and watching it slide back, that is this.
- **Switching a zone to Heat or Off shows straight away instead of after twenty seconds** ([#330](https://github.com/hiall-fyi/tado_ce/pull/330) - @Flavien) — With a bridge paired, changing the mode wrote to the radiator immediately but left the card showing the old mode until a follow-up check with Tado's cloud came back, about twenty seconds later. Temperature changes have always updated the card on the spot; mode changes now do the same. Zones following your schedule are unaffected, because "follow the schedule" is not something the bridge can express, so those still go via the cloud as before.
- **A Smart Valve Control zone no longer jumps to the valve figure the moment it compensates** — Smart Valve Control works by deliberately over-driving the radiator valve, so on a zone using it the card could jump to a number well above the one you asked for, 26°C against a setting of 21°C for instance, within seconds of a compensation write. That early jump is gone: the card holds the temperature you set until Tado itself reports the change. Note the card does still follow the valve figure once Tado's own data catches up a poll later, because Valve Target genuinely sets the zone's overlay to that temperature and the integration shows what Tado reports. If you want your own target on a dashboard, the `desired_target` attribute carries it, and the Smart Valve Control section of the feature guide has a template sensor for it.

## [4.3.1] - 2026-08-07

### Works on Home Assistant 2025.11 and newer, same as v4.3.0

No minimum-version change in this release, and none was ever needed.

The first build of v4.3.1 did require Home Assistant 2026.8, because of how it fixed the HomeKit clash below. That requirement is gone: the fix now works on everything from 2025.11 on. This note also claimed HACS would refuse to install v4.3.1 on an older Home Assistant, so you couldn't land on it by accident. That part was simply wrong. HACS offers it regardless of your version, and the check meant to stop the download usually has no version to compare against, so it goes ahead.

If you got the first build on a Home Assistant older than 2026.8 and the integration stopped loading, this replaces it: in HACS, open Tado CE, then ⋮ → Redownload → v4.3.1, and restart Home Assistant. Nothing was lost, and going back to v4.3.0 was always safe too.

### Still worth knowing: three things change at v5.0.0

Repeated from v4.3.0 because the release is closer now, with one correction and one addition. Nothing changes for you in this release.

**You'll need to be on v4.1.0 or later before installing v5.0.0.** This is the new one, and it's the only item here that can stop an upgrade. v5.0.0 drops the code that reads the pre-v4.1 data format, so it refuses to load if it finds settings older than that and tells you what to do instead of failing quietly. If you're on v4.1.0 or later, which almost everyone is, there is nothing to do. If you're on v3.x or v4.0.x, install v4.3.x first (your settings carry over automatically), then move to v5.0.0.

**The Predicted Window sensor is slated for retirement.** The `binary_sensor.*_window_predicted` sensor stays exactly as it is here. It drives display and insights only, never an actual heating decision, and getting its accuracy right has taken a lot of tweaking for what it gives back, so the plan is to retire it and put that effort into the core features more people rely on. If you lean on it in a dashboard or automation, raise it before v5.0.0 and it can be reconsidered.

**The Download Diagnostics button goes too.** v5.0.0 removes the **Settings → Download Diagnostics** button (the config-and-state snapshot). Sorting out an issue always comes down to a debug log, which shows what happened over time, where the snapshot only captures one frozen moment. It also carried an ongoing privacy cost, a redaction list to keep current so it never leaked a serial or token. No automation breaks and nothing you script against changes, the button just won't be there.

**Correction to the v4.3.0 note.** That note said the boiler diagnostic sensors' `bridge_` entity IDs would be renamed to `boiler_` at v5.0.0. That was wrong, and it's worth correcting because it would have read as a breaking change you needed to prepare for. Home Assistant derives an entity ID once when the entity is first created, from the name shown in your own language, and it belongs to you after that, so the integration neither sets it nor renames it. What v5.0.0 actually tidies is the internal ID these sensors use behind the scenes, which nothing you write in an automation ever refers to. Your entity IDs stay exactly as they are, and if you renamed any of them yourself, that stays too.

The full v5.0.0 list is in [ROADMAP.md](ROADMAP.md).

### Bug fixes

- **HomeKit Controller works again alongside Tado CE on Home Assistant 2026.8** ([#327](https://github.com/hiall-fyi/tado_ce/issues/327) - @jaimievansanten) — Home Assistant 2026.8 moved its built-in HomeKit Controller onto a newer version of the shared HomeKit library, while Tado CE still asked for the older one. Only one version can be installed at a time, so HomeKit Controller failed to start and every HomeKit device paired through it went unavailable, including bridges and accessories with nothing to do with Tado. Tado CE now accepts either version rather than asking for a specific one, so it uses whichever your Home Assistant ships and there is nothing left for the two to disagree about. Tado CE's own HomeKit local control was never affected, which is why this could go unnoticed on a setup that only pairs the tado bridge.
- **Installing on a Home Assistant older than 2026.8 no longer breaks the integration** ([#329](https://github.com/hiall-fyi/tado_ce/issues/329) - @Prodeguerriero) — The first build of the HomeKit fix above asked for the exact library version Home Assistant 2026.8 ships. That sorted 2026.8 and created the mirror-image problem everywhere below it, back to 2025.11, where Home Assistant wants the older library and Tado CE was then the side insisting on the newer one. The integration failed to load after a restart. Accepting either version fixes both directions, so there is nothing left to be incompatible with.

## [4.3.0] - 2026-08-01

### Heads-up: two things go away in v5.0.0

Nothing changes for you in this release, but two removals are worth flagging early so anyone who relies on them has time.

**The Predicted Window sensor is slated for retirement.** The `binary_sensor.*_window_predicted` sensor stays exactly as it is in this release. It's on the shortlist to be removed at v5.0.0 rather than kept on tuning: it drives display and insights only, never an actual heating decision, and getting its accuracy right has taken a lot of tweaking for what it gives back. The plan is to retire it and put that effort into the core features that more people rely on. If you lean on it in a dashboard or automation, raise it before v5.0.0 and it can be reconsidered.

**The Download Diagnostics button goes too.** v5.0.0 will remove the **Settings → Download Diagnostics** button (the config-and-state snapshot). In practice it hasn't earned its keep: sorting out an issue always comes down to a debug log, which shows what happened over time, where the snapshot only captures one frozen moment and can't. It also carried an ongoing privacy cost, a redaction list to keep current so it never leaked a serial or token. No automation breaks and nothing you script against changes, the button just won't be there; for any problem, a debug log is the thing to grab.

The full v5.0.0 removal list is in [ROADMAP.md](ROADMAP.md).

### Features

- **New `tado_ce.refresh` service to force a presence fetch** ([#303](https://github.com/hiall-fyi/tado_ce/issues/303) - @davidjirovec). Presence data (mobile devices, home/away) refreshes on a minimum interval to protect your API quota, which also floors how fast an automation can force it. The new service forces an immediate fetch of one of those, bypassing that floor for the one call, for anyone driving their own refresh cadence rather than relying on the automatic schedule. The automatic polling floor still applies the rest of the time.
- **Structured issue report forms**. Reporting a bug or asking for a feature now goes through GitHub's form chooser instead of a blank box, so the details that speed up a diagnosis (version, Home Assistant version, logs) get asked for up front. The in-app "report an issue" links point at the chooser too.

### Improvements

- **Outdoor temperature binding moved to its own "Outdoor Sensors" setting**. The outdoor temperature entity used to sit under Smart Comfort, so reaching it meant turning on a feature you might not want. It now lives in its own always-visible section under Advanced Settings, so any feature that reads outdoor temperature (Smart Comfort, mold risk) can reach it without switching on something unrelated. Your existing binding carries over untouched.
- **General Settings toggles regrouped**. The Tado Features toggles were listed in the order they were added, mixing presence, data, and view options together. They're now grouped by what they do: presence first, then data, then view. Weather Compensation moves to the end of Smart Automations since it needs the Internet Bridge first. Display order only, nothing behaves differently.

### Bug fixes

- **Custom polling interval can be cleared again**. Emptying the custom day or night interval box used to re-fill the old value on save, so the only way back to automatic timing was to type 0. Clearing the box now returns that interval to automatic as you'd expect.
- **AC heating/cooling status reads correctly on older firmware**. On AC units running older Tado firmware, the climate entity's action (Heating / Cooling / Idle) could show wrong because the power reading came in a shape the entity didn't recognise. It now reads both firmware shapes, so the action matches what the unit is actually doing.
- **The open-window detection count now actually counts**. The "detections today" figure on the open-window sensor was stuck at 0 and its last-detected time never updated, because the counter was checked a moment after the sensor had already marked itself detected, so the check never saw the change. Both now update on each detection.
- **HomeKit local control recovers on its own after a bridge firmware update or reconfigure**. When the tado bridge updated its firmware or rearranged its devices without dropping the connection, local control could quietly fall back to cloud (slower reactions, temperature offsets not applying straight away) and stay there until you reloaded the integration. It now spots the change and rebuilds its local device map on its own, so control comes back without a reload. It also copes with the bridge being briefly unreachable while it reboots, which is exactly when a firmware update rebuilds the map: it holds on to the working map and retries instead of being left with nothing to poll.
- **Re-pairing HomeKit brings local control straight back**. After the bridge lost its pairing (most often a factory reset), entering a new setup code saved the pairing and cleared the repair notification, but local control stayed on cloud until you restarted Home Assistant. Re-pairing now reconnects on its own once the code is accepted.
- **The HomeKit repair notification points at the right setting**. When the bridge pairing was lost, the notification sent you to General Settings to switch HomeKit on, which does nothing when it is already on, so there was no way to reach the setup code prompt by following it. It now points at Advanced Settings → HomeKit → "Pair again", which is the option that actually reopens the prompt.
- **A factory-reset bridge no longer churns the CPU** ([#322](https://github.com/hiall-fyi/tado_ce/issues/322) - @bobbinz, @onorbe). After the bridge was reset, Tado CE kept holding the old pairing, so every time the bridge announced itself on the network it tried to authenticate again and failed a second or two later, over and over, with a visible CPU cost. It now releases the dead pairing properly, so the retries stop and you get the repair notification instead.
- **Room temperature no longer shows a stale HomeKit reading as a cloud one**. On any home with the bridge paired, the temperature merge wrote its result back over the cloud reading it had just read, so the real cloud value was lost for that cycle. If you had set a room to read temperature from the cloud, it could show a HomeKit value labelled as cloud, and a stale one at that. The merge now keeps to itself and the cloud reading stays intact.
- **AC temperature changes sent over HomeKit are now checked against the cloud**. Heating zones already re-checked with Tado shortly after a local write, so a change the bridge accepted but never passed on would correct itself. AC zones did not, so a lost write could sit on screen as if it had worked until the next scheduled refresh. AC now does the same check.

## [4.2.1] - 2026-07-25

### Bug fixes

- **HomeKit local control now maps every room after a re-pair** ([#322](https://github.com/hiall-fyi/tado_ce/issues/322) - @bobbinz) — after re-pairing the bridge, some rooms could stay on cloud control while others went local, and it never sorted itself out. The bridge hands its radiators over one at a time, so a moment after pairing only some were visible, and the mapping stopped as soon as the first room matched instead of carrying on until every room was covered. It now keeps filling in rooms until they are all mapped, so a re-pair brings the whole home back to local control on its own.
- **Clearer HomeKit pairing error when another controller has the bridge** ([#322](https://github.com/hiall-fyi/tado_ce/issues/322) - @onorbe) — a pairing attempt that failed because Homebridge, Apple Home, or Home Assistant's own HomeKit was already holding the bridge showed a plain "incorrect PIN", pointing you at a code that was actually fine. The message now names the other likely cause, so you know to stop the competing controller and try again.
- **HomeKit pairing no longer tries to pair a stray bulb** ([#322](https://github.com/hiall-fyi/tado_ce/issues/322) - @bobbinz) — the pairing scan picked the first unpaired HomeKit device it saw on the network, without checking it was actually a tado bridge. On a home with unpaired HomeKit bulbs (or any other loose HomeKit accessory), it could send the tado code to a bulb, which turned it away as a wrong code even though the code was right. It now looks specifically for the tado bridge and walks past everything else.
- **Window detection counters survive a reload** — removing or reloading the integration dropped that day's open-window detection counts, so they reset instead of carrying over. They are now saved cleanly on the way out.

## [4.2.0] - 2026-07-16

### ⚠️ Heads-up: a legacy-cleanup release (v5.0.0) is on the horizon

Nothing changes for you in this release. But v5.0.0 will be a spring-clean that drops backward-compat code built up over the v3.x and v4.x cycles, and a few of those removals are worth knowing about early because they affect how you call services or which entity IDs you script against. Two to plan around:

- **Old swing values.** Automations calling `climate.set_swing_mode` with the old unified values (`off` / `vertical` / `horizontal` / `both`) have logged a deprecation warning since v4.1.0 and still work today, but the compatibility shim is removed at v5.0.0. Move them to the per-axis services (`climate.set_swing_horizontal_mode` and `climate.set_swing_vertical_mode`) before then. If your swing automations don't log a deprecation warning, they're already on the new services and need no change.
- **Boiler sensor entity IDs.** v4.1.4 renamed the boiler diagnostic sensors' display names from "Bridge …" to "Boiler …", but their entity IDs still carry the old `bridge_` wording. v5.0.0 aligns the IDs to `boiler_` with a migration step, so anything scripting against the old IDs will want updating around then. *(Corrected in v4.3.1: this turned out to be wrong. Entity IDs are yours, not the integration's, and v5.0.0 does not touch them. Nothing to update. See the v4.3.1 entry.)*

There's no date for v5.0.0 yet. The full list of planned removals (option-key migration, entity-naming polish, dead-code sweep, service-call consistency, and more) is in [ROADMAP.md](ROADMAP.md) under "v5.0.0 — Legacy cleanup", worth a read if you have automations or dashboards built on Tado CE.

### Features

- **Heating circuit control** ([#316](https://github.com/hiall-fyi/tado_ce/issues/316) - @rustyd0g) — a new per-zone select to assign a heating zone to a boiler circuit, or to "No heating circuit". Setting a zone to no circuit lets its radiator valves still open to soak up residual heat whenever the boiler is already running for another room, without that room ever firing the boiler itself, which is a genuine energy lever on a multi-circuit system. It's off by default; turn on **Heating Circuit Control** under Settings, and each heating zone gets the select (single-circuit homes get it too, for the no-circuit option). The circuit assignment reads back on the normal sync cadence rather than instantly, and it's fetched only when you enable the option, so it adds no polling for anyone who doesn't use it. Needs a bridge with boiler circuits. If you use the [Pulse Climate Card](https://github.com/hiall-fyi/pulse-card), version 1.9.0 adds an opt-in `heating_circuit` chip that shows a zone's circuit at a glance.

### Bug fixes

- **Hot-room comfort insights show up in summer** — a room getting too hot only raised a comfort insight while its heating was on, so all summer (heating off) a room could sit at 30°C or more and the insight list stayed silent, even though the Comfort Level sensor already read "Sweltering". The hot-side insight now fires whether or not heating is on. The cold-side one still waits for heating to be on, since a room that sits well below its target with the heating off is already flagged by the "heating off, room cold" insight. On air-conditioning zones the advice now points at the AC (lower the setpoint, turn on cooling) rather than telling you to reduce heating.
- **A dangerously hot room now outranks a low battery** — comfort insights all carried the same medium priority, so a room hot enough to be a real health risk sat level with, and below, a routine low-battery warning in the insight list. Heat now takes its priority from the feels-like temperature on the standard heat-index scale: the "Danger" band (heat cramps or exhaustion likely) is high priority, "Extreme Danger" (heat stroke likely) is critical, and milder heat stays medium. The priority and the recommendation text now come from the same feels-like reading, so a critical heat insight also carries the "feels like X°C" warning.
- **Hot water remembers your chosen temperature** ([#317](https://github.com/hiall-fyi/tado_ce/issues/317) - @Siiya27) — on a temperature-controlled tank, switching to Heat after the water had been off jumped to the tank's maximum instead of the temperature you last set. The target was dropped each time the tank went off, and again on a restart, so there was nothing left to resume to. It's now kept through both, so Heat comes back at your value (say 52°C) rather than the maximum. The temperature control also appears as soon as the tank supports it, instead of only after the next poll catches it on.

### Improvements

- **Heat and cold alerts for vulnerable occupants** — a new option under Advanced Settings, in its own Comfort & Safety section. For homes with elderly, infant, or unwell occupants, it raises both heat and cold warnings one level earlier, since the same temperature carries more risk for them. Cold alerts step up when a room with the heating off drops into a health-risk range (high below 16°C, critical below 12°C). Off by default.

## [4.1.4] - 2026-07-11

### Bug fixes

- **Phone trackers are grouped under the Tado CE device again** ([#314](https://github.com/hiall-fyi/tado_ce/issues/314) - @arnoslag) — v4.1.3 got the trackers reporting home / away again, but they sat loose in the entity list instead of under the Tado CE device. The tracker type introduced in v4.1.2 doesn't create a device entry at all. They've moved back to the tracker type that does, so each phone shows under the device as it did in v4.1.1, while keeping the Home Assistant compatibility that prompted the original change. Presence (home / not_home) and the geofencing sensor are unchanged.
- **Bridge connected sensor now works on setups with no boiler** ([#275](https://github.com/hiall-fyi/tado_ce/issues/275) - @50494554524F) — the sensor read the bridge's online flag from the boiler-wiring response, which is empty when nothing is wired to the bridge (a radiator-only home). On those setups it showed Unknown and never updated. It now falls back to reading the bridge's own connection status from the home device list, so the on / off state works whether or not a boiler is wired. Setups with a boiler or heat pump were already correct and are unaffected.
- **Hot water switches to Heat again on temperature-controlled tanks** ([#317](https://github.com/hiall-fyi/tado_ce/issues/317) - @Siiya27) — on hot-water zones that take a target temperature (solar or cylinder systems, not simple on/off combis), switching to Heat or pressing a boost-timer button failed every time the water was already off. Tado rejected the request because the sent payload left the temperature out. It now includes a target with the on request (your last-known one, or the tank's maximum if there isn't one), so Heat and the timer work again. On/off-only combi tanks are unaffected and still send a plain on with no temperature.

### Improvements

- **Boiler diagnostic sensors renamed from "Bridge …" to "Boiler …"** ([#275](https://github.com/hiall-fyi/tado_ce/issues/275) - @50494554524F) — the sensors fed by the bridge's boiler-wiring data (wiring state, flow and output temperature, the wired-device details) were all prefixed "Bridge", which read as if they were about the bridge itself rather than the boiler. They now read "Boiler Wiring State", "Boiler Flow Temperature", and so on. Display names only, so automations and dashboards keep working; the entity IDs are unchanged.

## [4.1.3] - 2026-07-08

### Bug fixes

- **Bridge connected sensor now reflects the bridge, not the cloud** ([#275](https://github.com/hiall-fyi/tado_ce/issues/275) - @50494554524F) — the "Bridge connected" sensor read whether the last call to Tado's cloud succeeded, not whether the bridge itself was online. Because that call is cloud-hosted, an offline bridge still got a reply and the sensor stayed on "connected" indefinitely, even across a reload. It now reads the bridge's own online status from Tado's response. Its on/off state tracks the bridge; if the Tado cloud itself becomes unreachable, the sensor shows unavailable rather than a stale reading. Only affects setups using the bridge connection with a serial and auth key.
- **HomeKit pairing recovers from a wrong code** ([#313](https://github.com/hiall-fyi/tado_ce/issues/313) - @Siiya27) — entering the wrong code on the first pairing attempt (for example the 4-digit auth code instead of the 8-digit setup code) left HomeKit switched on but never actually paired, with no way back to the code prompt short of disabling and re-enabling it. Two fixes: the setup code is now checked for the right format before pairing is attempted, with a message naming the code you need, and a new "Pair again" option in the HomeKit settings reopens the code prompt directly so you can recover without the toggle dance.
- **Mobile presence trackers no longer show unavailable** ([#314](https://github.com/hiall-fyi/tado_ce/issues/314) - @arnoslag) — a regression in v4.1.2: after the move to Home Assistant's connected/disconnected tracker style, the per-user phone trackers (`device_tracker.anne` and the like) all read "unavailable", while the home-wide presence sensor stayed correct. The new tracker type keys its identity on a hardware address, which Tado phones don't have, so each tracker registered with no identity and Home Assistant couldn't attach or restore it. The trackers now keep their own stable identity and report home / not_home per user as before. Downgrading to v4.1.1 was the workaround; this removes the need.
- **Sensors show unavailable instead of a stale reading when their own data goes away** — a handful of sensors could keep displaying their last value as if it were current after the thing behind them disappeared. The battery, connection and child-lock entities for a TRV you've removed from your account held the old reading, and the outside temperature, solar and weather-state sensors kept their last value when a poll came back without weather data. These now report unavailable and drop the stale reading. Individual thermal-model sensors (heating rate, inertia, and so on) also go unavailable when a poll fails, rather than showing a value from before the failure.


## [4.1.2] - 2026-07-05

### Bug fixes

- **Device Temperature Offsets show up with the toggle alone** ([#311](https://github.com/hiall-fyi/tado_ce/discussions/311) - @janchrillesen) — a question on this thread turned up a latent bug: with Device Temperature Offsets on but no zone running Smart Valve Control or Offset Sync, the offset was never fetched, so `offset_celsius` never appeared for anyone who only wanted to watch it. The offset now follows its own toggle: switch it on and it shows on your heating climate entities, and it keeps re-reading on the usual cadence instead of freezing at the boot value. The earlier restriction was meant to skip a pointless fetch when nothing used the offset, but the display itself is what uses it.
- **Bridge connection drops surface faster** — when the bridge API becomes unreachable (a network drop, or the Tado cloud rejecting the bridge call), the Bridge connected sensor now updates on the bridge's own polling cycle instead of waiting for the next cloud poll to come round. Only affects setups using the bridge connection with a serial and auth key.

### Home Assistant compatibility

- **Mobile presence trackers ready for Home Assistant 2027.7** — Home Assistant is retiring the old way location-based trackers report their state, and logged a deprecation warning for the Tado phone presence trackers on 2026.7.1. They've been moved to the connected/disconnected style that fits how Tado actually reports presence (a plain home / away flag, since Tado never gives out phone coordinates). The tracker state (`home` / `not_home`) and its attributes are unchanged; the one visible difference is the `source_type` attribute now reads `router` instead of `gps`, which is the honest label for a presence flag rather than a GPS fix. If you have an automation or template keyed on `source_type: gps` for a Tado tracker, update it to `router`.


## [4.1.1] - 2026-06-27

### Bug fixes

- **Integration no longer fails to load when HomeKit can't start** ([#308](https://github.com/hiall-fyi/tado_ce/issues/308) - @wisskid) — if you had HomeKit local control switched on but your Home Assistant host has no zeroconf service running, the whole integration crashed at startup and even the cloud side stopped working. HomeKit local control is optional, so it now quietly falls back to cloud-only and the integration loads normally. The same protection now covers a bridge that drops out while HomeKit is connecting, during startup or on a later refresh, so a dropped bridge falls back to cloud instead of taking the integration down with it. If you want local control, the fix is to add `default_config:` (or `zeroconf:`) to your `configuration.yaml` and restart.
- **Clearer pairing error when zeroconf is missing** ([#308](https://github.com/hiall-fyi/tado_ce/issues/308) - @wisskid) — trying to pair the HomeKit bridge on a host with no zeroconf service used to show a generic "pairing failed". It now names the actual cause and tells you to add `default_config:` (or `zeroconf:`) and restart.


## [4.1.0] - 2026-06-23

**AC swing axes, automation-friendly Smart Valve writes, temperature source control, HomeKit reliability**

Upgrading from v4.0.3: HACS auto-update, no config changes needed. All per-zone settings, HomeKit pairings, and sensor history carry forward. If you have automations or templates that read `swing_mode`, see the Migration section below.

### Features

- **Split AC swing into vertical and horizontal axes** ([#270](https://github.com/hiall-fyi/tado_ce/issues/270) - @Ralf84) — AC zones now expose a `Swing (vertical)` and a `Swing (horizontal)` dropdown, each populated from the cloud-reported capability set for your specific unit. Pick a fixed louver position on either dropdown to stop oscillation on that axis — useful in bedrooms or children's rooms where a constantly moving louver is disruptive. Simple `On / Off` units keep a two-value dropdown. Fine-grained positions (`UP`, `MID_DOWN`, `LEFT`, `MID_RIGHT`) are exposed directly where the unit reports them, with translations in German, Spanish, French, Italian, Dutch, and Portuguese.
- **Set a zone target from an automation without tripping Smart Valve Control** ([#256](https://github.com/hiall-fyi/tado_ce/issues/256) - @apilone) — the common holiday-automation workaround (raise a zone target on a bridge day) used to look identical to a manual slider grab, so Smart Valve Control backed off and left the room cold. The new `tado_ce.set_schedule_temperature` service marks the write as a scheduled change: the controller keeps compensating towards the new target and hands back to your schedule at the next Tado block. Leave `force_override` off (the default) to respect any manual override you made by hand; turn it on when the automation must take priority regardless.
- **Choose which temperature reading each zone shows** ([#293](https://github.com/hiall-fyi/tado_ce/discussions/293) - @churchofnoise) — a new per-zone Temperature source setting (Configure → Zone Configuration → Temperature). Automatic prefers the fast HomeKit reading when it's fresh and falls back to the cloud; HomeKit always prefers the local reading; Cloud always shows Tado's. An external temperature sensor, if you've set one, still takes precedence. This governs what the dashboard shows only; Smart Valve Control always calibrates against its own reading.
- **Identify a specific TRV or thermostat from Home Assistant** ([#291](https://github.com/hiall-fyi/tado_ce/issues/291) - @bobbinz) — each device now gets its own Identify button that flashes the LED so you can tell which physical radiator is which. Multi-TRV zones get one button per device. It flashes over HomeKit when the bridge is connected (no API call) and falls back to the cloud otherwise. The existing `identify_device` service now covers TRVs and thermostats, not just bridges.

### Improvements

- **Configurable refresh intervals for Home Presence, Weather Data, and Mobile Device Tracking** ([#303](https://github.com/hiall-fyi/tado_ce/issues/303) - @davidjirovec) — the per-type refresh floors (5 min for presence and mobile, 30 min for weather) were previously fixed. They're now configurable in Advanced Settings → Polling & API, each behind its own feature toggle. Defaults stay unchanged. On a 20,000-call plan, dialling presence and mobile down to 1 minute costs around 1,440 calls per device per day and gives tighter geofencing latency for automations.
- **Override duration relabelled to match the Tado app, with three options instead of four** — "Until you resume schedule", "Until next automatic change", and "Timer" replace the old "Tado Default" / "Next Time Block" / "Timer" / "Manual" wording. "Next Time Block" was a silent duplicate since v2.1.0; removing it loses nothing. New installs and new zones now default to "Until you resume schedule". The Timer cap rises from 3 hours to 12 hours, matching the Tado app.
- **Card actions update immediately and roll back on failure** — temperature sliders already worked this way; HVAC mode dropdowns, timer buttons, water-heater on/off, and boost buttons now match. If the cloud rejects or times out, the card reverts with an error toast.
- **AC capabilities refresh themselves after a re-pair or hardware swap** ([Discussion #280](https://github.com/hiall-fyi/tado_ce/discussions/280) - @Trebor87) — when you replaced or re-paired a Smart AC controller, the integration kept showing the old supported modes until you pressed Refresh AC Capabilities by hand. It now watches each zone's device serial and firmware version and re-fetches automatically on a swap or re-pair.
- **Re-pair, hardware swap, and zone changes on the Tado side are detected automatically** ([Discussion #280](https://github.com/hiall-fyi/tado_ce/discussions/280) - @Trebor87) — cache invalidation now triggers on the next quick poll instead of waiting for the next HA restart.
- **Device state sensors stay current** ([#286](https://github.com/hiall-fyi/tado_ce/issues/286) - @Fred224) — Connection, firmware version, and battery sensors used to freeze at boot-time values. They now refresh hourly on paid Auto-Assist tiers and every four hours on the free tier, so a controller dropping its cloud uplink shows up as Disconnected within the cycle.
- **Mold Risk % sensor renamed to Mold Risk Indicator** ([#90](https://github.com/hiall-fyi/tado_ce/issues/90) - @ChrisMarriott38) — the old name read like "percentage of mold risk" when it's actually surface relative humidity. The trailing `%` also caused a slug collision (`sensor.<zone>_mold_risk_2`) on fresh installs. Existing `entity_id`, `unique_id`, and attribute names are unchanged; only the UI label changes.
- **API Reference doc retired** — `API_REFERENCE.md` was mostly internal call-code taxonomy and material already in the Features Guide. The genuinely useful parts (rate-limit reset detection, per-tier polling guidance, API troubleshooting) now live in the Features Guide under Smart Polling and Troubleshooting. If you'd bookmarked the old file, those topics are all in [FEATURES_GUIDE.md](FEATURES_GUIDE.md) now.

### Bug fixes

- **Fan speed changes now reach older AC units** ([#305](https://github.com/hiall-fyi/tado_ce/issues/305) - @stefanzweig1979) — on units whose firmware reports fan options under the older naming (seen on Toshiba, Mitsubishi, and Fujitsu setups), changing the fan mode updated the dashboard but never reached the AC: temperature still applied, fan did not, and the Tado app kept showing the old setting. The integration was sending the fan setting under the wrong field name, which the cloud quietly ignored. It now sends the field the unit expects. Newer units were never affected.
- **Overnight Offset Sync swing fixed** ([#262](https://github.com/hiall-fyi/tado_ce/issues/262) - @simonotter) — with an external temperature sensor on a zone, the offset could drift to the ±10°C cap overnight and lurch back, leaving the room cold by morning. The controller now waits for Tado's room reading to reflect its last write before correcting again, so it can't chase a stale number into the cap.
- **Offset drift refresh no longer bursts every zone at once** ([#277](https://github.com/hiall-fyi/tado_ce/issues/277) - @wrowlands3) — the periodic offset drift refresh used to re-read every climate zone in one cycle, costing eight cloud calls at once on an eight-zone home. It now visits one zone per cycle, rotating through them, for a steady one call per cycle.
- **Presence mode no longer reverts to Home after a restart** ([#302](https://github.com/hiall-fyi/tado_ce/issues/302) - @beltra) — setting presence to Away with Home State Sync off held the change in memory but never wrote it to disk, so a restart brought back the last polled value. Presence changes are now saved immediately and survive a restart whether or not Sync is on.
- **Device Temperature Offsets no longer read every zone on startup when Smart Valve Control is off** ([#304](https://github.com/hiall-fyi/tado_ce/issues/304) - @wrowlands3) — with the display toggle on but SVC off everywhere, the integration was fetching every zone's stored offset on every restart and every hour for data nothing was using. Offset fetches now only run when at least one zone has Smart Valve Control or Offset Sync active.
- **Heating mode and room target no longer flip back and forth on HomeKit + cloud zones** ([#296](https://github.com/hiall-fyi/tado_ce/issues/296) - @apilone) — with both HomeKit and cloud polling active, the mode could oscillate because the integration accepted HomeKit's mode on the same value the cloud derives your schedule onto. HVAC mode now comes from the cloud only; room temperature still follows HomeKit for speed.
- **HomeKit local control now activates as soon as the zone mapping is ready** — when the link between each radiator and its zone built a moment late, local control could stay off until the next restart. It now starts listening the moment the mapping is built.
- **HomeKit local control now recovers automatically when the bridge is unreachable at startup** — if the bridge was offline or not yet reachable when HA started, the integration stayed cloud-only permanently until you reloaded. The reconnect loop now starts immediately on a failed startup connect.
- **A factory-reset bridge now surfaces a repair notice instead of retrying forever** — when the bridge is factory-reset, the stored pairing no longer matches. The integration now stops retrying and raises a Home Assistant Repairs notification pointing you to Settings → Tado CE → Configure to re-pair. Transient failures (bridge temporarily unreachable, network blip) continue to retry as before.
- **Pairing error messages are more specific** — the pairing flow now distinguishes between a wrong PIN, too many attempts, a busy bridge, a bridge that's already paired, and generic network errors, instead of guessing from the error text.
- **Removing the Tado CE integration clears its repair notices** — previously, removing and re-adding could leave stale "re-authenticate" or "quota exceeded" notices in the HA UI. They're now dismissed automatically on removal.
- **`tado_ce_ready` event now reaches boot-time automations** ([#287](https://github.com/hiall-fyi/tado_ce/issues/287) - @Newreader) — the event was firing before automation triggers had registered their listeners on a cold HA start. Anyone with an automation triggered by `tado_ce_ready` saw it never run after a restart, only after a manual reload. Fixed by deferring the fire until HA has fully started.
- **State Restore captures only persist on successful cloud writes** ([#278](https://github.com/hiall-fyi/tado_ce/issues/278) - @Newreader) — previously a snapshot was saved before the cloud write confirmed, so a failed write left a stale entry that could be replayed later. Captures now correspond to confirmed state changes only.
- **Resuming a zone already on schedule no longer wastes an API call** — `restore_previous_state`, the Resume All Schedules button, and setting a hot-water zone back to Auto used to fire a cloud call to clear an overlay that wasn't there. They now skip zones with no active overlay.
- **Setting a zone to Auto now clears an active override** — a zone carrying a timer or "until next automatic change" override used to ignore an Auto request because the mode already looked like Auto. It now clears the override so the zone genuinely returns to its schedule.
- **`set_schedule_temperature` target preserved when Smart Valve Control is already active** — if SVC was compensating toward a target when the service was called, the next evaluation could silently reset it back to the current schedule block. The target is now preserved until the schedule genuinely advances.
- **Advanced Settings no longer errors immediately after saving General Settings** — opening Advanced Settings in the same session as a General Settings save could hit a crash while the integration was still reloading. It now returns an empty page and works normally once the reload finishes.

### ⚠️ Migration

**Service-call automations using the old unified swing value** (`swing_mode: off / vertical / horizontal / both`) keep working with a deprecation warning logged per call. The compat shim is removed in v5.0.0:

```yaml
# Before (v4.0)
- service: climate.set_swing_mode
  data:
    entity_id: climate.tado_ce_living_room
    swing_mode: both

# After (v4.1+)
- service: climate.set_swing_mode
  data:
    entity_id: climate.tado_ce_living_room
    swing_mode: on
- service: climate.set_swing_horizontal_mode
  data:
    entity_id: climate.tado_ce_living_room
    swing_horizontal_mode: on
```

**Templates and Lovelace conditions** reading `state_attr('climate.X', 'swing_mode')` and matching `'both'`, `'vertical'`, or `'horizontal'` need updating. After v4.1 the attribute holds raw values like `'on'`, `'off'`, `'up'`, `'auto'`. Check `swing_mode` and `swing_horizontal_mode` separately:

```jinja
{# Before #}
{% if state_attr('climate.tado_ce_living_room', 'swing_mode') == 'both' %}

{# After #}
{% set v = state_attr('climate.tado_ce_living_room', 'swing_mode') %}
{% set h = state_attr('climate.tado_ce_living_room', 'swing_horizontal_mode') %}
{% if v not in ['off', None] and h not in ['off', None] %}
```

## [4.0.3] - 2026-06-10

A small stable patch, all bug fixes surfaced by internal audits of the code that changed across the v4.0 line, plus two HomeKit pairing fixes. Nothing here changes how you set anything up; if your installation has been fine, these are quiet correctness fixes.

### Bug fixes

- **Fixed the hot-water temperature snapping back after you set it** — changing a hot-water target temperature updated the slider straight away, then the next poll briefly reverted it to Tado's old reported value before correcting itself a poll later. The new value now holds on the dashboard until the cloud confirms it, so the slider stays where you put it.
- **Fixed Smart Valve and Offset Sync zones going quiet after a token expiry** — if your Tado token expired while a Smart Valve or Offset Sync zone was writing in the background, the write failed silently and the zone stopped responding until the next poll happened to hit the same error. Both now start the reauth prompt straight away, the same as the rest of the integration already did.
- **Fixed rate-limit recovery not kicking in when a write hit the quota** — if your daily Tado quota ran out while the integration was writing (a Smart Valve or Offset Sync adjustment, or a service call), it kept retrying into the exhausted quota and the "rate-limited" Repairs notice never showed for those writes. The rate-limit signal was being dropped before it reached the recovery path, so only the polling side ever surfaced it. Writes now record the back-off window, raise the same Repairs notice the polling path already did, and hold off until the window clears instead of retrying into a wall.
- **`tado_ce.resume_schedule` no longer wastes an API call on zones already on schedule** — resuming a zone that had no active overlay still fired a cloud call to clear an overlay that wasn't there. That skip existed once and was dropped in an earlier rewrite without anything noticing; it is now back, so resuming an already-on-schedule zone costs nothing.
- **Fixed the Offset Sync clamp warning going missing when a write was queued** — when the correction Offset Sync needs is larger than Tado's ±10°C device-offset limit, it flags the value as clamped so you know the displayed temperature still won't match the external sensor. If that write happened to be queued behind a rate-limit or quota back-off, the flag was lost when the queued write finally went out, so the dashboard showed the offset as if it had fully applied. The clamp flag now survives the queue.
- **Fixed HomeKit local control staying off after pairing** — after pairing the bridge, your zones could stay on cloud control because the link between each radiator and its room was only worked out once, right after pairing, before the bridge connection had settled. If that first attempt came up empty it never tried again until you reloaded the integration. It now retries on each polling cycle until the link is built, so local control comes up on its own.
- **Fixed being unable to recover after turning HomeKit off** — disabling HomeKit and later turning it back on demanded a fresh pairing PIN even though the pairing was still stored, and the option to unpair had vanished from Advanced Settings, so a disabled bridge was stuck both ways. Re-enabling now reconnects with the existing pairing, and the unpair toggle stays available whenever there's a pairing to remove, regardless of whether HomeKit is currently on.

### Documentation

- **Fixed the open-window automation example that didn't close the window** ([#295](https://github.com/hiall-fyi/tado_ce/issues/295) - @apilone) — the Features Guide paired `set_open_window_mode` (which writes a frost overlay) with `deactivate_open_window` to clear it, but `deactivate_open_window` only cancels an open window Tado detected itself, so it had nothing to act on and the zone stayed in frost protection. The example now uses `resume_schedule`, and the three open-window services' descriptions spell out which clears which.

### Under the hood

- Removed a handful of dead code paths an audit turned up: four config validators that the read-time range clamps had already made redundant, an unused cache-freshness check with no callers, and a stale re-export left over from a rename. No behaviour change.

## [4.0.2] - 2026-06-07

A stable patch on the v4.0 line, rolling up the v4.1.0-beta.2 work plus two passes that were scoped for v4.1.0-beta.3 and pulled forward so v4.0.x users don't have to wait for the v4.1.0 beta cycle to finish.

The headline is an adaptive-polling reframe. A 1,000-call-tier home with HomeKit connected was collapsing to 1-minute polling and burning through its daily quota. Polling now matches how fast each kind of data actually changes: weather and presence refresh on their own slower schedule instead of riding the zone-state cadence, the adaptive floor is a flat 5 minutes regardless of how big your quota is (zone temperature doesn't change any faster on a bigger plan), and HomeKit-connected setups follow your Cloud Data Refresh dial. If you want faster than 5 minutes, a custom polling interval still gets you there. Full detail under Quota handling below.

The other half is how the integration behaves when something goes wrong with Tado's cloud. Previously several classes of failure (refresh token revoked, persistent 403, rate-limit window) silently froze your entities on stale data with no UI cue. The integration now routes each into the right Home Assistant primitive: a reauth prompt for a dead token, a Repairs issue and the `Retry-After` window honoured for rate-limit, a clean error toast on service calls that hit either. Programmer bugs that the old code wrapped and silent-retried forever now surface as actual errors so they can be fixed.

If your installation has been running fine, this release should be largely invisible to you. If it hasn't been, you'll find out faster.

### Worth knowing before you upgrade

- **Cloud-only setups go `unavailable` during a rate-limit window** instead of staying on stale cached values. They recover on the first successful sync after the `Retry-After` window passes. Automations that compare temperature attributes (`>`, `<`) during that window may behave differently. HomeKit-paired setups are unaffected.
- **HomeKit-connected polling now follows your Cloud Data Refresh dial.** If you relied on the old automatic rate being faster than your dial, set a custom polling interval (Configure → Advanced Settings → Polling & API) to pin it back.
- **The automatic polling floor is now a flat 5 minutes for everyone.** If you're on a large quota (the 20,000-call plan) and relied on the old behaviour automatically polling faster than 5 minutes, set a custom polling interval (Configure → Advanced Settings → Polling & API) to pin it back. A custom interval below 5 minutes is honoured as long as your quota can sustain it.
- **Presence and mobile-device locations refresh at most every 5 minutes** in cloud-only mode (they used to update every poll). For tighter presence latency, rely on Home Assistant's own device_tracker or person integration rather than Tado's cloud presence.
- **A custom polling interval now governs zone updates only.** Weather and presence keep their own slower schedule regardless, so a fast custom interval no longer drags them along.

### When the cloud goes wrong, you see it

- **Reauth flow triggers when your refresh token is revoked** — if Tado invalidates your token (password change, manual revoke, persistent CDN block past three retries), the integration now raises a reauth prompt in Home Assistant. Previously the cloud calls returned silently, your entities stayed on stale data, and you had to notice and reload manually. The same flow also fires when a 401 hits a non-retryable POST (meter reading submission etc.) or when the home ID is missing from your config.
- **Rate-limit window honoured properly** — when Tado returns HTTP 429 with a `Retry-After` header, a cloud-only setup now waits for exactly that long before the next poll instead of polling on its configured interval and re-burning the 429. (A HomeKit-connected setup keeps serving its local readings through the window instead, so it doesn't re-burn the 429 either.) The 429 also surfaces as a Repairs issue ("Tado CE: Cloud rate-limited") with the recovery time, plus a `rate_limited_until` attribute on the API Usage sensor so dashboards can show it. The repair clears itself on the first successful sync after recovery.
- **Service calls during cloud trouble show the right error** — pressing "set temperature" or running `tado_ce.resume_schedule` while your token is dead now triggers reauth (with a toast saying "Tado authentication required") instead of a generic "set failed". Hitting the same service during a rate-limit window shows "Tado API rate-limited. Try again in N min." with the actual cooldown. Six service handlers (`set_temperature_offset`, `add_meter_reading`, `identify_device`, `set_away_configuration`, `activate_open_window`, `deactivate_open_window`) that previously bypassed this routing now flow through the same path.
- **HomeKit-connected installations stay live during cloud trouble** — if HomeKit local control is paired and the cloud is rate-limited or having a transient blip, your entities now stay live on HomeKit data instead of going `unavailable`. The Repairs entry still appears so you know cloud is down, but the dashboard isn't disrupted. Cloud-only setups (no HomeKit pairing) keep the existing behaviour: entities go `unavailable` and recover on the next successful sync.

### Persistence

- **Auto-detect Tado-side zone changes** ([Discussion #280](https://github.com/hiall-fyi/tado_ce/discussions/280) - @Trebor87) — re-pair, hardware swap, and zone add/remove on the Tado side now triggers cache invalidation automatically. The integration picks up the change on the next quick poll (5-30 min depending on cadence) and refetches the affected entries. The Refresh AC Capabilities hub button stays as a manual escape hatch.
- **Stale zone cleanup** — when a zone is removed in the Tado app, the integration now drops every cached reference to it on the next poll rather than carrying stale data until the next HA restart.
- **Device state stays current** ([#286](https://github.com/hiall-fyi/tado_ce/issues/286) - @Fred224) — the Connection, firmware version, and battery sensors used to freeze at boot-time values until you restarted HA. They now refresh on a periodic cadence (hourly on the 1,000-call transitional tier and the 20,000-call legacy tier, every 4 hours on the 100-call free tier), so an AC controller that drops Tado's cloud uplink (E04 errors, mains blip, Wi-Fi flap) actually shows up as Disconnected within the cycle, and reconnections clear it the same way.

### Bug fixes

- **`tado_ce_ready` event now reaches boot-time automations** ([#287](https://github.com/hiall-fyi/tado_ce/issues/287) - @Newreader) — the event was firing as soon as the integration finished its setup, which on a cold HA start happens before automation triggers register their listeners. Anyone with an automation triggered by `tado_ce_ready` saw it never run after a restart, only after a manual reload. Fixed by deferring the fire until HA has fully started; the reload path is unchanged because that one runs while HA is already up.
- **Refresh AC Capabilities button now actually refreshes** — since v4.0.0-beta.5's storage migration, the button was deleting a legacy file path that no longer exists, so the cache stayed populated and the re-fetch was silently skipped. Pressing the button now properly invalidates the cache and forces a fresh fetch from Tado, which is what you needed it to do all along after re-pairing a Smart AC controller.
- **Mobile device trackers now explain the Unknown state** ([Discussion #271](https://github.com/hiall-fyi/tado_ce/discussions/271) - @Prodeguerriero) — when a phone's Tado-app geo-tracking is on but the Tado cloud isn't receiving its location, the device tracker entity now exposes a `location_status` attribute reading `no_location_reported` instead of leaving you guessing why the entity reads Unknown. On Android the most common cause is battery optimisation killing the Tado app's background location service — the attribute names that scenario explicitly so you know where to look in the OS settings rather than chasing it as a Tado CE bug.

### State Restore

- **Captures only persist on successful cloud writes** ([#278](https://github.com/hiall-fyi/tado_ce/issues/278) - @Newreader, root-cause companion to v4.0.1's defensive 2-poll gate) — previously a snapshot was saved before the cloud write fired, so a failed write left a stale entry that could be replayed by a later overlay-cleared event. Captures now correspond to known-applied state changes only.

### User-action consistency

- **Card actions update the dashboard immediately and roll back on failure** — temperature sliders already worked this way; HVAC mode dropdowns, timer buttons, water-heater on/off, and boost/smart-boost buttons now match. The card updates the moment you click, and if the cloud rejects or times out it rolls back with an error toast. The `tado_ce.turn_off_all_zones`, `tado_ce.resume_schedule`, `tado_ce.set_open_window_mode`, and `tado_ce.restore_previous_state` services write to the cloud and then trigger an immediate refresh, so the dashboard catches up within a second rather than instantly; a failure there surfaces as an error on the service call rather than an optimistic roll-back.

### Quota handling

- **Low-quota gate now scales with your tier** — three places in the integration back off optional cloud work when remaining quota is low: the offset drift refresh, the calendar schedule fetch on first load, and the Smart Day/Night polling switch. All three used a hardcoded 100-call threshold, sized for the 100-call free tier. On the 1,000-call transitional tier that fires at 10% remaining (sensible). On the 20,000-call legacy tier it fires at 0.5% remaining (way too late — manual actions had no cushion). The threshold is now tier-aware: 10% of your daily limit, with a 100-call absolute floor. Free tier unchanged. Transitional tier unchanged at the boundary. Legacy tier now backs off at 2,000 remaining instead of 100, leaving 1,900 calls of headroom for manual actions.
- **Polling now matches how fast each kind of data actually changes** ([#289](https://github.com/hiall-fyi/tado_ce/issues/289) - @driagi) — weather, presence, and mobile-device locations used to refresh on the same cadence as your zone temperatures, so a fast polling interval fetched them far more often than they ever change. Presence and mobile locations had no rate limit at all and fired on every single poll. They now refresh at most every 30 minutes (weather) or 5 minutes (presence and locations) no matter how fast your zones poll. The adaptive polling floor is now a flat 5 minutes for everyone, whatever your daily call limit. The old logic let a big quota buy a faster automatic cadence (down to 1-minute polling), which is what was quietly burning through the day's calls for no real gain: zone temperature doesn't change any faster just because your plan is bigger. If you genuinely want faster than 5 minutes, set a custom polling interval and it's honoured as-is. And when HomeKit local control is connected, the cloud cadence follows your Cloud Data Refresh setting instead of an automatic faster rate. A custom polling interval now governs your zone updates only; weather and presence keep their own slower schedule. The API Usage sensor gained `weather_last_fetched_min_ago`, `presence_last_fetched_min_ago`, and `mobile_devices_last_fetched_min_ago` attributes so you can see which data type is using calls.

### i18n

- **Mold Risk % renamed to Mold Risk Indicator** ([#90](https://github.com/hiall-fyi/tado_ce/issues/90) - @ChrisMarriott38) — the numeric humidity sensor (added in v2.0.1) used to display as `Mold Risk %`, which read like "percentage of mold risk" but is actually surface relative humidity. The trailing `%` also caused a slug collision on fresh installs because HA's slugify drops it, so the entity was landing as `sensor.<zone>_mold_risk_2`. The friendly name is now `Mold Risk Indicator` across all seven locales (DE, ES, FR, IT, NL, PT translated via DeepL). Existing installs are unaffected: `entity_id`, `unique_id`, `device_class=HUMIDITY` (so HA still appends `%` in the UI), and all attribute names stay the same. Only the UI label changes, and fresh installs from this release onward get a clean auto-generated slug without the `_2` suffix.

### Home Assistant compatibility

- **Removed a HA Core deprecation warning** — Tado CE was using a device-tracker import path that HA Core marked deprecated and is removing in HA 2027.6. Switched to the current path. The "deprecated alias TrackerEntity was used from tado_ce" warning that appeared on every startup is gone. No behavioural change.

### Internal (no user-visible change)

- **Programmer bugs no longer silent-retry forever** — a number of `except Exception` catches across the API client, Smart Valve Control, services helpers, and rate-limit gate were swallowing programmer bugs (KeyError on a malformed response, AttributeError after a refactor, etc.) and silent-retrying them on every poll. Those catches are now scoped to the specific exception types they actually need to handle, so bugs surface as real errors with full tracebacks. (A couple of deliberate last-resort catches remain, e.g. the API client's "unexpected error, abandon this call and retry next poll" fallback, which is intentional rather than a swallowed bug.) If something regresses in a future Tado API change, you should see it as a failed-to-load integration rather than a silently degraded one.
- **Cloud-unreachable log gets quieter** — the warning that fired every poll when HomeKit was carrying entities through a cloud outage now logs once on the unavailable transition and once on recovery, matching Home Assistant's `log-when-unavailable` Quality Scale rule. No fewer events, just no log spam.
- **Settings descriptions corrected** — the bridge auth code is described as 4-digit (it is; the 8-digit figure was the HomeKit pairing code creeping in). The Mobile Device Tracking description now spells out that Frequent Mobile Sync lives under Advanced Settings → Polling & API and only appears once tracking is turned on and saved. The Device Sync Delay description gained its range and what to do with it. A couple of stale labels that pointed at controls which had moved were dropped.
- **Four unused feature toggles removed** — `zone_diagnostics`, `device_controls`, `boost_buttons`, and `environment_sensors` config keys had no UI anywhere and sat permanently on. The features they gated still run exactly as before; only the dead config keys are gone.

### Persistence

- **Auto-detect Tado-side zone changes** ([Discussion #280](https://github.com/hiall-fyi/tado_ce/discussions/280) - @Trebor87) — re-pair, hardware swap, and zone add/remove on the Tado side now triggers cache invalidation automatically. The integration picks up the change on the next quick poll (5-30 min depending on cadence) and refetches the affected entries. The Refresh AC Capabilities hub button stays as a manual escape hatch.
- **Stale zone cleanup** — when a zone is removed in the Tado app, the integration now drops every cached reference to it on the next poll rather than carrying stale data until the next HA restart.
- **Device state stays current** ([#286](https://github.com/hiall-fyi/tado_ce/issues/286) - @Fred224) — the Connection, firmware version, and battery sensors used to freeze at boot-time values until you restarted HA. They now refresh on a periodic cadence (hourly on paid Auto-Assist tiers, every 4 hours on the free tier), so an AC controller that drops Tado's cloud uplink (E04 errors, mains blip, Wi-Fi flap) actually shows up as Disconnected within the cycle, and reconnections clear it the same way.

### Bug fixes

- **`tado_ce_ready` event now reaches boot-time automations** ([#287](https://github.com/hiall-fyi/tado_ce/issues/287) - @Newreader) — the event was firing as soon as the integration finished its setup, which on a cold HA start happens before automation triggers register their listeners. Anyone with an automation triggered by `tado_ce_ready` saw it never run after a restart, only after a manual reload. Fixed by deferring the fire until HA has fully started; the reload path is unchanged because that one runs while HA is already up.
- **Refresh AC Capabilities button now actually refreshes** — since v4.0.0-beta.5's storage migration, the button was deleting a legacy file path that no longer exists, so the cache stayed populated and the re-fetch was silently skipped. Pressing the button now properly invalidates the cache and forces a fresh fetch from Tado, which is what you needed it to do all along after re-pairing a Smart AC controller.

### State Restore

- **Captures only persist on successful cloud writes** ([#278](https://github.com/hiall-fyi/tado_ce/issues/278) - @Newreader, root-cause companion to v4.0.1's defensive 2-poll gate) — previously a snapshot was saved before the cloud write fired, so a failed write left a stale entry that could be replayed by a later overlay-cleared event. Captures now correspond to known-applied state changes only.

### User-action consistency

- **Card actions update the dashboard immediately and roll back on failure** — temperature sliders already worked this way; HVAC mode dropdowns, timer buttons, water-heater on/off, and boost/smart-boost buttons now match. The card updates the moment you click, and if the cloud rejects or times out it rolls back with an error toast. The `tado_ce.turn_off_all_zones`, `tado_ce.resume_schedule`, `tado_ce.set_open_window_mode`, and `tado_ce.restore_previous_state` services write to the cloud and then trigger an immediate refresh, so the dashboard catches up within a second rather than instantly; a failure there surfaces as an error on the service call rather than an optimistic roll-back.

### i18n

- **Mold Risk % renamed to Mold Risk Indicator** ([#90](https://github.com/hiall-fyi/tado_ce/issues/90) - @ChrisMarriott38) — the numeric humidity sensor (added in v2.0.1) used to display as `Mold Risk %`, which read like "percentage of mold risk" but is actually surface relative humidity. The trailing `%` also caused a slug collision on fresh installs because HA's slugify drops it, so the entity was landing as `sensor.<zone>_mold_risk_2`. The friendly name is now `Mold Risk Indicator` across all seven locales (DE, ES, FR, IT, NL, PT translated via DeepL). Existing installs are unaffected: `entity_id`, `unique_id`, `device_class=HUMIDITY` (so HA still appends `%` in the UI), and all attribute names stay the same. Only the UI label changes, and fresh installs from this beta onward get a clean auto-generated slug without the `_2` suffix.

### Home Assistant compatibility

- **Removed a HA Core deprecation warning** — Tado CE was using a device-tracker import path that HA Core marked deprecated and is removing in HA 2027.6. Switched to the current path. The "deprecated alias TrackerEntity was used from tado_ce" warning that appeared on every startup is gone. No behavioural change.

## [4.0.1] - 2026-06-03

The biggest user-visible change is the AC swing dropdown split into separate vertical and horizontal axes, which already shipped to beta testers as v4.1.0-beta.1. AC users on fine-grained swing units (Mitsubishi, Fujitsu, similar) can now park each axis independently instead of being forced into a single sweeping motion.

Beyond the swing change, this release rolls up swing-fix polish, an AC HomeKit documentation correction (Smart AC Control V3+ units don't go through the bridge), and several defensive fixes raised by beta testers and v4.0 users.

### Features

- **`tado_ce.turn_off_all_zones` service** ([#283](https://github.com/hiall-fyi/tado_ce/issues/283) - @hapklaar) — Mirrors the Tado app's "Turn OFF all rooms" button. One call places every climate zone (heating + AC) into a manual OFF overlay; schedules stay suppressed until you resume each zone manually. Designed for "outside temperature above 15°C — turn off everything" automations where the next schedule block would otherwise override the off state. Hot water out of scope (Tado's own button targets climate zones only). FEATURES_GUIDE entry #14 has the full automation example.

### Bug Fixes

- **State Restore: defensive 2-poll confirmation before firing the restoration event** ([#278](https://github.com/hiall-fyi/tado_ce/issues/278) - @Newreader) — the `tado_ce_state_restoration_available` event was firing the moment a single poll showed a zone's overlay had cleared. A single observation can be a transient (quota-reset window, partial poll response, brief upstream blip) rather than a real overlay clear. The integration now waits for two consecutive polls confirming the same overlay clear before firing the event, filtering out transients without changing steady-state behaviour. The event still fires on real overlay clears (timer expiry, user resume from Tado app), just one polling cycle later (~5–12 minutes depending on cadence). FEATURES_GUIDE adds a defensive listener pattern (delay + re-check) for any automation subscribed to this event. Reported by @Newreader after observing a frost-protection overlay reverting to schedule heat in his setup; the underlying mechanism (whether transient API response or upstream Tado server behaviour) is still under investigation, but the defensive gate addresses the symptom regardless.
- **Polling resume log line no longer fires when never paused** — the polling-pause check was logging "expected reset time XX:XX UTC has passed — resuming polling" on every poll cycle once 24 hours had elapsed since the last quota reset, even on tiers where polling was never paused (anyone on 1000+ tier under normal load). The Tado API doesn't always refresh its quota-reset timestamp promptly, so the condition stayed True every cycle and the log filled with duplicates. The line now only fires on an actual paused → unpaused transition. Cosmetic fix; no behavioural change.
- **Drift refresh log line surfaces per-cycle quota cost** ([#277](https://github.com/hiall-fyi/tado_ce/issues/277) - @wrowlands3) — `Offset Sync: drift refresh complete` now ends with `... N cloud call(s) used this cycle` so users can audit drift refresh quota usage without inferring N from rate-limit decrement traces. FEATURES_GUIDE Smart Polling adds a worked daily-total formula and notes the quota-low pause threshold.
- **Adaptive polling now scales correctly across quota tiers** ([#276](https://github.com/hiall-fyi/tado_ce/issues/276) - @churchofnoise) — The narrowing direction of the adaptive math was capped at 5 minutes regardless of available quota, leaving 20K legacy-tier users polling 1/3 of what their quota afforded. The cap is now 1 minute (Tado's cloud doesn't update faster than that anyway), so high-quota users get sub-5-minute polling automatically. 100-call free tier and 1,000-call transitional tier unchanged because the math widens well above the floor on those budgets.
- **Swing dropdowns now follow physical louver order** ([#270](https://github.com/hiall-fyi/tado_ce/issues/270) - @BirbByte) — On units that report fine-grained positions, the dropdown was alphabetical (`Down / Mid / Mid (down) / Mid (up) / Up`), which doesn't map to where the louver actually sits. Vertical now reads top-to-bottom (`Auto, Up, Mid (up), Mid, Mid (down), Down, On`) and horizontal reads left-to-right (`Auto, Left, Mid (left), Mid, Mid (right), Right, On`), matching the Tado app and most AC remotes. Unknown values Tado adds in future fall through alphabetically at the tail so they stay visible.
- **Swing dropdown icons are now consistent** ([#270](https://github.com/hiall-fyi/tado_ce/issues/270) - @BirbByte) — `On` was picking up Home Assistant's built-in `mdi:swap-vertical` while the fixed positions had no icon at all, which looked uneven. Each swing value now ships its own icon: directional arrows for the position values, the auto-mode glyph for `Auto`, the swap glyphs for continuous `On`, and a small dot for `Off`.

### Improvements

- **DeepL polysemy fixes for swing translations** — DeepL guessed the wrong sense for several context-free single-word labels. `On` came back as a preposition or article in all six locales (`Am` in German, `El` in Spanish, etc.), `Right` came back as "correct" in four (`Richtig` / `Giusto` / `Juist` / `Certo`), Spanish and Portuguese had `Auto` translated literally as "car", and Italian's `Up` came out as the scroll-up sense. 21 hand-fixed strings across the six locales. Thanks to @Ralf84 for catching the German pair on first install.

### Documentation

- **Smart AC Control V3+ HomeKit support corrected** ([Discussion #271](https://github.com/hiall-fyi/tado_ce/discussions/271) - @MacrosorcH) — the Supported devices table read `✅ (temperature only)` for Smart AC Control V3 / V3+ since v4.0.0 GA, but @MacrosorcH pointed out that Smart AC Control units are standalone WiFi accessories with their own HomeKit pairing code, not bridged through the Internet Bridge. Tado CE only handles the bridge pairing flow today, so an AC unit's own standalone HomeKit pairing isn't wired in. (A simple AC temperature change can still go local over a connected bridge, the same fast path heating uses; mode, fan, and swing writes go through the cloud.) The table cell is now `❌` with a pointer to the FEATURES_GUIDE explanation; ROADMAP records standalone-unit pairing as a no-hardware-no-code future-consideration item rather than scoped work.
- **HomeKit local control setup steps fixed** ([Discussion #271](https://github.com/hiall-fyi/tado_ce/discussions/271) - @clude86) — The README setup section incorrectly told users to add the Tado bridge as an HA HomeKit Device first; this claims the bridge's single HomeKit-controller slot and stops Tado CE from finding it. Setup now starts directly from the Tado app's HomeKit pairing code straight into Tado CE's Configure form, no HA HomeKit Device step required.
- **TRV LED feedback after a HomeKit-routed write** ([#281](https://github.com/hiall-fyi/tado_ce/issues/281) - @amplitur) — Documented that the HomeKit Accessory Protocol doesn't carry a feedback channel for the LED-flash visual confirmation, with `tado_ce.identify_device` shown as a manual workaround.
- **Hub-level buttons documented in FEATURES_GUIDE** ([Discussion #280](https://github.com/hiall-fyi/tado_ce/discussions/280) - @Trebor87) — New "Hub Buttons" section in Enhanced Controls covers Resume All Schedules, Refresh AC Capabilities, and Refresh Schedule. Each entry lists entity_id, what the button does, when to press, and quota cost. Refresh AC Capabilities is the existing recovery path for AC zones whose HVAC modes show as `[OFF]` only after re-pairing — it was hard to find before, now it's documented properly.
- **Quota Reserve switch surfaced in Smart Polling docs** — The `switch.tado_ce_{home_id}_quota_reserve_enabled` entity is now named in the Quota Reserve section so users know which switch toggles the protection.
- **Child Lock and Early Start switches dedicated section** — New entry in Enhanced Controls explains the per-device (Child Lock) vs per-zone (Early Start) distinction, sync direction, and API cost.
- **Event-listener safety patterns** — New "Event listeners for automation builders" section lists the 5 events tado_ce fires and shows a defensive listener pattern (delay + re-check) for `tado_ce_state_restoration_available`. Defensive code in 4.0.1 covers most transients; the doc pattern covers the rest.
- **`add_meter_reading` and `identify_device` services documented**. Both have been live since v3.x but only had handler-level wiring, no user docs. FEATURES_GUIDE now covers `add_meter_reading` (submit a gas or electricity reading to Tado's Energy IQ, with an automation example for monthly smart-meter readings) and `identify_device` (flash the LED on a TRV by serial, useful for finding the offline one in a multi-TRV zone).
- **v5.0.0 upgrade-path notice**. A future v5.0.0 will drop the in-place migration code that upgrades v3.x option keys, entity IDs, and storage layouts. README and FEATURES_GUIDE now flag this early so any v3.x install still around can upgrade through v4.x first; one-jump v3.x to v5.0.0 won't be supported. No timeline yet, surfacing months ahead so there's no rush.

### i18n

- **Hub button display names renamed for clarity** — `Resume All` → `Resume All Schedules`; `Refresh AC` → `Refresh AC Capabilities`. The entity name itself now tells users what the button does without needing to consult docs. DeepL pass for DE/ES/FR/IT/NL/PT, with hand-fixes for two polysemy cases (DE "anzeigen" → "fortsetzen"; ES "Volver a" → "Reanudar").

### Internal

- **Codebase cleanup, no user-visible behaviour change.** Removed `EntityMeta.legacy_name` (a pre-translation_key holdover with 67 dead callsites and zero consumers), the one-time Test Mode entity-cleanup pass that's been a no-op since v4.0.0-beta.7 ran on every install, and `async_create_deprecated_config_issue` (a YAML-deprecation repair helper that was never called and structurally couldn't be, since Tado CE doesn't accept YAML configuration). Net 111 lines of dead code dropped from the integration. Translation files lose the now-orphaned `deprecated_yaml_config` repair string across all seven locales, kept in sync via the existing translation consistency check.

The unified `Off / Vertical / Horizontal / Both` swing dropdown is replaced with two independent dropdowns, one per axis. Units that report fine-grained louver positions (Mitsubishi, Fujitsu, and similar) can now be parked at a fixed direction such as Up, Mid, or Mid (left) instead of being forced into a sweeping motion.

### Features

- **Split AC swing into vertical and horizontal axes** ([#270](https://github.com/hiall-fyi/tado_ce/issues/270) - @Ralf84) — AC zones now expose a `Swing (vertical)` and a `Swing (horizontal)` dropdown, each populated from the cloud-reported capability set for your specific unit. Pick a fixed louver position on either dropdown to stop oscillation on that axis — useful in bedrooms or children's rooms where a constantly moving louver is disruptive. Simple `On / Off` units keep a two-value dropdown.
- **Fine-grained louver positions for capable AC units** — Units that report values like `UP`, `MID_DOWN`, `LEFT`, `MID_RIGHT` now expose those values directly in the dropdowns. Translations land in German, Spanish, French, Italian, Dutch, and Portuguese alongside English.

### Bug Fixes

- **Swing changes no longer silently drop the off-axis on `OFF`-less units** ([#270](https://github.com/hiall-fyi/tado_ce/issues/270) - @Ralf84) — Picking "Vertical" on a Mitsubishi or Fujitsu unit (which doesn't report `OFF` as a swing value) used to send only `verticalSwing=ON` and omit `horizontalSwing` from the payload, leaving the bridge to keep whatever horizontal state it last had. Each axis now writes its own value independently and the silent-drop fallback is gone.
- **Picking "On" from a swing dropdown no longer silently moves the off-axis** — `On` was incorrectly being translated as a legacy v4.0 unified value, which set the opposite axis to `Off` and logged a deprecation warning the user hadn't earned. `On` is now treated as a v4.1 raw axis value and only the axis you picked moves.

### Improvements

- **One log line per AC write failure, not two** — Temperature, HVAC mode, fan mode, and swing writes used to log the specific failure reason (timeout, exception, or "rejected by Tado") and then a generic "write failed" warning right after. The generic line now fires only when no specific reason was logged, so a timeout produces a single warning rather than two.

### ⚠️ Migration

**Service-call automations** — calls using the old unified value (`swing_mode: off / vertical / horizontal / both`) keep working with a deprecation warning logged each call. The compat shim is removed in v5.0.0. Recipe:

```yaml
# Before (v4.0)
- service: climate.set_swing_mode
  data:
    entity_id: climate.tado_ce_living_room
    swing_mode: both

# After (v4.1+)
- service: climate.set_swing_mode
  data:
    entity_id: climate.tado_ce_living_room
    swing_mode: on        # or 'auto', 'up', 'mid', etc. — your unit's capability
- service: climate.set_swing_horizontal_mode
  data:
    entity_id: climate.tado_ce_living_room
    swing_horizontal_mode: on
```

**Dashboard templates and Lovelace conditions** — anything reading `state_attr('climate.X', 'swing_mode')` and matching `'both'`, `'vertical'`, or `'horizontal'` needs updating. The compat shim covers service calls, not state reads. After v4.1 the attribute holds raw values like `'on'`, `'off'`, `'up'`, `'auto'`. Cards and templates should switch to checking `swing_mode` and `swing_horizontal_mode` separately:

```jinja
{# Before #}
{% if state_attr('climate.tado_ce_living_room', 'swing_mode') == 'both' %}

{# After #}
{% set v = state_attr('climate.tado_ce_living_room', 'swing_mode') %}
{% set h = state_attr('climate.tado_ce_living_room', 'swing_horizontal_mode') %}
{% if v not in ['off', None] and h not in ['off', None] %}
```

**HomeKit users with AC zones** — temperature and HVAC mode still update locally via HomeKit (typically within 2 seconds). Swing changes still go through Tado's cloud, so picking a swing position uses cloud quota and confirms on the next cloud poll (typically 5–30 minutes). Same as v4.0 — not a regression. A follow-up to wire HomeKit's binary `SwingMode` characteristic into the new vertical-axis dropdown for ON/OFF AC units is being scoped separately.

## [4.0.0] - 2026-05-23

**HomeKit Local Control & Smart Valve Control**

Existing v3.5.3 installs upgrade in place — data, schedules, and HomeKit pairings migrate automatically.

### ⚠️ Breaking Changes

- **Connection sensors are now binary sensors** ([#160](https://github.com/hiall-fyi/tado_ce/issues/160) - @Thilas, @jeverley) — `sensor.tado_ce_*_connection` entities migrate to `binary_sensor.tado_ce_*_connection` with the Connectivity device class. The migration is automatic on first startup; automations or dashboard cards referencing the old IDs need updating.
- **Hot water power sensor is now a binary sensor** ([#160](https://github.com/hiall-fyi/tado_ce/issues/160) - @jeverley) — `sensor.tado_ce_*_power` migrates to `binary_sensor.tado_ce_*_power` with the Power device class. Same — update any references.

### Features

- **HomeKit Local Control for Tado Internet Bridge** — Pair your Tado bridge via HomeKit and temperature / mode changes go through your local network instead of Tado's cloud, with sensor pushes arriving within ~2 seconds (cloud-only mode is bound by your polling interval, typically 5–30 minutes). The integration tracks how many API calls HomeKit saves you per day. If HomeKit becomes unavailable, the integration automatically falls back to the cloud and tries to recover in the background. Set up under **Settings → Tado CE → Configure → General Settings → enable HomeKit**.
- **Smart Valve Control — two modes for compensating inaccurate TRV readings** ([Discussion #231](https://github.com/hiall-fyi/tado_ce/discussions/231) - @Si-Hill, @wrowlands3, [#221](https://github.com/hiall-fyi/tado_ce/issues/221) - @simonotter) — TRV built-in sensors sit on the radiator and read warmer than the room, so Tado closes the valve before the room reaches target. With an external temperature sensor configured for a zone, you can now choose how to compensate:
  - **Offset Sync (recommended)** — Writes a device temperature offset so the Tado app and Tado's own modulation see your external sensor's reading.
  - **Valve Target (advanced)** — Directly overrides the TRV setpoint. Use only when Offset Sync isn't enough.

  Mutually exclusive per zone. Configure under **Settings → Tado CE → Configure → Zone Configuration → External Sensors → Smart Valve Control Mode**. Adjustments go via HomeKit when available (no API cost), with cloud as fallback. The controller backs off on manual changes and resumes on the next schedule block. See [FEATURES_GUIDE.md](FEATURES_GUIDE.md#-smart-valve-control) for details.
- **`tado_ce_ready` event for startup automations** ([#246](https://github.com/hiall-fyi/tado_ce/issues/246) - @Newreader) — Trigger startup automations on the `tado_ce_ready` event instead of guessing timing with delays or `wait_template` chains. The event fires once all climate entities have real data — temperature, offset, overlay mode, the lot. Payload includes `home_id`, `entry_id`, and `zone_count` for multi-home filtering.
- **Climate entity exposes Offset Sync clamp state** ([#262](https://github.com/hiall-fyi/tado_ce/issues/262) - @simonotter) — When the gap between your external sensor and the TRV needs a correction larger than ±10°C, Tado clamps the offset at the limit. The climate entity now exposes `offset_clamped: true` and `offset_clamp_direction: hit_max | hit_min` so dashboards and automations can react. A warning fires in the log so you know to check for draughts, a cold external wall behind the TRV, or an external sensor placed somewhere warmer than the radiator.

### Bug Fixes

- **Tado app changes now reach Home Assistant within seconds when HomeKit is connected** ([#253](https://github.com/hiall-fyi/tado_ce/issues/253) - @apilone, [#261](https://github.com/hiall-fyi/tado_ce/issues/261) - @apilone) — Changing temperature or HVAC mode in the Tado phone app used to take up to one cloud poll cycle to show in HA. With HomeKit connected, target temperature and mode now update from bridge events in real time, including when you flip a zone from OFF to HEAT or set a temperature on an OFF zone.
- **Climate card now shows OFF correctly** ([#258](https://github.com/hiall-fyi/tado_ce/issues/258) - @Newreader, @apilone, [Discussion #219](https://github.com/hiall-fyi/tado_ce/discussions/219) - @dragorex71) — When a zone was off — via schedule, Away mode, or `set_hvac_mode: off` — the card showed the previous heat target (e.g. 23°C) and HVAC mode "auto" while Tado was actually running frost protection at 5°C. The card now shows 5°C and "off" to match Tado's actual state.
- **HomeKit no longer overwrites your changes with stale cached values** ([#253](https://github.com/hiall-fyi/tado_ce/issues/253) - @apilone) — After changing temperature or mode, the bridge could push a stale value within seconds and undo your change. There's now a 3-minute write protection window after any HomeKit or cloud write — during this window, the bridge's stale target temperature and mode are ignored until the cloud confirms the actual state.
- **Heating controls no longer get stuck after a silent HomeKit failure** — If a HomeKit write completed locally but never reached Tado's servers, the integration could keep showing "heating" indefinitely and skip subsequent commands. Writes are now verified against Tado's cloud, the stale state is cleared if not confirmed, and future writes for that zone fall through to cloud until HomeKit recovers.
- **Service calls now refresh the entity immediately** ([Discussion #219](https://github.com/hiall-fyi/tado_ce/discussions/219) - @jeverley) — `set_open_window_mode`, `restore_previous_state`, `resume_schedule`, `set_climate_timer`, and `set_water_heater_timer` used to update the Tado app instantly but leave the HA entity stale until the next poll. They now trigger an immediate refresh.
- **Failed service calls now surface as errors instead of silent success** — `set_temperature_offset`, `set_climate_timer`, and `set_water_heater_timer` used to swallow per-zone failures and report overall success even when no write actually landed. They now raise an error visible in the HA UI when every zone fails, and log a warning for partial failures.
- **`restore_previous_state` and `resume_schedule` now actually restore the zone** ([#267](https://github.com/hiall-fyi/tado_ce/issues/267) - @apilone) — Calling `restore_previous_state` after changing a zone could leave the cloud unchanged while the HA log claimed "Restore executed". Two problems stacked: the saved state was being sent back to Tado in a format Tado's cloud rejected, and the service ignored the rejection and cleared the saved state anyway. The service now strips out fields Tado's cloud rejects before sending, checks Tado's response, and only clears the saved state when the call actually lands. On failure you get a clear error in the HA UI and the saved state stays put so you can retry. `resume_schedule` had the same problem and is fixed alongside.
- **Early Start and Child Lock switches roll back on failed writes** — If the API rejected a switch write (e.g. during a rate-limit window), the switch used to stay in the optimistic new state until the next refresh. The switch now reverts when the underlying API call reports failure.
- **Presence Mode "Auto" no longer briefly shows "Home" when you're away** — Switching to Auto used to overwrite the cached presence with "Home" for one poll cycle. Auto now leaves the cached presence alone and lets the next poll fill in the real value from geofencing.
- **Presence labels match across the Hub select and climate cards** ([Discussion #219](https://github.com/hiall-fyi/tado_ce/discussions/219) - @dragorex71) — On non-English Home Assistant installations, the Hub presence select and the climate card preset showed different translations (e.g. "Casa" vs "In casa" in Italian). All six supported languages now match.
- **Adaptive Preheat works with non-ASCII zone names** — Zones with accented characters or special characters (e.g. "Büro", "Salle-à-manger") were silently failing to find their matching entities, breaking Adaptive Preheat, water heater resume buttons, and thermal analytics. All zone-name lookups now use Home Assistant's standard slug method.
- **Weather Compensation no longer latches into "paused" overnight** ([#249](https://github.com/hiall-fyi/tado_ce/issues/249) - @driagi) — On long night-polling intervals (≥ 60 min, including auto night polling at 120 min), the engine could latch into "paused" forever even though the outdoor temperature source was reporting fresh values. The redundant guard causing this has been removed. The engine also holds the last known outdoor temperature for up to 30 minutes during transient outages instead of pausing on a single missed reading.
- **Token rotations no longer drop in-flight writes** — When Tado's auth servers rotate a session mid-request, the API returns 401. Read requests already retried with a fresh token; writes (temperature, mode, schedule resume) now do too. A transient 401 during normal token refresh also no longer deletes your refresh token — Home Assistant's reauth flow handles real auth failures, transient glitches recover on the next refresh.
- **Mold risk and surface temperature attributes are now translated** — The `temperature_source` attribute on Mold Risk / Mold Risk % sensors and the `calculation_method` attribute on the Surface Temp sensor used to display raw English values regardless of your Home Assistant language. They now show translated labels in German, Spanish, French, Italian, Dutch, and Portuguese alongside English.
- **Integration recovers from corrupt auxiliary storage files** — If one of several auxiliary storage files (weather compensation, bridge health, HomeKit savings, window detection state, state-restore) became corrupt after a crash or SD card issue, every entity could go unavailable. The integration now logs a warning and continues with defaults, healing the file on the next successful save.
- **Daily API usage no longer spikes when HomeKit is connected** ([#268](https://github.com/hiall-fyi/tado_ce/issues/268) - @wrowlands3) — A periodic offset drift refresh added during the 4.0 cycle fired every 30 minutes regardless of your HomeKit Cloud Refresh setting, pulling one cloud call per climate zone per cycle. On a home with eight zones at a 60-minute Cloud Refresh, that meant ~16 unexpected calls every hour. With HomeKit connected, the drift refresh now follows the same dial as the rest of cloud sync — set 60 minutes and it runs every 60 minutes. HomeKit-off installs keep the 30-minute floor as a safety net.
- **Weather fetches now follow your HomeKit Cloud Refresh setting** — Same shape as the drift-refresh bug above with a smaller blast radius (one call per cycle rather than per zone): the 30-minute weather skip floor was hardcoded and ignored the Cloud Refresh dial. Both paths are now driven by a single user-facing setting.
- **Adaptive Preheat tolerates incomplete cycle metrics** — When the heating cycle store had a partially-recorded cycle with a missing inertia time or heating rate, the preheat estimator could raise an error in the log instead of skipping. It now returns no estimate and lets the next valid cycle take over.

### Improvements

- **All data now uses Home Assistant's built-in storage** — Zone states, weather, rate limits, schedules, HomeKit pairings, and per-zone state files moved from custom JSON files to Home Assistant's standard storage. The move runs once on first startup; old files are renamed (not deleted) so you can roll back if needed. Startup is faster and HA stays responsive while saving.
- **Token refreshes ~50% less often** — The integration used to refresh access tokens every 5 minutes, but Tado issues them for roughly 10 minutes. It now reads the actual expiry from Tado's response and only refreshes when the token is about to expire.
- **Boiler flow temperature updates every 60 seconds** ([#237](https://github.com/hiall-fyi/tado_ce/issues/237) - @ChrisMarriott38) — The boiler flow sensor used to be tied to your cloud polling interval (up to 30 minutes between updates). It now polls the bridge independently every 60 seconds. The bridge API doesn't count against your Tado quota.
- **Custom polling interval applies everywhere** ([#239](https://github.com/hiall-fyi/tado_ce/issues/239) - @ChrisMarriott38) — If you set a custom day or night polling interval, all data (zone states, humidity, heating power, weather, boiler flow temperature) refreshes at that rate. Previously, HomeKit's Cloud Data Refresh setting could override your custom interval for some data.
- **Smarter polling when HomeKit is connected** — When HomeKit is providing live data, the integration skips redundant cloud fetches and stretches the polling interval. Weather data is fetched every 30 minutes instead of every poll. Cloud outages no longer make entities unavailable when HomeKit is still working.
- **Insight history writes drop ~50%** — The insight history file used to write once per polling cycle (up to 2,880 writes/day with 30-second polling). Writes are now debounced to once per minute, reducing SD card wear without losing data on shutdown.
- **API call history attributes capped to 10 entries (was 100)** — The API usage, limit, and call history sensors used to expose up to 100 entries each in their attributes, bloating the recorder database over time. Dashboards only show the most recent few, so the cap is now 10.
- **Less CPU work per poll on homes with many zones** — Zone insights are now computed once per polling cycle and cached for every zone sensor and the home sensor to read, instead of each sensor collecting independently.
- **Smarter HTTP error handling** — 404 on overlay deletion (already gone) is no longer logged as an error. 422 (Tado API rejection) is logged as a warning with the actual rejection reason. 429 reads the `Retry-After` header to know when to try again. 500 / 502 / 503 / 504 are retried automatically with backoff.
- **Settings UI reorganised** — General Settings now grouped by what each toggle does (Tado Features / Hardware Connections / Smart Automations / Advanced). Zone Configuration reordered to match how you think about a zone (Temperature Limits → Heating System → External Sensors → Smart Features → Manual Temperature Override). Internet Bridge gets its own settings section instead of living under "Weather Compensation" ([#240](https://github.com/hiall-fyi/tado_ce/issues/240) - @ChrisMarriott38). Existing entity IDs and config keys unchanged.
- **External sensor toggles renamed for clarity** — "Use External Temperature/Humidity Sensor" → "Override Tado's Temperature/Humidity Sensor". Turning the toggle off keeps your sensor selection saved, so you can pause without losing the configuration.
- **Hot Water Timer default moved to Polling & API** — Lived under Smart Comfort by accident; now lives next to the other service defaults. Config key unchanged.
- **Smart Comfort defaults to Light on first enable** — Used to default to None, which left the feature doing nothing.
- **Device serial numbers masked across logs and entity attributes** — Battery sensors, connection sensors, device tracker entities, log messages, and HomeKit mappings now show the first 6 characters followed by `…`. The bridge serial is printed on the device and the auth code is only 4 digits, so a leaked serial in a shared log could enable brute-force pairing.
- **Diagnostics dump redacts more sensitive fields** — Bridge serial, additional Tado API response fields, and other sensitive values are scrubbed from `Settings → Devices → Tado CE → Download Diagnostics`. Diagnostics also include a `data_flow_health` section with last cloud fetch timestamp, HomeKit and bridge connection status, and persistence state.
- **Climate entities show where their data comes from** — `temperature_source` and `humidity_source` attributes show `cloud`, `homekit`, or `external` instead of the legacy `tado` label. A new `last_write_source` attribute shows whether the most recent change went through HomeKit or the cloud.
- **Window Predicted, heating rate, and insights now react to real-time HomeKit data** — Window detection, heating cycle tracking, mold risk, comfort level, humidity trends, and heating anomalies were using cloud data even when HomeKit had fresher readings. They now use the same live data as the rest of the integration.
- **Plain-English log messages, with consistent prefixes and recovery hints** — Internal terms (`backed-off`, `bang-bang fallback`, `optimistic state expired`, `ROLLBACK`) replaced with plain-English equivalents. Every line now starts with a clear subsystem label (`Coordinator:`, `Climate AC:`, `HomeKit:`, `Smart Valve:`, etc.) so you can filter by feature when something goes wrong. Failures explain what the integration did about it ("rolling back optimistic state", "will retry on the next poll", "captured baseline preserved so you can retry"), so a log paste is enough to diagnose without a back-and-forth. Per-write chatter that used to flood multi-zone homes at info level is now debug; once-per-startup / shutdown summaries stay at info. Device serials and home IDs are masked everywhere so logs are safe to share publicly.

### Known Issues

- **`register_detection_callback() is deprecated` warning if HomeKit is enabled** — The warning comes from the `aiohomekit` library's internal BLE scanning code, not from Tado CE. It does not affect functionality and will disappear when `aiohomekit` releases an update. You can safely ignore it.

## [3.5.3] - 2026-04-08

### ⚠️ Prerequisites
- **Minimum Home Assistant version is now 2025.11** — Required by the smarter rate limit handling below. If you're on an older HA release, stay on v3.5.2 or upgrade HA first.

### Improvements
- **Overlay sensor now shows timer end time** ([#217](https://github.com/hiall-fyi/tado_ce/issues/217)) — When a Timer overlay is active, the `next_change` attribute now shows when the timer expires instead of the next schedule change. Two new attributes are also available: `overlay_expiry` (the exact end time) and `overlay_remaining_seconds` (countdown). Manual and Tado Mode overlays continue to show the next schedule change as before.
- **Smarter rate limit handling** — When the API quota is exhausted, the integration now tells HA exactly how long to wait before the next poll (using the known reset time) instead of using a fixed 15-minute retry. This means polling resumes faster after a quota reset.
- **Retry delay capped** — Exponential backoff delay is now capped at 30 seconds to prevent excessively long waits on repeated failures.
- **Device authorization polls less aggressively** — When signing in via device authorization, the integration now waits 5 seconds between polls instead of 2, matching the OAuth standard recommendation. You may notice the "waiting for browser authorization" step take a touch longer on fast networks, but it's less likely to hit rate limits on slow ones.

---

## [3.5.2] - 2026-04-06

### Bug Fixes
- **Fixed token refresh and API calls not retrying on DNS/network failures** ([#214](https://github.com/hiall-fyi/tado_ce/issues/214)) — If your DNS server briefly refused a query or the connection to Tado's servers timed out, the integration would give up immediately instead of retrying. This could leave all entities unavailable until the next poll cycle. Now token refresh and all API calls retry up to 3 times with exponential backoff on any transient network error (DNS failures, connection timeouts, connection resets), matching the existing 403 retry behaviour.

---

## [3.5.1] - 2026-04-06

**Reliability & Code Quality**

### Bug Fixes
- **Fixed stale insights sticking around after resolving** — The Home Insights sensor could show issues that had already resolved but were still within the reappearance grace period. For example, a mold risk warning that cleared would keep showing in the "persistent issues" list for up to an hour. Now only genuinely active issues appear.

### Improvements
- **More resilient API calls** — All API operations (temperature offsets, child lock, zone overlays, presence lock, schedules, meter readings, away configuration) now automatically retry when the Tado cloud returns a transient 403 error. Previously, only the main polling calls had retry logic — actions like changing temperature or toggling child lock would silently fail on a brief block from Tado's network protection layer. Now every cloud API call retries up to 3 times with increasing delays before giving up.
- **Token refresh also retries on 403** — If the Tado login server returns a transient 403 during token refresh, the integration now retries instead of immediately failing. Real authentication errors (401, invalid_grant) are still handled instantly without retry.
- **Background tasks shut down cleanly** — The write-batching system's background tasks (slider debounce and refresh coalescing) now log errors instead of failing silently, and cancel pending work on shutdown.

_Performance & storage_

- **Faster polling** — Rate limit data is now read from memory instead of reading a file from disk on every poll cycle. Small wins compound on low-quota homes that poll frequently.

---

## [3.5.0] - 2026-04-02

**Redesigned Settings & Architecture Overhaul**

### Features
- **Redesigned Options Flow** — Settings are now split into four clear sections: General Settings (feature toggles), Advanced Settings (tuning parameters for enabled features only), Zone Configuration, and Reset to Defaults. You no longer need to scroll through 79 options on one page. First-time setup for Internet Bridge and Weather Compensation now guides you through credentials step by step. You can also reset settings back to defaults (per feature or everything at once) without losing your Tado account or bridge pairing.

### Bug Fixes
- **Fixed quota deadlock on clean install** ([#204](https://github.com/hiall-fyi/tado_ce/issues/204) - @Saughassy) — On a fresh install with stale rate limit data and no known reset time, the integration could get stuck permanently in "quota critically low" state. Now allows both polling and manual actions to bootstrap fresh data when no reset time is known.
- **Fixed temperature offset not updating after service call** ([#211](https://github.com/hiall-fyi/tado_ce/issues/211) - @mat01) — After calling `set_climate_temperature_offset`, the `offset_celsius` attribute kept showing the old value until the next HA restart. Automations that read-then-write offsets would oscillate. Now updates the local cache immediately so the new offset is reflected right away.

### Improvements

_Options Flow & descriptions_

- **Clearer Settings Descriptions** ([Discussion #131](https://github.com/hiall-fyi/tado_ce/discussions/131) - @Prodeguerriero) — All option descriptions in General Settings and Advanced Settings have been rewritten in plain language. Technical jargon like "rate calculation", "inertia end", and "setpoint deviation" has been replaced with descriptions that explain what each setting actually does for you. Mobile Device Tracking now clearly states that locations only update on HA restart unless you enable Frequent Sync. API cost info uses consistent "per poll" / "on restart" wording instead of the confusing "full sync" / "quick sync" distinction.
- **Removed Legacy Options Flow** — The old single-page "Global Settings" flow has been fully removed (code, strings, and all translations). If you see a stale UI after upgrading, clear your browser cache.

_Insights & comfort_

- **Smarter, Cleaner Insights** — Insights got a full overhaul. Recommendations now only appear when they're relevant to your actual settings (e.g. no geofencing alerts when you have geofencing off). The Home Insights summary focuses on the single most urgent action with a clear reason, instead of listing everything. Empty attributes no longer clutter the sensor. Persistent issues show escalated priority (a battery problem lasting 2 weeks shows as Critical, not Low). Zone-level sensors now include how long an issue has been active. The weekly digest is a simple trend comparison — new, resolved, up or down from last week.
- **More Accurate "Feels Like" Temperature** — The Heat Index calculation no longer has a small jump at the transition point (~27°C). Previously, a tiny humidity increase could briefly make the "feels like" temperature drop instead of rise. Now the transition is smooth.

_Performance & storage_

- **Faster Startup** — The Insights feature now loads only the parts you have enabled instead of everything on every restart.

_Data integrity_

- **More state now survives HA restarts** — Weather compensation settings, bridge health status, and window detection history are now persisted and restored across restarts instead of resetting to defaults or empty on startup.
- **Configuration now lives entirely in the HA config entry** — The separate `config_{home_id}.json` file is no longer written. Existing files are migrated automatically on upgrade — no action needed.
- **Config entry version bumped to v12 with automatic migration** — Upgrading from earlier v3.x releases migrates your data to the new format on first start.

---

## [3.4.1] - 2026-03-26

### Bug Fixes
- **Fixed crash on clean install** ([#204](https://github.com/hiall-fyi/tado_ce/issues/204) - @Saughassy) — On a fresh install, the integration could fail to start with a Python `TypeError` before rate limit data was fully populated — the adaptive polling calculator was dividing by fields that hadn't arrived yet. All rate limit fields are now treated as optional during first setup, so a clean install boots cleanly even before the first API response lands.

---

## [3.4.0] - 2026-03-23

### ⚠️ Prerequisites
- **Minimum supported upgrade path is now v3.0.0+** — All migration code for upgrading from v2.x has been removed. Users still on v2.x should upgrade to a v3.x release first before taking this update.

### Features
- **API Write Optimization** — All enabled by default. Three new settings under **Settings → Tado CE → Configure → Global Settings → Polling & API** to reduce unnecessary API calls:
  - **Smart Actions Debounce** — When you drag a temperature slider, only the final value is sent to the API instead of every intermediate position. Configurable window (0–10 seconds, default 3). Set to 0 to disable.
  - **Action Guard** — Skips API calls when the requested state already matches the current state (e.g. setting 22°C when it's already 22°C). Always active.
  - **Device Sync Queue** — Device-level operations (child lock, early start) are now queued and executed sequentially with a configurable delay (0.5–5 seconds, default 1), preventing race conditions from rapid toggling. Always active.
  - **Write Coalescing** — Multiple rapid state changes trigger a single coordinated refresh instead of one per change. Always active.
  - **Resume Guard** — Resuming a zone's schedule is skipped if the zone is already following its schedule. Always active.
- **Schedule Preview** — Heating and AC climate entities now show a `scheduled_target_temperature` attribute with the current schedule target, so you can see what temperature the zone would be at without an overlay.

### Bug Fixes
- **Fixed Window Detection mode selector compatibility** — The window detection mode selector used capitalised option keys (`Active`, `Passive`, `Auto`) which Home Assistant's integration checks now reject. Switched to lowercase across all 7 languages. No user action needed — option labels in the UI are unchanged.

### Improvements
- **UFH Buffer Now Per-Zone** — Underfloor heating buffer is now configured per zone (via Zone Configuration) instead of a global setting. Zones with heating type set to Underfloor automatically get the buffer applied.
- **Crash-safe saves for zone config and outdoor temperature history** — These files now use the same crash-safe save method as other Tado CE data, so an unexpected shutdown can't leave them half-written.
- **Translation Sync** — Added missing Adaptive Preheat Mode selector translations across all 7 languages (German, Spanish, French, Italian, Dutch, Portuguese).

## [3.3.1] - 2026-03-21

### Improvements
- **Smarter Weather Compensation Curve** ([#187](https://github.com/hiall-fyi/tado_ce/issues/187) - @driagi) — Preset heating curves (Radiators Standard, Radiators Low Temp, Underfloor) now automatically calculate the slope from your min/max flow and design/shutoff temperatures. Previously, a fixed slope could cause the flow temperature to hit the minimum floor well before the shutoff temperature, creating a flat zone where outdoor changes had no effect. Now the curve modulates smoothly across the entire outdoor range. Custom preset still gives you full manual control.

## [3.3.0] - 2026-03-21

### Features
- **Weather Compensation** ([#187](https://github.com/hiall-fyi/tado_ce/issues/187) - @driagi) — Automatically adjusts your boiler's flow temperature based on outdoor temperature. Pick from three presets (Radiators Standard, Radiators Low Temp, Underfloor) or create a custom heating curve. Includes smoothing, room feedback, and a 10-minute hold between adjustments to prevent oscillation. Configured via **Settings → Tado CE → Configure → Global Settings → Flow Temperature Control**.
- **Enable Internet Bridge Toggle** — Simple on/off toggle at the top of Flow Temperature Control. Turn it off and all bridge-related entities are automatically removed — no need to manually clear credentials.
- **Adaptive Preheat Passive Mode** ([#171](https://github.com/hiall-fyi/tado_ce/issues/171) - @thefern69) — Preheat now has three modes per zone: Off, Active (always triggers), and Passive (only triggers when the zone is following its schedule — skips preheat if you've set a manual override from HomeKit, the Tado app, etc.). Existing users with preheat enabled are automatically migrated to Active. Configured via **Options Flow → Zone Configuration**.
- **Restore Previous State** — New `tado_ce.restore_previous_state` service that puts a zone back to whatever it was doing before the last change. Works with heating, AC, and hot water. State is saved automatically before any overlay action (timers, temperature changes, open window mode, preheat). If nothing was saved, it falls back to resuming the schedule. Saved state survives HA restarts and clears when you arrive home.
- **Passive Window Detection** — Detects open windows even when your heating or AC is off. Combines temperature drop speed, humidity changes, and indoor-outdoor temperature difference to tell the difference between a natural cooldown and an open window. Adjusts for your window type and is stricter in winter.
- **Window Detection Mode Per Zone** — Choose how each zone detects open windows: Active (only when heating/cooling is running), Passive (works anytime), or Auto (picks the best method automatically). Configured via **Options Flow → Zone Configuration**.
- **Heat Index & Heat Risk** — The Comfort Level sensor now shows "feels like" temperature when it's warm (above 26.7°C), factoring in humidity. Risk levels (Caution, Extreme Caution, Danger, Extreme Danger) appear in the sensor attributes and in comfort recommendations.
- **Bridge Connected Sensor** — New binary sensor that shows whether your Internet Bridge is reachable. Includes health info like response time, failure count, and last successful connection as attributes.
- **Bridge Health Tracking** — The bridge connection sensor tolerates brief hiccups — it only marks the bridge as disconnected after 3 consecutive failures, so a single timeout won't trigger a false alarm.

### Bug Fixes
- **Fixed Preheat Triggering During Away Mode** ([#171](https://github.com/hiall-fyi/tado_ce/issues/171) - @thefern69) — Preheat could still fire during the Home→Away transition due to a timing gap. Now properly checks presence before any heating action, including on startup.
- **Fixed Open Window Mode Duration** — The `set_open_window_mode` service was sending the duration as text instead of a number, which could cause the Tado API to reject the request. Now sends it correctly.

### Improvements

_Flow Temperature Control & Bridge_

- **Flow Temperature Control Settings** — Bridge credentials and weather compensation settings are now in one place instead of two separate menus, so there's less clicking around.
- **Fewer Bridge Entities by Default** — Only the most useful bridge entities are visible out of the box (Bridge Connected, Wiring State, Boiler Output Temperature, Boiler Flow Temperature). The rest are hidden and can be enabled manually if you need them.
- **Bridge Serial Validation** ([#187](https://github.com/hiall-fyi/tado_ce/issues/187) - @ChrisMarriott38) — The bridge serial field now checks that it starts with `IB` (v3+ bridge). V2 bridges (`GW` serial) aren't supported by the Bridge API. Weather Compensation still works without a bridge via cloud data.
- **Weather Compensation Blueprint Updated** ([#187](https://github.com/hiall-fyi/tado_ce/issues/187) - @driagi) — Blueprint tuned to reduce oscillation: larger step size (1.0°C), wider deadband (1.0°C), and 10-minute hold between adjustments.

_Window Detection_

- **Smoother Window Detection** — The Window Predicted sensor no longer flickers on/off rapidly. It now waits for several stable readings before clearing a detection (3 readings on Low sensitivity, 2 on Medium, 1 on High).
- **Window Detection Events** — HA events (`tado_ce_window_predicted` and `tado_ce_window_predicted_cleared`) now fire when a window is detected or cleared — useful for building your own automations.
- **Window Detection History** — The Window Predicted sensor now tracks when the last detection happened, how many times today, and which detection mode was used. Daily count resets at midnight.
- **Open Window Mode Saves State** — When `set_open_window_mode` activates, it now saves what the zone was doing first. After the window is closed, use `restore_previous_state` to go back to exactly where you were.

_Other_

- **Default Temperature on First Install** ([#182](https://github.com/hiall-fyi/tado_ce/issues/182) - @neonsp) — Climate entities now start with a sensible default (20°C heating, 24°C AC) instead of showing blank controls on first install.

## [3.2.2] - 2026-03-16

### Bug Fixes
- **Fixed Boiler Output Temperature Sensor Showing Wrong Value** ([#187](https://github.com/hiall-fyi/tado_ce/issues/187) - @driagi) — The boiler output temperature sensor was reading from the wrong data field, so it never showed the actual temperature. Now displays the correct real-time value. Also fixed the Wiring State sensor's extra attributes.

## [3.2.1] - 2026-03-16

### Features
- **Indefinite Open Window Mode** ([Discussion #184](https://github.com/hiall-fyi/tado_ce/discussions/184) - @jeverley) — The `set_open_window_mode` service now supports `duration: 0` to keep the window mode active until you manually resume. Great for contact sensor automations where you want full control.

### Bug Fixes
- **Fixed Bridge Sensor Showing "Unknown"** ([#187](https://github.com/hiall-fyi/tado_ce/issues/187) - @driagi) — Bridge API sensors were stuck on "Unknown" even when the bridge was connected. Now shows boiler temperature data correctly. Added better logging for troubleshooting.

## [3.2.0] - 2026-03-16

**Bridge API Integration — Flow Temperature Control**

### Features
- **Bridge API Integration** — Connect to your Tado Internet Bridge for direct boiler monitoring. Enter your bridge serial and auth key in Settings, and you'll get sensors for boiler wiring state, output temperature, and a control to set the max output temperature (25–80°C). Bridge data is fetched separately from the cloud, so bridge issues never affect your other sensors.
- **Bridge Entity Cleanup** — Remove your bridge credentials and all bridge-related entities are automatically cleaned up.
- **Set Open Window Mode Service** ([#172](https://github.com/hiall-fyi/tado_ce/issues/172), [Discussion #184](https://github.com/hiall-fyi/tado_ce/discussions/184) - @driagi) — New `set_open_window_mode` service lets you trigger open window mode from your own contact sensors (Zigbee, Z-Wave, etc.) without waiting for Tado's built-in detection. Defaults to your zone's timeout setting or 15 minutes.

### Bug Fixes
- **Fixed Climate Card Blank After HA Restart** ([#182](https://github.com/hiall-fyi/tado_ce/issues/182) - @neonsp) — The climate card no longer shows a blank temperature after restarting HA. Your last target temperature is now restored automatically, so the controls work right away.
- **Fixed External Sensor Not Updating Instantly** ([#143](https://github.com/hiall-fyi/tado_ce/issues/143) - @BirbByte) — External temperature and humidity sensors (HomeKit, Zigbee, etc.) now update the climate card immediately when the value changes, instead of waiting for the next poll cycle.

## [3.1.1] - 2026-03-15

**Manual Token Auth, Climate Card Fix & Smart AC Mode**

### Features
- **Manual Token Authentication** ([#185](https://github.com/hiall-fyi/tado_ce/issues/185)) — New fallback login method for when Tado's authorization server is down. You can now paste a refresh token from the Tado web app as an alternative way to sign in.

### Bug Fixes
- **Fixed Climate Card Unusable When Zone is OFF** ([#182](https://github.com/hiall-fyi/tado_ce/issues/182)) — When a heating zone is off, the climate card now keeps the last temperature showing so the slider and controls still work.

### Improvements
- **Updated Global Settings Description** ([Discussion #76](https://github.com/hiall-fyi/tado_ce/discussions/76)) — The setup instructions now match the current toggle-based settings layout.
- **AC Picks the Right Mode Automatically** ([#182](https://github.com/hiall-fyi/tado_ce/issues/182) - @neonsp) — When you turn on an AC zone by setting a temperature, it now picks HEAT or COOL based on whether the target is above or below the current temperature.

## [3.1.0] - 2026-03-14

**Options Flow Zone Configuration, Open Window Services, External Sensor Override & Entity Registry**

### Features
- **Open Window Services** ([#172](https://github.com/hiall-fyi/tado_ce/issues/172) - @driagi) — New `activate_open_window` and `deactivate_open_window` services let you trigger open window mode from your own window sensors (e.g., Zigbee contact sensors) instead of waiting 15+ minutes for Tado to detect it. A free Auto-Assist replacement via HA automations.
- **External Temperature & Humidity Sensors** ([#106](https://github.com/hiall-fyi/tado_ce/issues/106), [#143](https://github.com/hiall-fyi/tado_ce/issues/143) - @BirbByte) — Use any HA sensor (HomeKit, Zigbee, etc.) instead of Tado's built-in sensor for each zone. Configured in Zone Configuration.
- **Window Predicted Sensitivity** ([#135](https://github.com/hiall-fyi/tado_ce/issues/135) - @ChrisMarriott38) — Adjust how sensitive the open window prediction is per zone (Low, Medium, or High) to reduce false alarms.
- **Hub Control Switches** — New Test Mode and Quota Reserve switches on the hub device. Toggle them from your dashboard without going into Settings.
- **Options Flow Menu** — Settings are now organized into Global Settings and Zone Configuration sections for easier navigation.

### Bug Fixes
- **Fixed AC Max Temperature Capped at 25°C** ([#180](https://github.com/hiall-fyi/tado_ce/issues/180)) — AC zones that support up to 30°C were incorrectly limited to 25°C. Now uses your AC's actual temperature range unless you've set a custom override.

### Improvements
- **Zone Configuration Moved to Options Flow** — All per-zone settings (overlay mode, timer, temperature limits, offsets, heating type, etc.) are now in one place under Settings → Configure → Zone Configuration. No more config entities cluttering your dashboard.
- **Renamed "Tado Mode" → "Tado Default"** ([#176](https://github.com/hiall-fyi/tado_ce/issues/176)) — The overlay mode name now matches what Tado calls it.
- **Entity Categories Added** ([#178](https://github.com/hiall-fyi/tado_ce/issues/178)) — Configuration and diagnostic entities are now properly categorized, so they're organized correctly in the HA UI.
- **Smarter Full Sync** ([#141](https://github.com/hiall-fyi/tado_ce/issues/141) - @Xavinooo) — Full data sync now only runs on HA restart instead of every 6 hours, saving API calls.
- **Test Mode & Quota Reserve Skip Reload** — Toggling these settings no longer restarts the integration.
- **Health Score Formatting** — Home Insights health score now shows with emoji and label (e.g., "🟢 92 — Excellent") for quick reading.
- **Improved Translations** — All 6 non-English languages revised with more natural wording. Service names and descriptions now translated in all 7 languages.

## [3.0.4] - 2026-03-12

### Bug Fixes
- **Fixed API Reset Time Estimate Off by Hours** ([#173](https://github.com/hiall-fyi/tado_ce/issues/173) - @driagi) — The estimated API reset time was off by about 6 hours. Now calculates more accurately using your actual day/night schedule.
- **Fixed API Reset History Detection** ([#173](https://github.com/hiall-fyi/tado_ce/issues/173)) — History-based reset detection was silently failing. Added logging to help troubleshoot.
- **Fixed Smart Comfort Description** — The options flow description was listing the wrong sensors. Now correctly describes what's included.

### Improvements
- **Better Entity Cleanup** — Removing entities is now more accurate and won't accidentally remove the wrong ones.

## [3.0.3] - 2026-03-11

### Bug Fixes
- **Fixed Hub Sensors Stuck on Old Values** ([#173](https://github.com/hiall-fyi/tado_ce/issues/173) - @driagi) — Hub sensors (API Limit, Reset Time, Status, Polling Interval, Next Sync) were not updating after each sync. Now refreshes in real-time.

### Improvements
- **Better Feature Toggle Cleanup** — Disabling Weather or Mobile Device features now properly removes their entities and leftover devices.

## [3.0.2] - 2026-03-11

### Bug Fixes
- **Fixed Setup Hanging for Minutes** ([#170](https://github.com/hiall-fyi/tado_ce/issues/170) - @driagi, @tigro7, @mpartington) — The integration could hang during setup for up to 80 minutes if you had old API call history. Now starts up normally.
- **Fixed Preheat Firing During Away Mode** ([#171](https://github.com/hiall-fyi/tado_ce/issues/171) - @thefern69) — Preheat was still triggering even when nobody was home. Now correctly pauses when you're away.
- **Fixed Hub Sensors Showing Wrong Values** ([#173](https://github.com/hiall-fyi/tado_ce/issues/173) - @driagi) — Polling interval and reset time sensors were showing defaults instead of actual values.
- **Fixed Performance Warning During Sensor Update** — Resolved a warning caused by slow file access during sensor updates.
- **Fixed Raw Values in Insights** — Home Insights were showing internal codes instead of readable names.

### Improvements
- **Smarter Preheat for Active Heating** ([Discussion #163](https://github.com/hiall-fyi/tado_ce/discussions/163) - @thefern69) — Preheat now also considers cooling trends for the current temperature target, not just the next schedule change. Especially helpful for underfloor heating.
- **Cleaner Insights Display** — Insights now show grouped, emoji-prefixed lines for easier reading.
- **Shorter Recommendations** — Per-zone recommendations no longer repeat the zone name.

## [3.0.1] - 2026-03-10

### Bug Fixes
- **Removed `[CE]` Prefix from Entity Names** ([#167](https://github.com/hiall-fyi/tado_ce/issues/167) - @jeverley) — Entity names no longer include the `[CE]` prefix. Entity IDs are unchanged.
- **Fixed Duplicate Sensor Names** ([#167](https://github.com/hiall-fyi/tado_ce/issues/167) - @hapklaar) — Zones with multiple devices (e.g., a sensor and two valves) now include the device type in battery and connection sensor names to avoid duplicates.
- **Fixed Entities Going Unavailable Every 5 Minutes** ([#167](https://github.com/hiall-fyi/tado_ce/issues/167) - @hapklaar, @andyb2000) — Token refresh was accidentally restarting the integration every poll cycle, briefly making all entities unavailable. Now handles token updates silently.
- **Fixed Missing Translations** — 3 translation keys added to all 7 language files.

## [3.0.0] - 2026-03-10

**Multi-Home Support & Actionable Insights**

### Features
- **Multi-Home Support** ([#110](https://github.com/hiall-fyi/tado_ce/issues/110) - @robvol87, [#145](https://github.com/hiall-fyi/tado_ce/issues/145) - @Blankf) — Run multiple Tado accounts or homes in a single HA instance. Each home is fully isolated with its own data and settings.
- **Smarter Insight Summaries** — Home insights now show action-based summaries (e.g., "Replace batteries: Guest, Lounge — Mold risk: Bedroom") instead of generic counts.
- **Related Insights Merged** — Related issues in a zone are combined into a single action item (e.g., mold risk + high humidity + condensation → "humidity problem").
- **Insight History & Trending** — Insights are tracked across HA restarts with duration-aware messages and a weekly digest.
- **Insight Priority Escalation** — Issues that persist automatically escalate in priority (e.g., low battery for over 7 days becomes high priority).
- **Home Health Score** — A 0-100 score reflecting your overall home health, shown on the Home Insights sensor.
- **Preheat Cooling Rate Prediction** ([Discussion #163](https://github.com/hiall-fyi/tado_ce/discussions/163) - @thefern69) — Preheat now considers cooling trends when the room is above target, estimating when it will drop below target for proactive heating.

### Bug Fixes
- **Fixed Auth URL Showing 404** ([#104](https://github.com/hiall-fyi/tado_ce/issues/104)) — The authorization link during setup no longer leads to a broken page.
- **Fixed Window Sensor Not Working Without Auto-Assist** ([#157](https://github.com/hiall-fyi/tado_ce/issues/157) - @tanerpaca) — Window sensor now detects open windows even without an Auto-Assist subscription.
- **Fixed Preheat Triggering a Day Early** ([#164](https://github.com/hiall-fyi/tado_ce/issues/164) - @thefern69) — Preheat sensor no longer fires a day before it should.
- **Fixed Heating Rate Unit** — Heating rate was showing inconsistent values depending on which internal path computed it — some sites reported °C/min, others °C/h. Standardised on °C/h everywhere and set the sensor's unit attribute accordingly.

### Improvements
- **Timer Minimum Lowered to 1 Minute** ([#162](https://github.com/hiall-fyi/tado_ce/issues/162) - @joaomacp) — Climate and water heater timers now accept durations as short as 1 minute (was 15).
- **7-Language Support** — Config flow and options UI now available in English, German, Spanish, French, Italian, Dutch, and Portuguese.

## [2.3.1] - 2026-02-26

### Bug Fixes
- **Fixed AC Fan Speed Reverting on Some Brands** ([#142](https://github.com/hiall-fyi/tado_ce/issues/142) - @BirbByte) — Setting "High" fan speed on Mitsubishi/Fujitsu units would revert back. Fan speed options are now built from your AC's actual capabilities.
- **Fixed Startup Warning on Fresh Install** ([#127](https://github.com/hiall-fyi/tado_ce/issues/127) - @slflowfoon) — Resolved a warning that appeared on first install when the data folder didn't exist yet.

### Improvements
- **AC Capabilities Live Reload** — After pressing "Refresh AC Capabilities", AC entities update automatically without restarting HA.
- **Smarter AC Temperature Defaults** — Default temperature when switching modes now uses your AC's actual range instead of a fixed value.
- **Smoother AC Controls** — Fan and swing mode selections no longer flicker during updates.

---

## [2.3.0] - 2026-02-25

### Features
- **21 New Insight Types** — More actionable insights across zone efficiency, schedule, occupancy, weather, humidity, device health, and cross-zone analysis.

### Bug Fixes
- **Fixed Mold Risk Giving Wrong Advice** ([#147](https://github.com/hiall-fyi/tado_ce/issues/147) - @ChrisMarriott38) — Mold risk no longer suggests turning up the heating when the room is already warm enough. Now suggests ventilation or a dehumidifier instead.
- **Fixed Hot Water Overlay Showing for Combi Boilers** ([#149](https://github.com/hiall-fyi/tado_ce/issues/149) - @ChrisMarriott38) — Overlay Mode and Timer Duration entities no longer appear for combi boiler hot water zones where they don't apply.
- **Fixed Mobile Device Tracker Not Updating** ([#150](https://github.com/hiall-fyi/tado_ce/issues/150) - @driagi) — Device tracker was stuck on the state from last HA restart. Now updates every 30 seconds.

### Improvements
- **Flexible Climate Timer** ([#152](https://github.com/hiall-fyi/tado_ce/issues/152) - @mpartington) — `time_period` is now optional in the `set_climate_timer` service. Use `overlay: next_time_block` for "until next schedule change" or `overlay: manual` for indefinite.

---

## [2.2.3] - 2026-02-24

**Smart Day/Night Polling, AC Fan Fix & Climate Group Support**

### Bug Fixes
- **Fixed Polling Stuck at 120 Minutes for Low-Quota Users** ([#144](https://github.com/hiall-fyi/tado_ce/issues/144) - @mkruiver) — Users with few API calls left now get smart day/night polling instead of being stuck on very slow intervals.
- **Fixed Night Polling Using Wrong Interval** ([#141](https://github.com/hiall-fyi/tado_ce/issues/141) - @Xavinooo) — Day period quota now correctly accounts for your custom night interval setting.
- **Fixed AC Fan Speed Reverting** ([#142](https://github.com/hiall-fyi/tado_ce/issues/142) - @BirbByte) — Fan speed validation now checks against your AC's actual supported levels.

### Improvements
- **Climate Group Support** ([#139](https://github.com/hiall-fyi/tado_ce/discussions/139) - @merlinpimpim) — `set_climate_timer`, `set_water_heater_timer`, and `resume_schedule` services now work with climate groups defined in `configuration.yaml`.

---

## [2.2.2] - 2026-02-23

### Bug Fixes
- **Fixed API Polling Options Not Saving** ([#134](https://github.com/hiall-fyi/tado_ce/issues/134) - @Xavinooo) — Custom polling intervals now save correctly even when only one field is filled, and clearing a custom interval properly persists.

---

## [2.2.1] - 2026-02-23

### Bug Fixes
- **Fixed Hot Water Settings Missing for Tank Systems** ([#115](https://github.com/hiall-fyi/tado_ce/issues/115) - @jeverley) — Tank-based hot water users now correctly see Overlay Mode and Timer Duration controls.
- **Fixed Custom Polling Intervals Not Saving** ([#134](https://github.com/hiall-fyi/tado_ce/issues/134) - @ChrisMarriott38, @Xavinooo) — Custom polling intervals now save correctly after changing options.

---

## [2.2.0] - 2026-02-23

**Calibration Sensors & Actionable Insights**

### Features
- **Surface Temperature Sensor** ([#118](https://github.com/hiall-fyi/tado_ce/issues/118)) — Shows the calculated cold spot temperature in each zone, so you can calibrate mold risk with a laser thermometer.
- **Dew Point Sensor** ([#118](https://github.com/hiall-fyi/tado_ce/issues/118)) — Useful for dehumidifier automations and condensation prevention.
- **Window Predicted Sensor** ([Discussion #112](https://github.com/hiall-fyi/tado_ce/discussions/112) - @tigro7) — Detects open windows early by spotting unusual temperature drops, giving you a heads-up minutes before Tado's cloud detection kicks in.
- **Actionable Recommendations** ([Discussion #112](https://github.com/hiall-fyi/tado_ce/discussions/112) - @tigro7) — Environment, device, and hub sensors now include a `recommendation` attribute with specific advice.
- **Home Insights Sensor** ([Discussion #112](https://github.com/hiall-fyi/tado_ce/discussions/112) - @tigro7) — A hub-level sensor that aggregates insights from all zones with priority ranking.
- **Zone Insights Sensor** ([Discussion #112](https://github.com/hiall-fyi/tado_ce/discussions/112) - @tigro7) — Per-zone insights with an icon that changes based on the highest priority issue.

### Bug Fixes
- **Fixed Long Heating Cycles Never Completing** ([#125](https://github.com/hiall-fyi/tado_ce/issues/125) - @BruceRobertson) — Heating cycles longer than 50 minutes now complete correctly.
- **Fixed First-Run Error** ([#127](https://github.com/hiall-fyi/tado_ce/issues/127) - @slflowfoon, [PR #132](https://github.com/hiall-fyi/tado_ce/pull/132) - @hacker4257) — Fixed an error on first install when the storage folder didn't exist yet.
- **Fixed Hot Water Settings Missing for Tank Systems** ([#115](https://github.com/hiall-fyi/tado_ce/issues/115) - @jeverley) — Tank-based hot water systems now correctly get Overlay Mode and Timer Duration controls.
- **Fixed Custom Polling Issues** ([#126](https://github.com/hiall-fyi/tado_ce/issues/126) - @Xavinooo) — Custom night interval, day/night detection, and settings persistence all fixed.
- **Fixed AC Swing Mode for Mitsubishi Units** ([#128](https://github.com/hiall-fyi/tado_ce/issues/128) - @BirbByte) — AC units that don't support "OFF" as a swing value no longer get API errors.
- **Fixed Environment Sensor Cleanup** — All sensors now correctly removed when the feature is toggled off.
- **Fixed Heating Anomaly False Alarms** — Heating anomaly insight no longer fires on every update.

### Improvements
- **Readable Attribute Values** — Zone type, window type, and comfort model attributes now show human-readable names instead of internal codes.

---

## [2.1.1] - 2026-02-19

### Bug Fixes
- **Fixed Test Mode Using Wrong Reset Time** ([#120](https://github.com/hiall-fyi/tado_ce/issues/120), [#119](https://github.com/hiall-fyi/tado_ce/issues/119) - @ChrisMarriott38) — Test Mode polling intervals now calculate correctly.
- **Fixed Hot Water Zones Showing Wrong Entities** ([#115](https://github.com/hiall-fyi/tado_ce/issues/115) - @ChrisMarriott38) — Per-zone configuration controls (Surface Temp Offset, Min/Max Temp, etc.) no longer appear for hot water zones where they don't apply.

## [2.1.0] - 2026-02-18

**Per-Zone Configuration**

### Features
- **Per-Zone Overlay Mode** — Choose how long manual temperature changes last, separately for each zone (Tado Default, Timer, or Manual).
- **Per-Zone Timer Duration** — Set a custom timer duration per zone (15–180 minutes).
- **Per-Zone Thermal Analytics** ([#91](https://github.com/hiall-fyi/tado_ce/issues/91)) — Choose which zones have Thermal Analytics sensors. Zones that never call for heat can be turned off to keep your UI clean.
- **Per-Zone Surface Temp Offset** ([#90](https://github.com/hiall-fyi/tado_ce/issues/90)) — Calibrate mold risk calculation per zone using a laser thermometer reading.

### Bug Fixes
- **Fixed Preheat Time Showing `unknown` After Restart** — Preheat Time sensors now display correctly after HA restart.
- **Fixed Overlay Mode API Error** — Overlay termination now correctly maps to the Tado API.
- **Fixed Custom Polling Below 5 Minutes** ([#107](https://github.com/hiall-fyi/tado_ce/issues/107) - @jakeycrx) — Custom intervals of 1–4 minutes now work correctly.

### Improvements
- **Simplified Options UI** — Test Mode moved into the Tado CE Exclusive section for a cleaner layout.

## [2.0.2] - 2026-02-14

**Presence Mode Select & Configurable Overlay Mode**

### ⚠️ Breaking Changes
- **Presence Mode Select** — `switch.tado_ce_away_mode` has been replaced by `select.tado_ce_presence_mode` ([Discussion #102](https://github.com/hiall-fyi/tado_ce/discussions/102) - @wyx087). Update your automations from `switch.turn_on/turn_off` to `select.select_option`.

### Features
- **Presence Mode Select** — New 3-option select: Auto (resume geofencing), Home, or Away.
- **Configurable Overlay Mode** ([#101](https://github.com/hiall-fyi/tado_ce/issues/101) - @leoogermenia) — Choose how long manual temperature changes last: Tado Default, Next Time Block, or Manual (indefinite).

### Bug Fixes
- **Fixed Custom Polling Below 5 Minutes** ([#107](https://github.com/hiall-fyi/tado_ce/issues/107) - @jakeycrx) — Custom intervals of 1–4 minutes now work.
- **Fixed Polling Stuck at 120 Minutes** ([#99](https://github.com/hiall-fyi/tado_ce/issues/99) - @ChrisMarriott38) — Polling now calculates correctly when day and night hours are the same.

## [2.0.1] - 2026-02-12

**Mold Risk Percentage, Hot Water Fix & Bootstrap Reserve**

### Features
- **Mold Risk Percentage Sensor** ([#90](https://github.com/hiall-fyi/tado_ce/issues/90)) — New sensor for tracking mold risk over time in HA history.
- **Bootstrap Reserve Protection** ([#99](https://github.com/hiall-fyi/tado_ce/issues/99) - @ChrisMarriott38) — Reserves 3 API calls so the integration can always recover after a restart.
- **Test Mode** — Fully simulates the 100-call API tier with its own 24-hour cycle for testing.
- **Day/Night Aware Polling** — Uses fixed 120-minute intervals at night and adaptive intervals during the day based on remaining quota.

### Bug Fixes
- **Fixed Climate Entities Unavailable After Upgrade** ([#100](https://github.com/hiall-fyi/tado_ce/issues/100) - @Claeysjens) — Climate entities could stay unavailable after upgrading from an earlier v2.x release because of a state migration gap. Now restores correctly on first load after upgrade.
- **Fixed Hot Water Temperature Jumping Back** ([#98](https://github.com/hiall-fyi/tado_ce/issues/98) - @ChrisMarriott38) — Temperature changes no longer revert in the UI.
- **Fixed Quota Reserve Not Preventing API Limit** ([#99](https://github.com/hiall-fyi/tado_ce/issues/99) - @ChrisMarriott38) — The quota reserve guard previously didn't block all API-spending paths, so the integration could still exceed the Tado daily limit when reserve was active. All API entry points now honour the reserve.
- **Fixed Mold Risk Calculation** ([#90](https://github.com/hiall-fyi/tado_ce/issues/90) - @ChrisMarriott38) — Now correctly uses room temperature for dew point calculation.
- **Fixed Thermal Analytics Missing for Some Zones** ([#91](https://github.com/hiall-fyi/tado_ce/issues/91) - @ChrisMarriott38) — Sensors now appear for all zones with heating data, not just TRV zones.

## [2.0.0] - 2026-02-09

**Smart Polling, Mold Risk Enhancement & Thermal Analytics**

### Features
- **API Monitoring Sensors** ([#86](https://github.com/hiall-fyi/tado_ce/discussions/86), [#65](https://github.com/hiall-fyi/tado_ce/issues/65)) — New sensors showing next sync time, last sync, polling interval, call history, and API breakdown.
- **Thermal Analytics** — New sensors for heating zones: thermal inertia, average heating rate, preheat time, and more.
- **Adaptive Smart Polling** ([#89](https://github.com/hiall-fyi/tado_ce/issues/89) - @ChrisMarriott38) — Polling frequency automatically adjusts based on your remaining API quota.
- **Quota Reserve Protection** ([#94](https://github.com/hiall-fyi/tado_ce/issues/94) - @ChrisMarriott38) — Pauses polling when your API quota is critically low to prevent hitting the limit.
- **Enhanced Mold Risk** ([#90](https://github.com/hiall-fyi/tado_ce/issues/90) - @ChrisMarriott38) — Surface temperature calculation with configurable window type for more accurate mold risk assessment.

### Bug Fixes
- **Fixed hot water timer buttons not finding the right entity** ([#93](https://github.com/hiall-fyi/tado_ce/issues/93) - @Fred224) — Timer button entity IDs were constructed from the zone name, but HA may add suffixes when the ID conflicts with another integration. Buttons now look up the water heater entity via the entity registry by unique ID, with name-based lookup as fallback.

### Improvements
- **Removed "Tado CE" prefix from entity names** — Hub sensors were prefixed with "Tado CE " (e.g. "Tado CE API Usage") which was redundant once every entity was grouped under the Tado CE Hub device. Prefix dropped — entity IDs unchanged so existing automations and dashboards keep working.

## [1.10.0] - 2026-02-05

### Bug Fixes
- **Fixed Climate Entity Flickering** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @hapklaar, @chinezbrun, @neonsp) — Climate entities no longer flicker or revert when you make changes. Multiple layers of protection prevent stale data from overwriting your actions.

## [1.9.7] - 2026-02-04

### Bug Fixes
- **Fixed state flickering when quickly changing modes** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @chinezbrun) — Rapid mode changes (e.g. Auto → Heat → Off within a few seconds) could leave the climate card flicking between states as the optimistic UI reconciled against delayed API responses. Optimistic state tracking now correctly holds the latest user intent until the API confirms it.

## [1.9.6] - 2026-02-04

### Bug Fixes
- **Fixed heating/cooling status reverting after a mode change** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @hapklaar, @chinezbrun) — After switching modes, the `hvac_action` attribute could briefly revert to the pre-change value on the next poll, making automations that react to heating/cooling state fire twice.

## [1.9.5] - 2026-02-02

### Bug Fixes
- **Fixed heating/cooling status not updating when setting temperature** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @hapklaar, @chinezbrun) — Changing the target temperature didn't update the `hvac_action` attribute until the next poll cycle, so automations tracking heating state missed the transition.

## [1.9.4] - 2026-02-02

**Boost Buttons**

### Features
- **Boost Button** — One tap to set any zone to 25°C for 30 minutes.
- **Smart Boost Button** — Intelligent boost that calculates the right duration automatically.

### Bug Fixes
- **Fixed heating status stuck on "Heating" after switching to Auto** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @hapklaar) — The climate card kept showing "heating" after switching from a manual target back to Auto, even when the zone was actually idle.
- **Fixed AC startup warnings** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @neonsp) — Spurious warnings on HA startup about missing AC capabilities resolved.
- **Fixed slow zone sensor updates** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @chinezbrun) — Zone sensors lagged behind climate card state by up to a poll cycle; now updated in lock-step.

## [1.9.3] - 2026-02-02

### Bug Fixes
- **Fixed slow state updates for heating users** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @hapklaar, @chinezbrun) — Heating-mode state changes took longer to reach HA than expected. Optimistic state updates now push the new state immediately while the API catches up.
- **Fixed AC DRY mode error** ([#79](https://github.com/hiall-fyi/tado_ce/issues/79) - @Fred224, @neonsp) — Setting the AC to DRY mode raised an API error on some AC models. Now correctly mapped.

## [1.9.2] - 2026-02-01

### Bug Fixes
- **Fixed grey loading state on climate card** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @chinezbrun) — The climate card showed a grey "loading" overlay for several seconds after any state change. The card now shows the new value immediately instead of waiting for Tado's cloud to confirm.

## [1.9.1] - 2026-01-31

### Bug Fixes
- **Fixed crash on startup during device migration** ([#74](https://github.com/hiall-fyi/tado_ce/issues/74)) — Upgrading from an earlier 1.x release could crash at startup when the device migration path encountered a zone with no devices registered. Migration now handles empty device lists gracefully.

## [1.9.0] - 2026-01-31

**Smart Comfort Analytics + Environment Sensors**

### Features
- **Smart Comfort Analytics** — New sensors for heating/cooling rate, time to target, and heating efficiency.
- **Smart Comfort Insights** ([#33](https://github.com/hiall-fyi/tado_ce/discussions/33)) — Historical comparison, preheat advisor, and smart comfort target recommendations.
- **Environment Sensors** ([#64](https://github.com/hiall-fyi/tado_ce/issues/64)) — Mold risk and comfort level sensors for each zone.
- **Schedule Sensors** — Shows the next scheduled time and temperature for each zone.

### Bug Fixes
- **Fixed API reset detection for the 100-call limit** ([#54](https://github.com/hiall-fyi/tado_ce/issues/54)) — Users on the free 100-call API tier saw incorrect reset time estimates. The detection logic now reads the daily reset schedule correctly for low-quota accounts.
- **Fixed temperature offset for rooms with multiple TRVs** ([#66](https://github.com/hiall-fyi/tado_ce/issues/66)) — The temperature offset service only wrote to the first TRV in a zone; rooms with multiple radiator valves now get the offset applied to every TRV.
- **Fixed sensors being assigned to the wrong device** ([#56](https://github.com/hiall-fyi/tado_ce/issues/56)) — Some zone sensors appeared under the Hub device instead of the correct zone device. Device assignment now uses the zone's unique ID for lookup.

## [1.8.3] - 2026-01-26

### Features
- **Refresh AC Capabilities button** — New button on the Hub device to reload your AC unit's supported modes, fan speeds, and swing options without restarting HA. Useful if Tado's reported capabilities change or if you want to re-sync after connecting a new AC unit.

### Bug Fixes
- **Fixed AC not responding immediately after turning on** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @neonsp) — The first command after turning on an AC zone could take several seconds to register. Optimistic state now applies the command right away while the API catches up.

### Improvements
- **AC capabilities are now cached to save API calls** ([#61](https://github.com/hiall-fyi/tado_ce/issues/61) - @neonsp) — AC unit capabilities (supported modes, fan speeds, swing options) are only fetched when you first add the integration or manually refresh, instead of on every startup.

## [1.8.2] - 2026-01-26

### Bug Fixes
- **Fixed Resume All Schedules taking too long to respond** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @hapklaar) — The Resume All Schedules button could take several seconds to update the UI after pressing. Now refreshes immediately after the API confirms.

### Improvements
- **Smoother AC controls** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @neonsp) — Selections in AC mode, fan speed, and swing dropdowns no longer flicker during updates — optimistic state holds the new value until the API confirms.

## [1.8.1] - 2026-01-26

### Bug Fixes
- **Fixed AC instant feedback not working** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @neonsp) — AC mode and temperature changes didn't update the UI immediately, leaving users unsure whether the command had registered.
- **Fixed Resume All Schedules not refreshing the UI** ([#44](https://github.com/hiall-fyi/tado_ce/issues/44) - @hapklaar) — After pressing Resume All Schedules, the climate cards stayed on their overlay targets until the next poll. Now refresh immediately.

## [1.8.0] - 2026-01-26

**Schedule Calendar + Multi-Home Prep**

### Features
- **Schedule Calendar** — See your heating schedules as a calendar per zone, with each scheduled block appearing as a calendar event.
- **Per-zone Refresh Schedule button** — Trigger an on-demand schedule refresh for a specific zone without waiting for the next poll cycle.
- **API Reset sensor now shows extra details** ([#54](https://github.com/hiall-fyi/tado_ce/issues/54) - @ChrisMarriott38) — The API Reset sensor attributes now include next reset time, calls used this period, and detection method.

### Improvements
- **Multi-home preparation** — Data files are now stored per home (rather than globally) in preparation for the full multi-home support that lands in v3.0.0. No user action needed; existing single-home installs continue to work unchanged.

## [1.7.0] - 2026-01-26

### Features
- **Instant UI feedback** — Temperature changes, mode switches, and preset selections now show in the UI immediately instead of waiting for the next poll cycle to confirm. Optimistic state holds the new value until the API catches up.
- **Optional home state sync to save API calls** — Fetching Tado's cloud-side home presence state on every poll is now optional via a toggle in Options Flow. With it off, presence changes made on HA propagate to Tado without also polling for server-side changes you don't use.

### Improvements
- **Multi-home preparation — unique ID migration** — Entity unique IDs now include the home ID in preparation for the full multi-home support that lands in v3.0.0. Existing single-home installs are migrated automatically on first start; no user action needed.

## [1.6.3] - 2026-01-25

### Improvements
- **Uses HA history to detect API reset time more accurately** — The API reset time sensor now queries HA's history for past rate-limit sensor values to infer the actual reset window, rather than relying on fixed assumptions that could drift from Tado's real reset time.

## [1.6.2] - 2026-01-25

### Bug Fixes
- **Fixed API call history not being recorded** — API call counts were tracked in memory but not persisted, so the API usage sensor started from zero on every HA restart. Now correctly records history across restarts.
- **Fixed timezone issues in various sensors** — Sensors that displayed times (reset time, next sync, last sync) could show wrong values for users in non-UTC timezones. All time displays now honour HA's configured timezone.

## [1.6.1] - 2026-01-25

### Features
- **Configurable delay between rapid updates** — New Options Flow setting to adjust the debounce window for rapid state changes (e.g. slider drags). Prevents API spam when tuning temperatures quickly.

### Bug Fixes
- **Fixed API Usage and Reset sensors showing 0** — A regression in 1.6.0 left the API Usage and Reset sensors stuck on 0 regardless of actual quota state. Now reads the correct values on every poll.

## [1.6.0] - 2026-01-25

### Features
- **Faster API calls** — Tado API calls now run directly inside Home Assistant instead of through a separate process. Each call's overhead drops from ~100ms to <10ms, and the stability issues from the old approach are gone.

### Bug Fixes
- **Fixed database migration issue from older versions** — Upgrading from pre-1.5 releases could fail mid-migration, leaving entities in an inconsistent state. Upgrades can now resume safely if a previous attempt was interrupted partway through.
- **Fixed `climate.set_temperature` ignoring the mode you selected** — Passing `hvac_mode` along with `temperature` to the service was being silently dropped — only the temperature was applied. Now both fields are honoured in the same call.

## [1.5.5] - 2026-01-24

### Bug Fixes
- **Fixed AC Auto mode accidentally turning off the AC** — Setting an AC zone to Auto mode could be misinterpreted by Tado's API as "off". The mode now maps to the correct API value so Auto stays Auto.

### Improvements
- **Reduced API calls when changing settings** — Rapid setting changes in the Options Flow no longer trigger multiple config reloads; changes are coalesced and applied once.

## [1.5.4] - 2026-01-24

### Features
- **Unified swing dropdown for AC** — Vertical and horizontal swing options are now combined into a single dropdown with Off/Vertical/Horizontal/Both, matching the official Tado integration's layout.

### Bug Fixes
- **Fixed all AC control issues (modes, fan speed, swing)** — A set of regressions from 1.5.3 affecting AC mode changes, fan speed selections, and swing mode updates are all resolved in this release.

## [1.5.3] - 2026-01-24

### Features
- **Resume All Schedules button** — New one-tap button on the Hub device that resumes the schedule on every zone at once, instead of clicking each zone's resume button individually.

### Bug Fixes
- **Fixed AC control errors** — Several AC mode and fan speed commands were failing with API errors due to incorrect parameter mapping. All AC commands now pass the correct API payload.

## [1.5.2] - 2026-01-24

### Bug Fixes
- **Fixed losing your login after a HACS upgrade** — A HACS upgrade could wipe the stored refresh token, forcing you to re-authenticate after every update. The token storage path now survives HACS upgrades.

## [1.5.1] - 2026-01-24

### Features
- **Re-authenticate option in the UI** — New menu option in the integration's overflow menu to trigger the re-auth flow without removing and re-adding the integration.

### Bug Fixes
- **Fixed login errors for new users** — First-time setup could fail on accounts that hadn't been used with the old official integration because of a stale token format assumption. New-account onboarding now works cleanly.

## [1.5.0] - 2026-01-24

**Async Architecture Rewrite**

### Features
- **Temperature offset service** — New `set_climate_temperature_offset` service lets you calibrate per-TRV temperature offsets from automations or scripts.
- **Full AC support — all modes, fan speeds, and swing options** — Previously AC zones were limited to basic mode/temperature. All modes (Cool/Heat/Auto/Dry/Fan), all fan speeds, and all swing options are now exposed as climate card controls.
- **Hot water temperature control** — Tank-based hot water zones now expose a water heater entity with temperature and mode control, matching what the Tado app offers.

### Improvements
- **Faster API calls** — The way Tado CE talks to Tado's cloud has been reworked. Each call is faster and the integration uses fewer system resources.

## [1.4.1] - 2026-01-23

### Bug Fixes
- **Fixed login broken after upgrading** — Upgrading from 1.3.x to 1.4.0 could leave your login in a broken state because the token storage format changed. The upgrade path now migrates existing tokens instead of invalidating them.

## [1.4.0] - 2026-01-23

### Features
- **In-app login** — New browser-based authentication flow built into the integration setup. No more SSH or terminal access needed to grab tokens — click a link, sign in, and you're done.
- **Home selection for accounts with multiple homes** — If your Tado account manages more than one home, setup now lets you pick which home this integration entry controls. Add the integration again to add another home.

## [1.2.1] - 2026-01-22

### Bug Fixes
- **Fixed a rare startup issue with duplicate hub cleanup** — Under specific upgrade paths, two Hub devices could end up registered for the same integration. Startup cleanup now correctly deduplicates without removing the wrong entity.

## [1.2.0] - 2026-01-21

**Zone-Based Device Organization**

### Features
- **Each zone now appears as its own device in HA** — Previously every sensor and control sat under a flat "Tado CE" device. Each zone is now its own device containing its climate entity, sensors, and switches, matching how HA users expect to browse by room.
- **Optional weather sensors** — Outdoor temperature, solar intensity, and weather state sensors can now be enabled per-installation via Options Flow.
- **Customizable polling intervals** — Options Flow exposes a polling interval setting so you can balance freshness against API quota.

### Improvements
- **60–70% fewer API calls** — The polling strategy now batches zone reads and defers non-essential endpoints to a longer interval, cutting total API calls dramatically for typical multi-zone installs.

## [1.1.0] - 2026-01-19

### Features
- **Away Mode switch** — New switch on the Hub device to toggle Tado's Away mode directly from HA.
- **Home/Away preset mode support** — Climate entities now expose the Home/Away preset modes, so dashboard climate cards include a presence selector.

## [1.0.1] - 2026-01-18

### Bug Fixes
- **Fixed auto-detection of your home ID** — For accounts with multiple homes, the integration sometimes picked the wrong home ID on setup. Home ID detection now matches the first home you logged in with via the Tado app.

## [1.0.0] - 2026-01-17

### Features
- **Initial release** — First public release of Tado CE as a fork of the official Tado integration with community-driven enhancements.
