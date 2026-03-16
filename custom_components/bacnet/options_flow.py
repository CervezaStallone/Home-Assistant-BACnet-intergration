"""
Options flow for BACnet IP integration.

Provides two configuration steps accessible from the integration's "Configure" button:
  Step 1 (init)           – COV toggle, polling fallback interval, naming toggle
  Step 2 (domain_mapping) – Per-object HA domain override (sensor/switch/number/climate/…)
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_COV_INCREMENT,
    CONF_DOMAIN_MAPPING,
    CONF_ENABLE_COV,
    CONF_POLLING_INTERVAL,
    CONF_SELECTED_OBJECTS,
    CONF_USE_DESCRIPTION,
    DEFAULT_COV_INCREMENT,
    DEFAULT_DOMAIN_MAP,
    DEFAULT_ENABLE_COV,
    DEFAULT_POLLING_INTERVAL,
    DEFAULT_USE_DESCRIPTION,
    SUPPORTED_DOMAINS,
)

_LOGGER = logging.getLogger(__name__)


class BACnetOptionsFlow(config_entries.OptionsFlow):
    """Handle BACnet integration options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Store the config entry so we can read current data + options."""
        self._config_entry = config_entry
        self._options_so_far: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Step 1: General options (COV, polling, naming)
    # ------------------------------------------------------------------

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """First options step — COV, polling interval, naming toggle."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # --- Validate polling interval ---
            polling = user_input.get(CONF_POLLING_INTERVAL, DEFAULT_POLLING_INTERVAL)
            if not isinstance(polling, int) or polling < 1:
                errors["base"] = "invalid_polling_interval"

            if not errors:
                # Merge with existing options so domain mapping is preserved
                new_options = {**self._config_entry.options, **user_input}
                # Proceed to domain mapping step
                self._options_so_far = new_options
                return await self.async_step_domain_mapping()

        # --- Current values (fallback to defaults) ---
        current_cov = self._config_entry.options.get(
            CONF_ENABLE_COV, DEFAULT_ENABLE_COV
        )
        current_poll = self._config_entry.options.get(
            CONF_POLLING_INTERVAL, DEFAULT_POLLING_INTERVAL
        )
        current_desc = self._config_entry.options.get(
            CONF_USE_DESCRIPTION, DEFAULT_USE_DESCRIPTION
        )
        current_cov_inc = self._config_entry.options.get(
            CONF_COV_INCREMENT, DEFAULT_COV_INCREMENT
        )

        schema = vol.Schema(
            {
                vol.Optional(CONF_ENABLE_COV, default=current_cov): bool,
                vol.Optional(CONF_COV_INCREMENT, default=current_cov_inc): vol.All(
                    vol.Coerce(float), vol.Range(min=0.0)
                ),
                vol.Optional(CONF_POLLING_INTERVAL, default=current_poll): vol.All(
                    vol.Coerce(int), vol.Range(min=1)
                ),
                vol.Optional(CONF_USE_DESCRIPTION, default=current_desc): bool,
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 2: Per-object domain mapping
    # ------------------------------------------------------------------

    async def async_step_domain_mapping(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Second options step — let the user reassign HA domains per BACnet object.

        Each selected BACnet object is shown as a dropdown with the available
        HA domains (sensor, binary_sensor, switch, number, climate).
        The user can change the mapping and save.
        """
        errors: dict[str, str] = {}

        # Retrieve the selected objects from the config entry data
        selected_objects: list[dict[str, Any]] = self._config_entry.data.get(
            CONF_SELECTED_OBJECTS, []
        )

        if user_input is not None:
            # Build the domain mapping dict from form values
            domain_mapping: dict[str, str] = {}
            for obj in selected_objects:
                obj_key = f"{obj['object_type']}:{obj['instance']}"
                field_key = f"domain_{obj_key}"
                if field_key in user_input:
                    domain_mapping[obj_key] = user_input[field_key]

            # Store in options and create entry
            final_options = {
                **self._options_so_far,
                CONF_DOMAIN_MAPPING: domain_mapping,
            }
            return self.async_create_entry(title="", data=final_options)

        # --- Build the form: one dropdown per BACnet object ---
        current_mapping: dict[str, str] = self._config_entry.options.get(
            CONF_DOMAIN_MAPPING, {}
        )

        schema_fields: dict[Any, Any] = {}
        for obj in selected_objects:
            obj_key = f"{obj['object_type']}:{obj['instance']}"

            # Current domain: user override → default map → "sensor"
            current_domain = current_mapping.get(
                obj_key, DEFAULT_DOMAIN_MAP.get(obj["object_type"], "sensor")
            )

            field_key = f"domain_{obj_key}"
            schema_fields[
                vol.Optional(
                    field_key,
                    default=current_domain,
                    description={"suggested_value": current_domain},
                )
            ] = vol.In({d: d for d in SUPPORTED_DOMAINS})

        schema = vol.Schema(schema_fields)

        return self.async_show_form(
            step_id="domain_mapping",
            data_schema=schema,
            errors=errors,
        )
