"""Tado CE constants: domain, defaults, window U-values, overlay modes."""

from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path
from typing import Any, Final

DOMAIN = "tado_ce"
MANUFACTURER = "Joe Yiu (@hiall-fyi)"

# Dispatcher signal for HomeKit real-time entity updates
SIGNAL_HOMEKIT_UPDATE = "tado_ce_homekit_update_{home_id}"

# Bus event fired after first successful API sync and entity population
EVENT_READY: Final[str] = "tado_ce_ready"

# Tado bridge device models (Internet Bridge hardware)
# Used for pre-registering bridge devices in the device registry before platform setup
TADO_BRIDGE_MODELS = ["IB01", "IB02"]

# Bridge API independent poll interval (seconds).
# Bridge API uses its own auth key and does NOT count toward the Tado cloud API
# quota, so it can poll more frequently than the main coordinator cycle.
BRIDGE_POLL_INTERVAL_SECONDS: Final[float] = 60.0

# Data directory (persistent storage)
# Stored in .storage/tado_ce/ to prevent HACS upgrades from overwriting data files
# Use environment variable if set (for testing), otherwise use standard HA path
_BASE_CONFIG_DIR = os.environ.get("TADO_CE_CONFIG_DIR", "/config")
DATA_DIR = Path(_BASE_CONFIG_DIR) / ".storage" / "tado_ce"

# Service names
SERVICE_SET_CLIMATE_TIMER = "set_climate_timer"
SERVICE_SET_CLIMATE_SCHEDULE_TEMPERATURE = "set_climate_schedule_temperature"
SERVICE_SET_WATER_HEATER_TIMER = "set_water_heater_timer"
SERVICE_RESUME_CLIMATE_SCHEDULE = "resume_climate_schedule"
SERVICE_RESUME_WATER_HEATER_SCHEDULE = "resume_water_heater_schedule"
SERVICE_SET_CLIMATE_ACTIVE_TIMETABLE = "set_climate_active_timetable"
SERVICE_SET_TEMP_OFFSET = "set_climate_temperature_offset"  # Match official Tado integration
SERVICE_GET_CLIMATE_TEMPERATURE_OFFSET = "get_climate_temperature_offset"  # On-demand offset fetch
SERVICE_GET_FLOW_TEMPERATURE_SETTINGS = "get_flow_temperature_settings"
SERVICE_ADD_METER_READING = "add_meter_reading"
SERVICE_IDENTIFY_DEVICE = "identify_device"
SERVICE_SET_CLIMATE_AWAY_CONFIGURATION = "set_climate_away_configuration"
SERVICE_ACTIVATE_CLIMATE_OPEN_WINDOW = "activate_climate_open_window"
SERVICE_DEACTIVATE_CLIMATE_OPEN_WINDOW = "deactivate_climate_open_window"
SERVICE_SET_CLIMATE_OPEN_WINDOW_MODE = "set_climate_open_window_mode"
SERVICE_RESTORE_CLIMATE_PREVIOUS_STATE = "restore_climate_previous_state"
SERVICE_RESTORE_WATER_HEATER_PREVIOUS_STATE = "restore_water_heater_previous_state"
SERVICE_TURN_OFF_ALL_CLIMATE_ZONES = "turn_off_all_climate_zones"
SERVICE_REFRESH = "refresh"

# Every registered service, in registration order; unload removes exactly these.
SERVICE_NAMES: Final[tuple[str, ...]] = (
    SERVICE_SET_CLIMATE_TIMER,
    SERVICE_SET_CLIMATE_SCHEDULE_TEMPERATURE,
    SERVICE_SET_WATER_HEATER_TIMER,
    SERVICE_RESUME_CLIMATE_SCHEDULE,
    SERVICE_RESUME_WATER_HEATER_SCHEDULE,
    SERVICE_SET_CLIMATE_ACTIVE_TIMETABLE,
    SERVICE_TURN_OFF_ALL_CLIMATE_ZONES,
    SERVICE_SET_TEMP_OFFSET,
    SERVICE_ADD_METER_READING,
    SERVICE_REFRESH,
    SERVICE_IDENTIFY_DEVICE,
    SERVICE_SET_CLIMATE_AWAY_CONFIGURATION,
    SERVICE_ACTIVATE_CLIMATE_OPEN_WINDOW,
    SERVICE_DEACTIVATE_CLIMATE_OPEN_WINDOW,
    SERVICE_SET_CLIMATE_OPEN_WINDOW_MODE,
    SERVICE_GET_CLIMATE_TEMPERATURE_OFFSET,
    SERVICE_RESTORE_CLIMATE_PREVIOUS_STATE,
    SERVICE_RESTORE_WATER_HEATER_PREVIOUS_STATE,
    SERVICE_GET_FLOW_TEMPERATURE_SETTINGS,
)

