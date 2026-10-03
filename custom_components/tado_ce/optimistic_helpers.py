"""Tado CE optimistic state management: 4-layer stale data defense.

Layer 1: Time-window freshness (_optimistic_set_at)
Layer 2: Sequence numbers (_optimistic_sequence)
Layer 3: Expected-state confirmation (_expected_* fields)
Layer 4: Zone-fetch freshness (_optimistic_zone_fetch_epoch) — gates EXPIRED only.
"""

from __future__ import annotations

from enum import Enum
import logging
import time
from typing import TYPE_CHECKING, Any

from .const import DEFAULT_OPTIMISTIC_WINDOW_SECONDS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import TadoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# All optimistic tracking fields across entity types.
# clear_optimistic_state() resets every field present on the entity to None.
_OPTIMISTIC_FIELDS = (
    # Universal fields (all entity types)
    "_optimistic_set_at",
    "_optimistic_sequence",
    "_optimistic_preserved",
    "_optimistic_zone_fetch_epoch",
    # Climate entities (climate_heating.py, climate_ac.py)
    "_expected_hvac_mode",
    "_expected_hvac_action",
    "_expected_target_temperature",
    # Water heater entities
    "_expected_operation",
    "_expected_temperature",
    # Select entities
    "_expected_mode",
)


class OptimisticUpdateResult(Enum):
    """Result of optimistic update resolution."""

    ACCEPT_API = "accept_api"
    PRESERVE_OPTIMISTIC = "preserve"
    EXPIRED = "expired"


def clear_optimistic_state(entity: object) -> None:
    """Reset every optimistic-tracking field present on the entity to None."""
    for field in _OPTIMISTIC_FIELDS:
        if hasattr(entity, field):
            setattr(entity, field, None)


async def set_optimistic_fields(
    entity: object,
    coordinator: TadoDataUpdateCoordinator,
    *,
    expected: dict[str, Any] | None = None,
    preserved_attrs: dict[str, Any] | None = None,
) -> None:
    """Mark an entity as having an in-flight optimistic update.

    Stamps the entity with the current monotonic time, a coordinator
    sequence number, and the current zone-fetch epoch, records each
    key in `expected` as `_expected_{key}` on the entity, optionally
    stores extra attributes to preserve during the window (AC fan /
    swing modes etc.), and marks the entity fresh in the coordinator.
    The mark must be awaited before any async_write_ha_state() call
    so the next poll sees the updated freshness flag.
    """
    entity._optimistic_set_at = time.monotonic()  # type: ignore[attr-defined]
    entity._optimistic_sequence = coordinator.get_next_sequence()  # type: ignore[attr-defined]
    entity._optimistic_zone_fetch_epoch = coordinator.zone_fetch_epoch_for_write()  # type: ignore[attr-defined]

    if expected:
        for key, value in expected.items():
            setattr(entity, f"_expected_{key}", value)

    if preserved_attrs is not None:
        entity._optimistic_preserved = preserved_attrs  # type: ignore[attr-defined]

    await coordinator.mark_entity_fresh(entity.entity_id)  # type: ignore[attr-defined]

    _LOGGER.debug(
        "%s: optimistic update set, expected=%s, seq=%s",
        getattr(entity, "_zone_name", entity.entity_id),  # type: ignore[attr-defined]
        expected,
        entity._optimistic_sequence,  # type: ignore[attr-defined]
    )


def is_within_optimistic_window(
    hass: HomeAssistant,
    optimistic_set_at: float | None,
    entry_id: str | None = None,
) -> bool:
    """Return True when the elapsed time since `optimistic_set_at` is still inside the window."""
    if optimistic_set_at is None:
        return False
    from .helpers import get_optimistic_window

    elapsed = time.monotonic() - optimistic_set_at
    return elapsed < get_optimistic_window(hass, entry_id=entry_id) if hass else elapsed < DEFAULT_OPTIMISTIC_WINDOW_SECONDS


