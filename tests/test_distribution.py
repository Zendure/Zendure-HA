"""Distribution unit tests: fleet dispatch over contract fakes with simulated time, and the fleet output cap."""

from custom_components.zendure_ha.const import Phase
from custom_components.zendure_ha.device import DeviceState, DistributionDevice, ProfileCharge, ProfileDischarge
from custom_components.zendure_ha.distribution import RANGE_MIN, Distribution, DistributionMode


class FakeDevice(DistributionDevice):
    # contract fake: stores the clamped command, returns the current power_actual
    def __init__(self, hass, device_id: str, soc: float, power: int = 2400, kwh: float = 10.0) -> None:  # noqa: ANN001
        super().__init__(hass, device_id, [ProfileDischarge(power), ProfileCharge(-power)])
        self.soc = soc
        self.kwh = kwh
        self.state = DeviceState.ACTIVE  # under our control; EXTERNAL is excluded from the order

    def power(self, watts: int) -> int:
        self.power_setpoint = max(self.power_profile[1].power, min(self.power_profile[0].power, int(watts)))
        return self.power_actual

    def power_off(self) -> None:
        self.power_setpoint = 0
        self.state = DeviceState.EXTERNAL


def make_distribution(hass, entry, devices: list[FakeDevice], mode: DistributionMode = DistributionMode.SMART, max_output: int = 0) -> Distribution:  # noqa: ANN001
    dist = Distribution(hass, entry.entry_id)
    dist.devices = list(devices)
    dist.wear_date = float("inf")
    dist.mode = mode.value
    dist.max_output = max_output
    return dist


async def tick(dist: Distribution, demand: int, now: float) -> None:
    # one live tick: P1 shows what the fleet does not yet cover, then every device follows its setpoint perfectly
    p1 = demand - sum(d.power_actual for d in dist.devices)
    await dist._process_p1(int(p1), now)
    for d in dist.devices:
        d.power_actual = d.power_setpoint


async def run(dist: Distribution, demand: int, start: float, ticks: int) -> float:
    now = start
    for _ in range(ticks):
        now += 1.0
        await tick(dist, demand, now)
    return now


async def test_single_device_takes_the_demand(hass, entry) -> None:  # noqa: ANN001
    d1, d2 = FakeDevice(hass, "d1", 60.0), FakeDevice(hass, "d2", 30.0)
    dist = make_distribution(hass, entry, [d1, d2])
    await run(dist, 500, 1000.0, 10)
    assert 480 <= d1.power_setpoint <= 500  # highest SoC leads the discharge order
    assert d2.power_setpoint == 0


async def test_a_device_without_power_in_a_direction_stays_out_of_the_order(hass, entry) -> None:  # noqa: ANN001
    # the app limit reported as 0 (or a Hub that cannot charge) gives a 0 W profile; it must not be ranked, let alone divide by its band
    d1, d0 = FakeDevice(hass, "d1", 60.0), FakeDevice(hass, "d0", 60.0, power=0)
    dist = make_distribution(hass, entry, [d1, d0])
    dist.update_order()
    assert dist.profile[False].order == [d1] and dist.profile[True].order == [d1]


async def test_deadband_does_not_dispatch(hass, entry) -> None:  # noqa: ANN001
    d1 = FakeDevice(hass, "d1", 60.0)
    dist = make_distribution(hass, entry, [d1])
    await run(dist, 10, 1000.0, 5)
    assert d1.power_setpoint == 0


async def test_display_sensors_follow_the_meter(hass, entry) -> None:  # noqa: ANN001
    d1 = FakeDevice(hass, "d1", 60.0)
    dist = make_distribution(hass, entry, [d1], DistributionMode.OFF)
    dist.capacity = 8.0
    d1.power_actual = 300
    dist.solar = 200  # the solar groups, summed by the coordinator; a device's own DC solar only feeds its bypass
    await dist._process_p1(400, 1000.0)
    e = dist.entities
    assert (e["grid_import"].state, e["grid_export"].state, e["discharge_power"].state, e["charge_power"].state) == (400, 0, 300, 0)
    assert e["house_load"].state == 900  # import + solar + delivery
    assert e["available_kwh"].state == 5.0 and e["soc"].state == 62.5
    assert d1.power_setpoint == 0, "mode OFF never dispatches"


