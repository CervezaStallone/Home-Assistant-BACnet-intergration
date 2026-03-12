#!/usr/bin/env python3
"""
Virtueel BACnet Apparaat voor Test Doeleinden
=============================================

Dit script simuleert een BACnet/IP apparaat met diverse objecten waarvan
de waarden dynamisch veranderen, zodat je jouw BACnet-configuratie
(bijv. VOLTTRON BACnet auto-configuration) kunt testen.

Objecten:
  - Analog Input  (AI) : temperatuur, luchtvochtigheid, CO2, druk
  - Analog Output (AO) : setpoint temperatuur, ventilator snelheid
  - Analog Value  (AV) : energie totaal, PID output
  - Binary Input  (BI) : bezettingssensor, deursensor, raamcontact
  - Binary Output (BO) : verlichting, alarm
  - Binary Value  (BV) : nachtmodus, onderhoudsmodus

Gebruik:
  python virtual_bacnet_device.py

Configuratie via omgevingsvariabelen (of config.env):
  BACNET_DEVICE_ID    - Device instance nummer  (standaard: 599)
  BACNET_DEVICE_NAME  - Apparaatnaam            (standaard: VirtueleThermostaat)
  BACNET_ADDRESS      - IP/masker               (standaard: host)
  UPDATE_INTERVAL     - Updatefrequentie in sec (standaard: 5)
"""

import asyncio
import math
import os
import random
import socket
import time

from bacpypes3.ipv4.app import NormalApplication
from bacpypes3.local.device import DeviceObject
from bacpypes3.pdu import Address, IPv4Address, GlobalBroadcast
from bacpypes3.local.analog import (
    AnalogInputObject,
    AnalogOutputObject,
    AnalogValueObject,
)
from bacpypes3.local.binary import (
    BinaryInputObject,
    BinaryOutputObject,
    BinaryValueObject,
)
from bacpypes3.basetypes import EngineeringUnits, BinaryPV
from bacpypes3.apdu import IAmRequest


# ---------------------------------------------------------------------------
#   Configuratie uit omgevingsvariabelen
# ---------------------------------------------------------------------------
DEVICE_ID       = int(os.environ.get("BACNET_DEVICE_ID", "599"))
DEVICE_NAME     = os.environ.get("BACNET_DEVICE_NAME", "VirtueleThermostaat")
DEVICE_ADDRESS  = os.environ.get("BACNET_ADDRESS", "host")
BACNET_PORT     = int(os.environ.get("BACNET_PORT", "47808"))
UPDATE_INTERVAL = float(os.environ.get("UPDATE_INTERVAL", "5"))


# ---------------------------------------------------------------------------
#   Object definities
#   BACpypes3 gebruikt standaard string-namen voor object types
#   (conform ASHRAE 135 hyphenated conventie).
# ---------------------------------------------------------------------------
ANALOG_INPUTS = [
    {
        "id": ("analog-input", 0),
        "name": "ZoneTemperatuur",
        "value": 21.5,
        "units": "degreesCelsius",
        "description": "Ruimtetemperatuur zone 1",
    },
    {
        "id": ("analog-input", 1),
        "name": "Luchtvochtigheid",
        "value": 45.0,
        "units": "percentRelativeHumidity",
        "description": "Relatieve luchtvochtigheid zone 1",
    },
    {
        "id": ("analog-input", 2),
        "name": "CO2Concentratie",
        "value": 420.0,
        "units": "partsPerMillion",
        "description": "CO2 concentratie in ppm",
    },
    {
        "id": ("analog-input", 3),
        "name": "BuitenTemperatuur",
        "value": 8.0,
        "units": "degreesCelsius",
        "description": "Buitentemperatuur sensor",
    },
    {
        "id": ("analog-input", 4),
        "name": "Luchtdruk",
        "value": 1013.25,
        "units": "hectopascals",
        "description": "Atmosferische druk",
    },
]

