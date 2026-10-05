"""Replay a recorded series through the live Distribution and report what the meter would have done."""

from custom_components.zendure_ha.device import REPORT_WAIT_S, DeviceState, DistributionDevice, ProfileCharge, ProfileDischarge
from custom_components.zendure_ha.distribution import Distribution

# measured 2026-09-21/22 on the fleet with the integration unloaded, direct commands, the device polled at 15 Hz: the setpoint is in
# the device 1.8 s after the command (1.1 to 2.4 s per device, ZenSDK ~0.4 s quicker than hemsEP), the power then follows at ~200 W/s
# (150 to 210 on the 2400s, the 1600/800 not resolved); from 0 W the first power flows only 6 to 15 s after the command, already after
# 15 s idle; a stop lands after 2.3 s; commands are handled in order, one per ~1.3 s, a quicker follower is dropped
SETPOINT_S = 1.8  # command to setpoint in the device
RAMP_W_S = 200.0  # the power moves toward the applied setpoint at this slope
START_S = 8.4  # from standstill the first power flows this long after the command, median of five SolarFlows
STOP_S = 2.3  # a command to 0 W lands after this, in one step
PROCESS_S = 1.3  # one command per this; a command sent sooner after the previous one is dropped
LAG_ENABLED = True  # False reproduces the old optimistic bound, where a device followed its setpoint within one tick


class ReplayDevice(DistributionDevice):
    # the vendor contract, reduced to what a replay needs: remember the clamped command, follow it, drain the pack
    def __init__(self, hass, spec: dict) -> None:  # noqa: ANN001
        super().__init__(hass, spec["key"], [ProfileDischarge(spec["power_discharge"]), ProfileCharge(spec["power_charge"])])
        self.kwh = spec["kwh"]
        self.soc_min = spec.get("soc_min", self.soc_min)
        self.soc_max = spec.get("soc_max", self.soc_max)
        self.state = DeviceState.ACTIVE
        self.now = 0.0  # the replay clock, set by the loop; the state stamp carries it instead of the wall clock
        self.queue: list[tuple[float, int]] = []  # commands on their way into the device: (accepted at, target)
        self.applied = 0  # the setpoint the device works with
        self.flow_at = 0.0  # from standstill: when the inverter is up and the power starts to move
        self.last_accept = -1e18
        self.power_since = -1e18

    def power(self, watts: int) -> int:
        watts = max(self.power_profile[1].power, min(self.power_profile[0].power, int(watts)))

        # bypass: a full pack cannot take more, an empty one passes through no more than the panels make
        level = (self.soc - self.soc_min) / (self.soc_max - self.soc_min) * 100 if self.soc_max > self.soc_min else 0.0
        if watts < 0 and level >= 99:
            watts = 0
        elif level <= 1:
            watts = min(self.solar, watts)

        # the echo: until the queued command has been applied the device is busy with it, REPORT_WAIT_S at most
        if self.applied != self.power_setpoint and self.now - self.power_since < REPORT_WAIT_S:
            return self.power_actual

        # the driver's rule: no command while both the measured power and the last setpoint sit within 10 W of the target; otherwise it
        # goes out (again), SENT until the power follows. The device takes one command per PROCESS_S, in order, with one waiting
        # place: a command that would wait longer than a period is dropped (measured 25 percent lost at 1 Hz, none at 2 s)
        if abs(self.power_actual - watts) < 10 and abs(self.power_setpoint - watts) < 10:
            return self.power_actual
        if self.power_setpoint != watts:
            self.power_since = self.now
        self.power_setpoint = watts
        self.state = DeviceState.SENT
        self.state_since = self.now
        accept = max(self.now, self.last_accept + PROCESS_S)
        if accept - self.now < PROCESS_S:
            self.queue.append((accept, watts))
            self.last_accept = accept
        return self.power_actual

    def power_off(self) -> None:
        self.power_setpoint = 0


