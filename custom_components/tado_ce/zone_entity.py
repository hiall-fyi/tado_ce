"""What the heating, AC and hot-water entities share: identity, update hop, restore, fresh gate and timer tail."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .error_dispatch import is_typed_cloud_rejection
from .optimistic_helpers import fetch_landed_since_write

if TYPE_CHECKING:
    from collections.abc import Awaitable

    # Typing only: at runtime the mixin stays a plain object so it adds nothing to the MRO.
    class _ZoneHost(CoordinatorEntity[Any], RestoreEntity):
        _zone_id: str
        _zone_name: str
        _entity_type: str
        _attr_native_target_temperature: float | None

        def update(self) -> None: ...
else:
    _ZoneHost = object

_LOGGER = logging.getLogger(__name__)


class ZoneEntityMixin(_ZoneHost):
    """Identity, coordinator-update hop, restore-and-prime, fresh-gate skip and timer tail for a zone entity."""

    _LOG_LABEL: ClassVar[str]
    _ZONE_TYPE: ClassVar[str]

    @property
    def zone_id(self) -> str:
        """Return the Tado zone ID as a string."""
        return str(self._zone_id)

    @property
    def zone_type(self) -> str:
        """Return the zone type tag of this entity class."""
        return self._ZONE_TYPE

    @property
    def entity_type(self) -> str:
        """Return the entity type tag for state-capture routing."""
        return self._entity_type

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator data update."""
        self.update()
        self.async_write_ha_state()

    async def _async_restore_target(self, default: float | None) -> None:
        """Restore the last target across a restart, fall back to `default`, then prime from a published fetch."""
        last_state = await self.async_get_last_state()
        if last_state and last_state.attributes.get(ATTR_TEMPERATURE) is not None:
            self._attr_native_target_temperature = last_state.attributes[ATTR_TEMPERATURE]
            _LOGGER.debug(
                "%s: %s restored target %s°C from previous HA state",
                self._LOG_LABEL, self._zone_name, self._attr_native_target_temperature,
            )
        elif default is not None and self._attr_native_target_temperature is None:
            # No previous state: a default keeps the card controls usable immediately.
            self._attr_native_target_temperature = default
            _LOGGER.debug(
                "%s: %s has no previous state, defaulting target to %s°C",
                self._LOG_LABEL, self._zone_name, self._attr_native_target_temperature,
            )

        # a published fetch decides the target, the saved one only fills a gap
        if self.coordinator.last_zone_fetch_ts() is not None:
            self.update()

    def _skip_update_while_fresh(self, *, has_state: bool) -> bool:
        """Return True when `update()` should stop because a fresh optimistic state is held and no fetch landed since."""
        if not (self.coordinator.is_entity_fresh(self.entity_id) and not fetch_landed_since_write(self)):
            return False
        # Boot-freshness safety net: never skip an entity that has no cached state yet.
        if has_state:
            _LOGGER.debug("%s: %s skipping update, entity is fresh", self._LOG_LABEL, self._zone_name)
            return True
        _LOGGER.debug(
            "%s: %s marked fresh but has no cached state yet, updating anyway", self._LOG_LABEL, self._zone_name,
        )
        return False

    async def _async_timer_result(self, write: Awaitable[Any]) -> bool:
        """Await a timer write: a typed cloud rejection re-raises, a plain failure returns False."""
        try:
            await write
        except HomeAssistantError as err:
            # A rate limit, sign-in or blocked refusal keeps its translated message; a plain failure reports False.
            if is_typed_cloud_rejection(err):
                raise
            return False
        return True
