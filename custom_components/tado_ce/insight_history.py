"""Tado CE insight history tracker: duration-aware insight persistence + escalation."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .format_helpers import format_insight_type as _fmt_insight_type
from .format_helpers import format_priority as _fmt_priority
from .helpers import parse_iso_datetime
from .storage import async_migrate_json_to_store

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .insights_models import Insight

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = "1.0"
# Grace period: if an insight disappears for less than this, treat as same occurrence
REAPPEARANCE_GRACE_HOURS = 1
# Debounce delay for Store writes: coalesces per-poll `last_seen` updates to
# reduce SD flash wear on HA OS installs. HA's EVENT_HOMEASSISTANT_FINAL_WRITE
# flushes pending writes on shutdown, so no data loss.
SAVE_DELAY_SECONDS = 60


class InsightHistoryTracker:
    """Track insight appearance/disappearance for duration-aware messages and escalation."""

    def __init__(self, hass: HomeAssistant, home_id: str) -> None:
        self._hass = hass
        self._home_id = home_id
        self._store: Store[dict[str, Any]] = Store(
            hass,
            1,
            f"tado_ce/insight_history_{home_id}",
        )
        self._old_storage_path = Path(
            hass.config.path(
                f".storage/tado_ce/insight_history_{home_id}.json",
            ),
        )
        self._entries: dict[str, dict[str, Any]] = {}
        self._active_keys: set[str] = set()
        self._dirty = False

    @property
    def needs_save(self) -> bool:
        """Return True if there are unsaved changes."""
        return self._dirty

    @property
    def entries(self) -> dict[str, dict[str, Any]]:
        """Return current entries (read-only access for testing)."""
        return self._entries

    async def async_load(self) -> int:
        """Load history from Store, migrating the v3.x JSON file when present."""
        try:
            data: dict[str, Any] | list[Any] | None = await self._store.async_load()

            # First-run / fresh-install path: try the legacy
            # JSON before falling back to an empty history.
            if data is None:
                data = await async_migrate_json_to_store(
                    self._hass, self._old_storage_path, self._store,
                    label="insight_history",
                )

            if data is None:
                _LOGGER.debug(
                    "Insight History: no history file found, "
                    "starting fresh",
                )
                return 0
            if not isinstance(data, dict):
                _LOGGER.warning(
                    "Insight History: stored history has an "
                    "unexpected shape, discarding it and starting "
                    "fresh",
                )
                return 0
            self._entries = data.get("entries", {})
            _LOGGER.debug(
                "Insight History: loaded %d entr(ies) from Store",
                len(self._entries),
            )
            return len(self._entries)
        except (OSError, HomeAssistantError) as exc:
            _LOGGER.warning(
                "Insight History: history load failed (%s), "
                "starting fresh, will retry on next reload",
                exc,
            )
            self._entries = {}
            return 0

    def _serialize(self) -> dict[str, Any]:
        """Serialise entries for Store write. Called by async_delay_save at write time."""
        return {
            "version": STORAGE_VERSION,
            "saved_at": dt_util.utcnow().isoformat(),
            "entries": self._entries,
        }

    async def async_save(self) -> bool:
        """Schedule a debounced Store write when the history has unsaved changes."""
        if not self._dirty:
            return True

        try:
            self._store.async_delay_save(self._serialize, SAVE_DELAY_SECONDS)
            self._dirty = False
            _LOGGER.debug(
                "Insight History: queued debounced save: %d "
                "entr(ies)",
                len(self._entries),
            )
            return True
        except (OSError, HomeAssistantError):
            _LOGGER.warning(
                "Insight History: could not schedule debounced "
                "save, keeping the dirty flag, will retry on next "
                "save attempt",
                exc_info=True,
            )
            return False

    def update(self, current_insights: list[Insight], now: datetime) -> None:
        """Update history with current poll cycle's insights."""
        now_iso = now.isoformat()
        current_keys: set[str] = set()

        for insight in current_insights:
            key = self._make_key(insight.insight_type, insight.zone_name)
            current_keys.add(key)

            if key in self._entries:
                self._entries[key]["last_seen"] = now_iso
                self._dirty = True
            else:
                self._entries[key] = {
                    "first_seen": now_iso,
                    "last_seen": now_iso,
                    "base_priority": insight.priority.value
                    if hasattr(insight.priority, "value")
                    else int(insight.priority),
                    "occurrence_count": 1,
                }
                self._dirty = True

        grace_cutoff = now - timedelta(hours=REAPPEARANCE_GRACE_HOURS)
        keys_to_remove = []
        for key in self._entries:
            if key not in current_keys:
                last_seen_str = self._entries[key].get("last_seen", "")
                try:
                    last_seen = parse_iso_datetime(last_seen_str)
                    if last_seen <= grace_cutoff:
                        keys_to_remove.append(key)
                except (ValueError, TypeError):
                    keys_to_remove.append(key)

        for key in keys_to_remove:
            del self._entries[key]
            self._dirty = True

        self._active_keys = current_keys

    def get_duration(
        self,
        insight_type: str,
        zone_name: str | None = None,
    ) -> timedelta | None:
        """Get how long an insight has been active."""
        key = self._make_key(insight_type, zone_name)
        entry = self._entries.get(key)
        if not entry:
            return None

        try:
            first = parse_iso_datetime(entry["first_seen"])
            last = parse_iso_datetime(entry["last_seen"])
            return last - first
        except (ValueError, KeyError, TypeError):
            return None

    def get_persistent_insights(self, threshold_hours: int = 24) -> list[dict[str, Any]]:
        """Return insights currently active AND active for >= threshold_hours."""
        result = []
        threshold = timedelta(hours=threshold_hours)

        for key, entry in self._entries.items():
            # Skip entries not in current poll cycle (grace-period only)
            if key not in self._active_keys:
                continue
            try:
                first = parse_iso_datetime(entry["first_seen"])
                last = parse_iso_datetime(entry["last_seen"])
                duration = last - first
                if duration >= threshold:
                    parts = key.split(":", 1)
                    insight_type = parts[0]
                    zone_name = parts[1] if len(parts) > 1 else None
                    if zone_name == "_hub":
                        zone_name = None
                    priority_num = entry.get("base_priority", 0)
                    priority_names = {0: "none", 1: "low", 2: "medium", 3: "high", 4: "critical"}
                    result.append(
                        {
                            "insight_type": _fmt_insight_type(insight_type),
                            "zone_name": zone_name,
                            "duration_hours": round(duration.total_seconds() / 3600, 1),
                            "base_priority": _fmt_priority(
                                priority_names.get(priority_num, str(priority_num)),
                            ),
                        },
                    )
            except (ValueError, KeyError, TypeError):
                continue

        # Sort by duration descending: duration_hours is always float from round() above
        def _sort_key(item: dict[str, Any]) -> float:
            val = item.get("duration_hours", 0.0)
            return float(val) if isinstance(val, (int, float)) else 0.0

        result.sort(key=_sort_key, reverse=True)
        return result

    def prune_old_entries(self, max_age_days: int = 30) -> int:
        """Remove entries with last_seen older than max_age_days."""
        cutoff = dt_util.utcnow() - timedelta(days=max_age_days)
        keys_to_remove = []

        for key, entry in self._entries.items():
            try:
                last_seen = parse_iso_datetime(entry["last_seen"])
                if last_seen < cutoff:
                    keys_to_remove.append(key)
            except (ValueError, KeyError, TypeError):
                # Invalid entry: prune it
                keys_to_remove.append(key)

        for key in keys_to_remove:
            del self._entries[key]

        if keys_to_remove:
            self._dirty = True
            _LOGGER.debug(
                "Insight History: pruned %d stale entr(ies) past "
                "the retention window",
                len(keys_to_remove),
            )

        return len(keys_to_remove)

    @staticmethod
    def _make_key(insight_type: str, zone_name: str | None) -> str:
        """Create storage key from insight_type and zone_name."""
        return f"{insight_type}:{zone_name or '_hub'}"
