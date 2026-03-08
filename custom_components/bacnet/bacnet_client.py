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
from typing import Any, Callable, Union

from bacpypes3.ipv4.app import ForeignApplication, NormalApplication
from bacpypes3.local.device import DeviceObject
from bacpypes3.pdu import Address, IPv4Address
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

# Type alias for the application — either Normal or Foreign
_AppType = Union[NormalApplication, ForeignApplication]


class BACnetClient:
    """Wrapper around BACpypes3 providing a clean async API for HA.

    Usage:
        client = BACnetClient(local_ip="192.168.1.100", local_port=47808)
        await client.connect()                       # NormalApplication
        await client.connect(bbmd_address="x.x.x.x") # ForeignApplication
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
        self._app: _AppType | None = None
        self._cov_tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _build_app_args(self) -> tuple[DeviceObject, IPv4Address]:
        """Prepare IPv4Address and device object for app construction."""
        if self._local_ip:
            local_addr = IPv4Address(f"{self._local_ip}:{self._local_port}")
        else:
            local_addr = IPv4Address(f"0.0.0.0:{self._local_port}")

        device_object = DeviceObject(
            objectIdentifier=("device", 999999),
            objectName="HomeAssistant-BACnet",
            vendorIdentifier=0,
            maxApduLengthAccepted=1476,
            segmentationSupported="segmented-both",
        )
        return device_object, local_addr

    async def connect(
        self,
        bbmd_address: str | None = None,
        bbmd_ttl: int = 900,
    ) -> None:
        """Create the BACpypes3 application and bind to the network.

        BACpypes3 uses asyncio UDP transport internally, so the application
        constructor MUST be called from an async context with a running
        event loop.

        If bbmd_address is provided, a ForeignApplication is created and
        registered with the BBMD automatically.  Otherwise a
        NormalApplication is created for local-subnet communication.

        Args:
            bbmd_address: IP:port of the BBMD for cross-subnet communication.
                          If None, no BBMD registration is performed.
            bbmd_ttl: Time-to-live for foreign device registration (seconds).
        """
        device_object, local_addr = self._build_app_args()

        if bbmd_address:
            _LOGGER.debug(
                "Creating Foreign BACnet application on %s (BBMD=%s)",
                local_addr,
                bbmd_address,
            )
            self._app = ForeignApplication(device_object, local_addr)
            bbmd_addr = IPv4Address(bbmd_address)
            self._app.register(bbmd_addr, bbmd_ttl)
            _LOGGER.info(
                "BACnet Foreign Device registered with BBMD at %s (TTL=%ds)",
                bbmd_address,
                bbmd_ttl,
            )
        else:
            _LOGGER.debug("Creating Normal BACnet application on %s", local_addr)
            self._app = NormalApplication(device_object, local_addr)
            _LOGGER.info("BACnet client connected on %s (type=%s)", local_addr, type(self._app).__name__)

        # Wait for the UDP transport to be ready.  The NormalApplication
        # constructor schedules UDP endpoint creation as background tasks.
        # If we don't await them here, the first who_is / read_property may
        # silently fail because the socket is not yet bound.
        try:
            await self._wait_for_transport()
        except Exception:
            # Transport failed — clean up the app so the port is released.
            try:
                self._app.close()
            except Exception:  # noqa: BLE001
                pass
            self._app = None
            raise

    def _get_datagram_server(self):
        """Return the IPv4DatagramServer from the application stack.

        NormalApplication stores it at ``app.normal.server``;
        ForeignApplication stores it at ``app.server``.
        """
        if self._app is None:
            return None
        # NormalApplication wraps it inside NormalLinkLayer
        if hasattr(self._app, "normal"):
            return getattr(self._app.normal, "server", None)
        # ForeignApplication exposes it directly
        return getattr(self._app, "server", None)

    async def _wait_for_transport(self, timeout: float = 5.0) -> None:
        """Await the UDP transport tasks so the socket is actually bound.

        BACpypes3 schedules ``create_datagram_endpoint`` as background tasks
        in the ``IPv4DatagramServer`` constructor.  If the requested port is
        already in use, ``retrying_create_datagram_endpoint`` keeps retrying
        forever — our timeout detects that and raises early.
        """
        server = self._get_datagram_server()
        if server is None:
            _LOGGER.warning("Cannot locate IPv4DatagramServer — skipping transport check")
            return

        tasks = getattr(server, "_transport_tasks", [])
        if tasks:
            _LOGGER.debug("Waiting up to %.0fs for UDP transport …", timeout)
            try:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)
                server._transport_tasks = []
            except asyncio.TimeoutError:
                _LOGGER.error(
                    "UDP socket failed to bind within %.0fs — port %d may "
                    "already be in use. Try a different 'Local port' (e.g. 47809).",
                    timeout,
                    self._local_port,
                )
                raise RuntimeError(
                    f"UDP port {self._local_port} could not be bound "
                    f"(already in use?). Choose a different local port."
                ) from None

        # Log the actual bound address
        transport = getattr(server, "local_transport", None)
        if transport is not None:
            sock = transport.get_extra_info("socket")
            if sock is not None:
                bound = sock.getsockname()
                _LOGGER.info(
                    "UDP transport ready — actually bound to %s:%s", bound[0], bound[1]
                )
            else:
                _LOGGER.debug("UDP transport ready (socket details unavailable)")
        else:
            _LOGGER.warning(
                "UDP transport is None after awaiting tasks — "
                "network communication will likely fail"
            )

    async def disconnect(self) -> None:
        """Shut down the BACpypes3 application and release the UDP socket."""
        # Cancel all COV tasks
        for task in self._cov_tasks.values():
            task.cancel()
        self._cov_tasks.clear()

        if self._app is not None:
            try:
                # close() is synchronous in BACpypes3
                self._app.close()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Exception during app close (ignored)")
            self._app = None
            _LOGGER.info("BACnet client disconnected")

    # ------------------------------------------------------------------
    # Device discovery - Who-Is / I-Am
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
            # who_is() returns a Future that resolves to a list of I-Am APDUs
            i_am_list = await self._app.who_is(timeout=timeout)

            for i_am in i_am_list:
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
                _LOGGER.debug(
                    "Discovered device: %s (%d) at %s",
                    device_name,
                    device_id,
                    i_am.pduSource,
                )
        except asyncio.TimeoutError:
            pass  # normal - discovery just timed out
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Error during Who-Is discovery: %s", exc)

        _LOGGER.info("Discovery complete: found %d device(s)", len(devices))
        return devices

    # ------------------------------------------------------------------
    # Manual device identification (unicast)
    # ------------------------------------------------------------------

    async def read_device_info(
        self,
        device_address: str,
        device_id: int | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any] | None:
        """Read device identity from a known IP address (unicast).

        Sends a directed Who-Is to a specific address, or falls back to
        reading the Device object directly.  When *device_id* is provided
        the Who-Is uses low/high limits and the fallback reads that
        specific Device object instead of guessing common IDs.

        Returns a dict compatible with the discovery result format:
            {"device_id": int, "device_name": str, "address": str}

        Returns None if the device does not respond within *timeout* seconds.
        """
        try:
            return await asyncio.wait_for(
                self._read_device_info_inner(device_address, device_id),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Timeout (%.0fs) reaching device at %s", timeout, device_address
            )
            return None

    async def _read_device_info_inner(
        self, device_address: str, known_device_id: int | None = None
    ) -> dict[str, Any] | None:
        """Internal implementation of read_device_info (no outer timeout)."""
        if self._app is None:
            raise RuntimeError("Client not connected")

        addr = Address(device_address)
        _LOGGER.debug(
            "read_device_info: address=%s (parsed=%r), known_id=%s, app=%s",
            device_address,
            addr,
            known_device_id,
            type(self._app).__name__,
        )

        # Verify transport is live
        server = self._get_datagram_server()
        if server is not None:
            transport = getattr(server, "local_transport", None)
            if transport is None:
                _LOGGER.error(
                    "UDP transport is not ready — cannot send BACnet packets. "
                    "Port %d may already be in use on this host.",
                    self._local_port,
                )
                return None
            sock = transport.get_extra_info("socket")
            if sock is not None:
                _LOGGER.debug("UDP socket bound to %s", sock.getsockname())
        else:
            _LOGGER.warning("Cannot locate IPv4DatagramServer for transport check")

        # Strategy 1: directed Who-Is → I-Am
        try:
            who_is_kwargs: dict[str, Any] = {"address": addr, "timeout": 5}
            if known_device_id is not None:
                who_is_kwargs["low_limit"] = known_device_id
                who_is_kwargs["high_limit"] = known_device_id
            _LOGGER.debug("Strategy 1: Sending directed Who-Is %s", who_is_kwargs)
            i_am_list = await self._app.who_is(**who_is_kwargs)
            _LOGGER.debug("Who-Is returned %d I-Am(s)", len(i_am_list) if i_am_list else 0)
            if i_am_list:
                i_am = i_am_list[0]
                device_id = i_am.iAmDeviceIdentifier[1]
                device_name = f"Device {device_id}"
                try:
                    name = await asyncio.wait_for(
                        self._app.read_property(
                            addr,
                            ObjectIdentifier(("device", device_id)),
                            "objectName",
                        ),
                        timeout=3,
                    )
                    if name:
                        device_name = str(name)
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    _LOGGER.debug("Could not read objectName, using default")
                return {
                    "device_id": device_id,
                    "device_name": device_name,
                    "address": device_address,
                }
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "Strategy 1 (Who-Is) failed for %s: %s (%s)",
                device_address,
                exc,
                type(exc).__name__,
            )

        # Strategy 2: read the Device object directly via ReadProperty (unicast)
        ids_to_try: list[int]
        if known_device_id is not None:
            ids_to_try = [known_device_id]
        else:
            ids_to_try = [1, 0, 2, 100, 1000]

        _LOGGER.debug("Strategy 2: Trying ReadProperty for device IDs %s", ids_to_try)
        for test_id in ids_to_try:
            try:
                oid = ObjectIdentifier(("device", test_id))
                _LOGGER.debug(
                    "  Trying ReadProperty %s objectIdentifier from %s ...",
                    oid,
                    device_address,
                )
                obj_id = await asyncio.wait_for(
                    self._app.read_property(addr, oid, "objectIdentifier"),
                    timeout=3,
                )
                _LOGGER.debug("  ReadProperty returned: %s", obj_id)
                if obj_id is not None:
                    device_id = obj_id[1]
                    device_name = f"Device {device_id}"
                    try:
                        name = await asyncio.wait_for(
                            self._app.read_property(addr, oid, "objectName"),
                            timeout=3,
                        )
                        if name:
                            device_name = str(name)
                    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                        _LOGGER.debug("  Could not read objectName")
                    return {
                        "device_id": device_id,
                        "device_name": device_name,
                        "address": device_address,
                    }
            except asyncio.TimeoutError:
                _LOGGER.debug("  ReadProperty timeout for device,%d", test_id)
                continue
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug(
                    "  ReadProperty error for device,%d: %s (%s)",
                    test_id,
                    exc,
                    type(exc).__name__,
                )
                continue

        _LOGGER.warning(
            "Could not identify device at %s. "
            "Verify: (1) the device is on the same subnet or reachable via BBMD, "
            "(2) UDP port 47808 is not blocked by a firewall, "
            "(3) if Home Assistant runs in Docker, use --network=host, "
            "(4) try a different 'Local port' (e.g. 47809) in case port %d is "
            "already in use on this host.",
            device_address,
            self._local_port,
        )
        return None

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
                # Try to read priority array - if it exists the object is commandable
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
                "Failed to read metadata for %s:%d - %s", oid, instance, exc
            )
            return None

    async def _safe_read(
        self, addr: Address, oid: ObjectIdentifier, prop_name: str
    ) -> Any | None:
        """Read a single property, returning None on any error or timeout."""
        try:
            return await asyncio.wait_for(
                self._app.read_property(addr, oid, prop_name),
                timeout=5,
            )
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
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
        - To relinquish a commanded value (release the override), write Null
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
        is_commandable = object_type in (COMMANDABLE_TYPES | POTENTIALLY_WRITABLE_TYPES)

        # Convert None -> Null for relinquish
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
        callback: Callable[[str, dict[str, Any]], None],
        lifetime: int = 300,
    ) -> str | None:
        """Subscribe to Change of Value notifications for one object.

        BACpypes3 COV uses an async context manager (change_of_value)
        that keeps a queue of incoming property-value notifications.  We
        start a long-running task that reads from the queue and invokes
        callback for each notification.

        If the device does not support COV or the subscription fails,
        returns None so the caller can fall back to polling.

        Args:
            device_address: Target device address.
            object_type: Object type integer.
            instance: Object instance number.
            callback: callback(obj_key, {"presentValue": v, ...})
                      invoked on each COV notification.
            lifetime: COV subscription lifetime in seconds.  BACpypes3
                      automatically renews the subscription before it
                      expires when using the context manager.

        Returns:
            A subscription key string on success, or None on failure.
        """
        if self._app is None:
            raise RuntimeError("Client not connected")

        addr = IPv4Address(device_address)
        type_str = self._int_to_object_type_str(object_type)
        oid = ObjectIdentifier((type_str, instance))
        sub_key = f"{device_address}:{object_type}:{instance}"
        obj_key = f"{object_type}:{instance}"

        try:
            _LOGGER.debug(
                "Subscribing to COV for %s:%d at %s", type_str, instance, device_address
            )

            # change_of_value() returns a SubscriptionContextManager.
            # We run it inside a long-lived task so the context stays open
            # and the subscription is automatically renewed by BACpypes3.
            task = asyncio.create_task(
                self._cov_reader_task(
                    addr, oid, lifetime, sub_key, obj_key, callback
                )
            )
            self._cov_tasks[sub_key] = task

            # Give the task a moment to start and send the SubscribeCOV request.
            # If the device rejects instantly we will know.
            await asyncio.sleep(0.5)
            if task.done():
                exc = task.exception()
                if exc:
                    raise exc  # noqa: TRY301
                # Task finished immediately without error - unlikely but handle it
                _LOGGER.warning("COV task for %s ended immediately", sub_key)
                self._cov_tasks.pop(sub_key, None)
                return None

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
            self._cov_tasks.pop(sub_key, None)
            return None

    async def _cov_reader_task(
        self,
        addr: IPv4Address,
        oid: ObjectIdentifier,
        lifetime: int,
        sub_key: str,
        obj_key: str,
        callback: Callable[[str, dict[str, Any]], None],
    ) -> None:
        """Long-running task that reads from a COV subscription queue.

        Uses the BACpypes3 change_of_value() async context manager.
        The context manager handles subscription, renewal, and
        unsubscription automatically.
        """
        try:
            scm = self._app.change_of_value(
                addr,
                oid,
                lifetime=lifetime,
            )
            async with scm:
                while True:
                    prop_id, value = await scm.get_value()
                    prop_name = str(prop_id)
                    coerced = self._coerce_value(value)
                    _LOGGER.debug(
                        "COV notification %s: %s = %s", sub_key, prop_name, coerced
                    )
                    try:
                        callback(obj_key, {prop_name: coerced})
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("Error in COV callback for %s", sub_key)
        except asyncio.CancelledError:
            _LOGGER.debug("COV task cancelled for %s", sub_key)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("COV task ended for %s", sub_key, exc_info=True)

    async def unsubscribe_cov(self, sub_key: str) -> None:
        """Cancel a COV subscription by cancelling its reader task.

        The async-with context manager will send the unsubscribe
        request when the task is cancelled.
        """
        task = self._cov_tasks.pop(sub_key, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            _LOGGER.debug("COV unsubscribed: %s", sub_key)

    async def unsubscribe_all_cov(self) -> None:
        """Cancel all COV subscriptions."""
        for sub_key in list(self._cov_tasks):
            await self.unsubscribe_cov(sub_key)

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
        - Analog types -> Real (float)
        - Binary types -> Unsigned or enumeration
        - Multi-state types -> Unsigned
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
            # Using Enumerated (not Unsigned) per ASHRAE 135 - strict devices
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
    # Object type string - integer mapping
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