# Fetch types forceable via tado_ce.refresh (single source: gate, service enum,
# and request_forced_fetch all use these tokens).
FORCEABLE_FETCH_MOBILE = "mobile_devices"
FORCEABLE_FETCH_HOME_STATE = "home_state"
FORCEABLE_FETCH_WEATHER = "weather"
FORCEABLE_FETCH_ZONE_STATES = "zone_states"

# API Base URLs
TADO_API_BASE = "https://my.tado.com/api/v2"
TADO_GRAPHQL_API = "https://ext.api.tado.com/apps/graphql"
TADO_AUTH_URL = "https://login.tado.com/oauth2"
CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"

# API Endpoints (relative to TADO_API_BASE)
API_ENDPOINT_ME = f"{TADO_API_BASE}/me"
API_ENDPOINT_DEVICES = f"{TADO_API_BASE}/devices"  # + /{serial}

# Auth Endpoints
AUTH_ENDPOINT_DEVICE = f"{TADO_AUTH_URL}/device_authorize"
AUTH_ENDPOINT_TOKEN = f"{TADO_AUTH_URL}/token"

# Adaptive Smart Polling Constants
# MIN_POLLING_INTERVAL is the adaptive floor: the fastest cadence the adaptive
# math will ever pick on its own. It is deliberately tier-agnostic: zone
# temperature does not change any faster on a bigger quota, so a high daily
# limit does not buy a faster automatic cadence. Anyone who wants faster sets
# a custom polling interval explicitly; the adaptive math still backs off above
# this floor whenever the budget can't sustain it.
MIN_POLLING_INTERVAL = 5  # minutes (uniform adaptive floor, see note above)
DEFAULT_DAY_INTERVAL = 30  # minutes (default day polling interval)
DEFAULT_NIGHT_INTERVAL = 120  # minutes (default night polling interval)
RESET_CLAMP_SLACK_MINUTES = 2  # minutes past the predicted quota reset that a poll may land
LATE_RESET_FLOOR_MINUTES = 15  # minutes, longest interval while a predicted reset has not arrived
LATE_RESET_WINDOW_HOURS = 2  # hours after the predicted reset during which the late-reset floor applies
SMART_VALVE_NIGHT_FLOOR_MINUTES = 30  # minutes, longest night cloud interval for entries with valve-target zones
MAX_POLLING_INTERVAL = 120  # minutes (ensure reasonable updates even with low quota)
MAX_CUSTOM_INTERVAL = 1440  # minutes (24 hours, maximum custom polling interval)
POLLING_SAFETY_BUFFER = 0.90  # Reserve 10% quota for manual calls and unexpected usage
# A rise this close to the predicted reset counts as the reset: the recorded time is when the sync saw it, up to one poll gap late.
# This is the floor; the window widens to the lag stored with the last recorded reset.
QUOTA_RESET_EARLY_WINDOW_MINUTES: Final = MAX_POLLING_INTERVAL

# Quota Reserve Protection Constants
# When remaining quota falls below threshold, pause polling to reserve for manual operations
QUOTA_RESERVE_CALLS = 5  # Minimum reserved calls (absolute floor) - pause polling
QUOTA_RESERVE_PERCENT = 0.05  # Reserve 5% of daily limit (whichever is larger)

# Bootstrap Reserve - absolute minimum calls that are NEVER used
# These are reserved for auto-recovery after API reset (detecting reset, initial sync)
# Even manual actions are blocked when remaining <= QUOTA_BOOTSTRAP_CALLS
QUOTA_BOOTSTRAP_CALLS = 3  # Hard limit - never use these calls

