"""
BACnet client module — isolates all BACpypes3 interaction.

Responsibilities:
- Network connection (local bind + optional Foreign Device Registration with BBMD)
- Device discovery via Who-Is / I-Am
- Object list and property reads (ReadProperty / ReadPropertyMultiple)
- Property writes with Priority Array support and Null/Relinquish
- COV subscription management
- Commandability/writability detection
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from bacpypes3.ipv4.app import NormalApplication
from bacpypes3.local.device import DeviceObject
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import (
    CharacterString,
    Enumerated,
    Null,
    ObjectIdentifier,
    Real,
    Unsigned,
)

from .const import (
    DEFAULT_WRITE_PRIORITY,
    OBJECT_TYPE_ANALOG_INPUT,
    OBJECT_TYPE_ANALOG_OUTPUT,
    OBJECT_TYPE_ANALOG_VALUE,
    OBJECT_TYPE_BINARY_INPUT,
    OBJECT_TYPE_BINARY_OUTPUT,
    OBJECT_TYPE_BINARY_VALUE,
    OBJECT_TYPE_MULTI_STATE_INPUT,
    OBJECT_TYPE_MULTI_STATE_OUTPUT,
    OBJECT_TYPE_MULTI_STATE_VALUE,
    OBJECT_TYPE_NAMES,
    PROP_DESCRIPTION,
    PROP_OBJECT_IDENTIFIER,
    PROP_OBJECT_LIST,
    PROP_OBJECT_NAME,
    PROP_OBJECT_TYPE,
    PROP_OUT_OF_SERVICE,
    PROP_PRESENT_VALUE,
    PROP_PRIORITY_ARRAY,
    PROP_RELINQUISH_DEFAULT,
    PROP_STATUS_FLAGS,
    PROP_UNITS,
)

_LOGGER = logging.getLogger(__name__)

# BACnet object types we support importing as HA entities
SUPPORTED_OBJECT_TYPES: set[int] = {
    OBJECT_TYPE_ANALOG_INPUT,
    OBJECT_TYPE_ANALOG_OUTPUT,
    OBJECT_TYPE_ANALOG_VALUE,
    OBJECT_TYPE_BINARY_INPUT,
    OBJECT_TYPE_BINARY_OUTPUT,
    OBJECT_TYPE_BINARY_VALUE,
    OBJECT_TYPE_MULTI_STATE_INPUT,
    OBJECT_TYPE_MULTI_STATE_OUTPUT,
    OBJECT_TYPE_MULTI_STATE_VALUE,
}

# Object types that are inherently commandable (have a Priority Array)
COMMANDABLE_TYPES: set[int] = {
    OBJECT_TYPE_ANALOG_OUTPUT,
    OBJECT_TYPE_BINARY_OUTPUT,
    OBJECT_TYPE_MULTI_STATE_OUTPUT,
}

# Object types that *may* be writable (Values can optionally be commandable)
POTENTIALLY_WRITABLE_TYPES: set[int] = {
    OBJECT_TYPE_ANALOG_VALUE,
    OBJECT_TYPE_BINARY_VALUE,
    OBJECT_TYPE_MULTI_STATE_VALUE,
}


class BACnetClient:
    """Wrapper around BACpypes3 providing a clean async API for HA.

    Usage:
        client = BACnetClient(local_ip="192.168.1.100", local_port=47808)
        await client.connect()
        devices = await client.discover_devices(timeout=5)
        objects = await client.read_object_list(device_address, device_id)
        value = await client.read_property(address, obj_type, instance, prop_id)
        await client.write_property(address, obj_type, instance, prop_id, value, priority=8)
        await client.disconnect()
    """

    def __init__(
        self,
        local_ip: str = "",
        local_port: int = 47808,
    ) -> None:
        self._local_ip = local_ip
        self._local_port = local_port
        self._app: NormalApplication | None = None
        self._cov_callbacks: dict[str, Callable] = {}
        self._cov_contexts: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _build_app_args(self) -> tuple:
        """Prepare Address object and device object for app construction."""
        if self._local_ip:
            local_addr = Address(f"{self._local_ip}:{self._local_port}")
        else:
            local_addr = Address(f"0.0.0.0:{self._local_port}")

        device_object = DeviceObject(
            objectIdentifier=("device", 999999),
            objectName="HomeAssistant-BACnet",
            vendorIdentifier=0,
            maxApduLengthAccepted=1476,
            segmentationSupported="segmented-both",
        )
        return device_object, local_addr

    def connect_sync(self) -> None:
        """Create the BACpypes3 application (BLOCKING — binds UDP socket).

        This must be called from an executor thread when running inside HA
        to avoid blocking the event loop.  Use:
            await hass.async_add_executor_job(client.connect_sync)
        """
        device_object, local_addr = self._build_app_args()
        _LOGGER.debug("Creating BACnet application on %s (sync)", local_addr)
        self._app = NormalApplication(device_object, local_addr)
        _LOGGER.info("BACnet client connected on %s", local_addr)

    async def connect(self) -> None:
        """Create the BACpypes3 application and bind to the network.

        If local_ip is empty the OS default interface is used.
        NOTE: When running inside Home Assistant, prefer connect_sync() via
        hass.async_add_executor_job to avoid blocking the event loop.
        This async version is kept for use in the config flow where we
        have direct access to the event loop.
        """
        device_object, local_addr = self._build_app_args()
        _LOGGER.debug("Creating BACnet application on %s", local_addr)

        # NormalApplication constructor binds a UDP socket — this is
        # synchronous I/O and must not block the HA event loop.  When called
        # from within HA, the caller (async_setup_entry / config_flow) should
        # wrap this in hass.async_add_executor_job.  We store the constructor
        # args and create the app here; the caller is responsible for the
        # executor context.
        self._app = NormalApplication(device_object, local_addr)

        _LOGGER.info("BACnet client connected on %s", local_addr)

    async def disconnect(self) -> None:
        """Shut down the BACpypes3 application and release the UDP socket."""
        if self._app is not None:
            try:
                await self._app.close()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Exception during app close (ignored)")
            self._app = None
            _LOGGER.info("BACnet client disconnected")

    # ------------------------------------------------------------------
    # Foreign Device Registration (BBMD)
    # ------------------------------------------------------------------

    async def register_foreign_device(
        self, bbmd_address: str, ttl: int = 900
    ) -> None:
        """Register this application as a Foreign Device with a BBMD.

        This is required when BACnet devices reside on a different subnet/VLAN.
        The BBMD will forward broadcast messages (Who-Is, I-Am, etc.) across
        network boundaries.

        Args:
            bbmd_address: IP:port of the BBMD (e.g. "192.168.2.1:47808").
            ttl: Time-to-live for the registration in seconds (re-registration
                 happens automatically by BACpypes3).
        """
        if self._app is None:
            raise RuntimeError("Client not connected")

        addr = Address(bbmd_address)
        _LOGGER.info("Registering as Foreign Device with BBMD at %s (TTL=%ds)", bbmd_address, ttl)
        await self._app.register_as_foreign_device(addr, ttl)

    # ------------------------------------------------------------------
    # Device discovery – Who-Is / I-Am
    # ------------------------------------------------------------------

    async def discover_devices(self, timeout: float = 5.0) -> list[dict[str, Any]]:
        """Send a global Who-Is and collect I-Am responses.

        Returns a list of dicts with keys: device_id, device_name, address.
        """
        if self._app is None:
            raise RuntimeError("Client not connected")

        devices: list[dict[str, Any]] = []
        seen_ids: set[int] = set()

        _LOGGER.debug("Sending Who-Is broadcast (timeout=%.1fs)", timeout)

        try:
            # who_is returns an async iterator of I-Am responses
            async for i_am in self._app.who_is(timeout=timeout):
                device_id = i_am.iAmDeviceIdentifier[1]
                if device_id in seen_ids:
                    continue
                seen_ids.add(device_id)

                # Try to read the device name
                device_name = f"Device {device_id}"
                try:
                    name = await self._app.read_property(
                        i_am.pduSource,
                        ObjectIdentifier(("device", device_id)),
                        "objectName",
                    )
                    if name:
                        device_name = str(name)
                except Exception:  # noqa: BLE001
                    pass  # use default name

                devices.append(
                    {
                        "device_id": device_id,
                        "device_name": device_name,
                        "address": str(i_am.pduSource),
                    }
                )
                _LOGGER.debug("Discovered device: %s (%d) at %s", device_name, device_id, i_am.pduSource)
        except asyncio.TimeoutError:
            pass  # normal — discovery just timed out
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Error during Who-Is discovery: %s", exc)

        _LOGGER.info("Discovery complete: found %d device(s)", len(devices))
        return devices

    # ------------------------------------------------------------------
    # Object list and property reads
    # ------------------------------------------------------------------

    async def read_object_list(
        self, device_address: str, device_id: int
    ) -> list[dict[str, Any]]:
        """Read the Object List from a device and fetch metadata for each supported object.

        For each object we read: objectName, description, presentValue, units,
        statusFlags, outOfService. We also detect if the object is commandable.

        Returns a list of object dicts ready for storage in the config entry.
        """
        if self._app is None:
            raise RuntimeError("Client not connected")

        addr = Address(device_address)
        device_oid = ObjectIdentifier(("device", device_id))
        objects: list[dict[str, Any]] = []

        # 1. Read the Object List property from the Device object
        try:
            object_list = await self._app.read_property(addr, device_oid, "objectList")
        except Exception as exc:
            _LOGGER.error("Failed to read objectList from %s: %s", device_address, exc)
            raise

        if object_list is None:
            _LOGGER.warning("objectList is None for device %s", device_id)
            return objects

        # 2. Iterate and read metadata for each supported object type
        for oid in object_list:
            obj_type_str, instance = oid
            # Convert string type name to integer type code
            obj_type_int = self._object_type_str_to_int(obj_type_str)
            if obj_type_int is None or obj_type_int not in SUPPORTED_OBJECT_TYPES:
                continue

            obj_info = await self._read_object_metadata(addr, oid, obj_type_int, instance)
            if obj_info is not None:
                objects.append(obj_info)

        _LOGGER.info(
            "Read %d supported objects from device %s (%d)",
            len(objects),
            device_address,
            device_id,
        )
        return objects

    async def _read_object_metadata(
        self,
        addr: Address,
        oid: ObjectIdentifier,
        obj_type: int,
        instance: int,
    ) -> dict[str, Any] | None:
        """Read metadata properties for one BACnet object.

        Returns a dict suitable for storage in the config entry, or None on failure.
        """
        try:
            # Read commonly needed properties individually (safer than RPM for
            # devices that don't support ReadPropertyMultiple)
            object_name = await self._safe_read(addr, oid, "objectName") or f"Object {instance}"
            description = await self._safe_read(addr, oid, "description") or ""
            units = await self._safe_read(addr, oid, "units")
            present_value = await self._safe_read(addr, oid, "presentValue")

            # Determine if this object is commandable (has a Priority Array)
            commandable = obj_type in COMMANDABLE_TYPES
            if obj_type in POTENTIALLY_WRITABLE_TYPES:
                # Try to read priority array — if it exists the object is commandable
                pa = await self._safe_read(addr, oid, "priorityArray")
                if pa is not None:
                    commandable = True

            return {
                "object_type": obj_type,
                "instance": instance,
                "object_name": str(object_name),
                "description": str(description),
                "units": str(units) if units is not None else None,
                "present_value": self._coerce_value(present_value),
                "commandable": commandable,
            }
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to read metadata for %s:%d — %s", oid, instance, exc
            )
            return None

    async def _safe_read(
        self, addr: Address, oid: ObjectIdentifier, prop_name: str
    ) -> Any | None:
        """Read a single property, returning None on any error."""
        try:
            return await self._app.read_property(addr, oid, prop_name)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # Single property read (for coordinator polling)
    # ------------------------------------------------------------------

    async def read_property(
        self,
        device_address: str,
        object_type: int,
        instance: int,
        property_name: str = "presentValue",
    ) -> Any | None:
        """Read a single property from a BACnet object.

        Args:
            device_address: Target device IP address string.
            object_type: BACnet object type integer.
            instance: Object instance number.
            property_name: Property to read (default: presentValue).

        Returns:
            The property value, or None on error.
        """
        if self._app is None:
            raise RuntimeError("Client not connected")

        addr = Address(device_address)
        oid = ObjectIdentifier((self._int_to_object_type_str(object_type), instance))
        return await self._safe_read(addr, oid, property_name)

    async def read_multiple_properties(
        self,
        device_address: str,
        object_type: int,
        instance: int,
        property_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Read multiple properties from one object.

        Falls back to individual reads if ReadPropertyMultiple is not supported.
        """
        if property_names is None:
            property_names = ["presentValue", "statusFlags"]

        result: dict[str, Any] = {}
        for prop in property_names:
            value = await self.read_property(device_address, object_type, instance, prop)
            result[prop] = value
        return result

    # ------------------------------------------------------------------
    # Property write with Priority Array support
    # ------------------------------------------------------------------

    async def write_property(
        self,
        device_address: str,
        object_type: int,
        instance: int,
        property_name: str,
        value: Any,
        priority: int = DEFAULT_WRITE_PRIORITY,
    ) -> bool:
        """Write a value to a BACnet property with proper Priority Array handling.

        BACnet Standard compliance:
        - For commandable objects, writes go through the Priority Array at the
          specified priority level (default 16 = lowest).
        - To "relinquish" a commanded value (release the override), write Null
          at the previously written priority level.
        - For non-commandable/writable objects, priority is not used.

        Args:
            device_address: Target device address.
            object_type: BACnet object type integer.
            instance: Object instance number.
            property_name: Property to write (usually "presentValue").
            value: The value to write. Use None to send Null (relinquish).
            priority: BACnet priority level (1-16). Only used for commandable objects.

        Returns:
            True on success, False on failure.
        """
        if self._app is None:
            raise RuntimeError("Client not connected")

        addr = Address(device_address)
        type_str = self._int_to_object_type_str(object_type)
        oid = ObjectIdentifier((type_str, instance))

        # Determine if this is a commandable object that needs priority
        # NOTE: must parenthesise the set union — without parens, | is bitwise
        # OR on the integer values, not set union.
        is_commandable = object_type in (COMMANDABLE_TYPES | POTENTIALLY_WRITABLE_TYPES)

        # Convert None → Null for relinquish
        if value is None:
            bacnet_value = Null()
        else:
            bacnet_value = self._python_to_bacnet_value(value, object_type)

        try:
            _LOGGER.debug(
                "Writing %s to %s:%d.%s (priority=%s, commandable=%s)",
                value,
                type_str,
                instance,
                property_name,
                priority if is_commandable else "N/A",
                is_commandable,
            )

            if is_commandable:
                await self._app.write_property(
                    addr, oid, property_name, bacnet_value, priority=priority
                )
            else:
                await self._app.write_property(
                    addr, oid, property_name, bacnet_value
                )

            _LOGGER.debug("Write successful")
            return True

        except Exception as exc:  # noqa: BLE001
            _LOGGER.error(
                "Write failed for %s:%d.%s = %s: %s",
                type_str,
                instance,
                property_name,
                value,
                exc,
            )
            return False

    async def relinquish(
        self,
        device_address: str,
        object_type: int,
        instance: int,
        priority: int = DEFAULT_WRITE_PRIORITY,
    ) -> bool:
        """Send a Null write (relinquish) to release a previously commanded value.

        This clears the specified priority level in the Priority Array, allowing
        lower-priority values (or the Relinquish Default) to take effect.
        """
        return await self.write_property(
            device_address=device_address,
            object_type=object_type,
            instance=instance,
            property_name="presentValue",
            value=None,  # Null = relinquish
            priority=priority,
        )

    # ------------------------------------------------------------------
    # COV (Change of Value) subscriptions
    # ------------------------------------------------------------------

    async def subscribe_cov(
        self,
        device_address: str,
        object_type: int,
        instance: int,
        callback: Callable[[dict[str, Any]], None],
        lifetime: int = 300,
    ) -> str | None:
        """Subscribe to Change of Value notifications for one object.

        If the device does not support COV or the subscription fails,
        returns None so the caller can fall back to polling.

        Args:
            device_address: Target device address.
            object_type: Object type integer.
            instance: Object instance number.
            callback: Callable invoked with {"property": value, …} on notification.
            lifetime: COV subscription lifetime in seconds. The subscription
                      needs to be renewed before it expires.

        Returns:
            A subscription key string on success, or None on failure.
        """
        if self._app is None:
            raise RuntimeError("Client not connected")

        addr = Address(device_address)
        type_str = self._int_to_object_type_str(object_type)
        oid = ObjectIdentifier((type_str, instance))
        sub_key = f"{device_address}:{object_type}:{instance}"

        try:
            _LOGGER.debug("Subscribing to COV for %s:%d at %s", type_str, instance, device_address)

            cov_context = await self._app.subscribe_cov(
                addr,
                oid,
                confirmed=True,
                lifetime=lifetime,
            )

            # Store callback and context for notification routing and renewal
            self._cov_callbacks[sub_key] = callback
            self._cov_contexts[sub_key] = {
                "address": addr,
                "oid": oid,
                "lifetime": lifetime,
                "context": cov_context,
            }

            _LOGGER.info("COV subscription active for %s:%d", type_str, instance)
            return sub_key

        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "COV subscription failed for %s:%d at %s: %s. "
                "Falling back to polling.",
                type_str,
                instance,
                device_address,
                exc,
            )
            return None

    async def unsubscribe_cov(self, sub_key: str) -> None:
        """Cancel a COV subscription."""
        ctx = self._cov_contexts.pop(sub_key, None)
        self._cov_callbacks.pop(sub_key, None)

        if ctx is not None and self._app is not None:
            try:
                await self._app.unsubscribe_cov(ctx["context"])
                _LOGGER.debug("COV unsubscribed: %s", sub_key)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("COV unsubscribe failed (ignored): %s", sub_key)

    async def renew_cov_subscriptions(self) -> None:
        """Renew all active COV subscriptions before they expire."""
        for sub_key, ctx in list(self._cov_contexts.items()):
            try:
                new_context = await self._app.subscribe_cov(
                    ctx["address"],
                    ctx["oid"],
                    confirmed=True,
                    lifetime=ctx["lifetime"],
                )
                ctx["context"] = new_context
                _LOGGER.debug("COV subscription renewed: %s", sub_key)
            except Exception:  # noqa: BLE001
                _LOGGER.warning("COV renewal failed for %s", sub_key)

    def handle_cov_notification(
        self, subscription_key: str, changed_values: dict[str, Any]
    ) -> None:
        """Route an incoming COV notification to the registered callback.

        Called by the coordinator when BACpypes3 delivers a notification.
        """
        callback = self._cov_callbacks.get(subscription_key)
        if callback is not None:
            callback(changed_values)
        else:
            _LOGGER.debug("No callback for COV key %s", subscription_key)

    # ------------------------------------------------------------------
    # Value conversion helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_value(value: Any) -> Any:
        """Convert a BACpypes3 value to a plain Python type for JSON storage."""
        if value is None:
            return None
        if isinstance(value, (int, float, str, bool)):
            return value
        if isinstance(value, Real):
            return float(value)
        if isinstance(value, Unsigned):
            return int(value)
        if isinstance(value, CharacterString):
            return str(value)
        # Fallback
        return str(value)

    @staticmethod
    def _python_to_bacnet_value(value: Any, object_type: int) -> Any:
        """Convert a Python value to the appropriate BACpypes3 type.

        The correct BACnet type depends on the object type:
        - Analog types → Real (float)
        - Binary types → Unsigned or enumeration
        - Multi-state types → Unsigned
        """
        if value is None:
            return Null()

        if object_type in {
            OBJECT_TYPE_ANALOG_INPUT,
            OBJECT_TYPE_ANALOG_OUTPUT,
            OBJECT_TYPE_ANALOG_VALUE,
        }:
            return Real(float(value))

        if object_type in {
            OBJECT_TYPE_BINARY_INPUT,
            OBJECT_TYPE_BINARY_OUTPUT,
            OBJECT_TYPE_BINARY_VALUE,
        }:
            # BACnet binary PV is an Enumerated type: 0=inactive, 1=active
            # Using Enumerated (not Unsigned) per ASHRAE 135 — strict devices
            # will reject Unsigned for BinaryPV properties.
            return Enumerated(int(bool(value)))

        if object_type in {
            OBJECT_TYPE_MULTI_STATE_INPUT,
            OBJECT_TYPE_MULTI_STATE_OUTPUT,
            OBJECT_TYPE_MULTI_STATE_VALUE,
        }:
            return Unsigned(int(value))

        # Generic fallback
        return Real(float(value))

    # ------------------------------------------------------------------
    # Object type string ↔ integer mapping
    # ------------------------------------------------------------------

    _TYPE_STR_TO_INT: dict[str, int] = {
        "analogInput": OBJECT_TYPE_ANALOG_INPUT,
        "analogOutput": OBJECT_TYPE_ANALOG_OUTPUT,
        "analogValue": OBJECT_TYPE_ANALOG_VALUE,
        "binaryInput": OBJECT_TYPE_BINARY_INPUT,
        "binaryOutput": OBJECT_TYPE_BINARY_OUTPUT,
        "binaryValue": OBJECT_TYPE_BINARY_VALUE,
        "multiStateInput": OBJECT_TYPE_MULTI_STATE_INPUT,
        "multiStateOutput": OBJECT_TYPE_MULTI_STATE_OUTPUT,
        "multiStateValue": OBJECT_TYPE_MULTI_STATE_VALUE,
    }

    _INT_TO_TYPE_STR: dict[int, str] = {v: k for k, v in _TYPE_STR_TO_INT.items()}

    @classmethod
    def _object_type_str_to_int(cls, type_str: str | int) -> int | None:
        """Convert BACpypes3 object type string to integer ID."""
        if isinstance(type_str, int):
            return type_str
        return cls._TYPE_STR_TO_INT.get(str(type_str))

    @classmethod
    def _int_to_object_type_str(cls, type_int: int) -> str:
        """Convert integer object type to BACpypes3 type string."""
        return cls._INT_TO_TYPE_STR.get(type_int, f"type-{type_int}")