ANALOG_OUTPUTS = [
    {
        "id": ("analog-output", 0),
        "name": "TemperatuurSetpoint",
        "value": 22.0,
        "units": "degreesCelsius",
        "description": "Gewenste temperatuur setpoint",
    },
    {
        "id": ("analog-output", 1),
        "name": "VentilatorSnelheid",
        "value": 50.0,
        "units": "percent",
        "description": "Ventilator snelheid percentage",
    },
]

ANALOG_VALUES = [
    {
        "id": ("analog-value", 0),
        "name": "EnergieTotaal",
        "value": 15234.5,
        "units": "kilowattHours",
        "description": "Totaal energieverbruik",
    },
    {
        "id": ("analog-value", 1),
        "name": "PIDOutput",
        "value": 0.0,
        "units": "percent",
        "description": "PID regelaar output",
    },
]

BINARY_INPUTS = [
    {
        "id": ("binary-input", 0),
        "name": "Bezettingssensor",
        "value": "active",
        "description": "Ruimte bezettingssensor",
    },
    {
        "id": ("binary-input", 1),
        "name": "Deursensor",
        "value": "inactive",
        "description": "Deur open/dicht sensor",
    },
    {
        "id": ("binary-input", 2),
        "name": "Raamcontact",
        "value": "inactive",
        "description": "Raam open/dicht contact",
    },
]

BINARY_OUTPUTS = [
    {
        "id": ("binary-output", 0),
        "name": "Verlichting",
        "value": "active",
        "description": "Verlichting aan/uit",
    },
    {
        "id": ("binary-output", 1),
        "name": "AlarmRelais",
        "value": "inactive",
        "description": "Alarm relais uitgang",
    },
]

BINARY_VALUES = [
    {
        "id": ("binary-value", 0),
        "name": "Nachtmodus",
        "value": "inactive",
        "description": "Nachtmodus actief",
    },
    {
        "id": ("binary-value", 1),
        "name": "Onderhoudsmodus",
        "value": "inactive",
        "description": "Onderhoudsmodus actief",
    },
]


# ---------------------------------------------------------------------------
#   Simulatie engine — waarden veranderen realistisch over tijd
# ---------------------------------------------------------------------------
class SimulationEngine:
    """Simuleert realistische sensorwaarden die veranderen over tijd."""

    def __init__(self, objects: dict):
        self.objects = objects
        self.start_time = time.time()
        self.energy_counter = 15234.5

    def _elapsed(self) -> float:
        return time.time() - self.start_time

    def _get(self, key):
        obj = self.objects.get(key)
        return obj.presentValue if obj else None

    def _set(self, key, value):
        obj = self.objects.get(key)
        if obj is not None:
            obj.presentValue = value

    def update(self):
        """Werk alle sensorwaarden bij met realistische simulatie."""
        t = self._elapsed()

        # === Analog Inputs ===

        # AI:0 — Zonetemperatuur: sinusvormig (dag/nacht) + ruis
        base_temp = 21.5 + 2.0 * math.sin(t / 60.0)
        noise = random.gauss(0, 0.3)
        self._set(("analog-input", 0), round(base_temp + noise, 2))

        # AI:1 — Luchtvochtigheid: invers gecorreleerd met temperatuur
        humidity = 55.0 - 0.8 * (base_temp - 20.0) + random.gauss(0, 1.5)
        humidity = max(20.0, min(90.0, humidity))
        self._set(("analog-input", 1), round(humidity, 1))

        # AI:2 — CO2: stijgt bij bezetting, daalt bij afwezigheid
        occ = self._get(("binary-input", 0))
        co2_current = self._get(("analog-input", 2)) or 420.0
        if occ is not None and str(occ) == "active":
            co2_new = co2_current + random.uniform(2, 8)
        else:
            co2_new = co2_current - random.uniform(5, 15)
        co2_new = max(380.0, min(2000.0, co2_new))
        self._set(("analog-input", 2), round(co2_new, 1))

        # AI:3 — Buitentemperatuur: langzamere cyclus
        outside = 8.0 + 5.0 * math.sin(t / 300.0) + random.gauss(0, 0.2)
        self._set(("analog-input", 3), round(outside, 2))

        # AI:4 — Luchtdruk: zeer langzame variatie
        pressure = 1013.25 + 5.0 * math.sin(t / 600.0) + random.gauss(0, 0.5)
        self._set(("analog-input", 4), round(pressure, 2))

        # === Analog Values ===

        # AV:0 — Energieteller: stijgt altijd
        power = random.uniform(0.5, 3.0)
        self.energy_counter += power * (UPDATE_INTERVAL / 3600.0)
        self._set(("analog-value", 0), round(self.energy_counter, 2))

        # AV:1 — PID output: gebaseerd op verschil setpoint vs. actueel
        setpoint = self._get(("analog-output", 0)) or 22.0
        current_temp = self._get(("analog-input", 0)) or 21.5
        error = float(setpoint) - float(current_temp)
        pid_output = max(0.0, min(100.0, 50.0 + error * 15.0))
        self._set(("analog-value", 1), round(pid_output, 1))

        # === Binary Inputs ===

        # BI:0 — Bezetting: wisselt af en toe (~2% kans per update)
        if random.random() < 0.02:
            current = self._get(("binary-input", 0))
            new_val = "inactive" if str(current) == "active" else "active"
            self._set(("binary-input", 0), BinaryPV(new_val))

        # BI:1 — Deursensor: korte pulsen
        if random.random() < 0.05:
            self._set(("binary-input", 1), BinaryPV("active"))
        elif random.random() < 0.15:
            self._set(("binary-input", 1), BinaryPV("inactive"))

        # BI:2 — Raamcontact: zelden
        if random.random() < 0.01:
            current = self._get(("binary-input", 2))
            new_val = "inactive" if str(current) == "active" else "active"
            self._set(("binary-input", 2), BinaryPV(new_val))

        # === Binary Values ===

        # BV:0 — Nachtmodus: cyclische demonstratie
        night = "active" if math.sin(t / 120.0) > 0.3 else "inactive"
        self._set(("binary-value", 0), BinaryPV(night))