# Low Quota Threshold for Smart Day/Night, scales with the daily limit
# Users with remaining <= threshold get Smart Day/Night handling. The threshold
# is a proportion of the daily limit the API reports (max of an absolute floor
# and a percentage), so it tracks whatever quota Tado grants without hardcoding
# any tier breakpoint. A 100-call budget stays at the 100-call floor; a
# 1,000-call budget fires at 100 (10% = floor); a 20,000-call budget fires at
# 2,000 (10% > floor, proportional cushion). Read as QuotaLevels.soft_low.
LOW_QUOTA_RESERVE_PERCENT = 0.10  # 10% of daily limit
LOW_QUOTA_RESERVE_FLOOR = 100      # Floor (one full day's worth on a 100-call budget)

# Day-type mapping, shared by calendar.py's setup-time fetch and
# api_client.py's SVC-driven full-sync fetch, so both read one definition.
DAY_TYPES: Final[dict[str, list[str]]] = {
    "ONE_DAY": ["MONDAY_TO_SUNDAY"],
    "THREE_DAY": ["MONDAY_TO_FRIDAY", "SATURDAY", "SUNDAY"],
    "SEVEN_DAY": ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"],
}

# Tado's id for each timetable type, as sent to PUT activeTimetable.
ACTIVE_TIMETABLE_IDS: Final[dict[str, int]] = {"ONE_DAY": 0, "THREE_DAY": 1, "SEVEN_DAY": 2}

# Worst-case cost of fetching one zone's schedule: the activeTimetable call
# plus one blocks/{day_type} call per day type in the largest timetable.
MAX_SCHEDULE_FETCH_CALLS: Final[int] = 1 + max(len(days) for days in DAY_TYPES.values())

# Schedule Calendar's cache fill. One zone costs 1 + len(day_types) calls (8
# for SEVEN_DAY), so a 9-zone home is 72, most of a free-tier day. Zones the
# budget doesn't reach fill on a later setup run.
SCHEDULE_FILL_MAX_QUOTA_FRACTION = 0.25
# Assumed daily limit before the first rate-limit header lands.
ASSUMED_DAILY_LIMIT_UNKNOWN_TIER = 100

# Per-data-type minimum refresh intervals (minutes). Slow-changing cloud data
# gates on its own floor independent of the zone-state cycle, so a fast zone
# cadence doesn't drag weather/presence along. DEFAULT_* values are the
# fallback when the user has not set a custom interval; MIN_*/MAX_* define
# the configurable range exposed in Advanced Settings → Polling & API.
DEFAULT_WEATHER_MIN_REFRESH_MINUTES: Final[int] = 30
MIN_WEATHER_MIN_REFRESH_MINUTES: Final[int] = 15
MAX_WEATHER_MIN_REFRESH_MINUTES: Final[int] = 120

DEFAULT_PRESENCE_MIN_REFRESH_MINUTES: Final[int] = 5
MIN_PRESENCE_MIN_REFRESH_MINUTES: Final[int] = 1
MAX_PRESENCE_MIN_REFRESH_MINUTES: Final[int] = 60

DEFAULT_MOBILE_DEVICES_MIN_REFRESH_MINUTES: Final[int] = 5
MIN_MOBILE_DEVICES_MIN_REFRESH_MINUTES: Final[int] = 1
MAX_MOBILE_DEVICES_MIN_REFRESH_MINUTES: Final[int] = 60