async def test_large_demand_spreads_over_the_fleet(hass, entry) -> None:  # noqa: ANN001
    devices = [FakeDevice(hass, f"d{i}", 60.0, 800, 2.0) for i in range(3)]
    dist = make_distribution(hass, entry, devices)
    await run(dist, 2000, 1000.0, 40)
    total = sum(d.power_setpoint for d in devices)
    assert 1900 <= total <= 2000
    assert all(d.power_setpoint > RANGE_MIN for d in devices)


async def test_max_output_caps_the_fleet_delivery(hass, entry) -> None:  # noqa: ANN001
    # three 800 W devices, each allowed 800 W by the app: together they would deliver 2400 W; the German cap holds the sum at 800
    devices = [FakeDevice(hass, f"d{i}", 60.0, 800, 2.0) for i in range(3)]
    dist = make_distribution(hass, entry, devices, max_output=800)
    await run(dist, 2000, 1000.0, 40)
    total = sum(d.power_setpoint for d in devices)
    assert 700 <= total <= 800, total


async def test_max_output_leaves_charging_alone(hass, entry) -> None:  # noqa: ANN001
    devices = [FakeDevice(hass, f"d{i}", 30.0, 800, 2.0) for i in range(3)]
    dist = make_distribution(hass, entry, devices, max_output=800)
    await run(dist, -2000, 1000.0, 60)
    total = sum(d.power_setpoint for d in devices)
    assert total <= -1900, total


async def test_manual_mode_follows_the_manual_power(hass, entry) -> None:  # noqa: ANN001
    d1 = FakeDevice(hass, "d1", 60.0)
    dist = make_distribution(hass, entry, [d1], DistributionMode.MANUAL)
    dist.manual_power = 600
    await run(dist, 0, 1000.0, 5)
    assert d1.power_setpoint == 600


async def test_export_replay_writes_the_replay_file(hass, entry, monkeypatch, tmp_path) -> None:  # noqa: ANN001
    import json
    from datetime import datetime, timezone
    from types import SimpleNamespace

    import custom_components.zendure_ha.distribution as distribution_module
    from custom_components.zendure_ha.sensor import HaSensor

    class ReportingDevice(FakeDevice):
        def entity_create(self) -> None:
            for key in ("home_output", "home_input", "solar", "soc"):
                HaSensor(self, key, uom="W")
                self.entities[key].entity_id = f"sensor.{self.device_id}_{key}"

    d1 = ReportingDevice(hass, "d1", 60.0, 800, 1.92)
    d1.name = "SF800_88"
    dist = make_distribution(hass, entry, [d1], DistributionMode.OFF)
    dist.p1_entities = [("sensor.p1_consumed", 1, 1), ("sensor.p1_produced", -1, 1000)]

    base = datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc)
    stamp = lambda s: base.replace(second=s)  # noqa: E731
    recorded = {
        "sensor.p1_consumed": [(stamp(0), "500"), (stamp(10), "700"), (stamp(20), "unknown"), (stamp(30), "900")],
        "sensor.p1_produced": [(stamp(5), "0.2")],  # kW: a produced entity is subtracted, in watts
        "sensor.d1_home_output": [(stamp(0), "300"), (stamp(15), "600")],
        "sensor.d1_home_input": [(stamp(0), "0")],
        "sensor.d1_solar": [(stamp(0), "120")],
        "sensor.d1_soc": [(stamp(0), "60"), (stamp(25), "59")],
    }

    def changes(hass, start, end, entity_id, no_attributes):  # noqa: ANN001, ANN202
        return {entity_id: [SimpleNamespace(last_updated=t, state=s) for t, s in recorded.get(entity_id, [])]}

    async def run_job(fn, *args):  # noqa: ANN001, ANN202
        return fn(*args)

    monkeypatch.setattr(distribution_module, "get_instance", lambda hass: SimpleNamespace(async_add_executor_job=run_job))
    monkeypatch.setattr(distribution_module.history, "state_changes_during_period", changes)
    monkeypatch.setattr(distribution_module.dt_util, "utcnow", lambda: base.replace(hour=11))
    hass.config.config_dir = str(tmp_path)

    name = await dist.export_replay()
    data = json.loads((tmp_path / "www" / name).read_text(encoding="utf-8"))
    assert data["t"] == [stamp(s).timestamp() for s in (0, 5, 10, 30)], "the P1 readings are the master clock; the unknown state is dropped"
    assert data["p1"] == [500, 300, 500, 700], "consumed minus 200 W produced from the moment the pair reports"
    assert data["devices"] == [{"key": "sf800_88", "name": "SF800_88", "model": "", "kwh": 1.92, "power_discharge": 800, "power_charge": -800, "soc_min": 10.0, "soc_max": 90.0}]
    assert data["power"]["sf800_88"] == [300, 300, 300, 600] and data["solar"]["sf800_88"] == [120, 120, 120, 120] and data["soc"]["sf800_88"] == [60.0, 60.0, 60.0, 59.0]
    assert data["pv"] == [0, 0, 0, 0] and data["meta"]["samples"] == 4 and data["meta"]["p1_entity"] == "sensor.p1_consumed,sensor.p1_produced"
    assert (tmp_path / "www").is_dir()


