# Zendure Integration
![image](https://github.com/user-attachments/assets/393fec2b-af03-4876-a2d3-3bb3111de1d0)

## Compatible Devices

| Device |
|--------|
| Hyper 2000 |
| SolarFlow 800 |
| SolarFlow 2400 AC |
| ACE 1500 |
| AIO 2400 |
| Hub 1200 |
| Hub 2000 |

## What This Integration Does

This Home Assistant integration connects your Zendure power stations and energy storage devices to your smart home system. Once configured, it allows you to monitor and control your Zendure devices directly from Home Assistant. You can track battery levels, power input/output, manage charging settings, and integrate your Zendure devices into your home automation routines. The integration also provides a power manager feature that can help balance energy usage across multiple devices without requiring a seperate Shelly or P1 meter.

### How It Works

The integration works by connecting to the Zendure cloud API using your Zendure account credentials. After authentication, it automatically discovers all Zendure devices linked to your account and makes them available in Home Assistant. The integration uses MQTT to then get updates from Zendure cloud to update the relevant entities in Home assistant.

### Installation using HACS

You can also find a tutorial here: [Domotica & IoT](https://iotdomotica.nl/tutorial/install-zendure-home-assistant-integration-tutorial)

Preferable way to install this custom integration is to use [HACS](https://www.hacs.xyz/). Learn how to install HACS [here](https://www.hacs.xyz/docs/use/download/download).
After you have successfully installed and configured HACS you can simply press this button to add this repository to HACS and proceed to `Zendure Home Assistant Integration` installation.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=FireSon&repository=Zendure-HA&category=integration)

## Configuration options

![image]

It is strongly recommended to create a 2nd Zendure account for this integration to avoid being logged out of the app. To do this:
- Signout of the zendure app (or use a 2nd device/spouse for this if available)
- Register with a secondary e-mail (tip, for gmail you can use <youraddress>+zendure@gmail.com which will just end up in your own inbox)
- After setting up and activating the secondary account logout of it and back into your primary account
- Go to Profile > Device Sharing and setup a share for your 2nd account
- Logout of primary, into secondary
- Accept the request.

Now that this is completed use the 2nd account for the setup of the integration.

### Smart Matching Sensor Configuration

For the smart matching feature to work properly, you need to configure a power sensor that:

- Reports values in Watts (W)
- Reports negative values when there is excess energy (e.g., from solar production)
- Reports positive values when the house is drawing power from the grid

If your existing power meter sensor doesn't meet these requirements, you can create a template sensor to convert the values appropriately (see below).

#### Example: Converting DSMR Integration Values

If you're using the DSMR integration which reports values in kilowatts (kW) as separate "delivered" and "returned" sensors, you can create a template to combine and convert them to the required format:

```yaml
{{ (states("sensor.dsmr_reading_electricity_currently_delivered") | float - states("sensor.dsmr_reading_electricity_currently_returned") | float) * 1000 }}
```

This template:
1. Takes the currently delivered electricity value (positive when consuming from grid)
2. Subtracts the currently returned electricity value (positive when sending to grid)
3. Multiplies by 1000 to convert from kW to W

#### Setting Up a Template Sensor

You can set this up as a Helper in Home Assistant:

1. Go to Settings → Devices & Services → Helpers
2. Click "Add Helper" and select "Template"
3. Choose "Sensor" as the template type
4. Enter the template code above
5. Configure the name, icon, and unit of measurement (W)
6. Save the helper

For more information on template sensors, see the [Home Assistant Template documentation](https://www.home-assistant.io/integrations/template/).

## Telemetry
All the properties which the devices are reporting, are automatically added to HA.

### Exposed Sensors

Exposed sensors/controls can vary based on the device type.

| Sensor | Description | Unit | Device Class |
|--------|-------------|------|-------------|
| Electric Level | Current battery level | % | battery |
| Solar Input Power | Power input from solar panels | W | power |
| Pack Input Power | Power input to the battery pack | W | power |
| Output Pack Power | Power output from the battery pack | W | power |
| Output Home Power | Power output to home/devices | W | power |
| Grid Input Power | Power input from the grid | W | power |
| Remain Out Time | Estimated time remaining for discharge | h/min | duration |
| Remain Input Time | Estimated time remaining for full charge | h/min | duration |
| Pack Num | Number of battery packs connected | - | - |
| Pack State | Current state of the battery pack (Sleeping/Charging/Discharging) | - | - |
| Auto Model | Current operation mode | - | - |
| AC Mode | Current AC mode (input/output) | - | - |
| Hyper Temperature | Device temperature | °C | temperature |
| WiFi strength | WiFi signal strength | - | - |

### Controls

| Control | Type | Description |
|---------|------|-------------|
| Master Switch | Switch | Main power switch for the device |
| Buzzer Switch | Switch | Toggle device sound on/off |
| Lamp Switch | Switch | Toggle device light on/off |
| Limit Input | Number | Set maximum input power limit |
| Limit Output | Number | Set maximum output power limit |
| Soc maximum | Number | Set maximum state of charge level |
| Soc minimum | Number | Set minimum state of charge level |
| AC Mode | Select | Choose between AC input or output mode |

## ZendureManager
The ZendureManager, can be used to manage all Zendure devices.
- There are three mode of operation available for the Zendure Manger in order to mange how it operates:
    1) Off; the Zendure Manger does nothing.
    2) Manual power; the 'Zendure Manual Power' number is used to set discharging (if negative) and charging if positive.
    3) Smart matching; The 'P1 Sensor for smart matching' sensor is used to keep zero on the meter.

