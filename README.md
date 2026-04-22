<p align="center">
  <img src="https://zendure.com/cdn/shop/files/logo.svg" alt="Logo">
</p>

# Zendure Home Assistant Integration
This Home Assistant integration connects your Zendure devices to Home Assistant, making all reported parameters available as entities. You can track battery levels, power input/output, manage charging settings, and integrate your Zendure devices into your home automation routines. The integration also provides a power manager feature that can help balance energy usage across multiple devices when a P1 meter entity is supplied.


[![hacs][hacsbadge]][hacs] [![releasebadge]][release] [![License][license-shield]](LICENSE.md) [![hainstall][hainstallbadge]][hainstall]

## Overview

- **[Installation and ZendureApp Token](https://github.com/Zendure/Zendure-HA/wiki/Installation)**
  - [Troubleshooting Hyper2000](https://github.com/Zendure/Zendure-HA/wiki/Troubleshooting)
  - Tutorials
    - [Domotica & IoT](https://iotdomotica.nl/tutorial/install-zendure-home-assistant-integration-tutorial) 🇬🇧
    - [twoenter blog](https://www.twoenter.nl/blog/en/smarthome-en/zendure-home-battery-home-assistant-integration/) 🇬🇧 or [twoenter blog](https://www.twoenter.nl/blog/home-assistant-nl/zendure-thuisaccu-integratie-met-home-assistant/) 🇳🇱
    - [@Kieft-C](https://github.com/Kieft-C/Zendure-BKW-PV/wiki/Installation-Zendure-Home-Assistant-integration-%E2%80%93-Tutorial) 🇩🇪
  - Troubleshooting with few general hints
    - [Kieft-C](https://github.com/Kieft-C/Zendure-BKW-PV/wiki/Zendure-HA-integration-%E2%80%93-Troubleshoot-&-Mini-Anleitung) 🇩🇪

- **Configuration:**
  - [Fuse Group](https://github.com/Zendure/Zendure-HA/wiki/Fuse-Group)
  - Zendure Manager
    - [Power distribution strategy](https://github.com/Zendure/Zendure-HA/wiki/Power-distribution-strategy)
  - [Local Mqtt (Legacy devices)](https://github.com/Zendure/Zendure-HA/wiki/Local-Mqtt-(Legacy-Devices))
  - Home Assistant Energy Dashboard

- **Supported devices:**
  - Ace1500
  - Aio2400
  - Hyper2000
  - Hub1200 [German](https://github.com/Zendure/Zendure-HA/wiki/SolarFlow-Hub1200-German)
  - Hub2000
  - [SF800](https://github.com/Zendure/Zendure-HA/wiki/SolarFlow-800)
  - SF800 Pro
  - SF800 Plus
  - SF1600 AC+
  - SF2400 AC
  - SF2400 AC+
  - SF2400 Pro
  - SuperBase V6400 (?)
  - SuperBase V4600 not yet supported using the token

- **Device Automation:**
  - Cheap hours.
  - [Example: Zero-Export Power Distribution](#example-zero-export-power-distribution)

## Minimum Requirements
- [Home Assistant](https://github.com/home-assistant/core) 2025.5+

## Installation

### HACS (Home Assistant Community Store)

To install via HACS:

1. Navigate to HACS -> Integrations -> "+ Explore & Download Repos".
2. Search for "Zendure".
3. Click on the result and select "Download this Repository with HACS".
4. Refresh your browser (due to a known HA bug that may not update the integration list immediately).
5. Go to "Settings" in the Home Assistant sidebar, then select "Devices and Services".
6. Click the blue [+ Add Integration] button at the bottom right, search for "Zendure", and install it.

   [![Set up a new integration in Home Assistant](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=zendure_ha)


## Example: Zero-Export Power Distribution

This automation adjusts a SolarFlow 800 Pro's output limit every 5 seconds to match household consumption (read from an external smart meter), while protecting LiFePO4 battery lifetime with a configurable low-SoC taper.

**How it works**
- Triggers on a 5-second rolling average of grid power (positive = importing from grid)
- Incrementally adjusts `output_limit`: `new = current_limit + grid − feed_buffer`
- Below `soc_minimum`: output forced to 0
- Between `soc_minimum` and `soc_minimum + 10%`: linear taper from 0 to full
- Above: full match to consumption
- Clamped to `[0, maximum_inverter_power]`

**Tunable knobs**
- `feed_buffer` (default 40 W) — how much the automation deliberately *undershoots* household demand. A higher value means more sustained grid import but a safer margin against export spikes between updates. Lower if your meter and load are stable, raise if you still see feed-in.
- `dead_band` (default 10 W) — the minimum change in `output_limit` needed to trigger an API call. Skipping small adjustments reduces chatter to the Zendure cloud/device. If the SoC is at or below `soc_minimum`, the dead band is bypassed (emergency shutdown always fires).

**Required sensors/entities** (replace the `<...>` placeholders with your actual entity IDs)
- `sensor.<GRID_POWER_SENSOR>` — your smart-meter reading of grid power, ideally a short rolling average (positive = import, negative = export)
- `sensor.<ZENDURE_DEVICE>_battery_soc` — battery state of charge (`electricLevel`)
- `number.<ZENDURE_DEVICE>_soc_minimum` — SoC lower bound (`minSoc`)
- `number.<ZENDURE_DEVICE>_output_limit` — the inverter output limit (`outputLimit`)
- `sensor.<ZENDURE_DEVICE>_maximum_inverter_power` — max inverter output (`inverseMaxPower`)

Before enabling the automation, make sure the integration's **Operation Mode** is set to `Manual Power` — otherwise the integration's own smart logic will fight the automation. Paste the YAML below into the automation's **Edit in YAML** view (replace the whole body), then search-and-replace the two placeholders.

```yaml
alias: Power Distribution
mode: single
triggers:
  - trigger: state
    entity_id: sensor.<GRID_POWER_SENSOR>
conditions:
  - condition: not
    conditions:
      - condition: state
        entity_id: sensor.<GRID_POWER_SENSOR>
        state:
          - unavailable
          - unknown
  - condition: not
    conditions:
      - condition: state
        entity_id: sensor.<ZENDURE_DEVICE>_battery_soc
        state:
          - unavailable
          - unknown
  - condition: not
    conditions:
      - condition: state
        entity_id: number.<ZENDURE_DEVICE>_output_limit
        state:
          - unavailable
          - unknown
actions:
  - variables:
      feed_buffer: 40
      dead_band: 10
      grid: "{{ states('sensor.<GRID_POWER_SENSOR>') | float(0) }}"
      soc: "{{ states('sensor.<ZENDURE_DEVICE>_battery_soc') | float(0) }}"
      soc_min: "{{ states('number.<ZENDURE_DEVICE>_soc_minimum') | float(10) }}"
      current_limit: "{{ states('number.<ZENDURE_DEVICE>_output_limit') | float(0) }}"
      max_power: "{{ states('sensor.<ZENDURE_DEVICE>_maximum_inverter_power') | float(800) }}"
      raw_target: "{{ current_limit + grid - feed_buffer }}"
      taper_zone: "{{ soc_min + 10 }}"
      target: >-
        {% if soc <= soc_min %} 0
        {% elif soc <= taper_zone %} {{ (raw_target | float) * ((soc - soc_min) / 10) }}
        {% else %} {{ raw_target }}
        {% endif %}
      clamped: "{{ [[target | float, 0] | max, max_power] | min | round(0) }}"
  - condition: template
    value_template: "{{ clamped | float != current_limit and (soc <= soc_min or (clamped | float - current_limit) | abs > dead_band) }}"
  - action: number.set_value
    target:
      entity_id: number.<ZENDURE_DEVICE>_output_limit
    data:
      value: "{{ clamped }}"
```

## Contributing

Contributions are welcome! If you're interested in contributing, please review our [Contribution Guidelines](CONTRIBUTING.md) before submitting a pull request or issue.

## Support

If you find this project helpful and want to support its development, consider buying me a coffee!
[![Buy Me a Coffee][buymecoffeebadge]][buymecoffee]

---

[buymecoffee]: https://www.buymeacoffee.com/fireson
[buymecoffeebadge]: https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png
[license-shield]: https://img.shields.io/github/license/zendure/zendure-ha.svg?style=for-the-badge
[hacs]: https://github.com/zendure/zendure-ha
[hacsbadge]: https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge
[release]: https://github.com/zendure/zendure-ha/releases
[releasebadge]: https://img.shields.io/github/v/release/zendure/zendure-ha?style=for-the-badge
[buildstatus-shield]: https://img.shields.io/github/actions/workflow/status/zendure/zendure-ha/push.yml?branch=main&style=for-the-badge
[buildstatus-link]: https://github.com/zendure/zendure-ha/actions

[hainstall]: https://my.home-assistant.io/redirect/config_flow_start/?domain=zendure_ha
[hainstallbadge]: https://img.shields.io/badge/dynamic/json?style=for-the-badge&logo=home-assistant&logoColor=ccc&label=usage&suffix=%20installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/custom_integrations.json&query=$.zendure_ha.total


## License

MIT License
