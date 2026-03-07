"""Constants for the BACnet IP integration."""

# ---------------------------------------------------------------------------
# Integration identifiers
# ---------------------------------------------------------------------------
DOMAIN = "bacnet"
MANUFACTURER = "BACnet"

# ---------------------------------------------------------------------------
# Configuration keys  (config_flow / options_flow)
# ---------------------------------------------------------------------------
# Network
CONF_LOCAL_IP = "local_ip"
CONF_LOCAL_PORT = "local_port"
CONF_BBMD_ADDRESS = "bbmd_address"
CONF_BBMD_TTL = "bbmd_ttl"
CONF_USE_BBMD = "use_bbmd"

# Discovery / device
CONF_DEVICE_ID = "device_id"
CONF_DEVICE_NAME = "device_name"
CONF_DEVICE_ADDRESS = "device_address"
CONF_TARGET_ADDRESS = "target_address"

# Object selection
CONF_SELECTED_OBJECTS = "selected_objects"
CONF_SELECT_ALL = "select_all"

# Naming
CONF_USE_DESCRIPTION = "use_description"

# COV & Polling
CONF_ENABLE_COV = "enable_cov"
CONF_POLLING_INTERVAL = "polling_interval"

# Domain mapping
CONF_DOMAIN_MAPPING = "domain_mapping"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_PORT = 47808  # 0xBAC0 — standard BACnet/IP port
DEFAULT_BBMD_TTL = 900  # seconds (15 min)
DEFAULT_POLLING_INTERVAL = 60  # seconds
DEFAULT_ENABLE_COV = True
DEFAULT_USE_DESCRIPTION = False

# ---------------------------------------------------------------------------
# BACnet object type IDs  (from ASHRAE 135)
# ---------------------------------------------------------------------------
OBJECT_TYPE_ANALOG_INPUT = 0
OBJECT_TYPE_ANALOG_OUTPUT = 1
OBJECT_TYPE_ANALOG_VALUE = 2
OBJECT_TYPE_BINARY_INPUT = 3
OBJECT_TYPE_BINARY_OUTPUT = 4
OBJECT_TYPE_BINARY_VALUE = 5
OBJECT_TYPE_MULTI_STATE_INPUT = 13
OBJECT_TYPE_MULTI_STATE_OUTPUT = 14
OBJECT_TYPE_MULTI_STATE_VALUE = 19

# Human-readable labels for BACnet object types
OBJECT_TYPE_NAMES: dict[int, str] = {
    OBJECT_TYPE_ANALOG_INPUT: "Analog Input",
    OBJECT_TYPE_ANALOG_OUTPUT: "Analog Output",
    OBJECT_TYPE_ANALOG_VALUE: "Analog Value",
    OBJECT_TYPE_BINARY_INPUT: "Binary Input",
    OBJECT_TYPE_BINARY_OUTPUT: "Binary Output",
    OBJECT_TYPE_BINARY_VALUE: "Binary Value",
    OBJECT_TYPE_MULTI_STATE_INPUT: "Multi-State Input",
    OBJECT_TYPE_MULTI_STATE_OUTPUT: "Multi-State Output",
    OBJECT_TYPE_MULTI_STATE_VALUE: "Multi-State Value",
}

# ---------------------------------------------------------------------------
# Supported HA domains  (for dynamic domain mapping)
# ---------------------------------------------------------------------------
SUPPORTED_DOMAINS: list[str] = [
    "sensor",
    "binary_sensor",
    "switch",
    "number",
    "climate",
]

# Default mapping: BACnet object type → HA domain
DEFAULT_DOMAIN_MAP: dict[int, str] = {
    OBJECT_TYPE_ANALOG_INPUT: "sensor",
    OBJECT_TYPE_ANALOG_OUTPUT: "number",
    OBJECT_TYPE_ANALOG_VALUE: "sensor",
    OBJECT_TYPE_BINARY_INPUT: "binary_sensor",
    OBJECT_TYPE_BINARY_OUTPUT: "switch",
    OBJECT_TYPE_BINARY_VALUE: "switch",
    OBJECT_TYPE_MULTI_STATE_INPUT: "sensor",
    OBJECT_TYPE_MULTI_STATE_OUTPUT: "number",
    OBJECT_TYPE_MULTI_STATE_VALUE: "sensor",
}

# ---------------------------------------------------------------------------
# BACnet property IDs  (commonly used subset)
# ---------------------------------------------------------------------------
PROP_OBJECT_NAME = 77
PROP_DESCRIPTION = 28
PROP_PRESENT_VALUE = 85
PROP_STATUS_FLAGS = 111
PROP_UNITS = 117
PROP_OBJECT_TYPE = 79
PROP_OBJECT_IDENTIFIER = 75
PROP_OBJECT_LIST = 76
PROP_PRIORITY_ARRAY = 87
PROP_RELINQUISH_DEFAULT = 104
PROP_OUT_OF_SERVICE = 81
PROP_POLARITY = 84

# ---------------------------------------------------------------------------
# Priority levels for BACnet writes (1-16, lower = higher priority)
# ---------------------------------------------------------------------------
DEFAULT_WRITE_PRIORITY = 16  # Lowest priority — safe default

# ---------------------------------------------------------------------------
# Data keys stored in hass.data[DOMAIN][entry_id]
# ---------------------------------------------------------------------------
DATA_CLIENT = "client"
DATA_COORDINATOR = "coordinator"
DATA_OBJECTS = "objects"
DATA_DEVICE_INFO = "device_info"
DATA_UNSUB = "unsub"

# ---------------------------------------------------------------------------
# Events / signals
# ---------------------------------------------------------------------------
SIGNAL_BACNET_COV_UPDATE = f"{DOMAIN}_cov_update"
