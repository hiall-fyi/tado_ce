# Credits

Tado CE is built with the help of an amazing community. This page recognises everyone who has contributed — through support, bug reports, feature ideas, testing, and code.

---

## ☕ Supporters

Thank you to everyone who supported the project through [Buy Me a Coffee](https://buymeacoffee.com/hiallfyi). Your generosity keeps the lights on.

| | Supporters |
|---|-----------|
| ☕☕☕☕☕☕☕☕ | [@ChrisMarriott38](https://github.com/ChrisMarriott38) |
| ☕☕☕☕☕☕ | [@jeverley](https://github.com/jeverley) |
| ☕☕☕☕☕ | Marcel v.H., [@rodneyha](https://github.com/rodneyha), [@UKICS](https://github.com/UKICS) |
| ☕☕ | Arnaud L., [@hapklaar](https://github.com/hapklaar), [@jeromewir](https://github.com/jeromewir), Luke R., [@marcovn](https://github.com/marcovn), [@Prodeguerriero](https://github.com/Prodeguerriero) |
| ☕ | Alby T., [@MathiasB112](https://github.com/MathiasB112) |

---

## Per-Version Credits

Community contributors who helped shape each release through bug reports, feature requests, testing, and feedback.

### v4.1.0-beta.1

- **[@Ralf84](https://github.com/Ralf84)** — Drove the AC swing axis split from feature request to merged design ([#270](https://github.com/hiall-fyi/tado_ce/issues/270)). The original "Off / None option" request would have been a one-line patch; the debug log Ralf captured on his unit (`vertical={MID, AUTO, UP, MID_DOWN, DOWN, MID_UP, ON}`, `horizontal={MID, LEFT, MID_LEFT, MID_RIGHT, RIGHT, ON}`) exposed how lossy the v4.0 unified four-value dropdown was on fine-grained AC units, and turned the fix into a proper split-axis rework using `ClimateEntityFeature.SWING_HORIZONTAL_MODE`. The same root cause had produced [#128](https://github.com/hiall-fyi/tado_ce/issues/128) (@BirbByte, Mitsubishi swing) and [#142](https://github.com/hiall-fyi/tado_ce/issues/142) (@BirbByte, Mitsubishi/Fujitsu fan levels) in earlier cycles — closing all three together was only possible because Ralf grabbed the right log line on the first ask.

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
