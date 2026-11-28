"""What the heating and AC climate entities share: constructor fields and the listener lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, ClassVar

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import HVACAction, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import async_get_platforms
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .climate_helpers import (
    _revert_if_still_held,
    capture_climate_rollback_state,
    observed_climate_state,
    read_external_sensor,
    setup_climate_external_sensor_subscription,
)
from .const import DOMAIN, SIGNAL_HOMEKIT_UPDATE
from .device_manager import get_zone_device_info
from .entity_registry import ENTITY_REGISTRY
from .exceptions import TadoBlockedError, TadoRateLimitError
from .helpers import PerEntityAvailabilityMixin, get_homekit_cloud_lag_seconds
from .homekit_provider import PutOutcome
from .optimistic_helpers import (
    accept_write,
    accepted_values,
    clear_homekit_write,
    clear_optimistic_state,
    find_intent,
    note_ambiguous_put,
    observe_accepted,
    put_time,
    resend_entry,
    seed_accepted,
)
from .zone_entity import ZoneEntityMixin
from .zone_write_ledger import ordered_zone_write

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from homeassistant.core import HomeAssistant

    from .coordinator import TadoDataUpdateCoordinator
    from .zone_write_ledger import ResendEntry, ZoneWriteLedger

_LOGGER = logging.getLogger(__name__)


def _find_zone_entity(hass: HomeAssistant, coordinator: TadoDataUpdateCoordinator, zone_id: str) -> ZoneEntityMixin | None:
    """The loaded heating, AC or hot water entity of the zone, else None."""
    for platform in async_get_platforms(hass, DOMAIN):
        if platform.domain not in ("climate", "water_heater"):
            continue
        for entity in platform.entities.values():
            if isinstance(entity, ZoneEntityMixin) and entity.coordinator is coordinator and entity.zone_id == zone_id:
                return entity
    return None


def cancel_zone_tap(hass: HomeAssistant, coordinator: TadoDataUpdateCoordinator, zone_id: str) -> None:
    """Drop the zone's debounced tap for a service-plane write and put the loaded climate card back to what Tado holds."""
    # Every service-plane write that replaces the overlay calls this at the attempt, so it also ends a change held over HomeKit.
    coordinator.write_ledger.supersede(zone_id)
    entity = _find_zone_entity(hass, coordinator, zone_id)
    if not isinstance(entity, TadoClimateBase):
        # No climate entity for the zone (none loaded, or hot water, which sends at once and queues no tap): only the queued write needs dropping.
        coordinator.action_debouncer.cancel(zone_id)
        return
    # Read before the cancel: the stamp is the hold the queued tap set, the only one the restore may release.
    tap_stamp = entity._queued_tap_stamp()
    entity._restore_after_refusal(entity._cancel_pending_write(), tap_stamp)


def release_zone_hold(hass: HomeAssistant, coordinator: TadoDataUpdateCoordinator, zone_id: str) -> None:
    """End the loaded zone card's hold (heating, AC or hot water) after a service write that Tado accepted, so the card follows the held data again."""
    entity = _find_zone_entity(hass, coordinator, zone_id)
    # A half-removed entity (no hass or no platform) has nothing to write its state to.
    if entity is None or entity.hass is None or entity.platform is None:
        return
    # A write still queued, waiting for the zone's slot or sending is newer than this one and owns the card and its hold.
    ledger = getattr(coordinator, "write_ledger", None)
    if ledger is not None and ledger.busy(zone_id) is True:
        return
    clear_optimistic_state(entity)
    clear_homekit_write(entity)
    # The freshness mark would make update() skip, and the card must show the held data now, not at the next update past the window.
    coordinator.entity_freshness.pop(entity.entity_id, None)
    entity.update()
    entity.async_write_ha_state()