# Mechanism counter ids (Stage 0 measurement); counts under any other id are dropped.
MECH_POLL_SKIP: Final[str] = "poll_skip"
MECH_POLL_PAUSE: Final[str] = "poll_pause"
MECH_HK_PUT_AGREE: Final[str] = "homekit_put_agree"
MECH_HK_PUT_70402: Final[str] = "homekit_put_70402"
MECH_PREHEAT_PUT: Final[str] = "preheat_put"
MECH_SVC_CLOUD_WRITE: Final[str] = "svc_cloud_write"
MECH_BRIDGE_API_CALL: Final[str] = "bridge_api_call"
MECH_REFRESH_PROBE: Final[str] = "refresh_probe"
MECHANISM_IDS: Final[frozenset[str]] = frozenset(
    {
        MECH_POLL_SKIP,
        MECH_POLL_PAUSE,
        MECH_HK_PUT_AGREE,
        MECH_HK_PUT_70402,
        MECH_PREHEAT_PUT,
        MECH_SVC_CLOUD_WRITE,
        MECH_BRIDGE_API_CALL,
        MECH_REFRESH_PROBE,
    }
)
# Old id -> current id; load() renames through it so a later rename keeps stored counts.
MECHANISM_ID_MIGRATION: Final[dict[str, str]] = {}
# Upper edges (minutes) of the data-age gauge buckets; anything above the last is gt_<last>.
FRESHNESS_BUCKET_MINUTES: Final[tuple[int, ...]] = (5, 15, 30, 60, 120, 240)
# Upper edges (seconds) for the put-to-agreeing-fetch histogram.
PUT_AGREE_BUCKET_SECONDS: Final[tuple[int, ...]] = (20, 40, 60, 90, 120)
# Outcomes: le_<edge>/gt_<last>, unagreed (no agreeing fetch within this many seconds, then dropped), superseded (replaced by a newer put).
HOMEKIT_PUT_PENDING_MAX_SECONDS: Final[float] = 900.0

# Canonical window type to U-value mapping (W/m²K). Single source of truth for all
# mold risk calculations.
WINDOW_U_VALUES = {
    "single_pane": 5.0,  # Single glazing (old buildings)
    "double_pane": 2.7,  # Double glazing (most common, default)
    "triple_pane": 1.0,  # Triple glazing (modern buildings)
    "passive_house": 0.8,  # Passive house standard (high performance)
}
DEFAULT_WINDOW_TYPE = "double_pane"
INTERIOR_SURFACE_HEAT_TRANSFER_COEFFICIENT = 8.0  # W/m²K (standard value for indoor surfaces)


# =============================================================================
# Per-Zone Configuration Constants
# =============================================================================

# Overlay mode values (UPPERCASE - matches Tado API)
OVERLAY_MODE_TADO_MODE = "TADO_MODE"
OVERLAY_MODE_TIMER = "TIMER"
OVERLAY_MODE_MANUAL = "MANUAL"

# Overlay mode default: MANUAL ("Until you resume schedule"). Matches what a
# HomeKit local write produces, and what users mean when setting a zone from HA.
OVERLAY_MODE_DEFAULT = OVERLAY_MODE_MANUAL

# Overlay mode option keys (lowercase of the stored API values), used by the hub select and the zone form.
OVERLAY_MODE_KEYS = ("manual", "tado_mode", "timer")

# Overlay mode display names: verbatim Tado app wording, used to format the overlay_type attribute.
OVERLAY_MODE_MAP = {
    "Until you resume schedule": OVERLAY_MODE_MANUAL,
    "Until next automatic change": OVERLAY_MODE_TADO_MODE,
    "Timer": OVERLAY_MODE_TIMER,
}
OVERLAY_MODE_REVERSE_MAP = {v: k for k, v in OVERLAY_MODE_MAP.items()}

# Per-zone write route (Heating only): "auto" lets MANUAL writes go over HomeKit, "cloud" always uses the API.
WRITE_ROUTE_AUTO = "auto"
WRITE_ROUTE_CLOUD = "cloud"

# Timer duration default
TIMER_DURATION_DEFAULT = 60

# Default per-zone configuration values
DEFAULT_ZONE_CONFIG = {
    "heating_type": "radiator",  # radiator or ufh (Heating only)
    "ufh_buffer_minutes": 30,  # 0-60 minutes (Heating only, when UFH)
    "adaptive_preheat": "off",  # off / active / passive (Heating + AC)
    "window_type": "double_pane",  # single_pane/double_pane/triple_pane/passive_house (Heating + AC)
    "external_temp_sensor": "",  # HA entity_id for external temperature sensor (Heating + AC)
    "external_humidity_sensor": "",  # HA entity_id for external humidity sensor (Heating + AC)
    "overlay_mode": OVERLAY_MODE_DEFAULT,
    "write_route": WRITE_ROUTE_AUTO,  # auto / cloud (Heating only)
    "display_temp_source": "auto",  # auto / homekit / cloud (Heating + AC)
    "timer_duration": TIMER_DURATION_DEFAULT,  # 1-720 minutes (Heating + AC, when Timer)
    "min_temp": 5.0,  # 5-25°C (Heating + AC)
    "max_temp": 25.0,  # 15-30°C (Heating + AC)
    "surface_temp_offset": 0.0,  # -5.0 to +5.0°C offset for mold risk calculation
    "svc_mode": "off",  # off / valve_target / offset_sync (Heating only)
    "svc_offset_min_change": 0.5,  # 0.5-3.0°C, minimum offset change before writing (Offset Sync)
}

