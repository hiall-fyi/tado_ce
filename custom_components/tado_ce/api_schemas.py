"""Shape checks for Tado cloud responses, applied before a response is stored or used."""

from __future__ import annotations

from typing import Any, NamedTuple

import probatio as vol


class ResponseSchema(NamedTuple):
    """A named shape check; the label appears in the log, never the payload."""

    label: str
    check: Any


def _object(**required: Any) -> vol.Schema:
    """A dict that must carry the given keys; every other key is allowed."""
    return vol.Schema(
        {vol.Required(key): kind for key, kind in required.items()},
        extra=vol.ALLOW_EXTRA,
    )


_ANY_OBJECT = vol.Schema(dict)
_LIST_OF_OBJECTS = vol.Schema([_ANY_OBJECT])
_ID = vol.Any(int, str)

ZONE_STATES = ResponseSchema("zoneStates", _object(zoneStates=vol.Any(dict, None)))
ZONES_INFO = ResponseSchema("zones", vol.Schema([_object(id=_ID)]))
WEATHER = ResponseSchema("weather", _ANY_OBJECT)
HOME_STATE = ResponseSchema("state", _object(presence=str))
MOBILE_DEVICES = ResponseSchema("mobileDevices", _LIST_OF_OBJECTS)
HOME_DEVICES = ResponseSchema("devices", _LIST_OF_OBJECTS)
DEVICE_OFFSET = ResponseSchema(
    "temperatureOffset",
    vol.Schema({vol.Optional("celsius"): vol.Any(int, float, None)}, extra=vol.ALLOW_EXTRA),
)
ZONE_CAPABILITIES = ResponseSchema("capabilities", _ANY_OBJECT)
HEATING_CIRCUITS = ResponseSchema("heatingCircuits", _LIST_OF_OBJECTS)
ZONE_CONTROL = ResponseSchema("zoneControl", _ANY_OBJECT)
ACTIVE_TIMETABLE = ResponseSchema("activeTimetable", _object(id=_ID))
TIMETABLE_BLOCKS = ResponseSchema("timetableBlocks", _LIST_OF_OBJECTS)


def describe_invalid(err: vol.Invalid) -> str:
    """Say what was wrong and where, without echoing any value from the response."""
    where = "/".join(str(part) for part in err.path) or "top level"
    return f"{err.error_message} at {where}"
