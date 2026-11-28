"""Zone power and card target resolution: the one place that decides which source a zone's power and target come from."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any

from . import helpers as _helpers
from .const import HOMEKIT_OFF_RELEASE_LAG_SECONDS, HOMEKIT_POWER_DEVICE_TYPES, homekit_change_is_newer_than_fetch

if TYPE_CHECKING:
    from datetime import datetime

    from .coordinator import TadoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

_class_gate_logged: dict[tuple[str, str], tuple[str, ...]] = {}


def fresher_than(published: datetime | None, recorded: datetime | None) -> bool:
    """Whether a zone fetch is newer than a recorded fetch epoch; strict, no recorded epoch means any real fetch counts."""
    return published is not None and (recorded is None or published > recorded)


def fetch_covers_write(fetched: datetime | None, written_at: datetime | None, lag_seconds: float) -> bool:
    """Whether a zone fetch started more than the cloud lag after a write; never written counts as covered, no fetch does not."""
    return written_at is None or (fetched is not None and (fetched - written_at).total_seconds() > lag_seconds)


def zone_qualifies_for_homekit_power(
    coordinator: TadoDataUpdateCoordinator, zone_id: str, *, zones_info: list[dict[str, Any]] | None = None,
) -> bool:
    """Whether the zone is a heating zone whose every device is an allowed valve or wall thermostat; logs the device types once per zone when not."""
    zones_info = (zones_info if zones_info is not None else (coordinator.data or {}).get("zones_info")) or []
    zone = next((z for z in zones_info if str(z.get("id")) == str(zone_id)), None)
    devices = (zone or {}).get("devices") or []
    types = tuple(str(d.get("deviceType")) for d in devices)
    key = (coordinator.home_id, str(zone_id))
    if zone is not None and zone.get("type") == "HEATING" and devices and all(
        d.get("deviceType") in HOMEKIT_POWER_DEVICE_TYPES for d in devices
    ):
        _class_gate_logged.pop(key, None)
        return True
    if _class_gate_logged.get(key) != types:
        _class_gate_logged[key] = types
        _LOGGER.debug(
            "Helpers: zone %s keeps the cloud power, it is not a heating zone of valves and wall thermostats only (device types: %s)",
            zone_id, ", ".join(types) or "none",
        )
    return False


def owes_held_on(coordinator: TadoDataUpdateCoordinator, zone_id: str) -> bool:
    """Whether a write of ours switched the zone on over HomeKit while the cloud was down, so a cloud OFF is stale data."""
    ledger = getattr(coordinator, "write_ledger", None)
    return ledger is not None and ledger.owes_power_on(zone_id) is True


_zone_power_logged: dict[tuple[str, str], tuple[str | None, str, str | None]] = {}


def _resolve_zone_power(
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
    *,
    zone_data: dict[str, Any] | None = None,
    fetch_epoch: datetime | None = None,
    zones_info: list[dict[str, Any]] | None = None,
) -> tuple[str | None, str, str | None, datetime | None, datetime | None]:
    """Return `(power, source, homekit_power, changed, fetch_started)`; homekit_power is None when HomeKit was not consulted."""
    if zone_data is None:
        zone_data = _helpers.get_zone_state(coordinator.data, zone_id) or {}
    cloud_power = (zone_data.get("setting") or {}).get("power")
    provider = coordinator.homekit_provider
    # Before the class gate and the display source: our own owed ON is on whatever the zone class, unless HomeKit reads OFF; no fetch can land while the cloud is down.
    if cloud_power == "OFF" and provider is not None and provider.is_connected and _owed_on_beats_cloud_off(coordinator, zone_id, provider):
        return "ON", "homekit", provider.homekit_power(zone_id)[0], None, coordinator.last_zone_fetch_ts()
    if (
        provider is None or not provider.is_connected
        or not zone_qualifies_for_homekit_power(coordinator, zone_id, zones_info=zones_info)
    ):
        return cloud_power, "cloud", None, None, None
    zcm = coordinator.zone_config_manager
    if zcm is not None and zcm.get_zone_value(zone_id, "display_temp_source", "auto") == "cloud":
        return cloud_power, "cloud", None, None, None
    homekit_power, changed, observed = provider.homekit_power(zone_id)
    fetch_started = fetch_epoch if fetch_epoch is not None else coordinator.last_zone_fetch_ts()
    if homekit_power is None:
        return cloud_power, "cloud", None, changed, fetch_started
    # Strict: a change stamped at the fetch start may already be in that payload; a poll-seeded entry has no change time.
    if cloud_power is None or homekit_change_is_newer_than_fetch(changed, observed, fetch_started, 0.0):
        # An ON over a cloud OFF waits for the next fetch (our own owed ON returned above).
        if homekit_power == "ON" and cloud_power == "OFF":
            return cloud_power, "cloud", homekit_power, changed, fetch_started
        return homekit_power, "homekit", homekit_power, changed, fetch_started
    return cloud_power, "cloud", homekit_power, changed, fetch_started


def zone_power_with_source(
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
    *,
    zone_data: dict[str, Any] | None = None,
    fetch_epoch: datetime | None = None,
    zones_info: list[dict[str, Any]] | None = None,
) -> tuple[str | None, str]:
    """Return the zone power and where it came from, `(power, "homekit"|"cloud")`; the cloud power unless HomeKit has a usable value."""
    resolved = _resolve_and_log_power(
        coordinator, zone_id, zone_data=zone_data, fetch_epoch=fetch_epoch, zones_info=zones_info,
    )
    return resolved[0], resolved[1]


def _resolve_and_log_power(
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
    *,
    zone_data: dict[str, Any] | None,
    fetch_epoch: datetime | None,
    zones_info: list[dict[str, Any]] | None,
) -> tuple[str | None, str, str | None, datetime | None, datetime | None]:
    """Resolve the zone power and log a change of power, source or disagreement once."""
    resolved = _resolve_zone_power(
        coordinator, zone_id, zone_data=zone_data, fetch_epoch=fetch_epoch, zones_info=zones_info,
    )
    power, source, homekit_power, changed, fetch_started = resolved
    disagrees = homekit_power if source == "cloud" and homekit_power != power else None
    key = (coordinator.home_id, zone_id)
    previous = _zone_power_logged.get(key)
    if previous != (power, source, disagrees):
        _zone_power_logged[key] = (power, source, disagrees)
        if previous is None or previous[:2] != (power, source):
            _LOGGER.debug(
                "Helpers: zone %s power %s from %s (changed %s, fetch %s)", zone_id, power, source, changed, fetch_started,
            )
        if disagrees is not None:
            _LOGGER.debug("Helpers: zone %s HomeKit says %s, cloud says %s, using the cloud", zone_id, disagrees, power)
    return resolved


@dataclass(frozen=True, slots=True)
class ZoneView:
    """One zone's power, target and fetch epoch read together; pure reads, nothing is stored or engaged."""

    zone_id: str
    cloud_power: str | None
    power: str | None
    power_source: str
    cloud_target: float | None
    fetch_epoch: datetime | None
    cloud_overlay_termination: str | None


