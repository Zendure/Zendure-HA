"""Devices for Zendure Integration."""

from datetime import datetime

from homeassistant.core import HomeAssistant

from custom_components.zendure_ha.sensor import ZendureSensor

from .entity import ZendureEntities


class ZendureBattery(ZendureEntities):
    """Representation of a Zendure battery."""

    def __init__(self, hass: HomeAssistant, parent: str, device_sn: str) -> None:
        """Initialize the Zendure battery."""
        self.kWh = 0.0
        match device_sn[0]:
            case "A":
                if device_sn[3] == "3":
                    model = "AIO2400"
                    self.kWh = 2.4
                else:
                    model = "AB1000"
                    self.kWh = 0.96
            case "B":
                model = "AB1000S"
                self.kWh = 0.96
            case "C":
                model = "AB2000" + ("S" if device_sn[3] == "F" else "X" if device_sn[3] == "E" else "")
                self.kWh = 1.92
            case "F":
                model = "AB3000"
                self.kWh = 2.88
            case "J":
                model = "AB3000L"
                self.kWh = 2.88
            case _:
                model = "Unknown"
                self.kWh = 0.0

        super().__init__(hass, model, f"{model}_{device_sn[-8:]}", device_sn, parent=parent)
        self.lastseen = datetime.min

        # Create the battery entities."""
        self.state = ZendureSensor(self, "packState")
        self.socLevel = ZendureSensor(self, "socLevel", None, "%", "battery", "measurement")
        self.state = ZendureSensor(self, "state")
        self.power = ZendureSensor(self, "power", None, "W", "power", "measurement")
        self.maxTemp = ZendureSensor(self, "maxTemp", ZendureSensor.temp, "°C", "temperature", "measurement")
        self.totalVol = ZendureSensor(self, "totalVol", None, "V", "voltage", "measurement")
        self.batcur = ZendureSensor(self, "batcur", ZendureSensor.curr, "A", "current", "measurement")
        self.maxVol = ZendureSensor(self, "maxVol", None, "V", "voltage", "measurement", factor=100)
        self.minVol = ZendureSensor(self, "minVol", None, "V", "voltage", "measurement", factor=100)

    def entityRead(self, payload: dict) -> None:
        """Handle incoming MQTT message for the battery."""
        for key, value in payload.items():
            entity = self.__dict__.get(key)
            if key != "sn" and (entity := self.__dict__.get(key)):
                entity.update_value(value)
