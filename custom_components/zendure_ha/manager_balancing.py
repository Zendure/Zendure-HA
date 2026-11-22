"""SoC balancing for the Zendure manager.

The balancing methods live in a mixin so manager.py keeps the dispatch
flow and this file keeps the SoC-balancing splits.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from .const import DeviceState
from .device import ZendureDevice

_LOGGER = logging.getLogger(__name__)


class ZendureBalancing:
    """SoC-balancing splits for ZendureManager (see manager.py for the flag)."""

    def _soc_close(self, devices: list[ZendureDevice]) -> bool:
        """True when every device's SoC is within soc_balancing_spread % of the others."""
        if not devices:
            return False
        levels = [d.electricLevel.asInt for d in devices]
        return max(levels) - min(levels) <= self.soc_balancing_spread

    def _balancing_gain(self, spread: int) -> float:
        """0 at soc_balancing_spread, 1 at soc_balancing_spread_gain.

        Grows linearly between the two, so the balancing tilt fades in as
        the SoC spread opens instead of jumping in at the threshold.
        """
        window = self.soc_balancing_spread_gain - self.soc_balancing_spread
        if window <= 0:
            return 1.0
        return min(1.0, max(0.0, (spread - self.soc_balancing_spread) / window))

    async def _charge_balancing(self, setpoint: int, dev_start: int) -> tuple[int, int]:
        """Balance SoC while charging: the emptiest battery fills first.

        The charge budget is split as a blend of the strict 100/0 handover
        to the lowest-SoC device and the default weighted split, tilted by
        _balancing_gain: the wider the spread, the closer to the handover.
        """
        if not self.charge:
            return setpoint, dev_start
        spread = max(d.electricLevel.asInt for d in self.charge) - min(d.electricLevel.asInt for d in self.charge)
        gain = self._balancing_gain(spread)
        weight_total = sum(d.pwr_max * (100 - d.electricLevel.asInt) for d in self.charge)
        for d in sorted(self.charge, key=lambda d: d.electricLevel.asInt, reverse=False):
            # Strict handover: the whole remaining budget, up to the charge
            # limit. A full battery gets nothing - in the weighted path its
            # weight (100 - SoC) is 0, so this must match.
            strict_pwr = 0 if d.state == DeviceState.SOCFULL else max(setpoint, d.pwr_max)
            weight = d.pwr_max * (100 - d.electricLevel.asInt)
            # pwr_max is negative while charging, so weight_total is negative
            # too - != 0 keeps the sign-correct ratio (same as the default path).
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
        """Shares for a solar-covered setpoint when balancing is on.

        Each device passes only its floor, so its surplus PV charges its own
        battery. The demand left after the floors goes to the highest-SoC
        device, up to its own solar, then to the next one. Devices at a
        battery limit have floor 0 and only share in the leftover.
        """
        shares = {d: min(floor[d], dispatchable(d)) for d in self.discharge}
        remaining = max(0, setpoint - sum(shares.values()))
        for d in sorted(self.discharge, key=lambda d: d.electricLevel.asInt, reverse=True):
            # Solar-only: the battery budget is 0, so a device may take the
            # leftover only up to its own solar (on top of the floor it
            # already holds). Taking more would drain its battery to pass a
            # peer's stored solar.
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
        """Balance SoC while discharging: floors first, then the rest to the highest SoC.

        When the demand exceeds the solar, every device keeps its own
        pass-through solar and, when its floor applies, at least min_output;
        the battery budget goes to the highest-SoC device, up to its
        headroom, then to the next one. When the solar alone covers the
        demand, each device passes only its floor and stores the surplus PV
        in its own battery (see _balancing_solar_shares). Draining the
        fullest battery first is what pulls the group back together.
        """
        solar = {d: max(0, -d.pwr_produced - d.pwr_bypass) for d in self.discharge}
        floor = {d: self._apply_min_output(0, d) for d in self.discharge}
        floor_total = sum(max(0, floor[d] - solar[d]) for d in self.discharge)
        remaining = max(0, battery_budget - floor_total)
        strict_shares = self._balancing_solar_shares(setpoint, floor, dispatchable) if solaronly and solar_total > 0 else {}
        if not self.discharge:
            return setpoint, dev_start
        # The battery budget is split as a blend of the strict 100/0 handover
        # and the default weighted split. The tilt grows with the SoC spread
        # (_balancing_gain), so the split changes continuously where balancing
        # engages - a hard handover there flips the split every time the
        # devices' SoC cross the threshold.
        gain = self._balancing_gain(max(d.electricLevel.asInt for d in self.discharge) - min(d.electricLevel.asInt for d in self.discharge))
        weight_total = sum(d.pwr_max * d.electricLevel.asInt for d in self.discharge)

        for d in sorted(self.discharge, key=lambda d: d.electricLevel.asInt, reverse=True):
            headroom = dispatchable(d)
            eff_floor = 0
            if solaronly and solar_total > 0:
                pwr = strict_shares[d]
            else:
                eff_floor = min(max(solar[d], floor[d]), headroom)
                # Strict handover: the whole remaining budget to this device.
                # A full battery with solar already passes that solar to the
                # home on its own; it stays out of the split unless it is the
                # only device that can help (same rule as the default path).
                # Without solar it passes nothing, and it is the fullest
                # battery: zeroing it makes it go idle, get restarted and
                # zeroed again every other cycle.
                if d.state == DeviceState.SOCFULL and solar[d] > 0 and not no_alternative:
                    strict_give = 0
                else:
                    strict_give = min(remaining, max(0, headroom - eff_floor))
                # Default weighted share of the remaining budget.
                weight = d.pwr_max * d.electricLevel.asInt
                weighted_give = int(remaining * weight / weight_total) if weight_total > 0 else 0
                weight_total -= weight
                give = weighted_give + int(gain * (strict_give - weighted_give))
                give = min(give, max(0, headroom - eff_floor))
                pwr = eff_floor + give
            pwr = min(pwr, setpoint, headroom)

            # Same ramp as the default path: steep steps make the hub ignore
            # the command. Floor last, so a ramped value never drops below it.
            pwr = self._ramp(pwr, d)

            # Apply the floor one more time: whatever the cap above did to
            # pwr, a floored device's command must not end up below its floor.
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

            # Use what the device actually did, not pwr: power_discharge() always
            # returns the real result, which can differ from what was asked - so the
            # rest of the devices don't get handed a setpoint this one already covered.
            actual = max(0, await d.power_discharge(pwr + d.pwr_bypass) - d.pwr_bypass)
            setpoint -= actual
            if not (solaronly and solar_total > 0):
                # Pay for the battery the device really delivered above its
                # floor. What it could not deliver stays in the pot for the
                # next device (the next-highest SoC).
                remaining = max(0, remaining - max(0, actual - eff_floor))
            dev_start += 1 if actual != 0 and d.electricLevel.asInt + 3 < self.idle_lvlmax else 0

        return setpoint, dev_start
