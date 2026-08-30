"""Tado CE zone configuration manager: per-zone overrides + listener fan-out."""

from __future__ import annotations

from contextlib import suppress
import logging
from typing import TYPE_CHECKING, Any

from .const import DEFAULT_WINDOW_TYPE, DEFAULT_ZONE_CONFIG, OVERLAY_MODE_DEFAULT, WINDOW_U_VALUES

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

    from .config_manager import ConfigurationManager
    from .data_loader import DataLoader

_LOGGER = logging.getLogger(__name__)


class ZoneConfigManager:
    """Per-zone settings store + listener fan-out, backed by DataLoader's auxiliary store."""

    def __init__(self, hass: HomeAssistant, home_id: str, data_loader: DataLoader) -> None:
        """Initialise the manager (does not load, call `async_load` after construction)."""
        self._hass = hass
        self._home_id = home_id
        self._data_loader = data_loader
        self._config: dict[str, dict[str, Any]] = {}
        self._listeners: list[Callable[[str, str, Any], None]] = []

    async def async_load(self) -> None:
        """Load and migrate the zone config dict from Store."""
        raw = await self._data_loader.async_load_auxiliary("zone_config")
        if raw and isinstance(raw, dict):
            zones = raw.get("zones", {})
            self._config = zones if isinstance(zones, dict) else {}
        else:
            self._config = {}

        # Cumulative migrations applied on every load, cheap, and
        # ensures users on older schemas get cleaned up the next
        # time they open Options.
        migrated = False
        for zone_cfg in self._config.values():
            ap = zone_cfg.get("adaptive_preheat")
            if isinstance(ap, bool):
                zone_cfg["adaptive_preheat"] = "active" if ap else "off"
                migrated = True
            # `temp_offset` was a v2.x key that never reached the
            # cloud: strip on sight.
            if "temp_offset" in zone_cfg:
                del zone_cfg["temp_offset"]
                migrated = True
            # `smart_valve_control` (bool) → `svc_mode` (str):
            # the new shape supports `valve_target` / `cycle` /
            # `off`, the old bool only covered on/off.
            svc_bool = zone_cfg.pop("smart_valve_control", None)
            if svc_bool is not None:
                migrated = True
                if "svc_mode" not in zone_cfg:
                    zone_cfg["svc_mode"] = "valve_target" if svc_bool else "off"
        if migrated:
            await self.async_save()
            _LOGGER.info(
                "Zone Config: applied cumulative migrations to "
                "stored zone config",
            )
        _LOGGER.debug(
            "Zone Config: loaded overrides for %d zone(s)",
            len(self._config),
        )

    async def async_save(self) -> None:
        """Schedule a debounced write of the current zone config to Store."""
        self._data_loader.save_auxiliary("zone_config", {"version": 1, "zones": self._config})
        _LOGGER.debug(
            "Zone Config: queued save: %d zone(s)",
            len(self._config),
        )

    def get_zone_config(self, zone_id: str) -> dict[str, Any]:
        """Return the merged config for a zone: defaults + user overrides."""
        zone_config = self._config.get(str(zone_id), {})
        merged = {**DEFAULT_ZONE_CONFIG, **zone_config}
        # In-memory `adaptive_preheat` migration covers stale
        # caches that were loaded before `async_load` ran.
        ap = merged.get("adaptive_preheat")
        if isinstance(ap, bool):
            merged["adaptive_preheat"] = "active" if ap else "off"
        return merged

    def has_any_svc_active(self) -> bool:
        """True when at least one zone has Smart Valve Control or Offset Sync enabled."""
        return any(
            cfg.get("svc_mode", "off") != "off"
            for cfg in self._config.values()
        )

    def has_zone_override(self, zone_id: str, key: str) -> bool:
        """True when the user has explicitly set this key (not merely inherited the default)."""
        zone_config = self._config.get(str(zone_id), {})
        return key in zone_config

    def get_zone_value(self, zone_id: str, key: str, default: Any = None) -> Any:
        """Read one config key for a zone; falls back to `DEFAULT_ZONE_CONFIG[key]`."""
        config = self.get_zone_config(str(zone_id))
        if default is None:
            return config.get(key, DEFAULT_ZONE_CONFIG.get(key))
        return config.get(key, default)

    async def async_set_zone_value(self, zone_id: str, key: str, value: Any) -> None:
        """Set one config key for a zone, persist, and notify listeners on change."""
        zone_id = str(zone_id)
        if zone_id not in self._config:
            self._config[zone_id] = {}

        old_value = self._config[zone_id].get(key)
        self._config[zone_id][key] = value

        await self.async_save()

        if old_value != value:
            for listener in self._listeners:
                try:
                    listener(zone_id, key, value)
                except Exception:
                    _LOGGER.warning(
                        "Zone Config: listener raised while handling "
                        "zone %s key %s, continuing with the next "
                        "listener so other consumers still get the "
                        "update",
                        zone_id, key,
                        exc_info=True,
                    )

    async def async_prune_reassigned_zones(self, zone_ids: set[str]) -> set[str]:
        """Delete a reused zone's settings from the live config so a later unrelated save can't resurrect them."""
        pruned = {zone_id for zone_id in zone_ids if zone_id in self._config}
        if not pruned:
            return set()
        for zone_id in pruned:
            del self._config[zone_id]
        await self.async_save()
        _LOGGER.info(
            "Zone Config: cleared settings for reassigned zone(s) %s "
            "(the id is now a different room)",
            sorted(pruned),
        )
        return pruned

    async def async_get_or_fetch_overlay_default(self, zone_id: str, api_client: Any) -> str:
        """Return a zone's overlay_mode, fetching the Tado-app default if unset.

        If the zone already has a stored overlay_mode (an explicit user
        override OR a previously-cached fetch), it is returned without any
        cloud call. Otherwise this reads the user's own per-zone default from
        the cloud (`get_zone_default_overlay`) and caches it into zone_config,
        so the next call short-circuits on the stored value. Falls back to
        OVERLAY_MODE_DEFAULT (MANUAL) when the fetch is unavailable, and caches
        that too. Note: the fetch is gated by the stored-value check, not an
        in-method guard, so overlapping calls before the first cache-write can
        each fetch once.
        """
        zone_id = str(zone_id)
        if self.has_zone_override(zone_id, "overlay_mode"):
            return str(self.get_zone_value(zone_id, "overlay_mode", OVERLAY_MODE_DEFAULT))

        fetched = await api_client.get_zone_default_overlay(zone_id)
        mode = fetched if fetched in ("MANUAL", "TADO_MODE", "TIMER") else OVERLAY_MODE_DEFAULT
        await self.async_set_zone_value(zone_id, "overlay_mode", mode)
        _LOGGER.debug(
            "Zone Config: zone %s fresh overlay default resolved to %s "
            "(server default %s)", zone_id, mode, fetched,
        )
        return str(mode)

    def add_listener(self, callback: Callable[[str, str, Any], None]) -> Callable[[], None]:
        """Subscribe `callback(zone_id, key, value)` to config changes, returns an unsubscribe."""
        self._listeners.append(callback)

        def _remove_listener() -> None:
            """Idempotent unsubscribe: `suppress(ValueError)` covers double-removal races."""
            with suppress(ValueError):
                self._listeners.remove(callback)

        return _remove_listener

    def get_effective_window_type(self, zone_id: str, config_manager: ConfigurationManager) -> str:
        """Resolve the zone's window type: explicit per-zone override, else the live global setting.

        Never caches config_manager's value: Global Window Type is a user-editable
        Advanced Settings option, unlike overlay_mode's fetch-once cloud default.
        """
        if self.has_zone_override(zone_id, "window_type"):
            window_type = str(self.get_zone_value(zone_id, "window_type", DEFAULT_WINDOW_TYPE))
            if window_type not in WINDOW_U_VALUES:
                _LOGGER.warning(
                    "Zone Config: zone %s window_type override %r is not a "
                    "recognised window type; numeric U-value lookups will use "
                    "the default %s",
                    zone_id, window_type, DEFAULT_WINDOW_TYPE,
                )
            return window_type
        return config_manager.get_mold_risk_window_type()

    def get_window_u_value(self, zone_id: str, config_manager: ConfigurationManager) -> float:
        """Resolve the U-value (W/m²K) for the zone's effective window type."""
        window_type = self.get_effective_window_type(zone_id, config_manager)
        return WINDOW_U_VALUES.get(window_type, WINDOW_U_VALUES[DEFAULT_WINDOW_TYPE])

    def get_passive_detector_window_u_value(self, zone_id: str) -> float:
        """Resolve the per-zone U-value for the passive open-window detector's threshold scaling.

        Deliberately does NOT consult the global Window Type setting: that would let a
        house-wide setting silently change detection sensitivity, an axis already ruled
        orthogonal to this detector's U-value/threshold relationship.
        """
        window_type = self.get_zone_value(zone_id, "window_type", DEFAULT_WINDOW_TYPE)
        return WINDOW_U_VALUES.get(window_type, WINDOW_U_VALUES[DEFAULT_WINDOW_TYPE])

    def get_surface_temp_offset(self, zone_id: str) -> float:
        """Return the user-set surface offset (°C) for mold-risk calibration."""
        return self.get_zone_value(zone_id, "surface_temp_offset", 0.0)  # type: ignore[no-any-return]


    @property
    def zones(self) -> dict[str, dict[str, Any]]:
        """Return a defensive copy of the per-zone config dict."""
        return self._config.copy()