# ---------------------------------------------------------------------------
#   Console status weergave
# ---------------------------------------------------------------------------
def print_status(objects: dict):
    """Druk de huidige waarden af naar de console."""
    print(f"\n{'─' * 60}")
    print(f"  Status update [{time.strftime('%H:%M:%S')}]")
    print(f"{'─' * 60}")

    labels = {
        ("analog-input", 0):  ("AI:0", "ZoneTemperatuur",     "°C"),
        ("analog-input", 1):  ("AI:1", "Luchtvochtigheid",    "%RH"),
        ("analog-input", 2):  ("AI:2", "CO2Concentratie",     "ppm"),
        ("analog-input", 3):  ("AI:3", "BuitenTemperatuur",   "°C"),
        ("analog-input", 4):  ("AI:4", "Luchtdruk",           "hPa"),
        ("analog-output", 0): ("AO:0", "TemperatuurSetpoint", "°C"),
        ("analog-output", 1): ("AO:1", "VentilatorSnelheid",  "%"),
        ("analog-value", 0):  ("AV:0", "EnergieTotaal",       "kWh"),
        ("analog-value", 1):  ("AV:1", "PIDOutput",           "%"),
        ("binary-input", 0):  ("BI:0", "Bezettingssensor",    ""),
        ("binary-input", 1):  ("BI:1", "Deursensor",          ""),
        ("binary-input", 2):  ("BI:2", "Raamcontact",         ""),
        ("binary-output", 0): ("BO:0", "Verlichting",         ""),
        ("binary-output", 1): ("BO:1", "AlarmRelais",         ""),
        ("binary-value", 0):  ("BV:0", "Nachtmodus",          ""),
        ("binary-value", 1):  ("BV:1", "Onderhoudsmodus",     ""),
    }

    for key, (tag, name, unit) in labels.items():
        obj = objects.get(key)
        if obj:
            pv = obj.presentValue
            # Ensure binary values are always 0/1
            if key[0].startswith("binary"):
                bin_val = int(pv) if hasattr(pv, '__int__') else (1 if str(pv).lower() == "active" else 0)
                label = "ACTIVE" if bin_val == 1 else "inactive"
                val_str = f"{label:>10s} ({bin_val})"
            elif isinstance(pv, (int, float)):
                val_str = f"{pv:>10.2f} {unit}"
            else:
                val_str = f"{str(pv):>10s}"
            print(f"  {tag:5s}  {name:<22s} = {val_str}")

    print(f"{'─' * 60}")


