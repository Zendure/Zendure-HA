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
            lim = max if idx == 0 else min
            if len(self.devices) == 1:
                d.power_limit = lim(self.limit[idx], d.limit[idx])
            else:
                limit = 0
                weight = 0
                for fd in self.devices:
                    if fd.homePower.asInt != 0:
                        limit += fd.limit[idx]
                        weight += (100 - fd.level) * fd.limit[idx]
                avail = lim(self.limit[idx], limit)
                for fd in self.devices:
                    if fd.homePower.asInt != 0:
                        fd.power_limit = int(avail * ((100 - fd.level) * fd.limit[idx]) / weight) if weight < 0 else fd.limit[idx]
                        limit -= fd.limit[idx]
                        if limit > avail - fd.power_limit:
                            fd.power_limit = lim(avail - limit, avail)
                        fd.power_limit = lim(fd.power_limit, fd.limit[idx])
                        avail -= fd.power_limit
        return d.power_limit


CONST_EMPTY_GROUP = FuseGroup("empty", 0, 0)
