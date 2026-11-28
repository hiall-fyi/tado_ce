"""Tado CE coordinator: adaptive polling, sub-system orchestration, entity propagation."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from datetime import datetime, timedelta
import logging
import math
import sys
import time
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .cloud_health import EVENT_STATE, CloudEvent, CloudHealthState
from .const import (
    BRIDGE_POLL_INTERVAL_SECONDS,
    COLD_START_RETRY_MAX_ATTEMPTS,
    COLD_START_RETRY_SECONDS,
    DEFAULT_DAY_INTERVAL,
    DEMAND_FETCH_ON_FOREIGN_CHANGE,
    DEVICE_SYNC_QUEUE_MAX_DEPTH,
    DOMAIN,
    ENTITY_DATA_CONDENSATION_RISK,
    ENTITY_FRESHNESS_EXPIRY_SECONDS,
    FORCEABLE_FETCH_HOME_STATE,
    FORCEABLE_FETCH_MOBILE,
    FORCEABLE_FETCH_WEATHER,
    HOMEKIT_DROP_FETCH_MIN_GAP_SECONDS,
    MAPPING_RETRY_STALL_LIMIT,
    MECH_HK_PUT_70402,
    MECH_POLL_PAUSE,
    MECH_POLL_SKIP,
    OUTDOOR_TEMP_HISTORY_MAX,
    OVERLAY_MODE_DEFAULT,
    SMART_VALVE_NIGHT_FLOOR_MINUTES,
    TIMER_DURATION_DEFAULT,
    get_climate_zone_ids,
    is_climate_zone,
)
from .entity_cleanup import remove_zone_registry_rows
from .exceptions import TadoAuthError, TadoBlockedError, TadoBridgeApiError, TadoRateLimitError, TadoSyncError
from .fetch_plan import (
    GAP_PARTIAL,
    KIND_CAPABILITIES_FORCED,
    KIND_MOBILE_FULL,
    KIND_MOBILE_QUICK,
    KIND_OFFSETS_DRIFT,
    KIND_OFFSETS_FIRST_LOAD,
    KIND_WEATHER,
    KIND_ZONE_STATES,
    KIND_ZONES_INFO,
    SHED_FULL_SYNC_DEFERRED,
    SKIP_DEFERRED_FOR_BACKOFF,
    SKIP_DEFERRED_FOR_PROBE,
    FetchPlan,
    FetchTally,
    GapRecord,
    PlanConfig,
    PlanInputs,
    PollOutcome,
    StampUpdates,
    ZoneFact,
    apply_results,
    build_plan,
    build_post_plan,
    gap_after_failure,
    gap_cap_seconds,
    gate_post_plan,
    has_custom_interval,
    shed_plan,
    snapshot_shape,
    steady_state_cost,
)
from .helpers import (
    apply_cloud_reading_trust,
    get_homekit_cloud_lag_seconds,
    get_zone_states,
    hard_quota_reserve,
    mask_home_id,
    mask_serial,
    svc_schedule_zone_ids,
    zone_build_identity,
)
from .homekit_mapping import async_rebuild_and_save_mapping, cloud_only_climate_zones, device_serials
from .insight_history import InsightHistoryTracker
from .mechanism_counters import MechanismCounters
from .polling import get_polling_interval, should_pause_polling
from .quota_levels import QuotaLevels
from .ratelimit import _sanitize_retry_after
from .repair_helpers import (
    SVC_SENSOR_MISSING_ISSUE,
    TRV_BATTERY_LOW_ISSUE,
    ZONE_REASSIGNED_ISSUE,
    RepairIssueSpec,
    async_create_zone_reassigned_issue,
    async_dismiss_issue,
    async_issue_exists,
    async_sync_battery_repairs,
)
from .weather_compensation import (
    WeatherCompensationState,
)
from .write_health_tracker import WriteHealthTracker
from .write_optimizer import ActionDebouncer, DeviceSyncQueue
from .write_timing import WriteTimingRecorder
from .zone_fingerprint import ZoneFingerprintDelta, ZoneFingerprintTracker, zone_device_fingerprints_changed
from .zone_signals import fetch_covers_write, zone_qualifies_for_homekit_power, zone_view, zone_view_target
from .zone_state_registry import (
    REASSIGNED_ONLY,
    REMOVED_ONLY,
    ZONE_KEYED_FLAT_STORES,
    ZoneDropIncomplete,
    ZoneStateRegistry,
)
from .zone_write_ledger import ZoneWriteLedger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from homeassistant.const import Platform
    from homeassistant.core import HomeAssistant

    from .adaptive_preheat import AdaptivePreheatManager
    from .api_call_tracker import APICallTracker
    from .api_client import TadoApiClient
    from .bridge_api import TadoBridgeApiClient
    from .bridge_health import BridgeHealthTracker
    from .cloud_health import CloudHealth, CloudHealthChange
    from .config_manager import ConfigurationManager
    from .data_loader import DataLoader
    from .heating_coordinator import HeatingCycleCoordinator
    from .homekit_client import HomeKitClient
    from .homekit_provider import HomeKitLocalProvider
    from .offset_sync_controller import OffsetSyncController
    from .smart_comfort import SmartComfortManager
    from .state_reconciler import StateReconciler
    from .state_restore_manager import CapturedState, StateRestoreManager
    from .valve_controller import SmartValveController
    from .zone_config_manager import ZoneConfigManager
    from .zone_state_registry import ZoneDropReason

_LOGGER = logging.getLogger(__name__)

# Fetched polls in a row that must list a zone nowhere before its state is dropped.
ZONE_REMOVAL_CONFIRM_POLLS = 2

# (entry_id, zone_id) pairs whose entity reload was already scheduled this HA run; outlives the reloaded coordinator.
_ZONE_RELOADS_REQUESTED: set[tuple[str, str]] = set()

# Plan item kinds whose skip counter and diagnostics use an older name.
_SKIP_ITEM_NAMES = {KIND_MOBILE_QUICK: "mobile_devices"}

# Friendly names for withheld readings in log text
_WITHHELD_READING_NAMES = {"insideTemperature": "temperature", "humidity": "humidity"}

# What each non-OK cloud state means, worded for the log line.
_CLOUD_STATE_WORDING = {
    CloudHealthState.BACKOFF: "Tado asked us to slow down, so requests wait for a while",
    CloudHealthState.UNREACHABLE: "Tado could not be reached",
    CloudHealthState.BLOCKED: "Tado is refusing this connection",
    CloudHealthState.AUTH_FAILED: "Tado rejected the saved sign-in",
}

# Published entity data built on each withheld reading; hiding the reading switches these off.
_HIDDEN_READING_DEPENDENTS = {
    "insideTemperature": (ENTITY_DATA_CONDENSATION_RISK,),
    "humidity": (ENTITY_DATA_CONDENSATION_RISK,),
}


# HA official pattern: provides type safety for entry.runtime_data
type TadoConfigEntry = ConfigEntry[TadoDataUpdateCoordinator]


class TadoDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Per-config-entry coordinator with adaptive polling and sub-system orchestration."""

    config_entry: TadoConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        config_manager: ConfigurationManager,
        zone_config_manager: ZoneConfigManager,
        data_loader: DataLoader,
        api_client: TadoApiClient,
        api_tracker: APICallTracker,
        smart_comfort_manager: SmartComfortManager | None = None,
        heating_cycle_coordinator: HeatingCycleCoordinator | None = None,
        adaptive_preheat_manager: AdaptivePreheatManager | None = None,
        state_restore_manager: StateRestoreManager | None = None,
    ) -> None:
        """Initialise the coordinator with every per-entry dependency wired in."""
        # No snapshot yet, so the interval is the default and the full-sync term is not read.
        initial_interval = get_polling_interval(config_manager, full_sync_extra_calls=0)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} ({mask_home_id(entry.data.get('home_id'))})",
            update_interval=timedelta(minutes=initial_interval),
        )

        # Per-entry dependencies (accessible by entities via self.coordinator)
        self.config_manager = config_manager
        self.zone_config_manager = zone_config_manager
        self.data_loader = data_loader
        self.write_timing = WriteTimingRecorder(config_manager, data_loader)
        self.api_client = api_client
        self.api_tracker = api_tracker
        self.smart_comfort_manager = smart_comfort_manager
        self.heating_cycle_coordinator = heating_cycle_coordinator
        self.adaptive_preheat_manager = adaptive_preheat_manager
        self._sr_manager = state_restore_manager

        # RefreshHandler (self-reference resolves chicken-and-egg dependency)
        from .refresh_handler import RefreshHandler

        self.refresh_handler = RefreshHandler(self)

        self.home_id: str = entry.data.get("home_id") or "default"

        self.entity_freshness: dict[str, float] = {}
        self._freshness_lock = asyncio.Lock()
        self._global_sequence: int = 0

        self._heating_cycle_timeout_cancel: Callable[[], None] | None = None

        self.overlay_mode: str = OVERLAY_MODE_DEFAULT
        self.timer_duration: int = TIMER_DURATION_DEFAULT

        # Outdoor temp history (owned by coordinator; async load, debounced sync save)
        self._outdoor_temp_history: list[float] = []

        # Shared entity data store, entities publish computed values here
        # so other components (insight collector, adaptive preheat) can read
        # without cross-entity hass.states.get() coupling.
        #
        # LIFECYCLE: In-memory only, not persisted across restarts.
        # Populated progressively as entities process their first poll data.
        # Consumers MUST handle None returns from get_entity_data() during
        # the startup gap (typically 0-2 poll cycles after restart).
        #
        # Structure: {zone_id: {"condensation_risk": {...}, "preheat_now": {...}, ...}}
        self.entity_data: dict[str, dict[str, dict[str, Any]]] = {}

        # Pending cleanup flags, set by options flow, consumed by migration
        # after reload. Avoids transient hass.data[DOMAIN_*] keys.
        self._pending_cleanup: dict[str, dict[str, bool]] = {}

        self.insight_history = InsightHistoryTracker(hass, self.home_id)

        # Insight collector mutable state, owned by coordinator so duration
        # tracking survives individual sensor entities being disabled by the user.
        # anomaly_start_times drives "heating anomaly for N minutes"; humidity
        # histories drive humidity-trend insights; ventilation_diff_history
        # drives the ventilation-opportunity insight.
        self._insight_anomaly_start_times: dict[str, datetime] = {}
        self._insight_humidity_histories: dict[str, list[Any]] = {}
        self._insight_ventilation_diff_history: list[Any] = []

        self._last_full_sync: datetime | None = None
        self._cached_ratelimit: dict[str, Any] | None = None

        self._zone_fingerprint = ZoneFingerprintTracker()
        self._last_known_zone_types: dict[str, str] = {}
        self._last_known_zone_dates: dict[str, str] = {}
        self.zone_state = ZoneStateRegistry()
        # Owners still owing a drop per (zone, reason); lost on restart, so the first poll then runs every owner once.
        self._zone_drop_pending: dict[tuple[str, str], frozenset[str]] = {}
        # The dateCreated each reassigned zone's drop ran for, so a newer reassignment starts a full drop.
        self._zone_drop_dates: dict[str, str] = {}
        # Zones whose removal drop completed since both lists last listed them; cleared when a zone is listed again.
        self._zone_drops_done: set[str] = set()
        # Consecutive fetched polls that listed a zone nowhere; memory only, so a restart counts again.
        self._zone_absent_polls: dict[str, int] = {}
        # Zones zoneStates listed before zones_info did; built once zones_info lists them.
        self._zones_awaiting_listing: set[str] = set()
        self._request_full_sync_next_cycle: bool = False
        # Per mode, the quota day (last_reset_utc) whose first hold was already logged.
        self._quota_hold_logged: dict[bool, str | None] = {}
        # Zones whose battery repair failed on the last refresh, so a repair that keeps failing warns once.
        self._battery_repair_failed_zones: set[str] = set()

        # Polling pause state, set when should_pause_polling returns
        # True, cleared on the first cycle that returns False after a
        # pause. Used by should_pause_polling to suppress the
        # "resuming polling" INFO line when the user was never paused.
        self._was_paused: bool = False

        # True while a due full sync is being postponed because it does not fit the quota reserve.
        self._full_sync_deferred: bool = False

        # The sync plan of the latest poll, after shedding; the post-sync read uses it.
        self._poll_plan: FetchPlan | None = None
        self._last_post_plan: FetchPlan | None = None

        # Offset drift refresh tracker: the post-sync drift step stamps it when it obtains an offset.
        self._last_offset_resync: datetime | None = None

        # Round-robin drift-refresh cursor: zone_id of the last zone
        # tried. Keyed by zone_id (not list position) so it self-heals on
        # zone add/remove. In-memory; restarting at the list head on reboot is
        # harmless (drift refresh is a reconcile, not a write).
        self._last_refreshed_zone_id: str | None = None

        self._cold_start_retry_armed: bool = False
        self._cold_start_retry_cancel: Callable[[], None] | None = None
        self._cold_start_retry_attempts: int = 0
        self._cloud_health_unsub: Callable[[], None] | None = None

        # Bridge API client (lazy-init from options when bridge credentials present)
        self.bridge_api_client: TadoBridgeApiClient | None = None

        self.bridge_health_tracker: BridgeHealthTracker | None = None
        self._bridge_first_fetch_logged: bool = False

        # Independent bridge poll task (bridge API doesn't count toward cloud quota)
        self._bridge_poll_task: asyncio.Task[None] | None = None
        self._cached_bridge_data: dict[str, object] | None = None

        # Platforms loaded during setup (used by unload to match exactly)
        self.loaded_platforms: frozenset[Platform] = frozenset()
        # Zone ids the platforms built their entities for at setup (a confirmed removal takes the id out); None until they are forwarded.
        self.entity_zone_ids: frozenset[str] | None = None
        # The (type, name) each of those zones' entities were built with, so a reused id can tell whether they still fit.
        self.entity_zone_identity: dict[str, tuple[str, str]] | None = None

        # HomeKit local control (optional, set by entry_lifecycle when homekit_enabled)
        self.homekit_client: HomeKitClient | None = None
        # HomeKit shared controller (optional, the long-lived aiohomekit
        # Controller for this entry, built by entry_lifecycle when HomeKit
        # is enabled; its _hap browser keeps the pairing discovery cache
        # warm). Stopped on unload.
        self.homekit_controller: Any | None = None
        self.homekit_provider: HomeKitLocalProvider | None = None
        self.state_reconciler: StateReconciler | None = None

        self._last_cloud_zone_fetch: datetime | None = None
        self._last_zone_fetch_request: datetime | None = None
        # Start time of the newest zone-states fetch attempt, published before
        # the request so a write mid-sync snapshots it (see zone_fetch_epoch_for_write).
        self._zone_fetch_started_at: datetime | None = None
        self._last_weather_fetch: datetime | None = None
        self._last_home_state_fetch: datetime | None = None
        self._last_mobile_devices_fetch: datetime | None = None
        # Retry gaps after a silent fetch failure and the full-sync sub-items still owed; memory only.
        self._gaps: dict[str, GapRecord] = {}
        self._full_sync_partial: tuple[str, ...] = ()
        self._mapping_retry_stall_count: int = 0
        self._cloud_only_logged: frozenset[str] | None = None
        self._accessory_list_failing: bool = False
        self._mapping_retry_listed: bool = False
        self._mapping_retry_serials: dict[str, list[str]] | None = None
        self._prev_homekit_connected: bool = False
        self._homekit_reads_saved: int = 0
        self._homekit_writes_saved: int = 0
        self._savings_last_reset_seen: str | None = None  # recorder's reset marker last acted on
        self.mechanism_counters = MechanismCounters()
        self.last_poll_skips: dict[str, str] = {}
        self.last_poll_skips_at: datetime | None = None

        # HomeKit write metrics (ephemeral, reset on quota reset and reconnect)
        self._homekit_write_attempts: int = 0
        self._homekit_write_successes: int = 0
        self._homekit_write_fallbacks: int = 0
        self._homekit_write_latency_sum: float = 0.0
        self._homekit_write_latency_count: int = 0

        self.write_health_trackers: dict[str, WriteHealthTracker] = {}
        self.write_ledger = ZoneWriteLedger(self)
        # Zone -> zone-fetch epoch recorded when a HomeKit app-off was first seen; see zone_signals.is_homekit_off_held.
        self.homekit_off_latch: dict[str, datetime | None] = {}

        # Smart Valve Control: per-zone proportional offset controllers
        self.valve_controllers: dict[str, SmartValveController] = {}
        # Switched-off controllers that still owe a hand-back; kept apart so no consumer of valve_controllers sees them.
        self.retired_valve_controllers: dict[str, SmartValveController] = {}

        # Offset Sync: per-zone device offset controllers
        self.offset_sync_controllers: dict[str, OffsetSyncController] = {}

        # Per-zone locks serialise controller lifecycle changes.
        self._zone_transition_locks: dict[str, asyncio.Lock] = {}

        # Set by async_shutdown_valve_controllers to prevent queued transitions
        # from installing/activating controllers after shutdown begins.
        self._shutting_down: bool = False
        # Set by setup so a clean unload can run the same teardown a failed setup uses.
        self.setup_handles: Any = None
        # True while a start-up pass is owed: zone info was missing or a controller failed to start.
        self._valve_init_pending: bool = False
        # Zones whose current run of failed start-ups has been logged at WARNING.
        self._valve_init_failing: set[str] = set()
        # Controllers (kind, zone) whose current run of failed evaluations has been logged at WARNING.
        self._controller_eval_failing: set[tuple[str, str]] = set()
        self._zone_config_listener_registered: bool = False

        # Weather compensation mutable runtime state (persists across polls)
        self._wc_state = WeatherCompensationState()
        self._wc_state_loaded: bool = False

        self._action_debouncer = ActionDebouncer(
            default_window=float(config_manager.get_smart_actions_debounce_seconds()),
        )
        self._device_sync_queue = DeviceSyncQueue(
            delay=config_manager.get_device_sync_delay_seconds(),
            max_depth=DEVICE_SYNC_QUEUE_MAX_DEPTH,
            cloud_held=lambda: self.cloud_health.holds(),
        )
        self._register_zone_state_owners()
        self.attach_cloud_health()

    @property
    def outdoor_temp_history(self) -> list[float]:
        """Return outdoor temp history (read-only access for sensors)."""
        return self._outdoor_temp_history

    @property
    def is_homekit_active(self) -> bool:
        """Return True when HomeKit local provider is wired and currently connected."""
        return self.homekit_provider is not None and self.homekit_provider.is_connected

    @property
    def _homekit_can_keep_entities_live(self) -> bool:
        """True when a cloud failure/pause can serve cached data instead of raising.

        Needs HomeKit connected AND a prior successful fetch: on cold start
        self.data is still None, so returning it as a "success" would forward
        the climate platform with zero entities and no way to add them later.
        """
        return self.is_homekit_active and bool(self.data)

    @property
    def _homekit_can_start_on_saved_data(self) -> bool:
        """True when HomeKit is connected and the saved zone states can seed the first refresh."""
        if not self.is_homekit_active:
            return False
        saved = self.data_loader.get_cached("zones")
        return isinstance(saved, dict) and bool(saved.get("zoneStates"))

    def _log_withheld_transitions(
        self, previous: frozenset[tuple[str, str]], current: frozenset[tuple[str, str]],
    ) -> None:
        """Log once when a zone reading starts or stops being hidden."""
        for zone_id, key in sorted(current - previous):
            _LOGGER.info(
                "Coordinator: zone %s %s has stopped updating because its main device has been offline for four hours or more, so the last value is hidden",
                zone_id, _WITHHELD_READING_NAMES.get(key, key),
            )
        # A zone missing from this poll is not reported as unhidden.
        present = get_zone_states(self.data)
        for zone_id, key in sorted(previous - current):
            if zone_id not in present:
                continue
            _LOGGER.info(
                "Coordinator: zone %s %s is no longer hidden",
                zone_id, _WITHHELD_READING_NAMES.get(key, key),
            )

    def _reapply_trust_to_held_data(self) -> dict[str, Any]:
        """Re-run the time-based trust step on the held data so a reading still hides at four hours while cloud polling is paused; it never un-hides one."""
        # WHY: these polls never reach post-sync, so the gauge would miss the stale tail.
        self.observe_data_ages()
        data = self.data or {}
        previous: frozenset[tuple[str, str]] = data.get("withheld_readings") or frozenset()
        zones, found = apply_cloud_reading_trust(
            data.get("zones"), data.get("zones_info"), dt_util.utcnow(), previous,
        )
        # Union, not replace: dropped readings cannot be found again, and un-hiding needs fresh data.
        withheld = previous | found
        if withheld == previous:
            if not previous:
                return data
            # Hidden readings stay hidden: rebuild again, because the sensor can only republish after the last rebuild.
            return self._rebuild_zone_insights(data, {zone_id for zone_id, _ in previous})
        self._log_withheld_transitions(previous, withheld)
        held = {**data, "zones": zones, "withheld_readings": withheld}
        return self._refresh_insights_after_hide(held, withheld - previous)

    async def _async_keep_live_on_cloud_failure(
        self, *, retry_on_snapshot: bool = False
    ) -> dict[str, Any] | None:
        """Return data that keeps HomeKit entities live through a cloud failure, or None when there is none."""
        if self._homekit_can_keep_entities_live:
            result = self._reapply_trust_to_held_data()
        elif self._homekit_can_start_on_saved_data:
            result = await self._async_finish_poll(None)
            if retry_on_snapshot:
                self._arm_cold_start_retry()
        else:
            return None
        _LOGGER.debug("Coordinator: cloud call failed, HomeKit keeps the entities live on held data")
        return result

    def _arm_cold_start_retry(self) -> None:
        """Schedule the single catch-up cloud fetch; later calls on this coordinator do nothing."""
        if self._cold_start_retry_armed:
            return
        self._cold_start_retry_armed = True
        self._cold_start_retry_cancel = async_call_later(
            self.hass, COLD_START_RETRY_SECONDS, self._async_cold_start_retry
        )

    async def _async_cold_start_retry(self, _now: datetime) -> None:
        """Ask for the catch-up zone fetch; a refusal tries again at 120 s then 240 s, three attempts in all."""
        self._cold_start_retry_cancel = None
        if self.auth_failed:
            return
        # The catch-up exists only for a start with no zone fetch yet, so any stamp means one landed.
        if self._last_cloud_zone_fetch is not None:
            self._cold_start_retry_attempts = 0
            return
        self._cold_start_retry_attempts += 1
        attempt = self._cold_start_retry_attempts
        if self.is_cloud_backoff_active():
            refused = "backoff"
        elif await self.async_request_zone_fetch("cold_start_retry"):
            return
        else:
            refused = "request refused"
        if self._shutting_down or self.refresh_handler is None:
            return
        if attempt >= COLD_START_RETRY_MAX_ATTEMPTS:
            _LOGGER.debug("Coordinator: cold-start catch-up refused (%s), attempt %d was the last", refused, attempt)
            return
        delay = COLD_START_RETRY_SECONDS * 2**attempt
        _LOGGER.debug("Coordinator: cold-start catch-up refused (%s) on attempt %d, trying again in %.0f s", refused, attempt, delay)
        self._cold_start_retry_cancel = async_call_later(self.hass, delay, self._async_cold_start_retry)

    def cancel_cold_start_retry(self) -> None:
        """Cancel the pending catch-up fetch, if any."""
        cancel, self._cold_start_retry_cancel = self._cold_start_retry_cancel, None
        if cancel is not None:
            cancel()

    @contextlib.contextmanager
    def _publishing(self, data: dict[str, Any]) -> Iterator[None]:
        """Expose `data` as self.data for the block, then restore the previous value."""
        previous = self.data
        self.data = data
        try:
            yield
        finally:
            self.data = previous

    def _record_zone_insights(self, zone_insights: dict[str, list[Any]]) -> None:
        """Advance insight history for the given zone insights and persist the runtime state."""
        all_insights: list[Any] = []
        for insights_list in zone_insights.values():
            all_insights.extend(insights_list)
        self.insight_history.update(all_insights, dt_util.utcnow())
        # Persist anomaly timers and humidity/ventilation history so duration counters survive restarts.
        self._save_insight_runtime_state()

    def _publish_hidden_insight_inputs(self, newly_hidden: frozenset[tuple[str, str]]) -> None:
        """Mark the published entity data that depends on a newly hidden reading as unavailable."""
        for zone_id, key in sorted(newly_hidden):
            for entity_key in _HIDDEN_READING_DEPENDENTS.get(key, ()):
                self.publish_entity_data(zone_id, entity_key, {"state": "unavailable", "recommendation": ""})

    def _rebuild_zone_insights(
        self, held: dict[str, Any], zone_ids: set[str], newly_hidden: frozenset[tuple[str, str]] = frozenset(),
    ) -> dict[str, Any]:
        """Rebuild the insights of the given zones from the held data; a newly hidden reading also clears what was built on it."""
        try:
            from .sensor_insight_collector import refresh_zone_insights

            hcc = self.heating_cycle_coordinator
            for zone_id, key in sorted(newly_hidden):
                if hcc and key == "insideTemperature":
                    hcc.clear_zone_state(zone_id, source="cloud")
            self._publish_hidden_insight_inputs(newly_hidden)
            with self._publishing(held):
                refreshed = refresh_zone_insights(
                    self.hass, self, held.get("zone_insights") or {}, zone_ids,
                    self._insight_anomaly_start_times, self._insight_humidity_histories,
                )
                self._record_zone_insights(refreshed)
        except Exception:
            # Optional recompute: a raise here would turn a HomeKit keep-live into UpdateFailed.
            _LOGGER.debug("Coordinator: insight refresh failed, keeping the held insights", exc_info=True)
            return held
        return {**held, "zone_insights": refreshed}

    def _refresh_insights_after_hide(
        self, held: dict[str, Any], newly_hidden: frozenset[tuple[str, str]],
    ) -> dict[str, Any]:
        """Rebuild the insights of zones whose reading was just hidden, so they stop using it."""
        if not newly_hidden:
            return held
        # Sensors republish after this rebuild, so the next keep-live pass rebuilds again.
        return self._rebuild_zone_insights(held, {zone_id for zone_id, _ in newly_hidden}, newly_hidden)

    @property
    def action_debouncer(self) -> ActionDebouncer:
        """Return the action debouncer instance."""
        return self._action_debouncer

    @property
    def device_sync_queue(self) -> DeviceSyncQueue:
        """Return the device sync queue instance."""
        return self._device_sync_queue

    @property
    def homekit_write_metrics(self) -> dict[str, float]:
        """Return HomeKit write metrics for sensor exposure."""
        avg_latency = (
            self._homekit_write_latency_sum / self._homekit_write_latency_count
            if self._homekit_write_latency_count > 0
            else 0.0
        )
        return {
            "write_attempts": self._homekit_write_attempts,
            "write_successes": self._homekit_write_successes,
            "write_fallbacks": self._homekit_write_fallbacks,
            "write_avg_latency_ms": round(avg_latency, 1),
        }

    def write_health_for(self, zone_id: str) -> WriteHealthTracker | None:
        """Return the zone's HomeKit Write Health tracker (created on first use), or None without HomeKit."""
        if self.homekit_provider is None:
            return None
        tracker = self.write_health_trackers.get(zone_id)
        if tracker is None:
            tracker = self.write_health_trackers[zone_id] = WriteHealthTracker(zone_id)
        return tracker

    def reset_write_health(self) -> None:
        """Put every open Write Health circuit half-way on a bridge reconnect; closed ones and failure counts stay."""
        for tracker in self.write_health_trackers.values():
            tracker.half_open()

    def _reset_write_metrics(self) -> None:
        """Reset HomeKit write metrics counters."""
        self._homekit_write_attempts = 0
        self._homekit_write_successes = 0
        self._homekit_write_fallbacks = 0
        self._homekit_write_latency_sum = 0.0
        self._homekit_write_latency_count = 0

    def publish_entity_data(self, zone_id: str, key: str, data: dict[str, Any]) -> None:
        """Publish in-memory computed entity data; consumers must tolerate None during the startup gap."""
        # A removed zone's entity can still publish once from an update in flight, which must not undo the drop.
        if zone_id in self._zone_drops_done and zone_id not in self._zone_fingerprint.zone_ids:
            return
        if zone_id not in self.entity_data:
            self.entity_data[zone_id] = {}
        self.entity_data[zone_id][key] = data

    def get_entity_data(self, zone_id: str, key: str) -> dict[str, Any] | None:
        """Read published entity data for a zone, or None if not published yet."""
        return self.entity_data.get(zone_id, {}).get(key)

    async def _async_post_sync_processing(
        self, zone_data: dict[str, Any] | list[Any] | None, weather_data: dict[str, Any] | list[Any] | None,
        *, zone_fetch_started: datetime | None = None, fetch_start_mono: float | None = None,
    ) -> dict[str, Any]:
        """Run post-sync processing: history detection, cache reads, bridge, WC."""
        # Disk payload from before this process, not a fetch: it must not become a new sample.
        saved_snapshot = zone_fetch_started is None and self.last_zone_fetch_ts() is None

        from .ratelimit import (
            async_detect_reset_from_history,
            async_update_ratelimit_reset_time,
        )

        detected_reset = await async_detect_reset_from_history(self.hass, self.home_id)
        if detected_reset:
            _LOGGER.debug(
                "Coordinator: detected Tado API quota reset at %s UTC from "
                "HA history",
                detected_reset.strftime("%H:%M"),
            )
            await async_update_ratelimit_reset_time(
                self.hass, detected_reset, self.home_id, self.data_loader,
            )

        # Read all data from in-memory cache (populated by api_client write-through)
        config_data = self.data_loader.get_cached("config")
        home_state_data = self.data_loader.get_cached("home_state")
        # A stored dict from an earlier sync-on period must not outrank Tado's zone answer.
        if not self.config_manager.get_home_state_sync_enabled() and not (
            isinstance(home_state_data, dict) and "injected_until" in home_state_data
        ):
            home_state_data = None
        ratelimit_data = self.data_loader.get_cached("ratelimit")
        api_call_history_data: dict[str, Any] = dict(self.api_tracker._call_history) if self.api_tracker else {}
        zones_info_data = self.data_loader.get_cached("zones_info")
        mobile_devices_data = self.data_loader.get_cached("mobile_devices")
        home_devices_data = self.data_loader.get_cached("home_devices")
        async_sync_battery_repairs(self.hass, self.home_id, zones_info_data, self._battery_repair_failed_zones)

        # Withhold readings from an offline device before the heating cycle, weather compensation and result see them.
        previous_withheld: frozenset[tuple[str, str]] = (self.data or {}).get("withheld_readings") or frozenset()
        zone_data, withheld_readings = apply_cloud_reading_trust(
            zone_data, zones_info_data or None, dt_util.utcnow(), previous_withheld,
        )

        # Drift refresh and capabilities refetch run BEFORE snapshotting `offsets` and
        # `zone_capabilities`, so the result dict carries the fresh values.
        ratelimit_for_resync = ratelimit_data if isinstance(ratelimit_data, dict) else None
        await self._async_run_post_sync_plan(zones_info_data, ratelimit_for_resync)

        offsets_data = self.data_loader.get_cached("offsets")
        offset_clamps_data = self.data_loader.get_cached("offset_clamps")
        schedules_data = self.data_loader.get_cached("schedules")
        zone_capabilities = self.data_loader.get_cached("zone_capabilities")
        heating_circuits_data = self.data_loader.get_cached("heating_circuits")
        heating_circuit_control_data = self.data_loader.get_cached("heating_circuit_control")
        early_start_data = self.data_loader.get_cached("early_start")

        if not saved_snapshot:
            await self._accumulate_outdoor_temp_history(weather_data)

        # Only a fetch brings new readings; saved data or a skip poll would stamp old ones with the time of this poll.
        if zone_fetch_started is not None:
            await self._notify_heating_cycle_updates(
                zone_data, zone_fetch_started=zone_fetch_started, zones_info=zones_info_data,
            )
            self._resolve_pending_puts(zone_data, fetch_start_mono)

        await self._cleanup_entity_freshness()

        # Save dirty data (piggyback on poll cycle)
        if self.insight_history.needs_save:
            await self.insight_history.async_save()
        if self.api_tracker.needs_save:
            await self.api_tracker.async_save_if_dirty()

        # Bridge API data: seed on first poll so sensor platform has data at setup
        if self._cached_bridge_data is None:
            first_bridge = await self._async_fetch_bridge_data()
            if first_bridge is not None:
                self._cached_bridge_data = first_bridge
        bridge_data = self._cached_bridge_data
        self._ensure_bridge_poll_running()

        if not self._wc_state_loaded and self.config_manager.get_wc_enabled():
            raw = await self.data_loader.async_load_wc_state()
            if raw and isinstance(raw, dict):
                self._wc_state = WeatherCompensationState.from_dict(raw)
                _LOGGER.debug(
                    "Coordinator: restored persisted weather-compensation state",
                )
            self._wc_state_loaded = True

        # Weather compensation (after bridge data, needs bridge client)
        _zone_dict = zone_data if isinstance(zone_data, dict) else None
        _weather_dict = weather_data if isinstance(weather_data, dict) else None
        # Saved payloads are not current readings: the cycle runs on the live outdoor sensor or its persisted smoothing.
        wc_data = await self._async_run_weather_compensation(
            None if saved_snapshot else _zone_dict,
            None if saved_snapshot else _weather_dict,
            saved_data=saved_snapshot,
        )
        if wc_data is not None:
            self.data_loader.save_wc_state(self._wc_state.to_dict())

        result: dict[str, Any] = {
            "zones": zone_data or {},
            "withheld_readings": withheld_readings,
            "config": config_data or {},
            "home_state": home_state_data or {},
            "zone_capabilities": zone_capabilities or {},
            "ratelimit": ratelimit_data or {},
            "api_call_history": api_call_history_data or {},
            "zones_info": zones_info_data or [],
            "weather": weather_data or {},
            "mobile_devices": mobile_devices_data or [],
            "home_devices": home_devices_data or [],
            "offsets": offsets_data or {},
            "offset_clamps": offset_clamps_data or {},
            "schedules": schedules_data or {},
            "heating_circuits": heating_circuits_data or {},
            "heating_circuit_control": heating_circuit_control_data or {},
            "early_start": early_start_data or {},
        }
        if bridge_data is not None:
            result["bridge"] = bridge_data
        if wc_data is not None:
            result["weather_compensation"] = wc_data

        # Compute zone insights once per poll so all sensors read from one
        # pre-built map, and advance insight history so duration tracking runs
        # regardless of whether the home insights sensor entity is enabled.
        # collect_zone_insights and its transitive callees read coordinator.data
        # at ~8 sites (plus coordinator.get_entity_data / heating_cycle_coordinator
        # not reachable via result dict), so we temporarily publish result as
        # self.data instead of plumbing a coord_data arg through every function.
        # Framework assigns self.data = result on return, so this just anticipates
        # that by a few lines; _publishing restores on any exception.
        # Entities update only after this method returns, so a newly hidden reading is switched off here.
        self._publish_hidden_insight_inputs(withheld_readings - previous_withheld)
        with self._publishing(result):
            from .sensor_insight_collector import (
                advance_ventilation_diff_history,
                collect_zone_insights,
            )

            zone_insights = collect_zone_insights(
                self.hass, self,
                self._insight_anomaly_start_times,
                self._insight_humidity_histories,
                sample_history=not saved_snapshot,
            )
            result["zone_insights"] = zone_insights
            if not saved_snapshot:
                advance_ventilation_diff_history(self)
            self._record_zone_insights(zone_insights)

        await self._handle_state_restore_updates(home_state_data, result)

        # The framework only assigns self.data = result once this call
        # returns, so the loops below would otherwise see last cycle's data.
        self.data = result
        self._log_withheld_transitions(previous_withheld, withheld_readings)
        # Epoch advances with the data it describes. Stamped earlier, a bridge
        # listener update during the awaits above could pair it with old zones.
        if zone_fetch_started is not None:
            self._last_cloud_zone_fetch = zone_fetch_started

        # After the epoch stamp, so a hand-back judged inside this pass sees the fetch that brought the zones.
        await self._async_retry_valve_init(zones_info_data)

        for controller in (*self.valve_controllers.values(), *self.retired_valve_controllers.values()):
            await self._async_evaluate_controller("Smart Valve", controller)

        for settled_zone in [z for z, c in self.retired_valve_controllers.items() if not c.retiring]:
            del self.retired_valve_controllers[settled_zone]

        for controller_os in self.offset_sync_controllers.values():
            await self._async_evaluate_controller("Offset Sync", controller_os)

        if not saved_snapshot:
            await self._async_drop_vanished_zones(fresh_fetch=zone_fetch_started is not None)
        await self._async_retry_homekit_mapping()
        self._log_cloud_only_zones()

        self.observe_data_ages()
        self.flush_mechanism_counters()  # after the gauge sample so one save covers the poll
        return result

    async def _async_retry_homekit_mapping(self) -> None:
        """Rebuild the mapping each poll until every mappable zone is covered.

        Stops on completion, or after MAPPING_RETRY_STALL_LIMIT polls with no
        newly-mapped zone (an accessory the bridge never exposes).
        """
        from .homekit_mapping import cloud_serials_by_zone, mapping_covers_all_zones

        client = self.homekit_client
        if client is None or not client.is_connected:
            return
        zones_info = (self.data or {}).get("zones_info") or []
        if not zones_info:
            return
        current_serials = cloud_serials_by_zone(zones_info)
        if current_serials != self._mapping_retry_serials:
            # A device swap must not wait behind a budget spent on an earlier, unrelated gap.
            self._mapping_retry_serials = current_serials
            self.reset_mapping_retry_budget()
        if mapping_covers_all_zones(client.zone_aid_map, zones_info, client.cloud_serials, require_fingerprint=True):
            return
        if self._mapping_retry_stall_count >= MAPPING_RETRY_STALL_LIMIT:
            return
        zones_mapped_before = len(client.zone_aid_map)
        try:
            mapping = await async_rebuild_and_save_mapping(
                self.hass, client, self.home_id, zones_info,
                warn_on_empty_list=not self._accessory_list_failing,
            )
        except (TimeoutError, aiohttp.ClientError, ValueError, KeyError):
            _LOGGER.warning(
                "HomeKit: zone mapping rebuild raised an exception, "
                "will retry on the next poll", exc_info=True,
            )
            return
        self._accessory_list_failing = mapping.get("accessories_listed") is False
        if self._accessory_list_failing:
            return
        self._mapping_retry_listed = True
        if mapping.get("serial_to_zone"):
            _LOGGER.info(
                "HomeKit: zone mapping built on retry: %d zone(s) mapped",
                len(mapping["serial_to_zone"]),
            )
            # The mapping just gained zones. Setup subscribed to events while
            # zone_aid_map was still empty (0 chars), and no reconnect has fired
            # (the bridge stayed connected, it was the mapping that lagged, not
            # the connection). Subscribe now so HomeKit pushes actually flow;
            # without this the zone stays cloud-only despite a connected, mapped
            # bridge until an HA restart or a reconnect.
            provider = self.homekit_provider
            if provider is not None and provider.is_connected:
                # Both primitives self-protect (refresh → [], subscribe →
                # reconnect on a bridge drop), so no caller-side guard: the
                # cloud-domain (TimeoutError, ClientError, ValueError, KeyError)
                # tuple this used to carry never matched a bridge error anyway.
                await provider.async_refresh_accessories()
                await provider.async_subscribe_events()

        if mapping_covers_all_zones(client.zone_aid_map, zones_info, client.cloud_serials, require_fingerprint=True):
            _LOGGER.info(
                "HomeKit: zone mapping now complete, all %d zone(s) mapped",
                len(client.zone_aid_map),
            )

        # A stall is a poll that mapped no NEW zone, not merely an incomplete
        # one, so slow multi-poll enumeration is never abandoned mid-progress.
        if len(client.zone_aid_map) > zones_mapped_before:
            self._mapping_retry_stall_count = 0
        else:
            self._mapping_retry_stall_count += 1
            if self._mapping_retry_stall_count >= MAPPING_RETRY_STALL_LIMIT:
                unmapped = get_climate_zone_ids(zones_info) - set(client.zone_aid_map)
                _LOGGER.info(
                    "HomeKit: zone mapping still incomplete after %d polls with "
                    "no progress, zone(s) %s have no matching bridge accessory "
                    "and will use cloud-only state",
                    MAPPING_RETRY_STALL_LIMIT, unmapped,
                )

    def _log_cloud_only_zones(self) -> None:
        """Log the climate zones with no HomeKit link, once per change of that set."""
        client = self.homekit_client
        zones_info = (self.data or {}).get("zones_info") or []
        if client is None or not self.is_homekit_active or not zones_info:
            return
        # An empty mapping before the bridge accessory list has been read would name every zone as cloud-only.
        if not client.zone_aid_map and not self._mapping_retry_listed:
            return
        cloud_only = cloud_only_climate_zones(zones_info, client.zone_aid_map)
        ids = frozenset(cloud_only)
        if ids == self._cloud_only_logged:
            return
        self._cloud_only_logged = ids
        if cloud_only:
            minutes = int(self.update_interval.total_seconds() // 60) if self.update_interval else 0
            _LOGGER.info(
                "HomeKit: %s have no HomeKit link and refresh from the Tado cloud only, every %d minute(s)",
                ", ".join(cloud_only.values()), minutes,
            )

    def reset_mapping_retry_budget(self) -> None:
        """Re-arm the mapping-retry stall budget after a reconnect or zone change."""
        self._mapping_retry_stall_count = 0
        self._accessory_list_failing = False

    @property
    def cloud_health(self) -> CloudHealth:
        """The CloudHealth the API client records into."""
        return self.api_client.cloud_health

    @property
    def auth_failed(self) -> bool:
        """Return True while CloudHealth holds an auth failure; the poll then makes no cloud call."""
        return self.cloud_health.auth_failed

    @property
    def polling_paused(self) -> bool:
        """Return True while the quota reserve has paused cloud polling, so no zone fetch can land."""
        return self._was_paused

    def attach_cloud_health(self) -> None:
        """Subscribe the repair-issue listener to the current client's CloudHealth, replacing any earlier one."""
        self.detach_cloud_health()
        self._cloud_health_unsub = self.cloud_health.add_listener(self._on_cloud_health_change)
        self.write_ledger.rebind_health()

    def detach_cloud_health(self) -> None:
        """Stop the repair-issue listener; a later transition creates no issue."""
        unsubscribe, self._cloud_health_unsub = self._cloud_health_unsub, None
        if unsubscribe is not None:
            unsubscribe()

    @callback
    def _on_cloud_health_change(self, change: CloudHealthChange) -> None:
        """Log the cloud entering a problem state or returning to OK and create or dismiss the repair issues for one CloudHealth transition."""
        from .repair_helpers import (
            async_create_auth_issue,
            async_create_blocked_issue,
            async_create_rate_limit_issue,
            async_dismiss_cloud_health_issues,
        )

        if change.current is not CloudHealthState.OK and change.current is not change.previous:
            _LOGGER.info("Coordinator: Tado cloud problem: %s", _CLOUD_STATE_WORDING[change.current])
        elif change.current is CloudHealthState.OK and change.previous is not CloudHealthState.OK:
            _LOGGER.info("Coordinator: Tado cloud reachable again")

        if change.event is CloudEvent.REST_SUCCESS:
            if change.first_success or change.previous is not CloudHealthState.OK:
                async_dismiss_cloud_health_issues(self.hass, self.home_id)
            return
        target = EVENT_STATE[change.event]
        if target is CloudHealthState.BACKOFF:
            async_create_rate_limit_issue(
                self.hass, self.home_id, retry_after=math.ceil(self.cloud_health.backoff_remaining_seconds()),
            )
        elif target is CloudHealthState.BLOCKED:
            if change.blocked_issue_due:
                async_create_blocked_issue(self.hass, self.home_id)
        elif target is CloudHealthState.AUTH_FAILED:
            async_create_auth_issue(self.hass, self.home_id)

    def is_cloud_backoff_active(self) -> bool:
        """Return True while the cloud rate-limit backoff window holds."""
        return self.cloud_health.is_backoff_active()

    def _plan_health_ok(self) -> bool:
        """Return False while CloudHealth is unreachable, blocked or in backoff."""
        # AUTH_FAILED counts healthy here but not in holds(): the poll returns before planning, so no call is made either way.
        return self.cloud_health.state in (CloudHealthState.OK, CloudHealthState.AUTH_FAILED)

    def is_quota_spend_held(self, cost: int = 1, *, essential: bool = False) -> bool:
        """Return True when an automatic cloud call must wait for the quota; essential ones hold only at the floor."""
        ratelimit = self.data_loader.load_ratelimit_file()
        if ratelimit is None:
            held = False
        else:
            levels = QuotaLevels.build(ratelimit, self.config_manager)
            now = dt_util.utcnow()
            held = levels.manual_holds(ratelimit, 1, now) if essential else levels.poll_holds(ratelimit, cost, now)
        if not held:
            return held
        quota_day = (ratelimit or {}).get("last_reset_utc")
        first = essential not in self._quota_hold_logged or self._quota_hold_logged[essential] != quota_day
        self._quota_hold_logged[essential] = quota_day
        if first:
            remaining = (ratelimit or {}).get("remaining")
            if essential:
                _LOGGER.info(
                    "Coordinator: only %s Tado API calls are left, so handing a zone back to Tado is "
                    "holding until the quota is expected to reset",
                    remaining,
                )
            else:
                _LOGGER.info(
                    "Coordinator: %s Tado API calls are left, so Smart Valve Control, Offset Sync, Adaptive Preheat and "
                    "the catch-up zone fetch after a start or a HomeKit reconnect are holding their cloud calls to keep "
                    "the reserve until the quota is expected to reset",
                    remaining,
                )
        return held

    def last_zone_fetch_ts(self) -> datetime | None:
        """Return the start time of the zone-states fetch `self.data` currently carries.

        Advances in the same step that publishes that payload as `self.data`,
        so a reader never sees a newer epoch alongside older zone data. The
        freshness gates compare it against `zone_fetch_epoch_for_write()`.
        """
        return self._last_cloud_zone_fetch

    def last_full_sync_ts(self) -> datetime | None:
        """Return when the last full sync (zones info and devices) ran."""
        return self._last_full_sync

    def observe_data_ages(self) -> None:
        """Sample the age of each cloud-only clock that is set into the freshness gauge."""
        now = dt_util.utcnow()
        for clock, stamp in (("zone_states", self.last_zone_fetch_ts()), ("full_sync", self.last_full_sync_ts())):
            if not isinstance(stamp, datetime):
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=dt_util.UTC)
            self.mechanism_counters.observe_age(clock, max(0.0, (now - stamp).total_seconds() / 60))

    def record_own_power_write(self, zone_id: str, *, powered_off: bool) -> None:
        """Remember an accepted OFF write for the zone; an accepted ON write or AUTO forgets it."""
        self.write_ledger.note_own_power(zone_id, powered_off=powered_off)

    def own_off_mark(self, zone_id: str) -> bool:
        """Whether an own-OFF mark exists for the zone; unlike `has_own_off_write` it never pops a stale one."""
        return self.write_ledger.own_off_mark(zone_id)

    def has_own_off_write(self, zone_id: str) -> bool:
        """Whether our own accepted OFF write is still newer than any zone fetch that could show it."""
        return self.write_ledger.own_off_pending(zone_id)

    def zone_data_covers_own_write(self, zone_id: str) -> bool:
        """Whether `self.data` comes from a fetch that started past the cloud lag after the zone's last write (True when never written)."""
        stamp = self.state_reconciler.last_write_stamp(zone_id) if self.state_reconciler is not None else None
        if stamp is None:
            return True
        return fetch_covers_write(
            self.last_zone_fetch_ts(), stamp, get_homekit_cloud_lag_seconds(self.hass, self.config_entry.entry_id),
        )

    def zone_fetch_epoch_for_write(self) -> datetime | None:
        """Return the epoch a write should record for later freshness checks.

        Set before the zone-states request goes out, so a write landing while a
        sync is in flight records that sync's epoch: its payload may predate
        the write, so it must not count as fresher once it lands.
        """
        return self._zone_fetch_started_at

    def request_forced_fetch(self, fetch_type: str) -> None:
        """Force the next poll to bypass the floor for one slow-data type."""
        self.refresh_handler.request_forced_fetch(fetch_type)

    def request_forced_zone_fetch(self) -> None:
        """Force the next poll to fetch zone states even inside the HomeKit skip window."""
        self.refresh_handler.request_forced_zone_fetch()

    async def async_wait_for_poll_end(self) -> None:
        """Return once no poll is running, so a refresh request after it is not dropped by HA's Debouncer."""
        async with self._debounced_refresh.async_lock():
            pass

    def refresh_plan_cost(self) -> int:
        """Return the calls the poll a write-triggered refresh would run now must make."""
        try:
            inputs = self._plan_inputs(dt_util.utcnow(), self.is_homekit_active, force_zone_fetch=True, forced_types=frozenset())
            return build_plan(inputs).must_cost
        except Exception:
            # WHY: a price read must never break a caller; one call is the cost of the cheapest refresh.
            _LOGGER.debug("Coordinator: refresh cost unreadable, assuming 1 call", exc_info=True)
            return 1

    async def async_request_zone_fetch(self, reason: str) -> bool:
        """Ask the refresh handler for a forced zone fetch (quota-gated, spaced, debounced); True when one was requested, a skipped request leaves the flag for the next poll."""
        if self.refresh_handler is None:
            return False
        # WHY: the first poll fetches the zones itself, so a drop or reconnect before it returns has nothing to catch up on.
        if reason != "cold_start_retry" and self.data is None:
            _LOGGER.debug("Coordinator: skipping forced zone fetch (%s), the first poll has not returned", reason)
            return False
        now = dt_util.utcnow()
        recent = [t for t in (self._last_cloud_zone_fetch, self._last_zone_fetch_request) if t is not None]
        if self.is_quota_spend_held():
            self.refresh_handler.request_forced_zone_fetch()
            _LOGGER.debug("Coordinator: quota held (%s), zone fetch kept for the next poll", reason)
            return False
        # The spacing damps flapping reconnects; the single catch-up after a start and the one fetch a waiting resend or an owed change needs are exempt.
        if reason not in ("cold_start_retry", "resend_recovery", "owed_write") and recent and (now - max(recent)).total_seconds() < HOMEKIT_DROP_FETCH_MIN_GAP_SECONDS:
            self.refresh_handler.request_forced_zone_fetch()
            _LOGGER.debug("Coordinator: skipping forced zone fetch (%s), fetched recently, zone fetch kept for the next poll", reason)
            return False
        if await self.refresh_handler.trigger_refresh("homekit.bridge", reason, force=True):
            self._last_zone_fetch_request = now
            return True
        return False

    def _requeue(self, plan: FetchPlan, outcome: PollOutcome, satisfied: frozenset[str] = frozenset()) -> None:
        """Put back the fetch flags this poll consumed and did not use; a no-op once the entry is unloading."""
        if self.refresh_handler is None or self._shutting_down:
            _LOGGER.debug("Coordinator: not requeueing the pending fetches (%s), the entry is unloading", outcome)
            return
        cm = self.config_manager
        enabled = {
            FORCEABLE_FETCH_WEATHER: cm.get_weather_enabled(),
            FORCEABLE_FETCH_HOME_STATE: cm.get_home_state_sync_enabled(),
            FORCEABLE_FETCH_MOBILE: cm.get_mobile_devices_enabled() and cm.get_mobile_devices_frequent_sync(),
        }
        self.refresh_handler.requeue(
            plan, outcome, satisfied=satisfied, enabled_types=frozenset(t for t, on in enabled.items() if on),
        )
        _LOGGER.debug("Coordinator: poll ended %s, put back zone=%s forced=%s", outcome, plan.force_zone, sorted(plan.forced_types))

    def _note_skip(self, item: str, reason: str, calls: int = 1) -> None:
        """Record one skipped fetch of the current poll and count it."""
        self.last_poll_skips[item] = reason
        self.last_poll_skips_at = dt_util.utcnow()
        self.mechanism_counters.count(MECH_POLL_SKIP, f"{item}:{reason}", calls=calls)

    @callback
    def note_homekit_change(self, zone_id: str | None, changed_at: datetime | None) -> None:
        """Ask for one spaced zone fetch when a valve zone changed over HomeKit and the change is not ours; off by default."""
        # Order: flag, malformed event, valve-only zone, then the ledger; the flag is first so the default path reads nothing.
        if not DEMAND_FETCH_ON_FOREIGN_CHANGE or not zone_id or changed_at is None:
            return
        if not zone_qualifies_for_homekit_power(self, zone_id) or self.write_ledger.is_ours(zone_id, changed_at):
            return
        _LOGGER.debug("Coordinator: zone %s changed over HomeKit and not by us, requesting a zone fetch", zone_id)
        self.hass.async_create_task(self.async_request_zone_fetch("homekit_foreign_change"))

    def record_homekit_put_rejection(self, zone_id: str, hap_status: int, burst_position: int) -> None:
        """Count a -70402 put rejection by zone and burst position (capped at 5)."""
        if hap_status == -70402:
            self.mechanism_counters.count(MECH_HK_PUT_70402, f"{zone_id}:{min(burst_position, 5)}", calls=0)

    def _note_pause(self, reason: str, calls: int) -> None:
        """Count one paused poll, publish its reason and persist the count, as no paused path reaches post-sync."""
        self.mechanism_counters.count(MECH_POLL_PAUSE, reason, calls=calls)
        self.last_poll_skips = {"poll": reason}
        self.last_poll_skips_at = dt_util.utcnow()
        self.flush_mechanism_counters()

    def _zone_facts(self) -> tuple[ZoneFact, ...] | None:
        """The stored zone list as plan facts, or None when it is missing or malformed."""
        stored = self.data_loader.get_cached("zones_info")
        if not isinstance(stored, list) or not all(isinstance(z, dict) for z in stored):
            return None
        facts = []
        for zone in stored:
            devices = zone.get("devices")
            has_serial = isinstance(devices, list) and any(isinstance(d, dict) and d.get("shortSerialNo") for d in devices)
            facts.append(ZoneFact(str(zone.get("id")), str(zone.get("type") or ""), has_serial))
        return tuple(facts)

    def _unlisted_zone_count(self) -> int:
        """Count zones in the stored zone states that the stored zone list does not list yet."""
        stored = self.data_loader.get_cached("zones_info")
        states = self.data_loader.get_cached("zones")
        zone_states = states.get("zoneStates") if isinstance(states, dict) else None
        if not isinstance(stored, list) or not isinstance(zone_states, dict):
            return 0
        listed = {str(z.get("id")) for z in stored if isinstance(z, dict)}
        return sum(1 for zone_id in zone_states if str(zone_id) not in listed)

    def _cached_capability_ids(self) -> frozenset[str]:
        """The zone ids that already have stored capabilities."""
        capabilities = self.data_loader.get_cached("zone_capabilities")
        return frozenset(str(k) for k in capabilities) if isinstance(capabilities, dict) else frozenset()

    def _cached_early_start_ids(self) -> frozenset[str]:
        """The zone ids that already have a stored Early Start value."""
        early_start = self.data_loader.get_cached("early_start")
        return frozenset(str(k) for k in early_start) if isinstance(early_start, dict) else frozenset()

    def _plan_inputs(
        self, now: datetime, homekit_connected: bool, force_zone_fetch: bool, forced_types: frozenset[str],
    ) -> PlanInputs:
        """Gather everything the builder reads, once, at the start of a poll."""
        cm = self.config_manager
        interval_s = self.update_interval.total_seconds() if self.update_interval else 0.0
        return PlanInputs(
            now=now,
            config=PlanConfig.from_config_manager(cm),
            homekit_connected=homekit_connected,
            user_has_custom_interval=has_custom_interval(cm),
            dial_minutes=cm.get_homekit_cloud_sync_minutes(),
            full_sync_period_s=QuotaLevels.build(self.data_loader.load_ratelimit_file(), cm).full_sync_period,
            last_zone_fetch=self._last_cloud_zone_fetch,
            last_full_sync=self._last_full_sync,
            last_weather=self._last_weather_fetch,
            last_home_state=self._last_home_state_fetch,
            last_mobile=self._last_mobile_devices_fetch,
            last_offset_resync=self._last_offset_resync,
            request_full_sync=self._request_full_sync_next_cycle,
            force_zone_fetch=force_zone_fetch,
            forced_types=forced_types,
            zones_info=self._zone_facts(),
            cached_capability_ids=self._cached_capability_ids(),
            cached_early_start_ids=self._cached_early_start_ids(),
            unlisted_zone_count=self._unlisted_zone_count(),
            svc_zone_ids=svc_schedule_zone_ids(self.zone_config_manager, self.hass),
            gaps=dict(self._gaps),
            partial=self._full_sync_partial,
            poll_interval_s=interval_s if interval_s > 0 else DEFAULT_DAY_INTERVAL * 60.0,
            first_load_pending=self.first_load_pending_count(),
            health_ok=self._plan_health_ok(),
        )

    def first_load_pending_count(self) -> int:
        """Number of Offset Sync controllers whose first offset read is still owed."""
        return sum(1 for ctrl in self.offset_sync_controllers.values() if ctrl.is_first_load_pending)

    def post_sync_allows(self, kind: str, calls: int = 1) -> bool:
        """True when a post-sync style spend of this many calls may run now on the quota as it stands, or when the quota is unknown."""
        if kind == KIND_OFFSETS_FIRST_LOAD and self.is_cloud_backoff_active():
            return False
        self._load_ratelimit_from_cache()
        if snapshot_shape(self._cached_ratelimit) is not None:
            return True
        try:
            allowed = not self._would_pause(calls)
        except (TypeError, ValueError, ArithmeticError):
            # WHY: a damaged quota file must not stop a read the gate only prices; fail open like an absent snapshot.
            _LOGGER.debug("Coordinator: %s quota snapshot unreadable, allowing it", kind, exc_info=True)
            return True
        if not allowed:
            _LOGGER.debug("Coordinator: %s held back by the quota gate", kind)
        return allowed

    def _would_pause(self, cost: int) -> bool:
        """True when spending this many calls would pause polling on the current snapshot."""
        return should_pause_polling(self._cached_ratelimit or {}, self.config_manager, poll_cost=cost)[0]

    def _drift_allowed(self, snapshot: dict[str, Any] | None) -> bool:
        """False when the tier or the quota rules out the drift refresh."""
        remaining = snapshot.get("remaining") if isinstance(snapshot, dict) else None
        return QuotaLevels.build(snapshot, self.config_manager).drift_allowed(remaining)

    def _note_plan_skips(self, plan: FetchPlan) -> None:
        """Record why each enabled item is left out of this poll."""
        for kind, reason in plan.skipped:
            if reason != "disabled":
                self._note_skip(_SKIP_ITEM_NAMES.get(kind, kind), reason)
        cm = self.config_manager
        mobile_floored = (
            cm.get_mobile_devices_enabled() and cm.get_mobile_devices_frequent_sync()
            and not plan.has(KIND_MOBILE_QUICK) and not plan.has(KIND_MOBILE_FULL)
            and not any(kind == KIND_MOBILE_QUICK for kind, _ in plan.skipped)
        )
        if mobile_floored:
            self._note_skip("mobile_devices", "floor")

    def _note_full_sync_deferred(self, before: FetchPlan, after: FetchPlan, snapshot: dict[str, Any] | None) -> None:
        """Log once per episode and count a full sync the shed removed from the plan."""
        if not any(reason == SHED_FULL_SYNC_DEFERRED for _, reason in after.shed):
            return
        ratelimit = snapshot or {}
        if not self._full_sync_deferred:
            self._full_sync_deferred = True
            _LOGGER.info(
                "Coordinator: the next full refresh of zone and device details could use about %d of the %s "
                "API calls left, which would dip into the last %d calls kept back for manual changes. "
                "Tado CE will run regular polls only and do the full refresh once the quota allows, "
                "at the latest after the daily reset",
                before.must_cost + before.full_cost, ratelimit.get("remaining"), hard_quota_reserve(ratelimit.get("limit")),
            )
        self._note_skip("full_sync", SHED_FULL_SYNC_DEFERRED, calls=before.full_cost)

    def _apply_stamps(self, stamps: StampUpdates) -> None:
        """Store the fetch times of the items that returned data."""
        if stamps.weather is not None:
            self._last_weather_fetch = stamps.weather
        if stamps.home_state is not None:
            self._last_home_state_fetch = stamps.home_state
        if stamps.mobile is not None:
            self._last_mobile_devices_fetch = stamps.mobile
        if stamps.full_sync is not None:
            self._last_full_sync = stamps.full_sync
        if stamps.offset_resync is not None:
            self._last_offset_resync = stamps.offset_resync
        if stamps.clear_request_full_sync:
            self._request_full_sync_next_cycle = False
            self._full_sync_deferred = False

    def _apply_gaps(self, stamps: StampUpdates, now: datetime, poll_interval_s: float) -> None:
        """Clear the gaps of items that returned data and open or widen those that did not."""
        for kind in stamps.gap_ok:
            self._gaps.pop(kind, None)
        for kind, cap_s in stamps.gap_fail.items():
            gap = gap_after_failure(self._gaps.get(kind), now=now, poll_interval_s=poll_interval_s, cap_s=cap_s)
            self._gaps[kind] = gap
            _LOGGER.debug("Coordinator: %s returned nothing (%d in a row), next try %s", kind, gap.fails, gap.next_retry_at)
        # WHY: a poll without the full-sync group returns no partial list, so the owed sub-items stay.
        if GAP_PARTIAL in stamps.gap_ok or GAP_PARTIAL in stamps.gap_fail:
            self._full_sync_partial = stamps.partial

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Tado API. Dynamically adjusts update_interval."""
        # First, so a poll that ends early (sign-in expired) still ages out the changes held over HomeKit.
        self.write_ledger.sweep(dt_util.utcnow())
        if self.auth_failed:
            # WHY: held data is only live while HomeKit feeds it; without HomeKit the user must sign in again.
            if not self._homekit_can_keep_entities_live:
                raise ConfigEntryAuthFailed("Tado sign-in expired and HomeKit cannot keep the entities live")
            return self._reapply_trust_to_held_data()
        was_failing = self.last_update_success is False
        self.last_poll_skips = {}

        self._load_ratelimit_from_cache()

        homekit_connected = self.is_homekit_active

        force_zone_fetch, forced_fetch = self.refresh_handler.consume_pending_flags()

        cm = self.config_manager

        inputs = self._plan_inputs(dt_util.utcnow(), homekit_connected, force_zone_fetch, frozenset(forced_fetch))
        plan = build_plan(inputs)
        quick_cost = plan.must_cost
        self._poll_plan = plan

        if self._cached_ratelimit:
            should_pause, reason = should_pause_polling(
                self._cached_ratelimit,
                self.config_manager,
                poll_cost=quick_cost,
                was_paused=self._was_paused,
                can_keep_entities_live=bool(self.data) or self._homekit_can_start_on_saved_data,
            )
            if should_pause:
                _LOGGER.warning("Coordinator: %s", reason)
                self._note_pause("over_reserve", quick_cost)
                self._was_paused = True
                # WHY: a backoff window already set the interval to its end.
                if not self.is_cloud_backoff_active():
                    self.update_interval = timedelta(minutes=15)
                # The plan above consumed the forced requests; a paused poll fetched nothing.
                self._requeue(plan, PollOutcome.PAUSE)
                # WHY: the reserve exists for manual actions, and Home Assistant silently skips unavailable entities.
                if self.data:
                    return self._reapply_trust_to_held_data()
                if self._homekit_can_start_on_saved_data:
                    return await self._async_finish_poll(None)
                # retry_after lets HA defer the next refresh precisely.
                reset_seconds = self._cached_ratelimit.get("reset_seconds")
                retry_after = _sanitize_retry_after(reset_seconds)
                _LOGGER.debug(
                    "Coordinator: rate-limited, deferring next refresh by %ds",
                    retry_after,
                )
                raise UpdateFailed(reason, retry_after=retry_after)
            self._was_paused = False

        shed_from = plan
        plan = shed_plan(
            plan, snapshot=self._cached_ratelimit, would_pause=self._would_pause,
            drift_allowed=self._drift_allowed(self._cached_ratelimit),
        )
        self._note_full_sync_deferred(shed_from, plan, self._cached_ratelimit)
        self._note_plan_skips(plan)
        self._poll_plan = plan
        do_full_sync = plan.has(KIND_ZONES_INFO)

        if homekit_connected != self._prev_homekit_connected:
            if homekit_connected:
                _LOGGER.info(
                    "Coordinator: HomeKit connected, reducing cloud "
                    "polling frequency in favour of local reads",
                )
            else:
                _LOGGER.info(
                    "Coordinator: HomeKit disconnected, resuming full "
                    "cloud polling",
                )
            self._prev_homekit_connected = homekit_connected

        fetches_zone_states = plan.has(KIND_ZONE_STATES)
        zone_fetch_started = dt_util.utcnow() if fetches_zone_states else None
        fetch_start_mono = time.monotonic() if fetches_zone_states else None
        if zone_fetch_started is not None:
            self._zone_fetch_started_at = zone_fetch_started

        try:
            results = await self.api_client.async_sync(plan)
        except TadoRateLimitError as e:
            self._requeue(plan, PollOutcome.RATE_LIMIT)
            # WHY: whole seconds, rounded up, so the probe never fires just before the window ends.
            wait = float(math.ceil(max(self.cloud_health.backoff_remaining_seconds(), 60.0)))

            kept = await self._async_keep_live_on_cloud_failure()
            if kept is not None:
                self.update_interval = timedelta(seconds=wait)
                return kept

            raise UpdateFailed(
                f"Tado API rate-limited, retry after {max(1, math.ceil(wait / 60))} min",
                retry_after=wait,
            ) from e
        except TadoAuthError as e:
            self._requeue(plan, PollOutcome.AUTH)
            kept = await self._async_keep_live_on_cloud_failure()
            if kept is None:
                raise ConfigEntryAuthFailed(
                    "Refresh token expired; user must re-authenticate",
                ) from e
            self.config_entry.async_start_reauth(self.hass)
            return kept
        except TadoBlockedError as e:
            self._requeue(plan, PollOutcome.BLOCKED)
            kept = await self._async_keep_live_on_cloud_failure()
            if kept is not None:
                return kept
            raise UpdateFailed("Tado is blocking requests") from e
        except TadoSyncError as e:
            self._requeue(plan, PollOutcome.SYNC_ERROR)
            kept = await self._async_keep_live_on_cloud_failure(retry_on_snapshot=True)
            if kept is not None:
                return kept
            raise UpdateFailed(f"Tado CE sync failed: {e}") from e
        except Exception:
            # Broad on purpose: any unexpected sync failure must not lose the pending flags; it is logged and re-raised.
            _LOGGER.warning("Coordinator: unexpected error during the sync, putting back the pending fetches")
            self._requeue(plan, PollOutcome.UNEXPECTED)
            raise
        finally:
            self._apply_polling_interval(homekit_connected)

        # Stamp the time the sync finished, as before, so the floors keep their phase.
        finished = dt_util.utcnow()
        stamps = apply_results(plan, results, replace(inputs, now=finished))
        self._apply_stamps(stamps)
        self._apply_gaps(stamps, finished, inputs.poll_interval_s)
        self._requeue(plan, PollOutcome.SUCCESS, satisfied=stamps.satisfied_forced)

        rl_data = self.data_loader.get_cached("ratelimit")
        if isinstance(rl_data, dict):
            self._zero_savings_on_quota_reset(rl_data)

        if not fetches_zone_states:
            self.record_homekit_read_saved()

        if was_failing:
            _LOGGER.info(
                "Coordinator: connection restored, first poll after "
                "previous failure succeeded",
            )

        # One-line poll summary so users grepping the debug log can
        # see at a glance what each cycle fetched / skipped.
        _LOGGER.debug(
            "Coordinator: poll cycle, zones=%s weather=%s homekit=%s "
            "interval=%dm full=%s",
            "fetch" if fetches_zone_states else "skip",
            "fetch" if plan.has(KIND_WEATHER) else ("skip" if cm.get_weather_enabled() else "off"),
            "yes" if homekit_connected else "no",
            int(self.update_interval.total_seconds() // 60) if self.update_interval else 0,
            "yes" if do_full_sync else "no",
        )

        return await self._async_finish_poll(zone_fetch_started, fetch_start_mono)

    async def _async_finish_poll(
        self, zone_fetch_started: datetime | None, fetch_start_mono: float | None = None,
    ) -> dict[str, Any]:
        """Turn the cached zone and weather data into the published result (zone delta, prunes, post-sync)."""
        zone_data = self.data_loader.get_cached("zones")
        weather_data = self.data_loader.get_cached("weather")

        zone_states = (
            zone_data.get("zoneStates") if isinstance(zone_data, dict) else None
        )
        delta = self._zone_fingerprint.update(zone_states)
        if (
            not delta.is_first_poll
            and not delta.is_empty_response
            and (delta.added or delta.removed)
        ):
            _LOGGER.info(
                "Zone topology changed: added=%s removed=%s",
                sorted(delta.added), sorted(delta.removed),
            )
            await self._handle_zone_delta(delta)

        await self._prune_zone_type_change_schedules()
        await self._prune_reassigned_zones()

        return await self._async_post_sync_processing(
            zone_data, weather_data, zone_fetch_started=zone_fetch_started, fetch_start_mono=fetch_start_mono,
        )

    async def _async_run_post_sync_plan(
        self, zones_info: dict[str, Any] | list[Any] | None, ratelimit_snapshot: dict[str, Any] | None,
    ) -> None:
        """Plan and run the post-sync spends the sync just made due: the offset drift refresh and the capabilities refetch."""
        # WHY: the hard-reserve gate prices against the quota the sync left, not the one the poll started with.
        self._load_ratelimit_from_cache()
        poll_plan = self._poll_plan
        # WHY: one HomeKit read per poll; only a post-sync run with no poll plan reads it fresh.
        homekit_connected = poll_plan.homekit_connected if poll_plan is not None else self.is_homekit_active
        changed, fresh_fp, prev_fp = self._changed_capability_zones(zones_info)
        inputs = replace(
            self._plan_inputs(dt_util.utcnow(), homekit_connected, False, frozenset()),
            caps_changed=frozenset(changed),
        )
        plan = poll_plan if poll_plan is not None else FetchPlan(homekit_connected=homekit_connected)
        fresh = self.data_loader.get_cached("ratelimit")
        post_plan = gate_post_plan(
            build_post_plan(inputs, plan),
            snapshot=fresh if isinstance(fresh, dict) else None,
            would_pause=self._would_pause,
            drift_allowed=self._drift_allowed(ratelimit_snapshot),
        )
        if poll_plan is not None and poll_plan.probe_only:
            post_plan = post_plan.without(tuple(i.kind for i in post_plan.items), reason=SKIP_DEFERRED_FOR_PROBE)
        elif self.cloud_health.holds():
            # WHY: a refusal during this very poll (429, blocked, unreachable) holds the cloud after the plan was built, so the plan was not a probe.
            post_plan = post_plan.without(tuple(i.kind for i in post_plan.items), reason=SKIP_DEFERRED_FOR_BACKOFF)
        self._last_post_plan = post_plan
        for kind, reason in (*post_plan.skipped, *post_plan.shed):
            self._note_skip(kind, reason)
        if not changed:
            self._gaps.pop(KIND_CAPABILITIES_FORCED, None)
        if not isinstance(zones_info, list):
            return
        if post_plan.has(KIND_OFFSETS_DRIFT):
            tally = await self._run_offsets_drift(zones_info)
            if tally.obtained > 0:
                self._last_offset_resync = dt_util.utcnow()
            else:
                _LOGGER.debug("Offset Sync: drift refresh returned no offset, trying the next zone on the next due cycle")
        refreshed: set[str] = set()
        if post_plan.has(KIND_CAPABILITIES_FORCED):
            # WHY: not marking the cache dirty keeps unchanged zones' capabilities as the merge base.
            _LOGGER.info(
                "Zone capabilities: device change detected for zone(s) %s, refreshing from cloud", sorted(changed),
            )
            try:
                tally = await self.api_client._sync_zone_capabilities(zones_info, force_zone_ids=changed)
            except (TadoAuthError, TadoRateLimitError, TadoBlockedError) as e:
                self._recover_from_cloud_error(e)
                _LOGGER.debug("Zone capabilities: refetch hit %s, recovery started, retrying on the gap", type(e).__name__)
                refreshed = set()
            else:
                refreshed = set(tally.obtained_ids)
            self._record_capabilities_gap(changed - refreshed, inputs)
        await self._write_capability_fingerprints(changed, refreshed, fresh_fp, prev_fp)

    def _record_capabilities_gap(self, failed: set[str], inputs: PlanInputs) -> None:
        """Open or widen the retry gap when zones stayed unrefreshed, clear it when the refetch landed fully."""
        if not failed:
            self._gaps.pop(KIND_CAPABILITIES_FORCED, None)
            return
        cap_s = gap_cap_seconds(
            KIND_CAPABILITIES_FORCED, floor_s=0.0, poll_interval_s=inputs.poll_interval_s,
            full_sync_period_s=inputs.full_sync_period_s,
        )
        gap = gap_after_failure(
            self._gaps.get(KIND_CAPABILITIES_FORCED), now=dt_util.utcnow(),
            poll_interval_s=inputs.poll_interval_s, cap_s=cap_s,
        )
        self._gaps[KIND_CAPABILITIES_FORCED] = gap
        _LOGGER.debug(
            "Zone capabilities: refetch of zone(s) %s returned nothing (%d in a row), next try %s",
            sorted(failed), gap.fails, gap.next_retry_at,
        )

    def _changed_capability_zones(
        self, zones_info: object,
    ) -> tuple[set[str], dict[str, list[list[str]]], dict[str, Any]]:
        """Zones whose device fingerprint differs from the stored baseline, the fresh map and that baseline; empty on odd input."""
        if not isinstance(zones_info, list):
            return set(), {}, {}
        prev_raw = self.data_loader.get_cached("zone_capabilities_fp")
        prev_fp = dict(prev_raw) if isinstance(prev_raw, dict) else {}
        changed, fresh_fp = zone_device_fingerprints_changed(zones_info, prev_fp)
        return changed, fresh_fp, prev_fp

    async def _write_capability_fingerprints(
        self, changed: set[str], refreshed: set[str], fresh_fp: dict[str, list[list[str]]], prev_fp: dict[str, Any],
    ) -> None:
        """Store the fresh fingerprints; a changed zone keeps its old one until its capabilities landed."""
        to_save = {zone_id: prev_fp[zone_id] if zone_id in changed - refreshed else fp for zone_id, fp in fresh_fp.items()}
        updates = {zone_id: fp for zone_id, fp in to_save.items() if prev_fp.get(zone_id) != fp}
        removed = [zone_id for zone_id in prev_fp if zone_id not in to_save]
        if updates or removed:
            await self.data_loader.async_merge_store("zone_capabilities_fp", updates, removed)

    def _recover_from_cloud_error(self, exc: TadoAuthError | TadoRateLimitError | TadoBlockedError) -> None:
        """Start the recovery for a typed cloud error caught outside the poll's own arms; HA de-duplicates an open reauth flow."""
        from .error_dispatch import _apply_cloud_error_recovery

        _apply_cloud_error_recovery(exc, self.config_entry, self.hass)

    async def _run_offsets_drift(self, zones_info_data: list[Any]) -> FetchTally:
        """Re-read the offsets of the next heating zone in the round-robin and tally what came back."""
        # One heating zone per cycle keeps an N-zone home at 1 call a cycle; the cursor moves to the zone tried unless the read raised.
        heating_ids = sorted(
            str(z.get("id"))
            for z in zones_info_data
            if z.get("type") == "HEATING"
        )
        if not heating_ids:
            return FetchTally(0, 0)
        target = self._next_round_robin_zone(heating_ids)

        try:
            tally = await self.api_client.async_resync_offsets(
                zones_info_data, target_zone_id=target,
            )
        except (TadoAuthError, TadoRateLimitError, TadoBlockedError) as e:
            self._recover_from_cloud_error(e)
            _LOGGER.debug(
                "Offset Sync: drift refresh hit %s, recovery started, "
                "readback gate still protects the cache",
                type(e).__name__,
            )
            return FetchTally(0, 0)
        except TadoSyncError:
            # Transient sync error on a best-effort refresh: swallow and retry
            # next poll. Programmer bugs (KeyError / AttributeError / …) are
            # deliberately not caught, so they propagate and HA logs them.
            _LOGGER.debug(
                "Offset Sync: drift refresh fetch failed, readback gate "
                "still protects the cache, will retry next poll",
                exc_info=True,
            )
            return FetchTally(0, 0)

        self._last_refreshed_zone_id = target
        _LOGGER.debug(
            "Offset Sync: drift refresh of zone %s used %s cloud call(s), %s offset(s) obtained",
            target, tally.calls, tally.obtained,
        )
        return tally

    def _next_round_robin_zone(self, heating_ids: list[str]) -> str:
        """Pick the next zone_id after the cursor in sorted order, wrapping.

        Self-heals (returns the list head) when the cursor zone is gone
        (zone removed / re-paired), so the round-robin never skips or
        crashes on a topology change.
        """
        last = self._last_refreshed_zone_id
        if last is None or last not in heating_ids:
            return heating_ids[0]
        idx = heating_ids.index(last)
        return heating_ids[(idx + 1) % len(heating_ids)]

    async def _accumulate_outdoor_temp_history(
        self, weather_data: dict[str, Any] | list[Any] | None,
    ) -> None:
        """Append the latest outdoor reading to the history ring buffer and persist."""
        if not weather_data:
            return
        outdoor_temp = (weather_data.get("outsideTemperature") or {}).get("celsius")  # type: ignore[union-attr]
        if outdoor_temp is None:
            return

        prev_last = self._outdoor_temp_history[-1] if self._outdoor_temp_history else None

        self._outdoor_temp_history.append(outdoor_temp)
        if len(self._outdoor_temp_history) > OUTDOOR_TEMP_HISTORY_MAX:
            del self._outdoor_temp_history[: len(self._outdoor_temp_history) - OUTDOOR_TEMP_HISTORY_MAX]

        # Only schedule Store write when the new reading differs from the previous
        if outdoor_temp != prev_last:
            self.data_loader.save_outdoor_temp_history(self._outdoor_temp_history)

    async def _notify_heating_cycle_updates(
        self,
        zone_data: dict[str, Any] | list[Any] | None,
        *,
        zone_fetch_started: datetime | None = None,
        zones_info: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        """Feed each zone's target and current temperature to the heating-cycle snapshot, and to its detector for zones that are not HEATING."""
        if not self.heating_cycle_coordinator or not zone_data:
            return
        # Tado API may return null for 'zoneStates'; 'or {}' handles None correctly
        zone_states = zone_data.get("zoneStates") or {}  # type: ignore[union-attr]
        zone_list = zones_info if isinstance(zones_info, list) else []
        heating_ids = {str(z.get("id")) for z in zone_list if isinstance(z, dict) and z.get("type") == "HEATING"}
        for zone_id, data in zone_states.items():
            try:
                setting = data.get("setting") or {}
                target_temp = (setting.get("temperature") or {}).get("celsius")
                sensor_data = data.get("sensorDataPoints") or {}
                current_temp = (sensor_data.get("insideTemperature") or {}).get("celsius")
                if current_temp is None:
                    self.heating_cycle_coordinator.clear_zone_state(zone_id, source="cloud")
                    continue
            except (KeyError, TypeError, ValueError):
                _LOGGER.debug(
                    "Heating Cycle: zone %s temperature update could not "
                    "be parsed, skipping this tick",
                    zone_id,
                )
                continue
            if target_temp is None:
                continue
            # Outside the parse guard: a wrong keyword must raise, not be swallowed per zone.
            fed = target_temp
            if str(zone_id) in heating_ids:
                # The view reads power, target, epoch and the class gate from this fetch; this runs before the new data is published.
                view = zone_view(
                    self, str(zone_id), zone_data=data, fetch_epoch=zone_fetch_started, zones_info=zone_list,
                )
                resolved, _ = zone_view_target(self, view)
                fed = resolved if resolved is not None else target_temp
            # A HEATING zone's cycles come from its climate entity, so two temperature sources never share one cycle.
            try:
                await self.heating_cycle_coordinator.on_zone_update(
                    zone_id,
                    fed,
                    current_temp,
                    source="cloud",
                    track_cycle=str(zone_id) not in heating_ids,
                )
            except (KeyError, TypeError, ValueError):
                _LOGGER.debug(
                    "Heating Cycle: zone %s temperature update could not "
                    "be parsed, skipping this tick",
                    zone_id,
                )

    async def _handle_state_restore_updates(
        self,
        home_state_data: dict[str, Any] | list[Any] | None,
        result: dict[str, Any],
    ) -> None:
        """Clear captures on Away → Home and forward the poll result for timer-expiry detection."""
        if not self._sr_manager:
            return
        old_home_state = self.data.get("home_state", {}) if self.data else {}
        old_presence = old_home_state.get("presence")
        _hsd = home_state_data if isinstance(home_state_data, dict) else {}
        new_presence = _hsd.get("presence")
        if old_presence == "AWAY" and new_presence == "HOME":
            await self._sr_manager.clear_all()
            _LOGGER.info(
                "State Restore: home returned from Away, cleared all "
                "captured states so the Home schedule resumes cleanly",
            )

        self._sr_manager.on_poll_update(result)

    @property
    def full_sync_extra_calls_per_cycle(self) -> int:
        """Return the calls one full sync costs on the configured features, for the interval maths."""
        cm = self.config_manager
        # per_full_sync does not depend on HomeKit, so False is fine here.
        return steady_state_cost(
            PlanConfig.from_config_manager(cm),
            homekit_connected=False,
            user_has_custom_interval=has_custom_interval(cm),
            zones_info=self._zone_facts(),
            cached_capability_ids=self._cached_capability_ids(),
            cached_early_start_ids=self._cached_early_start_ids(),
            unlisted_zone_count=self._unlisted_zone_count(),
        ).per_full_sync

    async def _async_fetch_bridge_data(self) -> dict[str, object] | None:
        """Fetch bridge API data with health tracking."""
        options = self.config_entry.options
        bridge_serial = options.get("bridge_serial", "")
        bridge_auth_key = options.get("bridge_auth_key", "")

        if not bridge_serial or not bridge_auth_key or not self.config_manager.get_bridge_enabled():
            self.bridge_api_client = None
            self.bridge_health_tracker = None
            return None

        # Lazy-init health tracker when bridge credentials present
        if self.bridge_health_tracker is None:
            from .bridge_health import BridgeHealthTracker as _BridgeHealthTracker

            raw = await self.data_loader.async_load_bridge_health()
            if raw and isinstance(raw, dict):
                self.bridge_health_tracker = _BridgeHealthTracker.from_dict(raw)
            else:
                self.bridge_health_tracker = _BridgeHealthTracker()

        # Lazy-init (or re-init if credentials changed)
        if self.bridge_api_client is None:
            from homeassistant.helpers.aiohttp_client import (
                async_get_clientsession,
            )

            from .bridge_api import TadoBridgeApiClient

            session = async_get_clientsession(self.hass)
            self.bridge_api_client = TadoBridgeApiClient(
                session, bridge_serial, bridge_auth_key, counters=self.mechanism_counters,
            )
            _LOGGER.debug(
                "Bridge: API client initialised for serial %s…",
                mask_serial(bridge_serial),
            )

        try:
            start_time = time.monotonic()
            result = await self.bridge_api_client.async_get_wiring_state()
            elapsed_ms = (time.monotonic() - start_time) * 1000
            self.bridge_health_tracker.record_success(elapsed_ms)
            self.data_loader.save_bridge_health(self.bridge_health_tracker.to_dict())

            if not self._bridge_first_fetch_logged:
                from .bridge_discovery import flatten_response

                fields = flatten_response(result)
                _LOGGER.debug(
                    "Bridge: first fetch OK (%.0fms, %d field(s))",
                    elapsed_ms, len(fields),
                )
                self._bridge_first_fetch_logged = True

            if "boilerMaxOutputTemperatureInCelsius" in result:
                return result

            from .bridge_discovery import extract_capabilities, flatten_response

            # OpenTherm bridges report the setting only on their own endpoint, not in the wiring state
            if not extract_capabilities(flatten_response(result)).has_output_temperature:
                return result
            try:
                max_output = await self.bridge_api_client.async_get_max_output_temperature()
            except TadoBridgeApiError as err:
                _LOGGER.debug("Bridge: max output temperature read failed (%s), keeping the previous value", err)
                previous = (self._cached_bridge_data or {}).get("boilerMaxOutputTemperatureInCelsius")
                if previous is None:
                    return result
                return {**result, "boilerMaxOutputTemperatureInCelsius": previous}
            _LOGGER.debug("Bridge: max output temperature taken from the dedicated endpoint")
            return {**result, "boilerMaxOutputTemperatureInCelsius": max_output}
        except TadoBridgeApiError as e:
            self.bridge_health_tracker.record_failure(str(e))
            self.data_loader.save_bridge_health(self.bridge_health_tracker.to_dict())
            _LOGGER.debug(
                "Bridge: fetch failed (%s), cloud data unaffected",
                e,
            )
            return None

    def _ensure_bridge_poll_running(self) -> None:
        """Start the independent bridge poll loop if enabled and credentials are present.

        Bridge API uses its own auth key and does NOT count toward the
        Tado cloud API quota, so it runs on a fixed interval independent
        of the main coordinator polling cycle.
        """
        if not self.config_manager.get_bridge_enabled():
            return
        if self._bridge_poll_task is not None and not self._bridge_poll_task.done():
            return  # already running
        options = self.config_entry.options
        if not options.get("bridge_serial", "") or not options.get("bridge_auth_key", ""):
            return  # no credentials
        # eager_start=False keeps the first step on the next loop iteration, as asyncio.create_task does.
        self._bridge_poll_task = self.config_entry.async_create_background_task(
            self.hass,
            self._async_bridge_poll_loop(),
            name=f"tado_ce bridge poll {mask_home_id(self.home_id)}",
            eager_start=False,
        )

    async def _run_one_bridge_poll(self) -> None:
        """Run a single bridge fetch and push the result to listeners.

        On success, cache the data and write it into `coordinator.data`. On
        failure, leave the cached data untouched (don't re-push a stale wiring
        snapshot) but STILL notify listeners: the fetch already recorded the
        failure on the health tracker, so the Bridge connected sensor needs a
        refresh to reflect a bridge that has just dropped offline. Without this,
        an offline bridge would only surface on the next cloud poll cycle.
        """
        bridge_data = await self._async_fetch_bridge_data()
        if bridge_data is not None:
            self._cached_bridge_data = bridge_data
            self._update_bridge_in_coordinator_data()
        else:
            self.async_update_listeners()

    async def _async_bridge_poll_loop(self) -> None:
        """Poll bridge API on a fixed interval, independent of the cloud polling cycle.

        Polls once immediately, then every BRIDGE_POLL_INTERVAL_SECONDS.
        """
        while True:
            try:
                await self._run_one_bridge_poll()
            except Exception:
                # Broad on purpose: an escaping error would end this task, and
                # bridge polling would stop until the next cloud poll re-arms it.
                # Bridge API failures never reach here (the fetch records them on
                # the health tracker), so anything caught is unexpected.
                _LOGGER.warning(
                    "Bridge: unexpected error during poll, will retry on the next cycle",
                    exc_info=True,
                )
            await asyncio.sleep(BRIDGE_POLL_INTERVAL_SECONDS)

    def _update_bridge_in_coordinator_data(self) -> None:
        """Write cached bridge data into coordinator.data and notify listeners."""
        if self.data is None or self._cached_bridge_data is None:
            return
        self.data["bridge"] = self._cached_bridge_data
        self.async_update_listeners()

    def record_boiler_max_output(self, celsius: float) -> None:
        """Carry an accepted boiler max output write into the bridge cache and coordinator data."""
        if self._cached_bridge_data is None:
            return
        self._cached_bridge_data = {**self._cached_bridge_data, "boilerMaxOutputTemperatureInCelsius": celsius}
        self._update_bridge_in_coordinator_data()

    async def cancel_bridge_poll(self) -> None:
        """Cancel the independent bridge poll task."""
        if self._bridge_poll_task is not None:
            self._bridge_poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._bridge_poll_task
            self._bridge_poll_task = None

    async def _async_run_weather_compensation(
        self,
        zone_data: dict[str, Any] | None,
        weather_data: dict[str, Any] | None,
        *,
        saved_data: bool = False,
    ) -> dict[str, Any] | None:
        """Run one weather compensation evaluation cycle.

        Delegates to ``weather_compensation.async_run_wc_cycle`` which
        contains the full orchestration logic.  The coordinator only
        gates on enabled + bridge client availability.
        """
        cm = self.config_manager
        if not cm.get_wc_enabled() or self.bridge_api_client is None:
            self._wc_state.status = "disabled"
            return None

        if saved_data and cm.get_wc_room_compensation_enabled():
            # Room Compensation needs live zone readings; running without them would send the uncompensated flow temperature.
            self._wc_state.status = "paused"
            return {"status": "paused", "target_flow_temp": None, "should_send": False}

        from .weather_compensation import async_run_wc_cycle

        # WC is a best-effort sub-feature: a failed cycle must not break
        # the main poll. The cycle itself no longer carries a catch-all
        # (so bridge failures stay isolated and programmer bugs surface);
        # isolation lives here, at the caller. Cloud auth / rate-limit
        # errors are re-raised so the main dispatch handles them, though
        # the cycle makes no cloud calls today, this guards a future one.
        try:
            return await async_run_wc_cycle(
                config_manager=cm,
                bridge_api_client=self.bridge_api_client,
                wc_state=self._wc_state,
                hass=self.hass,
                weather_data=weather_data,
                zone_data=zone_data,
                update_interval=self.update_interval,
            )
        except (TadoAuthError, TadoRateLimitError, TadoBlockedError, TadoSyncError):
            raise
        except Exception:
            _LOGGER.warning(
                "Weather Compensation: cycle failed, main coordinator "
                "update unaffected, will retry next cycle",
                exc_info=True,
            )
            self._wc_state.status = "error"
            return None

    @property
    def smart_valve_night_floor_minutes(self) -> int | None:
        """Return the night interval cap for entries with a valve-target zone, else None."""
        if svc_schedule_zone_ids(self.zone_config_manager, self.hass):
            return SMART_VALVE_NIGHT_FLOOR_MINUTES
        return None

    def _apply_polling_interval(self, homekit_connected: bool) -> None:
        """Set the next poll interval from the quota snapshot the poll just received."""
        # WHY: the keep-live 429 arm owns the interval until the backoff ends.
        if self.is_cloud_backoff_active():
            return
        self._load_ratelimit_from_cache()
        new_interval = get_polling_interval(
            self.config_manager, self._cached_ratelimit, homekit_connected=homekit_connected,
            full_sync_extra_calls=self.full_sync_extra_calls_per_cycle,
            night_floor_minutes=self.smart_valve_night_floor_minutes,
        )
        self.update_interval = timedelta(minutes=new_interval)

    def _load_ratelimit_from_cache(self) -> None:
        """Load ratelimit data from DataLoader in-memory cache."""
        data = self.data_loader.load_ratelimit_file()
        if data is not None and isinstance(data, dict):
            self._cached_ratelimit = data
        else:
            self._cached_ratelimit = None

    async def mark_entity_fresh(self, entity_id: str) -> None:
        """Mark `entity_id` as having a recent API call (for the freshness gate)."""
        async with self._freshness_lock:
            self.entity_freshness[entity_id] = time.monotonic()
            _LOGGER.debug("Coordinator: marked %s fresh", entity_id)

    def is_entity_fresh(self, entity_id: str, debounce_seconds: int | None = None) -> bool:
        """Check if entity has a recent API call (within debounce window)."""
        if entity_id not in self.entity_freshness:
            return False
        if debounce_seconds is None:
            debounce_seconds = self.config_manager.get_refresh_debounce_seconds() + 2
        elapsed = time.monotonic() - self.entity_freshness[entity_id]
        if elapsed > debounce_seconds:
            del self.entity_freshness[entity_id]
            return False
        return True

    def get_next_sequence(self) -> int:
        """Return the next monotonic sequence number, wrapping safely at sys.maxsize."""
        self._global_sequence += 1
        if self._global_sequence >= sys.maxsize:
            _LOGGER.debug(
                "Coordinator: optimistic-update sequence reached "
                "sys.maxsize, wrapping back to 0",
            )
            self._global_sequence = 0
        return self._global_sequence

    async def _cleanup_entity_freshness(self) -> None:
        """Drop freshness entries older than the expiry window."""
        async with self._freshness_lock:
            now = time.monotonic()
            expired = [eid for eid, timestamp in self.entity_freshness.items() if now - timestamp > ENTITY_FRESHNESS_EXPIRY_SECONDS]
            for eid in expired:
                del self.entity_freshness[eid]
            if expired:
                _LOGGER.debug(
                    "Coordinator: cleaned up %d expired entity freshness "
                    "entry(ies)",
                    len(expired),
                )

    def _zero_savings_on_quota_reset(self, rl_data: dict[str, Any]) -> None:
        """Zero the HomeKit savings counters when the stored quota reset moved."""
        last_reset = rl_data.get("last_reset_utc")
        if not last_reset or last_reset == self._savings_last_reset_seen:
            return
        # First sighting (fresh install or older save) only adopts the marker.
        if self._savings_last_reset_seen is not None:
            _LOGGER.debug(
                "Coordinator: Tado API quota reset recorded at %s, zeroing "
                "HomeKit savings counters (was reads=%s, writes=%s)",
                last_reset, self._homekit_reads_saved, self._homekit_writes_saved,
            )
            self._homekit_reads_saved = 0
            self._homekit_writes_saved = 0
            self._reset_write_metrics()
            self.mechanism_counters.roll_day(last_reset)
        self._savings_last_reset_seen = last_reset
        self._save_homekit_savings()

    def _homekit_savings_payload(self) -> dict[str, Any]:
        """Return the one dict every savings write stores."""
        return {
            "reads_saved": self._homekit_reads_saved,
            "writes_saved": self._homekit_writes_saved,
            "last_reset_utc": self._savings_last_reset_seen,
            "mechanisms": self.mechanism_counters.to_payload(),
        }

    def _save_homekit_savings(self) -> None:
        """Persist HomeKit API savings counters (debounced via Store)."""
        self.data_loader.save_homekit_savings(self._homekit_savings_payload())

    def apply_saved_homekit_savings(self, saved: dict[str, Any] | None) -> None:
        """Restore the savings counters and mechanism counters from the persisted dict."""
        if saved and isinstance(saved, dict):
            self._homekit_reads_saved = saved.get("reads_saved", 0)
            self._homekit_writes_saved = saved.get("writes_saved", 0)
            self._savings_last_reset_seen = saved.get("last_reset_utc")
            self.mechanism_counters.load(saved.get("mechanisms"))
            _LOGGER.debug(
                "Setup: HomeKit savings loaded, reads=%s, writes=%s",
                self._homekit_reads_saved,
                self._homekit_writes_saved,
            )
        else:
            _LOGGER.debug(
                "Setup: HomeKit savings, no persisted data, starting fresh",
            )

    def flush_mechanism_counters(self) -> None:
        """Save the savings store when the mechanism counters changed since the last save."""
        if self.mechanism_counters.needs_save and self.data_loader is not None:
            self._save_homekit_savings()
            self.mechanism_counters.mark_saved()

    @property
    def homekit_reads_saved(self) -> int:
        """Return the number of HomeKit reads saved today."""
        return self._homekit_reads_saved

    @property
    def homekit_writes_saved(self) -> int:
        """Return the number of HomeKit writes saved today."""
        return self._homekit_writes_saved

    def record_homekit_read_saved(self) -> None:
        """Record one HomeKit read saving and persist."""
        self._homekit_reads_saved += 1
        self._save_homekit_savings()

    def _resolve_pending_puts(self, zone_data: dict[str, Any] | list[Any] | None, fetch_start: float | None) -> None:
        """Let the write ledger bucket each sampled put this fetch can answer."""
        self.write_ledger.resolve_agreement(zone_data, fetch_start)

    def record_homekit_write_saved(self, zone_id: str | None = None, payload: dict[str, Any] | None = None) -> None:
        """Record one HomeKit write saving, sample its agreement, and persist."""
        self._homekit_writes_saved += 1
        if zone_id:
            self.write_ledger.note_saved(zone_id, payload)
        self._save_homekit_savings()

    async def async_capture_state(
        self, zone_id: str, entity_type: str, source: str,
    ) -> None:
        """Capture zone state before overlay change; no-op when state restore is disabled."""
        if self._sr_manager is not None:
            await self._sr_manager.capture(zone_id, entity_type, source=source)

    async def async_restore_state(
        self, zone_id: str, entity_type: str,
    ) -> CapturedState | None:
        """Restore captured state for a zone (null-safe)."""
        if self._sr_manager is None:
            return None
        return await self._sr_manager.restore(zone_id, entity_type)

    async def async_peek_state(
        self, zone_id: str, entity_type: str,
    ) -> CapturedState | None:
        """Read captured state without consuming (null-safe).

        Used so the caller can attempt the cloud API write first and only
        clear the captured entry once the write succeeds.
        """
        if self._sr_manager is None:
            return None
        return await self._sr_manager.peek(zone_id, entity_type)

    async def async_clear_captured_state(
        self, zone_id: str, entity_type: str,
    ) -> None:
        """Clear a captured entry (null-safe)."""
        if self._sr_manager is None:
            return
        await self._sr_manager.clear(zone_id, entity_type)

    # ------------------------------------------------------------------
    # Insight runtime state persistence (anomaly timers + humidity history)
    # ------------------------------------------------------------------

    def _serialize_insight_runtime_state(self) -> dict[str, Any]:
        """Build JSON-safe dict for persistence."""
        return {
            "saved_at": dt_util.utcnow().isoformat(),
            "version": 1,
            "anomaly_start_times": {
                zone_id: ts.isoformat() if ts is not None else None
                for zone_id, ts in self._insight_anomaly_start_times.items()
            },
            # Humidity samples are plain floats, JSON round-trips cleanly.
            "humidity_histories": {
                zone_id: list(samples)
                for zone_id, samples in self._insight_humidity_histories.items()
            },
            "ventilation_diff_history": list(self._insight_ventilation_diff_history),
        }

    def _save_insight_runtime_state(self) -> None:
        """Schedule debounced save of insight runtime dicts."""
        from .const import INSIGHT_RUNTIME_STATE_KEY

        try:
            self.data_loader.save_auxiliary(
                INSIGHT_RUNTIME_STATE_KEY,
                self._serialize_insight_runtime_state(),
            )
        except (OSError, ValueError, TypeError) as e:
            _LOGGER.warning(
                "Coordinator: could not schedule insight runtime save "
                "(%s), anomaly timers and humidity history will be "
                "kept in memory only this cycle",
                e,
            )

    async def async_load_insight_runtime_state(self) -> None:
        """Restore insight runtime dicts from DataLoader auxiliary.

        Called during coordinator setup before the first poll. On missing
        file, corrupt data, or version mismatch, starts with empty dicts.
        """
        from .const import INSIGHT_RUNTIME_STATE_KEY
        from .helpers import parse_iso_datetime

        try:
            data = await self.data_loader.async_load_auxiliary(INSIGHT_RUNTIME_STATE_KEY)
        except (OSError, ValueError) as e:
            _LOGGER.debug(
                "Coordinator: no insight runtime state to restore (%s), "
                "starting fresh",
                e,
            )
            return

        if not isinstance(data, dict):
            _LOGGER.debug(
                "Coordinator: insight runtime state is not a dict, "
                "starting fresh",
            )
            return

        if data.get("version") != 1:
            _LOGGER.debug(
                "Coordinator: insight runtime state version %s does not "
                "match expected 1, starting fresh",
                data.get("version"),
            )
            return

        # Restore anomaly start times
        raw_anomalies = data.get("anomaly_start_times") or {}
        if isinstance(raw_anomalies, dict):
            for zone_id, iso_str in raw_anomalies.items():
                if not isinstance(iso_str, str):
                    continue
                try:
                    parsed = parse_iso_datetime(iso_str)
                    if parsed is not None:
                        self._insight_anomaly_start_times[str(zone_id)] = parsed
                except (ValueError, TypeError):
                    continue

        # Restore humidity histories
        raw_histories = data.get("humidity_histories") or {}
        if isinstance(raw_histories, dict):
            for zone_id, samples in raw_histories.items():
                if isinstance(samples, list):
                    # Filter to numeric samples only (defensive)
                    self._insight_humidity_histories[str(zone_id)] = [
                        float(s) for s in samples
                        if isinstance(s, (int, float))
                    ]

        # Restore ventilation differential history
        raw_diff_history = data.get("ventilation_diff_history") or []
        if isinstance(raw_diff_history, list):
            self._insight_ventilation_diff_history = [
                float(s) for s in raw_diff_history
                if isinstance(s, (int, float))
            ]

        _LOGGER.debug(
            "Coordinator: restored insight runtime state: %d anomaly "
            "timer(s), %d humidity history(ies), %d ventilation "
            "differential sample(s)",
            len(self._insight_anomaly_start_times),
            len(self._insight_humidity_histories),
            len(self._insight_ventilation_diff_history),
        )

    # ------------------------------------------------------------------
    # Zone identity persistence (dateCreated baseline for reassignment detection)
    # ------------------------------------------------------------------

    async def async_load_zone_identity(self) -> None:
        """Restore the persisted zone-identity baseline before the first poll."""
        from .const import ZONE_IDENTITY_KEY

        try:
            data = await self.data_loader.async_load_auxiliary(ZONE_IDENTITY_KEY)
        except (OSError, ValueError) as e:
            _LOGGER.debug(
                "Coordinator: no zone identity baseline to restore (%s), "
                "starting fresh",
                e,
            )
            return
        if isinstance(data, dict):
            self._last_known_zone_dates = {str(k): str(v) for k, v in data.items() if v is not None}

    def _save_zone_identity(self) -> None:
        """Schedule a debounced save of the zone-identity baseline."""
        from .const import ZONE_IDENTITY_KEY

        try:
            self.data_loader.save_auxiliary(ZONE_IDENTITY_KEY, dict(self._last_known_zone_dates))
        except (OSError, ValueError, TypeError) as e:
            _LOGGER.warning(
                "Coordinator: could not schedule zone identity save (%s), "
                "a reassignment detected this cycle may need to be "
                "re-detected after a restart",
                e,
            )

    # ------------------------------------------------------------------
    # Zone type persistence (HEATING baseline for stale-schedule detection)
    # ------------------------------------------------------------------

    async def async_load_zone_type_baseline(self) -> None:
        """Restore the persisted zone-type baseline before the first poll."""
        from .const import ZONE_TYPE_BASELINE_KEY

        try:
            data = await self.data_loader.async_load_auxiliary(ZONE_TYPE_BASELINE_KEY)
        except (OSError, ValueError) as e:
            _LOGGER.debug(
                "Coordinator: no zone type baseline to restore (%s), "
                "starting fresh",
                e,
            )
            return
        if isinstance(data, dict):
            self._last_known_zone_types = {str(k): str(v) for k, v in data.items() if v is not None}

    def _save_zone_type_baseline(self) -> None:
        """Schedule a debounced save of the zone-type baseline."""
        from .const import ZONE_TYPE_BASELINE_KEY

        try:
            self.data_loader.save_auxiliary(ZONE_TYPE_BASELINE_KEY, dict(self._last_known_zone_types))
        except (OSError, ValueError, TypeError) as e:
            _LOGGER.warning(
                "Coordinator: could not schedule zone type baseline save (%s), "
                "a type change detected this cycle may need to be "
                "re-detected after a restart",
                e,
            )

    async def async_shutdown_insight_state(self) -> None:
        """Persist insight runtime state synchronously during integration unload.

        The debouncer may not fire before the Store is torn down on
        shutdown, so we use async_update_store to write directly.
        """
        from .const import INSIGHT_RUNTIME_STATE_KEY

        try:
            await self.data_loader.async_update_store(
                INSIGHT_RUNTIME_STATE_KEY,
                self._serialize_insight_runtime_state(),
            )
        except (OSError, ValueError, TypeError) as e:
            _LOGGER.warning(
                "Coordinator: could not flush insight runtime state on "
                "shutdown (%s), anomaly timers and humidity history may "
                "be lost across this restart",
                e,
            )

    async def async_flush_all_stores(self) -> None:
        """Issue the pending write of every debounced store that holds per-zone state (loader stores, heating cycles, state restore); ordering only, HA swallows a failed write."""
        await self.data_loader.async_flush_pending_saves()
        if self.heating_cycle_coordinator is not None:
            await self.heating_cycle_coordinator.async_flush_storage()
        if self._sr_manager is not None:
            await self._sr_manager.async_shutdown()

    async def _handle_zone_delta(self, delta: ZoneFingerprintDelta) -> None:
        """Request the full sync that lets zones_info confirm a removal, and enable Preheat for added zones."""
        if delta.added or delta.removed:
            self._request_full_sync_next_cycle = True
            self.reset_mapping_retry_budget()
        self._zones_awaiting_listing |= delta.added

        if self.adaptive_preheat_manager is not None:
            for zone_id in delta.added:
                await self.adaptive_preheat_manager.async_enable_zone(zone_id)

    async def _async_drop_vanished_zones(self, *, fresh_fetch: bool = True) -> None:
        """Drop a zone once two fetched polls in a row list it in neither zones_info nor zoneStates; a failed owner is retried next poll."""
        zones_info = (self.data or {}).get("zones_info")
        listed = (
            {str(z["id"]) for z in zones_info if isinstance(z, dict) and z.get("id") is not None}
            if isinstance(zones_info, list)
            else set()
        )
        in_states = self._zone_fingerprint.zone_ids
        # A one-poll glitch of either list must never drop a zone, so an unreadable list drops nothing.
        if not listed or not in_states:
            if fresh_fetch:
                # A zone already at two confirmations with an owner still owing keeps its count.
                owing = {k[0] for k in self._zone_drop_pending if k[1] == "removed"}
                for zone_id in [z for z in self._zone_absent_polls if z not in owing]:
                    del self._zone_absent_polls[zone_id]
            return
        live = listed | in_states
        await self._async_rebuild_returned_zones((self._zone_drops_done | self._zones_awaiting_listing) & listed)
        self._schedule_reload_for_new_zones(listed & in_states, zones_info)
        self._zone_drops_done -= listed
        self._zones_awaiting_listing &= in_states - listed
        for key in [k for k in self._zone_drop_pending if k[1] == "removed" and k[0] in live]:
            del self._zone_drop_pending[key]
        self._discard_pending_reassigned_drops(keep=live)
        known = set(self._last_known_zone_dates) | set(self._last_known_zone_types)
        if self.homekit_client is not None:
            known |= set(self.homekit_client.zone_aid_map)
        absent = known - live - self._zone_drops_done
        for zone_id in [z for z in self._zone_absent_polls if z not in absent]:
            del self._zone_absent_polls[zone_id]
        for zone_id in sorted(absent):
            # Only a fetched poll counts; a poll retrying a failed owner has already counted twice.
            if fresh_fetch:
                self._zone_absent_polls[zone_id] = self._zone_absent_polls.get(zone_id, 0) + 1
            if self._zone_absent_polls.get(zone_id, 0) < ZONE_REMOVAL_CONFIRM_POLLS:
                continue
            try:
                await self._drop_zone_state(zone_id, "removed")
            except ZoneDropIncomplete as exc:
                _LOGGER.debug("Zone %s: removal not complete (%s), retrying next poll", zone_id, exc)
                continue
            self._zone_drops_done.add(zone_id)
            # A listener may have republished between a failed owner and this retry.
            self.entity_data.pop(zone_id, None)
            del self._zone_absent_polls[zone_id]

    def _schedule_reload_for_new_zones(self, live: set[str], zones_info: Any) -> None:
        """Schedule one entry reload, never inline, for a room the platforms hold no entities for, new in Tado or removed and listed again."""
        if self.entity_zone_ids is None:
            return
        types = {str(z["id"]): z.get("type") or "" for z in zones_info if isinstance(z, dict) and z.get("id") is not None}
        entry_id = self.config_entry.entry_id
        for zone_id in sorted(live - self.entity_zone_ids):
            zone_type = types.get(zone_id, "")
            if not (is_climate_zone(zone_type) or zone_type == "HOT_WATER") or (entry_id, zone_id) in _ZONE_RELOADS_REQUESTED:
                continue
            _ZONE_RELOADS_REQUESTED.add((entry_id, zone_id))
            _LOGGER.info("Zone %s is new or came back in Tado, reloading Tado CE once so it gets its entities", zone_id)
            self.hass.config_entries.async_schedule_reload(entry_id)
            return

    async def _async_rebuild_returned_zones(self, returned: set[str]) -> None:
        """Enable Preheat and queue the controller init for zones zones_info lists for the first time or again."""
        if not returned:
            return
        self._valve_init_pending = True
        if self.adaptive_preheat_manager is not None:
            for zone_id in sorted(returned):
                await self.adaptive_preheat_manager.async_enable_zone(zone_id)

    def _discard_pending_reassigned_drops(self, *, keep: set[str] | frozenset[str]) -> None:
        """Forget the pending reassignment drop, and the date it was keyed on, of every zone not in keep."""
        for key in [k for k in self._zone_drop_pending if k[1] == "reassigned" and k[0] not in keep]:
            del self._zone_drop_pending[key]
        for zone_id in [z for z in self._zone_drop_dates if z not in keep]:
            del self._zone_drop_dates[zone_id]

    async def _prune_zone_type_change_schedules(self) -> None:
        """Drop a zone's cached schedule when its type just changed away from HEATING.

        Compares this cycle's zone types against the last cycle's, the same
        pattern ZoneFingerprintTracker uses for presence. Runs every cycle,
        since a same-id type change never touches zoneStates. A zone with no
        prior HEATING recording is never pruned, so an AC zone's own
        button-fetched schedule is untouched.
        """
        zones_info = self.data_loader.get_cached("zones_info")
        if not isinstance(zones_info, list):
            return

        before = dict(self._last_known_zone_types)
        try:
            current_types = {str(z["id"]): z.get("type") for z in zones_info if z.get("id") is not None}
            transitioned = {
                zone_id for zone_id, prev_type in self._last_known_zone_types.items()
                if prev_type == "HEATING" and current_types.get(zone_id) not in (None, "HEATING")
            }
            # Seed every non-transitioned zone's baseline unconditionally, so a
            # zone with no prior recording still gets one even when nothing below runs.
            for zone_id, new_type in current_types.items():
                if zone_id not in transitioned:
                    self._last_known_zone_types[zone_id] = new_type
            if not transitioned:
                return

            try:
                # Broad on purpose: a store I/O failure here must not block the poll cycle.
                pruned = await self._drop_zones_from_flat_store("schedules", transitioned)
                if pruned:
                    _LOGGER.debug(
                        "Coordinator: dropped %d stale schedule entr(ies) for "
                        "zone(s) that changed away from HEATING: %s",
                        len(pruned), sorted(pruned),
                    )
            except Exception:
                _LOGGER.warning(
                    "Coordinator: could not check for stale schedules after a "
                    "zone-type change",
                    exc_info=True,
                )
                return  # retry next cycle: don't advance the transitioned baseline

            # Only after a successful attempt above does the transitioned zones'
            # baseline advance, so a failure above retries next cycle.
            for zone_id in transitioned:
                self._last_known_zone_types[zone_id] = current_types[zone_id]
        finally:
            # Seeding can mutate the baseline on any exit path, so save
            # whenever it changed, not just at the end.
            if self._last_known_zone_types != before:
                self._save_zone_type_baseline()

    def _register_zone_state_owners(self) -> None:
        """Register the holders of per-zone state in drop order; zone_config goes first so the controller teardown never reads the old room's settings."""
        self.zone_state.register("zone_config", self._drop_zone_config, reasons=REASSIGNED_ONLY)
        self.zone_state.register("controllers", self._drop_zone_controllers)
        self.zone_state.register("write_health", lambda z, r: self.write_health_trackers.pop(z, None) is not None)
        self.zone_state.register("homekit_off_latch", lambda z, r: self.homekit_off_latch.pop(z, False) is not False)
        self.zone_state.register("write_ledger", lambda z, r: self.write_ledger.forget_zone(z))
        self.zone_state.register("adaptive_preheat", self._drop_zone_preheat)
        self.zone_state.register("heating_cycles", self._drop_zone_heating_cycles)
        self.zone_state.register("smart_comfort", self._drop_zone_smart_comfort, reasons=REASSIGNED_ONLY)
        self.zone_state.register("insight_runtime", self._drop_zone_insight_runtime)
        self.zone_state.register("flat_stores", self._drop_zone_from_flat_stores)
        self.zone_state.register("homekit_mapping", self._drop_zone_homekit_mapping)
        self.zone_state.register("state_reconciler", self._drop_zone_reconciler)
        self.zone_state.register("state_restore", self._drop_zone_state_restore)
        # Before entity_data: the removed entities stop publishing once their rows are gone.
        self.zone_state.register("entity_registry_rows", self._drop_zone_entity_rows, reasons=REMOVED_ONLY)
        # After controllers: a running Offset Sync controller could re-write the clamp after it is cleared.
        self.zone_state.register("entity_data", self._drop_zone_entity_data)
        self.zone_state.register("offset_clamps", self._drop_zone_offset_clamps)
        self.zone_state.register(
            "sensor_missing_repair", lambda z, r: self._drop_zone_repair(SVC_SENSOR_MISSING_ISSUE, z),
        )
        # The low-battery notice follows its zone, so a removed or re-used id dismisses it.
        self.zone_state.register("battery_low_repair", lambda z, r: self._drop_zone_repair(TRV_BATTERY_LOW_ISSUE, z))
        self.zone_state.register(
            "zone_reassigned_repair", lambda z, r: self._drop_zone_repair(ZONE_REASSIGNED_ISSUE, z), reasons=REMOVED_ONLY,
        )

    async def _drop_zone_state(self, zone_id: str, reason: ZoneDropReason) -> frozenset[str]:
        """Drop everything a zone holds through the registry, retrying only the owners that failed last time."""
        key = (zone_id, reason)
        try:
            dropped = await self.zone_state.drop_zone(zone_id, reason, only=self._zone_drop_pending.get(key))
        except ZoneDropIncomplete as exc:
            self._zone_drop_pending[key] = frozenset(exc.failed)
            raise
        self._zone_drop_pending.pop(key, None)
        if dropped:
            _LOGGER.info("Zone %s (%s): dropped state held by %s", zone_id, reason, ", ".join(sorted(dropped)))
        return dropped

    async def _drop_zone_config(self, zone_id: str, reason: ZoneDropReason) -> bool:
        """Delete a reassigned zone's settings and raise the zone-reassigned repair issue, once."""
        pruned = await self.zone_config_manager.async_prune_reassigned_zones({zone_id})
        if not pruned:
            return False
        try:
            async_create_zone_reassigned_issue(self.hass, self.home_id, zone_id)
        except Exception:
            # Broad on purpose: a repair-issue failure must not block this zone's remaining drops or baseline advance.
            _LOGGER.warning(
                "Coordinator: could not raise the zone-reassigned repair issue for zone %s",
                zone_id, exc_info=True,
            )
        return True

    async def _drop_zone_controllers(self, zone_id: str, reason: ZoneDropReason) -> bool:
        """Stop the zone's valve, retiring and offset controllers; True when any held it."""
        held = any(
            zone_id in table
            for table in (self.valve_controllers, self.retired_valve_controllers, self.offset_sync_controllers)
        )
        await self._teardown_reassigned_controllers({zone_id})
        self._valve_init_failing.discard(zone_id)
        return held

    def _drop_zone_preheat(self, zone_id: str, reason: ZoneDropReason) -> bool:
        """Forget the zone in Adaptive Preheat; True when the manager held it."""
        manager = self.adaptive_preheat_manager
        return manager.forget_zone(zone_id) if manager is not None else False

    async def _drop_zone_heating_cycles(self, zone_id: str, reason: ZoneDropReason) -> bool:
        """Forget the zone's heating-cycle detector, metrics and stored cycles; True when any were held."""
        hcc = self.heating_cycle_coordinator
        return await hcc.async_forget_zone(zone_id) if hcc is not None else False

    def _drop_zone_smart_comfort(self, zone_id: str, reason: ZoneDropReason) -> bool:
        """Forget a reassigned zone's Smart Comfort history; True when the manager held it."""
        manager = self.smart_comfort_manager
        return manager.forget_zone(zone_id) if manager is not None else False

    def _drop_zone_insight_runtime(self, zone_id: str, reason: ZoneDropReason) -> bool:
        """Drop the zone's anomaly timer and humidity history and save the rest; True when it held either."""
        held = [table for table in (self._insight_anomaly_start_times, self._insight_humidity_histories) if zone_id in table]
        for table in held:
            del table[zone_id]
        if held:
            self._save_insight_runtime_state()
        return bool(held)

    async def _drop_zones_from_flat_store(self, name: str, zone_ids: set[str]) -> list[str]:
        """Remove zones from one flat store's live cache dict and save that same dict; returns the zone ids it held."""
        stored = await self.data_loader.async_load_auxiliary(name)
        live = self.data_loader.get_cached(name)
        stored_zones = stored if isinstance(stored, dict) else {}
        live_zones = live if isinstance(live, dict) else None
        held = [z for z in zone_ids if z in stored_zones or (live_zones is not None and z in live_zones)]
        if not held:
            return []
        # The cache is the newer copy (Offset Sync writes it alone), so the merge prunes it in place and saves that same object.
        pruned = await self.data_loader.async_merge_store(name, removed=held)
        if isinstance(self.data, dict) and name in self.data:
            self.data[name] = pruned
        return held

    async def _drop_zone_from_flat_stores(self, zone_id: str, reason: ZoneDropReason) -> bool:
        """Drop the zone from every flat zone-keyed store and tell its calendar entity to reload; a non-I/O failure propagates for a retry."""
        dropped: list[str] = []
        for name in ZONE_KEYED_FLAT_STORES:
            if not await self._drop_zones_from_flat_store(name, {zone_id}):
                continue
            dropped.append(name)
            if name == "schedules":
                self.hass.bus.async_fire(
                    f"{DOMAIN}_schedule_updated",
                    {"zone_id": zone_id, "zone_name": ""},
                )
        if dropped:
            _LOGGER.debug("Coordinator: dropped zone %s from the %s cache(s)", zone_id, dropped)
        return bool(dropped)

    async def _drop_zone_homekit_mapping(self, zone_id: str, reason: ZoneDropReason) -> bool:
        """Drop the zone's HomeKit mapping, provider cache and event map; True when it was mapped."""
        client = self.homekit_client
        if client is None:
            return False
        had = zone_id in client.zone_aid_map
        # A long-gone zone must not cost a save and a retry-budget reset on every start; a reused id still re-arms the retry.
        if not had and reason == "removed":
            return False
        await self._prune_reassigned_homekit_mapping({zone_id})
        return had

    def _drop_zone_reconciler(self, zone_id: str, reason: ZoneDropReason) -> bool:
        """Drop the zone's write stamp and merge-source transitions; True when it held any."""
        return self.state_reconciler.forget_zone(zone_id) if self.state_reconciler is not None else False

    async def _drop_zone_state_restore(self, zone_id: str, reason: ZoneDropReason) -> bool:
        """Drop the zone's captures and poll counters; True when it held any."""
        return await self._sr_manager.forget_zone(zone_id) if self._sr_manager is not None else False

    def _live_device_serials(self, zones_info: list[Any]) -> frozenset[str]:
        """Serials of every device a listed zone holds."""
        return frozenset(s for z in zones_info if isinstance(z, dict) for d in z.get("devices") or [] for s in device_serials(d))

    def _drop_zone_entity_rows(self, zone_id: str, reason: ZoneDropReason) -> bool:
        """Remove the removed room's registry rows, then forget it was built so its return rebuilds it; True when any row went."""
        live_serials = self._live_device_serials((self.data or {}).get("zones_info") or [])
        entities, devices = remove_zone_registry_rows(self.hass, self.config_entry, self.home_id, zone_id, live_serials)
        if self.entity_zone_ids is not None:
            self.entity_zone_ids = self.entity_zone_ids - {zone_id}
        if self.entity_zone_identity is not None:
            self.entity_zone_identity.pop(zone_id, None)
        _ZONE_RELOADS_REQUESTED.discard((self.config_entry.entry_id, zone_id))
        return bool(entities or devices)

    def _drop_zone_entity_data(self, zone_id: str, reason: ZoneDropReason) -> bool:
        """Drop the values entities published for the zone; its entities republish on their next update."""
        return self.entity_data.pop(zone_id, None) is not None

    def _drop_zone_offset_clamps(self, zone_id: str, reason: ZoneDropReason) -> bool:
        """Drop the zone's Offset Sync clamp flag from the cache and the published poll data; True when either held it."""
        held = False
        for table in (self.data_loader.get_cached("offset_clamps"), (self.data or {}).get("offset_clamps")):
            if isinstance(table, dict) and zone_id in table:
                del table[zone_id]
                held = True
        return held

    def _drop_zone_repair(self, spec: RepairIssueSpec, zone_id: str) -> bool:
        """Dismiss this home's repair of the zone (a failure propagates so the drop retries); True when it was raised."""
        held = async_issue_exists(self.hass, spec, home_id=self.home_id, zone_id=zone_id)
        async_dismiss_issue(self.hass, spec, home_id=self.home_id, zone_id=zone_id)
        return held

    async def _prune_reassigned_homekit_mapping(self, zone_ids: set[str]) -> None:
        """Remove reassigned or removed zone_ids from the live HomeKit mapping (never the Store directly) and re-arm the rebuild retry."""
        client = self.homekit_client
        if client is None:
            return
        if not zone_ids:
            return
        from .homekit_mapping import save_device_mapping

        serial_to_zone: dict[str, str] = {}
        zone_to_aids: dict[str, list[int]] = {}
        for zone_id in zone_ids:
            serial_to_zone, zone_to_aids = client.remove_zone_mapping(zone_id)
            if self.homekit_provider is not None:
                self.homekit_provider.drop_zone(zone_id)
        try:
            await save_device_mapping(
                self.hass, self.home_id,
                {
                    "serial_to_zone": serial_to_zone,
                    "zone_to_aids": zone_to_aids,
                    "cloud_serials": client.cloud_serials,
                },
            )
        except Exception:
            # Broad on purpose: a Store write failure here must not crash
            # the prune; only the on-disk copy stays stale until the next save.
            _LOGGER.warning(
                "Coordinator: could not persist the HomeKit mapping after "
                "pruning reassigned zone(s), the in-memory mapping is "
                "correct but a restart before the next successful save "
                "would revert it",
                exc_info=True,
            )
        self.reset_mapping_retry_budget()

    async def _prune_reassigned_zones(self) -> None:
        """Detect a zone-id reassignment and drop the zone through the registry; the baseline advances only once every owner has dropped it."""
        zones_info = self.data_loader.get_cached("zones_info")
        if not isinstance(zones_info, list):
            return
        before = dict(self._last_known_zone_dates)
        reassigned = self._compute_reassigned_zones(zones_info)
        # A zone no longer flagged (vanished, date reverted, anomaly advance) has no drop left to retry.
        self._discard_pending_reassigned_drops(keep=reassigned)
        if not reassigned:
            # This can mutate the baseline even with no reassignment, so
            # save regardless.
            if self._last_known_zone_dates != before:
                self._save_zone_identity()
            return

        new_dates = {str(z["id"]): z.get("dateCreated") for z in zones_info if z.get("id") is not None}

        advanced = False
        dropped: set[str] = set()
        for zone_id in sorted(reassigned):
            if self._zone_drop_dates.get(zone_id) != new_dates[zone_id]:
                self._zone_drop_pending.pop((zone_id, "reassigned"), None)
                self._zone_drop_dates[zone_id] = new_dates[zone_id]
            try:
                await self._drop_zone_state(zone_id, "reassigned")
            except Exception:
                # Broad on purpose: a single zone's prune failure must not
                # block the poll cycle or the other zone_ids in this batch.
                _LOGGER.warning(
                    "Coordinator: prune of reassigned zone %s failed, "
                    "will retry next cycle",
                    zone_id, exc_info=True,
                )
                continue
            # Issues the owners' queued writes before the immediate baseline write; HA logs and swallows a write that then fails.
            await self.async_flush_all_stores()
            self._last_known_zone_dates[zone_id] = new_dates[zone_id]
            self._zone_drop_dates.pop(zone_id, None)
            await self._write_zone_identity_through()
            advanced = True
            dropped.add(zone_id)

        if not advanced:
            self._save_zone_identity()
        self._schedule_reload_for_reassigned_zones(dropped, zones_info)

    def _schedule_reload_for_reassigned_zones(self, zone_ids: set[str], zones_info: list[Any]) -> None:
        """Schedule one entry reload, never inline, when a reassigned zone's type or name no longer matches its entities; a changed type first removes the old room's rows."""
        if self.entity_zone_identity is None:
            return
        now = {
            str(z["id"]): zone_build_identity(z)
            for z in zones_info
            if isinstance(z, dict) and z.get("id") is not None
        }
        stale = sorted(z for z in zone_ids if z in self.entity_zone_identity and now.get(z) not in (None, self.entity_zone_identity[z]))
        if stale:
            retyped = [z for z in stale if now[z][0] != self.entity_zone_identity[z][0]]
            if retyped:
                try:
                    live_serials = self._live_device_serials(zones_info)
                    for zone_id in retyped:
                        remove_zone_registry_rows(self.hass, self.config_entry, self.home_id, zone_id, live_serials)
                except Exception:
                    # Broad on purpose: a registry failure must not cost the reload that rebuilds the room.
                    _LOGGER.warning(
                        "Coordinator: could not remove the old entities of retyped zone(s) %s, they stay until deleted by hand",
                        ", ".join(retyped), exc_info=True,
                    )
            _LOGGER.info("Zone(s) %s were reassigned to a different room in Tado, reloading Tado CE once so their entities match", ", ".join(stale))
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

    async def _write_zone_identity_through(self) -> None:
        """Write the baseline now, so a crash cannot make the next start re-flag a zone whose drop finished."""
        from .const import ZONE_IDENTITY_KEY

        try:
            await self.data_loader.async_update_store(ZONE_IDENTITY_KEY, dict(self._last_known_zone_dates))
        except Exception:
            # Broad on purpose: a Store failure must not fail the poll; the debounced save is the fallback.
            _LOGGER.warning(
                "Coordinator: could not write the zone identity baseline through, "
                "falling back to the debounced save",
                exc_info=True,
            )
            self._save_zone_identity()

    def _compute_reassigned_zones(self, zones_info: list[Any]) -> set[str]:
        """Diff zones_info[].dateCreated against the persisted identity baseline.

        Returns the zone_ids whose date changed (a reassignment). The
        baseline is updated additively: present-and-unchanged or
        newly-seen zone_ids are recorded immediately; a zone_id in the
        returned set keeps its OLD baseline value here, so the caller
        (which runs the prunes) can advance it only after those prunes
        succeed. Absent zone_ids are never touched, so a zone_id that
        vanishes for one or more cycles is still diffed correctly when
        it reappears.
        """
        from .helpers import parse_iso_datetime

        if not isinstance(zones_info, list):
            return set()

        current_raw = {str(z["id"]): z.get("dateCreated") for z in zones_info if z.get("id") is not None}
        reassigned: set[str] = set()
        # Zones that can show a change this cycle; the baseline also holds long-gone zones.
        comparable = {z for z, raw in current_raw.items() if raw is not None and z in self._last_known_zone_dates}

        for zone_id, new_raw in current_raw.items():
            if new_raw is None:
                continue  # this cycle's fetch didn't carry a date; don't touch the baseline
            prev_raw = self._last_known_zone_dates.get(zone_id)
            if prev_raw is None:
                self._last_known_zone_dates[zone_id] = new_raw  # first-ever recording
                continue
            if prev_raw == new_raw:
                continue
            try:
                changed = parse_iso_datetime(prev_raw) != parse_iso_datetime(new_raw)
            except ValueError:
                _LOGGER.debug(
                    "Coordinator: could not parse dateCreated for zone %s "
                    "(prev=%s, new=%s), treating as unchanged this cycle",
                    zone_id, prev_raw, new_raw,
                )
                continue
            if changed:
                reassigned.add(zone_id)
            else:
                self._last_known_zone_dates[zone_id] = new_raw  # same instant, different rendering

        if len(reassigned) > 1 and reassigned == comparable:
            _LOGGER.warning(
                "Coordinator: every known zone's dateCreated changed in "
                "one cycle (%d zones), treating as a data anomaly, not "
                "pruning any zone this cycle",
                len(reassigned),
            )
            for zone_id in reassigned:
                self._last_known_zone_dates[zone_id] = current_raw[zone_id]
            return set()

        return reassigned

    # ------------------------------------------------------------------
    # Smart Valve Control lifecycle
    # ------------------------------------------------------------------

    def _forget_eval_failures(self, zone_id: str) -> None:
        """Start a fresh failure episode for a zone whose controller was replaced or discarded."""
        self._controller_eval_failing = {k for k in self._controller_eval_failing if k[1] != zone_id}

    async def _async_evaluate_controller(self, label: str, controller: Any) -> None:
        """Run one controller's evaluation; a failure never fails the poll or stops the next controller."""
        key = (label, controller.zone_id)
        try:
            await controller.async_evaluate()
        except TadoRateLimitError:
            _LOGGER.warning(
                "%s: zone %s evaluation hit Tado rate limit, "
                "backing off, will retry after the quota window",
                label, controller.zone_id,
            )
        except TadoBlockedError:
            _LOGGER.warning(
                "%s: zone %s evaluation was blocked by Tado, will retry on next poll",
                label, controller.zone_id,
            )
        except TadoAuthError as e:
            self._recover_from_cloud_error(e)
            _LOGGER.warning("%s: zone %s evaluation was refused by Tado, sign-in repair started", label, controller.zone_id)
        except Exception:
            # Optional feature degrade: an unexpected controller fault must not fail the poll; logged once per run of failures.
            level = logging.DEBUG if key in self._controller_eval_failing else logging.WARNING
            self._controller_eval_failing.add(key)
            _LOGGER.log(
                level,
                "%s: zone %s evaluation raised an exception, controller will retry on next poll",
                label, controller.zone_id, exc_info=True,
            )
        else:
            self._controller_eval_failing.discard(key)

    async def async_init_valve_controllers(self) -> None:
        """Spin up the per-zone valve and offset-sync controllers from svc_mode; safe to run again."""
        # Cleared first and set again on every exit that leaves work owed, so a mark raised while this pass runs survives it.
        self._valve_init_pending = False
        # Registered first so a mode change made before or during start-up is not lost.
        self._register_zone_config_listener()
        zones_info = self.data_loader.get_cached("zones_info")
        if not zones_info or not isinstance(zones_info, list):
            self._valve_init_pending = True
            _LOGGER.warning(
                "Smart Valve: Tado zone data is not loaded yet, controllers will be set up "
                "after the first zone update that includes it",
            )
            return

        _LOGGER.debug(
            "Smart Valve: checking %d zone(s) for eligible controllers",
            len(zones_info),
        )
        initialized_count = 0

        for zone in zones_info:
            if not isinstance(zone, dict) or zone.get("type", "") != "HEATING":
                continue
            zone_id = str(zone.get("id", ""))
            # Same per-zone lock as a config-change transition, so the two cannot both install a controller.
            async with self._zone_transition_locks.setdefault(zone_id, asyncio.Lock()):
                if self._shutting_down:
                    return
                try:
                    initialized_count += await self._init_zone_controller(zone_id)
                except TadoRateLimitError:
                    self._valve_init_pending = True
                    _LOGGER.warning(
                        "Smart Valve: setting up controllers hit Tado rate limit, "
                        "backing off, will retry after the quota window",
                    )
                    return
                except Exception:
                    # Optional feature degrade: one zone's start-up fault must not fail the poll or stop the other zones.
                    self._valve_init_pending = True
                    level = logging.DEBUG if zone_id in self._valve_init_failing else logging.WARNING
                    self._valve_init_failing.add(zone_id)
                    _LOGGER.log(
                        level,
                        "Smart Valve: zone %s controller could not be set up just now, "
                        "it will be tried again on the next poll",
                        zone_id, exc_info=True,
                    )
                else:
                    self._valve_init_failing.discard(zone_id)

        _LOGGER.info(
            "Smart Valve: %d controller(s) activated", initialized_count,
        )

    def _register_zone_config_listener(self) -> None:
        """Subscribe to zone config changes once."""
        if not self._zone_config_listener_registered:
            self.zone_config_manager.add_listener(self._on_zone_config_change)
            self._zone_config_listener_registered = True

    async def _init_zone_controller(self, zone_id: str) -> int:
        """Build the controller a heating zone's svc_mode asks for unless one exists; returns 1 when built."""
        from .offset_sync_controller import OffsetSyncController
        from .valve_controller import SmartValveController

        if zone_id in self.valve_controllers or zone_id in self.offset_sync_controllers:
            return 0

        config = self.zone_config_manager.get_zone_config(zone_id)
        svc_mode = config.get("svc_mode", "off")
        ext_sensor = config.get("external_temp_sensor", "")

        if zone_id in self.retired_valve_controllers:
            # A retiring controller only blocks another Smart Valve controller; Offset Sync is built next to it, as a mode switch does.
            if svc_mode != "offset_sync":
                return 0
        elif not (svc_mode == "valve_target" and ext_sensor):
            await self._restore_retiring_valve_controller(zone_id)

        if svc_mode == "off":
            return 0

        if not ext_sensor:
            _LOGGER.info(
                "Smart Valve: zone %s is set to %s mode but has no "
                "external sensor selected, pick a sensor in Zone "
                "Configuration to enable Smart Valve Control",
                zone_id, svc_mode,
            )
            return 0

        if svc_mode == "valve_target":
            controller = SmartValveController(self.hass, self, zone_id)
            await self._async_start_controller(self.valve_controllers, zone_id, controller)
            _LOGGER.debug(
                "Smart Valve: zone %s valve_target controller activated",
                zone_id,
            )
            return 1
        if svc_mode == "offset_sync":
            controller_os = OffsetSyncController(self.hass, self, zone_id)
            await self._async_start_controller(self.offset_sync_controllers, zone_id, controller_os)
            _LOGGER.debug(
                "Offset Sync: zone %s controller activated",
                zone_id,
            )
            return 1
        return 0

    async def _async_start_controller(
        self, registry: dict[str, Any], zone_id: str, controller: Any,
    ) -> None:
        """Register a controller, then activate it; a failed activation is stopped and removed before re-raising."""
        # Registered first so a shutdown that waits on the zone lock always finds it.
        registry[zone_id] = controller
        try:
            await controller.async_activate()
        except BaseException:
            # Broad on purpose and re-raised: whatever failed (or cancelled), its sensor listeners must not outlive it.
            controller.discard()
            registry.pop(zone_id, None)
            raise

    async def _async_retry_valve_init(self, zones_info: Any) -> None:
        """Finish controller start-up from the post-sync once a real fetch has brought zone info."""
        if (
            not self._valve_init_pending
            or self._shutting_down
            # A saved snapshot is not a fetch: no controller may be built on it.
            or self.last_zone_fetch_ts() is None
            or not isinstance(zones_info, list)
            or not zones_info
        ):
            return
        await self.async_init_valve_controllers()

    async def _restore_retiring_valve_controller(self, zone_id: str) -> None:
        """Rebuild a switched-off zone's controller when its saved state says a hand-back is still owed."""
        from .valve_controller import SmartValveController

        controller = SmartValveController(self.hass, self, zone_id)
        if await controller.async_restore_retiring():
            self.retired_valve_controllers[zone_id] = controller
            _LOGGER.warning(
                "Smart Valve: zone %s is switched off, but Tado had not taken back its last "
                "valve setting before Home Assistant stopped. Tado CE will try again after "
                "later zone updates, until Tado accepts it or something else changes the zone",
                zone_id,
            )

    async def async_shutdown_valve_controllers(self) -> None:
        """Deactivate and clean up all valve and offset sync controllers.

        Sets `_shutting_down` so any queued/in-flight `_transition` tasks
        bail out before touching controller dicts, preventing zombie
        controllers that would outlive the shutdown sequence.
        """
        self._shutting_down = True
        # Wait out any activation in flight so its controller is in the dicts swept below.
        for lock in list(self._zone_transition_locks.values()):
            async with lock:
                pass

        for zone_id, controller in list(self.valve_controllers.items()):
            try:
                await controller.async_deactivate()
            except Exception:
                _LOGGER.warning(
                    "Smart Valve: error deactivating controller for zone %s",
                    zone_id, exc_info=True,
                )
        self.valve_controllers.clear()
        # Their saved flag rebuilds them at the next start.
        self.retired_valve_controllers.clear()

        for zone_id, controller_os in list(self.offset_sync_controllers.items()):
            try:
                await controller_os.async_deactivate()
            except Exception:
                _LOGGER.warning(
                    "Offset Sync: error deactivating controller for zone %s",
                    zone_id, exc_info=True,
                )
        self.offset_sync_controllers.clear()

    async def _teardown_reassigned_controllers(self, zone_ids: set[str]) -> None:
        """Pop and stop each reassigned or removed zone's controller without a hand-back or save, bypassing the config-change listener."""
        for zone_id in zone_ids:
            self._forget_eval_failures(zone_id)
            valve = self.valve_controllers.pop(zone_id, None)
            if valve is not None:
                # Broad catch: one controller's failed teardown must not stop
                # the rest of this batch from being torn down.
                try:
                    valve.discard()
                except Exception:
                    _LOGGER.warning(
                        "Smart Valve: error stopping controller for "
                        "reassigned or removed zone %s",
                        zone_id, exc_info=True,
                    )
            retired = self.retired_valve_controllers.pop(zone_id, None)
            if retired is not None:
                try:
                    retired.discard()
                except Exception:
                    _LOGGER.warning(
                        "Smart Valve: error stopping retiring controller for "
                        "reassigned or removed zone %s",
                        zone_id, exc_info=True,
                    )
            offset = self.offset_sync_controllers.pop(zone_id, None)
            if offset is not None:
                try:
                    offset.discard()
                except Exception:
                    _LOGGER.warning(
                        "Offset Sync: error stopping controller for "
                        "reassigned or removed zone %s",
                        zone_id, exc_info=True,
                    )

    def _on_zone_config_change(self, zone_id: str, key: str, value: Any) -> None:
        """Activate / deactivate per-zone controllers when the user changes config.

        Reacts to `svc_mode` and `external_temp_sensor` key changes.
        All dict mutation runs inside a per-zone lock via the inner
        `_transition()` task so two rapid callbacks serialise instead
        of racing on pop / install.
        """
        if key not in ("svc_mode", "external_temp_sensor"):
            return

        from .const import SVC_MODE_OFFSET_SYNC, SVC_MODE_VALVE_TARGET

        # Snapshot the user's intent synchronously so each scheduled
        # `_transition` carries the config state from its triggering
        # callback, not whatever's in config when the task eventually
        # runs (which could be a later, conflicting change).
        config = self.zone_config_manager.get_zone_config(zone_id)
        current_mode: str = (
            str(value) if key == "svc_mode" else config.get("svc_mode", "off")
        )
        ext_sensor: str = (
            str(value) if key == "external_temp_sensor" else config.get("external_temp_sensor", "")
        )

        zones_info = self.data_loader.get_cached("zones_info")
        if not (zones_info and isinstance(zones_info, list)):
            # The post-sync init pass builds the controller once a fetch brings zone info.
            self._valve_init_pending = True
            _LOGGER.warning(
                "Smart Valve: zone type for %s is not known yet (zone "
                "info not loaded), refusing this config change rather "
                "than risk activating a controller on a non-heating "
                "zone. It will take effect once zone info is available",
                zone_id,
            )
            return

        zone_type = next(
            (z.get("type") for z in zones_info if str(z.get("id")) == zone_id),
            None,
        )
        if zone_type != "HEATING":
            _LOGGER.warning(
                "Smart Valve: zone %s is a %s zone. Smart Valve "
                "Control only works with heating zones (with TRVs), "
                "skipping config change",
                zone_id, zone_type,
            )
            return

        if current_mode == SVC_MODE_VALVE_TARGET and ext_sensor:
            self._request_full_sync_next_cycle = True

        async def _transition() -> None:
            # Two-phase shutdown check: once before queueing for the
            # lock, once after acquiring it. A shutdown that begins
            # while this task was queued must not allow activation
            # after the shutdown sequence runs.
            if self._shutting_down:
                return
            lock = self._zone_transition_locks.setdefault(zone_id, asyncio.Lock())
            async with lock:
                if self._shutting_down:
                    return

                # Mutate the controller dicts inside the lock so a
                # second concurrent transition cannot race on
                # pop / install.
                old_valve = self.valve_controllers.pop(zone_id, None)
                old_offset = self.offset_sync_controllers.pop(zone_id, None)
                will_run_valve = current_mode == SVC_MODE_VALVE_TARGET and bool(ext_sensor)
                retired = None
                if will_run_valve:
                    # The new live controller restores the saved flag and hands back itself.
                    retired = self.retired_valve_controllers.pop(zone_id, None)
                    # The post-sync loop may still hold it in its snapshot, so stop it before any await.
                    if retired is not None and retired.retiring is True:
                        retired.discard()
                hand_back_owed = False

                if old_valve:
                    _LOGGER.debug(
                        "Smart Valve: deactivating zone %s valve_target "
                        "controller before config change",
                        zone_id,
                    )
                    try:
                        # `is True` so a mocked deactivate cannot register a retiring controller.
                        hand_back_owed = await old_valve.async_deactivate() is True
                    except Exception:
                        _LOGGER.warning(
                            "Smart Valve: zone %s deactivation raised an "
                            "exception, controller may not have shut "
                            "down cleanly",
                            zone_id, exc_info=True,
                        )
                    if hand_back_owed and not will_run_valve:
                        self.retired_valve_controllers[zone_id] = old_valve
                    elif will_run_valve and old_valve.retiring is True:
                        # Still owing a hand-back but replaced: stop it, no await since deactivate returned.
                        old_valve.discard()

                if old_offset:
                    if current_mode == SVC_MODE_VALVE_TARGET:
                        _LOGGER.warning(
                            "Smart Valve: zone %s switching from "
                            "offset_sync to valve_target, any non-zero "
                            "device offset still on the TRV may cause "
                            "double compensation. Reset the offset to 0 "
                            "via the set_temperature_offset service if "
                            "you see overshoot.",
                            zone_id,
                        )
                    _LOGGER.debug(
                        "Offset Sync: deactivating zone %s controller "
                        "before config change",
                        zone_id,
                    )
                    try:
                        await old_offset.async_deactivate()
                    except Exception:
                        _LOGGER.warning(
                            "Offset Sync: zone %s deactivation raised an "
                            "exception, controller may not have shut "
                            "down cleanly",
                            zone_id, exc_info=True,
                        )

                new_controller: Any = None
                registry: dict[str, Any] = self.valve_controllers
                if current_mode == SVC_MODE_VALVE_TARGET:
                    if not ext_sensor:
                        _LOGGER.debug(
                            "Smart Valve: zone %s set to valve_target "
                            "mode but no external sensor, skipping "
                            "activation",
                            zone_id,
                        )
                        self._raise_sensor_missing_repair(zone_id, current_mode)
                    else:
                        from .valve_controller import SmartValveController

                        self._clear_sensor_missing_repair(zone_id)
                        new_controller = SmartValveController(self.hass, self, zone_id)

                elif current_mode == SVC_MODE_OFFSET_SYNC:
                    if not ext_sensor:
                        _LOGGER.debug(
                            "Offset Sync: zone %s set to offset_sync mode "
                            "but no external sensor, skipping activation",
                            zone_id,
                        )
                        self._raise_sensor_missing_repair(zone_id, current_mode)
                    else:
                        from .offset_sync_controller import OffsetSyncController

                        self._clear_sensor_missing_repair(zone_id)
                        new_controller = OffsetSyncController(self.hass, self, zone_id)
                        registry = self.offset_sync_controllers

                else:
                    # svc_mode is "off" or unknown: no new controller,
                    # but clear any stale repair issue for this zone.
                    self._clear_sensor_missing_repair(zone_id)

                # After the old controllers are stopped, so a new controller's first failure is logged at WARNING.
                self._forget_eval_failures(zone_id)
                previous = (old_valve or retired) if will_run_valve else old_offset
                if new_controller and previous is not None:
                    new_controller.adopt_predecessor(previous)

                if new_controller:
                    try:
                        await self._async_start_controller(registry, zone_id, new_controller)
                    except Exception:
                        # Broad on purpose, like the deactivate catches above: a failed activate must not end this task silently.
                        # The post-sync init pass rebuilds the zone once a zone update arrives.
                        self._valve_init_pending = True
                        _LOGGER.warning(
                            "Smart Valve: zone %s %s controller could not be activated, "
                            "it will be set up again after the next zone update",
                            zone_id, current_mode, exc_info=True,
                        )
                    else:
                        _LOGGER.info(
                            "Smart Valve: zone %s %s controller activated",
                            zone_id, current_mode,
                        )

        self.hass.async_create_task(_transition())

    def _raise_sensor_missing_repair(self, zone_id: str, mode: str) -> None:
        """Surface an HA repair notification when the user's zone has no external sensor.

        The repair is idempotent: HA's issue registry updates an
        existing issue with the same id rather than duplicating it.
        """
        try:
            from .repair_helpers import SVC_SENSOR_MISSING_ISSUE, async_create_issue

            zones_info_raw = self.data_loader.get_cached("zones_info")
            zones_info = zones_info_raw if isinstance(zones_info_raw, list) else []
            zone_name = next(
                (z.get("name", f"Zone {zone_id}") for z in zones_info
                 if str(z.get("id")) == zone_id),
                f"Zone {zone_id}",
            )

            async_create_issue(
                self.hass,
                SVC_SENSOR_MISSING_ISSUE,
                home_id=self.home_id,
                zone_id=zone_id,
                placeholders={"zone_name": zone_name, "mode": mode},
            )
        except (KeyError, ValueError, AttributeError):
            _LOGGER.debug(
                "Coordinator: could not raise sensor-missing repair for "
                "zone %s. HA issue registry call failed",
                zone_id, exc_info=True,
            )

    def _clear_sensor_missing_repair(self, zone_id: str) -> None:
        """Drop the sensor-missing repair notification for one zone."""
        try:
            from .repair_helpers import SVC_SENSOR_MISSING_ISSUE, async_dismiss_issue

            async_dismiss_issue(
                self.hass,
                SVC_SENSOR_MISSING_ISSUE,
                home_id=self.home_id,
                zone_id=zone_id,
            )
        except (KeyError, ValueError, AttributeError):
            _LOGGER.debug(
                "Coordinator: could not clear sensor-missing repair for "
                "zone %s. HA issue registry call failed",
                zone_id, exc_info=True,
            )