async def _hold(hass, entry, devices: list[FakeDevice], house: dict[int, float], ticks: int = 10) -> tuple[Distribution, dict]:  # noqa: ANN001
    # the house draws this per phase; the meter reads it minus whatever the fleet delivers there, the way a real meter does
    dist = make_distribution(hass, entry, devices, DistributionMode.MANUAL)
    dist.manual_power = 0
    dist.phase_limit_w = 1000.0
    now = 1000.0
    for _ in range(ticks):
        now += 1.0
        for phase in (1, 2, 3):
            dist.power[phase] = house[phase] - sum(d.power_actual for d in devices if d.power_phase == Phase(phase))
        await dist._process_p1(int(sum(dist.power)), now)
        for d in devices:
            d.power_actual = d.power_setpoint
    return dist, {phase: house[phase] - sum(d.power_actual for d in devices if d.power_phase == Phase(phase)) for phase in (1, 2, 3)}


async def test_the_guard_discharges_only_the_devices_on_the_overloaded_phase(hass, entry) -> None:  # noqa: ANN001
    # L3 draws 1500 W over a 1000 W limit; the device on L3 has to bring it under, the one on L1 stays out of it
    d1, d3 = FakeDevice(hass, "d1", 60.0), FakeDevice(hass, "d3", 60.0)
    d1.power_phase, d3.power_phase = Phase.l1, Phase.l3
    _, meters = await _hold(hass, entry, [d1, d3], {1: 0.0, 2: 0.0, 3: 1500.0})
    assert meters[3] <= 1000.0, f"L3 is still over its limit at {meters[3]:.0f} W"
    assert d3.power_setpoint > 0, "the device on L3 has to deliver"
    assert d1.power_setpoint <= 0, "L1 has room, so it takes back what L3 gives up, but it is never asked to deliver"


async def test_the_guard_wakes_a_fleet_that_is_doing_nothing(hass, entry) -> None:  # noqa: ANN001
    # the meter is balanced so the plan wants nothing; the guard still has to wake the device on the overloaded phase
    d1 = FakeDevice(hass, "d1", 60.0)
    d1.power_phase = Phase.l2
    _, meters = await _hold(hass, entry, [d1], {1: 0.0, 2: 1200.0, 3: 0.0})
    assert meters[2] <= 1000.0, f"idle plan, but L2 sits at {meters[2]:.0f} W"
    assert d1.power_setpoint > 0


async def test_the_guard_is_capped_by_what_the_device_can_deliver(hass, entry) -> None:  # noqa: ANN001
    d1 = FakeDevice(hass, "d1", 60.0, 800)
    d1.power_phase = Phase.l1
    _, meters = await _hold(hass, entry, [d1], {1: 3000.0, 2: 0.0, 3: 0.0})
    assert d1.power_setpoint == 800, "2000 W short, but the device is rated 800 W"
    assert meters[1] == 2200.0, "it gives what it has and the phase stays over its limit"


async def test_an_empty_pack_does_not_let_the_other_phases_charge_for_it(hass, entry) -> None:  # noqa: ANN001
    # the correction may only count on power that is really there, or the free phases charge for a delivery that never comes
    d1, d2 = FakeDevice(hass, "d1", 10.0), FakeDevice(hass, "d2", 60.0)
    d1.power_phase, d2.power_phase = Phase.l1, Phase.l2
    d1.soc_limit = 2  # the pack's own verdict: empty
    _, meters = await _hold(hass, entry, [d1, d2], {1: 3000.0, 2: 500.0, 3: 0.0})
    assert d1.power_setpoint == 0, "an empty pack cannot deliver"
    assert meters[2] == 500.0, f"L2 charged for a delivery that never came: {meters}"