class TadoClimateBase(PerEntityAvailabilityMixin, ZoneEntityMixin, CoordinatorEntity["TadoDataUpdateCoordinator"], ClimateEntity, RestoreEntity):
    """Constructor fields, listener lifecycle and verification scheduling for a heating or AC zone entity."""

    _attr_has_entity_name = True
    _DEFAULT_TARGET: ClassVar[float]
    # Prefix of the label the external-sensor subscription logs under.
    _SENSOR_LABEL_PREFIX: ClassVar[str] = ""

    def __init__(
        self,
        coordinator: TadoDataUpdateCoordinator,
        zone_id: str,
        zone_name: str,
        home_id: str,
        *,
        registry_key: str,
        device_zone_type: str,
    ) -> None:
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._home_id = home_id
        self._entry_id = coordinator.config_entry.entry_id
        self._entity_type = registry_key

        _meta = ENTITY_REGISTRY[registry_key]
        self._attr_name = None
        self._attr_translation_key = _meta.translation_key
        # Use zone_id for unique_id to maintain entity_id stability across zone name changes
        self._attr_unique_id = f"tado_ce_{home_id}_{_meta.unique_id_suffix.format(zone_id=zone_id)}"
        self._attr_native_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_device_info = get_zone_device_info(zone_id, zone_name, device_zone_type, home_id)

        self._data_present = False
        self._overlay_type: str | None = None

        # External sensor override tracking
        self._temperature_source = "cloud"
        self._humidity_source = "cloud"
        self._external_temp_sensor = ""
        self._external_humidity_sensor = ""
        self._last_write_source = ""

        # Optimistic state tracking with sequence numbers
        self._optimistic_set_at: float | None = None
        self._optimistic_sequence: int | None = None
        self._expected_hvac_mode: HVACMode | None = None
        self._expected_hvac_action: HVACAction | None = None
        self._expected_target_temperature: float | None = None
        self._expected_overlay_present: bool | None = None

        self._last_write_sequence: int | None = None

    def _write_ledger(self) -> ZoneWriteLedger | None:
        """The coordinator's write ledger, or None for a coordinator that has none."""
        return getattr(self.coordinator, "write_ledger", None)

    def _spawn_retry_task(self, coro: Coroutine[Any, Any, None]) -> asyncio.Future[Any]:
        """Start `coro` as an expired-write retry the ledger cancels when this entity is removed."""
        ledger: ZoneWriteLedger = self.coordinator.write_ledger
        return ledger.spawn_task(self._zone_id, coro, hass=self.hass)

    def _seed_accepted(self, include: tuple[str, ...] | None = None) -> None:
        """Start what Tado is known to hold from the card as it is now, for the names no poll or write has set yet."""
        seed_accepted(self, capture_climate_rollback_state(self, include))

    def _accept_homekit_put(self, sequence: int | None, names: tuple[str, ...] | None = None) -> None:
        """The bridge took the put, so what the card shows for `names` is what Tado holds, unless a newer write has taken the card since."""
        if self._optimistic_sequence != sequence:
            return
        accept_write(self, find_intent(self, sequence), capture_climate_rollback_state(self, names))

    def _observe_accepted(self, **about_to_show: Any) -> None:
        """Record the values the card takes from Tado as what Tado holds, with `about_to_show` for those it sets next."""
        observe_accepted(self, {**observed_climate_state(self), **about_to_show})

    def _announce_queued_write(self, names: tuple[str, ...], stamp: int | None) -> None:
        """Tell the ledger a debounced write is about to be queued, with what Tado holds for `names` and the tap's own hold."""
        ledger = self._write_ledger()
        if ledger is not None:
            ledger.queue(self._zone_id, accepted_values(self, names), stamp)

    async def _run_queued_write(self, send: Callable[[], Awaitable[None]]) -> None:
        """Run a debounced write, keeping its baseline until the PUT has ended."""
        ledger = self._write_ledger()
        if ledger is None:
            await send()
            return
        ledger.begin_send(self._zone_id)
        try:
            await send()
        finally:
            ledger.end_send(self._zone_id)

    def _queued_tap_stamp(self) -> int | None:
        """The hold of the tap still waiting in the debouncer, else None (a PUT already running cannot be dropped)."""
        ledger = self._write_ledger()
        return None if ledger is None else ledger.queued_tap_stamp(self._zone_id)

    def _cancel_pending_write(self) -> dict[str, Any] | None:
        """Cancel the zone's queued debounced write and return the rollback it held."""
        ledger = self._write_ledger()
        if ledger is None:
            self.coordinator.action_debouncer.cancel(self._zone_id)
            return None
        return ledger.cancel_queued(self._zone_id)

    def _restore_after_refusal(self, cancelled: dict[str, Any] | None, tap_stamp: int | None) -> None:
        """Put the card back to what Tado holds after a refused action dropped the queued tap, but only while the card's hold is still that tap's own."""
        # `tap_stamp` is read before the cancel: None means the tap was already sending or stamped nothing, so the card is not ours to restore.
        if cancelled is None or tap_stamp is None:
            return
        # The revert acts only while the live hold is still the tap's own stamp.
        _revert_if_still_held(self, cancelled, tap_stamp, _LOGGER)

    def _update_temp_limits(self) -> None:
        """Resolve min/max temp for this zone."""
        raise NotImplementedError

    def _handle_homekit_update(self, zone_id: str) -> None:
        """Handle HomeKit data update for this zone."""
        raise NotImplementedError

    async def async_added_to_hass(self) -> None:
        """Restore the last target, then register the zone-config, external-sensor and HomeKit listeners."""
        await super().async_added_to_hass()

        await self._async_restore_target(self._DEFAULT_TARGET)

        # Listen for zone config changes
        zone_config_manager = self.coordinator.zone_config_manager
        if zone_config_manager:

            @callback
            def _handle_zone_config_change(zone_id: str, key: str, value: Any) -> None:
                """Handle zone config change."""
                if zone_id == self._zone_id and key in ("min_temp", "max_temp", "display_temp_source"):
                    if key == "display_temp_source":
                        self._handle_coordinator_update()
                    else:
                        self._update_temp_limits()
                        self.async_write_ha_state()
                    _LOGGER.debug(
                        "%s: %s zone config %s changed to %s",
                        self._LOG_LABEL, self._zone_name, key, value,
                    )

            self.async_on_remove(zone_config_manager.add_listener(_handle_zone_config_change))
            # Initial update of temp limits
            self._update_temp_limits()

        # Subscribe to external sensor state changes for real-time updates
        self._subscribe_external_sensors()

        # Subscribe to HomeKit dispatcher signal for real-time sensor updates
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_HOMEKIT_UPDATE.format(home_id=self._home_id),
                self._handle_homekit_update,
            ),
        )
    async def async_will_remove_from_hass(self) -> None:
        """Drop the held resend and cancel the resend tasks."""
        self.coordinator.write_ledger.cancel_resends(self._zone_id)
        await super().async_will_remove_from_hass()

    @callback
    def _subscribe_external_sensors(self) -> None:
        """Subscribe to external sensor state changes for real-time updates."""
        for unsub in setup_climate_external_sensor_subscription(
            self, self._zone_id, label=f"{self._SENSOR_LABEL_PREFIX}{self._zone_name}",
        ):
            self.async_on_remove(unsub)

    def _update_sensor_data(self, zone_data: dict[str, Any]) -> tuple[float | None, str, float | None, str]:
        """Set current temperature and humidity with external > HomeKit > cloud priority; return both values and sources."""
        sensor_data = zone_data.get("sensorDataPoints") or {}
        cloud_temp = (sensor_data.get("insideTemperature") or {}).get("celsius")
        cloud_humidity = (sensor_data.get("humidity") or {}).get("percentage")

        zcm = self.coordinator.zone_config_manager
        ext_temp = read_external_sensor(self.hass, zcm, self._zone_id, "external_temp_sensor")
        ext_hum = read_external_sensor(self.hass, zcm, self._zone_id, "external_humidity_sensor")

        # Priority chain via StateReconciler (external > homekit if fresh > cloud)
        reconciler = self.coordinator.state_reconciler
        provider = self.coordinator.homekit_provider
        if reconciler and provider and provider.is_connected:
            reconciler.local_provider = provider
            display_source = (
                zcm.get_zone_value(self._zone_id, "display_temp_source", "auto")
                if zcm else "auto"
            )
            merged_temp, temp_source = reconciler.merge_zone_temperature(
                self._zone_id, cloud_temp, external_value=ext_temp,
                display_source=display_source, purpose="display",
            )
            merged_hum, hum_source = reconciler.merge_zone_humidity(
                self._zone_id, cloud_humidity, external_value=ext_hum,
            )
        elif ext_temp is not None:
            merged_temp, temp_source = ext_temp, "external"
            merged_hum, hum_source = (ext_hum, "external") if ext_hum is not None else (cloud_humidity, "cloud")
        elif ext_hum is not None:
            merged_temp, temp_source = cloud_temp, "cloud"
            merged_hum, hum_source = ext_hum, "external"
        else:
            merged_temp, temp_source = cloud_temp, "cloud"
            merged_hum, hum_source = cloud_humidity, "cloud"

        self._attr_native_current_temperature = merged_temp
        self._temperature_source = temp_source
        self._attr_current_humidity = merged_hum
        self._humidity_source = hum_source

        # Track configured entity_ids for extra_state_attributes
        if zcm:
            zc = zcm.get_zone_config(self._zone_id)
            self._external_temp_sensor = zc.get("external_temp_sensor", "")
            self._external_humidity_sensor = zc.get("external_humidity_sensor", "")
        return merged_temp, temp_source, merged_hum, hum_source

    async def _send_homekit_put(self, send: Callable[[], Awaitable[PutOutcome]], *, raised_msg: str) -> PutOutcome:
        """Count, time and send one HomeKit write inside the zone's slot; a value that is not a PutOutcome comes back UNAVAILABLE."""
        self.coordinator._homekit_write_attempts += 1
        t0 = time.monotonic()
        outcome: object = PutOutcome.UNAVAILABLE
        try:
            # Held for the put only: a cloud fallback takes the slot again afterwards.
            async with ordered_zone_write(self.coordinator, self._zone_id):
                # Timed from the send, so a wait for the slot does not count as bridge latency.
                t0 = time.monotonic()
                outcome = await send()
        except Exception:
            # A HomeKit provider can raise anything; a raise is a put the bridge did not take.
            _LOGGER.debug(raised_msg, self._zone_name, exc_info=True)
        elapsed_ms = (time.monotonic() - t0) * 1000
        self.coordinator._homekit_write_latency_sum += elapsed_ms
        self.coordinator._homekit_write_latency_count += 1
        # A value that is not a PutOutcome (an old bool) is never an acceptance.
        return outcome if isinstance(outcome, PutOutcome) else PutOutcome.UNAVAILABLE

    async def _try_homekit_write(
        self, send: Callable[[], Awaitable[PutOutcome]], *, raised_msg: str, failed_msg: str | None,
    ) -> bool:
        """Count, time and send one HomeKit write; False (any outcome but ACCEPTED) means the caller falls back to the cloud."""
        write_tracker = self.coordinator.write_health_for(self._zone_id)
        assert write_tracker is not None  # the caller's route decision was HomeKit  # noqa: S101
        outcome = await self._send_homekit_put(send, raised_msg=raised_msg)
        local_success = outcome is PutOutcome.ACCEPTED
        if outcome is PutOutcome.AMBIGUOUS:
            note_ambiguous_put(self)
        if local_success:
            # The tracker's success waits for the cloud poll to confirm; a local ack alone is not proof.
            self.coordinator._homekit_write_successes += 1
        else:
            write_tracker.record_failure()
            self.coordinator._homekit_write_fallbacks += 1
            if failed_msg is not None:
                _LOGGER.debug(failed_msg, self._zone_name)
        return local_success

    async def _run_expired_resend(
        self,
        label: str,
        again: Callable[[bool, ResendEntry], Coroutine[Any, Any, None]],
        *,
        quota_rearmed: bool,
        send: Callable[[], Awaitable[None]],
        value: tuple[str, float | None],
        entry: ResendEntry | None,
    ) -> None:
        """Run one EXPIRED cloud resend once the cloud gate and the ownership check allow it; a refused write keeps Tado's reported state, and a rate limit or block holds it again."""
        entry = entry or resend_entry(self, power=value[0], celsius=value[1])
        if entry is None:
            _LOGGER.info("%s: %s EXPIRED-retry cloud resend dropped, the write ledger could not record it", label, self._zone_name)
            return
        ledger = self.coordinator.write_ledger
        if not await ledger.gate_resend(self._zone_id, label, again, entry=entry, quota_rearmed=quota_rearmed):
            return
        # `send` builds termination, power and temperature itself, so nothing is built while the resend is held
        try:
            await send()
        except HomeAssistantError as err:
            if isinstance(err.__cause__, (TadoRateLimitError, TadoBlockedError)):
                ledger.hold_refused_resend(self._zone_id, label, again, entry=entry, quota_rearmed=quota_rearmed)
                return
            _LOGGER.debug("%s: %s EXPIRED-retry cloud write failed, staying on Tado's reported state", label, self._zone_name)

    def _schedule_cloud_verification(self) -> None:
        """Ask the refresh handler for one verification fetch after the cloud lag."""
        handler = self.coordinator.refresh_handler
        if handler is None:
            _LOGGER.debug("%s: %s refresh handler is gone, verification not scheduled", self._entity_type, self.entity_id)
            return
        ledger = self.coordinator.write_ledger
        if not ledger.verification_due(ledger.live(self._zone_id)):
            _LOGGER.debug("%s: %s verification fetch skipped for this kind, the regular poll verifies", self._entity_type, self.entity_id)
            return
        handler.schedule_homekit_verification(
            put_time(self) or dt_util.utcnow(),
            get_homekit_cloud_lag_seconds(self.hass, entry_id=self._entry_id),
            self.entity_id,
        )
