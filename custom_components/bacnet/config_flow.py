"""
Config flow for BACnet IP integration.

Provides a multi-step GUI configuration:
  Step 1 (user)          – Network settings: local IP/port, BBMD/Foreign Device config
  Step 2 (discovery)     – Who-Is device discovery, user selects one device
  Step 3 (select_objects)– Read object list from device, user picks objects with "Select All"
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
import homeassistant.helpers.config_validation as cv
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_BBMD_ADDRESS,
    CONF_BBMD_TTL,
    CONF_DEVICE_ADDRESS,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_LOCAL_IP,
    CONF_LOCAL_PORT,
    CONF_SELECT_ALL,
    CONF_SELECTED_OBJECTS,
    CONF_TARGET_ADDRESS,
    CONF_USE_BBMD,
    DEFAULT_BBMD_TTL,
    DEFAULT_PORT,
    DOMAIN,
    OBJECT_TYPE_NAMES,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_ip(ip_string: str) -> bool:
    """Return True if *ip_string* is a valid IPv4 address (or empty for auto)."""
    if not ip_string:
        return True  # empty = auto-detect
    try:
        ipaddress.IPv4Address(ip_string)
        return True
    except ValueError:
        return False


def _validate_bbmd_address(addr: str) -> bool:
    """Validate a BBMD address in 'IP:port' or plain 'IP' format."""
    if not addr:
        return False
    parts = addr.rsplit(":", 1)
    ip_part = parts[0]
    if not _validate_ip(ip_part):
        return False
    if len(parts) == 2:
        try:
            port = int(parts[1])
            return 1 <= port <= 65535
        except ValueError:
            return False
    return True


def _object_key(obj: dict[str, Any]) -> str:
    """Build a unique key string for a BACnet object dict."""
    return f"{obj['object_type']}:{obj['instance']}"


def _object_label(obj: dict[str, Any]) -> str:
    """Build a human-readable label for an object selection checkbox."""
    type_name = OBJECT_TYPE_NAMES.get(obj["object_type"], f"Type {obj['object_type']}")
    name = obj.get("object_name", "unnamed")
    instance = obj["instance"]
    return f"{type_name} ({instance}) — {name}"


# ---------------------------------------------------------------------------
# Config Flow
# ---------------------------------------------------------------------------

class BACnetConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the BACnet IP config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise flow-level state used across steps."""
        self._network_config: dict[str, Any] = {}
        self._discovered_devices: list[dict[str, Any]] = []
        self._selected_device: dict[str, Any] = {}
        self._discovered_objects: list[dict[str, Any]] = []
        self._client: Any | None = None  # BACnetClient instance during flow

    async def async_step_unignore(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle flow cancellation / cleanup.

        Ensures the BACnet client is disconnected if the user abandons
        the config flow mid-way (e.g. closes the browser tab).
        """
        await self._cleanup_client()
        return self.async_abort(reason="already_configured")

    async def _cleanup_client(self) -> None:
        """Disconnect the BACnet client if it was created during the flow."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Client cleanup error (ignored)")
            self._client = None

    # ------------------------------------------------------------------
    # Step 1: Network / BBMD configuration
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1 — configure the BACnet/IP network interface and optional BBMD."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # --- Validate inputs ---
            local_ip = user_input.get(CONF_LOCAL_IP, "").strip()
            if local_ip and not _validate_ip(local_ip):
                errors["base"] = "invalid_ip"

            target_address = user_input.get(CONF_TARGET_ADDRESS, "").strip()
            if target_address:
                # Validate target address (IP or IP:port)
                if not _validate_bbmd_address(target_address):
                    errors[CONF_TARGET_ADDRESS] = "invalid_ip"

            use_bbmd = user_input.get(CONF_USE_BBMD, False)
            bbmd_address = user_input.get(CONF_BBMD_ADDRESS, "").strip()
            if use_bbmd and not _validate_bbmd_address(bbmd_address):
                errors[CONF_BBMD_ADDRESS] = "invalid_ip"

            if not errors:
                # Store network config and move to discovery
                self._network_config = {
                    CONF_LOCAL_IP: local_ip,
                    CONF_LOCAL_PORT: user_input.get(CONF_LOCAL_PORT, DEFAULT_PORT),
                    CONF_TARGET_ADDRESS: target_address,
                    CONF_USE_BBMD: use_bbmd,
                    CONF_BBMD_ADDRESS: bbmd_address if use_bbmd else "",
                    CONF_BBMD_TTL: user_input.get(CONF_BBMD_TTL, DEFAULT_BBMD_TTL)
                    if use_bbmd
                    else DEFAULT_BBMD_TTL,
                }
                return await self.async_step_discovery()

        # --- Build the form schema ---
        schema = vol.Schema(
            {
                vol.Optional(CONF_LOCAL_IP, default=""): str,
                vol.Optional(CONF_LOCAL_PORT, default=DEFAULT_PORT): vol.Coerce(int),
                vol.Optional(CONF_TARGET_ADDRESS, default=""): str,
                vol.Optional(CONF_USE_BBMD, default=False): bool,
                vol.Optional(CONF_BBMD_ADDRESS, default=""): str,
                vol.Optional(CONF_BBMD_TTL, default=DEFAULT_BBMD_TTL): vol.Coerce(int),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 2: Device discovery via Who-Is
    # ------------------------------------------------------------------

    async def async_step_discovery(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2 — discover BACnet devices and let the user pick one."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # User selected a device — store it and move to object selection
            selected_key = user_input[CONF_DEVICE_ID]
            for dev in self._discovered_devices:
                if str(dev["device_id"]) == selected_key:
                    self._selected_device = dev
                    break
            else:
                errors["base"] = "unknown"

            if not errors:
                # Check if this device is already configured
                await self.async_set_unique_id(str(self._selected_device["device_id"]))
                self._abort_if_unique_id_configured()
                return await self.async_step_select_objects()

        # --- Perform discovery ---
        if not self._discovered_devices:
            from .bacnet_client import BACnetClient  # noqa: WPS433

            client = BACnetClient(
                local_ip=self._network_config[CONF_LOCAL_IP],
                local_port=self._network_config[CONF_LOCAL_PORT],
            )
            try:
                # connect() creates Normal or ForeignApplication depending
                # on whether a BBMD address is provided.
                bbmd_addr = None
                if self._network_config[CONF_USE_BBMD]:
                    bbmd_addr = self._network_config[CONF_BBMD_ADDRESS]
                await client.connect(
                    bbmd_address=bbmd_addr,
                    bbmd_ttl=self._network_config.get(CONF_BBMD_TTL, 900),
                )

                # Check if user specified a target address (manual entry)
                target = self._network_config.get(CONF_TARGET_ADDRESS, "")
                if target:
                    # Manual device entry — skip broadcast, unicast to device
                    # Append default BACnet port if not specified
                    if ":" not in target:
                        target = f"{target}:47808"
                    device_info = await client.read_device_info(target)
                    if device_info:
                        self._discovered_devices = [device_info]
                    else:
                        errors["base"] = "device_unreachable"
                else:
                    # Broadcast Who-Is discovery
                    self._discovered_devices = await client.discover_devices(timeout=5)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.error(
                    "Discovery failed: %s (%s)", exc, type(exc).__name__
                )
                errors["base"] = "cannot_connect"
            finally:
                self._client = client  # keep for object reads
                # Client stays open until flow ends or we close it

        if not errors and not self._discovered_devices:
            errors["base"] = "no_devices_found"

        if errors:
            # Go back to network step on hard errors
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Optional(
                            CONF_LOCAL_IP,
                            default=self._network_config.get(CONF_LOCAL_IP, ""),
                        ): str,
                        vol.Optional(
                            CONF_LOCAL_PORT,
                            default=self._network_config.get(CONF_LOCAL_PORT, DEFAULT_PORT),
                        ): vol.Coerce(int),
                        vol.Optional(
                            CONF_TARGET_ADDRESS,
                            default=self._network_config.get(CONF_TARGET_ADDRESS, ""),
                        ): str,
                        vol.Optional(
                            CONF_USE_BBMD,
                            default=self._network_config.get(CONF_USE_BBMD, False),
                        ): bool,
                        vol.Optional(
                            CONF_BBMD_ADDRESS,
                            default=self._network_config.get(CONF_BBMD_ADDRESS, ""),
                        ): str,
                        vol.Optional(
                            CONF_BBMD_TTL,
                            default=self._network_config.get(CONF_BBMD_TTL, DEFAULT_BBMD_TTL),
                        ): vol.Coerce(int),
                    }
                ),
                errors=errors,
            )

        # --- If only one device found (e.g. manual entry), auto-select it ---
        if len(self._discovered_devices) == 1 and user_input is None:
            self._selected_device = self._discovered_devices[0]
            await self.async_set_unique_id(str(self._selected_device["device_id"]))
            self._abort_if_unique_id_configured()
            return await self.async_step_select_objects()

        # --- Build device selection dropdown ---
        device_options = {
            str(dev["device_id"]): f"{dev.get('device_name', 'Device')} (ID {dev['device_id']}, {dev.get('address', '?')})"
            for dev in self._discovered_devices
        }

        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): vol.In(device_options),
            }
        )

        return self.async_show_form(
            step_id="discovery",
            data_schema=schema,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 3: Object selection with "Select All"
    # ------------------------------------------------------------------

    async def async_step_select_objects(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3 — read objects from the selected device, let user pick which to import."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # --- Process selection ---
            select_all = user_input.get(CONF_SELECT_ALL, False)
            if select_all:
                selected_keys = [_object_key(obj) for obj in self._discovered_objects]
            else:
                selected_keys = user_input.get(CONF_SELECTED_OBJECTS, [])

            if not selected_keys:
                errors["base"] = "no_objects_found"
            else:
                # Build the final object list with full metadata
                selected_objects: list[dict[str, Any]] = []
                for obj in self._discovered_objects:
                    if _object_key(obj) in selected_keys:
                        selected_objects.append(obj)

                # Clean up the client used during flow
                if self._client is not None:
                    await self._client.disconnect()
                    self._client = None

                # --- Create the config entry ---
                return self.async_create_entry(
                    title=self._selected_device.get("device_name", "BACnet Device"),
                    data={
                        **self._network_config,
                        CONF_DEVICE_ID: self._selected_device["device_id"],
                        CONF_DEVICE_NAME: self._selected_device.get("device_name", "BACnet Device"),
                        CONF_DEVICE_ADDRESS: self._selected_device.get("address", ""),
                        CONF_SELECTED_OBJECTS: selected_objects,
                    },
                )

        # --- Read object list from device (first visit to this step) ---
        if not self._discovered_objects:
            if self._client is None:
                errors["base"] = "cannot_connect"
            else:
                try:
                    self._discovered_objects = await self._client.read_object_list(
                        device_address=self._selected_device.get("address", ""),
                        device_id=self._selected_device["device_id"],
                    )
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.error(
                        "Failed to read object list from device %s: %s",
                        self._selected_device.get("device_id"),
                        exc,
                    )
                    errors["base"] = "no_objects_found"

        if not errors and not self._discovered_objects:
            errors["base"] = "no_objects_found"

        if errors:
            return self.async_show_form(
                step_id="select_objects",
                data_schema=vol.Schema({}),
                errors=errors,
            )

        # --- Build multi-select checkbox list ---
        object_options: dict[str, str] = {
            _object_key(obj): _object_label(obj) for obj in self._discovered_objects
        }

        schema = vol.Schema(
            {
                vol.Optional(CONF_SELECT_ALL, default=False): bool,
                vol.Optional(
                    CONF_SELECTED_OBJECTS, default=list(object_options.keys())
                ): cv.multi_select(object_options),
            }
        )

        return self.async_show_form(
            step_id="select_objects",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "device_name": self._selected_device.get("device_name", "BACnet Device"),
                "object_count": str(len(self._discovered_objects)),
            },
        )

    # ------------------------------------------------------------------
    # Options flow hook
    # ------------------------------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow handler."""
        from .options_flow import BACnetOptionsFlow  # noqa: WPS433

        return BACnetOptionsFlow(config_entry)
