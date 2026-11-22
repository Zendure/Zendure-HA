"""SoC balancing for the Zendure manager."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .const import DeviceState
from .device import ZendureDevice
from .select import ZendureSelect

_LOGGER = logging.getLogger(__name__)

# Below this SoC spread (%) the normal proportional split is used.
SOC_SPREAD_MIN = 1
# Spread (%) at which the split becomes a full 100/0 handover.
SOC_SPREAD_FULL = 10


class SocBalancingMixin:
    """Discharge the fullest battery first, charge the emptiest first."""

    soc_balancing: bool = True

    def update_soc_balancing(self, entity: ZendureSelect, _value: Any) -> None:
        self.soc_balancing = entity.value == 1
        _LOGGER.info("Update SoC balancing: %s", entity.current_option)

    def _balancing_active(self, devices: list[ZendureDevice]) -> bool:
        return self.soc_balancing and not self._soc_close(devices)

    def _balancing_bypass_exit(self, d: ZendureDevice) -> bool:
        """The highest-SoC device leaves bypass to supply the battery budget."""
        return not self._soc_close(self.discharge) and d.electricLevel.asInt == max(x.electricLevel.asInt for x in self.discharge)

    def _soc_close(self, devices: list[ZendureDevice]) -> bool:
        if not devices:
            return False
        levels = [d.electricLevel.asInt for d in devices]
        return max(levels) - min(levels) <= SOC_SPREAD_MIN

    def _balancing_gain(self, spread: int) -> float:
        """0 at SOC_SPREAD_MIN, 1 at SOC_SPREAD_FULL, linear between.

        A hard switch would flip the split every time the SoC values cross the threshold.
        """
        window = SOC_SPREAD_FULL - SOC_SPREAD_MIN
        if window <= 0:
            return 1.0
        return min(1.0, max(0.0, (spread - SOC_SPREAD_MIN) / window))

    async def _charge_balancing(self, setpoint: int, dev_start: int) -> tuple[int, int]:
        if not self.charge:
            return setpoint, dev_start
        spread = max(d.electricLevel.asInt for d in self.charge) - min(d.electricLevel.asInt for d in self.charge)
        gain = self._balancing_gain(spread)
        weight_total = sum(d.pwr_max * (100 - d.electricLevel.asInt) for d in self.charge)
        for d in sorted(self.charge, key=lambda d: d.electricLevel.asInt, reverse=False):
            strict_pwr = 0 if d.state == DeviceState.SOCFULL else max(setpoint, d.pwr_max)
            weight = d.pwr_max * (100 - d.electricLevel.asInt)
            # Charging values are negative, so weight_total is too.
            weighted_pwr = int(setpoint * weight / weight_total) if weight_total != 0 else 0
            weight_total -= weight
            pwr = weighted_pwr + int(gain * (strict_pwr - weighted_pwr))
            setpoint -= await d.power_charge(pwr)
            dev_start += -1 if pwr != 0 and d.electricLevel.asInt > self.idle_lvlmin + 3 else 0

        return setpoint, dev_start

    def _balancing_solar_shares(
        self,
        setpoint: int,
        floor: dict[ZendureDevice, int],
        dispatchable: Callable[[ZendureDevice], int],
    ) -> dict[ZendureDevice, int]:
        """Solar covers the setpoint: each device passes its floor, the rest goes to the highest SoC.

        A device takes at most its own solar, so no battery is drained to pass another device's solar.
        """
        shares = {d: min(floor[d], dispatchable(d)) for d in self.discharge}
        remaining = max(0, setpoint - sum(shares.values()))
        for d in sorted(self.discharge, key=lambda d: d.electricLevel.asInt, reverse=True):
            solar = max(0, -d.pwr_produced - d.pwr_bypass)
            cap = min(dispatchable(d), max(shares[d], solar))
            give = min(remaining, max(0, cap - shares[d]))
            shares[d] += give
            remaining -= give
        return shares

    async def _discharge_balancing(
        self,
        setpoint: int,
        dev_start: int,
        solaronly: bool,
        solar_total: int,
        battery_budget: int,
        dispatchable: Callable[[ZendureDevice], int],
        no_alternative: bool,
    ) -> tuple[int, int]:
        """Each device keeps its solar and floor; the battery budget goes to the highest SoC first."""
        solar = {d: max(0, -d.pwr_produced - d.pwr_bypass) for d in self.discharge}
        floor = {d: self._apply_min_output(0, d) for d in self.discharge}
        floor_total = sum(max(0, floor[d] - solar[d]) for d in self.discharge)
        remaining = max(0, battery_budget - floor_total)
        strict_shares = self._balancing_solar_shares(setpoint, floor, dispatchable) if solaronly and solar_total > 0 else {}
        if not self.discharge:
            return setpoint, dev_start
        gain = self._balancing_gain(max(d.electricLevel.asInt for d in self.discharge) - min(d.electricLevel.asInt for d in self.discharge))
        weight_total = sum(d.pwr_max * d.electricLevel.asInt for d in self.discharge)

        for d in sorted(self.discharge, key=lambda d: d.electricLevel.asInt, reverse=True):
            headroom = dispatchable(d)
            eff_floor = 0
            if solaronly and solar_total > 0:
                pwr = strict_shares[d]
            else:
                eff_floor = min(max(solar[d], floor[d]), headroom)
                # A full battery with solar passes it on its own. Without solar, zeroing it
                # makes it go idle and get restarted every other cycle.
                if d.state == DeviceState.SOCFULL and solar[d] > 0 and not no_alternative:
                    strict_give = 0
                else:
                    strict_give = min(remaining, max(0, headroom - eff_floor))
                weight = d.pwr_max * d.electricLevel.asInt
                weighted_give = int(remaining * weight / weight_total) if weight_total > 0 else 0
                weight_total -= weight
                give = weighted_give + int(gain * (strict_give - weighted_give))
                give = min(give, max(0, headroom - eff_floor))
                pwr = eff_floor + give
            pwr = min(pwr, setpoint, headroom)

            # A device ignores steep steps. Floor after the ramp, so the command stays above it.
            pwr = self._ramp(pwr, d)
            pre_floor = pwr
            pwr = self._apply_min_output(pwr, d)
            if d.min_output > 0:
                _LOGGER.debug(
                    "%s => min_output floor: pre=%sW post=%sW awake=%s battery_preserving=%s solar=%sW state=%s minSoc=%s socLimit=%s",
                    d.name,
                    pre_floor,
                    pwr,
                    d.awake,
                    d._floor_battery_preserving,
                    solar[d],
                    d.state.name,
                    d.minSoc.asNumber,
                    d.socLimit.asInt,
                )

            # Count what the device really delivered; what it missed stays for the next device.
            actual = max(0, await d.power_discharge(pwr + d.pwr_bypass) - d.pwr_bypass)
            setpoint -= actual
            if not (solaronly and solar_total > 0):
                remaining = max(0, remaining - max(0, actual - eff_floor))
            dev_start += 1 if actual != 0 and d.electricLevel.asInt + 3 < self.idle_lvlmax else 0

        return setpoint, dev_start
