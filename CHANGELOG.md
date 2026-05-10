# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **mDNS/Zeroconf auto-discovery**: Zendure devices are now detected automatically on the local network via `_http._tcp.local.` (confirmed) and `_zendure._tcp.local.` (speculative). A confirmation dialog shows the device model and serial number — retrieved directly from the device via `/properties/report` — and lets the user verify or override the pre-filled IP address before connecting. A progress spinner is shown while the connection is established. If the connection fails, the form reappears with an error and the IP can be corrected.

- **Manual mDNS scan**: The "Add integration" flow now opens with a choice: **Search automatically (mDNS)** or **Enter details manually**. Choosing "Search automatically" shows a progress spinner while the integration scans the network for Zendure devices; if a device is found, the confirmation dialog is pre-filled with its IP and serial number. This is useful when HA has not yet received an automatic discovery event (e.g. shortly after installation or after a reboot).

  > **Note for Kubernetes/Docker users:** mDNS multicast only reaches Home Assistant if it has a network interface in the same subnet as the Zendure device. You can verify whether HA sees the device under **Settings → System → Network → Network Discovery → Zeroconf Browser**. If the device appears there, auto-discovery will work. On Kubernetes with Multus, attach an IoT network interface to the HA pod — no additional configuration is required. Without a network interface in the device's subnet, auto-discovery will not work but manual setup via IP address still works.

### Fixed

- `doCommand` for zenSDK devices now uses `httpPost` (like `entityWrite`) instead of MQTT discovery topics — fixes commands being silently ignored by the device
- Auto-MQTT setup in mixed cloud+zenSDK setups no longer picks the wrong serial number; `find_zensdk_sn` matches by device IP, not list index
- MQTT disconnect handler now clears `device.mqtt` so zenSDK polling via `httpGet` resumes correctly after a disconnect
- Token-free setup now validates the device via `Api.Connect()` before creating a config entry — an unreachable `device_ip` no longer silently creates a broken entry
- `ApiHA`: empty `deviceList` from cloud no longer discards cloud MQTT credentials; the local device from `LocalDiscovery` is merged in while preserving MQTT config
- `Connect()` storage fallback now triggers on empty `deviceList`, not just on `None` — a response dict with `deviceList: []` is correctly treated as a cache miss
- Battery sub-devices (e.g. AB2000) are now registered and their entities created on the **first** `packData` MQTT message — previously a newly created battery skipped `entityUpdate` due to an `elif` branch, leaving it invisible in HA for up to 60 s (until the next poll cycle). This was most noticeable after a brief WiFi outage at startup when the initial `httpGet` failed.

## [1.4.0] - 2026-05-08

### Added

- `Api.LocalDiscovery()` — fallback device discovery via local zenSDK HTTP API (`GET /properties/report`) when cloud returns empty `deviceList`
- Optional `device_ip` field in config flow UI to enable local discovery
- **Token-free local setup**: when only `device_ip` is set (no token), the integration skips the cloud entirely and uses local zenSDK discovery only; `Api.Connect()` validates the device before creating a config entry
- **Mixed device setup**: when `device_ip` is set alongside a token, cloud discovery (legacy devices: Hyper 2000, SF 800 Pro, SF 2400 AC) and local zenSDK discovery are merged automatically — deduplicated by serial number
- **Local MQTT auto-setup**: `auto_mqtt_setup` checkbox calls `HA.Mqtt.SetConfig` RPC automatically during setup — no manual broker configuration required
- **Real-time push via local MQTT**: zenSDK devices push state updates immediately over the local broker; integration subscribes to `Zendure/+/{SN}/#` — no more 60 s HTTP polling when MQTT is connected
- zenSDK write commands now publish to `Zendure/{type}/{SN}/{property}/set` via MQTT instead of HTTP POST when MQTT is active
- `SolarFlow800Pro2` device class with `solarPower3`/`solarPower4` sensors (4 solar inputs vs 2 on Pro)
- `SolarFlow800Pro` now explicitly defines `solarPower1`/`solarPower2` with correct metadata (W, power, measurement)
- pytest suite covering local discovery, device mapping, API fallback, mixed scenario, ZenSdkMqttSetup, setStatus zenSDK order, fuseGrp guard, and Manual Power idle commanding — runs without Home Assistant installed
- Translation labels for `device_ip` and `auto_mqtt_setup` fields in all 4 languages (en, de, fr, nl)

