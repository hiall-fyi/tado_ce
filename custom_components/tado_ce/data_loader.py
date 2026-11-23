"""Tado CE data loader: per-home HA Store persistence with in-memory cache.

Manages two categories of persistent data via HA Store:

- **API data** (immediate save): written on every API response. Uses `Store.async_save()`.
- **Auxiliary data** (debounced save): per-feature state. Uses `Store.async_delay_save()`;
  HA flushes pending writes on `EVENT_HOMEASSISTANT_FINAL_WRITE`.

`HeatingCycleStorage`, `InsightHistoryTracker`, `StateRestoreManager`, and `APICallTracker`
keep their own Store instances for data-format migration, dirty-flag tracking, or
domain-specific load/save logic.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .const import (
    OUTDOOR_TEMP_HISTORY_MAX,
    OVERLAY_MODE_DEFAULT,
    TIMER_DURATION_DEFAULT,
    TIMER_DURATION_MAX,
    TIMER_DURATION_MIN,
)
from .helpers import normalise_timer_minutes

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_CACHE_MISSING = object()
"""Sentinel for negative cache entries (store empty / file not found)."""

_CACHE_DIRTY = object()
"""Sentinel marking a cache entry as stale; treated like missing on read but signals 'refetch'."""

# Store configuration: name -> save_delay_seconds
# delay=0 means immediate save (async_save), used for API data.
# delay>0 means debounced save (async_delay_save), used for auxiliary data.
_ALL_STORES: dict[str, int] = {
    # API Data: immediate save (written on every API response)
    "zones": 0,
    "config": 0,
    "home_state": 0,
    "ratelimit": 0,
    "zones_info": 0,
    "weather": 0,
    "mobile_devices": 0,
    "home_devices": 0,
    "offsets": 0,
    "schedules": 0,
    "zone_capabilities": 0,
    "zone_capabilities_fp": 0,
    "heating_circuits": 0,
    "heating_circuit_control": 0,
    # Auxiliary Data: debounced save
    "wc_state": 10,
    "bridge_health": 10,
    "outdoor_temp_history": 10,
    "zone_config": 5,
    "smart_comfort_cache": 30,
    "overlay_mode": 5,
    "timer_duration": 5,
    "homekit_savings": 10,
    "write_ledger": 10,
    "insight_runtime_state": 30,
    "zone_identity": 10,
    "zone_type_baseline": 10,
    "write_timing": 15,
    "window_control": 2,
}

STORE_VERSION = 1



class DataLoader:
    """One-per-config-entry HA Store persistence with a home-id-scoped cache.

    Usage::

        loader = DataLoader(home_id="12345", hass=hass)
        await loader.async_load_all_to_cache()
        zones = loader.get_cached("zones")
        wc = await loader.async_load_wc_state()
    """

    def __init__(self, home_id: str, hass: HomeAssistant | None = None) -> None:
        """Initialise the loader bound to one Tado home."""
        self._home_id = home_id
        self._cache: dict[str, Any] = {}
        self._stores: dict[str, Store[dict[str, Any] | list[Any]]] = {}
        if hass:
            self._init_stores(hass)

    def _init_stores(self, hass: HomeAssistant) -> None:
        """Create one HA Store instance per managed key."""
        for name in _ALL_STORES:
            key = f"tado_ce/{name}_{self._home_id}"
            if name == "write_timing":
                self._stores[name] = _RedactedKeyStore(hass, STORE_VERSION, key, f"tado_ce/{name}_REDACTED")
            else:
                self._stores[name] = Store(hass, STORE_VERSION, key)

    @property
    def home_id(self) -> str:
        """Return the Tado home ID this loader is scoped to."""
        return self._home_id

    # --- Cache API ---

    def update_cache(self, base_name: str, data: dict[str, Any] | list[Any]) -> None:
        """Update the in-memory cache entry without touching the Store."""
        self._cache[base_name] = data

    def get_cached(self, base_name: str) -> dict[str, Any] | list[Any] | None:
        """Return the cached value, or None when missing or negatively cached."""
        result = self._cache.get(base_name)
        if result is _CACHE_MISSING or result is _CACHE_DIRTY:
            return None
        return result

    def mark_cache_dirty(self, base_name: str) -> None:
        """Mark a cache entry as needing refetch on the next read."""
        self._cache[base_name] = _CACHE_DIRTY

    # --- API Data Store Operations ---

    async def async_update_store(self, name: str, data: dict[str, Any] | list[Any]) -> None:
        """Save API data to Store immediately and update the cache."""
        store = self._stores.get(name)
        if store is None:
            _LOGGER.warning(
                "Data Loader: cannot save unknown store %r, "
                "this is a programming error, the data will not persist",
                name,
            )
            return
        await store.async_save(data)
        self._cache[name] = data

    async def async_merge_store(
        self,
        name: str,
        updates: Mapping[str, Any] | None = None,
        removed: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Apply removals and updates to the live cached dict in place, save that same object, and return it."""
        store = self._stores.get(name)
        if store is None:
            _LOGGER.warning(
                "Data Loader: cannot merge into unknown store %r, "
                "this is a programming error, the data will not persist",
                name,
            )
            return {}
        live = self._cache.get(name)
        if not isinstance(live, dict):
            # The Store copy is the base, or a partial update would be saved over the zones it holds.
            loaded = await store.async_load()
            live = self._cache.get(name)
            if not isinstance(live, dict):
                live = dict(loaded) if isinstance(loaded, dict) else {}
                self._cache[name] = live
        # No await between the read and these mutations, so no other writer can interleave.
        for key in removed:
            live.pop(key, None)
        if updates:
            live.update(updates)
        # HA's Store logs a failed disk write instead of raising it, leaving the cache ahead of disk; the next save writes the whole dict.
        await store.async_save(live)
        return live

    async def async_flush_pending_saves(self) -> None:
        """Drain debounced writes before teardown.

        HA's FINAL_WRITE flushes delayed saves on shutdown but not on a
        config-entry reload, so call this on unload to avoid losing a value
        queued just before a reload.
        """
        for name, store in self._stores.items():
            try:
                # HA-internal, untyped: what FINAL_WRITE itself calls to drain a
                # pending delayed save. No public flush API exists; no-ops when
                # nothing is pending.
                await store._async_handle_write_data()  # type: ignore[no-untyped-call]  # noqa: SLF001
            except Exception:
                # One store's write failure must not block the rest.
                _LOGGER.debug(
                    "Data Loader: could not flush pending save for %r on "
                    "teardown", name, exc_info=True,
                )

    async def async_load_all_to_cache(self) -> None:
        """Populate the cache with every API-data store on cold start.

        Reads each store once and caches a negative sentinel when it
        comes back empty, so later reads know the difference between
        "not loaded yet" and "nothing stored".
        """
        api_store_names = [name for name, delay in _ALL_STORES.items() if delay == 0]
        for name in api_store_names:
            store = self._stores.get(name)
            if store is None:
                self._cache[name] = _CACHE_MISSING
                continue
            try:
                data = await store.async_load()
            except HomeAssistantError:
                _LOGGER.warning(
                    "Data Loader: could not load store %r, caching as "
                    "missing, integration will rebuild from the next "
                    "API response",
                    name, exc_info=True,
                )
                self._cache[name] = _CACHE_MISSING
                continue

            if data is not None:
                _LOGGER.debug("Data Loader: loaded %r (type=%s)", name, type(data).__name__)
            self._cache[name] = data if data is not None else _CACHE_MISSING

    # --- Backward-compatible sync cache reads ---
    # Thin wrappers preserved for callers written before
    # async_load_all_to_cache existed; they read straight from the
    # in-memory cache, so they are sync.

    def load_zones_file(self) -> dict[str, Any] | None:
        """Return cached zone states, or None if unloaded."""
        return self.get_cached("zones")  # type: ignore[return-value]

    def load_zones_info_file(self) -> list[Any] | None:
        """Return cached zone metadata, or None if unloaded."""
        return self.get_cached("zones_info")  # type: ignore[return-value]

    def load_mobile_devices_file(self) -> list[Any] | None:
        """Return cached mobile-device list, or None if unloaded."""
        return self.get_cached("mobile_devices")  # type: ignore[return-value]

    def load_ratelimit_file(self) -> dict[str, Any] | None:
        """Return the rate-limit snapshot with the newest parsed remaining and limit laid over it."""
        snapshot = self.get_cached("ratelimit")
        if not isinstance(snapshot, dict):
            return None
        live = self.get_cached("ratelimit_live")
        if not isinstance(live, dict):
            return snapshot
        overlay = {
            key: value for key in ("remaining", "limit")
            if isinstance(value := live.get(key), int) and not isinstance(value, bool)
        }
        return {**snapshot, **overlay} if overlay else snapshot

    def load_schedules_file(self) -> dict[str, Any] | None:
        """Return cached schedule data for every zone, or None if unloaded."""
        return self.get_cached("schedules")  # type: ignore[return-value]

    # === Convenience methods ===

    def get_zone_schedule(self, zone_id: str) -> dict[str, Any] | None:
        """Return the cached schedule for one zone, or None when unavailable."""
        schedules = self.load_schedules_file()
        if schedules:
            return schedules.get(zone_id)
        return None

    # === Auxiliary Data: Store-backed with debounced writes ===

    async def async_load_auxiliary(self, name: str) -> dict[str, Any] | list[Any] | None:
        """Load one auxiliary value from Store."""
        store = self._stores.get(name)
        if not store:
            _LOGGER.warning(
                "Data Loader: cannot load unknown auxiliary store %r, "
                "this is a programming error",
                name,
            )
            return None

        data = await store.async_load()
        if data is not None:
            return data
        return None

    def save_auxiliary(self, name: str, data: dict[str, Any] | list[Any]) -> None:
        """Queue a debounced save for one auxiliary value (delay per store config)."""
        store = self._stores.get(name)
        if store:
            delay = _ALL_STORES.get(name, 10)
            store.async_delay_save(lambda: data, delay)
        else:
            _LOGGER.warning(
                "Data Loader: cannot save unknown auxiliary store %r, "
                "this is a programming error, the data will not persist",
                name,
            )

    async def async_remove_auxiliary(self, name: str) -> None:
        """Delete one auxiliary store file and drop any save still queued for it."""
        store = self._stores.get(name)
        if store is not None:
            await store.async_remove()

    # === Overlay mode (per-home, Store-backed) ===

    async def async_load_overlay_mode(self) -> str:
        """Return the configured overlay mode, falling back to the default."""
        try:
            data = await self.async_load_auxiliary("overlay_mode")
            if data is None:
                return OVERLAY_MODE_DEFAULT
            if not isinstance(data, dict):
                _LOGGER.warning(
                    "Data Loader: overlay_mode store had unexpected format, "
                    "falling back to default %s",
                    OVERLAY_MODE_DEFAULT,
                )
                return OVERLAY_MODE_DEFAULT
            mode = data.get("overlay_mode", OVERLAY_MODE_DEFAULT)
            if mode == "NEXT_TIME_BLOCK":
                # Legacy value the v2 endpoint rejects; coerce to its
                # equivalent, "until next automatic change".
                mode = "TADO_MODE"
            if mode not in ("TADO_MODE", "TIMER", "MANUAL"):
                _LOGGER.warning(
                    "Data Loader: overlay_mode value %r is not recognised, "
                    "falling back to default %s",
                    mode, OVERLAY_MODE_DEFAULT,
                )
                return OVERLAY_MODE_DEFAULT
            return mode  # type: ignore[no-any-return]
        except (HomeAssistantError, OSError) as e:
            _LOGGER.warning(
                "Data Loader: could not load overlay mode (%s), "
                "using default %s",
                e, OVERLAY_MODE_DEFAULT,
            )
            return OVERLAY_MODE_DEFAULT

    def save_overlay_mode(self, mode: str) -> bool:
        """Persist the overlay mode if it is a recognised value."""
        if mode not in ("TADO_MODE", "TIMER", "MANUAL"):
            _LOGGER.warning(
                "Data Loader: refusing to save unrecognised overlay mode %r",
                mode,
            )
            return False
        self.save_auxiliary("overlay_mode", {"overlay_mode": mode})
        _LOGGER.debug("Data Loader: queued overlay mode save (%s)", mode)
        return True

    # === Timer duration (per-home, Store-backed) ===

    async def async_load_timer_duration(self) -> int:
        """Return the saved timer duration in minutes, or the default on error."""
        try:
            data = await self.async_load_auxiliary("timer_duration")
            if data is None:
                return TIMER_DURATION_DEFAULT
            if isinstance(data, dict):
                return normalise_timer_minutes(data.get("timer_duration", TIMER_DURATION_DEFAULT))
        except (HomeAssistantError, OSError) as e:
            _LOGGER.debug(
                "Data Loader: could not load timer duration (%s), "
                "using default %d minutes",
                e, TIMER_DURATION_DEFAULT,
            )
        return TIMER_DURATION_DEFAULT

    def save_timer_duration(self, duration: int) -> bool:
        """Persist the timer duration if it is inside the allowed range."""
        if not isinstance(duration, int) or duration < TIMER_DURATION_MIN or duration > TIMER_DURATION_MAX:
            _LOGGER.warning(
                "Data Loader: refusing to save timer duration %r, "
                "must be an int in %d..%d minutes",
                duration, TIMER_DURATION_MIN, TIMER_DURATION_MAX,
            )
            return False
        self.save_auxiliary("timer_duration", {"timer_duration": duration})
        _LOGGER.debug("Data Loader: queued timer duration save (%d minutes)", duration)
        return True

    # === Outdoor temperature history (Store-backed) ===

    async def async_load_outdoor_temp_history(self) -> list[Any]:
        """Return the cached outdoor-temperature ring buffer (most recent last)."""
        try:
            data = await self.async_load_auxiliary("outdoor_temp_history")
            if data is None:
                _LOGGER.debug(
                    "Data Loader: outdoor_temp_history not yet saved, "
                    "starting fresh",
                )
                return []
            if isinstance(data, dict):
                readings = data.get("readings", [])
                readings = [float(r) for r in readings if isinstance(r, (int, float))]
                return readings[-OUTDOOR_TEMP_HISTORY_MAX:]
            return []
        except (HomeAssistantError, OSError) as e:
            _LOGGER.warning(
                "Data Loader: could not load outdoor temperature history "
                "(%s), starting fresh",
                e,
            )
            return []

    def save_outdoor_temp_history(self, readings: list[Any]) -> bool:
        """Persist the outdoor-temperature ring buffer (trimmed to the configured max)."""
        trimmed = readings[-OUTDOOR_TEMP_HISTORY_MAX:]
        data = {"readings": trimmed}
        self.save_auxiliary("outdoor_temp_history", data)
        return True

    # === Weather Compensation State (Store-backed) ===

    async def async_load_wc_state(self) -> dict[str, Any] | None:
        """Return the persisted weather-compensation state, or None when missing."""
        try:
            data = await self.async_load_auxiliary("wc_state")
        except (HomeAssistantError, OSError) as e:
            _LOGGER.warning(
                "Data Loader: could not load weather-compensation state "
                "(%s), starting fresh, smoothing buffer will rebuild",
                e,
            )
            return None
        if data is not None and isinstance(data, dict):
            return data
        return None

    def save_wc_state(self, data: dict[str, Any]) -> bool:
        """Persist the weather-compensation state."""
        self.save_auxiliary("wc_state", data)
        return True

    # === Bridge Health State (Store-backed) ===

    async def async_load_bridge_health(self) -> dict[str, Any] | None:
        """Return the persisted bridge-health state, or None when missing."""
        try:
            data = await self.async_load_auxiliary("bridge_health")
        except (HomeAssistantError, OSError) as e:
            _LOGGER.warning(
                "Data Loader: could not load bridge-health state (%s), "
                "starting fresh, health metrics will rebuild",
                e,
            )
            return None
        if data is not None and isinstance(data, dict):
            return data
        return None

    def save_bridge_health(self, data: dict[str, Any]) -> bool:
        """Persist the bridge-health state."""
        self.save_auxiliary("bridge_health", data)
        return True

    # === HomeKit Savings Counters (Store-backed) ===

    async def async_load_homekit_savings(self) -> dict[str, Any] | None:
        """Return the persisted HomeKit API-savings counters, or None when missing."""
        try:
            data = await self.async_load_auxiliary("homekit_savings")
        except (HomeAssistantError, OSError) as e:
            _LOGGER.warning(
                "Data Loader: could not load HomeKit savings counters "
                "(%s), counters will reset",
                e,
            )
            return None
        if data is not None and isinstance(data, dict):
            return data
        return None

    def save_homekit_savings(self, data: dict[str, Any]) -> bool:
        """Persist the HomeKit API-savings counters."""
        self.save_auxiliary("homekit_savings", data)
        return True

    # === Write ledger owed entries (Store-backed) ===

    async def async_load_write_ledger(self) -> dict[str, Any] | None:
        """Return the persisted owed entries, or None when missing, unreadable or written by a newer version."""
        try:
            data = await self.async_load_auxiliary("write_ledger")
        except (HomeAssistantError, OSError, NotImplementedError) as e:
            # NotImplementedError is what Store raises for a newer stored version; the file is then ignored.
            _LOGGER.warning(
                "Data Loader: could not load the held-change store (%s), "
                "starting with none kept",
                e,
            )
            return None
        if data is not None and isinstance(data, dict):
            return data
        return None

    def save_write_ledger(self, data: dict[str, Any]) -> bool:
        """Persist the owed entries of the write ledger."""
        self.save_auxiliary("write_ledger", data)
        return True


class _RedactedKeyStore(Store[dict[str, Any] | list[Any]]):
    """Store whose file name keeps the real key but whose written JSON carries a fixed key."""

    def __init__(self, hass: HomeAssistant, version: int, key: str, posted_key: str) -> None:
        """Remember the key to write into the file."""
        self._posted_key = posted_key
        super().__init__(hass, version, key)

    # The posted file must not carry the Tado home id; the file name still comes from self.key.
    async def _async_write_data(self, data: dict[str, Any]) -> None:
        await super()._async_write_data({**data, "key": self._posted_key})

