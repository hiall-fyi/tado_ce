"""Registry of everything that holds per-zone state, dropped together when a zone goes away."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
import inspect
import logging
from typing import Final, Literal

_LOGGER = logging.getLogger(__name__)

ZoneDropReason = Literal["removed", "reassigned"]
ALL_REASONS: Final[frozenset[ZoneDropReason]] = frozenset({"removed", "reassigned"})
# Owners that must keep their data when a zone is merely removed.
REASSIGNED_ONLY: Final[frozenset[ZoneDropReason]] = frozenset({"reassigned"})
# Owners that must keep their data when a zone id is merely re-used.
REMOVED_ONLY: Final[frozenset[ZoneDropReason]] = frozenset({"removed"})
# Zone-keyed stores that are flat, one entry per zone; wrapped stores must never join this tuple.
ZONE_KEYED_FLAT_STORES: Final[tuple[str, ...]] = (
    "schedules", "zone_capabilities", "zone_capabilities_fp", "offsets", "heating_circuit_control",
)
WRAPPED_ZONE_STORES: Final[tuple[str, ...]] = ("zone_config", "smart_comfort_cache")
# "Class.attr" or "module.attr" -> the owner registered in TadoDataUpdateCoordinator._register_zone_state_owners that drops it.
ZONE_STATE_OWNED: Final[dict[str, str]] = {
    "ZoneConfigManager._config": "zone_config",
    "TadoDataUpdateCoordinator.valve_controllers": "controllers",
    "TadoDataUpdateCoordinator.retired_valve_controllers": "controllers",
    "TadoDataUpdateCoordinator.offset_sync_controllers": "controllers",
    "TadoDataUpdateCoordinator.write_health_trackers": "write_health",
    "TadoDataUpdateCoordinator.homekit_off_latch": "homekit_off_latch",
    "ZoneWriteLedger._records": "write_ledger",
    "ZoneWriteLedger._stale": "write_ledger",
    "ZoneWriteLedger._held": "write_ledger",
    "ZoneWriteLedger._tasks": "write_ledger",
    # The owed entries also live in the "write_ledger" store of this table; forget_zone saves it again without the zone's row.
    "data_loader._ALL_STORES": "write_ledger",
    "TadoDataUpdateCoordinator._valve_init_failing": "controllers",
    "TadoDataUpdateCoordinator._controller_eval_failing": "controllers",
    "AdaptivePreheatManager._zone_info": "adaptive_preheat",
    "AdaptivePreheatManager._enabled_zones": "adaptive_preheat",
    "AdaptivePreheatManager._zone_unsubs": "adaptive_preheat",
    "AdaptivePreheatManager._start_handles": "adaptive_preheat",
    "AdaptivePreheatManager._retry_overlay": "adaptive_preheat",
    "AdaptivePreheatManager._active_overlays": "adaptive_preheat",
    "AdaptivePreheatManager._app_off_logged": "adaptive_preheat",
    "HeatingCycleCoordinator._detectors": "heating_cycles",
    "HeatingCycleCoordinator._zone_data": "heating_cycles",
    "HeatingCycleCoordinator._zone_states": "heating_cycles",
    "HeatingCycleStorage._data": "heating_cycles",
    "SmartComfortManager._zones": "smart_comfort",
    "TadoDataUpdateCoordinator._insight_anomaly_start_times": "insight_runtime",
    "TadoDataUpdateCoordinator._insight_humidity_histories": "insight_runtime",
    # The flat stores are named in a tuple, not held in one attribute, so the key is that tuple.
    "zone_state_registry.ZONE_KEYED_FLAT_STORES": "flat_stores",
    "HomeKitClient._zone_to_aids": "homekit_mapping",
    "HomeKitClient._serial_to_zone": "homekit_mapping",
    "HomeKitClient._cloud_serials": "homekit_mapping",
    "HomeKitLocalProvider._cache": "homekit_mapping",
    "HomeKitLocalProvider._event_map": "homekit_mapping",
    "HomeKitLocalProvider._ambiguous_zones": "homekit_mapping",
    "StateReconciler._write_timestamps": "state_reconciler",
    "StateReconciler._prev_sources": "state_reconciler",
    "StateReconciler._write_counts": "state_reconciler",
    "StateRestoreManager._captured": "state_restore",
    "StateRestoreManager._previous_overlay_states": "state_restore",
    "StateRestoreManager._consecutive_cleared": "state_restore",
    "TadoDataUpdateCoordinator.entity_data": "entity_data",
    # The clamp flag lives in the data_loader cache and in the published poll data, so the key is the coordinator's `data`.
    "TadoDataUpdateCoordinator.data": "offset_clamps",
    # The repair owners hold no attribute: the key is the issue spec each one dismisses.
    "repair_helpers.SVC_SENSOR_MISSING_ISSUE": "sensor_missing_repair",
    "repair_helpers.TRV_BATTERY_LOW_ISSUE": "battery_low_repair",
    "repair_helpers.ZONE_REASSIGNED_ISSUE": "zone_reassigned_repair",
}
# "Class.attr" or "module.attr" -> one line on why it needs no owner.
ZONE_STATE_EXEMPT: Final[dict[str, str]] = {
    "TadoDataUpdateCoordinator._zone_drop_pending": "drop bookkeeping: an entry is removed when its drop succeeds or the zone returns, so it holds no zone state",
    "TadoDataUpdateCoordinator._zone_drop_dates": "drop bookkeeping: removed when the reassigned zone's drop succeeds or the zone is no longer flagged",
    "TadoDataUpdateCoordinator._zone_drops_done": "drop bookkeeping: an id leaves when the zone is listed again",
    "TadoDataUpdateCoordinator._zone_absent_polls": "drop bookkeeping: a counter removed when the zone returns or its drop is confirmed",
    "TadoDataUpdateCoordinator._zones_awaiting_listing": "bookkeeping of new ids, intersected with the zones still in the state list on every poll",
    "TadoDataUpdateCoordinator.entity_zone_ids": "one frozenset of ids replaced at setup, not per-zone state",
    "TadoDataUpdateCoordinator.entity_zone_identity": "one dict replaced at setup, a record of what the platforms built, not per-zone state",
    "TadoDataUpdateCoordinator._zone_fingerprint": "the source of the added/removed delta that triggers every drop",
    "TadoDataUpdateCoordinator._last_known_zone_types": "the baseline id-reuse detection compares against, persisted",
    "TadoDataUpdateCoordinator._last_known_zone_dates": "the baseline id-reuse detection compares against, persisted",
    "TadoDataUpdateCoordinator._zone_transition_locks": "one Lock per zone ever seen, holds no state; popping a held lock would split mutual exclusion",
    "TadoDataUpdateCoordinator._cloud_only_logged": "one value-compared log dedupe replaced on every poll",
    "TadoDataUpdateCoordinator.entity_freshness": "keyed by entity id and expiry-cleaned",
    "coordinator._ZONE_RELOADS_REQUESTED": "once-per-process marker that stops a reload loop, deliberately never cleared",
    "helpers._merge_logged": "DEBUG log dedupe only, holds no zone state",
    "zone_signals._class_gate_logged": "DEBUG log dedupe only, holds no zone state",
    "zone_signals._zone_power_logged": "DEBUG log dedupe only, holds no zone state",
    "AdaptivePreheatManager._starting": "set cleared in the finally of _trigger_preheat; discarding it mid-flight would allow a second start write",
    "AdaptivePreheatManager._start_tasks": "running start tasks, not keyed by zone; each discards itself when done and unload drains or cancels the rest",
    "SmartComfortManager._forget_save_pending": "one boolean, not keyed by zone",
    "ZoneStateRegistry._warned": "failure-log dedupe, discarded when the owner next succeeds for the zone",
    "ActionDebouncer._pending": "timers of 0 to 10 s (default 3) that pop themselves; controller keys are cancelled by the controller's discard, climate keys by user paths or expire on their own",
    "ActionDebouncer._pending_coros": "callbacks of 0 to 10 s (default 3) that pop themselves; controller keys are cancelled by the controller's discard, climate keys by user paths or expire on their own",
    "InsightHistoryTracker._entries": "keyed by insight type and zone name, not id, and aged out by its own retention",
    "TadoHomeInsightsSensor._escalated_priority_map": "keyed by insight type and zone name, rebuilt wholesale on every update",
    "TadoHomeInsightsSensor._aggregated": "rebuilt wholesale on every update and never indexed by a zone id",
}
# True: this owner dropped something.
ZoneDrop = Callable[[str, ZoneDropReason], Awaitable[bool | None] | bool | None]


class ZoneDropIncomplete(Exception):
    """Raised after every owner ran when at least one owner failed."""

    def __init__(self, zone_id: str, failed: tuple[str, ...]) -> None:
        super().__init__(f"zone {zone_id}: owners failed to drop state: {', '.join(failed)}")
        self.zone_id = zone_id
        self.failed = failed


class ZoneStateRegistry:
    """Ordered owners of per-zone state with per-owner failure isolation."""

    def __init__(self) -> None:
        self._owners: dict[str, tuple[ZoneDrop, frozenset[ZoneDropReason]]] = {}
        self._warned: set[tuple[str, str]] = set()

    def register(self, name: str, drop: ZoneDrop, *, reasons: frozenset[ZoneDropReason] = ALL_REASONS) -> None:
        """Add an owner; a duplicate name or an empty reason set is a programming error."""
        if name in self._owners:
            raise ValueError(f"zone state owner already registered: {name}")
        if not reasons:
            raise ValueError(f"zone state owner {name} needs at least one reason")
        self._owners[name] = (drop, reasons)

    @property
    def owner_names(self) -> tuple[str, ...]:
        """Registered owner names in registration order."""
        return tuple(self._owners)

    async def drop_zone(
        self, zone_id: str, reason: ZoneDropReason, *, only: frozenset[str] | None = None,
    ) -> frozenset[str]:
        """Run the owners for `reason` (or just `only`); return who dropped something, or raise ZoneDropIncomplete."""
        if reason not in ALL_REASONS:
            raise ValueError(f"unknown zone drop reason: {reason}")
        if only is not None and (unknown := only - self._owners.keys()):
            raise ValueError(f"unknown zone state owners: {', '.join(sorted(unknown))}")

        dropped: set[str] = set()
        failed: list[str] = []
        for name, (drop, reasons) in self._owners.items():
            if reason not in reasons or (only is not None and name not in only):
                continue
            try:
                result = drop(zone_id, reason)
                if inspect.isawaitable(result):
                    result = await result
            except Exception:
                # Owners are injected by other subsystems: one failing must not stop the rest.
                failed.append(name)
                self._log_failure(name, zone_id)
                continue
            self._warned.discard((name, zone_id))
            if result:
                dropped.add(name)
        if failed:
            raise ZoneDropIncomplete(zone_id, tuple(failed))
        return frozenset(dropped)

    def _log_failure(self, name: str, zone_id: str) -> None:
        key = (name, zone_id)
        if key in self._warned:
            _LOGGER.debug("Zone %s: state owner %s still failing", zone_id, name, exc_info=True)
            return
        self._warned.add(key)
        _LOGGER.warning("Zone %s: state owner %s failed to drop, will retry", zone_id, name, exc_info=True)