def zone_view(
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
    *,
    zone_data: dict[str, Any] | None = None,
    fetch_epoch: datetime | None = None,
    zones_info: list[dict[str, Any]] | None = None,
    sequence_pending: bool = False,
) -> ZoneView:
    """Read a zone's power and target inputs; the card target is `zone_view_target`, so a view never resolves it or touches the latch or own-OFF marks."""
    if zone_data is None:
        zone_data = _helpers.get_zone_state(coordinator.data, zone_id) or {}
    setting = zone_data.get("setting") or {}
    cloud_power = setting.get("power")
    epoch = fetch_epoch if fetch_epoch is not None else coordinator.last_zone_fetch_ts()
    if sequence_pending:
        # A pending write shows the cloud power with no HomeKit consulted, as the heating card always did.
        power, source = cloud_power, "cloud"
    else:
        power, source, *_ = _resolve_and_log_power(
            coordinator, zone_id, zone_data=zone_data, fetch_epoch=fetch_epoch, zones_info=zones_info,
        )
    overlay = zone_data.get("overlay")
    termination = overlay.get("termination") if isinstance(overlay, dict) else None
    return ZoneView(
        zone_id=zone_id,
        cloud_power=cloud_power,
        power=power,
        power_source=source,
        cloud_target=(setting.get("temperature") or {}).get("celsius"),
        fetch_epoch=epoch,
        cloud_overlay_termination=termination.get("type") if isinstance(termination, dict) else None,
    )