# Plausible Celsius range for an external temperature reading; anything outside is a bad sensor
EXTERNAL_TEMPERATURE_MIN_C: Final = -60.0
EXTERNAL_TEMPERATURE_MAX_C: Final = 130.0

# Surface temperature offset limits (for mold risk calibration)
SURFACE_TEMP_OFFSET_MIN = -5.0
SURFACE_TEMP_OFFSET_MAX = 5.0
SURFACE_TEMP_OFFSET_STEP = 0.1

# Per-zone temperature limits (user-configurable min/max setpoint bounds)
ZONE_TEMP_MIN_FLOOR = 5.0    # Absolute minimum, frost protection
ZONE_TEMP_MAX_CEILING = 30.0  # Absolute maximum, Tado hardware limit

# Heating type values
HEATING_TYPE_RADIATOR = "radiator"

# Open window mode defaults
OPEN_WINDOW_DEFAULT_TEMP = 5.0  # Frost protection temperature (°C)
OPEN_WINDOW_DEFAULT_TIMEOUT = 900  # 15 minutes in seconds (Tado default)

# Timer duration limits
TIMER_DURATION_MIN = 1
TIMER_DURATION_MAX = 720  # matches the Tado app's 12-hour cap

# Timer duration options (per-zone select), matches Tado app's 12-hour cap
TIMER_DURATION_OPTIONS = [
    "15", "30", "45", "60", "90", "120", "180",
    "240", "360", "480", "600", "720",
]

# Window type options (for per-zone select)
WINDOW_TYPE_MAP = {
    "Single Pane": "single_pane",
    "Double Pane": "double_pane",
    "Triple Pane": "triple_pane",
    "Passive House": "passive_house",
}
WINDOW_TYPE_REVERSE_MAP = {v: k for k, v in WINDOW_TYPE_MAP.items()}

# Device offset sanity bounds, reject values outside this range.
# Tado's API rejects exactly -10.0 (real floor is -9.99); -9.9 keeps clear of
# it after this integration's 1-decimal rounding.
DEVICE_OFFSET_MIN: float = -9.9
DEVICE_OFFSET_MAX: float = 10.0


# Insight runtime state (coordinator anomaly + humidity history persistence)
INSIGHT_RUNTIME_STATE_KEY = "insight_runtime_state"

# Zone identity baseline (per-zone dateCreated, for zone-reassignment detection)
ZONE_IDENTITY_KEY = "zone_identity"

# Zone type baseline (per-zone type, for HEATING-to-AC transition detection)
ZONE_TYPE_BASELINE_KEY = "zone_type_baseline"


def is_valid_device_offset(value: float | None) -> bool:
    """Return True if value is a finite number within the valid offset range.

    Consolidates the DEVICE_OFFSET_MIN <= x <= DEVICE_OFFSET_MAX check that
    was duplicated at write, read, sync, and readback sites.
    """
    if value is None:
        return False
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return DEVICE_OFFSET_MIN <= f <= DEVICE_OFFSET_MAX

# =============================================================================
# Smart Valve Control Constants
# =============================================================================

