"""Water-heater optimistic-write + rollback adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .climate_helpers import build_plan, run_cloud_write

if TYPE_CHECKING:
    from collections.abc import Coroutine


async def api_call_with_rollback_wh(
    entity: Any,
    api_coro: Coroutine,  # type: ignore[type-arg]
    *,
    operation: str | None,
    overlay_type: str | None,
    target_temp: float | None = None,
    reason: str,
    capture_source: str | None = None,
) -> bool:
    """Water-heater-flavoured shim around `run_cloud_write`."""
    optimistic: dict[str, Any] = {"_attr_current_operation": operation, "_overlay_type": overlay_type}
    if target_temp is not None:
        optimistic["_attr_native_target_temperature"] = target_temp

    plan = build_plan(
        entity,
        optimistic,
        expected={"operation": operation, "temperature": target_temp},
        preserved=None,
        refresh_signal="water_heater_change",
        reason=reason,
        capture_source=capture_source,
        capture_zone_id=getattr(entity, "_zone_id", None),
        capture_entity_type="water_heater",
        log_prefix="Water Heater",
    )
    return await run_cloud_write(entity, api_coro, plan)
