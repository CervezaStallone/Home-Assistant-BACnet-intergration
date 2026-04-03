"""
Data update coordinator for BACnet IP integration.

Manages two update strategies per BACnet object:
  1. COV (Change of Value) — preferred, event-driven, low latency
  2. Polling fallback — used when COV is disabled, unsupported, or subscription fails

The coordinator also handles:
  - COV subscription lifecycle (subscribe, renew, unsubscribe)
  - Aggregating updates from both COV and polling into a single data dict
  - Triggering HA entity state updates via async_set_updated_data
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .bacnet_client import BACnetClient
from .const import (
    DEFAULT_COV_INCREMENT,
    DEFAULT_DOMAIN_MAP,
    DEFAULT_ENABLE_COV,
    DEFAULT_POLLING_INTERVAL,
    DEFAULT_USE_DESCRIPTION,
    DOMAIN,
    OBJECT_TYPE_ANALOG_INPUT,
    OBJECT_TYPE_ANALOG_OUTPUT,
    OBJECT_TYPE_ANALOG_VALUE,
    OBJECT_TYPE_BINARY_VALUE,
    OBJECT_TYPE_MULTI_STATE_VALUE,
)

_LOGGER = logging.getLogger(__name__)

# COV subscription lifetime.  BACpypes3's change_of_value() context manager
# automatically renews the subscription before it expires.
COV_LIFETIME_SECONDS = 300


class BACnetCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate BACnet data updates for one device.

    self.data is a dict keyed by "object_type:instance", each value being a dict
    of the latest known property values for that object. Example:

        {
            "0:1": {"presentValue": 23.5, "statusFlags": [0,0,0,0]},
            "4:3": {"presentValue": 1, "statusFlags": [0,0,0,0]},
        }
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: BACnetClient,
        objects: list[dict[str, Any]],
        enable_cov: bool = DEFAULT_ENABLE_COV,
        polling_interval: int = DEFAULT_POLLING_INTERVAL,
        use_description: bool = DEFAULT_USE_DESCRIPTION,
        domain_overrides: dict[str, str] | None = None,
        entry: ConfigEntry | None = None,
        cov_increment: float = DEFAULT_COV_INCREMENT,
    ) -> None:
        """Initialise the coordinator.

        Args:
            hass: Home Assistant instance.
            client: Connected BACnetClient.
            objects: List of selected BACnet object dicts from config entry.
            enable_cov: Whether COV subscriptions should be attempted.
            polling_interval: Fallback polling interval in seconds.
            use_description: If True, use description (prop 28) for entity names.
            domain_overrides: Per-object HA domain overrides from options flow.
            entry: The ConfigEntry for accessing device addressing info.
            cov_increment: COV increment for analog objects (0.0 = device default).
        """
        self.client = client
        self.objects = objects
        self.enable_cov = enable_cov
        self.polling_interval = polling_interval
        self.use_description = use_description
        self.domain_overrides = domain_overrides or {}
        self.entry = entry
        self.cov_increment = cov_increment

        # Track which objects have active COV and which need polling
        self._cov_subscriptions: dict[str, str] = {}  # obj_key → sub_key
        self._polled_objects: list[dict[str, Any]] = []

        # Device address for reads/writes (from config entry data)
        self.device_address: str = ""
        if entry is not None:
            self.device_address = entry.data.get("device_address", "")

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id if entry else 'unknown'}",
            update_interval=timedelta(seconds=polling_interval),
        )

    # ------------------------------------------------------------------
    # First refresh — sets up COV subscriptions
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch latest data for all objects.

        On the first call this also sets up COV subscriptions and does an
        initial poll of ALL objects so that entities have state immediately.

        **Every** subsequent call polls ALL objects too, regardless of COV
        status.  COV provides faster intermediate updates between polls,
        but polling is the reliable baseline that guarantees values are
        always refreshed — even when a device accepts a COV subscription
        but never actually sends notifications.

        Returns:
            Dict keyed by "object_type:instance" → {property: value}.
        """
        # Use existing data as base (COV may have already pushed updates)
        data: dict[str, Any] = dict(self.data) if self.data else {}

        # --- First run: set up COV subscriptions ---
        first_run = not self._cov_subscriptions and not self._polled_objects
        if first_run:
            await self._setup_subscriptions()

        # Always poll ALL objects — COV is supplementary, polling is the
        # reliable baseline.  This ensures values update even when COV
        # subscriptions are accepted but notifications never arrive.
        for obj in self.objects:
            obj_key = f"{obj['object_type']}:{obj['instance']}"
            try:
                values = await self.client.read_multiple_properties(
                    device_address=self.device_address,
                    object_type=obj["object_type"],
                    instance=obj["instance"],
                    property_names=["presentValue", "statusFlags"],
                )
                data[obj_key] = values
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "Polling failed for %s: %s",
                    obj_key,
                    exc,
                )
                # Keep stale data rather than failing everything
                if obj_key not in data:
                    data[obj_key] = {"presentValue": None, "statusFlags": None}

        return data

    # ------------------------------------------------------------------
    # COV subscription management
    # ------------------------------------------------------------------

    # Analog object types that support covIncrement
    _ANALOG_TYPES = {
        OBJECT_TYPE_ANALOG_INPUT,
        OBJECT_TYPE_ANALOG_OUTPUT,
        OBJECT_TYPE_ANALOG_VALUE,
    }

    async def _setup_subscriptions(self) -> None:
        """Attempt COV subscriptions for all objects. Objects that fail get polled."""
        self._polled_objects = []

        for obj in self.objects:
            obj_key = f"{obj['object_type']}:{obj['instance']}"

            if self.enable_cov:
                # For analog objects, write the covIncrement to the device
                # before subscribing so the device uses the user's threshold.
                if self.cov_increment > 0 and obj["object_type"] in self._ANALOG_TYPES:
                    try:
                        await self.client.write_property(
                            device_address=self.device_address,
                            object_type=obj["object_type"],
                            instance=obj["instance"],
                            property_name="covIncrement",
                            value=self.cov_increment,
                        )
                        _LOGGER.debug(
                            "Set covIncrement=%.2f for %s",
                            self.cov_increment,
                            obj_key,
                        )
                    except Exception:  # noqa: BLE001
                        _LOGGER.debug(
                            "Could not write covIncrement for %s (device may "
                            "not support it — using device default)",
                            obj_key,
                        )

                sub_key = await self.client.subscribe_cov(
                    device_address=self.device_address,
                    object_type=obj["object_type"],
                    instance=obj["instance"],
                    callback=self._handle_cov_notification,
                    lifetime=COV_LIFETIME_SECONDS,
                )
                if sub_key is not None:
                    self._cov_subscriptions[obj_key] = sub_key
                    _LOGGER.debug("COV active for %s", obj_key)
                    continue

            # COV disabled or failed — add to polling list
            self._polled_objects.append(obj)
            _LOGGER.debug("Polling fallback for %s", obj_key)

        _LOGGER.info(
            "COV subscriptions: %d active, %d polling fallback",
            len(self._cov_subscriptions),
            len(self._polled_objects),
        )

        # BACpypes3 change_of_value() context manager handles renewal
        # automatically — no background renewal task needed.

    @callback
    def _handle_cov_notification(
        self, obj_key: str, changed_values: dict[str, Any]
    ) -> None:
        """Process an incoming COV notification and push update to entities.

        Called by the BACnetClient COV reader task whenever a property
        change is received.  We merge the changed properties into our
        data dict and tell HA to update affected entities.

        IMPORTANT: We update self.data directly and notify listeners
        instead of using async_set_updated_data(), because the latter
        resets the polling timer.  If COV notifications arrive frequently,
        that would prevent the scheduled _async_update_data poll from
        ever firing.

        Args:
            obj_key: Object identifier string ("object_type:instance").
            changed_values: Dict of changed property names → new values,
                            e.g. {"presentValue": 23.5}.
        """
        if self.data is None:
            return

        data = dict(self.data)
        if obj_key in data:
            data[obj_key].update(changed_values)
        else:
            data[obj_key] = changed_values

        # Update data and notify listeners WITHOUT resetting the poll timer.
        self.data = data
        self.async_update_listeners()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def async_shutdown(self) -> None:
        """Cancel all COV subscriptions and clean up."""
        await self.client.unsubscribe_all_cov()
        self._cov_subscriptions.clear()
        self._polled_objects.clear()

        _LOGGER.debug("Coordinator shutdown complete")

    # ------------------------------------------------------------------
    # Helpers for entity access
    # ------------------------------------------------------------------

    def get_object_value(self, obj_key: str, prop: str = "presentValue") -> Any:
        """Get the latest value for a specific object and property."""
        if self.data is None:
            return None
        obj_data = self.data.get(obj_key, {})
        return obj_data.get(prop)

    # Value object types that should use a writable domain when commandable
    _COMMANDABLE_VALUE_DOMAIN: dict[int, str] = {
        OBJECT_TYPE_ANALOG_VALUE: "number",
        OBJECT_TYPE_BINARY_VALUE: "switch",
        OBJECT_TYPE_MULTI_STATE_VALUE: "number",
    }

    def get_domain_for_object(self, obj: dict[str, Any]) -> str:
        """Determine the HA domain for a BACnet object, respecting user overrides."""
        obj_key = f"{obj['object_type']}:{obj['instance']}"
        override = self.domain_overrides.get(obj_key)
        if override:
            return override
        # Commandable Value objects should use a writable domain
        if obj.get("commandable") and obj["object_type"] in self._COMMANDABLE_VALUE_DOMAIN:
            return self._COMMANDABLE_VALUE_DOMAIN[obj["object_type"]]
        return DEFAULT_DOMAIN_MAP.get(obj["object_type"], "sensor")

    def get_entity_name(self, obj: dict[str, Any]) -> str:
        """Return the entity display name, respecting the use_description option."""
        if self.use_description and obj.get("description"):
            return obj["description"]
        return obj.get("object_name", f"BACnet {obj['object_type']}:{obj['instance']}")

    def is_cov_subscribed(self, obj_key: str) -> bool:
        """Return True if this object has an active COV subscription."""
        return obj_key in self._cov_subscriptions

    def get_update_method(self, obj_key: str) -> str:
        """Return 'COV' or 'polling' for how this object is updated."""
        return "COV" if self.is_cov_subscribed(obj_key) else "polling"

    def get_cov_increment_for(self, obj_key: str) -> float | None:
        """Return the configured COV increment for analog objects, None for binary."""
        if not self.is_cov_subscribed(obj_key):
            return None
        parts = obj_key.split(":")
        if len(parts) == 2:
            obj_type = int(parts[0])
            if obj_type in self._ANALOG_TYPES:
                return self.cov_increment if self.cov_increment > 0 else None
        return None
