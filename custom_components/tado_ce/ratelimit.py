"""Tado CE rate-limit management: bootstrap reserve, manual-action gating, reset detection.

Three roles: clamp the polling-retry interval to safe bounds when
the cloud says "back off" (`_sanitize_retry_after`), gate manual
user actions when remaining quota would otherwise prevent
auto-recovery after the next reset
(`should_block_manual_action` / `async_check_bootstrap_reserve`),
and detect the actual reset time from the API-usage sensor's HA
history so the integration can self-correct without waiting for the
cloud's reset header (`async_detect_reset_from_history`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .const import (
    _RATE_LIMIT_DEFAULT_S,
    _RATE_LIMIT_MAX_S,
    _RATE_LIMIT_MIN_S,
    DOMAIN,
    QUOTA_BOOTSTRAP_CALLS,
)
from .helpers import parse_iso_datetime, predicted_next_reset

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .config_manager import ConfigurationManager
    from .coordinator import TadoDataUpdateCoordinator
    from .data_loader import DataLoader

_LOGGER = logging.getLogger(__name__)


def _sanitize_retry_after(value: float | None) -> int:
    """Clamp a retry-after value to the safe `[_RATE_LIMIT_MIN_S, _RATE_LIMIT_MAX_S]` range.

    `None` or non-positive values fall back to the default.
    """
    if value is None or value <= 0:
        return _RATE_LIMIT_DEFAULT_S
    return int(min(max(value, _RATE_LIMIT_MIN_S), _RATE_LIMIT_MAX_S))


def calculate_seconds_until_reset(last_reset_utc: str | None) -> int | None:
    """Return seconds until the next API reset based on the last reset timestamp.

    Adds 24 hours, then rolls forward until the next reset is in the
    future, which means stale snapshots (e.g. after a long
    integration outage) still produce a sensible deadline. Returns
    None when the input is missing or unparseable.
    """
    if not last_reset_utc:
        return None
    try:
        last_reset = parse_iso_datetime(last_reset_utc)
        next_reset = last_reset + timedelta(hours=24)
        now_utc = dt_util.utcnow()
        while next_reset <= now_utc:
            next_reset += timedelta(hours=24)
        result = int((next_reset - now_utc).total_seconds())
        return result if result > 0 else None
    except (ValueError, TypeError):
        return None


def should_block_manual_action(
    ratelimit_data: dict[str, Any], config_manager: ConfigurationManager,
) -> tuple[bool, str]:
    """Return (should_block, user_facing_reason): gates manual actions at the bootstrap reserve.

    The bootstrap reserve is the absolute minimum quota needed for
    the integration to auto-recover after the next API reset. When
    remaining quota dips to that floor, *every* action, including
    user-initiated ones, is blocked so the recovery sync still has
    calls to spend.
    """
    if not config_manager.get_quota_reserve_enabled():
        _LOGGER.debug(
            "Rate Limit: quota reserve protection disabled, letting "
            "manual actions through",
        )
        return False, ""

    last_reset_utc = ratelimit_data.get("last_reset_utc")
    if last_reset_utc:
        next_reset = predicted_next_reset(last_reset_utc)
        if next_reset is None:
            _LOGGER.debug(
                "Rate Limit: could not parse last_reset_utc (%r), "
                "treating reset time as unknown",
                last_reset_utc,
            )
        elif dt_util.utcnow() >= next_reset:
            return False, ""
    else:
        # No reset time known yet (fresh install / stale snapshot),
        # so let manual actions through so the first API response
        # bootstraps the rate-limit state.
        _LOGGER.debug(
            "Rate Limit: no reset time known yet, letting manual "
            "actions through to bootstrap rate-limit data",
        )
        return False, ""

    remaining = ratelimit_data.get("remaining", 100)

    _LOGGER.debug(
        "Rate Limit: bootstrap check, remaining=%s, threshold=%s",
        remaining, QUOTA_BOOTSTRAP_CALLS,
    )

    if remaining <= QUOTA_BOOTSTRAP_CALLS:
        _rs = ratelimit_data.get("reset_seconds")
        reset_seconds = _rs if _rs is not None else 0

        if not reset_seconds and last_reset_utc:
            reset_seconds = calculate_seconds_until_reset(last_reset_utc) or 0

        if reset_seconds > 0:
            hours = reset_seconds // 3600
            minutes = (reset_seconds % 3600) // 60
            reset_info = f"reset in {hours}h {minutes}m"
        else:
            reset_info = "reset time unknown, will auto-recover after the next successful API response"

        reason = (
            f"API limit reached ({remaining} calls remaining). "
            f"All actions blocked to preserve auto-recovery capability. "
            f"Use the Tado app for emergency changes. "
            f"Integration will auto-recover at {reset_info}."
        )
        return True, reason

    return False, ""


async def async_check_bootstrap_reserve(
    hass: HomeAssistant,
    coordinator: TadoDataUpdateCoordinator | None = None,
) -> tuple[bool, str]:
    """Service-handler convenience wrapper around `should_block_manual_action`.

    Loads the rate-limit snapshot from the coordinator's data loader
    (off the event loop), then defers the policy decision to
    `should_block_manual_action`. Returns False when the coordinator
    is missing: service handlers that bypass it have no quota
    context to gate on.
    """
    try:
        if coordinator is not None:
            config_manager = coordinator.config_manager
            ratelimit_data = await hass.async_add_executor_job(
                coordinator.data_loader.load_ratelimit_file,
            )
        else:
            _LOGGER.debug(
                "Rate Limit: bootstrap check called without coordinator, "
                "skipping",
            )
            return False, ""

        if not ratelimit_data:
            return False, ""

        return should_block_manual_action(ratelimit_data, config_manager)
    except (OSError, ValueError, KeyError, TypeError) as e:
        _LOGGER.debug(
            "Rate Limit: bootstrap check raised an exception (%s), "
            "letting the action through to be safe",
            e,
        )
        return False, ""


async def async_show_api_limit_notification(hass: HomeAssistant, message: str) -> None:
    """Surface a persistent notification when manual actions are blocked by quota."""
    await hass.services.async_call(
        "persistent_notification",
        "create",
        {
            "title": "Tado CE: API Limit Reached",
            "message": message + "\n\n**Tip:** Use the official Tado app for emergency temperature changes.",
            "notification_id": "tado_ce_api_limit",
        },
    )


async def async_check_bootstrap_reserve_or_raise(
    hass: HomeAssistant,
    entity_name: str = "",
    coordinator: TadoDataUpdateCoordinator = None,  # type: ignore[assignment]
) -> None:
    """Block a manual action with `HomeAssistantError` when quota is at the reserve.

    DRY entry point for entities and services. When triggered, also
    surfaces a persistent notification so the user knows why the
    action failed without having to read the log.
    """
    from homeassistant.exceptions import HomeAssistantError

    should_block, reason = await async_check_bootstrap_reserve(hass, coordinator=coordinator)
    if should_block:
        log_name = f" for {entity_name}" if entity_name else ""
        _LOGGER.warning(
            "Rate Limit: blocking manual action%s: %s",
            log_name, reason,
        )
        await async_show_api_limit_notification(hass, reason)
        raise HomeAssistantError(
            reason,
            translation_domain=DOMAIN,
            translation_key="api_quota_critically_low",
        )




def _find_reset_in_history(history: list[Any]) -> datetime | None:
    """Return the reset time inferred from a list of history states, or None.

    Scans for either an explicit reset (value dropped > 80% or below
    10 from a > 50 baseline) or the minimum-value timestamp when the
    minimum is below 20. Falls through to None on inputs too noisy
    to interpret.
    """
    min_value = float("inf")
    min_time = None
    prev_value = None

    for state in history:
        try:
            value = int(state.state)
            state_time = state.last_changed

            if prev_value is not None and prev_value > 50:
                if value < prev_value * 0.2 or value < 10:
                    _LOGGER.debug(
                        "Rate Limit: detected reset in history (%s → %s at %s)",
                        prev_value, value, state_time,
                    )
                    return state_time.replace(tzinfo=UTC) if state_time.tzinfo is None else state_time  # type: ignore[no-any-return]

            if value < min_value:
                min_value = value
                min_time = state_time

            prev_value = value

        except (ValueError, TypeError):
            continue

    if min_time and min_value < 20:
        _LOGGER.debug(
            "Rate Limit: no clear reset in history, using minimum-value "
            "timestamp as a fallback (min=%s at %s)",
            min_value, min_time,
        )
        return min_time.replace(tzinfo=UTC) if min_time.tzinfo is None else min_time  # type: ignore[no-any-return]

    _LOGGER.debug(
        "Rate Limit: history did not contain a usable reset point "
        "(minimum value seen was %s)",
        min_value,
    )
    return None


def _resolve_api_usage_entity_id(hass: HomeAssistant, home_id: str | None) -> str:
    """Return the API-usage sensor entity ID, preferring a registry lookup by unique id."""
    from homeassistant.helpers import entity_registry as er

    if home_id:
        target_uid = f"tado_ce_{home_id}_api_usage"
        registry = er.async_get(hass)
        entry = registry.async_get_entity_id("sensor", "tado_ce", target_uid)
        if entry:
            _LOGGER.debug(
                "Rate Limit: found API usage sensor via registry: %s "
                "(unique_id=%s)",
                entry, target_uid,
            )
            return entry
        _LOGGER.debug(
            "Rate Limit: registry has no entry for unique_id %s, "
            "falling back to default entity id",
            target_uid,
        )

    _LOGGER.debug("Rate Limit: using fallback entity id sensor.tado_ce_api_usage")
    return "sensor.tado_ce_api_usage"


async def async_detect_reset_from_history(hass: HomeAssistant, home_id: str | None = None) -> datetime | None:
    """Infer the most recent quota reset time from the HA recorder, or None.

    Queries the API-usage sensor's last 36 h of history and looks
    for the value-dropped-to-floor moment. More accurate than
    rate-based extrapolation when the recorder has data to work
    with.
    """
    try:
        from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
        from homeassistant.components.recorder.history import get_significant_states

        end_time = dt_util.utcnow()
        start_time = end_time - timedelta(hours=36)

        entity_id = _resolve_api_usage_entity_id(hass, home_id)

        def _get_history() -> dict[str, list[Any]]:
            result = get_significant_states(
                hass, start_time, end_time, [entity_id],
                significant_changes_only=False,
            )
            return dict(result) if result else {}

        states = await get_instance(hass).async_add_executor_job(_get_history)

        if not states or entity_id not in states:
            available_keys = list(states.keys()) if states else []
            _LOGGER.debug(
                "Rate Limit: no recorder history for %s "
                "(available keys: %s)",
                entity_id,
                available_keys[:5] if available_keys else "none",
            )
            return None

        history = states[entity_id]
        if len(history) < 10:
            _LOGGER.debug(
                "Rate Limit: %s only has %d history point(s), not "
                "enough for reset detection",
                entity_id, len(history),
            )
            return None

        return _find_reset_in_history(history)

    except ImportError:
        _LOGGER.debug(
            "Rate Limit: HA recorder component not available, "
            "skipping history-based reset detection",
        )
        return None
    except Exception as e:
        _LOGGER.debug(
            "Rate Limit: history-based reset detection failed (%s), "
            "falling back to extrapolation",
            e,
        )
        return None


def _recalculate_reset_fields(data: dict[str, Any], detected_reset: datetime) -> None:
    """Update `reset_seconds`, `reset_at`, `reset_human` in-place from the detected reset."""
    now_utc = dt_util.utcnow()
    next_reset = detected_reset + timedelta(hours=24)

    while next_reset <= now_utc:
        next_reset += timedelta(hours=24)

    seconds_until_reset = int((next_reset - now_utc).total_seconds())

    if seconds_until_reset > 0:
        hours = seconds_until_reset // 3600
        minutes = (seconds_until_reset % 3600) // 60
        data["reset_seconds"] = seconds_until_reset
        data["reset_at"] = next_reset.isoformat()
        data["reset_human"] = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"


async def async_update_ratelimit_reset_time(
    hass: HomeAssistant,
    detected_reset: datetime,
    home_id: str | None = None,
    data_loader: DataLoader | None = None,
) -> None:
    """Update the ratelimit Store with a reset time detected from HA history.

    Called after a successful sync when history-based detection beats
    the cloud's reset header. Idempotent: bails out when the new
    reset matches the cached one.
    """
    try:
        if data_loader is None:
            return

        cached = data_loader.get_cached("ratelimit")
        if not isinstance(cached, dict):
            return
        data = dict(cached)

        new_reset = detected_reset.strftime("%Y-%m-%dT%H:%M:%SZ")
        if data.get("last_reset_utc") == new_reset:
            return

        # History can hold yesterday's reset too; an older one would mark today's as already passed.
        try:
            is_not_newer = detected_reset <= parse_iso_datetime(data["last_reset_utc"])
        except (KeyError, ValueError, TypeError) as e:
            # Missing or unreadable recorded reset: replace it as before
            _LOGGER.debug("Rate Limit: recorded reset time not usable (%s), replacing it", e)
        else:
            if is_not_newer:
                _LOGGER.debug(
                    "Rate Limit: history reset %s UTC is not newer than the recorded one, keeping it",
                    detected_reset.strftime("%Y-%m-%d %H:%M"),
                )
                return

        data["last_reset_utc"] = new_reset
        _recalculate_reset_fields(data, detected_reset)

        await data_loader.async_update_store("ratelimit", data)

        _LOGGER.debug(
            "Rate Limit: updated quota reset time from HA history. "
            "%s UTC",
            detected_reset.strftime("%H:%M"),
        )

    except (OSError, ValueError) as e:
        _LOGGER.debug(
            "Rate Limit: could not update reset time (%s), keeping "
            "previously-cached value",
            e,
        )