SMART_VALVE_HYSTERESIS: Final[float] = 0.3       # °C dead zone around target
SMART_VALVE_MIN_CHANGE: Final[float] = 0.5       # °C minimum write threshold
SMART_VALVE_CLOUD_RATE_LIMIT: Final[float] = 300.0  # seconds (5 minutes)
SMART_VALVE_DEBOUNCE_WINDOW: Final[float] = 3.0  # seconds (ActionDebouncer window)
ABSOLUTE_MAX_VALVE_TARGET: Final[float] = 30.0   # °C absolute upper bound for valve target
HOMEKIT_WRITE_GRACE_SECONDS: Final[float] = 60.0  # suppress manual override detection after write
SVC_RESUME_RETRY_SECONDS: Final[float] = 300.0  # minimum gap between retries of a failed hand-back to Tado
SVC_RESUME_RETRY_MAX_SECONDS: Final[float] = 21600.0  # cap on the doubling retry gap (6 hours)

# SVC operating mode (per-zone select, mutually exclusive)
SVC_MODE_VALVE_TARGET: Final[str] = "valve_target"
SVC_MODE_OFFSET_SYNC: Final[str] = "offset_sync"

# Offset Sync: minimum offset change before writing to device
SVC_OFFSET_MIN_CHANGE: Final[float] = 0.5  # °C (default)
SVC_OFFSET_MIN_CHANGE_MIN: Final[float] = 0.5  # °C, lower bound for config
SVC_OFFSET_MIN_CHANGE_MAX: Final[float] = 3.0  # °C, upper bound for config

# Offset Sync: maximum offset movement allowed in a single write, since
# inside_temperature can be momentarily unreliable while a zone is off.
SVC_OFFSET_MAX_STEP: Final[float] = 1.0  # °C

# Offset Sync: readings within this of each other count as unchanged (API
# precision is 0.1°C).
SVC_OFFSET_TRV_EPSILON: Final[float] = 0.05  # °C

# =============================================================================
# API Write Optimization Constants
# =============================================================================

# Smart Actions: zone-level debounce
SMART_ACTIONS_DEBOUNCE_DEFAULT = 3  # seconds
SMART_ACTIONS_DEBOUNCE_MIN = 0  # 0 = disabled
SMART_ACTIONS_DEBOUNCE_MAX = 10  # seconds

# Device Sync: sequential device operations
DEVICE_SYNC_DELAY_DEFAULT = 1.0  # seconds
DEVICE_SYNC_DELAY_MIN = 0.5  # seconds
DEVICE_SYNC_DELAY_MAX = 5.0  # seconds
DEVICE_SYNC_QUEUE_MAX_DEPTH = 20

# =============================================================================
# Retry / Transient Error Constants
# =============================================================================

MAX_RETRY_ATTEMPTS: Final = 3
# Empty zone replies needed before an empty list replaces stored zones (2 is a cautious guess, no captured blip confirms it).
ZONE_STATES_EMPTY_CONFIRMATIONS: Final = 2
RETRY_BASE_DELAY: Final = 2  # seconds, exponential: 2s, 4s, 8s
MAX_RETRY_DELAY: Final = 30  # seconds, cap to prevent runaway delays

# =============================================================================
# Rate Limit / Quota Constants
# =============================================================================

# Quota warning threshold: log warning when usage exceeds this percentage
QUOTA_WARNING_PERCENTAGE: Final[int] = 80

# =============================================================================
# Rate Limit Retry-After Constants (UpdateFailed(retry_after=N))
# =============================================================================

_RATE_LIMIT_MIN_S: Final = 10      # minimum wait (seconds)
_RATE_LIMIT_MAX_S: Final = 300     # maximum wait (5 minutes)
_RATE_LIMIT_DEFAULT_S: Final = 60  # default when no signal available

# =============================================================================
# HomeKit Timing Constants
# =============================================================================

# HomeKit cache older than this falls back to cloud
HOMEKIT_STALENESS_THRESHOLD: Final[timedelta] = timedelta(minutes=5)

# Hide a device-fed cloud reading once its device has been offline this long; healthy readings lag up to ~65 min and HomeKit cloud sync adds up to 120 min, so less would hide live ones.
CLOUD_READING_STALE_AFTER: Final[timedelta] = timedelta(hours=4)
# A device that came back reports a fresh reading, so one stamped later than last-seen plus this is trusted; 2 h rests on a single observed sample and is not yet verified.
CLOUD_READING_REPORT_SLACK: Final[timedelta] = timedelta(hours=2)

