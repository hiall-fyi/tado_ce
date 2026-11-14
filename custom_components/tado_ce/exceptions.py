"""Tado CE custom exceptions: auth errors, sync errors, API failures.

- TadoAuthError → ConfigEntryAuthFailed (triggers HA reauth flow)
- TadoSyncError → UpdateFailed (coordinator retries on next poll)
- TadoRateLimitError → "rate_limited" error in config flow UI
"""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError


class TadoAuthError(HomeAssistantError):
    """Raised when authentication fails (e.g., invalid_grant, expired refresh token).

    The coordinator catches this and raises ConfigEntryAuthFailed,
    which triggers HA's reauth flow prompting the user to re-authenticate.
    """


class TadoSyncError(HomeAssistantError):
    """Raised when sync fails due to network/server errors (not auth-related).

    The coordinator catches this and raises UpdateFailed,
    which marks the coordinator as failed and retries on next poll.
    """


class TadoBridgeApiError(HomeAssistantError):
    """Raised when a Bridge API call fails (network, HTTP, or parse error).

    Bridge API errors are isolated from the main cloud API: they never
    trigger OAuth reauth or affect coordinator cloud data.
    """


class TadoRateLimitError(HomeAssistantError):
    """Raised when the Tado API returns HTTP 429 and retries are exhausted."""

    def __init__(self, *args: object, retry_after: int = 0) -> None:
        """Store retry_after seconds for the coordinator's UpdateFailed dispatch."""
        super().__init__(*args)
        self.retry_after = retry_after


class TadoBlockedError(HomeAssistantError):
    """Tado's CDN keeps answering 403 after the retries, so the credentials are not at fault."""
