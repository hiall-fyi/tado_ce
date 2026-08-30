"""Tado CE repair-issue helpers: surface auth / quota problems via HA's UI."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .helpers import retry_after_to_minutes

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepairIssueSpec:
    """Per-issue parameters that drive async_create_issue / async_dismiss_issue."""

    translation_key: str
    severity: ir.IssueSeverity
    is_persistent: bool
    id_prefix: str | None = None

    @property
    def effective_id_prefix(self) -> str:
        """Default the registry-key prefix to translation_key when unspecified."""
        return self.id_prefix or self.translation_key


# ERROR + persistent: auth dead is only detectable when it occurs and the user
# must act, so the issue must survive a restart.
AUTH_ISSUE = RepairIssueSpec(
    translation_key="auth_token_expired",
    severity=ir.IssueSeverity.ERROR,
    is_persistent=True,
)

# WARNING + non-persistent: quota auto-recovers, so the issue is re-created
# only if the next sync still rate-limits.
RATE_LIMIT_ISSUE = RepairIssueSpec(
    translation_key="rate_limited",
    severity=ir.IssueSeverity.WARNING,
    is_persistent=False,
)

# WARNING + persistent: pairing credentials are invalid and the bridge can't be
# reached until the user re-pairs, so the issue survives a restart and the user
# still sees it after an HA reboot before they act.
HOMEKIT_PAIRING_INVALID_ISSUE = RepairIssueSpec(
    id_prefix="homekit_pairing_invalid",
    translation_key="homekit_pairing_invalid",
    severity=ir.IssueSeverity.WARNING,
    is_persistent=True,
)

# WARNING + persistent: the prune that triggers this issue is one-time and
# won't recreate it on a later poll, so it must survive a restart or the
# notice is lost for good before the user ever sees it.
ZONE_REASSIGNED_ISSUE = RepairIssueSpec(
    translation_key="zone_reassigned",
    severity=ir.IssueSeverity.WARNING,
    is_persistent=True,
)

# Backwards-compat constant, scheduled for removal in v5.0.0.
ISSUE_AUTH_EXPIRED = AUTH_ISSUE.effective_id_prefix


def _issue_id(spec: RepairIssueSpec, home_id: str | None, zone_id: str | None = None) -> str:
    """Build the registry-key as <prefix>[_<home_id>][_<zone_id>]."""
    parts = [spec.effective_id_prefix]
    if home_id:
        parts.append(home_id)
    if zone_id:
        parts.append(zone_id)
    return "_".join(parts)


def async_create_issue(
    hass: HomeAssistant,
    spec: RepairIssueSpec,
    *,
    home_id: str | None = None,
    zone_id: str | None = None,
    placeholders: dict[str, str] | None = None,
) -> None:
    """Create an HA repair issue per spec."""
    issue_id = _issue_id(spec, home_id, zone_id)
    full_placeholders = {"home_id": home_id or "default"}
    if placeholders:
        full_placeholders.update(placeholders)
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        is_persistent=spec.is_persistent,
        severity=spec.severity,
        translation_key=spec.translation_key,
        translation_placeholders=full_placeholders,
    )
    _LOGGER.debug("Repairs: created repair issue %s", issue_id)


def async_dismiss_issue(
    hass: HomeAssistant,
    spec: RepairIssueSpec,
    *,
    home_id: str | None = None,
) -> None:
    """Dismiss the repair issue if present (idempotent)."""
    issue_id = _issue_id(spec, home_id)
    ir.async_delete_issue(hass, DOMAIN, issue_id)
    _LOGGER.debug("Repairs: dismissed repair issue %s", issue_id)


def async_create_auth_issue(hass: HomeAssistant, home_id: str | None = None) -> None:
    """Surface an 'auth expired' repair so the user re-authenticates."""
    async_create_issue(hass, AUTH_ISSUE, home_id=home_id)


def async_dismiss_auth_issue(hass: HomeAssistant, home_id: str | None = None) -> None:
    """Clear the auth-expired repair after a successful re-auth."""
    async_dismiss_issue(hass, AUTH_ISSUE, home_id=home_id)


def async_create_rate_limit_issue(
    hass: HomeAssistant, home_id: str | None, retry_after: int,
) -> None:
    """Surface a 'rate-limited' repair while the API quota window holds."""
    async_create_issue(
        hass,
        RATE_LIMIT_ISSUE,
        home_id=home_id,
        placeholders={"retry_after_minutes": retry_after_to_minutes(retry_after)},
    )


def async_dismiss_rate_limit_issue(
    hass: HomeAssistant, home_id: str | None = None,
) -> None:
    """Clear the rate-limited repair after a successful sync."""
    async_dismiss_issue(hass, RATE_LIMIT_ISSUE, home_id=home_id)


def async_create_homekit_pairing_invalid_issue(
    hass: HomeAssistant, home_id: str | None = None,
) -> None:
    """Surface a 'HomeKit pairing invalid' repair so the user re-pairs."""
    async_create_issue(hass, HOMEKIT_PAIRING_INVALID_ISSUE, home_id=home_id)


def async_dismiss_homekit_pairing_invalid_issue(
    hass: HomeAssistant, home_id: str | None = None,
) -> None:
    """Clear the HomeKit pairing invalid repair after a successful re-pair."""
    async_dismiss_issue(hass, HOMEKIT_PAIRING_INVALID_ISSUE, home_id=home_id)


def async_create_zone_reassigned_issue(hass: HomeAssistant, home_id: str | None, zone_id: str) -> None:
    """Surface a repair issue after Tado reuses a zone id and its settings were cleared."""
    async_create_issue(
        hass,
        ZONE_REASSIGNED_ISSUE,
        home_id=home_id,
        zone_id=zone_id,
        placeholders={"zone_id": zone_id},
    )
