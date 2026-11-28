"""Pattern B service-call dispatch: used by service-call wrappers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NoReturn

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .exceptions import TadoAuthError, TadoBlockedError, TadoRateLimitError
from .helpers import retry_after_to_minutes

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def is_typed_cloud_rejection(err: HomeAssistantError) -> bool:
    """Return whether `err` is the translated 429, auth or blocked error `dispatch_to_service_call` raised."""
    return isinstance(err.__cause__, (TadoAuthError, TadoRateLimitError, TadoBlockedError))


def _apply_cloud_error_recovery(
    exc: TadoAuthError | TadoRateLimitError | TadoBlockedError,
    config_entry: ConfigEntry,
    hass: HomeAssistant,
) -> None:
    """Start reauth for an auth error; rate-limit and blocked errors need nothing here because the API client already recorded them."""
    if isinstance(exc, TadoAuthError):
        config_entry.async_start_reauth(hass)


def handle_background_write_error(
    exc: TadoAuthError | TadoRateLimitError | TadoBlockedError,
    config_entry: ConfigEntry,
    hass: HomeAssistant,
    log_message: str,
) -> None:
    """Log a typed cloud-write error and run its recovery without raising, for fire-and-forget callers."""
    _LOGGER.warning("%s", log_message)
    _apply_cloud_error_recovery(exc, config_entry, hass)


def dispatch_to_service_call(
    exc: TadoAuthError | TadoRateLimitError | TadoBlockedError,
    config_entry: ConfigEntry,
    hass: HomeAssistant,
) -> NoReturn:
    """Run the recovery for a typed cloud error, then raise the translated HomeAssistantError to the caller."""
    _apply_cloud_error_recovery(exc, config_entry, hass)

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

    if isinstance(exc, TadoBlockedError):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cloud_blocked",
        ) from exc

    # Unreachable per type signature, but keep a safe fallback
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="cloud_write_failed",
    ) from exc
