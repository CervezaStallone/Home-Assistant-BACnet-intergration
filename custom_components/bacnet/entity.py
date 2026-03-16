"""
Base entity for all BACnet platform entities.

Provides common functionality shared across sensor, switch, number, etc.:
  - Device info for the HA device registry
  - Coordinator-based state updates
  - Common properties (unique_id, name, available)
  - Helper for reading the current present value
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, OBJECT_TYPE_NAMES
from .coordinator import BACnetCoordinator

_LOGGER = logging.getLogger(__name__)


class BACnetEntity(CoordinatorEntity[BACnetCoordinator]):
    """Base class for BACnet entities.

    Each entity represents one BACnet object (e.g. Analog Input 1)
    on a specific BACnet device.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BACnetCoordinator,
        entry: ConfigEntry,
        obj: dict[str, Any],
    ) -> None:
        """Initialise the base entity.

        Args:
            coordinator: The BACnetCoordinator for this device.
            entry: The config entry this entity belongs to.
            obj: The BACnet object dict from the config entry data, containing
                 object_type, instance, object_name, description, units, etc.
        """
        super().__init__(coordinator)

        self._entry = entry
        self._obj = obj
        self._object_type: int = obj["object_type"]
        self._instance: int = obj["instance"]
        self._obj_key: str = f"{self._object_type}:{self._instance}"

        # Device info for HA device registry — groups all entities under one device
        device_id = entry.data.get("device_id", "unknown")
        device_name = entry.data.get("device_name", "BACnet Device")
        vendor_name = entry.data.get("vendor_name", "BACnet")
        model_name = entry.data.get("model_name", "")
        sw_version = entry.data.get("software_version", "")
        fw_version = entry.data.get("firmware_version", "")

        device_info = DeviceInfo(
            identifiers={(DOMAIN, str(device_id))},
            name=device_name,
            manufacturer=vendor_name,
        )
        if model_name:
            device_info["model"] = model_name
        else:
            device_info["model"] = f"BACnet Device {device_id}"
        if sw_version:
            device_info["sw_version"] = sw_version
        if fw_version:
            device_info["hw_version"] = fw_version

        self._attr_device_info = device_info

        # Unique ID: combination of config entry + object type + instance
        self._attr_unique_id = f"{entry.entry_id}_{self._object_type}_{self._instance}"

        # Entity name — respects the "use description" option
        self._attr_name = coordinator.get_entity_name(obj)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def obj_key(self) -> str:
        """Return the object key used in coordinator data (e.g. '0:1')."""
        return self._obj_key

    @property
    def object_type(self) -> int:
        """Return the BACnet object type integer."""
        return self._object_type

    @property
    def instance(self) -> int:
        """Return the BACnet object instance number."""
        return self._instance

    @property
    def bacnet_object(self) -> dict[str, Any]:
        """Return the full BACnet object configuration dict."""
        return self._obj

    @property
    def is_commandable(self) -> bool:
        """Return True if this object is commandable (has a Priority Array)."""
        return self._obj.get("commandable", False)

    @property
    def available(self) -> bool:
        """Return True if the coordinator has data for this object."""
        if self.coordinator.data is None:
            return False
        return self._obj_key in self.coordinator.data

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def get_present_value(self) -> Any:
        """Return the current presentValue from the coordinator data."""
        return self.coordinator.get_object_value(self._obj_key, "presentValue")

    def get_status_flags(self) -> list | None:
        """Return the current statusFlags from the coordinator data."""
        return self.coordinator.get_object_value(self._obj_key, "statusFlags")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes with BACnet-specific metadata."""
        type_name = OBJECT_TYPE_NAMES.get(
            self._object_type, f"Type {self._object_type}"
        )
        attrs: dict[str, Any] = {
            "bacnet_object_type": type_name,
            "bacnet_instance": self._instance,
            "bacnet_commandable": self.is_commandable,
        }
        if self._obj.get("units"):
            attrs["bacnet_units"] = self._obj["units"]
        if self._obj.get("description"):
            attrs["bacnet_description"] = self._obj["description"]

        status_flags = self.get_status_flags()
        if status_flags is not None:
            attrs["bacnet_status_flags"] = status_flags

        # Update method & COV details
        attrs["bacnet_update_method"] = self.coordinator.get_update_method(
            self._obj_key
        )
        cov_inc = self.coordinator.get_cov_increment_for(self._obj_key)
        if cov_inc is not None:
            attrs["bacnet_cov_increment"] = cov_inc

        return attrs
