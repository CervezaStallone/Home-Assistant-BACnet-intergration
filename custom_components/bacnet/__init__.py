"""
BACnet IP Integration for Home Assistant.

This integration provides full BACnet/IP support including:
- Local network and BBMD / Foreign Device Registration for cross-subnet communication
- Automatic device discovery via Who-Is / I-Am
- Per-object COV subscriptions with automatic polling fallback
- Read/write with proper Priority Array handling
- Dynamic domain mapping (sensor, switch, number, binary_sensor, climate)

All configuration is done via the GUI (config_flow / options_flow).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_BBMD_ADDRESS,
    CONF_BBMD_TTL,
    CONF_COV_INCREMENT,
    CONF_DOMAIN_MAPPING,
    CONF_ENABLE_COV,
    CONF_FIRMWARE_VERSION,
    CONF_LOCAL_IP,
    CONF_LOCAL_PORT,
    CONF_MODEL_NAME,
    CONF_POLLING_INTERVAL,
    CONF_SELECTED_OBJECTS,
    CONF_SOFTWARE_VERSION,
    CONF_USE_BBMD,
    CONF_USE_DESCRIPTION,
    CONF_VENDOR_NAME,
    DATA_CLIENT,
    DATA_COORDINATOR,
    DATA_DEVICE_INFO,
    DATA_OBJECTS,
    DATA_UNSUB,
    DEFAULT_COV_INCREMENT,
    DEFAULT_DOMAIN_MAP,
    DEFAULT_ENABLE_COV,
    DEFAULT_POLLING_INTERVAL,
    DEFAULT_USE_DESCRIPTION,
    DOMAIN,
    OBJECT_TYPE_ANALOG_VALUE,
    OBJECT_TYPE_BINARY_VALUE,
    OBJECT_TYPE_MULTI_STATE_VALUE,
)

_LOGGER = logging.getLogger(__name__)

# All platforms that this integration can dynamically register entities on.
PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.CLIMATE,
]


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


# Commandable Value objects should use writable domains
_COMMANDABLE_VALUE_DOMAIN: dict[int, str] = {
    OBJECT_TYPE_ANALOG_VALUE: "number",
    OBJECT_TYPE_BINARY_VALUE: "switch",
    OBJECT_TYPE_MULTI_STATE_VALUE: "number",
}


def _get_platforms_in_use(
    objects: list[dict], domain_overrides: dict[str, str]
) -> list[Platform]:
    """Determine which HA platforms are actually needed based on selected objects.

    This avoids setting up platform files that have zero entities, which
    keeps startup quick and log output clean.
    """
    domains_needed: set[str] = set()
    for obj in objects:
        obj_key = f"{obj['object_type']}:{obj['instance']}"
        # Check user overrides first, then commandable Value logic, then default
        override = domain_overrides.get(obj_key)
        if override:
            domains_needed.add(override)
        elif obj.get("commandable") and obj["object_type"] in _COMMANDABLE_VALUE_DOMAIN:
            domains_needed.add(_COMMANDABLE_VALUE_DOMAIN[obj["object_type"]])
        else:
            domains_needed.add(DEFAULT_DOMAIN_MAP.get(obj["object_type"], "sensor"))
    return [Platform(d) for d in domains_needed if d in {p.value for p in PLATFORMS}]


# ---------------------------------------------------------------------------
# Integration lifecycle
# ---------------------------------------------------------------------------


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up BACnet IP from a config entry.

    This is called by Home Assistant when the user completes the config flow
    or when HA starts and an existing config entry is loaded.

    Lifecycle:
    1. Create a BACnetClient and connect it to the network.
    2. Optionally register as a Foreign Device with a BBMD.
    3. Build the data coordinator for COV + polling fallback.
    4. Store runtime references in hass.data so platforms can access them.
    5. Forward setup to the required platform files.
    """
    # Lazy import to avoid loading BACpypes3 at integration discovery time
    from .bacnet_client import BACnetClient  # noqa: WPS433
    from .coordinator import BACnetCoordinator  # noqa: WPS433

    hass.data.setdefault(DOMAIN, {})

    # ---- 1. Extract configuration ----
    local_ip: str = entry.data.get(CONF_LOCAL_IP, "")
    local_port: int = entry.data.get(CONF_LOCAL_PORT, 47808)
    use_bbmd: bool = entry.data.get(CONF_USE_BBMD, False)
    bbmd_address: str = entry.data.get(CONF_BBMD_ADDRESS, "")
    bbmd_ttl: int = entry.data.get(CONF_BBMD_TTL, 900)
    selected_objects: list[dict[str, Any]] = entry.data.get(CONF_SELECTED_OBJECTS, [])

    # Options (may be updated at runtime via options_flow)
    enable_cov: bool = entry.options.get(CONF_ENABLE_COV, DEFAULT_ENABLE_COV)
    polling_interval: int = entry.options.get(
        CONF_POLLING_INTERVAL, DEFAULT_POLLING_INTERVAL
    )
    use_description: bool = entry.options.get(
        CONF_USE_DESCRIPTION, DEFAULT_USE_DESCRIPTION
    )
    domain_overrides: dict[str, str] = entry.options.get(CONF_DOMAIN_MAPPING, {})
    cov_increment: float = entry.options.get(CONF_COV_INCREMENT, DEFAULT_COV_INCREMENT)

    # ---- 2. Create & connect the BACnet client ----
    client = BACnetClient(
        local_ip=local_ip,
        local_port=local_port,
    )
    try:
        # connect() creates a NormalApplication or ForeignApplication
        # depending on whether a BBMD address is provided.  It must run
        # on the event loop (BACpypes3 uses asyncio UDP transport).
        await client.connect(
            bbmd_address=bbmd_address if use_bbmd else None,
            bbmd_ttl=bbmd_ttl,
        )
    except Exception as exc:  # noqa: BLE001
        _LOGGER.error("Failed to start BACnet client: %s", exc)
        raise ConfigEntryNotReady(f"Cannot connect to BACnet network: {exc}") from exc

    # ---- 4. Build coordinator ----
    coordinator = BACnetCoordinator(
        hass=hass,
        client=client,
        objects=selected_objects,
        enable_cov=enable_cov,
        polling_interval=polling_interval,
        use_description=use_description,
        domain_overrides=domain_overrides,
        entry=entry,
        cov_increment=cov_increment,
    )

    # Perform the first data refresh so entities have initial state
    await coordinator.async_config_entry_first_refresh()

    # ---- 5. Store runtime data ----
    hass.data[DOMAIN][entry.entry_id] = {
        DATA_CLIENT: client,
        DATA_COORDINATOR: coordinator,
        DATA_OBJECTS: selected_objects,
        DATA_DEVICE_INFO: {
            "device_id": entry.data.get("device_id"),
            "device_name": entry.data.get("device_name", "BACnet Device"),
            "device_address": entry.data.get("device_address", ""),
            "vendor_name": entry.data.get(CONF_VENDOR_NAME, ""),
            "model_name": entry.data.get(CONF_MODEL_NAME, ""),
            "firmware_version": entry.data.get(CONF_FIRMWARE_VERSION, ""),
            "software_version": entry.data.get(CONF_SOFTWARE_VERSION, ""),
        },
        DATA_UNSUB: [],
    }

    # ---- 6. Forward to platforms ----
    needed_platforms = _get_platforms_in_use(selected_objects, domain_overrides)
    await hass.config_entries.async_forward_entry_setups(entry, needed_platforms)

    # ---- 7. Listen for option changes ----
    unsub = entry.add_update_listener(_async_options_updated)
    hass.data[DOMAIN][entry.entry_id][DATA_UNSUB].append(unsub)

    _LOGGER.info(
        "BACnet integration setup complete for device '%s' with %d objects",
        entry.data.get("device_name", "unknown"),
        len(selected_objects),
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a BACnet config entry.

    Called when the user removes the integration or during HA shutdown.
    Cleans up:
    - COV subscriptions
    - Polling tasks
    - BACnet network connection
    - hass.data references
    """
    entry_data = hass.data[DOMAIN].get(entry.entry_id)
    if entry_data is None:
        return True

    # Determine which platforms were loaded
    domain_overrides: dict[str, str] = entry.options.get(CONF_DOMAIN_MAPPING, {})
    selected_objects = entry_data.get(DATA_OBJECTS, [])
    needed_platforms = _get_platforms_in_use(selected_objects, domain_overrides)

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, needed_platforms
    )

    if unload_ok:
        # Cancel update listener subscriptions
        for unsub in entry_data.get(DATA_UNSUB, []):
            unsub()

        # Shut down coordinator (cancels COV subs & polling tasks)
        coordinator = entry_data.get(DATA_COORDINATOR)
        if coordinator is not None:
            await coordinator.async_shutdown()

        # Disconnect the BACnet client
        client = entry_data.get(DATA_CLIENT)
        if client is not None:
            await client.disconnect()

        hass.data[DOMAIN].pop(entry.entry_id)
        _LOGGER.info("BACnet integration unloaded for entry %s", entry.entry_id)

    return unload_ok


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update.

    When the user changes options (COV, polling interval, naming, domain mapping)
    we reload the entire config entry so all entities and the coordinator
    pick up the new settings cleanly.
    """
    _LOGGER.debug("Options updated for BACnet entry %s — reloading", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)
