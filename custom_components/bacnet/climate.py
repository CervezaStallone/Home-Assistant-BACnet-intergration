"""
Climate platform for BACnet IP integration.

Creates HA climate entities for BACnet objects mapped to "climate".
This is the most complex mapping because a single HA climate entity
typically requires multiple BACnet points (setpoint, temperature, mode).

For simplicity, this integration maps ONE BACnet object (typically an
Analog Value representing a temperature setpoint) to a climate entity.
The entity provides:
  - Current temperature: read from presentValue
  - Target temperature: read/write presentValue (with Priority Array)
  - HVAC mode: heating-only by default (can be extended)

For full multi-point HVAC mapping, the user should use the domain_mapping
to assign the setpoint object to "climate", and leave the actual room
temperature sensor as "sensor".
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
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
    """Set up BACnet climate entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: BACnetCoordinator = data[DATA_COORDINATOR]
    objects: list[dict[str, Any]] = data[DATA_OBJECTS]

    entities: list[BACnetClimate] = []
    for obj in objects:
        domain = coordinator.get_domain_for_object(obj)
        if domain == "climate":
            entities.append(BACnetClimate(coordinator, entry, obj))

    if entities:
        async_add_entities(entities)
        _LOGGER.debug("Added %d BACnet climate entities", len(entities))


class BACnetClimate(BACnetEntity, ClimateEntity):
    """Representation of a BACnet setpoint object as a HA climate entity.

    Maps a single BACnet object (usually an Analog Value/Output used as
    a temperature setpoint) to the HA climate platform.

    Features:
      - TARGET_TEMPERATURE:    writable setpoint via presentValue
      - HVAC modes:            HEAT + OFF (OFF relinquishes the setpoint)
      - Temperature unit:      derived from BACnet engineering units
    """

    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_target_temperature_step = 0.5
    _attr_min_temp = 5.0
    _attr_max_temp = 40.0

    def __init__(
        self,
        coordinator: BACnetCoordinator,
        entry: ConfigEntry,
        obj: dict[str, Any],
    ) -> None:
        super().__init__(coordinator, entry, obj)
        self._write_priority = DEFAULT_WRITE_PRIORITY

        # Determine temperature unit from BACnet engineering units
        units = obj.get("units", "")
        if "fahrenheit" in str(units).lower():
            self._attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
            self._attr_min_temp = 40.0
            self._attr_max_temp = 104.0
        else:
            self._attr_temperature_unit = UnitOfTemperature.CELSIUS

        self._is_active = True  # Tracks whether we have an active setpoint

    # ------------------------------------------------------------------
    # State properties
    # ------------------------------------------------------------------

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature reading.

        Since this entity maps a setpoint object, current_temperature
        reflects the setpoint's presentValue. For a true room temperature,
        the user should create a separate sensor entity.
        """
        value = self.get_present_value()
        if value is None:
            return None
        try:
            return round(float(value), 1)
        except (ValueError, TypeError):
            return None

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature (setpoint)."""
        return self.current_temperature

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current HVAC mode.

        HEAT = setpoint is actively commanded
        OFF  = setpoint has been relinquished (Null written)
        """
        if not self._is_active:
            return HVACMode.OFF
        if self.get_present_value() is not None:
            return HVACMode.HEAT
        return HVACMode.OFF

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the target temperature by writing to the BACnet setpoint object.

        Uses the Priority Array for commandable objects.
        """
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        client: BACnetClient = self.hass.data[DOMAIN][self._entry.entry_id][DATA_CLIENT]
        success = await client.write_property(
            device_address=self.coordinator.device_address,
            object_type=self._object_type,
            instance=self._instance,
            property_name="presentValue",
            value=float(temperature),
            priority=self._write_priority,
        )
        if success:
            self._is_active = True
            await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode.

        HEAT: Re-write the last known setpoint (or do nothing if already active).
        OFF:  Relinquish the setpoint by writing Null at the current priority,
              releasing the override and allowing the Relinquish Default to
              take effect on the BACnet device.
        """
        client: BACnetClient = self.hass.data[DOMAIN][self._entry.entry_id][DATA_CLIENT]

        if hvac_mode == HVACMode.OFF:
            success = await client.relinquish(
                device_address=self.coordinator.device_address,
                object_type=self._object_type,
                instance=self._instance,
                priority=self._write_priority,
            )
            if success:
                self._is_active = False
                await self.coordinator.async_request_refresh()

        elif hvac_mode == HVACMode.HEAT:
            # Re-activate: write the current target temperature (if known)
            current = self.target_temperature
            if current is not None:
                await self.async_set_temperature(temperature=current)
            self._is_active = True
