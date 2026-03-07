[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=CervezaStallone&repository=Home-Assistant-BACnet-intergration&category=integration)

# BACnet IP Integration for Home Assistant

A production-ready [Home Assistant](https://www.home-assistant.io/) custom integration that brings full **BACnet/IP** support to your smart-home or building-automation setup. Built on top of [BACpypes3](https://github.com/JoelBender/BACpypes3) — the modern, async-first Python BACnet stack.

> **HACS Compatible** · GUI-only setup · No YAML required · ASHRAE 135 compliant writes

---

## Key Features

| Category | What you get |
|----------|-------------|
| **Zero-YAML Setup** | Three-step GUI config flow: network → discover → select objects. Options flow for runtime tuning. |
| **Device Discovery** | Automatic **Who-Is / I-Am** broadcast discovers every BACnet device on the network. |
| **BBMD & Foreign Device** | Register as a Foreign Device with a BBMD to reach devices across VLANs, subnets, and routed BACnet networks. |
| **COV + Polling** | Subscribe to **Change of Value** notifications for instant updates. Devices that reject COV automatically fall back to configurable polling. |
| **Priority Array Writes** | Writes go through the correct priority level (1–16). Turn-off sends a **Null / Relinquish** to release overrides cleanly. |
| **Dynamic Domain Mapping** | Override the default HA domain per object at any time — move a temperature setpoint from `sensor` to `climate`, or a binary value from `switch` to `binary_sensor`. |
| **Flexible Naming** | Choose between BACnet `objectName` (property 77) or `description` (property 28) for entity display names. |
| **Selective Import** | Import only the objects you need, or click **Select All** for everything. |

---

## Supported BACnet Object Types

| BACnet Object Type | ASHRAE 135 ID | Default HA Domain | Priority Array |
|----|:---:|----|----|
| Analog Input | 0 | `sensor` | — |
| Analog Output | 1 | `number` | Yes |
| Analog Value | 2 | `sensor` | Optional¹ |
| Binary Input | 3 | `binary_sensor` | — |
| Binary Output | 4 | `switch` | Yes |
| Binary Value | 5 | `switch` | Optional¹ |
| Multi-State Input | 13 | `sensor` | — |
| Multi-State Output | 14 | `number` | Yes |
| Multi-State Value | 19 | `sensor` | Optional¹ |

¹ Value objects may or may not be commandable — the integration auto-detects this by probing the Priority Array during discovery.

---

## Installation

### HACS (Recommended)

1. Click the **My Home Assistant** badge above, or:
   - Open HACS → **Integrations** → **⋮ (three dots)** → **Custom repositories**
   - Paste the repository URL and select category **Integration**
2. Click **Download** / **Install**
3. **Restart** Home Assistant

### Manual

```bash
# From this repo root
cp -r custom_components/bacnet <HA_CONFIG>/custom_components/bacnet
```

Restart Home Assistant after copying.

---

## Quick Start

### Step 1 — Network Configuration

1. **Settings → Devices & Services → Add Integration → BACnet IP**
2. Enter your network settings:

| Field | Description | Default |
|-------|-------------|---------|
| Local IP Address | Leave empty for auto-detect, or enter a specific NIC address | *(auto)* |
| Local Port | BACnet/IP UDP port | `47808` (0xBAC0) |
| Enable BBMD | Check if devices are on a different subnet | `No` |
| BBMD Address | IP:port of the BBMD router | — |
| BBMD TTL | Foreign device registration lifetime (seconds) | `900` |

### Step 2 — Device Discovery

The integration sends a **Who-Is** broadcast and lists all responding devices.
Select the device you want to integrate.

### Step 3 — Object Selection

The integration reads the device's Object List and shows all supported objects
with their names, types, and current values. Use **Select All** or pick individual objects.

Click **Submit** — entities are created and data starts flowing.

---

## Runtime Options

Click **Configure** on the BACnet integration card to access:

| Option | Description | Default |
|--------|-------------|---------|
| Enable COV | Use Change of Value subscriptions (event-driven updates) | `On` |
| Polling Interval | Fallback polling rate in seconds | `60` |
| Use Description | Show BACnet `description` instead of `objectName` as entity name | `Off` |
| Domain Mapping | Per-object HA domain override (sensor / binary_sensor / switch / number / climate) | Auto |

Changes take effect immediately — the integration reloads automatically.

---

## Architecture

```
custom_components/bacnet/
├── __init__.py          # Entry setup & teardown lifecycle
├── config_flow.py       # 3-step GUI configuration wizard
├── options_flow.py      # Runtime options (COV, polling, naming, domain map)
├── bacnet_client.py     # BACpypes3 wrapper — all network I/O
├── coordinator.py       # DataUpdateCoordinator — COV + polling engine
├── entity.py            # Base CoordinatorEntity with BACnet metadata
├── sensor.py            # Analog/multi-state → HA sensor
├── binary_sensor.py     # Binary input → HA binary sensor
├── switch.py            # Binary output/value → HA switch (with relinquish)
├── number.py            # Analog/multi-state output → HA number
├── climate.py           # Setpoint → HA climate with HEAT/OFF modes
├── const.py             # All constants, defaults, object type maps
├── manifest.json        # HA integration manifest
├── strings.json         # UI strings (English)
└── translations/
    └── en.json          # English translations
```

### Data Flow

```
BACnet Device
  ↕  UDP/IP (port 47808)
BACnetClient (bacnet_client.py)
  ↕  Who-Is  │  ReadProperty  │  WriteProperty  │  SubscribeCOV
BACnetCoordinator (coordinator.py)
  ↕  async_set_updated_data()
CoordinatorEntity subclasses (sensor / switch / number / …)
  ↕
Home Assistant state machine & frontend
```

---

## BACnet Write Behaviour

### Priority Array

All writes to commandable objects use the **Priority Array** (BACnet standard, ASHRAE 135):

- **Turn ON / Set Value** → writes at priority level 16 (lowest, safe default)
- **Turn OFF / Relinquish** → writes **Null** at the same priority level, clearing the override and letting lower-priority commands or the Relinquish Default take effect

The write priority is configurable per entity in future releases.

### Binary Present Value

Binary outputs use the `Enumerated` BACnet application type (`0 = inactive`, `1 = active`), not `Unsigned`. This complies with ASHRAE 135 and avoids write rejections on strict devices.

---

## Requirements

- **Home Assistant** 2024.1.0 or newer
- **Python** 3.11+
- **Network** UDP port 47808 accessible to BACnet devices
- **Cross-subnet** A BBMD or BACnet router on the target network

---

## Troubleshooting

| Symptom | Likely Cause | Solution |
|---------|-------------|----------|
| "No devices found" | Simulator/device not running or wrong subnet | Verify device is reachable on UDP 47808 |
| "Cannot connect" | Port 47808 already bound | Stop other BACnet applications or change the port |
| Entities show "Unavailable" | Device went offline | Restart the device; entities recover automatically |
| COV not working | Device doesn't support COV | Expected — polling fallback activates automatically |
| Write has no effect | Object is not commandable | Check `bacnet_commandable` extra attribute |
| Toggle OFF doesn't change value | Relinquish Default takes effect | This is correct BACnet behaviour — the RD value replaces the override |

Enable debug logging for detailed diagnostics:

```yaml
logger:
  logs:
    custom_components.bacnet: debug
```

---

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-improvement`)
3. Submit a Pull Request

---

## License

GPL-3.0 — see [LICENSE](LICENSE) for details.

---

## Credits

- Developed by **[BRDC](https://brdc.nl)**
- [BACpypes3](https://github.com/JoelBender/BACpypes3) by Joel Bender — the Python BACnet stack powering this integration
- [Home Assistant](https://www.home-assistant.io/) — the open-source home automation platform
- ASHRAE Standard 135 — the BACnet protocol specification
