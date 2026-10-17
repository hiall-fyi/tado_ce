"""Water-heater optimistic-write + rollback adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .climate_helpers import RollbackPlan, _attempt_with_rollback

if TYPE_CHECKING:
    from collections.abc import Coroutine


async def api_call_with_rollback_wh(
    entity: Any,
    api_coro: Coroutine,  # type: ignore[type-arg]
    *,
    operation: str | None,
    target_temp: float | None = None,
    reason: str,
    capture_source: str | None = None,
) -> bool:
    """Water-heater-flavoured shim around `_attempt_with_rollback`."""
    optimistic: dict[str, Any] = {"_attr_current_operation": operation}
    if target_temp is not None:
        optimistic["_attr_target_temperature"] = target_temp

    plan = RollbackPlan(
        optimistic=optimistic,
        rollback={
            "_attr_current_operation": entity._attr_current_operation,
            "_attr_target_temperature": entity._attr_target_temperature,
        },
        expected={"operation": operation, "temperature": target_temp},
        preserved_attrs=None,
        refresh_signal="water_heater_change",
        reason=reason,
        capture_source=capture_source,
        capture_zone_id=getattr(entity, "_zone_id", None),
        capture_entity_type="water_heater",
        log_prefix="Water Heater",
    )
    return await _attempt_with_rollback(entity, api_coro, plan)
