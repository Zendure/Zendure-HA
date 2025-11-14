"""Fusegroup for Zendure devices."""

from __future__ import annotations

import logging

from custom_components.zendure_ha.const import DeviceState, SmartMode

from .device import ZendureDevice

_LOGGER = logging.getLogger(__name__)


class FuseGroup:
    """Zendure Fuse Group."""

    def __init__(self, name: str, maxpower: int, minpower: int, devices: list[ZendureDevice] | None = None) -> None:
        """Initialize the fuse group."""
        self.name: str = name
        self.maxpower = maxpower
        self.minpower = minpower
        self.pwr_used = 0
        self.devices: list[ZendureDevice] = devices if devices is not None else []
        for d in self.devices:
            d.fuseGrp = self

    def chargePower(self, device: ZendureDevice, pwr_update: int) -> int:
        """Return the charge power for a device."""
        if len(self.devices) == 1:
            device.maxPower = max(self.minpower, device.chargeLimit)
        elif pwr_update != self.pwr_update:
            # calculate maxPower for all devices in the group
            self.pwr_update = pwr_update
            total = 0
            for d in self.devices:
                if (d.homeOutput.asInt > 0 or d.batteryInput.asInt > 0) and d.state != DeviceState.SOCFULL:
                    d.maxPower = d.chargeLimit + max(d.maxSolar - d.chargeLimit, d.pwr_produced)
                    total += d.maxPower * (100 - d.electricLevel.asInt)

            for d in self.devices:
                if (d.homeOutput.asInt > 0 or d.batteryInput.asInt > 0) and d.state != DeviceState.SOCFULL:
                    d.maxPower = int(self.minpower * d.maxPower * (100 - d.electricLevel.asInt) / total)
                else:
                    d.maxPower = 0
        return device.maxPower

    def dischargeLimit(self, d: ZendureDevice, solarOnly: bool) -> int:
        """Return the discharge power for a device."""
        solarOnly |= d.state == DeviceState.SOCEMPTY

        if solarOnly:
            d.pwr = 0 if -d.pwr_produced < SmartMode.POWER_START + 20 else SmartMode.POWER_START
        else:
            d.pwr = d.dischargeStart if d.state in [DeviceState.INACTIVE, DeviceState.SOCFULL] else 0

        if d.pwr == 0:
            return 0
        if len(self.devices) == 1:
            d.pwr = min(d.pwr, self.maxpower, d.dischargeLimit)
        else:
            used = sum(fd.pwr for fd in self.devices if fd.state in [DeviceState.ACTIVE, DeviceState.SOCFULL])
            d.pwr = 0 if d.pwr > self.maxpower - used else min(d.pwr, self.maxpower - used, d.dischargeLimit)
            if d.pwr == 0:
                return 0
        return d.dischargeLoad if not solarOnly else d.pwr

    def dischargePower(self, d: ZendureDevice, pwr: int, solarOnly: bool) -> int:
        """Return the discharge power for a device."""
        solarOnly |= d.state == DeviceState.SOCEMPTY
        if solarOnly:
            pwr = min(-d.pwr_produced, pwr)

        if len(self.devices) == 1:
            pwr = min(pwr, self.maxpower, d.dischargeLimit)
        else:
            used = sum(fd.pwr for fd in self.devices if fd.state in [DeviceState.ACTIVE, DeviceState.SOCFULL])
            pwr = min(pwr, self.maxpower - used, d.dischargeLimit)
        d.pwr += pwr
        return pwr

