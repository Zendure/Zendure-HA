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
        self.limit = [minpower, maxpower]
        self.initPower = True
        self.devices: list[ZendureDevice] = devices if devices is not None else []
        for d in self.devices:
            d.fuseGrp = self

    def devicelimit(self, d: ZendureDevice, idx: int) -> int:
        """Return the limit discharge power for a device."""
        if self.initPower:
            self.initPower = False
            lim = max if idx == 1 else min
            if len(self.devices) == 1:
                d.power_limit = lim(self.limit[idx], d.limit[idx])
            else:
                limit = 0
                weight = 0
                for fd in self.devices:
                    if fd.homePower.asInt != 0:
                        limit += fd.limit[idx]
                        weight += (100 - fd.electricLevel.asInt) * fd.limit[idx]
                avail = lim(self.limit[idx], limit)
                for fd in self.devices:
                    if fd.homePower.asInt != 0:
                        fd.power_limit = int(avail * ((100 - fd.electricLevel.asInt) * fd.limit[idx]) / weight) if weight < 0 else fd.limit[idx]
                        limit -= fd.limit[idx]
                        if limit > avail - fd.power_limit:
                            fd.power_limit = lim(avail - limit, avail)
                        fd.power_limit = lim(fd.power_limit, fd.limit[idx])
                        avail -= fd.power_limit
        return d.power_limit

    # def discharge_limit(self, d: ZendureDevice) -> int:
    #     """Return the limit discharge power for a device."""
    #     if self.initPower:
    #         self.initPower = False
    #         if len(self.devices) == 1:
    #             d.pwr_max = min(self.maxpower, d.discharge_limit)
    #         else:
    #             limit = 0
    #             weight = 0
    #             for fd in self.devices:
    #                 if fd.homeOutput.asInt > 0:
    #                     limit += fd.discharge_limit
    #                     weight += fd.electricLevel.asInt * fd.discharge_limit
    #             avail = min(self.maxpower, limit)
    #             for fd in self.devices:
    #                 if fd.homeOutput.asInt > 0:
    #                     fd.pwr_max = int(avail * (fd.electricLevel.asInt * fd.discharge_limit) / weight) if weight > 0 else fd.discharge_start
    #                     limit -= fd.discharge_limit
    #                     if limit < avail - fd.pwr_max:
    #                         fd.pwr_max = min(avail - limit, avail)
    #                     fd.pwr_max = min(fd.pwr_max, fd.discharge_limit)
    #                     avail -= fd.pwr_max

    #     return d.pwr_max