# Periodic poll to keep cache fresh, must be < staleness threshold
HOMEKIT_CACHE_REFRESH_SECONDS: Final[float] = HOMEKIT_STALENESS_THRESHOLD.total_seconds() * 0.4  # 120s (2 min)

# Cloud sync interval when HomeKit is connected (user-configurable)
DEFAULT_HOMEKIT_CLOUD_SYNC_MINUTES: Final[int] = 30
MIN_HOMEKIT_CLOUD_SYNC_MINUTES: Final[int] = 5
MAX_HOMEKIT_CLOUD_SYNC_MINUTES: Final[int] = 120

HOMEKIT_DROP_FETCH_MIN_GAP_SECONDS: Final[float] = 600.0

# One catch-up cloud fetch this long after a start on the saved snapshot.
COLD_START_RETRY_SECONDS: Final[float] = 60.0
# A refused catch-up tries again after 2x then 4x the first delay, three attempts in total.
COLD_START_RETRY_MAX_ATTEMPTS: Final[int] = 3

# HomeKit write timeout, fallback to cloud if local write exceeds this
HOMEKIT_WRITE_TIMEOUT_SECONDS: Final[float] = 3.0

# Puts each within this many seconds of the previous one form one burst
HOMEKIT_BURST_GAP_SECONDS: Final[float] = 5.0

# Cloud deviceType of the radiator valves (VA02) and wall thermostat (RU02) whose HomeKit target heating state means power; any other device in a zone keeps the cloud power
HOMEKIT_POWER_DEVICE_TYPES: Final = frozenset({"VA02", "RU02"})

# How long the Tado cloud takes to show a HomeKit put, before its poll can be trusted to confirm or refute it
HOMEKIT_CLOUD_LAG_SECONDS: Final[float] = 90.0

# Whether a HomeKit put of each kind schedules a verification fetch; setting a kind to False skips that fetch and leaves the regular poll to verify it
VERIFY_FETCH_BY_KIND: Final[dict[str, bool]] = {
    "temperature": True,
    "power_on": True,
    "power_off": True,
    "timer": True,
    "mode": True,
    "fan": True,
    "swing": True,
    "operation": True,
}

# True asks for one spaced zone fetch when a valve zone's target or power changes over HomeKit and the change is not ours; False leaves it to the regular poll
DEMAND_FETCH_ON_FOREIGN_CHANGE: Final = False

# Extra seconds after a HomeKit-originated OFF before a zone fetch may release the app-off hold; 0.0 keeps today's behaviour until the cloud lag is measured
HOMEKIT_OFF_RELEASE_LAG_SECONDS: Final[float] = 0.0


def homekit_change_is_newer_than_fetch(
    changed_at: datetime | None,
    observed_at: datetime | None,
    fetch_epoch: datetime | None,
    lag_seconds: float = 0.0,
) -> bool:
    """Return True when a HomeKit change is newer than the published cloud fetch, less the lag the cloud needs to show it."""
    # No fetch published: any reading counts. A poll-seeded entry has no change time, so with a fetch it never counts.
    if fetch_epoch is None:
        return observed_at is not None
    return changed_at is not None and changed_at > fetch_epoch - timedelta(seconds=lag_seconds)

# After the lag, how long an unconfirmed HomeKit put may still be resent over the cloud
HOMEKIT_EXPIRE_WINDOW_SECONDS: Final[float] = 120.0

# Longest a zone fetch that follows a chained write is expected to run
HOMEKIT_CHAINED_FETCH_MAX_SECONDS: Final[float] = 110.0

# Write-side circuit breaker, skip HomeKit writes after consecutive failures
WRITE_FAILURE_THRESHOLD: Final[int] = 3
WRITE_CIRCUIT_OPEN_SECONDS: Final[float] = 300.0  # 5 minutes cooldown

# Cache refresh failure threshold, trigger reconnect after consecutive failures
CACHE_REFRESH_FAILURE_THRESHOLD: Final[int] = 3
# A read loop that keeps failing backs its reconnects off to one per this many seconds
HOMEKIT_LOOP_RECONNECT_MIN_GAP_SECONDS: Final[float] = 3600.0

# HomeKit mapping retry: stop retrying after this many polls with no newly
# mapped zone (the remaining zones have no matching bridge accessory)
MAPPING_RETRY_STALL_LIMIT: Final[int] = 3

