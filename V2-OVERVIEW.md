# Zendure HA v2: overview for contributors

v2 is a rewrite of the integration around three ideas: one config entry with a subentry per device,
a strict split between the fleet regulator and the vendor code, and a device contract that is generic
enough that a second vendor could be added without touching the regulator.

This document is a map of the branch. It is not a user guide.

## What changed at the top level

| 1.x | v2 |
| --- | --- |
| `manager.py` (fleet + wiring + P1) | `coordinator.py` (wiring) + `distribution.py` (fleet) |
| `api.py` (cloud calls) | `zendure/driver.py` |
| `devices/*.py` (one file per model) | `zendure/solarflow.py` + the `PRODUCTS` table |
| `fusegroup.py` | gone, see [Fuse groups are gone](#fuse-groups-are-gone) |
| `device.py` (Zendure specifics) | `device.py` (generic contract) + `zendure/device.py` (Zendure) |
| devices configured in the entry | devices as config subentries |
| translations only | `strings.json` plus translations |

## Fuse groups are gone

1.x let the user define fuse groups: a shared circuit with a combined maximum, over which the
integration divided the available power itself. v2 does not have them and will not get them back.

The limits set in the Zendure app are what counts. They are recorded there under a signed form,
following the national regulations that apply to the installation. The integration uses the device
with those settings and does not compute a combined maximum load over several devices itself. Under
those regulations it may not, and it is potentially dangerous.

The device's app limit is therefore already the ceiling, and it reaches the code by itself:
`inverseMaxPower` from the telemetry sets `power_profile[0].power`, the floor and the bands follow
from it, and `power()` clamps on it.

Two limits do remain, and neither is a fuse group:

- **The phase guard.** `max_kw` is the main connection, divided over the three phases. `phase_need`
  per phase is what that phase's own devices must still deliver to stay under it, and `update_power()`
  honours it before the plan. This is the house connection, not a per-circuit group.
- **The output cap.** `max_output` limits what the whole fleet delivers to the house. Germany allows
  800 W, and the per-device app limit does not cover that: three devices each set to 800 W make 2400 W.

Decided 2026-09-13.

## Layout

```
custom_components/zendure_ha/
  __init__.py        setup/unload/migrate, nothing else
  coordinator.py     the one object per entry: driver + distribution + devices + store + P1 tracking
  distribution.py    the fleet regulator (vendor agnostic)
  device.py          DistributionDevice: the contract every power device offers the regulator
  entity.py          EntityDevice + HaEntity base, naming, registry lookup
  config_flow.py     one instance, settings in sections, a subentry flow per device
  migration.py       1.x entry, devices and entity ids onto the v2 shape
  const.py           domain, Phase, watt_factor
  sensor.py number.py select.py switch.py button.py binary_sensor.py time.py
  zendure/           everything vendor specific
    driver.py        MQTT (local + cloud), discovery, telemetry routing, PRODUCTS
    device.py        Zendure identity, HA registry entry, BLE transport
    solarflow.py     the battery devices (+ ZendureLegacy, ZendureHub)
    battery.py       the packs
    smartmeter.py    the meters
```

The rule the layout enforces: `distribution.py` never imports from `zendure/`, and `zendure/` never
decides how power is shared. Everything they exchange goes through `DistributionDevice`.

## Config entry and subentries

One entry (single instance, `VERSION = 2`). Its `data` holds one section per object: `distribution`
and `zendure`. Everything else is a subentry:

- one `distribution` subentry, so the fleet controls get their own device card,
- one `zendure` subentry per device, keyed by its serial number `sn`; `prodkey` and `device_id` are kept only for the MQTT topics, alongside `ip`, `blemac` and the pack names.

`Coordinator.sync_subentries()` mirrors the subentries into live devices: what is new gets built by the
driver, what is gone gets dropped. Adding a device is therefore never a reload, it is a subentry write.
Discovery uses the same path: the driver calls `on_discovered`, the coordinator writes a subentry, HA's
update listener builds the device.

A device's name is computed once, by `unique_name(family, sn, taken)`: the family from the `PRODUCTS`
table plus the shortest free serial tail, for example `SF2400_7a`. Every `unique_id` and entity id is
derived from that name, so a newcomer never renames an existing device.

## The device contract

`DistributionDevice` (in `device.py`) is all the regulator knows about a device:

- `state` (`DeviceState`: CREATED, OFFLINE, EXTERNAL, SENT, CONFIRM, ACTIVE) and `state_since`,
- `soc`, `soc_min`, `soc_max`, `soc_limit` (the device's own "full"/"empty" verdict), `kwh`, `temp`,
- `solar`, `bypass`, `grid_off_power`,
- `power_setpoint`, `power_actual`, `power_since`, `power_phase`,
- `power_profile`: a `ProfileDischarge` and a `ProfileCharge`, each with a rated power and the floor
  (10 percent) and band (30 to 95 percent) that follow from it,
- `power(watts)` and `power_off()`.

The vendor implements `power()` and keeps every field truthful. `ProfileDischarge.available_power()`
also carries the thermal derating, and `available_kwh()` turns SoC into energy, so the regulator never
has to know why a device cannot deliver.

## The regulator

`Distribution._process_p1()` runs on every P1 reading. In order:

1. **Telemetry.** Fuse load per phase, the fleet's own output (a bypassing device's pass-through counts
   as house baseline, the off-grid socket as house load), and the display sensors.
2. **Setpoint.** `p1 + fleet output + correction`, then the mode (off, manual, smart, smart charging,
   smart discharging, store solar) and the regulatory output cap.
3. **Stabilization.** A running estimate of movement and variance picks a regime: `quiet`, `solar` or
   `cycle`. Quiet needs two samples outside the noise band before it moves, solar closes a share of the
   error per tick, cycle jumps straight to the new level on a flank. The tuned constants sit at the top
   of the file with the measurement they came from.
4. **Active set.** `update_active()` sizes the number of running devices against an optimal zone
   (30 to 60 percent of the active set's rated power) with a watt-second integral on both edges, so a
   short excursion does not start or stop a device.
5. **Distribution.** `update_power()` gives every active device its share. Below the summed floors
   everyone drops pro rata. Above them the device with the most kWh per watt of headroom is served
   first, which makes a single pass exact. A device that has not reported since its last command is
   skipped, but only for 10 seconds, so one silent device cannot freeze the fleet.
6. **Fuse guard.** A phase over its limit gets the difference from its own devices, before anything is sent.

One daily job runs off the recorder: `update_wear()` computes 7-day throughput per device (feeding the
order score) and the RTE sensor. Which phase a device sits on is no longer guessed daily: it is a
per-device `power_phase` select, and a `detect_phase` button runs the regression of the device's own
power steps against the per-phase meters once, on demand.

`update_order()` reranks the devices per direction every 10 minutes and is frozen in between, so the
ordering cannot flap.

## Transports and discovery

A device can be reached four ways, tracked in `ConnectionType`: local MQTT, cloud MQTT, the local HTTP
API (zenSDK) and BLE. A command goes over the broker when the device is heard there, and over the device
loop otherwise; the dispatch sits at the three call sites that send, there is no longer a single
`_send()`. `device_loop()` keeps one task per device alive, falls back through `cmd_recover()` when a
device goes quiet and backs off to 300 seconds.

Commands differ per family (see [Commanding the devices](#commanding-the-devices)): SolarFlow writes its
limits, `ZendureHyper` drives `hemsEP`, `ZendureLegacy` (AIO 2400, Ace 1500) uses `deviceAutomation` and
`ZendureHub` a bare number.

Discovery has three entries, all ending in the same `discover()`:

- the cloud device list from the app token,
- mDNS: the device is asked who it is over `http://<ip>/properties/report`,
- Bluetooth: a `Zen*` stranger is asked over the BLESPP handshake, once per address per run.

`PRODUCTS` is the single table mapping a product to its class, model name, naming family and rated
powers; `PRODKEYS` is derived from it to resolve the cloud's product key to a product. Adding a model is
one row, not a file.

A 3CT smart meter is created like any device, discovered on its `deviceSn`; `smartmeter.py` reports its
total power as the sum of its three phase readings.

**Cloud bridge.** A device that runs on the local broker is invisible to the Zendure app. The driver
logs in to the cloud as that device, forwards the app's requests to the local broker and, for 5 minutes
after a request, forwards its reports back. Bridged copies are marked `isHA` so they are not re-read.

## Commanding the devices

The fleet is regulated on every P1 reading, so a command can reach a device several times a minute. The
command path differs per family:

- **SolarFlow and the rest.** A setpoint is a `properties/write` of `_limits(watts)`: `smartMode`,
  `acMode` and the `outputLimit`/`inputLimit` pair.
- **The Hyper.** `ZendureHyper` (a subclass of `ZendureSolarFlow`) drives `hemsEP`: `hemsState=1` once,
  then `function/invoke hemsEP` with `freq=0` to apply at once.
- **Taking a local device over (zenSDK).** A device with a known ip is driven over its local HTTP API
  (`ConnectionType.ZENSDK`) and stays there once it answers, while its broker reports are still read.
  `cmd_zensdk()` sends the same limits over HTTP; a failed poll drops it back to OFFLINE. The poll
  interval is 2 s.

## Entities

Entities are declared, not written by hand. `entity_create()` lists the ones a device always has, and
the `SENSORS` table creates the rest on the first report that carries them, so a device only gets the
entities its model and wiring actually produce. `entity_update(key, value)` routes an incoming vendor
key to its entity and creates it if needed. Restorable states and `power_phase` live in one HA `Store`
(`zendure_ha.devices`), dumped every 15 minutes and on unload.

## Migration from 1.x

`async_migrate_entry` handles `version == 1`. `migration.py` moves the settings into sections, turns the
configured devices into subentries, and maps every old translation key onto its new display key
(`DEVICE_KEYS`, `BATTERY_KEYS`), removing the entities that have no successor. Devices that were members
of the main entry are taken out of it so they only belong to their subentry.

If you change an entity key, add the old name to `DEVICE_KEYS`. Dropping one without a mapping orphans
the user's history.

## Tests

```bash
pip install -r requirements.txt -r requirements_test.txt
pytest
```

`tests/` covers setup, config flow, coordinator, migration, naming, registry lookup, the bridge, the
SolarFlow device, translations and the distribution itself.

`tests/replay/` is the interesting one. A recording is a JSON file with the P1 series and every device's
power, solar and SoC, exported from a live install with the **export replay** button (24 hours out of
the recorder into `www/`). `replay.py` runs that recording through the real `Distribution` with a device
model that reproduces the measured behavior: 3 seconds of dead time, 5 extra seconds when a device was
cold, a 5 second report cadence, the SENT state and an optional follow factor for a device that delivers
less than it is asked.

`benchmarks.json` pins the meter figures per recording (`total_wh`, `median`, `p90`, `within100`). The
assertion is one-sided: worse on any measure fails, and far better also fails, with a note to re-pin so
a later regression cannot hide under a stale number.

```bash
pytest tests/replay -s            # measure
REPLAY_REPIN=1 pytest tests/replay -s   # measure and write the pins back
```

Any change to `distribution.py` should be judged on these numbers, not on reasoning alone.

## Conventions

- `ruff format` and `ruff check --fix` through `scripts/lint`.
- Code on one line where it fits, no column alignment.
- Comments in English, short, and they explain intent or carry a measurement, not what the line does.
- A tuned constant sits at the top of its module with the measurement that produced it.
- No helper that has a single call site.