def zone_view_target(
    coordinator: TadoDataUpdateCoordinator, view: ZoneView, shown_power: str | None = None, *, homekit_only: bool = False,
) -> tuple[float | None, str]:
    """Resolve the card target for a view on demand; `shown_power` replaces the view's power, `homekit_only` leaves the cloud value out."""
    return resolve_zone_card_target(
        coordinator,
        view.zone_id,
        cloud_celsius=None if homekit_only else view.cloud_target,
        power=view.power if shown_power is None else shown_power,
        fetch_epoch=view.fetch_epoch,
    )


def _latch_record_epoch(epoch: datetime | None, changed_at: datetime | None, lag_seconds: float) -> datetime | None:
    """The fetch epoch the latch records: the plain epoch, or the later of it and the HomeKit change plus the lag when the lag is positive."""
    if lag_seconds <= 0 or changed_at is None or epoch is None:
        return epoch
    return max(epoch, changed_at + timedelta(seconds=lag_seconds))


def _off_record_is_overtaken(coordinator: TadoDataUpdateCoordinator, zone_id: str, recorded: datetime | None) -> bool:
    """Whether a recorded epoch protects nothing for a new OFF: a landed fetch, or a fetch in flight when the OFF came, is newer."""
    if fresher_than(coordinator.last_zone_fetch_ts(), recorded):
        return True
    # A fetch that started after the entry and no later than this OFF may land with the pre-OFF payload; one that started after the OFF is a fresh sight and must release it.
    started = coordinator.zone_fetch_epoch_for_write()
    changed_at = _resolve_zone_power(coordinator, zone_id)[3]
    return started is not None and changed_at is not None and started <= changed_at and fresher_than(started, recorded)


def is_homekit_off_held(coordinator: TadoDataUpdateCoordinator, zone_id: str) -> bool:
    """Whether a HomeKit app-off is held: from first seen until a zone fetch newer than that moment lands."""
    latch = coordinator.homekit_off_latch
    if zone_power_with_source(coordinator, zone_id) == ("OFF", "homekit"):
        if zone_id not in latch or _off_record_is_overtaken(coordinator, zone_id, latch[zone_id]):
            epoch = coordinator.zone_fetch_epoch_for_write() or coordinator.last_zone_fetch_ts()
            # The change time is read only when the lag is on, so the shipped 0.0 path stays exactly the plain epoch.
            changed_at = _resolve_zone_power(coordinator, zone_id)[3] if HOMEKIT_OFF_RELEASE_LAG_SECONDS > 0 else None
            latch[zone_id] = _latch_record_epoch(epoch, changed_at, HOMEKIT_OFF_RELEASE_LAG_SECONDS)
            _LOGGER.debug("Helpers: zone %s HomeKit app-off held until a newer zone fetch lands", zone_id)
        return True
    if zone_id not in latch:
        return False
    recorded = latch[zone_id]
    fetched = coordinator.last_zone_fetch_ts()
    # The signal can vanish (bridge drop, source set to Cloud) while the cloud data still predates the OFF, so only a newer fetch releases.
    if not fresher_than(fetched, recorded):
        return True
    del latch[zone_id]
    _LOGGER.debug("Helpers: zone %s HomeKit app-off released by a zone fetch newer than the hold", zone_id)
    return False


def cloud_overlay_is_off(zone_data: dict[str, Any] | None) -> bool:
    """Whether the cloud overlay on the zone switches it off (power OFF); a schedule-OFF block has no overlay and does not count."""
    overlay = (zone_data or {}).get("overlay")
    setting = overlay.get("setting") if isinstance(overlay, dict) else None
    return isinstance(setting, dict) and setting.get("power") == "OFF"


