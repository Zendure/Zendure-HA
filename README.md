<p align="center">
  <img src="https://zendure.com/cdn/shop/files/zendure-logo-infinity-charge_240x.png?v=1717728038" alt="Logo">
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

- **Configuration:**
  - [Fuse Group](https://github.com/Zendure/Zendure-HA/wiki/Fuse-Group) 
  - Zendure Manager
    - [Power distribution strategy](https://github.com/Zendure/Zendure-HA/wiki/Power-distribution-strategy)
  - [Local Mqtt (Legacy devices)](https://github.com/Zendure/Zendure-HA/wiki/Local-Mqtt-(Legacy-Devices))
  - Home Assistent Energy Dashboard

- **Supported devices:**
  - Ace1500
  - Aio2400
  - Hyper2000
  - Hub1200 [German](https://github.com/Zendure/Zendure-HA/wiki/SolarFlow-Hub1200-German)
  - Hub2000
  - [SF800](https://github.com/Zendure/Zendure-HA/wiki/SolarFlow-800)
  - SF800 Pro
  - SF800 Plus
  - SF2400 AC
  - SuperBase V6400

- **Device Automation:**
  - Cheap hours.

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