### Fixed

- `Api.Init()` crashed with `KeyError: 'clientId'` when cloud returns empty `mqtt: {}` — now guarded
- `Api.ApiHA()` read `device_ip` from API response instead of user config (shadowed `data` variable)
- `ApiHA`: empty `deviceList` from cloud no longer discards cloud MQTT credentials; the local device from `LocalDiscovery` is merged in while preserving MQTT config
- `Connect()` storage fallback now triggers on empty `deviceList`, not just on `None` — a response dict with `deviceList: []` is correctly treated as a cache miss
- Token-free setup now validates the device via `Api.Connect()` before creating a config entry — an unreachable `device_ip` no longer silently creates a broken entry
- `ZendureZenSdk.dataRefresh()` skips HTTP polling when local MQTT is connected; falls back to HTTP only when MQTT is unavailable
- zenSDK MQTT message handler: `asyncio` and `timedelta` imports added (caused NameError on first message)
- zenSDK MQTT message handler: string values `ON`/`OFF`/`yes`/`no`/`heating`/`not_heating` mapped to int before entity update
- zenSDK MQTT entity updates run via `run_coroutine_threadsafe` to avoid event loop threading errors
- `setStatus()` `fuseGroup` check now correctly precedes the zenSDK online check — `fuseGroup=0` ("unused") is the intentional mechanism to disable a device in a multi-device group
- `powerChanged()` crashed with `AttributeError: fuseGrp` for devices not assigned to a FuseGroup — now falls back to device limits directly
- Config entry migration now advances existing installations to the current schema version instead of leaving them at `1.5` — prevents repeated migration attempts on subsequent startups
- `doCommand` for zenSDK devices now uses `httpPost` (like `entityWrite`) instead of MQTT discovery topics — fixes commands being silently ignored by the device
- Auto-MQTT setup in mixed cloud+zenSDK setups no longer picks the wrong serial number; `find_zensdk_sn` matches by device IP, not list index
- MQTT disconnect handler now clears `device.mqtt` so zenSDK polling via `httpGet` resumes correctly after a disconnect
- `ZenSdkMqttSetup`: config flow now shows an error when MQTT setup fails instead of silently creating a broken entry
- Missing `productKey` field in `LocalDiscovery` response caused `KeyError` on device init
- Added `"solarflow800pro2"` / `"solarflow 800 pro2"` → `SolarFlow800Pro2` mapping in `createdevice`
- `ZendureManager.powerChanged()`: idle devices now receive Manual Power commands (charge/discharge) — fixes "on/off flapping" when device reports 0W output
- `SolarFlow800Pro` was used as stand-in for Pro 2 — replaced with dedicated `SolarFlow800Pro2` class
- Legacy cloud & zenSDK: discovery support restored
- Local discovery: device connect fixed

## [1.3.1] - 2026-04-28

### Fixed

- Fix total_kwh `state_class` and silence empty-sentinel warning
- Correct error: `ZendureNumber` object has no attribute `asInt`
- Correct power start value for SolarFlow 2400 AC+

## [1.3.0] - 2026-04-27

### Added

- SolarFlow 800 Pro 2: added to device mapping
- Off-grid devices: review fixes for PR #1288
- Add randomness to start power
- Add internal battery support for SF1600AC+ and SF2400ACPro
- Add total kWh sensor to expose installed battery capacity

### Fixed