# ---------------------------------------------------------------------------
#   IP-adres detectie
# ---------------------------------------------------------------------------
def get_local_ip() -> str:
    """Detecteer het lokale IP-adres van deze machine."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_local_ip_with_mask() -> str:
    """Detecteer IP-adres met CIDR subnetmasker, bijv. '10.13.37.40/24'."""
    ip = get_local_ip()
    try:
        import subprocess
        result = subprocess.run(
            ["ip", "-o", "-f", "inet", "addr", "show"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split("\n"):
            if ip in line:
                # Formaat: "2: eth0  inet 10.13.37.40/24 brd ..."
                parts = line.split()
                for part in parts:
                    if ip in part and "/" in part:
                        return part  # bijv. "10.13.37.40/24"
    except Exception:
        pass
    return f"{ip}/24"  # fallback


# ---------------------------------------------------------------------------
#   Banner
# ---------------------------------------------------------------------------
def print_banner(local_ip: str):
    total = (len(ANALOG_INPUTS) + len(ANALOG_OUTPUTS) + len(ANALOG_VALUES) +
             len(BINARY_INPUTS) + len(BINARY_OUTPUTS) + len(BINARY_VALUES))

    print("=" * 78)
    print("  Virtueel BACnet Apparaat — Testomgeving")
    print("=" * 78)
    print(f"  Device Object ID (DOI) : device,{DEVICE_ID}")
    print(f"  Apparaat Naam          : {DEVICE_NAME}")
    print(f"  IP Adres               : {local_ip}")
    print(f"  BACnet/IP Poort        : {BACNET_PORT} (0x{BACNET_PORT:04X})")
    print(f"  Bereikbaar op          : {local_ip}:{BACNET_PORT}")
    print(f"  Update interval        : {UPDATE_INTERVAL}s")
    print("=" * 78)

    # Eenheden vertaaltabel (korte weergave)
    UNIT_SHORT = {
        "degreesCelsius": "°C",
        "percentRelativeHumidity": "%RH",
        "partsPerMillion": "ppm",
        "hectopascals": "hPa",
        "percent": "%",
        "kilowattHours": "kWh",
    }

    # --- Objecttabel ---
    print()
    hdr = f"  {'Type':<5s} {'Object ID':<20s} {'Naam':<24s} {'Waarde':>10s}  {'Eenheid':<6s} {'RW':<3s}"
    print(hdr)
    print(f"  {'─'*5} {'─'*20} {'─'*24} {'─'*10}  {'─'*6} {'─'*3}")

    all_cfgs = [
        (ANALOG_INPUTS,  "AI", True,  "R"),
        (ANALOG_OUTPUTS, "AO", True,  "R/W"),
        (ANALOG_VALUES,  "AV", True,  "R/W"),
        (BINARY_INPUTS,  "BI", False, "R"),
        (BINARY_OUTPUTS, "BO", False, "R/W"),
        (BINARY_VALUES,  "BV", False, "R/W"),
    ]

    for cfgs, tag, is_analog, rw in all_cfgs:
        for cfg in cfgs:
            oid = f"{cfg['id'][0]},{cfg['id'][1]}"
            if is_analog:
                val_str = f"{cfg['value']:>10g}"
                unit_str = UNIT_SHORT.get(cfg['units'], cfg['units'])
            else:
                val_str = f"{cfg['value']:>10s}"
                unit_str = "—"
            print(f"  {tag:<5s} {oid:<20s} {cfg['name']:<24s} {val_str}  {unit_str:<6s} {rw:<3s}")

    print(f"\n  Totaal: {total} objecten")
    print()

    # --- Snelle test commando's ---
    print("-" * 78)
    print("  Snelle test commando's (kopieer & plak):")
    print("-" * 78)
    print(f"  # Lees zonetemperatuur:")
    print(f"  python test_client.py {local_ip}")
    print()
    print(f"  # Who-Is (vind apparaten op netwerk):")
    print(f"  #   bacpypes3 WhoIs")
    print(f"  # Read Property (temperatuur):")
    print(f"  #   bacpypes3 ReadProperty {local_ip} analog-input,0 present-value")
    print(f"  # Write Property (setpoint naar 24°C):")
    print(f"  #   bacpypes3 WriteProperty {local_ip} analog-output,0 present-value 24.0")
    print("-" * 78)
    print("  Druk Ctrl+C om te stoppen")
    print("=" * 78)


# ---------------------------------------------------------------------------
#   Objecten aanmaken en toevoegen aan de applicatie
# ---------------------------------------------------------------------------
def create_objects(app: NormalApplication) -> dict:
    """Maak alle BACnet objecten aan en voeg ze toe. Geeft dict met referenties."""
    objects = {}

    for cfg in ANALOG_INPUTS:
        obj = AnalogInputObject(
            objectIdentifier=cfg["id"],
            objectName=cfg["name"],
            presentValue=float(cfg["value"]),
            units=EngineeringUnits(cfg["units"]),
            description=cfg["description"],
            statusFlags=[0, 0, 0, 0],
            eventState="normal",
            outOfService=False,
        )
        app.add_object(obj)
        objects[cfg["id"]] = obj

    for cfg in ANALOG_OUTPUTS:
        obj = AnalogOutputObject(
            objectIdentifier=cfg["id"],
            objectName=cfg["name"],
            presentValue=float(cfg["value"]),
            units=EngineeringUnits(cfg["units"]),
            description=cfg["description"],
            statusFlags=[0, 0, 0, 0],
            eventState="normal",
            outOfService=False,
        )
        app.add_object(obj)
        objects[cfg["id"]] = obj

    for cfg in ANALOG_VALUES:
        obj = AnalogValueObject(
            objectIdentifier=cfg["id"],
            objectName=cfg["name"],
            presentValue=float(cfg["value"]),
            units=EngineeringUnits(cfg["units"]),
            description=cfg["description"],
            statusFlags=[0, 0, 0, 0],
            eventState="normal",
            outOfService=False,
        )
        app.add_object(obj)
        objects[cfg["id"]] = obj

    for cfg in BINARY_INPUTS:
        bin_val = 1 if str(cfg["value"]).lower() == "active" else 0
        obj = BinaryInputObject(
            objectIdentifier=cfg["id"],
            objectName=cfg["name"],
            presentValue=bin_val,
            description=cfg["description"],
            statusFlags=[0, 0, 0, 0],
            eventState="normal",
            outOfService=False,
        )
        app.add_object(obj)
        objects[cfg["id"]] = obj

    for cfg in BINARY_OUTPUTS:
        bin_val = 1 if str(cfg["value"]).lower() == "active" else 0
        obj = BinaryOutputObject(
            objectIdentifier=cfg["id"],
            objectName=cfg["name"],
            presentValue=bin_val,
            description=cfg["description"],
            statusFlags=[0, 0, 0, 0],
            eventState="normal",
            outOfService=False,
        )
        app.add_object(obj)
        objects[cfg["id"]] = obj

    for cfg in BINARY_VALUES:
        bin_val = 1 if str(cfg["value"]).lower() == "active" else 0
        obj = BinaryValueObject(
            objectIdentifier=cfg["id"],
            objectName=cfg["name"],
            presentValue=bin_val,
            description=cfg["description"],
            statusFlags=[0, 0, 0, 0],
            eventState="normal",
            outOfService=False,
        )
        app.add_object(obj)
        objects[cfg["id"]] = obj

    return objects


# ---------------------------------------------------------------------------
#   Hoofd async loop
# ---------------------------------------------------------------------------
async def main():
    local_ip = get_local_ip()

    print_banner(local_ip)

    # Maak de BACnet applicatie via NormalApplication
    # (zelfde methode als de HA BACnet integratie gebruikt)
    local_addr = IPv4Address(f"{local_ip}:{BACNET_PORT}")
    device_object = DeviceObject(
        objectIdentifier=("device", DEVICE_ID),
        objectName=DEVICE_NAME,
        vendorIdentifier=999,
        maxApduLengthAccepted=1476,
        segmentationSupported="segmented-both",
    )

    print(f"[INFO] Maak NormalApplication op {local_addr}...")
    app = NormalApplication(device_object, local_addr)
    print(f"[INFO] BACnet applicatie gebonden op {local_addr}")

    # --- Monkey-patch BACnet handlers voor request logging ---
    _orig_whois = app.do_WhoIsRequest

    async def _logged_whois(apdu):
        print(f"\n[BACNET] >>> Who-Is ontvangen van {apdu.pduSource}",
              flush=True)
        await _orig_whois(apdu)
        print(f"[BACNET] <<< I-Am verstuurd naar {apdu.pduSource}",
              flush=True)

    app.do_WhoIsRequest = _logged_whois

    _orig_read = getattr(app, 'do_ReadPropertyRequest', None)
    if _orig_read:
        async def _logged_read(apdu):
            print(f"[BACNET] >>> ReadProperty van {apdu.pduSource}: "
                  f"{apdu.objectIdentifier} / {apdu.propertyIdentifier}",
                  flush=True)
            await _orig_read(apdu)
        app.do_ReadPropertyRequest = _logged_read

    _orig_iam = app.do_IAmRequest

    async def _logged_iam(apdu):
        print(f"[BACNET] >>> I-Am ontvangen van {apdu.pduSource}: "
              f"{apdu.iAmDeviceIdentifier}", flush=True)
        await _orig_iam(apdu)

    app.do_IAmRequest = _logged_iam

    # Voeg objecten toe
    objects = create_objects(app)
    print(f"\n[INFO] {len(objects)} objecten aangemaakt en geregistreerd.")

    # Wacht tot de netwerk-sockets klaar zijn voordat we I-Am sturen
    await asyncio.sleep(2)

    # Bepaal het subnet-broadcast adres voor I-Am aankondigingen
    # NormalApplication heeft geen broadcast socket, dus we sturen
    # een I-Am als unicast naar het subnet-broadcast adres
    subnet_broadcast = local_ip.rsplit(".", 1)[0] + ".255"
    broadcast_addr = Address(subnet_broadcast)

    def safe_i_am():
        """Stuur I-Am naar subnet-broadcast adres (unicast naar .255)."""
        app.i_am(address=broadcast_addr)

    # Stuur startup I-Am zodat het apparaat zichzelf aankondigt
    print("[INFO] Stuur startup I-Am aankondiging...")
    safe_i_am()
    await asyncio.sleep(3)
    safe_i_am()
    print(f"[INFO] I-Am verstuurd (device,{DEVICE_ID} op {local_ip})")

    # Start de simulatie engine
    sim = SimulationEngine(objects)

    print("[INFO] Virtueel BACnet apparaat draait. Wacht op BACnet verzoeken...")
    print(f"[INFO] Waarden worden elke {UPDATE_INTERVAL:.0f} seconden bijgewerkt.\n")

    # Periodieke update loop — stuur ook periodiek I-Am (elke ~60s)
    update_count = 0
    try:
        while True:
            await asyncio.sleep(UPDATE_INTERVAL)
            sim.update()
            update_count += 1

            # Stuur periodiek I-Am (elke ~60 seconden)
            # zodat nieuwe apparaten op het netwerk ons kunnen vinden
            if update_count % int(60 / UPDATE_INTERVAL) == 0:
                safe_i_am()

            # Print status elke 3e update
            if update_count % 3 == 0:
                print_status(objects)

    except asyncio.CancelledError:
        pass
    finally:
        print("\n[INFO] Afsluiten...")
        app.close()
        print("[INFO] Virtueel BACnet apparaat gestopt.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[INFO] Gestopt door gebruiker.")
