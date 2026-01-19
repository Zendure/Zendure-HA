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

    def charge_limit(self, d: ZendureDevice) -> int:
        """Return the limit discharge power for a device."""
        if self.initPower:
            self.initPower = False
            if len(self.devices) == 1:
                d.pwr_max = max(self.minpower, d.charge_limit)
            else:
                limit = 0
                weight = 0
                for fd in self.devices:
                    if fd.homeInput.asInt > 0:
                        limit += fd.charge_limit
                        weight += (100 - fd.electricLevel.asInt) * fd.charge_limit
                avail = max(self.minpower, limit)
                for fd in self.devices:
                    if fd.homeInput.asInt > 0:
                        fd.pwr_max = int(avail * ((100 - fd.electricLevel.asInt) * fd.charge_limit) / weight) if weight < 0 else fd.charge_start
                        limit -= fd.charge_limit
                        if limit > avail - fd.pwr_max:
                            fd.pwr_max = max(avail - limit, avail)
                        fd.pwr_max = max(fd.pwr_max, fd.charge_limit)
                        avail -= fd.pwr_max

        return d.pwr_max

    def discharge_limit(self, d: ZendureDevice) -> int:
        """Return the limit discharge power for a device."""
        if self.initPower:
            self.initPower = False
            if len(self.devices) == 1:
                d.pwr_max = min(self.maxpower, d.discharge_limit)
            else:
                limit = 0
                weight = 0
                for fd in self.devices:
                    if fd.homeOutput.asInt > 0:
                        limit += fd.discharge_limit
                        weight += fd.electricLevel.asInt * fd.discharge_limit
                avail = min(self.maxpower, limit)
                for fd in self.devices:
                    if fd.homeOutput.asInt > 0:
                        fd.pwr_max = int(avail * (fd.electricLevel.asInt * fd.discharge_limit) / weight) if weight > 0 else fd.discharge_start
                        limit -= fd.discharge_limit
                        if limit < avail - fd.pwr_max:
                            fd.pwr_max = min(avail - limit, avail)
                        fd.pwr_max = min(fd.pwr_max, fd.discharge_limit)
                        avail -= fd.pwr_max

        return d.pwr_max
