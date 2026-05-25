# Roadmap

Planned features and improvements for Tado CE.

For completed features, see [CHANGELOG.md](CHANGELOG.md).

---

## Up Next

**v4.1.0-beta.1 — May 2026.** AC swing dropdown is now split into
independent vertical and horizontal axes, populated directly from the
cloud-reported capability set. Units that report fine-grained louver
positions (Mitsubishi, Fujitsu, etc.) can now be parked at fixed
positions like `Up`, `Mid (down)`, `Left`, instead of being forced
into a sweeping motion. See [CHANGELOG.md](CHANGELOG.md) for the
migration recipe.

**v4.0.0 shipped — May 2026.** Headline changes: HomeKit local
control, Smart Valve Control (Offset Sync + Valve Target modes),
Weather Compensation, multi-home support, actionable insights, and a
redesigned Options Flow. See [CHANGELOG.md](CHANGELOG.md) for what
changed for users coming from v3.5.3.

The next milestone is gathering field feedback on v4.1.0-beta.1 and
triaging the items below for the rest of the 4.x cycle.

## Future Consideration

### AC

- **HomeKit local path for AC swing (vertical, ON/OFF)** — v4.1's
  split swing dropdown still goes through Tado's cloud, so picking a
  swing position uses cloud quota and confirms on the next poll.
  HomeKit's accessory protocol can carry only a binary swing on/off
  (no axes, no fixed positions), but for the largest user segment —
  simple ON/OFF AC units — that's already useful. Wire HomeKit's
  binary `SwingMode` characteristic into the new vertical-axis dropdown
  for those units; cloud stays the path for fine-grained positions and
  the horizontal axis. Gated on confirming Tado's bridge actually
  advertises the characteristic — beta-tester help wanted. Tracker
  issue coming once that's confirmed.

### Smart Valve Control

- **Automation-Friendly Temperature Override** ([#256](https://github.com/hiall-fyi/tado_ce/issues/256) - @apilone) — A new service that sets a zone's target temperature without triggering SVC back-off. Designed for holiday/calendar automations that override the Tado schedule — currently these are indistinguishable from manual changes, so SVC stops compensating exactly when you need it most. No timeline yet.

- **External Flow Temperature Sensor** ([#254](https://github.com/hiall-fyi/tado_ce/issues/254) - @apilone) — Let Weather Compensation read your boiler's actual flow temperature from any HA sensor entity (e.g. myVaillant, ebusd, OTGW) instead of requiring Tado's own OpenTherm bridge. Same "external sensor" pattern already used for room temperature and humidity — just a new config option pointing at your boiler integration's flow temp sensor. Would work for anyone whose boiler integration exposes flow temperature in HA. No timeline yet — post-GA.

- **Exponential Heating Curve** ([#187](https://github.com/hiall-fyi/tado_ce/issues/187) - @driagi) — Non-linear heating curve for weather compensation, using a building thermal sensitivity coefficient (`k` factor). Better models real-world heat loss in well-insulated vs poorly-insulated buildings compared to the current linear approach. Would sit alongside the existing linear presets as an "Expert" option. Deferred for real-world validation during the next heating season.

- **Air Comfort System** ([#64](https://github.com/hiall-fyi/tado_ce/issues/64)) — Per-zone indoor air quality monitoring inspired by the Tado app's Air Comfort feature. Two components: (1) Air Freshness — per-zone freshness level from window opening history and AC activity, zero extra API calls; (2) Outdoor Air Quality — optional external AQI sensor input via Options Flow, same pattern as external temperature/humidity sensors.

### Infrastructure

- **Local Only Mode** — A toggle that stops all cloud polling after initial setup, running purely off HomeKit bridge data. Technically feasible — the coordinator already skips cloud calls when HomeKit provides live data. Tradeoff: cloud-only data (schedules, battery, heating power, geofencing) would go stale. Could include a daily cloud check for diagnostics.

- **Periodic Full Sync** — Currently `zones_info`, `offsets`, `schedules`, and `ac_capabilities` only refresh on the first poll after restart. A periodic full sync (e.g. every 6 hours) would keep this data fresh without requiring a restart. Low priority — this data rarely changes.

### Long-Term Exploration

- **Fully Local Control** ([Discussion #29](https://github.com/hiall-fyi/tado_ce/discussions/29)) — Control via the 868MHz protocol between Bridge and TRVs, bypassing both cloud and HomeKit. Requires specialized hardware and community help.
