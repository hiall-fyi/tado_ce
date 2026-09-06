# Credits

Tado CE is built with the help of an amazing community. This page recognises everyone who has contributed — through support, bug reports, feature ideas, testing, and code.

---

## ☕ Supporters

Thank you to everyone who supported the project through [Buy Me a Coffee](https://buymeacoffee.com/hiallfyi). Your generosity keeps the lights on.

| | Supporters |
|---|-----------|
| ☕☕☕☕☕☕☕☕ | [@ChrisMarriott38](https://github.com/ChrisMarriott38) |
| ☕☕☕☕☕☕ | [@jeverley](https://github.com/jeverley) |
| ☕☕☕☕☕ | Marcel v.H., [@Prodeguerriero](https://github.com/Prodeguerriero), [@rodneyha](https://github.com/rodneyha), [@UKICS](https://github.com/UKICS), [@wisskid](https://github.com/wisskid) |
| ☕☕☕☕ | [@hapklaar](https://github.com/hapklaar) |
| ☕☕ | Arnaud L., [@janchrillesen](https://github.com/janchrillesen), [@jeromewir](https://github.com/jeromewir), Luke R., [@marcovn](https://github.com/marcovn), [@ratormat](https://github.com/ratormat), Vit P. |
| ☕ | Alby T., kottrupk, [@MathiasB112](https://github.com/MathiasB112), Philipp H. |

---

## 💻 Code Contributors

Community members who sent an actual patch, not just a report — reviewed and either merged directly or folded into the shipped fix on the same diagnosis.

- **[@nicohabets](https://github.com/nicohabets)** — [PR #341](https://github.com/hiall-fyi/tado_ce/pull/341), the quota-reserve fix for [#340](https://github.com/hiall-fyi/tado_ce/issues/340). Landed as its own commit sharing the same diagnosis rather than a direct merge, since `main` runs ahead of what's on GitHub, plus the two sibling handlers he flagged separately.
- **[@Flavien](https://github.com/Flavien)** — [PR #330](https://github.com/hiall-fyi/tado_ce/pull/330), three HomeKit sync fixes for v4.3.2, two shipped as written.
- **[@hacker4257](https://github.com/hacker4257)** — [PR #132](https://github.com/hiall-fyi/tado_ce/pull/132), the API call history directory fix for v2.2.0.

---

## Per-Version Credits

Community contributors who helped shape each release through bug reports, feature requests, testing, feedback, and code.

### v4.4.1

- **[@davidjirovec](https://github.com/davidjirovec)** — Found a narrower survivor of the OFF-zone guard v4.4.0 had just shipped ([#335](https://github.com/hiall-fyi/tado_ce/issues/335), comment 9). A Home Assistant Recorder reproduction with file:line references across three functions showed that a `set_hvac_mode` → `set_temperature` pair sent two seconds apart, exactly what a window-closed automation sends when restoring heat, let the second call trust the first call's own unconfirmed optimistic write and slip back onto HomeKit before anything had confirmed the zone was actually on. That trace turned "check confirmed zone power, not the entity's own optimistic state" from a hunch into the fix.
- **[@Si-Hill](https://github.com/Si-Hill)** — Reported TRVs waking up and audibly moving at night on a system that had been off for months ([#343](https://github.com/hiall-fyi/tado_ce/issues/343)). The first answer, that Offset Sync correcting while off is intentional and harmless, turned out to be incomplete; coming back with "it's happening at random times, worse at night" rather than accepting that answer is what kept the thread open long enough for the real bug to surface.
- **[@jokinzengotita](https://github.com/jokinzengotita)** — Supplied the debug log that turned #343 from a noise complaint into a fixable bug ([#345](https://github.com/hiall-fyi/tado_ce/issues/345)): exact offset sequences across three zones swinging several degrees and flipping sign between polls (zone 6: -7.8°C → -4.1°C → +3.5°C → +7.3°C → ...), with heating power confirmed at 0% throughout, plus a clean isolation test, disabling Offset Sync and resetting every device offset, that confirmed the writes stopped. That data showed the offset was genuinely oscillating rather than settling after a one-off recalibration, and gave the fix a concrete case to hold itself against.

### v4.4.0

- **[@nicohabets](https://github.com/nicohabets)** — Reported that every entity went unavailable for hours when the daily API quota ran low, even with HomeKit connected ([#340](https://github.com/hiall-fyi/tado_ce/issues/340)), then opened [PR #341](https://github.com/hiall-fyi/tado_ce/pull/341) with a fix and the diagnosis behind it: the pre-emptive reserve guard raised `UpdateFailed` unconditionally while a separate guard right next to it already checked HomeKit first. Caught his own regression before anyone else saw it: a cold start with the first version of his fix applied would have returned an empty refresh as if it had succeeded, so he gated the fallback on the coordinator already holding data instead. Then built a standalone offline harness that lifts the real code block out of `coordinator.py` by line range and runs it against both the released version and his branch with a stub coordinator, rather than just describing the fix in words. The shipped fix lands as its own commit sharing his diagnosis, extended to two sibling handlers he'd separately flagged as carrying the identical gap.
- **[@EDelsman](https://github.com/EDelsman)** — Reported the exact crash Home Assistant's 2026.9 beta produces ([#338](https://github.com/hiall-fyi/tado_ce/issues/338)) with the full traceback, which turned a beta-compatibility question into a concrete fix rather than a wait-and-see. Stayed through a long side investigation into an unrelated temperature swing on his own setup, and offered to test a release candidate.
- **[@bobbinz](https://github.com/bobbinz)** — Kept a debug log running through a HomeKit bridge that kept losing its pairing ([#322](https://github.com/hiall-fyi/tado_ce/issues/322)), and the fresh log from this round showed something new: the bridge resetting the connection mid-handshake, with the underlying HomeKit library retrying several times a second and no backoff at all, a different failure to the clean auth-refusal his same thread had already got fixed back in v4.3.0. That log is what separated the two failure modes and is why this one earned its own fix rather than being folded into the old one.
- **[@davidjirovec](https://github.com/davidjirovec)** — Traced a zone with no Window Type override always reading double-pane glazing regardless of the global setting under Smart Comfort, down to the exact fallback chain across four files with file:line references for each step ([#337](https://github.com/hiall-fyi/tado_ce/issues/337)). Also caught that no code path ever copies the global value into a zone lacking its own override, which is precisely the gap the fix closes.
- **[@motoko-bv](https://github.com/motoko-bv)** — Multi-round investigation into a HomeKit-set temperature reverting to 5°C ([#335](https://github.com/hiall-fyi/tado_ce/issues/335)). Retracted his own first theory after checking the source himself, traced the "Unknown" Target sensor to its power-state check, and captured a debug log precise enough to pin the actual mechanism: `_handle_homekit_update` was reading the entity's own just-written attribute and logging it as a cloud confirmation that had never actually reached Tado. That mislabelled source now reads `local-write` instead of `cloud`, exactly because of where he pointed.
- **[@Thilas](https://github.com/Thilas)** — Reopened a long-closed issue ([#266](https://github.com/hiall-fyi/tado_ce/issues/266)) with the question that undid a settled assumption: why should Offset Sync stop correcting the device offset just because a zone is off? There had been a reason at the time, but it was the wrong one — Offset Sync's job is keeping Tado's own display accurate, which has nothing to do with whether the zone is calling for heat.
- **[@BirbByte](https://github.com/BirbByte)** — Reported "Pairing failed" on a device that was never going to pair the way he was trying ([#334](https://github.com/hiall-fyi/tado_ce/issues/334)): a Smart AC Control unit, which HomeKit sees as a standalone thermostat outside Tado CE's bridge-only pairing flow. His screenshots made the mismatch obvious, and exposed that the actual "could not find the bridge" message the code already had was being swallowed into a generic retry prompt instead of shown.

### v4.3.3

- **[@davidjirovec](https://github.com/davidjirovec)** — Reported that switching a zone off arrives as two separate changes rather than one ([#332](https://github.com/hiall-fyi/tado_ce/issues/332)), and did the work that made it actionable: a recorder timeline with the context IDs on both changes, the same sequence reproduced twice hours apart, and the two functions responsible named from reading the source. He was also careful to separate it from an older report that looks similar, which is what stopped it being closed as a duplicate. Both of his source readings held up, and one of them was worse than he put it: the helper takes the target it is handed and throws it away. Following his shape into the local-control path then turned up the same gap there, so both are fixed together, and it showed that v4.3.2 had introduced that half four days earlier.

### v4.3.2

- **[@Flavien](https://github.com/Flavien)** — Opened [#330](https://github.com/hiall-fyi/tado_ce/pull/330) with three HomeKit sync problems, each traced to the line that produces it and each with a patch. Two of them are the first two fixes in this release. The reverting-temperature one had been live since v4.0.2 and my own notes recorded it as already fixed, so it was never going to surface from my side: it needed someone to sit with the symptom and follow it into the write path. He also found the same omission in the air-conditioning code, so the heating and AC halves are fixed together rather than one now and one later. The third of his three is a real problem too, but the patch reopens a subtler one, so it is deferred to v5.0.0 where the fix can be shaped properly.

### v4.3.1

- **[@jaimievansanten](https://github.com/jaimievansanten)** — Reported the Home Assistant 2026.8 clash ([#327](https://github.com/hiall-fyi/tado_ce/issues/327)) the day after Core released it, with the piece that made it diagnosable in one read: not just "HomeKit Controller won't start" but both manifest requirements side by side, the exact constructor error, and the fact that unrelated HomeKit pairings were going down with it. That last detail is what marked it urgent rather than cosmetic. Tado CE runs its own HomeKit client, so a setup that only pairs the tado bridge sees nothing wrong, and without his report this would have sat unnoticed on my side while it broke other people's accessories.
- **[@Prodeguerriero](https://github.com/Prodeguerriero)** — Reported that the first build of this release installed on his Home Assistant 2026.6 even though the note said it couldn't, then failed to load ([#329](https://github.com/hiall-fyi/tado_ce/issues/329)). One report, two defects underneath it: the minimum-version guard doesn't block the download the way I'd claimed, and the #327 fix had pinned the HomeKit library rather than following it, which made Tado CE the incompatible one on every Home Assistant below 2026.8. His screenshot of the version he was running is what made the first undeniable, and the second only came to light because he reported it at all: upgrading Home Assistant had already fixed his own setup, so he had nothing left to gain by writing it up.

### v4.3.0

- **[@davidjirovec](https://github.com/davidjirovec)** — Came back on [#303](https://github.com/hiall-fyi/tado_ce/issues/303) with the case that turned configurable intervals into a proper service: on his 20,000-call plan he wanted a `homeassistant.update_entity` to force a mobile-presence fetch on demand, and the per-type floor was blocking it just as it blocks the automatic cadence. That distinction, a deliberate forced refresh versus a background poll, is exactly what the new `tado_ce.refresh` service separates out, bypassing the floor for the one call while leaving the automatic protection in place.
- **[@bobbinz](https://github.com/bobbinz)** — Stayed on the HomeKit thread ([#322](https://github.com/hiall-fyi/tado_ce/issues/322)) past the mapping and pairing-scan fixes to the last piece: after a bridge reset, his debug log showed the connection thrashing pair-verify against a pairing the bridge had already dropped, with the CPU spike that came from it. That log is what pinned the stale-pairing teardown, so a reset bridge now releases its half cleanly instead of spinning.
- **[@onorbe](https://github.com/onorbe)** — His earlier no-competing-controller finding on the same thread ([#322](https://github.com/hiall-fyi/tado_ce/issues/322)) is what reframed the spin as tado_ce's own stale pairing rather than an external controller, which is the reading the teardown fix is built on.

### v4.2.1

- **[@bobbinz](https://github.com/bobbinz)** — Pinned two separate HomeKit bugs in one thread ([#322](https://github.com/hiall-fyi/tado_ce/issues/322)). First, HomeKit local control coming up on only one room of three after a re-pair: his sensor snapshot (four hours connected, still `mapped_zones: 1 / unmapped_zones: 2`) is the "stuck partial, never recovers" reading that pointed at the mapping retry giving up after the first room, a bug latent since v4.0.3. Then, when a re-pair attempt threw a wrong-code error, his log showed the tado PIN going to `10.10.64.181` (one of his unpaired HomeKit bulbs) instead of the bridge, which is what exposed the pairing scan grabbing the first unpaired device rather than the tado bridge. Also shared the mapping workaround (unpair, ignore the zeroconf entry, re-pair once every radiator is awake).
- **[@onorbe](https://github.com/onorbe)** — Chased a pairing that kept rejecting a code that was genuinely correct ([#322](https://github.com/hiall-fyi/tado_ce/issues/322)), through debug logs and photos of the bridge label matched against what he typed. Worked out himself that a running Homebridge was grabbing the bridge mid-handshake, which is exactly the competing-controller case the error message never mentioned. That's why the pairing error now names it.

### v4.2.0

- **[@rustyd0g](https://github.com/rustyd0g)** — Requested the per-zone heating circuit select ([#316](https://github.com/hiall-fyi/tado_ce/issues/316)), pointing at the Core work and the open draft PR, and laid out the residual-heat use case that makes it worth having: set a zone to "No heating circuit" so its valves still soak up spare heat when the boiler runs for another room, without that room firing the boiler itself. Offered to test the multi-circuit side, which is the part I can't exercise on my own single-circuit setup.
- **[@Siiya27](https://github.com/Siiya27)** — Tested the v4.1.4 hot-water fix on his temperature-controlled tank ([#317](https://github.com/hiall-fyi/tado_ce/issues/317)) and reported that Heat came back at the tank's maximum instead of his chosen 52°. Worked through the diagnosis step by step and confirmed the target wasn't surviving the overnight off period, and that the temperature control only reappeared after a poll. That testing is what turned the follow-up into a definite fix, since his setup is the one that actually exercises this and mine can't.

### v4.1.4

- **[@50494554524F](https://github.com/50494554524F)** — Kept going on the bridge sensor after v4.1.3 and pulled the raw API responses that cracked it: the boiler-wiring endpoint returns nothing useful on a radiator-only home, and the bridge's real status lives in the home device list instead. That is exactly the source the fix now reads. Also spotted that the boiler-wiring diagnostic sensors are misnamed as "bridge" ones, which is queued as a follow-up.
- **[@arnoslag](https://github.com/arnoslag)** — Followed through on the tracker report ([#314](https://github.com/hiall-fyi/tado_ce/issues/314)): confirmed v4.1.3 restored presence but left the trackers ungrouped, and even patched his own copy to prove the device grouping was the missing piece. That confirmation is why the grouping fix shipped this release.
- **[@Siiya27](https://github.com/Siiya27)** — Reported that switching hot water to Heat failed every time on his temperature-controlled tank ([#317](https://github.com/hiall-fyi/tado_ce/issues/317)), with the exact 422 from Tado and a clean repro. He also worked out the cause himself: the overlay payload was missing the temperature field that a temperature-capable tank requires. That analysis pointed straight at the fix.

### v4.1.3

- **[@50494554524F](https://github.com/50494554524F)** — Stayed on the bridge connection report ([#275](https://github.com/hiall-fyi/tado_ce/issues/275)) through several rounds and, crucially, sent the debug log that pinned the real cause: with the bridge physically off, Tado's cloud still answered the bridge call with an HTTP 200, so the sensor read "connected" indefinitely. That evidence is why the fix landed. The sensor now reads the bridge's own online flag from the response, and treats an unreachable cloud as unavailable rather than a stale reading.
- **[@Siiya27](https://github.com/Siiya27)** — Reported that HomeKit pairing never attempted a handshake and behaved identically for right and wrong codes ([#313](https://github.com/hiall-fyi/tado_ce/issues/313)), with debug logs on both sides and a clean repro. Then confirmed the diagnosis and worked out the trigger himself (a wrong code on the first attempt left HomeKit enabled but unpaired, with no way back to the prompt), and suggested both fixes that shipped: checking the code format up front, and a "Pair again" option to recover.
- **[@arnoslag](https://github.com/arnoslag)** — Caught a clean regression ([#314](https://github.com/hiall-fyi/tado_ce/issues/314)): per-user mobile trackers went unavailable on v4.1.2 while the home-wide presence sensor stayed fine, with a precise v4.1.1-worked / v4.1.2-broke split, repro steps, and the HACS downgrade workaround. That sharp before/after pointed straight at the tracker change and made the fix quick to pin.

### v4.1.2

- **[@janchrillesen](https://github.com/janchrillesen)** — Asked on the discussions how to see the device temperature offset. His setup turned out fine once Offset Sync was on, but the question surfaced a real bug: with Device Temperature Offsets enabled and no zone running Smart Valve Control or Offset Sync, the offset was never fetched, so anyone who only wanted to watch `offset_celsius` saw nothing. Fixed so the toggle works on its own ([#311](https://github.com/hiall-fyi/tado_ce/discussions/311)).
- **[@50494554524F](https://github.com/50494554524F)** — Reopened the connection-sensor report ([#275](https://github.com/hiall-fyi/tado_ce/issues/275)), which led to the bridge connected sensor now updating on the bridge's own poll cycle when the bridge API becomes unreachable, instead of waiting for the next cloud poll.

### v4.1.1

- **[@wisskid](https://github.com/wisskid)** — Reported that the integration crashed outright on 4.1.0 ([#308](https://github.com/hiall-fyi/tado_ce/issues/308)) and posted the full traceback that made it an exact fix: with HomeKit local control on but no zeroconf service on the host, `aiohomekit` raised `TransportNotSupportedError` from an unguarded startup call and took the whole entry down, cloud side included. He'd already found his own side of it (no `default_config:` / `zeroconf:` in `configuration.yaml`), then spotted that the issue had auto-closed and reopened it so the crash itself would still get fixed rather than written off as a config quirk. That nudge is why the fix shipped: an optional local feature failing now falls back to cloud-only quietly, and pairing on a host without zeroconf names the real cause instead of a vague "pairing failed".

### v4.1.0

The 4.1 cycle ran across five public betas between May and June 2026, an internal HomeKit reliability pass (startup reconnect, factory-reset Repair notices, clearer pairing errors), and a couple of fixes folded straight into the GA cut. The contributors below shaped the release through bug reports, feature requests, and field testing across the full cycle.

- **[@Ralf84](https://github.com/Ralf84)** — Drove the AC swing axis split from feature request to merged design ([#270](https://github.com/hiall-fyi/tado_ce/issues/270)). His debug log (`vertical={MID, AUTO, UP, MID_DOWN, DOWN, MID_UP, ON}`, `horizontal={MID, LEFT, MID_LEFT, MID_RIGHT, RIGHT, ON}`) exposed how lossy the v4.0 unified four-value dropdown was on fine-grained AC units, which turned a one-line patch into a proper `ClimateEntityFeature.SWING_HORIZONTAL_MODE` split. That same log closed two related issues from earlier cycles ([#128](https://github.com/hiall-fyi/tado_ce/issues/128), [#142](https://github.com/hiall-fyi/tado_ce/issues/142)) — all three were only fixable together once the right capability data was on the table.
- **[@BirbByte](https://github.com/BirbByte)** — Caught the polish bugs that turned beta.1's split-axis swing from "works" to "feels right" ([#270](https://github.com/hiall-fyi/tado_ce/issues/270)). His Mitsubishi screenshot showed values arriving alphabetically rather than in physical-louver order, which drove the explicit ordering layer and the uniform icon set (directional `mdi:arrow-*-thin` for positions, `mdi:auto-mode` for Auto, `mdi:circle-small` for Off) that now ships across every AC make.
- **[@apilone](https://github.com/apilone)** — Two separate contributions. In beta.3 he reported climate state and temperature flickering out of sync on a hybrid HomeKit + cloud zone ([#296](https://github.com/hiall-fyi/tado_ce/issues/296)), with a full debug log and step-by-step repro that pinpointed the HomeKit/cloud HVAC-mode conflict; the mode now comes from the cloud only, which stops the flap. In beta.5 he laid out the holiday-automation problem in [#256](https://github.com/hiall-fyi/tado_ce/issues/256) and sketched the fix that shipped as `set_schedule_temperature`: a write that marks the change as programmatic so Smart Valve Control keeps compensating rather than backing off.
- **[@churchofnoise](https://github.com/churchofnoise)** — Asked why the cloud temperature seemed to update more often than the poll interval ([#293](https://github.com/hiall-fyi/tado_ce/issues/293)), and stayed through the back-and-forth as it turned out to be interpolation. That thread surfaced the need to let each zone choose which temperature reading the dashboard shows, which landed as the new per-zone Temperature Source setting in beta.3.
- **[@bobbinz](https://github.com/bobbinz)** — Requested a way to tell which physical TRV is which ([#291](https://github.com/hiall-fyi/tado_ce/issues/291)) and tested the `identify_device` service both ways, with and without the full serial from the Tado app. The "even the app serial errors" detail pinned a real bug in the service's device-resolution path, and the investigation into what the bridge exposes locally turned up a native HomeKit Identify characteristic — the new per-device button now flashes the LED over the LAN at no quota cost.
- **[@simonotter](https://github.com/simonotter)** — Carried the overnight offset-swing investigation in [#262](https://github.com/hiall-fyi/tado_ce/issues/262) through to beta.4. His full-night log was the decisive evidence: the offset marched to the ±10°C cap and lurched back while the cache was provably healthy, which ruled out the cache theories two earlier fixes had chased and pinned it as a control-loop timing problem. The redesign now waits for Tado's room reading to reflect the last write before correcting again.
- **[@wrowlands3](https://github.com/wrowlands3)** — Two contributions across beta.4 and beta.5. His [#277](https://github.com/hiall-fyi/tado_ce/issues/277) report of the drift refresh bursting one call per zone drove the round-robin rework in beta.4. He then followed up in [#304](https://github.com/hiall-fyi/tado_ce/issues/304) with a log showing the startup full-sync path still reading every zone's offset on every restart; the beta.5 fix gates the fetch on SVC being active on at least one zone.
- **[@Trebor87](https://github.com/Trebor87)** — His re-pair walkthrough in [Discussion #280](https://github.com/hiall-fyi/tado_ce/discussions/280) became the basis for automatic AC capabilities refresh in beta.4. Where beta.2 documented the manual button, beta.4 watches each zone's device serial and firmware so a re-pair or hardware swap re-fetches the supported modes on its own, and the climate entity rebuilds its mode list at the same time.
- **[@beltra](https://github.com/beltra)** — Reported presence mode reverting to Home after a restart ([#302](https://github.com/hiall-fyi/tado_ce/issues/302)) with the detail that pinned it: the logbook showed no user or automation behind the change, just the integration setting it on startup. He also ruled out a phantom cloud write by tracing one of his own automations, which narrowed the bug straight to the local restore. The cache write turned out never to reach disk, so an Away set with Home State Sync off didn't survive a restart.
- **[@davidjirovec](https://github.com/davidjirovec)** — Filed [#303](https://github.com/hiall-fyi/tado_ce/issues/303) as the clearest statement of a need that had been building since [#276](https://github.com/hiall-fyi/tado_ce/issues/276): on a 20,000-call plan, a 5-minute floor on presence and mobile locations is a real latency cost for automations. His report was the point where the quota and cadence work was stable enough to expose configurable intervals safely.
- **[@stefanzweig1979](https://github.com/stefanzweig1979)** — Reported that fan speed changes never reached his Toshiba unit ([#305](https://github.com/hiall-fyi/tado_ce/issues/305)): the dashboard updated but the AC didn't react, while temperature changes worked. He pulled the debug payload showing the integration sending `fanSpeeds`, then captured the real Tado web-app request showing the field should be the singular `fanSpeed` on older firmware. That capture turned a plausible theory into a confirmed one-line fix and ruled out guessing the wrong field.

### v4.0.3

- **[@apilone](https://github.com/apilone)** — Spotted that the open-window automation example in the Features Guide didn't actually close the window ([#295](https://github.com/hiall-fyi/tado_ce/issues/295)), with a precise repro: `set_open_window_mode` put the zone into manual 5°C, then `deactivate_open_window` did nothing. He'd already worked out it might need `restore_previous_state`. He was right on both counts. The three open-window services run on two different mechanisms (`set_open_window_mode` writes a frost overlay; `activate`/`deactivate_open_window` drive Tado's own detection state), and both the example and the `deactivate` service description had blurred them. The example now uses `resume_schedule`, and the service descriptions spell out which one clears which.

### v4.0.2

- **[@driagi](https://github.com/driagi)** — Reported cloud API calls jumping sharply after updating to 4.0.1, with the bridge still connected and HomeKit working ([#289](https://github.com/hiall-fyi/tado_ce/issues/289)). His instinct that something changed at 4.0.1 was right, and the evidence chain proved it: a full debug log showing a poll firing every 60 seconds, four API-usage charts capturing a stable saw-tooth from mid-May then a step-change after 5 June, and the Diagnostica panel confirming the 1-minute interval was adaptive's own choice, not a setting he'd made. That triangulated the root cause to the 4.0.1 adaptive-floor change (5 min dropped to 1 min for the 20,000-call tier's benefit) collapsing the 1,000-call tier to 1-minute polling whenever quota looked healthy. The adaptive-polling reframe was already scoped for v4.1.0-beta.3; his report was the reason to pull it onto the stable line now (a flat 5-minute floor for every plan instead of letting a big quota poll faster, per-type refresh, HomeKit-defer) so 4.0.x users get the fix without waiting for the v4.1.0 beta cycle to finish.

### v4.0.1

- **[@Newreader](https://github.com/Newreader)** — Methodical investigation into a frost-protection overlay reverting to 20°C heat after polling resumed from a quota-reset window ([#278](https://github.com/hiall-fyi/tado_ce/issues/278)). His cross-version comparison (App1 on beta.16 not reproducing vs App2 on v4.0.0 reproducing), combined with the safeguard automation he wired up to confirm no client-side trigger, narrowed the search space to the State Restore Manager's overlay-cleared event path. The root cause is still open as of 4.0.1 — a controlled reproduction on a different 1000-tier setup didn't trigger it during the quota-reset window, so we can't yet pinpoint whether it's tier-specific, setup-specific (no zone controller), or a transient that the defensive gate now filters by accident. His hypothesis that a single overlay-cleared observation could be a transient blip drove the defensive N≥2 confirmation gate in 4.0.1 — the integration now requires two consecutive polls reporting overlay=null before firing the restoration event. That should filter out a transient blip if that's what it was; investigation continues for the underlying mechanism.
- **[@wrowlands3](https://github.com/wrowlands3)** — Surfaced that the post-#268 drift-refresh fix in beta.16 still bursts N cloud calls per cycle ([#277](https://github.com/hiall-fyi/tado_ce/issues/277)) — the fix had been described as "no more burst" but the actual shape was "one batch on Cloud Refresh cadence", which still reads as a burst on his 8-zone install. His persistence in re-running the trace and providing the full log file led to the observability fix that surfaces the per-cycle quota cost in the drift refresh log line, plus the FEATURES_GUIDE Smart Polling entry showing the daily-total formula so users can audit their own quota usage without inferring N from rate-limit decrement traces.
- **[@churchofnoise](https://github.com/churchofnoise)** — Asked why adaptive polling wasn't using more of his grandfathered 20,000-call quota ([#276](https://github.com/hiall-fyi/tado_ce/issues/276)). His report (145 calls used out of a 20,000-call daily allowance, 16 hours into the day) was a concrete data point that sent me back into the adaptive polling math, and v4.0.1 responded by dropping the floor so a big quota could poll faster automatically. That turned out to be the wrong call: it's what let a healthy quota collapse to 1-minute polling and burn calls for no real gain, which is exactly what [#289](https://github.com/hiall-fyi/tado_ce/issues/289) then surfaced. v4.0.2 walks it back to a flat 5-minute floor for every plan, on the reasoning that zone temperature doesn't change any faster on a bigger quota, with a custom interval as the opt-in for anyone who genuinely wants faster. His data point still earns the credit; it kicked off the rethink that the polling system now rests on, even if the first answer to it was the wrong one.
- **[@Trebor87](https://github.com/Trebor87)** — Surfaced the install-lifetime AC capabilities cache trap after re-pairing a Smart AC Control unit ([Discussion #280](https://github.com/hiall-fyi/tado_ce/discussions/280)). After Tado's E04 errors forced a controller rebuild, the local cache (keyed by zone_id) still pointed at the old zone_id while the new pairing got a fresh one, leaving the climate entity falling through to a defaults-only `[OFF]` HVAC mode list. His careful walkthrough of the symptom and follow-up confirmation that the existing **Refresh AC Capabilities** button repairs it without manually deleting the cache file drove the FEATURES_GUIDE Hub Buttons section that documents the button properly, plus the rename from "Refresh AC" to "Refresh AC Capabilities" so the entity name itself tells users what the button does.
- **[@BirbByte](https://github.com/BirbByte)** — Caught the polish bugs that turned beta.1's split-axis swing dropdown from "works" into "feels right" ([#270](https://github.com/hiall-fyi/tado_ce/issues/270)). His Mitsubishi screenshot showed the values arriving in alphabetical order (`Down / Mid / Mid (down) / Mid (up) / Up`) rather than in physical-louver order, which read as a random scatter on a position picker; that drove the explicit ordering layer that now ships, with unknown values from future Tado capability changes still falling through alphabetically at the tail so they stay visible. The same screenshot also caught the icon inconsistency where `On` rendered with HA's built-in `mdi:swap-vertical` while every fixed position had no icon at all, which led to the uniform set that ships here (directional `mdi:arrow-*-thin` for positions, `mdi:auto-mode` for `Auto`, `mdi:circle-small` for `Off`). Both fixes apply across every AC make, not just Mitsubishi, because the underlying capability sort and the icon dictionary are shared paths.
- **[@MacrosorcH](https://github.com/MacrosorcH)** — Hardware-on-hand correction that fixed a wrong HomeKit local-control claim shipping since v4.0.0 GA ([Discussion #271](https://github.com/hiall-fyi/tado_ce/discussions/271)). The README HomeKit table cell read `✅ (temperature only)` for Smart AC Control V3+, which assumed AC units pair through the Internet Bridge V3+ alongside thermostats and TRVs; the actual hardware behaviour, which he verified on his own units, is that each Smart AC Control is an autonomous WiFi accessory with its own 8-digit HomeKit code paired separately from the bridge. None of the HAP HeaterCooler service characteristics that AC units use are wired in today, so every AC write goes through the cloud regardless of whether HomeKit is paired. The README cell, FEATURES_GUIDE Known Limitations entry, and ROADMAP entry all corrected together; without his correction, the wrong claim would still be in the docs and a follow-up "AC swing local-path scoping" thread that turned out to be built on the same wrong premise would have shipped a few weeks of misdirection.
- **[@clude86](https://github.com/clude86)** — Reported the misleading HomeKit setup steps in the README that had been wrong since v4.0.0 GA ([Discussion #271](https://github.com/hiall-fyi/tado_ce/discussions/271)). Step 2 instructed users to add the Tado Internet Bridge as a HomeKit Device through Home Assistant's standard HomeKit integration before entering the pairing code in Tado CE, which fails because the Tado bridge only allows one HomeKit controller at a time and HA's built-in pairing claims the slot, leaving Tado CE unable to find an unpaired bridge. The clear walkthrough of the working alternative (enable HomeKit in the Tado app, skip the HA HomeKit Device step entirely, enter the code straight into Tado CE's Configure form) drove the rewrite of the README setup section and the Apple Home FAQ entry, plus the removal of the misleading restart-HA line. First user to spot it.
- **[@amplitur](https://github.com/amplitur)** — Reported the missing TRV LED feedback after a HomeKit-routed temperature change ([#281](https://github.com/hiall-fyi/tado_ce/issues/281)). The HomeKit Accessory Protocol doesn't carry a feedback channel for the visual confirmation that Tado's bridge protocol triggers, so the silent local write is by-design; documenting it explicitly + showing the `tado_ce.identify_device` workaround (one cloud call to flash the LED on demand) closes the discoverability gap so other HomeKit users have a clear answer when they notice the same thing.
- **[@hapklaar](https://github.com/hapklaar)** — Requested a `tado_ce.turn_off_all_zones` service that mirrors the Tado app's "Turn OFF all rooms" button ([#283](https://github.com/hiall-fyi/tado_ce/issues/283)). Concrete use case: an automation that turns off heating when outside temperature is above 15°C, where calling `climate.turn_off` per zone gets immediately overridden by the next schedule block. The new service uses MANUAL termination so schedules stay suppressed until manually resumed, matching exactly how the Tado app's button behaves and closing the gap his automation hit.

### v4.0.0

The 4.0 cycle ran across 16 betas between April and May 2026. The contributors below shaped the release through bug reports, feature ideas, multi-round debugging, and field testing. Many of them returned across multiple betas as fixes uncovered new edge cases — credit covers the cycle as a whole, not a single point in time.

- **[@simonotter](https://github.com/simonotter)** — The single biggest driver of Smart Valve Control quality. Originally reported `offset_celsius` showing a raw Fahrenheit value with detailed math checks and a working automation workaround that confirmed the feedback-loop root cause ([#221](https://github.com/hiall-fyi/tado_ce/issues/221)). Then carried the SVC investigation across three months — the cloud-write-failed-every-cycle screenshot, the Invalid repairs platform startup error, the all-OFF schedule deadlock, the double-compensation warning, the Offset Sync stuck-after-restart timer, and finally the ±10°C clamp graph showing HA at +10°C while Tado showed -5.9°C ([#262](https://github.com/hiall-fyi/tado_ce/issues/262)) — which surfaced the silent clamp saturation as a UX issue and revealed a poisoned-cache failure mode in the same thread. The Offset Sync feature exists in shippable form because of his persistence.
- **[@ChrisMarriott38](https://github.com/ChrisMarriott38)** — The most prolific reporter of the cycle. Spotted the spikey temperature and humidity charts when HomeKit was connected ([#224](https://github.com/hiall-fyi/tado_ce/issues/224)), the blocky 5% humidity jumps with side-by-side Tado-app comparison ([#234](https://github.com/hiall-fyi/tado_ce/issues/234)), the boiler flow temperature being tied to cloud polling with a 200-minute-vs-60-second comparison chart ([#237](https://github.com/hiall-fyi/tado_ce/issues/237)), the bridge credentials wipe on saving collapsed sections ([#227](https://github.com/hiall-fyi/tado_ce/issues/227)), the API counter reset-on-restart, the bridge-credentials-under-WC misclassification ([#240](https://github.com/hiall-fyi/tado_ce/issues/240)), the custom polling not keeping weather fresh ([#239](https://github.com/hiall-fyi/tado_ce/issues/239)), the blocking I/O startup warning ([Discussion #219](https://github.com/hiall-fyi/tado_ce/discussions/219)), and the real-time HomeKit update gap. Almost every HomeKit-related fix in 4.0 traces to one of his side-by-side screenshots or graphs.
- **[@Si-Hill](https://github.com/Si-Hill)** — Started Smart Valve Control with the question "how do I get the TRV to keep heating until the room actually reaches target?" ([Discussion #231](https://github.com/hiall-fyi/tado_ce/discussions/231)) and the RoomMind / BetterThermostat context that shaped the proportional-offset approach. Then drove Offset Sync from "writes when heating is OFF causing TRV motor noise overnight" through the 5-minute oscillation graphs to the per-zone sensitivity proposal that shipped as **Offset Sync Sensitivity** ([Discussion #219](https://github.com/hiall-fyi/tado_ce/discussions/219), [#251](https://github.com/hiall-fyi/tado_ce/issues/251)). Pulse-card-side debugging on `valve_target` decimal precision and the SVC idle-state attribute also came from his thread ([pulse-card#45](https://github.com/hiall-fyi/pulse-card/issues/45)).
- **[@wrowlands3](https://github.com/wrowlands3)** — Validated Smart Valve Control's design with real Better Thermostat screenshots and API-cost data that confirmed the need for a native solution ([Discussion #231](https://github.com/hiall-fyi/tado_ce/discussions/231)). Flagged the "all schedule blocks OFF" deadlock during beta testing, reported the integration-failing-to-set-up crash on null overlay temperature ([#252](https://github.com/hiall-fyi/tado_ce/issues/252)), and shared the Valve Target oscillation graph that proved the HomeKit stale-bridge-target bug affected Valve Target users alongside Offset Sync — which extended the 3-minute write protection window to cloud writes.
- **[@apilone](https://github.com/apilone)** — Owned the HomeKit-vs-Tado-app sync gap from end to end. The original "HomeKit not updating target temperature or mode" report ([#253](https://github.com/hiall-fyi/tado_ce/issues/253)) shipped the 3-minute write protection window. The frost-protection regression with the precise 5°C → 19.5°C oscillation screenshot sequence ([#258](https://github.com/hiall-fyi/tado_ce/issues/258)) extended OFF-state handling. The two-bugs-in-one-thread report ([#261](https://github.com/hiall-fyi/tado_ce/issues/261)) — OFF→HEAT not reflecting plus the API-burst loop draining the daily quota overnight — came with the API Usage chart and log evidence that pinpointed the back-off-retry setup loop, a problem invisible without that chart.
- **[@driagi](https://github.com/driagi)** — Persisted through four rounds of Weather Compensation "Unknown" debugging across two betas ([#249](https://github.com/hiall-fyi/tado_ce/issues/249)). The decisive datapoints — Status sensor showing `paused`, clean outdoor history with no gaps, 8-to-48-hour stuck periods starting after midnight, and the context that he had switched from ~7-min polling to Auto night polling (120 min) after HomeKit shipped — triangulated a second, deeper bug (a stale-reading gate that latched the engine when evaluation gaps exceeded 60 min). Also shared the Italian-HA debug log that exposed DeepL translating placeholder names inside curly braces across all 6 non-English files, and raised the bridge serial masking concern.
- **[@Newreader](https://github.com/Newreader)** — Three rounds of exceptionally detailed startup analysis. The 429 rate-limit traceback exposed the config flow's missing retry logic. The "entity is fresh" false-positive analysis — narrowing from "4 of 7 zones affected" through the `Marked entity fresh` grep timing — pinpointed a tricky boot-time race ([#246](https://github.com/hiall-fyi/tado_ce/issues/246)). When `tado_ce_ready` was proposed, his refinement was the one that made it correct: fire only when all climate entities have real data, not just when the coordinator has run. Also reported the climate card sticking on 23°C after `set_hvac_mode: off` while Tado correctly showed Frost Protection ([#258](https://github.com/hiall-fyi/tado_ce/issues/258)).
- **[@dragorex71](https://github.com/dragorex71)** — Spotted that climate entities showed "auto" instead of "off" when Away mode turned off heating via the schedule, with debug logs that traced it to the integration only checking manual overlays. Caught the presence-Away-snap-back-to-Home bug across both the Hub select and climate card preset paths ([Discussion #219](https://github.com/hiall-fyi/tado_ce/discussions/219)), and flagged the Italian translation mismatch between the two ("Casa"/"Via" vs "In casa"/"Fuori casa"). Also reported the original blank Advanced Settings page ([#220](https://github.com/hiall-fyi/tado_ce/issues/220)).
- **[@jeverley](https://github.com/jeverley)** — Pushed the binary sensor entity-type correction further than the original proposal — identifying that connection and hot water power are genuinely boolean and should use the `CONNECTIVITY` and `POWER` device classes while battery correctly stays as a sensor ([#160](https://github.com/hiall-fyi/tado_ce/issues/160)). Discovered that overlay services weren't triggering entity state updates, and spotted that overnight temperature graphs showed polling steps despite HomeKit being connected ([Discussion #219](https://github.com/hiall-fyi/tado_ce/discussions/219)). Also built and shared a comprehensive [Window Mode Blueprint](https://raw.githubusercontent.com/jeverley/home-assistant-blueprints/refs/heads/main/blueprints/automation/tado_ce_window_mode_sensors.yaml) for controlling Tado CE window mode with physical sensors.
- **[@mpartington](https://github.com/mpartington)** — Tested all three overlay-mode workarounds and reported back with clear results that confirmed `climate.set_hvac_mode` wasn't respecting the configured overlay mode ([Discussion #219](https://github.com/hiall-fyi/tado_ce/discussions/219)). The systematic comparison (global Manual, `set_climate_timer` with manual, `set_climate_timer` with next_time_block) directly led to discovering the HomeKit write path was bypassing overlay termination entirely.
- **[@hapklaar](https://github.com/hapklaar)** — Reported the climate card showing the wrong target temperature after switching from manual to Auto, with side-by-side HA-vs-Tado-app screenshots and debug logs confirming HomeKit connection stability ([Discussion #219](https://github.com/hiall-fyi/tado_ce/discussions/219)).
- **[@Thilas](https://github.com/Thilas)** — Originally proposed converting battery and connection sensors to binary sensors for better HA grouping support ([#160](https://github.com/hiall-fyi/tado_ce/issues/160)).

### v3.5.3

- **[@jeverley](https://github.com/jeverley)** — Reported that overlay sensors don't show timer end time, and provided the full API response confirming the `termination.expiry` field for Timer overlays ([#217](https://github.com/hiall-fyi/tado_ce/issues/217))

### v3.5.2

- **[@driagi](https://github.com/driagi)** — Reported DNS failure causing token refresh to give up without retrying, with full traceback that pinpointed the missing network error retry path ([#214](https://github.com/hiall-fyi/tado_ce/issues/214))

### v3.5.1

- **[@driagi](https://github.com/driagi)** — Flagged that the integration lacked error recovery when API calls failed, with all entities staying unavailable until manual reconfiguration ([#206](https://github.com/hiall-fyi/tado_ce/issues/206)). This report prompted a full review of every API call path over the Easter break, uncovering that 11 cloud operations had no retry logic at all. The resulting audit also led to the codebase-wide quality cleanup in this release — clearing the deck before the HomeKit local control work ahead.

### v3.5.0

- **[@Saughassy](https://github.com/Saughassy)** — Original clean install crash report that led to the deeper quota deadlock root cause fix ([#204](https://github.com/hiall-fyi/tado_ce/issues/204))
- **[@mat01](https://github.com/mat01)** — Reported stale `offset_celsius` attribute after service call with detailed root cause analysis, enabling a quick fix ([#211](https://github.com/hiall-fyi/tado_ce/issues/211))
- **[@Prodeguerriero](https://github.com/Prodeguerriero)** — Flagged confusing "full sync" / "quick sync" wording in settings descriptions and suggested renaming the Home binary sensor to "Geofencing" ([Discussion #131](https://github.com/hiall-fyi/tado_ce/discussions/131))

### v3.4.1

- **[@Saughassy](https://github.com/Saughassy)** — Reported clean install crash with detailed debug logs that pinpointed the rate limit data handling issue ([#204](https://github.com/hiall-fyi/tado_ce/issues/204))

### v3.3.1

- **[@driagi](https://github.com/driagi)** — Identified that fixed-slope heating curves hit the min flow floor prematurely, proposed the auto-slope formula that now powers all preset curves ([#187](https://github.com/hiall-fyi/tado_ce/issues/187))

### v3.3.0

- **[@driagi](https://github.com/driagi)** — Proposed weather compensation for boiler flow temperature, provided extensive Blueprint testing and feedback including oscillation fix and temperature averaging suggestions ([#187](https://github.com/hiall-fyi/tado_ce/issues/187))
- **[@thefern69](https://github.com/thefern69)** — Reported preheat still triggering during Away mode transition, proposed passive preheat mode to respect external manual overrides ([#171](https://github.com/hiall-fyi/tado_ce/issues/171))
- **[@ChrisMarriott38](https://github.com/ChrisMarriott38)** — Investigated V2 vs V3 bridge compatibility, provided serial format details that led to bridge serial validation, tested Bridge entity visibility ([#187](https://github.com/hiall-fyi/tado_ce/issues/187))
- **[@neonsp](https://github.com/neonsp)** — Confirmed climate card null temperature on first install, suggested default temperature values and smart AC mode selection ([#182](https://github.com/hiall-fyi/tado_ce/issues/182))

### v3.2.2

- **[@driagi](https://github.com/driagi)** — Provided critical v3.2.1 debug logs that revealed the wrong data path for Boiler Output Temperature sensor, enabling the fix ([#187](https://github.com/hiall-fyi/tado_ce/issues/187))

### v3.2.1

- **[@driagi](https://github.com/driagi)** — Reported Bridge API sensor showing "Unknown" and provided debug logs for troubleshooting ([#187](https://github.com/hiall-fyi/tado_ce/issues/187))
- **[@jeverley](https://github.com/jeverley)** — Requested indefinite open window mode duration ([Discussion #184](https://github.com/hiall-fyi/tado_ce/discussions/184))

### v3.2.0

- **[@neonsp](https://github.com/neonsp)** — Reported climate card temperature null after HA restart ([#182](https://github.com/hiall-fyi/tado_ce/issues/182))
- **[@BirbByte](https://github.com/BirbByte)** — Reported external sensor not updating in real-time ([#143](https://github.com/hiall-fyi/tado_ce/issues/143))
- **[@driagi](https://github.com/driagi)** — Requested set open window mode service ([#172](https://github.com/hiall-fyi/tado_ce/issues/172), [Discussion #184](https://github.com/hiall-fyi/tado_ce/discussions/184))

### v3.1.1

- **[@MathiasB112](https://github.com/MathiasB112)** — Reported device authorization broken due to Tado server changes ([#185](https://github.com/hiall-fyi/tado_ce/issues/185))
- **[@UKICS](https://github.com/UKICS)** — Confirmed device authorization issue affecting both Tado CE and official integration ([#185](https://github.com/hiall-fyi/tado_ce/issues/185))
- **[@driagi](https://github.com/driagi)** — Reported climate card unusable when zone is OFF ([#182](https://github.com/hiall-fyi/tado_ce/issues/182))
- **[@neonsp](https://github.com/neonsp)** — Reported AC always defaulting to COOL when turning on ([#182](https://github.com/hiall-fyi/tado_ce/issues/182))

### v3.1.0

- **[@jeverley](https://github.com/jeverley)** — Requested EntityCategory for all entities ([#178](https://github.com/hiall-fyi/tado_ce/issues/178))
- **[@driagi](https://github.com/driagi)** — Requested Open Window Mode services ([#172](https://github.com/hiall-fyi/tado_ce/issues/172))
- **[@ChrisMarriott38](https://github.com/ChrisMarriott38)** — Requested Window Predicted Sensitivity ([#135](https://github.com/hiall-fyi/tado_ce/issues/135))
- **[@BirbByte](https://github.com/BirbByte)** — Requested External Sensor Override ([#143](https://github.com/hiall-fyi/tado_ce/issues/143))
- **[@neonsp](https://github.com/neonsp)** — Reported AC temperature limits capped at 25°C ([#180](https://github.com/hiall-fyi/tado_ce/issues/180))
- **[@rodneyha](https://github.com/rodneyha)** — Reported overlay mode naming confusion ([#176](https://github.com/hiall-fyi/tado_ce/issues/176))
- **[@Xavinooo](https://github.com/Xavinooo)** — Reported full sync running every 6 hours ([#141](https://github.com/hiall-fyi/tado_ce/issues/141))

### v3.0.4

- **[@driagi](https://github.com/driagi)** — Reported inaccurate API reset time estimation ([#173](https://github.com/hiall-fyi/tado_ce/issues/173))

### v3.0.3

- **[@driagi](https://github.com/driagi)** — Reported hub sensors showing stale data after API sync ([#173](https://github.com/hiall-fyi/tado_ce/issues/173))

### v3.0.2

- **[@driagi](https://github.com/driagi)** — Reported setup deadlock and hub sensor issues ([#170](https://github.com/hiall-fyi/tado_ce/issues/170), [#173](https://github.com/hiall-fyi/tado_ce/issues/173))
- **[@tigro7](https://github.com/tigro7)** — Confirmed deadlock issue ([#170](https://github.com/hiall-fyi/tado_ce/issues/170))
- **[@mpartington](https://github.com/mpartington)** — Confirmed deadlock issue ([#170](https://github.com/hiall-fyi/tado_ce/issues/170))
- **[@thefern69](https://github.com/thefern69)** — Reported preheat triggering during Away mode, requested cooling rate prediction ([#171](https://github.com/hiall-fyi/tado_ce/issues/171), [Discussion #163](https://github.com/hiall-fyi/tado_ce/discussions/163))

### v3.0.1

- **[@jeverley](https://github.com/jeverley)** — Reported `[CE]` prefix in entity names ([#167](https://github.com/hiall-fyi/tado_ce/issues/167))
- **[@hapklaar](https://github.com/hapklaar)** — Reported duplicate sensor names and entities going unavailable ([#167](https://github.com/hiall-fyi/tado_ce/issues/167))
- **[@andyb2000](https://github.com/andyb2000)** — Confirmed entities going unavailable ([#167](https://github.com/hiall-fyi/tado_ce/issues/167))

### v3.0.0

- **[@robvol87](https://github.com/robvol87)** — Requested multi-home support ([#110](https://github.com/hiall-fyi/tado_ce/issues/110))
- **[@Blankf](https://github.com/Blankf)** — Requested multi-home support ([#145](https://github.com/hiall-fyi/tado_ce/issues/145))
- **[@thefern69](https://github.com/thefern69)** — Requested preheat cooling rate prediction, reported preheat sensor triggering early ([Discussion #163](https://github.com/hiall-fyi/tado_ce/discussions/163), [#164](https://github.com/hiall-fyi/tado_ce/issues/164))
- **[@joaomacp](https://github.com/joaomacp)** — Requested lowering timer minimum to 1 minute ([#162](https://github.com/hiall-fyi/tado_ce/issues/162))
- **[@tanerpaca](https://github.com/tanerpaca)** — Reported window sensor not detecting without Auto-Assist ([#157](https://github.com/hiall-fyi/tado_ce/issues/157))

### v2.3.1

- **[@BirbByte](https://github.com/BirbByte)** — Reported AC fan speed reverting on Mitsubishi/Fujitsu ([#142](https://github.com/hiall-fyi/tado_ce/issues/142))
- **[@slflowfoon](https://github.com/slflowfoon)** — Reported blocking I/O warning on fresh install ([#127](https://github.com/hiall-fyi/tado_ce/issues/127))

### v2.3.0

- **[@mpartington](https://github.com/mpartington)** — Requested enhanced `set_climate_timer` service ([#152](https://github.com/hiall-fyi/tado_ce/issues/152))
- **[@ChrisMarriott38](https://github.com/ChrisMarriott38)** — Reported mold risk and hot water overlay issues ([#147](https://github.com/hiall-fyi/tado_ce/issues/147), [#149](https://github.com/hiall-fyi/tado_ce/issues/149))
- **[@driagi](https://github.com/driagi)** — Reported mobile device tracker not updating ([#150](https://github.com/hiall-fyi/tado_ce/issues/150))

### v2.2.3

- **[@mkruiver](https://github.com/mkruiver)** — Reported adaptive polling stuck for low-quota users ([#144](https://github.com/hiall-fyi/tado_ce/issues/144))
- **[@Xavinooo](https://github.com/Xavinooo)** — Reported night polling using wrong interval ([#141](https://github.com/hiall-fyi/tado_ce/issues/141))
- **[@BirbByte](https://github.com/BirbByte)** — Reported AC fan speed reverting ([#142](https://github.com/hiall-fyi/tado_ce/issues/142))
- **[@merlinpimpim](https://github.com/merlinpimpim)** — Requested climate group support ([Discussion #139](https://github.com/hiall-fyi/tado_ce/discussions/139))

### v2.2.2

- **[@Xavinooo](https://github.com/Xavinooo)** — Reported API options validation issues ([#134](https://github.com/hiall-fyi/tado_ce/issues/134))

### v2.2.1

- **[@jeverley](https://github.com/jeverley)** — Reported hot water config for tank-based systems ([#115](https://github.com/hiall-fyi/tado_ce/issues/115))
- **[@ChrisMarriott38](https://github.com/ChrisMarriott38)** — Reported API options not saving ([#134](https://github.com/hiall-fyi/tado_ce/issues/134))
- **[@Xavinooo](https://github.com/Xavinooo)** — Confirmed API options not saving ([#134](https://github.com/hiall-fyi/tado_ce/issues/134))

### v2.2.0

- **[@tigro7](https://github.com/tigro7)** — Proposed window predicted sensor, actionable recommendations, home/zone insights ([Discussion #112](https://github.com/hiall-fyi/tado_ce/discussions/112))
- **[@BruceRobertson](https://github.com/BruceRobertson)** — Reported heating cycle never completing ([#125](https://github.com/hiall-fyi/tado_ce/issues/125))
- **[@slflowfoon](https://github.com/slflowfoon)** — Reported API call history error on first run ([#127](https://github.com/hiall-fyi/tado_ce/issues/127))
- **[@hacker4257](https://github.com/hacker4257)** — Contributed fix for API call history directory creation ([PR #132](https://github.com/hiall-fyi/tado_ce/pull/132))
- **[@jeverley](https://github.com/jeverley)** — Reported hot water config for tank-based systems ([#115](https://github.com/hiall-fyi/tado_ce/issues/115))
- **[@Xavinooo](https://github.com/Xavinooo)** — Reported polling override issues ([#126](https://github.com/hiall-fyi/tado_ce/issues/126))
- **[@BirbByte](https://github.com/BirbByte)** — Reported AC swing mode issue for Mitsubishi ([#128](https://github.com/hiall-fyi/tado_ce/issues/128))

### v2.1.1

- **[@ChrisMarriott38](https://github.com/ChrisMarriott38)** — Reported Test Mode polling and hot water entity issues ([#119](https://github.com/hiall-fyi/tado_ce/issues/119), [#120](https://github.com/hiall-fyi/tado_ce/issues/120))

### v2.1.0

- **[@ChrisMarriott38](https://github.com/ChrisMarriott38)** — Inspired per-zone surface temp offset and thermal analytics selection ([#90](https://github.com/hiall-fyi/tado_ce/issues/90), [#91](https://github.com/hiall-fyi/tado_ce/issues/91))
- **[@jakeycrx](https://github.com/jakeycrx)** — Reported custom polling below 5 minutes not working ([#107](https://github.com/hiall-fyi/tado_ce/issues/107))

### v2.0.2

- **[@wyx087](https://github.com/wyx087)** — Proposed Presence Mode Select ([Discussion #102](https://github.com/hiall-fyi/tado_ce/discussions/102))
- **[@leoogermenia](https://github.com/leoogermenia)** — Requested configurable overlay mode ([#101](https://github.com/hiall-fyi/tado_ce/issues/101))
- **[@ChrisMarriott38](https://github.com/ChrisMarriott38)** — Reported polling stuck in Uniform Mode ([#99](https://github.com/hiall-fyi/tado_ce/issues/99))

### v2.0.1

- **[@Claeysjens](https://github.com/Claeysjens)** — Reported climate entities unavailable after upgrade ([#100](https://github.com/hiall-fyi/tado_ce/issues/100))
- **[@ChrisMarriott38](https://github.com/ChrisMarriott38)** — Reported mold risk, thermal analytics, hot water, and quota reserve issues ([#90](https://github.com/hiall-fyi/tado_ce/issues/90), [#91](https://github.com/hiall-fyi/tado_ce/issues/91), [#98](https://github.com/hiall-fyi/tado_ce/issues/98), [#99](https://github.com/hiall-fyi/tado_ce/issues/99))

### v2.0.0

- **[@ChrisMarriott38](https://github.com/ChrisMarriott38)** — Inspired adaptive polling, enhanced mold risk, quota reserve ([#89](https://github.com/hiall-fyi/tado_ce/issues/89), [#90](https://github.com/hiall-fyi/tado_ce/issues/90), [#94](https://github.com/hiall-fyi/tado_ce/issues/94))
- **[@Fred224](https://github.com/Fred224)** — Reported hot water timer buttons issue ([#93](https://github.com/hiall-fyi/tado_ce/issues/93))

### v1.10.0

- **[@hapklaar](https://github.com/hapklaar)**, **[@chinezbrun](https://github.com/chinezbrun)**, **[@neonsp](https://github.com/neonsp)** — Extensive testing and feedback on climate entity flickering ([#44](https://github.com/hiall-fyi/tado_ce/issues/44))

### v1.9.x

- **[@hapklaar](https://github.com/hapklaar)** — Reported hvac_action stuck, Resume All delay, grey loading state ([#44](https://github.com/hiall-fyi/tado_ce/issues/44))
- **[@chinezbrun](https://github.com/chinezbrun)** — Reported grey loading state, slow state confirmation ([#44](https://github.com/hiall-fyi/tado_ce/issues/44))
- **[@neonsp](https://github.com/neonsp)** — Reported AC startup warnings, optimistic update issues ([#44](https://github.com/hiall-fyi/tado_ce/issues/44))
- **[@Fred224](https://github.com/Fred224)** — Reported AC DRY mode 422 error ([#79](https://github.com/hiall-fyi/tado_ce/issues/79))
- **[@thefern69](https://github.com/thefern69)** — Reported device migration crash, proposed preheat concept ([#74](https://github.com/hiall-fyi/tado_ce/issues/74), [Discussion #33](https://github.com/hiall-fyi/tado_ce/discussions/33))
- **[@ChrisMarriott38](https://github.com/ChrisMarriott38)** — Reported API reset, device sensor, and environment monitoring issues ([#54](https://github.com/hiall-fyi/tado_ce/issues/54), [#56](https://github.com/hiall-fyi/tado_ce/issues/56), [#64](https://github.com/hiall-fyi/tado_ce/issues/64))
- **[@neonsp](https://github.com/neonsp)** — Reported AC capabilities consuming unnecessary API calls ([#61](https://github.com/hiall-fyi/tado_ce/issues/61))
- **[@colinada](https://github.com/colinada)** — Reported temperature offset for multi-TRV rooms ([#66](https://github.com/hiall-fyi/tado_ce/issues/66))

### v1.8.x

- **[@neonsp](https://github.com/neonsp)** — Reported AC state feedback issues, suggested capabilities caching ([#44](https://github.com/hiall-fyi/tado_ce/issues/44), [#61](https://github.com/hiall-fyi/tado_ce/issues/61))
- **[@hapklaar](https://github.com/hapklaar)** — Reported Resume All delay, requested Resume All button ([#44](https://github.com/hiall-fyi/tado_ce/issues/44), [Discussion #39](https://github.com/hiall-fyi/tado_ce/discussions/39))
- **[@ChrisMarriott38](https://github.com/ChrisMarriott38)** — Requested API Reset attributes, suggested Home State Sync default ([#54](https://github.com/hiall-fyi/tado_ce/issues/54), [#55](https://github.com/hiall-fyi/tado_ce/issues/55))

### v1.7.0

- **[@neonsp](https://github.com/neonsp)** — Reported UI not updating immediately, requested optional homeState sync ([#44](https://github.com/hiall-fyi/tado_ce/issues/44), [#31](https://github.com/hiall-fyi/tado_ce/issues/31))

### v1.6.0

- **[@neonsp](https://github.com/neonsp)** — Reported `climate.set_temperature` ignoring `hvac_mode` ([#31](https://github.com/hiall-fyi/tado_ce/issues/31))
- **[@hapklaar](https://github.com/hapklaar)** — Reported climate entities not updating consistently ([#44](https://github.com/hiall-fyi/tado_ce/issues/44))

### v1.5.x

- **[@neonsp](https://github.com/neonsp)** — Comprehensive AC testing, identified all 6 AC issues ([#31](https://github.com/hiall-fyi/tado_ce/issues/31))
- **[@hapklaar](https://github.com/hapklaar)** — Requested Resume All button, reported token loss ([Discussion #39](https://github.com/hiall-fyi/tado_ce/discussions/39), [#34](https://github.com/hiall-fyi/tado_ce/issues/34))
- **[@jeverley](https://github.com/jeverley)** — Reported token loss, requested re-authenticate option ([#34](https://github.com/hiall-fyi/tado_ce/issues/34))
- **[@wrowlands3](https://github.com/wrowlands3)** — Confirmed token loss issue ([#34](https://github.com/hiall-fyi/tado_ce/issues/34))
- **[@mkruiver](https://github.com/mkruiver)** — Reported OAuth flow error for new users ([#36](https://github.com/hiall-fyi/tado_ce/issues/36))
- **[@harryvandervossen](https://github.com/harryvandervossen)** — Provided detailed OAuth flow feedback ([Discussion #35](https://github.com/hiall-fyi/tado_ce/discussions/35))
- **[@pisolofin](https://github.com/pisolofin)** — Requested `get_temperature_offset` service ([#24](https://github.com/hiall-fyi/tado_ce/issues/24))
- **[@ohipe](https://github.com/ohipe)** — Requested optional `offset_celsius` attribute ([#25](https://github.com/hiall-fyi/tado_ce/issues/25))
- **[@beltrao](https://github.com/beltrao)** — Requested frequent mobile device sync ([#28](https://github.com/hiall-fyi/tado_ce/issues/28))
- **[@hapklaar](https://github.com/hapklaar)** — Reported authentication broken after upgrade ([#26](https://github.com/hiall-fyi/tado_ce/issues/26))
- **[@mjsarfatti](https://github.com/mjsarfatti)** — Confirmed authentication broken after upgrade ([#26](https://github.com/hiall-fyi/tado_ce/issues/26))

### v1.4.0

- **[@ChrisMarriott38](https://github.com/ChrisMarriott38)** — Reported boiler flow temp, API reset time, and options UI issues ([#15](https://github.com/hiall-fyi/tado_ce/issues/15), [#16](https://github.com/hiall-fyi/tado_ce/issues/16), [#17](https://github.com/hiall-fyi/tado_ce/issues/17))
- **[@jeverley](https://github.com/jeverley)** — Reported climate preset mode stuck on Away ([#22](https://github.com/hiall-fyi/tado_ce/issues/22))
- **[@hapklaar](https://github.com/hapklaar)** — Volunteered for OpenTherm testing

### v1.2.x

- **[@marcovn](https://github.com/marcovn)** — Reported duplicate hub issue, confusing entity names ([#10](https://github.com/hiall-fyi/tado_ce/issues/10), [#11](https://github.com/hiall-fyi/tado_ce/issues/11))
- **[@ChrisMarriott38](https://github.com/ChrisMarriott38)** — Extensive feature requests and testing ([#4](https://github.com/hiall-fyi/tado_ce/issues/4), [#10](https://github.com/hiall-fyi/tado_ce/issues/10))
- **[@wrowlands3](https://github.com/wrowlands3)** — Requested zone-based device organization ([#4](https://github.com/hiall-fyi/tado_ce/issues/4))
- **[@donnie-darko](https://github.com/donnie-darko)** — Requested `set_water_heater_timer` service ([#4](https://github.com/hiall-fyi/tado_ce/issues/4))
- **[@StreborStrebor](https://github.com/StreborStrebor)** — Requested immediate refresh, AC fan/swing controls ([#4](https://github.com/hiall-fyi/tado_ce/issues/4))
- **[@hapklaar](https://github.com/hapklaar)** — Suggested humidity attribute, preset mode support ([#2](https://github.com/hiall-fyi/tado_ce/issues/2), [#5](https://github.com/hiall-fyi/tado_ce/issues/5), [#10](https://github.com/hiall-fyi/tado_ce/issues/10))
- **[@LorDHarA](https://github.com/LorDHarA)** — First bug report — 403 auth error ([#1](https://github.com/hiall-fyi/tado_ce/issues/1))
- **[@MJWMJW2](https://github.com/MJWMJW2)** — Requested Away Mode switch ([#3](https://github.com/hiall-fyi/tado_ce/issues/3))
- **[@ctcampbell](https://github.com/ctcampbell)** — Requested proper hot water operation modes ([#6](https://github.com/hiall-fyi/tado_ce/issues/6))
- **[@greavous1138](https://github.com/greavous1138)** — Reported `duration` parameter issue, requested boost button ([#7](https://github.com/hiall-fyi/tado_ce/issues/7))
- **[@thefern69](https://github.com/thefern69)** — Provided Docker installation instructions ([#9](https://github.com/hiall-fyi/tado_ce/issues/9))

---

## 🌟 Special Thanks

**All community members** who tested, reported issues, shared use cases, and supported the project. You make Tado CE better every release.

---

**Made with ❤️ by the Tado CE community**