In all of these modes, the current is always distributed dynamicly, based on the 'actual soc' for charging and discharging.
The actual soc is calculated like this:
- chargecapacity = packNum * max(0, socSet - electricLevel)
- dischargecapacity = packNum * max(0, electricLevel - minSoc)

In this way the maximal availability for charging/discharging is achieved. This is also the reason why the AC mode can not be manipulated because it would break this feature.

## Clusters
At this moment the integration cannot handle the Zenlink cluster (will be added in the future).
However it is possible to create clusters of your own in the integration. For which you can use the information about clusters from the Zendure App for that as well. This option is only available if you have multiple devices.
![image](https://github.com/user-attachments/assets/dba74b54-e75f-481d-b35b-98a37f079fad)
In this example the Zen 05 behaves like a cluster with a maximum output of 800watt. At this moment there are three options available 800/1200 and 2400 watt. The Zen66 device is part of this cluster. The output per device of this cluster is dependant on the actual capacity of the devices. If the device is not in a cluster the ZendureManager will use it maximum input or output. Wherever the device cluster is not defined, the ZendureManager will not use the device! The configured values are persisted, and also after a reboot of HA they should stay the same.

## Home assistant Energy Dashboard

The Zendure integration reports power values in watts (W), which represent instantaneous power flow. However, the Home Assistant Energy Dashboard requires energy values in watt-hours (Wh) or kilowatt-hours (kWh), which represent accumulated energy over time.

To integrate your Zendure devices with the Energy Dashboard, you'll need to create additional sensors that convert the power readings into energy measurements. You can use the (Riemann sum) Integral sensor to accumulate power readings into energy values.

To do this go to Devices & Services > Helpers and add an Integral sensor for both the power flowing into the battery (eg: `sensor.hyper_2000_grid_input_power`) as well as the power feeding back to the grid/house (eg: `sensor.hyper_2000_energy_power`)

Once you have the integral sensors set up:

1. Go to Settings → Dashboards → Energy
2. In the "Grid" section, add your grid consumption/return sensors
3. In the "Battery" section:
   - Add your Zendure battery
   - Select the integral sensor for energy going into the battery
   - Select the integral sensor for energy coming from the battery
4. Save your configuration & wait to the next hour before the summarized data starts to show up.

For more information, see the [Home Assistant Energy documentation](https://www.home-assistant.io/docs/energy/).

## License

MIT License