def _has_fresher_zone_poll(entity: object) -> bool:
    """Return True unless a fresher zone poll is still owed since the write.

    A cloud write triggers an immediate refresh; a HomeKit-local write only
    schedules a delayed one, so elapsed time alone can't tell a genuine
    failure from a confirmation that just hasn't landed yet. Skips the check
    (returns True) whenever there's nothing to compare, so this never
    introduces a new hang on a path that had none.
    """
    written_epoch = getattr(entity, "_optimistic_zone_fetch_epoch", None)
    if written_epoch is None:
        return True
    coordinator = getattr(entity, "coordinator", None)
    if coordinator is None:
        return True
    current_epoch = coordinator.last_zone_fetch_ts()
    if current_epoch is None:
        return True
    return bool(current_epoch > written_epoch)


def resolve_optimistic_update(
    entity: object,
    *,
    api_values: dict[str, Any],
    entry_id: str | None = None,
) -> OptimisticUpdateResult:
    """Decide whether to keep optimistic state or accept the API-reported values.

    Three layers, in order: sequence + expected-state confirmation
    (preferred), time-window fallback (when the entity has no
    sequence), and expiry (sequence set but window elapsed without
    confirmation, and a zone poll newer than the write has landed). A
    `_expected_*` field of None counts as "don't check this key".
    """
    seq = getattr(entity, "_optimistic_sequence", None)
    set_at = getattr(entity, "_optimistic_set_at", None)

    if seq is None and set_at is None:
        return OptimisticUpdateResult.ACCEPT_API

    if seq is not None:
        all_confirmed = True
        for key, api_val in api_values.items():
            expected_val = getattr(entity, f"_expected_{key}", None)
            if expected_val is not None and expected_val != api_val:
                all_confirmed = False
                break

        if all_confirmed:
            _LOGGER.debug(
                "%s: API confirmed optimistic state, clearing tracking",
                getattr(entity, "_zone_name", getattr(entity, "entity_id", "?")),
            )
            clear_optimistic_state(entity)
            return OptimisticUpdateResult.ACCEPT_API

        # Window elapsed without confirmation: the write likely
        # failed silently, so accept API state to avoid the entity
        # getting stuck on a value Tado never agreed to — unless no
        # fresher zone poll has landed yet to prove that.
        hass: HomeAssistant | None = getattr(entity, "hass", None)
        window_elapsed = set_at is not None and not is_within_optimistic_window(
            hass, set_at, entry_id=entry_id,  # type: ignore[arg-type]
        )
        if window_elapsed and not _has_fresher_zone_poll(entity):
            _LOGGER.debug(
                "%s: optimistic window elapsed but no fresher zone poll "
                "yet, holding",
                getattr(entity, "_zone_name", getattr(entity, "entity_id", "?")),
            )
            return OptimisticUpdateResult.PRESERVE_OPTIMISTIC

        if window_elapsed:
            _LOGGER.warning(
                "%s: optimistic window expired without API confirmation, "
                "accepting Tado's reported state",
                getattr(entity, "_zone_name", getattr(entity, "entity_id", "?")),
            )
            clear_optimistic_state(entity)
            return OptimisticUpdateResult.EXPIRED

        _LOGGER.debug(
            "%s: preserving optimistic state. API has not yet confirmed",
            getattr(entity, "_zone_name", getattr(entity, "entity_id", "?")),
        )
        return OptimisticUpdateResult.PRESERVE_OPTIMISTIC

    # Time-window fallback (entity has no sequence, e.g. some switches).
    hass = getattr(entity, "hass", None)
    if hass and is_within_optimistic_window(hass, set_at, entry_id=entry_id):
        _LOGGER.debug(
            "%s: preserving optimistic state, still inside time window",
            getattr(entity, "_zone_name", getattr(entity, "entity_id", "?")),
        )
        return OptimisticUpdateResult.PRESERVE_OPTIMISTIC

    clear_optimistic_state(entity)
    return OptimisticUpdateResult.ACCEPT_API