# Periodic device-offset resync interval. Tado's own calibration or the
# user's app can change an offset with no HA write, and the per-write
# readback gate only proves our writes landed, not later server-side
# drift, so re-fetch every offset at least this often.
OFFSET_DRIFT_REFRESH_SECONDS: Final[int] = 30 * 60

# Without a periodic refresh, `connectionState` / `currentFwVersion` /
# `batteryState` freeze at boot, they only land in zone-info, not in
# per-cycle zone state. Free-tier interval is longer so the refresh
# stays under ~6% of daily quota.
ZONES_INFO_REFRESH_SECONDS_PAID: Final[int] = 60 * 60
ZONES_INFO_REFRESH_SECONDS_FREE: Final[int] = 4 * 60 * 60
# Free-tier detection sentinel: the 100-call free tier reports limit=100, so
# `limit <= 200` reliably distinguishes it from the 1,000-call transitional
# and 20,000-call legacy tiers without false-positives. 200 is padding so a
# slight Tado-side adjustment to the free-tier limit (say 150) wouldn't break
# detection.
ZONES_INFO_FREE_TIER_THRESHOLD: Final[int] = 200

# =============================================================================
# Climate Zone Type Helper
# =============================================================================

CLIMATE_ZONE_TYPES: Final[frozenset[str]] = frozenset({"HEATING", "AIR_CONDITIONING"})

CAPABILITY_ZONE_TYPES: Final[frozenset[str]] = frozenset(
    {"AIR_CONDITIONING", "HOT_WATER"},
)

# Outdoor temperature history: 14 days × 24 hourly readings
OUTDOOR_TEMP_HISTORY_MAX: Final = 336

# Entity freshness expiry, stale entries cleaned up after this many seconds
ENTITY_FRESHNESS_EXPIRY_SECONDS: Final[int] = 60

# Buffer added to debounce delay for optimistic window calculation
OPTIMISTIC_WINDOW_BUFFER_SECONDS: Final[float] = 2.0

# Default optimistic window when hass is unavailable (seconds)
# = DEFAULT_REFRESH_DEBOUNCE_SECONDS (15) + OPTIMISTIC_WINDOW_BUFFER_SECONDS (2)
DEFAULT_OPTIMISTIC_WINDOW_SECONDS: Final[float] = 17.0

# Seconds per day, used for duration formatting
SECONDS_PER_DAY: Final[int] = 86400

# Insight escalation: days before an insight is considered long-standing
INSIGHT_ESCALATION_DAYS: Final[int] = 14

# Comfort deviation thresholds (°C from comfort target)
_COMFORT_FREEZING_THRESHOLD: Final[int] = -6
_COMFORT_COLD_THRESHOLD: Final[int] = -4
_COMFORT_COOL_THRESHOLD: Final[int] = -2
_COMFORT_WARM_THRESHOLD: Final[int] = 2
_COMFORT_HOT_THRESHOLD: Final[int] = 4
_COMFORT_SWELTERING_THRESHOLD: Final[int] = 6

# Humidity thresholds for comfort suffix
_HUMIDITY_DRY_THRESHOLD: Final[int] = 35  # %: below this is "Dry"
_HUMIDITY_HUMID_THRESHOLD: Final[int] = 70  # %: above this is "Humid"

# =============================================================================
# Entity Data Keys: cross-component data sharing via coordinator.entity_data
# =============================================================================

ENTITY_DATA_CONDENSATION_RISK: Final[str] = "condensation_risk"
ENTITY_DATA_PREHEAT_NOW: Final[str] = "preheat_now"
ENTITY_DATA_PREHEAT_ADVISOR: Final[str] = "preheat_advisor"


def is_climate_zone(zone_type: str) -> bool:
    """Return True if zone_type is a climate-controlled zone (heating or AC)."""
    return zone_type in CLIMATE_ZONE_TYPES


def get_climate_zone_ids(zones_info: list[dict[str, Any]]) -> set[str]:
    """Build a set of zone IDs for climate zones only."""
    return {str(z.get("id")) for z in zones_info if z.get("type") in CLIMATE_ZONE_TYPES}