async def replay(hass, entry_id: str, data: dict, mode: int) -> dict:  # noqa: ANN001
    dist = Distribution(hass, entry_id)
    dist.wear_date = float("inf")
    dist.mode = mode
    dist.devices = [ReplayDevice(hass, spec) for spec in data["devices"]]
    keys = [spec["key"] for spec in data["devices"]]
    for device, key in zip(dist.devices, keys):
        device.soc = data["soc"][key][0]  # start where the recording starts, or the ranking is wrong from the first tick

    times, p1s, pvs = data["t"], data["p1"], data["pv"]
    segments, import_wh, export_wh, switches, previous, recorded_wh = [], 0.0, 0.0, 0, set(), 0.0
    for i in range(len(times) - 1):
        now, step = times[i], min(times[i + 1] - times[i], 60.0)

        # the house net of its own PV, from the recording; the fleet part is replaced by ours
        house = p1s[i] + pvs[i] + sum(data["power"][key][i] for key in keys)
        grid = house - pvs[i] - sum(d.power_actual for d in dist.devices)
        for device, key in zip(dist.devices, keys):
            device.now = now
            device.solar = data["solar"][key][i]
            device.bypass = not device.kwh  # no pack: the panels always run straight through

        await dist._process_p1(int(grid), now)

        # a real device answers after its measured dead time and then slews; LAG_ENABLED False is the old optimistic bound
        for device, key in zip(dist.devices, keys):
            if not LAG_ENABLED:
                device.power_actual = device.power_setpoint
                continue
            if not device.kwh:  # no pack: the panels run straight through, the recording is what it delivered
                device.power_actual = data["power"][key][i]
                continue
            # the device applies its commands in order: a stop after STOP_S, a setpoint after SETPOINT_S; from standstill the inverter first has to start
            while device.queue and now >= device.queue[0][0] + (STOP_S if device.queue[0][1] == 0 else SETPOINT_S):
                accepted, target = device.queue.pop(0)
                if device.applied == 0 and target != 0:
                    device.flow_at = accepted + START_S
                device.applied = target
            # the power: a stop lands at once, otherwise it moves toward the setpoint at RAMP_W_S once the inverter runs
            if device.applied == 0:
                device.power_actual = 0
            elif now >= device.flow_at:
                move = RAMP_W_S * step
                delta = device.applied - device.power_actual
                device.power_actual = device.applied if abs(delta) <= move else device.power_actual + (move if delta > 0 else -move)
            # the state follows the report like the driver: ACTIVE once the power is within 40 W of the setpoint
            if device.state == DeviceState.SENT and abs(device.power_actual - device.power_setpoint) <= 40:
                device.state = DeviceState.ACTIVE
            device.soc = max(0.0, min(100.0, device.soc - device.power_actual * step / 36000.0 / device.kwh))
            level = (device.soc - device.soc_min) / (device.soc_max - device.soc_min) * 100 if device.soc_max > device.soc_min else 0.0
            device.soc_limit = 2 if level <= 1 else 1 if level >= 99 else 0  # the pack's own verdict, as the real device reports it

        segments.append((abs(grid), step))
        import_wh += max(0, grid) * step / 3600
        export_wh += max(0, -grid) * step / 3600
        recorded_wh += abs(p1s[i]) * step / 3600  # the meter as it actually ran that day
        active = {d.device_id for d in dist.devices if d.power_setpoint}
        switches += len(active ^ previous)
        previous = active

    total = sum(step for _, step in segments)
    segments.sort()

    def percentile(share: float) -> float:
        seen = 0.0
        for value, step in segments:
            seen += step
            if seen >= total * share:
                return value
        return segments[-1][0]

    return {
        "hours": total / 3600,
        "import_wh": import_wh,
        "export_wh": export_wh,
        "total_wh": import_wh + export_wh,
        "recorded_wh": recorded_wh,
        "median": percentile(0.5),
        "p90": percentile(0.9),
        "p99": percentile(0.99),
        "within": {watts: sum(step for value, step in segments if value <= watts) / total * 100 for watts in (100, 250, 500)},
        "switches": switches,
        "soc": {device.device_id: device.soc for device in dist.devices},
    }