def is_off_in_app(coordinator: TadoDataUpdateCoordinator, zone_id: str, zone_data: dict[str, Any] | None) -> bool:
    """Whether the zone was switched off in the Tado app: a cloud OFF overlay, or a held HomeKit app-off."""
    # `or` short-circuits on purpose: a cloud OFF overlay neither engages nor releases the HomeKit latch.
    return (cloud_overlay_is_off(zone_data) and not owes_held_on(coordinator, zone_id)) or is_homekit_off_held(coordinator, zone_id)


def zone_power(coordinator: TadoDataUpdateCoordinator, zone_id: str) -> str | None:
    """Return the resolved zone power, "ON", "OFF" or None."""
    return zone_power_with_source(coordinator, zone_id)[0]


def zone_power_is_on(coordinator: TadoDataUpdateCoordinator, zone_id: str) -> bool:
    """Whether the resolved zone power is ON; False while our own accepted OFF write is unconfirmed (has_own_off_write)."""
    return not coordinator.has_own_off_write(zone_id) and zone_power(coordinator, zone_id) == "ON"


def resolve_zone_card_target(
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
    *,
    cloud_celsius: float | None,
    power: str | None,
    fetch_epoch: datetime | None,
) -> tuple[float | None, str]:
    """Return the target a climate card shows and its source; the cloud value when there is no reconciler."""
    reconciler = coordinator.state_reconciler
    if reconciler is None:
        return cloud_celsius, "cloud"
    held = owed_held_target(coordinator, zone_id, ambiguous_ok=False) if power == "ON" else None
    if held is not None:
        return held, "homekit"
    # Attached whether or not the bridge is connected, and cleared with the provider.
    reconciler.local_provider = coordinator.homekit_provider
    zcm = coordinator.zone_config_manager
    display_source = zcm.get_zone_value(zone_id, "display_temp_source", "auto") if zcm else "auto"
    lag = _helpers.get_homekit_cloud_lag_seconds(coordinator.hass, coordinator.config_entry.entry_id)
    return reconciler.resolve_zone_target(
        zone_id,
        cloud_celsius=cloud_celsius,
        zone_power=power,
        lag_seconds=lag,
        fetch_epoch=fetch_epoch,
        display_source=display_source,
    )


def _owed_on_beats_cloud_off(coordinator: TadoDataUpdateCoordinator, zone_id: str, provider: Any) -> bool:
    """Whether our owed ON stands over a cloud OFF: no fetch has covered it, or the device still reads the ON with no newer change than that fetch."""
    ledger = getattr(coordinator, "write_ledger", None)
    if ledger is None or ledger.owes_on(zone_id) is not True:
        return False
    device_power, changed, observed = provider.homekit_power(zone_id)
    if device_power == "OFF":
        return False
    if ledger.owes_power_on(zone_id) is True:
        return True
    return device_power == "ON" and not homekit_change_is_newer_than_fetch(changed, observed, coordinator.last_zone_fetch_ts(), 0.0)


def owed_held_target(coordinator: TadoDataUpdateCoordinator, zone_id: str, *, ambiguous_ok: bool) -> float | None:
    """The target of our owed ON while nothing newer than it has been seen (no covering fetch showing ON, no differing device change after it)."""
    ledger = getattr(coordinator, "write_ledger", None)
    if ledger is None or ledger.owes_on(zone_id) is not True:
        return None
    owed = ledger.owed(zone_id)
    target = ledger.owed_target(zone_id)
    if owed is None or target is None or (owed.ambiguous and not ambiguous_ok):
        return None
    cloud_off = ((_helpers.get_zone_state(coordinator.data, zone_id) or {}).get("setting") or {}).get("power") == "OFF"
    if not cloud_off and ledger.owes_power_on(zone_id) is not True:
        return None
    provider = coordinator.homekit_provider
    reading, changed, _observed = provider.get_target_temperature(zone_id) if provider is not None and provider.is_connected else (None, None, None)
    if reading is not None and changed is not None and changed > owed.created_at and abs(float(reading) - target) > 0.05:
        return None
    return float(target)

