"""Tado CE repair-issue helpers: surface auth / quota problems via HA's UI."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import issue_registry as ir

from .cloud_health import reset_blocked_record
from .const import DOMAIN
from .helpers import offline_since_date, retry_after_to_minutes
from .insights_device import calculate_battery_recommendation

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

# WARNING + non-persistent: the connection is blocked, not the login, and the next blocked attempt re-creates it.
BLOCKED_ISSUE = RepairIssueSpec(
    translation_key="cloud_blocked",
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

# ERROR + persistent: setup aborted here and nothing re-creates the issue if
# dismissed, so it must survive a restart.
UNSUPPORTED_UPGRADE_PATH_ISSUE = RepairIssueSpec(
    translation_key="unsupported_upgrade_path",
    severity=ir.IssueSeverity.ERROR,
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

# WARNING + non-persistent: the Smart Valve controller re-raises it while the sensor is missing.
SVC_SENSOR_MISSING_ISSUE = RepairIssueSpec(
    translation_key="svc_sensor_missing",
    severity=ir.IssueSeverity.WARNING,
    is_persistent=False,
)

# WARNING + non-persistent: re-raised from zones_info on every refresh while a device battery is LOW or CRITICAL.
TRV_BATTERY_LOW_ISSUE = RepairIssueSpec(
    translation_key="battery_low",
    severity=ir.IssueSeverity.WARNING,
    is_persistent=False,
)


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
    zone_id: str | None = None,
) -> None:
    """Dismiss the repair issue if present (idempotent)."""
    issue_id = _issue_id(spec, home_id, zone_id)
    ir.async_delete_issue(hass, DOMAIN, issue_id)
    _LOGGER.debug("Repairs: dismissed repair issue %s", issue_id)


def async_issue_exists(
    hass: HomeAssistant,
    spec: RepairIssueSpec,
    *,
    home_id: str | None = None,
    zone_id: str | None = None,
) -> bool:
    """True when the repair issue is in the registry right now."""
    return (DOMAIN, _issue_id(spec, home_id, zone_id)) in ir.async_get(hass).issues


def _dismiss_matching(hass: HomeAssistant, is_match: Callable[[str], bool]) -> int:
    """Delete every tado_ce issue whose id satisfies is_match (keys listed first)."""
    ids = [
        issue_id
        for domain, issue_id in list(ir.async_get(hass).issues)
        if domain == DOMAIN and is_match(issue_id)
    ]
    for issue_id in ids:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    return len(ids)


def async_dismiss_zone_issues(hass: HomeAssistant, home_id: str | None) -> int:
    """Dismiss every zone-scoped repair of one home when its entry is removed."""
    if not home_id:
        return 0
    prefixes = (
        f"{ZONE_REASSIGNED_ISSUE.effective_id_prefix}_{home_id}_",
        f"{SVC_SENSOR_MISSING_ISSUE.effective_id_prefix}_{home_id}_",
        f"{TRV_BATTERY_LOW_ISSUE.effective_id_prefix}_{home_id}_",
    )
    return _dismiss_matching(hass, lambda issue_id: issue_id.startswith(prefixes))


def async_dismiss_legacy_sensor_missing_issues(hass: HomeAssistant) -> int:
    """Dismiss pre-upgrade home-less svc_sensor_missing_<zone> issues (ids carry no further underscore)."""
    prefix = f"{SVC_SENSOR_MISSING_ISSUE.effective_id_prefix}_"
    return _dismiss_matching(
        hass,
        lambda issue_id: issue_id.startswith(prefix) and "_" not in issue_id[len(prefix):],
    )


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


def async_create_blocked_issue(hass: HomeAssistant, home_id: str | None = None) -> None:
    """Surface a 'Tado is blocking requests' repair; no re-login applies."""
    async_create_issue(hass, BLOCKED_ISSUE, home_id=home_id)


def async_dismiss_blocked_issue(hass: HomeAssistant, home_id: str | None = None) -> None:
    """Clear the blocked repair after a successful call."""
    async_dismiss_issue(hass, BLOCKED_ISSUE, home_id=home_id)


def async_dismiss_cloud_health_issues(hass: HomeAssistant, home_id: str | None = None) -> None:
    """Clear the repairs CloudHealth owns once the cloud answers again."""
    async_dismiss_auth_issue(hass, home_id)
    async_dismiss_rate_limit_issue(hass, home_id)
    async_dismiss_blocked_issue(hass, home_id)


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


def async_create_battery_low_issue(
    hass: HomeAssistant, home_id: str | None, zone_id: str, zone_name: str, devices_text: str, advice: str,
) -> None:
    """Surface a repair naming the zone whose device battery is LOW or CRITICAL."""
    async_create_issue(
        hass,
        TRV_BATTERY_LOW_ISSUE,
        home_id=home_id,
        zone_id=zone_id,
        placeholders={"zone_name": zone_name, "devices": devices_text, "advice": advice},
    )


def async_dismiss_battery_low_issue(hass: HomeAssistant, home_id: str | None, zone_id: str) -> None:
    """Clear the low-battery repair of one zone."""
    async_dismiss_issue(hass, TRV_BATTERY_LOW_ISSUE, home_id=home_id, zone_id=zone_id)


def _sync_zone_battery_repair(hass: HomeAssistant, home_id: str | None, zone: Any) -> None:
    """Raise or clear the low-battery repair of one zone; an empty device list changes nothing."""
    # First two letters of the Tado deviceType (VA radiator valve, RU wall thermostat, SU smart thermostat).
    device_names = {"VA": "radiator valve", "RU": "wall thermostat", "SU": "smart thermostat"}
    devices = [d for d in (zone.get("devices") or []) if isinstance(d, dict)] if isinstance(zone, dict) else []
    if not devices or zone.get("id") is None:
        return
    zone_id = str(zone["id"])
    zone_name = str(zone.get("name") or f"Zone {zone_id}")
    low = [d for d in devices if str(d.get("batteryState") or "").upper() in ("LOW", "CRITICAL")]
    existed = async_issue_exists(hass, TRV_BATTERY_LOW_ISSUE, home_id=home_id, zone_id=zone_id)
    if not low:
        # A missing or unknown reading is not a normal battery, so only an explicit NORMAL clears the notice.
        if existed and any(str(d.get("batteryState") or "").upper() == "NORMAL" for d in devices):
            async_dismiss_battery_low_issue(hass, home_id, zone_id)
            _LOGGER.info("Repairs: battery repair cleared for zone %s", zone_id)
        return
    names = ", ".join(sorted({device_names.get(str(d.get("deviceType") or "")[:2].upper(), "Tado device") for d in low}))
    advice = " ".join(dict.fromkeys(
        calculate_battery_recommendation(
            str(d["batteryState"]), zone_name, d.get("deviceType"), offline_since_date(d.get("connectionState")),
        ) for d in low
    ))
    async_create_battery_low_issue(hass, home_id, zone_id, zone_name, names, advice)
    if not existed:
        _LOGGER.info("Repairs: battery repair raised for zone %s (%s)", zone_id, names)


def async_sync_battery_repairs(hass: HomeAssistant, home_id: str | None, zones_info: object) -> None:
    """Raise or clear one low-battery repair per zone; a missing list changes nothing and one failing zone skips only itself."""
    if not isinstance(zones_info, list):
        return
    for zone in zones_info:
        try:
            _sync_zone_battery_repair(hass, home_id, zone)
        except Exception:  # noqa: BLE001  # the Repair is optional, so no registry or payload failure may abort a poll
            _LOGGER.warning(
                "Repairs: battery repair failed for zone %s", zone.get("id") if isinstance(zone, dict) else "?", exc_info=True,
            )


def repair_home_id(home_id: str | None) -> str:
    """Return the home id repairs are filed under: the coordinator uses "default" for an entry with no home id."""
    return home_id or "default"


def async_dismiss_home_issues(hass: HomeAssistant, home_id: str | None) -> None:
    """Dismiss every repair filed for one home, when its entry is removed or moved to another home."""
    resolved = repair_home_id(home_id)
    async_dismiss_homekit_pairing_invalid_issue(hass, resolved)
    async_dismiss_cloud_health_issues(hass, resolved)
    reset_blocked_record(resolved)
    # The migration files this one under the raw home id.
    async_dismiss_issue(hass, UNSUPPORTED_UPGRADE_PATH_ISSUE, home_id=home_id)
    async_dismiss_zone_issues(hass, resolved)
