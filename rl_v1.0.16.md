# Release v1.0.16

## New Features

### Automatische device-identificatie vanuit BACnet

Tijdens discovery worden nu de volgende BACnet properties automatisch
uitgelezen van het Device Object:

| BACnet Property              | HA Device Registry |
|------------------------------|--------------------|
| `vendorName` (property 121)  | Manufacturer       |
| `modelName` (property 70)    | Model              |
| `applicationSoftwareVersion` | Software version   |
| `firmwareRevision`           | Firmware version   |

Hierdoor toont het HA apparaatregister de werkelijke fabrikant, model en
versie-informatie van het BACnet device — in plaats van het generieke
"BACnet" / "BACnet Device 599".

### Bewerkbare apparaatnaam tijdens setup

In de **"Select BACnet Objects"** stap van de config flow staat nu
bovenaan een tekstveld **"Device name"**, vooraf ingevuld met de BACnet
`objectName` van het device. De gebruiker kan dit aanpassen voordat de
config entry wordt aangemaakt.

Zowel de integratietitel als de apparaatnaam in het HA device registry
gebruiken de gekozen naam.

### COV- en update-informatie zichtbaar per entity

Elke entity toont nu in de extra state attributes:

| Attribuut                | Beschrijving                                      |
|--------------------------|---------------------------------------------------|
| `bacnet_update_method`   | `COV` of `polling` — hoe het object wordt bijgewerkt |
| `bacnet_cov_increment`   | Geconfigureerde COV-drempelwaarde (alleen bij analoge objecten met actief COV) |

Hiermee is direct zichtbaar welk update-mechanisme per object actief is
en wat de ingestelde gevoeligheid is.

---

## Bug Fixes

### Fix: waarden updaten niet in HA ondanks wijziging op device

Objecten met een actieve COV-subscriptie werden voorheen **uitgesloten
van polling**. Als een device de COV-subscription accepteert maar nooit
daadwerkelijk notificaties stuurt, werden die objecten nooit meer
bijgewerkt in HA.

**Oplossing:**
Alle objecten worden nu **altijd** gepoll (elke 30 seconden) als
betrouwbare baseline. COV-subscripties bieden nog steeds snellere
tussentijdse updates, maar polling garandeert dat waarden altijd
bijwerken — ongeacht of het device correct COV-notificaties verstuurt.

---

## Overig

### README herschreven

De README is volledig herschreven met focus op gebruiksvriendelijkheid:
- Toegankelijke intro in gewone taal
- Stap-voor-stap setup-instructies
- Nieuwe features gedocumenteerd (COV increment, device naming, vendor
  info, update method attributes)
- Entity attributes tabel met alle extra state attributes
- Device information sectie
- Troubleshooting tabel
