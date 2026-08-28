"""Tado CE coordinator: adaptive polling, sub-system orchestration, entity propagation."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta
import logging
import sys
import time
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .const import (
    BRIDGE_POLL_INTERVAL_SECONDS,
    DEVICE_SYNC_QUEUE_MAX_DEPTH,
    DOMAIN,
    ENTITY_FRESHNESS_EXPIRY_SECONDS,
    FORCEABLE_FETCH_HOME_STATE,
    FORCEABLE_FETCH_MOBILE,
    FORCEABLE_FETCH_WEATHER,
    HOMEKIT_SAVINGS_RESET_MIN_JUMP,
    HOMEKIT_SAVINGS_RESET_RATIO,
    MAPPING_RETRY_STALL_LIMIT,
    OFFSET_DRIFT_REFRESH_SECONDS,
    OUTDOOR_TEMP_HISTORY_MAX,
    OVERLAY_MODE_DEFAULT,
    TIMER_DURATION_DEFAULT,
    ZONES_INFO_FREE_TIER_THRESHOLD,
    ZONES_INFO_REFRESH_SECONDS_FREE,
    ZONES_INFO_REFRESH_SECONDS_PAID,
    get_climate_zone_ids,
    is_climate_zone,
)
from .exceptions import TadoAuthError, TadoBridgeApiError, TadoRateLimitError, TadoSyncError
from .helpers import low_quota_threshold, mask_serial
from .homekit_mapping import async_rebuild_and_save_mapping
from .insight_history import InsightHistoryTracker
from .polling import get_polling_interval, should_pause_polling
from .ratelimit import _sanitize_retry_after
from .weather_compensation import (
    WeatherCompensationState,
)
from .write_health_tracker import WriteHealthTracker
from .write_optimizer import ActionDebouncer, DeviceSyncQueue, RefreshCoalescer
from .zone_fingerprint import ZoneFingerprintDelta, ZoneFingerprintTracker

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.const import Platform
    from homeassistant.core import HomeAssistant

    from .adaptive_preheat import AdaptivePreheatManager
    from .api_call_tracker import APICallTracker
    from .api_client import TadoApiClient
    from .bridge_api import TadoBridgeApiClient
    from .bridge_health import BridgeHealthTracker
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

_LOGGER = logging.getLogger(__name__)


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
        initial_interval = get_polling_interval(config_manager)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} ({config_manager.get_all_config().get('home_id', 'default')})",
            update_interval=timedelta(minutes=initial_interval),
        )

        # Per-entry dependencies (accessible by entities via self.coordinator)
        self.config_manager = config_manager
        self.zone_config_manager = zone_config_manager
        self.data_loader = data_loader
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
        self._freshness_cleanup_cancel: Callable[[], None] | None = None
        self._global_sequence: int = 0

        self._heating_cycle_timeout_cancel: Callable[[], None] | None = None

        self.overlay_mode: str = OVERLAY_MODE_DEFAULT
        self.timer_duration: int = TIMER_DURATION_DEFAULT

        # Outdoor temp history (owned by coordinator; async load, debounced sync save)
        self._outdoor_temp_history: list[float] = []
        self._outdoor_temp_loaded: bool = False

        # Shared entity data store, entities publish computed values here
        # so other components (insight collector, adaptive preheat) can read
        # without cross-entity hass.states.get() coupling.
        #
        # LIFECYCLE: In-memory only, not persisted across restarts.
        # Populated progressively as entities process their first poll data.
        # Consumers MUST handle None returns from get_entity_data() during
        # the startup gap (typically 0-2 poll cycles after restart).
        #
        # Structure: {zone_id: {"condensation_risk": {...}, "window_predicted": {...}, ...}}
        self.entity_data: dict[str, dict[str, dict[str, Any]]] = {}

        # Pending cleanup flags, set by options flow, consumed by migration
        # after reload. Avoids transient hass.data[DOMAIN_*] keys.
        self._pending_cleanup: dict[str, dict[str, bool]] = {}

        self.insight_history = InsightHistoryTracker(hass, self.home_id)

        # Insight collector mutable state, owned by coordinator so duration
        # tracking survives individual sensor entities being disabled by the user.
        # anomaly_start_times drives "heating anomaly for N minutes"; humidity
        # histories drive humidity-trend insights.
        self._insight_anomaly_start_times: dict[str, datetime] = {}
        self._insight_humidity_histories: dict[str, list[Any]] = {}

        self._last_full_sync: datetime | None = None
        self._cached_ratelimit: dict[str, Any] | None = None

        self._zone_fingerprint = ZoneFingerprintTracker()
        self._request_full_sync_next_cycle: bool = False

        # Polling pause state, set when should_pause_polling returns
        # True, cleared on the first cycle that returns False after a
        # pause. Used by should_pause_polling to suppress the
        # "resuming polling" INFO line when the user was never paused.
        self._was_paused: bool = False

        # Periodic offset drift refresh tracker. The full sync's
        # _sync_offsets pass updates this stamp; the post-sync drift
        # check fires when the stamp ages past
        # OFFSET_DRIFT_REFRESH_SECONDS.
        self._last_offset_resync: datetime | None = None

        # Round-robin drift-refresh cursor: zone_id of the last zone
        # refreshed. Keyed by zone_id (not list position) so it self-heals on
        # zone add/remove. In-memory; restarting at the list head on reboot is
        # harmless (drift refresh is a reconcile, not a write).
        self._last_refreshed_zone_id: str | None = None

        # Beta.3: rate-limit window observability + once-per-transition log gating
        self._rate_limited_until: datetime | None = None
        self._cloud_unavailable_logged: bool = False

        # Bridge API client (lazy-init from options when bridge credentials present)
        self.bridge_api_client: TadoBridgeApiClient | None = None

        self.bridge_health_tracker: BridgeHealthTracker | None = None
        self._bridge_first_fetch_logged: bool = False

        # Independent bridge poll task (bridge API doesn't count toward cloud quota)
        self._bridge_poll_task: asyncio.Task[None] | None = None
        self._cached_bridge_data: dict[str, object] | None = None

        # Platforms loaded during setup (used by unload to match exactly)
        self.loaded_platforms: frozenset[Platform] = frozenset()

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
        self._last_weather_fetch: datetime | None = None
        self._last_home_state_fetch: datetime | None = None
        self._last_mobile_devices_fetch: datetime | None = None
        self._mapping_retry_stall_count: int = 0
        self._prev_homekit_connected: bool = False
        self._homekit_reads_saved: int = 0
        self._homekit_writes_saved: int = 0
        self._prev_savings_remaining: int | None = None  # for reset detection

        # HomeKit write metrics (ephemeral, reset on quota reset and reconnect)
        self._homekit_write_attempts: int = 0
        self._homekit_write_successes: int = 0
        self._homekit_write_fallbacks: int = 0
        self._homekit_write_latency_sum: float = 0.0
        self._homekit_write_latency_count: int = 0

        self.write_health_tracker: WriteHealthTracker | None = None

        # Smart Valve Control: per-zone proportional offset controllers
        self.valve_controllers: dict[str, SmartValveController] = {}

        # Offset Sync: per-zone device offset controllers
        self.offset_sync_controllers: dict[str, OffsetSyncController] = {}

        # Per-zone locks serialise controller lifecycle changes.
        self._zone_transition_locks: dict[str, asyncio.Lock] = {}

        # Set by async_shutdown_valve_controllers to prevent queued transitions
        # from installing/activating controllers after shutdown begins.
        self._shutting_down: bool = False

        # Weather compensation mutable runtime state (persists across polls)
        self._wc_state = WeatherCompensationState()
        self._wc_state_loaded: bool = False

        self._action_debouncer = ActionDebouncer(
            default_window=float(config_manager.get_smart_actions_debounce_seconds()),
        )
        self._device_sync_queue = DeviceSyncQueue(
            delay=config_manager.get_device_sync_delay_seconds(),
            max_depth=DEVICE_SYNC_QUEUE_MAX_DEPTH,
        )
        debounce_window = config_manager.get_smart_actions_debounce_seconds()
        self._refresh_coalescer = RefreshCoalescer(
            coordinator=self,
            window=2.0,
            skip_when_fresh=(debounce_window > 0),
        )

    @property
    def outdoor_temp_history(self) -> list[float]:
        """Return outdoor temp history (read-only access for sensors)."""
        return self._outdoor_temp_history

    @property
    def is_homekit_active(self) -> bool:
        """Return True when HomeKit local provider is wired and currently connected."""
        return self.homekit_provider is not None and self.homekit_provider.is_connected

    def _log_cloud_unavailable(self, exc: Exception | str) -> None:
        """Log INFO on the first cloud→unreachable transition (idempotent).

        Accepts a plain reason string as well, so the pre-emptive quota
        pause can reuse it without inventing an exception.
        """
        if not self._cloud_unavailable_logged:
            _LOGGER.info(
                "Coordinator: Tado cloud unreachable (%s), keeping "
                "entities live on HomeKit local data",
                exc,
            )
            self._cloud_unavailable_logged = True

    def _log_cloud_available(self) -> None:
        """Log INFO on the cloud→reachable transition (idempotent)."""
        if self._cloud_unavailable_logged:
            _LOGGER.info("Coordinator: Tado cloud reachable again")
            self._cloud_unavailable_logged = False

    @property
    def action_debouncer(self) -> ActionDebouncer:
        """Return the action debouncer instance."""
        return self._action_debouncer

    @property
    def device_sync_queue(self) -> DeviceSyncQueue:
        """Return the device sync queue instance."""
        return self._device_sync_queue

    @property
    def refresh_coalescer(self) -> RefreshCoalescer:
        """Return the refresh coalescer instance."""
        return self._refresh_coalescer

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

    def _reset_write_metrics(self) -> None:
        """Reset HomeKit write metrics counters."""
        self._homekit_write_attempts = 0
        self._homekit_write_successes = 0
        self._homekit_write_fallbacks = 0
        self._homekit_write_latency_sum = 0.0
        self._homekit_write_latency_count = 0

    def publish_entity_data(self, zone_id: str, key: str, data: dict[str, Any]) -> None:
        """Publish in-memory computed entity data; consumers must tolerate None during the startup gap."""
        if zone_id not in self.entity_data:
            self.entity_data[zone_id] = {}
        self.entity_data[zone_id][key] = data

    def get_entity_data(self, zone_id: str, key: str) -> dict[str, Any] | None:
        """Read published entity data for a zone, or None if not published yet."""
        return self.entity_data.get(zone_id, {}).get(key)

    async def _async_post_sync_processing(
        self, zone_data: dict[str, Any] | list[Any] | None, weather_data: dict[str, Any] | list[Any] | None,
    ) -> dict[str, Any]:
        """Run post-sync processing: history detection, cache reads, bridge, WC."""
        if self._cloud_unavailable_logged:
            from .repair_helpers import async_dismiss_rate_limit_issue

            self._log_cloud_available()
            async_dismiss_rate_limit_issue(self.hass, self.home_id)
            self._rate_limited_until = None

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
        ratelimit_data = self.data_loader.get_cached("ratelimit")
        api_call_history_data: dict[str, Any] = dict(self.api_tracker._call_history) if self.api_tracker else {}
        zones_info_data = self.data_loader.get_cached("zones_info")
        mobile_devices_data = self.data_loader.get_cached("mobile_devices")
        home_devices_data = self.data_loader.get_cached("home_devices")

        # Periodic offset drift refresh: fire BEFORE snapshotting
        # `offsets` so the result dict carries the freshly re-fetched
        # values. See _maybe_resync_offsets for the gating logic.
        ratelimit_for_resync = ratelimit_data if isinstance(ratelimit_data, dict) else None
        await self._maybe_resync_offsets(zones_info_data, ratelimit_for_resync)

        # Detect AC re-pair / hardware swap and refresh capabilities BEFORE
        # snapshotting `ac_capabilities` below, so the result dict carries the
        # fresh value. No-op when there are no AC zones or nothing changed.
        await self._reconcile_ac_capabilities_fingerprint()

        offsets_data = self.data_loader.get_cached("offsets")
        schedules_data = self.data_loader.get_cached("schedules")
        ac_capabilities = self.data_loader.get_cached("ac_capabilities")
        heating_circuits_data = self.data_loader.get_cached("heating_circuits")
        heating_circuit_control_data = self.data_loader.get_cached("heating_circuit_control")

        await self._accumulate_outdoor_temp_history(weather_data)

        await self._notify_heating_cycle_updates(zone_data)

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
        wc_data = await self._async_run_weather_compensation(
            bridge_data, _zone_dict, _weather_dict,
        )
        if wc_data is not None:
            self.data_loader.save_wc_state(self._wc_state.to_dict())

        result: dict[str, Any] = {
            "zones": zone_data or {},
            "config": config_data or {},
            "home_state": home_state_data or {},
            "ac_capabilities": ac_capabilities or {},
            "ratelimit": ratelimit_data or {},
            "api_call_history": api_call_history_data or {},
            "zones_info": zones_info_data or [],
            "weather": weather_data or {},
            "mobile_devices": mobile_devices_data or [],
            "home_devices": home_devices_data or [],
            "offsets": offsets_data or {},
            "schedules": schedules_data or {},
            "heating_circuits": heating_circuits_data or {},
            "heating_circuit_control": heating_circuit_control_data or {},
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
        # that by a few lines; finally restores on any exception.
        previous_data = self.data
        self.data = result
        try:
            from .sensor_insight_collector import collect_zone_insights

            zone_insights = collect_zone_insights(
                self.hass, self,
                self._insight_anomaly_start_times,
                self._insight_humidity_histories,
            )
            result["zone_insights"] = zone_insights

            all_insights: list[Any] = []
            for insights_list in zone_insights.values():
                all_insights.extend(insights_list)
            self.insight_history.update(all_insights, dt_util.utcnow())
            # Persist insight runtime state (anomaly timers + humidity history)
            # so dashboard duration counters survive HA restarts.
            self._save_insight_runtime_state()
        finally:
            self.data = previous_data

        await self._handle_state_restore_updates(home_state_data, result)

        for controller in self.valve_controllers.values():
            try:
                await controller.async_evaluate()
            except TadoRateLimitError as e:
                self.record_cloud_backoff(e.retry_after)
                _LOGGER.warning(
                    "Smart Valve: zone %s evaluation hit Tado rate limit, "
                    "backing off, will retry after the quota window",
                    controller.zone_id,
                )
            except (TimeoutError, aiohttp.ClientError, ValueError, KeyError):
                _LOGGER.warning(
                    "Smart Valve: zone %s evaluation raised an exception, "
                    "controller will retry on next poll",
                    controller.zone_id, exc_info=True,
                )

        for controller_os in self.offset_sync_controllers.values():
            try:
                await controller_os.async_evaluate()
            except TadoRateLimitError as e:
                self.record_cloud_backoff(e.retry_after)
                _LOGGER.warning(
                    "Offset Sync: zone %s evaluation hit Tado rate limit, "
                    "backing off, will retry after the quota window",
                    controller_os.zone_id,
                )
            except (TimeoutError, aiohttp.ClientError, ValueError, KeyError):
                _LOGGER.warning(
                    "Offset Sync: zone %s evaluation raised an exception, "
                    "controller will retry on next poll",
                    controller_os.zone_id, exc_info=True,
                )

        await self._async_retry_homekit_mapping()

        return result

    async def _async_retry_homekit_mapping(self) -> None:
        """Rebuild the mapping each poll until every mappable zone is covered.

        Stops on completion, or after MAPPING_RETRY_STALL_LIMIT polls with no
        newly-mapped zone (an accessory the bridge never exposes).
        """
        from .homekit_mapping import mapping_covers_all_zones

        client = self.homekit_client
        if client is None or not client.is_connected:
            return
        zones_info = (self.data or {}).get("zones_info") or []
        if not zones_info:
            return
        if mapping_covers_all_zones(client.zone_aid_map, zones_info):
            return
        if self._mapping_retry_stall_count >= MAPPING_RETRY_STALL_LIMIT:
            return
        zones_mapped_before = len(client.zone_aid_map)
        try:
            mapping = await async_rebuild_and_save_mapping(
                self.hass, client, self.home_id, zones_info,
            )
        except (TimeoutError, aiohttp.ClientError, ValueError, KeyError):
            _LOGGER.warning(
                "HomeKit: zone mapping rebuild raised an exception, "
                "will retry on the next poll", exc_info=True,
            )
            return
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

    def reset_mapping_retry_budget(self) -> None:
        """Re-arm the mapping-retry stall budget after a reconnect or zone change."""
        self._mapping_retry_stall_count = 0

    def record_cloud_backoff(self, retry_after: int) -> None:
        """Record a cloud rate-limit window and surface the repair issue.

        Shared by the main poll dispatch and the background-write
        dispatcher so both honour the same backoff state.
        """
        from .repair_helpers import async_create_rate_limit_issue

        self._rate_limited_until = dt_util.utcnow() + timedelta(seconds=retry_after)
        async_create_rate_limit_issue(self.hass, self.home_id, retry_after=retry_after)

    def is_cloud_backoff_active(self) -> bool:
        """Return True while the cloud rate-limit backoff window holds."""
        until = self._rate_limited_until
        if until is None:
            return False
        return dt_util.utcnow() < until

    def last_zone_fetch_ts(self) -> datetime | None:
        """Return the UTC time zone-states were last actually fetched.

        Marks when every zone's insideTemperature last refreshed from cloud
        (one zone-states payload carries all zones). The Offset Sync
        settling-gate reads this to know its last write has been reflected.
        """
        return self._last_cloud_zone_fetch

    def request_forced_fetch(self, fetch_type: str) -> None:
        """Force the next poll to bypass the floor for one slow-data type."""
        self.refresh_handler.request_forced_fetch(fetch_type)

    def _requeue_forced_fetch(self, forced_fetch: set[str]) -> None:
        """Re-queue forced types that a poll consumed but did not fetch."""
        for fetch_type in forced_fetch:
            self.refresh_handler.request_forced_fetch(fetch_type)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Tado API. Dynamically adjusts update_interval."""
        was_failing = self.last_update_success is False

        self._load_ratelimit_from_cache()
        if self._cached_ratelimit:
            should_pause, reason = should_pause_polling(
                self._cached_ratelimit,
                self.config_manager,
                was_paused=self._was_paused,
            )
            if should_pause:
                _LOGGER.warning("Coordinator: %s", reason)
                self._was_paused = True
                self.update_interval = timedelta(minutes=15)
                if self.is_homekit_active:
                    # The local provider is still serving this home, so the
                    # zones keep updating without spending a cloud call.
                    # Raising UpdateFailed here would mark every
                    # CoordinatorEntity unavailable for the whole quota
                    # window; hold the last cloud snapshot instead and let
                    # StateReconciler merge fresh local values over it, the
                    # same way the TadoRateLimitError handler below does.
                    self._log_cloud_unavailable(reason)
                    return self.data or {}
                # retry_after lets HA defer the next refresh precisely.
                reset_seconds = self._cached_ratelimit.get("reset_seconds")
                retry_after = _sanitize_retry_after(reset_seconds)
                _LOGGER.debug(
                    "Coordinator: rate-limited, deferring next refresh by %ds",
                    retry_after,
                )
                raise UpdateFailed(reason, retry_after=retry_after)
            self._was_paused = False

        homekit_connected = (
            self.homekit_provider is not None
            and self.homekit_provider.is_connected
        )
        new_interval = get_polling_interval(
            self.config_manager, self._cached_ratelimit, homekit_connected=homekit_connected,
        )
        self.update_interval = timedelta(minutes=new_interval)

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

        do_full_sync = self._should_do_full_sync()

        zone_only, force_zone_fetch, forced_fetch = self.refresh_handler.consume_pending_flags()
        if forced_fetch:
            # zone-only skips the quick-extras fetch, which would swallow the forced type.
            zone_only = False

        cm = self.config_manager

        # If user explicitly set a custom polling interval, respect it:
        # fetch all data every cycle so humidity/heating power/weather stay
        # fresh at the user's chosen rate. Only use HomeKit skip logic when
        # polling is automatic (no custom interval).
        user_has_custom = (
            cm.get_custom_day_interval() is not None
            or cm.get_custom_night_interval() is not None
        )

        skip_zone_states = False
        if force_zone_fetch:
            skip_zone_states = False
        elif homekit_connected and self._last_cloud_zone_fetch is not None and not user_has_custom:
            cloud_sync_minutes = cm.get_homekit_cloud_sync_minutes()
            elapsed = (dt_util.utcnow() - self._last_cloud_zone_fetch).total_seconds() / 60
            skip_zone_states = elapsed < cloud_sync_minutes

        # Slow-changing data gates on its own per-type floor, so a fast zone
        # cycle doesn't drag it along (see _should_skip_by_floor).
        skip_weather = self._should_skip_by_floor(
            self._last_weather_fetch, cm.get_weather_min_refresh_minutes(),
            homekit_connected=homekit_connected,
        ) and FORCEABLE_FETCH_WEATHER not in forced_fetch
        skip_home_state = self._should_skip_by_floor(
            self._last_home_state_fetch, cm.get_presence_min_refresh_minutes(),
            homekit_connected=homekit_connected,
        ) and FORCEABLE_FETCH_HOME_STATE not in forced_fetch
        skip_mobile_devices = self._should_skip_by_floor(
            self._last_mobile_devices_fetch, cm.get_mobile_devices_min_refresh_minutes(),
            homekit_connected=homekit_connected,
        ) and FORCEABLE_FETCH_MOBILE not in forced_fetch

        try:
            await self.api_client.async_sync(
                quick=not do_full_sync,
                skip_zone_states=skip_zone_states,
                zone_only=zone_only,
                weather_enabled=cm.get_weather_enabled() and not skip_weather,
                mobile_devices_enabled=cm.get_mobile_devices_enabled(),
                mobile_devices_frequent_sync=(
                    cm.get_mobile_devices_frequent_sync() and not skip_mobile_devices
                ),
                offset_enabled=cm.get_offset_enabled(),
                home_state_sync_enabled=cm.get_home_state_sync_enabled() and not skip_home_state,
            )
        except TadoRateLimitError as e:
            self.record_cloud_backoff(e.retry_after)
            self._requeue_forced_fetch(forced_fetch)

            if self.is_homekit_active:
                self._log_cloud_unavailable(e)
                return self.data or {}

            raise UpdateFailed(
                f"Tado API rate-limited, retry after {max(1, e.retry_after // 60)} min",
                retry_after=float(e.retry_after),
            ) from e
        except TadoAuthError as e:
            from .repair_helpers import async_create_auth_issue

            self._requeue_forced_fetch(forced_fetch)
            async_create_auth_issue(self.hass, self.home_id)
            raise ConfigEntryAuthFailed(
                "Refresh token expired; user must re-authenticate",
            ) from e
        except TadoSyncError as e:
            self._requeue_forced_fetch(forced_fetch)
            if self.is_homekit_active:
                self._log_cloud_unavailable(e)
                return self.data or {}
            raise UpdateFailed(f"Tado CE sync failed: {e}") from e

        if do_full_sync:
            self._last_full_sync = dt_util.utcnow()
            self._request_full_sync_next_cycle = False
            # Stamp only when the full sync actually fetched offsets (same
            # gate as the fetch), so the drift refresh skips one interval
            # rather than double-fetching. If offsets weren't fetched, leave
            # the stamp so the drift refresh isn't wrongly suppressed.
            if cm.get_offset_enabled():
                self._last_offset_resync = dt_util.utcnow()

        # Reset HomeKit savings counters when API quota resets.
        # Detection: remaining jumps up significantly (e.g. 100 → 5000).
        # This mirrors api_client._calculate_live_ratelimit's reset detection.
        rl_data = self.data_loader.get_cached("ratelimit")
        if isinstance(rl_data, dict):
            current_remaining = rl_data.get("remaining")
            if (
                current_remaining is not None
                and self._prev_savings_remaining is not None
                and current_remaining > self._prev_savings_remaining + max(
                    HOMEKIT_SAVINGS_RESET_MIN_JUMP,
                    int(rl_data.get("limit", 5000) * HOMEKIT_SAVINGS_RESET_RATIO),
                )
            ):
                _LOGGER.info(
                    "Coordinator: Tado API quota reset detected "
                    "(remaining %s → %s), zeroing HomeKit savings "
                    "counters (was reads=%s, writes=%s)",
                    self._prev_savings_remaining, current_remaining,
                    self._homekit_reads_saved, self._homekit_writes_saved,
                )
                self._homekit_reads_saved = 0
                self._homekit_writes_saved = 0
                self._reset_write_metrics()
                self._save_homekit_savings()
            self._prev_savings_remaining = current_remaining

        if not skip_zone_states:
            self._last_cloud_zone_fetch = dt_util.utcnow()
        else:
            self.record_homekit_read_saved()

        if not skip_weather and cm.get_weather_enabled() and not zone_only:
            self._last_weather_fetch = dt_util.utcnow()
            forced_fetch.discard(FORCEABLE_FETCH_WEATHER)
        if not skip_home_state and cm.get_home_state_sync_enabled() and not zone_only:
            self._last_home_state_fetch = dt_util.utcnow()
            forced_fetch.discard(FORCEABLE_FETCH_HOME_STATE)
        if (
            not skip_mobile_devices
            and cm.get_mobile_devices_enabled()
            and cm.get_mobile_devices_frequent_sync()
            and not zone_only
        ):
            self._last_mobile_devices_fetch = dt_util.utcnow()
            forced_fetch.discard(FORCEABLE_FETCH_MOBILE)
        # Drop forced types for disabled features (a no-op that would re-queue
        # forever); only transiently gated-out types below are re-queued to retry.
        if not cm.get_home_state_sync_enabled():
            forced_fetch.discard(FORCEABLE_FETCH_HOME_STATE)
        if not (cm.get_mobile_devices_enabled() and cm.get_mobile_devices_frequent_sync()):
            forced_fetch.discard(FORCEABLE_FETCH_MOBILE)
        self._requeue_forced_fetch(forced_fetch)

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
            "skip" if skip_zone_states else "fetch",
            "skip" if skip_weather or zone_only else ("fetch" if cm.get_weather_enabled() else "off"),
            "yes" if homekit_connected else "no",
            new_interval,
            "yes" if do_full_sync else "no",
        )

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

        return await self._async_post_sync_processing(zone_data, weather_data)

    async def _maybe_resync_offsets(
        self,
        zones_info_data: dict[str, Any] | list[Any] | None,
        ratelimit_data: dict[str, Any] | None,
    ) -> None:
        """Re-fetch device offsets from Tado when the cache may be stale.

        Runs from `_async_post_sync_processing` to catch server-side
        offset drift (Tado's own calibration or a manual Tado-app edit)
        that HA never wrote and so never readback-verified. Gated on
        three conditions, all must hold:

        1. Offset feature enabled.
        2. Drift-refresh floor elapsed. With HomeKit connected the floor
           is `max(OFFSET_DRIFT_REFRESH_SECONDS, homekit_cloud_sync_minutes)`;
           otherwise `OFFSET_DRIFT_REFRESH_SECONDS` alone.
        3. Quota not below the tier-aware low-quota threshold: safe to
           skip under quota pressure because the per-write readback gate
           still protects the cache.
        """
        if not self.config_manager.get_offset_enabled():
            return

        if not zones_info_data or not isinstance(zones_info_data, list):
            return

        effective_floor = OFFSET_DRIFT_REFRESH_SECONDS
        homekit_connected = (
            self.homekit_provider is not None
            and self.homekit_provider.is_connected
        )
        if homekit_connected:
            hk_sync_seconds = self.config_manager.get_homekit_cloud_sync_minutes() * 60
            effective_floor = max(OFFSET_DRIFT_REFRESH_SECONDS, hk_sync_seconds)

        last = self._last_offset_resync
        if last is not None:
            age = (dt_util.utcnow() - last).total_seconds()
            if age < effective_floor:
                return

        if isinstance(ratelimit_data, dict):
            remaining = ratelimit_data.get("remaining")
            limit = ratelimit_data.get("limit")
            if isinstance(remaining, int) and remaining < low_quota_threshold(limit):
                _LOGGER.debug(
                    "Offset Sync: skipping drift refresh. Tado API quota "
                    "low (%s call(s) remaining)",
                    remaining,
                )
                return

        # Round-robin: refresh ONE climate zone per cycle, advancing a
        # zone_id-keyed cursor, so an N-zone home costs 1 cloud call/cycle
        # instead of an N-call burst. Per-zone refresh interval becomes
        # cadence x N. The cursor advances only after a successful fetch, so a
        # quota-skipped cycle (above) retries the same zone next time.
        climate_ids = sorted(
            str(z.get("id"))
            for z in zones_info_data
            if is_climate_zone(z.get("type") or "")
        )
        if not climate_ids:
            return
        target = self._next_round_robin_zone(climate_ids)

        try:
            calls_made = await self.api_client.async_resync_offsets(
                zones_info_data, target_zone_id=target,
            )
        except (TadoAuthError, TadoRateLimitError) as e:
            # Best-effort refresh: it doesn't raise to a caller, but a revoked
            # token or exhausted quota still needs the same recovery the other
            # paths get (start reauth / record the back-off) so a token
            # revoked during the drift refresh surfaces now, not only on the
            # next poll. The readback gate still protects the cache meanwhile.
            from .error_dispatch import _apply_cloud_error_recovery

            _apply_cloud_error_recovery(e, self.config_entry, self.hass, self)
            _LOGGER.debug(
                "Offset Sync: drift refresh hit %s, recovery started, "
                "readback gate still protects the cache",
                type(e).__name__,
            )
            return
        except TadoSyncError:
            # Transient sync error on a best-effort refresh: swallow and retry
            # next poll. Programmer bugs (KeyError / AttributeError / …) are
            # deliberately not caught, so they propagate and HA logs them.
            _LOGGER.debug(
                "Offset Sync: drift refresh fetch failed, readback gate "
                "still protects the cache, will retry next poll",
                exc_info=True,
            )
            return

        self._last_refreshed_zone_id = target
        self._last_offset_resync = dt_util.utcnow()
        _LOGGER.debug(
            "Offset Sync: drift refresh complete, zone %s reconciled with "
            "Tado, %s cloud call(s) used this cycle",
            target, calls_made,
        )

    def _next_round_robin_zone(self, climate_ids: list[str]) -> str:
        """Pick the next zone_id after the cursor in sorted order, wrapping.

        Self-heals (returns the list head) when the cursor zone is gone
        (zone removed / re-paired), so the round-robin never skips or
        crashes on a topology change.
        """
        last = self._last_refreshed_zone_id
        if last is None or last not in climate_ids:
            return climate_ids[0]
        idx = climate_ids.index(last)
        return climate_ids[(idx + 1) % len(climate_ids)]

    async def _reconcile_ac_capabilities_fingerprint(self) -> None:
        """Detect AC re-pair / hardware swap and force a capabilities re-fetch.

        Reads the persisted fingerprint sidecar as the baseline (so a re-pair
        that happened before a reboot is still caught on the first post-reboot
        poll, when the in-memory tracker has no baseline), diffs it against the
        fresh `zones_info` device fingerprints, and re-fetches only the changed
        zones. Writes the fresh fingerprints back to the sidecar. A no-op when
        there are no AC zones or nothing changed.
        """
        from .zone_fingerprint import ac_device_fingerprints_changed

        zones_info = self.data_loader.get_cached("zones_info")
        if not isinstance(zones_info, list):
            return

        prev_raw = self.data_loader.get_cached("ac_capabilities_fp")
        prev_fp = dict(prev_raw) if isinstance(prev_raw, dict) else {}

        changed, fresh = ac_device_fingerprints_changed(zones_info, prev_fp)

        if fresh != prev_fp:
            await self.data_loader.async_update_store("ac_capabilities_fp", fresh)

        if changed:
            # Don't mark the whole cache dirty; force_zone_ids re-fetches
            # exactly the changed zones while _sync_ac_capabilities keeps the
            # rest from the live cache. Marking dirty would empty that merge
            # base, refetching every AC zone and risking a transient blip on an
            # unchanged zone dropping its cached caps.
            _LOGGER.info(
                "AC capabilities: device change detected for zone(s) %s, "
                "refreshing from cloud",
                sorted(changed),
            )
            await self.api_client._sync_ac_capabilities(
                zones_info, force_zone_ids=changed,
            )

    async def _accumulate_outdoor_temp_history(
        self, weather_data: dict[str, Any] | list[Any] | None,
    ) -> None:
        """Append the latest outdoor reading to the history ring buffer and persist."""
        if not weather_data:
            return
        outdoor_temp = (weather_data.get("outsideTemperature") or {}).get("celsius")  # type: ignore[union-attr]
        if outdoor_temp is None:
            return

        # Defensive guard: should already be loaded during setup, but
        # if a future refactor drops the eager load this branch keeps
        # outdoor temp history working.
        if not self._outdoor_temp_loaded:
            _LOGGER.warning(
                "Coordinator: outdoor temperature history not eagerly "
                "loaded during setup, loading now",
            )
            self._outdoor_temp_history = await self.data_loader.async_load_outdoor_temp_history()
            self._outdoor_temp_loaded = True

        prev_last = self._outdoor_temp_history[-1] if self._outdoor_temp_history else None

        self._outdoor_temp_history.append(outdoor_temp)
        if len(self._outdoor_temp_history) > OUTDOOR_TEMP_HISTORY_MAX:
            del self._outdoor_temp_history[: len(self._outdoor_temp_history) - OUTDOOR_TEMP_HISTORY_MAX]

        # Only schedule Store write when the new reading differs from the previous
        if outdoor_temp != prev_last:
            self.data_loader.save_outdoor_temp_history(self._outdoor_temp_history)

    async def _notify_heating_cycle_updates(
        self, zone_data: dict[str, Any] | list[Any] | None,
    ) -> None:
        """Forward each zone's target / current temperature to the heating-cycle coordinator."""
        if not self.heating_cycle_coordinator or not zone_data:
            return
        # Tado API may return null for 'zoneStates'; 'or {}' handles None correctly
        zone_states = zone_data.get("zoneStates") or {}  # type: ignore[union-attr]
        for zone_id, data in zone_states.items():
            try:
                setting = data.get("setting") or {}
                target_temp = (setting.get("temperature") or {}).get("celsius")
                sensor_data = data.get("sensorDataPoints") or {}
                current_temp = (sensor_data.get("insideTemperature") or {}).get("celsius")
                if target_temp is not None and current_temp is not None:
                    await self.heating_cycle_coordinator.on_zone_update(
                        zone_id,
                        target_temp,
                        current_temp,
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

    def _zones_info_refresh_seconds(self) -> int:
        # Falls back to the paid interval until the ratelimit cache
        # populates so a valid paid-tier user doesn't get the slower
        # cadence on the first poll after restart.
        rl = self.data_loader.get_cached("ratelimit")
        if isinstance(rl, dict):
            limit = rl.get("limit")
            if isinstance(limit, int) and limit <= ZONES_INFO_FREE_TIER_THRESHOLD:
                return ZONES_INFO_REFRESH_SECONDS_FREE
        return ZONES_INFO_REFRESH_SECONDS_PAID

    def _should_do_full_sync(self) -> bool:
        """Whether the next sync should fetch device-info as well as zone state."""
        if self._last_full_sync is None:
            return True
        if self._request_full_sync_next_cycle:
            return True
        age = (dt_util.utcnow() - self._last_full_sync).total_seconds()
        return age >= self._zones_info_refresh_seconds()

    def _should_skip_by_floor(
        self,
        last_fetch: datetime | None,
        base_floor_minutes: int,
        *,
        homekit_connected: bool,
    ) -> bool:
        """Return True when a slow-data fetch is still within its floor.

        The floor applies in ALL cadence states (adaptive, custom, HomeKit) so
        a fast zone cycle never drags slow data along. When HomeKit is
        connected the floor widens to the user's cloud-sync dial, same as the
        zone-state and offset-drift paths.
        """
        if last_fetch is None:
            return False  # never fetched → must fetch
        floor = base_floor_minutes
        if homekit_connected:
            floor = max(base_floor_minutes, self.config_manager.get_homekit_cloud_sync_minutes())
        elapsed = (dt_util.utcnow() - last_fetch).total_seconds() / 60
        return elapsed < floor

    async def _async_fetch_bridge_data(self) -> dict[str, object] | None:
        """Fetch bridge API data with health tracking."""
        options = self.config_entry.options
        bridge_serial = options.get("bridge_serial", "")
        bridge_auth_key = options.get("bridge_auth_key", "")

        if not bridge_serial or not bridge_auth_key:
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
            self.bridge_api_client = TadoBridgeApiClient(session, bridge_serial, bridge_auth_key)
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

            return result
        except TadoBridgeApiError as e:
            self.bridge_health_tracker.record_failure(str(e))
            self.data_loader.save_bridge_health(self.bridge_health_tracker.to_dict())
            _LOGGER.debug(
                "Bridge: fetch failed (%s), cloud data unaffected",
                e,
            )
            return None

    def _ensure_bridge_poll_running(self) -> None:
        """Start the independent bridge poll loop if credentials are present.

        Bridge API uses its own auth key and does NOT count toward the
        Tado cloud API quota, so it runs on a fixed interval independent
        of the main coordinator polling cycle.
        """
        if self._bridge_poll_task is not None and not self._bridge_poll_task.done():
            return  # already running
        options = self.config_entry.options
        if not options.get("bridge_serial", "") or not options.get("bridge_auth_key", ""):
            return  # no credentials
        self._bridge_poll_task = asyncio.create_task(self._async_bridge_poll_loop())

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
                _LOGGER.debug(
                    "Bridge: poll failed, will retry on the next cycle",
                )
            await asyncio.sleep(BRIDGE_POLL_INTERVAL_SECONDS)

    def _update_bridge_in_coordinator_data(self) -> None:
        """Write cached bridge data into coordinator.data and notify listeners."""
        if self.data is None or self._cached_bridge_data is None:
            return
        self.data["bridge"] = self._cached_bridge_data
        self.async_update_listeners()

    async def cancel_bridge_poll(self) -> None:
        """Cancel the independent bridge poll task."""
        if self._bridge_poll_task is not None:
            self._bridge_poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._bridge_poll_task
            self._bridge_poll_task = None

    async def _async_run_weather_compensation(
        self,
        bridge_data: dict[str, object] | None,
        zone_data: dict[str, Any] | None,
        weather_data: dict[str, Any] | None,
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
                bridge_data=bridge_data,
            )
        except (TadoAuthError, TadoRateLimitError, TadoSyncError):
            raise
        except Exception:
            _LOGGER.warning(
                "Weather Compensation: cycle failed, main coordinator "
                "update unaffected, will retry next cycle",
                exc_info=True,
            )
            self._wc_state.status = "error"
            return None

    def _load_ratelimit_from_cache(self) -> None:
        """Load ratelimit data from DataLoader in-memory cache."""
        data = self.data_loader.get_cached("ratelimit")
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




    def save_wc_state_if_loaded(self) -> None:
        """Persist weather compensation state if it was loaded."""
        if self._wc_state_loaded:
            self.data_loader.save_wc_state(self._wc_state.to_dict())

    def _save_homekit_savings(self) -> None:
        """Persist HomeKit API savings counters (debounced via Store)."""
        self.data_loader.save_homekit_savings({
            "reads_saved": self._homekit_reads_saved,
            "writes_saved": self._homekit_writes_saved,
            "prev_remaining": self._prev_savings_remaining,
        })

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

    def record_homekit_write_saved(self, zone_id: str | None = None) -> None:
        """Record one HomeKit write saving, update reconciler, and persist."""
        self._homekit_writes_saved += 1
        if zone_id and self.state_reconciler:
            self.state_reconciler.record_local_write(zone_id)
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

    def get_state_restore_diagnostics(self) -> list[dict[str, str]]:
        """Return state restore diagnostics summary (null-safe)."""
        if self._sr_manager is None:
            return []
        return self._sr_manager.get_diagnostics_summary()

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

        _LOGGER.debug(
            "Coordinator: restored insight runtime state: %d anomaly "
            "timer(s), %d humidity history(ies)",
            len(self._insight_anomaly_start_times),
            len(self._insight_humidity_histories),
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

    async def async_shutdown_state_restore(self) -> None:
        """Persist and shut down state restore manager (null-safe)."""
        if self._sr_manager is not None:
            await self._sr_manager.async_shutdown()

    async def _handle_zone_delta(self, delta: ZoneFingerprintDelta) -> None:
        """Invalidate caches and prune zombie zone-keyed entries on topology change.

        Best-effort: a single store's prune failure is logged and never
        blocks the remaining stores.
        """
        from .data_loader import _CACHE_DIRTY
        from .helpers import prune_zone_keyed_dict

        if delta.added or delta.removed:
            self.data_loader._cache["zones_info"] = _CACHE_DIRTY
            self._request_full_sync_next_cycle = True
            self.reset_mapping_retry_budget()

        if not delta.removed:
            return

        current_zones = self._zone_fingerprint._previous or frozenset()

        for store_name in (
            "schedules", "zone_config", "smart_comfort_cache",
            "ac_capabilities", "ac_capabilities_fp", "offsets",
            "heating_circuit_control",
        ):
            try:
                data = await self.data_loader.async_load_auxiliary(store_name)
                if not isinstance(data, dict):
                    continue
                removed = prune_zone_keyed_dict(data, current_zones)
                if removed:
                    await self.data_loader.async_update_store(store_name, data)
                    _LOGGER.info(
                        "Persistence: pruned %d stale entries from %s",
                        removed, store_name,
                    )
            except Exception:
                _LOGGER.warning(
                    "Persistence: prune of %s failed; continuing",
                    store_name, exc_info=True,
                )

        # In-memory insight runtime state (two zone-keyed dicts, not a
        # DataLoader store), prune the removed zone and persist.
        try:
            insight_removed = 0
            for d in (self._insight_anomaly_start_times, self._insight_humidity_histories):
                for zid in [k for k in d if k not in current_zones]:
                    d.pop(zid, None)
                    insight_removed += 1
            if insight_removed:
                self._save_insight_runtime_state()
                _LOGGER.info(
                    "Persistence: pruned %d stale insight entrie(s)",
                    insight_removed,
                )
        except Exception:
            _LOGGER.warning(
                "Persistence: prune of insight runtime state failed; continuing",
                exc_info=True,
            )

        if self._sr_manager is not None:
            try:
                removed = self._sr_manager.prune_stale_captures(current_zones)
                if removed:
                    _LOGGER.info(
                        "State Restore: pruned %d stale capture(s)", removed,
                    )
            except Exception:
                _LOGGER.warning(
                    "Persistence: prune of state_restore failed",
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Smart Valve Control lifecycle
    # ------------------------------------------------------------------

    async def async_init_valve_controllers(self) -> None:
        """Spin up the per-zone valve and offset-sync controllers from svc_mode."""
        from .offset_sync_controller import OffsetSyncController
        from .valve_controller import SmartValveController

        zones_info = self.data_loader.get_cached("zones_info")
        if not zones_info or not isinstance(zones_info, list):
            _LOGGER.warning(
                "Smart Valve: Tado zone data not loaded yet, controllers "
                "will be set up on the next successful poll",
            )
            return

        _LOGGER.debug(
            "Smart Valve: checking %d zone(s) for eligible controllers",
            len(zones_info),
        )
        initialized_count = 0

        for zone in zones_info:
            zone_id = str(zone.get("id", ""))
            zone_type = zone.get("type", "")

            if zone_type != "HEATING":
                continue

            config = self.zone_config_manager.get_zone_config(zone_id)
            svc_mode = config.get("svc_mode", "off")
            ext_sensor = config.get("external_temp_sensor", "")

            if svc_mode == "off":
                continue

            if not ext_sensor:
                _LOGGER.info(
                    "Smart Valve: zone %s is set to %s mode but has no "
                    "external sensor selected, pick a sensor in Zone "
                    "Configuration to enable Smart Valve Control",
                    zone_id, svc_mode,
                )
                continue

            if svc_mode == "valve_target":
                controller = SmartValveController(self.hass, self, zone_id)
                await controller.async_activate()
                self.valve_controllers[zone_id] = controller
                initialized_count += 1
                _LOGGER.debug(
                    "Smart Valve: zone %s valve_target controller activated",
                    zone_id,
                )
            elif svc_mode == "offset_sync":
                controller_os = OffsetSyncController(self.hass, self, zone_id)
                await controller_os.async_activate()
                self.offset_sync_controllers[zone_id] = controller_os
                initialized_count += 1
                _LOGGER.debug(
                    "Offset Sync: zone %s controller activated",
                    zone_id,
                )

        _LOGGER.info(
            "Smart Valve: %d controller(s) activated", initialized_count,
        )

        self.zone_config_manager.add_listener(self._on_zone_config_change)

    async def async_shutdown_valve_controllers(self) -> None:
        """Deactivate and clean up all valve and offset sync controllers.

        Sets `_shutting_down` so any queued/in-flight `_transition` tasks
        bail out before touching controller dicts, preventing zombie
        controllers that would outlive the shutdown sequence.
        """
        self._shutting_down = True

        for zone_id, controller in list(self.valve_controllers.items()):
            try:
                await controller.async_deactivate()
            except Exception:
                _LOGGER.warning(
                    "Smart Valve: error deactivating controller for zone %s",
                    zone_id, exc_info=True,
                )
        self.valve_controllers.clear()

        for zone_id, controller_os in list(self.offset_sync_controllers.items()):
            try:
                await controller_os.async_deactivate()
            except Exception:
                _LOGGER.warning(
                    "Offset Sync: error deactivating controller for zone %s",
                    zone_id, exc_info=True,
                )
        self.offset_sync_controllers.clear()

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
        if zones_info and isinstance(zones_info, list):
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

                if old_valve:
                    _LOGGER.debug(
                        "Smart Valve: deactivating zone %s valve_target "
                        "controller before config change",
                        zone_id,
                    )
                    try:
                        await old_valve.async_deactivate()
                    except Exception:
                        _LOGGER.warning(
                            "Smart Valve: zone %s deactivation raised an "
                            "exception, controller may not have shut "
                            "down cleanly",
                            zone_id, exc_info=True,
                        )

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
                if current_mode == SVC_MODE_VALVE_TARGET:
                    if not ext_sensor:
                        _LOGGER.info(
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
                        self.valve_controllers[zone_id] = new_controller

                elif current_mode == SVC_MODE_OFFSET_SYNC:
                    if not ext_sensor:
                        _LOGGER.info(
                            "Offset Sync: zone %s set to offset_sync mode "
                            "but no external sensor, skipping activation",
                            zone_id,
                        )
                        self._raise_sensor_missing_repair(zone_id, current_mode)
                    else:
                        from .offset_sync_controller import OffsetSyncController

                        self._clear_sensor_missing_repair(zone_id)
                        new_controller = OffsetSyncController(self.hass, self, zone_id)
                        self.offset_sync_controllers[zone_id] = new_controller

                else:
                    # svc_mode is "off" or unknown: no new controller,
                    # but clear any stale repair issue for this zone.
                    self._clear_sensor_missing_repair(zone_id)

                if new_controller:
                    await new_controller.async_activate()
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
            from homeassistant.helpers import issue_registry as ir

            zones_info_raw = self.data_loader.get_cached("zones_info")
            zones_info = zones_info_raw if isinstance(zones_info_raw, list) else []
            zone_name = next(
                (z.get("name", f"Zone {zone_id}") for z in zones_info
                 if str(z.get("id")) == zone_id),
                f"Zone {zone_id}",
            )

            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"svc_sensor_missing_{zone_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="svc_sensor_missing",
                translation_placeholders={
                    "zone_name": zone_name,
                    "mode": mode,
                },
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
            from homeassistant.helpers import issue_registry as ir

            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                f"svc_sensor_missing_{zone_id}",
            )
        except (KeyError, ValueError, AttributeError):
            _LOGGER.debug(
                "Coordinator: could not clear sensor-missing repair for "
                "zone %s. HA issue registry call failed",
                zone_id, exc_info=True,
            )

