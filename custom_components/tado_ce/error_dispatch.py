"""Pattern B service-call dispatch: used by service-call wrappers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, NoReturn

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .exceptions import TadoAuthError, TadoRateLimitError
from .helpers import retry_after_to_minutes

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def is_typed_cloud_rejection(err: HomeAssistantError) -> bool:
    """Return whether `err` is the translated 429 or auth error `dispatch_to_service_call` raised."""
    return isinstance(err.__cause__, (TadoAuthError, TadoRateLimitError))


def _apply_cloud_error_recovery(
    exc: TadoAuthError | TadoRateLimitError,
    config_entry: ConfigEntry,
    hass: HomeAssistant,
    coordinator: Any,
) -> None:
    """Run the recovery side effect for a typed cloud error, no raise / no log.

    Single source of truth for both dispatchers: an auth error starts the
    reauth flow (per HA docs, `ConfigEntryAuthFailed` raised from a
    service-call path does NOT trigger reauth, so the explicit
    `async_start_reauth` is required); a rate-limit error records the
    coordinator back-off window (+ repair issue) so every path that hits the
    quota gets the same recovery the read path does. Both are idempotent.

    `coordinator` is typed `Any` to avoid a circular import: annotating it
    as TadoDataUpdateCoordinator would import coordinator.py, which imports
    this module.
    """
    if isinstance(exc, TadoAuthError):
        config_entry.async_start_reauth(hass)
    elif isinstance(exc, TadoRateLimitError):
        coordinator.record_cloud_backoff(exc.retry_after)


def handle_background_write_error(
    exc: TadoAuthError | TadoRateLimitError,
    config_entry: ConfigEntry,
    hass: HomeAssistant,
    coordinator: Any,
    log_message: str,
) -> None:
    """Non-raising dispatch for background-controller cloud-write errors.

    The non-raising sibling of `dispatch_to_service_call`. Both the Smart
    Valve and Offset Sync controllers run fire-and-forget off the
    coordinator's post-sync loop, so they cannot raise a HomeAssistantError
    to a caller, so they log and recover. `log_message` is the caller's
    already-formatted warning.
    """
    _LOGGER.warning("%s", log_message)
    _apply_cloud_error_recovery(exc, config_entry, hass, coordinator)
    # NB: param order (exc, config_entry, hass, coordinator) matches the two
    # sibling dispatchers above: keep all three aligned.


def dispatch_to_service_call(
    exc: TadoAuthError | TadoRateLimitError,
    config_entry: ConfigEntry,
    hass: HomeAssistant,
    coordinator: Any,
) -> NoReturn:
    """Pattern B canonical dispatch: runs cloud-error recovery, then raises.

    The raising sibling of `handle_background_write_error`: same recovery side
    effect (reauth / back-off via `_apply_cloud_error_recovery`), but surfaces
    a translated HomeAssistantError to the service caller afterwards.
    """
    _apply_cloud_error_recovery(exc, config_entry, hass, coordinator)

    if isinstance(exc, TadoAuthError):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="auth_required",
        ) from exc

    if isinstance(exc, TadoRateLimitError):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="rate_limited",
            translation_placeholders={
                "retry_after_minutes": retry_after_to_minutes(exc.retry_after),
            },
        ) from exc

    # Unreachable per type signature, but keep a safe fallback
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="cloud_write_failed",
    ) from exc
