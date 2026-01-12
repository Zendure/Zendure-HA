"""Fusegroup for Zendure devices."""

from __future__ import annotations

import logging

from .device import ZendureDevice

_LOGGER = logging.getLogger(__name__)


class FuseGroup:
    """Zendure Fuse Group."""

    def __init__(self, name: str, maxpower: int, minpower: int, devices: list[ZendureDevice] | None = None) -> None:
        """Initialize the fuse group."""
        self.name: str = name
        self.maxpower = maxpower
        self.minpower = minpower
        self.initPower = True
        self.devices: list[ZendureDevice] = devices if devices is not None else []
        for d in self.devices:
            d.fuseGrp = self

    def chargelimit(self, d: ZendureDevice) -> int:
        """Return the limit discharge power for a device."""
        if self.initPower:
            self.initPower = False
            if len(self.devices) == 1:
                d.pwr_max = max(self.minpower, d.limit[0])
            else:
                limit = 0
                weight = 0
                for fd in self.devices:
                    if fd.homeInput.asInt > 0:
                        limit += fd.limit[0]
                        weight += (100 - fd.electricLevel.asInt) * fd.limit[0]
                avail = min(self.minpower, limit)
                for fd in self.devices:
                    if fd.homeInput.asInt > 0:
                        fd.pwr_max = int(avail * ((100 - fd.electricLevel.asInt) * fd.limit[0]) / weight) if weight < 0 else fd.charge_start
                        limit -= fd.limit[0]
                        if limit > avail - fd.pwr_max:
                            fd.pwr_max = max(avail - limit, avail)
                        fd.pwr_max = max(fd.pwr_max, fd.limit[0])
                        avail -= fd.pwr_max

        return d.pwr_max

    def dischargelimit(self, d: ZendureDevice) -> int:
        """Return the limit discharge power for a device."""
        if self.initPower:
            self.initPower = False
            if len(self.devices) == 1:
                d.pwr_max = min(self.maxpower, d.limit[1])
            else:
                limit = 0
                weight = 0
                for fd in self.devices:
                    if fd.homeOutput.asInt > 0:
                        limit += fd.limit[1]
                        weight += fd.electricLevel.asInt * fd.limit[1]
                avail = min(self.maxpower, limit)
                for fd in self.devices:
                    if fd.homeOutput.asInt > 0:
                        fd.pwr_max = int(avail * (fd.electricLevel.asInt * fd.limit[1]) / weight) if weight > 0 else fd.discharge_start
                        limit -= fd.limit[1]
                        if limit < avail - fd.pwr_max:
                            fd.pwr_max = min(avail - limit, avail)
                        fd.pwr_max = min(fd.pwr_max, fd.limit[1])
                        avail -= fd.pwr_max

        return d.pwr_max
