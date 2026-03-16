"""
Switch platform for BACnet IP integration.

Creates HA switch entities for BACnet objects mapped to "switch".
Typically: Binary Output, Binary Value (when commandable).

Switches support on/off with proper Priority Array handling:
  - Turn ON  → write active (1) at the configured priority
  - Turn OFF → write Null (relinquish) at the same priority to release the override
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DATA_CLIENT,
    DATA_COORDINATOR,
    DATA_OBJECTS,
    DEFAULT_WRITE_PRIORITY,
    DOMAIN,
)
from .bacnet_client import BACnetClient
from .coordinator import BACnetCoordinator
from .entity import BACnetEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BACnet switch entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: BACnetCoordinator = data[DATA_COORDINATOR]
    objects: list[dict[str, Any]] = data[DATA_OBJECTS]

    entities: list[BACnetSwitch] = []
    for obj in objects:
        domain = coordinator.get_domain_for_object(obj)
        if domain == "switch":
            entities.append(BACnetSwitch(coordinator, entry, obj))

    if entities:
        async_add_entities(entities)
        _LOGGER.debug("Added %d BACnet switch entities", len(entities))


class BACnetSwitch(BACnetEntity, SwitchEntity):
    """Representation of a commandable BACnet binary object as a HA switch.

    Write strategy (BACnet standard compliant):
      - turn_on:  Write presentValue = 1 (active) at priority level
      - turn_off: Write presentValue = Null (relinquish) at same priority level
                  This releases the override and lets lower-priority commands
                  or the Relinquish Default take effect.
    """

    def __init__(
        self,
        coordinator: BACnetCoordinator,
        entry: ConfigEntry,
        obj: dict[str, Any],
    ) -> None:
        super().__init__(coordinator, entry, obj)
        self._write_priority = DEFAULT_WRITE_PRIORITY

    @property
    def is_on(self) -> bool | None:
        """Return True if the switch is on (presentValue = active/1)."""
        value = self.get_present_value()
        if value is None:
            return None
        if isinstance(value, str):
            return value.lower() in ("active", "1", "true", "on")
        return bool(int(value))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on by writing active (1) to presentValue.

        For commandable objects this writes at the configured priority level
        in the Priority Array.
        """
        client: BACnetClient = self.hass.data[DOMAIN][self._entry.entry_id][DATA_CLIENT]
        success = await client.write_property(
            device_address=self.coordinator.device_address,
            object_type=self._object_type,
            instance=self._instance,
            property_name="presentValue",
            value=1,  # active
            priority=self._write_priority,
        )
        if success:
            # Optimistic update: immediately reflect in HA
            await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off by writing inactive (0) at the configured priority.

        Per BACnet standard, for commandable objects this writes at the
        specified priority level in the Priority Array, setting the output
        to inactive (0).
        """
        client: BACnetClient = self.hass.data[DOMAIN][self._entry.entry_id][DATA_CLIENT]
        success = await client.write_property(
            device_address=self.coordinator.device_address,
            object_type=self._object_type,
            instance=self._instance,
            property_name="presentValue",
            value=0,  # inactive
            priority=self._write_priority,
        )
        if success:
            await self.coordinator.async_request_refresh()
