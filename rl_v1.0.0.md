## v1.0.0 — Initial Release

**BACnet/IP integration for Home Assistant** — full GUI setup, no YAML required.

### Highlights

- **Three-step config flow** — configure network, discover devices via Who-Is, select objects to import
- **9 BACnet object types** — Analog/Binary/Multi-State Input, Output, and Value
- **5 HA platforms** — sensor, binary_sensor, switch, number, climate
- **BBMD / Foreign Device Registration** — cross-subnet communication out of the box
- **COV subscriptions** with automatic polling fallback for devices that don't support Change of Value
- **Priority Array writes** (levels 1–16) with proper Null/Relinquish for override release
- **Dynamic domain mapping** — reassign any BACnet object to a different HA platform at runtime
- **Flexible entity naming** — choose between BACnet `objectName` or `description`
- **Options flow** — adjust COV, polling interval, naming, and domain mapping without reconfiguring

### Supported Object Types

| Object Type | Default Domain |
|---|---|
| Analog Input | sensor |
| Analog Output | number |
| Analog Value | sensor |
| Binary Input | binary_sensor |
| Binary Output | switch |
| Binary Value | switch |
| Multi-State Input | sensor |
| Multi-State Output | number |
| Multi-State Value | sensor |

### Requirements

- Home Assistant 2024.1.0+
- BACpypes3 0.0.99+ (installed automatically)
- Network access to BACnet/IP devices on UDP 47808

### Installation

Install via **HACS** (recommended) or copy `custom_components/bacnet` manually.

---

*Developed by [BRDC](https://brdc.nl)*