- Fix `state_class` of energy sensors
- Remove binary sensor `pass` state from entity attributes
- Change byPass to ZendureSensor and update logic
- Security: opt-in MQTT user creation and enforce `local_only=True`
- Fix division by zero in `power_charge` / `power_discharge`
- Fix discharge state display when min SOC limit is reached
- Fix: prevent setpoint sign flip for SOCFULL devices (issue #1151)
- Fix migration error
- Fix entity renaming and helper source patching
- Refactor entity ID and unique ID handling during migration
- Fix entity_id after HA 2026.2 update
- Fix invalid entity_ids / entities with `_2` suffix
- Refactor property name conversion in `entityWrite` for camelCase writing

### Changed

- Async migration and reload handling
- Logging: use lazy %-formatting in all `_LOGGER` calls
- Device ID migration, custom snakecase, and code review fixes
- Swap and rename battery models for SF2400AC+ internal battery support
- Rename `MATCHING_CHARGE_BAT` to `STORE_SOLAR` and update translations
- Update hub1200/hub2000/aio2400 charge limits
- Update FR / NL / DE translations

## [1.2.6] - 2026-03-05

### Added

- Smart Charge: `MATCHING_CHARGE_BAT` state with translations (DE/FR/NL/EN)
- Add idle mode for positive setpoint in MATCHING_CHARGE
- Add `model_id` to device
- Add possibility to delete a device
- SolarFlow 1600: device integration
- SolarFlow 2400 Pro: dedicated device class, correct charge limits

### Fixed

- Fix condition to check operation mode in MATCHING_CHARGE
- Fix aggrOffGrid sensor type from `total_increasing` to `total`
- Fix reconnect for local-only devices
- Update discharge handling to prevent grid power usage
- Fix invalid entity ID error in HA 2026.02
- Fix timeout for zenSDK HTTP communication
- Fix aggrCharge not updating when battery heating is active

### Changed

- Entity ID migration: automations, scripts, dashboards, and templates updated
- Improve entity ID / unique ID change detection logic
- Enhance device renaming to include entry domain and helper source patching
- Update HA 2026.2 compatibility

## [1.2.5] - 2026-02-02

### Fixed

- Fix discharge state when min SOC limit is reached
- Fix division by zero in power distribution
- Migrate manager, fix snakecase errors
- Fix idle device startup power
- Fix battery naming for AB2000X (internal and external)
- Fix SolarFlow 2400 AC+ charge limits
- Fix correct initialization order

### Changed

- Device renaming: rename device and update entity IDs in one step
- Async migration to `async_migrate_entry`
- Improve migration logging and entry handling
- Update signed var translations

## [1.2.4] - 2026-01-16

### Fixed

- Fix invalid entity ID (HA 2026.02 regression)
- Fix entities with `_2` suffix after migration
- Fix reconnect handling for local-only devices
- Fix ClientTimeout for zenSDK HTTP

### Changed

- Refactor battery type retrieval in `ZendureBattery`
- Update device state keys in migration repairs

## [1.2.3] - 2026-01-16

### Fixed

- Fix aggrOffGrid sensor type from `total_increasing` to `total`
- Fix timeout for ClientSession in zenSDK HTTP communication
- Bump ruff to 0.14.11–0.14.14

## [1.2.2] - 2026-01-06

### Fixed

- Fix Hyper satellite plug handling
- Fix charging devices with off-grid power
- Improve API error handling
- ACE 1500: add missing entities
- Fix invalid entity ID generation

### Changed

- Allow discharge in smart charge when solar input is available

## [1.2.1] - 2025-12-28

### Added

- AC & DC switch support
- Default values for select entities (autoHeat default: 1)
- ZenSDK devices: full integration

### Fixed

- Fix setpoint when consuming power from OffGrid socket (issue #1015)
- Fix property name conversion

### Changed

- Allow discharge in smart charge when solar input is available

## [1.2.0] - 2025-12-26

### Added

- Smart Charge mode with multi-language translations (DE/FR/NL/EN)
- Total kWh sensor to expose installed battery capacity
- Bypass cycling fix (issue #1162)
- SF 2400 AC+ correct charge limits for SF 2400 Pro

### Fixed

- Fix SOC-full discharge-bypass cycling
- Update hub2000.py charge and discharge limits
- Update hub1200.py charge limits
- Combine SF 800 family into one file

### Changed

- Refactor translation key handling to use camelCase for writing properties
- Refactor `entity.py` for improved readability
- Async migration and reload handling
