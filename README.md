# Tado CE - Custom Integration for Home Assistant

<div align="center">

<!-- Platform Badges -->
![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2026.1.3-blue?style=for-the-badge&logo=home-assistant) ![Tado](https://img.shields.io/badge/Tado-V3%2FV3%2B-orange?style=for-the-badge) ![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)

<!-- Status Badges -->
![Version](https://img.shields.io/badge/Version-1.7.0-purple?style=for-the-badge) ![License](https://img.shields.io/badge/License-AGPL--3.0-blue?style=for-the-badge) ![Maintained](https://img.shields.io/badge/Maintained-Yes-green.svg?style=for-the-badge) ![Tests](https://img.shields.io/badge/Tests-235%20Passing-success?style=for-the-badge)

<!-- Community Badges -->
![GitHub stars](https://img.shields.io/github/stars/hiall-fyi/tado_ce?style=for-the-badge&logo=github) ![GitHub forks](https://img.shields.io/github/forks/hiall-fyi/tado_ce?style=for-the-badge&logo=github) ![GitHub issues](https://img.shields.io/github/issues/hiall-fyi/tado_ce?style=for-the-badge&logo=github) ![GitHub last commit](https://img.shields.io/github/last-commit/hiall-fyi/tado_ce?style=for-the-badge&logo=github)

**A lightweight custom integration for Tado smart thermostats with real API rate limit tracking from Tado's response headers.**

[Features](#-features) • [Quick Start](#-quick-start) • [Entities](#-entities) • [Services](#-services) • [Troubleshooting](#-troubleshooting) • [Discussions](https://github.com/hiall-fyi/tado_ce/discussions)

</div>

---

## Why Tado CE?

In late 2025, Tado announced they would begin enforcing **daily API rate limits** on their REST API. This caught many Home Assistant users off guard:

| Plan | Daily Limit |
|------|-------------|
| Standard (after grace period) | 100 calls |
| Standard (grace period) | 5,000 calls |
| Auto-Assist subscription | 20,000 calls |

The official Home Assistant Tado integration doesn't show your actual API usage or remaining quota. Users have no way to know if they're about to hit the limit until they get blocked.

**Common issues reported by the community:**

- [Upcoming changes to the tado API](https://github.com/home-assistant/core/issues/151223) - Official announcement
- [Tado rate limiting API calls](https://community.home-assistant.io/t/tado-rate-limiting-api-calls/928751) - Community discussion
- [Tado Authentication Broken](https://github.com/home-assistant/core/issues/160472) - Auth issues after HA update
- [Tado login fails](https://github.com/home-assistant/core/issues/161054) - Login timeout issues
- [Re-Authentication loop](https://github.com/home-assistant/core/issues/160237) - Keeps asking for re-auth
- [Bad error handling](https://github.com/home-assistant/core/issues/160487) - Poor error messages
- [Tado Integration Stopped Working](https://community.home-assistant.io/t/tado-integration-stopped-working/867145) - Password flow deprecated

**Tado CE was created to solve these problems:**

1. **Real-time rate limit tracking** - See exactly how many API calls you've used
2. **Dynamic limit detection** - Automatically detects your actual limit (100/5000/20000)
3. **Reset time tracking** - Know when your quota resets (from Tado API headers)
4. **Smart day/night polling** - More frequent during day, less at night
5. **Lightweight design** - Minimal API calls, maximum functionality

---

## Features

Full climate, AC, hot water control with timer support, geofencing, presence detection, weather data, and more. See [Comparison](#-comparison-with-official-integration) for complete feature list.

**Tado CE Exclusive:**

| Feature | Description |
|---------|-------------|
| **Real API Rate Limit** | Actual usage from Tado API headers, not estimates |
| **Reset Time from API** | Automatically detects when your rate limit resets |
| **Dynamic Limit Detection** | Auto-detects your limit (100/5000/20000) |
| **Smart Day/Night Polling** | More frequent during day, less at night to save API calls |
| **Customizable Polling** | Configure day/night hours and custom polling intervals |
| **Multi-Home Selection** | Select which home to configure during setup |
| **Optional Weather/Mobile** | Toggle sensors on/off to save API calls |
| **API Call History** | Track all API calls with configurable retention |
| **Immediate Refresh** | Dashboard updates immediately after user actions |
| **Enhanced Hot Water** | AUTO/HEAT/OFF modes with timer presets (30/60/90 min) |
| **Boiler Flow Temp** | Auto-detected sensor for OpenTherm systems |
| **Zone-Based Devices** | Each zone as separate device with cleaner entity names |
| **Full Async Architecture** | Non-blocking API calls for better responsiveness |
| **Test Mode** | Simulate 100 call limit for testing |

---

## Prerequisites

- Home Assistant 2024.1 or later (tested on 2026.1.3)
- Tado account with V2/V3/V3+ devices

---

## Quick Start

### 1. Install via HACS (Recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=hiall-fyi&repository=tado_ce&category=integration)

1. Click the button above (or add `https://github.com/hiall-fyi/tado_ce` as a custom repository in HACS)
2. Install "Tado CE" from HACS
3. Restart Home Assistant

**Manual Installation**
```bash
# Copy tado_ce folder to custom_components
cp -r tado_ce /config/custom_components/
```

### 2. Add Integration & Authenticate

1. Go to **Settings > Devices & Services**
2. Click **Add Integration**
3. Search for **Tado CE**
4. Click **Submit** to start authorization
5. Click the link or visit `https://login.tado.com/device` and enter the code shown
6. Authorize in your browser
7. Click **Submit** when done
8. If you have multiple homes, select which one to use

That's it! No SSH required. The integration handles everything through Tado's secure device authorization flow.

---

### 3. Verify Success

Check your Home Assistant logs (**Settings > System > Logs**). A successful setup looks like:

```
Tado CE: Integration loading...
Tado CE: Polling interval set to 30m (day)
Tado CE: Executing full sync
Tado CE full sync SUCCESS
Tado CE binary_sensor: Setting up...
Tado CE water_heater: Setting up...
Tado CE device_tracker: Setting up...
Tado CE switch: Setting up...
Tado CE water heaters loaded: 1
Tado CE switches loaded: 17
Tado CE binary sensors loaded: 10
Tado CE sensors loaded: 69
Tado CE climates loaded: 9
Tado CE: Integration loaded successfully
```

The exact numbers depend on your Tado setup (zones, devices, etc.).

---

## Configuration Options

After installation, you can configure Tado CE by clicking the **gear icon** on the integration card in **Settings > Devices & Services > Tado CE**.

### Available Options

#### API Optimization

**Enable Weather Sensors** (v1.2.0)
- **Unchecked** (default): Weather sensors disabled, saves 1 API call per sync
- **Checked**: Weather sensors enabled (outside temperature, solar intensity, weather condition)

**Enable Mobile Device Tracking** (v1.2.0)
- **Unchecked** (default): Mobile device tracking disabled, saves 1 API call per full sync
- **Checked**: Mobile device tracking enabled (device tracker entities)

**Sync Mobile Devices Frequently** (v1.5.0)
- **Unchecked** (default): Mobile devices sync every 6 hours (full sync only)
- **Checked**: Mobile devices sync every quick sync - useful for presence-based automations

**Enable Temperature Offset Attribute** (v1.5.0)
- **Unchecked** (default): No offset attribute on climate entities
- **Checked**: Adds `offset_celsius` attribute to climate entities (synced every 6 hours, uses 1 API call per device)

**Enable Test Mode (100 API call limit)** (v1.2.0)
- **Unchecked** (default): Normal operation with your actual API limit
- **Checked**: Simulates post-grace period with 100 call limit for testing

**API History Retention (days, 0=forever)** (v1.2.0)
- **Default**: 14 days
- **0**: Keep forever
- **Number**: Days to keep API call history

#### Polling Configuration (v1.2.0)

**Day Start Hour (0-23)**
- **Default**: 7am
- Defines when "day" period starts for smart polling

**Night Start Hour (0-23)**
- **Default**: 11pm
- Defines when "night" period starts for smart polling
- **Tip** (v1.4.0): Set Day = Night for uniform 24/7 polling

**Custom Day Polling Interval (minutes, optional)**
- **Empty**: Use smart polling based on API quota
- **Number (1-1440)**: Override smart polling with fixed interval for day period

**Custom Night Polling Interval (minutes, optional)**
- **Empty**: Use smart polling based on API quota
- **Number (1-1440)**: Override smart polling with fixed interval for night period

**Note**: When custom intervals are not set, Tado CE uses smart polling that automatically adjusts based on your API quota. See [Smart Polling](#smart-polling) section for details.

#### Hot Water Settings (v1.2.0)

**Hot Water Timer Duration (minutes)**
- **Default**: 60 minutes
- **Range**: 5-1440 minutes
- Duration used when HEAT mode is activated on hot water heater

**Note**: Configuration changes take effect immediately without requiring a Home Assistant restart.

---

### 4. Device Overview

Once set up, you'll see your Tado zones organized as separate devices with clean entity names.

---

## Entities

### API Sensors (Hub Entities)

| Entity | Description |
|--------|-------------|
| `sensor.tado_ce_api_usage` | API calls used today (e.g. "142/5000") |
| `sensor.tado_ce_api_reset` | Time until rate limit resets (e.g. "5h 30m") |

### Weather Sensors (Hub Entities)

| Entity | Description |
|--------|-------------|
| `sensor.tado_ce_outside_temperature` | Outside temperature at your location |
| `sensor.tado_ce_solar_intensity` | Solar intensity percentage |
| `sensor.tado_ce_weather_state` | Current weather condition |

### Per Heating Zone (Zone Entities - No Prefix)

| Entity | Description |
|--------|-------------|
| `climate.{zone}` | Climate entity for control |
| `sensor.{zone}_temperature` | Current temperature |
| `sensor.{zone}_humidity` | Current humidity |
| `sensor.{zone}_heating` | Heating power (0-100%) |
| `sensor.{zone}_target` | Target temperature |
| `sensor.{zone}_mode` | Mode (Manual/Schedule/Off) |
| `sensor.{zone}_battery` | Battery status (NORMAL/LOW) |
| `sensor.{zone}_connection` | Device connection (Online/Offline) |
| `binary_sensor.{zone}_open_window` | Open window detected |

**Note:** `{zone}` is your zone name in lowercase with spaces replaced by underscores. For example, "Living Room" becomes `living_room`.

### Switches (Zone & Hub Entities)

| Entity | Description | API Calls |
|--------|-------------|-----------|
| `switch.tado_ce_away_mode` | Toggle Home/Away mode manually (Hub) | 1 per toggle |
| `switch.{zone}_child_lock` | Enable/disable child lock (Zone) | 1 per toggle |
| `switch.{zone}_early_start` | Enable/disable smart pre-heating (Zone) | 1 per toggle |

### Other Entities

| Entity | Description |
|--------|-------------|
| `binary_sensor.tado_ce_home` | Home/Away status (geofencing) - Hub |
| `water_heater.{zone}` | Hot water control with AUTO/HEAT/OFF modes |
| `sensor.{zone}_boiler_flow_temperature` | Boiler flow temperature (hot water zones only) |
| `button.{zone}_timer_30min` | Quick 30-minute hot water timer |
| `button.{zone}_timer_60min` | Quick 60-minute hot water timer |
| `button.{zone}_timer_90min` | Quick 90-minute hot water timer |
| `button.tado_ce_resume_all_schedules` | Resume schedules for all zones (delete all overlays) - Hub |
| `device_tracker.tado_ce_{device}` | Mobile device presence - Hub |

The water heater entity supports three operation modes:

| Mode | Description | API Action |
|------|-------------|------------|
| **AUTO** | Follow Tado schedule | Deletes any overlay, returns to programmed schedule |
| **HEAT** | Turn on with timer | Creates timer overlay with configurable duration (default 60 min) |
| **OFF** | Turn off | Creates manual overlay with power OFF |

**Configuring Timer Duration:**

1. Go to Home Assistant Settings > Devices & Services > Tado CE
2. Click "Configure"
3. Set "Hot Water Timer Duration" (5-1440 minutes, default 60)
4. Next time you select HEAT mode, it will use the new duration

**When to use each mode:**

- **AUTO**: Most common - let Tado follow your programmed schedule
- **HEAT**: Temporary boost - heat water for a specific duration (e.g., unexpected guests)
- **OFF**: Turn off water heating (e.g., going on vacation)

---

## Services

| Service | Description |
|---------|-------------|
| `set_climate_timer` | Set heating/cooling with timer or until next schedule |
| `set_water_heater_timer` | Turn on hot water with timer |
| `resume_schedule` | Delete overlay, return to schedule |
| `set_climate_temperature_offset` | Calibrate device temperature (-10 to +10C) |
| `get_temperature_offset` | Fetch current offset (Tado CE exclusive) |
| `identify_device` | Flash device LED |
| `set_away_configuration` | Configure away temperature |
| `add_meter_reading` | Add Energy IQ reading (supports historical dates) |

All services available in **Developer Tools > Services** with full parameter documentation.

---

## Smart Polling

The integration automatically adjusts polling frequency based on your API limit and time of day.

### Polling Schedule

| API Limit | Day (7am-11pm) | Night (11pm-7am) | Est. Calls/Day |
|-----------|----------------|------------------|----------------|
| 100 | 30 min | 2 hours | ~80 calls |
| 1,000 | 15 min | 1 hour | ~160 calls |
| 5,000 | 10 min | 30 min | ~240 calls |
| 20,000 | 5 min | 15 min | ~480 calls |

### 100 Calls/Day Breakdown

| Time Period | Duration | Interval | Syncs | Calls | Total |
|-------------|----------|----------|-------|-------|-------|
| Day (7am-11pm) | 16h | 30 min | 32 | 2 | 64 |
| Night (11pm-7am) | 8h | 2h | 4 | 2 | 8 |
| Full sync | 24h | 6h | 4 | 2 | 8 |
| **Total** | | | | | **80** |

This leaves a 20% buffer for manual syncs or service calls.

---

## Supported Devices

| Device | Type | Support |
|--------|------|---------|
| Smart Thermostat V2 | HEATING | Full (community verified) |
| Smart Thermostat V3/V3+ | HEATING | Full |
| Smart Radiator Thermostat (SRT/VA02) | HEATING | Full |
| Smart AC Control V3/V3+ | AIR_CONDITIONING | Full |
| Wireless Temperature Sensor | HEATING | Full |
| Internet Bridge V3 | Infrastructure | N/A |
| **Tado X Series** | Matter/Thread | Not Supported |

### Tado X Devices

Tado X devices use Matter over Thread and are **not supported** by this integration. Use the Home Assistant Matter integration instead.

See [Using tado Smart Thermostat X through Matter](https://community.home-assistant.io/t/using-tado-smart-thermostat-x-through-matter/736576) for setup instructions.

---

## Limitations

| Limitation | Description |
|------------|-------------|
| **Cloud-Only** | All control goes through Tado's cloud servers |
| **No GPS** | Device trackers only show home/not_home status |
| **Rotating Tokens** | If token expires, re-authentication required |
| **No Schedule Management** | Use Tado app for schedule changes |
| **No Historical Data** | Would consume too many API calls |

---

## Future Features

See [ROADMAP.md](ROADMAP.md) for planned features, API limitations, and ideas. Want a feature? [Open an issue](https://github.com/hiall-fyi/tado_ce/issues) or submit a PR!

---

## Troubleshooting

### Token refresh failed / Re-authentication required

1. Go to **Settings > Devices & Services > Tado CE**
2. Click **Configure** or look for re-authentication prompt
3. Follow the device authorization flow (link + code)

### Check status

Check your Home Assistant logs: **Settings > System > Logs**

Filter by "tado_ce" to see integration-specific messages.

### No device tracker entities

Device trackers only appear for mobile devices with geo tracking enabled in the Tado app.

### Enable debug logging

For detailed troubleshooting, enable debug logging in your `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.tado_ce: debug
```

This will show detailed information about:
- Token refresh operations and caching
- API call tracking with timestamps
- Rate limit header parsing
- Polling interval calculations
- Quota availability checks
- Configuration updates

After adding this, restart Home Assistant and check **Settings > System > Logs** for detailed debug information.

---

## Comparison with Official Integration

### Shared Features (Both Support)

Climate Control, AC Control, Temperature/Humidity Sensors, Open Window Detection, Home/Away (Geofencing), Presence Detection, Weather Data, Child Lock Switch, Timer Overlay, Temperature Offset, Energy IQ, Device Identify, Away Configuration, Connection State

### Tado CE Enhancements

| Feature | Official | Tado CE |
|---------|:--------:|:-------:|
| Hot Water Control | Basic | Enhanced |
| Hot Water Timer Presets | No | Yes |
| Boiler Flow Temperature | No | Auto-detect |
| Early Start Switch | No | Yes |
| Energy IQ Historical Dates | No | Yes |
| Zone-Based Devices | No | Yes |
| Optional Weather/Mobile | No | Yes |

### Tado CE Exclusive

- Real API Rate Limit Tracking (from Tado headers)
- Dynamic Limit Detection (100/5000/20000)
- Reset Time from API Headers
- Smart Day/Night Polling
- Multi-Home Selection
- Customizable Polling Intervals
- API Call History & Retention
- Immediate Dashboard Refresh
- Test Mode (100 call limit)

## Resources

- [Tado API Rate Limit Announcement](https://community.home-assistant.io/t/tado-rate-limiting-api-calls/928751)
- [Official Tado Integration](https://www.home-assistant.io/integrations/tado/)
- [Tado API Documentation (Community)](https://github.com/kritsel/tado-openapispec-v2)
- [Roadmap & Feature Requests](ROADMAP.md)
- [Complete Entities Reference](ENTITIES.md)
- [Release Credits & Community Contributors](RELEASE_CREDITS.md)

---

## Support

For issues and questions:

1. Check the [Troubleshooting](#troubleshooting) section
2. Check logs: **Settings > System > Logs**
3. [Open an issue on GitHub](https://github.com/hiall-fyi/tado_ce/issues)

---

## License

This project is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)**.

### What this means:

- **Free to use** - Use this integration for personal or commercial purposes
- **Free to modify** - Adapt the code to your needs
- **Free to distribute** - Share with others

### Requirements:

- **Open Source** - Any modifications or derivative works must also be open source under AGPL-3.0
- **Attribution** - You must give appropriate credit to the original author
- **Source Available** - If you run a modified version as a network service, you must make the source code available

### Credits

**Original Author:** Joe Yiu ([@hiall-fyi](https://github.com/hiall-fyi))

If you use or modify this code, please include attribution:
```
Based on Tado CE by Joe Yiu (@hiall-fyi)
https://github.com/hiall-fyi/tado_ce
```

See [LICENSE](LICENSE) file for full details.

---

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

By contributing, you agree that your contributions will be licensed under the AGPL-3.0 license.

Check the [Roadmap](ROADMAP.md) for planned features and ideas.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

---

## Star History

If you find this integration useful, please consider giving it a star!

[![Star History Chart](https://api.star-history.com/svg?repos=hiall-fyi/tado_ce&type=Date)](https://star-history.com/#hiall-fyi/tado_ce&Date)

---

<div align="center">

### Support This Project

If this integration has saved you from rate limit headaches, made your Tado setup better, or simply made your life easier, consider supporting the project!

**Your support helps me:**

<p align="center">
Maintain and improve this integration<br>
Fix bugs and add new features<br>
Create better documentation<br>
Stay caffeinated while coding!
</p>

[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-Support-FFDD00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/hiallfyi)

*Every coffee makes a difference! Thank you to all supporters!*

**Made with love by Joe Yiu ([@hiall-fyi](https://github.com/hiall-fyi))**

</div>

---

**Version**: 1.7.0  
**Last Updated**: 2026-01-26  
**Tested On**: Home Assistant 2026.1.3 (HAOS, Docker, Core)

---

## Disclaimer

This project is not affiliated with, endorsed by, or connected to tado GmbH or Home Assistant.

- **tado** and the tado logo are registered trademarks of tado GmbH.
- **Home Assistant** is a trademark of Nabu Casa, Inc.
- All product names, logos, and brands are property of their respective owners.

This integration is provided "as is" without warranty of any kind. Use at your own risk. The authors are not responsible for any damages or issues arising from the use of this software, including but not limited to API rate limit violations, account restrictions, or data loss.

This is an independent, community-developed project created to help Home Assistant users better manage their Tado API usage.
