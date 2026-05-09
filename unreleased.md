## [Unreleased]

### Added

- **SolarFlow800Pro2 device class**: dedicated class with support for 4 independent solar inputs (`solar_power1`–`solar_power4`), replacing the generic fallback previously used for this model
- **zenSDK local discovery**: `Api.LocalDiscovery` queries `GET /properties/report` directly on the device — no cloud authentication required. Returns serial number and product model.
- **Token-free setup**: devices reachable via local IP can be configured without a Zendure cloud account; `Api.Connect()` validates the device before creating a config entry
- **Mixed cloud + local operation**: cloud MQTT credentials are preserved when a local device is merged into the device list — both connection modes work side by side

### Fixed

- `ApiHA`: empty `deviceList` from cloud no longer discards cloud MQTT credentials; the local device from `LocalDiscovery` is merged in while preserving MQTT config
- `Connect()` storage fallback now triggers on empty `deviceList`, not just on `None` — a response dict with `deviceList: []` is correctly treated as a cache miss
