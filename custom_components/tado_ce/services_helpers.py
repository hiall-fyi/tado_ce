"""Services-layer wrapper for cloud writes that operate on a discovered entity."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiohttp

from .climate_base import cancel_zone_tap, release_zone_hold
from .helpers import async_trigger_immediate_refresh, mask_api_url
from .zone_write_ledger import ordered_zone_write

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from homeassistant.core import HomeAssistant

    from .coordinator import TadoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def _verify_unknown_outcome(
    hass: HomeAssistant, coordinator: TadoDataUpdateCoordinator, zone_id: str, refresh_entity_id: str | None, stamp_write: bool,
) -> None:
    """Mark the zone unconfirmed after a timeout or lost connection and ask for one zone fetch to show what Tado holds."""
    # "" is a non-zone call; a write that stamps nothing (an offset) leaves the zone's overlay as it was.
    if zone_id and stamp_write:
        coordinator.write_ledger.mark_unknown(zone_id)
    if refresh_entity_id is not None:
        await async_trigger_immediate_refresh(hass, refresh_entity_id, "write_unknown")


async def run_service_call(
    *,
    hass: HomeAssistant,
    coordinator: TadoDataUpdateCoordinator,
    zone_id: str,
    entity_type: str,
    api_coro: Coroutine,  # type: ignore[type-arg]
    capture_source: str | None,
    refresh_entity_id: str | None,
    reason: str,
    force_refresh: bool = False,
    stamp_write: bool = True,
    cancels_pending_write: bool = True,
    own_power_off: bool | None = None,
) -> bool:
    """Cloud write with timeout + capture-on-success + immediate refresh; bool return signals user-visible success."""
    from .api_client import raising_network_errors
    from .error_dispatch import dispatch_to_service_call
    from .exceptions import TadoAuthError, TadoBlockedError, TadoRateLimitError

    # A pending debounced write must not fire after an overlay write or delete; "" is a non-zone call.
    if zone_id and cancels_pending_write:
        cancel_zone_tap(hass, coordinator, zone_id)

    api_success = False
    try:
        # The same predicate that drops the tap: only a write that replaces the zone's overlay or hold takes the slot, outside the timeout.
        async with ordered_zone_write(coordinator, zone_id if cancels_pending_write else ""):
            with raising_network_errors():
                async with asyncio.timeout(10):
                    api_success = bool(await api_coro)
    except TimeoutError:
        _LOGGER.warning(
            "Services: %s for zone %s timed out after 10s",
            reason, zone_id,
        )
        await _verify_unknown_outcome(hass, coordinator, zone_id, refresh_entity_id, stamp_write)
        return False
    except (TadoAuthError, TadoRateLimitError, TadoBlockedError) as e:
        dispatch_to_service_call(e, coordinator.config_entry, hass, coordinator)
    except aiohttp.ClientError as e:
        _LOGGER.warning(
            "Services: %s for zone %s network error (%s)",
            reason, zone_id, mask_api_url(str(e) or type(e).__name__),
        )
        await _verify_unknown_outcome(hass, coordinator, zone_id, refresh_entity_id, stamp_write)
        return False
    finally:
        # A wait that is cancelled never awaited the coroutine; closing it keeps the loop from warning.
        if asyncio.iscoroutine(api_coro):
            api_coro.close()

    if not api_success:
        return False

    # Only a write that changes a target or power protects the zone from a stale bridge read.
    if stamp_write and zone_id and coordinator.state_reconciler is not None:
        coordinator.state_reconciler.record_local_write(zone_id)

    # Before the capture and the refresh, like the entity writers: a capture error must not skip the mark.
    if zone_id and own_power_off is not None:
        coordinator.record_own_power_write(zone_id, powered_off=own_power_off)

    if capture_source is not None:
        try:
            await coordinator.async_capture_state(zone_id, entity_type, capture_source)
        except (KeyError, AttributeError, OSError) as e:
            _LOGGER.warning(
                "Services: capture-on-success for zone %s failed (%s); "
                "user write succeeded, restoration may be unavailable",
                zone_id, e,
            )

    # The accepted write supersedes the card's earlier hold; released before the refresh so the refresh reads the card without it.
    if zone_id and cancels_pending_write:
        release_zone_hold(hass, coordinator, zone_id)

    if refresh_entity_id is not None:
        await async_trigger_immediate_refresh(hass, refresh_entity_id, reason, force=force_refresh)

    return True