async def test_the_guard_leaves_the_plan_alone_inside_the_limit(hass, entry) -> None:  # noqa: ANN001
    d1 = FakeDevice(hass, "d1", 60.0)
    d1.power_phase = Phase.l1
    dist = make_distribution(hass, entry, [d1])
    dist.phase_limit_w = 1000.0
    dist.power = [0.0, 800.0, 0.0, 0.0]
    await run(dist, 500, 1000.0, 10)
    assert 480 <= d1.power_setpoint <= 500, "the phase stays under its limit, so the plan decides"


async def test_phase_detection_follows_the_meter_that_moves_with_the_device(hass, entry, monkeypatch) -> None:  # noqa: ANN001
    # the button steps the device; the phase meter that carries the whole step is its own
    import custom_components.zendure_ha.distribution as distribution_module
    from custom_components.zendure_ha.const import PhaseModi
    from custom_components.zendure_ha.select import HaSelect

    class PhaseDevice(FakeDevice):
        def entity_create(self) -> None:
            HaSelect(self, "power_phase", PhaseModi)

    d1 = PhaseDevice(hass, "d1", 60.0)
    dist = make_distribution(hass, entry, [d1], DistributionMode.MANUAL)
    dist.power = [0.0, 1000.0, 2000.0, 3000.0]

    async def follow(_seconds: float) -> None:
        # charging lands on L2 alone; the other two phases stay where they are
        dist.power[2] = 2000.0 - d1.power_setpoint

    monkeypatch.setattr(distribution_module.asyncio, "sleep", follow)
    await dist.detect_phase(d1)
    assert d1.power_phase is Phase.l2


async def test_phase_detection_leaves_a_device_it_cannot_place(hass, entry, monkeypatch) -> None:  # noqa: ANN001
    # the step spreads over all three phases, so no meter carries it and the device keeps the phase it had
    import custom_components.zendure_ha.distribution as distribution_module
    from custom_components.zendure_ha.const import PhaseModi
    from custom_components.zendure_ha.select import HaSelect

    class PhaseDevice(FakeDevice):
        def entity_create(self) -> None:
            HaSelect(self, "power_phase", PhaseModi)

    d1 = PhaseDevice(hass, "d1", 60.0)
    dist = make_distribution(hass, entry, [d1], DistributionMode.MANUAL)
    dist.power = [0.0, 1000.0, 2000.0, 3000.0]

    async def spread(_seconds: float) -> None:
        for n in (1, 2, 3):
            dist.power[n] = 1000.0 * n - d1.power_setpoint / 3

    monkeypatch.setattr(distribution_module.asyncio, "sleep", spread)
    await dist.detect_phase(d1)
    assert d1.power_phase is Phase.all

async def test_bypass_solar_is_house_baseline_but_the_battery_part_is_not(hass, entry) -> None:  # noqa: ANN001
    # the device delivers 900 W of which 400 W is solar passing straight through; only the 500 W from the battery is the fleet's
    d1 = FakeDevice(hass, "d1", 60.0)
    d1.bypass, d1.solar, d1.power_actual = True, 400, 900
    dist = make_distribution(hass, entry, [d1], DistributionMode.OFF)
    await dist._process_p1(0, 1000.0)
    assert dist.entities["discharge_power"].state == 500


async def test_offgrid_load_is_added_back_to_the_fleet_contribution(hass, entry) -> None:  # noqa: ANN001
    # the socket draws 150 W through gridInputPower, so power_actual reads it as charging while the house never sees it
    d1 = FakeDevice(hass, "d1", 60.0)
    d1.power_actual, d1.grid_off_power = -150, 150
    dist = make_distribution(hass, entry, [d1], DistributionMode.OFF)
    await dist._process_p1(150, 1000.0)
    assert dist.entities["charge_power"].state == 0, "the socket is a house load, not the fleet charging"


async def test_negative_offgrid_is_an_inverter_feeding_the_socket(hass, entry) -> None:  # noqa: ANN001
    d1 = FakeDevice(hass, "d1", 60.0)
    d1.power_actual, d1.grid_off_power = 0, -200
    dist = make_distribution(hass, entry, [d1], DistributionMode.OFF)
    await dist._process_p1(500, 1000.0)
    assert dist.entities["charge_power"].state == 0, "a negative reading is solar coming in, never a load to cover"
